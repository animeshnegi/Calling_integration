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
  fixture.email_config = {provider:'sendgrid',enabled:true, from_email:'old@example.com', from_name:'Old', has_api_key:true,
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
    check('saved sender is rendered', d.getElementById('email-from').value === 'old@example.com');
    input('email-from', 'replacement@example.com');
    input('email-name', 'New sender');
    input('email-enabled', false);
    input('email-provider', 'elasticemail');
    d.getElementById('email-provider').dispatchEvent(new w.Event('change', {bubbles:true}));
    check('switching providers asks for the selected provider key', d.getElementById('email-key-status').textContent.includes('Elastic Email'));
    input('email-key', ' ELASTIC-new-key ');
    input('transcription-enabled', true);
    input('profile-email', 'draft@example.com');
    fixture.email_deliveries = [{mailbox:'101',recipient:'<img>@example.com',status:'delivered',transcription_status:'resource_limit',attempts:1}];
    await w.eval('loadState()');
    check('background state changes preserve draft sender/name', d.getElementById('email-from').value === 'replacement@example.com' && d.getElementById('email-name').value === 'New sender');
    check('background refresh preserves disabled automatic mail and transcription opt-in', !d.getElementById('email-enabled').checked && d.getElementById('transcription-enabled').checked);
    check('background refresh preserves the selected provider', d.getElementById('email-provider').value === 'elasticemail');
    check('background refresh never clears replacement credentials', d.getElementById('email-key').value === ' ELASTIC-new-key ');
    check('background refresh preserves profile email draft', d.getElementById('profile-email').value === 'draft@example.com');
    check('acceptance is not labelled inbox delivery', d.getElementById('email-delivery-list').textContent.includes('Accepted by email provider'));
    check('transcription status is displayed safely', d.getElementById('email-delivery-list').textContent.includes('resource limit') && !d.querySelector('#email-delivery-list img'));
    input('email-test','test@example.com');
    const realFetch = w.fetch;
    let failSave = false, failTest = false;
    w.fetch = async (url, options={}) => {
      if (url === '/admin/api/email-config' && options.method === 'POST') {
        if (failSave) return {ok:false,status:400,json:async()=>({error:'Invalid API key format'})};
        const patch = JSON.parse(options.body);
        fixture.email_config = {provider:patch.provider,enabled:patch.enabled,from_email:patch.from_email,from_name:patch.from_name,
                                has_api_key:true,transcription_enabled:patch.transcription_enabled};
        // Keep the harness's request capture and route behavior.
        await realFetch(url,options);
        return {ok:true,status:200,json:async()=>({ok:true,email_config:fixture.email_config})};
      }
      if (url === '/admin/api/email-config/test' && failTest) {
        await realFetch(url,options);
        return {ok:true,status:200,json:async()=>({ok:false,error:'Elastic Email HTTP 401: Check the API key.'})};
      }
      return realFetch(url,options);
    };
    d.getElementById('test-email').click();
    check('save/test controls lock against double submissions', d.getElementById('test-email').disabled && d.getElementById('email-key').disabled && d.getElementById('email-provider').disabled);
    d.getElementById('test-email').click();
    await settle();
    const saved = posts(env,'/admin/api/email-config');
    const tests = posts(env,'/admin/api/email-config/test');
    check('test performs one save then one test',saved.length === 1 && tests.length === 1 && env.seen.indexOf(saved[0]) < env.seen.indexOf(tests[0]));
    const payload = JSON.parse(saved[0].body);
    check('replacement key and current sender are saved, not ignored', payload.provider === 'elasticemail' && payload.api_key === 'ELASTIC-new-key' && payload.from_email === 'replacement@example.com');
    check('test opts in to a structured result for proxies', JSON.parse(tests[0].body).result_only === true);
    check('local transcription is explicitly opted in without any transcription credential', payload.transcription_enabled && !('transcription_api_key' in payload));
    check('test does not silently enable automatic notifications', !payload.enabled);
    check('saved credentials are removed from input fields', !d.getElementById('email-key').value);
    check('transcription API key input has been removed',!d.getElementById('transcription-key') && d.getElementById('email-form').textContent.includes('Local, offline'));
    check('test success clearly distinguishes provider acceptance',d.getElementById('email-feedback').textContent.includes('Test accepted by Elastic Email') && d.getElementById('email-feedback').textContent.includes('Check your inbox/spam'));
    check('controls are restored after completion',!d.getElementById('test-email').disabled && !d.getElementById('email-key').disabled);
    failSave = true;
    input('email-key','Bearer bad-key');
    d.getElementById('test-email').click();
    await settle();
    check('failed save never sends a test using stale credentials',posts(env,'/admin/api/email-config/test').length === 1);
    check('failed save keeps the draft for correction', d.getElementById('email-key').value === 'Bearer bad-key' && d.getElementById('email-feedback').textContent.includes('Invalid API key'));
    failSave = false; failTest = true;
    input('email-key','elastic.corrected');
    d.getElementById('test-email').click();
    await settle();
    check('HTTP 200 test failures persist inline instead of showing false success',d.getElementById('email-feedback').textContent.includes('Elastic Email HTTP 401'));
    check('a failed test does not undo a successful key save',!d.getElementById('email-key').value && fixture.email_config.has_api_key);
    check('no browser runtime errors', env.errors.length === 0);
  } finally { env.dom.window.close(); }
  console.log(`\n${checks} email UI checks passed.`);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
