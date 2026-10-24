"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const web = path.join(__dirname, "../app/backend/src/center_messenger/web");
const source = fs.readFileSync(path.join(web, "alfa-progress.js"), "utf8");
const markup = fs.readFileSync(path.join(web, "index.html"), "utf8");
function report(extra = {}) {
  return { configured: true, state: "active", messages: 82749, conversations: 2411,
    backfill_pending: true, history_complete: false, streams: [{ branch_id: "2", direction: "sent",
      source_rows: 16974, expected_rows: 19499, missing_rows: 2525, checked_page: 848 }], ...extra };
}
function setup(t, handler) {
  const dom = new JSDOM(markup, { runScripts: "outside-only", url: "https://example.test" });
  const { window } = dom, timers = new Map(), calls = [];
  let next = 0;
  window.setTimeout = (callback, delay) => { timers.set(++next, { callback, delay }); return next; };
  window.clearTimeout = id => timers.delete(id);
  window.eval(source);
  const $ = selector => window.document.querySelector(selector);
  const state = { epoch: 1, workspace: { id: "workspace-1" } };
  const app = window.CenterAlfaProgress({ $, state, api: async (...args) => {
    calls.push(args); return handler(...args);
  }, show: (target, visible = true) => { target.hidden = !visible; }, date: value => value });
  t.after(() => { app.reset(); dom.window.close(); });
  async function poll() {
    assert.equal(timers.size, 1, "exactly one next request must be scheduled");
    const [id, timer] = [...timers][0]; timers.delete(id);
    timer.callback(); await app.start();
  }
  return { app, $, state, calls, timers, poll };
}
test("503 preserves counters, visibly pauses animation, and automatically recovers", async t => {
  let count = 0;
  const ui = setup(t, () => {
    count += 1;
    if (count === 2) throw Object.assign(new Error("unavailable"), { status: 503 });
    return report({ messages: count === 3 ? 82770 : 82749 });
  });
  await ui.app.start(); await ui.poll();
  assert.ok(ui.$("#alfa-max-progress").classList.contains("is-offline"));
  assert.match(ui.$("#alfa-max-progress-detail").textContent, /82749.*Повторяем запрос через 2 с/s);
  await ui.poll();
  assert.equal(ui.$('[data-metric="messages"] strong').dataset.count, "82770");
  assert.ok(!ui.$("#alfa-max-progress").classList.contains("is-offline"));
});
test("repeated failures use bounded backoff and keep retrying", async t => {
  const ui = setup(t, () => { throw new Error("network"); });
  await ui.app.start();
  const delays = [];
  for (let i = 0; i < 7; i++) { delays.push([...ui.timers.values()][0].delay); await ui.poll(); }
  assert.deepEqual(delays, [2000, 4000, 8000, 16000, 30000, 30000, 30000]);
});
test("archive movement stays visible when message count does not grow", async t => {
  let page = 848;
  const ui = setup(t, () => report({ streams: [{ branch_id: "2", direction: "sent",
    source_rows: 16974, expected_rows: 19499, checked_page: page++ }] }));
  await ui.app.start();
  assert.match(ui.$("#alfa-max-progress-streams").textContent, /страница 849/);
  await ui.poll();
  assert.match(ui.$("#alfa-max-progress-streams").textContent, /страница 850/);
});
test("an unresolved archive gap is paused and never presented as completion", async t => {
  const ui = setup(t, () => report({ backfill_pending: false, history_paused: true, syncing: true,
    streams: [{ branch_id: "4", direction: "sent", source_rows: 672, expected_rows: 684,
      missing_rows: 12, paused_until: "2026-10-24T18:00:00+03:00", retry: 1 }] }));
  await ui.app.start();
  assert.ok(ui.$("#alfa-max-progress").classList.contains("is-paused"));
  assert.ok(!ui.$("#alfa-max-progress").classList.contains("is-complete"));
  assert.match(ui.$("#alfa-max-progress-streams").textContent, /недостача 12/i);
  assert.equal(ui.timers.size, 1);
});
test("completion continues periodic checks for later history changes", async t => {
  const ui = setup(t, () => report({ history_complete: true, backfill_pending: false, syncing: true,
    streams: [{ branch_id: "1", direction: "receive", source_rows: 10, expected_rows: 10, complete: true, missing_rows: 0 }] }));
  await ui.app.start();
  assert.ok(ui.$("#alfa-max-progress").classList.contains("is-complete"));
  assert.equal([...ui.timers.values()][0].delay, 15000);
});
test("concurrent refresh calls share one in-flight request", async t => {
  let finish;
  const ui = setup(t, () => new Promise(resolve => { finish = resolve; }));
  const first = ui.app.start(), second = ui.app.start();
  assert.equal(ui.calls.length, 1);
  finish(report()); await Promise.all([first, second]);
  assert.equal(ui.timers.size, 1);
  assert.equal(ui.calls[0][1].timeoutMs, 15000);
});
test("workspace switch discards stale results and cannot revive polling", async t => {
  let finish;
  const ui = setup(t, () => new Promise(resolve => { finish = resolve; }));
  const pending = ui.app.start();
  ui.state.epoch += 1; ui.state.workspace = { id: "workspace-2" }; ui.app.reset();
  finish(report()); await pending;
  assert.ok(ui.$("#alfa-max-progress").hidden);
  assert.equal(ui.timers.size, 0);
});
test("disconnect stops polling and hides stale progress", async t => {
  const ui = setup(t, () => ({ configured: false, state: "not_configured" }));
  await ui.app.start();
  assert.ok(ui.$("#alfa-max-progress").hidden);
  assert.equal(ui.timers.size, 0);
});
test("leaving connections prevents status polling while reading messages", async t => {
  const ui = setup(t, () => report());
  await ui.app.start();
  ui.state.pane = "messages";
  await ui.poll();
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.timers.size, 0);
  assert.ok(ui.$("#alfa-max-progress").hidden);
});
test("saved-history repair displays its checkpoint rather than a stalled archive count", async t => {
  const ui = setup(t, () => report({ syncing: true, client_repair: { processed: 60, total: 100, complete: false } }));
  await ui.app.start();
  assert.match(ui.$("#alfa-max-progress-text").textContent, /Восстанавливаем историю клиентов/);
  assert.equal(ui.$("#alfa-max-progress-track").getAttribute("aria-valuenow"), "60");
  assert.equal(ui.timers.size, 1);
});
test("visible messages and retained database records have separate counters", async t => {
  const ui = setup(t, () => report({ messages: 6, stored_messages: 32 }));
  await ui.app.start();
  assert.equal(ui.$('[data-metric="messages"] strong').dataset.count, "6");
  assert.equal(ui.$('[data-metric="stored"] strong').dataset.count, "32");
});

test("partial pause stays visible while independent journals continue", async t => {
  const ui = setup(t, () => report({ syncing: true, history_paused: true, backfill_pending: true,
    streams: [{ branch_id: '1', direction: 'receive', source_rows: 10, expected_rows: 20, checked_page: 2 },
      { branch_id: '2', direction: 'sent', source_rows: 10, expected_rows: 12, paused_until: '2026-10-24T18:00:00Z', missing_rows: 2 }],
    worker: { phase: 'reading', branch_id: '1', direction: 'receive', page: 3, completed_pages: 1, total_pages: 5 } }));
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress').dataset.mode, 'running');
  assert.match(ui.$('#alfa-max-progress-warning').textContent, /На паузе журналов: 1/);
  assert.match(ui.$('#alfa-max-progress-activity').textContent, /страница 4/);
});
test("legacy identities require attention after a scan rather than endless loading", async t => {
  const ui = setup(t, () => report({ syncing: true, backfill_pending: false, unidentified_rows: 3,
    streams: [{ branch_id: '1', direction: 'receive', source_rows: 20, expected_rows: 20, complete: true, unidentified_rows: 3 }] }));
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress').dataset.mode, 'attention');
  assert.match(ui.$('#alfa-max-progress-text').textContent, /Обход завершён/);
  assert.equal(ui.$('#alfa-max-progress').classList.contains('is-complete'), false);
});
test("worker page heartbeat moves while saved message totals stay unchanged", async t => {
  let page = 24;
  const ui = setup(t, () => report({ syncing: true, worker: { phase: 'reading', branch_id: '2', direction: 'sent', page: page++, completed_pages: 2, total_pages: 8 } }));
  await ui.app.start(); assert.match(ui.$('#alfa-max-progress-activity').textContent, /страница 25/);
  await ui.poll(); assert.match(ui.$('#alfa-max-progress-activity').textContent, /страница 26/);
  assert.equal(ui.$('[data-metric="messages"] strong').dataset.count, '82749');
});
test("a current timeout abort retries instead of silently stopping status polling", async t => {
  const ui = setup(t, () => { throw Object.assign(new Error('timeout'), { name: 'AbortError' }); });
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress').dataset.mode, 'offline');
  assert.equal(ui.timers.size, 1);
});
test("unknown repair size never invents a percentage", async t => {
  const ui = setup(t, () => report({ client_repair: { processed: 720, complete: false } }));
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress-track').hasAttribute('aria-valuenow'), false);
  assert.match(ui.$('[data-sync-step="repair"] small').textContent, /720/);
});
test("an expired CRM session remains visible and keeps checking for reconnection", async t => {
  const ui = setup(t, () => report({ state: 'reauth_required' }));
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress').hidden, false);
  assert.equal(ui.$('#alfa-max-progress').dataset.mode, 'error');
  assert.match(ui.$('#alfa-max-progress-text').textContent, /подключение/);
  assert.equal(ui.timers.size, 1);
});
