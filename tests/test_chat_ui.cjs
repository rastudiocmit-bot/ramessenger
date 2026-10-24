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
  return { app, $, window, calls, poll, ids };
}

test("enter keeps all channels selected", async t => {
  const ui = setup(t);
  await ui.app.enter();
  assert.equal(ui.$("#chat-channel").value, "");
  assert.ok(!ui.calls.find(url => url.pathname.endsWith("/chats")).searchParams.has("connection"));
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
  const dispatch=(type,x)=>{const e=new ui.window.Event(type);Object.assign(e,{pointerType:'touch',clientX:x});row.dispatchEvent(e);};
  dispatch('pointerdown',200);dispatch('pointermove',80);dispatch('pointerup',80);await flush();
  assert.equal(body.seen_checkpoint.change_seq,100);
  assert.equal(body.seen_checkpoint.conversation_id,identifier(1));
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
