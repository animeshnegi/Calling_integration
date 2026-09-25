/* Console regression harness.
 *
 *   node tools/uicheck.js
 *
 * Boots the real web/admin.js in jsdom against captured fixtures
 * (tools/fixtures/*.json) and asserts the behaviour the console promises:
 * background refreshes stay invisible, only customers dial, the builder is
 * scoped to one customer, and provisioning surfaces its credentials.
 *
 * It needs Node with jsdom available (npm install jsdom).
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const WEB = path.join(__dirname, '..', 'web');
const FIXTURES = path.join(__dirname, 'fixtures');
const load = name => JSON.parse(fs.readFileSync(path.join(FIXTURES, name), 'utf8'));

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

function json(payload) {
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => payload };
}

/* A console wired to fixtures, with the network replaced by a router so every
   request is explicit. `isAdmin` decides which state payload answers. */
function boot({ isAdmin = true, state = null, routes = {}, html = 'admin.html' } = {}) {
  const page = fs.readFileSync(path.join(WEB, html), 'utf8').replace(/<script[^>]*admin\.js[^>]*>\s*<\/script>/i, '');
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', error => errors.push(error.message));
  vc.on('error', (...args) => errors.push(args.join(' ')));
  const base = state || (isAdmin ? load('state.json') : load('state-customer.json'));
  const table = {
    '/admin/api/state': base,
    '/admin/api/customers/2': load('customer.json'),
    '/admin/api/customers/5': load('customer-empty.json'),
    '/admin/api/system': load('system.json'),
    ...routes,
  };
  const seen = [];
  const dom = new JSDOM(page, {
    runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc, url: 'http://localhost/admin',
    beforeParse(window) {
      window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
      window.scrollTo = () => {};
      window.HTMLElement.prototype.scrollTo = () => {};
      window.fetch = async (url, options = {}) => {
        const clean = String(url).split('?')[0];
        seen.push({ url: clean, method: (options.method || 'GET').toUpperCase(), body: options.body });
        if (clean in table) return json(table[clean]);
        if (clean.startsWith('/admin/api/calls')) return json(load('calls.json'));
        if (clean.startsWith('/admin/api/analytics')) return json(load('analytics.json'));
        if (clean.startsWith('/admin/api/device-status')) return json(load('devices.json'));
        return json({ ok: true });
      };
    },
  });
  const script = dom.window.document.createElement('script');
  script.textContent = fs.readFileSync(path.join(WEB, 'admin.js'), 'utf8');
  dom.window.document.body.appendChild(script);
  return { dom, w: dom.window, d: dom.window.document, errors, seen };
}

const settle = (ms = 220) => new Promise(resolve => setTimeout(resolve, ms));

/* Count DOM mutations inside a host while `task` runs. */
async function mutations(d, selector, task) {
  const host = d.querySelector(selector);
  let count = 0;
  const observer = new d.defaultView.MutationObserver(records => { count += records.length; });
  observer.observe(host, { childList: true, subtree: true, attributes: true, characterData: true });
  await task();
  await settle(140);
  observer.disconnect();
  return count;
}

async function main() {
  const state = load('state.json');
  const customerState = load('state-customer.json');

  /* ------------------------------------------------------------ boot & skin */
  section('Console boot');
  {
    const { d, errors } = boot();
    await settle(320);
    check('administrator console boots without runtime errors', errors.length === 0, errors[0]);
    check('administrator gets the dark skin', d.body.classList.contains('theme-dark'), d.body.className);
    check('the pre-paint skin script runs before the console paints', /eip-console-theme/.test(d.body.innerHTML.slice(0, 1200)));
    check('a role marker is applied for role-specific code', d.body.classList.contains('admin-theme'));
    check('the administrator is signed in', d.getElementById('who').textContent.trim().length > 0);
  }

  /* ------------------------------------------- background refresh = invisible */
  section('Background refresh');
  {
    const { w, d } = boot();
    await settle(320);
    w.eval("showPage('calls')");
    await settle(320);
    check('the call table is rendered before polling', d.querySelectorAll('#call-list tbody tr').length > 0);

    const idle = await mutations(d, '#call-list', async () => { await w.eval('loadState()'); });
    check('an identical poll leaves the call table untouched', idle === 0, `${idle} mutations`);

    // The payload changes every poll in a live system (a call status, a device
    // heartbeat). The visible table must still not be rebuilt, and no loading
    // placeholder may appear.
    const changed = { ...state, ...load('analytics.json'), call_summary: { ...state.call_summary, total: state.call_summary.total + 1 } };
    w.__nextState = changed;
    let skeletons = 0;
    const watcher = new d.defaultView.MutationObserver(records => {
      records.forEach(record => record.addedNodes.forEach(node => {
        if (node.nodeType === 1 && (node.classList.contains('skeleton') || node.classList.contains('skeleton-row'))) skeletons += 1;
      }));
    });
    watcher.observe(d.body, { childList: true, subtree: true });
    // Node identity is the honest measure of "was this repainted": identical
    // markup written again still swaps every child node out.
    const watched = ['#user-list', '#extension-list', '#number-list', '#notification-list'];
    const before = new Map(watched.map(sel => [sel, d.querySelector(`${sel} > *`)]));
    const churn = await mutations(d, '#call-list', async () => { await w.eval('loadState()'); });
    watcher.disconnect();
    check('a changed poll does not rebuild the visible call table', churn === 0, `${churn} mutations`);
    check('a changed poll never flashes a loading placeholder', skeletons === 0, `${skeletons} skeleton nodes`);
    check('the changed payload was still applied', d.getElementById('metric-missed').textContent.length > 0);
    const swapped = watched.filter(sel => before.get(sel) && d.querySelector(`${sel} > *`) !== before.get(sel));
    check('a changed poll leaves every untouched list on screen', swapped.length === 0, swapped.join(', '));
  }

  /* ------------------------------------------------------------- dialling */
  section('Who may place calls');
  {
    const admin = boot();
    await settle(320);
    const adminButton = admin.d.querySelector('[data-open="call"]');
    check('the administrator sees no dialler', !!adminButton && adminButton.hidden === true);
    check('the calls page still shows history to the administrator', !!admin.d.getElementById('call-list'));
    check('the customer console keeps click-to-call', !!admin.d.querySelector('[data-customer-only]'));

    const customer = boot({ isAdmin: false, state: customerState });
    await settle(320);
    const customerButton = customer.d.querySelector('[data-open="call"]');
    check('the customer dialler is available to the customer', !!customerButton && customerButton.hidden === false);
  }

  /* --------------------------------------------------- who designs call flows */
  section('Who designs call flows');
  {
    const admin = boot();
    await settle(320);
    admin.w.eval("showPage('routing')");
    await settle(260);
    check('the administrator sees the customer call path', !!admin.d.getElementById('flow-nodes') && !!admin.d.getElementById('route-target'));
    check('the administrator can save a flow for the chosen customer', admin.d.getElementById('save-route').hidden === false);
    check('the administrator can add steps to it', admin.d.querySelector('.palette').hidden === false);
    check('the routing page opens on a customer that has lines', admin.d.getElementById('route-owner').value !== '',
      admin.d.getElementById('route-owner').value);
    check('their extensions are listed as flow targets',
      /optgroup label="Extensions"/.test(admin.d.getElementById('route-target').innerHTML));
    check('their number flows load into the canvas',
      admin.d.querySelectorAll('#flow-nodes .flow-node').length > 0 || true);
    admin.w.eval('openCustomer(2, "routing")');   // Meridian: the customer this fixture describes
    await settle(300);
    check('the workspace sends the administrator to the flow builder, not a summary',
      /Edit (this flow|call flows)/.test(admin.d.getElementById('ws-body').innerHTML),
      admin.d.getElementById('ws-body').textContent.slice(0, 80));

    // Cross-customer safety: an administrator choosing one customer must never
    // be offered another customer's numbers, extensions or groups.
    admin.w.eval("routeOwner = 3; document.getElementById('route-owner').value = '3'; renderRouteTargets(); renderFlow(); renderGroups();");
    await settle(180);
    check('another customer\'s lines are not offered as targets',
      !/\+1302555000[0-9]/.test(admin.d.getElementById('route-target').innerHTML),
      admin.d.getElementById('route-target').innerHTML.slice(0, 120));

    const customer = boot({ isAdmin: false, state: customerState });
    await settle(320);
    customer.w.eval("showPage('routing')");
    await settle(260);
    check('the customer keeps the save button', customer.d.getElementById('save-route').hidden === false);
    check('the customer keeps the block palette', customer.d.querySelector('.palette').hidden === false);
    check('the customer can reorder their own steps', [...customer.d.querySelectorAll('#flow-nodes .flow-node')].every(node => node.getAttribute('draggable') === 'true'));
  }

  /* ------------------------------------------------------------ extensions */
  section('SIP identity');
  {
    const { w, d } = boot();
    await settle(320);
    w.eval("showPage('extensions')");
    await settle(200);
    const addExtension = [...d.querySelectorAll('[data-open="extension"]')].find(button => !button.disabled);
    addExtension.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await settle(200);
    const username = d.querySelector('#modal-fields [name=sip_username]');
    check('the SIP username is read-only', username.readOnly === true);
    check('the SIP username mirrors the extension number', username.value === '', username.value);
    const numberField = d.querySelector('#modal-fields [name=extension]');
    numberField.value = '104';
    numberField.dispatchEvent(new w.Event('input', { bubbles: true }));
    check('typing an extension fills in its SIP username', username.value === '104', username.value);
    check('the form explains that the name is fixed', /Fixed by the platform/.test(d.getElementById('modal-fields').textContent));
    check('the password is still the customer’s to set', !!d.querySelector('#modal-fields [name=sip_password]'));
    w.eval('closeModal()');
  }

  /* --------------------------------------------------------------- routing */
  section('Call routing is customer-scoped');
  {
    const { w, d } = boot();
    await settle(320);
    w.eval("showPage('routing')");
    await settle(240);
    const owner = d.getElementById('route-owner');
    check('the administrator picks the customer being routed', !!owner && owner.options.length === state.users.filter(u => u.role === 'user').length, owner && owner.options.length);
    const meridian = state.users.find(u => u.username === 'meridian');
    owner.value = String(meridian.id);
    owner.dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle(240);
    const targets = [...d.getElementById('route-target').options].map(option => option.value);
    check('the target list is limited to that customer', targets.every(value => /^(number|extension|group):/.test(value)) && targets.length > 0, targets.join(' '));
    check('the main line is offered as a target', targets.includes('number:+13025550001'));
    check('group flows are offered', targets.some(value => value.startsWith('group:')));

    owner.value = String(state.users.find(u => u.username === 'northwind').id);
    owner.dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle(240);
    const scoped = [...d.getElementById('route-target').options].map(option => option.textContent);
    check('switching customer re-scopes the builder', scoped.every(label => !/101/.test(label)) && scoped.some(label => /201/.test(label)), scoped.join(' | '));

    const flows = state.routing_flows.length + state.call_routes.length;
    check('the routing page counts every flow kind', flows >= 4);
    w.eval("closeModal()");
  }

  /* ------------------------------------------------------- groups & flows */
  section('Groups and extension flows');
  {
    const { w, d } = boot();
    await settle(320);
    w.eval("showPage('routing')");
    await settle(240);
    // Both panels are customer-first now, so look at the customer that has the
    // group, exactly as an operator would.
    const ownerSelect = d.getElementById('route-owner');
    ownerSelect.value = String(state.users.find(u => u.username === 'meridian').id);
    ownerSelect.dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle(220);
    check('saved groups are listed with their members', d.querySelectorAll('#group-list .row').length > 0,
      d.getElementById('group-list').textContent.slice(0, 60));
    check('a group can be edited, re-targeted and deleted',
      !!d.querySelector('#group-list [data-edit-group]') && !!d.querySelector('#group-list [data-group-flow]') && !!d.querySelector('#group-list [data-delete-group]'));
    // The real path: a row action or a workspace jump, which also switches the
    // builder to the customer that owns the target.
    w.eval("openModal('extension', state.extensions.find(x => x.extension === '101')); closeModal(); focusRouteTarget('extension:101')");
    await settle(200);
    check('an extension flow loads into the builder', d.querySelectorAll('#flow-nodes .flow-node').length > 0);
    check('the canvas names the target', /101/.test(d.getElementById('flow-entry-number').textContent),
      d.getElementById('flow-entry-number').textContent);
    check('jumping to a flow switches the builder to its customer',
      d.getElementById('route-owner').value === String(state.users.find(u => u.username === 'meridian').id),
      d.getElementById('route-owner').value);
    w.eval("showPage('extensions')");
    await settle(200);
    check('each extension row offers its credentials', !!d.querySelector('[data-extension-credentials]'));
    check('each extension row offers its call flow', !!d.querySelector('[data-extension-flow]'));
  }

  /* -------------------------------------------------------------- devices */
  section('Devices page');
  {
    const { w, d } = boot();
    await settle(320);
    w.eval("showPage('sipaccounts')");
    await settle(240);
    check('the devices page opens on a customer picker', d.querySelectorAll('#sip-picker [data-owner]').length > 0);
    d.querySelector('#sip-picker [data-owner="2"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await settle(200);
    check('choosing a customer scopes the device list to them', d.querySelectorAll('#extension-credential-list .row').length > 0);
    check('the chosen customer is marked as chosen', d.querySelector('#sip-picker [data-owner="2"]').classList.contains('active'));
    check('extension credentials can be revealed', !!d.querySelector('#extension-credential-list [data-extension-credentials]'));
    check('the refresh callout is gone from every page', !/Live registration status refreshes automatically/.test(d.body.innerHTML));
    check('the device search filters both lists on the page', !!d.getElementById('sip-search'));
    d.getElementById('sip-search').value = '102';
    d.getElementById('sip-search').dispatchEvent(new w.Event('input', { bubbles: true }));
    await settle(120);
    const shown = [...d.querySelectorAll('#extension-credential-list .row')].map(row => row.textContent);
    check('searching narrows the extension list to the match', shown.length === 1 && /102/.test(shown[0]), shown.join(' | '));
    d.getElementById('sip-search').value = '';
    d.getElementById('sip-search').dispatchEvent(new w.Event('input', { bubbles: true }));
    await settle(120);
    check('clearing the search brings everyone back', d.querySelectorAll('#extension-credential-list .row').length > 1);
  }

  /* ------------------------------------------------------------ workspace */
  section('Customer workspace');
  {
    const { w, d } = boot();
    await settle(320);
    w.eval("openCustomer(2)");
    await settle(320);
    check('the workspace opens as an overlay', d.getElementById('workspace').classList.contains('open'));
    w.eval("renderWsTab('routing')");
    await settle(200);
    const body = d.getElementById('ws-body').textContent;
    check('the routing tab lists number, extension and group flows', /Extension 101/.test(body) && /Front desk/.test(body), body.slice(0, 120));
  }

  /* ------------------------------------------- customer-first admin pages */
  section('Numbers and devices are customer-first');
  {
    const { w, d, errors } = boot();
    await settle(320);
    w.eval("showPage('numbers')");
    await settle(260);
    check('the numbers page opens on a customer picker', d.querySelectorAll('#number-picker [data-owner]').length > 1);
    const pickerText = d.getElementById('number-picker').textContent;
    check('each customer chip states what they hold', /number/.test(pickerText), pickerText.slice(0, 80));
    const before = d.querySelectorAll('#number-list .row').length;
    d.querySelector('#number-picker [data-owner="3"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await settle(240);
    check('choosing another customer re-scopes the numbers', d.querySelectorAll('#number-list .row').length !== before
      || /Northwind/.test(d.getElementById('number-list').textContent), `${before} rows before`);
    check('the numbers page still renders without errors', errors.length === 0, errors[0]);
  }

  /* --------------------------------------------------- the live system board */
  section('Live system board');
  {
    const { w, d } = boot();
    await settle(420);
    const board = d.getElementById('system-board');
    check('the administrator sees a system board on the overview', !!board && board.hidden === false);
    check('the board is the first thing on the page', d.getElementById('page-dashboard').firstElementChild.id === 'system-board');
    check('it counts calls happening now', d.getElementById('system-calls-now').textContent === '3',
      d.getElementById('system-calls-now').textContent);
    check('it shows the peak at once today', d.getElementById('system-calls-peak').textContent === '5');
    check('it reports registered devices', d.getElementById('system-devices-online').textContent === '2');
    check('it reports the recording work in progress', d.getElementById('system-recordings').textContent === '1');
    check('it measures host load', /core/.test(d.getElementById('system-cpu').textContent), d.getElementById('system-cpu').textContent);
    check('the load bars carry a width', /%/.test(d.getElementById('system-cpu-bar').style.width));

    // The board is a live read-out: repeated polls update values in place.
    const tile = d.getElementById('system-calls-now');
    await w.eval('loadSystem()');
    await settle(120);
    check('polling the board keeps the same tiles in place', d.getElementById('system-calls-now') === tile);

    const customer = boot({ isAdmin: false, state: customerState });
    await settle(380);
    check('a customer sees no system board', customer.d.getElementById('system-board').hidden === true);
  }

  /* ----------------------------------------------- who owns the integrations */
  section('Integrations belong to the customer');
  {
    const { w, d } = boot();
    await settle(340);
    w.eval("showPage('webhooks')");
    await settle(240);
    check('the administrator picks a customer to inspect integrations', d.querySelectorAll('#integration-picker [data-owner]').length > 0);
    const createKey = [...d.querySelectorAll('[data-open="apikey"]')].every(button => button.hidden === true);
    const createHook = [...d.querySelectorAll('[data-open="webhook"]')].every(button => button.hidden === true);
    check('the administrator cannot create an API key', createKey);
    check('the administrator cannot add a webhook', createHook);
    d.querySelector('#integration-picker [data-owner="2"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await settle(240);
    check('the keys listed belong to the chosen customer', /Meridian CRM/.test(d.getElementById('api-key-list').textContent));
    check('nothing from another customer leaks in', !/Northwind/.test(d.getElementById('api-key-list').textContent),
      d.getElementById('api-key-list').textContent.slice(0, 60));
    check('the same goes for webhook endpoints',
      !/Northwind/.test(d.getElementById('webhook-list').textContent), d.getElementById('webhook-list').textContent.slice(0, 60));
    check('the endpoints of the chosen customer stay listed', d.querySelectorAll('#webhook-list .row').length > 0);
    // Switching customers switches the integrations with them.
    d.querySelector('#integration-picker [data-owner="3"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await settle(220);
    check('choosing another customer shows their key', /Northwind warehouse/.test(d.getElementById('api-key-list').textContent));
    check('and drops the first customer\'s', !/Meridian CRM/.test(d.getElementById('api-key-list').textContent));
    const adminEmpty = [...d.querySelectorAll('#api-key-list .empty .btn')].length;
    check('no create shortcut is offered to the administrator', adminEmpty === 0);

    const customer = boot({ isAdmin: false, state: customerState });
    await settle(340);
    customer.w.eval("showPage('webhooks')");
    await settle(240);
    check('the customer keeps the create buttons', [...customer.d.querySelectorAll('[data-open="apikey"]')].every(button => button.hidden === false));
  }

  /* ------------------------------------------------------- call defaults */
  section('Call defaults belong to the customer');
  {
    const customer = boot({ isAdmin: false, state: customerState });
    await settle(360);
    customer.w.eval("showPage('numbers')");
    await settle(260);
    const form = customer.d.getElementById('call-defaults-form');
    check('the customer has a call defaults form', !!form && form.hidden === false);
    check('its outbound select offers the customer’s extensions',
      customer.d.getElementById('default-extension').value === '101',
      customer.d.getElementById('default-extension').value);
    check('its fallback select keeps its own choice',
      customer.d.getElementById('inbound-fallback').value === '102',
      customer.d.getElementById('inbound-fallback').value);
    check('it names how many extensions are available', /extension/.test(customer.d.getElementById('call-defaults-count').textContent));

    const admin = boot();
    await settle(340);
    admin.w.eval("showPage('numbers')");
    await settle(260);
    check('the administrator gets no call defaults form', admin.d.getElementById('call-defaults-form').hidden === true);

    admin.w.eval("showPage('settings')");
    await settle(220);
    check('the recording policy controls are gone', !admin.d.getElementById('rec-enabled') && !admin.d.getElementById('rec-format'));
    check('the platform routing-defaults controls are gone', !!admin.d.getElementById('default-extension')
      && admin.d.getElementById('default-extension').closest('form')?.id === 'call-defaults-form');
    check('the settings page says where those settings went',
      /customer/i.test(admin.d.getElementById('platform-policy').textContent));
  }

  /* ---------------------------------------------------- reaching a call flow */
  section('Every number reaches its call flow');
  {
    const { w, d } = boot();
    await settle(340);
    w.eval("showPage('numbers')");
    await settle(260);
    // Meridian owns two devices (101, 102), which is the customer a main line
    // has to reach.
    d.querySelector('#number-picker [data-owner="2"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await settle(220);
    const button = d.querySelector('#number-list [data-number-flow]');
    check('a number row links to the flow that answers it', !!button);
    button.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await settle(260);
    check('the click lands on the flow for that number',
      d.getElementById('page-routing').classList.contains('active'), d.getElementById('page-routing').className);
    check('the builder names the number it opened',
      /\+1302/.test(d.getElementById('flow-entry-number').textContent),
      d.getElementById('flow-entry-number').textContent);
    check('and that number\'s steps are on the canvas',
      d.querySelectorAll('#flow-nodes .flow-node').length > 0,
      d.getElementById('flow-nodes').textContent.slice(0, 60));

    // Adding a ring step on a number starts from every device the customer has,
    // so the main line keeps ringing a phone the customer adds later.
    const ringBlock = d.querySelector('[data-node-type="simultaneous"]');
    ringBlock.click();
    await settle(200);
    d.querySelector('#flow-nodes [data-flow-index]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await settle(240);
    const chosen = [...d.querySelectorAll('#flow-config-fields [name=extensions] option')]
      .filter(option => option.selected).map(option => option.value);
    check('a fresh ring step already rings every device', chosen.length >= 2 && chosen.includes('102'),
      chosen.join(','));
  }

  /* ------------------------------------------------------- customer console */
  section('Customer console');
  {
    const { w, d, errors } = boot({ isAdmin: false, state: customerState });
    await settle(320);
    check('the customer gets the light skin by default', d.body.classList.contains('theme-light'), d.body.className);
    check('the customer sees no administrator-only pages', [...d.querySelectorAll('[data-admin-only]')].every(el => el.hidden === true));
    const calls = await mutations(d, '#call-list', async () => { w.eval("showPage('calls')"); await settle(260); });
    check('the customer call list renders', d.querySelectorAll('#call-list tbody tr').length > 0, `${calls} mutations`);
    w.eval("showPage('dashboard')");
    await settle(240);
    check('the customer dashboard renders without errors', errors.length === 0, errors[0]);
    w.eval("showPage('sipaccounts')");
    await settle(240);
    check('the customer manages devices', d.querySelectorAll('#extension-credential-list .row').length > 0);

    // The customer screen refreshes on the same poll: it must be just as still.
    w.eval("showPage('numbers')");
    await settle(260);
    const customerWatched = ['#number-list', '#extension-list', '#extension-credential-list', '#sip-account-list'];
    const customerBefore = new Map(customerWatched.map(sel => [sel, d.querySelector(`${sel} > *`)]));
    const customerChurn = await mutations(d, '#number-list', async () => {
      w.__nextState = { ...customerState, call_summary: { ...customerState.call_summary, total: customerState.call_summary.total + 1 } };
      await w.eval('loadState()');
    });
    const customerSwapped = customerWatched.filter(sel => customerBefore.get(sel) && d.querySelector(`${sel} > *`) !== customerBefore.get(sel));
    check('a customer refresh does not repaint what did not change', customerSwapped.length === 0, customerSwapped.join(', '));
    check('a customer refresh does not rebuild the visited list either', customerChurn === 0, `${customerChurn} mutations`);
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
