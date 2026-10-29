#!/usr/bin/env node
/*
 * Optional real-PBX loopback regression. It creates an isolated Asterisk
 * configuration from the real Python renderer, a temporary SQLite database,
 * and throwaway SIP credentials. Asterisk, the browser and pjsua stay on the
 * same machine; this is NOT a test of the deployed VM, public NAT or carrier.
 *
 * Linux prerequisites: Asterisk 22 with PJSIP/WebSocket/DTLS-SRTP modules,
 * pjsua, a C compiler, the Python app requirements, puppeteer-core and Chrome.
 * Point the test at binaries rather than installing or modifying a real PBX:
 *   ASTERISK_BIN=/path/to/asterisk ASTERISK_MODULES=/path/to/asterisk/modules \
 *   PJSUA_BIN=/path/to/pjsua PYTHON=/path/to/python node tools/phone-asteriskcheck.js
 * Set CHROMIUM=/path/to/chrome, or install @sparticuz/chromium@131.0.1.
 * Asterisk's shared-library directories must already be in LD_LIBRARY_PATH
 * if needed.
 */
'use strict';

const assert = require('assert/strict');
const { spawn, spawnSync, execFileSync } = require('child_process');
const dgram = require('dgram');
const fs = require('fs');
const http = require('http');
const net = require('net');
const os = require('os');
const path = require('path');
const puppeteer = require('puppeteer-core');

const ROOT = path.resolve(__dirname, '..');
const WEB = path.join(ROOT, 'web');
const ASTERISK = process.env.ASTERISK_BIN;
const MODULES = process.env.ASTERISK_MODULES;
const PJSUA = process.env.PJSUA_BIN;
const PYTHON = process.env.PYTHON || 'python3';
const BAD_HOST = '123abc456def';
const PASSWORD = 'test-password';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const header = (message, name) => {
  const match = message.match(new RegExp('^' + name + ':\\s*(.*)$', 'im'));
  return match ? match[1].trim() : '';
};

function check(label, value) {
  assert.ok(value, label);
  console.log('PASS  ' + label);
}
async function until(fn, description, timeout = 18000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await fn()) return;
    await wait(75);
  }
  throw new Error('Timed out: ' + description);
}
async function freeTcpPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}
async function freeUdpPort() {
  const socket = dgram.createSocket('udp4');
  await new Promise((resolve, reject) => socket.once('error', reject).bind(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  return port;
}
function write(file, text) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
}
function staticServer() {
  return http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const name = url.pathname === '/phone' ? 'phone.html' : path.basename(url.pathname);
    if (!/^(phone\.html|phone\.js|phone\.css|jssip\.min\.js|sw\.js|manifest\.json)$/.test(name)) {
      res.writeHead(404).end(); return;
    }
    const type = name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css'
      : name.endsWith('.json') ? 'application/json' : 'text/html';
    res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
    res.end(fs.readFileSync(path.join(WEB, name)));
  });
}
async function capture(page) {
  const frames = [], cdp = await page.createCDPSession();
  await cdp.send('Network.enable');
  for (const [direction, event] of [['in', 'Network.webSocketFrameReceived'], ['out', 'Network.webSocketFrameSent']]) {
    cdp.on(event, item => {
      if (item.response?.payloadData) frames.push({ direction, message: item.response.payloadData });
    });
  }
  return frames;
}
function frame(frames, index, direction, start, callId = '') {
  return frames.slice(index).find(item => item.direction === direction && start.test(item.message)
    && (!callId || header(item.message, 'Call-ID') === callId));
}
async function signIn(page, base, wsPort, username) {
  await page.goto(base + '/phone', { waitUntil: 'domcontentloaded' });
  await page.type('#login-username', username);
  await page.type('#login-password', PASSWORD);
  await page.type('#login-domain', 'sip.test');
  await page.type('#login-wss', `ws://127.0.0.1:${wsPort}/ws`);
  await page.click('#login-form button[type=submit]');
  try {
    await page.waitForFunction(() => !document.getElementById('phone-app').classList.contains('hidden'), { timeout: 15000 });
  } catch (error) {
    console.error('Phone login status:', await page.$eval('#login-status', el => el.textContent));
    throw error;
  }
  // Keep this loopback test independent of public STUN servers.
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
async function closed(page) {
  await page.waitForFunction(() => document.getElementById('active-call-view').classList.contains('hidden'), { timeout: 12000 });
}
async function audioFlows(page) {
  return page.evaluate(async () => {
    if (!currentSession?.connection) return false;
    const stats = await currentSession.connection.getStats();
    let received = 0, sent = 0, energy = 0;
    stats.forEach(stat => {
      if (stat.type === 'inbound-rtp' && (stat.kind || stat.mediaType) === 'audio') {
        received += stat.packetsReceived || 0; energy += stat.totalAudioEnergy || 0;
      }
      if (stat.type === 'outbound-rtp' && (stat.kind || stat.mediaType) === 'audio') sent += stat.packetsSent || 0;
    });
    const stream = document.getElementById('remote-audio').srcObject;
    return received > 10 && sent > 10 && energy > 0 && stream && stream.getAudioTracks().length > 0;
  });
}
async function stopChild(child, input = '') {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  if (input && child.stdin.writable) child.stdin.write(input);
  else child.kill('SIGTERM');
  await Promise.race([new Promise(resolve => child.once('exit', resolve)), wait(4000)]);
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
}

async function main() {
  if (process.platform !== 'linux' || !ASTERISK || !MODULES || !PJSUA) {
    throw new Error('Linux plus ASTERISK_BIN, ASTERISK_MODULES and PJSUA_BIN are required; see the comment at the top of this script.');
  }
  if (!fs.existsSync(ASTERISK) || !fs.existsSync(MODULES) || !fs.existsSync(PJSUA)) {
    throw new Error('The configured Asterisk binary, module directory or pjsua binary does not exist.');
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'eip-phone-pbx-'));
  const etc = path.join(tmp, 'etc'), data = path.join(tmp, 'data');
  const socketDir = path.join(tmp, 'run'), logs = path.join(tmp, 'logs'), spool = path.join(tmp, 'spool');
  const config = path.join(etc, 'asterisk.conf'), dynamic = path.join(etc, 'pjsip.dynamic.conf');
  let ast, pjsua, browser, server, astLog = '', plainLog = '';
  const errors = [];
  try {
    for (const dir of [etc, data, path.join(data, 'keys', 'keys'), socketDir, logs, spool]) fs.mkdirSync(dir, { recursive: true });
    const result = spawnSync(PYTHON, [path.join(ROOT, 'tools/phone-asterisk-fixture.py'), etc], {
      cwd: ROOT, encoding: 'utf8', env: { ...process.env, PYTHONPATH: [ROOT, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) },
    });
    if (result.status !== 0) throw new Error('Unable to generate test configuration: ' + result.stderr);
    const ids = JSON.parse(result.stdout);
    const original = fs.readFileSync(dynamic, 'utf8');
    const wsPort = await freeTcpPort(), sipPort = await freeUdpPort();
    let clientPort;
    do { clientPort = await freeUdpPort(); } while (clientPort === sipPort);
    const rtpStart = 20000 + 2 * Math.floor(Math.random() * 12000);

    write(config, `[directories]\nastetcdir => ${etc}\nastmoddir => ${MODULES}\nastvarlibdir => ${data}\nastdbdir => ${data}\nastkeydir => ${path.join(data, 'keys')}\nastdatadir => ${data}\nastagidir => ${path.join(data, 'agi-bin')}\nastspooldir => ${spool}\nastrundir => ${socketDir}\nastlogdir => ${logs}\n[options]\nverbose = 1\n`);
    write(path.join(etc, 'modules.conf'), fs.readFileSync(path.join(ROOT, 'asterisk/config/modules.conf'), 'utf8'));
    write(path.join(etc, 'pjsip.conf'), `[global]\ntype=global\nendpoint_identifier_order=ip,username,auth_username,anonymous\n[transport-udp]\ntype=transport\nprotocol=udp\nbind=127.0.0.1:${sipPort}\n[transport-wss]\ntype=transport\nprotocol=wss\nbind=0.0.0.0\n#include pjsip.dynamic.conf\n`);
    write(path.join(etc, 'http.conf'), `[general]\nenabled=yes\nbindaddr=127.0.0.1\nbindport=${wsPort}\n`);
    write(path.join(etc, 'rtp.conf'), `[general]\nrtpstart=${rtpStart}\nrtpend=${rtpStart + 100}\nicesupport=yes\n`);
    write(path.join(etc, 'logger.conf'), '[general]\n[logfiles]\nconsole => notice,warning,error,verbose\n');

    // Simulate Docker's numeric-leading container hostname for this Asterisk
    // process alone, without changing the host's hostname or its privileges.
    const preloadSource = path.join(tmp, 'hostname.c'), preload = path.join(tmp, 'hostname.so');
    write(preloadSource, `#include <string.h>\n#include <stddef.h>\nint gethostname(char *name, size_t len) { const char *h = "${BAD_HOST}"; if (len) { strncpy(name, h, len - 1); name[len - 1] = 0; } return 0; }\n`);
    execFileSync(process.env.CC || 'cc', ['-shared', '-fPIC', '-o', preload, preloadSource]);
    const astEnv = { ...process.env, LD_PRELOAD: preload };
    ast = spawn(ASTERISK, ['-C', config, '-f', '-n', '-v'], { env: astEnv, stdio: ['ignore', 'pipe', 'pipe'] });
    let astError = null;
    ast.on('error', error => { astError = error; });
    const addLog = chunk => { astLog = (astLog + chunk.toString()).slice(-24000); };
    ast.stdout.on('data', addLog); ast.stderr.on('data', addLog);
    await until(() => {
      if (astError) throw astError;
      if (ast.exitCode !== null || ast.signalCode !== null) throw new Error('Temporary Asterisk exited during startup.');
      return /Asterisk Ready\./.test(astLog);
    }, 'Asterisk startup', 30000);
    const cli = command => execFileSync(ASTERISK, ['-C', config, '-rx', command], { encoding: 'utf8', env: astEnv });
    check('actual Asterisk has the generated WebRTC endpoint', /from_domain\s*:\s*sip\.test/i.test(cli(`pjsip show endpoint ${ids.browser_102}`)));

    server = staticServer();
    await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    let executablePath = process.env.CHROMIUM, args = ['--no-sandbox', '--disable-setuid-sandbox'];
    if (!executablePath) {
      const imported = require('@sparticuz/chromium');
      const chromium = imported.default || imported;
      executablePath = await chromium.executablePath(); args = chromium.args;
    }
    browser = await puppeteer.launch({ executablePath, headless: true, args: [...args.filter(arg => arg !== '--single-process'),
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required',
      '--disable-features=WebRtcHideLocalIpsWithMdns'] });
    const contextA = await browser.createBrowserContext(), contextB = await browser.createBrowserContext();
    const a = await contextA.newPage(), b = await contextB.newPage();
    for (const page of [a, b]) page.on('pageerror', error => errors.push(error.message));
    const af = await capture(a), bf = await capture(b);
    await signIn(a, base, wsPort, ids.browser_101);
    await signIn(b, base, wsPort, ids.browser_102);
    check('two real browsers register with actual Asterisk', true);

    // Reproduce the reported failure with the previously rendered endpoint.
    write(dynamic, original.replace(/^from_domain=sip\.test\n/gm, ''));
    assert.match(cli('module reload res_pjsip.so'), /reloaded successfully/i);
    check('baseline endpoint has no from_domain', /from_domain\s*:\s*$/m.test(cli(`pjsip show endpoint ${ids.browser_102}`)));
    let index = bf.length;
    await dial(a, '102');
    await until(() => frame(bf, index, 'in', /^INVITE /i), 'Asterisk INVITE with old configuration');
    const bad = frame(bf, index, 'in', /^INVITE /i).message, badId = header(bad, 'Call-ID');
    await wait(1000);
    check('actual Asterisk uses the invalid numeric-leading hostname in From and Contact without from_domain',
      header(bad, 'From').includes('@' + BAD_HOST) && header(bad, 'Contact').includes('@' + BAD_HOST));
    check('shipped JsSIP drops that INVITE without a response or incoming screen',
      !frame(bf, index, 'out', /^SIP\/2\.0 /i, badId)
      && await b.$eval('#incoming-actions', el => el.classList.contains('hidden')));
    await a.click('#hangup-btn'); await closed(a); await wait(500);

    // Restore the actual renderer output and reload the live endpoint.
    write(dynamic, original);
    assert.match(cli('module reload res_pjsip.so'), /reloaded successfully/i);
    check('live PJSIP reload applies the generated browser from_domain', /from_domain\s*:\s*sip\.test/i.test(cli(`pjsip show endpoint ${ids.browser_102}`)));
    index = bf.length;
    await dial(a, '102'); await ringing(b);
    await until(() => frame(bf, index, 'in', /^INVITE /i), 'corrected Asterisk INVITE');
    const invite = frame(bf, index, 'in', /^INVITE /i).message, id = header(invite, 'Call-ID');
    check('actual Asterisk emits valid From and Contact for the browser',
      /@sip\.test/i.test(header(invite, 'From')) && /@sip\.test/i.test(header(invite, 'Contact')));
    await until(() => frame(bf, index, 'out', /^SIP\/2\.0 180/i, id), '180 Ringing');
    check('browser sends 100 and 180 and displays the incoming call', !!frame(bf, index, 'out', /^SIP\/2\.0 100/i, id));
    await b.click('#answer-btn');
    await until(() => bf.slice(index).some(item => item.direction === 'out' && /^SIP\/2\.0 200/i.test(item.message)
      && header(item.message, 'Call-ID') === id && / INVITE$/i.test(header(item.message, 'CSeq'))), 'browser answer');
    await until(() => frame(bf, index, 'in', /^ACK /i, id), 'Asterisk ACK');
    await until(async () => await audioFlows(a) && await audioFlows(b), 'browser-to-browser audio through Asterisk', 25000);
    check('actual Asterisk answers with ACK and relays decoded audio in both directions', true);
    await b.click('#hangup-btn'); await closed(a); await closed(b);
    await until(() => frame(bf, index, 'out', /^BYE /i, id), 'browser BYE');
    await until(() => bf.slice(index).some(item => item.direction === 'in' && /^SIP\/2\.0 200/i.test(item.message)
      && header(item.message, 'Call-ID') === id && / BYE$/i.test(header(item.message, 'CSeq'))), 'Asterisk 200 to BYE');
    check('actual Asterisk acknowledges the browser BYE', true);

    const reverseIndex = af.length;
    await dial(b, '101'); await ringing(a);
    const reverse = frame(af, reverseIndex, 'in', /^INVITE /i).message, reverseId = header(reverse, 'Call-ID');
    await a.click('#answer-btn');
    await until(async () => await audioFlows(a) && await audioFlows(b), 'reverse browser audio through Asterisk', 25000);
    await b.click('#hangup-btn'); await closed(a); await closed(b);
    await until(() => frame(af, reverseIndex, 'in', /^BYE /i, reverseId), 'PBX BYE');
    await until(() => af.slice(reverseIndex).some(item => item.direction === 'out' && /^SIP\/2\.0 200/i.test(item.message)
      && header(item.message, 'Call-ID') === reverseId && / BYE$/i.test(header(item.message, 'CSeq'))), 'browser 200 to BYE');
    check('reverse browser call has two-way audio and acknowledges the PBX BYE', true);
    await contextB.close();
    await until(() => !cli('pjsip show contacts').includes(ids.browser_102 + '/'), 'closed browser contact removal', 10000);

    // A real plain UDP/RTP SIP client stands in for a Zoiper-style endpoint.
    // It loops incoming audio, allowing the browser to verify decoded energy.
    const uri = `sip:127.0.0.1:${sipPort}`;
    pjsua = spawn(PJSUA, [`--id=sip:${ids.plain_102}@127.0.0.1:${sipPort}`, `--registrar=${uri}`,
      '--realm=*', `--username=${ids.plain_102}`, `--password=${PASSWORD}`, `--local-port=${clientPort}`,
      '--bound-addr=127.0.0.1', '--auto-answer=200', '--auto-loop', '--null-audio', '--no-tcp', '--log-level=4', '--app-log-level=4']);
    const addPlain = chunk => { plainLog = (plainLog + chunk.toString()).slice(-100000); };
    let plainError = null;
    pjsua.on('error', error => { plainError = error; });
    pjsua.stdout.on('data', addPlain); pjsua.stderr.on('data', addPlain);
    await until(() => {
      if (plainError) throw plainError;
      if (pjsua.exitCode !== null || pjsua.signalCode !== null) throw new Error('Plain SIP test client exited.');
      return /registration success, status=200/i.test(plainLog);
    }, 'plain SIP registration');
    check('plain SIP client registers with actual Asterisk', true);
    index = af.length;
    let logIndex = plainLog.length;
    await dial(a, '102');
    await until(() => af.slice(index).some(item => item.direction === 'in' && /^SIP\/2\.0 200/i.test(item.message)
      && / INVITE$/i.test(header(item.message, 'CSeq'))), 'browser-to-plain SIP answer');
    await until(() => audioFlows(a), 'browser-to-plain SIP audio', 25000);
    check('browser-to-plain SIP call reaches Asterisk and exchanges decoded audio', /Request msg INVITE/i.test(plainLog.slice(logIndex)));
    await a.click('#hangup-btn'); await closed(a);
    await until(() => /\[DISCONNECTED\]|state changed to DISCONNECTED/i.test(plainLog.slice(logIndex)), 'plain SIP disconnect', 10000);

    index = af.length;
    pjsua.stdin.write(`m\nsip:101@127.0.0.1:${sipPort}\n`);
    await ringing(a);
    await until(() => frame(af, index, 'in', /^INVITE /i), 'plain SIP to browser INVITE');
    const plainInvite = frame(af, index, 'in', /^INVITE /i).message, plainId = header(plainInvite, 'Call-ID');
    check('plain SIP client causes Asterisk to ring the browser with valid SIP hosts',
      /@sip\.test/i.test(header(plainInvite, 'From')) && /@sip\.test/i.test(header(plainInvite, 'Contact')));
    await a.click('#answer-btn');
    await until(() => frame(af, index, 'in', /^ACK /i, plainId), 'ACK to browser from Asterisk');
    await until(() => audioFlows(a), 'plain SIP to browser audio', 25000);
    check('plain SIP to browser call answers and exchanges decoded audio', true);
    pjsua.stdin.write('h\n'); await closed(a);
    await until(() => frame(af, index, 'in', /^BYE /i, plainId), 'Asterisk BYE after plain SIP hangup');
    check('plain SIP hangup reaches the browser as a PBX BYE', true);
    check('no uncaught browser exceptions', errors.length === 0);
  } catch (error) {
    // SIP credentials are throwaway fixtures. Avoid printing captured SIP
    // frames or authorization headers even on failure.
    if (astLog) console.error('Temporary Asterisk log tail:\n' + astLog.slice(-6000));
    throw error;
  } finally {
    await stopChild(pjsua, 'q\n');
    if (browser) await browser.close();
    if (server) await new Promise(resolve => server.close(resolve));
    await stopChild(ast);
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
