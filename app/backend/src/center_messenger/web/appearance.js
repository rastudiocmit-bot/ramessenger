"use strict";
(() => {
  const key = "cm-theme", root = document.documentElement;
  const normalize = value => value === "dark" ? "dark" : "light";
  let theme = "light";
  try { theme = normalize(localStorage.getItem(key)); } catch (_) { /* Storage is optional. */ }

  function apply(value) {
    theme = normalize(value);
    root.dataset.theme = theme;
    document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", theme);
    const control = document.querySelector("#chat-theme");
    if (control) control.value = theme;
  }
  // This script runs before stylesheets, so a saved dark theme does not flash light.
  // System colour preferences never change the explicitly chosen application theme.
  apply(theme);

  function bind() {
    const control = document.querySelector("#chat-theme");
    if (!control) return;
    control.value = theme;
    control.addEventListener("change", () => {
      apply(control.value);
      const status = document.querySelector("#chat-theme-status");
      try {
        localStorage.setItem(key, theme);
        if (status) { status.textContent = ""; status.hidden = true; }
      } catch (_) {
        if (status) {
          status.textContent = "Тема применена. Браузер запретил сохранение настройки; после закрытия страницы выбор может сброситься.";
          status.hidden = false;
        }
      }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind, { once: true });
  else bind();
  window.addEventListener("storage", event => {
    try { if (event.storageArea && event.storageArea !== localStorage) return; } catch (_) { return; }
    if (event.key === key || event.key === null) apply(event.newValue);
  });
})();
