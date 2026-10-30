/* Real Chromium popup autoplay/console-ringer regression. No SIP server needed.
 * npm install --no-save puppeteer-core @sparticuz/chromium
 * CHROMIUM=/path/to/chromium node tools/phone-popup-audiocheck.js
 */
const assert = require('node:assert/strict');
const fs = require('fs');
const http = require('http');
const path = require('path');
const puppeteer = require('puppeteer-core');
const ROOT = path.join(__dirname, '..');
const WEB = path.join(ROOT, 'web');

async function main() {
  const admin = fs.readFileSync(path.join(WEB, 'admin.js'), 'utf8');
  const start = admin.indexOf('const softphoneWindows = [];');
  const end = admin.indexOf('/* The softphone window is the size', start);
  const relayStart = admin.indexOf('function sendSoftphone(entry)');
  const relayEnd = admin.indexOf('/* Rotate the password', relayStart);
  assert.ok(start >= 0 && end > start && relayStart > end && relayEnd > relayStart);
  // Exercise the real console ringer and message listener in an isolated page,
  // without the administrative API, accounts or production credentials.
  const consoleHtml = `<!doctype html><button id="open">Open softphone</button><script>
${admin.slice(start, end)}
${admin.slice(relayStart, relayEnd)}
document.getElementById('open').onclick = () => {
  prepareConsoleRinger();
  window.testPhone = window.open('/phone?connect=extension', 'test-phone');
  softphoneWindows.push({key:'fixture',win:window.testPhone,ready:false,payload:null});
};
</script>`;
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/') { res.writeHead(200, {'Content-Type':'text/html'}).end(consoleHtml); return; }
    if (url.pathname === '/phone-hold.wav') {
      res.writeHead(200, {'Content-Type':'audio/wav'}).end(fs.readFileSync(path.join(ROOT,'asterisk/moh/engineerip-hold.wav'))); return;
    }
    const name = url.pathname === '/phone' ? 'phone.html' : path.basename(url.pathname);
    if (!/^(phone\.html|phone\.js|phone\.css|jssip\.min\.js|sw\.js|manifest\.json)$/.test(name)) { res.writeHead(404).end(); return; }
    const type = name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : name.endsWith('.json') ? 'application/json' : 'text/html';
    res.writeHead(200, {'Content-Type':type, 'Cache-Control':'no-store'}).end(fs.readFileSync(path.join(WEB,name)));
  });
  await new Promise((resolve,reject)=>server.once('error',reject).listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    let executablePath = process.env.CHROMIUM, args = ['--no-sandbox','--disable-setuid-sandbox'];
    if (!executablePath) {
      const imported = require('@sparticuz/chromium'); const chromium = imported.default || imported;
      executablePath = await chromium.executablePath(); args = chromium.args;
    }
    browser = await puppeteer.launch({executablePath, headless:true,
      args:[...args.filter(arg=>arg !== '--single-process'), '--autoplay-policy=document-user-activation-required']});
    const parent = await browser.newPage();
    const errors = []; parent.on('pageerror', error => errors.push(error.message));
    await parent.goto(`http://127.0.0.1:${server.address().port}/`);
    const [popup] = await Promise.all([new Promise(resolve=>parent.once('popup',resolve)), parent.click('#open')]);
    popup.on('pageerror', error => errors.push(error.message));
    await popup.waitForFunction(() => typeof startRingtone === 'function');
    // Force the new document into the autoplay-blocked state, as happens on
    // popup browsers where the opener's click is not inherited by the phone.
    await popup.evaluate(async () => { const context = getRingerContext(); await context.suspend(); startRingtone({}); });
    await parent.waitForFunction(() => !!consoleRingerSource, {timeout:5000});
    assert.ok(await parent.evaluate(() => consoleRingerContext.state === 'running'
      && consoleRingerSource.loop && consoleRingerSource.buffer.getChannelData(0).some(x => Math.abs(x) > .1)));
    assert.ok(await popup.$eval('#ringer-notice', node => !node.classList.contains('hidden')));
    await popup.evaluate(() => stopRingtone());
    await parent.waitForFunction(() => !consoleRingerSource, {timeout:5000});
    assert.equal(errors.length, 0, errors.join('\n'));
    console.log('PASS  real popup blocked audio rings through its trusted console and stops cleanly');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
}
main().catch(error=>{console.error(error);process.exitCode=1});
