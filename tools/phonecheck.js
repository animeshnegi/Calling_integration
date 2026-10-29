/* Softphone regression harness.
 *
 *   npm install jsdom && node tools/phonecheck.js
 *
 * Boots the real web/phone.js in jsdom with a stub JsSIP in place of the
 * vendored build and a fake SIP session in place of the WebRTC stack, then
 * walks the contract the phone promises: SIP sign-in and auto-reconnect, the
 * keypad and paste, outbound calls (progress, answer, end), incoming
 * answer/decline, the in-call controls, recents (local and API-backed),
 * contacts, settings and logout.
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
const assetVersion = () => {
  const code = fs.readFileSync(path.join(WEB, 'sw.js'), 'utf8');
  const match = code.match(/const VERSION = "(\d+)"/);
  return match ? match[1] : '';
};
const VERSION = assetVersion();
const VERSIONED_CSS = `/phone.css?v=${VERSION}`;
const VERSIONED_JS = `/phone.js?v=${VERSION}`;
const SHELL_URLS = ['/phone', VERSIONED_CSS, VERSIONED_JS, `/phone-hold.wav?v=${VERSION}`, '/jssip.min.js', '/manifest.json'];

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
    const listeners = {};
    const peer = {
      ontrack: null, _stats: null, _receivers: [], iceConnectionState: 'new', listeners,
      addEventListener(name, callback) { (listeners[name] = listeners[name] || []).push(callback); },
      dispatch(name, event) { (listeners[name] || []).slice().forEach(callback => callback(event)); },
      track(event) {
        this.dispatch('track', event);
        if (this.ontrack && !(event && event.__eipHandled)) this.ontrack(event);
        return event;
      },
      getReceivers() { return this._receivers; },
      async getStats() { return this._stats || []; },
    };
    const session = {
      direction,
      connection: peer,
      remote_identity: { uri: { user: caller, toString: () => `sip:${caller}@sip.example.com` } },
      _terminated: false, _ended: false, _answered: null, _muted: null, _held: false, _dtmf: [],
      _connectionSent: false,
      terminate() { this._terminated = true; this._ended = true; this.emit('ended'); },
      isEnded() { return this._ended; },
      /* JsSIP creates the peer synchronously, but acceptance follows ICE
         gathering and the SIP response; answer() itself does not confirm. */
      answer(options) { this._answered = options; this.sendConnection(); },
      mute(options) { this._muted = options; },
      unmute(options) { this._muted = { audio: false, unmutedWith: options }; },
      hold(options, done) { this._held = true; this.emit('hold', { originator: 'local' }); if (done) done(); return true; },
      unhold(options, done) { this._held = false; this.emit('unhold', { originator: 'local' }); if (done) done(); return true; },
      sendDTMF(tone) { this._dtmf.push(String(tone)); },
      /* The other end of the call, driven by the test. */
      ring(code = 180, body = '') { this.emit('progress', { originator: 'remote', response: { status_code: code, body } }); },
      connect() { peer.iceConnectionState = 'connected'; this.emit('confirmed'); },
      fail(cause) { this._ended = true; this.emit('failed', { cause }); },
      sendConnection() {
        if (!this._connectionSent) {
          this._connectionSent = true;
          this.emit('peerconnection', { peerconnection: peer });
        }
        return peer;
      },
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
      this._connected = false;
      this._registered = false;
      this.registers = 0;
      instances.push(this);
    }

    start() {
      this.started = true;
      this.stopped = false;
      setTimeout(() => {
        if (this.stopped) return;
        if (String(this.config.password).startsWith('bad')) {
          this.emit('registrationFailed', { cause: 'Rejected' });
          return;
        }
        this._connected = true;
        this._registered = true;
        this.emit('connected');
        this.emit('registered');
      }, 0);
    }

    stop() { this.stopped = true; this._connected = false; this._registered = false; this.emit('disconnected'); }
    isConnected() { return this._connected; }
    isRegistered() { return this._registered; }
    register() { this.registers += 1; this._registered = true; this.emit('registered'); }

    call(target, options) {
      const session = makeSession({ direction: 'outgoing' });
      /* This is the regression: the real stack emits "peerconnection"
         synchronously inside ua.call(), before phone.js can attachSession. */
      session.sendConnection();
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

    drop() { this._connected = false; this._registered = false; this.emit('disconnected'); }
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
  /* Named caches, the way a browser keeps them: a release installs into its
     own cache and activation deletes the older ones. */
  const stores = new Map();
  const network = [];
  /* The Cache API keys on absolute URLs: a worker that precaches "/phone"
     stores "https://phone.example.com/phone". */
  const absolute = value => new URL(String(value.url || value), 'https://phone.example.com').href;
  const makeResponse = url => ({ ok: true, url: absolute(url), clone: () => makeResponse(url) });
  const namedCache = store => ({
    addAll: async urls => urls.forEach(url => store.set(absolute(url), makeResponse(url))),
    put: async (request, response) => store.set(absolute(request), response),
    match: async request => store.get(absolute(request)),
  });
  const cacheStorage = {
    open: async name => {
      if (!stores.has(name)) stores.set(name, new Map());
      return namedCache(stores.get(name));
    },
    match: async request => {
      for (const store of stores.values()) {
        const hit = store.get(absolute(request));
        if (hit) return hit;
      }
      return undefined;
    },
    keys: async () => [...stores.keys()],
    delete: async name => stores.delete(name),
  };
  const sandbox = {
    self: {
      addEventListener: (name, handler) => { handlers[name] = handler; },
      skipWaiting: () => {},
      clients: { claim: async () => {} },
    },
    caches: cacheStorage,
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
    /* Every cache together, as the checks want to see what was written. */
    get entries() {
      const combined = new Map();
      for (const store of stores.values()) for (const [key, value] of store) combined.set(key, value);
      return combined;
    },
    network,
    caches: cacheStorage,
  };
}

/* The page as a browser receives it, with phone.js executed for real. */
function boot({ settings = null, recents = null, contacts = null, password = null,
                clipboard = '', prompts = [], fetchImpl = null, search = '', opener = null,
                ringAudio = null, holdPlayback = 'running' } = {}) {
  const page = fs.readFileSync(path.join(WEB, 'phone.html'), 'utf8')
    .replace(/<script[^>]*jssip[^>]*><\/script>/i, '');
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error.message));
  virtualConsole.on('error', (...args) => errors.push(args.join(' ')));
  const sip = makeFakeSip();
  const ringStats = { started: 0, stopped: 0, disconnected: 0, resumed: 0, samples: null, contexts: [] };
  const holdStats = { started: 0, stopped: 0, blocked: holdPlayback === 'blocked' };
  const dom = new JSDOM(page, {
    runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole,
    url: `https://phone.example.com/phone${search}`,
    beforeParse(window) {
      window.JsSIP = sip.api;
      if (opener) Object.defineProperty(window, 'opener', { value: opener, configurable: true });
      /* jsdom seams: media, clipboard, prompts and a Date the test can move. */
      window.HTMLMediaElement.prototype.play = function () {
        if (this.id === 'hold-audio') {
          holdStats.started++;
          if (holdStats.blocked) return Promise.reject(new Error('Autoplay blocked'));
        }
        return Promise.resolve();
      };
      window.HTMLMediaElement.prototype.pause = function () { if (this.id === 'hold-audio') holdStats.stopped++; };
      if (ringAudio) {
        window.AudioContext = class FakeAudioContext {
          constructor() { this.state = ringAudio === 'running' ? 'running' : 'suspended'; this.sampleRate = 8000; this.destination = {}; ringStats.contexts.push(this); }
          createBuffer(channels, length) {
            const data = new Float32Array(length); ringStats.samples = data;
            return { getChannelData: () => data };
          }
          createBufferSource() {
            return { connect() {}, start() { ringStats.started++; }, stop() { ringStats.stopped++; },
              disconnect() { ringStats.disconnected++; } };
          }
          resume() {
            ringStats.resumed++;
            if (ringAudio === 'blocked') return Promise.reject(new Error('Autoplay blocked'));
            this.state = 'running'; if (this.onstatechange) this.onstatechange(); return Promise.resolve();
          }
        };
      }
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
  return { dom, w, d, errors, sip, dialog, ringStats, holdStats };
}

const shown = (d, id) => !d.getElementById(id).classList.contains('hidden');
const session = env => env.sip.calls[env.sip.calls.length - 1];
/* Capture only the call-setup timers, so tests can exercise both limits
   instantly without changing registration, UI, or JsSIP timing. */
function captureSetupTimers(env) {
  const originalSet = env.w.setTimeout.bind(env.w), originalClear = env.w.clearTimeout.bind(env.w);
  const pending = new Map();
  let next = 1000000;
  env.w.setTimeout = (fn, delay, ...args) => {
    if (![250, 500, 800, 4000, 8000, 20000].includes(delay)) return originalSet(fn, delay, ...args);
    const id = next++; pending.set(id, { fn, delay, args }); return id;
  };
  env.w.clearTimeout = id => { if (pending.has(id)) pending.delete(id); else originalClear(id); };
  return {
    count: delay => [...pending.values()].filter(item => item.delay === delay).length,
    run: delay => {
      const entry = [...pending].find(([, item]) => item.delay === delay);
      if (!entry) return false;
      pending.delete(entry[0]); entry[1].fn(...entry[1].args); return true;
    },
  };
}
async function startTestCall(env, number = '1555') {
  await signIn(env);
  typeDigits(env.d, number);
  env.d.getElementById('call-btn').click();
  await settle(20);
  return session(env);
}

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
    const shippedPage = fs.readFileSync(path.join(WEB, 'phone.html'), 'utf8');
    check('the SIP stack is loaded from the vendored same-origin build',
      /<script src="\/jssip\.min\.js">/.test(shippedPage));
    check('and falls back to the pinned upstream build if that fails',
      /JsSIP@3\.10\.1/.test(shippedPage));
    check('the in-call status line carries the class its stylesheet styles',
      env.d.getElementById('active-status').classList.contains('active-status'),
      env.d.getElementById('active-status').className);
    check('the page requests versioned CSS/JS so a stale cached build cannot answer a new page',
      shippedPage.includes(`href="/phone.css?v=${VERSION}"`)
      && shippedPage.includes(`src="/phone.js?v=${VERSION}"`), VERSION);
    const swSource = fs.readFileSync(path.join(WEB, 'sw.js'), 'utf8');
    check('the service worker precaches the same versioned assets',
      swSource.includes(`const VERSION = "${VERSION}"`)
      && swSource.includes('`/phone.js?v=${VERSION}`')
      && swSource.includes('`/phone.css?v=${VERSION}`'), VERSION);
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
    check("the WebSocket rides this page's own host, where HTTPS already works",
      good.sip.sockets[0] === 'wss://phone.example.com/ws', good.sip.sockets[0]);
    check('the registration is started', ua.started === true);
    check('long-call SIP session timers stay enabled and refresh with UPDATE',
      ua.config.session_timers === true && ua.config.session_timers_refresh_method === 'UPDATE',
      JSON.stringify({ timers: ua.config.session_timers, method: ua.config.session_timers_refresh_method }));
    check('registration and WebSocket recovery are bounded, not left at stale defaults',
      ua.config.register_expires === 300
      && ua.config.connection_recovery_min_interval === 2
      && ua.config.connection_recovery_max_interval === 8,
      JSON.stringify(ua.config));
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

    /* The SIP domain may be the bare VM IP - the media rules push operators
       there - and an IP cannot hold a TLS certificate, so the WebSocket must
       never follow it. */
    const bareIp = boot();
    await signIn(bareIp, { domain: '203.0.113.10' });
    check('a bare-IP SIP address never becomes the WebSocket address - an IP cannot hold a TLS certificate',
      bareIp.sip.sockets[0] === 'wss://phone.example.com/ws', bareIp.sip.sockets[0]);

    const legacySocket = boot({settings: {
      username: 'KUDGTE_101', domain: 'sip.engineerip.com', wss: 'wss://sip.engineerip.com/ws'
    }});
    check('stored legacy domain-derived socket migrates to the page host',
      legacySocket.d.getElementById('login-wss').value === 'wss://phone.example.com/ws');
    const customSocket = boot({settings: {
      domain: 'sip.engineerip.com', wss: 'wss://edge.example.com:8089/ws'
    }});
    check('migration preserves other custom WebSocket servers',
      customSocket.d.getElementById('login-wss').value === 'wss://edge.example.com:8089/ws');
    const chosenSocket = boot({settings: {
      domain: 'sip.engineerip.com', wss: 'wss://sip.engineerip.com/ws', wssOriginVersion: 1
    }});
    check('a server explicitly saved after migration is preserved',
      chosenSocket.d.getElementById('login-wss').value === 'wss://sip.engineerip.com/ws');

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
    const lostUa = lost.sip.instances[0];
    lostUa.drop();
    check('a dropped WebSocket marks the phone as reconnecting, not silently ready',
      !lost.d.getElementById('connection-state').classList.contains('online')
      && /Reconnecting/.test(lost.d.getElementById('connection-state').textContent)
      && lost.d.getElementById('connection-state').classList.contains('connecting'),
      lost.d.getElementById('connection-state').textContent + ' / ' + lost.d.getElementById('connection-state').className);
    lost.w.eval('ensureSipConnection()');
    await settle(20);
    check('the visibility/online watchdog restarts the dropped signalling connection',
      lostUa.isConnected() && lostUa.isRegistered(),
      JSON.stringify({ connected: lostUa.isConnected(), registered: lostUa.isRegistered() }));
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
    check('the call gathers candidates through STUN, or a NAT-ed browser offers only its private address',
      Array.isArray(placed.options.pcConfig.iceServers) && placed.options.pcConfig.iceServers.length > 0
      && placed.options.pcConfig.iceServers.every(s => /^stun:|^turn/.test(String(s.urls))),
      JSON.stringify(placed.options.pcConfig));
    check("the microphone is captured with the browser's own call processing",
      placed.options.mediaConstraints.audio.echoCancellation === true
      && placed.options.mediaConstraints.audio.noiseSuppression === true
      && placed.options.mediaConstraints.audio.autoGainControl === true,
      JSON.stringify(placed.options.mediaConstraints.audio));
    check('the active-call screen opens over the dialer',
      !d.getElementById('active-call-view').classList.contains('hidden')
      && d.getElementById('active-number').textContent === '15555123456');
    check('the call button is disabled while a call is up', d.getElementById('call-btn').disabled === true);
    check('the premium call screen keeps End Call in a fixed footer',
      shown(d, 'hangup-btn') && !!d.getElementById('hangup-btn').closest('.call-footer')
      && d.getElementById('active-call-view').dataset.phase === 'dialing');
    check('the status starts by preparing the call, not claiming a SIP answer',
      /Preparing call/.test(d.getElementById('active-status').textContent));

    placed.session.ring();
    check('ringing is reported while the far end rings', /Ringing/.test(d.getElementById('active-status').textContent));
    check('the call phase drives the ringing animation state',
      d.getElementById('active-call-view').dataset.phase === 'ringing');

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
    check('unmute calls the SIP unmute method and turns the microphone back on',
      placed.session._muted.audio === false && placed.session._muted.unmutedWith.audio === true
      && !d.getElementById('mute-btn').classList.contains('active'));
    d.getElementById('hold-btn').click();
    check('hold holds the far end', placed.session._held === true && d.getElementById('hold-btn').classList.contains('active'));
    d.getElementById('hold-btn').click();
    check('unhold releases it', placed.session._held === false);
    check('the speaker/handset selector is absent and Windows keeps the default audio output',
      !d.getElementById('speaker-btn') && d.getElementById('remote-audio').muted === false);
    d.getElementById('mute-btn').click();
    d.getElementById('hold-btn').click();

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
    check('the next call starts unmuted, unheld and on the system output',
      !env.d.getElementById('mute-btn').classList.contains('active')
      && !env.d.getElementById('hold-btn').classList.contains('active')
      && !env.d.getElementById('speaker-btn')
      && env.d.getElementById('remote-audio').muted === false
      && session(env).session._muted === null);
    await hangUp(env);
    check('and that call is filed under its own number',
      JSON.parse(env.w.localStorage.getItem('eip-phone-recents'))[0].number === '15559998888');
    env.dom.window.close();

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

  /* -------------------------------------------------- media reliability */
  section('Media, registration and in-call responsiveness');
  {
    const early = boot();
    const placed = await startTestCall(early, '1555');
    const peer = placed.session.connection;
    check('an outgoing peerconnection created inside ua.call() is still bound',
      peer.__eipAudioBound === true && typeof peer.ontrack === 'function'
      && (peer.listeners.track || []).length > 0,
      JSON.stringify({ bound: peer.__eipAudioBound, listeners: Object.keys(peer.listeners) }));
    const track = { kind: 'audio', id: 'remote-track-1' };
    peer.track({ track });  // No a=msid stream: Asterisk may deliver only a track.
    const attached = early.d.getElementById('remote-audio').srcObject;
    check('a remote track without an msid stream is still attached and playable',
      attached && typeof attached.getTracks === 'function' && attached.getTracks().includes(track),
      JSON.stringify(attached && attached.getTracks && attached.getTracks().map(t => t.id)));
    check('the remote audio element is never muted by the media fix',
      early.d.getElementById('remote-audio').muted === false);
    early.dom.window.close();

    const blocked = boot();
    const blockedCall = await startTestCall(blocked, '1666');
    const blockedPeer = blockedCall.session.connection;
    blocked.d.getElementById('remote-audio').play = () => Promise.reject(new Error('autoplay blocked'));
    blockedPeer.track({ track: { kind: 'audio', id: 'blocked-track' } });
    await settle(20);
    check('a blocked autoplay promise offers a one-tap audio unlock',
      shown(blocked.d, 'enable-audio-btn'),
      blocked.d.getElementById('enable-audio-btn').className);
    blocked.d.getElementById('remote-audio').play = () => Promise.resolve();
    blocked.d.getElementById('enable-audio-btn').click();
    await settle(20);
    check('tapping the unlock retries playback and keeps the far end audible',
      !shown(blocked.d, 'enable-audio-btn')
      && blocked.d.getElementById('remote-audio').muted === false,
      blocked.d.getElementById('enable-audio-btn').className);
    blocked.dom.window.close();

    const keep = boot();
    await signIn(keep);
    const keepUa = keep.sip.instances[0];
    const beforeKeepalive = keepUa.registers;
    keep.w.eval('sendRegistrationKeepalive()');
    check('a registration keepalive refreshes the SIP contact before a proxy idle timeout',
      keepUa.registers === beforeKeepalive + 1,
      `${beforeKeepalive} -> ${keepUa.registers}`);
    keep.dom.window.close();

    const dtmf = boot();
    const dtmfCall = await startTestCall(dtmf, '1770');
    dtmf.d.getElementById('dtmf-btn').click();
    check('the in-call keypad opens for IVR navigation',
      shown(dtmf.d, 'dtmf-panel')
      && dtmf.d.getElementById('dtmf-btn').getAttribute('aria-expanded') === 'true',
      dtmf.d.getElementById('dtmf-panel').className);
    check('End Call stays visible while the DTMF keypad is open',
      shown(dtmf.d, 'hangup-btn')
      && !!dtmf.d.getElementById('hangup-btn').closest('.call-footer'));
    dtmf.d.querySelector('#dtmf-panel [data-dtmf="5"]').click();
    check('a keypad press sends that DTMF tone on the live session',
      dtmfCall.session._dtmf.join('') === '5', dtmfCall.session._dtmf.join(''));
    const key7 = new dtmf.w.Event('keydown', { bubbles: true });
    Object.defineProperty(key7, 'key', { value: '7' });
    dtmf.d.dispatchEvent(key7);
    check('the physical keyboard sends DTMF during a call instead of editing the dialer',
      dtmfCall.session._dtmf.join('') === '57'
      && dtmf.d.getElementById('number-input').value === '1770',
      dtmfCall.session._dtmf.join('') + ' / ' + dtmf.d.getElementById('number-input').value);
    dtmf.dom.window.close();

    const mic = boot();
    const micCall = await startTestCall(mic, '1888');
    // Real JsSIP emits failed first, followed by getusermediafailed.
    micCall.session.fail('User Denied Media Access');
    micCall.session.emit('getusermediafailed', new Error('denied'));
    await settle(20);
    check('a denied microphone remains visible after the call screen closes',
      !shown(mic.d, 'active-call-view')
      && /Microphone unavailable/.test(mic.d.getElementById('dial-hint').textContent)
      && mic.d.getElementById('call-btn').disabled === false,
      mic.d.getElementById('dial-hint').textContent);
    mic.dom.window.close();
  }

  /* ---------------------------------------- ringback and hold call feedback */
  section('Ringback and hold');
  {
    const internal = boot({ ringAudio: 'running' });
    const call = await startTestCall(internal, '102');
    call.session.ring(100);
    check('a 100 Trying response does not falsely play a ringing tone', internal.ringStats.started === 0);
    call.session.ring(180);
    check('extension 180 Ringing starts a non-silent local ringback loop',
      internal.ringStats.started === 1 && internal.ringStats.samples.some(sample => Math.abs(sample) > 0.05));
    call.session.ring(180);
    check('repeated ringing responses do not start overlapping tones', internal.ringStats.started === 1);
    call.session.emit('sdp', { originator: 'remote', sdp: 'v=0\r\nm=audio' });
    check('remote early media stops local ringback so announcements are not covered', internal.ringStats.stopped === 1);
    call.session.ring(180);
    check('ringback stays off after early media, even if another 180 arrives', internal.ringStats.started === 1);
    call.session.connect(); await hangUp(internal); internal.dom.window.close();

    const external = boot({ ringAudio: 'running' });
    const outside = await startTestCall(external, '15551234567');
    outside.session.ring(183);
    check('an external call with progress but no early SDP also has audible ringback', external.ringStats.started === 1);
    outside.session.ring(183, 'v=0\r\nm=audio');
    check('a carrier-provided SDP stops the generated tone', external.ringStats.stopped === 1);
    outside.session.fail('Busy Here'); await settle(20);
    check('a failed external call stops ringback and reports the failure',
      external.ringStats.stopped === 1 && /Busy Here/.test(external.d.getElementById('dial-hint').textContent));
    external.dom.window.close();

    const silentSdp = boot({ ringAudio: 'running' });
    await signIn(silentSdp); const silenceTimers = captureSetupTimers(silentSdp);
    typeDigits(silentSdp.d, '104'); silentSdp.d.getElementById('call-btn').click(); await settle(20);
    const silentCall = session(silentSdp).session;
    silentCall.ring(180, 'v=0\r\nm=audio');
    check('a 180 with SDP first gives carrier early audio a chance',
      silentSdp.ringStats.started === 0 && silenceTimers.count(800) === 1);
    silenceTimers.run(800); await settle(20);
    check('when that 180 carries no audible media, a local ringback fills the silence', silentSdp.ringStats.started === 1);
    silentCall.connection._stats = [{ type: 'inbound-rtp', kind: 'audio', packetsReceived: 4, totalAudioEnergy: 0.01 }];
    silenceTimers.run(500); await settle(20);
    check('local ringback stops as soon as the far end provides real audio',
      silentSdp.ringStats.stopped === 1 && silenceTimers.count(500) === 0);
    silentCall.terminate(); silentSdp.dom.window.close();

    const announcement = boot({ ringAudio: 'running' });
    await signIn(announcement); const announceTimers = captureSetupTimers(announcement);
    typeDigits(announcement.d, '104'); announcement.d.getElementById('call-btn').click(); await settle(20);
    const announceCall = session(announcement).session;
    announceCall.ring(180, 'v=0\r\nm=audio'); announceCall.ring(183, 'v=0\r\nm=audio');
    check('a carrier announcement cancels a pending silent-180 fallback',
      announceTimers.count(800) === 0 && announcement.ringStats.started === 0);
    announceCall.terminate(); announcement.dom.window.close();

    const racing = boot({ ringAudio: 'running' });
    await signIn(racing); const raceTimers = captureSetupTimers(racing);
    typeDigits(racing.d, '104'); racing.d.getElementById('call-btn').click(); await settle(20);
    const racingCall = session(racing).session;
    let finishStats; racingCall.connection.getStats = () => new Promise(resolve => {
      if (!finishStats) finishStats = resolve; else resolve([]);
    });
    racingCall.ring(180, 'v=0\r\nm=audio'); raceTimers.run(800);
    racingCall.connect(); finishStats([]); await settle(20);
    check('an in-flight early-media probe cannot start ringback after answer', racing.ringStats.started === 0);
    await hangUp(racing); racing.dom.window.close();

    const answer = boot({ ringAudio: 'running' });
    const answered = await startTestCall(answer, '105'); answered.session.ring(); answered.session.connect();
    check('answering stops the local ringback immediately', answer.ringStats.stopped === 1);
    await hangUp(answer); answer.dom.window.close();

    const hold = boot();
    const heldCall = await startTestCall(hold, '102'); heldCall.session.connect(); await settle(20);
    hold.d.getElementById('hold-btn').click();
    check('local hold shows an honest hold notice and changes the control to Resume',
      shown(hold.d, 'hold-notice') && /You put this call on hold/.test(hold.d.getElementById('hold-notice').textContent)
      && /Resume/.test(hold.d.getElementById('hold-btn').textContent) && hold.holdStats.started === 0);
    hold.d.getElementById('hold-btn').click();
    check('local resume clears the hold notice', !shown(hold.d, 'hold-notice') && !heldCall.session._held);
    heldCall.session.emit('hold', { originator: 'remote' }); await settle(20);
    check('remote hold visibly tells the user and starts the local hold music',
      shown(hold.d, 'hold-notice') && /other person put you on hold/i.test(hold.d.getElementById('hold-notice').textContent)
      && hold.holdStats.started === 1 && hold.d.getElementById('mute-btn').disabled);
    heldCall.session.emit('unhold', { originator: 'remote' }); await settle(20);
    check('remote resume stops music and restores the connected call',
      !shown(hold.d, 'hold-notice') && hold.holdStats.stopped >= 1 && !hold.d.getElementById('mute-btn').disabled);
    heldCall.session.hold = () => false;
    hold.d.getElementById('hold-btn').click();
    check('a refused or still-busy SIP hold never pretends the call is held',
      !hold.d.getElementById('hold-btn').classList.contains('active')
      && !shown(hold.d, 'hold-notice') && /not ready/i.test(hold.d.getElementById('active-status').textContent));
    await hangUp(hold); hold.dom.window.close();

    const blocked = boot({ holdPlayback: 'blocked' });
    const blockedCall = await startTestCall(blocked, '103'); blockedCall.session.connect(); await settle(20);
    blockedCall.session.emit('hold', { originator: 'remote' }); await settle(20);
    check('blocked hold music exposes an actionable unlock rather than hiding the hold',
      shown(blocked.d, 'hold-notice') && shown(blocked.d, 'enable-audio-btn')
      && /hold music/i.test(blocked.d.getElementById('enable-audio-btn').textContent));
    blocked.holdStats.blocked = false; blocked.d.getElementById('enable-audio-btn').click(); await settle(20);
    check('the audio unlock retries hold music and hides the prompt',
      blocked.holdStats.started === 2 && !shown(blocked.d, 'enable-audio-btn'));
    await hangUp(blocked); blocked.dom.window.close();
  }

  /* ------------------------------------------ bounded ICE and real ordering */
  section('ICE gathering and SIP setup');
  {
    const env = boot();
    await signIn(env);
    const timers = captureSetupTimers(env);
    typeDigits(env.d, '102'); env.d.getElementById('call-btn').click(); await settle(20);
    const call = session(env).session;
    let hostReady = 0, reflexiveReady = 0;
    call.emit('icecandidate', { candidate: { candidate: 'typ host' }, ready: () => { hostReady++; } });
    check('a host candidate is not released immediately, leaving time for NAT discovery',
      hostReady === 0 && timers.count(4000) === 1 && timers.count(250) === 0);
    call.emit('icecandidate', { candidate: { candidate: 'typ srflx' }, ready: () => { reflexiveReady++; } });
    check('a public reflexive candidate schedules a quick release instead of the old eight-second wait',
      timers.count(250) === 1 && timers.count(4000) === 1);
    timers.run(250);
    check('the fast release includes the latest candidates and clears the fallback',
      hostReady === 0 && reflexiveReady === 1 && timers.count(4000) === 0);
    call.emit('sending');
    check('sending the INVITE cancels the no-signalling watchdog', timers.count(20000) === 0);
    call.terminate(); env.dom.window.close();

    const turn = boot(); await signIn(turn);
    turn.w.eval('iceServers = [{ urls: ["stun:example.test", "turns:relay.test"], username: "x", credential: "y" }]');
    const turnTimers = captureSetupTimers(turn);
    typeDigits(turn.d, '103'); turn.d.getElementById('call-btn').click(); await settle(20);
    const turnCall = session(turn).session;
    let turnReady = 0;
    turnCall.emit('icecandidate', { candidate: { type: 'host' }, ready: () => { turnReady++; } });
    turnCall.emit('icecandidate', { candidate: { type: 'srflx' }, ready: () => { turnReady++; } });
    check('with TURN configured a host or reflexive candidate does not discard a slower relay',
      turnTimers.count(8000) === 1 && turnTimers.count(250) === 0 && turnReady === 0);
    turnCall.emit('icecandidate', { candidate: { type: 'relay' }, ready: () => { turnReady++; } });
    check('a relay candidate allows a quick release', turnTimers.count(250) === 1);
    turnTimers.run(250);
    check('the TURN fast release waits until the relay exists', turnReady === 1 && turnTimers.count(8000) === 0);
    turnCall.terminate(); turn.dom.window.close();

    const complete = boot(); await signIn(complete);
    const completedTimers = captureSetupTimers(complete);
    typeDigits(complete.d, '103'); complete.d.getElementById('call-btn').click(); await settle(20);
    const completedCall = session(complete).session;
    let released = 0;
    completedCall.emit('icecandidate', { candidate: { type: 'host' }, ready: () => { released++; } });
    completedCall.emit('sdp', { originator: 'local' });
    check('normal gathering completion cancels the early-release timer',
      completedTimers.count(4000) === 0 && released === 0);
    completedCall.emit('icecandidate', { candidate: { type: 'host' }, ready: () => { released++; } });
    completedCall.terminate();
    check('ending a call cancels both candidate and setup timers',
      completedTimers.count(4000) === 0 && completedTimers.count(20000) === 0);
    complete.dom.window.close();

    const hostOnly = boot(); await signIn(hostOnly);
    const hostTimers = captureSetupTimers(hostOnly);
    typeDigits(hostOnly.d, '103'); hostOnly.d.getElementById('call-btn').click(); await settle(20);
    const hostCall = session(hostOnly).session; let fallbackReady = 0;
    hostCall.emit('icecandidate', { candidate: { type: 'host' }, ready: () => { fallbackReady++; } });
    hostTimers.run(4000);
    check('host-only gathering is bounded at four seconds, rather than eight', fallbackReady === 1);
    hostCall.terminate(); hostOnly.dom.window.close();

    const stuck = boot(); await signIn(stuck);
    const stuckTimers = captureSetupTimers(stuck);
    typeDigits(stuck.d, '104'); stuck.d.getElementById('call-btn').click(); await settle(20);
    const stuckCall = session(stuck).session;
    stuckTimers.run(20000); await settle(20);
    check('no candidate and no INVITE eventually fails visibly instead of hanging',
      stuckCall._terminated && !shown(stuck.d, 'active-call-view')
      && /setup timed out/i.test(stuck.d.getElementById('dial-hint').textContent));
    // Late events from an old session must not close a new call.
    stuck.d.getElementById('call-btn').click(); await settle(20);
    const nextCall = session(stuck).session;
    stuckCall.emit('failed', { cause: 'late failure' });
    check('a stale session cannot overwrite or terminate the next call',
      shown(stuck.d, 'active-call-view') && !nextCall._terminated);
    nextCall.terminate(); stuck.dom.window.close();

    const media = boot();
    const mediaCall = (await startTestCall(media, '105')).session;
    mediaCall.connection.iceConnectionState = 'checking';
    mediaCall.emit('accepted');
    check('after SIP acceptance the phone distinguishes audio still connecting',
      /Connecting audio/.test(media.d.getElementById('active-status').textContent));
    mediaCall.emit('peerconnection:createofferfailed', new Error('renegotiation failed'));
    check('a later renegotiation error does not forcibly hang up an accepted call',
      shown(media.d, 'active-call-view') && !mediaCall._terminated);
    mediaCall.emit('ended', { cause: 'RTP Timeout' });
    check('an ICE failure terminated by JsSIP remains visible on the dialer',
      /Media connection failed/.test(media.d.getElementById('dial-hint').textContent)
      && media.d.getElementById('dial-hint').classList.contains('error'));
    media.dom.window.close();

    const incoming = boot(); await signIn(incoming);
    const ringing = incoming.sip.instances[0].receive({ caller: '101' });
    incoming.d.getElementById('answer-btn').click();
    ringing.connection.iceConnectionState = 'checking';
    ringing.connection.dispatch('iceconnectionstatechange');
    check('ICE checking before the SIP answer is not described as connected audio',
      /Preparing/.test(incoming.d.getElementById('active-status').textContent));
    ringing.terminate();
    check('a caller who hangs up while the answer is still preparing is missed',
      JSON.parse(incoming.w.localStorage.getItem('eip-phone-recents'))[0].status === 'missed');
    incoming.dom.window.close();
  }

  /* ---------------------------------------------------- incoming calls */
  section('Incoming calls');
  {
    const ringing = boot({ ringAudio: 'running' });
    await signIn(ringing);
    const call = ringing.sip.instances[0].receive({ caller: '101' });
    await settle(20);
    check('an incoming call starts a real local looping ringtone without touching call media',
      ringing.ringStats.started === 1 && ringing.ringStats.samples.length === 32000
      && ringing.ringStats.samples.some(sample => Math.abs(sample) > 0.01)
      && !call._answered);
    ringing.d.getElementById('answer-btn').click();
    await settle(20);
    check('answering immediately stops and disconnects the local ringtone',
      ringing.ringStats.stopped === 1 && ringing.ringStats.disconnected === 1 && !!call._answered);
    await hangUp(ringing); ringing.dom.window.close();

    const locked = boot({ ringAudio: 'suspended' });
    await signIn(locked);
    locked.sip.instances[0].receive({ caller: '102' });
    await settle(20);
    check('an incoming call clearly offers to unlock browser-blocked ringing',
      shown(locked.d, 'enable-ringing-btn') && locked.ringStats.started === 1);
    locked.d.getElementById('enable-ringing-btn').click();
    await settle(20);
    check('the user gesture resumes ringing and hides the unlock prompt',
      locked.ringStats.resumed === 1 && !shown(locked.d, 'enable-ringing-btn'));
    locked.d.getElementById('decline-btn').click(); await settle(20);
    check('decline stops the ringtone', locked.ringStats.stopped === 1);
    locked.dom.window.close();

    const reset = boot({ ringAudio: 'running' });
    await signIn(reset); reset.sip.instances[0].receive({ caller: '102' }); await settle(20);
    const oldContext = reset.ringStats.contexts[0]; oldContext.state = 'closed'; oldContext.onstatechange();
    check('a closed audio context exposes the ringer unlock prompt', shown(reset.d, 'enable-ringing-btn'));
    reset.d.getElementById('enable-ringing-btn').click(); await settle(20);
    check('a user gesture recreates a closed audio context and resumes the ringtone',
      reset.ringStats.contexts.length === 2 && reset.ringStats.started === 2
      && reset.ringStats.stopped === 1 && !shown(reset.d, 'enable-ringing-btn'));
    reset.d.getElementById('decline-btn').click(); await settle(20); reset.dom.window.close();

    const blocked = boot({ ringAudio: 'blocked' });
    await signIn(blocked);
    const blockedCall = blocked.sip.instances[0].receive({ caller: '103' }); await settle(20);
    blocked.d.getElementById('enable-ringing-btn').click(); await settle(20);
    check('autoplay rejection leaves the incoming controls usable and the prompt visible',
      shown(blocked.d, 'enable-ringing-btn') && shown(blocked.d, 'incoming-actions'));
    blocked.d.getElementById('answer-btn').click(); await settle(20);
    check('a blocked ringtone never prevents the call from being answered',
      !!blockedCall._answered && blocked.ringStats.stopped === 1);
    await hangUp(blocked); blocked.dom.window.close();

    const cancelled = boot({ ringAudio: 'running' });
    await signIn(cancelled);
    const cancelledCall = cancelled.sip.instances[0].receive({ caller: '104' });
    await settle(20); cancelledCall.terminate(); await settle(20);
    check('remote cancellation stops the ringtone and closes the call screen',
      cancelled.ringStats.stopped === 1 && !shown(cancelled.d, 'active-call-view'));
    cancelled.dom.window.close();

    const posted = [];
    const opener = { closed: false, postMessage(message, origin) { posted.push({ type: message.type, origin }); } };
    const fallback = boot({ search: '?connect=extension', opener, ringAudio: 'suspended' });
    await signIn(fallback);
    const fallbackCall = fallback.sip.instances[0].receive({ caller: '104' }); await settle(20);
    check('a blocked popup asks only its same-origin console to play the incoming ringtone',
      posted.some(row => row.type === 'eip-softphone:ring-start' && row.origin === 'https://phone.example.com')
      && shown(fallback.d, 'ringer-notice'));
    fallback.d.getElementById('enable-ringing-btn').click(); await settle(20);
    check('unlocking the popup stops the console fallback to avoid double ringing',
      posted.some(row => row.type === 'eip-softphone:ring-stop') && !shown(fallback.d, 'ringer-notice'));
    fallbackCall.terminate(); await settle(20); fallback.dom.window.close();

    const instant = boot({ ringAudio: 'running' });
    await signIn(instant);
    const instantCall = instant.sip.instances[0].receive({ caller: '105' });
    instantCall.terminate(); await settle(20);
    check('a call cancelled before deferred audio setup never starts a late ringtone',
      instant.ringStats.started === 0 && !shown(instant.d, 'active-call-view'));
    instant.dom.window.close();

    const quickAnswer = boot({ ringAudio: 'running' });
    await signIn(quickAnswer);
    const quickCall = quickAnswer.sip.instances[0].receive({ caller: '106' });
    quickAnswer.d.getElementById('answer-btn').click(); await settle(20);
    check('an immediate answer never starts a late ringtone during the conversation',
      !!quickCall._answered && quickAnswer.ringStats.started === 0);
    await hangUp(quickAnswer); quickAnswer.dom.window.close();
  }
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
    check('and the same STUN configuration the outgoing calls use',
      incoming._answered && Array.isArray(incoming._answered.pcConfig.iceServers)
      && incoming._answered.pcConfig.iceServers.length > 0);
    check('answering waits for actual SIP acceptance and does not start the timer',
      !shown(d, 'incoming-actions') && shown(d, 'hangup-btn')
      && d.getElementById('active-label').textContent === 'Answering'
      && /Preparing answer/.test(d.getElementById('active-status').textContent)
      && d.getElementById('call-timer').textContent === '00:00');
    incoming.connect();
    check('only SIP acceptance marks the call connected',
      d.getElementById('active-label').textContent === 'Call'
      && /Connected/.test(d.getElementById('active-status').textContent));
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
    const callBody = (css.match(/\.call-body\{[^}]*\}/) || [''])[0];
    const callFooter = (css.match(/\.call-footer\{[^}]*\}/) || [''])[0];
    check('the in-call body scrolls while the end-call footer stays fixed',
      /overflow-y:auto/.test(callBody) && /flex:0 0 auto/.test(callFooter)
      && /z-index:1/.test(callFooter), callBody + ' / ' + callFooter);
    check('short windows get their own layout instead of a scrollbar',
      /@media\(max-height:\d+px\)/.test(css));
    check('keyboard users get a visible focus ring', /:focus-visible\{[^}]*outline:2px solid/.test(css));
    check('the keyboard hint only appears where there is a keyboard',
      /\.dial-tip\{display:none/.test(css) && /@media\(min-width:700px\)\{\s*\.dial-tip\{display:block\}/.test(css));
    check('the phone layout keeps its small-screen rules and safe areas',
      /@media\(max-width:520px\)/.test(css) && /env\(safe-area-inset-bottom\)/.test(css)
      && /env\(safe-area-inset-top\)/.test(css));
    // The keypad rows are sized from the window, but no key may ever be smaller than a finger.
    const keyHeights = [...css.matchAll(/\.keypad button\{[^}]*min-height:(\d+)px/g)].map(m => Number(m[1]));
    check('every keypad layout keeps a finger-sized target',
      keyHeights.length >= 1 && keyHeights.every(height => height >= 44), keyHeights.join(', '));
    // The card is one height variable: a phone-sized maximum, and never taller than the window.
    const cards = [...css.matchAll(/--screen-h:min\((\d+)px,calc\(100dvh - \d+px\)\)/g)].map(m => Number(m[1]));
    check('the card is phone-shaped and never taller than the window',
      cards.length > 0 && cards.every(height => height <= 900) && /\.phone-app,\.console-screen\{[^}]*height:var\(--screen-h\)/.test(css), cards.join(', '));
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
      && d.getElementById('sip-wss').value === 'wss://phone.example.com/ws',
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
      SHELL_URLS.every(url => worker.entries.has(`https://phone.example.com${url}`)),
      [...worker.entries.keys()].join(', '));
    check('and reaches for nothing outside this site', worker.network.length === 0, worker.network.join(', '));
    await worker.activate();

    const cached = await worker.fetch('https://phone.example.com/phone');
    check('the shell is served online',
      cached && cached.url === 'https://phone.example.com/phone', cached && cached.url);
    check('by revalidating with the network', worker.network.length === 1, worker.network.join(', '));

    const fresh = await worker.fetch(`https://phone.example.com${VERSIONED_JS}`);
    check('a cached versioned asset is answered too',
      fresh && fresh.url === `https://phone.example.com${VERSIONED_JS}`, fresh && fresh.url);

    const stack = await worker.fetch('https://phone.example.com/jssip.min.js');
    check('the SIP stack is served online as well',
      stack && stack.url === 'https://phone.example.com/jssip.min.js', stack && stack.url);

    const state = await worker.fetch('https://phone.example.com/admin/api/state');
    check('the console API is left to the network, never cached', state === undefined);
    const consolePage = await worker.fetch('https://phone.example.com/admin', { mode: 'navigate' });
    check('and so is the console page itself', consolePage === undefined);
    const api = await worker.fetch('https://phone.example.com/api/v1/calls?limit=50');
    check('nor does call history pass through the cache', api === undefined);
    const cdn = await worker.fetch('https://cdn.jsdelivr.net/gh/versatica/JsSIP@3.10.1/dist/jssip.min.js');
    check('the fallback CDN build is left to the network, never cached', cdn === undefined);
    const post = await worker.fetch('https://phone.example.com/phone', { method: 'POST' });
    check('a non-GET request is never answered from the shell', post === undefined);
    check('nothing else was written to the cache',
      [...worker.entries.keys()].sort().join(', ') ===
        [...SHELL_URLS].sort().map(url => `https://phone.example.com${url}`).join(', '),
      [...worker.entries.keys()].join(', '));

    /* A release must replace the shell outright: a copy cached by an earlier
       release used to shadow the new files forever (caches.match looks in
       every cache, oldest first), which kept broken pages - like one with no
       SIP stack - alive after the fix had shipped. */
    const stale = () => ({ url: 'https://phone.example.com/phone', stale: true, clone() { return this; } });
    const upgrading = shellWorker();
    const older = await upgrading.caches.open('eip-phone-v5');
    await older.put('https://phone.example.com/phone', stale());
    await upgrading.install();
    const served = await upgrading.fetch('https://phone.example.com/phone', { mode: 'navigate' });
    check('a cache from an older release never shadows the shell the worker reads',
      served && !served.stale);
    await upgrading.activate();
    check('and activation deletes the older cache outright',
      !(await upgrading.caches.keys()).includes('eip-phone-v5'),
      (await upgrading.caches.keys()).join(', '));

    const offlineWorker = shellWorker({ offline: true });
    await offlineWorker.install();
    await offlineWorker.activate();
    const reopened = await offlineWorker.fetch('https://phone.example.com/phone', { mode: 'navigate' });
    check('offline, opening the phone still works from the cache',
      reopened && reopened.url === 'https://phone.example.com/phone', reopened && reopened.url);

    const currentCache = (await worker.caches.keys())[0];
    const sameRelease = await worker.caches.open(currentCache);
    await sameRelease.put(`https://phone.example.com${VERSIONED_JS}`, stale());
    const refreshed = await worker.fetch(`https://phone.example.com${VERSIONED_JS}`);
    check('even a stale asset in the CURRENT cache cannot shadow a deployed fix',
      refreshed && !refreshed.stale && worker.network.includes(`https://phone.example.com${VERSIONED_JS}`));
    const offlineAsset = await offlineWorker.fetch(`https://phone.example.com${VERSIONED_JS}`);
    check('offline assets still use the shell fallback', !!offlineAsset);

    const shipped = fs.readFileSync(path.join(WEB, 'sw.js'), 'utf8');
    check('a release can replace the shell by bumping the cache name',
      /const CACHE = "eip-phone-v\d+";/.test(shipped));

    section('The softphone the admin console opens for one extension');
    const CONNECT = '?connect=extension';
    const ORIGIN = 'https://phone.example.com';
    const consoleWindow = { posts: [], postMessage(message, origin) { this.posts.push({ message, origin }); } };
    const fromWindow = (env, data, origin, source) => {
      const event = new env.w.Event('message');
      Object.defineProperty(event, 'data', { value: data });
      Object.defineProperty(event, 'origin', { value: origin });
      Object.defineProperty(event, 'source', { value: source });
      env.w.dispatchEvent(event);
    };
    const handoffMessage = (password = 'handed-secret', extra = {}) => ({
      type: 'eip-softphone:connect', extension: '101@+13025550002', label: '101 · +13025550002',
      sip_username: 'MERIDIANHEALTH_101_13025550002', sip_password: password,
      domain: 'sip.engineerip.com', wss: 'wss://sip.engineerip.com/ws',
      ...extra,
    });

    const stored = boot({
      search: CONNECT, opener: consoleWindow,
      settings: { username: 'OLD_201', domain: 'sip.engineerip.com', wss: '' }, password: 'stored-secret',
    });
    await settle(40);
    check('the window says it is ready to the console that opened it',
      consoleWindow.posts.some(entry => entry.message.type === 'eip-softphone:ready' && entry.origin === ORIGIN),
      JSON.stringify(consoleWindow.posts));
    check('a stored phone session is not signed in from the console window', stored.sip.instances.length === 0);

    const waiting = boot({ search: CONNECT, opener: consoleWindow });
    await settle(20);
    check('until the console signs it in, the window says so instead of showing a form',
      shown(waiting.d, 'console-screen') && !shown(waiting.d, 'login-screen') && !shown(waiting.d, 'phone-app'));

    const clean = boot({ search: CONNECT, opener: consoleWindow });
    await settle(40);
    fromWindow(clean, handoffMessage(), 'https://evil.example.com', consoleWindow);
    fromWindow(clean, handoffMessage(), ORIGIN, { posted: true });
    await settle(40);
    check('a sign-in from another origin, or from another window, is ignored', clean.sip.instances.length === 0);

    fromWindow(clean, handoffMessage(), ORIGIN, consoleWindow);
    await settle(60);
    const ua = clean.sip.instances[0];
    check('the console\'s sign-in registers that extension\'s own SIP identity',
      !!ua && ua.config.uri === 'sip:MERIDIANHEALTH_101_13025550002@sip.engineerip.com', ua && ua.config.uri);
    check('over the WebSocket address the console gave', clean.sip.sockets[0] === 'wss://sip.engineerip.com/ws', clean.sip.sockets[0]);
    check('a registered console window shows the dialer', shown(clean.d, 'phone-app') && !shown(clean.d, 'login-screen'));
    check('the window is titled for the extension', /Softphone · 101 · \+13025550002/.test(clean.d.title), clean.d.title);
    check('the sign-in is never written to local storage', clean.w.localStorage.getItem('eip-phone-settings') === null,
      clean.w.localStorage.getItem('eip-phone-settings'));
    check('nor to session storage', clean.w.sessionStorage.getItem('eip-phone-sip-password') === null);

    const iced = boot({ search: CONNECT, opener: consoleWindow });
    await settle(40);
    fromWindow(iced, handoffMessage('handed-secret', {
      iceServers: [{ urls: 'turn:turn.internal.example', username: 'u', credential: 'p' }],
    }), ORIGIN, consoleWindow);
    await settle(60);
    typeDigits(iced.d, '15550003333');
    iced.d.getElementById('call-btn').click();
    await settle(20);
    check('the platform\'s ICE servers ride along with the sign-in',
      session(iced).options.pcConfig.iceServers[0].urls === 'turn:turn.internal.example',
      JSON.stringify(session(iced).options.pcConfig));
    iced.dom.window.close();

    const denied = boot({ search: CONNECT, opener: consoleWindow });
    await settle(40);
    fromWindow(denied, handoffMessage('bad-password'), ORIGIN, consoleWindow);
    await settle(60);
    check('a rejected extension sign-in returns to the sign-in screen with the reason',
      shown(denied.d, 'login-screen') && /Rejected/.test(denied.d.getElementById('login-status').textContent),
      denied.d.getElementById('login-status').textContent);
    check('and keeps that extension\'s username for another attempt',
      denied.d.getElementById('login-username').value === 'MERIDIANHEALTH_101_13025550002',
      denied.d.getElementById('login-username').value);
    check('and writes nothing to storage', denied.w.localStorage.getItem('eip-phone-settings') === null);

    const plainOpen = boot({});
    await settle(40);
    check('opened without the console, the phone signs in from its own form as before',
      plainOpen.sip.instances.length === 0 && shown(plainOpen.d, 'login-screen') && !plainOpen.w.opener);
  }

  {
    section('WebSocket diagnostics report evidence, not an assumed nginx cause');
    for (const status of [404,400,426,502,503,504]) {
      const env=boot({fetchImpl:async()=>({status})});
      const message=await env.w.eval('wssDiagnosis("wss://phone.example.com/ws")');
      check(`HTTP ${status} probe includes its actual status`,message.includes(`HTTP ${status}`));
      if(status===404) {
        check('404 distinguishes missing nginx routing from an Asterisk handler failure',message.includes('nginx may') && message.includes('or Asterisk'));
        check('404 does not claim an unverified nginx root cause',!message.includes('the reverse proxy has no route'));
        check('404 explains an app rebuild cannot install a host nginx route',message.includes('Rebuilding the web app does not install'));
      }
      if(status===400 || status===426)check(`HTTP ${status} plain probe is not mistaken for a failed real handshake`,message.includes('plain HTTP probe') && message.includes('real HTTP 101') && message.includes('SIP subprotocol'));
      check(`HTTP ${status} diagnostic does not register or place a call`,env.sip.instances.length===0 && env.sip.calls.length===0);
      env.dom.window.close();
    }
    const blocked=boot({fetchImpl:async()=>{throw new TypeError('network blocked');}});
    const message=await blocked.w.eval('wssDiagnosis("wss://phone.example.com/ws")');
    check('blocked diagnostic probe does not guess a proxy failure',message.includes('cannot determine whether DNS, TLS, the proxy or Asterisk'));
    blocked.dom.window.close();
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
