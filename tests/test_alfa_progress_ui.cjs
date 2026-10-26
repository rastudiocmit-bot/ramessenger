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
  const worker = {live:true,phase:'reading',updated_at:new Date().toISOString(),branch_id:'2',direction:'sent'};
  const result = {configured:true,state:'active',syncing:true,messages:82749,conversations:2411,
    backfill_pending:true,history_complete:false,streams:[{branch_id:'2',direction:'sent',source_rows:16974,expected_rows:19499,missing_rows:0,checked_page:848}],...extra};
  result.worker = {...worker,...(extra.worker||{})};return result;
}
function setup(t, handler) {
  const dom = new JSDOM(markup, { runScripts: "outside-only", pretendToBeVisual: true, url: "https://example.test" });
  const { window } = dom, timers = new Map(), calls = [];
  let next = 0, clock = Date.now(); window.Date.now = () => clock;
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
    clock += timer.delay; timer.callback(); await app.start();
  }
  return { app, $, state, calls, timers, poll, window, elapse:ms=>{clock+=ms;} };
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
test("live freshness shows overload and clears it after a successful read", async t => {
  let delayed = true;
  const ui = setup(t, () => report({syncing:false,
    freshness:{delayed_journals:delayed ? 2 : 0, max_lag_seconds:delayed ? 45 : 0, unobserved_journals:0},
    live_cycle:{checked:3, journals:8, failed:0, added_records:1}
  }));
  await ui.app.start();
  assert.equal(ui.$('#alfa-loader-live-badge').textContent, 'Есть задержка');
  assert.match(ui.$('#alfa-loader-live-note').textContent, /45 с.*журналов: 2/);
  assert.match(ui.$('#alfa-loader-live-note').textContent, /Проверено 3 из 8/);
  delayed = false; await ui.poll();
  assert.equal(ui.$('#alfa-loader-live-badge').textContent, 'На связи');
  assert.doesNotMatch(ui.$('#alfa-loader-live-note').textContent, /Задержка проверки/);
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
  assert.match(ui.$("#alfa-max-progress-streams").textContent, /страница.*849/);
  await ui.poll();
  assert.match(ui.$("#alfa-max-progress-streams").textContent, /страница.*850/);
});
test("an unresolved archive gap is paused and never presented as completion", async t => {
  const ui = setup(t, () => report({ backfill_pending: false, history_paused: true, syncing: true,
    streams: [{ branch_id: "4", direction: "sent", source_rows: 672, expected_rows: 684,
      missing_rows: 12, paused_until: "2026-10-24T18:00:00+03:00", retry: 1 }] }));
  await ui.app.start();
  assert.match(ui.$("#alfa-max-progress-warning").textContent,/приостановлена/);
  assert.ok(!ui.$("#alfa-max-progress").classList.contains("is-complete"));
  assert.match(ui.$("#alfa-max-progress-streams").textContent, /без сохранённой записи: 12/i);
  assert.equal(ui.timers.size, 1);
});
test("completion continues periodic checks for later history changes", async t => {
  const ui = setup(t, () => report({ history_complete: true, backfill_pending: false, syncing: true,
    streams: [{ branch_id: "1", direction: "receive", source_rows: 10, expected_rows: 10, complete: true, missing_rows: 0 }] }));
  await ui.app.start();
  assert.equal(ui.$('#alfa-loader-history-badge').textContent,"Сохранена");
  assert.equal([...ui.timers.values()][0].delay, 1000);
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
  const ui = setup(t, () => report({ syncing: true, worker:{phase:"repair"},client_repair: { processed: 60, total: 100, complete: false } }));
  await ui.app.start();
  assert.match(ui.$("#alfa-max-progress-text").textContent, /связи клиентов по ID/);
  assert.match(ui.$('#alfa-loader-repair-note').textContent,/60 из 100/);
  assert.equal(ui.$('#alfa-max-progress-track').getAttribute("aria-valuemax"), "1");
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
  assert.match(ui.$('#alfa-loader-history-alerts').textContent, /Филиал 2.*ожидаем повтор/);
  assert.match(ui.$('#alfa-max-progress-activity').textContent, /страница 4/);
});
test("legacy identities require attention after a scan rather than endless loading", async t => {
  const ui = setup(t, () => report({ syncing: true, backfill_pending: false, unidentified_rows: 3, syncing:false,
    streams: [{ branch_id: '1', direction: 'receive', source_rows: 20, expected_rows: 20, complete: true, unidentified_rows: 3 }] }));
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress').dataset.mode, 'attention');
  assert.match(ui.$('#alfa-max-progress-text').textContent, /История в базе/);
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
  assert.match(ui.$('#alfa-loader-repair-note').textContent, /720.*Общий объём пока неизвестен/);
  assert.equal(ui.$('#alfa-max-progress-track').getAttribute('aria-valuenow'), "0");
  assert.equal(ui.$('#alfa-max-progress-track').getAttribute('aria-valuemax'), "1");
});
test("an expired CRM session remains visible and keeps checking for reconnection", async t => {
  const ui = setup(t, () => report({ state: 'reauth_required' }));
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress').hidden, false);
  assert.equal(ui.$('#alfa-max-progress').dataset.mode, 'error');
  assert.match(ui.$('#alfa-max-progress-text').textContent, /подключение/);
  assert.equal(ui.timers.size, 1);
});

test("light live updates keep saved counters and display exact CRM customer and message IDs safely", async t => {
  let n = 0;
  const ui = setup(t, () => ++n === 1 ? report() : { activity_only: true, configured: true, state: 'active', syncing: true,
    worker: { live: true, updated_at:new Date().toISOString(), phase: 'projecting', operation: 'save_message', record_id: '9007199254740997', position: 4, position_total: 20,
      client: { name: '<img src=x onerror=alert(1)>', cards: [{ customer_id: '7001', branch_id: '2', name: 'Child' }] } } });
  await ui.app.start(); await ui.poll();
  assert.match(ui.calls[1][0], /activity_only=true/);
  assert.equal(ui.$('[data-metric="messages"] strong').dataset.count, '82749');
  assert.match(ui.$('#alfa-max-progress-client-subtitle').textContent, /7001/);
  assert.match(ui.$('#alfa-max-progress-client-facts').textContent, /9007199254740997/);
  assert.equal(ui.$('#alfa-max-progress-client-name').querySelector('img'), null);
  assert.match(ui.$('#alfa-max-progress-client-label').textContent, /Обрабатываем сейчас/);
});
test("selected operation stays open while the current client changes and can return to live view", async t => {
  let name = 'Current A';
  const event = { id: 'entry-1', at: '2026-10-24T16:33:51Z', kind: 'message', client: { name: 'Earlier', cards: [{customer_id:'7',branch_id:'2'}] }, record_id:'100', result:'imported',committed:false };
  const ui=setup(t,()=>report({syncing:true,worker:{live:true,phase:'projecting',operation:'save_message',client:{name,cards:[]},recent:[event]}}));
  await ui.app.start(); ui.$('[data-event="entry-1"]').click();
  assert.equal(ui.$('#alfa-max-progress-client-name').textContent,'Earlier');
  name='Current B';event.committed=true;await ui.poll();
  assert.equal(ui.$('#alfa-max-progress-client-name').textContent,'Earlier');
  assert.match(ui.$('#alfa-max-progress-client-operation').textContent,/сохранено/);
  ui.$('#alfa-max-progress-client-back').click();assert.equal(ui.$('#alfa-max-progress-client-name').textContent,'Current B');
});
test("coverage refresh is time bounded and reset removes all previous customer details", async t => {
  const ui=setup(t,()=>report({worker:{last_client:{name:'Private name',cards:[{customer_id:'77',branch_id:'1'}]}}}));
  await ui.app.start();for(let n=0;n<15;n++)await ui.poll();
  assert.deepEqual(ui.calls.map(c=>c[0].includes('activity_only=true')),[false,...Array(14).fill(true),false]);
  ui.app.reset();assert.doesNotMatch(ui.$('#alfa-max-progress-client').textContent,/Private name|ID 77/);
});
test("a persisted last client is never labelled as the currently running client", async t=>{
  const ui=setup(t,()=>report({syncing:true,worker:{live:false,phase:'repair',client:{name:'Previous',cards:[]}}}));
  await ui.app.start();assert.match(ui.$('#alfa-max-progress-client-label').textContent,/Последний/);
  assert.equal(ui.$('#alfa-max-progress-client-live').dataset.live,'false');
});
test("reading a new page never attributes it to the preceding client and saving has its own phase", async t=>{
  let phase='reading';
  const ui=setup(t,()=>report({syncing:true,worker:{live:true,phase,operation:'read_page',page:22,last_client:{name:'Previous',cards:[{customer_id:'77',branch_id:'1'}]}}}));
  await ui.app.start();assert.equal(ui.$('#alfa-max-progress-client-name').textContent,'Читаем журнал AlfaCRM');
  assert.doesNotMatch(ui.$('#alfa-max-progress-client').textContent,/Previous|ID 77/);
  phase='saving';await ui.poll();assert.match(ui.$('#alfa-max-progress-client-name').textContent,/Подготавливаем пакет/);
});

test("a valid lease with a stale report cannot animate as running", async t=>{
  const ui=setup(t,()=>report({lease_expires_at:new Date(Date.now()+240000).toISOString(),
    worker:{live:true,phase:'saving',updated_at:new Date(Date.now()-180000).toISOString()}}));
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress').dataset.mode,'stale');
  assert.match(ui.$('#alfa-max-progress-warning').textContent,/Нет свежего подтверждения/);
  assert.doesNotMatch(ui.$('#alfa-max-progress-client-facts').textContent,/ID записи CRM/);
});

test("loaded journals retain their result while unresolved legacy IDs need attention", async t=>{
  const ui=setup(t,()=>report({history_loaded:true,syncing:false,backfill_pending:false,unidentified_rows:283,
    streams:Array.from({length:8},(_,i)=>({branch_id:String(i/2|0),direction:i%2?'sent':'receive',complete:true,verification:{status:'legacy'}}))}));
  await ui.app.start();
  assert.equal(ui.$('#alfa-max-progress-value').textContent,'8 из 8 журналов сохранено');
  assert.equal(ui.$('#alfa-max-progress-track').children.length,8);
  assert.equal(ui.$('#alfa-max-progress').dataset.mode,'attention');
  assert.match(ui.$('#alfa-max-progress-warning').textContent,/283/);
});

test("hidden monitor stops requests and resumes with a fresh full snapshot", async t=>{
  const ui=setup(t,()=>report());ui.state.pane='connections';await ui.app.start();
  Object.defineProperty(ui.window.document,'hidden',{value:true,configurable:true});
  ui.window.document.dispatchEvent(new ui.window.Event('visibilitychange'));
  assert.equal(ui.timers.size,0);
  Object.defineProperty(ui.window.document,'hidden',{value:false,configurable:true});
  ui.window.document.dispatchEvent(new ui.window.Event('visibilitychange'));await ui.app.start();
  assert.equal(ui.calls.length,2);assert.equal(ui.calls[1][0].includes('activity_only'),false);
});

function loaderReport(extra = {}) {
  const streams = Array.from({length:8},(_,i)=>({branch_id:String(1+(i/2|0)),direction:i%2?'sent':'receive',
    complete:![3,7].includes(i),source_rows:20,expected_rows:20,verification:{status:![3,7].includes(i)?'verified':'pending'},
    ...(i===7?{error_code:'crm_unavailable',error:'Не удалось открыть страницу',history_retry_at:'2026-10-25T14:33:00Z'}:{})}));
  return report({streams,history_loaded:false,archive_paused:false,
    last_live_cycle:{checked:8,journals:8,failed:0,added_records:7,completed_at:new Date().toISOString()},
    cards:{configured:true,total:52,ready:47,cached:50,queued:5,errors:0,as_of:new Date().toISOString()},
    card_worker:{name:'Card parent',customer_id:'9007199254740997',branch_id:'2',manual:false},
    worker:{flow:'archive',phase:'projecting',operation:'save_message',client:{name:'History parent',cards:[{customer_id:'1248',branch_id:'2'}]},record_id:'38541'},
    ...extra});
}
async function settle(ui) {
  await new Promise(resolve=>setImmediate(resolve));
  await ui.app.start();
  await new Promise(resolve=>setImmediate(resolve));
}
test("real cache counters and current card IDs are separate from history and source records", async t=>{
  const ui=setup(t,()=>loaderReport());
  await ui.app.start();
  assert.match(ui.$('#alfa-loader-cards-value').textContent,/47 из 52 клиентов/);
  assert.match(ui.$('#alfa-loader-cards-current').textContent,/Card parent.*9007199254740997/);
  assert.match(ui.$('#alfa-max-progress-client-name').textContent,/History parent/);
  assert.equal(ui.$('#alfa-loader-live-count').textContent,'7');
  assert.match(ui.$('#alfa-loader-live-count-label').textContent,/записей/);
  assert.equal(ui.$('#alfa-loader-cards-track').getAttribute('aria-valuemax'),'52');
});
test("card work names the exact financial or lesson operation", async t=>{
  let operation='reading_finance';
  const ui=setup(t,()=>{
    const data=loaderReport();data.card_worker.operation=operation;return data;
  });
  await ui.app.start();
  assert.match(ui.$('#alfa-loader-cards-current').textContent,/баланс и абонементы/);
  operation='reading_recent_lessons';await ui.poll();
  assert.match(ui.$('#alfa-loader-cards-current').textContent,/недавние и будущие занятия/);
  operation='reading_lesson_history';await ui.poll();
  assert.match(ui.$('#alfa-loader-cards-current').textContent,/годовая история занятий/);
  operation='reading_customer_groups';await ui.poll();
  assert.match(ui.$('#alfa-loader-cards-current').textContent,/действующие группы клиента/);
  assert.match(ui.$('#alfa-loader-cards-current').textContent,/9007199254740997/);
});
test("active group progress is separate from awaiting-answer card totals and is cleared on reset", async t=>{
  const payload=loaderReport();payload.cards.groups={total:117,ready:111,queued:6,errors:1,unlinked:0};
  const ui=setup(t,()=>payload);await ui.app.start();
  assert.match(ui.$('#alfa-loader-groups-status').textContent,/111 из 117.*в очереди: 6.*повтора: 1/);
  assert.match(ui.$('#alfa-loader-cards-value').textContent,/47 из 52/);
  ui.app.reset();
  assert.equal(ui.$('#alfa-loader-groups-status').textContent,'');
  assert.equal(ui.$('#alfa-loader-groups-status').hidden,true);
});
test("archive pause is a server mutation and retains all completed journals and live checks", async t=>{
  let paused=false;const posts=[];
  const ui=setup(t,(url,options)=>{
    if(options?.method==='POST'){posts.push(JSON.parse(JSON.stringify(options.data)));paused=options.data.archive_paused;return {queued:true,archive_paused:paused};}
    return loaderReport({archive_paused:paused});
  });
  await ui.app.start();ui.$('#alfa-loader-pause').click();await settle(ui);
  assert.deepEqual(posts,[{archive_paused:true}]);
  assert.equal(ui.$('#alfa-loader-pause').textContent,'Продолжить историю');
  assert.match(ui.$('#alfa-max-progress-value').textContent,/6 из 8/);
  assert.equal(ui.$('#alfa-loader-live-count').textContent,'7');
  assert.equal(ui.$('#alfa-loader-cards-badge').textContent,'Обновляются');
  assert.ok([...ui.window.document.querySelectorAll('[data-retry-branch]')].every(b=>b.disabled));
  ui.$('#alfa-loader-pause').click();await settle(ui);
  assert.deepEqual(posts,[{archive_paused:true},{archive_paused:false}]);
  assert.equal(ui.$('#alfa-loader-pause').getAttribute('aria-pressed'),'false');
});
test("retry posts only the selected journal and cannot reset finished history", async t=>{
  const posts=[];let retried=false;
  const ui=setup(t,(url,options)=>{
    if(options?.method==='POST'){posts.push(JSON.parse(JSON.stringify(options.data)));retried=true;return {queued:true,already_running:true,archive_paused:false};}
    const result=loaderReport();
    if(retried){delete result.streams[7].error_code;delete result.streams[7].error;delete result.streams[7].history_retry_at;}
    return result;
  });
  await ui.app.start();ui.$('#alfa-loader-history-alerts [data-retry-branch]').click();await settle(ui);
  assert.deepEqual(posts,[{branch_id:4,direction:'sent'}]);
  assert.match(ui.$('#alfa-max-progress-value').textContent,/6 из 8/);
  assert.equal(ui.$('#alfa-loader-history-alerts').children.length,0);
});
test("an action failure stays visible after restoring the last successful report", async t=>{
  const ui=setup(t,(url,options)=>{if(options?.method==='POST')throw new Error('Сначала восстановите подключение.');return loaderReport();});
  await ui.app.start();ui.$('#alfa-loader-pause').click();await settle(ui);
  assert.match(ui.$('#alfa-max-progress-warning').textContent,/Сначала восстановите/);
  assert.equal(ui.$('#alfa-loader-pause').textContent,'Приостановить историю');
});
test("a revoked permission on a control removes cached private names and stops polling", async t=>{
  const ui=setup(t,(url,options)=>{
    if(options?.method==='POST')throw Object.assign(new Error('Forbidden'),{status:403});
    return loaderReport({worker:{flow:'archive',client:{name:'Secret history',cards:[]}},card_worker:{name:'Secret card',customer_id:'9',branch_id:'1'}});
  });
  await ui.app.start();ui.$('#alfa-loader-pause').click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.doesNotMatch(ui.$('#alfa-max-progress').textContent,/Secret history|Secret card/);
  assert.equal(ui.timers.size,0);
  assert.ok(ui.$('#alfa-max-sync').disabled);
});
test("a light update clears an expired card lease without dropping cached totals", async t=>{
  let count=0;
  const ui=setup(t,()=>++count===1?loaderReport():{activity_only:true,configured:true,state:'active',syncing:false,card_worker:null,worker:{live:false,phase:'waiting'}});
  await ui.app.start();await ui.poll();
  assert.doesNotMatch(ui.$('#alfa-loader-cards-current').textContent,/Card parent/);
  assert.match(ui.$('#alfa-loader-cards-value').textContent,/47 из 52/);
});
test("current operation follows its actual live or archive flow", async t=>{
  let flow='live';
  const ui=setup(t,()=>loaderReport({worker:{flow,phase:'projecting',client:{name:'Current',cards:[]}}}));
  await ui.app.start();assert.ok(ui.$('#alfa-loader-live-operation #alfa-max-progress-client'));
  flow='archive';await ui.poll();assert.ok(ui.$('#alfa-loader-archive-operation #alfa-max-progress-client'));
});
test("no waiting clients has an honest empty cache state without a progress denominator", async t=>{
  const ui=setup(t,()=>loaderReport({cards:{configured:true,total:0,ready:0,cached:0,queued:0},card_worker:null}));
  await ui.app.start();assert.match(ui.$('#alfa-loader-cards-value').textContent,/Нет клиентов/);
  assert.ok(ui.$('#alfa-loader-cards-track').hidden);
  assert.equal(ui.$('#alfa-loader-cards-track').hasAttribute('aria-valuemax'),false);
});
test("status polling preserves keyboard focus on a journal retry", async t=>{
  const ui=setup(t,()=>loaderReport());
  await ui.app.start();
  for(let parent=ui.$('#alfa-max-progress');parent;parent=parent.parentElement)parent.hidden=false;
  ui.$('#alfa-loader-history-alerts [data-retry-branch]').focus();
  assert.equal(ui.window.document.activeElement.dataset.retryBranch,'4');
  await ui.poll();
  assert.equal(ui.window.document.activeElement.dataset.retryBranch,'4');
  assert.equal(ui.window.document.activeElement.dataset.retryDirection,'sent');
});
test("workspace reset clears cache aggregates and progress bars before an unavailable response", async t=>{
  let offline=false;const ui=setup(t,()=>{if(offline)throw new Error('network');return loaderReport();});
  await ui.app.start();ui.state.epoch++;ui.state.workspace={id:'workspace-2'};ui.app.reset();offline=true;
  await ui.app.start();
  assert.equal(ui.$('#alfa-loader-cards-queue').textContent,'');
  assert.equal(ui.$('#alfa-loader-cards-track').hasAttribute('aria-valuenow'),false);
  assert.equal(ui.$('#alfa-loader-cards-fill').style.width,'0%');
  assert.equal(ui.$('#alfa-max-progress-value').textContent,'—');
  assert.doesNotMatch(ui.$('#alfa-max-progress').textContent,/Card parent|History parent|47 из 52/);
});
