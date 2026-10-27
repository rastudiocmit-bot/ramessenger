"use strict";
// Service notices and errors: a light glass capsule over the current screen.
// It never moves the layout underneath. A new notice shows expanded; routine
// ones vanish after 4 s, warnings and errors shrink after 8 s into a small
// counter pill. The pill opens a short list; every notice can be deleted.
// Operators can switch the whole thing off in «Настройки мессенджера»; then
// errors fall back to the inline line next to the form or the composer.
window.CenterNotices = ({ $ }) => {
  const STORE = "cm:service-notices";
  const LIFETIME = { info: 4000, success: 4000, warning: 8000, error: 8000 };
  const LIMIT = 20;
  const icons = {
    error: '<path d="M12 7v6M12 16.5v.5"/><circle cx="12" cy="12" r="9"/>',
    warning: '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2.5"/><path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.6v.4"/>',
    success: '<path d="m4 12.5 5 5L20 6.5"/>',
  };
  const svg = (paths, size = 18) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
  const cross = svg('<path d="M18 6 6 18M6 6l12 12"/>', 15);
  const chevron = svg('<path d="m6 15 6-6 6 6"/>', 16);

  let enabled = true;
  try { enabled = localStorage.getItem(STORE) !== "off"; } catch (_) { /* Private mode: on by default. */ }
  let items = [], shown = null, timer = null, seq = 0, panelOpen = false;

  const root = document.createElement("div");
  root.className = "notices"; root.setAttribute("aria-live", "polite");
  root.innerHTML = `
    <div class="notice-capsule" role="status" hidden>
      <span class="notice-ic"></span>
      <div class="notice-body"><strong class="notice-title"></strong><span class="notice-text"></span></div>
      <button type="button" class="notice-action" hidden></button>
      <button type="button" class="notice-del" aria-label="Удалить уведомление" title="Удалить">${cross}</button>
    </div>
    <button type="button" class="notice-pill" hidden><span class="notice-count"></span><span class="notice-pill-label"></span></button>
    <div class="notice-panel" role="dialog" aria-label="Уведомления" hidden>
      <div class="notice-panel-head"><strong>Уведомления</strong>
        <button type="button" class="notice-clear">Очистить</button>
        <button type="button" class="notice-fold" aria-label="Свернуть" title="Свернуть">${chevron}</button></div>
      <div class="notice-list"></div>
    </div>`;
  document.body.append(root);
  const capsule = root.querySelector(".notice-capsule"), pill = root.querySelector(".notice-pill");
  const panel = root.querySelector(".notice-panel"), list = root.querySelector(".notice-list");

  const plural = n => n % 10 === 1 && n % 100 !== 11 ? "уведомление" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "уведомления" : "уведомлений";
  const clock = at => new Date(at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const unread = () => items.filter(item => !item.read && (item.type === "error" || item.type === "warning"));

  function icon(target, type) { target.className = `notice-ic ${type}`; target.innerHTML = svg(icons[type] || icons.info); }

  function render() {
    capsule.hidden = !shown || panelOpen;
    if (shown) {
      icon(capsule.querySelector(".notice-ic"), shown.type);
      capsule.dataset.type = shown.type;
      capsule.setAttribute("role", shown.type === "error" ? "alert" : "status");
      capsule.querySelector(".notice-title").textContent = shown.title + (shown.count > 1 ? ` · ${shown.count} раза` : "");
      const text = capsule.querySelector(".notice-text"); text.textContent = shown.text || ""; text.hidden = !shown.text;
      const action = capsule.querySelector(".notice-action");
      action.hidden = !shown.action; action.textContent = shown.action?.label || "";
    }
    const count = unread().length;
    pill.hidden = !enabled || Boolean(shown) || panelOpen || !count;
    pill.querySelector(".notice-count").textContent = String(count);
    pill.querySelector(".notice-pill-label").textContent = plural(count);
    panel.hidden = !panelOpen;
    if (panelOpen) renderList();
  }
  function renderList() {
    list.replaceChildren();
    if (!items.length) { const empty = document.createElement("p"); empty.className = "notice-empty"; empty.textContent = "Уведомлений нет."; list.append(empty); return; }
    for (const item of items) {
      const row = document.createElement("div"); row.className = `notice-row${item.read ? " read" : ""}${item.type === "error" && !item.read ? " hot" : ""}`;
      const ic = document.createElement("span"); icon(ic, item.type);
      const body = document.createElement("div"); body.className = "notice-body";
      const title = document.createElement("strong"); title.className = "notice-title"; title.textContent = item.title + (item.count > 1 ? ` · ${item.count} раза` : "");
      const meta = document.createElement("span"); meta.className = "notice-text"; meta.textContent = [item.text, clock(item.at)].filter(Boolean).join(" · ");
      body.append(title, meta); row.append(ic, body);
      if (item.action) {
        const action = document.createElement("button"); action.type = "button"; action.className = "notice-action"; action.textContent = item.action.label;
        action.addEventListener("click", () => { run(item); });
        row.append(action);
      }
      const del = document.createElement("button"); del.type = "button"; del.className = "notice-del"; del.innerHTML = cross;
      del.setAttribute("aria-label", "Удалить уведомление"); del.title = "Удалить";
      del.addEventListener("click", () => remove(item.id));
      row.append(del); list.append(row);
    }
  }

  function settle(item) {
    clearTimeout(timer);
    timer = setTimeout(() => { if (shown === item) collapse(); }, LIFETIME[item.type] || 6000);
  }
  function collapse() {
    clearTimeout(timer);
    if (shown && (shown.type === "info" || shown.type === "success")) shown.read = true;
    shown = null; render();
  }
  function remove(id) {
    items = items.filter(item => item.id !== id);
    if (shown?.id === id) { clearTimeout(timer); shown = null; }
    if (panelOpen && !items.length) panelOpen = false;
    render();
  }
  function run(item) {
    item.read = true;
    try { item.action?.run?.(); } finally { if (shown === item) collapse(); else render(); }
  }

  /** Show a notice. Returns false when notices are switched off (the caller keeps its fallback). */
  function notify({ type = "error", title, text = "", action = null, key = null } = {}) {
    if (!enabled || !title) return false;
    const id = key || `${type}|${title}|${text}`;
    let item = items.find(entry => entry.key === id);
    if (item) { items = items.filter(entry => entry !== item); item.count += 1; item.at = Date.now(); item.read = false; item.text = text; item.action = action; }
    else item = { id: `n${++seq}`, key: id, type, title, text, action, count: 1, at: Date.now(), read: false };
    items.unshift(item); items = items.slice(0, LIMIT);
    if (!panelOpen) { shown = item; settle(item); }
    render(); return true;
  }
  /** A notice whose cause went away (a service is back): drop it quietly. */
  function resolve(key) {
    const item = items.find(entry => entry.key === key); if (item) remove(item.id);
  }

  capsule.querySelector(".notice-action").addEventListener("click", () => { if (shown) run(shown); });
  capsule.querySelector(".notice-del").addEventListener("click", () => { if (shown) remove(shown.id); });
  capsule.addEventListener("pointerenter", () => clearTimeout(timer));
  capsule.addEventListener("pointerleave", () => { if (shown) settle(shown); });
  // Swipe the capsule up (or sideways) to fold it away.
  let drag = null;
  capsule.addEventListener("pointerdown", event => {
    if (event.target.closest("button")) return;
    drag = { x: event.clientX, y: event.clientY, id: event.pointerId }; clearTimeout(timer);
    try { capsule.setPointerCapture(event.pointerId); } catch (_) { /* Older browsers. */ }
  });
  capsule.addEventListener("pointermove", event => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x, dy = Math.min(0, event.clientY - drag.y);
    capsule.style.transform = `translate(calc(-50% + ${dx * 0.6}px), ${dy * 0.8}px)`;
    capsule.style.opacity = String(Math.max(0.2, 1 - (Math.abs(dx) + Math.abs(dy)) / 160));
  });
  const endDrag = event => {
    if (!drag || event.pointerId !== drag.id) return;
    const moved = Math.abs(event.clientX - drag.x) > 60 || drag.y - event.clientY > 30;
    drag = null; capsule.style.transform = ""; capsule.style.opacity = "";
    if (moved) collapse();
    else if (shown) { panelOpen = true; shown.read = true; clearTimeout(timer); shown = null; render(); }
  };
  capsule.addEventListener("pointerup", endDrag);
  capsule.addEventListener("pointercancel", event => { drag = null; capsule.style.transform = ""; capsule.style.opacity = ""; if (shown) settle(shown); void event; });

  pill.addEventListener("click", () => { panelOpen = true; for (const item of items) item.read = true; render(); });
  root.querySelector(".notice-fold").addEventListener("click", () => { panelOpen = false; render(); });
  root.querySelector(".notice-clear").addEventListener("click", () => { items = []; panelOpen = false; shown = null; render(); });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (panelOpen) { panelOpen = false; render(); } else if (shown) collapse();
  });
  document.addEventListener("pointerdown", event => {
    if (panelOpen && !root.contains(event.target)) { panelOpen = false; render(); }
  }, true);

  // «Настройки мессенджера» → «Всплывающие уведомления».
  const toggle = $("#notices-enabled");
  if (toggle) {
    toggle.checked = enabled;
    toggle.addEventListener("change", () => {
      enabled = toggle.checked;
      try { localStorage.setItem(STORE, enabled ? "on" : "off"); } catch (_) { /* Kept for this visit only. */ }
      if (!enabled) { shown = null; panelOpen = false; clearTimeout(timer); }
      render();
    });
  }
  render();
  return { notify, resolve, get enabled() { return enabled; } };
};
