"use strict";
// Operator tasks: a promise to a client with a due time, made from chat messages.
// A long press on a message (right click on a computer) opens its menu with
// «Сделать задачей»; the task shows up in «Задачи», as a strip in the dialog, as a
// marker in the chat list and as a reminder capsule when it is due.
window.CenterTasks = ({ $, state, api, node, show, toast = () => {}, notices = null, renderPane = async () => {} }) => {
  const icon = name => window.CenterIcon ? window.CenterIcon(name) : document.createElement("span");
  const PRESS_MS = 480, STILL_PX = 10, POLL_MS = 60000;
  const KINDS = { makeup: "Отработка", subscription: "Абонемент", group: "Группа", call: "Перезвонить", other: "Другое" };
  let ctx = null, ctxAt = 0, ctxEpoch = -1, summary = null, summaryBusy = null;
  let scope = "mine", closedOpen = false, lists = { open: [], closed: [] }, listBusy = false;
  let convId = null, convTasks = [], convSeq = 0, pickMode = false, picked = new Set();
  let press = null, menuFor = null, lastTap = { id: null, at: 0 };
  let sheet = null, sheetTask = null, sheetDraft = null, sheetDue = null;

  // ── access, time ─────────────────────────────────────────────────────────
  const base = () => `/workspaces/${state.workspace.id}`;
  const allowed = () => {
    const ws = state.workspace; if (!state.user || !ws) return false;
    return ["owner", "admin"].includes(ws.role) || (ws.sections || []).includes("messages");
  };
  const readOnly = () => state.workspace?.role === "observer" || Boolean(ctx?.read_only);
  const zone = () => ctx?.timezone || state.workspace?.timezone || "Europe/Moscow";
  const parts = (value, options) => Object.fromEntries(new Intl.DateTimeFormat("ru-RU", { timeZone: zone(), hourCycle: "h23", ...options })
    .formatToParts(new Date(value)).filter(p => p.type !== "literal").map(p => [p.type, p.value]));
  const dayKey = value => { const p = parts(value, { year: "numeric", month: "2-digit", day: "2-digit" }); return `${p.year}-${p.month}-${p.day}`; };
  const clock = value => { const p = parts(value, { hour: "2-digit", minute: "2-digit" }); return `${p.hour}:${p.minute}`; };
  const shortDay = value => new Intl.DateTimeFormat("ru-RU", { timeZone: zone(), day: "numeric", month: "short" }).format(new Date(value)).replace(".", "");
  const weekday = value => new Intl.DateTimeFormat("ru-RU", { timeZone: zone(), weekday: "short" }).format(new Date(value));
  const addDays = (key, days) => { const d = new Date(`${key}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); };
  function bucket(task, now = Date.now()) {
    const due = new Date(task.due_at).getTime();
    if (due < now) return "overdue";
    const today = dayKey(now), day = dayKey(due);
    return day === today ? "today" : day === addDays(today, 1) ? "tomorrow" : "later";
  }
  function dueText(task, now = Date.now()) {
    const kind = bucket(task, now), at = task.due_at;
    if (kind === "overdue") return dayKey(at) === dayKey(now) ? `просрочено · было до ${clock(at)}` : `просрочено · было ${shortDay(at)}`;
    if (kind === "today") return `до ${clock(at)}`;
    if (kind === "tomorrow") return `завтра ${clock(at)}`;
    return `${shortDay(at)}, ${clock(at)}`;
  }
  // A wall-clock time typed in the studio's time zone → an ISO instant.
  function zonedIso(local) {
    const [d, t] = local.split("T"); if (!d || !t) return null;
    const [y, m, day] = d.split("-").map(Number), [hh, mm] = t.split(":").map(Number);
    const guess = Date.UTC(y, m - 1, day, hh, mm);
    const p = parts(guess, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    const shown = Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day), Number(p.hour), Number(p.minute));
    return new Date(guess - (shown - guess)).toISOString();
  }
  function localInput(value) {
    const p = parts(value, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  }
  const plural = (n, one, few, many) => { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many; };
  const store = { get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Private mode. */ } } };

  // ── data ─────────────────────────────────────────────────────────────────
  async function context(force = false) {
    if (!allowed()) return null;
    if (!force && ctx && ctxEpoch === state.epoch && Date.now() - ctxAt < 60000) return ctx;
    const epoch = state.epoch, result = await api(`${base()}/tasks/options`, { timeoutMs: 15000 });
    if (epoch !== state.epoch) return ctx;
    ctx = result; ctxAt = Date.now(); ctxEpoch = epoch;
    state.taskPress = ctx.personal?.press || "split";
    return ctx;
  }
  async function refresh() {
    if (!allowed()) { summary = null; paintBadge(); return null; }
    if (summaryBusy) return summaryBusy;
    const epoch = state.epoch;
    summaryBusy = (async () => {
      try {
        if (ctxEpoch !== epoch) await context(true);
        const result = await api(`${base()}/tasks/summary`, { timeoutMs: 15000, track: true });
        if (epoch !== state.epoch) return null;
        summary = result; paintBadge(); paintRows(); remind();
        if (convId && result.conversations?.[convId]?.count !== convTasks.filter(t => t.status === "open").length) loadConversation(convId);
        return result;
      } catch (error) { if (error.name !== "AbortError" && error.status !== 403) console.warn("tasks summary", error.message); return null; }
      finally { summaryBusy = null; }
    })();
    return summaryBusy;
  }
  async function changed() {
    await Promise.all([refresh(), convId ? loadConversation(convId) : null, state.pane === "tasks" ? loadLists() : null]);
  }
  async function loadConversation(id) {
    const seq = ++convSeq;
    try {
      const result = await api(`${base()}/tasks?conversation=${encodeURIComponent(id)}`, { timeoutMs: 15000 });
      if (seq !== convSeq || id !== convId) return;
      convTasks = result.tasks || []; paintStrip(); paintBubbles();
    } catch (error) { if (error.name !== "AbortError") console.warn("tasks conversation", error.message); }
  }

  // ── navigation badge and chat list markers ───────────────────────────────
  function paintBadge() {
    const count = summary?.badge || 0;
    for (const tab of document.querySelectorAll('[data-pane="tasks"]')) {
      let dot = tab.querySelector(".dot");
      if (!count) { dot?.remove(); continue; }
      if (!dot) { dot = node("span", "", "dot task-dot"); tab.append(dot); }
      const label = String(Math.min(count, 99));
      if (dot.textContent !== label) dot.textContent = label;
      tab.setAttribute("aria-label", `Задачи: ${count} ${plural(count, "задача", "задачи", "задач")} на сегодня`);
    }
  }
  function paintRows() {
    const markers = summary?.conversations || {};
    for (const row of document.querySelectorAll("#chat-list .chat-row[data-id]")) {
      const top = row.querySelector(".row-top"); if (!top) continue;
      const info = markers[row.dataset.id]; let mark = top.querySelector(".row-task");
      if (!info) { mark?.remove(); continue; }
      const late = new Date(info.due_at).getTime() < Date.now();
      const text = (late ? "просрочено" : "до " + (dayKey(info.due_at) === dayKey(Date.now()) ? clock(info.due_at) : shortDay(info.due_at))) + (info.count > 1 ? ` · ${info.count}` : "");
      if (!mark) { mark = node("span", undefined, "row-task"); mark.append(icon("task"), node("span", "")); top.insertBefore(mark, top.querySelector(".row-time")); }
      mark.classList.toggle("late", late);
      if (mark.lastChild.textContent !== text) mark.lastChild.textContent = text;
      mark.title = `Открытых задач: ${info.count}`;
    }
  }

  // ── strip in the dialog and marks on source messages ─────────────────────
  const canvas = $(".pane-chat .canvas"), history = $("#chat-history");
  const strip = node("div", undefined, "task-strip"); strip.id = "task-strip"; strip.hidden = true;
  strip.innerHTML = `<span class="task-strip-ic"></span><button type="button" class="task-strip-main"><strong></strong><small></small></button><button type="button" class="task-strip-done">Выполнено</button>`;
  strip.querySelector(".task-strip-ic").append(icon("task"));
  canvas?.insertBefore(strip, $("#chat-header")?.nextSibling || null);
  const openTasks = () => convTasks.filter(t => t.status === "open").sort((a, b) => a.due_at.localeCompare(b.due_at));
  function paintStrip() {
    const list = openTasks(), visible = Boolean(convId && list.length && state.pane === "messages" && !$("#chat-stream")?.hidden);
    strip.hidden = !visible; canvas?.classList.toggle("has-task-strip", visible);
    document.documentElement.classList.toggle("task-strip-on", visible);
    if (!visible) return;
    const first = list[0], late = bucket(first) === "overdue";
    strip.classList.toggle("late", late);
    strip.querySelector("strong").textContent = first.title;
    strip.querySelector("small").textContent = [dueText(first), list.length > 1 ? `ещё ${list.length - 1} ${plural(list.length - 1, "задача", "задачи", "задач")}` : ""].filter(Boolean).join(" · ");
    strip.querySelector(".task-strip-done").hidden = readOnly();
  }
  strip.querySelector(".task-strip-main").addEventListener("click", () => {
    const list = openTasks(); if (list.length === 1) openSheet({ task: list[0] }); else openConversationList();
  });
  strip.querySelector(".task-strip-done").addEventListener("click", () => { const first = openTasks()[0]; if (first) complete(first); });
  function paintBubbles() {
    const marked = new Map();
    for (const task of convTasks) if (task.status === "open") for (const source of task.sources || []) marked.set(source.id, task);
    for (const bubble of history.querySelectorAll(".chat-bubble.msg[data-id]")) {
      const task = marked.get(bubble.dataset.id); let tag = bubble.querySelector(".task-tag");
      if (!task) { tag?.remove(); continue; }
      if (!tag) {
        tag = node("button", undefined, "task-tag"); tag.type = "button"; tag.append(icon("task"), node("span", "задача"));
        tag.addEventListener("click", event => { event.stopPropagation(); const current = convTasks.find(t => t.id === tag.dataset.task); if (current) openSheet({ task: current }); });
        (bubble.querySelector(".meta") || bubble).prepend(tag);
      }
      tag.dataset.task = task.id; tag.title = `Задача: ${task.title}`;
    }
  }
  function watchConversation() {
    const id = state.pane === "messages" && history.dataset.conversation && !$("#chat-stream")?.hidden ? history.dataset.conversation : null;
    if (id !== convId) {
      convId = id; convTasks = []; endPick(); paintStrip();
      if (id && allowed()) loadConversation(id);
    } else { paintStrip(); paintBubbles(); }
  }
  new MutationObserver(watchConversation).observe(history, { childList: true, attributes: true, attributeFilter: ["data-conversation"] });
  new MutationObserver(watchConversation).observe($("#chat-stream"), { attributes: true, attributeFilter: ["hidden"] });
  const list = $("#chat-list");
  if (list) new MutationObserver(() => { if (summary) paintRows(); }).observe(list, { childList: true, subtree: true });
  for (const holder of [$("#navrail"), $("#tabbar")]) if (holder) new MutationObserver(paintBadge).observe(holder, { childList: true });

  // ── message menu: long press on a bubble, right click on a computer ──────
  const backdrop = node("div", undefined, "task-menu-backdrop"); backdrop.hidden = true;
  const menu = node("div", undefined, "task-menu"); menu.setAttribute("role", "menu"); menu.setAttribute("aria-label", "Действия с сообщением"); menu.hidden = true;
  document.body.append(backdrop, menu);
  function menuItem(label, glyph, handler, primary = false) {
    const item = node("button", undefined, `task-menu-item${primary ? " primary" : ""}`); item.type = "button"; item.setAttribute("role", "menuitem");
    item.append(icon(glyph), node("span", label));
    item.addEventListener("click", () => { const bubble = menuFor; closeMenu(); handler(bubble); });
    return item;
  }
  function openMenu(bubble, point) {
    if (!bubble || !convId) return;
    closeMenu(); menuFor = bubble; bubble.classList.add("task-lift");
    const items = [];
    if (!readOnly()) items.push(menuItem("Сделать задачей", "task", target => createFrom([target.dataset.id], "menu"), true));
    items.push(menuItem("Копировать", "copy", target => copyText(target)));
    if (!readOnly()) items.push(menuItem("Выбрать несколько", "checks", target => startPick(target)));
    menu.replaceChildren(...items); backdrop.hidden = false; menu.hidden = false;
    const rect = bubble.getBoundingClientRect(), width = menu.offsetWidth || 240, height = menu.offsetHeight || 190;
    let top = point?.y ?? rect.bottom + 8, left = point?.x ?? (bubble.classList.contains("out") ? rect.right - width : rect.left);
    if (!point) top = rect.bottom + 8;
    if (top + height > innerHeight - 12) top = Math.max(12, (point?.y ?? rect.top) - height - 8);
    left = Math.max(12, Math.min(left, innerWidth - width - 12));
    menu.style.top = `${Math.round(top)}px`; menu.style.left = `${Math.round(left)}px`;
    navigator.vibrate?.(12);
    requestAnimationFrame(() => menu.querySelector("button")?.focus({ preventScroll: true }));
  }
  function closeMenu() {
    menuFor?.classList.remove("task-lift"); menuFor = null;
    backdrop.hidden = true; menu.hidden = true;
  }
  backdrop.addEventListener("pointerdown", event => { event.preventDefault(); closeMenu(); });
  menu.addEventListener("keydown", event => {
    const buttons = [...menu.querySelectorAll("button")], index = buttons.indexOf(document.activeElement);
    if (event.key === "ArrowDown") { event.preventDefault(); buttons[(index + 1) % buttons.length]?.focus(); }
    if (event.key === "ArrowUp") { event.preventDefault(); buttons[(index - 1 + buttons.length) % buttons.length]?.focus(); }
  });
  async function copyText(bubble) {
    const value = bubble.querySelector(".msg-text")?.textContent || "";
    try { await navigator.clipboard.writeText(value); notices?.notify({ type: "success", title: "Текст скопирован", key: "task:copy", count: false }); }
    catch (_) { toast("Не удалось скопировать текст.", true); }
  }
  const bubbleAt = target => target?.closest?.(".chat-bubble.msg[data-id]");
  const mode = () => state.taskPress || ctx?.personal?.press || "split";
  history.addEventListener("pointerdown", event => {
    const bubble = bubbleAt(event.target);
    if (!bubble || event.pointerType !== "touch" || !event.isPrimary || pickMode || mode() === "voice" || event.target.closest("a,button")) return;
    press = { id: event.pointerId, x: event.clientX, y: event.clientY, bubble };
    press.timer = setTimeout(() => {
      if (!press || press.bubble !== bubble) return;
      press.fired = true; openMenu(bubble);
    }, PRESS_MS);
  });
  document.addEventListener("pointermove", event => {
    if (press && event.pointerId === press.id && !press.fired && Math.hypot(event.clientX - press.x, event.clientY - press.y) > STILL_PX) { clearTimeout(press.timer); press = null; }
  });
  const endPress = event => {
    if (!press || event.pointerId !== press.id) return;
    clearTimeout(press.timer);
    if (press.fired) { event.preventDefault?.(); swallowClick = performance.now() + 500; }
    press = null;
  };
  let swallowClick = 0;
  document.addEventListener("pointerup", endPress); document.addEventListener("pointercancel", endPress);
  document.addEventListener("click", event => { if (performance.now() < swallowClick && !menu.contains(event.target)) { swallowClick = 0; event.preventDefault(); event.stopPropagation(); } }, true);
  history.addEventListener("contextmenu", event => {
    const bubble = bubbleAt(event.target); if (!bubble) return;
    event.preventDefault();
    if (press?.fired || pickMode) return;
    if (event.pointerType === "touch" || (event.sourceCapabilities && event.sourceCapabilities.firesTouchEvents)) return;
    openMenu(bubble, { x: event.clientX, y: event.clientY });
  });
  history.addEventListener("click", event => {
    const bubble = bubbleAt(event.target); if (!bubble) return;
    if (pickMode) { event.preventDefault(); togglePick(bubble); return; }
    if (mode() === "voice" && event.pointerType !== "mouse") {
      const now = performance.now();
      if (lastTap.id === bubble.dataset.id && now - lastTap.at < 350) { lastTap = { id: null, at: 0 }; openMenu(bubble); }
      else lastTap = { id: bubble.dataset.id, at: now };
    }
  });

  // ── picking several messages ─────────────────────────────────────────────
  const pickBar = node("div", undefined, "task-pick-bar"); pickBar.hidden = true;
  pickBar.innerHTML = `<button type="button" class="task-pick-cancel">Отмена</button><button type="button" class="task-pick-make"></button>`;
  canvas?.append(pickBar);
  function startPick(bubble) {
    pickMode = true; picked = new Set(); document.documentElement.classList.add("task-selecting");
    canvas?.classList.add("task-picking"); pickBar.hidden = false; togglePick(bubble);
  }
  function togglePick(bubble) {
    const id = bubble.dataset.id;
    if (picked.has(id)) picked.delete(id); else if (picked.size < 5) picked.add(id); else { notices?.notify({ type: "info", title: "Не больше 5 сообщений в одной задаче", key: "task:pick-limit", count: false }); return; }
    bubble.classList.toggle("task-picked", picked.has(id)); paintPick();
  }
  function paintPick() {
    const make = pickBar.querySelector(".task-pick-make");
    make.textContent = picked.size ? `Задача из ${picked.size} ${plural(picked.size, "сообщения", "сообщений", "сообщений")}` : "Выберите сообщения";
    make.disabled = !picked.size;
  }
  function endPick() {
    if (!pickMode) return;
    pickMode = false; document.documentElement.classList.remove("task-selecting"); canvas?.classList.remove("task-picking");
    pickBar.hidden = true; for (const bubble of history.querySelectorAll(".task-picked")) bubble.classList.remove("task-picked"); picked = new Set();
  }
  pickBar.querySelector(".task-pick-cancel").addEventListener("click", endPick);
  pickBar.querySelector(".task-pick-make").addEventListener("click", () => {
    const order = [...history.querySelectorAll(".chat-bubble.msg[data-id]")].map(b => b.dataset.id).filter(id => picked.has(id));
    endPick(); createFrom(order, "select");
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (!menu.hidden) { closeMenu(); event.stopPropagation(); } else if (pickMode) endPick();
  }, true);
  document.addEventListener("cm:chat-selected", () => { closeMenu(); endPick(); });

  // ── create / edit sheet ──────────────────────────────────────────────────
  function bubbleSource(id) {
    const bubble = history.querySelector(`.chat-bubble.msg[data-id="${CSS.escape(id)}"]`);
    return { id, text: (bubble?.querySelector(".msg-text")?.textContent || "").trim(), direction: bubble?.classList.contains("out") ? "out" : "in", at: null };
  }
  function clientLabel() { return ($("#chat-person strong")?.textContent || "").trim(); }
  async function createFrom(ids, origin) {
    const sources = ids.map(bubbleSource), first = sources.find(s => s.direction === "in") || sources[0];
    const text = (first?.text || "").replace(/\s+/g, " ").trim();
    const title = text.length > 90 ? text.slice(0, 88).replace(/\s+\S*$/, "") + "…" : text;
    await openSheet({ draft: { title, sources, conversation_id: convId, client_label: clientLabel(), origin } });
  }
  function buildSheet() {
    const dialog = document.createElement("dialog"); dialog.className = "live-sheet task-sheet"; dialog.id = "task-dialog";
    dialog.setAttribute("aria-labelledby", "task-sheet-title");
    dialog.innerHTML = `<div class="sheet-head"><div><h2 id="task-sheet-title">Новая задача</h2><small class="task-sheet-client"></small></div>
      <button class="iconbtn plain" type="button" data-task-close aria-label="Закрыть"></button></div>
      <form class="sheet-body task-form" novalidate>
        <label class="task-field"><span>Что сделать</span><textarea name="title" rows="2" maxlength="200" required></textarea></label>
        <div class="task-quotes"></div>
        <div class="task-field" role="group" aria-labelledby="task-due-label"><span id="task-due-label">Срок</span><div class="task-chips task-due"></div>
          <input class="task-due-input" type="datetime-local" aria-label="Дата и время срока" hidden></div>
        <div class="task-field" role="group" aria-labelledby="task-kind-label"><span id="task-kind-label">Тип</span><div class="task-chips task-kinds"></div></div>
        <label class="task-field task-assignee"><span>Исполнитель</span><select name="assignee"></select></label>
        <label class="task-field"><span>Подробности</span><textarea name="details" rows="2" maxlength="2000" placeholder="Необязательно"></textarea></label>
        <p class="notice error task-error" role="alert" hidden></p>
        <div class="task-actions"><button class="btn sec task-open-chat" type="button" hidden>Открыть переписку</button>
          <button class="btn sec task-cancel-task" type="button" hidden>Отменить задачу</button>
          <button class="btn sec task-done" type="button" hidden>Выполнено</button>
          <button class="btn primary task-save" type="submit">Создать задачу</button></div>
      </form>`;
    dialog.querySelector("[data-task-close]").append(icon("close"));
    dialog.querySelector("[data-task-close]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
    dialog.querySelector("form").addEventListener("submit", event => { event.preventDefault(); saveSheet(); });
    dialog.querySelector(".task-due-input").addEventListener("change", event => { sheetDue = zonedIso(event.target.value); paintDue(); });
    dialog.querySelector(".task-done").addEventListener("click", () => { const task = sheetTask; dialog.close(); if (task) complete(task); });
    dialog.querySelector(".task-cancel-task").addEventListener("click", async () => {
      const task = sheetTask; if (!task) return;
      await action(task, task.status === "open" ? "cancel" : "reopen", task.status === "open" ? "Задача отменена" : "Задача снова открыта"); dialog.close();
    });
    dialog.querySelector(".task-open-chat").addEventListener("click", () => { const task = sheetTask; dialog.close(); if (task?.conversation_id) openChat(task); });
    document.body.append(dialog); return dialog;
  }
  function chip(label, pressed, handler, extra = "") {
    const button = node("button", label, `task-chip${extra}`); button.type = "button"; button.setAttribute("aria-pressed", String(pressed));
    button.addEventListener("click", handler); return button;
  }
  function presetLabel(key, value) {
    if (key === "hour") return "Через 1 ч";
    if (key === "two_hours") return `Через 2 ч · ${clock(value)}`;
    if (key === "end_of_day") return dayKey(value) === dayKey(Date.now()) ? `До конца дня · ${clock(value)}` : `${weekday(value)} до ${clock(value)}`;
    return dayKey(value) === addDays(dayKey(Date.now()), 1) ? `Завтра ${clock(value)}` : `${weekday(value)} ${clock(value)}`;
  }
  function paintDue() {
    const box = sheet.querySelector(".task-due"), input = sheet.querySelector(".task-due-input"), presets = ctx?.presets || {};
    const entries = ["hour", "end_of_day", "tomorrow_morning"].filter(key => presets[key]).map(key => [key, presets[key]]);
    const seen = new Set(), chips = [];
    let matched = false;
    for (const [key, value] of entries) {
      const label = presetLabel(key, value); if (seen.has(label)) continue; seen.add(label);
      const on = Boolean(sheetDue) && Math.abs(new Date(sheetDue) - new Date(value)) < 60000; matched ||= on;
      chips.push(chip(label, on, () => { sheetDue = value; input.hidden = true; paintDue(); }));
    }
    const custom = Boolean(sheetDue) && !matched;
    chips.push(chip(custom ? `${shortDay(sheetDue)}, ${clock(sheetDue)}` : "Дата…", custom || !input.hidden, () => {
      input.hidden = false; input.value = localInput(sheetDue || presets.default || Date.now()); input.focus(); input.showPicker?.();
    }));
    box.replaceChildren(...chips);
  }
  function paintKinds(selected) {
    const box = sheet.querySelector(".task-kinds");
    box.replaceChildren(...Object.entries(KINDS).map(([key, title]) => chip(title, key === selected, () => {
      sheet.dataset.kind = sheet.dataset.kind === key ? "" : key; paintKinds(sheet.dataset.kind);
    }, " small")));
  }
  function paintQuotes(sources, editable) {
    const box = sheet.querySelector(".task-quotes"); box.replaceChildren();
    for (const source of sources) {
      const quote = node("div", undefined, `task-quote ${source.direction === "out" ? "out" : "in"}`);
      const glyph = node("span", "“", "task-quote-mark"); glyph.setAttribute("aria-hidden", "true");
      quote.append(glyph, node("span", source.text || "Сообщение без текста", "task-quote-text"));
      if (editable && sources.length > 1) {
        const remove = node("button", undefined, "task-quote-del"); remove.type = "button"; remove.setAttribute("aria-label", "Убрать сообщение из задачи"); remove.append(icon("close"));
        remove.addEventListener("click", () => { sheetDraft.sources = sheetDraft.sources.filter(s => s.id !== source.id); paintQuotes(sheetDraft.sources, true); });
        quote.append(remove);
      }
      box.append(quote);
    }
    box.hidden = !sources.length;
  }
  async function openSheet({ task = null, draft = null }) {
    sheet ||= buildSheet();
    try { await context(!ctx || Date.now() - ctxAt > 30000); }
    catch (error) { toast(error.message, true); return; }
    sheetTask = task; sheetDraft = draft ? { ...draft, sources: [...(draft.sources || [])] } : null;
    const editing = Boolean(task), form = sheet.querySelector("form");
    sheet.querySelector("#task-sheet-title").textContent = editing ? (task.status === "open" ? "Задача" : task.status === "done" ? "Задача выполнена" : "Задача отменена") : "Новая задача";
    sheet.querySelector(".task-sheet-client").textContent = (editing ? task.client_label : draft?.client_label) || "";
    form.elements.title.value = editing ? task.title : draft?.title || "";
    form.elements.details.value = editing ? task.details || "" : "";
    sheetDue = editing ? task.due_at : ctx?.presets?.default || null;
    sheet.querySelector(".task-due-input").hidden = true;
    sheet.dataset.kind = editing ? task.kind || "" : "";
    paintDue(); paintKinds(sheet.dataset.kind);
    paintQuotes(editing ? task.sources || [] : sheetDraft?.sources || [], !editing);
    const assignee = form.elements.assignee, people = ctx?.assignees || [];
    assignee.replaceChildren(...people.map(p => new Option(p.id === ctx.me ? `${p.name} (я)` : p.name, p.id)));
    assignee.value = editing ? task.assignee_id : ctx?.me;
    sheet.querySelector(".task-assignee").hidden = people.length < 2;
    const locked = readOnly();
    for (const field of form.querySelectorAll("textarea,select,input")) field.disabled = locked;
    sheet.querySelector(".task-save").hidden = locked;
    sheet.querySelector(".task-save").textContent = editing ? "Сохранить" : "Создать задачу";
    sheet.querySelector(".task-done").hidden = !editing || locked || task.status !== "open";
    sheet.querySelector(".task-cancel-task").hidden = !editing || locked;
    sheet.querySelector(".task-cancel-task").textContent = editing && task.status !== "open" ? "Открыть снова" : "Отменить задачу";
    sheet.querySelector(".task-open-chat").hidden = !editing || !task.conversation_id || (state.pane === "messages" && task.conversation_id === convId);
    const error = sheet.querySelector(".task-error"); error.hidden = true; error.textContent = "";
    if (!sheet.open) sheet.showModal();
    if (!editing) requestAnimationFrame(() => { const field = form.elements.title; field.focus(); field.setSelectionRange(field.value.length, field.value.length); });
  }
  async function saveSheet() {
    const form = sheet.querySelector("form"), error = sheet.querySelector(".task-error"), save = sheet.querySelector(".task-save");
    const title = form.elements.title.value.replace(/\s+/g, " ").trim();
    if (!title) { error.textContent = "Напишите, что нужно сделать."; error.hidden = false; form.elements.title.focus(); return; }
    if (!sheetDue) { error.textContent = "Выберите срок."; error.hidden = false; return; }
    const data = { title, details: form.elements.details.value.trim(), kind: sheet.dataset.kind || null, due_at: sheetDue };
    if (!sheet.querySelector(".task-assignee").hidden) data.assignee_id = form.elements.assignee.value;
    save.disabled = true;
    try {
      if (sheetTask) {
        await api(`${base()}/tasks/${sheetTask.id}`, { method: "PATCH", data });
        sheet.close(); notices?.notify({ type: "success", title: "Задача сохранена", key: "task:saved", count: false });
      } else {
        const draft = sheetDraft || {};
        const created = await api(`${base()}/tasks`, { method: "POST", data: { ...data, conversation_id: draft.conversation_id || null,
          client_label: draft.client_label || "", message_ids: (draft.sources || []).map(s => s.id), origin: draft.origin || "manual" } });
        sheet.close();
        notices?.notify({ type: "success", title: "Задача создана", text: `${created.title} · ${dueText(created)}`, key: `task:created:${created.id}`, always: true, count: false, glyph: "task" });
      }
      await changed();
    } catch (failure) { if (failure.name !== "AbortError") { error.textContent = failure.message; error.hidden = false; } }
    finally { save.disabled = false; }
  }
  function openConversationList() {
    sheet ||= buildSheet();
    const dialog = $("#task-conversation-dialog") || (() => {
      const d = document.createElement("dialog"); d.className = "live-sheet task-sheet"; d.id = "task-conversation-dialog";
      d.innerHTML = `<div class="sheet-head"><div><h2>Задачи по клиенту</h2><small class="task-sheet-client"></small></div><button class="iconbtn plain" type="button" aria-label="Закрыть"></button></div><div class="sheet-body task-conv-list"></div>`;
      d.querySelector(".sheet-head button").append(icon("close")); d.querySelector(".sheet-head button").addEventListener("click", () => d.close());
      d.addEventListener("click", event => { if (event.target === d) d.close(); });
      document.body.append(d); return d;
    })();
    dialog.querySelector(".task-sheet-client").textContent = clientLabel();
    dialog.querySelector(".task-conv-list").replaceChildren(...openTasks().map(task => taskRow(task, () => { dialog.close(); openSheet({ task }); })));
    if (!dialog.open) dialog.showModal();
  }

  // ── actions ──────────────────────────────────────────────────────────────
  async function action(task, verb, title, data) {
    try {
      const result = await api(`${base()}/tasks/${task.id}/${verb}`, { method: "POST", data: data || {} });
      notices?.resolve(`task:due:${task.id}`);
      if (title) notices?.notify({ type: "success", title, text: result.title, key: `task:${verb}:${task.id}`, always: true, count: false });
      await changed(); return result;
    } catch (error) { if (error.name !== "AbortError") toast(error.message, true); return null; }
  }
  async function complete(task) {
    const done = await action(task, "complete", null); if (!done) return;
    const here = state.pane === "messages" && task.conversation_id === convId;
    notices?.notify({ type: "success", title: "Задача выполнена", text: done.title, key: `task:complete:${task.id}`, always: true, count: false,
      actions: [
        ...(task.conversation_id ? [{ label: "Ответить клиенту", run: () => (here ? Promise.resolve() : openChat(task)).then(() => $("#chat-input")?.focus()) }] : []),
        { label: "Вернуть", run: () => action(task, "reopen", "Задача снова открыта") },
      ] });
  }
  function snoozeTarget(kind) {
    const presets = ctx?.presets || {};
    if (kind === "hour") return new Date(Date.now() + 3600000).toISOString();
    return presets.tomorrow_morning || new Date(Date.now() + 86400000).toISOString();
  }
  async function openChat(task) {
    document.dispatchEvent(new CustomEvent("cm:open-chat", { detail: { id: task.conversation_id, message: task.sources?.[0]?.id || null } }));
  }

  // ── reminders ────────────────────────────────────────────────────────────
  const remindedKey = task => `cm:task-reminded:${task.id}@${task.due_at}`;
  function remind() {
    if (!summary || !ctx || ctx.personal?.reminders === false) return;
    const now = Date.now(), due = (summary.upcoming || []).filter(task => new Date(task.due_at).getTime() <= now && !store.get(remindedKey(task)));
    if (due.length > 3) {
      for (const task of due) store.set(remindedKey(task), "1");
      notices?.notify({ type: "warning", title: `Просрочено: ${due.length} ${plural(due.length, "задача", "задачи", "задач")}`, text: due.slice(0, 2).map(t => t.title).join(" · "),
        key: "task:due:many", always: true, count: false, glyph: "bell", actions: [{ label: "Открыть задачи", run: () => renderPane("tasks") }] });
    } else for (const task of due) {
      store.set(remindedKey(task), "1");
      notices?.notify({ type: "warning", title: `Пора: ${task.title}`, text: [task.client_label, `срок ${clock(task.due_at)}`].filter(Boolean).join(" · "),
        key: `task:due:${task.id}`, always: true, sticky: true, count: false, glyph: "bell", actions: [
          { label: "Выполнено", run: () => complete(task) },
          { label: "Через 1 ч", run: () => action(task, "snooze", "Задача отложена на час", { due_at: snoozeTarget("hour") }) },
          { label: "Открыть", keep: true, run: () => task.conversation_id ? openChat(task) : openSheet({ task }) },
        ] });
    }
    digest(now);
  }
  function digest(now) {
    if (ctx.personal?.digest === false || !summary) return;
    const today = dayKey(now), studio = summary.studio || ctx.studio || {}, time = clock(now);
    const open = summary.today + summary.overdue;
    if (open && store.get("cm:task-digest-start") !== today && time >= (studio.day_start || "00:00")) {
      store.set("cm:task-digest-start", today);
      notices?.notify({ type: "info", title: `Сегодня ${summary.today} ${plural(summary.today, "задача", "задачи", "задач")}` + (summary.overdue ? `, ${summary.overdue} ${plural(summary.overdue, "просрочена", "просрочены", "просрочено")}` : ""),
        key: "task:digest", always: true, count: false, actions: [{ label: "Открыть задачи", run: () => renderPane("tasks") }] });
    }
    const end = studio.day_end || "20:00", [eh, em] = end.split(":").map(Number), warn = `${String(Math.floor((eh * 60 + em - 30) / 60)).padStart(2, "0")}:${String((eh * 60 + em - 30) % 60).padStart(2, "0")}`;
    if (summary.today && store.get("cm:task-digest-end") !== today && time >= warn && time < end) {
      store.set("cm:task-digest-end", today);
      notices?.notify({ type: "warning", title: `До конца дня осталось ${summary.today} ${plural(summary.today, "задача", "задачи", "задач")}`, text: "Выполните или перенесите на завтра.",
        key: "task:digest-end", always: true, count: false, actions: [{ label: "Открыть задачи", run: () => renderPane("tasks") }] });
    }
  }
  setInterval(() => { if (!document.hidden && allowed()) refresh(); }, POLL_MS);
  setInterval(() => { if (!document.hidden) remind(); }, 20000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && allowed()) refresh(); });

  // ── «Задачи» section ─────────────────────────────────────────────────────
  const pane = $("#pane-tasks");
  function taskRow(task, onOpen) {
    const row = node("div", undefined, `task-row ${task.status === "open" ? bucket(task) : task.status}`);
    const check = node("button", undefined, "task-check"); check.type = "button";
    check.setAttribute("aria-label", task.status === "open" ? `Отметить выполненной: ${task.title}` : `Открыть снова: ${task.title}`);
    if (task.status === "done") check.append(icon("check"));
    check.disabled = readOnly();
    check.addEventListener("click", event => { event.stopPropagation(); if (task.status === "open") complete(task); else action(task, "reopen", "Задача снова открыта"); });
    const main = node("button", undefined, "task-row-main"); main.type = "button";
    main.append(node("strong", task.title), node("small", [task.client_label, task.status === "open" ? dueText(task) : task.status === "done" ? "выполнена" : "отменена",
      scope === "all" && task.assignee_name && task.assignee_id !== ctx?.me ? task.assignee_name : ""].filter(Boolean).join(" · ")));
    main.addEventListener("click", onOpen || (() => openSheet({ task })));
    const chevron = node("span", undefined, "task-row-chev"); chevron.append(icon("chevron"));
    row.append(check, main, chevron); return row;
  }
  async function loadLists() {
    if (!allowed() || listBusy) return;
    listBusy = true; const epoch = state.epoch;
    try {
      await context();
      const [open, closed] = await Promise.all([
        api(`${base()}/tasks?scope=${scope}&status=open`, { timeoutMs: 15000 }),
        closedOpen ? api(`${base()}/tasks?scope=${scope}&status=closed`, { timeoutMs: 15000 }) : Promise.resolve({ tasks: lists.closed }),
      ]);
      if (epoch !== state.epoch) return;
      lists = { open: open.tasks || [], closed: closed.tasks || [] }; paintPane();
    } catch (error) { if (error.name !== "AbortError") { const box = pane.querySelector(".task-pane-error"); box.textContent = error.message; box.hidden = false; } }
    finally { listBusy = false; }
  }
  function paintPane() {
    if (!pane) return;
    pane.querySelector(".task-pane-error").hidden = true;
    const all = !ctx?.admin && ctx?.studio?.visibility === "own" ? false : true;
    const tabs = pane.querySelector(".task-scope"); tabs.hidden = !all;
    for (const tab of tabs.querySelectorAll("button")) tab.setAttribute("aria-selected", String(tab.dataset.scope === scope));
    pane.querySelector(".task-new").hidden = readOnly();
    const groups = { overdue: [], today: [], tomorrow: [], later: [] }, now = Date.now();
    for (const task of lists.open) groups[bucket(task, now)].push(task);
    const titles = { overdue: "Просрочено", today: "Сегодня", tomorrow: "Завтра", later: "Позже" };
    const body = pane.querySelector(".task-groups"); body.replaceChildren();
    for (const key of Object.keys(groups)) {
      if (!groups[key].length) continue;
      const section = node("section", undefined, `task-group ${key}`);
      section.append(node("h2", `${titles[key]} · ${groups[key].length}`, "task-group-title"));
      const card = node("div", undefined, "task-card"); card.append(...groups[key].map(task => taskRow(task))); section.append(card); body.append(section);
    }
    if (!lists.open.length) {
      const empty = node("div", undefined, "task-empty"); const glyph = node("span", undefined, "task-empty-ic"); glyph.append(icon("task"));
      empty.append(glyph, node("strong", scope === "mine" ? "Открытых задач нет" : "У студии нет открытых задач"),
        node("p", "Удержите сообщение в переписке (на компьютере — правая кнопка мыши) и выберите «Сделать задачей»."));
      body.append(empty);
    }
    const toggle = node("button", undefined, "task-closed-toggle"); toggle.type = "button"; toggle.setAttribute("aria-expanded", String(closedOpen));
    toggle.append(icon(closedOpen ? "down" : "chevron"), node("span", closedOpen ? `Выполнено и отменено за неделю · ${lists.closed.length}` : "Выполнено и отменено за неделю"));
    toggle.addEventListener("click", () => { closedOpen = !closedOpen; loadLists(); });
    body.append(toggle);
    if (closedOpen && lists.closed.length) { const card = node("div", undefined, "task-card closed"); card.append(...lists.closed.map(task => taskRow(task))); body.append(card); }
  }
  if (pane) {
    pane.querySelector(".task-new").append(icon("plus"));
    pane.querySelector(".task-settings").append(icon("cog"));
    pane.querySelector(".task-new").addEventListener("click", () => openSheet({ draft: { title: "", sources: [], conversation_id: null, client_label: "", origin: "manual" } }));
    pane.querySelector(".task-settings").addEventListener("click", openSettings);
    pane.querySelector(".task-scope").addEventListener("click", event => { const tab = event.target.closest("button[data-scope]"); if (!tab || tab.dataset.scope === scope) return; scope = tab.dataset.scope; loadLists(); });
  }

  // ── settings ─────────────────────────────────────────────────────────────
  async function openSettings() {
    try { await context(true); } catch (error) { toast(error.message, true); return; }
    const dialog = $("#task-settings-dialog"), form = dialog.querySelector("form"), p = ctx.personal, s = ctx.studio;
    form.elements.press.value = p.press; form.elements.voice_task.value = p.voice_task;
    form.elements.reminders.checked = p.reminders; form.elements.hints.checked = p.hints; form.elements.digest.checked = p.digest;
    form.elements.day_start.value = s.day_start; form.elements.day_end.value = s.day_end;
    for (const box of form.querySelectorAll('input[name="work_days"]')) box.checked = s.work_days.includes(Number(box.value));
    form.elements.default_due.value = s.default_due; form.elements.visibility.value = s.visibility; form.elements.alfa_copy.checked = s.alfa_copy;
    for (const field of form.querySelectorAll("[data-studio] :is(input,select)")) field.disabled = !ctx.admin;
    show(dialog.querySelector(".task-studio-note"), !ctx.admin);
    dialog.querySelector(".task-settings-error").hidden = true;
    dialog.showModal();
  }
  const settingsDialog = $("#task-settings-dialog");
  settingsDialog?.querySelector("form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, error = settingsDialog.querySelector(".task-settings-error"), save = form.querySelector('[type="submit"]');
    save.disabled = true; error.hidden = true;
    try {
      const personal = { press: form.elements.press.value, voice_task: form.elements.voice_task.value,
        reminders: form.elements.reminders.checked, hints: form.elements.hints.checked, digest: form.elements.digest.checked };
      await api(`${base()}/tasks/preferences`, { method: "PUT", data: personal });
      if (ctx.admin) {
        const days = [...form.querySelectorAll('input[name="work_days"]:checked')].map(box => Number(box.value));
        if (!days.length) throw new Error("Отметьте хотя бы один рабочий день.");
        if (form.elements.day_end.value <= form.elements.day_start.value) throw new Error("Конец рабочего дня должен быть позже начала.");
        await api(`${base()}/tasks/settings`, { method: "PUT", data: { day_start: form.elements.day_start.value, day_end: form.elements.day_end.value,
          work_days: days, default_due: form.elements.default_due.value, visibility: form.elements.visibility.value, alfa_copy: form.elements.alfa_copy.checked } });
      }
      await context(true); settingsDialog.close();
      notices?.notify({ type: "success", title: "Настройки задач сохранены", key: "task:settings", count: false });
      if (state.pane === "tasks") loadLists();
    } catch (failure) { if (failure.name !== "AbortError") { error.textContent = failure.message; error.hidden = false; } }
    finally { save.disabled = false; }
  });

  // ── lifecycle ────────────────────────────────────────────────────────────
  async function enter() { if (!allowed()) return; paintPane(); await Promise.all([loadLists(), refresh()]); }
  function reset() {
    ctx = null; ctxAt = 0; ctxEpoch = -1; summary = null; lists = { open: [], closed: [] }; convId = null; convTasks = [];
    scope = "mine"; closedOpen = false; closeMenu(); endPick(); paintBadge(); paintStrip();
    if (sheet?.open) sheet.close();
  }
  function sync() {
    if (ctxEpoch !== state.epoch) { reset(); if (allowed()) refresh(); }
    watchConversation(); paintBadge();
  }
  return { enter, sync, reset, refresh, open: openSheet };
};
