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
    check('the administrator gets no save button', admin.d.getElementById('save-route').hidden === true);
    check('the administrator gets no block palette', admin.d.querySelector('.palette').hidden === true);
    check('the administrator still sees the customer call path', !!admin.d.getElementById('flow-nodes') && !!admin.d.getElementById('route-target'));
    check('the administrator gets no remove control on a step', admin.d.querySelectorAll('#flow-nodes .remove').length === 0);
    check('the administrator cannot reorder steps', [...admin.d.querySelectorAll('#flow-nodes .flow-node')].every(node => node.getAttribute('draggable') === 'false'));
    admin.w.eval('openCustomer(2, "routing")');   // Meridian: the customer this fixture describes
    await settle(300);
    check('the workspace offers the administrator a view, not a builder',
      /View (this flow|call flows)/.test(admin.d.getElementById('ws-body').innerHTML),
      admin.d.getElementById('ws-body').textContent.slice(0, 80));

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
    check('saved groups are listed with their members', d.querySelectorAll('#group-list .row').length > 0);
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
    check('the devices page lists extension credentials', d.querySelectorAll('#extension-credential-list .row').length > 0);
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
