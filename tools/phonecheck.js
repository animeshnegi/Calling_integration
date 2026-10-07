/* Softphone regression harness.
 *
 *   npm install jsdom && node tools/phonecheck.js
 *
 * Boots the real web/phone.js in jsdom with a stub JsSIP in place of the CDN
 * build and a fake SIP session in place of the WebRTC stack, then walks the
 * contract the phone promises: SIP sign-in and auto-reconnect, the keypad and
 * paste, outbound calls (progress, answer, end), incoming answer/decline, the
 * in-call controls, recents (local and API-backed), contacts, settings and
 * logout.
 *
 * jsdom has no media engine, no dialog element implementation and no
 * clipboard, so those are stubbed; what the page asks the SIP stack to do is
 * recorded instead, which is the part a real browser cannot check for us.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { JSDOM, VirtualConsole } = require('jsdom');

const WEB = path.join(__dirname, '..', 'web');
const LOGO = 'https://engineerip.com/static/img/logo.png';

let passed = 0;
const failures = [];
let output = '';

function check(label, condition, detail) {
  if (condition) {
    passed += 1;
    output += `  PASS  ${label}\n`;
  } else {
    output += `  FAIL  ${label}${detail === undefined ? '' : ` — ${detail}`}\n`;
    failures.push(label);
  }
}

function section(title) {
  output += `\n=== ${title} ===\n`;
}

const settle = (ms = 30) => new Promise(resolve => setTimeout(resolve, ms));

/* A JsSIP stand-in: it records what the page asked it to do and lets the test
   play the other end of the call. */
function makeFakeSip() {
  const api = {};
  const sockets = [];
  const instances = [];
  const calls = [];

  function emitter(target) {
    target._handlers = {};
    target.on = function on(name, callback) {
      (this._handlers[name] = this._handlers[name] || []).push(callback);
      return this;
    };
    target.emit = function emit(name, ...args) {
      (this._handlers[name] || []).slice().forEach(callback => callback(...args));
      return this;
    };
    return target;
  }

  function makeSession({ direction = 'outgoing', caller = '' } = {}) {
    /* JsSIP exposes the RTCPeerConnection as session.connection, and the page
       reads its stats to report call quality. */
    const peer = { ontrack: null, _stats: null, async getStats() { return this._stats || []; } };
    const session = {
      direction,
      connection: peer,
      remote_identity: { uri: { user: caller, toString: () => `sip:${caller}@sip.example.com` } },
      _terminated: false, _ended: false, _answered: null, _muted: null, _held: false,
      terminate() { this._terminated = true; this._ended = true; this.emit('ended'); },
      isEnded() { return this._ended; },
      answer(options) { this._answered = options; this.emit('confirmed'); },
      mute(options) { this._muted = options; },
      hold() { this._held = true; this.emit('hold'); },
      unhold() { this._held = false; this.emit('unhold'); },
      /* The other end of the call, driven by the test. */
      ring() { this.emit('progress'); },
      connect() { this.emit('confirmed'); },
      fail(cause) { this._ended = true; this.emit('failed', { cause }); },
      sendConnection() { this.emit('peerconnection', { peerconnection: peer }); return peer; },
      /* What the browser would report for a negotiated codec and its network. */
      negotiates(codec, { received = 1000, lost = 0, jitter = 0 } = {}) {
        peer._stats = [
          { id: 'codec-1', type: 'codec', mimeType: `audio/${codec}` },
          { id: 'codec-dtmf', type: 'codec', mimeType: 'audio/telephone-event' },
          { id: 'in-1', type: 'inbound-rtp', kind: 'audio', codecId: 'codec-1', packetsReceived: received, packetsLost: lost, jitter },
          { id: 'out-1', type: 'outbound-rtp', kind: 'audio', codecId: 'codec-dtmf' },
        ];
      },
    };
    return emitter(session);
  }

  class WebSocketInterface {
    constructor(url) { this.url = url; sockets.push(url); }
  }

  class UA {
    constructor(config) {
      emitter(this);
      this.config = config;
      this.calls = [];
      this.started = false;
      this.stopped = false;
      instances.push(this);
    }

    start() {
      this.started = true;
      setTimeout(() => {
        if (this.stopped) return;
        if (String(this.config.password).startsWith('bad')) {
          this.emit('registrationFailed', { cause: 'Rejected' });
          return;
        }
        this.emit('connected');
        this.emit('registered');
      }, 0);
    }

    stop() { this.stopped = true; this.emit('disconnected'); }

    call(target, options) {
      const session = makeSession({ direction: 'outgoing' });
      this.calls.push({ target, options, session });
      calls.push({ target, options, session });
      return session;
    }

    /* What Asterisk would do: an INVITE arrives for this endpoint. */
    receive({ caller = '101' } = {}) {
      const session = makeSession({ direction: 'incoming', caller });
      this.emit('newRTCSession', { session, originator: 'remote' });
      return session;
    }

    drop() { this.emit('disconnected'); }
  }

  api.WebSocketInterface = WebSocketInterface;
  api.UA = UA;
  api._sockets = sockets;
  api._instances = instances;
  api._calls = calls;
  api._makeSession = makeSession;
  return { api, sockets, instances, calls };
}

/* The service worker, run in a stand-in ServiceWorkerGlobalScope: a fake
   cache, a fake network that records what it was asked for, and the ability to
   take the network down. */
function shellWorker({ offline = false } = {}) {
  const code = fs.readFileSync(path.join(WEB, 'sw.js'), 'utf8');
  const handlers = {};
  const entries = new Map();
  const network = [];
  /* The Cache API keys on absolute URLs: a worker that precaches "/phone"
     stores "https://phone.example.com/phone". */
  const absolute = value => new URL(String(value.url || value), 'https://phone.example.com').href;
  const makeResponse = url => ({ url: absolute(url), clone: () => makeResponse(url) });
  const cache = {
    addAll: async urls => urls.forEach(url => entries.set(absolute(url), makeResponse(url))),
    put: async (request, response) => entries.set(absolute(request), response),
    match: async request => entries.get(absolute(request)),
  };
  const sandbox = {
    self: {
      addEventListener: (name, handler) => { handlers[name] = handler; },
      skipWaiting: () => {},
      clients: { claim: async () => {} },
    },
    caches: { open: async () => cache, match: async request => cache.match(request) },
    location: { origin: 'https://phone.example.com' },
    URL,
    Response: { error: () => ({ error: true }) },
    Promise, Set, Map, Array, Object, String, Error, JSON, Number,
    fetch: async request => {
      const url = String(request.url || request);
      network.push(url);
      if (offline) throw new Error('offline');
      return makeResponse(url);
    },
  };
  vm.runInNewContext(code, sandbox);
  const run = async (name, event) => {
    let answered;
    handlers[name]({ ...event, waitUntil: promise => { answered = promise; } });
    if (answered) await answered;
  };
  return {
    install: () => run('install', {}),
    activate: () => run('activate', {}),
    fetch: async (url, { method = 'GET', mode = 'cors' } = {}) => {
      const request = { url, method, mode };
      let responded;
      handlers.fetch({ request, respondWith: promise => { responded = promise; } });
      return responded ? responded.then(value => value, error => { throw error; }) : undefined;
    },
    entries, network,
  };
}

/* The page as a browser receives it, with phone.js executed for real. */
function boot({ settings = null, recents = null, contacts = null, password = null,
                clipboard = '', prompts = [], fetchImpl = null } = {}) {
  const page = fs.readFileSync(path.join(WEB, 'phone.html'), 'utf8')
    .replace(/<script[^>]*jssip[^>]*><\/script>/i, '');
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error.message));
  virtualConsole.on('error', (...args) => errors.push(args.join(' ')));
  const sip = makeFakeSip();
  const dom = new JSDOM(page, {
    runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole,
    url: 'https://phone.example.com/phone',
    beforeParse(window) {
      window.JsSIP = sip.api;
      /* jsdom seams: media, clipboard, prompts and a Date the test can move. */
      window.HTMLMediaElement.prototype.play = () => Promise.resolve();
      Object.defineProperty(window.HTMLMediaElement.prototype, 'srcObject', {
        configurable: true,
        get() { return this._srcObject || null; },
        set(value) { this._srcObject = value; },
      });
      Object.defineProperty(window.navigator, 'clipboard', {
        configurable: true, value: { readText: async () => clipboard },
      });
      window.prompt = () => (prompts.length ? prompts.shift() : null);
      window.alert = () => {};
      window.fetch = fetchImpl || (async () => ({ ok: false, status: 404, json: async () => ({}) }));
      const realNow = window.Date.now.bind(window.Date);
      window.__offset = 0;
      window.Date.now = () => realNow() + window.__offset;
    },
  });
  const w = dom.window;
  const d = w.document;
  const dialog = d.getElementById('settings-dialog');
  dialog.showModal = function showModal() { this.setAttribute('open', ''); };
  dialog.close = function close() { this.removeAttribute('open'); };
  if (password) w.sessionStorage.setItem('eip-phone-sip-password', password);
  if (settings) w.localStorage.setItem('eip-phone-settings', JSON.stringify(settings));
  if (recents) w.localStorage.setItem('eip-phone-recents', JSON.stringify(recents));
  if (contacts) w.localStorage.setItem('eip-phone-contacts', JSON.stringify(contacts));
  const script = d.createElement('script');
  script.textContent = fs.readFileSync(path.join(WEB, 'phone.js'), 'utf8');
  d.body.appendChild(script);
  return { dom, w, d, errors, sip, dialog };
}

const shown = (d, id) => !d.getElementById(id).classList.contains('hidden');
const session = env => env.sip.calls[env.sip.calls.length - 1];

function typeDigits(d, digits) {
  for (const digit of digits) d.querySelector(`.keypad button[data-key="${digit}"]`).click();
}

async function signIn(env, { username = 'KUDGTE_101', password = 'sip-secret', domain = 'sip.engineerip.com', wss = '' } = {}) {
  const { d, w } = env;
  d.getElementById('login-username').value = username;
  d.getElementById('login-password').value = password;
  d.getElementById('login-domain').value = domain;
  d.getElementById('login-wss').value = wss;
  d.getElementById('login-form').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await settle(40);
}

async function hangUp(env) {
  env.d.getElementById('hangup-btn').click();
  await settle(20);
}

async function main() {
  /* ------------------------------------------------- the page itself */
  section('The page a browser receives');
  {
    const env = boot();
    const html = env.d.documentElement.outerHTML;
    check('the phone page names EngineerIP in its title',
      /EngineerIP Phone/.test(env.d.title), env.d.title);
    check('the favicon is the EngineerIP logo',
      new RegExp(`rel="icon"[^>]*${LOGO.replace(/[/.]/g, '\\$&')}`).test(html)
      || (env.d.querySelector('link[rel="icon"]') || {}).href === LOGO,
      (env.d.querySelector('link[rel="icon"]') || {}).href);
    check('an iOS device gets the same logo as an app icon',
      (env.d.querySelector('link[rel="apple-touch-icon"]') || {}).href === LOGO,
      (env.d.querySelector('link[rel="apple-touch-icon"]') || {}).href);
    check('the installable-app manifest is linked', /\/manifest\.json/.test(html));
    check('the SIP stack is loaded from the pinned CDN build',
      /jssip@3\.10\.1/.test(fs.readFileSync(path.join(WEB, 'phone.html'), 'utf8')));
    check('the in-call status line carries the class its stylesheet styles',
      env.d.getElementById('active-status').classList.contains('active-status'),
      env.d.getElementById('active-status').className);
    check('no runtime errors while the page boots', env.errors.length === 0, env.errors[0]);
    check('a first-time visitor sees the sign-in screen, not a broken phone',
      shown(env.d, 'login-screen') && !shown(env.d, 'phone-app'));
    env.dom.window.close();
  }

  /* --------------------------------------------------------- sign-in */
  section('SIP sign-in');
  {
    const env = boot();
    env.d.getElementById('login-username').value = 'KUDGTE_101';
    env.d.getElementById('login-form').dispatchEvent(new env.w.Event('submit', { bubbles: true, cancelable: true }));
    await settle(40);
    check('an empty password does not create a SIP connection', env.sip.instances.length === 0);

    const good = boot();
    await signIn(good);
    const ua = good.sip.instances[0];
    check('signing in registers the username with the SIP domain',
      ua && ua.config.uri === 'sip:KUDGTE_101@sip.engineerip.com', ua && ua.config.uri);
    check('the WebSocket transport is derived from the domain',
      good.sip.sockets[0] === 'wss://sip.engineerip.com/ws', good.sip.sockets[0]);
    check('the registration is started', ua.started === true);
    check('a registered phone shows the dialer', shown(good.d, 'phone-app') && !shown(good.d, 'login-screen'));
    check('the connection badge says the phone is ready',
      /Ready/.test(good.d.getElementById('connection-state').textContent)
      && good.d.getElementById('connection-state').classList.contains('online'),
      good.d.getElementById('connection-state').textContent + ' / ' + good.d.getElementById('connection-state').className);
    check('the sign-in screen confirms the session',
      /Authenticated and registered/.test(good.d.getElementById('login-status').textContent));
    check('the password stays in session storage, never in local storage',
      good.w.sessionStorage.getItem('eip-phone-sip-password') === 'sip-secret'
      && !/sip-secret/.test(good.w.localStorage.getItem('eip-phone-settings') || ''),
      good.w.localStorage.getItem('eip-phone-settings'));
    check('the username and domain are remembered for next time',
      JSON.parse(good.w.localStorage.getItem('eip-phone-settings')).username === 'KUDGTE_101');

    const scheme = boot();
    await signIn(scheme, { domain: 'https://sip.engineerip.com/', wss: 'wss://edge.engineerip.com:8089/ws' });
    check('a pasted domain loses its scheme and trailing slash',
      scheme.sip.instances[0].config.uri === 'sip:KUDGTE_101@sip.engineerip.com', scheme.sip.instances[0].config.uri);
    check('an explicit WebSocket server overrides the default',
      scheme.sip.sockets[0] === 'wss://edge.engineerip.com:8089/ws', scheme.sip.sockets[0]);

    const denied = boot();
    await signIn(denied, { password: 'bad-password' });
    check('a rejected password keeps the phone on the sign-in screen', !shown(denied.d, 'phone-app'));
    check('and says what happened instead of hanging',
      /Rejected/.test(denied.d.getElementById('login-status').textContent)
      && denied.d.getElementById('login-status').classList.contains('error'),
      denied.d.getElementById('login-status').textContent);
    check('the sign-in button becomes usable again',
      denied.d.getElementById('login-btn').disabled === false
      && /Connect/.test(denied.d.getElementById('login-btn').textContent),
      denied.d.getElementById('login-btn').textContent);
    check('the failed connection is stopped, not left retrying', denied.sip.instances[0].stopped === true);

    const lost = boot();
    await signIn(lost);
    lost.sip.instances[0].drop();
    check('a dropped WebSocket marks the phone offline',
      !lost.d.getElementById('connection-state').classList.contains('online'),
      lost.d.getElementById('connection-state').className);
  }

  /* ------------------------------------------------- keypad & paste */
  section('Keypad, paste and the dial display');
  {
    const env = boot();
    await signIn(env);
    const { d } = env;
    typeDigits(d, '1555');
    check('keypad presses fill the number field', d.getElementById('number-input').value === '1555');
    check('and the display follows what was typed', d.getElementById('dial-display').textContent === '1555');
    check('the hint says the number is ready to call', /Ready to call/.test(d.getElementById('dial-hint').textContent));
    d.getElementById('backspace-btn').click();
    check('delete removes one digit', d.getElementById('number-input').value === '155');
    d.getElementById('clear-btn').click();
    check('clear empties the field and the display',
      d.getElementById('number-input').value === '' && d.getElementById('dial-display').textContent === 'Enter number');

    typeDigits(d, '*#0');
    check('the keypad also reaches the star, hash and zero keys',
      d.getElementById('number-input').value === '*#0', d.getElementById('number-input').value);
    check('the zero key advertises the international plus',
      /\+/.test(d.querySelector('.keypad button[data-key="0"] small').textContent));
    d.getElementById('clear-btn').click();

    const pasted = boot({ clipboard: '+1 (555) 512-3456' });
    await signIn(pasted);
    pasted.d.getElementById('paste-btn').click();
    await settle(20);
    check('pasting a formatted number keeps only the dialable characters',
      pasted.d.getElementById('number-input').value === '+15555123456',
      pasted.d.getElementById('number-input').value);

    const typedPaste = boot();
    await signIn(typedPaste);
    const field = typedPaste.d.getElementById('number-input');
    field.value = '+1 555 512-3456';
    field.dispatchEvent(new typedPaste.w.Event('paste', { bubbles: true }));
    await settle(20);
    check('pasting into the field itself is cleaned the same way', field.value === '+15555123456', field.value);
  }

  /* ----------------------------------------------------- placing a call */
  section('Placing a call');
  {
    const env = boot();
    await signIn(env);
    const { d } = env;
    typeDigits(d, '15555123456');
    d.getElementById('call-btn').click();
    await settle(20);

    const placed = session(env);
    check('the call is placed to the dialed number at the SIP domain',
      placed && placed.target === 'sip:15555123456@sip.engineerip.com', placed && placed.target);
    check('audio-only media is negotiated',
      placed.options.mediaConstraints.video === false && placed.options.mediaConstraints.audio !== false);
    check("the microphone is captured with the browser's own call processing",
      placed.options.mediaConstraints.audio.echoCancellation === true
      && placed.options.mediaConstraints.audio.noiseSuppression === true
      && placed.options.mediaConstraints.audio.autoGainControl === true,
      JSON.stringify(placed.options.mediaConstraints.audio));
    check('the active-call screen opens over the dialer',
      !d.getElementById('active-call-view').classList.contains('hidden')
      && d.getElementById('active-number').textContent === '15555123456');
    check('the call button is disabled while a call is up', d.getElementById('call-btn').disabled === true);
    check('the status starts at connecting', /Connecting/.test(d.getElementById('active-status').textContent));

    placed.session.ring();
    check('ringing is reported while the far end rings', /Ringing/.test(d.getElementById('active-status').textContent));

    const peer = placed.session.sendConnection();
    const stream = { id: 'remote-stream' };
    check('the page listens for the remote audio track', typeof peer.ontrack === 'function');
    peer.ontrack({ streams: [stream] });
    check('the remote stream is attached to the audio element',
      d.getElementById('remote-audio').srcObject === stream);

    placed.session.connect();
    check('answering is reported as connected', /Connected/.test(d.getElementById('active-status').textContent));
    const before = d.getElementById('call-timer').textContent;
    env.w.__offset += 65000;
    await settle(600);
    check('the call timer counts from the moment the call connects',
      d.getElementById('call-timer').textContent === '01:05' && before === '00:00',
      `${before} -> ${d.getElementById('call-timer').textContent}`);

    d.getElementById('mute-btn').click();
    check('mute silences the microphone and lights the button',
      JSON.stringify(placed.session._muted) === '{"audio":true}'
      && d.getElementById('mute-btn').classList.contains('active'), JSON.stringify(placed.session._muted));
    d.getElementById('mute-btn').click();
    check('unmute turns the microphone back on',
      JSON.stringify(placed.session._muted) === '{"audio":false}'
      && !d.getElementById('mute-btn').classList.contains('active'));
    d.getElementById('hold-btn').click();
    check('hold holds the far end', placed.session._held === true && d.getElementById('hold-btn').classList.contains('active'));
    d.getElementById('hold-btn').click();
    check('unhold releases it', placed.session._held === false);
    d.getElementById('speaker-btn').click();
    check('speaker mutes the earpiece output where the browser has no output routing',
      d.getElementById('remote-audio').muted === true && d.getElementById('speaker-btn').classList.contains('active'));
    d.getElementById('speaker-btn').click();
    check('and unmutes it again', d.getElementById('remote-audio').muted === false);
    d.getElementById('mute-btn').click();
    d.getElementById('hold-btn').click();
    d.getElementById('speaker-btn').click();

    await hangUp(env);
    check('hanging up terminates the SIP session', placed.session._terminated === true);
    check('the dialer comes back after the call',
      d.getElementById('active-call-view').classList.contains('hidden')
      && d.getElementById('call-btn').disabled === false);
    const recents = JSON.parse(env.w.localStorage.getItem('eip-phone-recents'));
    check('the finished call is written to recents once, not once per event',
      recents.length === 1 && recents[0].number === '15555123456'
      && recents[0].direction === 'outgoing' && recents[0].status === 'completed',
      JSON.stringify(recents));

    env.d.getElementById('clear-btn').click();
    typeDigits(env.d, '15559998888');
    env.d.getElementById('call-btn').click();
    await settle(20);
    check('the next call starts unmuted, unheld and on the earpiece',
      !env.d.getElementById('mute-btn').classList.contains('active')
      && !env.d.getElementById('hold-btn').classList.contains('active')
      && !env.d.getElementById('speaker-btn').classList.contains('active')
      && env.d.getElementById('remote-audio').muted === false
      && session(env).session._muted === null);
    await hangUp(env);
    check('and that call is filed under its own number',
      JSON.parse(env.w.localStorage.getItem('eip-phone-recents'))[0].number === '15559998888');
    env.dom.window.close();

    const rerouted = boot();
    rerouted.w.HTMLMediaElement.prototype.setSinkId = function (device) { this._sink = device; return Promise.resolve(); };
    await signIn(rerouted);
    typeDigits(rerouted.d, '15550001111');
    rerouted.d.getElementById('call-btn').click();
    await settle(20);
    rerouted.d.getElementById('speaker-btn').click();
    await settle(20);
    check('a browser that can route audio is asked for the loudspeaker',
      rerouted.d.getElementById('remote-audio')._sink === 'speaker',
      rerouted.d.getElementById('remote-audio')._sink);
    check('and the earpiece is left audible while it does',
      rerouted.d.getElementById('remote-audio').muted === false);
    rerouted.d.getElementById('speaker-btn').click();
    await settle(20);
    check('turning speaker off goes back to the default output',
      rerouted.d.getElementById('remote-audio')._sink === 'default');
    rerouted.dom.window.close();

    const failing = boot();
    await signIn(failing);
    typeDigits(failing.d, '15550000000');
    failing.d.getElementById('call-btn').click();
    await settle(20);
    session(failing).session.fail('Busy Here');
    check('a refused call says why',
      /Call failed: Busy Here/.test(failing.d.getElementById('active-status').textContent),
      failing.d.getElementById('active-status').textContent);
    const failed = JSON.parse(failing.w.localStorage.getItem('eip-phone-recents'));
    check('and is recorded as failed', failed[0].status === 'failed', JSON.stringify(failed));
  }

  /* ---------------------------------------------------- incoming calls */
  section('Incoming calls');
  {
    const env = boot();
    await signIn(env);
    const { d } = env;
    const incoming = env.sip.instances[0].receive({ caller: '101' });
    await settle(20);
    check('an incoming call shows who is ringing',
      d.getElementById('active-number').textContent === '101'
      && d.getElementById('active-label').textContent === 'Incoming call');
    check('the caller is offered answer and decline, not just hangup',
      shown(d, 'incoming-actions') && !shown(d, 'hangup-btn'));
    check('the incoming call is not timed yet', d.getElementById('call-timer').textContent === '00:00');

    d.getElementById('answer-btn').click();
    await settle(20);
    check('answering accepts the call with the same audio-only media',
      incoming._answered && incoming._answered.mediaConstraints.video === false
      && incoming._answered.mediaConstraints.audio !== false);
    check('the in-call controls replace the answer buttons',
      !shown(d, 'incoming-actions') && shown(d, 'hangup-btn')
      && d.getElementById('active-label').textContent === 'Call');
    await hangUp(env);
    check('the answered call is recorded as an incoming call',
      JSON.parse(env.w.localStorage.getItem('eip-phone-recents'))[0].direction === 'incoming');

    const declined = boot();
    await signIn(declined);
    const ringing = declined.sip.instances[0].receive({ caller: '102' });
    await settle(20);
    declined.d.getElementById('decline-btn').click();
    await settle(20);
    check('declining ends the ringing call', ringing._terminated === true);
    check('and closes the active-call screen', declined.d.getElementById('active-call-view').classList.contains('hidden'));
    check('a declined call is not filed as a completed call',
      JSON.parse(declined.w.localStorage.getItem('eip-phone-recents'))[0].status === 'declined',
      JSON.stringify(JSON.parse(declined.w.localStorage.getItem('eip-phone-recents'))[0]));

    const busy = boot();
    await signIn(busy);
    const first = busy.sip.instances[0].receive({ caller: '101' });
    await settle(20);
    busy.d.getElementById('answer-btn').click();
    await settle(20);
    const second = busy.sip.instances[0].receive({ caller: '102' });
    await settle(20);
    check('a second caller while busy is turned away', second._terminated === true);
    check('and the active call is untouched',
      busy.d.getElementById('active-number').textContent === '101' && first._terminated === false);
    await hangUp(busy);

    const missed = boot();
    await signIn(missed);
    const gaveUp = missed.sip.instances[0].receive({ caller: '103' });
    await settle(20);
    gaveUp.terminate();
    await settle(20);
    check('a caller who gives up before the phone is answered is filed as missed',
      JSON.parse(missed.w.localStorage.getItem('eip-phone-recents'))[0].status === 'missed',
      JSON.stringify(JSON.parse(missed.w.localStorage.getItem('eip-phone-recents'))[0]));
    check('and the phone is usable again afterwards',
      missed.d.getElementById('active-call-view').classList.contains('hidden')
      && missed.d.getElementById('call-btn').disabled === false);
  }

  /* --------------------------------------------------------- HD audio */
  section('HD audio and the quality badge');
  {
    const hd = boot();
    await signIn(hd);
    typeDigits(hd.d, '15551230001');
    hd.d.getElementById('call-btn').click();
    await settle(20);
    const hdSession = session(hd);
    check('nothing claims HD before the call connects',
      hd.d.getElementById('call-quality').classList.contains('hidden'),
      hd.d.getElementById('call-quality').textContent);
    hdSession.session.connect();
    await settle(30);
    check('the badge stays quiet while the browser has no stats yet',
      hd.d.getElementById('call-quality').classList.contains('hidden'));
    hdSession.session.negotiates('G722');
    await settle(2100);
    const badge = hd.d.getElementById('call-quality');
    check('a wideband codec is reported as HD, by name',
      /HD/.test(badge.textContent) && /G722/.test(badge.textContent), badge.textContent);
    check('and it is marked as a good call',
      badge.classList.contains('good') && !badge.classList.contains('warn'));
    await hangUp(hd);
    check('the badge goes away with the call', badge.classList.contains('hidden'));
    hd.dom.window.close();

    const narrow = boot();
    await signIn(narrow);
    typeDigits(narrow.d, '15551230002');
    narrow.d.getElementById('call-btn').click();
    await settle(20);
    session(narrow).session.connect();
    session(narrow).session.negotiates('PCMU');
    await settle(2100);
    check('a narrowband codec is reported as a standard call, not as HD',
      /Standard/.test(narrow.d.getElementById('call-quality').textContent)
      && !/HD/.test(narrow.d.getElementById('call-quality').textContent),
      narrow.d.getElementById('call-quality').textContent);

    session(narrow).session.negotiates('G722', { received: 900, lost: 120, jitter: 0.06 });
    await settle(2100);
    check('a lossy network is called out even on a wideband codec',
      /HD/.test(narrow.d.getElementById('call-quality').textContent)
      && /unstable network/.test(narrow.d.getElementById('call-quality').textContent)
      && narrow.d.getElementById('call-quality').classList.contains('warn'),
      narrow.d.getElementById('call-quality').textContent);
    narrow.dom.window.close();

    const dtmf = boot();
    await signIn(dtmf);
    typeDigits(dtmf.d, '15551230003');
    dtmf.d.getElementById('call-btn').click();
    await settle(20);
    session(dtmf).session.connect();
    session(dtmf).session.negotiates('telephone-event');
    await settle(2100);
    check('the DTMF codec is never mistaken for the audio codec',
      dtmf.d.getElementById('call-quality').classList.contains('hidden'),
      dtmf.d.getElementById('call-quality').textContent);
  }

  /* ------------------------------------------ dialling with a real keyboard */
  section('Dialling from a physical keyboard');
  {
    const env = boot();
    await signIn(env);
    const { d, w } = env;
    const press = key => d.dispatchEvent(new w.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
    press('1'); press('8'); press('0'); press('0');
    check('typing digits outside a field fills the dial display',
      d.getElementById('number-input').value === '1800', d.getElementById('number-input').value);
    press('Backspace');
    check('backspace deletes a digit', d.getElementById('number-input').value === '180');
    press('*'); press('#');
    check('the star and hash keys work from the keyboard too',
      d.getElementById('number-input').value === '180*#', d.getElementById('number-input').value);
    press('Enter');
    await settle(20);
    check('Enter places the call',
      Boolean(session(env)) && session(env).target === 'sip:180*#@sip.engineerip.com',
      session(env) && session(env).target);
    press('Escape');
    await settle(20);
    check('Escape hangs the call up',
      session(env).session._terminated === true
      && env.d.getElementById('active-call-view').classList.contains('hidden'));
    env.dom.window.close();

    const focused = boot();
    await signIn(focused);
    focused.d.getElementById('contacts-btn').focus();
    focused.d.dispatchEvent(new focused.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    await settle(20);
    check('Enter on a focused button keeps the button, it does not dial',
      focused.sip.calls.length === 0, `${focused.sip.calls.length} call(s)`);
    focused.dom.window.close();

    const login = boot();
    login.d.getElementById('login-username').focus();
    login.d.getElementById('login-username').dispatchEvent(
      new login.w.KeyboardEvent('keydown', { key: '5', bubbles: true, cancelable: true }));
    check('typing in the sign-in form never dials',
      login.d.getElementById('number-input').value === '' && login.sip.instances.length === 0,
      login.d.getElementById('number-input').value);

    const dialog = boot();
    await signIn(dialog);
    dialog.d.getElementById('settings-btn').click();
    dialog.d.getElementById('sip-username').focus();
    dialog.d.dispatchEvent(new dialog.w.KeyboardEvent('keydown', { key: '7', bubbles: true, cancelable: true }));
    check('and neither does typing in the settings dialog',
      dialog.d.getElementById('number-input').value === '', dialog.d.getElementById('number-input').value);
  }

  /* ------------------------------------------- the phone's layout contract */
  section('The layout contract, phone and browser');
  {
    const css = fs.readFileSync(path.join(WEB, 'phone.css'), 'utf8');
    const html = fs.readFileSync(path.join(WEB, 'phone.html'), 'utf8');
    const script = fs.readFileSync(path.join(WEB, 'phone.js'), 'utf8');
    const used = new Set();
    [...html.matchAll(/class="([^"]+)"/g)].forEach(m => m[1].split(/\s+/).forEach(name => used.add(name)));
    [...script.matchAll(/classList\.(?:toggle|add|remove)\("([^"]+)"/g)].forEach(m => used.add(m[1]));
    const styled = new Set([...css.matchAll(/\.([A-Za-z][\w-]*)/g)].map(m => m[1]));
    const unstyled = [...used].filter(name => !styled.has(name));
    check('every class the phone applies has a rule', unstyled.length === 0, unstyled.join(', '));

    const view = (css.match(/\.view\{[^}]*\}/) || [''])[0];
    check('the dialer view scrolls instead of clipping the keypad on a short window',
      /overflow-y:auto/.test(view), view);
    check('the in-call screen scrolls too', /\.active-call\{[^}]*overflow-y:auto/.test(css));
    check('short windows get their own layout instead of a scrollbar',
      /@media\(max-height:760px\)/.test(css));
    check('keyboard users get a visible focus ring', /:focus-visible\{[^}]*outline:2px solid/.test(css));
    check('the keyboard hint only appears where there is a keyboard',
      /\.dial-tip\{display:none/.test(css) && /@media\(min-width:700px\)\{\.dial-tip\{display:block\}/.test(css));
    check('the phone layout keeps its small-screen rules and safe areas',
      /@media\(max-width:520px\)/.test(css) && /env\(safe-area-inset-bottom\)/.test(css)
      && /env\(safe-area-inset-top\)/.test(css));
    const keyHeights = [...css.matchAll(/\.keypad button\{[^}]*height:(\d+)px/g)].map(m => Number(m[1]));
    check('every keypad layout keeps a finger-sized target',
      keyHeights.length >= 2 && keyHeights.every(height => height >= 44), keyHeights.join(', '));
    const cards = [...css.matchAll(/min-height:min\((\d+)px/g)].map(m => Number(m[1]));
    check('the card is phone-shaped and never taller than the window',
      cards.length > 0 && cards.every(width => width <= 900), cards.join(', '));
  }

  /* ----------------------------------------------------------- recents */
  section('Recents');
  {
    const env = boot({
      recents: [
        { number: '+13025550123', direction: 'incoming', status: 'completed', at: Date.parse('2026-10-06T10:20:00Z') },
        { number: '+13025550999', direction: 'outgoing', status: 'failed', at: Date.parse('2026-10-06T11:45:00Z') },
      ],
    });
    await signIn(env);
    env.d.querySelector('.nav-item[data-view="recents-view"]').click();
    await settle(20);
    const rows = [...env.d.querySelectorAll('#recents-list .list-item')];
    check('the recents view lists the stored calls', rows.length === 2, `${rows.length} rows`);
    check('each row shows the number and its outcome',
      /\+13025550123/.test(rows[0].textContent) && /completed/.test(rows[0].textContent)
      && /failed/.test(rows[1].textContent));
    check('an incoming call is marked as such',
      rows[0].querySelector('.avatar').textContent === '↓'
      && rows[1].querySelector('.avatar').textContent === '↑');
    env.d.querySelector('#recents-list .quick-call').click();
    await settle(20);
    check('calling back from recents dials that number',
      session(env) && session(env).target === 'sip:+13025550123@sip.engineerip.com',
      session(env) && session(env).target);
    env.dom.window.close();

    const api = boot({
      settings: { username: 'KUDGTE_101', domain: 'sip.engineerip.com', wss: 'wss://sip.engineerip.com/ws', apiToken: 'token-123' },
      recents: [],
      fetchImpl: async (url, options) => {
        api.seen = { url, headers: options && options.headers };
        return {
          ok: true, status: 200,
          json: async () => ({
            calls: [
              { call_id: 'abc', phone: '+13025550001', status: 'answered', direction: 'outgoing', started_at: '2026-10-06T09:00:00Z' },
              { call_id: 'def', phone: '<img src=x onerror="window.__pwned=1">', status: 'no answer', direction: 'incoming', started_at: '2026-10-06T09:30:00Z' },
            ],
          }),
        };
      },
    });
    await signIn(api);
    api.d.getElementById('refresh-recents').click();
    await settle(40);
    const apiRows = [...api.d.querySelectorAll('#recents-list .list-item')];
    check('the API token is sent as a bearer token to the calls endpoint',
      api.seen && api.seen.url === '/api/v1/calls?limit=50' && api.seen.headers.Authorization === 'Bearer token-123',
      JSON.stringify(api.seen));
    check('server call history replaces the local list', apiRows.length === 2, `${apiRows.length} rows`);
    check('server rows name the other party', /\+13025550001/.test(apiRows[0].textContent), apiRows[0].textContent);
    check('a hostile number in the API payload is escaped, not executed',
      apiRows[1].querySelector('img') === null && api.w.__pwned === undefined,
      apiRows[1].innerHTML);
  }

  /* ---------------------------------------------------------- contacts */
  section('Contacts');
  {
    const env = boot({ prompts: ['Dana Whitfield', '+13025550188', 'Eve "the boss" <img src=x>', '+13025550199'] });
    await signIn(env);
    const { d } = env;
    env.d.getElementById('contacts-btn').click();
    check('the dialer offers a way into the contact list', d.getElementById('contacts-view').classList.contains('active'));
    check('an empty address book says so', !d.getElementById('contacts-empty').classList.contains('hidden'));

    d.getElementById('add-contact').click();
    check('adding a contact stores it', JSON.parse(env.w.localStorage.getItem('eip-phone-contacts')).length === 1);
    check('and the new contact appears in the list',
      /Dana Whitfield/.test(d.getElementById('contacts-list').textContent) && /\+13025550188/.test(d.getElementById('contacts-list').textContent));
    check('the empty message is retired once a contact exists',
      d.getElementById('contacts-empty').classList.contains('hidden'));

    d.getElementById('add-contact').click();
    check('a contact name is escaped rather than stored as markup',
      d.getElementById('contacts-list').querySelectorAll('img').length === 0
      && /Eve/.test(d.getElementById('contacts-list').textContent));

    const first = d.querySelector('#contacts-list .quick-call');
    first.click();
    await settle(20);
    check('calling a contact dials its number from the dialer',
      d.getElementById('dialer-view').classList.contains('active')
      && session(env) && session(env).target === 'sip:+13025550188@sip.engineerip.com',
      session(env) && session(env).target);
    await hangUp(env);

    const saved = boot({ contacts: [{ name: 'Dana Whitfield', phone: '+13025550188' }] });
    await signIn(saved);
    check('contacts survive a reload',
      /Dana Whitfield/.test(saved.d.getElementById('contacts-list').textContent));
  }

  /* -------------------------------------------------- settings & logout */
  section('Settings, reconnect and logout');
  {
    const env = boot();
    await signIn(env);
    const { d } = env;
    d.getElementById('settings-btn').click();
    check('settings open as a dialog', d.getElementById('settings-dialog').hasAttribute('open'));
    check('the dialog is prefilled with the signed-in account',
      d.getElementById('sip-username').value === 'KUDGTE_101'
      && d.getElementById('sip-domain').value === 'sip.engineerip.com'
      && d.getElementById('sip-wss').value === 'wss://sip.engineerip.com/ws',
      d.getElementById('sip-wss').value);

    d.getElementById('sip-wss').value = 'wss://edge.engineerip.com/ws';
    d.getElementById('sip-extension').value = '101';
    d.getElementById('connect-btn').click();
    await settle(40);
    check('reconnecting drops the old registration and uses the new server',
      env.sip.instances.length === 2 && env.sip.instances[0].stopped === true
      && env.sip.sockets[1] === 'wss://edge.engineerip.com/ws',
      env.sip.sockets.join(', '));
    check('the dialog reports the reconnected phone',
      /Phone connected/.test(d.getElementById('settings-status').textContent),
      d.getElementById('settings-status').textContent);
    check('the extension is remembered with the account',
      JSON.parse(env.w.localStorage.getItem('eip-phone-settings')).extension === '101');

    d.getElementById('logout-btn').click();
    await settle(20);
    check('logging out returns to the sign-in screen',
      shown(d, 'login-screen') && !shown(d, 'phone-app') && !d.getElementById('settings-dialog').hasAttribute('open'));
    check('the stored password is dropped', env.w.sessionStorage.getItem('eip-phone-sip-password') === null);
    check('the stored account is dropped', env.w.localStorage.getItem('eip-phone-settings') === null);
    check('the registration is stopped', env.sip.instances[1].stopped === true);
    env.dom.window.close();

    const token = boot();
    await signIn(token);
    token.d.getElementById('settings-btn').click();
    token.d.getElementById('api-token').value = 'token-123';
    token.d.getElementById('connect-btn').click();
    await settle(40);
    check('an API token is saved outside the SIP secrets',
      JSON.parse(token.w.localStorage.getItem('eip-phone-settings')).apiToken === 'token-123');
  }

  /* ------------------------------------------------------ auto-reconnect */
  section('Returning to an open tab');
  {
    const env = boot({
      settings: { username: 'KUDGTE_101', domain: 'sip.engineerip.com', wss: 'wss://sip.engineerip.com/ws' },
      password: 'sip-secret',
    });
    check('a reload with a live session says it is reconnecting',
      /Reconnecting/.test(env.d.getElementById('login-status').textContent),
      env.d.getElementById('login-status').textContent);
    await settle(40);
    check('and signs back in without asking for the password again',
      shown(env.d, 'phone-app') && /Ready/.test(env.d.getElementById('connection-state').textContent));
    env.dom.window.close();

    const expired = boot({
      settings: { username: 'KUDGTE_101', domain: 'sip.engineerip.com', wss: 'wss://sip.engineerip.com/ws' },
      password: 'bad-password',
    });
    await settle(60);
    check('a rejected stored session falls back to the sign-in screen',
      shown(expired.d, 'login-screen') && /sign in again/i.test(expired.d.getElementById('login-status').textContent),
      expired.d.getElementById('login-status').textContent);
    check('and the stale password field is cleared',
      expired.d.getElementById('login-password').value === '');
    check('the page still reports no runtime errors on this path', expired.errors.length === 0, expired.errors[0]);
  }

  /* ------------------------------------------------ the offline shell */
  section('The offline shell (service worker)');
  {
    const worker = shellWorker();
    await worker.install();
    check('installing precaches the phone shell',
      ['/phone', '/phone.css', '/phone.js', '/manifest.json']
        .every(url => worker.entries.has(`https://phone.example.com${url}`)),
      [...worker.entries.keys()].join(', '));
    check('and reaches for nothing outside this site', worker.network.length === 0, worker.network.join(', '));
    await worker.activate();

    const cached = await worker.fetch('https://phone.example.com/phone');
    check('the shell is served from the cache',
      cached && cached.url === 'https://phone.example.com/phone', cached && cached.url);
    check('without going to the network', worker.network.length === 0, worker.network.join(', '));

    const fresh = await worker.fetch('https://phone.example.com/phone.js');
    check('a cached asset is answered too',
      fresh && fresh.url === 'https://phone.example.com/phone.js', fresh && fresh.url);

    const state = await worker.fetch('https://phone.example.com/admin/api/state');
    check('the console API is left to the network, never cached', state === undefined);
    const consolePage = await worker.fetch('https://phone.example.com/admin', { mode: 'navigate' });
    check('and so is the console page itself', consolePage === undefined);
    const api = await worker.fetch('https://phone.example.com/api/v1/calls?limit=50');
    check('nor does call history pass through the cache', api === undefined);
    const cdn = await worker.fetch('https://cdn.jsdelivr.net/npm/jssip@3.10.1/dist/jssip.min.js');
    check('the SIP library from the CDN is untouched', cdn === undefined);
    const post = await worker.fetch('https://phone.example.com/phone', { method: 'POST' });
    check('a non-GET request is never answered from the shell', post === undefined);
    check('nothing else was written to the cache',
      [...worker.entries.keys()].sort().join(', ') ===
        ['/manifest.json', '/phone', '/phone.css', '/phone.js']
          .map(url => `https://phone.example.com${url}`).join(', '),
      [...worker.entries.keys()].join(', '));

    const offlineWorker = shellWorker({ offline: true });
    await offlineWorker.install();
    await offlineWorker.activate();
    const reopened = await offlineWorker.fetch('https://phone.example.com/phone', { mode: 'navigate' });
    check('offline, opening the phone still works from the cache',
      reopened && reopened.url === 'https://phone.example.com/phone', reopened && reopened.url);

    const shipped = fs.readFileSync(path.join(WEB, 'sw.js'), 'utf8');
    check('a release can replace the shell by bumping the cache name',
      /const CACHE = "eip-phone-v\d+";/.test(shipped));
  }

  output += `\n${failures.length ? `${failures.length} CHECK(S) FAILED\n${failures.map(f => `  - ${f}`).join('\n')}\n` : 'ALL CHECKS PASSED'}\n`;
  output += `${passed} checks passed\n`;
  process.stdout.write(output);
  process.exit(failures.length ? 1 : 0);
}

main().catch(error => {
  process.stdout.write(`${output}\nHARNESS ERROR: ${error && error.stack}\n`);
  process.exit(1);
});
