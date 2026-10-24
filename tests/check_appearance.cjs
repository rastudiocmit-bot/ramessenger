// Whole supplied cabinet: explicit theme, independent list scroll and mobile layout.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: 'dark' });
    const data = await fixture(context), page = await context.newPage(); page.setDefaultTimeout(10000);
    const open = async target => { await target.goto('https://cabinet.example.test/?view=messages'); await target.waitForSelector('.chat-bubble.system'); await target.evaluate(() => document.fonts.ready); };
    const save = async name => { if (process.env.CM_APPEARANCE_SCREENSHOT_DIR) { fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR, { recursive: true }); await page.screenshot({ path: path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR, name + '.png'), animations: 'disabled' }); } };
    await open(page);
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
    assert.equal(await page.locator('.avatar-count').first().textContent(), '1');
    assert.equal(await page.locator('.avatar-letter').first().textContent(), 'АС');
    assert.equal(await page.locator('.avatar-count').first().evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(61, 141, 196)');
    for (const selector of ['.chat-list-scroll', '#chat-stream', '.directory-list-panel'])
      assert.equal(await page.locator(selector).evaluate(e => getComputedStyle(e).scrollbarWidth), 'none');
    const list = page.locator('.chat-list-scroll'), bounds = await list.boundingBox();
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2); await page.mouse.wheel(0, 500);
    await page.waitForFunction(() => document.querySelector('.chat-list-scroll').scrollTop > 0);
    const listWheelScroll = await list.evaluate(e => e.scrollTop); await list.evaluate(e => { e.scrollTop = 0; });
    await save('installed-light-desktop');
    await page.locator('#profile-tab').click(); await page.locator('#profile-theme').check();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    assert.ok(await page.locator('#profile-theme').evaluate(e => e.closest('.item').getBoundingClientRect().height < 100), 'Settings must remain compact horizontal rows');
    await page.locator('#pane-profile>.scroll').evaluate(e => { e.scrollTop = 0; });
    await save('installed-dark-profile');
    await page.locator('#messages-tab').click(); await page.waitForSelector('.chat-bubble'); await save('installed-dark-desktop');
    await page.emulateMedia({ colorScheme: 'light' }); await page.reload(); await page.waitForSelector('.chat-bubble');
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    const other = await context.newPage(); await open(other); await other.locator('#profile-tab').click(); await other.locator('#profile-theme').uncheck();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'light');
    assert.equal(await page.locator('#chat-theme').inputValue(), 'light'); await other.close();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('#app').dataset.view === 'list');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390); await save('installed-light-mobile');
    await page.locator('.chat-select').first().click(); await page.waitForFunction(() => document.querySelector('#app').dataset.view === 'chat');
    assert.equal(await page.locator('#tabbar').isVisible(), false); await save('installed-light-mobile-chat');
    await page.locator('#chat-list-toggle').click(); await page.locator('#more-tab').click();
    await page.locator('#navigation-options [data-pane="profile"]').click(); await page.locator('#profile-theme').check();
    await save('installed-dark-mobile-profile');
    await page.locator('#messages-mobile-tab').click(); await save('installed-dark-mobile');
    assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []); assert.deepEqual(data.mutations, []);
    console.log(JSON.stringify({ browser: browser.version(), defaultLight: true, explicitTheme: true, persistedAfterReload: true,
      updatedOtherTab: true, listWheelScroll, mobileWidth: 390, phoneChat: true, errors: data.errors }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
