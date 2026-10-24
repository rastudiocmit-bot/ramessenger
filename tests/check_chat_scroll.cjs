// Optional real-browser check: npm run test:scroll
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../app/backend/src/center_messenger/web');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => route.abort());
    const html = fs.readFileSync(root + '/index.html', 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
      .replace(/<link\b[^>]*>/g, '');
    await page.setContent(html);
    await page.addStyleTag({ content: fs.readFileSync(root + '/app.css', 'utf8') });
    await page.addStyleTag({ content: '#loading,.sidebar,.topbar{display:none!important} #cabinet{display:block!important} .content{padding:15px!important} .chat-layout{height:760px!important;min-height:0!important} [hidden]{display:none!important}' });
    await page.addScriptTag({ content: fs.readFileSync(root + '/chat.js', 'utf8') });
    await page.evaluate(async () => {
      const nativeTimeout = window.setTimeout.bind(window), timers = new Map(); let timer = 0, seq = 100;
      window.setTimeout = (callback, delay) => { timers.set(++timer, { callback, delay }); return timer; };
      window.clearTimeout = id => timers.delete(id);
      const $ = s => document.querySelector(s), identifier = n => `00000000-0000-0000-0000-${String(n).padStart(12, '0')}`;
      const make = n => { const at = new Date(Date.UTC(2024, 0, 1) + n * 86400000).toISOString();
        return { id: identifier(n), text: `Message ${n}\n` + 'Variable height content '.repeat(n % 14 + 1),
          direction: n % 3 ? 'in' : 'out', actor_type: n === 200 ? 'automation' : 'contact',
          attachments: [], version: 1, occurred_at: at, sort_time: String(Date.parse(at) * 1000),
          crm_order: String(n), state: 'received' }; };
      const rows = Array.from({ length: 200 }, (_, i) => make(i + 1)), deltas = [], calls = [];
      const conv = { id: identifier(1), name: 'Synthetic client', students: [], status: 'archive',
        channel_name: 'AlfaCRM', revision: 1, unread: true, unread_count: 1, can_send: true };
      const state = { user: { id: 'test' }, workspace: { id: 'test', role: 'owner', timezone: 'Europe/Moscow' }, pane: 'messages', epoch: 1 };
      const snapshot = () => { const s = $('#chat-stream'), bounds = s.getBoundingClientRect();
        const el = [...$('#chat-history').children].find(e => e.dataset.id && e.getBoundingClientRect().top > bounds.top + 80 && e.getBoundingClientRect().top < bounds.bottom - 80);
        return el ? { id: el.dataset.id, top: el.getBoundingClientRect().top, scrollTop: s.scrollTop } : null; };
      const api = async url => {
        const parsed = new URL(url, 'https://example.test'); calls.push(parsed.toString());
        if (parsed.pathname.endsWith('/integrations/max')) return { vault_ready: true, channels: [{ id: 'native', name: 'CRM', kind: 'alfa_max_browser' }] };
        if (parsed.pathname.endsWith('/integrations/alfa')) return { branches: [] };
        if (parsed.pathname.endsWith('/chats')) return { conversations: [conv], total: 1, unread: 1, page: 0, has_more: false };
        if (!parsed.pathname.endsWith('/messages')) throw new Error(url);
        const isDelta = parsed.searchParams.has('after_seq'), before = parsed.searchParams.get('before_id');
        let messages, more = false;
        if (isDelta) { messages = deltas.splice(0); seq += 1; }
        else {
          const end = before ? rows.findIndex(r => r.id === before) : rows.length;
          const start = Math.max(0, end - 20); messages = rows.slice(start, end); more = start > 0;
          if (before) { window.scrollTest.pendingAnchor = snapshot(); await new Promise(resolve => nativeTimeout(resolve, 80)); }
        }
        return { conversation: conv, messages, next_seq: seq, has_more: more, more_changes: false, total: rows.length };
      };
      const app = window.CenterChat({ $, state, api, node: (tag, value, className) => { const e = document.createElement(tag);
        if (value !== undefined) e.textContent = value; if (className) e.className = className; return e; },
        show: (e, visible = true) => { e.hidden = !visible; }, notice: (v, e) => { if (e) e.textContent = v; },
        date: v => v, toast: () => {}, confirm: async () => true, renderPane: async () => {} });
      $('#cabinet').hidden = false; $('#pane-messages').hidden = false;
      window.scrollTest = { app, rows, calls, snapshot, identifier,
        insertDelta: () => { const fresh = make(201); rows.push(fresh); deltas.push(rows[0], fresh); },
        poll: async () => { const entry = [...timers.entries()].find(([, t]) => t.delay === 2500);
          if (!entry) throw new Error('Missing poll'); timers.delete(entry[0]); await entry[1].callback(); } };
      await app.enter();
    });
    await page.waitForFunction(() => document.querySelectorAll('.chat-bubble').length === 20);
    const initial = await page.locator('#chat-stream').evaluate(s => ({ height: s.clientHeight, scrollHeight: s.scrollHeight }));
    assert.ok(initial.height > 100 && initial.scrollHeight > initial.height, JSON.stringify(initial));
    await page.locator('#chat-stream').evaluate(s => { s.scrollTop = 350; });
    await page.waitForTimeout(150);
    const before = await page.evaluate(() => scrollTest.snapshot());
    assert.ok(before, 'Need a real visible anchor');
    await page.evaluate(async () => { scrollTest.insertDelta(); await scrollTest.poll(); });
    const after = await page.evaluate(id => document.querySelector(`.chat-bubble[data-id="${id}"]`).getBoundingClientRect().top, before.id);
    assert.ok(Math.abs(after - before.top) <= 1, `Delta moved anchor ${after - before.top}px`);
    await page.locator('#chat-stream').evaluate(s => { s.scrollTop = 260; });
    await page.waitForTimeout(80);
    const box = await page.locator('#chat-stream').boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -150);
    await page.waitForFunction(() => document.querySelectorAll('.chat-bubble').length >= 42);
    const older = await page.evaluate(() => {
      const anchor = scrollTest.pendingAnchor;
      const top = document.querySelector(`.chat-bubble[data-id="${anchor.id}"]`).getBoundingClientRect().top;
      return { drift: top - anchor.top, boundary: new URL(scrollTest.calls.find(c => c.includes('before_id='))).searchParams.get('before_id') };
    });
    assert.ok(Math.abs(older.drift) <= 1, `Older page moved anchor ${older.drift}px`);
    assert.equal(older.boundary, '00000000-0000-0000-0000-000000000181');
    for (let i = 0; i < 15; i++) {
      if (await page.locator('#chat-history-more').isHidden()) break;
      await page.locator('#chat-stream').evaluate(s => { s.scrollTop = 0; });
      await page.mouse.wheel(0, -500);
      await page.waitForTimeout(150);
    }
    const ids = await page.locator('.chat-bubble').evaluateAll(nodes => nodes.map(n => Number(n.dataset.id.slice(-12))));
    assert.deepEqual(ids, Array.from({ length: 201 }, (_, i) => i + 1));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ browser: await browser.version(), messages: ids.length,
      deltaAnchorDrift: after - before.top, wheelAnchorDrift: older.drift, pageBoundary: older.boundary, errors }, null, 2));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
