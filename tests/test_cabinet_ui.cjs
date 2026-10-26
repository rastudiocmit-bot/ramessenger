"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const web = path.join(__dirname, "../app/backend/src/center_messenger/web");
const all = { summary: true, messages: true, queue: true, clients: true, team: true, connections: true, overview: true, security: true, profile: true };
function setup(t, handler = async () => ({}), role = "owner") {
  const dom = new JSDOM(fs.readFileSync(web + "/index.html", "utf8"), { url: "https://cabinet.example.test", runScripts: "outside-only" });
  const { window } = dom, $ = selector => window.document.querySelector(selector);
  if (!window.HTMLDialogElement.prototype.showModal) window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  if (!window.HTMLDialogElement.prototype.close) window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const state = { user: { id: "user" }, workspace: { id: "first", name: "First", timezone: "Europe/Moscow", role }, epoch: 1, pane: "messages", chatView: "all" };
  window.setTimeout = () => 1; window.clearTimeout = () => {};
  window.eval(fs.readFileSync(web + "/icons.js", "utf8"));
  window.eval(fs.readFileSync(web + "/cabinet.js", "utf8"));
  const calls = [];
  const app = window.CenterCabinet({ $, state, api: handler,
    node: (tag, value, cls) => { const e = window.document.createElement(tag); if (value !== undefined) e.textContent = value; if (cls) e.className = cls; return e; },
    show: (e, visible = true) => { e.hidden = !visible; }, roles: { owner: "Владелец", operator: "Оператор", observer: "Наблюдатель" },
    renderPane: async value => { calls.push(value); } });
  t.after(() => { app.leave(); window.close(); });
  return { app, $, state, window, calls };
}
test("owner gets the supplied desktop navigation and three primary mobile tabs", t => {
  const ui = setup(t); ui.app.update(all);
  assert.deepEqual([...ui.$("#navrail").children].map(n => n.dataset.pane), ["messages", "summary", "clients", "team", "connections", "overview", "security", "profile"]);
  assert.deepEqual([...ui.$("#tabbar").querySelectorAll("[data-pane]")].map(n => n.dataset.pane), ["messages", "summary", "clients"]);
  ui.$("#more-tab").click();
  assert.equal(ui.$("#navigation-options").querySelectorAll("[data-pane]").length, 5);
});
test("operator uses the unified chats list and cannot show administrative sections", async t => {
  const ui = setup(t, undefined, "operator");
  ui.app.update({ ...all, summary: false, team: false, clients: false, connections: false });
  assert.equal(ui.$("#team-tab"), null); assert.equal(ui.$("#connections-tab"), null);
  assert.equal(ui.$("#queue-tab"), null);
  await ui.app.navigate("queue", "unread");
  assert.deepEqual(ui.calls, ["messages"]); assert.equal(ui.state.chatView, "all");
});
test("observer receives only granted sections and channel settings remain read only", t => {
  const ui = setup(t, undefined, "observer");
  ui.app.update({ ...all, team: false, connections: false });
  assert.equal(ui.$("#team-tab"), null); assert.equal(ui.$("#connections-tab"), null);
  assert.equal(ui.$("#profile-connections").hasAttribute("data-pane"), false);
  assert.ok(ui.$("#clients-tab"));
});
test("workspace switch clears private metrics and ignores the old summary response", async t => {
  let resolve;
  const ui = setup(t, () => new Promise(yes => { resolve = yes; }));
  ui.state.pane = "summary"; ui.app.update(all);
  const pending = ui.app.summary();
  ui.$("#summary-content").textContent = "Old private counts";
  ui.state.epoch += 1; ui.state.workspace = { ...ui.state.workspace, id: "second", name: "Second" }; ui.app.update(all);
  resolve({ incoming: 999, days: [], branches: [], operators: [] }); await pending;
  assert.doesNotMatch(ui.$("#summary-content").textContent, /999|Old private/);
  assert.equal(ui.$("#profile-workspace").textContent, "Second");
});
test("an old metadata response cannot replace the new workspace's branches", async t => {
  let resolve;
  const ui = setup(t, () => new Promise(yes => { resolve = yes; })); ui.app.update(all);
  const pending = ui.app.metadata();
  ui.state.epoch += 1; ui.state.workspace.id = "second"; ui.app.update(all);
  resolve({ channels: [{ enabled: true }], branches: [{ id: "1", name: "Private old branch" }] }); await pending;
  assert.equal(ui.$("#profile-channels").textContent, "");
  assert.doesNotMatch(ui.$("#organization-branches").textContent, /Private/);
});

const summaryData = { incoming: 12, previous_incoming: 2, waiting: 3, new_leads: 1,
  average_response_seconds: 60, days: [], branches: [], operators: [], computed_at: "2026-10-26T12:00:00Z" };
test("summary retains last good metrics with timestamp and retry after temporary failure", async t => {
  let fail = false;
  const ui = setup(t, async () => { if (fail) throw Object.assign(new Error("Сервис временно недоступен."), { status: 503 }); return summaryData; });
  ui.state.pane = "summary"; ui.app.update(all); await ui.app.summary(); fail = true; await ui.app.summary();
  assert.ok(ui.$("#summary-content .kpi-grid"));
  assert.match(ui.$("#summary-error").textContent, /Показаны данные на.*26\.10\.2026/);
  assert.equal(ui.$("#summary-error button").textContent, "Повторить");
});
test("first summary failure ends loading without inventing zeros", async t => {
  const ui = setup(t, async () => { throw new Error("Unavailable"); });
  ui.state.pane = "summary"; ui.app.update(all); await ui.app.summary();
  assert.match(ui.$("#summary-content").textContent, /пока недоступны/);
  assert.equal(ui.$("#summary-content .kpi-grid"), null);
});
test("summary stops duplicate requests and aborts on leaving", async t => {
  let resolve, signal, count = 0;
  const ui = setup(t, (path, options) => { count++; signal = options.signal; return new Promise(yes => { resolve = yes; }); });
  ui.state.pane = "summary"; ui.app.update(all);
  const pending = ui.app.summary(); await ui.app.summary(); assert.equal(count, 1);
  ui.app.leave(); assert.equal(signal.aborted, true); resolve(summaryData); await pending;
  assert.equal(ui.$("#summary-content .kpi-grid"), null);
});
test("summary permission rejection removes previously rendered private metrics", async t => {
  let denied = false;
  const ui = setup(t, async () => { if (denied) throw Object.assign(new Error("Нет доступа"), { status: 403 }); return summaryData; });
  ui.state.pane = "summary"; ui.app.update(all); await ui.app.summary(); denied = true; await ui.app.summary();
  assert.equal(ui.$("#summary-content").textContent, "");
  assert.equal(ui.$("#summary-error button"), null);
});
test("logout reset clears private summary and error text", async t => {
  const ui = setup(t, async () => summaryData);
  ui.state.pane = "summary"; ui.app.update(all); await ui.app.summary(); ui.app.reset();
  assert.equal(ui.$("#summary-content").textContent, "");
  assert.equal(ui.$("#summary-error").textContent, "");
});
