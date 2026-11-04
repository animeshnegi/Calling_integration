/* Real-browser layout smoke test against tools/devpreview.py (never a live PBX).
 * PORT=5001 .venv/bin/python tools/devpreview.py
 * npm install --no-save --package-lock=false playwright-core @sparticuz/chromium
 * UI_BASE_URL=http://127.0.0.1:5001 node tools/formal-ui-browsercheck.js
 * No SIP registration or calls. Optional UI_SCREENSHOTS points outside Git.
 */
const { chromium: playwright } = require('playwright-core');
const chromiumModule = require('@sparticuz/chromium');
const chromium = chromiumModule.default || chromiumModule;
const path = require('path');
const fs = require('fs');

(async () => {
  const baseURL = process.env.UI_BASE_URL || 'http://127.0.0.1:5001';
  const browser = await playwright.launch({ executablePath: await chromium.executablePath(), args: chromium.args.filter(arg => arg !== '--single-process'), headless: true });
  let checks = 0;
  const check = (ok, message) => { if (!ok) throw new Error(message); checks++; };
  try {
    for (const [username, password, admin] of [
      ['engineerip', 'preview-admin-password', true],
      ['meridian', 'customer-password-01', false],
    ]) {
      const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const login = await context.request.post('/login', { data: { username, password } });
      check(login.ok(), `${username}: login (${login.status()}; restart the local preview if rate-limited)`);
      await page.goto('/admin');
      await page.waitForFunction(() => !document.body.classList.contains('theme-loading'));
      check(await page.locator('.modal:visible').count() === 0, `${username}: modals closed at startup`);
      check(!await page.locator('#workspace').isVisible(), `${username}: workspace closed at startup`);
      for (const selector of (admin ? ['[data-customer-only]'] : ['[data-admin-only]'])) {
        check(await page.locator(selector + ':visible').count() === 0, `${username}: role-specific controls hidden`);
      }
      const pages = await page.locator('#nav [data-page]:visible').evaluateAll(nodes => nodes.map(node => node.dataset.page));
      for (const name of pages) {
        await page.locator(`#nav [data-page="${name}"]`).click();
        await page.waitForTimeout(100);
        check(await page.locator(`#page-${name}`).isVisible(), `${username}: ${name} page opens`);
        await page.setViewportSize({ width: 390, height: 844 });
        check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${username}: ${name} mobile overflow`);
        await page.setViewportSize({ width: 1440, height: 1000 });
      }
      await page.locator('#nav [data-page="dashboard"]').click();
      if (process.env.UI_SCREENSHOTS) {
        fs.mkdirSync(process.env.UI_SCREENSHOTS, { recursive: true });
        await page.screenshot({ path: path.join(process.env.UI_SCREENSHOTS, `${admin ? 'admin' : 'customer'}-desktop.png`), fullPage: true });
      }
      // Both themes must set the body's inherited text color, not just cards.
      await page.locator('#theme-toggle').click();
      check(await page.evaluate(() => {
        const dark = document.body.classList.contains('theme-dark');
        const color = getComputedStyle(document.body).color;
        return dark ? color === 'rgb(241, 245, 249)' : color === 'rgb(15, 23, 42)';
      }), `${username}: theme inherited foreground`);
      await page.locator('#theme-toggle').click();
      if (admin) {
        await page.locator('#page-dashboard [data-new="user"]').click();
        await page.locator('#modal').waitFor({ state: 'visible' });
        check(await page.locator('#modal-fields').evaluate(el => getComputedStyle(el).overflowY === 'auto'), 'modal has independent scroll');
        await page.locator('#modal-close').click();
        check(!await page.locator('#modal').isVisible(), 'modal closes');
        await page.locator('#nav [data-page="users"]').click();
        await page.locator('[data-open-customer]').first().click();
        await page.locator('#workspace').waitFor({ state: 'visible' });
        await page.waitForTimeout(300);
        check(await page.locator('#workspace').isVisible(), 'customer drawer opens');
        const geometry = await page.locator('#workspace').evaluate(el => {
          const rect = el.getBoundingClientRect();
          const scroll = el.querySelector('.ws-scroll');
          return rect.right <= innerWidth + 1 && rect.bottom <= innerHeight + 1 && getComputedStyle(scroll).overflowY === 'auto';
        });
        check(geometry, 'drawer stays in viewport with independent scrolling');
        await page.locator('#ws-close').click();
      }
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(300);
      check(await page.locator('#sidebar').evaluate(el => el.getBoundingClientRect().right <= 1), `${username}: mobile nav off canvas`);
      await page.locator('#menu').click();
      await page.waitForTimeout(300);
      check(await page.locator('#sidebar').evaluate(el => el.getBoundingClientRect().left >= -1), `${username}: mobile nav opens`);
      await page.locator('#nav [data-page="dashboard"]').click();
      await page.waitForTimeout(300);
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${username}: no mobile horizontal overflow`);
      if (process.env.UI_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.UI_SCREENSHOTS, `${admin ? 'admin' : 'customer'}-mobile.png`), fullPage: true });
      check(errors.length === 0, `${username}: browser errors: ${errors.join('; ')}`);
      await context.close();
    }
    const publicContext = await browser.newContext({ baseURL });
    const publicPage = await publicContext.newPage();
    for (const url of ['/', '/login', '/documentation', '/phone']) {
      for (const width of [390, 1440]) {
        await publicPage.setViewportSize({ width, height: 900 });
        const response = await publicPage.goto(url);
        check(response.ok(), `${url}: HTTP ${response.status()}`);
        check(await publicPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${url}: overflow at ${width}px`);
      }
    }
    await publicContext.close();
    console.log(`PASS: ${checks} real-browser layout/navigation checks`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
