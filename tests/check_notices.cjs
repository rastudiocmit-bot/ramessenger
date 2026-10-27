// Service notice capsule: show, delete, fold into the pill, open the list, switch off.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
const shots = process.env.CM_TEST_SCREENSHOTS;
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
  const save = async (page, name) => { if (shots) { fs.mkdirSync(shots, { recursive: true }); await page.screenshot({ path: path.join(shots, name + '.png') }); } };
  try {
    for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
      const data = await fixture(context, 'owner');
      const page = await context.newPage(); page.setDefaultTimeout(15000);
      await page.goto('https://cabinet.example.test/?view=messages'); await page.locator('.chat-select').first().tap();
      await page.evaluate(t => { document.documentElement.dataset.theme = t; }, theme);
      await page.waitForTimeout(600);
      // A second instance, as app.js makes one, drives the same capsule API.
      const notify = (args) => page.evaluate(a => window.__n.notify(a), args);
      await page.evaluate(() => { document.querySelectorAll('.notices').forEach(n => n.remove()); window.__n = window.CenterNotices({ $: s => document.querySelector(s) }); });
      assert.equal(await notify({ type: 'error', title: 'Не отправлено в MAX', text: 'Анна Смирнова · AlfaCRM не ответила', action: { label: 'Повторить' } }), true);
      await page.waitForSelector('.notice-capsule:not([hidden])');
      const box = await page.locator('.notice-capsule').boundingBox();
      assert.ok(box.x >= 15 && box.x + box.width <= 375, 'fits the phone width');
      await page.waitForTimeout(400); await save(page, `notice-${theme}-new`);
      // Delete straight from the incoming capsule.
      await page.locator('.notice-capsule .notice-del').tap();
      await page.waitForSelector('.notice-capsule', { state: 'hidden' });
      assert.equal(await page.locator('.notice-pill').isHidden(), true, 'a deleted notice leaves nothing behind');
      // Two warnings; the same one twice counts instead of stacking.
      await notify({ type: 'warning', title: 'AlfaCRM просит войти', text: 'История приостановлена', key: 'a' });
      await notify({ type: 'error', title: 'Текст без агента', text: 'DeepSeek не подключён', key: 'b' });
      await notify({ type: 'error', title: 'Текст без агента', text: 'DeepSeek не подключён', key: 'b' });
      assert.match(await page.locator('.notice-capsule .notice-title').textContent(), /2 раза/);
      await page.waitForSelector('.notice-pill:not([hidden])', { timeout: 10000 });
      assert.equal(await page.locator('.notice-count').textContent(), '2');
      await save(page, `notice-${theme}-pill`);
      await page.locator('.notice-pill').tap(); await page.waitForSelector('.notice-panel:not([hidden])');
      assert.equal(await page.locator('.notice-row').count(), 2);
      await page.waitForTimeout(400); await save(page, `notice-${theme}-panel`);
      await page.locator('.notice-row .notice-del').first().tap();
      assert.equal(await page.locator('.notice-row').count(), 1, 'a notice is deleted from the list');
      await page.locator('.notice-fold').tap(); await page.waitForSelector('.notice-panel', { state: 'hidden' });
      // Switched off in the settings: nothing shows, the caller keeps its fallback.
      assert.equal(await page.locator('#notices-enabled').count(), 1, 'setting exists');
      await page.evaluate(() => { const t = document.querySelector('#notices-enabled'); t.checked = false; t.dispatchEvent(new Event('change')); });
      assert.equal(await page.evaluate(() => localStorage.getItem('cm:service-notices')), 'off');
      await page.evaluate(() => { document.querySelectorAll('.notices').forEach(n => n.remove()); window.__n = window.CenterNotices({ $: s => document.querySelector(s) }); });
      assert.equal(await notify({ type: 'error', title: 'Скрыто' }), false);
      assert.equal(await page.locator('.notice-capsule:not([hidden])').count(), 0);
      assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []);
      await context.close();
    }
    console.log(JSON.stringify({ browser: browser.version(), ok: true }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
