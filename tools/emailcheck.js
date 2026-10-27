/* Real admin.js/jsdom email settings regressions.
 * NODE_PATH=/path/to/node_modules node tools/emailcheck.js
 */
const assert = require('node:assert/strict');
const {boot, settle, load} = require('./uicheck');
let checks = 0;
const check = (label, condition) => { assert.ok(condition, label); checks++; console.log(`PASS ${label}`); };
const posts = (env, route) => env.seen.filter(row => row.url === route && row.method === 'POST');

async function run() {
  const fixture = load('state.json');
  fixture.email_config = {enabled:true, from_email:'old@example.com', from_name:'Old', has_api_key:true,
                          transcription_enabled:false};
  const env = boot({state:fixture});
  const {w,d} = env;
  const input = (id, value) => {
    const node = d.getElementById(id);
    if (typeof value === 'boolean') node.checked = value; else node.value = value;
    node.dispatchEvent(new w.Event('input', {bubbles:true}));
  };
  try {
    await settle();
    check('saved sender is rendered', d.getElementById('sendgrid-from').value === 'old@example.com');
    input('sendgrid-from', 'replacement@example.com');
    input('sendgrid-name', 'New sender');
    input('email-enabled', false);
    input('sendgrid-key', ' SG.new-key ');
    input('transcription-enabled', true);
    input('profile-email', 'draft@example.com');
    fixture.email_deliveries = [{mailbox:'101',recipient:'<img>@example.com',status:'delivered',transcription_status:'resource_limit',attempts:1}];
    await w.eval('loadState()');
    check('background state changes preserve draft sender/name', d.getElementById('sendgrid-from').value === 'replacement@example.com' && d.getElementById('sendgrid-name').value === 'New sender');
    check('background refresh preserves disabled automatic mail and transcription opt-in', !d.getElementById('email-enabled').checked && d.getElementById('transcription-enabled').checked);
    check('background refresh never clears replacement credentials', d.getElementById('sendgrid-key').value === ' SG.new-key ');
    check('background refresh preserves profile email draft', d.getElementById('profile-email').value === 'draft@example.com');
    check('acceptance is not labelled inbox delivery', d.getElementById('email-delivery-list').textContent.includes('Accepted by SendGrid'));
    check('transcription status is displayed safely', d.getElementById('email-delivery-list').textContent.includes('resource limit') && !d.querySelector('#email-delivery-list img'));
    input('sendgrid-test','test@example.com');
    const realFetch = w.fetch;
    let failSave = false, failTest = false;
    w.fetch = async (url, options={}) => {
      if (url === '/admin/api/email-config' && options.method === 'POST') {
        if (failSave) return {ok:false,status:400,json:async()=>({error:'Invalid API key format'})};
        const patch = JSON.parse(options.body);
        fixture.email_config = {enabled:patch.enabled,from_email:patch.from_email,from_name:patch.from_name,
                                has_api_key:true,transcription_enabled:patch.transcription_enabled};
        // Keep the harness's request capture and route behavior.
        await realFetch(url,options);
        return {ok:true,status:200,json:async()=>({ok:true,email_config:fixture.email_config})};
      }
      if (url === '/admin/api/email-config/test' && failTest) {
        await realFetch(url,options);
        return {ok:false,status:502,json:async()=>({error:'SendGrid HTTP 401: Save a replacement SendGrid key.'})};
      }
      return realFetch(url,options);
    };
    d.getElementById('test-email').click();
    check('save/test controls lock against double submissions', d.getElementById('test-email').disabled && d.getElementById('sendgrid-key').disabled);
    d.getElementById('test-email').click();
    await settle();
    const saved = posts(env,'/admin/api/email-config');
    const tests = posts(env,'/admin/api/email-config/test');
    check('test performs one save then one test',saved.length === 1 && tests.length === 1 && env.seen.indexOf(saved[0]) < env.seen.indexOf(tests[0]));
    const payload = JSON.parse(saved[0].body);
    check('replacement key and current sender are saved, not ignored', payload.api_key === 'SG.new-key' && payload.from_email === 'replacement@example.com');
    check('local transcription is explicitly opted in without any transcription credential', payload.transcription_enabled && !('transcription_api_key' in payload));
    check('test does not silently enable automatic notifications', !payload.enabled);
    check('saved credentials are removed from input fields', !d.getElementById('sendgrid-key').value);
    check('transcription API key input has been removed',!d.getElementById('transcription-key') && d.getElementById('email-form').textContent.includes('Local, offline'));
    check('test success clearly distinguishes provider acceptance',d.getElementById('email-feedback').textContent.includes('Check your inbox/spam'));
    check('controls are restored after completion',!d.getElementById('test-email').disabled && !d.getElementById('sendgrid-key').disabled);
    failSave = true;
    input('sendgrid-key','Bearer bad-key');
    d.getElementById('test-email').click();
    await settle();
    check('failed save never sends a test using stale credentials',posts(env,'/admin/api/email-config/test').length === 1);
    check('failed save keeps the draft for correction', d.getElementById('sendgrid-key').value === 'Bearer bad-key' && d.getElementById('email-feedback').textContent.includes('Invalid API key'));
    failSave = false; failTest = true;
    input('sendgrid-key','SG.corrected');
    d.getElementById('test-email').click();
    await settle();
    check('provider errors persist inline instead of a generic failure',d.getElementById('email-feedback').textContent.includes('SendGrid HTTP 401'));
    check('a failed test does not undo a successful key save',!d.getElementById('sendgrid-key').value && fixture.email_config.has_api_key);
    check('no browser runtime errors', env.errors.length === 0);
  } finally { env.dom.window.close(); }
  console.log(`\n${checks} email UI checks passed.`);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
