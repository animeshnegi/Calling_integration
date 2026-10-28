/* Real-browser WebRTC/JsSIP regression (no live PBX required).
 *
 * Install puppeteer-core and ws, and provide a Chrome/Chromium executable as
 * CHROMIUM, or also install @sparticuz/chromium. Example on a machine with
 * Chrome:
 *   npm install --no-save puppeteer-core ws
 *   CHROMIUM=/path/to/chromium node tools/phone-realcheck.js
 *
 * Two real Chrome pages use the shipped phone.js and vendored JsSIP. A tiny
 * loopback SIP-over-WebSocket relay below handles REGISTER and forwards SIP
 * dialogs; it is deliberately not an Asterisk replacement. Actual browser
 * RTCPeerConnections, fake microphone devices, DTLS-SRTP and RTP packets are
 * used. We suppress only ICE's completion notification to reproduce a slow
 * STUN/TURN server; actual host candidates and media transport stay real.
 * This does NOT test the deployed VM, carrier or Internet NAT/firewall.
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const assert = require('assert/strict');
const puppeteer = require('puppeteer-core');
const { WebSocketServer, WebSocket } = require('ws');

const root = path.join(__dirname, '..', 'web');
const records = [], contacts = new Map(), dialogs = new Map();
const header = (message, name) => {
  const match = message.match(new RegExp('^' + name + ':\\s*(.*)$', 'im'));
  return match ? match[1].trim() : '';
};
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, message, timeout = 18000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await fn()) return; await wait(50); }
  throw new Error('Timed out: ' + message);
}
function check(label, condition) { assert.ok(condition, label); console.log('PASS  ' + label); }
function replyOk(socket, message) {
  const to = header(message, 'To');
  const contact = header(message, 'Contact');
  const response = [
    'SIP/2.0 200 OK',
    'Via: ' + header(message, 'Via'),
    'From: ' + header(message, 'From'),
    'To: ' + (to.includes(';tag=') ? to : to + ';tag=regtest'),
    'Call-ID: ' + header(message, 'Call-ID'),
    'CSeq: ' + header(message, 'CSeq'),
    ...(contact ? ['Contact: ' + contact + ';expires=300'] : []),
    'Expires: 300', 'Content-Length: 0', '', '',
  ].join('\r\n');
  socket.send(response);
}
function onSip(socket, bytes) {
  const message = bytes.toString();
  if (!message.trim()) return;
  const first = message.split('\r\n', 1)[0];
  const method = first.split(' ')[0];
  const callId = header(message, 'Call-ID');
  const cseq = header(message, 'CSeq');
  if (process.env.PHONE_REALCHECK_TRACE) console.log('SIP', socket.user || '?', first, cseq);
  records.push({ first, method, callId, cseq, user: socket.user || '', time: Date.now(), message });
  if (method === 'REGISTER') {
    const username = (header(message, 'From').match(/sip:([^@;>]+)@/i) || [])[1];
    if (!username) throw new Error('Cannot identify REGISTER sender');
    socket.user = username; contacts.set(username, socket); replyOk(socket, message); return;
  }
  if (method === 'INVITE' && !dialogs.has(callId)) {
    const target = (first.match(/^INVITE sip:([^@;>]+)@/i) || [])[1];
    const peer = contacts.get(target);
    if (!peer) throw new Error('No test contact for ' + target);
    dialogs.set(callId, [socket, peer]);
  }
  const pair = dialogs.get(callId);
  if (!pair) throw new Error('No test dialog for: ' + first);
  const peer = pair[0] === socket ? pair[1] : pair[0];
  if (peer.readyState === WebSocket.OPEN) peer.send(message);
}

function pageShim() {
  // Preserve the browser's RTCPeerConnection and media implementation, but
  // postpone just the completion notifications JsSIP relies on. This ensures
  // the early-ready path is tested even on a fast local network.
  const proto = RTCPeerConnection.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'iceGatheringState');
  Object.defineProperty(proto, 'iceGatheringState', {
    ...descriptor,
    get() {
      const state = descriptor.get.call(this);
      return window.__stallIce && state === 'complete' ? 'gathering' : state;
    },
  });
  const add = proto.addEventListener, remove = proto.removeEventListener;
  const wrappers = new WeakMap();
  proto.addEventListener = function(type, callback, options) {
    if ((type !== 'icecandidate' && type !== 'icegatheringstatechange') || typeof callback !== 'function')
      return add.call(this, type, callback, options);
    let map = wrappers.get(this); if (!map) { map = new Map(); wrappers.set(this, map); }
    const wrapped = event => {
      if (window.__noCandidates && type === 'icecandidate') return;
      if (window.__stallIce && (type === 'icecandidate' ? !event.candidate : descriptor.get.call(this) === 'complete')) return;
      callback.call(this, event);
    };
    map.set(callback, wrapped);
    return add.call(this, type, wrapped, options);
  };
  proto.removeEventListener = function(type, callback, options) {
    const map = wrappers.get(this), wrapped = map && map.get(callback);
    if (wrapped) map.delete(callback);
    return remove.call(this, type, wrapped || callback, options);
  };
}
async function signIn(page, base, name) {
  await page.evaluateOnNewDocument(pageShim);
  await page.goto(base + '/phone', { waitUntil: 'domcontentloaded' });
  await page.type('#login-username', name);
  await page.type('#login-password', 'test-password');
  await page.type('#login-domain', 'sip.test');
  await page.type('#login-wss', base.replace('http:', 'ws:') + '/ws');
  await page.click('#login-form button[type=submit]');
  try {
    await page.waitForFunction(() => !document.getElementById('phone-app').classList.contains('hidden'), { timeout: 12000 });
  } catch (error) {
    console.error('Sign-in status:', await page.$eval('#login-status', el => el.textContent));
    throw error;
  }
  // Do not depend on public STUN servers for this loopback regression.
  await page.evaluate(() => { iceServers = [{ urls: 'stun:127.0.0.1:9' }]; });
}
async function dial(page, number) {
  await page.$eval('#number-input', (input, value) => {
    input.value = value; input.dispatchEvent(new Event('input', { bubbles: true }));
  }, number);
  await page.click('#call-btn');
}
async function ringing(page) {
  await page.waitForFunction(() => !document.getElementById('incoming-actions').classList.contains('hidden'), { timeout: 18000 });
}
async function hasPackets(page) {
  return page.evaluate(async () => {
    const session = typeof currentSession === 'undefined' ? null : currentSession;
    if (!session || !session.connection) return false;
    const stats = await session.connection.getStats();
    let received = 0, sent = 0, energy = 0;
    stats.forEach(stat => {
      if (stat.type === 'inbound-rtp' && (stat.kind || stat.mediaType) === 'audio') {
        received += stat.packetsReceived || 0; energy += stat.totalAudioEnergy || 0;
      }
      if (stat.type === 'outbound-rtp' && (stat.kind || stat.mediaType) === 'audio') sent += stat.packetsSent || 0;
    });
    const audio = document.getElementById('remote-audio').srcObject;
    return received > 5 && sent > 5 && energy > 0 && audio && audio.getAudioTracks().length > 0;
  });
}
async function mediaBoth(a, b) {
  await until(async () => await hasPackets(a) && await hasPackets(b), 'bidirectional RTP and decoded audio energy', 18000);
  check('both real peers exchange RTP, decode non-silent audio and attach a remote audio track', true);
}
async function closed(page) {
  await page.waitForFunction(() => document.getElementById('active-call-view').classList.contains('hidden'), { timeout: 10000 });
}
function newInviteAfter(index) {
  return records.slice(index).find(item => item.method === 'INVITE');
}
function finalsFor(callId) {
  return records.filter(item => item.callId === callId && /^SIP\/2\.0 [2-6]\d\d/.test(item.first) && / INVITE$/i.test(item.cseq));
}

async function main() {
  const server = http.createServer((req, res) => {
    const name = new URL(req.url, 'http://localhost').pathname === '/phone' ? 'phone.html' : path.basename(new URL(req.url, 'http://localhost').pathname);
    if (!/^(phone\.html|phone\.js|phone\.css|jssip\.min\.js|sw\.js|manifest\.json)$/.test(name)) { res.writeHead(404).end(); return; }
    const type = name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : name.endsWith('.json') ? 'application/json' : 'text/html';
    res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
    res.end(fs.readFileSync(path.join(root, name)));
  });
  const wss = new WebSocketServer({ server, path: '/ws', handleProtocols: protocols => protocols.has('sip') ? 'sip' : false });
  wss.on('connection', socket => socket.on('message', data => onSip(socket, data)));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  let executablePath = process.env.CHROMIUM, browser;
  let args = ['--no-sandbox', '--disable-setuid-sandbox'];
  if (!executablePath) {
    const chromium = require('@sparticuz/chromium');
    executablePath = await chromium.executablePath(); args = chromium.args;
  }
  try {
    browser = await puppeteer.launch({ executablePath, headless: true, args: [...args.filter(arg => arg !== '--single-process'),
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required'] });
    // Separate contexts emulate different users/devices and prevent local
    // settings from the first page auto-signing the second page into 101.
    const a = await (await browser.createBrowserContext()).newPage();
    const b = await (await browser.createBrowserContext()).newPage();
    await b.setViewport({ width: 320, height: 500 });
    const errors = [];
    for (const page of [a, b]) {
      page.on('pageerror', error => { errors.push(error.message); if (process.env.PHONE_REALCHECK_TRACE) console.error('PAGE', error); });
      if (process.env.PHONE_REALCHECK_TRACE) page.on('console', message => console.log('BROWSER', message.text()));
    }
    await signIn(a, base, '101'); await signIn(b, base, '102');
    check('both real JsSIP clients register through SIP-over-WebSocket', contacts.has('101') && contacts.has('102'));

    // Stall completion at BOTH ends: the app must release actual candidates
    // after a bounded interval to send the initial INVITE and then the 200 OK.
    await a.evaluate(() => { window.__stallIce = true; });
    await b.evaluate(() => { window.__stallIce = true; });
    let start = Date.now(), index = records.length;
    await dial(a, '102'); await ringing(b);
    const invite = newInviteAfter(index);
    check('stalled outgoing gathering still sends an actual INVITE after the candidate wait',
      !!invite && invite.time - start >= 7000 && invite.time - start < 16000 && /a=candidate:/i.test(invite.message));
    start = Date.now(); await b.click('#answer-btn');
    const layout = await b.evaluate(() => {
      const status = document.getElementById('active-status');
      const hangup = document.getElementById('hangup-btn').getBoundingClientRect();
      const box = status.getBoundingClientRect();
      return { text: status.textContent, style: getComputedStyle(status).whiteSpace,
        statusBottom: box.bottom, hangupBottom: hangup.bottom, screen: innerHeight };
    });
    check('microphone preparation and End Call remain visible on a small viewport',
      /microphone/i.test(layout.text) && layout.style !== 'nowrap'
      && layout.statusBottom <= layout.screen && layout.hangupBottom <= layout.screen);
    await until(() => finalsFor(invite.callId).some(item => /^SIP\/2\.0 200/.test(item.first)), 'incoming 200 OK');
    const ok = finalsFor(invite.callId).find(item => /^SIP\/2\.0 200/.test(item.first));
    check('stalled incoming gathering sends a real 200 OK with candidates before PBX timeout',
      ok.time - start >= 7000 && ok.time - start < 16000 && /a=candidate:/i.test(ok.message));
    await mediaBoth(a, b);
    await a.click('#hangup-btn'); await closed(a); await closed(b);

    // Reverse call direction with normal browser gathering.
    await a.evaluate(() => { window.__stallIce = false; });
    await b.evaluate(() => { window.__stallIce = false; });
    index = records.length; await dial(b, '101'); await ringing(a);
    const reverse = newInviteAfter(index); await a.click('#answer-btn');
    await until(() => finalsFor(reverse.callId).some(item => /^SIP\/2\.0 200/.test(item.first)), 'reverse 200 OK');
    await mediaBoth(a, b);
    check('the reverse call direction also sends INVITE and 200 OK', !!reverse);
    await b.click('#hangup-btn'); await closed(a); await closed(b);

    // Real JsSIP emits failed BEFORE getusermediafailed. The useful message
    // must survive the closing call screen and stay visible on the dialer.
    await b.evaluate(() => { navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError')); });
    index = records.length; await dial(a, '102'); await ringing(b);
    const denied = newInviteAfter(index); await b.click('#answer-btn'); await closed(b);
    await until(() => finalsFor(denied.callId).length > 0, 'mic-denied SIP response');
    check('real JsSIP microphone denial remains visible and produces no 200 OK',
      await b.$eval('#dial-hint', el => /Microphone unavailable/.test(el.textContent) && el.classList.contains('error'))
      && !finalsFor(denied.callId).some(item => /^SIP\/2\.0 200/.test(item.first)));
    await closed(a);

    // JsSIP's incoming createAnswer failure emits a peerconnection error
    // before its own 500 reply; verify the UI recovers and the wire sees only
    // one final response, not duplicate competing final SIP responses.
    await b.evaluate(() => {
      delete navigator.mediaDevices.getUserMedia;
      RTCPeerConnection.prototype.createAnswer = () => Promise.reject(new Error('test SDP failure'));
    });
    index = records.length; await dial(a, '102'); await ringing(b);
    const sdpFailure = newInviteAfter(index); await b.click('#answer-btn'); await closed(b);
    await until(() => finalsFor(sdpFailure.callId).length > 0, 'SDP failure response');
    await wait(300);
    check('incoming SDP failure closes visibly and sends only one final SIP response',
      await b.$eval('#dial-hint', el => /Could not prepare call audio/.test(el.textContent))
      && finalsFor(sdpFailure.callId).length === 1
      && !finalsFor(sdpFailure.callId).some(item => /^SIP\/2\.0 200/.test(item.first)));
    await closed(a);

    // With no candidate and no completion event JsSIP exposes no ready()
    // callback. The separate watchdog must close the call and explain it.
    await a.evaluate(() => { window.__stallIce = true; window.__noCandidates = true; });
    index = records.length; await dial(a, '102');
    await a.waitForFunction(() => document.getElementById('active-call-view').classList.contains('hidden'), { timeout: 26000 });
    check('real JsSIP with no ICE candidates times out visibly without sending a bogus INVITE',
      !newInviteAfter(index) && await a.$eval('#dial-hint', el => /setup timed out/i.test(el.textContent)));
    check('no uncaught browser exceptions during real signalling/media', errors.length === 0 || (console.error(errors), false));
  } finally {
    if (browser) await browser.close();
    wss.clients.forEach(client => client.terminate()); wss.close(); server.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
