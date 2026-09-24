/* ============================================================================
   EIP Telephony Control — console application
   One shared bundle serves two deliberately different experiences:
     administrator (dark control room)  and  customer (light business view).
   Role differences come from body.theme-* plus [data-admin-only] /
   [data-customer-only]; the API contract is identical for both.
   ========================================================================= */
'use strict';

/* ----------------------------------------------------------- 1. Utilities */
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const truth = v => ['true','1','yes','on'].includes(String(v).toLowerCase());
const val = id => ($(id)?.value ?? '').trim();
const fmtNum = n => Number(n || 0).toLocaleString('en-US');
const groupBy = (rows, key) => rows.reduce((out, row) => { const k = key(row); (out[k] ??= []).push(row); return out; }, {});

function fmtDate(value, withTime = true) {
  if (!value) return '—';
  const d = new Date(String(value).replace(' ', 'T'));
  if (Number.isNaN(d.valueOf())) return String(value);
  return withTime
    ? d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}
function fmtDay(value) {
  if (!value) return '—';
  const d = new Date(String(value).replace(' ', 'T'));
  return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}
function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds || 0)));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
function money(cents) { return `$${(Number(cents || 0) / 100).toFixed(2)}`; }

let searchTimer;
function debounce(fn, delay = 300) { clearTimeout(searchTimer); searchTimer = setTimeout(fn, delay); }

/* Animated number transitions for statistics. */
function countTo(el, target) {
  if (!el) return;
  const to = Number(target || 0), from = Number(el.dataset.count || 0);
  el.dataset.count = String(to);
  if (from === to || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    el.textContent = fmtNum(to);
    return;
  }
  const start = performance.now(), span = 620;
  const step = now => {
    const p = Math.min(1, (now - start) / span);
    el.textContent = fmtNum(Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3))));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* --------------------------------------------------------------- 2. State */
let csrf = '';
let state = {
  extensions: [], phone_numbers: [], providers: [], webhooks: [], webhook_deliveries: [],
  users: [], customers: [], sip_accounts: [], requests: [], activity: [], notifications: [],
  call_routes: [], api_keys: [], invoices: [], email_deliveries: [], settings: {}, email_config: {},
  call_summary: {}, voicemail_summary: {}, is_admin: false,
};
let modalType = '', editing = null, callOffset = 0, recordingOffset = 0;
let currentPage = 'dashboard', workspace = null, wsTab = 'overview';
let flowNodes = [], flowConfigIndex = -1, pendingFulfilRequest = null;

const pageMeta = {
  dashboard: ['Overview', 'Your telephony environment at a glance'],
  users: ['Customers', 'Open a customer to manage everything they own in one place'],
  numbers: ['Numbers', 'Ownership, assignment and outbound caller ID'],
  sipaccounts: ['Devices & SIP', 'Credentials, connected devices and live registration'],
  extensions: ['Extensions', 'Internal destinations grouped by customer'],
  routing: ['Call Routing', 'Design the incoming call journey visually'],
  calls: ['Calls', 'Inbound and outbound activity across extensions'],
  recordings: ['Recordings', 'Search and play secure call audio'],
  voicemails: ['Voicemail', 'Messages organised by mailbox'],
  billing: ['Billing', 'Cycles, next payments and invoice history'],
  webhooks: ['APIs & Webhooks', 'Keys, event endpoints and delivery health'],
  notifications: ['Notifications', 'Assignments, devices, payments and alerts'],
  security: ['Security', 'Protect your account access'],
  requests: ['Requests', 'Approve customer provisioning requests'],
  activity: ['Activity', 'Important platform and customer actions'],
  providers: ['Carrier Providers', 'Platform carrier connections — administrator only'],
  email: ['Email Delivery', 'SendGrid voicemail attachments'],
  settings: ['Platform Settings', 'Global routing and recording policy'],
};
/* Pages a customer account must never open. Administrator-only resources are
   also stripped from the API payload for customers, so this is defence in depth. */
const CUSTOMER_BLOCKED = ['users', 'providers', 'email', 'settings', 'requests', 'activity'];

/* ------------------------------------------------------------ 3. Transport */
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';
  if (csrf && options.method && !['GET', 'HEAD'].includes(options.method)) headers['X-CSRF-Token'] = csrf;
  const response = await fetch(path, { ...options, headers });
  let data = {};
  try { data = await response.json(); } catch { /* empty body is valid for deletes */ }
  if (response.status === 401) { location = '/admin/login'; throw Error('Login required'); }
  if (data.csrf_token) csrf = data.csrf_token;
  if (!response.ok) throw Error(data.error || `Request failed (${response.status})`);
  return data;
}

/* ------------------------------------------------------------ 4. Feedback */
function notify(message, isError = false) {
  const host = $('toasts');
  if (!host) return;
  const node = document.createElement('div');
  node.className = `toast${isError ? ' error' : ''}`;
  const life = isError ? 5200 : 3200;
  node.style.setProperty('--toast-life', `${life}ms`);
  node.innerHTML = `<span class="glyph">${isError ? '!' : '✓'}</span><span>${esc(message)}</span>`;
  host.appendChild(node);
  setTimeout(() => {
    node.classList.add('leaving');
    node.addEventListener('animationend', () => node.remove(), { once: true });
  }, life);
}

function empty(title, text, glyph = '∅', tone = '', action = '') {
  return `<div class="empty${tone ? ` tone-${tone}` : ''}"><span class="empty-icon">${glyph}</span>
    <b>${esc(title)}</b><span>${esc(text)}</span>${action ? `<div class="empty-action">${action}</div>` : ''}</div>`;
}
/* Empty states offer the action that fills them, reusing existing flows. */
function emptyAction(label, attrs, primary = false) {
  return `<button class="btn ${primary ? 'primary' : 'ghost'} sm" ${attrs}>${label}</button>`;
}
/* Failures get a real state with a way out, not a blank panel. */
function errorState(title, text, action) {
  return `<div class="empty tone-bad"><span class="empty-icon">⚠</span><b>${esc(title)}</b><span>${esc(text)}</span>
    <div class="empty-action"><button class="btn ghost sm" data-retry="${esc(action)}">Try again</button></div></div>`;
}
const RETRY = {
  calls: () => loadCalls(), recordings: () => loadRecordings(),
  voicemails: () => loadVoicemails(), state: () => loadState(),
};
function skeletonPanel(rows = 3) {
  const widths = ['', 'w-60', 'w-80', 'w-40'];
  return `<div class="skeleton-list">${Array.from({ length: rows }, (_, i) => `<div class="skeleton line ${widths[i % widths.length]}"></div>`).join('')}</div>`;
}
/* Mark the panel that owns a list while its request is in flight. */
function setLoading(el, on) {
  el?.closest('.panel')?.classList.toggle('loading', !!on);
}
function skeletonRows(rows = 6) {
  return `<div class="skeleton-list">${Array.from({ length: rows }, (_, i) => `<div class="skeleton" style="height:52px;opacity:${(1 - i * 0.09).toFixed(2)}"></div>`).join('')}</div>`;
}

/* A badge pulses once when its count changes, then stays quiet across polls. */
function setBadge(el, value) {
  if (!el) return;
  const next = String(value);
  const changed = el.dataset.value !== undefined && el.dataset.value !== next;
  el.dataset.value = next;
  el.textContent = next;
  el.hidden = !Number(value);
  if (changed) {
    el.classList.remove('pulse');
    void el.offsetWidth;
    el.classList.add('pulse');
  }
}

/* Sequential entrances: children of these hosts animate in with a short stagger
   (capped, otherwise a 50-row table would still be arriving a second later). */
const STAGGER_HOSTS = [
  'user-list', 'number-list', 'provider-list', 'sip-account-list', 'api-key-list',
  'webhook-list', 'webhook-delivery-list', 'email-delivery-list', 'extension-list',
  'request-list', 'my-request-list', 'activity-list', 'notification-list',
  'invoice-list', 'subscription-list', 'platform-admin-list', 'recording-list',
  'voicemail-list',
];
function markStagger() {
  STAGGER_HOSTS.forEach(id => {
    const host = $(id);
    if (!host) return;
    [...host.children].forEach((child, index) => child.style.setProperty('--i', String(Math.min(index, 10))));
  });
  document.querySelectorAll('table.data tbody').forEach(body => {
    [...body.rows].forEach((row, index) => row.style.setProperty('--i', String(Math.min(index, 14))));
  });
}
/* Status pill with a leading dot; keeps wording human across both themes. */
const seenStatus = new Map();
/* Passing a stable key lets a status pill pulse the first time it changes after
   a refresh, so approvals, cancellations and finalisations are noticeable. */
function statusPill(value, key) {
  const v = String(value || 'unknown');
  const previous = key ? seenStatus.get(key) : undefined;
  const changed = previous !== undefined && previous !== v;
  if (key) seenStatus.set(key, v);
  return `<span class="status ${esc(v)}${changed ? ' changed' : ''}">${esc(v.replaceAll('_', ' '))}</span>`;
}
function tag(text, tone = '', glyph = '') {
  return `<span class="tag ${tone}">${glyph ? `${glyph} ` : ''}${esc(text)}</span>`;
}
function registration(account) {
  const online = account.registration_status === 'online';
  return `<span class="device ${online ? 'online' : 'offline'}"><i></i>${online ? 'Registered' : 'Offline'}</span>`;
}
function kv(label, value, cls = '') {
  return `<div class="kv-item ${cls}"><small>${esc(label)}</small><strong>${esc(value || '—')}</strong></div>`;
}

/* --------------------------------------------------------- 5. Navigation */
function showPage(name) {
  if (!pageMeta[name]) name = 'dashboard';
  if (!state.is_admin && CUSTOMER_BLOCKED.includes(name)) name = 'dashboard';
  if (name === 'users' && !state.is_admin) name = 'dashboard';
  currentPage = name;
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === `page-${name}`));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.page === name));
  $('page-title').textContent = pageMeta[name][0];
  $('page-subtitle').textContent = pageMeta[name][1];
  history.replaceState(null, '', `#${name}`);
  $('sidebar').classList.remove('open');
  if (!$('workspace').classList.contains('open')) $('scrim').classList.remove('open');
  if (name === 'calls') loadCalls();
  if (name === 'recordings') loadRecordings();
  if (name === 'voicemails') loadVoicemails();
}

/* ------------------------------------------------- 6. Health & analytics */
async function checkHealth() {
  try {
    const response = await fetch('/health');
    const data = await response.json();
    $('health').classList.toggle('bad', !response.ok);
    $('health').querySelector('span').textContent = response.ok ? 'System operational' : (data.error || 'System unavailable');
  } catch {
    $('health').classList.add('bad');
    $('health').querySelector('span').textContent = 'System unavailable';
  }
}

async function loadAnalytics() {
  try {
    const data = await api('/admin/api/analytics');
    $('metric-answer-rate').textContent = `${data.answer_rate}%`;
    $('metric-missed').textContent = fmtNum(data.missed);
    $('metric-duration').textContent = fmtDuration(data.average_duration_seconds);
    $('metric-direction').textContent = `${fmtNum(data.inbound)} / ${fmtNum(data.outbound)}`;
    const max = Math.max(1, ...data.daily.map(d => d.total));
    $('call-trend').innerHTML = data.daily.map((day, i) => `
      <div class="chart-day" title="${esc(day.date)}: ${day.answered} answered of ${day.total}">
        <i style="height:${Math.max(3, (day.total / max) * 100)}%"></i>
        <b style="height:${Math.max(3, (day.answered / max) * 100)}%"></b>
        ${i % 2 === 0 ? `<small>${esc(String(day.date).slice(5))}</small>` : ''}
      </div>`).join('');
  } catch {
    $('call-trend').innerHTML = empty('Analytics unavailable', 'Call metrics will retry automatically.', '◷');
  }
}

async function loadRecent() {
  const target = state.is_admin ? $('recent-calls') : $('recent-calls-customer');
  if (!target) return;
  try {
    const data = await api('/admin/api/calls?limit=6');
    target.innerHTML = callTable(data.calls);
  } catch (error) { notify(error.message, true); }
}

/* ------------------------------------------------------- 7. Boot & refresh */
async function loadState() {
  const keepWorkspace = workspace?.customer?.id;
  try {
    state = await api('/admin/api/state');
    csrf = state.csrf_token;
    document.body.classList.toggle('admin-theme', !!state.is_admin);
    document.body.classList.toggle('customer-theme', !state.is_admin);
    document.body.classList.remove('theme-loading');
    $('who').textContent = state.username;
    $('role').textContent = state.is_admin ? 'Platform administrator' : 'Customer account';
    $('avatar').textContent = (state.username || 'A')[0].toUpperCase();
    document.querySelectorAll('[data-admin-only]').forEach(el => (el.hidden = !state.is_admin));
    document.querySelectorAll('[data-customer-only]').forEach(el => (el.hidden = !!state.is_admin));
    renderAll();
    checkHealth();
    loadAnalytics();
    loadRecent();
    if (keepWorkspace && $('workspace').classList.contains('open')) await openCustomer(keepWorkspace, wsTab, true);
  } catch (error) { notify(error.message, true); }
}

async function refreshDeviceStatus() {
  try {
    const data = await api('/admin/api/device-status');
    const live = new Map(data.devices.map(d => [d.id, d.registration_status]));
    // Only touch the DOM when a state actually changed: re-rendering on every
    // poll would replay entrance animations while nothing had moved.
    const apply = accounts => (accounts || []).reduce((changed, account) => {
      const next = live.get(account.id);
      if (next === undefined || next === account.registration_status) return changed;
      account.registration_status = next;
      return true;
    }, false);
    const ownChanged = apply(state.sip_accounts);
    if (ownChanged && currentPage === 'sipaccounts') renderSipAccounts();
    if (workspace && $('workspace').classList.contains('open')) {
      const changed = apply(workspace.sip_accounts);
      updateWsDeviceChip();
      syncDeviceChip('#customer-status', state.sip_accounts);
      if (changed && (wsTab === 'devices' || wsTab === 'overview')) renderWsTab(wsTab);
    } else if (ownChanged) {
      syncDeviceChip('#customer-status', state.sip_accounts);
    }
  } catch { /* health polling already surfaces connectivity problems */ }
}

/* ------------------------------------------------------- 8. Render: shell */
function renderAll() {
  const summary = state.call_summary || {};
  countTo($('stat-total'), summary.total);
  countTo($('stat-answered'), summary.answered);
  countTo($('stat-failed'), summary.failed);
  countTo($('stat-recordings'), summary.recordings);
  countTo($('stat-voicemails'), state.voicemail_summary?.new);
  const rate = summary.total ? Math.round((summary.answered / summary.total) * 100) : 0;
  $('answer-rate').textContent = `${rate}% answer rate`;

  const activeNumbers = state.phone_numbers.filter(n => n.active);
  countTo($('dash-customers'), state.customers.filter(c => c.active).length);
  countTo($('dash-numbers'), activeNumbers.length);
  countTo($('dash-sipaccounts'), state.sip_accounts.filter(s => s.active).length);
  countTo($('dash-providers'), state.providers.filter(p => p.active).length);

  const pending = (state.requests || []).filter(r => r.status === 'pending').length;
  setBadge($('request-badge'), pending);
  setBadge($('notification-badge'), (state.notifications || []).filter(n => !n.read_at).length);

  if (!state.is_admin) renderCustomerStatus();
  renderExtensions();
  renderNumbers();
  renderProviders();
  renderCustomers();
  renderSipAccounts();
  renderRequests();
  renderMyRequests();
  renderActivity();
  renderNotifications();
  renderApiKeys();
  renderWebhooks();
  renderDeliveries();
  renderBilling();
  renderSelects();
  renderSettings();
  renderEmailSettings();
  renderFlow();

  const myExtension = state.extensions.find(x => x.extension === state.assigned_extension);
  $('profile-recording-form').hidden = !myExtension;
  $('profile-recording').checked = !!myExtension?.recording_enabled;
  const globalRecording = truth(state.settings?.recording_enabled);
  $('profile-recording-help').textContent = globalRecording
    ? 'Global recording is enabled; you can opt your extension in or out.'
    : 'The administrator has switched recording off for everyone; your preference is still saved for later.';
  $('profile-email').value = state.email || '';
  markStagger();
}

/* --------------------------------------------------- 9. Render: customers */
function renderCustomers() {
  const query = val('customer-search').toLowerCase();
  const rows = state.customers.filter(c =>
    `${c.company_name} ${c.full_name} ${c.username} ${c.email} ${c.phone}`.toLowerCase().includes(query));
  $('customer-count').textContent = `${rows.length} customer${rows.length === 1 ? '' : 's'}`;
  $('user-list').innerHTML = rows.map((c, i) => `
    <article class="glass-card card-enter" style="--i:${Math.min(i, 10)}">
      <div class="glass-card-head">
        <span class="ws-glyph">${esc((c.company_name || c.username || 'C')[0].toUpperCase())}</span>
        <div>
          <h3>${esc(c.company_name || c.username)}</h3>
          <p>${esc(c.full_name || c.username)} · ${esc(c.email)}</p>
        </div>
      </div>
      <div class="tags">
        ${c.active ? tag('Active', 'on') : tag('Disabled', 'off')}
        ${tag(`${c.number_count} number${c.number_count === 1 ? '' : 's'}`, '', '☎')}
        ${tag(`${c.sip_count} device${c.sip_count === 1 ? '' : 's'}`, '', '◈')}
        ${tag(`${c.extension_count} ext`, '', '⌁')}
        ${c.payment_status === 'current' ? tag('Paid up', 'on', '▣') : tag('Payment due', 'warn', '▣')}
      </div>
      <div class="ws-card-actions">
        <button class="btn primary sm" data-open-customer="${c.id}">Open workspace</button>
        <button class="btn ghost sm" data-edit-user="${c.id}">Edit details</button>
      </div>
    </article>`).join('') || empty('No customers yet', 'Create a customer, or wait for a public signup.', '◍', '',
      emptyAction('Create customer', 'data-open="user"', true));

  $('platform-admin-list').innerHTML = state.users.filter(u => u.role === 'admin').map(u => `
    <div class="row">
      <span class="row-icon">${esc((u.username || 'A')[0].toUpperCase())}</span>
      <div><h3>${esc(u.username)}</h3><p>${esc(u.email)}</p></div>
      <div class="tags">${u.active ? tag('Active', 'on') : tag('Disabled', 'off')}${tag('Administrator', 'violet')}</div>
      <div class="row-actions"><button class="btn danger sm" data-delete-user="${u.id}">Delete</button></div>
    </div>`).join('') || empty('No additional administrators', 'The bootstrap administrator remains active.', '⌂');
  markStagger();
}

/* -------------------------------------------------- 10. Render: extensions */
function extensionName(number) {
  const item = state.extensions.find(x => x.extension === number);
  return item?.display_name || `Extension ${number}`;
}
function renderExtensions() {
  const query = val('extension-search').toLowerCase();
  const rows = state.extensions.filter(x => `${x.extension} ${x.display_name} ${x.sip_username}`.toLowerCase().includes(query));
  $('extension-count').textContent = `${rows.length} extension${rows.length === 1 ? '' : 's'}`;
  const card = x => `
    <div class="row">
      <span class="row-icon">${esc(x.extension)}</span>
      <div><h3>${esc(x.display_name || `Extension ${x.extension}`)}</h3><p>SIP username: ${esc(x.sip_username)}</p></div>
      <div class="tags">
        ${x.active ? tag('Active', 'on') : tag('Disabled', 'off')}
        ${x.recording_enabled ? tag('Recording on', 'on') : tag('Recording off')}
        ${x.voicemail_enabled ? tag('Voicemail on', 'info') : tag('Voicemail off')}
      </div>
      <div class="row-actions">
        <button class="btn ghost sm" data-edit-extension="${x.extension}">Edit</button>
        <button class="btn danger sm" data-delete-extension="${x.extension}">Delete</button>
      </div>
    </div>`;
  if (state.is_admin) {
    const groups = groupBy(rows, x => {
      const owner = state.users.find(u => u.id === x.owner_user_id);
      return owner?.company_name || owner?.username || 'Platform / unassigned';
    });
    $('extension-list').innerHTML = Object.entries(groups).map(([name, items]) => `
      <div class="acc-item open">
        <div class="acc-head"><span class="row-icon">${esc(name[0].toUpperCase())}</span>
          <div><h3 style="font-size:13px">${esc(name)}</h3><p style="font-size:11px;color:var(--text-3)">${items.length} extension${items.length === 1 ? '' : 's'}</p></div>
          <span class="chev">›</span>
        </div>
        <div class="acc-body" style="padding:0">${items.map(card).join('')}</div>
      </div>`).join('') || empty('No extensions found', 'Open a customer workspace to create an extension.', '⌁');
  } else {
    $('extension-list').innerHTML = rows.map(card).join('') || empty('No extensions yet', 'Create departments such as Sales, Support or Operations.', '⌁', '',
      emptyAction('Create extension', 'data-open="extension"', true));
  }
  markStagger();
}

/* ----------------------------------------------------- 11. Render: numbers */
function renderNumbers() {
  const query = val('number-search').toLowerCase();
  const rows = state.phone_numbers.filter(x => `${x.number} ${x.provider} ${x.description} ${x.inbound_extension}`.toLowerCase().includes(query));
  $('number-count').textContent = `${rows.length} number${rows.length === 1 ? '' : 's'}`;
  $('number-list').innerHTML = rows.map(x => {
    const owner = state.users.find(u => u.id === x.owner_user_id);
    const sip = state.sip_accounts.find(s => s.phone_number === x.number);
    const expiring = x.discontinue_at && x.discontinue_at <= new Date().toISOString().slice(0, 10);
    return `
    <div class="row">
      <span class="row-icon">☎</span>
      <div><h3>${esc(x.number)}</h3><p>${esc(owner?.company_name || owner?.username || 'Platform unassigned')}</p></div>
      <div>
        <p style="font-size:12px;color:var(--text-2)">${state.is_admin ? esc(sip?.label || 'No device linked') : 'Managed by EIP'}</p>
        <div class="tags" style="margin-top:7px">
          ${x.active ? tag('Active', 'on') : tag('Disabled', 'off')}
          ${tag(`Ext ${x.inbound_extension || '—'}`, 'info')}
          ${tag(`${money(x.monthly_price_cents ?? 500)}/mo`)}
          ${x.default_outbound ? tag('Default caller ID', 'violet') : ''}
          ${x.discontinue_at ? tag(`Ends ${fmtDay(x.discontinue_at)}`, expiring ? 'off' : 'warn') : ''}
        </div>
      </div>
      <div class="row-actions">
        ${state.is_admin
          ? `<button class="btn ghost sm" data-edit-number="${x.id}">Manage</button><button class="btn danger sm" data-delete-number="${x.id}">Delete</button>`
          : (x.active && !x.default_outbound ? `<button class="btn primary sm" data-default-number="${x.id}">Use for outbound</button>` : '')}
      </div>
    </div>`;
  }).join('') || empty('No phone numbers', state.is_admin ? 'Assign a number from a customer workspace.' : 'Request a number to get started.', '☎', '',
      state.is_admin ? emptyAction('Assign a number', 'data-open="number"', true) : emptyAction('Request a number', 'data-open="request"', true));
  markStagger();
}

/* --------------------------------------------------- 12. Render: providers */
function renderProviders() {
  $('provider-list').innerHTML = state.providers.map(x => `
    <div class="row">
      <span class="row-icon">⇄</span>
      <div><h3>${esc(x.name)}</h3><p>${esc(x.server)}:${esc(x.port)} · ${esc(String(x.transport).toUpperCase())}</p></div>
      <div>
        <p style="font-size:12px;color:var(--text-2)">Gateway user: ${esc(x.username)}</p>
        <div class="tags" style="margin-top:7px">
          ${tag(x.codecs)}${tag(x.allowed_ips, 'violet')}
          ${x.active ? tag('Active', 'on') : tag('Disabled', 'off')}
        </div>
      </div>
      <div class="row-actions">
        <button class="btn ghost sm" data-edit-provider="${x.id}">Edit</button>
        <button class="btn danger sm" data-delete-provider="${x.id}">Delete</button>
      </div>
    </div>`).join('') || empty('No SIP providers', 'Add a carrier before assigning phone numbers.', '⇄', '',
      emptyAction('Add provider', 'data-open="provider"', true));
  markStagger();
}

/* ------------------------------------------------ 13. Render: SIP accounts */
function renderSipAccounts() {
  const rows = state.sip_accounts || [];
  $('sip-count').textContent = `${rows.length} account${rows.length === 1 ? '' : 's'}`;
  $('sip-account-list').innerHTML = rows.map((x, i) => {
    const owner = state.users.find(u => u.id === x.owner_user_id);
    return `
    <article class="glass-card card-enter" style="--i:${Math.min(i, 10)}">
      <div class="glass-card-head">
        <span class="ws-glyph">◈</span>
        <div><h3>${esc(x.label)}</h3><p>${esc(owner?.company_name || owner?.username || 'My account')}</p></div>
        ${registration(x)}
      </div>
      <div class="kv kv-2">
        ${kv('SIP username', x.sip_username)}
        ${kv('Server', `${x.server}:${x.port}`)}
        ${kv('Assigned number', x.phone_number || 'Not linked')}
        ${kv('Extension', x.extension || 'Not linked')}
      </div>
      <div class="ws-card-actions">
        <button class="btn primary sm" data-show-credentials="${x.id}">Credentials</button>
        ${state.is_admin ? `<button class="btn ghost sm" data-edit-sip="${x.id}">Edit</button><button class="btn danger sm" data-delete-sip="${x.id}">Delete</button>` : ''}
      </div>
    </article>`;
  }).join('') || empty('No devices yet', state.is_admin ? 'Assign SIP credentials here or from a customer workspace.' : 'Add the phone or softphone you want to connect.', '◈', '',
      emptyAction(state.is_admin ? 'Assign SIP service' : 'Add a device', 'data-open="sipaccount"', true));
  markStagger();
}

/* ------------------------------------------------- 14. Render: API & hooks */
function renderApiKeys() {
  $('api-key-list').innerHTML = (state.api_keys || []).map(x => `
    <div class="row">
      <span class="row-icon">⌘</span>
      <div><h3>${esc(x.name)}</h3><p><code>${esc(x.prefix)}…</code> · created ${esc(fmtDay(x.created_at))}</p></div>
      <div class="tags">
        <span class="tag on">Active</span>
        ${tag(x.scopes === '*' ? 'Full access' : x.scopes, 'info')}
        ${tag(`Last used ${x.last_used_at ? fmtDate(x.last_used_at) : 'never'}`)}
      </div>
      <div class="row-actions"><button class="btn danger sm" data-revoke-key="${x.id}">Revoke</button></div>
    </div>`).join('') || empty('No API keys', 'Create a scoped key so your own software can call the EIP API.', '⌘', '',
      emptyAction('Create API key', 'data-open="apikey"', true));
  markStagger();
}

function renderWebhooks() {
  $('webhook-list').innerHTML = state.webhooks.map(x => `
    <div class="row">
      <span class="row-icon">◇</span>
      <div><h3>${esc(x.name)}</h3><p>${esc(x.url)}</p></div>
      <div class="tags">
        ${x.active ? tag('Active', 'on') : tag('Paused', 'off')}
        ${x.has_token ? tag('Signed with secret', 'violet', '⚿') : tag('No secret', 'warn')}
        ${tag(x.events === '*' ? 'All events' : x.events, 'info')}
      </div>
      <div class="row-actions">
        <button class="btn ghost sm" data-test-webhook="${x.id}">Test</button>
        <button class="btn ghost sm" data-edit-webhook="${x.id}">Edit</button>
        <button class="btn danger sm" data-delete-webhook="${x.id}">Delete</button>
      </div>
    </div>`).join('') || empty('No webhook endpoints', 'Add an endpoint to push call events to your CRM.', '◇', '',
      emptyAction('Add webhook', 'data-open="webhook"', true));
  markStagger();
}

function renderDeliveries() {
  const rows = state.webhook_deliveries || [];
  $('delivery-count').textContent = `${rows.length} deliver${rows.length === 1 ? 'y' : 'ies'}`;
  $('webhook-delivery-list').innerHTML = rows.length ? `
    <table class="data"><thead><tr><th>Event</th><th>Endpoint</th><th>Status</th><th>Attempts</th><th>Updated</th><th>Last error</th></tr></thead>
    <tbody>${rows.map(x => `<tr>
      <td class="cell-strong">${esc(x.event)}</td>
      <td>${esc(x.webhook_name || x.endpoint_id || '—')}</td>
      <td>${statusPill(x.status)}</td>
      <td>${esc(x.attempts)}</td>
      <td>${esc(fmtDate(x.updated_at))}</td>
      <td>${esc(x.last_error || '—')}</td>
    </tr>`).join('')}</tbody></table>` : empty('No deliveries yet', 'Delivery attempts appear here once calls trigger your endpoints.', '◇');
}

/* ------------------------------------------------ 15. Render: requests etc */
function renderRequests() {
  const rows = state.requests || [];
  const pending = rows.filter(r => r.status === 'pending');
  $('request-count').textContent = `${pending.length} pending`;
  $('request-list').innerHTML = rows.map(x => `
    <div class="row">
      <span class="row-icon">↗</span>
      <div><h3>${esc(x.company_name || x.username)} · ${esc(String(x.request_type).replaceAll('_', ' '))}</h3><p>${esc(x.details)}</p></div>
      <div>${statusPill(x.status, `request-${x.id}`)}<p style="font-size:11px;color:var(--text-3);margin-top:6px">${esc(fmtDate(x.created_at))}</p></div>
      <div class="row-actions">
        ${x.status === 'pending' ? `
          <button class="btn primary sm" data-resolve-request="${x.id}:approved">Approve</button>
          <button class="btn ghost sm" data-assign-request="${x.id}:${x.user_id}">Assign number</button>
          <button class="btn danger sm" data-resolve-request="${x.id}:rejected">Reject</button>` : ''}
      </div>
    </div>`).join('') || empty('No requests', 'Customer number and access requests will appear here.', '↗');
  markStagger();
}

function renderMyRequests() {
  const host = $('my-request-list');
  if (!host) return;
  const rows = state.requests || [];
  const pendingNumber = rows.some(r => r.request_type === 'number' && r.status === 'pending');
  const button = $('request-number');
  if (button) {
    button.disabled = pendingNumber;
    button.textContent = pendingNumber ? 'Number request pending' : (state.phone_numbers.length ? 'Request another number' : 'Request a number');
  }
  $('my-request-count').textContent = `${rows.length} request${rows.length === 1 ? '' : 's'}`;
  host.innerHTML = rows.slice(0, 5).map(x => `
    <div class="tl-item"><b>${esc(String(x.request_type).replaceAll('_', ' '))}</b>
      <p>${esc(x.details)}</p>
      ${x.admin_note ? `<small>Administrator: ${esc(x.admin_note)}</small>` : ''}
      <small>${esc(fmtDate(x.created_at))}</small>
      <div class="tags" style="margin-top:7px">${statusPill(x.status, `myrequest-${x.id}`)}</div>
    </div>`).join('') || empty('No requests yet', 'Request a phone number and its progress will appear here.', '↗');

  /* Setup journey: completed steps are ticked, the first unfinished step is the
     current milestone, and every milestone links to the page that advances it. */
  // Customers raise requests from their dashboard; administrators review them
  // on the requests page, so each milestone points at the right surface.
  const requestPage = state.is_admin ? 'requests' : 'dashboard';
  const steps = [
    ['request', 'Request a number', rows.some(r => r.request_type === 'number'), requestPage],
    ['number', 'Number assigned', !!state.phone_numbers.length, 'numbers'],
    ['sip', 'Connect a device', !!state.sip_accounts.length, 'sipaccounts'],
    ['extensions', 'Extensions', !!state.extensions.length, 'extensions'],
    ['routing', 'Call routing', !!state.call_routes.length, 'routing'],
    ['api', 'APIs & webhooks', !!(state.api_keys.length || myWebhooks()), 'webhooks'],
  ];
  const currentIndex = steps.findIndex(([, , done]) => !done);
  const doneCount = steps.filter(([, , done]) => done).length;
  const percent = Math.round((doneCount / steps.length) * 100);
  const journey = [
    `<div class="journey-head"><b>${doneCount} of ${steps.length} steps complete</b>`,
    `<span class="journey-bar"><i style="--p:${percent}%"></i></span>`,
    `<small>${steps.length - doneCount === 0 ? 'Your phone system is fully live' : `Next: ${esc(steps[currentIndex][1])}`}</small></div>`,
    '<div class="journey-steps">',
  ];
  steps.forEach(([key, label, done, page], index) => {
    if (index) journey.push(`<i class="${steps[index - 1][2] ? 'done' : ''}"></i>`);
    const state = done ? 'done' : (index === currentIndex ? 'current' : '');
    const glyph = done ? '✓' : index === currentIndex ? '◐' : index + 1;
    journey.push(`<button class="journey-step ${state}" data-page="${esc(page)}" data-journey="${esc(key)}" type="button">
      <span class="tick">${glyph}</span>${esc(label)}</button>`);
  });
  journey.push('</div>');
  $('customer-journey').innerHTML = journey.join('');
  markStagger();
}

/* Webhooks are already scoped to the signed-in customer in non-admin payloads. */
function myWebhooks() {
  return (state.webhooks || []).length;
}

function renderActivity() {
  $('activity-list').innerHTML = (state.activity || []).map(x => `
    <div class="tl-item"><b>${esc(x.description)}</b><p>${esc(String(x.action).replaceAll('.', ' '))} · ${esc(x.resource_type)}</p><small>${esc(fmtDate(x.created_at))}</small></div>
  `).join('') || empty('No activity yet', 'Important platform changes will be recorded here.', '◌');
  markStagger();
}

function renderNotifications() {
  $('notification-list').innerHTML = (state.notifications || []).map(x => `
    <div class="row">
      <span class="row-icon">${x.read_at ? '○' : '●'}</span>
      <div><h3>${esc(x.title)}</h3><p>${esc(x.message)}</p></div>
      <div>${x.read_at ? tag('Read') : tag('Unread', 'warn')}<p style="font-size:11px;color:var(--text-3);margin-top:6px">${esc(fmtDate(x.created_at))}</p></div>
      <div class="row-actions">${x.read_at ? '' : `<button class="btn ghost sm" data-read-notification="${x.id}">Mark read</button>`}</div>
    </div>`).join('') || empty('You are all caught up', 'New assignments and service events will appear here.', '●');
  markStagger();
}

/* --------------------------------------------------- 16. Render: billing */
function renderBilling() {
  const numbers = state.phone_numbers || [];
  $('subscription-list').innerHTML = numbers.map(x => `
    <div class="row">
      <span class="row-icon">$</span>
      <div><h3>${esc(x.number)}</h3><p>${esc(x.description || 'EIP phone number')}</p></div>
      <div class="tags">
        ${tag(`${money(x.monthly_price_cents ?? 500)}/month`, 'on')}
        ${tag(`Renews day ${x.billing_cycle_day || 1}`)}
        ${x.discontinue_at ? tag(`Ends ${fmtDay(x.discontinue_at)}`, 'off') : ''}
      </div>
      <div class="row-actions">${!state.is_admin && !x.discontinue_at ? `<button class="btn danger sm" data-discontinue-number="${esc(x.number)}">Discontinue at renewal</button>` : ''}</div>
    </div>`).join('') || empty('No active subscriptions', state.is_admin ? 'Assign a number to start billing.' : 'An administrator will assign your phone numbers.', '▣');

  const invoices = state.invoices || [];
  $('invoice-list').innerHTML = invoices.length ? `
    <table class="data"><thead><tr><th>Invoice</th><th>Number</th><th>Period</th><th>Amount</th><th>Status</th><th>Due</th><th>Action</th></tr></thead>
    <tbody>${invoices.map(x => `<tr>
      <td class="cell-strong">#${esc(x.id)}</td>
      <td>${esc(x.number)}</td>
      <td>${esc(x.period_start)} → ${esc(x.period_end)}</td>
      <td>${money(x.amount_cents)}</td>
      <td>${statusPill(x.status)}</td>
      <td>${esc(fmtDay(x.due_at))}</td>
      <td>${state.is_admin && x.status === 'open' ? `<button class="btn ghost sm" data-paid-invoice="${x.id}">Mark paid</button>` : '—'}</td>
    </tr>`).join('')}</tbody></table>` : empty('No invoices yet', 'Invoices appear automatically for assigned numbers.', '▣');
  markStagger();
}

/* --------------------------------------------------- 17. Render: settings */
function optionList(includeEmpty = false) {
  return `${includeEmpty ? '<option value="">Use fallback extension</option>' : ''}${state.extensions.filter(x => x.active)
    .map(x => `<option value="${esc(x.extension)}">${esc(x.extension)} — ${esc(x.display_name || 'Unnamed')}</option>`).join('')}`;
}

function renderSelects() {
  const customerSelect = $('recording-customer');
  if (customerSelect) customerSelect.innerHTML = '<option value="">All customers</option>' + state.customers
    .map(x => `<option value="${x.id}">${esc(x.company_name || x.username)}</option>`).join('');
  const allExtensions = `<option value="">All extensions</option>${optionList()}`;
  $('call-extension').innerHTML = allExtensions;
  $('recording-extension').innerHTML = allExtensions;
  $('voicemail-extension').innerHTML = `<option value="">All mailboxes</option>${state.extensions
    .filter(x => x.voicemail_enabled).map(x => `<option value="${esc(x.extension)}">${esc(x.extension)} — ${esc(x.display_name || 'Unnamed')}</option>`).join('')}`;
  $('default-extension').innerHTML = optionList();
  $('inbound-fallback').innerHTML = optionList();
}

function renderSettings() {
  const s = state.settings || {};
  const first = state.extensions.find(x => x.active)?.extension || '';
  $('default-extension').value = s.default_extension || first;
  $('inbound-fallback').value = s.inbound_fallback_extension || s.default_extension || first;
  $('rec-enabled').checked = truth(s.recording_enabled);
  $('rec-format').value = s.recording_format || 'wav';
  $('rec-retention').value = s.recording_retention_days || 90;
  $('rec-max').value = s.recording_max_duration_seconds || 0;
  $('rec-announcement').checked = truth(s.recording_announcement);
  $('rec-media').value = s.recording_announcement_media || '';
  $('rec-beep').checked = truth(s.recording_beep);
  markStagger();
}

function renderEmailSettings() {
  const config = state.email_config || {};
  $('email-enabled').checked = !!config.enabled;
  $('sendgrid-from').value = config.from_email || '';
  $('sendgrid-name').value = config.from_name || 'EIP Telephony Voicemail';
  $('sendgrid-key-status').textContent = config.has_api_key ? 'API key configured — leave blank to keep it' : 'No API key configured';
  const rows = state.email_deliveries || [];
  $('email-delivery-list').innerHTML = rows.length ? `
    <table class="data"><thead><tr><th>Mailbox</th><th>Recipient</th><th>Status</th><th>Attempts</th><th>Updated</th><th>Error</th></tr></thead>
    <tbody>${rows.map(x => `<tr>
      <td class="cell-strong">${esc(x.mailbox)}</td><td>${esc(x.recipient)}</td><td>${statusPill(x.status, `mail-${x.id}`)}</td>
      <td>${esc(x.attempts)}</td><td>${esc(fmtDate(x.updated_at))}</td><td>${esc(x.last_error || '—')}</td>
    </tr>`).join('')}</tbody></table>` : empty('No delivery attempts', 'New voicemail email attempts will appear here.', '✎');
}

/* ------------------------------------------------------ 18. Call tables */
function callTable(calls, compact = false) {
  if (!calls.length) return empty('No calls found', 'Calls will appear here once activity begins.', '◷');
  return `<table class="data"><thead><tr>
      <th>Caller / destination</th><th>Direction</th><th>Assigned number</th><th>Extension</th><th>Status</th><th>Started</th><th>Duration</th>${compact ? '' : '<th>Recording / reference</th>'}
    </tr></thead><tbody>${calls.map(x => `<tr>
      <td class="cell-strong">${esc(x.phone)}<span class="cell-sub">${esc(x.provider || 'EIP network')}</span></td>
      <td>${tag(x.direction || 'outbound', x.direction === 'inbound' ? 'info' : '')}</td>
      <td>${esc(x.caller_id_number || '—')}</td>
      <td>${esc(x.extension)}<span class="cell-sub">${esc(extensionName(x.extension))}</span></td>
      <td>${statusPill(x.status, `call-${x.call_id}`)}</td>
      <td>${esc(fmtDate(x.started_at))}</td>
      <td>${fmtDuration(x.duration_seconds)}</td>
      ${compact ? '' : `<td>${x.recording_status === 'finalized' ? tag('Available', 'on') : tag('None')}<span class="cell-sub">${esc(x.contact_id || x.call_id || '—')}</span></td>`}
    </tr>`).join('')}</tbody></table>`;
}

async function loadCalls() {
  const params = new URLSearchParams({ limit: '50', offset: String(callOffset) });
  if (val('call-extension')) params.set('extension', val('call-extension'));
  if (val('call-status')) params.set('status', val('call-status'));
  if (val('call-search')) params.set('q', val('call-search'));
  $('call-list').innerHTML = skeletonRows(6);
  setLoading($('call-list'), true);
  try {
    const data = await api(`/admin/api/calls?${params}`);
    $('call-count').textContent = `${fmtNum(data.total)} call${data.total === 1 ? '' : 's'}`;
    $('call-list').innerHTML = callTable(data.calls);
    renderPager('call-pager', data.total, callOffset, value => { callOffset = value; loadCalls(); });
    markStagger();
  } catch (error) {
    $('call-list').innerHTML = errorState('Calls could not be loaded', error.message, 'calls');
    notify(error.message, true);
  } finally {
    setLoading($('call-list'), false);
  }
}

function renderPager(id, total, offset, callback) {
  const target = $(id);
  target.innerHTML = '';
  if (total <= 50) return;
  const previous = document.createElement('button');
  const next = document.createElement('button');
  previous.className = next.className = 'btn ghost sm';
  previous.textContent = '← Previous';
  next.textContent = 'Next →';
  previous.disabled = offset === 0;
  next.disabled = offset + 50 >= total;
  previous.onclick = () => callback(Math.max(0, offset - 50));
  next.onclick = () => callback(offset + 50);
  target.append(previous, next);
}

/* ------------------------------------------------------- 19. Recordings */
function waveform() {
  return `<span class="wave">${Array.from({ length: 30 }, (_, i) => `<i style="height:${7 + (i * 11) % 24}px"></i>`).join('')}</span>`;
}

async function loadRecordings() {
  const params = new URLSearchParams({ limit: '50', offset: String(recordingOffset), recordings: 'true' });
  if (val('recording-extension')) params.set('extension', val('recording-extension'));
  if (val('recording-search')) params.set('q', val('recording-search'));
  setLoading($('recording-list'), true);
  try {
    const data = await api(`/admin/api/calls?${params}`);
    const customerId = Number(val('recording-customer') || 0);
    const from = val('recording-from'), to = val('recording-to');
    const owned = customerId ? new Set(state.extensions.filter(x => x.owner_user_id === customerId).map(x => x.extension)) : null;
    const calls = data.calls.filter(x =>
      (!owned || owned.has(x.extension)) &&
      (!from || String(x.started_at).slice(0, 10) >= from) &&
      (!to || String(x.started_at).slice(0, 10) <= to));
    $('recording-count').textContent = `${calls.length} recording${calls.length === 1 ? '' : 's'}`;
    const groups = groupBy(calls, x => x.extension);
    $('recording-list').innerHTML = Object.entries(groups).map(([ext, items]) => `
      <div class="acc-item open">
        <div class="acc-head">
          <span class="row-icon">◉</span>
          <div><h3 style="font-size:13px">Extension ${esc(ext)} · ${esc(extensionName(ext))}</h3>
            <p style="font-size:11px;color:var(--text-3)">${items.length} recording${items.length === 1 ? '' : 's'}</p></div>
          <span class="chev">›</span>
        </div>
        <div class="acc-body" style="padding:0 18px 16px">${items.map(x => `
          <div class="row rec-row" style="grid-template-columns:minmax(150px,1fr) minmax(140px,auto) minmax(240px,1.4fr)">
            <div><h3>${esc(x.phone)}</h3><p>${esc(fmtDate(x.started_at))} · ${fmtDuration(x.duration_seconds)}</p></div>
            <div>${statusPill(x.recording_status, `rec-${x.call_id}`)}<span class="cell-sub">Call ${esc(x.call_id)}</span></div>
            <div style="display:flex;align-items:center;gap:11px;flex-wrap:wrap">
              ${waveform()}
              ${x.recording_status === 'finalized'
                ? `<audio controls preload="none" src="/admin/api/recordings/${encodeURIComponent(x.call_id)}/file"></audio>
                   <a class="btn ghost sm" download href="/admin/api/recordings/${encodeURIComponent(x.call_id)}/file">Download</a>`
                : '<small style="color:var(--text-3)">Audio becomes available after finalisation.</small>'}
            </div>
          </div>`).join('')}</div>
      </div>`).join('') || empty('No recordings found', 'Try another extension, or complete a recorded call.', '◉');
    renderPager('recording-pager', data.total, recordingOffset, value => { recordingOffset = value; loadRecordings(); });
    markStagger();
  } catch (error) {
    $('recording-list').innerHTML = errorState('Recordings could not be loaded', error.message, 'recordings');
    notify(error.message, true);
  } finally {
    setLoading($('recording-list'), false);
  }
}

/* -------------------------------------------------------- 20. Voicemail */
async function loadVoicemails() {
  const params = new URLSearchParams();
  if (val('voicemail-extension')) params.set('extension', val('voicemail-extension'));
  if (val('voicemail-folder')) params.set('folder', val('voicemail-folder'));
  setLoading($('voicemail-list'), true);
  try {
    const data = await api(`/admin/api/voicemails?${params}`);
    const query = val('voicemail-search').toLowerCase();
    const messages = data.voicemails.filter(x => `${x.caller_id} ${x.message} ${x.mailbox}`.toLowerCase().includes(query));
    $('voicemail-count').textContent = `${messages.length} message${messages.length === 1 ? '' : 's'}`;
    const groups = groupBy(messages, x => x.mailbox);
    $('voicemail-list').innerHTML = Object.entries(groups).map(([mailbox, items]) => `
      <div class="acc-item open">
        <div class="acc-head">
          <span class="row-icon">✉</span>
          <div><h3 style="font-size:13px">Mailbox ${esc(mailbox)} · ${esc(extensionName(mailbox))}</h3>
            <p style="font-size:11px;color:var(--text-3)">${items.filter(x => x.folder === 'inbox').length} new of ${items.length}</p></div>
          <span class="chev">›</span>
        </div>
        <div class="acc-body" style="padding:0 18px 16px">${items.map(x => `
          <div class="row" style="grid-template-columns:minmax(150px,1fr) minmax(220px,1.3fr) auto">
            <div><h3>${esc(x.caller_id)}</h3><p>${esc(fmtDate(x.received_at))} · ${fmtDuration(x.duration_seconds)}</p></div>
            <div style="display:flex;align-items:center;gap:10px">
              ${tag(x.folder, x.folder === 'inbox' ? 'warn' : x.folder === 'urgent' ? 'off' : '')}
              <audio controls preload="none" src="/admin/api/voicemails/${esc(x.mailbox)}/${esc(x.folder)}/${esc(x.message)}/file"></audio>
            </div>
            <div class="row-actions">
              ${x.folder === 'inbox' ? `<button class="btn ghost sm" data-read-voicemail="${esc(x.mailbox)}:${esc(x.folder)}:${esc(x.message)}">Mark read</button>` : ''}
              <button class="btn danger sm" data-delete-voicemail="${esc(x.mailbox)}:${esc(x.folder)}:${esc(x.message)}">Delete</button>
            </div>
          </div>`).join('')}</div>
      </div>`).join('') || empty('No voicemail messages', 'Enable voicemail on an extension and unanswered callers can leave a message.', '✉');
    markStagger();
  } catch (error) {
    $('voicemail-list').innerHTML = errorState('Voicemail could not be loaded', error.message, 'voicemails');
    notify(error.message, true);
  } finally {
    setLoading($('voicemail-list'), false);
  }
}

async function voicemailAction(action, value) {
  const [mailbox, folder, message] = value.split(':');
  const suffix = action === 'read' ? '/read' : '';
  if (action === 'delete' && !confirm('Delete this voicemail permanently?')) return;
  try {
    await api(`/admin/api/voicemails/${mailbox}/${folder}/${message}${suffix}`, { method: action === 'read' ? 'POST' : 'DELETE' });
    notify(action === 'read' ? 'Voicemail marked as read' : 'Voicemail deleted');
    await loadVoicemails();
    await loadState();
  } catch (error) { notify(error.message, true); }
}

/* ---------------------------------------------------- 21. Call flow studio */
const FLOW_ICONS = { business_hours: '◷', simultaneous: '⇉', sequential: '⇢', ring_group: '◎', extension: '⌁', voicemail: '✉', forward: '↗' };
const FLOW_TILES = { business_hours: 'tile-hours', simultaneous: 'tile-ring', sequential: 'tile-seq', ring_group: 'tile-group', extension: 'tile-ext', voicemail: 'tile-vm', forward: 'tile-fwd' };

function renderFlow() {
  const select = $('route-number');
  if (!select) return;
  const prior = select.value;
  select.innerHTML = (state.phone_numbers || []).map(x => `<option value="${esc(x.number)}">${esc(x.number)} — ${esc(x.description || 'Main')}</option>`).join('');
  select.value = prior || select.options[0]?.value || '';
  $('flow-entry-number').textContent = select.value || 'Assign a number to begin';
  const saved = (state.call_routes || []).find(x => x.phone_number === select.value);
  if (saved) flowNodes = saved.route?.nodes || [];
  renderFlowNodes();
}

function renderFlowNodes() {
  const host = $('flow-nodes');
  if (!host) return;
  host.innerHTML = flowNodes.map((node, index) => `
    <div class="flow-node type-${esc(node.type)} ${node.configured ? 'configured' : ''}" draggable="true" data-flow-index="${index}" style="--i:${Math.min(index, 8)}">
      <span class="icon ${FLOW_TILES[node.type] || 'tile-ext'}">${FLOW_ICONS[node.type] || '◇'}</span>
      <div class="copy"><b>${esc(String(node.type).replaceAll('_', ' '))}</b><small>${esc(node.label || 'Click to configure this step')}</small></div>
      <span class="step">${String(index + 1).padStart(2, '0')}</span>
      <button class="remove" data-remove-node="${index}" aria-label="Remove step">✕</button>
    </div>`).join('');
  $('flow-canvas').classList.toggle('has-nodes', flowNodes.length > 0);
}

function flowExtensionOptions(selected = []) {
  const values = Array.isArray(selected) ? selected : [selected];
  const number = (state.phone_numbers || []).find(x => x.number === $('route-number').value);
  const available = state.is_admin && number?.owner_user_id
    ? state.extensions.filter(x => x.owner_user_id === number.owner_user_id)
    : state.extensions;
  return available.map(x => `<option value="${esc(x.extension)}" ${values.includes(x.extension) ? 'selected' : ''}>${esc(x.extension)} — ${esc(x.display_name || 'Extension')}</option>`).join('');
}

function openFlowConfig(index) {
  flowConfigIndex = index;
  const node = flowNodes[index];
  if (!node) return;
  const common = `<label class="field">Step label<input name="label" maxlength="80" value="${esc(node.label === 'Click to configure' ? '' : node.label || '')}"></label>`;
  let fields = '';
  if (node.type === 'business_hours') {
    fields = `<div class="field-row">
        <label class="field">Open time<input type="time" name="start" value="${esc(node.start || '09:00')}" required></label>
        <label class="field">Close time<input type="time" name="end" value="${esc(node.end || '17:00')}" required></label>
      </div>
      <label class="field">Business days<select name="days" multiple size="7">${['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
        .map((day, i) => `<option value="${i + 1}" ${(node.days || [1,2,3,4,5]).includes(i + 1) ? 'selected' : ''}>${day}</option>`).join('')}</select>
        <small>Use Ctrl/Cmd to select multiple days.</small></label>`;
  } else if (['simultaneous', 'sequential', 'ring_group'].includes(node.type)) {
    fields = `<label class="field">Ring destinations<select name="extensions" multiple size="6" required>${flowExtensionOptions(node.extensions || [])}</select><small>Select at least one extension.</small></label>
      <label class="field">Ring timeout (seconds)<input type="number" name="timeout" min="5" max="120" value="${node.timeout || 25}" required></label>`;
  } else if (node.type === 'extension') {
    fields = `<label class="field">Destination extension<select name="extension" required><option value="">Choose extension</option>${flowExtensionOptions(node.extension || '')}</select></label>`;
  } else if (node.type === 'voicemail') {
    fields = `<label class="field">Voicemail mailbox<select name="mailbox" required><option value="">Choose mailbox</option>${flowExtensionOptions(node.mailbox || '')}</select></label>`;
  } else if (node.type === 'forward') {
    fields = `<label class="field">Forward to E.164 number<input name="phone" type="tel" pattern="\\+[1-9][0-9]{7,14}" placeholder="+13025550123" value="${esc(node.phone || '')}" required></label>
      <label class="field">Ring timeout (seconds)<input type="number" name="timeout" min="5" max="120" value="${node.timeout || 25}" required></label>`;
  }
  $('flow-config-title').textContent = `Configure ${String(node.type).replaceAll('_', ' ')}`;
  $('flow-config-fields').innerHTML = common + fields;
  openOverlay('flow-config-modal');
}

function saveFlowConfig(event) {
  event.preventDefault();
  const node = flowNodes[flowConfigIndex];
  const form = new FormData(event.target);
  if (!node) return closeOverlay('flow-config-modal');
  node.label = String(form.get('label') || '').trim();
  if (node.type === 'business_hours') {
    node.start = form.get('start');
    node.end = form.get('end');
    node.days = form.getAll('days').map(Number);
    if (!node.days.length) return notify('Select at least one business day', true);
    node.label = node.label || `${node.start}–${node.end} · ${node.days.length} days`;
  } else if (['simultaneous', 'sequential', 'ring_group'].includes(node.type)) {
    node.extensions = form.getAll('extensions');
    node.timeout = Number(form.get('timeout'));
    if (!node.extensions.length) return notify('Select at least one extension', true);
    node.label = node.label || `${node.extensions.join(', ')} · ${node.timeout}s`;
  } else if (node.type === 'extension') {
    node.extension = form.get('extension');
    node.label = node.label || `Extension ${node.extension}`;
  } else if (node.type === 'voicemail') {
    node.mailbox = form.get('mailbox');
    node.label = node.label || `Mailbox ${node.mailbox}`;
  } else if (node.type === 'forward') {
    node.phone = form.get('phone');
    node.timeout = Number(form.get('timeout'));
    node.label = node.label || `${node.phone} · ${node.timeout}s`;
  }
  node.configured = true;
  renderFlowNodes();
  closeOverlay('flow-config-modal');
  notify('Routing step configured');
}

/* ------------------------------------------------------ 22. Overlay engine */
function openOverlay(id) {
  const node = $(id);
  node.classList.add('open');
  node.setAttribute('aria-hidden', 'false');
}
function closeOverlay(id) {
  const node = $(id);
  node.classList.remove('open');
  node.setAttribute('aria-hidden', 'true');
}
function closeModal() {
  closeOverlay('modal');
  $('modal-form').reset();
  $('modal-save').hidden = false;
  $('modal-card').classList.remove('wide');
  editing = null;
  pendingFulfilRequest = null;
}

/* --------------------------------------------------- 23. Modal templates */
const activeCustomers = () => state.users.filter(x => x.role === 'user' && x.active);

const templates = {
  call: () => ({
    title: 'New outbound call',
    subtitle: 'Your phone rings first. The customer sees the selected callback number.',
    fields: `<label class="field">Customer phone number<input name="phone" type="tel" placeholder="+13025550123" required></label>
      <div class="field-row">
        <label class="field">Extension<select name="extension" ${state.is_admin ? '' : 'disabled'}>${state.extensions.filter(x => x.active)
          .map(x => `<option value="${x.extension}">${x.extension} — ${esc(x.display_name || 'Unnamed')}</option>`).join('')}</select></label>
        <label class="field">Callback / caller ID<select name="caller_id_number"><option value="">Use default assigned number</option>${state.phone_numbers.filter(x => x.active)
          .map(x => `<option value="${esc(x.number)}" ${x.default_outbound ? 'selected' : ''}>${esc(x.number)} — ${esc(x.description || '')}</option>`).join('')}</select></label>
      </div>
      <div class="field-row">
        <label class="field">CRM contact ID (optional)<input name="contact_id"></label>
        <label class="field">CRM member ID (optional)<input name="member_id"></label>
      </div>`,
  }),
  extension: item => ({
    title: item ? 'Edit extension' : 'Add extension',
    subtitle: 'Configure the SIP identity and per-extension policies',
    fields: `${state.is_admin ? `<label class="field">Customer account<select name="owner_user_id"><option value="">Platform / administrator</option>${activeCustomers()
      .map(x => `<option value="${x.id}" ${item?.owner_user_id === x.id ? 'selected' : ''}>${esc(x.username)} — ${esc(x.email)}</option>`).join('')}</select>
      <small>The customer will be able to manage this extension.</small></label>` : ''}
      <div class="field-row">
        <label class="field">Extension<input name="extension" inputmode="numeric" maxlength="3" ${item ? 'readonly' : ''} placeholder="102" required value="${esc(item?.extension || '')}"></label>
        <label class="field">Employee / display name<input name="display_name" maxlength="120" placeholder="Sales desk" value="${esc(item?.display_name || '')}"></label>
      </div>
      <div class="field-row">
        <label class="field">SIP username<input name="sip_username" maxlength="80" placeholder="Defaults to extension" value="${esc(item?.sip_username || '')}"></label>
        <label class="field">SIP password<input name="sip_password" type="password" autocomplete="new-password" placeholder="${item ? 'Leave blank to keep existing' : 'Required'}"></label>
      </div>
      <label class="check" style="margin-bottom:13px"><input name="active" type="checkbox" ${!item || item.active ? 'checked' : ''}> Active and allowed to make calls</label>
      <label class="check" style="margin-bottom:13px"><input name="recording_enabled" type="checkbox" ${item?.recording_enabled ? 'checked' : ''}> Allow recording when global recording is enabled</label>
      <div class="field-row">
        <label class="check"><input name="voicemail_enabled" type="checkbox" ${item?.voicemail_enabled ? 'checked' : ''}> Enable voicemail</label>
        <label class="field">Voicemail PIN<input name="voicemail_pin" type="password" inputmode="numeric" pattern="[0-9]{4,10}" placeholder="${item ? 'Leave blank to keep existing' : '4 to 10 digits'}"></label>
      </div>
      <label class="field">Voicemail notification email<input name="voicemail_email" type="email" placeholder="employee@example.com" value="${esc(item?.voicemail_email || '')}"><small>New messages are sent here when SendGrid is enabled.</small></label>
      <label class="check"><input name="webrtc_enabled" type="checkbox" ${item?.webrtc_enabled ? 'checked' : ''}> WebRTC enabled</label>`,
  }),
  number: item => ({
    title: item ? 'Manage phone number' : 'Assign phone number',
    subtitle: 'Assign the carrier, owner, routing and monthly billing',
    fields: `<label class="field">Customer account<select name="owner_user_id"><option value="">Platform / administrator</option>${activeCustomers()
      .map(x => `<option value="${x.id}" ${item?.owner_user_id === x.id ? 'selected' : ''}>${esc(x.username)} — ${esc(x.email)}</option>`).join('')}</select></label>
      <label class="field">Phone number (E.164)<input name="number" type="tel" ${item ? 'readonly' : ''} placeholder="+13025551234" required value="${esc(item?.number || '')}"></label>
      <div class="field-row">
        <label class="field">SIP provider<select name="provider" required><option value="">Select provider</option>${state.providers.filter(x => x.active)
          .map(x => `<option value="${esc(x.name)}" ${item?.provider === x.name ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select></label>
        <label class="field">Inbound extension<select name="inbound_extension"><option value="">Choose after creating an extension</option>${state.extensions.filter(x => x.active && String(x.owner_user_id ?? '') === String(item?.owner_user_id ?? ''))
          .map(x => `<option value="${x.extension}" ${item?.inbound_extension === x.extension ? 'selected' : ''}>${x.extension} — ${esc(x.display_name || 'Unnamed')}</option>`).join('')}</select></label>
      </div>
      <label class="field">Description<input name="description" maxlength="160" placeholder="Customer primary number" value="${esc(item?.description || '')}"></label>
      <div class="field-row">
        <label class="field">Monthly price (USD)<input name="monthly_price" type="number" min="0" step="0.01" value="${((item?.monthly_price_cents ?? 500) / 100).toFixed(2)}"></label>
        <label class="field">Billing cycle day<input name="billing_cycle_day" type="number" min="1" max="28" value="${item?.billing_cycle_day || 1}"></label>
      </div>
      <div class="field-row">
        <label class="field">Billing start<input name="billing_start" type="date" value="${esc(item?.billing_start || '')}"></label>
        <label class="field">Discontinue on<input name="discontinue_at" type="date" value="${esc(item?.discontinue_at || '')}"><small>Leave blank to keep active.</small></label>
      </div>
      <label class="check" style="margin-bottom:13px"><input name="default_outbound" type="checkbox" ${item?.default_outbound ? 'checked' : ''}> Default outbound caller ID for this extension</label>
      <label class="check"><input name="active" type="checkbox" ${!item || item.active ? 'checked' : ''}> Number is active</label>`,
  }),
  sipaccount: item => ({
    title: item ? 'Edit SIP service' : 'Assign SIP service',
    subtitle: 'Secure credentials and device mapping for one customer device',
    fields: `<label class="field">Customer<select name="owner_user_id" required>${activeCustomers()
      .map(x => `<option value="${x.id}" ${item?.owner_user_id === x.id ? 'selected' : ''}>${esc(x.company_name || x.username)}</option>`).join('')}</select></label>
      <div class="field-row">
        <label class="field">Label<input name="label" required value="${esc(item?.label || 'Primary softphone')}"></label>
        <label class="field">SIP username<input name="sip_username" required value="${esc(item?.sip_username || '')}"></label>
      </div>
      <div class="field-row">
        <label class="field">SIP password<input name="sip_password" type="password" autocomplete="new-password" placeholder="${item ? 'Leave blank to keep existing' : 'Required'}"></label>
        <label class="field">Server<input name="server" required value="${esc(item?.server || location.hostname)}"></label>
      </div>
      <div class="field-row">
        <label class="field">Port<input name="port" type="number" value="${item?.port || 5060}"></label>
        <label class="field">Transport<select name="transport"><option value="udp">UDP</option><option value="tcp" ${item?.transport === 'tcp' ? 'selected' : ''}>TCP</option><option value="tls" ${item?.transport === 'tls' ? 'selected' : ''}>TLS</option></select></label>
      </div>
      <div class="field-row">
        <label class="field">Assigned number<select name="phone_number"><option value="">None</option>${state.phone_numbers
          .map(x => `<option value="${esc(x.number)}" ${item?.phone_number === x.number ? 'selected' : ''}>${esc(x.number)}</option>`).join('')}</select></label>
        <label class="field">Extension<select name="extension"><option value="">None</option>${state.extensions
          .map(x => `<option value="${x.extension}" ${item?.extension === x.extension ? 'selected' : ''}>${x.extension} — ${esc(x.display_name || 'Extension')}</option>`).join('')}</select></label>
      </div>
      <label class="check"><input name="active" type="checkbox" ${!item || item.active ? 'checked' : ''}> Device is active</label>`,
  }),
  provider: item => ({
    title: item ? 'Edit SIP provider' : 'Add SIP provider',
    subtitle: 'Administrator-only carrier credentials and trusted source networks',
    fields: `<label class="field">Provider name<input name="name" maxlength="80" placeholder="IPComms" ${item ? 'readonly' : ''} required value="${esc(item?.name || '')}"></label>
      <div class="field-row">
        <label class="field">SIP server<input name="server" placeholder="sip.example.com" required value="${esc(item?.server || '')}"></label>
        <label class="field">Port<input name="port" type="number" min="1" max="65535" required value="${item?.port || 5060}"></label>
      </div>
      <div class="field-row">
        <label class="field">Username<input name="username" required value="${esc(item?.username || '')}"></label>
        <label class="field">Password<input name="password" type="password" autocomplete="new-password" placeholder="${item ? 'Leave blank to keep existing' : 'Required'}"></label>
      </div>
      <div class="field-row">
        <label class="field">Transport<select name="transport"><option value="udp" ${item?.transport !== 'tcp' ? 'selected' : ''}>UDP</option><option value="tcp" ${item?.transport === 'tcp' ? 'selected' : ''}>TCP</option></select></label>
        <label class="field">Codecs<input name="codecs" value="${esc(item?.codecs || 'ulaw,alaw')}" required></label>
      </div>
      <label class="field">Allowed provider IPs / CIDRs<input name="allowed_ips" placeholder="203.0.113.10/32,203.0.113.0/24" required value="${esc(item?.allowed_ips || '')}"><small>Required. Only these networks may identify as this provider.</small></label>
      <label class="check"><input name="active" type="checkbox" ${!item || item.active ? 'checked' : ''}> Provider is active</label>`,
  }),
  webhook: item => ({
    title: item ? 'Edit webhook endpoint' : 'Add webhook endpoint',
    subtitle: 'Deliver authenticated call lifecycle events to your CRM',
    fields: `<label class="field">Name<input name="name" maxlength="80" placeholder="Production CRM" required value="${esc(item?.name || '')}"></label>
      <label class="field">Endpoint URL<input name="url" type="url" maxlength="1000" placeholder="https://crm.example.com/api/telephony/events" required value="${esc(item?.url || '')}"></label>
      <label class="field">Signing secret<input name="token" type="password" autocomplete="new-password" placeholder="${item ? 'Leave blank to keep existing' : 'Optional shared secret'}"><small>When set, each delivery is signed with an HMAC-SHA256 header so your service can verify authenticity.</small></label>
      <label class="field">Events<select name="events" multiple size="7" required>${['*','call.started','call.ringing','call.answered','call.completed','call.failed','call.voicemail']
        .map(ev => `<option value="${ev}" ${(item?.events || '*').split(',').includes(ev) ? 'selected' : ''}>${ev === '*' ? 'All call events' : ev}</option>`).join('')}</select>
        <small>Select only the events this integration needs.</small></label>
      <label class="check"><input name="active" type="checkbox" ${!item || item.active ? 'checked' : ''}> Endpoint is active</label>`,
  }),
  apikey: () => ({
    title: 'Create API key',
    subtitle: 'The secret is displayed exactly once, immediately after creation',
    fields: `<label class="field">Integration name<input name="name" required placeholder="Production CRM"></label>
      <label class="field">Scopes<select name="scopes" multiple size="8">
        <option value="calls:read">Read calls</option>
        <option value="calls:write">Create / update calls</option>
        <option value="config:read">Read extensions and numbers</option>
        <option value="recordings:read">Read recordings</option>
        <option value="voicemail:read">Read voicemail</option>
        <option value="voicemail:write">Manage voicemail</option>
        <option value="webhooks:manage">Manage webhooks</option>
        <option value="*">Full access</option>
      </select><small>Use Ctrl/Cmd to select multiple. Prefer only calls:read, calls:write and config:read for a normal CRM.</small></label>`,
  }),
  user: item => ({
    title: item ? 'Edit customer' : 'Add customer',
    subtitle: 'Login details only — numbers, devices and SIP service are assigned separately',
    fields: `<div class="field-row">
        <label class="field">Name<input name="full_name" required value="${esc(item?.full_name || '')}"></label>
        <label class="field">Company<input name="company_name" required value="${esc(item?.company_name || '')}"></label>
      </div>
      <div class="field-row">
        <label class="field">Role<input name="job_role" required value="${esc(item?.job_role || '')}"></label>
        <label class="field">Phone<input name="phone" type="tel" required value="${esc(item?.phone || '')}"></label>
      </div>
      <div class="field-row">
        <label class="field">Username<input name="username" required value="${esc(item?.username || '')}"></label>
        <label class="field">Email<input name="email" type="email" required value="${esc(item?.email || '')}"></label>
      </div>
      <label class="field">Password<input name="password" type="password" autocomplete="new-password" placeholder="${item ? 'Leave blank to keep existing' : 'Minimum 14 characters'}"></label>
      <label class="check"><input name="active" type="checkbox" ${!item || item.active ? 'checked' : ''}> Customer can sign in</label>`,
  }),
  request: () => ({
    title: 'Request a phone number',
    subtitle: 'Tell the administrator what your business needs',
    fields: `<label class="field">Request type<select name="request_type">
        <option value="number">New phone number</option>
        <option value="routing">Routing assistance</option>
        <option value="billing">Billing question</option>
        <option value="access">Access support</option>
      </select></label>
      <label class="field">Requirements<textarea name="details" rows="5" maxlength="2000" required placeholder="Preferred country, area code, local or toll-free number, and how it will be used"></textarea>
        <small>Include the area code or region and any timing requirements.</small></label>`,
  }),
  platformadmin: () => ({
    title: 'Add platform administrator',
    subtitle: 'Privileged account with access to every customer and carrier setting',
    fields: `<div class="field-row">
        <label class="field">Username<input name="username" required></label>
        <label class="field">Email<input name="email" type="email" required></label>
      </div>
      <label class="field">Password<input name="password" type="password" minlength="14" autocomplete="new-password" required></label>`,
  }),
};

function openModal(type, item = null) {
  modalType = type;
  editing = item;
  const template = templates[type](item);
  $('modal-title').textContent = template.title;
  $('modal-subtitle').textContent = template.subtitle;
  $('modal-fields').innerHTML = template.fields;
  $('modal-save').hidden = false;
  $('modal-save').textContent = item ? 'Save changes' : 'Save';
  // Wider canvas for the resource-heavy forms.
  $('modal-card').classList.toggle('wide', ['number', 'extension', 'sipaccount', 'webhook'].includes(type));
  openOverlay('modal');

  if (type === 'call') {
    const extension = $('modal-fields').querySelector('[name=extension]');
    const number = $('modal-fields').querySelector('[name=caller_id_number]');
    const update = () => {
      [...number.options].forEach((option, index) => {
        if (index) option.hidden = state.phone_numbers.find(x => x.number === option.value)?.inbound_extension !== extension.value;
      });
      const preferred = [...number.options].find(o => !o.hidden && state.phone_numbers.find(x => x.number === o.value)?.default_outbound);
      number.value = preferred?.value || '';
    };
    extension?.addEventListener('change', update);
    update();
  }
  if (type === 'number') {
    const owner = $('modal-fields').querySelector('[name=owner_user_id]');
    const extension = $('modal-fields').querySelector('[name=inbound_extension]');
    owner?.addEventListener('change', () => {
      extension.innerHTML = '<option value="">Choose an extension</option>' + state.extensions
        .filter(x => String(x.owner_user_id ?? '') === owner.value && x.active)
        .map(x => `<option value="${x.extension}">${x.extension} — ${esc(x.display_name || 'Unnamed')}</option>`).join('');
    });
  }
  if (type === 'sipaccount') {
    const owner = $('modal-fields').querySelector('[name=owner_user_id]');
    const number = $('modal-fields').querySelector('[name=phone_number]');
    const extension = $('modal-fields').querySelector('[name=extension]');
    const update = () => {
      const id = Number(owner.value);
      number.innerHTML = '<option value="">None</option>' + state.phone_numbers.filter(x => x.owner_user_id === id)
        .map(x => `<option value="${esc(x.number)}" ${item?.phone_number === x.number ? 'selected' : ''}>${esc(x.number)}</option>`).join('');
      extension.innerHTML = '<option value="">None</option>' + state.extensions.filter(x => x.owner_user_id === id)
        .map(x => `<option value="${x.extension}" ${item?.extension === x.extension ? 'selected' : ''}>${x.extension} — ${esc(x.display_name || 'Extension')}</option>`).join('');
    };
    owner?.addEventListener('change', update);
    update();
  }
  setTimeout(() => $('modal-fields').querySelector('input,select')?.focus(), 80);
}

/* One-time secret reveals (API key, device credentials). */
function showSecret({ title, subtitle, body, value, label }) {
  modalType = 'secret';
  $('modal-title').textContent = title;
  $('modal-subtitle').textContent = subtitle;
  $('modal-fields').innerHTML = body;
  $('modal-save').hidden = true;
  $('modal-card').classList.add('wide');
  openOverlay('modal');
  if (value !== undefined) {
    const input = $('created-api-key');
    if (input) input.value = value;
  }
}

/* --------------------------------------------------------- 24. Mutations */
async function saveModal(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  event.target.querySelectorAll('input[type=checkbox]').forEach(x => (data[x.name] = x.checked));
  if (modalType === 'apikey') data.scopes = [...event.target.querySelector('[name=scopes]').selectedOptions].map(x => x.value).join(',');
  if (modalType === 'webhook') data.events = [...event.target.querySelector('[name=events]').selectedOptions].map(x => x.value).join(',');
  if (['webhook', 'user'].includes(modalType) && editing) data.id = editing.id;
  if (modalType === 'sipaccount' && editing) data.id = editing.id;
  try {
    const collection = { number: 'numbers', webhook: 'webhooks', apikey: 'api-keys', sipaccount: 'sip-accounts' }[modalType] || `${modalType}s`;
    const result = await api(`/admin/api/${collection}`, { method: 'POST', body: JSON.stringify(data) });

    if (modalType === 'number' && pendingFulfilRequest) {
      await api(`/admin/api/requests/${pendingFulfilRequest}/resolve`, { method: 'POST', body: JSON.stringify({ status: 'fulfilled', admin_note: `Number ${data.number} assigned` }) });
      pendingFulfilRequest = null;
    }
    if (modalType === 'apikey') {
      const refreshedCustomer = workspace?.customer?.id, refreshedTab = wsTab;
      closeModal();
      await loadState();
      if (refreshedCustomer) await openCustomer(refreshedCustomer, refreshedTab, true);
      showSecret({
        title: 'API key created',
        subtitle: 'Copy and store this credential now — for security it cannot be shown again.',
        value: result.token,
        body: `<div class="notice warn"><span class="glyph">⚿</span><div><b>Store this key safely</b>Use it as a Bearer token from your own server. Never embed it in browser code, mobile apps or public repositories.</div></div>
          <label class="field">Secret key<div class="secret"><input id="created-api-key" readonly><button class="btn primary" type="button" data-copy-secret>Copy</button></div></label>`,
      });
      notify('API key created');
      return;
    }
    const messages = {
      call: 'Outbound call started', extension: 'Extension saved', number: 'Phone number saved',
      sipaccount: 'SIP service saved', provider: 'Provider saved', webhook: 'Webhook saved',
      user: 'Customer saved', platformadmin: 'Administrator added',
    };
    notify(messages[modalType] || 'Saved');
    const keepCustomer = workspace?.customer?.id, keepTab = wsTab;
    closeModal();
    await loadState();
    if (keepCustomer) await openCustomer(keepCustomer, keepTab, true);
    if (modalType === 'call') loadCalls();
  } catch (error) { notify(error.message, true); }
}

async function remove(type, id, label) {
  if (!confirm(`Delete ${label}? This cannot be undone.`)) return;
  try {
    const path = type === 'number' ? `/admin/api/numbers/${encodeURIComponent(label)}` : `/admin/api/${type}s/${id}`;
    await api(path, { method: 'DELETE' });
    notify(`${type[0].toUpperCase() + type.slice(1)} deleted`);
    const keepCustomer = workspace?.customer?.id, keepTab = wsTab;
    await loadState();
    if (keepCustomer) await openCustomer(keepCustomer, keepTab, true);
  } catch (error) { notify(error.message, true); }
}

/* Device credentials — the only place a customer sees their SIP password. */
async function showCredentials(id) {
  try {
    const data = await api(`/admin/api/sip-accounts/${id}/credentials`);
    const x = data.sip_account;
    const rows = [
      ['Number', x.phone_number], ['SIP username', x.sip_username], ['Password', x.sip_password],
      ['Server', x.server], ['Port', x.port], ['Transport', String(x.transport).toUpperCase()],
      ['Extension', x.extension],
    ];
    showSecret({
      title: 'Device credentials',
      subtitle: 'Enter these into your phone or softphone. Share them only with the person using this device.',
      body: `<div class="notice warn"><span class="glyph">⚿</span><div><b>Treat these as secrets</b>Anyone with these credentials can place calls as this device. Rotate them from the console if they are ever exposed.</div></div>
        <div class="grid cols-2">${rows.map(([label, value]) => `
          <label class="field">${esc(label)}
            <div style="display:flex;gap:8px">
              <input ${label === 'Password' ? 'type="password" data-secret' : ''} readonly value="${esc(value || '—')}">
              ${label === 'Password' ? '<button type="button" class="btn ghost" data-toggle-secret>Show</button>' : ''}
              <button type="button" class="btn ghost" data-copy-value="${esc(value || '')}">Copy</button>
            </div>
          </label>`).join('')}</div>`,
    });
  } catch (error) { notify(error.message, true); }
}

/* ================================================== 25. CUSTOMER WORKSPACE
   The administrator's primary control centre. Every tab reads from the
   existing endpoints — no new backend surface is introduced. */
const WS_TABS = [
  ['overview', '▦', 'Overview'],
  ['numbers', '☎', 'Numbers'],
  ['devices', '◈', 'Devices & SIP'],
  ['routing', '⌘', 'Routing'],
  ['calls', '◷', 'Calls'],
  ['recordings', '◉', 'Recordings'],
  ['requests', '↗', 'Requests'],
  ['billing', '▣', 'Billing'],
  ['integrations', '◇', 'Integrations'],
  ['activity', '◌', 'Activity'],
];

const wsOwned = key => (state[key] || []).filter(row => row.owner_user_id === workspace?.customer?.id);
const wsCalls = () => workspace?.calls || [];
const wsRecordings = () => wsCalls().filter(c => c.recording_name && c.recording_status !== 'deleted');

async function openCustomer(customerId, tab = 'overview', silent = false) {
  if (!state.is_admin) return;
  try {
    if (!silent) {
      $('ws-body').innerHTML = skeletonPanel(4);
      $('ws-status').innerHTML = '<div class="skeleton line w-60" style="margin:0"></div>';
      $('ws-quick').innerHTML = '';
      $('ws-recent').innerHTML = '';
    }
    workspace = await api(`/admin/api/customers/${customerId}`);
    renderWsHeader();
    renderWsTabs();
    wsTab = WS_TABS.some(t => t[0] === tab) ? tab : 'overview';
    renderWsTab(wsTab);
    openOverlay('workspace');
    $('scrim').classList.add('open');
    setTimeout(() => $('ws-close').focus(), 90);
  } catch (error) { notify(error.message, true); }
}

/* The workspace header is a compact command centre: identity, account state,
   live device count, billing position and the customer's latest activity, with
   shortcuts into the flows an administrator reaches for most often. */
function renderWsHeader() {
  if (!workspace) return;
  const c = workspace.customer;
  const today = new Date().toISOString().slice(0, 10);
  const openInvoices = workspace.invoices.filter(i => i.status === 'open');
  const overdue = openInvoices.filter(i => i.due_at && i.due_at < today);
  const pendingRequests = workspace.requests.filter(r => r.status === 'pending').length;
  const devices = workspace.sip_accounts || [];
  const online = devices.filter(d => d.registration_status === 'online').length;

  const avatar = $('ws-avatar');
  avatar.textContent = (c.company_name || c.full_name || c.username || 'C').trim()[0].toUpperCase();
  avatar.classList.toggle('muted', !c.active);
  avatar.classList.add('pop');
  $('ws-title').textContent = c.company_name || c.username;
  $('ws-meta').textContent = `${c.full_name || c.username} · ${c.email}`;
  $('ws-flags').innerHTML = [
    c.active ? tag('Active', 'on') : tag('Access disabled', 'off'),
    c.username ? tag(c.username, '', '@') : '',
    c.job_role ? tag(c.job_role, 'violet') : '',
    overdue.length ? tag(`${overdue.length} payment due`, 'warn', '▣') : tag('Paid up', 'on', '▣'),
  ].join('');

  const deviceTone = !devices.length ? '' : online === devices.length ? 'ok' : online ? 'warn' : 'bad';
  const paymentTone = overdue.length ? 'bad' : openInvoices.length ? 'warn' : 'ok';
  $('ws-status').innerHTML = [
    ['user', c.active ? 'Active account' : 'Access disabled', c.active ? 'ok' : 'bad',
      `Member since ${fmtDay(c.created_at)}`],
    ['devices', devices.length ? `${online} of ${devices.length} registered` : 'No devices yet',
      deviceTone, devices.length ? 'Live SIP registration' : 'Add a device to go live'],
    ['payments', overdue.length ? `${overdue.length} invoice overdue`
      : openInvoices.length ? `${openInvoices.length} invoice open` : 'Paid up', paymentTone,
      `${workspace.invoices.length} invoice${workspace.invoices.length === 1 ? '' : 's'} on file`],
    ['requests', pendingRequests ? `${pendingRequests} awaiting decision` : 'Nothing pending',
      pendingRequests ? 'warn' : 'ok', `${workspace.requests.length} request${workspace.requests.length === 1 ? '' : 's'} raised`],
  ].map(([key, value, tone, hint]) => `<div class="ws-chip ${tone}" data-chip="${key}">
      <i>${WS_CHIP_GLYPHS[key]}</i><p><small>${esc(hint)}</small><b>${esc(value)}</b></p></div>`).join('');

  // Quick actions reuse the existing modals and pages — nothing new is created.
  $('ws-quick').innerHTML = [
    ['extension', 'Add extension', '⌁', 'data-open="extension"'],
    ['sipaccount', 'Add device', '◈', 'data-open="sipaccount"'],
    ['number', 'Assign number', '☎', 'data-open="number"'],
    ['routing', 'Open call flow', '⌘', `data-ws-goto="routing"`],
    ['customer', 'Edit customer', '✎', 'data-ws-edit-customer="1"'],
  ].map(([, label, glyph, attr]) => `<button type="button" ${attr}><span class="glyph">${glyph}</span>${label}</button>`).join('');

  const recent = (workspace.activity || []).slice(0, 3);
  $('ws-recent').innerHTML = recent.length
    ? recent.map(x => `<div class="tl-item"><b>${esc(x.description)}</b>
        <p>${esc(String(x.action).replaceAll('.', ' '))} · ${esc(x.resource_type)}</p>
        <small>${esc(fmtDate(x.created_at))}</small></div>`).join('')
    : '<div class="tl-item"><b>No recorded activity yet</b><p>Provisioning and access changes will appear here.</p></div>';

  $('ws-metrics').innerHTML = [
    ['☎', 'Numbers', workspace.numbers.length, 'ws-metric-numbers'],
    ['◈', 'Devices', devices.length, 'ws-metric-devices'],
    ['⌁', 'Extensions', workspace.extensions.length, 'ws-metric-extensions'],
    ['↗', 'Pending requests', pendingRequests, 'ws-metric-requests'],
  ].map(([glyph, label, value, id]) => `
    <div class="ws-metric"><span class="glyph">${glyph}</span><p><small>${label}</small><b id="${id}">0</b></p></div>`).join('');
  [['ws-metric-numbers', workspace.numbers.length], ['ws-metric-devices', devices.length],
    ['ws-metric-extensions', workspace.extensions.length], ['ws-metric-requests', pendingRequests]]
    .forEach(([id, value]) => countTo($(id), value));
}

const WS_CHIP_GLYPHS = { user: '◍', devices: '◈', payments: '▣', requests: '↗' };
let wsTabCounts = {};

/* Live registration polls update the device chip in place, so neither header
   re-animates every eight seconds. */
function syncDeviceChip(rootSelector, accounts) {
  const chip = document.querySelector(`${rootSelector} [data-chip="devices"]`);
  if (!chip) return;
  const devices = accounts || [];
  const online = devices.filter(d => d.registration_status === 'online').length;
  const next = devices.length ? `${online} of ${devices.length} registered` : 'No devices yet';
  const tone = !devices.length ? '' : online === devices.length ? 'ok' : online ? 'warn' : 'bad';
  const label = chip.querySelector('b');
  if (!label || label.textContent === next) return;
  label.textContent = next;
  chip.classList.remove('ok', 'warn', 'bad');
  if (tone) chip.classList.add(tone);
  label.classList.remove('bump');
  void label.offsetWidth;
  label.classList.add('bump');
}
function updateWsDeviceChip() {
  if (workspace) syncDeviceChip('#ws-status', workspace.sip_accounts);
}

/* Customer dashboard summary: the same chips as the administrator's workspace,
   limited to what a customer owns and can act on. */
function renderCustomerStatus() {
  const host = $('customer-status');
  if (!host) return;
  const devices = state.sip_accounts || [];
  const online = devices.filter(d => d.registration_status === 'online').length;
  const numbers = state.phone_numbers || [];
  const invoices = (state.invoices || []).filter(i => i.status === 'open');
  const overdue = invoices.filter(i => i.due_at && i.due_at < new Date().toISOString().slice(0, 10));
  const deviceTone = !devices.length ? '' : online === devices.length ? 'ok' : online ? 'warn' : 'bad';
  host.innerHTML = [
    ['user', 'Account', `${state.username} · active`, 'ok', `${state.extensions?.length || 0} extension${state.extensions?.length === 1 ? '' : 's'} ready`],
    ['numbers', 'Phone numbers', numbers.length ? `${numbers.length} assigned` : 'None yet',
      numbers.length ? 'ok' : 'warn', numbers.length ? 'Receiving calls' : 'Request a number to begin'],
    ['devices', 'Devices', devices.length ? `${online} of ${devices.length} registered` : 'No devices yet',
      deviceTone, devices.length ? 'Live SIP registration' : 'Add the phone or softphone you use'],
    ['payments', 'Billing', overdue.length ? `${overdue.length} overdue`
      : invoices.length ? `${invoices.length} open` : 'Paid up',
      overdue.length ? 'bad' : invoices.length ? 'warn' : 'ok',
      `${(state.invoices || []).length} invoice${(state.invoices || []).length === 1 ? '' : 's'} on file`],
  ].map(([key, label, value, tone, hint]) => `<div class="ws-chip ${tone}" data-chip="${key}">
      <i>${WS_CHIP_GLYPHS[key] || '•'}</i><p><small>${esc(hint)}</small><b>${esc(value)}</b></p></div>`).join('');
}

function renderWsTabs() {
  const pending = workspace.requests.filter(r => r.status === 'pending').length;
  const counts = { numbers: workspace.numbers.length, devices: workspace.sip_accounts.length, requests: pending, integrations: wsOwned('webhooks').length + wsOwned('api_keys').length };
  const pulse = key => (counts[key] !== wsTabCounts[key] ? ' pulse' : '');
  $('ws-tabs').innerHTML = WS_TABS.map(([key, glyph, label]) => `
    <button class="ws-tab ${key === wsTab ? 'active' : ''}" data-ws-tab="${key}">
      <span class="glyph">${glyph}</span>${esc(label)}
      ${counts[key] ? `<span class="dot${pulse(key)}">${counts[key]}</span>` : ''}
    </button>`).join('') + '<span class="ws-tab-ink" id="ws-tab-ink" aria-hidden="true"></span>';
  wsTabCounts = { ...counts };
  moveTabInk();
}

/* One indicator glides between tabs instead of an underline per tab. */
function moveTabInk() {
  const ink = $('ws-tab-ink');
  const active = document.querySelector('#ws-tabs .ws-tab.active');
  if (!ink || !active) return;
  ink.style.setProperty('--ink-x', `${active.offsetLeft}px`);
  ink.style.setProperty('--ink-w', `${active.offsetWidth}px`);
}

function wsEmpty(title, text, glyph) { return `<div class="ws-empty">${empty(title, text, glyph)}</div>`; }

function renderWsTab(tab) {
  if (!workspace) return;
  wsTab = tab;
  document.querySelectorAll('[data-ws-tab]').forEach(b => b.classList.toggle('active', b.dataset.wsTab === tab));
  const body = $('ws-body');
  body.innerHTML = ({
    overview: wsOverview(), numbers: wsNumbers(), devices: wsDevices(), routing: wsRouting(),
    calls: wsCallsTab(), recordings: wsRecordingsTab(), requests: wsRequestsTab(),
    billing: wsBilling(), integrations: wsIntegrations(), activity: wsActivityTab(),
  }[tab] || wsOverview());
  body.scrollTop = 0;
  body.classList.remove('swapping');
  void body.offsetWidth;
  body.classList.add('swapping');
  moveTabInk();
  markStagger();
}

/* --- Overview: everything that needs attention, at a glance. --- */
function wsOverview() {
  const c = workspace.customer;
  const openInvoices = workspace.invoices.filter(i => i.status === 'open');
  const overdue = openInvoices.filter(i => i.due_at && i.due_at < new Date().toISOString().slice(0, 10));
  const answered = wsCalls().filter(x => x.answered).length;
  const routes = wsOwned('call_routes');
  const unlinked = workspace.numbers.filter(n => !workspace.sip_accounts.some(s => s.phone_number === n.number));
  return `
  <section class="ws-section">
    <div class="ws-section-head"><div><h3>Account profile</h3><p>Login identity and primary business contact.</p></div>
      <button class="btn ghost sm" data-edit-user="${c.id}">Edit customer</button></div>
    <div class="panel" style="background:transparent;border:0;box-shadow:none">
      <div class="kv kv-2">
        ${kv('Full name', c.full_name)}${kv('Company', c.company_name)}
        ${kv('Business role', c.job_role)}${kv('Phone', c.phone)}
        ${kv('Username', c.username)}${kv('Email', c.email)}
      </div>
    </div>
  </section>

  <section class="ws-section">
    <div class="ws-section-head"><div><h3>Needs attention</h3><p>Resource gaps and billing items for this customer.</p></div></div>
    <div class="ws-cards">
      ${overdue.length ? `<article class="ws-card"><div class="ws-card-head"><span class="ws-glyph" style="background:linear-gradient(140deg,#f59e0b,#fbbf24)">!</span>
        <div><small>Billing</small><h4>${overdue.length} invoice${overdue.length === 1 ? '' : 's'} past due</h4></div>
        <button class="btn ghost sm" data-ws-tab="billing">Review</button></div></article>` : ''}
      ${workspace.requests.filter(r => r.status === 'pending').length ? `<article class="ws-card"><div class="ws-card-head"><span class="ws-glyph" style="background:linear-gradient(140deg,#f43f5e,#fb7185)">↗</span>
        <div><small>Requests</small><h4>${workspace.requests.filter(r => r.status === 'pending').length} request${workspace.requests.filter(r => r.status === 'pending').length === 1 ? '' : 's'} awaiting a decision</h4></div>
        <button class="btn primary sm" data-ws-tab="requests">Review</button></div></article>` : ''}
      ${unlinked.length ? `<article class="ws-card"><div class="ws-card-head"><span class="ws-glyph" style="background:linear-gradient(140deg,#38bdf8,#7dd3fc)">☎</span>
        <div><small>Provisioning</small><h4>${unlinked.length} number${unlinked.length === 1 ? '' : 's'} without a device</h4></div>
        <button class="btn ghost sm" data-ws-tab="numbers">View numbers</button></div></article>` : ''}
      ${!routes.length ? `<article class="ws-card"><div class="ws-card-head"><span class="ws-glyph">⌘</span>
        <div><small>Routing</small><h4>No call flow configured yet</h4></div>
        <button class="btn ghost sm" data-ws-tab="routing">Set up routing</button></div></article>` : ''}
      ${(!overdue.length && !workspace.requests.filter(r => r.status === 'pending').length && !unlinked.length && routes.length)
        ? `<article class="ws-card"><div class="ws-card-head"><span class="ws-glyph" style="background:linear-gradient(140deg,#22c55e,#4ade80)">✓</span>
          <div><small>Status</small><h4>Everything provisioned and up to date</h4></div></div></article>` : ''}
    </div>
  </section>

  <section class="ws-section">
    <div class="ws-section-head"><div><h3>Resource summary</h3><p>What this customer owns right now.</p></div></div>
    <div class="ws-cards">
      <article class="ws-card"><div class="ws-card-head"><span class="ws-glyph">☎</span><div><small>Numbers</small><h4>${workspace.numbers.length} assigned</h4>
        <p>${workspace.numbers.filter(n => n.active).length} active · ${workspace.numbers.filter(n => n.default_outbound).length} default caller ID</p></div>
        <button class="btn ghost sm" data-ws-tab="numbers">Manage</button></div></article>
      <article class="ws-card"><div class="ws-card-head"><span class="ws-glyph">◈</span><div><small>Devices</small><h4>${workspace.sip_accounts.length} SIP account${workspace.sip_accounts.length === 1 ? '' : 's'}</h4>
        <p>${workspace.sip_accounts.filter(s => s.registration_status === 'online').length} registered now</p></div>
        <button class="btn ghost sm" data-ws-tab="devices">Manage</button></div></article>
      <article class="ws-card"><div class="ws-card-head"><span class="ws-glyph">◷</span><div><small>Call activity</small><h4>${workspace.call_total} call${workspace.call_total === 1 ? '' : 's'}</h4>
        <p>${answered} answered in the most recent ${wsCalls().length}</p></div>
        <button class="btn ghost sm" data-ws-tab="calls">View calls</button></div></article>
      <article class="ws-card"><div class="ws-card-head"><span class="ws-glyph">▣</span><div><small>Billing</small><h4>${openInvoices.length} open invoice${openInvoices.length === 1 ? '' : 's'}</h4>
        <p>${money(workspace.numbers.reduce((sum, n) => sum + (n.monthly_price_cents ?? 500), 0))} monthly recurring</p></div>
        <button class="btn ghost sm" data-ws-tab="billing">Review</button></div></article>
    </div>
  </section>`;
}

/* --- Numbers --- */
function wsNumbers() {
  const c = workspace.customer;
  const cards = workspace.numbers.map(x => {
    const sip = workspace.sip_accounts.find(a => a.phone_number === x.number);
    const route = wsOwned('call_routes').find(r => r.phone_number === x.number);
    return `<article class="ws-card">
      <div class="ws-card-head">
        <span class="ws-glyph">☎</span>
        <div><small>Phone number</small><h4>${esc(x.number)}</h4>
          <div class="tags" style="margin-top:6px">
            ${x.active ? tag('Active', 'on') : tag('Disabled', 'off')}
            ${x.default_outbound ? tag('Default caller ID', 'violet') : ''}
            ${x.discontinue_at ? tag(`Ends ${fmtDay(x.discontinue_at)}`, 'warn') : ''}
          </div>
        </div>
      </div>
      <div class="kv kv-2">
        ${kv('Description', x.description || 'Customer number')}
        ${kv('Inbound extension', x.inbound_extension || 'Not assigned')}
        ${kv('Device', sip?.label || 'No device linked')}
        ${kv('Call flow', route ? 'Configured' : 'Not configured')}
        ${kv('Monthly rate', money(x.monthly_price_cents ?? 500))}
        ${kv('Billing cycle', `Day ${x.billing_cycle_day || 1}`)}
      </div>
      <div class="ws-card-actions">
        <button class="btn ghost sm" data-edit-number="${x.id}">Manage number</button>
        ${route ? '' : `<button class="btn ghost sm" data-ws-tab="routing">Configure routing</button>`}
      </div>
    </article>`;
  }).join('');
  return `<section class="ws-section">
    <div class="ws-section-head"><div><h3>Assigned phone numbers</h3><p>Numbers are provisioned here. The customer controls routing and outbound caller ID.</p></div>
      <button class="btn primary" data-new-for-customer="number:${c.id}">＋ Assign number</button></div>
    <div class="ws-cards">${cards || wsEmpty('No numbers assigned', 'Assign the first phone number to begin provisioning.', '☎')}</div>
  </section>`;
}

/* --- Devices & SIP --- */
function wsDevices() {
  const c = workspace.customer;
  const cards = workspace.sip_accounts.map(x => `
    <article class="ws-card">
      <div class="ws-card-head">
        <span class="ws-glyph">◈</span>
        <div><small>Device</small><h4>${esc(x.label)}</h4>
          <p>${esc(x.sip_username)} @ ${esc(x.server)}:${esc(x.port)}</p></div>
        ${registration(x)}
      </div>
      <div class="kv kv-2">
        ${kv('Assigned number', x.phone_number || 'Not linked')}
        ${kv('Extension', x.extension || 'Not linked')}
        ${kv('Transport', String(x.transport).toUpperCase())}
        ${kv('Credential', x.has_password ? 'Stored securely' : 'Password required')}
      </div>
      <div class="ws-card-actions">
        <button class="btn primary sm" data-show-credentials="${x.id}">Credentials</button>
        <button class="btn ghost sm" data-edit-sip="${x.id}">Edit service</button>
      </div>
    </article>`).join('');
  return `<section class="ws-section">
    <div class="ws-section-head"><div><h3>Devices &amp; SIP accounts</h3><p>Credentials for the phones and softphones this customer connects. The customer can view these too.</p></div>
      <button class="btn primary" data-new-for-customer="sipaccount:${c.id}">＋ Assign SIP service</button></div>
    <div class="ws-cards">${cards || wsEmpty('No SIP service assigned', 'Assign device credentials after provisioning a number.', '◈')}</div>

    <div class="ws-section-head" style="margin-top:26px"><div><h3>Extensions</h3><p>Internal destinations owned by this customer.</p></div>
      <button class="btn ghost" data-new-for-customer="extension:${c.id}">＋ Add extension</button></div>
    <div class="ws-cards">${workspace.extensions.map(x => `
      <article class="ws-card"><div class="ws-card-head">
        <span class="ws-glyph">⌁</span>
        <div><small>Extension ${esc(x.extension)}</small><h4>${esc(x.display_name || 'Unnamed')}</h4>
          <div class="tags" style="margin-top:6px">
            ${x.active ? tag('Active', 'on') : tag('Disabled', 'off')}
            ${x.recording_enabled ? tag('Recording on') : tag('Recording off')}
            ${x.voicemail_enabled ? tag('Voicemail on', 'info') : tag('Voicemail off')}
          </div></div>
        <button class="btn ghost sm" data-edit-extension="${esc(x.extension)}">Edit</button>
      </div></article>`).join('') || wsEmpty('No extensions', 'Add an extension to give this customer an internal destination.', '⌁')}</div>
  </section>`;
}

/* --- Routing --- */
function wsRouting() {
  const routes = wsOwned('call_routes');
  const cards = routes.map(route => {
    const nodes = route.route?.nodes || [];
    return `<article class="ws-card">
      <div class="ws-card-head"><span class="ws-glyph">⌘</span>
        <div><small>Call flow</small><h4>${esc(route.phone_number)}</h4><p>${esc(route.name || 'Main call flow')}</p>
          <div class="tags" style="margin-top:6px">${route.active ? tag('Active', 'on') : tag('Paused', 'off')}${tag(`${nodes.length} step${nodes.length === 1 ? '' : 's'}`)}</div>
        </div>
      </div>
      <div class="ws-flow">${nodes.length ? nodes.map((node, i) => `
        <div class="ws-flow-node">
          <span class="tile ${FLOW_TILES[node.type] || 'tile-ext'}" style="width:32px;height:32px;border-radius:10px;display:grid;place-items:center;color:#fff">${FLOW_ICONS[node.type] || '◇'}</span>
          <div style="flex:1;min-width:0"><b style="font-size:12.5px;text-transform:capitalize">${esc(String(node.type).replaceAll('_', ' '))}</b>
            <small style="display:block;color:var(--text-3);font-size:11px">${esc(node.label || 'Not configured')}</small></div>
          <span class="step">${String(i + 1).padStart(2, '0')}</span>
        </div>`).join('') : '<small style="color:var(--text-3)">No steps configured for this number yet.</small>'}</div>
      <div class="ws-card-actions"><button class="btn ghost sm" data-ws-route="${esc(route.phone_number)}">Open in flow builder</button></div>
    </article>`;
  }).join('');
  const unconfigured = workspace.numbers.filter(n => !routes.some(r => r.phone_number === n.number));
  return `<section class="ws-section">
    <div class="ws-section-head"><div><h3>Call routing</h3><p>How inbound calls to this customer's numbers are handled.</p></div>
      <button class="btn primary" data-ws-route="${esc(workspace.numbers[0]?.number || '')}">Open flow builder</button></div>
    <div class="ws-cards">${cards || wsEmpty('No call flow configured', 'Open the flow builder to design how inbound calls are routed.', '⌘')}</div>
    ${unconfigured.length ? `<div class="notice" style="margin-top:16px"><span class="glyph">⌘</span><div><b>Numbers without a call flow</b>${unconfigured.map(n => esc(n.number)).join(', ')}</div></div>` : ''}
  </section>`;
}

/* --- Calls / Recordings --- */
function wsCallsTab() {
  return `<section class="ws-section">
    <div class="ws-section-head"><div><h3>Recent calls</h3><p>The most recent ${wsCalls().length} of ${workspace.call_total} calls for this customer.</p></div>
      <button class="btn ghost sm" data-ws-goto="calls">Open full call history</button></div>
    <div class="panel" style="overflow:hidden"><div class="table-wrap">${callTable(wsCalls())}</div></div>
  </section>`;
}

function wsRecordingsTab() {
  const rows = wsRecordings();
  return `<section class="ws-section">
    <div class="ws-section-head"><div><h3>Recordings</h3><p>Secure playback for this customer's recorded calls. Only finalised audio can be played.</p></div>
      <button class="btn ghost sm" data-ws-goto="recordings">Open recording library</button></div>
    <div class="ws-cards">${rows.map(x => `
      <article class="ws-card">
        <div class="ws-card-head"><span class="ws-glyph">◉</span>
          <div><small>Extension ${esc(x.extension)}</small><h4>${esc(x.phone)}</h4>
            <p>${esc(fmtDate(x.started_at))} · ${fmtDuration(x.duration_seconds)}</p></div>
          ${statusPill(x.recording_status)}
        </div>
        ${x.recording_status === 'finalized' ? `<div style="margin-top:13px;display:flex;align-items:center;gap:11px;flex-wrap:wrap">
          ${waveform()}<audio controls preload="none" src="/admin/api/recordings/${encodeURIComponent(x.call_id)}/file"></audio>
          <a class="btn ghost sm" download href="/admin/api/recordings/${encodeURIComponent(x.call_id)}/file">Download</a></div>`
        : '<p style="margin-top:11px;color:var(--text-3);font-size:11.5px">Audio becomes available after finalisation.</p>'}
      </article>`).join('') || wsEmpty('No recordings', 'Recording is off by default; enable it globally and per extension.', '◉')}</div>
  </section>`;
}

/* --- Requests --- */
function wsRequestsTab() {
  const rows = workspace.requests;
  return `<section class="ws-section">
    <div class="ws-section-head"><div><h3>Customer requests</h3><p>Approve, fulfil or reject provisioning and access requests raised by this customer.</p></div></div>
    <div class="ws-cards">${rows.map(x => `
      <article class="ws-card">
        <div class="ws-card-head"><span class="ws-glyph" style="background:linear-gradient(140deg,#f43f5e,#fb7185)">↗</span>
          <div><small>${esc(String(x.request_type).replaceAll('_', ' '))}</small><h4>${esc(x.details)}</h4>
            <p>Raised ${esc(fmtDate(x.created_at))}${x.admin_note ? ` · ${esc(x.admin_note)}` : ''}</p></div>
          ${statusPill(x.status)}
        </div>
        ${x.status === 'pending' ? `<div class="ws-card-actions">
          <button class="btn primary sm" data-resolve-request="${x.id}:approved">Approve</button>
          <button class="btn ghost sm" data-assign-request="${x.id}:${x.user_id}">Assign a number</button>
          <button class="btn danger sm" data-resolve-request="${x.id}:rejected">Reject</button>
        </div>` : ''}
      </article>`).join('') || wsEmpty('No requests', 'This customer has not raised any requests.', '↗')}</div>
  </section>`;
}

/* --- Billing --- */
function wsBilling() {
  const invoices = workspace.invoices;
  const open = invoices.filter(i => i.status === 'open');
  const recurring = workspace.numbers.reduce((sum, n) => sum + (n.monthly_price_cents ?? 500), 0);
  return `<section class="ws-section">
    <div class="ws-section-head"><div><h3>Billing &amp; payments</h3><p>Payments are collected externally. Record the outcome here to keep status accurate.</p></div></div>
    <div class="ws-cards">
      <article class="ws-card"><div class="ws-card-head"><span class="ws-glyph">▣</span>
        <div><small>Recurring monthly</small><h4>${money(recurring)}</h4>
          <p>${workspace.numbers.length} number${workspace.numbers.length === 1 ? '' : 's'} at $5/month</p></div></div></article>
      <article class="ws-card"><div class="ws-card-head"><span class="ws-glyph" style="background:linear-gradient(140deg,${open.length ? '#f59e0b,#fbbf24' : '#22c55e,#4ade80'})">${open.length ? '!' : '✓'}</span>
        <div><small>Payment status</small><h4>${open.length ? `${open.length} invoice${open.length === 1 ? '' : 's'} open` : 'All invoices settled'}</h4>
          <p>${money(open.reduce((sum, i) => sum + i.amount_cents, 0))} outstanding</p></div></div></article>
    </div>
    <div class="panel" style="margin-top:16px;overflow:hidden">
      <div class="panel-head"><div><h2>Invoices</h2><p>Full billing history for this customer</p></div></div>
      <div class="table-wrap">${invoices.length ? `
        <table class="data"><thead><tr><th>Invoice</th><th>Number</th><th>Period</th><th>Amount</th><th>Status</th><th>Due</th><th>Action</th></tr></thead>
        <tbody>${invoices.map(x => `<tr>
          <td class="cell-strong">#${esc(x.id)}</td><td>${esc(x.number)}</td>
          <td>${esc(x.period_start)} → ${esc(x.period_end)}</td><td>${money(x.amount_cents)}</td>
          <td>${statusPill(x.status, `invoice-${x.id}`)}</td><td>${esc(fmtDay(x.due_at))}</td>
          <td>${x.status === 'open' ? `<button class="btn ghost sm" data-paid-invoice="${x.id}">Mark paid</button>` : '—'}</td>
        </tr>`).join('')}</tbody></table>` : empty('No invoices', 'Invoices are generated for each assigned number.', '▣')}</div>
    </div>
  </section>`;
}

/* --- Integrations (API keys + webhooks) --- */
function wsIntegrations() {
  const keys = wsOwned('api_keys');
  const hooks = wsOwned('webhooks');
  return `<section class="ws-section">
    <div class="notice secure"><span class="glyph">◇</span><div><b>Customer-owned integrations</b>These keys and endpoints belong to this customer. Signing secrets and key material are never displayed here after creation.</div></div>

    <div class="ws-section-head"><div><h3>API keys</h3><p>Scoped credentials this customer's software uses to call the EIP API.</p></div></div>
    <div class="ws-cards">${keys.map(x => `
      <article class="ws-card"><div class="ws-card-head"><span class="ws-glyph">⌘</span>
        <div><small>API key</small><h4>${esc(x.name)}</h4><p><code>${esc(x.prefix)}…</code> · created ${esc(fmtDay(x.created_at))}</p></div>
        <span class="tag on">Active</span>
      </div>
      <div class="kv">${kv('Scopes', x.scopes === '*' ? 'Full access' : x.scopes)}${kv('Last used', x.last_used_at ? fmtDate(x.last_used_at) : 'Never')}</div>
      <div class="ws-card-actions"><button class="btn danger sm" data-revoke-key="${x.id}">Revoke key</button></div></article>`).join('')
      || wsEmpty('No API keys', 'This customer has not created any integration keys.', '⌘')}</div>

    <div class="ws-section-head" style="margin-top:26px"><div><h3>Webhook endpoints</h3><p>Call lifecycle events pushed to this customer's systems.</p></div></div>
    <div class="ws-cards">${hooks.map(x => `
      <article class="ws-card"><div class="ws-card-head"><span class="ws-glyph">◇</span>
        <div><small>Webhook</small><h4>${esc(x.name)}</h4><p style="word-break:break-all">${esc(x.url)}</p></div>
        ${x.active ? tag('Active', 'on') : tag('Paused', 'off')}
      </div>
      <div class="kv">${kv('Subscribed events', x.events === '*' ? 'All call events' : x.events)}
        ${kv('Signing secret', x.has_token ? 'Configured (hidden)' : 'Not set')}</div>
      <div class="ws-card-actions">
        <button class="btn ghost sm" data-test-webhook="${x.id}">Send test</button>
        <button class="btn ghost sm" data-edit-webhook="${x.id}">Edit</button>
      </div></article>`).join('') || wsEmpty('No webhook endpoints', 'This customer has not subscribed to any events.', '◇')}</div>
  </section>`;
}

function wsActivityTab() {
  return `<section class="ws-section">
    <div class="ws-section-head"><div><h3>Customer activity</h3><p>Provisioning, access and integration changes for this customer.</p></div></div>
    <div class="panel" style="background:transparent;border:0;box-shadow:none"><div class="timeline" style="padding:0">
      ${workspace.activity.map(x => `<div class="tl-item"><b>${esc(x.description)}</b>
        <p>${esc(String(x.action).replaceAll('.', ' '))} · ${esc(x.resource_type)}</p>
        <small>${esc(fmtDate(x.created_at))}</small></div>`).join('') || empty('No activity yet', 'Customer and administrator changes will be recorded here.', '◌')}
    </div></div>
  </section>`;
}

function closeWorkspace() {
  closeOverlay('workspace');
  $('scrim').classList.remove('open');
  workspace = null;
}

/* ============================================================ 26. Events */
document.addEventListener('click', async event => {
  // Grouped lists (extensions by customer, recordings/voicemails by extension)
  // collapse from their header, so long lists stay scannable.
  const head = event.target.closest('.acc-head');
  if (head) { head.closest('.acc-item')?.classList.toggle('open'); return; }

  const button = event.target.closest('button');
  if (!button || button.disabled) return;
  const d = button.dataset;
  const keepWorkspace = () => workspace?.customer?.id;

  if (d.retry) return RETRY[d.retry]?.();
  if (d.openCustomer) return openCustomer(Number(d.openCustomer));
  if (d.wsTab) return renderWsTab(d.wsTab);
  if (d.wsGoto) { closeWorkspace(); showPage(d.wsGoto); return; }
  if (d.wsRoute) {
    const number = d.wsRoute;
    closeWorkspace();
    showPage('routing');
    if (number) {
      $('route-number').value = number;
      $('flow-entry-number').textContent = number;
      flowNodes = (state.call_routes || []).find(x => x.phone_number === number)?.route?.nodes || [];
      renderFlowNodes();
    }
    return;
  }
  if (d.showCredentials) return showCredentials(Number(d.showCredentials));
  if (d.editSip) return openModal('sipaccount', state.sip_accounts.find(x => x.id === Number(d.editSip)));

  if (d.toggleSecret !== undefined) {
    const input = button.parentElement.querySelector('[data-secret]');
    if (input) { input.type = input.type === 'password' ? 'text' : 'password'; button.textContent = input.type === 'password' ? 'Show' : 'Hide'; }
    return;
  }
  if (d.copyValue !== undefined) { navigator.clipboard?.writeText(d.copyValue); notify('Copied securely'); return; }
  if (d.copySecret) { navigator.clipboard?.writeText($('created-api-key').value); notify('API key copied'); return; }

  if (d.removeNode !== undefined) { flowNodes.splice(Number(d.removeNode), 1); renderFlowNodes(); return; }
  if (d.readNotification) {
    try { await api(`/admin/api/notifications/${d.readNotification}/read`, { method: 'POST' }); await loadState(); }
    catch (error) { notify(error.message, true); }
    return;
  }
  if (d.resolveRequest) {
    const [id, status] = d.resolveRequest.split(':');
    try {
      await api(`/admin/api/requests/${id}/resolve`, { method: 'POST', body: JSON.stringify({ status, admin_note: `Request ${status} by administrator` }) });
      notify(`Request ${status}`);
      const id2 = keepWorkspace(), tab = wsTab;
      await loadState();
      if (id2) await openCustomer(id2, tab, true);
    } catch (error) { notify(error.message, true); }
    return;
  }
  if (d.assignRequest) {
    const [requestId, userId] = d.assignRequest.split(':');
    pendingFulfilRequest = requestId;
    closeWorkspace();
    showPage('numbers');
    openModal('number');
    setTimeout(() => {
      const owner = $('modal-fields').querySelector('[name=owner_user_id]');
      if (owner) { owner.value = userId; owner.dispatchEvent(new Event('change')); }
    }, 0);
    return;
  }
  if (d.newForCustomer) {
    const [type, id] = d.newForCustomer.split(':');
    openModal(type);
    setTimeout(() => {
      const owner = $('modal-fields').querySelector('[name=owner_user_id]');
      if (owner) { owner.value = id; owner.dispatchEvent(new Event('change')); }
    }, 0);
    return;
  }
  if (d.page) return showPage(d.page);
  if (d.go) { showPage(d.go); if (d.new) openModal(d.new); return; }
  if (d.open) return openModal(d.open);

  if (d.editExtension) return openModal('extension', state.extensions.find(x => x.extension === d.editExtension));
  if (d.editNumber) return openModal('number', state.phone_numbers.find(x => x.id === Number(d.editNumber)));
  if (d.editProvider) return openModal('provider', state.providers.find(x => x.id === Number(d.editProvider)));
  if (d.editWebhook) return openModal('webhook', state.webhooks.find(x => x.id === Number(d.editWebhook)));
  if (d.editUser) return openModal('user', state.users.find(x => x.id === Number(d.editUser)));
  if (d.wsEditCustomer) return openModal('user', workspace?.customer);

  if (d.deleteExtension) return remove('extension', d.deleteExtension, d.deleteExtension);
  if (d.deleteNumber) { const item = state.phone_numbers.find(x => x.id === Number(d.deleteNumber)); return remove('number', item.id, item.number); }
  if (d.deleteProvider) { const item = state.providers.find(x => x.id === Number(d.deleteProvider)); return remove('provider', item.id, item.name); }
  if (d.deleteWebhook) { const item = state.webhooks.find(x => x.id === Number(d.deleteWebhook)); return remove('webhook', item.id, item.name); }
  if (d.deleteUser) { const item = state.users.find(x => x.id === Number(d.deleteUser)); return remove('user', item.id, item.username); }
  if (d.deleteSip) {
    if (!confirm('Delete this SIP account and revoke its credentials?')) return;
    try {
      await api(`/admin/api/sip-accounts/${d.deleteSip}`, { method: 'DELETE' });
      notify('SIP account deleted');
      const id = keepWorkspace(), tab = wsTab;
      await loadState();
      if (id) await openCustomer(id, tab, true);
    } catch (error) { notify(error.message, true); }
    return;
  }
  if (d.discontinueNumber) {
    if (!confirm('Discontinue this number at its next monthly renewal? Calls will stop on that date.')) return;
    try {
      const result = await api(`/admin/api/numbers/${encodeURIComponent(d.discontinueNumber)}/discontinue`, { method: 'POST' });
      notify(`Number scheduled to end ${result.discontinue_at}`);
      await loadState();
    } catch (error) { notify(error.message, true); }
    return;
  }
  if (d.paidInvoice) {
    try {
      await api(`/admin/api/invoices/${d.paidInvoice}/status`, { method: 'POST', body: JSON.stringify({ status: 'paid' }) });
      notify('Invoice marked paid');
      const id = keepWorkspace(), tab = wsTab;
      await loadState();
      if (id) await openCustomer(id, tab, true);
    } catch (error) { notify(error.message, true); }
    return;
  }
  if (d.revokeKey) {
    if (!confirm('Permanently delete this API key? Integrations using it will stop working immediately.')) return;
    try {
      await api(`/admin/api/api-keys/${d.revokeKey}`, { method: 'DELETE' });
      notify('API key revoked');
      const id = keepWorkspace(), tab = wsTab;
      await loadState();
      if (id) await openCustomer(id, tab, true);
    } catch (error) { notify(error.message, true); }
    return;
  }
  if (d.defaultNumber) {
    const item = state.phone_numbers.find(x => x.id === Number(d.defaultNumber));
    try {
      await api('/admin/api/numbers/default', { method: 'POST', body: JSON.stringify({ number: item.number, extension: item.inbound_extension }) });
      notify('Default outbound number updated');
      await loadState();
    } catch (error) { notify(error.message, true); }
    return;
  }
  if (d.readVoicemail) return voicemailAction('read', d.readVoicemail);
  if (d.deleteVoicemail) return voicemailAction('delete', d.deleteVoicemail);
  if (d.testWebhook) {
    try {
      const result = await api(`/admin/api/webhooks/${d.testWebhook}/test`, { method: 'POST' });
      notify(`Webhook delivered — HTTP ${result.status_code}`);
    } catch (error) { notify(error.message, true); }
    return;
  }
});

/* ------------------------------------------------------- 27. Field wiring */
const wire = (id, event, handler) => $(id)?.addEventListener(event, handler);
['customer-search', 'extension-search', 'number-search'].forEach(id => wire(id, 'input', () => ({ 'customer-search': renderCustomers, 'extension-search': renderExtensions, 'number-search': renderNumbers }[id]())));
wire('call-search', 'input', () => debounce(() => { callOffset = 0; loadCalls(); }));
wire('recording-search', 'input', () => debounce(() => { recordingOffset = 0; loadRecordings(); }));
wire('voicemail-search', 'input', () => debounce(loadVoicemails));
['call-extension', 'call-status'].forEach(id => wire(id, 'change', () => { callOffset = 0; loadCalls(); }));
['recording-extension', 'recording-customer', 'recording-from', 'recording-to'].forEach(id => wire(id, 'change', () => { recordingOffset = 0; loadRecordings(); }));
['voicemail-extension', 'voicemail-folder'].forEach(id => wire(id, 'change', loadVoicemails));
wire('refresh-calls', 'click', loadCalls);
wire('refresh-recordings', 'click', loadRecordings);
wire('refresh-voicemails', 'click', loadVoicemails);

wire('modal-form', 'submit', saveModal);
wire('modal-close', 'click', closeModal);
wire('modal-cancel', 'click', closeModal);
wire('modal', 'click', event => { if (event.target === $('modal')) closeModal(); });
wire('ws-close', 'click', closeWorkspace);
wire('menu', 'click', () => { $('sidebar').classList.add('open'); $('scrim').classList.add('open'); });
wire('scrim', 'click', () => {
  $('sidebar').classList.remove('open');
  if ($('workspace').classList.contains('open')) closeWorkspace();
  else $('scrim').classList.remove('open');
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if ($('modal').classList.contains('open')) closeModal();
  else if ($('flow-config-modal').classList.contains('open')) closeOverlay('flow-config-modal');
  else if ($('workspace').classList.contains('open')) closeWorkspace();
  else $('sidebar').classList.remove('open');
});
wire('topbar', 'click', () => {});

wire('read-all-notifications', 'click', async () => {
  try { await api('/admin/api/notifications/read-all', { method: 'POST' }); notify('Notifications marked as read'); await loadState(); }
  catch (error) { notify(error.message, true); }
});
wire('request-number', 'click', () => openModal('request'));
wire('logout', 'click', async () => {
  try { await api('/admin/logout', { method: 'POST' }); } finally { location = '/admin/login'; }
});

/* Call-flow canvas: click to configure, drag to reorder, palette drag to add. */
wire('flow-config-form', 'submit', saveFlowConfig);
wire('flow-config-close', 'click', () => closeOverlay('flow-config-modal'));
wire('flow-config-cancel', 'click', () => closeOverlay('flow-config-modal'));
wire('flow-config-modal', 'click', event => { if (event.target === $('flow-config-modal')) closeOverlay('flow-config-modal'); });
wire('flow-nodes', 'click', event => {
  if (event.target.closest('[data-remove-node]')) return;
  const node = event.target.closest('[data-flow-index]');
  if (node) openFlowConfig(Number(node.dataset.flowIndex));
});
wire('flow-nodes', 'dragstart', event => {
  const node = event.target.closest('[data-flow-index]');
  if (node) event.dataTransfer.setData('application/x-flow-index', node.dataset.flowIndex);
});
wire('flow-nodes', 'dragover', event => event.preventDefault());
wire('flow-nodes', 'drop', event => {
  const target = event.target.closest('[data-flow-index]');
  const source = Number(event.dataTransfer.getData('application/x-flow-index'));
  if (target && Number.isInteger(source)) {
    event.preventDefault();
    const [node] = flowNodes.splice(source, 1);
    flowNodes.splice(Number(target.dataset.flowIndex), 0, node);
    renderFlowNodes();
  }
});
const dragSurface = $('flow-canvas');
const dragging = (el, on) => el?.classList.toggle('dragging', on);
document.querySelectorAll('[data-node-type]').forEach(button => {
  button.addEventListener('dragstart', event => {
    event.dataTransfer.setData('text/plain', button.dataset.nodeType);
    dragging(button, true);
  });
  button.addEventListener('dragend', () => dragging(button, false));
  button.onclick = () => { flowNodes.push({ type: button.dataset.nodeType, label: 'Click to configure' }); renderFlowNodes(); };
});
wire('flow-nodes', 'dragstart', event => dragging(event.target.closest('[data-flow-index]'), true));
wire('flow-nodes', 'dragend', event => dragging(event.target.closest('[data-flow-index]'), false));
if (dragSurface) {
  let depth = 0;
  dragSurface.addEventListener('dragenter', () => { depth += 1; dragSurface.classList.add('drag-over'); });
  dragSurface.addEventListener('dragleave', () => { depth -= 1; if (depth <= 0) { depth = 0; dragSurface.classList.remove('drag-over'); } });
  dragSurface.addEventListener('dragover', event => event.preventDefault());
  dragSurface.addEventListener('drop', event => {
    event.preventDefault();
    depth = 0;
    dragSurface.classList.remove('drag-over');
    const type = event.dataTransfer.getData('text/plain');
    if (type) { flowNodes.push({ type, label: 'Click to configure' }); renderFlowNodes(); }
  });
}
wire('route-number', 'change', () => {
  $('flow-entry-number').textContent = $('route-number').value || 'Assign a number to begin';
  flowNodes = (state.call_routes || []).find(x => x.phone_number === $('route-number').value)?.route?.nodes || [];
  renderFlowNodes();
});
wire('save-route', 'click', async () => {
  if (!$('route-number').value) return notify('Assign a number first', true);
  if (!flowNodes.length || flowNodes.some(n => !n.configured)) return notify('Add and configure every routing step before saving', true);
  try {
    await api('/admin/api/call-routes', { method: 'POST', body: JSON.stringify({ phone_number: $('route-number').value, name: 'Main call flow', route: { nodes: flowNodes }, active: true }) });
    notify('Call flow saved');
    const id = workspace?.customer?.id, tab = wsTab;
    await loadState();
    if (id) await openCustomer(id, tab, true);
  } catch (error) { notify(error.message, true); }
});

/* ------------------------------------------------------------- 28. Forms */
wire('settings-form', 'submit', async event => {
  event.preventDefault();
  try {
    await api('/admin/api/settings', { method: 'POST', body: JSON.stringify({
      default_extension: val('default-extension'), inbound_fallback_extension: val('inbound-fallback'),
      recording_enabled: $('rec-enabled').checked, recording_format: val('rec-format'),
      recording_retention_days: val('rec-retention'), recording_max_duration_seconds: val('rec-max'),
      recording_announcement: $('rec-announcement').checked, recording_announcement_media: val('rec-media'),
      recording_beep: $('rec-beep').checked,
    }) });
    notify('Call settings saved');
    await loadState();
  } catch (error) { notify(error.message, true); }
});
wire('email-form', 'submit', async event => {
  event.preventDefault();
  try {
    await api('/admin/api/email-config', { method: 'POST', body: JSON.stringify({
      enabled: $('email-enabled').checked, api_key: val('sendgrid-key'),
      from_email: val('sendgrid-from'), from_name: val('sendgrid-name'),
    }) });
    $('sendgrid-key').value = '';
    notify('SendGrid configuration saved');
    await loadState();
  } catch (error) { notify(error.message, true); }
});
wire('test-email', 'click', async () => {
  try {
    const result = await api('/admin/api/email-config/test', { method: 'POST', body: JSON.stringify({ email: val('sendgrid-test') }) });
    notify(result.ok ? 'Test email accepted by SendGrid' : 'Test failed');
  } catch (error) { notify(error.message, true); }
});
wire('profile-recording-form', 'submit', async event => {
  event.preventDefault();
  try {
    await api('/admin/api/profile/recording', { method: 'POST', body: JSON.stringify({ enabled: $('profile-recording').checked }) });
    notify('Recording preference updated');
    await loadState();
  } catch (error) { notify(error.message, true); }
});
wire('profile-email-form', 'submit', async event => {
  event.preventDefault();
  try {
    await api('/admin/api/profile/email', { method: 'POST', body: JSON.stringify({ email: val('profile-email') }) });
    notify('Voicemail email updated');
    await loadState();
  } catch (error) { notify(error.message, true); }
});
wire('password-form', 'submit', async event => {
  event.preventDefault();
  if ($('new-password').value !== $('confirm-password').value) return notify('Passwords do not match', true);
  try {
    await api('/admin/api/password', { method: 'POST', body: JSON.stringify({ password: $('new-password').value }) });
    event.target.reset();
    notify('Password changed');
  } catch (error) { notify(error.message, true); }
});

/* -------------------------------------------------------------- 29. Boot */
window.addEventListener('scroll', () => $('topbar')?.classList.toggle('scrolled', window.scrollY > 6), { passive: true });
window.addEventListener('resize', () => { if (workspace) moveTabInk(); }, { passive: true });
// Webfonts change tab widths after first paint; realign the indicator once loaded.
document.fonts?.ready.then(() => { if (workspace) moveTabInk(); });

loadState().then(() => {
  const requested = location.hash.slice(1);
  showPage(pageMeta[requested] ? requested : (state.is_admin ? 'users' : 'dashboard'));
});

setInterval(checkHealth, 30000);
setInterval(() => {
  if (document.hidden || $('modal').classList.contains('open')) return;
  loadState();
}, 15000);
setInterval(() => { if (!document.hidden) refreshDeviceStatus(); }, 8000);
