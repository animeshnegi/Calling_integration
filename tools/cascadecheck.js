/* Cascade regression check.
 *
 *   node tools/cascadecheck.js
 *
 * The setup-journey progress bar rendered as an empty chip and the step ticks
 * grew a border, because rules written for the *old* markup (`.journey span`,
 * `.journey i`) still matched the new one and outranked the rules that describe
 * it (`.journey-bar` is 0-1-0, `.journey span` is 0-1-1). Nothing in jsdom can
 * see that - jsdom never resolves the cascade - so this resolves it directly:
 * for a given element path it collects every declaration that matches, applies
 * specificity and source order, and reports the winner.
 *
 * Selectors are matched structurally (tag, class, descendant, child). Rules that
 * need a state the check does not model (:hover, :focus-visible) or that live in
 * a media query are reported separately and never win.
 */
const fs = require('fs');
const path = require('path');
const postcss = require('postcss');

const CSS = path.join(__dirname, '..', 'web', 'admin.css');
const root = postcss.parse(fs.readFileSync(CSS, 'utf8'));

/* ------------------------------------------------------------- matching --- */
function parseCompound(text) {
  const compound = { tag: '', classes: [], attr: [] };
  for (const part of text.match(/[.#]?[A-Za-z0-9_\-[\]="]+/g) || []) {
    if (part.startsWith('.')) compound.classes.push(part.slice(1));
    else if (part.startsWith('#')) compound.attr.push(part);
    else if (part.startsWith('[')) compound.attr.push(part);
    else compound.tag = part;
  }
  return compound;
}

/* Split a selector into (combinator, compound) steps, innermost last. */
function parseSelector(selector) {
  const steps = [];
  const tokens = selector.replace(/\s*>\s*/g, ' > ').trim().split(/\s+/);
  let combinator = ' ';
  for (const token of tokens) {
    if (token === '>') { combinator = '>'; continue; }
    steps.push({ combinator, compound: parseCompound(token), raw: token });
    combinator = ' ';
  }
  return steps;
}

const compoundMatches = (element, compound) =>
  (!compound.tag || compound.tag === element.tag) &&
  compound.classes.every(name => element.classes.includes(name));

/* Does `selector` match the element at path[index], whose ancestors are path[0..index-1]? */
function selectorMatches(steps, path, index) {
  const last = steps.length - 1;
  if (!compoundMatches(path[index], steps[last].compound)) return false;
  let cursor = index - 1;
  for (let step = last - 1; step >= 0; step -= 1) {
    const { combinator, compound } = steps[step];
    if (combinator === '>') {
      if (cursor < 0 || !compoundMatches(path[cursor], compound)) return false;
      cursor -= 1;
      continue;
    }
    let found = -1;
    for (let probe = cursor; probe >= 0; probe -= 1) {
      if (compoundMatches(path[probe], compound)) { found = probe; break; }
    }
    if (found < 0) return false;
    cursor = found - 1;
  }
  return true;
}

const STATE_PSEUDO = /:(hover|active|focus|focus-visible|focus-within|checked|disabled|not|first|last|nth)/;
const AT_PSEUDO = /::/;

function specificity(selector) {
  const withoutStrings = selector.replace(/\[[^\]]*\]/g, '[]');
  const ids = (withoutStrings.match(/#[\w-]+/g) || []).length;
  const classes = (withoutStrings.match(/\.[\w-]+/g) || []).length
    + (withoutStrings.match(/\[[^\]]*\]/g) || []).length
    + (withoutStrings.match(/:(?!:)[\w-]+(\([^)]*\))?/g) || []).length;
  const tags = parseSelector(withoutStrings).reduce((total, step) => total + (step.compound.tag ? 1 : 0), 0);
  return ids * 10000 + classes * 100 + tags;
}

/* Every declaration matching the element, in cascade order. */
function winners(path, property) {
  const matched = [];
  let order = 0;
  root.walkRules(rule => {
    order += 1;
    let media = null;
    for (let parent = rule.parent; parent; parent = parent.parent) {
      if (parent.type === 'atrule') media = parent.name + ' ' + parent.params;
    }
    for (const selector of rule.selectors) {
      const clean = selector.trim();
      const steps = parseSelector(clean);
      const targets = path.length - 1;
      if (!selectorMatches(steps, path, targets)) continue;
      for (const decl of rule.nodes || []) {
        if (decl.type !== 'decl' || decl.prop !== property) continue;
        matched.push({
          selector: clean, media, order, value: decl.value, important: Boolean(decl.important),
          specificity: specificity(clean),
          inert: STATE_PSEUDO.test(clean) || AT_PSEUDO.test(clean) || Boolean(media),
        });
      }
    }
  });
  return matched.sort((a, b) => (a.inert ? -1 : 0) - (b.inert ? -1 : 0)
    || a.specificity - b.specificity || a.order - b.order);
}

/* ---------------------------------------------------------------- checks --- */
let passed = 0;
const failures = [];

function element(tag, ...classes) { return { tag, classes }; }

function check(label, ok, detail) {
  if (ok) { passed += 1; console.log(`  PASS  ${label}`); }
  else { failures.push(label); console.log(`  FAIL  ${label}${detail ? ` — ${detail}` : ''}`); }
}

/* The setup-journey bar: a real progress bar, filled by --p. */
{
  console.log('\n=== Setup journey: the progress bar ===');
  const path = [element('body'), element('div', 'journey'), element('span', 'journey-bar'), element('i')];
  const width = winners(path, 'width');
  const top = width[width.length - 1];
  check('the bar fill takes its width from the journey bar rule, not an old one',
    top && top.selector === '.journey-bar i' && top.value === 'var(--p,0%)',
    top ? `${top.selector} -> ${top.value}` : 'no width declared');

  const background = winners(path, 'background');
  const topBackground = background[background.length - 1];
  check('the bar fill keeps its gradient',
    topBackground && topBackground.value.startsWith('linear-gradient'),
    topBackground ? topBackground.value : 'none');

  const track = winners([element('body'), element('div', 'journey'), element('span', 'journey-bar')], 'background');
  const topTrack = track[track.length - 1];
  check('the track is the dim rail, not a surface chip',
    topTrack && topTrack.selector === '.journey-bar' && /8b93a8/.test(topTrack.value),
    topTrack ? `${topTrack.selector} -> ${topTrack.value}` : 'none');

  const padding = winners([element('body'), element('div', 'journey'), element('span', 'journey-bar')], 'padding');
  const live = padding.filter(rule => !rule.inert).pop();
  check('nothing pads the track into a pill', !live || /^(0|0px)$/.test(live.value.trim()),
    live ? `${live.selector} -> ${live.value}` : '');

  const radius = winners(path, 'border-radius').filter(x => x.selector === '.journey i');
  check('no legacy rule rounds the fill differently', radius.length === 0);
}

/* The per-step tick: a small circle that only holds its glyph. */
{
  console.log('\n=== Setup journey: the step ticks ===');
  const path = [element('body'), element('div', 'journey'), element('div', 'journey-steps'),
    element('button', 'journey-step'), element('span', 'tick')];
  const zero = value => !value || /^(0|0px|none)$/.test(value.trim());
  const padding = winners(path, 'padding').filter(rule => !rule.inert).pop();
  check('a tick is never padded like a chip', zero(padding && padding.value),
    padding ? `${padding.selector} -> ${padding.value}` : '');
  const border = winners(path, 'border').filter(rule => !rule.inert).pop();
  check('a tick never grows a chip border', zero(border && border.value),
    border ? `${border.selector} -> ${border.value}` : '');
  const size = winners(path, 'width').filter(rule => !rule.inert).pop();
  check('a tick is a fixed 17px circle', size && size.value === '17px', size ? size.value : 'none');
  const background = winners(path, 'background').filter(rule => !rule.inert).pop();
  check('the tick background is the tick rule', background && background.selector.endsWith('.tick'),
    background ? background.selector : 'none');

  const done = [element('body'), element('div', 'journey'), element('div', 'journey-steps'),
    element('button', 'journey-step', 'done'), element('span', 'tick')];
  const doneBackground = winners(done, 'background').filter(rule => !rule.inert).pop();
  check('a completed tick is filled with the success fill',
    doneBackground && doneBackground.value.includes('--fill-ok'),
    doneBackground ? doneBackground.value : 'none');
}

/* The credential sheet: readable table, copy column, nothing clipped. */
{
  console.log('\n=== Credential sheet ===');
  const cell = [element('body'), element('div', 'cred-sheet'), element('table', 'cred-table'),
    element('td', 'cred-copy'), element('button', 'btn', 'ghost', 'sm')];
  const padding = winners(cell, 'padding').filter(rule => !rule.inert).pop();
  // The compact button lives in the sheet's own rule; the generic .btn.sm rule
  // must not win, or the copy buttons would stretch the rows apart again.
  check('the copy button is compacted by the sheet, not stretched by a cell',
    padding && /^3px 9px$/.test(padding.value) && /cred/.test(padding.selector),
    padding ? `${padding.selector} -> ${padding.value}` : 'none');
  const row = [element('body'), element('div', 'cred-sheet'), element('table', 'cred-table'),
    element('td', 'cred-copy')];
  const rowPadding = winners(row, 'padding').filter(rule => !rule.inert).pop();
  check('rows sit close together', rowPadding && /^5px 9px$/.test(rowPadding.value),
    rowPadding ? `${rowPadding.selector} -> ${rowPadding.value}` : 'none');
  const code = [element('body'), element('div', 'cred-sheet'), element('table', 'cred-table'),
    element('td'), element('code')];
  const font = winners(code, 'font-family').filter(rule => !rule.inert).pop();
  check('credential values are monospace', font && /mono/.test(font.value), font ? font.value : 'none');
  const hidden = winners([element('body'), element('div', 'cred-sheet'), element('div', 'cred-rotate')], 'display');
  const rotateGlyph = hidden[hidden.length - 1];
  check('the change-password panel is a grid, revealed by its hidden attribute',
    rotateGlyph && rotateGlyph.value === 'grid', rotateGlyph ? rotateGlyph.value : 'none');
}

/* The customer workspace drawer: the identity stays, one region scrolls, and the
   tab row pins inside it. jsdom cannot measure a scroll box, so the rules that
   decide it are resolved here instead: exactly one element may scroll, the head
   may not take the height the region needs, and nothing later may take the
   sticky positioning away from the tab row. */
{
  console.log('\n=== Workspace drawer ===');
  const scroll = [element('body'), element('div', 'workspace'), element('div', 'ws-scroll')];
  const overflow = winners(scroll, 'overflow-y').filter(rule => !rule.inert).pop();
  check('the region below the identity is the scroller',
    overflow && overflow.value === 'auto', overflow ? `${overflow.selector} -> ${overflow.value}` : 'none');
  const flex = winners(scroll, 'flex').filter(rule => !rule.inert).pop();
  check('it takes the height the identity leaves',
    flex && /flex:1|^1/.test(flex.value.replace(/\s/g, '')), flex ? flex.value : 'none');
  const minHeight = winners(scroll, 'min-height').filter(rule => !rule.inert).pop();
  check('and may shrink, so it never pushes itself out of the drawer',
    minHeight && minHeight.value === '0', minHeight ? minHeight.value : 'none');

  const head = [element('body'), element('div', 'workspace'), element('header', 'ws-head')];
  const headFlex = winners(head, 'flex').filter(rule => !rule.inert).pop();
  check('the identity block keeps its natural height',
    headFlex && headFlex.value === 'none', headFlex ? headFlex.value : 'none');

  const tabs = [element('body'), element('div', 'ws-scroll'), element('nav', 'ws-tabs')];
  const position = winners(tabs, 'position').filter(rule => !rule.inert).pop();
  check('the tab row sticks to the top of that region',
    position && position.value === 'sticky' && /ws-tabs/.test(position.selector),
    position ? `${position.selector} -> ${position.value}` : 'none');
  const top = winners(tabs, 'top').filter(rule => !rule.inert).pop();
  check('at the top edge, with nothing above it',
    top && top.value === '0', top ? top.value : 'none');
  const zIndex = winners(tabs, 'z-index').filter(rule => !rule.inert).pop();
  check('drawn above the content scrolling underneath',
    zIndex && Number(zIndex.value) >= 2, zIndex ? zIndex.value : 'none');

  const body = [element('body'), element('div', 'ws-scroll'), element('div', 'ws-body')];
  const bodyOverflow = winners(body, 'overflow-y').filter(rule => !rule.inert).pop();
  check('the tab body itself does not scroll any more',
    !bodyOverflow || bodyOverflow.value !== 'auto',
    bodyOverflow ? `${bodyOverflow.selector} -> ${bodyOverflow.value}` : 'no overflow rule');
  const summary = [element('body'), element('div', 'ws-scroll'), element('div', 'ws-summary')];
  const summaryPadding = winners(summary, 'padding').filter(rule => !rule.inert).pop();
  check('the account summary scrolls inside the region',
    summaryPadding && /^20px/.test(summaryPadding.value),
    summaryPadding ? `${summaryPadding.selector} -> ${summaryPadding.value}` : 'none');
}

console.log(`\n${failures.length ? `${failures.length} CASCADE CHECK(S) FAILED` : 'CASCADE CHECK PASSED'}`);
console.log(`${passed} checks passed`);
if (failures.length) failures.forEach(name => console.log(`  - ${name}`));
process.exit(failures.length ? 1 : 0);
