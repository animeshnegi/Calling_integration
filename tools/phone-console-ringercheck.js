/* Targeted console/softphone ringer relay regression.
 * npm install --no-save jsdom && node tools/phone-console-ringercheck.js
 */
const assert = require('node:assert/strict');
const { boot, settle } = require('./uicheck.js');

async function main() {
  const env = boot(); await settle(300);
  const stats = { started: 0, stopped: 0, disconnected: 0, samples: null };
  env.w.AudioContext = class {
    constructor() { this.state = 'running'; this.sampleRate = 8000; this.destination = {}; }
    createBuffer(channels, length) {
      stats.samples = new Float32Array(length);
      return { getChannelData: () => stats.samples };
    }
    createBufferSource() {
      return { connect() {}, start() { stats.started++; }, stop() { stats.stopped++; },
        disconnect() { stats.disconnected++; } };
    }
    resume() { this.state = 'running'; return Promise.resolve(); }
  };
  const first = { closed: false }, second = { closed: false }, unknown = { closed: false };
  env.w.first = first; env.w.second = second;
  env.w.eval('softphoneWindows.push({key:"101",win:window.first},{key:"102",win:window.second}); prepareConsoleRinger()');
  function message(source, type, origin = 'http://localhost') {
    const event = new env.w.Event('message');
    Object.defineProperties(event, { source: { value: source }, origin: { value: origin }, data: { value: { type } } });
    env.w.dispatchEvent(event);
  }
  message(unknown, 'eip-softphone:ring-start');
  message(first, 'eip-softphone:ring-start', 'https://evil.example');
  assert.equal(stats.started, 0, 'unknown windows and cross-origin messages must not ring');
  message(first, 'eip-softphone:ring-start');
  assert.equal(stats.started, 1, 'a known blocked phone starts the console ringer');
  assert.ok(stats.samples.some(sample => Math.abs(sample) > .1), 'ringtone has audible samples');
  message(first, 'eip-softphone:ring-start');
  message(second, 'eip-softphone:ring-start');
  assert.equal(stats.started, 1, 'repeated/multiple phones share one tone');
  message(first, 'eip-softphone:ring-stop');
  assert.equal(stats.stopped, 0, 'another ringing phone keeps the shared tone playing');
  message(second, 'eip-softphone:ring-stop');
  assert.equal(stats.stopped, 1, 'the tone stops when the last phone stops');
  assert.equal(stats.disconnected, 1);
  message(first, 'eip-softphone:ring-start');
  first.closed = true; env.w.eval('updateConsoleRinger()');
  assert.equal(stats.stopped, 2, 'closing a ringing phone cannot leave a stuck tone');
  assert.equal(env.errors.length, 0, env.errors.join('\n'));
  env.dom.window.close();
  console.log('PASS  authenticated console fallback, overlapping phones and cleanup');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
