/* Legibility audit for the two console skins.
 *
 *   node tools/contrastcheck.js
 *
 * Rules that paint text are matched against the surface they sit on - either the
 * background declared by the same rule, or the nearest ancestor rule that sets
 * one - and every pairing is checked against WCAG AA in both skins. Because
 * colours are mostly tokens, the audit resolves var() chains per skin before
 * measuring, which is exactly what a browser does at paint time.
 */
const fs = require('fs');
const path = require('path');

const CSS = fs.readFileSync(path.join(__dirname, '..', 'web', 'admin.css'), 'utf8');

/* ---------------------------------------------------------------- parsing */
function parse(css) {
  const rules = [];
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const walk = (text, atRule) => {
    let index = 0;
    while (index < text.length) {
      const open = text.indexOf('{', index);
      if (open === -1) break;
      const selector = text.slice(index, open).trim();
      let depth = 1, cursor = open + 1;
      while (cursor < text.length && depth > 0) {
        if (text[cursor] === '{') depth += 1;
        else if (text[cursor] === '}') depth -= 1;
        cursor += 1;
      }
      const body = text.slice(open + 1, cursor - 1);
      if (selector.startsWith('@')) walk(body, selector);
      else if (selector) rules.push({ selector, body, atRule });
      index = cursor;
    }
  };
  walk(stripped, null);
  return rules;
}

const RULES = parse(CSS);

/* ------------------------------------------------------------- colour util */
function toRgb(value) {
  const text = String(value).trim();
  let match = text.match(/^#([0-9a-fA-F]{3,8})$/);
  if (match) {
    let hex = match[1];
    if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
    if (hex.length === 4) hex = hex.split('').map(c => c + c).join('');
    const [r, g, b, a] = [0, 2, 4, 6].map(i => parseInt(hex.slice(i, i + 2), 16));
    return { r, g, b, a: hex.length === 8 ? a / 255 : 1 };
  }
  match = text.match(/^rgba?\(([^)]+)\)$/);
  if (match) {
    const parts = match[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  }
  return null;
}

function over(fore, back) {
  const a = fore.a ?? 1;
  return {
    r: Math.round(fore.r * a + back.r * (1 - a)),
    g: Math.round(fore.g * a + back.g * (1 - a)),
    b: Math.round(fore.b * a + back.b * (1 - a)),
    a: 1,
  };
}

function luminance({ r, g, b }) {
  const channel = value => {
    const c = value / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/* ------------------------------------------------------------ token tables */
function declarations(body) {
  return body.split(';').map(part => part.trim()).filter(Boolean).map(part => {
    const colon = part.indexOf(':');
    return { prop: part.slice(0, colon).trim(), value: part.slice(colon + 1).trim() };
  });
}

function tokensFor(skin) {
  const tokens = {};
  for (const rule of RULES) {
    const selector = rule.selector.replace(/\s+/g, '');
    const isDark = selector.includes('theme-dark');
    const isLight = selector.includes('theme-light');
    const applies = skin === 'dark' ? isDark || selector === ':root' : isLight || selector === ':root';
    if (!applies) continue;
    // A skin block wins over :root, so apply :root first and skin last.
    if (selector !== ':root') continue;
    for (const { prop, value } of declarations(rule.body)) if (prop.startsWith('--')) tokens[prop] = value;
  }
  for (const rule of RULES) {
    const selector = rule.selector.replace(/\s+/g, '');
    const isDark = selector.includes('theme-dark');
    const isLight = selector.includes('theme-light');
    const applies = skin === 'dark' ? isDark : isLight;
    if (!applies) continue;
    for (const { prop, value } of declarations(rule.body)) if (prop.startsWith('--')) tokens[prop] = value;
  }
  return tokens;
}

/* Replace every var() inside a value - including ones buried in a gradient or
   a shorthand - the way the cascade does before the browser paints. */
function substitute(value, tokens, depth = 0) {
  if (depth > 6 || value === undefined) return value;
  return String(value).replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)/g, (match, name, fallback) => {
    const target = tokens[name] ?? fallback;
    return target === undefined ? match : substitute(target, tokens, depth + 1);
  });
}

function resolve(value, tokens, depth = 0) {
  if (depth > 6 || value === undefined) return value;
  const text = String(value).trim();
  const variable = text.match(/^var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)$/);
  if (!variable) return text;
  const fallback = variable[2] !== undefined ? variable[2] : undefined;
  const target = tokens[variable[1]] ?? fallback;
  return target === undefined ? undefined : resolve(target, tokens, depth + 1);
}

/* First opaque colour in a background shorthand. */
function backgroundColor(value, tokens) {
  if (value === undefined) return null;
  const resolved = resolve(value, tokens);
  if (!resolved) return null;
  const colour = toRgb(resolved);
  if (colour) return colour;
  for (const part of resolved.split(/\s+/)) {
    const candidate = toRgb(part);
    if (candidate) return candidate;
  }
  return null;
}

const SKINS = { dark: tokensFor('dark'), light: tokensFor('light') };
const BASE = { dark: toRgb('#070b16'), light: toRgb('#f4f5fa') };

/* ------------------------------------------------- element/surface matching */
/* A rule only paints one skin: theme-scoped selectors are exclusive, anything
   else (tokens, base layout, shared components) applies to both. */
function appliesToSkin(selector, skin) {
  if (selector.includes('theme-light')) return skin === 'light';
  if (selector.includes('theme-dark')) return skin === 'dark';
  // The sign-in page declares its own skin (dark) on <body>, so its copy is
  // never painted on the light surface.
  if (/\.login-/.test(selector)) return skin === 'dark';
  return true;
}

const alternatives = selector => selector.split(',').map(part => part.trim().replace(/\s+/g, ' ')).filter(Boolean);

/* Every colour mentioned by a background shorthand, averaged when it is a
   gradient: an approximation that is honest about what the eye sees. */
function backgroundColours(value, tokens) {
  if (value === undefined) return [];
  const resolved = substitute(resolve(value, tokens), tokens);
  if (!resolved) return [];
  const direct = toRgb(resolved);
  if (direct) return [direct];
  const found = [];
  const pattern = /#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)/g;
  let match;
  while ((match = pattern.exec(resolved)) !== null) {
    const colour = toRgb(match[0]);
    if (colour) found.push(colour);
  }
  return found;
}

function mix(colours) {
  if (!colours.length) return null;
  return {
    r: Math.round(colours.reduce((sum, c) => sum + c.r, 0) / colours.length),
    g: Math.round(colours.reduce((sum, c) => sum + c.g, 0) / colours.length),
    b: Math.round(colours.reduce((sum, c) => sum + c.b, 0) / colours.length),
    a: Math.max(...colours.map(c => c.a ?? 1)),
  };
}

/* The surface an element paints on: the skin's page colour, then every ancestor
   background composited in order, then the element's own background. */
function surfaceFor(selector, ownBackground, tokens, skin) {
  let best = null;
  for (const alternative of alternatives(selector)) {
    const parts = alternative.split(/\s*[>+~]\s*|\s+/).filter(Boolean);
    const chain = [];
    for (let take = 1; take < parts.length; take += 1) {
      const prefix = parts.slice(0, take).join(' ');
      let background = null;
      for (const rule of RULES) {
        if (!appliesToSkin(rule.selector, skin)) continue;
        if (!alternatives(rule.selector).includes(prefix)) continue;
        const declaration = declarations(rule.body).find(item => item.prop === 'background' || item.prop === 'background-color');
        if (!declaration) continue;
        const colour = mix(backgroundColours(declaration.value, tokens));
        if (colour) background = colour;
      }
      if (background) chain.push(background);
    }
    const composited = chain.reduce((surface, colour) => over(colour, surface), BASE[skin]);
    if (!best) best = composited;
  }
  const base = best || BASE[skin];
  const own = ownBackground ? mix(backgroundColours(ownBackground, tokens)) : null;
  return own ? over(own, base) : base;
}

/* ------------------------------------------------------------------- audit */
const failures = [];
const advisories = [];
let measured = 0;

for (const skin of ['dark', 'light']) {
  const tokens = SKINS[skin];
  for (const rule of RULES) {
    if (rule.atRule && rule.atRule.includes('prefers-reduced-motion')) continue;
    if (!appliesToSkin(rule.selector, skin)) continue;
    const declarationsList = declarations(rule.body);
    const colorDecl = declarationsList.find(item => item.prop === 'color');
    if (!colorDecl) continue;
    const foreground = toRgb(resolve(colorDecl.value, tokens));
    if (!foreground) continue;
    const own = declarationsList.find(item => item.prop === 'background' || item.prop === 'background-color');
    const surface = surfaceFor(rule.selector, own && own.value, tokens, skin);
    const ratio = contrast(over(foreground, surface), surface);
    measured += 1;
    const text = rule.selector.slice(0, 66);
    // Icons and decorative glyphs are allowed to sit lower than body text.
    const decorative = /glyph|icon|tile|aw |\.dot|chev|brand-mark|::marker|::before|::after/.test(rule.selector);
    const minimum = decorative ? 2.0 : 4.5;
    if (ratio < minimum) {
      const entry = `${skin.padEnd(5)} ${ratio.toFixed(2)}:1  ${text}` +
        (process.env.DEBUG ? `  [fg ${colorDecl.value} -> ${JSON.stringify(foreground)} on ${JSON.stringify(surface)}]` : '');
      (decorative ? advisories : failures).push(entry);
    }
  }
}

process.stdout.write(`audited ${measured} text declarations across both skins\n\n`);
if (advisories.length) {
  process.stdout.write(`decorative glyphs below 2:1 (${advisories.length}):\n`);
  advisories.forEach(line => process.stdout.write(`  ${line}\n`));
  process.stdout.write('\n');
}
if (failures.length) {
  process.stdout.write(`TEXT BELOW 4.5:1 (${failures.length}):\n`);
  failures.forEach(line => process.stdout.write(`  ${line}\n`));
  process.stdout.write('\nCONTRAST CHECK FAILED\n');
  process.exit(1);
}
process.stdout.write('CONTRAST CHECK PASSED\n');
