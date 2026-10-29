// Synthetic APIs only: defaults, recovery, navigation and persistence in the full app.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const data = await fixture(context), page = await context.newPage();
    let unavailable = true;
    await context.route('**/directory/card-loader', route => unavailable
      ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Сервис временно недоступен' }) })
      : route.fallback());
    await page.goto('https://cabinet.example.test/');
    await page.locator('#connections-tab').click();
    await page.locator('#connections-wizard-tab').click();
    await page.waitForSelector('#card-loader-error');
    assert.equal(await page.locator('#card-loader-form [name=finance_interval_minutes]').inputValue(), '30');
    assert.equal(await page.locator('#card-loader-form button[type=submit]').isDisabled(), true);
    unavailable = false; await page.locator('#card-loader-retry').click();
    await page.waitForFunction(() => !document.querySelector('#card-loader-form button[type=submit]').disabled);
    await page.locator('#card-loader-form .advanced').evaluate(element => { element.open = true; });
    await page.locator('#card-loader-form [name=requests_per_turn]').fill('6');
    await page.locator('#card-loader-form button[type=submit]').click();
    await page.waitForFunction(() => /сохранено/.test(document.querySelector('#card-loader-status').textContent));
    assert.equal(data.mutations.at(-1).body.requests_per_turn, 6);
    await page.locator('#connections-settings-tab').click();
    assert.equal(await page.locator('#card-loader-form').isVisible(), false);
    await page.locator('#connections-monitor-tab').focus(); await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#connections-wizard-tab').getAttribute('aria-selected'), 'true');
    await page.waitForFunction(() => !document.querySelector('#card-loader-form button[type=submit]').disabled);
    assert.equal(await page.locator('#card-loader-form [name=requests_per_turn]').inputValue(), '6');
    await page.screenshot({ path: '/tmp/card-loader-wizard-desktop.png', animations: 'disabled' });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.locator('#connections-wizard-view').evaluate(element => element.scrollWidth <= element.clientWidth + 1));
    await page.screenshot({ path: '/tmp/card-loader-wizard-mobile.png', animations: 'disabled' });
    assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []);
    console.log('Card loader wizard: defaults, retry, save, keyboard navigation and mobile layout passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
