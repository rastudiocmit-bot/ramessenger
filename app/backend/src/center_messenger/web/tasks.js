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
  let scope = "mine", closedOpen = false, lists = { open: [], closed: [] }, listBusy = false, counts = {};
  let convId = null, convTasks = [], convSeq = 0, pickMode = false, picked = new Set();
  let press = null, menuFor = null, menuLift = null, lastTap = { id: null, at: 0 };
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
    state.tasksVoice = !ctx.read_only;
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
    const box = canvas?.getBoundingClientRect(), onScreen = Boolean(box && box.width && box.right > 4 && box.left < innerWidth - 4);
    const list = openTasks(), visible = Boolean(convId && list.length && state.pane === "messages" && !$("#chat-stream")?.hidden && onScreen);
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
  // On a phone the dialog slides in and out; the strip follows what is on screen.
  if ($("#app")) new MutationObserver(() => { paintStrip(); setTimeout(paintStrip, 320); }).observe($("#app"), { attributes: true, attributeFilter: ["data-view", "data-tab"] });
  addEventListener("resize", () => paintStrip());
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
    closeMenu(); menuFor = bubble;
    const at = bubble.getBoundingClientRect(), lifted = bubble.cloneNode(true);
    lifted.className = bubble.className + " task-lifted"; lifted.removeAttribute("data-id"); lifted.setAttribute("aria-hidden", "true");
    lifted.querySelector(".task-tag")?.remove(); lifted.querySelector(".task-pick-badge")?.remove();
    Object.assign(lifted.style, { left: `${at.left}px`, top: `${at.top}px`, width: `${at.width}px` });
    document.body.append(lifted); menuLift = lifted; bubble.classList.add("task-lift");
    const items = [];
    if (!readOnly()) items.push(menuItem("Сделать задачей", "task", target => createFrom([target.dataset.id], "menu"), true));
    items.push(menuItem("Копировать", "copy", target => copyText(target)));
    if (!readOnly()) items.push(menuItem("Выбрать несколько", "checks", target => startPick(target)));
    menu.replaceChildren(...items); backdrop.hidden = false; menu.hidden = false;
    // Keep menu placement tied to the untouched message geometry. The source
    // bubble is hidden and scaled while its fixed visual copy is displayed.
    const rect = at, width = menu.offsetWidth || 240, height = menu.offsetHeight || 190;
    let top = point?.y ?? rect.bottom + 8, left = point?.x ?? (bubble.classList.contains("out") ? rect.right - width : rect.left);
    if (!point) top = rect.bottom + 8;
    if (top + height > innerHeight - 12) top = Math.max(12, (point?.y ?? rect.top) - height - 8);
    left = Math.max(12, Math.min(left, innerWidth - width - 12));
    menu.style.top = `${Math.round(top)}px`; menu.style.left = `${Math.round(left)}px`;
    navigator.vibrate?.(12);
    requestAnimationFrame(() => menu.querySelector("button")?.focus({ preventScroll: true }));
  }
  function closeMenu() {
    menuFor?.classList.remove("task-lift"); menuFor = null; menuLift?.remove(); menuLift = null;
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
    bubble.classList.toggle("task-picked", picked.has(id));
    const holder = bubble.querySelector(".bub") || bubble;
    if (picked.has(id) && !holder.querySelector(".task-pick-badge")) { const badge = node("span", undefined, "task-pick-badge"); badge.setAttribute("aria-hidden", "true"); badge.append(icon("check")); holder.append(badge); }
    if (!picked.has(id)) holder.querySelector(".task-pick-badge")?.remove();
    paintPick();
  }
  function paintPick() {
    const make = pickBar.querySelector(".task-pick-make");
    make.textContent = picked.size ? `Задача из ${picked.size} ${plural(picked.size, "сообщения", "сообщений", "сообщений")}` : "Выберите сообщения";
    make.disabled = !picked.size;
  }
  function endPick() {
    if (!pickMode) return;
    pickMode = false; document.documentElement.classList.remove("task-selecting"); canvas?.classList.remove("task-picking");
    pickBar.hidden = true; for (const bubble of history.querySelectorAll(".task-picked")) { bubble.classList.remove("task-picked"); bubble.querySelector(".task-pick-badge")?.remove(); } picked = new Set();
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

  // ── sheets on a computer: a floating card exactly as wide as the composer, standing on it,
  // with every corner rounded and the site left as it is (no shade, no blur). On a phone
  // they stay bottom sheets.
  function placeSheet(dialog) {
    if (!dialog?.open) return;
    const wide = innerWidth >= 720, compose = $("#chat-compose");
    dialog.classList.toggle("floating", wide);
    if (!wide) { for (const key of ["left", "width", "bottom", "top", "maxHeight"]) dialog.style[key] = ""; return; }
    const box = compose && !compose.hidden && state.pane === "messages" ? compose.getBoundingClientRect() : null;
    if (box && box.width > 240 && box.top > 200) {
      const ceiling = Math.max(12, ($("#chat-header")?.getBoundingClientRect().bottom || 12) + 8);
      Object.assign(dialog.style, { left: `${Math.round(box.left)}px`, width: `${Math.round(box.width)}px`, top: "auto",
        bottom: `${Math.round(innerHeight - box.top + 10)}px`, maxHeight: `${Math.round(Math.max(260, box.top - 10 - ceiling))}px` });
    } else {
      const area = document.querySelector("[data-page]:not([hidden])")?.getBoundingClientRect() || { left: 0, width: innerWidth };
      const width = Math.min(600, area.width - 48);
      Object.assign(dialog.style, { left: `${Math.round(area.left + (area.width - width) / 2)}px`, width: `${Math.round(width)}px`,
        top: "auto", bottom: "24px", maxHeight: `${innerHeight - 48}px` });
    }
  }
  addEventListener("resize", () => { for (const dialog of document.querySelectorAll(".task-sheet[open], #task-settings-dialog[open]")) { placeSheet(dialog); paintEdges(dialog); } fitKeyboard(); });
  // The middle of a sheet scrolls; its head and the button at the bottom stay put. Lines and soft
  // edges show only when there is something to scroll.
  function paintEdges(dialog) {
    const body = dialog?.querySelector(".sheet-body"); if (!body) return;
    const more = body.scrollHeight - body.clientHeight > 2;
    dialog.classList.toggle("scrolls", more);
    dialog.classList.toggle("at-top", body.scrollTop <= 1);
    dialog.classList.toggle("at-end", body.scrollTop >= body.scrollHeight - body.clientHeight - 1);
  }
  // Bring a just-opened block (or the field being typed in) into the visible middle.
  function revealIn(element) {
    const body = element?.closest(".sheet-body"); if (!body) return;
    requestAnimationFrame(() => {
      const top = element.offsetTop - body.offsetTop - 8, bottom = top + element.offsetHeight + 16;
      if (top < body.scrollTop) body.scrollTo({ top, behavior: "smooth" });
      else if (bottom > body.scrollTop + body.clientHeight) body.scrollTo({ top: Math.min(top, bottom - body.clientHeight), behavior: "smooth" });
    });
  }
  // On a phone the sheet stands on the keyboard: its bottom follows the visible part of the screen.
  function fitKeyboard() {
    if (!sheet?.open || innerWidth >= 720) return;
    const view = window.visualViewport, covered = view ? Math.max(0, innerHeight - view.height - view.offsetTop) : 0;
    // A tall phone keeps the chat header in sight; a short one gives the sheet the whole height.
    const ceiling = innerHeight < 700 ? 10 : Math.max(12, ($("#chat-header")?.getBoundingClientRect().bottom || 12) + 8);
    sheet.style.bottom = covered ? `${Math.round(covered)}px` : "";
    sheet.style.maxHeight = `${Math.round((view ? view.height + view.offsetTop : innerHeight) - ceiling)}px`;
    paintEdges(sheet);
  }
  window.visualViewport?.addEventListener("resize", fitKeyboard);
  window.visualViewport?.addEventListener("scroll", fitKeyboard);
  // «Что сделать» grows with a long draft instead of hiding its second half.
  function fitTitle() {
    for (const [name, most] of [["title", 160], ["details", 220]]) {
      const field = sheet?.querySelector(`textarea[name="${name}"]`); if (!field) continue;
      field.style.height = "auto"; field.style.height = `${Math.min(most, field.scrollHeight + 2)}px`;
    }
  }

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
    const draft = { title, sources, conversation_id: convId, client_label: clientLabel(), origin };
    await openSheet({ draft });
    if (!readOnly()) suggest(draft);
  }
  // The model rewrites the sheet only where the operator has not touched it yet.
  let suggestSeq = 0;
  async function suggest(draft) {
    const seq = ++suggestSeq, form = sheet.querySelector("form"), badge = sheet.querySelector(".task-ai-badge");
    const initial = { title: form.elements.title.value, due: sheetDue, kind: sheet.dataset.kind, details: form.elements.details.value };
    badge.hidden = false; badge.textContent = "AI думает…"; badge.classList.add("busy");
    try {
      const result = await api(`${base()}/tasks/draft`, { method: "POST", timeoutMs: 18000,
        data: { conversation_id: draft.conversation_id, message_ids: draft.sources.map(source => source.id), client_label: draft.client_label } });
      if (seq !== suggestSeq || !sheet.open || sheetTask || sheetDraft?.conversation_id !== draft.conversation_id) return;
      if (!result.ai) { badge.hidden = true; return; }
      if (form.elements.title.value === initial.title && result.title) { form.elements.title.value = result.title; fitTitle(); }
      if (form.elements.details.value === initial.details && !initial.details && result.details) { form.elements.details.value = result.details; paintDetails(sheet.querySelector(".task-details").classList.contains("open")); fitTitle(); }
      if (sheet.dataset.kind === initial.kind && !initial.kind && result.kind) { sheet.dataset.kind = result.kind; sheet.dataset.kindAi = "1"; paintKinds(result.kind); }
      if (sheetDue === initial.due && result.due_at) { sheetDue = result.due_at; paintDue(); }
      badge.textContent = "Черновик AI"; badge.classList.remove("busy");
    } catch (_) { if (seq === suggestSeq) badge.hidden = true; }
  }
  function buildSheet() {
    const dialog = document.createElement("dialog"); dialog.className = "live-sheet task-sheet"; dialog.id = "task-dialog";
    dialog.setAttribute("aria-labelledby", "task-sheet-title");
    dialog.innerHTML = `<div class="sheet-head task-head"><h2 id="task-sheet-title">Новая задача</h2><span class="task-ai-badge" hidden></span>
      <button class="iconbtn plain task-close" type="button" data-task-close aria-label="Закрыть"></button></div>
      <small class="task-sheet-client"></small>
      <form class="sheet-body task-form" novalidate>
        <label class="task-field task-title-field"><span>Что сделать?</span><span class="task-title-box"><textarea name="title" rows="1" maxlength="200" required></textarea><span class="task-title-pen" aria-hidden="true"></span></span></label>
        <div class="task-quotes"></div>
        <div class="task-field task-due-field" role="group" aria-labelledby="task-due-label"><span id="task-due-label">Срок</span><div class="task-chips task-due"></div>
          <input class="task-due-input" type="datetime-local" aria-label="Дата и время срока" hidden></div>
        <div class="task-pair">
          <label class="task-pick task-kind-pick"><span class="task-pick-label">Тип</span><span class="task-pick-value"></span><select name="kind" aria-label="Тип задачи"></select><span class="task-pick-chev" aria-hidden="true"></span></label>
          <label class="task-pick task-assignee"><span class="task-pick-label">Исполнитель</span><span class="task-pick-value"></span><select name="assignee" aria-label="Исполнитель"></select><span class="task-pick-chev" aria-hidden="true"></span></label>
        </div>
        <div class="task-details">
          <div class="task-details-head"><span class="task-details-label">Подробности</span><button class="task-details-toggle" type="button" aria-expanded="false"></button></div>
          <p class="task-details-preview" hidden></p>
          <textarea name="details" rows="2" maxlength="2000" placeholder="Необязательно" aria-label="Подробности"></textarea>
        </div>
        <p class="notice error task-error" role="alert" hidden></p>
        <div class="task-actions"><button class="btn sec task-open-chat" type="button" hidden>Открыть переписку</button>
          <button class="btn sec task-cancel-task" type="button" hidden>Отменить задачу</button>
          <button class="btn sec task-done" type="button" hidden>Выполнено</button>
          <button class="btn primary task-save" type="submit">Создать задачу</button>
          <button class="btn primary task-edit-done" type="button">Готово</button></div>
      </form>`;
    dialog.querySelector("[data-task-close]").append(icon("close"));
    dialog.querySelector(".task-title-pen").append(icon("edit"));
    for (const chev of dialog.querySelectorAll(".task-pick-chev")) chev.append(icon("down"));
    const title = dialog.querySelector('textarea[name="title"]');
    // A phone keeps only the text and «Готово» above the keyboard while the task is being typed.
    const typing = () => matchMedia("(pointer: coarse)").matches && innerWidth < 720;
    // Nothing is hidden while typing: only the button at the bottom reads «Готово», so the sheet never jumps.
    title.addEventListener("focus", () => {
      if (!typing()) return;
      dialog.classList.add("title-editing");
      setTimeout(() => { if (document.activeElement === title) { fitKeyboard(); revealIn(title.closest(".task-field")); } }, 280);
    });
    title.addEventListener("blur", () => { dialog.classList.remove("title-editing"); setTimeout(fitKeyboard, 60); });
    dialog.querySelector("form").addEventListener("scroll", () => paintEdges(dialog), { passive: true });
    dialog.querySelector(".task-edit-done").addEventListener("pointerdown", event => event.preventDefault());
    dialog.querySelector(".task-edit-done").addEventListener("click", () => title.blur());
    dialog.querySelector('select[name="kind"]').addEventListener("change", event => { sheet.dataset.kind = event.target.value; sheet.dataset.kindAi = ""; paintKinds(sheet.dataset.kind); });
    dialog.querySelector('select[name="assignee"]').addEventListener("change", paintAssignee);
    dialog.querySelector(".task-details-toggle").addEventListener("click", () => {
      const open = !dialog.querySelector(".task-details").classList.contains("open");
      try { localStorage.setItem("cm:task-details-open", open ? "1" : ""); } catch (_) {}
      paintDetails(open); if (open) { fitTitle(); revealIn(dialog.querySelector(".task-details")); dialog.querySelector('textarea[name="details"]').focus({ preventScroll: true }); }
      paintEdges(dialog);
    });
    dialog.querySelector("[data-task-close]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
    dialog.querySelector("form").addEventListener("submit", event => { event.preventDefault(); saveSheet(); });
    title.addEventListener("input", fitTitle);
    dialog.querySelector('textarea[name="details"]')?.addEventListener("input", fitTitle);
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
    if (key === "hour") return "Через 1 час";
    if (key === "two_hours") return `Через 2 часа · ${clock(value)}`;
    if (key === "end_of_day") return dayKey(value) === dayKey(Date.now()) ? `Сегодня до ${clock(value)}` : `${weekday(value)} до ${clock(value)}`;
    return dayKey(value) === addDays(dayKey(Date.now()), 1) ? `Завтра к ${clock(value)}` : `${weekday(value)} к ${clock(value)}`;
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
    chips.push(chip(custom ? `${shortDay(sheetDue)}, ${clock(sheetDue)}` : "Выбрать дату…", custom || !input.hidden, () => {
      input.hidden = false; input.value = localInput(sheetDue || presets.default || Date.now()); input.focus(); input.showPicker?.();
    }));
    box.replaceChildren(...chips);
  }
  function paintKinds(selected) {
    const select = sheet.querySelector('select[name="kind"]');
    if (select.options.length !== Object.keys(KINDS).length + 1) select.replaceChildren(new Option("Не выбран", ""), ...Object.entries(KINDS).map(([key, title]) => new Option(title, key)));
    select.value = selected || "";
    const box = sheet.querySelector(".task-kind-pick");
    box.querySelector(".task-pick-label").textContent = sheet.dataset.kindAi && selected ? "Тип · AI" : "Тип";
    box.querySelector(".task-pick-value").textContent = KINDS[selected] || "Не выбран";
    box.classList.toggle("empty", !selected);
  }
  function paintAssignee() {
    const select = sheet.querySelector('select[name="assignee"]'), box = sheet.querySelector(".task-assignee");
    box.querySelector(".task-pick-value").textContent = select.selectedOptions[0]?.textContent || "Вы";
  }
  const detailsPoints = text => text.split("\n").map(line => line.trim()).filter(Boolean);
  function paintDetails(open) {
    const box = sheet.querySelector(".task-details"), field = box.querySelector("textarea"), toggle = box.querySelector(".task-details-toggle");
    const lines = detailsPoints(field.value), preview = box.querySelector(".task-details-preview");
    box.classList.toggle("open", open); toggle.setAttribute("aria-expanded", String(open)); field.hidden = !open;
    toggle.replaceChildren(document.createTextNode(open ? "Свернуть" : lines.length > 1 ? `Подробности · ${lines.length} ${plural(lines.length, "пункт", "пункта", "пунктов")}` : lines.length ? "Подробности" : "+ Подробности"), icon("down"));
    preview.textContent = lines.join(" · "); preview.hidden = open || !lines.length;
  }
  function detailsOpenByDefault() { try { return localStorage.getItem("cm:task-details-open") === "1"; } catch (_) { return false; } }
  function paintQuotes(sources, editable, open = false) {
    const box = sheet.querySelector(".task-quotes"); box.replaceChildren(); box.classList.toggle("open", open);
    box.hidden = !sources.length; if (!sources.length) return;
    if (!open) {
      const short = node("button", undefined, `task-quote task-quote-short ${sources[0].direction === "out" ? "out" : "in"}`); short.type = "button";
      short.setAttribute("aria-label", "Показать сообщения целиком");
      const glyph = node("span", "“", "task-quote-mark"); glyph.setAttribute("aria-hidden", "true");
      short.append(glyph, node("span", sources[0].text || "Сообщение без текста", "task-quote-text"));
      if (sources.length > 1) short.append(node("span", `+${sources.length - 1}`, "task-quote-more"));
      const chev = node("span", undefined, "task-quote-chev"); chev.setAttribute("aria-hidden", "true"); chev.append(icon("chevron")); short.append(chev);
      short.addEventListener("click", () => { paintQuotes(sources, editable, true); paintEdges(sheet); revealIn(box); });
      box.append(short); return;
    }
    for (const source of sources) {
      const quote = node("div", undefined, `task-quote ${source.direction === "out" ? "out" : "in"}`);
      const glyph = node("span", "“", "task-quote-mark"); glyph.setAttribute("aria-hidden", "true");
      quote.append(glyph, node("span", source.text || "Сообщение без текста", "task-quote-text"));
      if (editable && sources.length > 1) {
        const remove = node("button", undefined, "task-quote-del"); remove.type = "button"; remove.setAttribute("aria-label", "Убрать сообщение из задачи"); remove.append(icon("close"));
        remove.addEventListener("click", () => { sheetDraft.sources = sheetDraft.sources.filter(s => s.id !== source.id); paintQuotes(sheetDraft.sources, true, true); });
        quote.append(remove);
      }
      box.append(quote);
    }
    const fold = node("button", sources.length > 1 ? "Свернуть сообщения" : "Свернуть сообщение", "task-quote-fold"); fold.type = "button";
    fold.addEventListener("click", () => { paintQuotes(sources, editable, false); paintEdges(sheet); }); box.append(fold);
  }
  async function openSheet({ task = null, draft = null }) {
    sheet ||= buildSheet();
    // A failed refresh keeps the sheet usable with the options already loaded.
    try { await context(!ctx || Date.now() - ctxAt > 30000); }
    catch (error) { if (!ctx) { toast(error.message, true); return; } }
    sheetTask = task; sheetDraft = draft ? { ...draft, sources: [...(draft.sources || [])] } : null;
    const editing = Boolean(task), form = sheet.querySelector("form");
    suggestSeq += 1;
    const heading = sheet.querySelector("#task-sheet-title"), badge = sheet.querySelector(".task-ai-badge");
    heading.textContent = editing ? (task.status === "open" ? "Задача" : task.status === "done" ? "Задача выполнена" : "Задача отменена") : "Новая задача";
    sheet.classList.remove("title-editing");
    badge.hidden = !(draft?.origin === "voice" && draft?.title); badge.textContent = "Из голоса"; badge.classList.remove("busy");
    sheet.querySelector(".task-sheet-client").textContent = (editing ? task.client_label : draft?.client_label) || "";
    form.elements.title.value = editing ? task.title : draft?.title || "";
    form.elements.details.value = editing ? task.details || "" : "";
    sheetDue = editing ? task.due_at : draft?.due_at || ctx?.presets?.default || null;
    sheet.querySelector(".task-due-input").hidden = true;
    sheet.dataset.kind = editing ? task.kind || "" : draft?.kind || ""; sheet.dataset.kindAi = "";
    paintDue(); paintKinds(sheet.dataset.kind);
    paintQuotes(editing ? task.sources || [] : sheetDraft?.sources || [], !editing);
    const assignee = form.elements.assignee, people = ctx?.assignees || [];
    assignee.replaceChildren(...people.map(p => new Option(p.id === ctx.me ? `${p.name} (я)` : p.name, p.id)));
    assignee.value = editing ? task.assignee_id : draft?.assignee_id || ctx?.me;
    if (!assignee.value) assignee.value = ctx?.me;
    // With nobody else to choose, the box just says who does it.
    const choose = people.length >= 2; sheet.dataset.assign = choose ? "1" : "";
    if (!choose) assignee.replaceChildren(new Option(editing && task.assignee_id && task.assignee_id !== ctx?.me ? task.assignee_name || "Сотрудник" : "Вы", assignee.value || ctx?.me || ""));
    sheet.querySelector(".task-assignee").classList.toggle("fixed", !choose);
    paintAssignee();
    paintDetails(detailsOpenByDefault());
    const locked = readOnly();
    for (const field of form.querySelectorAll("textarea,select,input")) field.disabled = locked;
    if (!choose) assignee.disabled = true;
    sheet.querySelector(".task-save").hidden = locked;
    sheet.querySelector(".task-save").textContent = editing ? "Сохранить" : "Создать задачу";
    sheet.querySelector(".task-done").hidden = !editing || locked || task.status !== "open";
    sheet.querySelector(".task-cancel-task").hidden = !editing || locked;
    sheet.querySelector(".task-cancel-task").textContent = editing && task.status !== "open" ? "Открыть снова" : "Отменить задачу";
    sheet.querySelector(".task-open-chat").hidden = !editing || !task.conversation_id || (state.pane === "messages" && task.conversation_id === convId);
    const error = sheet.querySelector(".task-error"); error.hidden = true; error.textContent = "";
    if (!sheet.open) sheet.showModal();
    placeSheet(sheet); fitTitle(); fitKeyboard(); sheet.querySelector(".sheet-body").scrollTop = 0; paintEdges(sheet);
    // No caret on opening: the keyboard would cover the sheet. A tap on the field starts editing.
  }
  async function saveSheet() {
    const form = sheet.querySelector("form"), error = sheet.querySelector(".task-error"), save = sheet.querySelector(".task-save");
    const title = form.elements.title.value.replace(/\s+/g, " ").trim();
    if (!title) { error.textContent = "Напишите, что нужно сделать."; error.hidden = false; form.elements.title.focus(); return; }
    if (!sheetDue) { error.textContent = "Выберите срок."; error.hidden = false; return; }
    const data = { title, details: form.elements.details.value.trim(), kind: sheet.dataset.kind || null, due_at: sheetDue };
    if (sheet.dataset.assign) data.assignee_id = form.elements.assignee.value;
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
    placeSheet(dialog);
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
  // «Выполнено» from the strip, a reminder or the task sheet opens «Задача выполнена» with a
  // reply draft; a tick in the list stays quick and offers the reply in the capsule.
  async function complete(task, { ask = true } = {}) {
    const done = await action(task, "complete", null); if (!done) return;
    if (ask && task.conversation_id && !readOnly()) { openDone(done); return; }
    doneCapsule(done);
  }
  function doneCapsule(task) {
    notices?.notify({ type: "success", title: "Задача выполнена", text: task.title, key: `task:complete:${task.id}`, always: true, count: false,
      actions: [
        ...(task.conversation_id ? [{ label: "Ответить клиенту", run: () => openDone(task) }] : []),
        { label: "Вернуть", run: () => action(task, "reopen", "Задача снова открыта") },
      ] });
  }
  let doneSheet = null, doneTask = null, doneSeq = 0;
  function buildDone() {
    const dialog = document.createElement("dialog"); dialog.className = "live-sheet task-sheet task-done-sheet"; dialog.id = "task-done-dialog";
    dialog.setAttribute("aria-labelledby", "task-done-title");
    dialog.innerHTML = `<div class="task-done-head"><span class="task-done-ic"></span><div><h2 id="task-done-title">Задача выполнена</h2><small class="task-done-what"></small></div>
      <button class="iconbtn plain" type="button" data-done-close aria-label="Закрыть"></button></div>
      <label class="task-field"><span class="task-done-label">Ответ клиенту</span><textarea rows="4" maxlength="4000" class="task-done-text"></textarea></label>
      <p class="form-note task-done-note">Можно поправить перед отправкой. Отправит оператор, не AI.</p>
      <p class="notice error task-done-error" role="alert" hidden></p>
      <div class="task-done-actions"><button type="button" class="btn sec task-done-skip">Без ответа</button><button type="button" class="btn primary task-done-send">Отправить</button></div>
      <button type="button" class="task-done-undo">Вернуть задачу в работу</button>`;
    dialog.querySelector(".task-done-ic").append(icon("check"));
    dialog.querySelector("[data-done-close]").append(icon("close"));
    const close = () => { doneSeq += 1; dialog.close(); };
    dialog.querySelector("[data-done-close]").addEventListener("click", close);
    dialog.addEventListener("click", event => { if (event.target === dialog) close(); });
    dialog.querySelector(".task-done-skip").addEventListener("click", () => { const task = doneTask; close(); if (task) doneCapsule(task); });
    dialog.querySelector(".task-done-undo").addEventListener("click", async () => { const task = doneTask; close(); if (task) await action(task, "reopen", "Задача снова открыта"); });
    dialog.querySelector(".task-done-text").addEventListener("input", () => paintDoneSend());
    dialog.querySelector(".task-done-send").addEventListener("click", sendDone);
    document.body.append(dialog); return dialog;
  }
  function paintDoneSend() { doneSheet.querySelector(".task-done-send").disabled = !doneSheet.querySelector(".task-done-text").value.trim(); }
  async function openDone(task) {
    doneSheet ||= buildDone(); doneTask = task; const seq = ++doneSeq;
    const area = doneSheet.querySelector(".task-done-text"), label = doneSheet.querySelector(".task-done-label");
    doneSheet.querySelector(".task-done-what").textContent = [task.title, task.client_label].filter(Boolean).join(" · ");
    doneSheet.querySelector(".task-done-error").hidden = true;
    area.value = ""; area.placeholder = "Готовим черновик ответа…"; area.classList.add("busy"); label.textContent = "Ответ клиенту";
    paintDoneSend();
    if (!doneSheet.open) doneSheet.showModal();
    placeSheet(doneSheet);
    try {
      const result = await api(`${base()}/tasks/${task.id}/reply`, { method: "POST", timeoutMs: 13000 });
      if (seq !== doneSeq) return;
      if (result.text && !area.value.trim()) { area.value = result.text; label.textContent = "Ответ клиенту · черновик AI"; }
    } catch (_) { /* The operator writes the answer. */ }
    if (seq !== doneSeq) return;
    area.classList.remove("busy"); area.placeholder = "Напишите ответ клиенту"; paintDoneSend();
  }
  // «Отправить» goes through the dialog's own composer, exactly like a typed answer.
  async function sendDone() {
    const task = doneTask, text = doneSheet.querySelector(".task-done-text").value.trim(), error = doneSheet.querySelector(".task-done-error");
    if (!task || !text) return;
    if (!(state.pane === "messages" && convId === task.conversation_id)) {
      await openChat(task);
      for (let step = 0; step < 30 && convId !== task.conversation_id; step += 1) await new Promise(resolve => setTimeout(resolve, 150));
    }
    const input = $("#chat-input"), send = $("#chat-send");
    if (convId !== task.conversation_id || !input || input.disabled) { error.textContent = "Не удалось открыть переписку с клиентом. Ответ можно отправить из диалога."; error.hidden = false; return; }
    if (input.value.trim() && input.value.trim() !== text) { error.textContent = "В поле ввода этого диалога уже есть черновик. Отправьте или очистите его."; error.hidden = false; return; }
    input.value = text; input.dispatchEvent(new Event("input", { bubbles: true }));
    doneSeq += 1; doneSheet.close();
    await new Promise(resolve => requestAnimationFrame(resolve));
    if (!send.disabled) send.click(); else input.focus();
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
      const late = Math.round((now - new Date(task.due_at).getTime()) / 60000);
      notices?.notify({ type: "warning", strong: true, title: `Пора: ${task.title}`,
        text: [task.client_label, `срок ${clock(task.due_at)}` + (late >= 5 ? `, просрочено на ${late < 60 ? late + " мин" : Math.round(late / 60) + " ч"}` : "")].filter(Boolean).join(" · "),
        key: `task:due:${task.id}`, always: true, sticky: true, count: false, glyph: "bell", actions: [
          { label: "Выполнено", run: () => complete(task) },
          { label: "Через 1 ч", run: () => action(task, "snooze", "Задача отложена на час", { due_at: snoozeTarget("hour") }) },
          { label: "Открыть", keep: true, run: () => task.conversation_id ? openChat(task) : openSheet({ task }) },
        ] });
    }
    // A quiet heads-up 15 minutes before a task with a set time.
    for (const task of summary.upcoming || []) {
      const left = Math.round((new Date(task.due_at).getTime() - now) / 60000), key = `cm:task-soon:${task.id}@${task.due_at}`;
      if (left <= 0 || left > 15 || store.get(key) || store.get(remindedKey(task))) continue;
      store.set(key, "1");
      notices?.notify({ type: "info", glyph: "bell", title: `Скоро: ${task.title}`, text: [task.client_label, `срок ${clock(task.due_at)}, осталось ${left} мин`].filter(Boolean).join(" · "),
        key: `task:soon:${task.id}`, always: true, count: false, actions: [{ label: "Открыть", run: () => task.conversation_id ? openChat(task) : openSheet({ task }) }] });
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

  // ── dictated tasks (voice.js: finger to «Задача», or a phrase starting «Задача…») ──
  document.addEventListener("cm:voice-task", async event => {
    const { text, conversation, spokenPrefix } = event.detail || {};
    if (!text || !allowed() || readOnly()) return;
    try {
      await context();
      const instant = ctx?.personal?.voice_task !== "confirm";
      const result = await api(`${base()}/tasks/voice`, { method: "POST", timeoutMs: 20000,
        data: { text, conversation_id: conversation || null, client_label: conversation && conversation === convId ? clientLabel() : "", create: instant } });
      if (result.task) {
        const task = result.task;
        notices?.notify({ type: "success", glyph: "task", title: spokenPrefix ? "Похоже на задачу — клиенту не отправили" : "Задача создана",
          text: [task.title, dueText(task), task.kind ? KINDS[task.kind] : "", task.sources?.length ? `${task.sources.length} ${plural(task.sources.length, "сообщение", "сообщения", "сообщений")} клиента` : ""].filter(Boolean).join(" · "),
          key: `task:voice:${task.id}`, always: true, sticky: true, count: false, ttl: 8000,
          actions: [{ label: "Изменить", keep: true, run: () => openSheet({ task }) }, { label: "Отменить", run: () => action(task, "cancel", "Задача отменена") }] });
        await changed();
      } else {
        const draft = result.draft;
        await openSheet({ draft: { title: draft.title, sources: draft.sources || [], conversation_id: draft.conversation_id, client_label: draft.client_label,
          origin: "voice", due_at: draft.due_at, kind: draft.kind, assignee_id: draft.assignee_id } });
      }
    } catch (error) { if (error.name !== "AbortError") toast(error.message, true); }
  });

  // ── promise hint: «уточню», «проверю» in the operator's answer → «Создать задачу?» ──
  const PROMISE = /уточн|провер|посмотр|сообщ|перезвон|вернусь|напиш|узна|отвеч/i;
  const hintBar = node("div", undefined, "task-hint"); hintBar.hidden = true; hintBar.setAttribute("role", "status");
  hintBar.innerHTML = `<span class="task-hint-ic"></span><button type="button" class="task-hint-main"><strong>Создать задачу?</strong><small></small></button><button type="button" class="task-hint-make">Создать</button><button type="button" class="task-hint-close" aria-label="Не создавать"></button>`;
  hintBar.querySelector(".task-hint-ic").append(icon("task")); hintBar.querySelector(".task-hint-close").append(icon("close"));
  canvas?.append(hintBar);
  let hint = null, hintSeq = 0;
  const hintOffKey = id => `cm:task-hint-off:${id}:${dayKey(Date.now())}`;
  function hideHint() { hint = null; hintSeq += 1; hintBar.hidden = true; canvas?.classList.remove("has-task-hint"); }
  async function askHint(text, conversation) {
    if (!conversation || !text || !PROMISE.test(text) || readOnly() || ctx?.personal?.hints === false || store.get(hintOffKey(conversation))) return;
    // With an open task in this dialog the capsule already asks «Задача выполнена?» — one question at a time.
    if (conversation === convId && openTasks().length) return;
    const seq = ++hintSeq;
    await new Promise(resolve => setTimeout(resolve, 900));
    try {
      const result = await api(`${base()}/tasks/hint`, { method: "POST", timeoutMs: 13000, data: { conversation_id: conversation, text } });
      if (seq !== hintSeq || conversation !== convId || !result.hint) return;
      hint = { ...result.hint, conversation_id: conversation, client_label: clientLabel() };
      hintBar.querySelector("small").textContent = `${hint.title} · ${dueText(hint)}`;
      hintBar.hidden = false; canvas?.classList.add("has-task-hint");
    } catch (_) { /* A missed hint is not an error for the operator. */ }
  }
  hintBar.querySelector(".task-hint-close").addEventListener("click", () => { if (hint) store.set(hintOffKey(hint.conversation_id), "1"); hideHint(); });
  hintBar.querySelector(".task-hint-main").addEventListener("click", () => {
    const current = hint; hideHint(); if (!current) return;
    openSheet({ draft: { title: current.title, sources: current.sources || [], conversation_id: current.conversation_id, client_label: current.client_label,
      origin: "ai_hint", due_at: current.due_at, kind: current.kind } });
  });
  hintBar.querySelector(".task-hint-make").addEventListener("click", async () => {
    const current = hint; hideHint(); if (!current) return;
    try {
      const created = await api(`${base()}/tasks`, { method: "POST", data: { title: current.title, kind: current.kind || null, due_at: current.due_at,
        conversation_id: current.conversation_id, client_label: current.client_label, message_ids: (current.sources || []).map(source => source.id), origin: "ai_hint" } });
      notices?.notify({ type: "success", glyph: "task", title: "Задача создана", text: `${created.title} · ${dueText(created)}`, key: `task:created:${created.id}`, always: true, count: false,
        actions: [{ label: "Изменить", keep: true, run: () => openSheet({ task: created }) }] });
      await changed();
    } catch (error) { if (error.name !== "AbortError") toast(error.message, true); }
  });
  document.addEventListener("cm:chat-selected", hideHint);
  $("#chat-compose")?.addEventListener("submit", () => { const text = $("#chat-input")?.value || ""; if (convId) askHint(text.trim(), convId); }, true);

  // ── after an answer to the client: «Задача выполнена?» ─────────────────────
  let seenOut = new Set(), seenFor = null, replySent = 0;
  $("#chat-compose")?.addEventListener("submit", () => { if (convId && openTasks().length) replySent = Date.now(); }, true);
  const asked = new Set();
  function watchReplies() {
    const ids = [...history.querySelectorAll(".chat-bubble.msg.out[data-id]")].map(bubble => bubble.dataset.id);
    if (seenFor !== convId) { seenFor = convId; seenOut = new Set(ids); return; }
    const fresh = ids.filter(id => !seenOut.has(id)); fresh.forEach(id => seenOut.add(id));
    const first = openTasks()[0];
    // Only right after the operator pressed «Отправить» in this dialog.
    if (!fresh.length || !first || !replySent || Date.now() - replySent > 30000 || asked.has(first.id) || readOnly()) return;
    replySent = 0; asked.add(first.id);
    notices?.notify({ type: "info", glyph: "task", title: "Ответили клиенту. Задача выполнена?", text: first.title, key: `task:ask:${first.id}`,
      always: true, sticky: true, count: false, ttl: 10000, actions: [{ label: "Да, выполнена", run: () => complete(first, { ask: false }) }, { label: "Нет", run: () => {} }] });
  }
  new MutationObserver(() => { if (convId) watchReplies(); }).observe(history, { childList: true });

  // ── «Задачи» section ─────────────────────────────────────────────────────
  const pane = $("#pane-tasks");
  function taskRow(task, onOpen) {
    const row = node("div", undefined, `task-row ${task.status === "open" ? bucket(task) : task.status}`);
    const check = node("button", undefined, "task-check"); check.type = "button";
    check.setAttribute("aria-label", task.status === "open" ? `Отметить выполненной: ${task.title}` : `Открыть снова: ${task.title}`);
    if (task.status === "done") check.append(icon("check"));
    check.disabled = readOnly();
    check.addEventListener("click", event => { event.stopPropagation(); if (task.status === "open") complete(task, { ask: false }); else action(task, "reopen", "Задача снова открыта"); });
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
      const both = ctx?.admin || ctx?.studio?.visibility !== "own";
      const [open, other, closed] = await Promise.all([
        api(`${base()}/tasks?scope=${scope}&status=open`, { timeoutMs: 15000 }),
        both ? api(`${base()}/tasks?scope=${scope === "mine" ? "all" : "mine"}&status=open`, { timeoutMs: 15000 }) : Promise.resolve({ tasks: [] }),
        closedOpen ? api(`${base()}/tasks?scope=${scope}&status=closed`, { timeoutMs: 15000 }) : Promise.resolve({ tasks: lists.closed }),
      ]);
      if (epoch !== state.epoch) return;
      lists = { open: open.tasks || [], closed: closed.tasks || [] };
      counts = scope === "mine" ? { mine: lists.open.length, all: (other.tasks || []).length } : { all: lists.open.length, mine: (other.tasks || []).length };
      paintPane();
    } catch (error) { if (error.name !== "AbortError") { const box = pane.querySelector(".task-pane-error"); box.textContent = error.message; box.hidden = false; } }
    finally { listBusy = false; }
  }
  function paintPane() {
    if (!pane) return;
    pane.querySelector(".task-pane-error").hidden = true;
    const all = !ctx?.admin && ctx?.studio?.visibility === "own" ? false : true;
    const tabs = pane.querySelector(".task-scope"); tabs.hidden = !all;
    for (const tab of tabs.querySelectorAll("button")) {
      tab.setAttribute("aria-selected", String(tab.dataset.scope === scope));
      const count = counts[tab.dataset.scope];
      tab.textContent = (tab.dataset.scope === "mine" ? "Мои" : "Все") + (count === undefined ? "" : ` · ${count}`);
    }
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
    if (closedOpen && !lists.closed.length) toggle.lastChild.textContent = "За неделю ничего не закрыто";
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
    form.elements.alfa_copy.disabled = true; // the AlfaCRM connection is read-only for now
    show(dialog.querySelector(".task-studio-note"), !ctx.admin);
    dialog.querySelector(".task-settings-error").hidden = true;
    dialog.showModal(); placeSheet(dialog);
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
