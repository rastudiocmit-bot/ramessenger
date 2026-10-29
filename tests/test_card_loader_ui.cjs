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
};

function setup(t) {
  const dom = new JSDOM(fs.readFileSync(web + "/index.html", "utf8"), { runScripts: "outside-only" });
  const { window } = dom, $ = selector => window.document.querySelector(selector);
  window.eval(fs.readFileSync(web + "/card-loader.js", "utf8"));
  const calls = [];
  const state = { workspace: { id: "workspace", role: "owner" } };
  const api = async (url, options = {}) => { calls.push([url, options]); return { ...settings, ...(options.data || {}) }; };
  const loader = window.CenterCardLoader({ $, state, api, show: (element, visible = true) => { element.hidden = !visible; } });
  t.after(() => window.close());
  return { $, state, calls, loader, window };
}

test("card loader reads persisted policy and explains fixed concurrency", async t => {
  const ui = setup(t); await ui.loader.load();
  assert.equal(ui.calls[0][0], "/workspaces/workspace/directory/card-loader");
  assert.equal(ui.$("#card-loader-form").elements.lessons_interval_minutes.value, "45");
  assert.equal(ui.$("#card-loader-form").elements.history_interval_minutes.value, "0");
  assert.match(ui.$("#card-loader-status").textContent, /1 карточка одновременно.*350 мс/);
});

test("card loader saves typed values and preload switches", async t => {
  const ui = setup(t); await ui.loader.load();
  const form = ui.$("#card-loader-form");
  form.elements.preload_unanswered.checked = false;
  form.elements.requests_per_turn.value = "6";
  form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setImmediate(resolve));
  const [url, options] = ui.calls.at(-1);
  assert.equal(url, "/workspaces/workspace/directory/card-loader");
  assert.equal(options.method, "PUT");
  assert.equal(options.data.preload_unanswered, false);
  assert.equal(options.data.requests_per_turn, 6);
  assert.match(ui.$("#card-loader-status").textContent, /сохранено/);
});
