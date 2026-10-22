/* Focused regression checks for the two-editor routing studio.
 * NODE_PATH=/path/to/node_modules node tools/routingcheck.js
 */
const assert = require('node:assert/strict');
const {boot, settle, chooseScope, load} = require('./uicheck');
let checks = 0;
function check(label, condition) { assert.ok(condition, label); checks++; console.log(`PASS ${label}`); }
const event = (env, element, kind='change') => element.dispatchEvent(new env.w.Event(kind, {bubbles:true}));
const posts = env => env.seen.filter(row=>row.url==='/admin/api/call-routes' && row.method==='POST');

async function run(isAdmin) {
  // The older general console fixture predates number-scoped flow keys.
  // Give these scenarios explicit same-company identities, as the API now saves.
  const fixture = load(isAdmin ? 'state.json' : 'state-customer.json');
  for (const route of fixture.call_routes || []) for (const node of route.route.nodes) {
    if (node.extensions) node.extensions = node.extensions.map(value => {
      const row = fixture.extensions.find(row => Number(row.owner_user_id) === Number(route.owner_user_id) && String(row.digits || row.extension).split('@')[0] === String(value).split('@')[0]);
      return row ? row.key || row.extension : value;
    });
  }
  const env = boot({isAdmin, state:fixture});
  const {w,d} = env;
  w.confirm=()=>true;
  try {
    await settle(300);
    w.eval("showPage('routing')");
    if (isAdmin) chooseScope(w,d,{customer:2,number:'+13025550001'});
    await settle(150);
    const role = isAdmin?'admin':'customer';
    const picker=d.getElementById(isAdmin?'route-target':'route-extension');
    check(`${role}: number mode offers only number journeys`, [...picker.options].every(option=>option.value.startsWith('number:')));
    check(`${role}: number target is not silently redirected to its primary desk`,w.eval('currentFlowKey()')==='number:+13025550001');
    d.getElementById('extension-flow-tab').click();
    check(`${role}: extension mode has dedicated ring/no-answer controls`,!!d.getElementById('desk-ring-enabled') && !!d.getElementById('desk-no-answer'));
    check(`${role}: extension picker offers only desks`, [...picker.options].every(option=>option.value.startsWith('extension:')));
    check(`${role}: extension picker includes this customer's other primary line`, [...picker.options].some(option=>option.value==='extension:102@+13025550002'));
    check(`${role}: sources offer this customer's three numbers only`, [...d.querySelectorAll('[name=desk-ring-source]')].length===3);
    check(`${role}: primary direct ring assignment starts selected`,d.querySelector('[name=desk-ring-source][value="+13025550001"]').checked);
    check(`${role}: no-answer destination cannot select itself`, ![...d.getElementById('desk-next-extension').options].some(option=>option.value==='101@+13025550001'));
    const second=d.querySelector('[name=desk-ring-source][value="+13025550002"]');second.checked=true;event(env,second);
    const action=d.getElementById('desk-no-answer');action.value='forward';event(env,action);
    check(`${role}: external forwarding fields replace mailbox fields`,!d.querySelector('[data-desk-action=forward]').hidden && d.querySelector('[data-desk-action=voicemail]').hidden);
    const ring=d.getElementById('desk-ring-enabled');ring.checked=false;event(env,ring);
    d.getElementById('desk-forward-number').value='not-a-number';
    d.getElementById('save-route').click();await settle(40);
    check(`${role}: invalid forwarding is rejected before POST`,posts(env).length===0);
    d.getElementById('desk-forward-number').value='+13025550123';
    await w.eval('loadState()');
    check(`${role}: background refresh preserves the unsaved desk draft`,d.getElementById('desk-forward-number').value==='+13025550123' && !d.getElementById('desk-ring-enabled').checked);
    d.getElementById('save-route').click();await settle(250);
    const payload=JSON.parse(posts(env).at(-1).body);
    check(`${role}: saves the extension target`,payload.target_type==='extension' && payload.target==='101@+13025550001');
    check(`${role}: saves ring off without discarding the no-answer policy`,payload.route.nodes[0].ring_enabled===false && payload.route.nodes[0].no_answer==='forward');
    check(`${role}: saves both ring sources, not number ownership`,payload.ring_sources.includes('+13025550001') && payload.ring_sources.includes('+13025550002'));
    check(`${role}: external forwarding is a real validated flow node`,payload.route.nodes.some(node=>node.type==='forward' && node.phone==='+13025550123'));
    d.getElementById('number-flow-tab').click();
    const menu=d.querySelector('.routing-library [data-node-type=ivr]');menu.click();
    check(`${role}: IVR config opens without a scope error`,d.getElementById('flow-config-modal').classList.contains('open') && !!d.querySelector('[name=prompt]'));
    w.eval('renderFlowPalette();renderFlowPalette()');
    d.querySelector('[data-add-map]').click();
    check(`${role}: mapping listener is registered once across rerenders`,d.querySelectorAll('#ivr-map-rows .map-row').length===1);
    check(`${role}: explicit IVR mapping can choose an owned desk on another primary line`, [...d.querySelector('[name=map_target]').options].some(option=>option.value==='102@+13025550002'));
    d.getElementById('flow-config-cancel').click();
    d.querySelector('[data-node-type=ring_group]').click();
    check(`${role}: ring destinations are accessible checkboxes across owned lines`,d.querySelectorAll('[name=extensions][type=checkbox]').length===2);
    check(`${role}: no runtime errors`,env.errors.length===0);
  } finally {env.dom.window.close();}
}
(async()=>{await run(true);await run(false);console.log(`\n${checks} routing checks passed`);})().catch(error=>{console.error(error);process.exit(1);});
