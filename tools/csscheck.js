/* Stylesheet contract check.
 *
 *   node tools/csscheck.js
 *
 * Parses web/admin.css, then cross-checks it against the markup and the console
 * script: a class the UI applies must have a rule, a rule that no longer has a
 * user is reported, and the entrance cascade must stay keyed on .page.entering
 * rather than .page.active. Needs postcss.
 */
const fs = require('fs');
const path = require('path');
const postcss = require('postcss');

const WEB = path.join(__dirname, '..', 'web');
const css = fs.readFileSync(path.join(WEB, 'admin.css'), 'utf8');
const html = fs.readFileSync(path.join(WEB, 'admin.html'), 'utf8');
const login = fs.readFileSync(path.join(WEB, 'admin-login.html'), 'utf8');
const script = fs.readFileSync(path.join(WEB, 'admin.js'), 'utf8');

let failures = 0;
const check = (label, ok, detail) => {
  process.stdout.write(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || detail === undefined ? '' : ` — ${detail}`}\n`);
  if (!ok) failures += 1;
};

const root = postcss.parse(css);
check('admin.css parses with postcss', true);

/* Class names in this project are lowercase and hyphenated; anything else that
   looks like a class (interpolation fragments, vendor prefixes) is noise. */
const CLASS = /^[a-z][a-z0-9-]*[a-z0-9]$/;
const defined = new Set();
root.walkRules(rule => {
  const source = rule.parent && rule.parent.type === 'atrule' ? rule.parent.params : '';
  const text = `${source} ${rule.selector}`;
  for (const match of text.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) if (CLASS.test(match[1])) defined.add(match[1]);
});

/* Classes the UI applies: markup attributes, classList calls and template
   literals in the console script. */
const applied = new Set();
const collect = text => {
  for (const match of text.matchAll(/class="([^"]+)"/g)) match[1].split(/\s+/).forEach(name => name && applied.add(name));
  for (const match of text.matchAll(/classList\.(?:add|remove|toggle)\(\s*'([^']+)'/g)) applied.add(match[1]);
  for (const match of text.matchAll(/classList\.(?:add|remove|toggle)\(\s*"([^"]+)"/g)) applied.add(match[1]);
  for (const match of text.matchAll(/className\s*=\s*'([^']+)'/g)) match[1].split(/\s+/).forEach(name => name && applied.add(name));
  // template-built class attributes: class="row ${x ? 'a' : 'b'}"
  for (const match of text.matchAll(/class="([^"$]*)\$\{([^}]*)\}([^"]*)"/g)) {
    `${match[1]} ${match[3]}`.split(/\s+/).forEach(name => name && applied.add(name));
    for (const literal of match[2].matchAll(/'([^']*)'/g)) literal[1].split(/\s+/).forEach(name => name && applied.add(name));
  }
};
collect(html);
collect(login);
collect(script);

/* Allow-list: classes owned by the reduced-motion media query, decorative role
   markers styled by the tokens, and states applied by libraries. */
const allowed = new Set(['theme-loading', 'theme-dark', 'theme-light', 'admin-theme', 'customer-theme']);
const clean = [...applied].filter(name => CLASS.test(name));
const missing = clean.filter(name => !defined.has(name) && !allowed.has(name));
check(`every class the UI applies exists in CSS (${clean.length} applied)`, missing.length === 0, missing.join(', '));

/* State classes are built from data (statusPill(x.status)), so a rule counts as
   used when the name appears anywhere in the markup or the script. */
const everywhere = `${html}\n${login}\n${script}`;
/* Call and registration states arrive from the database, so their selectors are
   legitimate even when today's data never produces them. */
const DATA_STATES = new Set([
  'inactive', 'initiated', 'queued', 'retrying', 'ringing', 'dialing_customer', 'dialing',
  'employee_answered', 'answered', 'completed', 'failed', 'rejected', 'overdue', 'void',
  'open', 'pending', 'paid', 'delivered', 'finalized', 'online', 'offline', 'approved',
  'fulfilled', 'recording', 'deleted', 'processing', 'on', 'off', 'warn', 'info', 'violet',
]);
const unused = [...defined].filter(name => !clean.includes(name) && !everywhere.includes(name) && !DATA_STATES.has(name));
check(`no class rule is left without a user (${defined.size} defined)`, unused.length === 0, unused.slice(0, 12).join(', '));

/* The chosen option used to render as white text with no background: the
   browser drops background-image on option, so the gradient disappeared and only
   `color:#fff` survived - an invisible row in the list. */
check('a chosen option is painted in both skins',
  css.includes('body.theme-light select option:checked{background:#e6e9ff;color:#1b2140')
  && css.includes('body.theme-dark select option:checked{background:#2b2a63;color:#ffffff'),
  'solid colours, no gradient');
check('and no option is left white-on-white by a dropped gradient',
  !/select option:checked\s*\{[^}]*linear-gradient/.test(css),
  'no gradient on an option background');
check('the picker itself is styled where the engine hands it over',
  css.includes('@supports (appearance: base-select)')
  && css.includes('::picker(select)') && css.includes('option:hover'),
  'base-select panel, options and hover');
check('the flow toolbar may fold rather than push the save button out',
  /\.flow-toolbar\s*\{[^}]*flex-wrap:\s*wrap/.test(css)
  && /\.flow-toolbar \.tools\s*\{[^}]*flex-wrap:\s*wrap/.test(css),
  'wraps at narrow widths');
check('the public pages wear the console light surface',
  fs.readFileSync(path.join(WEB, 'index.html'), 'utf8').includes('#f4f5fa')
  && fs.readFileSync(path.join(WEB, 'documentation.html'), 'utf8').includes('--bg:#f4f5fa')
  && login.includes('theme-light login-body'),
  'landing, documentation and sign-in');

/* The "little thing coming from the left" was a sheen: a highlight that started
   off-canvas left and swept across a surface on hover. Every one of them is gone
   - the keyframes, the button shine, the card sweeps and the panel sweeps - and
   what replaced them is a still gloss that never moves. */
check('no sheen sweep survives',
  !/sheenSweep/.test(css) && !/hover::after\s*\{[^}]*translateX/.test(css)
  && !/\.glass-card:hover::before\s*\{[^}]*translateX/.test(css),
  'keyframes, button shine and card sweeps removed');
check('and the surfaces keep a still gloss instead',
  /\.panel\.sheen::after[^{]*\{[^}]*linear-gradient\(180deg/.test(css)
  && /\.ws-card::after\{[^}]*linear-gradient\(180deg/.test(css),
  'painted once at the top edge');
check('hover still lifts what it should',
  /\.ws-card:hover\{transform:translateY/.test(css) && /\.btn\.primary:not\(:disabled\):hover\{transform:translateY/.test(css),
  'lift and shadow, no streak');

check('the entrance cascade is keyed on .page.entering', css.includes('.page.entering > *'));
check('no cascade is keyed on .page.active', !/\.page\.active\s*>/.test(css));
check('both skins define their own tokens', css.includes('body.theme-dark{') && css.includes('body.theme-light{'));
check('reduced motion is honoured', /prefers-reduced-motion:reduce/.test(css));
check('focus rings are drawn with :focus-visible', css.includes(':focus-visible'));
check('scrollbars are themed', css.includes('::-webkit-scrollbar-thumb'));

process.stdout.write(`\n${failures ? `${failures} CSS CHECK(S) FAILED` : 'CSS CHECK PASSED'}\n`);
process.exit(failures ? 1 : 0);
