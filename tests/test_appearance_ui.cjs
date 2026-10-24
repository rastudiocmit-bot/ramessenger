"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const web = path.join(__dirname, "../app/backend/src/center_messenger/web");
const markup = fs.readFileSync(path.join(web, "index.html"), "utf8");
const source = fs.readFileSync(path.join(web, "appearance.js"), "utf8");

async function setup(t, { saved, blocked = false } = {}) {
  const dom = new JSDOM(markup, { url: "https://messenger.example.test", runScripts: "outside-only" });
  const { window } = dom;
  t.after(() => window.close());
  // An operating system preference is deliberately irrelevant to application theme.
  window.matchMedia = () => ({ matches: true });
  if (saved !== undefined) window.localStorage.setItem("cm-theme", saved);
  if (blocked) Object.defineProperty(window, "localStorage", { get() { throw new Error("Storage blocked"); } });
  window.eval(source);
  if (window.document.readyState === "loading") await new Promise(resolve => window.document.addEventListener("DOMContentLoaded", resolve, { once: true }));
  const $ = selector => window.document.querySelector(selector);
  const change = value => { $("#chat-theme").value = value; $("#chat-theme").dispatchEvent(new window.Event("change")); };
  const theme = () => window.document.documentElement.dataset.theme;
  return { window, $, theme, change };
}

test("default theme stays light even with a dark system preference", async t => {
  const ui = await setup(t);
  assert.equal(ui.theme(), "light");
  assert.equal(ui.$("#chat-theme").value, "light");
  assert.equal(ui.$('meta[name="color-scheme"]').content, "light");
});

test("saved theme applies before settings bind and survives a new page", async t => {
  const ui = await setup(t, { saved: "dark" });
  assert.equal(ui.theme(), "dark");
  assert.equal(ui.$("#chat-theme").value, "dark");
  ui.change("light");
  assert.equal(ui.window.localStorage.getItem("cm-theme"), "light");
  const next = await setup(t, { saved: ui.window.localStorage.getItem("cm-theme") });
  assert.equal(next.theme(), "light");
});

test("invalid saved preference falls back to light without executing stored content", async t => {
  const ui = await setup(t, { saved: '<script>throw "invalid"</script>' });
  assert.equal(ui.theme(), "light");
  assert.equal(ui.$("#chat-theme").value, "light");
});

test("changing theme preserves unrelated notification preferences", async t => {
  const ui = await setup(t);
  ui.window.localStorage.setItem("cm-sound", '{"enabled":true,"volume":55}');
  ui.change("dark");
  assert.equal(ui.theme(), "dark");
  assert.equal(ui.$('meta[name="color-scheme"]').content, "dark");
  assert.equal(ui.window.localStorage.getItem("cm-theme"), "dark");
  assert.equal(ui.window.localStorage.getItem("cm-sound"), '{"enabled":true,"volume":55}');
});

test("blocked storage still permits a theme change and explains missing persistence", async t => {
  const ui = await setup(t, { blocked: true });
  ui.change("dark");
  assert.equal(ui.theme(), "dark");
  assert.equal(ui.$("#chat-theme-status").hidden, false);
  assert.match(ui.$("#chat-theme-status").textContent, /запретил сохранение/);
});

test("another tab updates appearance while other storage keys and session storage are ignored", async t => {
  const ui = await setup(t);
  const dispatch = options => ui.window.dispatchEvent(new ui.window.StorageEvent("storage", options));
  dispatch({ key: "cm-theme", newValue: "dark", storageArea: ui.window.localStorage });
  assert.equal(ui.theme(), "dark");
  assert.equal(ui.$("#chat-theme").value, "dark");
  dispatch({ key: "cm-sound", newValue: "light", storageArea: ui.window.localStorage });
  dispatch({ key: null, newValue: null, storageArea: ui.window.sessionStorage });
  assert.equal(ui.theme(), "dark");
  dispatch({ key: null, newValue: null, storageArea: ui.window.localStorage });
  assert.equal(ui.theme(), "light");
});
