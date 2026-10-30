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
  } finally {await settle(25);env.dom.window.close();}
}

async function runSelection(isAdmin) {
  const fixture = load(isAdmin ? 'state.json' : 'state-customer.json');
  fixture.phone_numbers.push({number:'+13025550999',owner_user_id:2,active:0,description:'Disabled line'});
  fixture.call_routes[0].route.nodes.push({type:'ivr',configured:true,prompt:'Welcome',keys:[{key:'1',target_type:'extension',target:'101@+13025550001'}]});
  const env = boot({isAdmin,state:fixture});
  const {w,d} = env;
  const role = isAdmin?'admin':'customer';
  let confirmations=0, accept=false;
  w.confirm=()=>{confirmations++;return accept;};
  const key=()=>w.eval('currentFlowKey()');
  const options=()=>[...d.querySelectorAll('[data-workflow-key]')];
  const pick=target=>options().find(option=>option.dataset.workflowKey===target).click();
  const search=value=>{d.getElementById('routing-workflow-search').value=value;event(env,d.getElementById('routing-workflow-search'),'input');};
  try {
    await settle(250);w.eval("showPage('routing')");
    if(isAdmin)chooseScope(w,d,{customer:2,number:'+13025550001'});
    check(`${role}: exact workflow labels`,d.querySelector('#number-flow-tab b').textContent==='Number Flow' && d.querySelector('#extension-flow-tab b').textContent==='Extension Call Handling');
    check(`${role}: chooser includes all three owned active numbers`,options().length===3 && options().every(option=>!option.dataset.workflowKey.includes('999')));
    check(`${role}: one resource selected, matching editor context`,options().filter(option=>option.getAttribute('aria-selected')==='true').length===1 && d.getElementById('routing-editor-title').textContent.includes('+13025550001'));
    w.eval("flowNodes.find(node=>node.type==='ivr').keys.push({key:'2'})");
    check(`${role}: nested draft mapping never mutates saved state`,w.eval("savedFlowFor('number','+13025550001').route.nodes.find(node=>node.type==='ivr').keys.length")===1);
    search('1 (302) 555-0002');
    check(`${role}: search ignores phone punctuation`,options().length===1 && options()[0].dataset.workflowKey==='number:+13025550002');
    search('spare');check(`${role}: search includes number labels`,options().length===1 && options()[0].textContent.includes('spare'));
    search('nonexistent');check(`${role}: no matches does not discard selected workflow`,options().length===0 && !d.getElementById('routing-no-results').hidden && key()==='number:+13025550001');
    search('');
    const first=options()[0];first.focus();first.dispatchEvent(new w.KeyboardEvent('keydown',{key:'End',bubbles:true}));
    check(`${role}: keyboard End reaches last resource without switching draft`,d.activeElement===options().at(-1) && key()==='number:+13025550001');
    d.activeElement.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Home',bubbles:true}));
    check(`${role}: keyboard Home returns to first resource`,d.activeElement===options()[0]);
    w.eval('markRoutingDirty()');pick('number:+13025550002');
    check(`${role}: cancel resource change preserves dirty workflow`,key()==='number:+13025550001' && w.eval('routingDirty'));
    const draft=w.eval('JSON.stringify(flowNodes)');
    const shortcut=d.createElement('button');shortcut.dataset.wsRoute='+13025550002';d.body.appendChild(shortcut);shortcut.click();shortcut.remove();
    check(`${role}: workspace shortcut respects discard cancellation`,key()==='number:+13025550001' && w.eval('JSON.stringify(flowNodes)')===draft && w.eval('routingDirty'));
    const before=confirmations;d.getElementById('number-flow-tab').click();pick(key());
    check(`${role}: same tab or selected resource never discards draft`,confirmations===before && w.eval('routingDirty'));
    d.getElementById('extension-flow-tab').click();
    check(`${role}: cancel mode change preserves selected tab`,key()==='number:+13025550001' && d.getElementById('number-flow-tab').getAttribute('aria-selected')==='true');
    if(isAdmin){chooseScope(w,d,{customer:5});check(`${role}: cancel customer change preserves owner and context`,w.eval('routingOwner()')===2 && key()==='number:+13025550001' && d.querySelector('#page-routing .scope-customer-select').value==='2');}
    accept=true;pick('number:+13025550002');
    check(`${role}: accepted switch updates exact number and clears dirty state`,key()==='number:+13025550002' && !w.eval('routingDirty') && d.getElementById('routing-editor-title').textContent==='+13025550002');
    const fetch=w.fetch;let release;
    w.fetch=async(url,opts={})=>{if(url==='/admin/api/call-routes' && opts.method==='POST')await new Promise(resolve=>{release=resolve;});return fetch(url,opts);};
    d.getElementById('save-route').click();await settle(15);
    check(`${role}: saving freezes editor and both mode tabs`,d.getElementById('routing-workspace').hasAttribute('inert') && d.getElementById('number-flow-tab').disabled && d.getElementById('extension-flow-tab').disabled);
    w.eval("focusRouteTarget('number:+13025550001')");
    check(`${role}: cannot navigate to a different target while saving`,key()==='number:+13025550002');
    release();await settle(230);
    check(`${role}: save locks always release`,!d.getElementById('routing-workspace').hasAttribute('inert') && !d.getElementById('extension-flow-tab').disabled);
    const payload=JSON.parse(posts(env).at(-1).body);
    check(`${role}: chooser saves only the chosen number`,payload.target==='+13025550002' && payload.target_type==='number' && payload.phone_number==='+13025550002');
    w.fetch=async(url,opts={})=>{if(url==='/admin/api/call-routes' && opts.method==='POST')throw new Error('Simulated save failure');return fetch(url,opts);};
    w.eval('markRoutingDirty()');d.getElementById('save-route').click();await settle(50);
    check(`${role}: failed save keeps draft and unlocks controls`,w.eval('routingDirty') && !d.getElementById('routing-workspace').hasAttribute('inert') && !d.getElementById('save-route').disabled);
    w.fetch=fetch;
    d.getElementById('extension-flow-tab').click();
    check(`${role}: extension cards list both owned lines with primary context`,options().length===2 && options().every(option=>option.textContent.includes('Primary number')));
    search('Health 102');check(`${role}: extension search matches display name`,options().length===1 && options()[0].dataset.workflowKey==='extension:102@+13025550002');
    pick('extension:102@+13025550002');search('');
    check(`${role}: chooser loads exact canonical extension identity`,key()==='extension:102@+13025550002' && d.getElementById('routing-editor-title').textContent.includes('102'));
    const ring=d.getElementById('desk-ring-enabled');ring.checked=false;event(env,ring);
    accept=false;pick('extension:101@+13025550001');
    check(`${role}: cancelled desk switch preserves live input`,key()==='extension:102@+13025550002' && !d.getElementById('desk-ring-enabled').checked);
    accept=true;d.getElementById('number-flow-tab').click();
    check(`${role}: returning to Number Flow remembers last number`,key()==='number:+13025550002');
    d.getElementById('extension-flow-tab').click();
    check(`${role}: returning to Extension Call Handling remembers last desk`,key()==='extension:102@+13025550002');
    if(isAdmin){
      chooseScope(w,d,{customer:5});
      check(`${role}: customer change never shows another customer's selection`,options().every(option=>!option.dataset.workflowKey.includes('+1302555000')) && key()!=='extension:102@+13025550002');
      chooseScope(w,d,{customer:2});
      check(`${role}: returning customer restores their last extension`,key()==='extension:102@+13025550002');
    }
    w.eval("state.extensions=[];renderFlow()");
    check(`${role}: no extensions shows meaningful empty state and disables save`,!d.getElementById('routing-editor-empty').hidden && d.getElementById('save-route').disabled && options().length===0 && key()==='');
    d.getElementById('number-flow-tab').click();
    for(let i=0;i<75;i++)w.eval(`state.phone_numbers.push({number:'+1999555${String(i).padStart(4,'0')}',active:1,owner_user_id:2,description:'Branch ${i}'})`);
    w.eval('renderRoutingChoices()');search('Branch 74');
    check(`${role}: large inventories remain searchable`,options().length===1 && options()[0].textContent.includes('Branch 74'));
    w.eval('state.phone_numbers=[];renderFlow()');
    check(`${role}: no numbers shows empty state without stale target`,key()==='' && d.getElementById('page-routing').dataset.hasTarget==='false' && d.getElementById('save-route').disabled);
    check(`${role}: selection scenarios have no runtime errors`,env.errors.length===0);
  } finally {await settle(25);env.dom.window.close();}
}

(async()=>{await run(true);await run(false);await runSelection(true);await runSelection(false);console.log(`\n${checks} routing checks passed`);})().catch(error=>{console.error(error);process.exit(1);});
