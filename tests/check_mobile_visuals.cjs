// Real layout and touch regression. All data and requests are synthetic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');

const nextFrame = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

(async () => {
  const checks = [];
  for (const width of [320, 390, 430, 719, 1440]) for (const theme of ['light', 'dark']) {
    // Launch separately: the supported single-process test Chromium closes with its last context.
    const browser = await chromium.launch({executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
      args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')]});
    try {
      const context = await browser.newContext({viewport: {width, height: width === 320 ? 568 : 900},
        isMobile: width < 720, hasTouch: width < 720, reducedMotion: 'reduce'});
      const data = await fixture(context), page = await context.newPage(); page.setDefaultTimeout(10000);
      data.conversations.forEach(c => { c.seen_checkpoint = {conversation_id: c.id, revision: 1, change_seq: 4, latest_inbound_id: 'm4'}; });
      await page.goto('https://cabinet.example.test/'); await page.waitForSelector('.chat-row');
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; return document.fonts.ready; }, theme);
      const save = async suffix => {
        if (!process.env.CM_MOBILE_VISUAL_SCREENSHOT_DIR) return;
        fs.mkdirSync(process.env.CM_MOBILE_VISUAL_SCREENSHOT_DIR, {recursive: true});
        await page.screenshot({path: path.join(process.env.CM_MOBILE_VISUAL_SCREENSHOT_DIR, `${width}-${theme}-${suffix}.png`), animations: 'disabled'});
      };
      assert.equal(await page.locator('#toast, .toast').count(), 0, 'no global notification overlays');

      if (width < 720) {
        await page.locator('#more-tab').click();
        const menu = await page.evaluate(() => {
          const dialog = document.querySelector('#navigation-dialog'), body = document.querySelector('#navigation-options');
          const r = dialog.getBoundingClientRect(), c = dialog.querySelector('[data-close]').getBoundingClientRect();
          const rows = [...body.children].map(e => {const b = e.getBoundingClientRect(); return {x: b.x, right: b.right, top: b.top, transform: getComputedStyle(e).transform};});
          return {transform: getComputedStyle(body).transform, rightGap: r.right - c.right, bottom: r.bottom, rows};
        });
        assert.equal(menu.transform, 'none'); assert.equal(menu.rows.length, 5);
        assert.ok(menu.rightGap >= 15 && menu.rightGap <= 24, 'Close belongs at the right edge');
        for (const row of menu.rows) {assert.equal(row.transform, 'none'); assert.ok(Math.abs(row.x - menu.rows[0].x) < 1); assert.ok(row.x >= 0 && row.right <= width);}
        await save('menu');
        await page.locator('#navigation-dialog [data-close]').click();
        assert.equal(await page.locator('#navigation-dialog').isVisible(), false);
      }

      const scroller = page.locator('.chat-list-scroll');
      for (const fraction of [1, 0, .7, .2, 1, 0]) {
        await scroller.evaluate((e, fraction) => { e.scrollTop = (e.scrollHeight - e.clientHeight) * fraction; }, fraction);
        await nextFrame(page);
        const layers = await page.evaluate(() => {
          const list = document.querySelector('#chat-list');
          let surface = list;
          while (surface.parentElement && getComputedStyle(surface).backgroundColor === 'rgba(0, 0, 0, 0)') surface = surface.parentElement;
          return {surface: getComputedStyle(surface).backgroundColor,
            rows: [...list.children].filter(e => e.classList.contains('row-wrap')).map(e => ({background: getComputedStyle(e).backgroundColor, action: getComputedStyle(e.querySelector('.row-action')).visibility}))};
        });
        for (const row of layers.rows) {assert.equal(row.background, layers.surface); assert.equal(row.action, 'hidden');}
      }
      // Model a row whose descendants have not been painted yet during fast scrolling.
      await page.locator('.row-wrap').first().evaluate(e => { e.style.contentVisibility = 'hidden'; });
      await save('skipped-row');
      await page.locator('.row-wrap').first().evaluate(e => { e.style.contentVisibility = ''; });

      if (width === 390 && theme === 'light') {
        const session = await context.newCDPSession(page);
        const touch = (type, x, y) => session.send('Input.dispatchTouchEvent', {type,
          touchPoints: ['touchEnd', 'touchCancel'].includes(type) ? [] : [{x, y, id: 1}]});
        const bounds = await page.locator('.chat-row').first().boundingBox(), x = bounds.x + bounds.width - 30, y = bounds.y + 25;
        const handled = () => data.mutations.filter(e => e.path.endsWith('/handled'));
        await touch('touchStart', x, y); await touch('touchMove', x - 2, y + 30); await touch('touchMove', x - 80, y + 100); await touch('touchEnd');
        await nextFrame(page); assert.equal(handled().length, 0);
        assert.equal(await page.locator('.row-wrap.is-swiping').count(), 0);
        await scroller.evaluate(e => { e.scrollTop = 0; }); await nextFrame(page);
        await touch('touchStart', x, y); await touch('touchMove', x - 55, y); await nextFrame(page);
        assert.equal(await page.locator('.row-wrap.is-swiping').count(), 1);
        assert.equal(await page.locator('.row-action').first().evaluate(e => getComputedStyle(e).visibility), 'visible');
        await touch("touchMove", x - 95, y); await nextFrame(page); await save("swipe-armed"); await touch("touchMove", x - 55, y); await nextFrame(page);
        await save("swipe"); await touch("touchCancel"); await nextFrame(page);
        assert.equal(await page.locator('.row-wrap.is-swiping').count(), 0); assert.equal(handled().length, 0);
        await touch('touchStart', x, y); await touch('touchMove', x - 110, y); await nextFrame(page); await touch('touchEnd');
        await page.waitForFunction(() => !document.querySelector('.chat-row[data-id="client-0"] .avatar-count'));
        assert.equal(handled().length, 1); assert.equal(handled()[0].body.seen_checkpoint.change_seq, 4);
        assert.equal(await page.locator('#chat-layout').evaluate(e => e.classList.contains('has-selection')), false);
        await session.detach();
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
      assert.deepEqual(data.errors, []); assert.deepEqual(data.unexpected, []);
      checks.push({width, theme, menu: width < 720, noToasts: true, touch: width === 390 && theme === 'light'});
    } finally { await browser.close(); }
  }
  console.log(JSON.stringify({checks}, null, 2));
})().catch(error => {console.error(error); process.exitCode = 1;});
