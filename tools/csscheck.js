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

check('the entrance cascade is keyed on .page.entering', css.includes('.page.entering > *'));
check('no cascade is keyed on .page.active', !/\.page\.active\s*>/.test(css));
check('both skins define their own tokens', css.includes('body.theme-dark{') && css.includes('body.theme-light{'));
check('reduced motion is honoured', /prefers-reduced-motion:reduce/.test(css));
check('focus rings are drawn with :focus-visible', css.includes(':focus-visible'));
check('scrollbars are themed', css.includes('::-webkit-scrollbar-thumb'));

process.stdout.write(`\n${failures ? `${failures} CSS CHECK(S) FAILED` : 'CSS CHECK PASSED'}\n`);
process.exit(failures ? 1 : 0);
