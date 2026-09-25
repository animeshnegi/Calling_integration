/* What the console actually shows, from the live server.
 *
 *   node tools/livepages.js [--customer meridian] [--password customer-password-01]
 *
 * Boots the real web/admin.js in jsdom but sends every API call to the running
 * preview (tools/devpreview.py) with a real session and CSRF token, then prints
 * the visible text of each page. This is the "did it really change?" check: it
 * reads the same payloads a browser would, so stale fixtures cannot flatter it.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const WEB = path.join(__dirname, '..', 'web');
const BASE = process.env.PREVIEW || 'http://127.0.0.1:5000';
const args = Object.fromEntries(process.argv.slice(2).map(x => {
  const [k, v] = x.replace(/^--/, '').split('=');
  return [k, v === undefined ? true : v];
}));

let cookies = [];

async function login(username, password) {
  const page = await fetch(`${BASE}/login`, { redirect: 'manual' });
  const jar = page.headers.getSetCookie ? page.headers.getSetCookie() : [];
  const res = await fetch(`${BASE}/login`, {
    method: 'POST', redirect: 'manual',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', cookie: jar.map(c => c.split(';')[0]).join('; ') },
    body: new URLSearchParams({ username, password }).toString(),
  });
  const set = res.headers.getSetCookie ? res.headers.getSetCookie() : [];
  cookies = [...jar, ...set].map(c => c.split(';')[0]);
  if (![200, 302].includes(res.status)) throw new Error(`login failed: ${res.status} ${await res.text()}`);
  return cookies;
}

function api(pathname, method = 'GET', body = null, csrf = '') {
  const headers = { cookie: cookies.join('; ') };
  if (body) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = csrf;
  return fetch(`${BASE}${pathname}`, { method, headers, body: body ? JSON.stringify(body) : undefined })
    .then(async res => ({ status: res.status, payload: await res.json().catch(() => null) }));
}

function boot(html = 'admin.html') {
  const page = fs.readFileSync(path.join(WEB, html), 'utf8').replace(/<script[^>]*admin\.js[^>]*>\s*<\/script>/i, '');
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', error => errors.push(error.message));
  vc.on('error', (...rest) => errors.push(rest.join(' ')));
  const dom = new JSDOM(page, {
    runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc, url: `${BASE}/admin`,
    beforeParse(window) {
      window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
      window.scrollTo = () => {};
      window.HTMLElement.prototype.scrollTo = () => {};
      window.confirm = () => true;
      window.fetch = async (url, options = {}) => {
        const method = (options.method || 'GET').toUpperCase();
        let payload = null;
        if (options.body) { try { payload = JSON.parse(options.body); } catch { payload = options.body; } }
        const state = window.__csrf ? { csrf_token: window.__csrf } : null;
        if (payload && state && !payload.csrf_token) payload = { ...payload, csrf_token: state.csrf_token };
        const { status, payload: body } = await api(String(url), method, payload,
          method === 'GET' ? '' : (payload && payload.csrf_token) || window.__csrf || '');
        return { ok: status < 400, status, headers: { get: () => 'application/json' }, json: async () => body, text: async () => JSON.stringify(body) };
      };
    },
  });
  const script = dom.window.document.createElement('script');
  script.textContent = fs.readFileSync(path.join(WEB, 'admin.js'), 'utf8');
  dom.window.document.body.appendChild(script);
  return { w: dom.window, d: dom.window.document, errors };
}

const settle = (ms = 300) => new Promise(resolve => setTimeout(resolve, ms));
const text = node => (node ? node.textContent.replace(/\s+/g, ' ').trim() : '(missing)');
const show = (title, value) => console.log(`\n${'-'.repeat(78)}\n${title}\n${'-'.repeat(78)}\n${value}`);
const page = async (w, d, name, wait = 320) => { w.eval(`showPage('${name}')`); await settle(wait); };

async function main() {
  const who = args.customer || 'engineerip';
  const password = args.password || (args.customer ? 'customer-password-01' : 'preview-admin-password');
  await login(who, password);
  const state = await api('/admin/api/state');
  const csrf = state.payload.csrf_token;
  console.log(`signed in as ${who} — is_admin=${state.payload.is_admin}, customers=${state.payload.customers?.length ?? 0}`);

  const { w, d, errors } = boot();
  w.__csrf = csrf;
  await settle(700);

  // The customer workspace drawer: what stays still and what scrolls.
  await w.eval("openCustomer(2)");
  await settle(900);
  const drawer = d.getElementById('workspace');
  const scrollBox = d.getElementById('ws-scroll');
  const stillBox = d.querySelector('.ws-head .ws-head-top');
  show('ADMIN · the customer workspace drawer',
    `drawer open: ${drawer?.classList.contains('open')}\n`
    + `still (outside the scroller): ${stillBox ? stillBox.textContent.replace(/\s+/g, ' ').trim().slice(0, 90) : 'missing'}\n`
    + `scrolls: ${[...(scrollBox?.children || [])].map(child => child.id || child.className).join(' , ')}\n`
    + `pinned row: ${d.getElementById('ws-tabs')?.getAttribute('aria-label')} `
    + `(${d.querySelectorAll('#ws-tabs [data-ws-tab]').length} tabs) then ${d.getElementById('ws-body')?.id}\n`
    + `account summary: ${text(d.getElementById('ws-status'))}`);
  w.eval("closeWorkspace()");
  await settle(300);

  await page(w, d, 'dashboard');
  show('ADMIN · Overview — the system board (first thing on the page)',
    `${d.getElementById('page-dashboard').firstElementChild.id}\n${text(d.getElementById('system-board'))}`);

  await page(w, d, 'settings');
  show('ADMIN · Settings — the platform recording switch',
    `switch: ${d.getElementById('platform-recording')?.checked} (${text(d.getElementById('recording-platform-state'))})\n`
    + `${text(d.getElementById('recording-platform-help'))}`);
  show('ADMIN · Settings — the address every device and link is built from',
    `host: ${d.getElementById('service-host')?.value} port: ${d.getElementById('service-sip-port')?.value}`
    + ` (${text(d.getElementById('service-address-state'))})\n`
    + `${text(d.getElementById('service-address-preview'))}`);
  show('ADMIN · Settings — what is left of the old platform controls',
    text(d.getElementById('page-settings')).slice(0, 900));
  const editable = ['rec-enabled', 'rec-format', 'rec-retention', 'default-extension', 'inbound-fallback']
    .filter(id => { const el = d.getElementById(id); return el && !el.closest('#call-defaults-form'); });
  show('ADMIN · Settings — editable policy controls still present', editable.length ? editable.join(', ') : 'none');

  await page(w, d, 'numbers');
  show('ADMIN · Numbers — choose a customer, then their numbers',
    `${text(d.getElementById('number-picker')).slice(0, 200)}\nrows shown: ${d.querySelectorAll('#number-list .row').length}\n${text(d.getElementById('number-list')).slice(0, 300)}`);

  await page(w, d, 'sipaccounts');
  show('ADMIN · Devices & SIP — choose a customer, then their extensions and devices',
    `${text(d.getElementById('sip-picker')).slice(0, 240)}\ncredential rows: ${d.querySelectorAll('#extension-credential-list .row').length}\nsip rows: ${d.querySelectorAll('#sip-account-list .row').length}\n${text(d.getElementById('sip-account-list')).slice(0, 240)}`);

  // The credential sheet itself: open the first extension's credentials, then
  // measure how much of it is above the fold.
  await settle(400);
  const credButton = d.querySelector('#extension-credential-list [data-extension-credentials]');
  if (!credButton) {
    show('ADMIN · the credentials sheet for one extension', 'no extension listed for this customer');
  }
  if (credButton) {
    w.eval(`showExtensionCredentials('${credButton.dataset.extensionCredentials}')`);
    await settle(600);
    const body = d.getElementById('modal-fields');
    const sheet = body?.querySelector('.cred-sheet');
    const rows = [...(body?.querySelectorAll('.cred-table tr') || [])];
    show('ADMIN · the credentials sheet for one extension',
      `table rows: ${rows.length} (copyable ${rows.filter(r => r.querySelector('[data-copy-value]')).length})\n`
      + `everything in one sheet: ${!!sheet} · rotate panel hidden at rest: ${body?.querySelector('.cred-rotate')?.hidden}\n`
      + `${text(sheet).slice(0, 420)}`);
  }

  await page(w, d, 'webhooks');
  const docLink = d.querySelector('#page-webhooks .doc-link a');
  show('ADMIN · APIs & Webhooks — the documentation link and API base on top',
    `href: ${docLink?.getAttribute('href')} target: ${docLink?.getAttribute('target')}\n${text(d.querySelector('#page-webhooks .doc-link'))}`);
  show('ADMIN · APIs & Webhooks — whose keys, and what he may do',
    `picker: ${text(d.getElementById('integration-picker')).slice(0, 200)}\nkeys: ${text(d.getElementById('api-key-list')).slice(0, 220)}\nwebhooks: ${text(d.getElementById('webhook-list')).slice(0, 220)}\ncreate buttons: ${[...d.querySelectorAll('[data-open="apikey"],[data-open="webhook"]')].map(x => `${x.dataset.open}:hidden=${x.hidden}`).join(' ')}`);

  await page(w, d, 'routing');
  show('ADMIN · Call flows — which customer, and can he build',
    `owner selector: ${[...d.querySelectorAll('#route-owner option')].map(o => `${o.value}:${o.textContent}`).join(', ')} (value ${d.getElementById('route-owner').value})\nsave button hidden=${d.getElementById('save-route').hidden}, disabled=${d.getElementById('save-route').disabled}\nblocks offered: ${[...d.querySelectorAll('[data-node-type]')].length}\ntargets: ${text(d.getElementById('route-target')).slice(0, 200)}\ncanvas: ${text(d.getElementById('flow-nodes')).slice(0, 200)}\ngroups: ${text(d.getElementById('group-list')).slice(0, 200)}`);
  if (errors.length) console.log('\npage errors:', errors.slice(0, 3));

  // The customer's own view of the same console.
  await login(args.customer || 'meridian', 'customer-password-01');
  const custState = await api('/admin/api/state');
  const cust = boot();
  cust.w.__csrf = custState.payload.csrf_token;
  await settle(700);
  await page(cust.w, cust.d, 'webhooks');
  show('CUSTOMER · APIs & Webhooks — where the documentation is, and on what address',
    `${text(cust.d.querySelector('#page-webhooks .doc-link'))}`);

  await page(cust.w, cust.d, 'numbers');
  show('CUSTOMER · Numbers — their own call defaults',
    `${text(cust.d.getElementById('call-defaults-form'))}\nform hidden=${cust.d.getElementById('call-defaults-form').hidden}`);
  await page(cust.w, cust.d, 'dashboard');
  const bar = cust.d.querySelector('#customer-journey .journey-bar');
  const fill = cust.d.querySelector('#customer-journey .journey-bar i');
  const ticks = [...cust.d.querySelectorAll('#customer-journey .journey-step .tick')];
  show('CUSTOMER · Setup journey — the bar and its icons',
    `headline: ${text(cust.d.querySelector('#customer-journey .journey-head b'))}\n`
    + `role: ${bar?.getAttribute('role')} valuenow: ${bar?.getAttribute('aria-valuenow')} label: ${bar?.getAttribute('aria-label')}\n`
    + `fill width: ${fill?.style.getPropertyValue('--p')} (computed ${cust.w.getComputedStyle(fill).width})\n`
    + `ticks: ${ticks.map(t => t.textContent.trim()).join(' ')}\n`
    + `steps: ${text(cust.d.querySelector('#customer-journey .journey-steps'))}`);

  await page(cust.w, cust.d, 'sipaccounts');
  const customerCred = cust.d.querySelector('#extension-credential-list [data-extension-credentials]');
  if (customerCred) {
    cust.w.eval(`showExtensionCredentials('${customerCred.dataset.extensionCredentials}')`);
    await settle(600);
    show('CUSTOMER · the credential sheet a customer copies from',
      `rows: ${cust.d.querySelectorAll('#modal-fields .cred-table tr').length}`
      + ` · copy-all: ${cust.d.querySelector('#modal-fields [data-cred-copy-all]')?.textContent.trim()}\n`
      + `${cust.w.eval('credentialLines(credentialSheet.credentials)')}`);
    cust.w.eval('closeModal()');
  }

  await page(cust.w, cust.d, 'routing');
  show('CUSTOMER · Call flows',
    `targets: ${text(cust.d.getElementById('route-target')).slice(0, 200)}\ncanvas: ${text(cust.d.getElementById('flow-nodes')).slice(0, 200)}\nflows in state: ${custState.payload.routing_flows.map(r => `${r.target_type} ${r.target}`).join(', ')}\nnumber flows: ${custState.payload.call_routes.map(r => r.phone_number).join(', ')}`);
}

main().then(() => process.exit(0)).catch(error => { console.error('LIVEPAGES ERROR:', error.message); process.exit(1); });
