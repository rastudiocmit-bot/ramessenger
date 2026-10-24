// Geometry and editing checks use intercepted synthetic API replies only.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const data = await fixture(context), page = await context.newPage(); page.setDefaultTimeout(10000);
    await page.goto('https://cabinet.example.test/?view=messages'); await page.waitForSelector('.chat-bubble');
    const input = page.locator('#chat-input');
    const geometry = () => page.evaluate(() => {
      const input = document.querySelector('#chat-input'), composer = document.querySelector('#chat-compose');
      const box = input.getBoundingClientRect(), send = document.querySelector('#chat-send').getBoundingClientRect();
      const canvas = input.closest('.canvas').getBoundingClientRect();
      const last = [...document.querySelectorAll('.chat-bubble')].at(-1).getBoundingClientRect();
      return { height: box.height, top: box.top, bottom: box.bottom, internalOverflow: input.scrollHeight-input.clientHeight,
        scrollTop: input.scrollTop, sendVisible: send.top >= canvas.top && send.bottom <= canvas.bottom+1,
        lastVisible: last.bottom <= composer.getBoundingClientRect().top, canvasTop: canvas.top, maxHeight: getComputedStyle(input).maxHeight };
    });
    const short = await geometry();
    await input.fill(Array.from({ length: 12 }, (_, n) => `Предложение ${n+1}.`).join('\n'));
    const desktop = await geometry();
    assert.ok(desktop.height > 128 && desktop.top < short.top); assert.ok(Math.abs(desktop.bottom-short.bottom) < 1);
    assert.ok(desktop.internalOverflow <= 1 && desktop.lastVisible && desktop.sendVisible);
    await page.locator('#profile-tab').click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#messages-mobile-tab').click(); await page.locator('.chat-select').first().click();
    await page.waitForFunction(() => document.querySelector('#app').dataset.view === 'chat');
    await page.waitForFunction(() => document.querySelector('#chat-input').clientHeight >= document.querySelector('#chat-input').scrollHeight-1);
    assert.ok((await geometry()).height > 128, 'Restored draft must keep its full height after a hidden resize');
    await input.fill('Длинный текст, который переносится на новые строки. '.repeat(25));
    await page.waitForFunction(() => document.querySelector('#chat-input').clientHeight >= document.querySelector('#chat-input').scrollHeight-1);
    const mobile = await geometry(); assert.ok(mobile.height > 128 && mobile.internalOverflow <= 1 && mobile.sendVisible);
    await input.fill('Строка сообщения\n'.repeat(80));
    const tall = await geometry(); assert.ok(tall.height > 844 && tall.internalOverflow <= 1 && tall.sendVisible); assert.equal(tall.scrollTop, 0);
    await input.press('Control+Home'); await input.press('Control+End');
    await input.fill('Короткое сообщение'); const shrunk = await geometry();
    assert.ok(shrunk.height < 80 && shrunk.sendVisible && shrunk.internalOverflow <= 1);
    await page.locator('#chat-send').click(); await page.waitForFunction(() => document.querySelector('#chat-input').value === '');
    assert.ok((await geometry()).height < 80);
    assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []);
    assert.equal(data.mutations.filter(c => c.path.endsWith('/messages')).length, 1);
    console.log(JSON.stringify({ browser: browser.version(), desktop, mobile, tall, shrunk, leadCounter: 2, errors: data.errors }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
