"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const web = path.join(__dirname, "../app/backend/src/center_messenger/web");

const settings = {
  preload_unanswered: true, preload_active_groups: true,
  finance_interval_minutes: 30, lessons_interval_minutes: 45,
  groups_interval_minutes: 360, history_interval_minutes: 0,
  requests_per_turn: 4, busy_retry_seconds: 5, retry_initial_seconds: 30,
  retry_max_seconds: 1800, manual_priority_seconds: 30,
  manual_interest_minutes: 10, manual_burst: 5,
  parallel_card_jobs: 1, host_request_gap_ms: 350,
  preload_active_cards: true, automatic_clients_only: false, pause_background_on_send: true,
  active_interval_minutes: 720, requests_per_minute: 120, manual_max_wait_seconds: 60,
};

function setup(t, handler) {
  const dom = new JSDOM(fs.readFileSync(web + "/index.html", "utf8"), { runScripts: "outside-only" });
  const { window } = dom, $ = selector => window.document.querySelector(selector);
  window.eval(fs.readFileSync(web + "/card-loader.js", "utf8"));
  const calls = [];
  const state = { workspace: { id: "workspace", role: "owner" } };
  const api = async (url, options = {}) => { calls.push([url, options]); return handler ? handler(url, options) : { ...settings, ...(options.data || {}) }; };
  const loader = window.CenterCardLoader({ $, state, api, show: (element, visible = true) => { element.hidden = !visible; } });
  t.after(() => window.close());
  return { $, state, calls, loader, window };
}

test("wizard contains one loader form with visible defaults before the API responds", async t => {
  let resolve;
  const ui = setup(t, () => new Promise(done => { resolve = done; }));
  const form = ui.$("#card-loader-form");
  assert.equal(form.closest('[role="tabpanel"]').id, "connections-wizard-view");
  assert.equal(ui.$("#connections-settings-view #card-loader-form"), null);
  assert.equal(ui.window.document.querySelectorAll("#card-loader-form").length, 1);
  assert.equal(form.elements.finance_interval_minutes.value, "30");
  assert.equal(form.elements.lessons_interval_minutes.value, "30");
  assert.equal(form.elements.groups_interval_minutes.value, "360");
  assert.equal(form.elements.history_interval_minutes.value, "0");
  assert.equal(form.elements.preload_unanswered.checked, true);
  assert.equal(form.elements.preload_active_groups.checked, true);
  const first = ui.loader.load(), second = ui.loader.load();
  assert.equal(ui.calls.length, 1);
  assert.equal(form.querySelector('button[type="submit"]').disabled, true);
  resolve(settings); await Promise.all([first, second]);
  assert.equal(form.elements.lessons_interval_minutes.value, "45");
  assert.equal(form.querySelector('button[type="submit"]').disabled, false);
});

test("failed settings read keeps defaults, shows retry and never saves over unknown settings", async t => {
  let failing = true;
  const ui = setup(t, () => { if (failing) throw new Error("Сервис временно недоступен"); return settings; });
  await ui.loader.load();
  const form = ui.$("#card-loader-form");
  assert.equal(form.elements.retry_initial_seconds.value, "30");
  assert.equal(ui.$("#card-loader-error").hidden, false);
  assert.match(ui.$("#card-loader-error-text").textContent, /Не удалось прочитать/);
  form.dispatchEvent(new ui.window.Event("submit", { cancelable: true }));
  assert.equal(ui.calls.length, 1);
  failing = false; ui.$("#card-loader-retry").click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.$("#card-loader-error").hidden, true);
  assert.equal(form.elements.lessons_interval_minutes.value, "45");
  assert.equal(form.querySelector('button[type="submit"]').disabled, false);
});

test("API intervals outside presets remain visible and do not turn into an empty selection", async t => {
  const ui = setup(t, () => ({ ...settings, history_interval_minutes: 480 }));
  await ui.loader.load();
  assert.equal(ui.$("#card-loader-form").elements.history_interval_minutes.value, "480");
});

test("reset restores defaults and fences a save response from the previous workspace", async t => {
  let resolve;
  const ui = setup(t, (_, options) => options.method === "PUT" ? new Promise(done => { resolve = done; }) : settings);
  await ui.loader.load();
  ui.$("#card-loader-form").dispatchEvent(new ui.window.Event("submit", { cancelable: true }));
  ui.state.workspace = { id: "another", role: "owner" }; ui.loader.reset();
  resolve({ ...settings, finance_interval_minutes: 80 });
  await new Promise(done => setImmediate(done));
  const form = ui.$("#card-loader-form");
  assert.equal(form.elements.finance_interval_minutes.value, "30");
  assert.equal(form.elements.lessons_interval_minutes.value, "30");
  assert.equal(form.querySelector('button[type="submit"]').disabled, true);
  assert.equal(form.querySelector('button[type="submit"]').textContent, "Сохранить настройки загрузчика");
});

test("card loader reads persisted policy and explains fixed concurrency", async t => {
  const ui = setup(t); await ui.loader.load();
  assert.equal(ui.calls[0][0], "/workspaces/workspace/directory/card-loader");
  assert.equal(ui.$("#card-loader-form").elements.lessons_interval_minutes.value, "45");
  assert.equal(ui.$("#card-loader-form").elements.history_interval_minutes.value, "0");
  assert.match(ui.$("#card-loader-limits").textContent, /1 карточка одновременно.*120 запросов/);
  assert.match(ui.$("#card-loader-status").textContent, /Значения можно изменить/);
});

test("card loader saves typed values and preload switches", async t => {
  const ui = setup(t); await ui.loader.load();
  const form = ui.$("#card-loader-form");
  form.elements.preload_unanswered.checked = false;
  form.elements.requests_per_turn.value = "6";
  form.elements.parallel_card_jobs.value = "2";
  form.elements.requests_per_minute.value = "60";
  form.elements.active_interval_minutes.value = "360";
  form.elements.pause_background_on_send.checked = false;
  form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setImmediate(resolve));
  const [url, options] = ui.calls.at(-1);
  assert.equal(url, "/workspaces/workspace/directory/card-loader");
  assert.equal(options.method, "PUT");
  assert.equal(options.data.preload_unanswered, false);
  assert.equal(options.data.requests_per_turn, 6);
  assert.equal(options.data.parallel_card_jobs, 2);
  assert.equal(options.data.requests_per_minute, 60);
  assert.equal(options.data.active_interval_minutes, 360);
  assert.equal(options.data.pause_background_on_send, false);
  assert.match(ui.$("#card-loader-status").textContent, /сохранены/);
});

test("prefilled fields stay editable during loading and a late GET preserves the operator draft", async t => {
  let resolve;
  const ui = setup(t, () => new Promise(done => { resolve = done; }));
  const request = ui.loader.load(), form = ui.$("#card-loader-form");
  assert.equal(form.elements.finance_interval_minutes.disabled, false);
  form.elements.finance_interval_minutes.value = "90";
  form.elements.finance_interval_minutes.dispatchEvent(new ui.window.Event("input", { bubbles: true }));
  resolve({ ...settings, finance_interval_minutes: 60 }); await request;
  assert.equal(form.elements.finance_interval_minutes.value, "90");
  assert.equal(form.elements.lessons_interval_minutes.value, "45");
  assert.match(ui.$("#card-loader-status").textContent, /несохранённые/);
  assert.equal(form.querySelector('button[type="submit"]').disabled, false);
});

test("reopening the wizard preserves edits without a duplicate settings GET", async t => {
  const ui = setup(t); await ui.loader.load();
  const field = ui.$("#card-loader-form").elements.finance_interval_minutes;
  field.value = "75"; field.dispatchEvent(new ui.window.Event("input", { bubbles: true }));
  await ui.loader.load();
  assert.equal(field.value, "75"); assert.equal(ui.calls.length, 1);
});

test("a newer edit during saving survives the PUT response", async t => {
  let resolve;
  const ui = setup(t, (_, options) => options.method === "PUT" ? new Promise(done => { resolve = done; }) : settings);
  await ui.loader.load();
  const form = ui.$("#card-loader-form"), field = form.elements.finance_interval_minutes;
  field.value = "60"; form.dispatchEvent(new ui.window.Event("submit", { cancelable: true }));
  assert.equal(field.disabled, false);
  field.value = "90"; field.dispatchEvent(new ui.window.Event("input", { bubbles: true }));
  resolve({ ...settings, finance_interval_minutes: 60 });
  await new Promise(done => setImmediate(done));
  assert.equal(field.value, "90");
  assert.match(ui.$("#card-loader-status").textContent, /Более новые изменения ещё не сохранены/);
});

test("saved values are restored on a fresh page and defaults do not overwrite them", async t => {
  let stored = { ...settings };
  const handler = (_, options) => { if (options.data) stored = { ...stored, ...options.data }; return stored; };
  const first = setup(t, handler); await first.loader.load();
  const form = first.$("#card-loader-form"); form.elements.finance_interval_minutes.value = "85";
  form.elements.preload_active_cards.checked = false;
  form.dispatchEvent(new first.window.Event("submit", { cancelable: true }));
  await new Promise(done => setImmediate(done));
  const second = setup(t, handler); await second.loader.load();
  assert.equal(second.$("#card-loader-form").elements.finance_interval_minutes.value, "85");
  assert.equal(second.$("#card-loader-form").elements.preload_active_cards.checked, false);
});

test("invalid priority timing shows an inline explanation without sending a PUT", async t => {
  const ui = setup(t); await ui.loader.load();
  const form = ui.$("#card-loader-form"); form.elements.manual_priority_seconds.value = "90";
  form.dispatchEvent(new ui.window.Event("submit", { cancelable: true }));
  await new Promise(done => setImmediate(done));
  assert.equal(ui.calls.length, 1);
  assert.match(ui.$("#card-loader-error-text").textContent, /Порог задержки/);
  assert.equal(ui.$("#card-loader-retry").hidden, true);
});
