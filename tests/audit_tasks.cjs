// Visual audit of the task screens: screenshots + geometry checks (overlaps, overflow, clipping).
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
const out = process.env.CM_AUDIT_DIR || '/tmp/task-audit';
fs.mkdirSync(out, { recursive: true });
const hours = h => new Date(Date.now() + h * 3600e3).toISOString();
const seed = () => [
  { id: 'a1', title: 'Перезвонить про группу «Лего» и уточнить расписание на ноябрь', client_label: 'Ольга Ким', conversation_id: 'client-3', due_at: hours(-2) },
  { id: 'a2', title: 'Записать Мишу на отработку', client_label: 'Анна Смирнова', conversation_id: 'client-0', due_at: hours(1.5), kind: 'makeup',
    sources: [{ id: 'm4', text: 'Спасибо! Запишите нас, пожалуйста, на пробное занятие.', direction: 'in' }] },
  { id: 'a3', title: 'Проверить оплату за ноябрь', client_label: 'Игорь Лебедев', conversation_id: 'client-5', due_at: hours(3) },
  { id: 'a4', title: 'Справка для бассейна', client_label: 'Анна Смирнова', conversation_id: 'client-0', due_at: hours(26) },
  { id: 'a5', title: 'Позвонить по поводу летнего лагеря', client_label: 'Мария Ветрова', conversation_id: 'client-7', due_at: hours(80), assignee_id: 'operator-1', assignee_name: 'Мария Петрова' },
];
const problems = [];
async function geometry(page, label) {
  const found = await page.evaluate(() => {
    const out = [], vis = e => e && !e.hidden && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
    const r = e => e.getBoundingClientRect(), hit = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
    const W = innerWidth, H = innerHeight;
    const pairs = [['#task-strip', '#chat-header'], ['.task-hint', '#chat-compose'], ['.notice-capsule', '#task-strip'], ['.notice-capsule', '#chat-header'],
      ['.task-hint', '#task-strip'], ['.task-pick-bar', '#task-strip']];
    for (const [a, b] of pairs) { const x = document.querySelector(a), y = document.querySelector(b); if (vis(x) && vis(y) && hit(r(x), r(y))) out.push(`${a} overlaps ${b}`); }
    for (const sel of ['#task-strip', '.task-hint', '.task-menu', '.notice-capsule', '.task-pick-bar', '#task-dialog[open]', '#task-done-dialog[open]', '.chat-bubble.task-lifted']) {
      const e = document.querySelector(sel); if (!vis(e)) continue; const b = r(e);
      if (b.left < -1 || b.right > W + 1 || b.top < -1 || b.bottom > H + 1) out.push(`${sel} outside viewport ${JSON.stringify({ l: b.left | 0, r: b.right | 0, t: b.top | 0, b: b.bottom | 0 })}`);
    }
    // Text that is cut without an ellipsis, or children wider than their parent.
    for (const e of document.querySelectorAll('#task-strip *, .task-hint *, .task-row *, .task-sheet *, #task-settings-panel *, #task-done-dialog *, .notice-capsule *, .task-menu *, .row-task, .task-scope *')) {
      if (!vis(e)) continue; const cs = getComputedStyle(e);
      if (e.scrollWidth > e.clientWidth + 2 && cs.overflowX === 'visible' && cs.textOverflow !== 'ellipsis' && !['svg', 'path', 'rect', 'g', 'circle', 'polyline'].includes(e.tagName.toLowerCase()))
        out.push(`overflow ${e.tagName.toLowerCase()}.${(e.className && e.className.baseVal === undefined ? e.className : '').toString().split(' ')[0]} "${(e.textContent || '').trim().slice(0, 30)}"`);
      const p = e.parentElement; if (p && vis(p) && r(e).right > r(p).right + 2 && getComputedStyle(p).overflowX === 'visible') out.push(`sticks out ${e.tagName.toLowerCase()}.${String(e.className).split(' ')[0]} "${(e.textContent || '').trim().slice(0, 24)}"`);
    }
    for (const row of document.querySelectorAll('.chat-row')) {
      const mark = row.querySelector('.row-task'), name = row.querySelector('.row-name'), time = row.querySelector('.row-time');
      if (vis(mark) && name && hit(r(mark), r(name))) out.push('row marker overlaps the name');
      if (vis(mark) && time && hit(r(mark), r(time))) out.push('row marker overlaps the time');
    }
    for (const e of document.querySelectorAll('.task-check, .task-strip-done, .task-hint-make, .task-chip, .task-menu-item, .notice-act')) {
      if (!vis(e)) continue; const b = r(e); if (b.height < 32) out.push(`small target ${e.className.split(' ')[0]} ${b.height | 0}px`);
    }
    return [...new Set(out)];
  });
  for (const f of found) problems.push(`${label}: ${f}`);
}
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] });
  const sizes = [{ name: 'p360', viewport: { width: 360, height: 640 }, phone: true }, { name: 'p390', viewport: { width: 390, height: 844 }, phone: true },
    { name: 'd1280', viewport: { width: 1280, height: 800 }, phone: false }];
  try {
    for (const size of sizes) for (const theme of ['light', 'dark']) {
      const label = `${size.name}-${theme}`;
      const context = await browser.newContext({ viewport: size.viewport, isMobile: size.phone, hasTouch: size.phone, colorScheme: theme, permissions: ['microphone'] });
      await fixture(context, 'owner', undefined, { tasks: seed(), taskAi: true, voice: true });
      const page = await context.newPage(); page.setDefaultTimeout(15000);
      const shot = async name => { await page.waitForTimeout(450); await geometry(page, `${label} ${name}`); await page.screenshot({ path: path.join(out, `${label}-${name}.png`) }); };
      await page.goto('https://cabinet.example.test/?view=messages');
      await page.evaluate(t => { document.documentElement.dataset.theme = t; }, theme);
      await page.waitForSelector('.chat-row .row-task');
      // Dismiss the reminder for the overdue task so the list shows clean.
      await page.waitForTimeout(800);
      await shot('list');
      await page.locator('.chat-select').first().click();
      await page.waitForSelector('#task-strip:not([hidden])');
      await page.evaluate(() => document.querySelectorAll('.notice-capsule .notice-del').forEach(b => b.click()));
      await shot('chat-strip');
      // Menu
      if (size.phone) {
        await page.evaluate(() => { const t = document.querySelector('#chat-history .chat-bubble.msg[data-id="m1"]'), b = t.getBoundingClientRect();
          const init = { pointerType: 'touch', isPrimary: true, pointerId: 9, bubbles: true, clientX: b.left + 20, clientY: b.top + 10 }; t.dispatchEvent(new PointerEvent('pointerdown', init)); window.__p = init; });
        await page.waitForTimeout(700); await page.evaluate(() => document.dispatchEvent(new PointerEvent('pointerup', window.__p)));
      } else await page.locator('#chat-history .chat-bubble.msg[data-id="m1"] .msg-text').click({ button: 'right' });
      await page.waitForSelector('.task-menu:not([hidden])'); await shot('menu');
      await page.locator('.task-menu-item.primary').click();
      await page.waitForSelector('#task-dialog .task-ai-badge:not(.busy)'); await shot('sheet');
      await page.locator('#task-dialog [data-task-close]').click();
      // Hint
      await page.locator('#chat-input').fill('Уточню расписание и напишу');
      // With an open task the capsule asks «Задача выполнена?» instead of a hint.
      await page.locator('#chat-send').click(); await page.waitForTimeout(1800); await shot('after-reply');
      await page.evaluate(() => document.querySelectorAll('.notice-capsule .notice-del, .task-hint-close').forEach(b => { if (b.offsetParent) b.click(); }));
      // Reminder
      await page.evaluate(() => { for (const k of Object.keys(localStorage)) if (k.startsWith('cm:task-reminded')) localStorage.removeItem(k); });
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      // Complete from the strip
      await page.locator('#task-strip .task-strip-done').click(); await page.waitForSelector('#task-done-dialog[open]');
      await page.waitForFunction(() => !document.querySelector('#task-done-dialog .task-done-text').classList.contains('busy')); await shot('done');
      await page.locator('#task-done-dialog .task-done-skip').click(); await page.waitForTimeout(300); await shot('done-capsule');
      // Tasks section
      if (size.phone) { await page.locator('#chat-list-toggle').click(); await page.locator('#tabbar [data-pane="tasks"]').click(); }
      else await page.locator('#navrail [data-pane="tasks"]').click();
      await page.waitForSelector('#pane-tasks .task-row'); await page.evaluate(() => document.querySelectorAll('.notice-capsule .notice-del').forEach(b => b.click()));
      await shot('tasks');
      await page.locator('#pane-tasks .task-closed-toggle').click(); await page.waitForTimeout(300); await shot('tasks-closed');
      await page.locator('#tasks-settings-tab').click(); await page.waitForFunction(() => !document.querySelector('.task-settings-save').disabled); await shot('settings');
      await page.evaluate(() => { const d = document.querySelector('#pane-tasks .scroll'); d.scrollTop = d.scrollHeight; }); await shot('settings-end');
      await context.close();
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(out, 'problems.txt'), problems.join('\n'));
  console.log(problems.length ? problems.join('\n') : 'no geometry problems');
})().catch(error => { console.error(error); process.exit(1); });
