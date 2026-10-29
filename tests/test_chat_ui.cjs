"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const web = path.join(__dirname, "../app/backend/src/center_messenger/web");
const source = fs.readFileSync(path.join(web, "chat.js"), "utf8");
const markup = fs.readFileSync(path.join(web, "index.html"), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
const flush = async () => { for (let i = 0; i < 8; i++) await tick(); };
const identifier = n => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
function message(n, extra = {}) {
  return { id: identifier(n), direction: "in", actor_type: "contact", text: `Message ${n}`,
    occurred_at: "2026-10-24T00:00:00+00:00", sort_time: "1792800000000000",
    crm_order: String(n), version: 1, attachments: [], state: "received", ...extra };
}
function conversation(n) {
  return { id: identifier(n), name: `Parent ${n}`, students: [], status: "client", unread: true,
    unread_count: 1, last_message_at: "2026-10-24T00:00:00Z", preview: "Message",
    channel_name: "AlfaCRM", can_send: true, inbound_seq: 1, revision: 1 };
}
function history(id, messages = [], extra = {}) {
  return { conversation: { ...conversation(1), id }, messages, next_seq: 100,
    has_more: false, more_changes: false, removed_ids: [], ...extra };
}
function setup(t, handler, { conversations = [conversation(1)], autoFrames = false } = {}) {
  const dom = new JSDOM(markup, { url: "https://messenger.example.test", runScripts: "outside-only",
    pretendToBeVisual: true });
  const { window } = dom, timers = new Map(), calls = [];
  let timerId = 0, closed = false;
  window.setTimeout = (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; };
  window.clearTimeout = id => timers.delete(id);
  window.requestAnimationFrame = callback => autoFrames ? setImmediate(() => { if (!closed) callback(); }) : 0;
  window.matchMedia = () => ({ matches: true });
  window.eval(source);
  const $ = selector => window.document.querySelector(selector);
  const state = { user: { id: "operator" }, workspace: { id: "test", role: "owner", timezone: "Europe/Moscow" },
    pane: "messages", epoch: 1 };
  const api = async (url, options) => {
    const parsed = new URL(url, window.location.origin);
    calls.push(parsed);
    const result = handler && await handler(parsed, options);
    if (result !== undefined) return result;
    if (parsed.pathname.endsWith("/chats/context")) return { channels: [
      { id: "native", name: "AlfaCRM", kind: "alfa_max_browser" },
      { id: "bot", name: "Bot", kind: "max_bot" }], branches: [], can_write: true };
    if (parsed.pathname.endsWith("/integrations/max")) return { vault_ready: true, channels: [
      { id: "native", name: "AlfaCRM", kind: "alfa_max_browser" },
      { id: "bot", name: "Bot", kind: "max_bot" }] };
    if (parsed.pathname.endsWith("/integrations/alfa")) return { branches: [] };
    if (parsed.pathname.endsWith("/chats")) return { conversations,
      page: Number(parsed.searchParams.get("through_page") || parsed.searchParams.get("page") || 0),
      total: conversations.length, unread: conversations.length, has_more: false };
    if (parsed.pathname.endsWith("/messages")) return history(parsed.pathname.split("/").at(-2));
    throw new Error(`Unexpected request: ${url}`);
  };
  const app = window.CenterChat({ $, state, api,
    node: (tag, value, className) => {
      const element = window.document.createElement(tag);
      if (value !== undefined) element.textContent = value;
      if (className) element.className = className;
      return element;
    },
    show: (element, visible = true) => { element.hidden = !visible; },
    notice: (value, element) => { if (element) element.textContent = value; },
    toast: () => {}, date: value => value, confirm: async () => true, renderPane: async () => {} });
  t.after(() => { closed = true; app.reset(); dom.window.close(); });
  const poll = async () => {
    const entry = [...timers.entries()].find(([, timer]) => timer.delay === 2500);
    assert.ok(entry, "chat poll must be scheduled");
    timers.delete(entry[0]);
    return entry[1].callback();
  };
  const ids = () => [...$("#chat-history").querySelectorAll(".chat-bubble")].map(node => node.dataset.id);
  return { app, $, window, calls, poll, ids, timers, state };
}

test("enter keeps all channels selected", async t => {
  const ui = setup(t);
  await ui.app.enter();
  assert.equal(ui.$("#chat-channel").value, "");
  assert.ok(!ui.calls.find(url => url.pathname.endsWith("/chats")).searchParams.has("connection"));
});

test("child-first presentation persists through context and keeps identities, counters and drafts", async t=>{
  let preference='parent_first';const parent='Мария Иванова',child='Анна Иванова';
  const ui=setup(t,(url,options)=>{
    if(url.pathname.endsWith('/chats/context'))return {channels:[],branches:[],preferences:{name_order:preference}};
    if(url.pathname.endsWith('/preferences')){preference=options.data.name_order;return {name_order:preference};}
  },{conversations:[{...conversation(1),name:parent,parent_name:parent,students:[child],children:[{id:'child-1',name:child}]}]});
  await ui.app.enter();ui.$('#chat-input').value='Черновик';
  ui.$('#chat-name-order').value='child_first';ui.$('#chat-name-order').dispatchEvent(new ui.window.Event('change'));await flush();
  assert.equal(ui.$('.row-name').textContent,child);assert.equal(ui.$('.row-kids').textContent,parent);
  assert.equal(ui.$('.avatar-count').textContent,'1');assert.equal(ui.$('.chat-row').dataset.id,identifier(1));
  assert.equal(ui.$('#chat-input').value,'Черновик');
  await ui.app.enter();assert.equal(ui.$('.row-name').textContent,child);
});

test("name presentation has deterministic multiple children and an honest missing-parent fallback", async t=>{
  const ui=setup(t,url=>url.pathname.endsWith('/chats/context')?{channels:[],branches:[],preferences:{name_order:'child_first'}}:undefined,
    {conversations:[{...conversation(1),parent_name:'Родитель',children:[{id:'2',name:'Яна'},{id:'1',name:'Анна'}]},
      {...conversation(2),name:'Известное имя',parent_name:null,children:[{id:'3',name:'Ирина'}]}]});
  await ui.app.enter();const rows=[...ui.window.document.querySelectorAll('.chat-row')];
  assert.equal(rows[0].querySelector('.row-name').textContent,'Анна · Яна');
  assert.equal(rows[1].querySelector('.row-name').textContent,'Ирина');
  assert.equal(rows[1].querySelector('.row-kids').textContent,'Родитель не указан');
});

test("dialog list keeps unread first and newest first in each group after a live update", async t => {
  let rows = [
    {...conversation(1), unread:false, unread_count:0, last_message_at:'2026-10-24T12:00:00Z'},
    {...conversation(2), last_message_at:'2026-10-24T10:00:00Z',waiting_since:'2026-10-20T09:00:00Z'},
    {...conversation(3), unread:false, unread_count:0, last_message_at:'2026-10-24T11:00:00Z'},
    {...conversation(4), last_message_at:'2026-10-24T09:00:00Z'}];
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/chats'))return {conversations:rows,page:0,total:4,unread:2,has_more:false};
    if(url.pathname.endsWith('/messages'))return history(url.pathname.split('/').at(-2),[],{conversation:{id:url.pathname.split('/').at(-2),unread:true,can_send:true,inbound_seq:1,revision:1}});
  });
  const ids=()=>[...ui.$('#chat-list').querySelectorAll('[data-id]')].map(e=>e.dataset.id);
  await ui.app.enter();assert.deepEqual(ids(),[2,4,1,3].map(identifier));
  assert.equal(ui.$(`[data-id="${identifier(2)}"] time`).dateTime,'2026-10-24T10:00:00Z');
  assert.doesNotMatch(ui.$(`[data-id="${identifier(2)}"] time`).textContent,/ждёт/);
  rows=rows.map(r=>r.id===identifier(4)?{...r,last_message_at:'2026-10-24T13:00:00Z'}:r);
  await ui.poll();assert.deepEqual(ids(),[4,2,1,3].map(identifier));
});

test("dialog sorting preserves microseconds and puts unknown dates last within their group", async t=>{
  const ui=setup(t,undefined,{conversations:[{...conversation(1),last_message_sort_time:'1792800000000000'},
    {...conversation(2),last_message_sort_time:'1792800000000001'}, {...conversation(3),last_message_at:null}]});
  await ui.app.enter();
  assert.deepEqual([...ui.$('#chat-list').querySelectorAll('[data-id]')].map(e=>e.dataset.id),[2,1,3].map(identifier));
});

test("handling a dialog immediately moves it below unread dialogs and refreshes the loaded prefix", async t=>{
  let reads=0,release;
  const rows=[{...conversation(1),last_message_at:'2026-10-24T12:00:00Z'},
    {...conversation(2),last_message_at:'2026-10-24T10:00:00Z'},
    {...conversation(3),unread:false,unread_count:0,last_message_at:'2026-10-24T09:00:00Z'}];
  const ui=setup(t,(url,options)=>{
    if(url.pathname.endsWith('/handled'))return {conversation:{...rows[0],unread:false,unread_count:0,revision:2}};
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{id:identifier(1),unread:true,can_send:true,inbound_seq:1,revision:1}});
    if(url.pathname.endsWith('/chats')){
      const response={conversations:rows,page:1,total:3,unread:reads?1:2,has_more:false};
      if(++reads>1){assert.equal(url.searchParams.get('through_page'),'1');return new Promise(resolve=>{release=()=>resolve(response);});}
      return response;
    }
  });
  await ui.app.enter();ui.$('#chat-handled').click();await flush();
  assert.deepEqual([...ui.$('#chat-list').querySelectorAll('[data-id]')].map(e=>e.dataset.id),[2,1,3].map(identifier));
  assert.equal(ui.$(`[data-id="${identifier(1)}"] .avatar-count`),null);
  release();await flush();
});

test("unread count uses a badge inside the avatar and preserves the client initial", async t => {
  const ui = setup(t, undefined, { conversations: [conversation(1), { ...conversation(2), unread_count: 150 },
    { ...conversation(3), unread: false, unread_count: 0 }] });
  await ui.app.enter();
  const avatars = [...ui.window.document.querySelectorAll("#chat-list .contact-avatar")];
  assert.equal(avatars[0].querySelector(".avatar-letter").textContent, "P1");
  assert.equal(avatars[0].querySelector(".avatar-count").textContent, "1");
  assert.equal(avatars[1].querySelector(".avatar-count").textContent, "99");
  assert.match(avatars[1].getAttribute("aria-label"), /150/);
  assert.equal(avatars[2].querySelector(".avatar-count"), null);
  assert.equal(ui.window.document.querySelectorAll("#chat-list .avatar-count").length, 2);
});

test("big CRM keys and sub-millisecond times render without precision loss", async t => {
  const ui = setup(t, url => url.pathname.endsWith("/messages") ? history(identifier(1), [
    message(3, { sort_time: "1792800000000001", crm_order: "1" }),
    message(1, { crm_order: "9007199254740993" }),
    message(2, { crm_order: "9007199254740992" })]) : undefined);
  await ui.app.enter();
  assert.deepEqual(ui.ids(), [identifier(2), identifier(1), identifier(3)]);
});

test("one list has no queue tabs and legacy view does not create hidden filters", async t => {
  const ui = setup(t);
  await ui.app.enter();
  assert.equal(ui.$("#chat-chips"), null);
  assert.equal(ui.$("[data-chat-view]"), null);
  assert.equal(ui.$("#chat-chip-lead"), null);
  ui.app.setView("lead"); await flush();
  assert.equal(ui.$("#chat-status").value, "");
  assert.ok(ui.calls.filter(url => url.pathname.endsWith("/chats")).every(url => !url.searchParams.has("view") && !url.searchParams.has("status")));
  ui.$("#chat-status").value = "lead";
  ui.app.setView("all"); await flush();
  assert.equal(ui.$("#chat-status").value, "lead");
  assert.equal(ui.$("#chat-filter-chip").closest("#pane-profile") !== null, true);
});

test("older history cursor uses the same order as the rendered stream", async t => {
  let cursor;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    if (url.searchParams.has("before_id")) { cursor = url.searchParams.get("before_id"); return history(identifier(1)); }
    return history(identifier(1), [message(1, { crm_order: "20" }), message(2, { crm_order: "10" })], { has_more: true });
  });
  await ui.app.enter();
  ui.$("#chat-stream").dispatchEvent(new ui.window.Event("scroll"));
  await flush();
  assert.equal(cursor, identifier(2));
});

test("poll waits for older history and preserves the live change cursor", async t => {
  let releaseOlder, olderStarted = false, deltaStarted = false;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    if (url.searchParams.has("before_id")) {
      olderStarted = true;
      return new Promise(resolve => { releaseOlder = () => resolve(history(identifier(1), [message(1)], { next_seq: 999 })); });
    }
    if (url.searchParams.has("after_seq")) {
      deltaStarted = true;
      assert.equal(url.searchParams.get("after_seq"), "100");
      return history(identifier(1), [message(3)], { next_seq: 101 });
    }
    return history(identifier(1), [message(2)], { has_more: true });
  });
  await ui.app.enter();
  ui.$("#chat-stream").dispatchEvent(new ui.window.Event("scroll"));
  await flush();
  assert.ok(olderStarted);
  const polling = ui.poll();
  await flush();
  assert.equal(deltaStarted, false);
  releaseOlder();
  await polling;
  assert.deepEqual(ui.ids(), [identifier(1), identifier(2), identifier(3)]);
});

test("delta removes a superseded bubble from an already open chat", async t => {
  const ui = setup(t, url => url.pathname.endsWith("/messages") ?
    (url.searchParams.has("after_seq") ? history(identifier(1), [], { next_seq: 101, removed_ids: [identifier(2)] }) :
      history(identifier(1), [message(1), message(2)])) : undefined);
  await ui.app.enter();
  await ui.poll();
  assert.deepEqual(ui.ids(), [identifier(1)]);
});

test('a partial CRM delta never hides a sent bubble before its replacement arrives',async t=>{
  let failed=false;
  const original=message(20,{direction:'out',actor_type:'human',state:'accepted',request_id:identifier(90)});
  const replacement=message(21,{direction:'out',actor_type:'human',state:'accepted',request_id:identifier(90)});
  const ui=setup(t,url=>{
    if(!url.pathname.endsWith('/messages'))return;
    const cursor=url.searchParams.get('after_seq');
    if(cursor==='100')return history(identifier(1),[],{next_seq:101,more_changes:true,removed_ids:[original.id]});
    if(cursor==='101'){
      if(!failed){failed=true;throw new Error('History read interrupted');}
      return history(identifier(1),[replacement],{next_seq:102});
    }
    return history(identifier(1),[original]);
  });
  await ui.app.enter();assert.deepEqual(ui.ids(),[original.id]);
  await ui.poll();assert.deepEqual(ui.ids(),[original.id]);
  await ui.app.enter();assert.deepEqual(ui.ids(),[replacement.id]);
});

test("refreshing loaded list pages removes conversations that left the filter", async t => {
  const full = Array.from({ length: 60 }, (_, index) => conversation(index + 1));
  let refresh = false;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/chats")) return;
    const page = Number(url.searchParams.get("through_page") || url.searchParams.get("page") || 0);
    const records = refresh ? full.slice(0, -1) : full;
    const conversations = url.searchParams.has("through_page") ? records.slice(0, (page + 1) * 30) : records.slice(page * 30, (page + 1) * 30);
    return { conversations, page, total: records.length, unread: records.length, has_more: (page + 1) * 30 < records.length };
  });
  await ui.app.enter();
  ui.$(".chat-list-scroll").dispatchEvent(new ui.window.Event("scroll"));
  await flush();
  assert.equal(ui.$("#chat-list").children.length, 60);
  refresh = true;
  await ui.poll();
  assert.equal(ui.$("#chat-list").children.length, 59);
  assert.equal(ui.$(`#chat-list [data-id="${identifier(60)}"]`), null);
});

test("a delayed response cannot replace the newly selected conversation", async t => {
  let releaseFirst;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    const id = url.pathname.split("/").at(-2);
    if (id === identifier(1)) return new Promise(resolve => { releaseFirst = () => resolve(history(id, [message(1)])); });
    return history(id, [message(2)]);
  }, { conversations: [conversation(1), conversation(2)] });
  const entering = ui.app.enter();
  await flush();
  ui.$(`#chat-list [data-id="${identifier(2)}"] .chat-select`).click();
  await flush();
  releaseFirst();
  await entering;
  assert.deepEqual(ui.ids(), [identifier(2)]);
});

test("CRM attachment names display as text", async t => {
  const ui = setup(t, url => url.pathname.endsWith("/messages") ? history(identifier(1), [
    message(1, { attachments: ["file"], attachment_names: ["schedule.pdf"] })]) : undefined);
  await ui.app.enter();
  assert.match(ui.$(".chat-attachment").textContent, /^schedule\.pdf/);
});

test("merged branch chat redirects to the parent chat without losing a draft", async t => {
  let merged = false;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    const id = url.pathname.split("/").at(-2);
    return history(merged ? identifier(2) : id, [message(1)]);
  });
  await ui.app.enter();
  ui.$("#chat-input").value = "My draft";
  ui.$("#chat-input").dispatchEvent(new ui.window.Event("input"));
  merged = true;
  await ui.poll();
  assert.equal(ui.$("#chat-input").value, "My draft");
  assert.ok(ui.calls.some(url => url.pathname.endsWith(`/${identifier(2)}/messages`)));
});

test("a restored server cursor replaces stale cached messages", async t => {
  let delta = false;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    if (url.searchParams.has("after_seq")) {
      if (delta) {
        assert.equal(url.searchParams.get("after_seq"), "4");
        return history(identifier(1), [], { next_seq: 4 });
      }
      delta = true;
      return history(identifier(1), [message(3)], { next_seq: 4, reset: true });
    }
    return history(identifier(1), [message(1), message(2)]);
  });
  await ui.app.enter();
  await ui.poll();
  assert.deepEqual(ui.ids(), [identifier(3)]);
  await ui.poll();
});

test("both existing drafts survive a branch chat merge", async t => {
  let merged = false;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    const id = url.pathname.split("/").at(-2);
    return history(merged ? identifier(1) : id, [message(1)]);
  }, { conversations: [conversation(1), conversation(2)] });
  await ui.app.enter();
  ui.$("#chat-input").value = "First draft";
  ui.$("#chat-input").dispatchEvent(new ui.window.Event("input"));
  ui.$(`#chat-list [data-id="${identifier(2)}"] .chat-select`).click();
  await flush();
  ui.$("#chat-input").value = "Second draft";
  ui.$("#chat-input").dispatchEvent(new ui.window.Event("input"));
  merged = true;
  await ui.poll();
  assert.equal(ui.$("#chat-input").value, "First draft\n\nSecond draft");
});

test("a single short message automatically loads older pages without a scroll", async t => {
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    const before = url.searchParams.get("before_id");
    if (before === identifier(8)) return history(identifier(1), [message(5), message(6), message(7)], { total: 8, has_more: true });
    if (before === identifier(5)) return history(identifier(1), [message(1), message(2), message(3), message(4)], { total: 8 });
    return history(identifier(1), [message(8)], { total: 8, has_more: true });
  }, { autoFrames: true });
  Object.defineProperties(ui.$("#chat-stream"), {
    clientHeight: { get: () => 500 },
    scrollHeight: { get: () => 100 + ui.ids().length * 60 }
  });
  await ui.app.enter();
  await flush();
  assert.deepEqual(ui.ids(), Array.from({ length: 8 }, (_, i) => identifier(i + 1)));
});

test("older history has a retry button and retains visible messages after an error", async t => {
  let attempts = 0;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    if (url.searchParams.has("before_id")) {
      if (++attempts === 1) throw new Error("Temporary history error");
      return history(identifier(1), [message(1)], { total: 2 });
    }
    return history(identifier(1), [message(2)], { total: 2, has_more: true });
  });
  await ui.app.enter();
  ui.$("#chat-history-more").click();
  await flush();
  assert.deepEqual(ui.ids(), [identifier(2)]);
  assert.match(ui.$("#chat-history-more").textContent, /Повторить/);
  ui.$("#chat-history-more").click();
  await flush();
  assert.deepEqual(ui.ids(), [identifier(1), identifier(2)]);
  assert.equal(ui.$("#chat-history-more").hidden, true);
});

test("all fifty messages remain visible after paging one client history", async t => {
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    const before = url.searchParams.get("before_id");
    const end = before ? Number(before.slice(-12)) - 1 : 50;
    const start = Math.max(1, end - 19);
    return history(identifier(1), Array.from({ length: end - start + 1 }, (_, i) => message(start + i)),
      { total: 50, has_more: start > 1 });
  });
  await ui.app.enter();
  assert.equal(ui.ids().length, 20);
  ui.$("#chat-history-more").click();
  await flush();
  ui.$("#chat-history-more").click();
  await flush();
  assert.deepEqual(ui.ids(), Array.from({ length: 50 }, (_, i) => identifier(i + 1)));
  assert.match(ui.$("#chat-history-note").textContent, /50/);
});

test("one imported message shows that AlfaCRM history is still loading", async t => {
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    const delta = url.searchParams.has("after_seq");
    const result = history(identifier(1), delta ? [message(2), message(3)] : [message(1)],
      { total: delta ? 3 : 1, next_seq: delta ? 102 : 100 });
    result.conversation.history_loading = !delta;
    return result;
  });
  await ui.app.enter();
  assert.match(ui.$("#chat-history-note").textContent, /Загружаем.*AlfaCRM/);
  await ui.poll();
  assert.equal(ui.ids().length, 3);
  assert.doesNotMatch(ui.$("#chat-history-note").textContent, /Загружаем/);
});

test("an old delta cannot skip the unloaded interval when paging history", async t => {
  let anchor;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    if (url.searchParams.has("after_seq")) return history(identifier(1), [message(1)], { next_seq: 101 });
    if (url.searchParams.has("before_id")) {
      anchor = url.searchParams.get("before_id");
      return history(identifier(1), [message(61)]);
    }
    return history(identifier(1), [message(81), message(82)], { has_more: true });
  });
  await ui.app.enter(); await ui.poll();
  ui.$("#chat-history-more").click(); await flush();
  assert.equal(anchor, identifier(81));
});

test("upward wheel at the top requests the next older page", async t => {
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    return url.searchParams.has("before_id") ? history(identifier(1), [message(1)])
      : history(identifier(1), [message(2)], { has_more: true });
  });
  await ui.app.enter();
  ui.$("#chat-stream").scrollTop = 0;
  ui.$("#chat-stream").dispatchEvent(new ui.window.WheelEvent("wheel", { deltaY: -100 }));
  await flush();
  assert.deepEqual(ui.ids(), [identifier(1), identifier(2)]);
});

function timelineLayout(ui) {
  const stream = ui.$("#chat-stream");
  Object.defineProperty(stream, "clientHeight", { configurable: true, value: 100 });
  Object.defineProperty(stream, "scrollHeight", { configurable: true, get: () => ui.ids().length * 50 });
  stream.getBoundingClientRect = () => ({ top: 0, bottom: 100, height: 100 });
  const original = ui.window.HTMLElement.prototype.getBoundingClientRect;
  ui.window.HTMLElement.prototype.getBoundingClientRect = function () {
    if (!this.classList.contains("chat-bubble")) return original.call(this);
    const index = [...this.parentElement.children].filter(n => n.classList.contains("chat-bubble")).indexOf(this);
    const top = index * 50 - stream.scrollTop;
    return { top, bottom: top + 50, height: 50 };
  };
  return stream;
}

test("mixed old and new deltas keep the visible message at the same position", async t => {
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    return url.searchParams.has("after_seq") ? history(identifier(1), [message(1), message(9)], { next_seq: 101 })
      : history(identifier(1), [message(2), message(3), message(4), message(5), message(6)]);
  });
  const stream = timelineLayout(ui);
  await ui.app.enter(); stream.scrollTop = 50;
  const before = ui.$(`[data-id="${identifier(3)}"].chat-bubble`).getBoundingClientRect().top;
  await ui.poll();
  assert.equal(ui.$(`[data-id="${identifier(3)}"].chat-bubble`).getBoundingClientRect().top, before);
});

test("expanded automation messages stay expanded after a history update", async t => {
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    return url.searchParams.has("after_seq") ? history(identifier(1), [message(2)], { next_seq: 101 })
      : history(identifier(1), [message(1, { actor_type: "automation" })]);
  });
  await ui.app.enter(); ui.$(".system-toggle").click(); await ui.poll();
  assert.equal(ui.$(".system-body").hidden, false);
});

test("history day labels distinguish years across the whole period", async t => {
  const ui = setup(t, url => url.pathname.endsWith("/messages") ? history(identifier(1), [
    message(1, { occurred_at: "2024-10-24T00:00:00Z", sort_time: "1729728000000000" }),
    message(2, { occurred_at: "2025-10-24T00:00:00Z", sort_time: "1761264000000000" }),
  ]) : undefined);
  await ui.app.enter();
  const days = [...ui.$("#chat-history").querySelectorAll(".chat-day")].map(n => n.textContent);
  assert.match(days[0], /2024/); assert.match(days[1], /2025/);
});

test("a reset during older paging also replaces the live cursor and page boundary", async t => {
  let stale = false, anchor;
  const ui = setup(t, url => {
    if (!url.pathname.endsWith("/messages")) return;
    if (url.searchParams.has("after_seq")) {
      assert.equal(url.searchParams.get("after_seq"), "50");
      return history(identifier(1), [], { next_seq: 50 });
    }
    if (url.searchParams.has("before_id")) {
      if (!stale) { stale = true; return history(identifier(1), [message(10)], { reset: true, next_seq: 50, has_more: true }); }
      anchor = url.searchParams.get("before_id"); return history(identifier(1), [message(9)], { next_seq: 50 });
    }
    return history(identifier(1), [message(80)], { next_seq: 100, has_more: true });
  });
  await ui.app.enter(); ui.$("#chat-history-more").click(); await flush();
  assert.deepEqual(ui.ids(), [identifier(10)]);
  await ui.poll(); ui.$("#chat-history-more").click(); await flush();
  assert.equal(anchor, identifier(10));
});

test("archive pages cannot advance the seen boundary used by handled actions", async t => {
  const first = { conversation_id: identifier(1), revision: 1, change_seq: 100, latest_inbound_id: identifier(2) };
  const later = { ...first, revision: 2, change_seq: 101, latest_inbound_id: identifier(3) };
  let handled;
  const ui = setup(t, (url, options) => {
    if (url.pathname.endsWith('/handled')) {
      handled = options.data.seen_checkpoint;
      return {conversation:{...conversation(1),revision:3,unread_count:1,seen_checkpoint:first}};
    }
    if (!url.pathname.endsWith('/messages')) return;
    if (url.searchParams.has('before_id')) return history(identifier(1), [message(1)], {conversation:{...conversation(1),revision:2,inbound_seq:2,seen_checkpoint:later},next_seq:101});
    return history(identifier(1), [message(2)], {conversation:{...conversation(1),seen_checkpoint:first},has_more:true});
  });
  await ui.app.enter();
  ui.$('#chat-stream').dispatchEvent(new ui.window.WheelEvent('wheel',{deltaY:-100}));await flush();
  ui.$('#chat-handled').click();await flush();
  assert.equal(handled.change_seq,100);
  assert.equal(handled.latest_inbound_id,identifier(2));
  assert.equal(ui.$('#chat-handled').hidden,false,'unseen incoming remains pending after the action');
});

test("a partial delta keeps the previous seen boundary until the whole batch is displayed", async t => {
  const first = { conversation_id: identifier(1), revision: 1, change_seq: 100, latest_inbound_id: identifier(1) };
  let complete, received;
  const ui = setup(t,(url, options)=>{
    if(url.pathname.endsWith('/handled')) {received=options.data.seen_checkpoint;return {conversation:{...conversation(1),revision:3,seen_checkpoint:first}};}
    if(!url.pathname.endsWith('/messages'))return;
    if(url.searchParams.get('after_seq')==='101')return new Promise(resolve=>{complete=()=>resolve(history(identifier(1),[message(3)],{conversation:{...conversation(1),revision:3,seen_checkpoint:{...first,change_seq:102,latest_inbound_id:identifier(3)}},next_seq:102}));});
    if(url.searchParams.has('after_seq'))return history(identifier(1),[message(2)],{conversation:{...conversation(1),revision:2,seen_checkpoint:{...first,change_seq:102,latest_inbound_id:identifier(3)}},next_seq:101,more_changes:true});
    return history(identifier(1),[message(1)],{conversation:{...conversation(1),seen_checkpoint:first}});
  });
  await ui.app.enter();const polling=ui.poll();await flush();
  assert.ok(complete);
  ui.$('#chat-handled').click();await flush();
  assert.equal(received.change_seq,100);
  complete();await polling;
});

test("swiping a list row posts its displayed checkpoint and preserves loaded pages", async t=>{
  const seen={conversation_id:identifier(1),revision:1,change_seq:100,latest_inbound_id:identifier(1)};
  let body;
  const ui=setup(t,(url,options)=>{
    if(url.pathname.endsWith('/handled')){body=options.data;return {conversation:{...conversation(1),unread:false,unread_count:0,revision:2,seen_checkpoint:seen}};}
  },{conversations:[{...conversation(1),seen_checkpoint:seen}]});
  await ui.app.enter();
  const row=ui.$('.chat-row');
  const dispatch=(type,x)=>{const e=new ui.window.Event(type);Object.assign(e,{pointerType:'touch',clientX:x,clientY:100});row.dispatchEvent(e);};
  dispatch('pointerdown',200);dispatch('pointermove',80);dispatch('pointerup',80);await flush();
  assert.equal(body.seen_checkpoint.change_seq,100);
  assert.equal(body.seen_checkpoint.conversation_id,identifier(1));
});

test("vertical and diagonal scrolling cannot expose or submit the handled action", async t=>{
  const ui=setup(t);
  await ui.app.enter();
  const row=ui.$('.chat-row'),wrap=row.parentElement;
  const dispatch=(type,x,y)=>{const e=new ui.window.Event(type);Object.assign(e,{pointerType:'touch',pointerId:1,clientX:x,clientY:y});row.dispatchEvent(e);};
  dispatch('pointerdown',200,100);
  dispatch('pointermove',195,120);
  assert.equal(row.style.transform,'');assert.equal(wrap.classList.contains('is-swiping'),false);
  // Even without a browser pointercancel, this stays a vertical gesture.
  dispatch('pointermove',70,260);dispatch('pointerup',70,260);await flush();
  assert.equal(ui.calls.some(url=>url.pathname.endsWith('/handled')),false);
});

test("cancelled and short swipes restore the row without marking messages handled", async t=>{
  const ui=setup(t);
  await ui.app.enter();
  const row=ui.$('.chat-row'),wrap=row.parentElement;
  const dispatch=(type,x,y=100)=>{const e=new ui.window.Event(type);Object.assign(e,{pointerType:'touch',pointerId:1,clientX:x,clientY:y});row.dispatchEvent(e);};
  dispatch('pointerdown',200);dispatch('pointermove',80);
  assert.equal(wrap.classList.contains('is-swiping'),true);
  dispatch('pointercancel',80);dispatch('pointerup',80);await flush();
  assert.equal(row.style.transform,'');assert.equal(wrap.classList.contains('is-swiping'),false);
  dispatch('pointerdown',200);dispatch('pointermove',180);dispatch('pointerup',180);await flush();
  assert.equal(row.style.transform,'');assert.equal(wrap.classList.contains('is-swiping'),false);
  assert.equal(ui.calls.some(url=>url.pathname.endsWith('/handled')),false);
});

test("a swipe suppresses the generated click instead of opening the conversation", async t=>{
  const ui=setup(t);
  await ui.app.enter();
  const row=ui.$('.chat-row'),select=row.querySelector('.chat-select');
  let clicks=0;select.addEventListener('click',()=>{clicks+=1;});
  const dispatch=(type,x)=>{const e=new ui.window.Event(type);Object.assign(e,{pointerType:'touch',pointerId:1,clientX:x,clientY:100});row.dispatchEvent(e);};
  dispatch('pointerdown',200);dispatch('pointermove',180);dispatch('pointerup',180);
  select.click();assert.equal(clicks,0);
  dispatch('pointerdown',200);dispatch('pointerup',200);
  select.click();assert.equal(clicks,1);
});

test("transferring touch capture from the child button keeps a deliberate swipe active", async t=>{
  let handled=0;
  const ui=setup(t,(url)=>{if(url.pathname.endsWith('/handled')){handled+=1;return {conversation:{...conversation(1),unread:false,unread_count:0,revision:2}};}});
  await ui.app.enter();
  const row=ui.$('.chat-row'),select=row.querySelector('.chat-select');
  const dispatch=(target,type,x)=>{const e=new ui.window.Event(type,{bubbles:true});Object.assign(e,{pointerType:'touch',pointerId:1,clientX:x,clientY:100});target.dispatchEvent(e);};
  dispatch(select,'pointerdown',200);dispatch(row,'pointermove',80);
  dispatch(select,'lostpointercapture',80);
  assert.equal(row.parentElement.classList.contains('is-swiping'),true);
  dispatch(row,'pointerup',80);await flush();
  assert.equal(handled,1);
});

test("a late history response cannot overwrite a newer list counter at the same revision", async t=>{
  let lists=0;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/chats'))return {conversations:[{...conversation(1),unread_count:++lists===1?1:2,queue_seq:lists===1?'100':'101'}],total:1,unread:1,page:0,has_more:false};
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[message(1)],{conversation:{...conversation(1),queue_seq:'100'}});
  });
  await ui.app.enter();await ui.poll();
  assert.equal(ui.$('.avatar-count').textContent,'2');
});

test("family balance shows the CRM total while keeping the subscription amount separate", async t=>{
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(90)}});
    if(url.pathname.endsWith('/family'))return {id:identifier(90),name:'Родитель',phones:[],emails:[],today:'2026-10-24',students:[{id:identifier(91),name:'Ребёнок',balance:'3962.50',groups:[],cards:[{id:'7',branch_id:'1',branch_name:'Филиал',status:'client',needs_refresh:false,paid_lesson_count:3,subscriptions:[{id:40,name:'Модельная школа',balance:'2725.00',begin:'2026-10-01',end:'2027-07-04'}]}]}]};
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  const amount=ui.$('.family-stats .family-stat strong');
  assert.match(amount.textContent.replace(/\s/g,''),/^3962,5₽$/);
  assert.match(amount.parentElement.title,/базовый счёт и абонементы/);
  assert.match(ui.$('.family-item-value').textContent.replace(/\s/g,''),/^2725₽$/);
});

test("negative family debt uses the total including base money, even with positive subscriptions", async t=>{
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(90)}});
    if(url.pathname.endsWith('/family'))return {id:identifier(90),name:'Родитель',phones:[],emails:[],today:'2026-10-24',students:[{id:identifier(91),name:'Ребёнок',balance:'-120.75',groups:[],cards:[{id:'7',branch_id:'1',branch_name:'Филиал',status:'client',needs_refresh:false,subscriptions:[{id:40,name:'Абонемент',balance:'2725.00'}]}]}]};
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.match(ui.$('.family-stats .negative strong').textContent.replace(/\s/g,''),/^-120,75₽$/);
  assert.match(ui.$('.family-note.debt').textContent,/Общий баланс клиента отрицательный/);
  assert.doesNotMatch(ui.$('.family-note.debt').textContent,/Баланс по абонементам/);
});

test("family uses unique server subscription and paid totals while retaining both branches", async t=>{
  const subscription={id:40,name:'Художественная школа',balance:'8062.50',begin:'2026-10-01',end:'2027-07-04',branch_name:'Центральный · Северный'};
  const cards=['Центральный','Северный'].map((name,i)=>({id:'7',branch_id:String(i+1),branch_name:name,status:'client',needs_refresh:false,paid_lesson_count:9,subscriptions:[subscription],lessons:[]}));
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(90)}});
    if(url.pathname.endsWith('/family'))return {id:identifier(90),name:'Родитель',phones:[],emails:[],today:'2026-10-24',students:[{id:identifier(91),name:'Ребёнок',balance:'8062.50',paid_lesson_count:9,subscriptions:[subscription],groups:[],cards}]};
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.equal(ui.$('.family-stats').children[1].querySelector('strong').textContent,'9 занятий');
  assert.equal(ui.$('.family-stats').children[2].querySelector('strong').textContent,'1');
  assert.equal(ui.$('#chat-details-student').querySelectorAll('.family-item-value').length,1);
  assert.equal(ui.$('.family-item-main small').textContent,'до 04.07.2027');
  assert.match(ui.$('#chat-details-student').textContent,/Филиалы/);
  const branches=[...ui.$('#chat-details-student').querySelectorAll('.family-part')].find(part=>part.querySelector('.family-label').textContent==='Филиалы');
  assert.doesNotMatch(branches.textContent,/Клиент|Лид|Архивный/);
  assert.doesNotMatch(ui.$('#chat-details-student').textContent,/оплачено 9/);
});

test("conflicting branch observations show unknown totals without endless preparation", async t=>{
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(90)}});
    if(url.pathname.endsWith('/family'))return {id:identifier(90),name:'Родитель',phones:[],emails:[],today:'2026-10-24',students:[{id:identifier(91),name:'Ребёнок',balance:'8062.50',paid_lesson_count:null,
      subscriptions:[{id:40,name:'Школа',needs_review:true,balance:null,branch_name:'Два филиала'}],groups:[],cards:[{id:'7',branch_id:'1',subscriptions:[],lessons:[],paid_lesson_count:9}]}]};
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.equal(ui.$('.family-stats').children[1].querySelector('strong').textContent,'—');
  assert.equal(ui.$('.family-stats').children[2].querySelector('strong').textContent,'—');
  assert.match(ui.$('#chat-details-student').textContent,/Уточните сведения/);
  assert.doesNotMatch(ui.$('#chat-details-student').textContent,/Готовим подробную карточку/);
});

test('window patches retain unchanged nodes and apply new ordering without duplicates', async t=>{
  let lists=0;
  const rows=[conversation(1),conversation(2),conversation(3)];
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/chats')) {
      assert.equal(url.searchParams.get('window'),'90');
      if(++lists===1)return {window_token:'one',reset:true,conversations:rows,order:rows.map(r=>r.id),total:3,unread:3,page:0,has_more:false};
      assert.equal(url.searchParams.get('window_token'),'one');
      return {window_token:'two',reset:false,conversations:[{...conversation(3),preview:'Changed',last_message_at:'2026-10-24T12:00:00Z'}],order:[3,1,2].map(identifier),total:3,unread:3,page:0,has_more:false};
    }
  });
  await ui.app.enter();
  const retained=ui.$(`.chat-row[data-id="${identifier(2)}"]`);
  ui.$('#chat-input').value='Unsent draft';
  await ui.poll();
  assert.deepEqual([...ui.$('#chat-list').querySelectorAll('.chat-row')].map(row=>row.dataset.id),[3,1,2].map(identifier));
  assert.equal(ui.$(`.chat-row[data-id="${identifier(2)}"]`),retained);
  assert.equal(ui.$('#chat-input').value,'Unsent draft');
});

test('list prefetch starts several screens before the end and never overlaps a poll', async t=>{
  let reads=0,release;
  const ui=setup(t,url=>{
    if(!url.pathname.endsWith('/chats'))return;
    reads++;
    if(reads===2)return new Promise(resolve=>{release=()=>resolve({window_token:'second',reset:false,conversations:[],order:[identifier(1)],total:200,unread:1,page:1,has_more:false});});
    return {window_token:'first',reset:true,conversations:[conversation(1)],order:[identifier(1)],total:200,unread:1,page:0,has_more:true};
  });
  await ui.app.enter();
  const scroll=ui.$('.chat-list-scroll');
  Object.defineProperties(scroll,{clientHeight:{get:()=>600},scrollHeight:{get:()=>3000}});
  scroll.scrollTop=1000;scroll.dispatchEvent(new ui.window.Event('scroll'));
  await flush();assert.equal(reads,2);
  const polling=ui.poll();await flush();assert.equal(reads,2);
  release();await polling;
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/chats')).at(-1).searchParams.get('window'),'180');
});

function preparedFamily(n) {
  return {id:identifier(90+n),name:`Family ${n}`,phones:[],emails:[],today:'2026-10-24',
    cache:{ready:true,refreshing:false,auto_refresh:false,updated_at:'2026-10-24T00:00:00Z',sources:1,cached_sources:1,overdue:false},
    students:[{id:identifier(100+n),name:`Child ${n}`,balance:'100',groups:[],cards:[{id:String(n),branch_id:'1',branch_name:'Branch',status:'client',needs_refresh:false,subscriptions:[]}]}]};
}
function dockFamily(ui) {
  ui.window.matchMedia=()=>({matches:false});
  Object.defineProperty(ui.$('#chat-layout'),'clientWidth',{get:()=>1400});
  ui.$('.pane-list').getBoundingClientRect=()=>({width:380});
  const dialog=ui.$('#chat-details-dialog');
  dialog.show=dialog.showModal=function(){this.open=true;};
  dialog.close=function(){this.open=false;this.dispatchEvent(new ui.window.Event('close'));};
}

test('CRM review badge explains current reasons and disappears after the conflict is resolved',async t=>{
  const family=preparedFamily(1);family.needs_review=true;family.cache.auto_refresh=true;
  family.review_reasons=[{code:'customer_name_conflict',text:'Различаются ФИО ребёнка в филиалах.',customer_ids:['9160'],branch_ids:['1','2']}];
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return structuredClone(family);
  });
  dockFamily(ui);await ui.app.enter();ui.$('#chat-details').click();await flush();
  const badge=[...ui.$('#chat-details-badges').children].find(node=>node.textContent==='Нужна проверка в CRM');
  assert.ok(badge);assert.match(badge.title,/Различаются ФИО/);assert.match(badge.title,/ID AlfaCRM: 9160/);assert.match(badge.title,/Филиалы: 1, 2/);
  family.needs_review=false;family.review_reasons=[];
  await runTimer(ui,60000);
  assert.doesNotMatch(ui.$('#chat-details-badges').textContent,/Нужна проверка/);
  assert.match(ui.$('#chat-details-badges').textContent,/Клиент/);
});

function enableBulkDialog(ui) {
  const dialog=ui.$('#chat-bulk-dialog');
  dialog.showModal=function(){this.open=true;};
  dialog.close=function(){this.open=false;this.dispatchEvent(new ui.window.Event('close'));};
  return dialog;
}
function handledPointer(ui,type,x=20,y=20) {
  const event=new ui.window.MouseEvent(type,{bubbles:true,button:0,clientX:x,clientY:y});
  Object.defineProperties(event,{pointerId:{value:1},isPrimary:{value:true}});
  ui.$('#chat-handled').dispatchEvent(event);
}
async function holdHandled(ui) {
  handledPointer(ui,'pointerdown');
  const timer=[...ui.timers.entries()].find(([,timer])=>timer.delay===700);
  assert.ok(timer,'a long press has an explicit hold delay');
  ui.timers.delete(timer[0]);timer[1].callback();await flush();
}
const bulkPreview=(n=1)=>({operation_id:identifier(800+n),client_count:71,conversation_count:72,message_count:93,
  expires_at:new Date(Date.now()+600000).toISOString()});

test('long press prepares all unread chats and cancellation never marks the current chat or the batch',async t=>{
  const posts=[];
  const ui=setup(t,(url,options)=>{
    if(options?.method==='POST'){posts.push(url.pathname);if(url.pathname.endsWith('/handled/prepare'))return bulkPreview();}
  });
  const dialog=enableBulkDialog(ui);await ui.app.enter();
  await holdHandled(ui);
  assert.equal(dialog.open,true);
  assert.match(ui.$('#chat-bulk-summary').textContent,/Клиентов: 71.*Диалогов: 72.*Сообщений без ответа: 93/);
  assert.equal(ui.$('#chat-bulk-confirm').disabled,false);
  handledPointer(ui,'pointerup');
  ui.$('#chat-handled').dispatchEvent(new ui.window.MouseEvent('click',{bubbles:true,detail:1}));
  ui.$('#chat-bulk-cancel').click();await flush();
  assert.equal(dialog.open,false);
  assert.deepEqual(posts,['/workspaces/test/chats/handled/prepare']);
});

test('a short handled click still only processes the current conversation',async t=>{
  const posts=[];
  const ui=setup(t,(url,options)=>{
    if(options?.method==='POST'){posts.push(url.pathname);if(url.pathname.endsWith('/handled'))return {conversation:{...conversation(1),unread:false,unread_count:0}};}
  });
  enableBulkDialog(ui);await ui.app.enter();
  handledPointer(ui,'pointerdown');handledPointer(ui,'pointerup');
  ui.$('#chat-handled').dispatchEvent(new ui.window.MouseEvent('click',{bubbles:true,detail:1}));await flush();
  assert.deepEqual(posts,[`/workspaces/test/chats/${identifier(1)}/handled`]);
});

test('moving or cancelling a press does not process a conversation',async t=>{
  const ui=setup(t);enableBulkDialog(ui);await ui.app.enter();
  handledPointer(ui,'pointerdown');handledPointer(ui,'pointermove',40,40);handledPointer(ui,'pointerup');
  ui.$('#chat-handled').dispatchEvent(new ui.window.MouseEvent('click',{bubbles:true,detail:1}));await flush();
  assert.equal([...ui.timers.values()].some(timer=>timer.delay===700),false);
  assert.equal(ui.calls.filter(url=>url.pathname.includes('/handled')).length,0);
  handledPointer(ui,'pointerdown');handledPointer(ui,'pointercancel');
  ui.$('#chat-handled').dispatchEvent(new ui.window.MouseEvent('click',{bubbles:true,detail:1}));await flush();
  assert.equal(ui.calls.filter(url=>url.pathname.includes('/handled')).length,0);
});

test('bulk confirmation requires its own button and a lost response retries the same frozen operation',async t=>{
  const bodies=[];let attempts=0,prepares=0;
  const ui=setup(t,(url,options)=>{
    if(url.pathname.endsWith('/handled/prepare')){prepares++;return bulkPreview();}
    if(url.pathname.endsWith('/handled/confirm')){
      bodies.push(JSON.parse(JSON.stringify(options.data)));
      if(++attempts===1)throw Object.assign(new Error('Ответ временно недоступен'),{status:503});
      return {processed:72,already_handled:0,skipped:0,messages:93};
    }
  });
  const dialog=enableBulkDialog(ui);await ui.app.enter();await holdHandled(ui);
  assert.equal(attempts,0);
  assert.equal(ui.window.document.activeElement,ui.$('#chat-bulk-cancel'));
  ui.$('#chat-bulk-confirm').click();await flush();
  assert.equal(dialog.open,true);
  assert.match(ui.$('#chat-bulk-error').textContent,/временно недоступен/);
  ui.$('#chat-bulk-confirm').click();await flush();
  assert.equal(dialog.open,false);
  assert.equal(prepares,1);
  assert.deepEqual(bodies,[{operation_id:identifier(801),confirmed:true},{operation_id:identifier(801),confirmed:true}]);
});

test('Shift Enter opens the same confirmation and an expired operation needs a refreshed list',async t=>{
  let prepares=0;
  const ui=setup(t,(url,options)=>{
    if(url.pathname.endsWith('/handled/prepare'))return bulkPreview(++prepares);
    if(url.pathname.endsWith('/handled/confirm'))throw Object.assign(new Error('Срок подтверждения истёк'),{status:409});
  });
  enableBulkDialog(ui);await ui.app.enter();
  ui.$('#chat-handled').dispatchEvent(new ui.window.KeyboardEvent('keydown',{key:'Enter',shiftKey:true,bubbles:true}));await flush();
  ui.$('#chat-bulk-confirm').click();await flush();
  assert.equal(ui.$('#chat-bulk-confirm').disabled,true);
  assert.equal(ui.$('#chat-bulk-retry').hidden,false);
  ui.$('#chat-bulk-retry').click();await flush();
  assert.equal(prepares,2);
  assert.equal(ui.$('#chat-bulk-confirm').disabled,false);
});

test('reset discards a delayed bulk preview and restores controls',async t=>{
  let release;
  const ui=setup(t,url=>{if(url.pathname.endsWith('/handled/prepare'))return new Promise(resolve=>{release=resolve;});});
  const dialog=enableBulkDialog(ui);await ui.app.enter();await holdHandled(ui);
  ui.app.reset();release(bulkPreview());await flush();
  assert.equal(dialog.open,false);
  assert.equal(ui.$('#chat-bulk-cancel').disabled,false);
  assert.equal(ui.$('#chat-bulk-close').disabled,false);
  assert.equal(ui.$('#chat-bulk-confirm').disabled,true);
});

test('a queued native close event cannot erase a newly opened bulk confirmation',async t=>{
  let confirms=0;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/handled/prepare'))return bulkPreview();
    if(url.pathname.endsWith('/handled/confirm')){confirms++;return {processed:72,already_handled:0,skipped:0,messages:93};}
  });
  const dialog=enableBulkDialog(ui),closed=[];
  dialog.close=function(){this.open=false;closed.push(()=>this.dispatchEvent(new ui.window.Event('close')));};
  await ui.app.enter();await holdHandled(ui);ui.$('#chat-bulk-cancel').click();
  ui.$('#chat-handled').dispatchEvent(new ui.window.KeyboardEvent('keydown',{key:'Enter',shiftKey:true,bubbles:true}));await flush();
  closed.shift()();
  assert.equal(dialog.open,true);ui.$('#chat-bulk-confirm').click();await flush();
  assert.equal(confirms,1);assert.equal(dialog.open,false);
});

test('pinned read client opens its full card without a second click, with no hidden-card preloading', async t=>{
  const rows=[{...conversation(1),contact_id:identifier(91)}, {...conversation(2),unread:false,unread_count:0,contact_id:identifier(92)}];
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages')){
      const n=Number(url.pathname.split('/').at(-2).slice(-2));
      return history(identifier(n),[],{conversation:rows[n-1]});
    }
    if(url.pathname.endsWith('/family'))return preparedFamily(url.pathname.includes(identifier(2))?2:1);
  },{conversations:rows});
  dockFamily(ui);await ui.app.enter();
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/family')).length,0);
  ui.$('#chat-details').click();await flush();ui.$('#chat-details-pin').click();
  ui.$(`.chat-row[data-id="${identifier(2)}"] .chat-select`).click();await flush();
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/family') && url.searchParams.get('cached_only')==='true').length,2);
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/family') && !url.searchParams.has('cached_only')).length,2);
  assert.doesNotMatch(ui.$('#chat-details-dialog').textContent,/Child 1/);
  assert.doesNotMatch(ui.$('#chat-details-dialog').textContent,/Открыть карточку клиента/);
  assert.match(ui.$('#chat-details-student').textContent,/Child 2/);
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/family')).at(-1).searchParams.has('auto'),false);
  assert.equal([...ui.timers.values()].some(timer=>[3000,30000,60000].includes(timer.delay)),false,
    'a prepared read client does not start periodic card loading');
});

test('pinned pending client reads ready card in parallel with a slow timeline', async t=>{
  let release;
  const rows=[{...conversation(1),contact_id:identifier(91)},{...conversation(2),contact_id:identifier(92)}];
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages')){
      if(url.pathname.includes(identifier(2)))return new Promise(resolve=>{release=()=>resolve(history(identifier(2),[],{conversation:rows[1]}));});
      return history(identifier(1),[],{conversation:rows[0]});
    }
    if(url.pathname.endsWith('/family'))return preparedFamily(url.pathname.includes(identifier(2))?2:1);
  },{conversations:rows});
  dockFamily(ui);await ui.app.enter();ui.$('#chat-details').click();await flush();ui.$('#chat-details-pin').click();
  ui.$(`.chat-row[data-id="${identifier(2)}"] .chat-select`).click();await flush();
  assert.ok(release);assert.match(ui.$('#chat-details-student').textContent,/Child 2/);
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/family')).at(-1).searchParams.has('auto'),false);
  release();await flush();
});

test('opening family for staff uses prepared GET data without per-source CRM mutations', async t=>{
  const ui=setup(t,(url,options)=>{
    assert.ok(!options?.method || options.method==='GET','Opening a card must not run browser-driven refresh POSTs');
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return {...preparedFamily(1),cache:{...preparedFamily(1).cache,refreshing:true}};
  });
  ui.state.workspace.role='operator';
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.match(ui.$('#chat-details-student').textContent,/Child 1/);
  assert.match(ui.$('#chat-details-sync').textContent,/ · обновляем$/);
  assert.equal(ui.$('#chat-details-refresh').hidden,false);
  assert.equal(ui.$('#chat-details-refresh').disabled,true);
  assert.equal(ui.$('#chat-details-refresh').getAttribute('aria-busy'),'true');
});

test('card footer stays brief while the tooltip retains separate freshness dates and errors', async t=>{
  const family=preparedFamily(1);
  family.cache={...family.cache,refreshing:true,lessons_updated_at:'2026-10-24T00:10:00Z',
    lessons_history_updated_at:'2026-10-23T00:00:00Z',
    groups_updated_at:'2026-10-24T00:05:00Z',
    error:'Не удалось обновить занятия. Баланс и абонементы обновляются отдельно.'};
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return family;
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  const sync=ui.$('#chat-details-sync'),text=sync.title;
  assert.equal(sync.textContent,'Обновлено 2026-10-24T00:00:00Z · есть ошибка обновления');
  assert.equal(sync.getAttribute('aria-label'),text);
  assert.match(text,/Баланс и абонементы:.*2026-10-24/);
  assert.match(text,/Занятия:.*00:10/);
  assert.match(text,/Год занятий проверен.*2026-10-23/);
  assert.match(text,/Группы:.*00:05/);
  assert.match(text,/Не удалось обновить занятия/);
  assert.match(ui.$('.family-stats').textContent,/100/);
});

test('paused CRM credentials keep cached facts visible without animation and polling observes recovery', async t=>{
  let family=preparedFamily(1);
  family.cache={...family.cache,pending:true,paused:true,refreshing:false,auto_refresh:false,
    error:'CRM не приняла ключ.',failures:[{code:'credentials_required'}]};
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return family;
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.match(ui.$('#chat-details-sync').textContent,/проверьте ключ CRM/);
  assert.match(ui.$('.family-stats').textContent,/100/);
  assert.equal(ui.$('#chat-details-dialog').classList.contains('is-refreshing'),false);
  const timer=[...ui.timers.entries()].find(([,item])=>item.delay===60000);
  assert.ok(timer,'a paused manual card still observes a later server recovery');
  family={...family,cache:{...family.cache,pending:false,paused:false,error:null,failures:[],updated_at:'2026-10-27T00:00:00Z'}};
  ui.timers.delete(timer[0]);await timer[1].callback();await flush();
  assert.match(ui.$('#chat-details-sync').textContent,/2026-10-27/);
  assert.doesNotMatch(ui.$('#chat-details-sync').textContent,/проверьте ключ CRM/);
});

test('disk card renders before a slow enqueue and keeps its content while the refresh arrow spins', async t=>{
  let release;
  let family=preparedFamily(1);
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family')){
      if(url.searchParams.get('cached_only')==='true')return family;
      return new Promise(resolve=>{release=()=>{family={...family,cache:{...family.cache,refreshing:true,pending:true}};resolve(family);};});
    }
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.ok(release);assert.match(ui.$('.family-stats').textContent,/100/);
  const stats=ui.$('.family-stats');
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/family'))[0].searchParams.get('cached_only'),'true');
  release();await flush();
  assert.equal(ui.$('.family-stats'),stats);
  assert.equal(ui.$('#chat-details-dialog').classList.contains('is-refreshing'),true);
  assert.equal(ui.$('#chat-details-refresh').getAttribute('aria-busy'),'true');
  family={...family,cache:{...family.cache,pending:false,refreshing:false}};
  await runTimer(ui,3000);await flush();
  assert.equal(ui.$('#chat-details-dialog').classList.contains('is-refreshing'),false);
  assert.equal(ui.$('#chat-details-refresh').getAttribute('aria-busy'),'false');
  assert.equal(ui.$('.family-stats'),stats);
});

test('manual refresh keeps the disk snapshot but only an active server lease spins', async t=>{
  let release;
  const family=preparedFamily(1);
  const ui=setup(t,(url,options)=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return family;
    if(url.pathname.endsWith('/family/refresh'))return new Promise(resolve=>{release=()=>resolve(family);});
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  const stats=ui.$('.family-stats');ui.$('#chat-details-refresh').click();await flush();
  assert.ok(release);assert.equal(ui.$('.family-stats'),stats);
  assert.equal(ui.$('#chat-details-refresh').getAttribute('aria-busy'),'false');
  assert.equal(ui.$('#chat-details-refresh').disabled,true);
  release();await flush();
  assert.equal(ui.$('#chat-details-refresh').getAttribute('aria-busy'),'false');
  assert.equal(ui.$('.family-stats'),stats);
});

test('ready card footer shows one update time and its refresh interval', async t=>{
  const family=preparedFamily(1);family.cache.auto_refresh=true;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return family;
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.equal(ui.$('#chat-details-sync').textContent,'Обновлено 2026-10-24T00:00:00Z · каждые 30 мин');
});

test('card queue, active lease and busy channel have distinct labels and polling clocks', async t=>{
  let family=preparedFamily(1);
  family.cache={...family.cache,state:'queued',complete:false,incomplete:true,pending:true,loading:false,
    queue_position:2,sections:{finance:{label:'Баланс и абонементы',status:'queued'}}};
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return structuredClone(family);
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.equal(ui.$('#chat-details-sync').textContent,'В очереди · позиция 2');
  assert.equal(ui.$('#chat-details-dialog').classList.contains('is-refreshing'),false);
  family={...family,cache:{...family.cache,state:'loading',loading:true,refreshing:true,
    sections:{finance:{label:'Баланс и абонементы',status:'loading'}}}};
  await runTimer(ui,5000);await flush();
  assert.equal(ui.$('#chat-details-sync').textContent,'Загружаем баланс и абонементы');
  assert.equal(ui.$('#chat-details-dialog').classList.contains('is-refreshing'),true);
  family={...family,cache:{...family.cache,state:'busy',loading:false,refreshing:false,busy:true,
    retry_at:'2026-10-24T00:00:05Z',sections:{finance:{label:'Баланс и абонементы',status:'busy'}}}};
  await runTimer(ui,3000);await flush();
  assert.match(ui.$('#chat-details-sync').textContent,/^Канал AlfaCRM занят/);
  assert.equal(ui.$('#chat-details-dialog').classList.contains('is-refreshing'),false);
  assert.ok([...ui.timers.values()].some(timer=>timer.delay===10000));
});

test('preferred CRM child opens first while an operator child choice survives card polling', async t=>{
  const first=preparedFamily(1),second=preparedFamily(2);
  const family={...first,id:identifier(99),source_contact_id:identifier(91),
    selection:{student:second.students[0].id,signature:'preferred-2'},
    cache:{...first.cache,auto_refresh:true},students:[...first.students,...second.students]};
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return family;
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.match(ui.$('.family-kid[aria-pressed="true"]').textContent,/2/);
  assert.match(ui.$('#chat-details-student h3').textContent,/Child 2/);
  ui.$('.family-kid').click();await flush();
  const watch=[...ui.timers.entries()].find(([,timer])=>timer.delay===60000);
  assert.ok(watch);ui.timers.delete(watch[0]);await watch[1].callback();await flush();
  assert.match(ui.$('#chat-details-student h3').textContent,/Child 1/);
});

test('a new preferred active student replaces the old choice, while unchanged preference preserves later manual choices', async t=>{
  const first=preparedFamily(1),second=preparedFamily(2);
  let family={...first,id:identifier(99),source_contact_id:identifier(91),
    selection:{student:first.students[0].id,signature:'archive'},
    cache:{...first.cache,auto_refresh:true},students:[...first.students,...second.students]};
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return family;
  });
  ui.$('#chat-details-dialog').showModal=function(){this.open=true;};
  ui.$('#chat-details-dialog').close=function(){this.open=false;};
  await ui.app.enter();ui.$('#chat-details').click();await flush();
  ui.$('.family-kid').click();await flush();
  assert.match(ui.$('#chat-details-student h3').textContent,/Child 1/);
  family={...family,selection:{student:second.students[0].id,signature:'active'}};
  await runTimer(ui,60000);await flush();
  assert.match(ui.$('#chat-details-student h3').textContent,/Child 2/);
  ui.$('.family-kid').click();await flush();
  family={...family,selection:{...family.selection,signature:'active-new-facts'}};
  await runTimer(ui,60000);await flush();
  assert.match(ui.$('#chat-details-student h3').textContent,/Child 1/);
});

test('a stalled list does not hold multiple rounds of live messages or duplicate its request', async t=>{
  let lists=0,deltas=0,release;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/chats')&&++lists===2)return new Promise(resolve=>{release=()=>resolve({conversations:[conversation(1)],page:0,total:1,unread:1,has_more:false});});
    if(url.pathname.endsWith('/messages'))return url.searchParams.has('after_seq')
      ?history(identifier(1),[message(2+(deltas++))],{next_seq:100+deltas}):history(identifier(1),[message(1)]);
  });
  await ui.app.enter();const first=ui.poll();await flush();
  assert.deepEqual(ui.ids(),[1,2].map(identifier));assert.equal(lists,2);
  await ui.poll();assert.deepEqual(ui.ids(),[1,2,3].map(identifier));assert.equal(lists,2);
  release();await first;
});

test('a stalled timeline does not hold list updates or queue duplicate history polls', async t=>{
  let lists=0,deltas=0,release;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/chats'))return {conversations:[{...conversation(1),name:`List ${++lists}`}],page:0,total:1,unread:1,has_more:false};
    if(url.pathname.endsWith('/messages'))return url.searchParams.has('after_seq')
      ?new Promise(resolve=>{deltas++;release=()=>resolve(history(identifier(1),[message(2)],{next_seq:101}));}):history(identifier(1),[message(1)]);
  });
  await ui.app.enter();const first=ui.poll();await flush();
  await ui.poll();await ui.poll();assert.equal(lists,4);assert.equal(deltas,1);
  assert.match(ui.$('.row-name').textContent,/List 4/);release();await first;
});

test('a failing list backs off independently while new messages continue and its error stays visible', async t=>{
  let lists=0,deltas=0;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/chats')&&++lists>1)throw new Error('List unavailable');
    if(url.pathname.endsWith('/messages'))return url.searchParams.has('after_seq')
      ?history(identifier(1),[message(2+(deltas++))],{next_seq:100+deltas}):history(identifier(1),[message(1)]);
  });
  await ui.app.enter();await ui.poll();await ui.poll();
  assert.equal(lists,2);assert.deepEqual(ui.ids(),[1,2,3].map(identifier));
  assert.equal(ui.$('#chat-error').textContent,'List unavailable');
});

test('selecting a different client aborts the previous timeline and immediately loads the new one', async t=>{
  let signal;
  const ui=setup(t,(url,options)=>{
    if(!url.pathname.endsWith('/messages'))return;
    const id=url.pathname.split('/').at(-2);
    if(url.searchParams.has('after_seq'))return new Promise((resolve,reject)=>{
      signal=options.signal;signal.addEventListener('abort',()=>reject(new ui.window.DOMException('Cancelled','AbortError')),{once:true});
    });
    return history(id,[message(id===identifier(1)?1:2)],{conversation:{...conversation(id===identifier(1)?1:2),id}});
  },{conversations:[conversation(1),conversation(2)]});
  await ui.app.enter();ui.$('#chat-input').value='Saved draft';const pending=ui.poll();await flush();
  ui.$(`.chat-row[data-id="${identifier(2)}"] .chat-select`).click();await flush();await pending;
  assert.equal(signal.aborted,true);assert.deepEqual(ui.ids(),[identifier(2)]);
  assert.equal(ui.$('#chat-error').textContent,'');
  ui.$(`.chat-row[data-id="${identifier(1)}"] .chat-select`).click();await flush();
  assert.equal(ui.$('#chat-input').value,'Saved draft');
});

test('a live patch retains unchanged bubbles, dates and expanded automation while editing and inserting in time order', async t=>{
  let round=0;
  const initial=[message(1),message(2,{actor_type:'automation',template_name:'Reminder'}),message(3)];
  const ui=setup(t,url=>url.pathname.endsWith('/messages')?(url.searchParams.has('after_seq')
    ?history(identifier(1),[message(4,{crm_order:'2'}),message(3,{text:'Edited',version:2})],{next_seq:++round+100})
    :history(identifier(1),initial)):undefined);
  await ui.app.enter();const kept=ui.$(`[data-id="${identifier(1)}"].chat-bubble`),system=ui.$('.system'),day=ui.$('.chat-day'),edited=ui.$(`[data-id="${identifier(3)}"].chat-bubble`);
  ui.$('.system-toggle').click();await ui.poll();
  assert.equal(ui.$(`[data-id="${identifier(1)}"].chat-bubble`),kept);assert.equal(ui.$('.system'),system);assert.equal(ui.$('.chat-day'),day);
  assert.equal(ui.$('.system-body').hidden,false);assert.notEqual(ui.$(`[data-id="${identifier(3)}"].chat-bubble`),edited);
  assert.deepEqual(ui.ids(),[1,2,4,3].map(identifier));assert.match(ui.$('#chat-history').textContent,/Edited/);
  const all=[...ui.$('#chat-history').children];await ui.poll();assert.deepEqual([...ui.$('#chat-history').children],all);
});

test('metadata-only updates retain the timeline while updating history progress and the handled checkpoint', async t=>{
  const ui=setup(t,url=>url.pathname.endsWith('/messages')?(url.searchParams.has('after_seq')
    ?history(identifier(1),[],{next_seq:101,total:50,conversation:{...conversation(1),revision:2,inbound_seq:7,history_loading:true,seen_checkpoint:'new'}})
    :history(identifier(1),[message(1)])):undefined);
  await ui.app.enter();const bubble=ui.$('.chat-bubble'),day=ui.$('.chat-day');await ui.poll();
  assert.equal(ui.$('.chat-bubble'),bubble);assert.equal(ui.$('.chat-day'),day);
  assert.match(ui.$('#chat-history-note').textContent,/1 из 50.*более раннюю историю/);
});

test('retained message grouping and day separators follow removals and older insertions', async t=>{
  const yesterday=message(4,{occurred_at:'2026-10-23T00:00:00Z',sort_time:'1792713600000000'});
  const ui=setup(t,url=>url.pathname.endsWith('/messages')?(url.searchParams.has('after_seq')
    ?history(identifier(1),[yesterday],{next_seq:101,removed_ids:[identifier(2)]})
    :history(identifier(1),[message(1),message(2),message(3)])):undefined);
  await ui.app.enter();const first=ui.$(`[data-id="${identifier(1)}"].chat-bubble`),last=ui.$(`[data-id="${identifier(3)}"].chat-bubble`);
  await ui.poll();assert.deepEqual(ui.ids(),[4,1,3].map(identifier));assert.equal(ui.$('#chat-history').querySelectorAll('.chat-day').length,2);
  assert.equal(ui.$(`[data-id="${identifier(1)}"].chat-bubble`),first);assert.equal(ui.$(`[data-id="${identifier(3)}"].chat-bubble`),last);
  assert.equal(first.classList.contains('first'),true);assert.equal(first.classList.contains('last'),false);assert.equal(last.classList.contains('last'),true);
});

test('card freshness updates retain focused controls and child choice, and changed balances preserve scrolling', async t=>{
  const original=preparedFamily(1),second=preparedFamily(2);
  let family={...original,students:[...original.students,...second.students],cache:{...original.cache,auto_refresh:true}};
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[],{conversation:{...conversation(1),contact_id:identifier(91)}});
    if(url.pathname.endsWith('/family'))return family;
  });
  ui.$('#cabinet').hidden=false;ui.$('#pane-messages').hidden=false;
  dockFamily(ui);await ui.app.enter();ui.$('#chat-details').click();await flush();
  const selected=ui.$('.family-kid:nth-child(2)');selected.click();selected.focus();const stats=ui.$('.family-stats');
  assert.equal(ui.window.document.activeElement,selected);
  const refresh=async()=>{const timer=[...ui.timers.entries()].find(([,entry])=>entry.delay===60000);assert.ok(timer);ui.timers.delete(timer[0]);await timer[1].callback();};
  family={...family,snapshot_at:'2026-10-24T10:00:00Z',cache:{...family.cache,updated_at:'2026-10-24T01:00:00Z'}};
  await refresh();assert.equal(ui.$('.family-stats'),stats);assert.equal(ui.$('.family-kid[aria-pressed="true"]'),selected);
  assert.equal(ui.window.document.activeElement,selected);assert.match(ui.$('#chat-details-sync').textContent,/01:00/);
  const scroll=ui.$('.family-scroll');scroll.scrollTop=120;
  family={...family,students:family.students.map(student=>student.id===second.students[0].id?{...student,balance:'345'}:student)};
  await refresh();assert.match(ui.$('.family-stats').textContent,/345/);assert.equal(scroll.scrollTop,120);
  assert.match(ui.$('#chat-details-student h3').textContent,/Child 2/);
});

test('repeated card opening shares one pending read and cannot render a different client response', async t=>{
  let reads=0,release;
  const rows=[{...conversation(1),contact_id:identifier(91)},{...conversation(2),contact_id:identifier(92)}];
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages')){const n=url.pathname.includes(identifier(2))?2:1;return history(identifier(n),[],{conversation:rows[n-1]});}
    if(url.pathname.endsWith('/family')){reads++;if(url.pathname.includes(identifier(1)))return new Promise(resolve=>{release=()=>resolve(preparedFamily(1));});return preparedFamily(2);}
  },{conversations:rows});
  dockFamily(ui);await ui.app.enter();ui.$('#chat-details').click();ui.$('#chat-details').click();await flush();assert.equal(reads,1);
  ui.$('#chat-details-pin').click();ui.$(`.chat-row[data-id="${identifier(2)}"] .chat-select`).click();await flush();
  assert.match(ui.$('#chat-details-student').textContent,/Child 2/);release();await flush();
  assert.equal(reads,3);assert.doesNotMatch(ui.$('#chat-details-student').textContent,/Child 1/);
});

test('date formatter reuse follows the workspace timezone when an unchanged message crosses midnight', async t=>{
  const record=message(1,{occurred_at:'2026-10-23T22:30:00Z',sort_time:'1792794600000000'});
  const ui=setup(t,url=>url.pathname.endsWith('/messages')?history(identifier(1),url.searchParams.has('after_seq')?[]:[record]):undefined);
  await ui.app.enter();assert.match(ui.$('.chat-day').textContent,/24 октября/);assert.equal(ui.$('.meta time').textContent,'01:30');
  ui.state.workspace.timezone='Etc/UTC';await ui.poll();
  assert.match(ui.$('.chat-day').textContent,/23 октября/);assert.equal(ui.$('.meta time').textContent,'22:30');
});

function bootstrapPayload(rows=[conversation(1)],messages=[message(1)]) {
  return {context:{channels:[{id:'native',name:'CRM',kind:'alfa_max_browser'}],branches:[],preferences:{name_order:'parent_first'}},
    listing:{conversations:rows,order:rows.map(row=>row.id),window_token:'bootstrap',reset:true,page:0,total:rows.length,unread:rows.filter(row=>row.unread).length,has_more:false},
    history:rows.length?history(rows[0].id,messages,{conversation:rows[0]}):null};
}
async function runTimer(ui,delay) {
  const entry=[...ui.timers.entries()].find(([,timer])=>timer.delay===delay);
  assert.ok(entry,`Expected ${delay}ms timer`);ui.timers.delete(entry[0]);await entry[1].callback();await flush();
}
function eventSources(ui) {
  const sources=[];
  ui.window.EventSource=class extends ui.window.EventTarget {
    constructor(url){super();this.url=url;this.closed=false;sources.push(this);}
    close(){this.closed=true;}
    emit(type){this.dispatchEvent(new ui.window.Event(type));}
  };
  return sources;
}

test('protocol two opens context, first dialogs and messages from one bootstrap request',async t=>{
  const ui=setup(t,url=>url.pathname.endsWith('/bootstrap')?bootstrapPayload([conversation(1)],[message(1),message(2)]):undefined);
  ui.state.chatProtocol=2;await ui.app.enter();
  assert.deepEqual(ui.ids(),[1,2].map(identifier));
  assert.equal(ui.calls.length,1);assert.ok(ui.calls[0].pathname.endsWith('/bootstrap'));
  assert.match(ui.$('#chat-person').textContent,/Parent 1/);
});

test('a transient bootstrap outage falls back once to independent authorized reads',async t=>{
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))throw Object.assign(new Error('Service unavailable'),{status:503});
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[message(1)]);
  });
  ui.state.chatProtocol=2;await ui.app.enter();
  assert.deepEqual(ui.ids(),[identifier(1)]);
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/bootstrap')).length,1);
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/context')).length,1);
  assert.equal(ui.$('#chat-error').textContent,'');
});

test('bootstrap authorization failures cannot fall back through another route',async t=>{
  const ui=setup(t,url=>{if(url.pathname.endsWith('/bootstrap'))throw Object.assign(new Error('No access'),{status:403});});
  ui.state.chatProtocol=2;await ui.app.enter();
  assert.equal(ui.calls.length,1);assert.equal(ui.$('#chat-error').textContent,'No access');
});

test('a partial bootstrap displays its dialogs before retrying the failed first timeline',async t=>{
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))return {...bootstrapPayload(),history:null,history_pending:true};
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[message(1)]);
  });
  ui.state.chatProtocol=2;await ui.app.enter();
  assert.match(ui.$('.row-name').textContent,/Parent 1/);assert.equal(ui.calls.length,1);
  assert.match(ui.$('#chat-history').textContent,/Открываем переписку/);
  await ui.poll();assert.deepEqual(ui.ids(),[identifier(1)]);
  assert.equal(ui.$('#chat-error').textContent,'');
});

test('a failed first history read stays visible through list success and clears only on history recovery',async t=>{
  let clock=1000,reads=0;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages')){
      if(++reads===1)throw Object.assign(new Error('Service unavailable'),{status:503});
      return history(identifier(1),[message(1)]);
    }
  });
  ui.window.Date.now=()=>clock;
  await ui.app.enter();assert.equal(ui.$('#chat-error').textContent,'Service unavailable');
  await ui.poll();assert.equal(reads,1);
  assert.equal(ui.$('#chat-error').textContent,'Service unavailable');
  clock+=9000;await ui.poll();assert.deepEqual(ui.ids(),[identifier(1)]);
  assert.equal(ui.$('#chat-error').textContent,'');
});

test('a healthy event stream does not postpone recovery of an unloaded timeline for forty five seconds',async t=>{
  const payload={...bootstrapPayload(),history:null,history_pending:true};
  payload.listing.version='1:2026-10-26';
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))return payload;
    if(url.pathname.endsWith('/chat-updates'))return {next_seq:1,updates:[],has_more:false};
  });
  ui.state.chatProtocol=2;const sources=eventSources(ui);await ui.app.enter();ui.app.watch();
  sources[0].dispatchEvent(new ui.window.MessageEvent('ready',{data:JSON.stringify({version:payload.listing.version})}));
  await flush();await ui.poll();
  assert.ok(ui.calls.some(url=>url.pathname.endsWith('/messages')));
  assert.equal(ui.$('#chat-error').textContent,'');
});

test('late bootstrap cannot replace a filter and client chosen while it was loading',async t=>{
  let release;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))return new Promise(resolve=>{release=()=>resolve(bootstrapPayload());});
    if(url.pathname.endsWith('/chats'))return {conversations:[conversation(2)],page:0,total:1,unread:1,has_more:false};
    if(url.pathname.endsWith('/messages'))return history(identifier(2),[message(2)],{conversation:conversation(2)});
  });
  ui.state.chatProtocol=2;const opening=ui.app.enter();await flush();
  ui.$('#chat-status').value='lead';ui.$('#chat-status').dispatchEvent(new ui.window.Event('change'));await flush();
  release();await opening;assert.deepEqual(ui.ids(),[identifier(2)]);
  assert.equal(ui.$('#chat-status').value,'lead');assert.match(ui.$('.row-name').textContent,/Parent 2/);
});

test('protocol two fetches forty rows and preloads upward before the last 180 pixels',async t=>{
  let before;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))return {...bootstrapPayload(),history:history(identifier(1),[message(2)],{has_more:true})};
    if(url.pathname.endsWith('/messages')){before=url;return history(identifier(1),[message(1)]);}
  });
  ui.state.chatProtocol=2;await ui.app.enter();Object.defineProperty(ui.$('#chat-stream'),'clientHeight',{get:()=>600});
  ui.$('#chat-stream').scrollTop=700;ui.$('#chat-stream').dispatchEvent(new ui.window.WheelEvent('wheel',{deltaY:-100}));await flush();
  assert.equal(before.searchParams.get('limit'),'40');assert.equal(before.searchParams.get('before_id'),identifier(2));
  assert.deepEqual(ui.ids(),[1,2].map(identifier));
});

test('compact card readiness and unchanged versions retain the selected card',async t=>{
  const card=preparedFamily(1);card.version='a'.repeat(64);card.cache.auto_refresh=true;card.students[0].subscriptions=[];
  for(const source of card.students[0].cards){delete source.subscriptions;delete source.lessons;source.subscriptions_ready=true;source.lessons_ready=true;}
  const row={...conversation(1),contact_id:identifier(91)};
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))return bootstrapPayload([row],[]);
    if(url.pathname.endsWith('/family')){
      assert.equal(url.searchParams.get('compact'),'true');
      return url.searchParams.get('known_version')===card.version?{not_modified:true,version:card.version}:JSON.parse(JSON.stringify(card));
    }
  });
  ui.state.chatProtocol=2;dockFamily(ui);await ui.app.enter();ui.$('#chat-details').click();await flush();
  const stats=ui.$('.family-stats');assert.doesNotMatch(ui.$('#chat-details-student').textContent,/Готовим подробную карточку/);
  await runTimer(ui,60000);assert.equal(ui.$('.family-stats'),stats);
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/family')).at(-1).searchParams.get('known_version'),card.version);
  card.version='b'.repeat(64);card.cache.updated_at='2026-10-24T01:00:00Z';
  await runTimer(ui,60000);assert.equal(ui.$('.family-stats'),stats);
  assert.match(ui.$('#chat-details-sync').textContent,/01:00/);
});

test('event stream changes refresh chats, healthy idle uses a safety timer, and failures restore polling',async t=>{
  let changed=false;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))return bootstrapPayload();
    if(url.pathname.endsWith('/chat-updates'))return {next_seq:changed?101:100,updates:[],has_more:false};
    if(url.pathname.endsWith('/messages'))return history(identifier(1),changed?[message(2)]:[],{next_seq:changed?101:100});
  });
  ui.state.chatProtocol=2;const sources=eventSources(ui);await ui.app.enter();ui.app.watch();
  assert.equal(sources.length,1);sources[0].emit('ready');await flush();await runTimer(ui,100);
  assert.ok([...ui.timers.values()].some(timer=>timer.delay===45000));
  assert.equal([...ui.timers.values()].some(timer=>timer.delay===3500),false);
  changed=true;sources[0].emit('change');await flush();await runTimer(ui,80);assert.deepEqual(ui.ids(),[1,2].map(identifier));
  sources[0].onerror();assert.equal(sources[0].closed,true);await runTimer(ui,100);
  assert.ok([...ui.timers.values()].some(timer=>timer.delay===2500));
  await runTimer(ui,30000);assert.equal(sources.length,2);
});

test('stream heartbeats cannot cancel notification catchup, and access loss clears private chat data',async t=>{
  let reads=0;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))return bootstrapPayload();
    if(url.pathname.endsWith('/chat-updates'))return {next_seq:++reads,updates:[],has_more:reads===1};
  });
  ui.state.chatProtocol=2;const sources=eventSources(ui);await ui.app.enter();ui.app.watch();sources[0].emit('ready');await flush();
  sources[0].emit('pulse');await runTimer(ui,150);assert.equal(reads,2);
  let revoked=0;ui.window.document.addEventListener('cm:chat-access-lost',()=>revoked++);
  sources[0].emit('access_lost');assert.equal(sources[0].closed,true);assert.equal(revoked,1);assert.deepEqual(ui.ids(),[]);
  const count=ui.calls.length;sources[0].emit('change');await flush();assert.equal(ui.calls.length,count);
});

test('a connecting stream without a first frame falls back quietly and retries later',async t=>{
  const ui=setup(t,url=>url.pathname.endsWith('/chat-updates')?{next_seq:1,updates:[],has_more:false}:undefined);
  ui.state.chatProtocol=2;const sources=eventSources(ui);ui.app.watch();await runTimer(ui,6000);
  assert.equal(sources[0].closed,true);assert.equal(ui.$('#chat-error').textContent,'');
  assert.ok([...ui.timers.values()].some(timer=>timer.delay===30000));
});

test('a healthy stream retries failed notification reads without requiring a new event',async t=>{
  let reads=0;
  const ui=setup(t,url=>{if(url.pathname.endsWith('/chat-updates')){if(++reads===1)throw new Error('Temporary outage');return {next_seq:1,updates:[],has_more:false};}});
  ui.state.chatProtocol=2;const sources=eventSources(ui);ui.app.watch();sources[0].emit('ready');await flush();
  sources[0].emit('pulse');await runTimer(ui,10000);assert.equal(reads,2);assert.equal(ui.$('#chat-error').textContent,'');
});

test('a long dialog list renders a bounded window and preserves its visible client when priorities change',async t=>{
  let rows=Array.from({length:600},(_,n)=>conversation(n+1));
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/bootstrap'))return bootstrapPayload(rows,[]);
    if(url.pathname.endsWith('/chats'))return {conversations:rows,page:0,total:rows.length,unread:rows.length,has_more:false};
  },{autoFrames:true});
  ui.state.chatProtocol=2;
  const scroll=ui.$('.chat-list-scroll'),list=ui.$('#chat-list');
  Object.defineProperties(scroll,{clientHeight:{get:()=>600},clientWidth:{get:()=>360}});
  scroll.getBoundingClientRect=()=>({top:0,bottom:600,width:360,height:600});
  const original=ui.window.HTMLElement.prototype.getBoundingClientRect;
  ui.window.HTMLElement.prototype.getBoundingClientRect=function(){
    if(this===list)return {top:-scroll.scrollTop,bottom:0,width:360,height:0};
    if(this.classList.contains('row-wrap')){
      let top=-scroll.scrollTop;for(let previous=this.previousElementSibling;previous;previous=previous.previousElementSibling)top+=previous.dataset.key?84:parseFloat(previous.style.height)||0;
      return {top,bottom:top+84,width:360,height:84};
    }
    return original.call(this);
  };
  await ui.app.enter();assert.ok(list.querySelectorAll('.chat-row').length<100);
  scroll.scrollTop=15000;scroll.dispatchEvent(new ui.window.Event('scroll'));await flush();
  assert.ok(list.querySelectorAll('.chat-row').length<100);
  const anchor=[...list.children].find(row=>row.dataset.key&&row.getBoundingClientRect().bottom>0),top=anchor.getBoundingClientRect().top;
  const id=anchor.dataset.key;
  rows=rows.map((row,n)=>n===599?{...row,last_message_at:'2026-10-26T12:00:00Z'}:row);
  await ui.poll();const retained=list.querySelector(`[data-key="${id}"]`);
  assert.ok(retained);assert.equal(retained.getBoundingClientRect().top,top);
  assert.equal(ui.$('#chat-visible-count').textContent,'600 из 600');
  scroll.scrollTop=0;scroll.dispatchEvent(new ui.window.Event('scroll'));await flush();
  assert.equal(list.querySelector('.chat-row').dataset.id,identifier(600));
});

test('an oversized dictated draft stays intact and cannot be submitted until shortened',async t=>{
 let sent=0;const ui=setup(t,(url,args)=>{if(url.pathname.endsWith('/messages')&&args?.method==='POST')sent++;});await ui.app.enter();
 const input=ui.$('#chat-input');input.value='Текст '.repeat(800);input.dispatchEvent(new ui.window.Event('input',{bubbles:true}));
 assert.equal(ui.$('#chat-send').disabled,true);assert.match(ui.$('#chat-send-hint').textContent,/4000/);
 ui.$('#chat-compose').dispatchEvent(new ui.window.Event('submit',{cancelable:true}));await flush();
 assert.equal(input.value,'Текст '.repeat(800));assert.equal(sent,0);
 input.value='Короткий ответ';input.dispatchEvent(new ui.window.Event('input',{bubbles:true}));
 assert.equal(ui.$('#chat-send').disabled,false);assert.equal(ui.$('#chat-send-hint').hidden,true);
});

test('a lost send response retains the request ID and a new voice draft can be sent',async t=>{
  const posts=[],saved=new Map();
  const ui=setup(t,(url,options)=>{
    if(!url.pathname.endsWith('/messages'))return;
    if(options?.method==='POST'){
      const payload=options.data;posts.push(payload);
      if(!saved.has(payload.request_id))saved.set(payload.request_id,message(200+saved.size,{
        text:payload.text,direction:'out',actor_type:'human',state:'accepted',request_id:payload.request_id
      }));
      if(posts.length===1){const error=new Error('Ответ сервера потерян');error.name='NetworkError';throw error;}
      return {command_id:identifier(posts.length),state:'accepted',request_id:payload.request_id};
    }
    return history(identifier(1),[...saved.values()]);
  });
  await ui.app.enter();
  const input=ui.$('#chat-input'),submit=()=>ui.$('#chat-compose').dispatchEvent(new ui.window.Event('submit',{cancelable:true}));
  input.value='Можно во вторник с 16:30 до 18:00';input.dispatchEvent(new ui.window.Event('input',{bubbles:true}));
  submit();await flush();
  assert.equal(input.value,'');
  assert.equal(ui.$('#chat-send').classList.contains('is-retry'),true);
  input.value='Голосовой текст';input.dispatchEvent(new ui.window.Event('input',{bubbles:true}));
  assert.equal(input.disabled,false);
  assert.equal(ui.$('#chat-send').disabled,false);
  assert.equal(ui.$('#chat-send').classList.contains('is-retry'),false);
  submit();await flush();
  assert.equal(posts.length,3);
  assert.equal(posts[0].request_id,posts[1].request_id,'the uncertain request is retried idempotently');
  assert.notEqual(posts[1].request_id,posts[2].request_id);
  assert.deepEqual([...saved.values()].map(item=>item.text),['Можно во вторник с 16:30 до 18:00','Голосовой текст']);
  assert.equal(input.value,'');
  assert.equal(ui.$('#chat-send').classList.contains('is-retry'),false);
  assert.equal(ui.ids().length,2);
});

test('voice transcription during an in-flight send survives its confirmation and history poll',async t=>{
  let release,first=true,oldRequest;
  const saved=[];
  const ui=setup(t,(url,options)=>{
    if(!url.pathname.endsWith('/messages'))return;
    if(options?.method==='POST'){
      if(first){first=false;oldRequest=options.data.request_id;
        saved.push(message(211,{text:options.data.text,direction:'out',actor_type:'human',request_id:oldRequest}));
        return new Promise(resolve=>{release=()=>resolve({command_id:identifier(1),state:'queued',request_id:oldRequest});});
      }
      saved.push(message(212,{text:options.data.text,direction:'out',actor_type:'human',request_id:options.data.request_id}));
      return {command_id:identifier(2),state:'queued',request_id:options.data.request_id};
    }
    return history(identifier(1),saved);
  });
  await ui.app.enter();
  const input=ui.$('#chat-input');input.value='Старый ответ';input.dispatchEvent(new ui.window.Event('input',{bubbles:true}));
  ui.$('#chat-compose').dispatchEvent(new ui.window.Event('submit',{cancelable:true}));await flush();
  assert.equal(input.value,'');
  input.value='Текст из записи';input.dispatchEvent(new ui.window.Event('input',{bubbles:true}));
  release();await flush();
  assert.equal(input.value,'Текст из записи');
  assert.equal(ui.$('#chat-send').disabled,false);
  assert.equal(ui.$('#chat-send').classList.contains('is-retry'),false);
  ui.$('#chat-compose').dispatchEvent(new ui.window.Event('submit',{cancelable:true}));await flush();
  assert.deepEqual(saved.map(item=>item.text),['Старый ответ','Текст из записи']);
  assert.equal(ui.ids().length,2);
});


test("cancelled outgoing message shows a warning rather than a success check", async t => {
  const ui = setup(t, url => url.pathname.endsWith("/messages") ? history(identifier(1), [message(42, {
    direction: "out", actor_type: "human", state: "cancelled", error: "Отправка отменена: доступ сотрудника изменён."
  })]) : undefined);
  ui.window.CenterIcon = name => { const span = ui.window.document.createElement("span"); span.dataset.icon = name; return span; };
  await ui.app.enter(); await flush();
  const status = ui.$("#chat-history .state-cancelled");
  assert.ok(status);
  assert.ok(status.querySelector('[data-icon="warn"]'));
  assert.equal(status.querySelector('[data-icon="check"]'), null);
  assert.match(ui.$("#chat-history .chat-message-error").textContent, /Отправка отменена/);
});

test("a missing or ambiguous MAX recipient cannot be resent into another failed bubble", async t => {
  for (const code of ["recipient_missing", "multiple_recipients", "invalid_recipient"]) {
    const ui = setup(t, url => url.pathname.endsWith("/messages") ? history(identifier(1), [message(42, {
      direction: "out", actor_type: "human", state: "failed", text: "Сообщение",
      error_code: code, error: "Не удалось определить получателя MAX."
    })]) : undefined);
    await ui.app.enter(); await flush();
    assert.equal(ui.$("#chat-history .chat-retry-send"), null);
    assert.equal(ui.$("#chat-history .chat-retry-edit"), null);
    assert.match(ui.$("#chat-history .chat-message-error").textContent, /Проверьте адресата/);
    assert.equal(ui.calls.filter(url => url.pathname.endsWith("/messages") && url.searchParams.has("request_id")).length, 0);
  }
});

test('unknown CRM identity shows exact ID and retry reason without forcing manual linking', async t => {
  const items = [{ ...conversation(1), contact_id: null, status: 'unlinked',
    identity_resolution: {state:'queued', text:'Карточка ожидает загрузки из CRM', customer_id:'99'} }];
  const ui = setup(t, url => url.pathname.endsWith('/messages') ? history(identifier(1), [message(1)], {conversation:items[0]}) : undefined, {conversations:items});
  dockFamily(ui); await ui.app.enter(); ui.$('#chat-details').click(); await flush();
  assert.equal(ui.$('#chat-details-dialog').open, true);
  assert.match(ui.$('#chat-details-contact').textContent, /ожидает загрузки.*ID CRM: 99/);
  assert.equal(ui.calls.filter(url=>url.pathname.endsWith('/family')).length, 0);
  items[0] = {...items[0], identity_resolution:{state:'not_found',text:'CRM не вернула карточку с этим ID, включая архив',customer_id:'99',next_at:'2026-10-27T12:00:00Z'}};
  await ui.poll(); await flush();
  assert.match(ui.$('#chat-details-contact').textContent,/не вернула карточку/);
  assert.match(ui.$('#chat-details-sync').textContent,/Следующая проверка/);
});

test('identity refresh does not repeatedly reopen an unrelated failed family request on every chat poll', async t => {
  const items = [{...conversation(1),contact_id:identifier(91)}];
  let reads=0;
  const ui=setup(t,url=>{
    if(url.pathname.endsWith('/messages'))return history(identifier(1),[message(1)],{conversation:items[0]});
    if(url.pathname.endsWith('/family')){reads++;throw Object.assign(new Error('Unavailable'),{status:503});}
  },{conversations:items});
  dockFamily(ui);await ui.app.enter();ui.$('#chat-details').click();await flush();
  assert.equal(reads,1);await ui.poll();await flush();await ui.poll();await flush();
  assert.equal(reads,1);
});
