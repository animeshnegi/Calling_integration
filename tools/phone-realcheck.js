/* Real-browser WebRTC/JsSIP regression (no live PBX required).
 *
 * Install puppeteer-core and ws, and provide a Chrome/Chromium executable as
 * CHROMIUM, or also install @sparticuz/chromium. Example on a machine with
 * Chrome:
 *   npm install --no-save puppeteer-core ws
 *   CHROMIUM=/path/to/chromium node tools/phone-realcheck.js
 * For a bundled headless build, install @sparticuz/chromium as well.
 *
 * Two real Chrome pages use the shipped phone.js and vendored JsSIP. A tiny
 * loopback SIP-over-WebSocket relay below handles REGISTER and forwards SIP
 * dialogs; it is deliberately not an Asterisk replacement. A loopback STUN
 * responder removes any dependency on Internet STUN. Actual browser
 * RTCPeerConnections, fake microphone devices, DTLS-SRTP and RTP packets are
 * used. We suppress ICE's completion notification to exercise the app's slow
 * gathering timeout; actual candidates and media transport stay real.
 * This does NOT test the deployed VM, carrier or Internet NAT/firewall.
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const dgram = require('dgram');
const crypto = require('crypto');
const assert = require('assert/strict');
const puppeteer = require('puppeteer-core');
const { WebSocketServer, WebSocket } = require('ws');

const root = path.join(__dirname, '..', 'web');
const records = [], contacts = new Map(), dialogs = new Map(), challenges = new Set();
let privateConnectionLine = false, rewrittenConnectionLines = 0, challengeNextInvite = false;
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
function challengeInvite(socket, message) {
  const response = [
    'SIP/2.0 401 Unauthorized', 'Via: ' + header(message, 'Via'),
    'From: ' + header(message, 'From'), 'To: ' + header(message, 'To') + ';tag=challenge',
    'Call-ID: ' + header(message, 'Call-ID'), 'CSeq: ' + header(message, 'CSeq'),
    'WWW-Authenticate: Digest realm="asterisk",nonce="test-nonce",algorithm=MD5,qop="auth"',
    'Content-Length: 0', '', '',
  ].join('\r\n');
  socket.send(response);
}
function validInviteDigest(message, username) {
  const fields = {};
  for (const match of header(message, 'Authorization').matchAll(/(\w+)=(?:"([^"]*)"|([^,\s]+))/g)) {
    fields[match[1].toLowerCase()] = match[2] === undefined ? match[3] : match[2];
  }
  const md5 = text => crypto.createHash('md5').update(text).digest('hex');
  if (fields.username !== username || fields.realm !== 'asterisk' || fields.nonce !== 'test-nonce'
      || fields.qop !== 'auth' || !fields.nc || !fields.cnonce || !fields.uri) return false;
  const ha1 = md5(`${username}:asterisk:test-password`), ha2 = md5(`INVITE:${fields.uri}`);
  return fields.response === md5(`${ha1}:${fields.nonce}:${fields.nc}:${fields.cnonce}:${fields.qop}:${ha2}`);
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
    socket.user = username;
    socket.contactUser = (header(message, 'Contact').match(/sip:([^@;>]+)@/i) || [])[1];
    contacts.set(username, socket); replyOk(socket, message); return;
  }
  if (method === 'INVITE' && challengeNextInvite && !header(message, 'Authorization')) {
    challengeNextInvite = false; challenges.add(callId); challengeInvite(socket, message); return;
  }
  // The ACK to an authentication challenge is transaction-specific; it is
  // not the ACK to the eventual successful dialog.
  if (method === 'ACK' && challenges.has(callId) && !dialogs.has(callId)) return;
  if (method === 'INVITE' && challenges.has(callId)) {
    if (!validInviteDigest(message, socket.user)) throw new Error('Invalid browser INVITE digest');
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
  if (peer.readyState === WebSocket.OPEN) {
    let forwarded = message;
    if (privateConnectionLine) {
      const split = forwarded.indexOf('\r\n\r\n');
      if (split >= 0 && /Content-Type:\s*application\/sdp/i.test(forwarded.slice(0, split))) {
        let head = forwarded.slice(0, split), body = forwarded.slice(split + 4);
        body = body.replace(/c=IN IP4 [^\r\n]+/g, () => { rewrittenConnectionLines++; return 'c=IN IP4 172.21.0.2'; });
        head = head.replace(/Content-Length:\s*\d+/i, 'Content-Length: ' + Buffer.byteLength(body));
        forwarded = head + '\r\n\r\n' + body;
      }
    }
    peer.send(forwarded);
  }
}

// Sanitized version of the production Asterisk 22 WebSocket INVITE. The
// request target is the random contact username from the real REGISTER; the
// caller, addresses, ICE credentials and fingerprint below are only fixtures.
function asteriskInvite(target, fromHost, contactHost, id) {
  const sdp = [
    'v=0', 'o=- 123456 123456 IN IP4 172.21.0.2', 's=Asterisk',
    'c=IN IP4 172.21.0.2', 't=0 0', 'a=msid-semantic:WMS *',
    'a=group:BUNDLE audio-0', 'm=audio 10046 UDP/TLS/RTP/SAVPF 9 0 8 101',
    'a=connection:new', 'a=setup:actpass',
    'a=fingerprint:SHA-256 ' + Array(32).fill('AA').join(':'),
    'a=ice-ufrag:0123456789abcdef0123456789abcdef',
    'a=ice-pwd:0123456789abcdef0123456789abcdef',
    'a=candidate:H123 1 UDP 2130706431 198.51.100.42 10046 typ host',
    'a=rtpmap:9 G722/8000', 'a=rtpmap:0 PCMU/8000', 'a=rtpmap:8 PCMA/8000',
    'a=rtpmap:101 telephone-event/8000', 'a=fmtp:101 0-16', 'a=ptime:20',
    'a=maxptime:140', 'a=sendrecv', 'a=rtcp-mux',
    'a=ssrc:161924846 cname:test-cname',
    'a=msid:00000000-0000-4000-8000-000000000001 00000000-0000-4000-8000-000000000002',
    'a=rtcp-fb:* transport-cc', 'a=mid:audio-0', '',
  ].join('\r\n');
  return [
    `INVITE sip:${target}@172.21.0.1:36120;transport=WS SIP/2.0`,
    `Via: SIP/2.0/WS 172.21.0.2:8088;rport;branch=z9hG4bK-${id};alias`,
    `From: <sip:CALLER_101@${fromHost}>;tag=from-${id}`,
    `To: <sip:${target}@172.21.0.1>`,
    `Contact: <sip:asterisk@${contactHost}:5060;transport=ws>`,
    `Call-ID: ${id}`, 'CSeq: 23765 INVITE',
    'Allow: OPTIONS, REGISTER, SUBSCRIBE, NOTIFY, PUBLISH, INVITE, ACK, BYE, CANCEL, UPDATE, PRACK, MESSAGE, INFO, REFER',
    'Supported: 100rel, timer, replaces, norefersub, histinfo',
    'Session-Expires: 1800', 'Min-SE: 90', 'Max-Forwards: 70',
    'User-Agent: Asterisk PBX 22.11.0', 'Content-Type: application/sdp',
    'Content-Length: ' + Buffer.byteLength(sdp), '', sdp,
  ].join('\r\n');
}

// Complete a simulated PBX-initiated dialog using the browser's real Contact
// and To tag. The fixture deliberately uses the same socket as the INVITE.
function asteriskDialogRequest(invite, answer, method, cseq) {
  const contact = (header(answer, 'Contact').match(/<([^>]+)>/) || [])[1];
  assert.ok(contact, 'browser answer Contact');
  return [
    `${method} ${contact} SIP/2.0`,
    `Via: SIP/2.0/WS 172.21.0.2:8088;rport;branch=z9hG4bK-${method.toLowerCase()}-${cseq};alias`,
    'From: ' + header(invite, 'From'), 'To: ' + header(answer, 'To'),
    'Call-ID: ' + header(invite, 'Call-ID'), `CSeq: ${cseq} ${method}`,
    'Max-Forwards: 70', 'Content-Length: 0', '', '',
  ].join('\r\n');
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
async function signIn(page, base, name, stunPort) {
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
  // Use the local responder, not a public (or intentionally black-holed)
  // STUN server. The separate shim below simulates delayed completion.
  await page.evaluate(port => { iceServers = [{ urls: `stun:127.0.0.1:${port}` }]; }, stunPort);
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
  try {
    await until(async () => await hasPackets(a) && await hasPackets(b), 'bidirectional RTP and decoded audio energy', 18000);
  } catch (error) {
    if (process.env.PHONE_REALCHECK_TRACE) {
      for (const page of [a, b]) console.log('MEDIA', await page.evaluate(async () => {
        const pc = currentSession?.connection;
        if (!pc) return 'no peer connection';
        const items = [];
        (await pc.getStats()).forEach(s => {
          if (['inbound-rtp', 'outbound-rtp', 'candidate-pair'].includes(s.type))
            items.push({ type: s.type, kind: s.kind, state: s.state, packetsReceived: s.packetsReceived,
              packetsSent: s.packetsSent, totalAudioEnergy: s.totalAudioEnergy });
        });
        const candidates = sdp => (sdp || '').split(/\r?\n/).filter(line => line.startsWith('a=candidate:'));
        return { ice: pc.iceConnectionState, connection: pc.connectionState, items,
          localCandidates: candidates(pc.localDescription?.sdp), remoteCandidates: candidates(pc.remoteDescription?.sdp) };
      }));
    }
    throw error;
  }
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

// Minimal RFC 5389 Binding success for an IPv4 request. This responder is
// bound to loopback only and is solely a test fixture, not a production STUN
// service. It keeps the real ICE agent healthy while the page shim delays
// the completion events observed by JsSIP.
function respondToStun(socket, message, peer) {
  if (message.length < 20 || message.readUInt16BE(0) !== 0x0001 || message.readUInt32BE(4) !== 0x2112a442) return;
  const address = peer.address.split('.').map(Number);
  if (address.length !== 4 || address.some(octet => !Number.isInteger(octet) || octet < 0 || octet > 255)) return;
  const response = Buffer.alloc(32);
  response.writeUInt16BE(0x0101, 0); // Binding success
  response.writeUInt16BE(12, 2);
  message.copy(response, 4, 4, 20); // magic cookie and transaction id
  response.writeUInt16BE(0x0020, 20); // XOR-MAPPED-ADDRESS
  response.writeUInt16BE(8, 22);
  response[25] = 1; // IPv4
  response.writeUInt16BE(peer.port ^ 0x2112, 26);
  for (let i = 0; i < 4; i++) response[28 + i] = address[i] ^ message[4 + i];
  socket.send(response, peer.port, peer.address);
}

async function main() {
  const server = http.createServer((req, res) => {
    const name = new URL(req.url, 'http://localhost').pathname === '/phone' ? 'phone.html' : path.basename(new URL(req.url, 'http://localhost').pathname);
    if (name === 'phone-hold.wav') {
      res.writeHead(200, { 'Content-Type': 'audio/wav', 'Cache-Control': 'no-store' });
      res.end(fs.readFileSync(path.join(__dirname, '..', 'asterisk', 'moh', 'engineerip-hold.wav'))); return;
    }
    if (!/^(phone\.html|phone\.js|phone\.css|jssip\.min\.js|sw\.js|manifest\.json)$/.test(name)) { res.writeHead(404).end(); return; }
    const type = name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : name.endsWith('.json') ? 'application/json' : 'text/html';
    res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
    res.end(fs.readFileSync(path.join(root, name)));
  });
  const wss = new WebSocketServer({ server, path: '/ws', handleProtocols: protocols => protocols.has('sip') ? 'sip' : false });
  wss.on('connection', socket => socket.on('message', data => onSip(socket, data)));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  let executablePath = process.env.CHROMIUM, browser, stun;
  let args = ['--no-sandbox', '--disable-setuid-sandbox'];
  try {
    stun = dgram.createSocket('udp4');
    stun.on('message', (message, peer) => respondToStun(stun, message, peer));
    await new Promise((resolve, reject) => {
      stun.once('error', reject);
      stun.bind(0, '127.0.0.1', () => { stun.removeListener('error', reject); resolve(); });
    });
    if (!executablePath) {
      const imported = require('@sparticuz/chromium');
      const chromium = imported.default || imported;
      executablePath = await chromium.executablePath(); args = chromium.args;
    }
    browser = await puppeteer.launch({ executablePath, headless: true, args: [...args.filter(arg => arg !== '--single-process'),
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required',
      '--disable-features=WebRtcHideLocalIpsWithMdns'] });
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
    await signIn(a, base, '101', stun.address().port); await signIn(b, base, '102', stun.address().port);
    check('both real JsSIP clients register through SIP-over-WebSocket', contacts.has('101') && contacts.has('102'));

    // Asterisk can fall back to a single-label Docker ID in From and Contact.
    // JsSIP 3.10.1 rejects either numeric-leading hostname before it can send
    // even 100 Trying. Reproduce the observed wire shape before proving that
    // a valid browser endpoint from_domain rings and can answer the same SDP.
    const target = contacts.get('102');
    assert.ok(target.contactUser, 'REGISTER contact username');
    const pbx = {
      readyState: WebSocket.OPEN,
      send(message) { if (/^BYE /i.test(message)) replyOk(target, message); },
    };
    for (const [id, fromHost, contactHost] of [
      ['bad-from', '123abc456def', 'sip.test'],
      ['bad-contact', 'sip.test', '123abc456def'],
    ]) {
      dialogs.set(id, [pbx, target]);
      target.send(asteriskInvite(target.contactUser, fromHost, contactHost, id));
      await wait(500);
      check('JsSIP reproduces the dropped Asterisk INVITE with invalid ' + id.slice(4) + ' host',
        !records.some(record => record.callId === id && /^SIP\/2\.0 /.test(record.first))
        && await b.$eval('#incoming-actions', el => el.classList.contains('hidden')));
    }
    const fixedId = 'valid-browser-from-domain';
    dialogs.set(fixedId, [pbx, target]);
    const fixedInvite = asteriskInvite(target.contactUser, 'sip.test', 'sip.test', fixedId);
    target.send(fixedInvite);
    await ringing(b);
    await until(() => records.some(record => record.callId === fixedId && /^SIP\/2\.0 180/.test(record.first)), 'Asterisk-style 180 Ringing');
    check('valid SIP host in the Asterisk-style From and Contact produces 100 and 180 and displays the incoming call',
      records.some(record => record.callId === fixedId && /^SIP\/2\.0 100/.test(record.first))
      && records.some(record => record.callId === fixedId && /^SIP\/2\.0 180/.test(record.first)));
    await until(() => b.evaluate(() => !!ringerSource), 'incoming ringtone');
    check('real Chromium starts a local Web Audio ringtone with non-zero samples',
      await b.evaluate(() => !!ringerSource && ringerSource.loop && ringerContext.state === 'running'
        && ringerSource.buffer.getChannelData(0).some(sample => Math.abs(sample) > 0.01)));
    await b.click('#answer-btn');
    check('the ringtone stops immediately on answer', await b.evaluate(() => ringerSource === null));
    await until(() => finalsFor(fixedId).some(item => /^SIP\/2\.0 200/.test(item.first)), 'Asterisk-style SDP answer');
    const fixedAnswer = finalsFor(fixedId).find(item => /^SIP\/2\.0 200/.test(item.first)).message;
    check('real Chromium accepts the captured Asterisk SDP shape and sends a 200 OK', true);
    await b.evaluate(() => { window.__pbxConfirmed = false; currentSession.on('confirmed', () => { window.__pbxConfirmed = true; }); });
    target.send(asteriskDialogRequest(fixedInvite, fixedAnswer, 'ACK', 23765));
    await until(() => b.evaluate(() => window.__pbxConfirmed), 'Asterisk-style ACK confirms dialog');
    await b.click('#hangup-btn'); await closed(b);
    await until(() => records.some(record => record.callId === fixedId && record.method === 'BYE'), 'browser BYE to Asterisk-style contact');
    check('the simulated PBX ACK confirms the dialog and browser BYE targets the corrected Contact',
      records.some(record => record.callId === fixedId && /^BYE sip:asterisk@sip\.test(?::5060)?;transport=ws /i.test(record.first)));

    // Exercise the other hang-up direction too: the browser must match the
    // dialog and acknowledge a PBX BYE after the synthetic ACK.
    const remoteByeId = 'valid-browser-remote-bye';
    const remoteByeInvite = asteriskInvite(target.contactUser, 'sip.test', 'sip.test', remoteByeId);
    dialogs.set(remoteByeId, [pbx, target]); target.send(remoteByeInvite);
    await ringing(b); await b.click('#answer-btn');
    await until(() => finalsFor(remoteByeId).some(item => /^SIP\/2\.0 200/.test(item.first)), 'second Asterisk-style SDP answer');
    const remoteByeAnswer = finalsFor(remoteByeId).find(item => /^SIP\/2\.0 200/.test(item.first)).message;
    await b.evaluate(() => { window.__pbxConfirmed = false; currentSession.on('confirmed', () => { window.__pbxConfirmed = true; }); });
    target.send(asteriskDialogRequest(remoteByeInvite, remoteByeAnswer, 'ACK', 23765));
    await until(() => b.evaluate(() => window.__pbxConfirmed), 'second Asterisk-style ACK');
    target.send(asteriskDialogRequest(remoteByeInvite, remoteByeAnswer, 'BYE', 23766));
    await closed(b);
    await until(() => records.some(record => record.callId === remoteByeId && /^SIP\/2\.0 200/.test(record.first) && / BYE$/i.test(record.cseq)), '200 to PBX BYE');
    check('the browser accepts and acknowledges a simulated PBX BYE in the corrected dialog', true);

    // A broken or unavailable audio device must not throw out of the
    // newRTCSession callback and suppress the SIP 180 response.
    await b.evaluate(async () => {
      if (ringerContext) await ringerContext.close();
      ringerContext = null;
      window.__originalRingerContext = window.AudioContext;
      window.AudioContext = class { constructor() { throw new Error('test audio device failure'); } };
    });
    const noToneId = 'ringer-unavailable';
    dialogs.set(noToneId, [pbx, target]);
    target.send(asteriskInvite(target.contactUser, 'sip.test', 'sip.test', noToneId));
    await ringing(b);
    await until(() => records.some(record => record.callId === noToneId && /^SIP\/2\.0 180/.test(record.first)), '180 without ringtone');
    await until(() => b.$eval('#enable-ringing-btn', el => !el.classList.contains('hidden')), 'unavailable ringer prompt');
    check('unavailable local ringtone does not suppress SIP 180 or the answer controls',
      await b.$eval('#enable-ringing-btn', el => !el.classList.contains('hidden') && /unavailable/i.test(el.textContent)));
    const ringLayout = await b.evaluate(() => {
      const answer = document.getElementById('answer-btn').getBoundingClientRect();
      const decline = document.getElementById('decline-btn').getBoundingClientRect();
      const prompt = document.getElementById('enable-ringing-btn').getBoundingClientRect();
      return { answer: answer.bottom, decline: decline.bottom, prompt: prompt.bottom, screen: innerHeight };
    });
    check('ringing prompt and answer/decline remain visible on a small viewport',
      ringLayout.answer <= ringLayout.screen && ringLayout.decline <= ringLayout.screen && ringLayout.prompt <= ringLayout.screen);
    await b.click('#decline-btn'); await closed(b);
    await b.evaluate(() => { window.AudioContext = window.__originalRingerContext; });

    // Stall the completion events at BOTH ends. The app should release as
    // soon as the local STUN responder supplies a reflexive candidate, instead
    // of waiting for the old eight-second deadline.
    await a.evaluate(() => { window.__stallIce = true; });
    await b.evaluate(() => { window.__stallIce = true; });
    // The VM's WebSocket peer is local to Asterisk, so its SDP c= can stay
    // private even with usable ICE candidates. Prove real browsers still use
    // those candidates by deliberately changing c= on both loopback legs.
    privateConnectionLine = true;
    let start = Date.now(), index = records.length;
    await dial(a, '102'); await ringing(b);
    const invite = newInviteAfter(index);
    check('a reflexive candidate sends the real outgoing INVITE promptly despite stalled completion',
      !!invite && invite.time - start < 5000 && /a=candidate:.* typ srflx/i.test(invite.message));
    await until(() => a.evaluate(() => !!ringbackSource), 'outgoing ringback');
    check('a real browser plays generated ringback while the extension rings',
      await a.evaluate(() => !!ringbackSource && ringbackSource.loop && ringbackSource.buffer.getChannelData(0).some(x => Math.abs(x) > .05)));
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
    check('a reflexive candidate also sends the real 200 OK promptly',
      ok.time - start < 5000 && /a=candidate:.* typ srflx/i.test(ok.message));
    await mediaBoth(a, b);
    check('a private SDP connection line does not prevent ICE-selected bidirectional audio in the loopback test',
      rewrittenConnectionLines >= 2);
    check('the real browser stops ringback after the other party answers', await a.evaluate(() => !ringbackSource));
    await a.evaluate(() => { window.__stallIce = false; });
    await b.evaluate(() => { window.__stallIce = false; });
    const holdIndex = records.length;
    await a.click('#hold-btn');
    await b.waitForFunction(() => !document.getElementById('hold-notice').classList.contains('hidden')
      && /other person put you on hold/i.test(document.getElementById('hold-notice').textContent));
    await until(() => b.evaluate(() => !document.getElementById('hold-audio').paused
      && document.getElementById('hold-audio').currentTime > 0.1), 'browser hold-music playback');
    check('real JsSIP sends hold SDP and the remote browser shows hold while playing the song',
      records.slice(holdIndex).some(item => item.method === 'INVITE' && /a=sendonly/i.test(item.message)));
    await a.waitForFunction(() => !document.getElementById('hold-btn').disabled);
    await a.click('#hold-btn');
    await b.waitForFunction(() => document.getElementById('hold-notice').classList.contains('hidden')
      && document.getElementById('hold-audio').paused);
    check('unhold clears the remote notice and stops its hold music', true);
    await a.click('#hangup-btn'); await closed(a); await closed(b);
    privateConnectionLine = false;

    // Reverse call direction with normal browser gathering.
    await a.evaluate(() => { window.__stallIce = false; });
    await b.evaluate(() => { window.__stallIce = false; });
    challengeNextInvite = true;
    index = records.length; await dial(b, '101'); await ringing(a);
    const reverse = newInviteAfter(index);
    check('browser outgoing INVITE survives an Asterisk-style 401 digest challenge',
      !!reverse && records.filter(record => record.callId === reverse.callId && record.method === 'INVITE').length === 2
      && records.some(record => record.callId === reverse.callId && record.method === 'INVITE'
        && validInviteDigest(record.message, '102')));
    await a.click('#answer-btn');
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
    if (stun) { try { stun.close(); } catch (error) { if (error.code !== 'ERR_SOCKET_DGRAM_NOT_RUNNING') throw error; } }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
