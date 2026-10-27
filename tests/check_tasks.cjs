// Tasks from messages: long press menu, create sheet, strip, markers, section, reminders, settings.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
const shots = process.env.CM_TEST_SCREENSHOTS;
const settle = page => page.waitForTimeout(450);

async function longPress(page, selector) {
  await page.evaluate(sel => {
    const target = document.querySelector(sel), box = target.getBoundingClientRect();
    const init = { pointerType: 'touch', isPrimary: true, pointerId: 7, bubbles: true, cancelable: true, clientX: box.left + 20, clientY: box.top + 12, button: 0 };
    target.dispatchEvent(new PointerEvent('pointerdown', init));
    window.__press = init;
  }, selector);
  await page.waitForTimeout(700);
  await page.evaluate(() => document.dispatchEvent(new PointerEvent('pointerup', window.__press)));
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
  const save = async (page, name) => { if (shots) { fs.mkdirSync(shots, { recursive: true }); await page.screenshot({ path: path.join(shots, name + '.png') }); } };
  try {
    // ── phone: long press → menu → sheet → strip, marks, section, completion ──
    {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
      const data = await fixture(context, 'owner', undefined, { voice: true });
      const page = await context.newPage(); page.setDefaultTimeout(15000);
      await page.goto('https://cabinet.example.test/?view=messages');
      await page.locator('.chat-select').first().tap();
      await page.waitForSelector('#chat-history .chat-bubble.msg[data-id="m1"]');
      await settle(page);
      await longPress(page, '#chat-history .chat-bubble.msg[data-id="m1"]');
      await page.waitForSelector('.task-menu:not([hidden])');
      assert.equal(await page.evaluate(() => document.documentElement.classList.contains('voice-holding')), false, 'a long press on a message does not record');
      assert.equal(await page.locator('#voice-hold:not([hidden])').count(), 0, 'no voice layer over the menu');
      assert.match(await page.locator('.task-menu-item').first().innerText(), /Сделать задачей/);
      await save(page, 'tasks-menu');
      await page.locator('.task-menu-item.primary').tap();
      await page.waitForSelector('#task-dialog[open]');
      assert.match(await page.locator('#task-dialog textarea[name="title"]').inputValue(), /подготовку к школе/);
      assert.equal(await page.locator('#task-dialog .task-quote').count(), 1);
      await settle(page); await save(page, 'tasks-sheet');
      await page.locator('#task-dialog .task-chip', { hasText: 'Через 1 ч' }).tap();
      await page.locator('#task-dialog .task-chip', { hasText: 'Отработка' }).tap();
      await page.locator('#task-dialog .task-save').tap();
      await page.waitForSelector('#task-dialog', { state: 'hidden' });
      const created = data.mutations.find(m => m.method === 'POST' && m.path.endsWith('/tasks'));
      assert.deepEqual(created.body.message_ids, ['m1']);
      assert.equal(created.body.conversation_id, 'client-0');
      assert.equal(created.body.origin, 'menu');
      assert.equal(created.body.kind, 'makeup');
      assert.ok(Math.abs(Date.parse(created.body.due_at) - Date.now() - 3600e3) < 120e3, 'due in about an hour');
      await page.waitForSelector('#task-strip:not([hidden])');
      assert.match(await page.locator('#task-strip strong').innerText(), /подготовку к школе/);
      await page.waitForSelector('#chat-history .chat-bubble[data-id="m1"] .task-tag');
      const strip = await page.locator('#task-strip').boundingBox();
      assert.ok(strip.x >= 0 && strip.x + strip.width <= 390, 'strip fits the phone');
      await settle(page); await save(page, 'tasks-strip');
      // The chat list shows a marker on that dialog.
      await page.locator('#chat-list-toggle').tap();
      await page.waitForSelector('.chat-row[data-id="client-0"] .row-task');
      // «Задачи» in the tab bar with a badge; the row completes from its circle.
      const tab = page.locator('#tabbar [data-pane="tasks"]');
      await tab.waitFor(); await page.waitForSelector('#tabbar [data-pane="tasks"] .dot');
      assert.equal(await page.locator('#tabbar [data-pane="tasks"] .dot').innerText(), '1');
      await tab.tap();
      await page.waitForSelector('#pane-tasks:not([hidden]) .task-row');
      assert.match(await page.locator('#pane-tasks .task-group-title').first().innerText(), /(Сегодня|Завтра) · 1/); // «через 1 ч» late in the evening is tomorrow
      await settle(page); await save(page, 'tasks-section');
      await page.locator('#pane-tasks .task-check').first().tap();
      await page.waitForSelector('.notice-capsule:not([hidden]) .notice-title:has-text("Задача выполнена")');
      assert.ok(data.mutations.some(m => m.path.endsWith('/complete')));
      await page.waitForSelector('#pane-tasks .task-empty');
      await settle(page); await save(page, 'tasks-done');
      assert.deepEqual(data.unexpected, []); assert.deepEqual(data.errors, []);
      await context.close();
    }
    // ── an overdue task reminds with actions; «Через 1 ч» snoozes ────────────
    {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
      const data = await fixture(context, 'operator', ['messages', 'security'], { tasks: [
        { id: 'late-1', title: 'Перезвонить про группу «Лего»', client_label: 'Ольга Ким', conversation_id: 'client-3', due_at: new Date(Date.now() - 600e3).toISOString() }] });
      const page = await context.newPage(); page.setDefaultTimeout(15000);
      await page.goto('https://cabinet.example.test/?view=messages');
      await page.waitForSelector('.notice-capsule:not([hidden]) .notice-title:has-text("Пора: Перезвонить")');
      assert.equal(await page.locator('.notice-capsule .notice-act').count(), 3);
      await settle(page); await save(page, 'tasks-reminder');
      await page.locator('.notice-capsule .notice-act', { hasText: 'Через 1 ч' }).tap();
      await page.waitForFunction(() => true);
      await page.waitForTimeout(300);
      const snooze = data.mutations.find(m => m.path.endsWith('/late-1/snooze'));
      assert.ok(snooze && Date.parse(snooze.body.due_at) > Date.now(), 'snoozed into the future');
      // Settings: switching long press to «Везде меню» is saved and applied.
      await page.locator('#tabbar [data-pane="tasks"]').tap();
      await page.locator('#pane-tasks .task-settings').tap();
      await page.waitForSelector('#task-settings-dialog[open]');
      assert.equal(await page.locator('#task-settings-dialog input[name="visibility"]').first().isDisabled(), true, 'operators cannot change studio settings');
      await page.locator('#task-settings-dialog input[name="press"][value="menu"]').check();
      await settle(page); await save(page, 'tasks-settings');
      await page.locator('#task-settings-dialog [type="submit"]').tap();
      await page.waitForSelector('#task-settings-dialog', { state: 'hidden' });
      assert.equal(data.mutations.find(m => m.path.endsWith('/tasks/preferences')).body.press, 'menu');
      assert.ok(!data.mutations.some(m => m.path.endsWith('/tasks/settings')), 'studio settings untouched by an operator');
      assert.equal(await page.evaluate(() => window.document.documentElement && true), true);
      assert.deepEqual(data.unexpected, []); assert.deepEqual(data.errors, []);
      await context.close();
    }
    // ── computer: right click, pick two messages, one task ───────────────────
    {
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const data = await fixture(context, 'owner');
      const page = await context.newPage(); page.setDefaultTimeout(15000);
      await page.goto('https://cabinet.example.test/?view=messages');
      await page.locator('.chat-select').first().click();
      await page.waitForSelector('#chat-history .chat-bubble.msg[data-id="m4"]');
      await page.locator('#chat-history .chat-bubble.msg[data-id="m1"] .msg-text').click({ button: 'right' });
      await page.waitForSelector('.task-menu:not([hidden])');
      await page.locator('.task-menu-item', { hasText: 'Выбрать несколько' }).click();
      await page.locator('#chat-history .chat-bubble.msg[data-id="m4"]').click();
      assert.match(await page.locator('.task-pick-make').innerText(), /Задача из 2 сообщений/);
      await settle(page); await save(page, 'tasks-pick-desktop');
      await page.locator('.task-pick-make').click();
      await page.waitForSelector('#task-dialog[open]');
      assert.equal(await page.locator('#task-dialog .task-quote').count(), 2);
      assert.equal(await page.locator('#task-dialog .task-assignee').isVisible(), true, 'administrators choose the assignee');
      await page.locator('#task-dialog select[name="assignee"]').selectOption('operator-1');
      await page.locator('#task-dialog .task-save').click();
      await page.waitForSelector('#task-dialog', { state: 'hidden' });
      const created = data.mutations.find(m => m.method === 'POST' && m.path.endsWith('/tasks'));
      assert.deepEqual(created.body.message_ids, ['m1', 'm4']); assert.equal(created.body.origin, 'select');
      assert.equal(created.body.assignee_id, 'operator-1');
      await page.waitForSelector('#task-strip:not([hidden])');
      await settle(page); await save(page, 'tasks-desktop');
      assert.deepEqual(data.unexpected, []); assert.deepEqual(data.errors, []);
      await context.close();
    }
    // ── AI: the sheet's draft, the promise hint, the reply draft on completion ──
    {
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const data = await fixture(context, 'owner', undefined, { taskAi: true });
      const page = await context.newPage(); page.setDefaultTimeout(15000);
      await page.goto('https://cabinet.example.test/?view=messages');
      await page.locator('.chat-select').first().click();
      await page.waitForSelector('#chat-history .chat-bubble.msg[data-id="m4"]');
      await page.locator('#chat-history .chat-bubble.msg[data-id="m4"] .msg-text').click({ button: 'right' });
      await page.waitForSelector('.task-menu:not([hidden])');
      assert.equal(await page.evaluate(() => { const b = document.querySelector('.task-menu-backdrop'); return b ? getComputedStyle(b).backgroundColor : 'rgba(0, 0, 0, 0)'; }), 'rgba(0, 0, 0, 0)', 'no shade behind the menu on a computer');
      await page.locator('.task-menu-item.primary').click();
      await page.waitForSelector('#task-dialog .task-ai-badge:not([hidden]):not(.busy)');
      assert.equal(await page.locator('#task-dialog textarea[name="title"]').inputValue(), 'Записать Мишу на пробное занятие, отправить ссылку на оплату');
      assert.match(await page.locator('#task-dialog textarea[name="details"]').inputValue(), /— Отправить ссылку на оплату/);
      // On a computer the sheet floats over the composer at exactly its width, with the page left bright.
      const geo = await page.evaluate(() => { const d = document.querySelector('#task-dialog'), c = document.querySelector('#chat-compose').getBoundingClientRect(), b = d.getBoundingClientRect();
        return { dl: b.left - c.left, dr: b.right - c.right, gap: c.top - b.bottom, top: b.top, bottom: b.bottom, H: innerHeight,
          shade: getComputedStyle(d, '::backdrop').backgroundColor, blur: getComputedStyle(d, '::backdrop').backdropFilter, radius: getComputedStyle(d).borderBottomLeftRadius }; });
      assert.ok(Math.abs(geo.dl) <= 1 && Math.abs(geo.dr) <= 1, `sheet width follows the composer ${JSON.stringify(geo)}`);
      assert.ok(geo.gap >= 0 && geo.top >= 0 && geo.bottom <= geo.H, `sheet sits above the composer inside the window ${JSON.stringify(geo)}`);
      assert.match(geo.shade, /rgba\(0, 0, 0, 0\)|transparent/); assert.ok(!geo.blur || geo.blur === 'none'); assert.equal(geo.radius, '22px');
      assert.equal(await page.locator('#task-dialog .task-chip[aria-pressed="true"]', { hasText: 'Группа' }).count(), 1);
      await settle(page); await save(page, 'tasks-ai-draft');
      await page.locator('#task-dialog [data-task-close]').click();
      // A promise in the answer → «Создать задачу?» above the composer.
      await page.locator('#chat-input').fill('Уточню расписание у педагога и напишу вам');
      await page.locator('#chat-send').click();
      await page.waitForSelector('.task-hint:not([hidden])');
      assert.match(await page.locator('.task-hint small').innerText(), /Уточнить расписание для Миши/);
      await settle(page); await save(page, 'tasks-ai-hint');
      await page.locator('.task-hint-make').click();
      await page.waitForSelector('#task-strip:not([hidden])');
      const created = data.mutations.find(m => m.method === 'POST' && m.path.endsWith('/tasks'));
      assert.equal(created.body.origin, 'ai_hint'); assert.deepEqual(created.body.message_ids, ['m4']);
      // Done from the strip → «Задача выполнена» with the model's reply; the operator sends it.
      await page.locator('#task-strip .task-strip-done').click();
      await page.waitForSelector('#task-done-dialog[open]');
      await page.waitForFunction(() => document.querySelector('#task-done-dialog .task-done-text').value.startsWith('Анна, записали'));
      assert.ok(!data.mutations.some(m => m.path.endsWith('/messages') && m.body?.text?.startsWith('Анна, записали')), 'the reply is never sent by itself');
      await settle(page); await save(page, 'tasks-ai-reply');
      await page.locator('#task-done-dialog .task-done-send').click();
      await page.waitForFunction(() => !document.querySelector('#task-done-dialog').open);
      await page.waitForTimeout(500);
      assert.ok(data.mutations.some(m => m.path.endsWith('/messages') && m.body?.text?.startsWith('Анна, записали')), 'sent after «Отправить»');
      assert.deepEqual(data.unexpected, []); assert.deepEqual(data.errors, []);
      await context.close();
    }
    // ── voice: finger to «Задача» makes a task, not a draft; a reply asks to close it ──
    {
      const voiceBrowser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
        args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
      try {
        const context = await voiceBrowser.newContext({ viewport: { width: 390, height: 844 }, permissions: ['microphone'], isMobile: true, hasTouch: true });
        const data = await fixture(context, 'operator', ['messages'], { voice: true });
        const page = await context.newPage(); page.setDefaultTimeout(15000);
        await page.goto('https://cabinet.example.test/?view=messages');
        await page.locator('.chat-select').first().tap();
        await page.waitForSelector('#chat-history .chat-bubble.msg[data-id="m4"]'); await page.waitForTimeout(600);
        const cdp = await context.newCDPSession(page);
        const touch = (type, point) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: point ? [point] : [] });
        // Beside an incoming message: free background on the right, room to slide left.
        const box = await page.locator('#chat-history .chat-bubble.msg[data-id="m4"]').boundingBox();
        const from = { x: Math.min(380, (box.x + box.width + 390) / 2), y: box.y + box.height / 2 };
        await touch('touchStart', from);
        await page.waitForSelector('#voice-hold[data-phase="recording"]');
        assert.equal(await page.locator('#voice-hold.can-task .voice-hold-task').isVisible(), true, '«Задача» waits on the left');
        await page.waitForTimeout(700);
        for (let step = 1; step <= 6; step++) { await touch('touchMove', { x: from.x - step * 25, y: from.y }); await page.waitForTimeout(30); }
        await page.waitForSelector('#voice-hold[data-phase="task"]');
        await save(page, 'tasks-voice-target');
        await touch('touchEnd');
        await page.waitForSelector('.notice-capsule:not([hidden]) .notice-title:has-text("Задача создана")');
        assert.equal(data.tasks.voice.length, 1); assert.equal(data.tasks.voice[0].conversation_id, 'client-0'); assert.equal(data.tasks.voice[0].create, true);
        assert.equal(await page.locator('#chat-input').inputValue(), '', 'the phrase never lands in the client draft');
        assert.equal(await page.locator('.notice-capsule .notice-act').count(), 2);
        await page.waitForSelector('#task-strip:not([hidden])');
        await page.waitForTimeout(400); await save(page, 'tasks-voice-created');
        // Answer the client: the capsule asks whether the task is done.
        await page.locator('#chat-input').fill('Записали вас на завтра, ждём!');
        await page.locator('#chat-send').tap();
        await page.waitForSelector('.notice-capsule:not([hidden]) .notice-title:has-text("Задача выполнена?")');
        await page.locator('.notice-capsule .notice-act', { hasText: 'Да, выполнена' }).tap();
        await page.waitForFunction(() => document.querySelector('#task-strip').hidden);
        assert.ok(data.mutations.some(m => m.path.endsWith('/complete')));
        assert.deepEqual(data.unexpected, []); assert.deepEqual(data.errors, []);
        await context.close();
      } finally { await voiceBrowser.close(); }
    }
    console.log('tasks: ok');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
