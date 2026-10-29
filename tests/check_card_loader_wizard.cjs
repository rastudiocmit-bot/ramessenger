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
    assert.equal(await page.locator('#connections-wizard-tab').getAttribute('aria-selected'), 'true');
    await page.waitForSelector('#card-loader-error');
    assert.equal(await page.locator('#card-loader-form [name=finance_interval_minutes]').inputValue(), '30');
    assert.equal(await page.locator('#card-loader-form button[type=submit]').isDisabled(), true);
    unavailable = false; await page.locator('#card-loader-retry').click();
    await page.waitForFunction(() => !document.querySelector('#card-loader-form button[type=submit]').disabled);
    await page.locator('#card-loader-form .advanced > summary').click();
    await page.locator('#card-loader-form [name=requests_per_turn]').fill('6');
    await page.locator('#card-loader-form button[type=submit]').click();
    await page.waitForFunction(() => /сохранен[оы]/i.test(document.querySelector('#card-loader-status').textContent));
    assert.equal(data.mutations.at(-1).body.requests_per_turn, 6);
    await page.locator('#connections-settings-tab').click();
    assert.equal(await page.locator('#card-loader-form').isVisible(), false);
    await page.locator('#connections-settings-tab').focus(); await page.keyboard.press('ArrowLeft');
    assert.equal(await page.locator('#connections-wizard-tab').getAttribute('aria-selected'), 'true');
    await page.waitForFunction(() => !document.querySelector('#card-loader-form button[type=submit]').disabled);
    assert.equal(await page.locator('#card-loader-form [name=requests_per_turn]').inputValue(), '6');
    // An unsaved edit survives both a tab switch and an external MAX state change.
    await page.locator('#card-loader-form [name=finance_interval_minutes]').fill('75');
    await context.route('**/integrations/alfa-max', route => route.fulfill({ contentType: 'application/json',
      body: JSON.stringify({ configured: true, state: 'reauth_required', vault_ready: true, crm_configured: true, branches: [] }) }));
    await page.locator('#messages-tab').click(); await page.locator('#connections-tab').click();
    assert.equal(await page.locator('#connections-wizard-view').isVisible(), true);
    assert.equal(await page.locator('#card-loader-form [name=finance_interval_minutes]').inputValue(), '75');
    await page.locator('#card-loader-form button[type=submit]').click();
    await page.waitForFunction(() => /Настройки сохранены/.test(document.querySelector('#card-loader-status').textContent));
    await page.reload(); await page.locator('#connections-tab').click(); await page.locator('#connections-wizard-tab').click();
    await page.waitForFunction(() => !document.querySelector('#card-loader-form button[type=submit]').disabled);
    assert.equal(await page.locator('#card-loader-form [name=finance_interval_minutes]').inputValue(), '75');
    await page.locator('#card-loader-form .advanced > summary').click();
    assert.equal(await page.locator('#card-loader-form input:disabled,#card-loader-form select:disabled').count(), 0);
    const switches = await page.locator('.loader-switch').evaluateAll(rows => rows.map(row => {
      const control = row.querySelector('input').getBoundingClientRect(), text = row.querySelector('span').getBoundingClientRect();
      return { width: control.width, height: control.height, horizontal: control.left >= text.right };
    }));
    assert.ok(switches.every(control => control.width === 34 && control.height === 20 && control.horizontal));
    assert.equal(await page.locator('#connections-wizard-view #alfa-form').count(), 1);
    assert.equal(await page.locator('#connections-wizard-view #alfa-max-connect-form').count(), 1);
    assert.equal(await page.locator('#connections-settings-view #alfa-form').count(), 0);
    assert.equal(await page.locator('#wizard-monitor-slot #connection-monitor-content').count(), 1);
    const sizes = await page.locator('#wizard-data-settings,#wizard-native-settings,#wizard-card-settings').evaluateAll(elements => elements.map(el => {
      const box = el.getBoundingClientRect(); return { left: box.left, width: box.width, top: box.top, bottom: box.bottom };
    }));
    assert.ok(sizes.every(box => Math.abs(box.width-sizes[0].width)<1 && Math.abs(box.left-sizes[0].left)<1));
    assert.ok(sizes[0].bottom <= sizes[1].top && sizes[1].bottom <= sizes[2].top);
    const monitorHandle = await page.locator('#connection-monitor-content').elementHandle();
    assert.equal(await page.locator('#connections-monitor-tab,#connections-monitor-view').count(), 0);
    assert.equal(await page.locator('.connection-tabs [role=tab]').count(), 4);
    assert.equal(await page.locator('#connections-wizard-tab').textContent(), 'Мастер синхронизации АльфаСРМ');
    assert.equal(await page.locator('.wizard-page-heading h2').textContent(), 'Мастер синхронизации АльфаСРМ');
    await page.locator('#connections-settings-tab').click();
    assert.equal(await page.locator('#connection-monitor-content').isVisible(), false);
    await page.locator('#connections-wizard-tab').click();
    assert.equal(await monitorHandle.evaluate(el => el === document.querySelector('#wizard-monitor-slot #connection-monitor-content')), true);
    assert.equal(await page.locator('[id=connection-monitor-content]').count(), 1);
    // Restore the connected synthetic service before visual QA.
    await context.unroute('**/integrations/alfa-max');
    await page.locator('#messages-tab').click(); await page.locator('#connections-tab').click();
    await page.waitForFunction(() => document.querySelector('#alfa-max-status').textContent === 'Подключено');
    await page.locator('#wizard-monitor-slot #alfa-max-progress').waitFor({ state: 'visible' });
    const settingsCalls = data.calls.filter(call => call.path.endsWith('/directory/card-loader')).length;
    for (let i=0;i<3;i++) {
      await page.locator('#connections-settings-tab').click();
      await page.locator('#connections-wizard-tab').click();
    }
    assert.equal(data.calls.filter(call => call.path.endsWith('/directory/card-loader')).length, settingsCalls);
    assert.equal(await monitorHandle.evaluate(el => el === document.querySelector('#wizard-monitor-slot #connection-monitor-content')), true);
    // Clear the intentionally triggered reauthentication notice before visual QA.
    for (const button of await page.locator('.notice-capsule:not([hidden]) .notice-del').all()) await button.click();
    await page.locator('#card-loader-form .advanced > summary').click();
    await page.locator('#connections-wizard-view').evaluate(element => element.closest('.scroll').scrollTop = 0);
    await page.screenshot({ path: '/tmp/card-loader-wizard-desktop.png', animations: 'disabled' });
    await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
    await page.screenshot({ path: '/tmp/card-loader-wizard-dark-desktop.png', animations: 'disabled' });
    await page.evaluate(() => document.documentElement.dataset.theme = 'light');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.locator('#connections-wizard-view').evaluate(element => element.scrollWidth <= element.clientWidth + 1));
    await page.screenshot({ path: '/tmp/card-loader-wizard-mobile.png', animations: 'disabled' });
    await page.locator('#card-loader-form [name=finance_interval_minutes]').scrollIntoViewIfNeeded();
    await page.screenshot({ path: '/tmp/card-loader-wizard-mobile-fields.png', animations: 'disabled' });
    await page.locator('#card-loader-form button[type=submit]').scrollIntoViewIfNeeded();
    assert.ok(await page.locator('#card-loader-form button[type=submit]').evaluate(element => {
      const box = element.getBoundingClientRect(); return box.top >= 0 && box.bottom <= innerHeight;
    }));
    await page.screenshot({ path: '/tmp/card-loader-wizard-mobile-save.png', animations: 'disabled' });
    await page.locator('#wizard-monitor-title').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#wizard-monitor-slot #connection-monitor-content').count(), 1);
    assert.ok(await page.locator('#connection-monitor-content').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await page.screenshot({ path: '/tmp/card-loader-wizard-mobile-monitor.png', animations: 'disabled' });
    assert.equal(await page.locator('#connections-settings-view #card-loader-form').count(), 0);
    assert.equal(await page.locator('#connections-wizard-view #card-loader-form').count(), 1);
    assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []);
    console.log('Card loader wizard: defaults, retry, save, keyboard navigation and mobile layout passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
