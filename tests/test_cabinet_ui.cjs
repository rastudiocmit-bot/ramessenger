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
