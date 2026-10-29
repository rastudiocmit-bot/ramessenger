"use strict";
window.CenterChat = ({ $, state, api, node, show, notice, date, toast, confirm, renderPane }) => {
  const icon = name => window.CenterIcon ? window.CenterIcon(name) : node("span", name === "checks" ? "✓✓" : name === "check" ? "✓" : "", "icon-fallback");
  const quickReplies = [
    ["Пробное занятие", "Здравствуйте! Записала вас на пробное занятие. Подскажите, какой день недели удобнее — будни после 17:00 или суббота утром?"],
    ["Перенос занятия", "Занятие перенесено. Подтвердите, пожалуйста, что новое время вам подходит."],
    ["Оплата получена", "Оплату получили, абонемент продлён. Спасибо!"],
    ["Остаток занятий", "По абонементу осталось занятий — отправляю актуальный остаток. Продлим на тот же формат?"],
    ["Уточнить филиал", "Подскажите, пожалуйста, какой филиал вам удобнее?"]
  ];
  const statuses = { client: "Клиент", lead: "Лид", archive: "Архивный", unlinked: "Связать с CRM" };
  const sendLabels = { queued: "В очереди", sending: "Отправляем…", accepted: "Принято MAX", delivered: "Доставлено", read: "Прочитано", failed: "Сообщение не отправлено", unknown: "Проверяем отправку", cancelled: "Сообщение не отправлено" };
  const recipientErrors = new Set(["recipient_missing", "multiple_recipients", "invalid_recipient", "max_recipient_missing", "crm_customer_missing"]);
  let generation = 0, listSeq = 0, selectionSeq = 0, linkSeq = 0, cardSeq = 0, detailsSeq = 0;
  let family = null, familySelected = null, familyPreferredStudent = null, familyBusy = false, familyChildChoice = false;
  let familyTimer = null, familyRenderKey = "";
  const familyCache = new Map(), familyRequests = new Map();
  let listToken = null, listTask = null, bootstrapBusy = false;
  let channels = [], items = [], selected = null, page = 0, hasMore = false;
  let timer, searchTimer, linkTimer, pollBusy = false, actionBusy = false, historyBusy = false, listLoading = false, listRetryRequired = false;
  let listPollAfter = 0, listPollError = null, historyPollError = null;
  let notifyTimer, notifySeq = null, notifyEpoch = null, notifyTarget = null, notifyBusy = false;
  let stream = null, streamTimer = null, streamRetry = null, streamLastSeen = 0, streamHealthy = false, notifyAgain = false;
  let streamVersion = null, lastViewVersion = null;
  let nameOrder = "parent_first", preferenceBusy = false;
  const bulkDialog = $("#chat-bulk-dialog");
  let bulkOperation = null, bulkLoading = false, bulkBusy = false, bulkSeq = 0, bulkOpenedSeq = -1;
  let handledTimer = null, handledPointer = null, handledConsumed = false;
  let lastList = "", linkConversation = null, tokenChannel = null, soundContext = null;
  const listHeights = new Map();
  let listGeometry = null, listViewportFrame = 0, listViewportWidth = 0, listRenderRevision = 0, listMeasureRevision = 0, lastViewportKey = "";
  let listTopSpacer = null, listBottomSpacer = null;
  const cache = new Map();
  const active = () => state.user && state.workspace && state.pane === "messages";
  const valid = (epoch, gen) => state.epoch === epoch && generation === gen;
  const base = () => `/workspaces/${state.workspace.id}`;
  const channelPath = () => `${base()}/integrations/max`;
  const chatPath = id => `${base()}/chats${id ? `/${id}` : ""}`;
  const ignored = error => error.name === "AbortError";
  const transient = error => ignored(error) || [408, 429, 502, 503, 504].includes(error.status) || error.status >= 500 || ["NetworkError", "TimeoutError"].includes(error.name) || ["network", "timeout"].includes(error.code);
  const currentItem = () => items.find(item => item.id === selected);
  function fail(error, target = $("#chat-error")) { if (!ignored(error)) notice(error.message, target); }
  function button(text, className, handler) {
    const control = node("button", text, className); control.type = "button";
    control.addEventListener("click", handler); return control;
  }
  function patchChildren(parent, wanted) {
    let cursor = parent.firstChild;
    for (const child of wanted) {
      if (child === cursor) cursor = cursor.nextSibling;
      else parent.insertBefore(child, cursor);
    }
    const retained = new Set(wanted);
    for (const child of [...parent.children]) if (!retained.has(child)) child.remove();
  }
  function reset() {
    stopStream(); bootstrapBusy = false; notifyAgain = false;
    streamVersion = null; lastViewVersion = null;
    bulkSeq += 1; bulkOperation = null; bulkLoading = false; bulkBusy = false;
    clearTimeout(handledTimer); handledTimer = null; handledPointer = null; handledConsumed = false;
    if (bulkDialog.open) bulkDialog.close();
    $("#chat-bulk-summary").replaceChildren(); notice("", $("#chat-bulk-error"));
    for (const id of ["#chat-bulk-close", "#chat-bulk-cancel", "#chat-bulk-retry"]) $(id).disabled = false;
    $("#chat-bulk-confirm").disabled = true; $("#chat-bulk-confirm").textContent = "Подтвердить";
    show($("#chat-bulk-retry"), false);
    state.voiceInput = false; document.dispatchEvent(new CustomEvent("cm:voice-availability"));
    nameOrder = "parent_first"; preferenceBusy = false;
    state.chatPreferenceRevision = (state.chatPreferenceRevision || 0) + 1;
    $("#chat-name-order").disabled = false; $("#chat-name-order").value = nameOrder; renderNameExample();
    $("#chat-name-save-status").textContent = ""; show($("#chat-name-save-status"), false);
    generation += 1; listSeq += 1; selectionSeq += 1; linkSeq += 1; cardSeq += 1; detailsSeq += 1; family = null; familySelected = null; familyPreferredStudent = null; familyChildChoice = false; familyBusy = false; clearTimeout(familyTimer); familyTimer = null;
    clearTimeout(timer); clearTimeout(searchTimer); clearTimeout(linkTimer); clearTimeout(notifyTimer);
    notifyTimer = null; notifyBusy = false; notifySeq = null; notifyEpoch = null; notifyTarget = null; show($("#chat-new-marker"), false);
    for (const data of cache.values()) data.controller?.abort();
    cache.clear(); familyCache.clear(); cancelFamilyRequests(); familyRequests.clear(); familyRenderKey = ""; listToken = null; listTask = null; channels = []; items = []; selected = null; page = 0; hasMore = false;
    listHeights.clear(); listGeometry = null; listViewportFrame = 0; lastViewportKey = "";
    listPollAfter = 0; listPollError = null; historyPollError = null;
    pollBusy = false; actionBusy = false; historyBusy = false; listLoading = false; listRetryRequired = false; lastList = ""; linkConversation = null; tokenChannel = null;
    ["#chat-list", "#chat-history", "#chat-link-results", "#max-channels"].forEach(id => $(id).replaceChildren());
    ["#chat-error", "#max-error", "#max-token-error", "#chat-link-error"].forEach(id => notice("", $(id)));
    $("#chat-input").value = ""; $("#chat-search").value = ""; $("#chat-link-search").value = "";
    $("#chat-send").classList.remove("is-retry"); $("#chat-send").setAttribute("aria-label", "Отправить"); $("#chat-send").title = "Отправить";
    $("#chat-send-hint").hidden = true; $("#chat-send-hint").textContent = ""; resizeCompose();
    $("#chat-branch").replaceChildren(new Option("Все филиалы", ""));
    $("#chat-channel").replaceChildren(new Option("Все каналы MAX", "")); $("#chat-status").value = "";
    $("#max-form").reset(); $("#max-token-form").reset();
    $("#chat-layout").classList.remove("has-selection", "list-collapsed");
    show($("#chat-empty")); show($("#chat-more"), false); show($("#chat-retry"), false); show($("#chat-handled"), false); show($("#chat-details"), false); show($("#chat-unread-count"), false);
    $("#chat-visible-count").textContent = ""; $("#chat-sync-state").textContent = "История на сервере";
    show($("#chat-history-more"), false); show($("#chat-history-status"), false); $("#chat-history-note").textContent = "";
    show($("#chat-stream"), false); show($("#chat-compose"), false);
    $("#chat-person").replaceChildren(node("strong", "Выберите диалог"), node("small", "Новые сообщения и ваши ответы"));
    for (const id of ["max-token-dialog", "chat-link-dialog", "chat-settings-dialog", "chat-details-dialog"]) if ($(`#${id}`).open) $(`#${id}`).close();
  }
  async function connections() {
    const epoch = state.epoch, gen = generation;
    try {
      const result = await api(channelPath());
      if (!valid(epoch, gen)) return;
      const listedChannels = result.channels || [];
      const botChannels = listedChannels.filter(channel => channel.kind === "max_bot");
      const nativeChannels = listedChannels.filter(channel => channel.kind === "alfa_max_browser");
      channels = [...botChannels, ...nativeChannels];
      $("#max-form").querySelectorAll("input,button").forEach(el => { el.disabled = !result.vault_ready || actionBusy; });
      $("#max-add").open = botChannels.length === 0;
      const list = $("#max-channels"); list.replaceChildren();
      for (const channel of botChannels) {
        const card = node("section", undefined, "max-channel mrow");
        const heading = node("div", undefined, "max-channel-heading");
        const info = node("div"); info.append(node("strong", channel.name), node("small", channel.bot_name || "Доступ ещё не проверен"));
        const status = channel.can_send ? (channel.last_inbound_at ? "Приём включён" : "Ожидаем первое сообщение") : channel.error ? "Нужна проверка" : channel.verified ? "Доступ проверен" : "Токен сохранён";
        const statusNode = node("span", undefined, "dotline " + (channel.enabled ? "on" : "off")); statusNode.append(node("i"), node("span", channel.enabled ? "Активен" : "Отключён")); statusNode.title = status;
        const avatar = node("span", undefined, "item-icon"); avatar.append(icon("chats"));
        heading.append(info, statusNode); card.append(avatar, heading);
        if (channel.error) card.append(node("p", channel.error, "notice error"));
        if (channel.failed_events) card.append(node("p", `Не разобрано событий: ${channel.failed_events}. Они сохранены в базе; требуется проверка формата.`, "notice error"));
        const detail = [];
        if (channel.foreign_subscriptions) detail.push(`Других подключений бота: ${channel.foreign_subscriptions}`);
        if (channel.last_inbound_at) detail.push(`Последнее событие: ${date(channel.last_inbound_at)}`);
        if (detail.length) card.append(node("p", detail.join(" · "), "muted small"));
        const actions = node("div", undefined, "max-channel-actions");
        actions.append(button("Проверить доступ", "secondary compact", () => channelAction(channel, "check")),
          button(channel.enabled ? "Отключить приём" : "Включить приём", channel.enabled ? "text-button" : "primary compact", () => channelAction(channel, channel.enabled ? "disable" : "enable")),
          button("Обновить токен", "text-button", () => {
            if ($("#bot-actions-dialog").open) $("#bot-actions-dialog").close();
            tokenChannel = channel.id; const form = $("#max-token-form"); form.reset(); form.elements.name.value = channel.name;
            notice("", $("#max-token-error")); $("#max-token-dialog").showModal();
          }));
        actions.querySelectorAll("button").forEach(el => { el.disabled = actionBusy; el.classList.add("btn"); if (!el.classList.contains("primary")) el.classList.add("sec"); });
        const settings = button("", "iconbtn plain", () => { $("#bot-actions-body").replaceChildren(actions); $("#bot-actions-dialog").showModal(); });
        settings.setAttribute("aria-label", `Настройки бота: ${channel.name}`); settings.append(icon("cog")); settings.disabled = actionBusy;
        card.append(settings); list.append(card);
      }
      const previous = $("#chat-channel").value;
      $("#chat-channel").replaceChildren(new Option("Все каналы MAX", ""), ...channels.map(c => new Option(c.name, c.id)));
      if (channels.some(c => c.id === previous)) $("#chat-channel").value = previous;
    } catch (error) { if (valid(epoch, gen)) fail(error, $("#max-error")); }
  }
  async function channelAction(channel, action) {
    if (actionBusy) return;
    const epoch = state.epoch, gen = generation;
    if (action === "disable" && !await confirm("Отключить приём MAX?", "Новые сообщения перестанут поступать в кабинет. История сохранится, подключение АльфаCRM продолжит работу.", "Отключить")) return;
    if (!valid(epoch, gen)) return;
    actionBusy = true; notice("", $("#max-error"));
    $("#max-channels").querySelectorAll("button").forEach(el => { el.disabled = true; });
    try {
      await api(`${channelPath()}/${channel.id}/${action}`, { method: "POST" });
      if (valid(epoch, gen) && $("#bot-actions-dialog").open) $("#bot-actions-dialog").close();
      if (valid(epoch, gen)) toast(action === "enable" ? "Приём включён. Напишите боту из своего MAX, чтобы проверить переписку." : action === "check" ? "Доступ MAX проверен." : "Приём отключён.");
    } catch (error) { if (valid(epoch, gen)) fail(error, $("#max-error")); }
    finally { if (valid(epoch, gen)) { actionBusy = false; await connections(); } }
  }
  $("#max-form").addEventListener("submit", async event => {
    event.preventDefault(); if (actionBusy) return;
    const epoch = state.epoch, gen = generation, form = event.currentTarget;
    const data = { name: form.elements.name.value.trim(), token: form.elements.token.value.trim() };
    actionBusy = true; notice("", $("#max-error")); form.querySelectorAll("input,button").forEach(el => { el.disabled = true; });
    try {
      const channel = await api(channelPath(), { method: "POST", data });
      form.elements.token.value = "";
      await api(`${channelPath()}/${channel.id}/check`, { method: "POST" });
      if (valid(epoch, gen)) toast("Бот проверен. Включите приём сообщений кнопкой в подключении.");
    } catch (error) { if (valid(epoch, gen)) fail(error, $("#max-error")); }
    finally { if (valid(epoch, gen)) { form.elements.token.value = ""; actionBusy = false; await connections(); } }
  });
  $("#max-token-form").addEventListener("submit", async event => {
    event.preventDefault(); if (actionBusy || !tokenChannel) return;
    const epoch = state.epoch, gen = generation, form = event.currentTarget, channel = tokenChannel;
    actionBusy = true; form.querySelectorAll("input,button").forEach(el => { el.disabled = true; });
    try {
      await api(`${channelPath()}/${channel}`, { method: "PUT", data: { name: form.elements.name.value.trim(), token: form.elements.token.value.trim() } });
      if (valid(epoch, gen)) { $("#max-token-dialog").close(); toast("Токен обновлён."); }
    } catch (error) { if (valid(epoch, gen)) fail(error, $("#max-token-error")); }
    finally { if (valid(epoch, gen)) { actionBusy = false; form.elements.token.value = ""; form.querySelectorAll("input,button").forEach(el => { el.disabled = false; }); await connections(); } }
  });
  $("#max-token-dialog").addEventListener("close", () => { tokenChannel = null; $("#max-token-form").reset(); });

  function entry(id) {
    if (!cache.has(id)) {
      if (cache.size >= 60) { const removable = [...cache.keys()].find(key => key !== selected && !cache.get(key).draft && !cache.get(key).pending); if (removable) cache.delete(removable); }
      cache.set(id, { messages: new Map(), messageRevision: 0, next: 0, oldest: null, expanded: new Set(), more: false, loaded: false, meta: null, draft: "", pending: null });
    }
    return cache.get(id);
  }
  function conversationTime(item) {
    if (item.last_message_sort_time !== null && item.last_message_sort_time !== undefined && /^-?\d+$/.test(String(item.last_message_sort_time))) return BigInt(item.last_message_sort_time);
    const milliseconds = Date.parse(item.last_message_at);
    return Number.isFinite(milliseconds) ? BigInt(milliseconds) * 1000n : null;
  }
  function compareConversations(a, b) {
    const unread = Number(Boolean(b.unread)) - Number(Boolean(a.unread));
    if (unread) return unread;
    const left = conversationTime(a), right = conversationTime(b);
    if (left !== right) return left === null ? 1 : right === null ? -1 : left > right ? -1 : 1;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  }
  function displayNames(item) {
    const parent = item.parent_name === undefined ? item.name : item.parent_name;
    const children = item.children?.length ? [...new Map([...item.children].sort((a,b) => a.name.localeCompare(b.name,"ru") || String(a.id).localeCompare(String(b.id))).map(c => [c.id,c])).values()].map(c => c.name).join(" · ") : (item.students || []).join(" · ");
    if (!parent) return { primary: children || item.name, secondary: children ? "Родитель не указан" : "Связь с CRM не подтверждена" };
    if (!children) return { primary: parent, secondary: "Ребёнок не указан" };
    return nameOrder === "child_first" ? { primary: children, secondary: parent } : { primary: parent, secondary: children };
  }
  function renderNameExample() {
    const sample = $("#chat-name-example");
    sample.querySelector("strong").textContent = nameOrder === "child_first" ? "Ребёнок: Анна Иванова" : "Родитель: Мария Иванова";
    sample.querySelector("small").textContent = nameOrder === "child_first" ? "Родитель: Мария Иванова" : "Ребёнок: Анна Иванова";
  }
  function applyPreferences(preferences) {
    nameOrder = preferences?.name_order === "child_first" ? "child_first" : "parent_first";
    $("#chat-name-order").value = nameOrder; renderNameExample(); renderList(true);
  }
  $("#chat-name-order").addEventListener("change", async () => {
    if (preferenceBusy || !state.workspace) return;
    const epoch = state.epoch, gen = generation, previous = nameOrder;
    state.chatPreferenceRevision = (state.chatPreferenceRevision || 0) + 1;
    preferenceBusy = true; $("#chat-name-order").disabled = true;
    applyPreferences({ name_order: $("#chat-name-order").value });
    const status = $("#chat-name-save-status"); status.textContent = "Сохраняем…"; show(status);
    try {
      const result = await api(`${chatPath()}/preferences`, { method: "PUT", data: { name_order: nameOrder } });
      if (!valid(epoch, gen)) return;
      applyPreferences(result); status.textContent = "Сохранено для вашего аккаунта";
    } catch (error) {
      if (valid(epoch, gen)) { applyPreferences({name_order:previous}); status.textContent = error.message; }
    } finally { if (valid(epoch, gen)) { preferenceBusy = false; $("#chat-name-order").disabled = false; } }
  });
  document.addEventListener("cm:chat-preferences", event => {
    if (event.detail.epoch === state.epoch && event.detail.revision === (state.chatPreferenceRevision || 0) && !preferenceBusy) applyPreferences(event.detail.preferences);
  });
  function queueListViewport() {
    if (!active() || state.chatProtocol < 2 || items.length <= 240 || listViewportFrame) return;
    const epoch = state.epoch, gen = generation;
    listViewportFrame = requestAnimationFrame(() => {
      if (!valid(epoch, gen)) return;
      listViewportFrame = 0; renderList(false, true);
    });
  }
  function measureListRows(list) {
    let changed = false;
    for (const row of list.children) {
      if (!row.dataset.key || row.classList.contains("is-folding") || row.classList.contains("is-leaving")) continue;
      const height = row.getBoundingClientRect().height;
      if (height > 32 && Math.abs((listHeights.get(row.dataset.key) || 0) - height) > .5) {
        listHeights.set(row.dataset.key, height); changed = true;
      }
    }
    if (changed) { listGeometry = null; listMeasureRevision++; }
  }
  function listOffsets() {
    if (listGeometry) return listGeometry;
    const known = [...listHeights.values()];
    const estimate = known.length ? known.reduce((sum, height) => sum + height, 0) / known.length : 84;
    const offsets = [0], index = new Map();
    for (let i = 0; i < items.length; i++) {
      index.set(items[i].id, i); offsets.push(offsets[i] + (listHeights.get(items[i].id) || estimate));
    }
    listGeometry = { offsets, index }; return listGeometry;
  }
  function offsetIndex(offsets, value) {
    let low = 0, high = offsets.length - 1;
    while (low < high) { const mid = (low + high) >> 1; if (offsets[mid] <= value) low = mid + 1; else high = mid; }
    return Math.max(0, low - 1);
  }
  function renderList(force = false, viewportOnly = false) {
    const today = dayKey(new Date());
    if (!viewportOnly) {
      items.sort(compareConversations);
      const signature = JSON.stringify([items, selected, nameOrder, today, state.workspace?.timezone]);
      if (!force && signature === lastList) return;
      lastList = signature; listRenderRevision++; listGeometry = null;
    }
    const list = $("#chat-list"), scroller = $(".chat-list-scroll"), top = scroller.scrollTop;
    list.classList.toggle("windowed-list", state.chatProtocol >= 2);
    if (viewportOnly && list.querySelector(".is-swiping,.is-settling,.is-leaving,.is-folding")) return;
    if (scroller.clientWidth !== listViewportWidth) { listViewportWidth = scroller.clientWidth; listHeights.clear(); listGeometry = null; }
    const bounds = scroller.getBoundingClientRect();
    const anchor = [...list.children].find(row => row.dataset.key && row.getBoundingClientRect().bottom > bounds.top);
    const position = anchor ? { id: anchor.dataset.key, top: anchor.getBoundingClientRect().top - bounds.top } : null;
    const virtual = state.chatProtocol >= 2 && items.length > 240 && scroller.clientHeight > 0 && scroller.clientWidth > 0;
    let start = 0, end = items.length;
    if (virtual) {
      const geometry = listOffsets(), overscan = Math.max(600, scroller.clientHeight * 2);
      const index = !viewportOnly && position && top > 0 ? geometry.index.get(position.id) : undefined;
      const origin = list.getBoundingClientRect().top - bounds.top + top;
      const target = index !== undefined ? Math.max(0, origin + geometry.offsets[index] - position.top) : top;
      start = offsetIndex(geometry.offsets, Math.max(0, target - origin - overscan));
      end = Math.min(items.length, offsetIndex(geometry.offsets, target - origin + scroller.clientHeight + overscan) + 2);
    }
    const viewportKey = () => `${listRenderRevision}/${start}/${end}/${scroller.clientWidth}/${scroller.clientHeight}/${listMeasureRevision}`;
    if (viewportOnly && lastViewportKey === viewportKey()) return;
    const existing = new Map([...list.children].filter(row => row.dataset.key).map(row => [row.dataset.key, row]));
    const wanted = [];
    for (const item of items.slice(start, end)) {
      const key = JSON.stringify([item, item.id === selected, today, nameOrder, state.workspace?.timezone]);
      const previous = existing.get(item.id);
      if (previous?.listKey === key) { wanted.push(previous); continue; }
      const row = node("div", undefined, `row chat-row${item.unread ? " unread waiting" : ""}${item.id === selected ? " selected" : ""}`); row.dataset.id = item.id; row.setAttribute("aria-current", String(item.id === selected));
      const names = displayNames(item);
      const avatar = button("", `contact-avatar ava ${item.status}${item.unread ? " pulsing" : ""} status-${item.status}`, () => openCards(item));
      avatar.title = item.contact_id ? `Открыть карточку АльфаCRM: ${item.name}` : "Связать диалог с контактом АльфаCRM"; avatar.setAttribute("aria-label", avatar.title);
      const letter = names.primary.trim().split(/\s+/).slice(0, 2).map(part => part.slice(0, 1)).join("").toLocaleUpperCase("ru");
      avatar.append(node("span", letter, "avatar-letter"), node("span", item.contact_id ? "↗" : "+", "avatar-open"));
      if (item.unread_count > 0) {
        const count = node("span", String(Math.min(item.unread_count, 99)), "avatar-count ava-n");
        count.setAttribute("aria-hidden", "true"); avatar.append(count);
        avatar.setAttribute("aria-label", `${avatar.title}. Сообщений без ответа: ${item.unread_count}`);
      }
      const body = button("", "chat-select row-main", () => select(item.id)); body.setAttribute("aria-label", `Открыть переписку: ${item.name}`); body.setAttribute("aria-pressed", String(item.id === selected));
      const heading = node("span", undefined, "row-top"), timestamp = node("time", listTime(item), "row-time");
      if (item.last_message_at) { timestamp.dateTime = item.last_message_at; timestamp.title = date(item.last_message_at); }
      if (item.unread && item.waiting_since) timestamp.title += " · Ожидает ответа с " + date(item.waiting_since);
      const name = node("strong", names.primary, "row-name"); name.title = names.primary;
      heading.append(name, timestamp);
      body.append(heading);
      const secondary = node("span", names.secondary, "row-kids contact-students"); secondary.title = names.secondary; body.append(secondary);
      const bottom = node("span", undefined, "row-bot");
      bottom.append(node("span", item.preview || "Переписка ещё не началась", "row-prev"));
      if (item.last_direction === "out" && !item.unread && item.last_state) { const tick = node("span", undefined, "tick" + (item.last_state === "read" ? " read" : "")); tick.title = sendLabels[item.last_state] || ""; tick.append(icon(["read", "delivered"].includes(item.last_state) ? "checks" : ["queued", "sending", "unknown"].includes(item.last_state) ? "clock" : ["failed", "cancelled"].includes(item.last_state) ? "warn" : "check")); bottom.append(tick); }
      body.append(bottom);
      const meta = node("span", undefined, "contact-meta");
      if (channels.length > 1) meta.append(node("small", item.channel_name, "muted"));
      if (meta.childNodes.length) body.append(meta);
      row.append(avatar, body); const wrap = node("div", undefined, "row-wrap");
      // Behind the row: a blue panel that grows with the swipe. The check sits in a
      // ring that fills as the swipe nears the threshold, then pops.
      const action = node("div", undefined, "row-action"), badge = node("span", undefined, "row-action-badge");
      badge.append(icon("check")); action.append(node("span", undefined, "row-action-inner"));
      action.firstChild.append(badge, node("span", "Обработано", "row-action-label"));
      wrap.dataset.key = item.id; wrap.listKey = key;
      wrap.append(action, row); wanted.push(wrap); wireSwipe(row, item);
    }
    list.setAttribute("role", "list");
    for (let i = 0; i < wanted.length; i++) {
      wanted[i].setAttribute("role", "listitem"); wanted[i].setAttribute("aria-posinset", String(start + i + 1));
      wanted[i].setAttribute("aria-setsize", String(items.length));
    }
    const setSpacers = () => {
      const offsets = listOffsets().offsets;
      listTopSpacer.style.height = `${offsets[start]}px`;
      listBottomSpacer.style.height = `${offsets[items.length] - offsets[end]}px`;
    };
    if (virtual) {
      if (!listTopSpacer) {
        listTopSpacer = node("div", undefined, "chat-list-spacer"); listBottomSpacer = node("div", undefined, "chat-list-spacer");
        listTopSpacer.setAttribute("aria-hidden", "true"); listBottomSpacer.setAttribute("aria-hidden", "true");
      }
      setSpacers(); wanted.unshift(listTopSpacer); wanted.push(listBottomSpacer);
    }
    patchChildren(list, wanted);
    if (state.chatProtocol >= 2 && scroller.clientWidth > 0) { measureListRows(list); if (virtual) setSpacers(); }
    const restored = position && wanted.find(row => row.dataset.key === position.id);
    scroller.scrollTop = restored && top > 0 ? top + restored.getBoundingClientRect().top - bounds.top - position.top : top;
    lastViewportKey = viewportKey();
  }
  function listTime(item) {
    if (!item.last_message_at) return "";
    const at = new Date(item.last_message_at), today = new Date();
    if (Number.isNaN(at.getTime())) return "";
    if (dayKey(at) === dayKey(today)) return time(at);
    if (dayKey(at) === dayKey(new Date(today.getTime() - 86400000))) return "вчера";
    if (today - at < 6 * 86400000) return new Intl.DateTimeFormat("ru-RU", { timeZone: state.workspace?.timezone, weekday: "short" }).format(at);
    return new Intl.DateTimeFormat("ru-RU", { timeZone: state.workspace?.timezone, day: "2-digit", month: "2-digit", ...(at.getFullYear() === today.getFullYear() ? {} : { year: "numeric" }) }).format(at);
  }
  function wireSwipe(row, item) {
    let start = null, distance = 0, swiping = false, suppressClick = false;
    const checkpoint = item.seen_checkpoint;
    const THRESHOLD = 76;
    const wrap = () => row.parentElement;
    const paint = offset => {
      const shown = -offset, progress = Math.min(1, shown / THRESHOLD);
      row.style.transform = offset ? `translateX(${offset}px)` : "";
      wrap()?.style.setProperty("--swipe", `${shown}px`);
      wrap()?.style.setProperty("--swipe-p", progress.toFixed(3));
      // The word appears only once the panel is wide enough to hold it.
      wrap()?.style.setProperty("--swipe-l", Math.max(0, Math.min(1, (shown - 88) / 22)).toFixed(3));
      const armed = shown >= THRESHOLD;
      if (armed && !wrap()?.classList.contains("is-armed")) navigator.vibrate?.(10);
      wrap()?.classList.toggle("is-armed", armed);
    };
    const reset = (animate = false) => {
      const pointerId = start?.id, target = wrap();
      start = null; distance = 0; swiping = false;
      if (animate && target) {
        // Spring back: the panel shrinks behind the row as it returns.
        target.classList.add("is-settling"); target.classList.remove("is-swiping", "is-armed"); paint(0);
        setTimeout(() => { target.classList.remove("is-settling"); }, 320);
      } else { paint(0); target?.classList.remove("is-swiping", "is-armed"); }
      if (pointerId !== undefined && row.hasPointerCapture?.(pointerId)) row.releasePointerCapture(pointerId);
    };
    row.addEventListener("pointerdown", event => {
      if (start || event.isPrimary === false || event.pointerType === "mouse" || !item.unread || state.workspace?.role === "observer") return;
      suppressClick = false;
      start = { x: event.clientX, y: event.clientY, id: event.pointerId };
    });
    row.addEventListener("pointermove", event => {
      if (!start || event.pointerId !== start.id) return;
      const x = event.clientX - start.x, y = event.clientY - start.y;
      if (!swiping) {
        // Commit to vertical scrolling before exposing the handled action.
        if (Math.abs(y) > 8 && Math.abs(y) >= Math.abs(x)) { reset(); return; }
        if (x >= -8 || Math.abs(x) <= Math.abs(y)) return;
        swiping = true; suppressClick = true;
        row.parentElement?.classList.add("is-swiping");
        if (event.pointerId !== undefined) row.setPointerCapture?.(event.pointerId);
      }
      distance = Math.min(0, x);
      // Free up to 140px, then resist like a rubber band.
      const pull = -distance, eased = pull <= 140 ? pull : 140 + (pull - 140) * 0.3;
      paint(-Math.min(eased, row.clientWidth * 0.8));
    });
    const end = async event => {
      if (!start || event.pointerId !== start.id) return;
      const handled = swiping && distance <= -THRESHOLD && !actionBusy;
      if (!handled) { reset(swiping); return; }
      const target = wrap(), pointerId = start.id;
      start = null; swiping = false;
      if (row.hasPointerCapture?.(pointerId)) row.releasePointerCapture(pointerId);
      // Fly off to the left while the panel fills the row, then fold the row away.
      target.classList.add("is-leaving");
      paint(-row.clientWidth);
      const leave = new Promise(resolve => setTimeout(() => {
        target.style.height = `${target.offsetHeight}px`; void target.offsetHeight;
        target.classList.add("is-folding"); target.style.height = "0px";
        setTimeout(resolve, 200);
      }, 260));
      const epoch = state.epoch, gen = generation;
      actionBusy = true;
      try {
        const result = await api(`${chatPath(item.id)}/handled`, { method: "POST", data: { seen_checkpoint: checkpoint } });
        await leave;
        if (!valid(epoch, gen)) return;
        if (result.conversation) mergeMeta(result.conversation);
        renderList(true);
        // Still listed (e.g. «Все»): it comes back quietly, without the badge.
        const back = [...$("#chat-list").children].find(node => node.dataset.key === item.id);
        if (back && back !== target) { back.classList.add("is-returning"); setTimeout(() => back.classList.remove("is-returning"), 420); }
        await loadList(false, true, true);
        if (selected === item.id) await loadHistory(item.id);
      } catch (error) {
        await leave;
        // Not handled after all: unfold the row back into place.
        target.classList.remove("is-leaving", "is-folding", "is-armed", "is-swiping"); target.style.height = ""; paint(0);
        if (valid(epoch, gen)) fail(error);
      }
      finally { if (valid(epoch, gen)) { actionBusy = false; renderHeader(); } }
    };
    row.addEventListener("pointerup", end);
    row.addEventListener("pointercancel", event => { if (start && event.pointerId === start.id) { suppressClick = false; reset(swiping); } });
    row.addEventListener("lostpointercapture", event => {
      // A button's implicit touch capture is transferred to this row mid-swipe.
      if (event.target === row && start && event.pointerId === start.id) reset();
    });
    row.addEventListener("click", event => {
      if (suppressClick) { suppressClick = false; event.preventDefault(); event.stopImmediatePropagation(); }
    }, true);
  }
  let dateFormats, dateTimezone;
  function chatDateFormats() {
    const timeZone = state.workspace?.timezone;
    if (!dateFormats || dateTimezone !== timeZone) {
      dateTimezone = timeZone;
      dateFormats = {
        time: new Intl.DateTimeFormat("ru-RU", { timeZone, hour: "2-digit", minute: "2-digit" }),
        day: new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }),
        short: new Intl.DateTimeFormat("en-GB", { timeZone, day: "2-digit", month: "2-digit" }),
        long: new Intl.DateTimeFormat("ru-RU", { timeZone, day: "numeric", month: "long", year: "numeric" })
      };
    }
    return dateFormats;
  }
  function time(value) {
    const d = new Date(value); return Number.isNaN(d.getTime()) ? "" : chatDateFormats().time.format(d);
  }
  const dayKey = value => {
    const d = new Date(value); if (Number.isNaN(d.getTime())) return "";
    return chatDateFormats().day.format(d);
  };
  const shortDate = value => {
    const d = new Date(value); if (Number.isNaN(d.getTime())) return "";
    return chatDateFormats().short.format(d).replace("/", ".");
  };
  function currentMeta(next, previous) {
    if (!previous) return true;
    if (next.queue_seq != null && previous.queue_seq != null) {
      const left = BigInt(next.queue_seq), right = BigInt(previous.queue_seq);
      if (left !== right) return left > right;
    }
    return next.revision === undefined || previous.revision === undefined || next.revision >= previous.revision;
  }
  function mergeMeta(meta) {
    const item = items.find(item => item.id === meta.id); if (item && currentMeta(meta, item)) Object.assign(item, meta);
  }
  function updateListLoader({ loading = false, failed = false } = {}) {
    show($("#chat-more"), hasMore || loading || failed);
    $("#chat-load-label").textContent = loading ? "Загружаем диалоги…" : failed ? "Не удалось загрузить список." : "";
    show($("#chat-load-label"), loading || failed);
    show($("#chat-retry"), failed);
  }
  function maybeLoadMore() {
    const scroller = $(".chat-list-scroll");
    if (!active() || !hasMore || listLoading || listRetryRequired || !scroller) return;
    if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < Math.max(900, scroller.clientHeight * 2.5)) {
      loadList(true).catch(error => fail(error));
    }
  }
  function loadList(append = false, preservePages = false, refresh = false) {
    if (listLoading) {
      const pending = listTask || Promise.resolve(), epoch = state.epoch, gen = generation;
      // A mutation needs a snapshot taken after its commit, even when an older
      // append/poll was already in flight. Ordinary polling shares that request.
      return refresh ? pending.catch(() => {}).then(() => valid(epoch, gen) && active() ? loadList(append, preservePages, true) : undefined) : pending;
    }
    const request = loadListPage(append, preservePages);
    listTask = request;
    request.finally(() => { if (listTask === request) listTask = null; }).catch(() => {});
    return request;
  }
  async function loadListPage(append = false, preservePages = false, prepared = null, preparedHistory = null, deferHistory = false) {
    if (append && (listRetryRequired || !hasMore)) return;
    const epoch = state.epoch, gen = generation, seq = ++listSeq;
    const targetPage = append ? page + 1 : preservePages ? page : 0;
    // The server patches an exact ordered prefix. A moving dialog cannot
    // leave a hole between independently fetched offset pages.
    const params = new URLSearchParams({ through_page: targetPage, window: (targetPage + 1) * 90 });
    if ((append || preservePages) && listToken) params.set("window_token", listToken);
    for (const [key, id] of [["q", "chat-search"], ["branch", "chat-branch"], ["status", "chat-status"], ["connection", "chat-channel"]]) if ($(`#${id}`).value) params.set(key, $(`#${id}`).value);
    listLoading = true;
    if (append) updateListLoader({ loading: true });
    try {
      const payload = prepared || await api(`${chatPath()}?${params}`, { timeoutMs: 20000 });
      if (!valid(epoch, gen) || seq !== listSeq || !active()) return;
      if (payload.version) lastViewVersion = payload.version;
      if (payload.window_token) {
        listToken = payload.window_token;
        if (!payload.not_modified) {
          const known = new Map((payload.reset ? [] : items).map(item => [item.id, item]));
          for (const item of payload.conversations) {
            const old = known.get(item.id);
            known.set(item.id, old && !currentMeta(item, old) ? { ...item, ...old } : item);
          }
          items = payload.order.map(id => known.get(id));
          if (items.some(item => !item)) { listToken = null; throw new Error("Обновите список диалогов."); }
        }
      } else { items = payload.conversations; listToken = null; }
      page = payload.page;
      hasMore = payload.has_more;
      listRetryRequired = false;
      renderList(); show($("#chat-empty"), items.length === 0); updateListLoader();
      const selectedChannel = channels.find(channel => channel.id === $("#chat-channel").value);
      const nativeChannelSelected = selectedChannel?.kind === "alfa_max_browser";
      const nativeChannelAvailable = channels.some(channel => channel.kind === "alfa_max_browser");
      $("#chat-empty").textContent = !channels.length
        ? "Подключите MAX Bot API или MAX через AlfaCRM в разделе «Подключения»."
        : nativeChannelSelected || (!selectedChannel && nativeChannelAvailable)
          ? "В этом канале пока нет загруженных диалогов. Проверьте синхронизацию истории в разделе «Подключения»."
          : "Диалогов по этому запросу пока нет. Новые сообщения появятся после обращения клиента к боту.";
      $("#chat-visible-count").textContent = payload.total ? `${items.length} из ${payload.total}` : "";
      $("#chat-unread-count").textContent = String(payload.unread); show($("#chat-unread-count"), payload.unread > 0);
      if (!selected && items.length) {
        // Release scrolling/polling before a slow timeline or card finishes.
        listLoading = false;
        if (hasMore) requestAnimationFrame(maybeLoadMore);
        const first = (items.find(item => item.unread) || items[0]).id;
        await select(first, false, preparedHistory?.conversation?.id === first ? preparedHistory : null, deferHistory);
      }
      else if (selected && currentItem()) { renderHeader(); refreshIdentityDetails(); }
    } catch (error) {
      if (append && valid(epoch, gen) && seq === listSeq) { listRetryRequired = true; updateListLoader({ failed: true }); }
      throw error;
    } finally {
      if (seq === listSeq) { listLoading = false; if (hasMore && !listRetryRequired) requestAnimationFrame(maybeLoadMore); }
    }
  }
  function renderHeader() {
    const data = cache.get(selected), item = currentItem(); if (!data) return;
    const name = item?.name || data.meta?.peer_name || "Контакт MAX";
    const person = $("#chat-person"); person.replaceChildren(node("strong", name));
    const caption = [item?.students?.join(" · "), data.meta?.channel_name || "MAX"].filter(Boolean).join(" · ");
    person.append(node("small", caption));
    if ($("#chat-header-avatar")) { const avatar = $("#chat-header-avatar"); avatar.className = `ava ${item?.status || "me"}`; avatar.textContent = name.split(/\s+/).slice(0, 2).map(part => part[0]).join("").toLocaleUpperCase("ru"); }
    show($("#chat-handled"), Boolean(data.meta?.unread) && state.workspace?.role !== "observer");
    show($("#chat-details"), Boolean(data.meta));
    $("#chat-handled").disabled = actionBusy || bulkLoading;
    $("#chat-handled").setAttribute("aria-busy", String(bulkLoading || bulkBusy));
    const unavailable = !data.meta?.can_send || state.workspace?.role === "observer";
    // A lost send response must not lock a later dictated or typed answer.
    $("#chat-input").disabled = unavailable;
    if ($("#chat-templates")) $("#chat-templates").disabled = unavailable;
    const send = $("#chat-send");
    const draft = $("#chat-input").value.trim(), tooLong = draft.length > 4000;
    const retry = Boolean(data.pending) && !draft;
    send.disabled = unavailable || historyBusy || (Boolean(draft) ? tooLong : !retry);
    send.classList.toggle("is-retry", retry);
    send.setAttribute("aria-label", retry ? "Проверить отправку" : "Отправить");
    send.title = retry ? "Проверить отправку" : "Отправить";
    const hint = $("#chat-send-hint");
    hint.hidden = !tooLong;
    hint.textContent = tooLong ? "Сократите текст до 4000 символов перед отправкой." : "";
  }
  function compareMessages(a, b) {
    const value = message => BigInt(message.sort_time ?? Math.trunc(new Date(message.occurred_at).getTime()) * 1000);
    const order = message => /^[0-9]{1,19}$/.test(String(message.crm_order ?? "")) ? BigInt(message.crm_order) : 0n;
    const at = value(a), bt = value(b), ao = order(a), bo = order(b);
    return at < bt ? -1 : at > bt ? 1 : ao < bo ? -1 : ao > bo ? 1 : a.id.localeCompare(b.id);
  }
  function rememberScroll() {
    const stream = $("#chat-stream"), bounds = stream.getBoundingClientRect();
    const anchors = [];
    for (const bubble of $("#chat-history").children) {
      if (!bubble.dataset.id) continue;
      const rect = bubble.getBoundingClientRect();
      if (rect.bottom > bounds.top && rect.top < bounds.bottom) anchors.push({ id: bubble.dataset.id, top: rect.top - bounds.top });
      if (rect.top >= bounds.bottom && anchors.length) break;
    }
    return { anchors, top: stream.scrollTop, height: stream.scrollHeight,
      nearBottom: stream.scrollHeight - stream.scrollTop - stream.clientHeight < 100 };
  }
  function restoreScroll(position, { bottom = false, older = false, follow = false } = {}) {
    const stream = $("#chat-stream");
    if (bottom || (follow && position.nearBottom && !older)) { stream.scrollTop = stream.scrollHeight; return; }
    const bubbles = new Map([...$("#chat-history").children].filter(n => n.dataset.id).map(n => [n.dataset.id, n]));
    for (const anchor of position.anchors) {
      const bubble = bubbles.get(anchor.id);
      if (bubble) {
        stream.scrollTop += bubble.getBoundingClientRect().top - stream.getBoundingClientRect().top - anchor.top;
        return;
      }
    }
    stream.scrollTop = position.top + (older ? stream.scrollHeight - position.height : 0);
  }
  function renderHistoryControls(data, { preserve = true } = {}) {
    const position = preserve ? rememberScroll() : null;
    const more = $("#chat-history-more"), status = $("#chat-history-status");
    show(more, Boolean(data.more || data.olderError));
    more.disabled = Boolean(data.olderLoading);
    more.textContent = data.olderLoading ? "Загрузка истории…" : data.olderError ? "Повторить загрузку истории" : "Загрузить более ранние сообщения";
    show(status, Boolean(data.olderLoading || data.olderError));
    status.textContent = data.olderError || (data.olderLoading ? "Загружаем сообщения…" : "");
    $("#chat-stream").setAttribute("aria-busy", String(Boolean(data.olderLoading)));
    const total = Math.max(data.total || 0, data.messages.size);
    const count = `Показано сообщений: ${data.messages.size} из ${total}.`;
    const progress = data.meta?.history_loading ? " Загружаем более раннюю историю из AlfaCRM…" : "";
    $("#chat-history-note").textContent = count + progress;
    if (position) restoreScroll(position);
  }
  function maybeFillHistory() {
    const data = cache.get(selected), stream = $("#chat-stream");
    if (!active() || !data?.more || data.olderLoading || data.olderError || historyBusy || stream.hidden) return;
    // A short timeline has no scrollbar, so it cannot produce an upward scroll
    // event. Fill the visible area instead of stranding the reader on one row.
    if (stream.clientHeight > 0 && stream.scrollHeight <= stream.clientHeight + 1) loadOlder();
  }
  async function loadOlder({ retry = false } = {}) {
    const id = selected, data = cache.get(id);
    if (!active() || !data?.more || data.olderLoading || historyBusy || (data.olderError && !retry)) return;
    const epoch = state.epoch, gen = generation, choice = selectionSeq;
    data.olderLoading = true; data.olderError = ""; historyBusy = true;
    renderHistoryControls(data); renderHeader();
    try { await loadHistory(id, "older"); }
    catch (error) {
      if (valid(epoch, gen) && selected === id && choice === selectionSeq && !ignored(error)) data.olderError = error.message;
    } finally {
      data.olderLoading = false;
      if (valid(epoch, gen) && selected === id && choice === selectionSeq) {
        historyBusy = false; renderHistoryControls(data); renderHeader();
        if (!data.olderError) requestAnimationFrame(maybeFillHistory);
      }
    }
  }
  function renderMessages(id, { bottom = false, older = false } = {}) {
    const data = cache.get(id); if (id !== selected || !data) return;
    const history = $("#chat-history"), todayKey = dayKey(new Date());
    const renderKey = `${id}/${data.messageRevision}/${todayKey}/${state.workspace?.timezone}`;
    if (!bottom && !older && data.renderKey === renderKey && history.dataset.conversation === id) {
      renderHistoryControls(data); renderHeader(); return;
    }
    data.renderKey = renderKey;
    const stream = $("#chat-stream"), position = rememberScroll();
    renderHistoryControls(data, { preserve: false });
    const existing = new Map(history.dataset.conversation === id ? [...history.children].map(child => [child.dataset.key, child]) : []);
    const wanted = []; let day = "";
    const messages = [...data.messages.values()].sort(compareMessages);
    // Each message that went out answers only the nearest earlier failed copy of its text.
    const resentBy = new Map(), waiting = new Map();
    for (const m of messages) {
      if (m.direction !== "out" || !m.text || m.deleted) continue;
      if (m.state === "failed") { (waiting.get(m.text) || waiting.set(m.text, []).get(m.text)).push(m); continue; }
      if (!["accepted", "delivered", "read"].includes(m.state)) continue;
      const failed = waiting.get(m.text)?.pop(); if (failed) resentBy.set(failed.id, m);
    }
    for (let i = 0; i < messages.length; i++) {
      const message = messages[i];
      const messageDay = dayKey(message.occurred_at);
      if (messageDay !== day) {
        const key = `day:${messageDay}`, label = messageDay === todayKey ? "Сегодня" : chatDateFormats().long.format(new Date(message.occurred_at));
        const separator = existing.get(key) || node("div", label, "chat-day sep");
        separator.dataset.key = key;
        if (separator.textContent !== label) separator.textContent = label;
        wanted.push(separator); day = messageDay;
      }
      const next = messages[i + 1], previous = messages[i - 1];
      const grouped = Boolean(next && next.actor_type !== "automation" && next.direction === message.direction && next.actor_type === message.actor_type && dayKey(next.occurred_at) === messageDay);
      const first = !previous || previous.actor_type !== message.actor_type || previous.direction !== message.direction || dayKey(previous.occurred_at) !== messageDay;
      // A failed message whose text later went out shows «sent again» instead of the actions.
      const resent = resentBy.get(message.id) || null;
      const key = `message:${message.id}`, messageKey = JSON.stringify([message, todayKey, state.workspace?.timezone, resent?.id || null, Boolean(data.meta?.can_send)]);
      const retained = existing.get(key);
      if (retained?.messageKey === messageKey) {
        if (message.actor_type !== "automation") { retained.classList.toggle("first", first); retained.classList.toggle("last", !grouped); }
        wanted.push(retained); continue;
      }
      if (message.actor_type === "automation") {
        const bubble = node("div", undefined, "chat-bubble system sys"); bubble.dataset.id = message.id;
        bubble.dataset.key = key; bubble.messageKey = messageKey;
        const body = node("p", message.text || "", "system-body sys-body");
        body.hidden = !data.expanded.has(message.id);
        const glyph = node("span", body.hidden ? "+" : "−", "system-toggle-glyph glyph");
        const toggle = button("", "system-toggle sys-toggle", () => {
          const opening = body.hidden;
          body.hidden = !opening;
          if (opening) data.expanded.add(message.id); else data.expanded.delete(message.id);
          glyph.textContent = opening ? "−" : "+";
          toggle.setAttribute("aria-expanded", String(opening));
        });
        toggle.append(glyph);
        toggle.type = "button"; toggle.setAttribute("aria-expanded", String(!body.hidden));
        const when = (dayKey(message.occurred_at) !== todayKey ? shortDate(message.occurred_at) + " " : "") + time(message.occurred_at);
        const row = node("div", undefined, "system-row sys-row");
        const right = node("span", undefined, "system-right sys-right");
        right.append(node("time", when, "system-time sys-time"), toggle);
        const label = node("span", undefined, "system-label sys-label");
        label.title = `Системное сообщение: ${message.template_name || "Системное сообщение"}`;
        label.append(icon("cog"), node("b", message.template_name || "Системное сообщение")); row.append(label, right);
        toggle.addEventListener("click", event => event.stopPropagation());
        bubble.addEventListener("click", () => { if (!String(window.getSelection?.() || "")) toggle.click(); });
        bubble.append(row, body);
        wanted.push(bubble);
        continue;
      }
      const bubble = node("div", undefined, `chat-bubble msg ${message.direction === "out" ? "out outgoing" : "in incoming"}${first ? " first" : ""}${grouped ? "" : " last"}${message.state === "failed" ? " failed" : ""}`); bubble.dataset.id = message.id;
      bubble.dataset.key = key; bubble.messageKey = messageKey;
      const content = node("div", undefined, "bub");
      if (message.forwarded) content.append(node("small", "Пересланное сообщение", "chat-forwarded"));
      content.append(node("span", message.deleted ? "Сообщение удалено" : message.text || (message.forwarded ? "Пересланное содержимое пока недоступно" : message.attachments.length ? "" : "Содержимое сообщения недоступно"), message.deleted ? "muted msg-text" : "msg-text"));
      if (!message.deleted) for (const [index, type] of message.attachments.entries()) {
        const names = { image: "Изображение", video: "Видео", audio: "Аудио", file: "Файл", contact: "Контакт", location: "Геопозиция", sticker: "Стикер" };
        const attachment = node("span", undefined, "att chat-attachment"); attachment.append(icon("file"), node("span", `${message.attachment_names?.[index] || names[type] || "Вложение"} · открыть в MAX`)); content.append(attachment);
      }
      const meta = node("span", undefined, "meta");
      if (message.edited && !message.deleted) meta.append(node("span", "изменено", "ed"));
      meta.append(node("time", time(message.occurred_at)));
      if (message.direction === "out") {
        const status = node("span", undefined, `send-state state-${message.state}`); status.title = sendLabels[message.state] || "";
        status.append(icon(["delivered", "read"].includes(message.state) ? "checks" : ["queued", "sending", "unknown"].includes(message.state) ? "clock" : ["failed", "cancelled"].includes(message.state) ? "warn" : "check"), node("span", sendLabels[message.state] || "", "sr")); meta.append(status);
      }
      content.append(meta); bubble.append(content);
      // A definite failure (nothing reached the client): one quiet line «Не отправлено · Повторить · Изменить».
      const failedOut = message.direction === "out" && ["failed", "cancelled"].includes(message.state) && !message.deleted;
      if (!(failedOut && resent)) {
        if (failedOut) bubble.append(node("small", "Сообщение не отправлено", "chat-message-error"));
        if (failedOut && message.state === "failed" && message.text && data.meta?.can_send && !recipientErrors.has(message.error_code)) {
          const actions = node("span", undefined, "chat-message-actions");
          const again = node("button", undefined, "chat-retry-send"); again.type = "button"; again.append(icon("retry"), node("span", "Повторить"));
          again.addEventListener("click", () => resendFailed(id, message.text, false));
          const edit = node("button", undefined, "chat-retry-edit"); edit.type = "button"; edit.append(icon("edit"), node("span", "Изменить"));
          edit.addEventListener("click", () => resendFailed(id, message.text, true));
          actions.append(again, edit); bubble.append(actions);
        }
      }
      wanted.push(bubble);
    }
    patchChildren(history, wanted); history.dataset.conversation = id;
    show(stream); show($("#chat-compose"));
    resizeCompose();
    restoreScroll(position, { bottom, older, follow: true });
    renderHeader();
    requestAnimationFrame(maybeFillHistory);
  }
  function reconcilePending(data) {
    if (!data.pending) return;
    const canonical = [...data.messages.values()].find(m => m.request_id === data.pending.request_id);
    // The composer may already hold a new voice transcription. Only the old
    // request is resolved by this history row; the new draft belongs to the user.
    if (canonical) { data.pending = null; if (cache.get(selected) === data) renderHeader(); }
  }
  async function loadHistory(id, mode = "delta") {
    const epoch = state.epoch, gen = generation, choice = selectionSeq, data = entry(id);
    // Polling, scroll and send confirmation share one per-chat request queue.
    // Build each cursor only after the previous response was merged.
    const request = (data.loading || Promise.resolve()).catch(() => {}).then(() => {
      if (!valid(epoch, gen) || choice !== selectionSeq || selected !== id || !active()) return;
      return loadHistoryPage(id, mode);
    });
    data.loading = request;
    try { await request; }
    finally { if (data.loading === request) data.loading = null; }
  }
  async function loadHistoryPage(id, mode = "delta") {
    const epoch = state.epoch, gen = generation, choice = selectionSeq, data = entry(id);
    if (mode === "delta" && !data.loaded) mode = "initial";
    const params = new URLSearchParams();
    if (state.chatProtocol >= 2) params.set("limit", "40");
    if (mode === "delta" && data.loaded) params.set("after_seq", data.next);
    if (mode === "older") {
      // Live updates may include an ancient edited/backfilled row beyond an
      // unloaded interval. Only page responses advance the contiguous boundary.
      const first = data.oldest;
      if (!first || !data.more) return;
      params.set("before_time", first.occurred_at); params.set("before_id", first.id);
    }
    const controller = new AbortController(); data.controller = controller;
    let result;
    try { result = await api(`${chatPath(id)}/messages?${params}`, { signal: controller.signal, timeoutMs: 20000 }); }
    finally { if (data.controller === controller) data.controller = null; }
    if (!valid(epoch, gen) || cache.get(id) !== data || choice !== selectionSeq || selected !== id) return;
    await mergeHistory(id, result, mode);
  }
  async function mergeHistory(id, result, mode) {
    const epoch = state.epoch, gen = generation, choice = selectionSeq, data = entry(id);
    if (result.conversation.id !== id) {
      const canonical = entry(result.conversation.id);
      if (data.draft && canonical.draft && canonical.draft !== data.draft) {
        canonical.draft += `\n\n${data.draft}`;
        toast("Черновики объединённых диалогов сохранены в поле ответа.");
      } else if (!canonical.draft) canonical.draft = data.draft;
      if (!canonical.pending) canonical.pending = data.pending;
      data.draft = ""; data.pending = null;
      // select() saves the old input, so clear it only after transferring it.
      $("#chat-input").value = "";
      const reopenDetails = detailsDialog.open;
      await select(result.conversation.id, false);
      if (reopenDetails && valid(epoch, gen) && selected === result.conversation.id && !detailsDialog.open) await openDetails({ auto: true });
      return;
    }
    let changed = Boolean(result.reset);
    if (result.reset) { data.messages.clear(); data.deferredRemovals = null; data.loaded = false; data.oldest = null; }
    for (const message of result.messages) {
      data.deferredRemovals?.delete(message.id);
      const old = data.messages.get(message.id);
      if ((!old || old.version <= message.version) && JSON.stringify(old) !== JSON.stringify(message)) {
        data.messages.set(message.id, message); changed = true;
      }
    }
    if (mode === "delta" && result.more_changes) {
      // The replacement for a superseded local send may be in the next batch.
      // Keep its bubble until the whole change sequence has been read.
      if (!data.deferredRemovals) data.deferredRemovals = new Set();
      for (const removed of result.removed_ids || []) data.deferredRemovals.add(removed);
    } else {
      for (const removed of result.removed_ids || []) {
        if (data.deferredRemovals) data.deferredRemovals.add(removed);
        else changed = data.messages.delete(removed) || changed;
      }
      if (mode !== "older" && data.deferredRemovals) {
        for (const removed of data.deferredRemovals) changed = data.messages.delete(removed) || changed;
        data.deferredRemovals = null;
      }
    }
    if (changed) data.messageRevision += 1;
    data.total = result.total ?? data.messages.size;
    // Earlier pages must not advance the live cursor past unseen intervening updates.
    if (mode !== "older" || result.reset) data.next = result.reset ? result.next_seq : Math.max(data.next, result.next_seq);
    if (!data.loaded || mode === "older") {
      data.more = result.has_more;
      const first = [...result.messages].sort(compareMessages)[0];
      if (first) data.oldest = { id: first.id, occurred_at: first.occurred_at };
    }
    data.loaded = true;
    if (currentMeta(result.conversation, data.meta)) {
      const meta = { ...result.conversation };
      // An archive page or a partial change batch has not displayed the latest
      // incoming yet. Keep the last complete live snapshot as the seen boundary.
      if ((mode === "older" && !result.reset) || result.more_changes) {
        meta.seen_checkpoint = data.meta?.seen_checkpoint;
        meta.inbound_seq = data.meta?.inbound_seq;
      }
      data.meta = meta; mergeMeta(meta);
    }
    reconcilePending(data);
    renderMessages(id, { bottom: mode === "initial" || result.reset, older: mode === "older" && !result.reset }); renderList();
    refreshIdentityDetails();
    if (result.more_changes && mode !== "older") await loadHistoryPage(id);
  }
  async function select(id, mobile = true, preparedHistory = null, deferHistory = false) {
    cache.get(selected)?.controller?.abort(); historyPollError = null;
    previousHistoryTop = 0;
    if (id !== selected) document.dispatchEvent(new CustomEvent("cm:chat-selected"));
    const keepDetails = id !== selected && pinnedDocked();
    if (id !== selected && detailsDialog.open && !keepDetails) detailsDialog.close();
    if (keepDetails && detailsDialog.open) familyPending(currentItemFor(id)?.name);
    if (selected && cache.has(selected) && !cache.get(selected).pending) cache.get(selected).draft = $("#chat-input").value;
    selectionSeq += 1; selected = id; const data = entry(id); historyBusy = false; data.pollAfter = 0;
    if (mobile) $("#chat-layout").classList.add("has-selection");
    $("#chat-input").value = data.draft; resizeCompose();
    renderList(); renderPollStatus();
    if (data.loaded) renderMessages(id, { bottom: true });
    else {
      show($("#chat-stream")); show($("#chat-compose"), false);
      $("#chat-history").replaceChildren(node("p", "Открываем переписку…", "empty"));
      $("#chat-person").replaceChildren(node("strong", currentItem()?.name || "MAX"));
    }
    if (keepDetails && selected === id && active()) openDetails({ auto: true }).catch(error => fail(error));
    if (deferHistory && !preparedHistory && !data.loaded) return;
    try {
      if (preparedHistory) await mergeHistory(id, preparedHistory, "initial"); else await loadHistory(id, data.loaded ? "delta" : "initial");
      if (selected === id && active()) { historyPollError = null; renderPollStatus(); }
    } catch (error) { if (selected === id && active() && !ignored(error)) {
      historyPollError = error; data.pollAfter = Date.now() + 8000; renderPollStatus();
    } }
  }
  async function enter() {
    const epoch = state.epoch, gen = generation;
    const openingListSeq = listSeq;
    const previousSelection = selected;
    clearTimeout(timer); notice("", $("#chat-error"));
    const initial = state.chatProtocol >= 2 && !selected && !items.length;
    if (initial) { bootstrapBusy = true; show($("#chat-empty"), false); }
    try {
      const preferenceRevision = state.chatPreferenceRevision || 0;
      let prepared = null;
      if (initial) {
        const params = new URLSearchParams();
        for (const [key, id] of [["q", "chat-search"], ["branch", "chat-branch"], ["status", "chat-status"], ["connection", "chat-channel"]]) if ($(`#${id}`).value) params.set(key, $(`#${id}`).value);
        try { prepared = await api(`${chatPath()}/bootstrap?${params}`, { timeoutMs: 20000 }); }
        catch (error) {
          // Keep the independent read endpoints as a bounded startup fallback.
          // Authorization failures must never be retried via another route.
          if (![404, 405, 502, 503, 504].includes(error.status) && !["TimeoutError", "NetworkError"].includes(error.name)) throw error;
          if (!valid(epoch, gen) || !active()) return;
        }
      }
      const context = prepared?.context || await api(`${chatPath()}/context`, { timeoutMs: 20000 });
      if (!valid(epoch, gen) || !active()) return;
      if (preferenceRevision === (state.chatPreferenceRevision || 0) && !preferenceBusy) applyPreferences(context.preferences);
      state.voiceInput = Boolean(context.voice_input);
      state.voiceMaxSeconds = context.voice_max_seconds || 180;
      document.dispatchEvent(new CustomEvent("cm:voice-availability"));
      channels = context.channels || [];
      const channel = $("#chat-channel"), chosen = channel.value;
      channel.replaceChildren(new Option("Все каналы MAX", ""), ...channels.map(c => new Option(c.name, c.id)));
      if (channels.some(c => c.id === chosen)) channel.value = chosen;
      const old = $("#chat-branch").value;
      const branches = context.branches || [];
      $("#chat-branch").replaceChildren(new Option("Все филиалы", ""), ...branches.map(b => new Option(b.name, b.id)));
      if (branches.some(b => String(b.id) === old)) $("#chat-branch").value = old;
      renderFilterChip();
      if (prepared) { if (listSeq === openingListSeq) await loadListPage(false, false, prepared.listing, prepared.history, prepared.history_pending === true); }
      else await loadList();
      if (selected && !currentItem()) await select(selected, false);
      else if (previousSelection && selected === previousSelection) await loadHistory(selected);
      if (selected && family && detailsDialog.open) watchFamily(selected, family.source_contact_id || family.id, epoch, gen, detailsSeq);
    } catch (error) { if (valid(epoch, gen)) fail(error); }
    finally { if (valid(epoch, gen)) {
      bootstrapBusy = false;
      schedule(streamHealthy && (!streamVersion || !lastViewVersion || streamVersion !== lastViewVersion) ? 100 : 2500);
    } }
  }
  function schedule(delay = 2500) {
    clearTimeout(timer); if (!active()) return;
    const data = cache.get(selected);
    const checkingSend = data?.pending || [...(data?.messages.values() || [])].some(message => ["queued", "sending", "unknown"].includes(message.state));
    if (streamHealthy && delay === 2500 && !checkingSend && !listPollError && !historyPollError && (!selected || data?.loaded)) delay = 45000;
    timer = setTimeout(poll, delay);
  }
  async function recoverPending(id, data) {
    // This repeats only intake of the SAME command UUID, never the browser POST.
    // Read history first: the server may already have committed the command.
    if (!data.pending || data.recovering || historyBusy || !data.meta?.can_send || Date.now() < (data.pendingAfter || 0)) return;
    const payload = data.pending, epoch = state.epoch, gen = generation;
    data.recovering = true;
    try {
      await api(`${chatPath(id)}/messages`, { method: "POST", data: payload, timeoutMs: 20000 });
      if (valid(epoch, gen) && data.pending === payload) {
        data.pending = null; data.pendingRetries = 0;
        if (selected === id) { renderHeader(); await loadHistory(id); }
      }
    } catch (error) {
      if (!valid(epoch, gen) || ignored(error) || data.pending !== payload) return;
      if (error.status >= 400 && error.status < 500 && !transient(error)) {
        data.pending = null;
        data.draft = [payload.text, data.draft].filter(Boolean).join("\n\n");
        if (selected === id) {
          $("#chat-input").value = data.draft; resizeCompose(); renderHeader();
          fail(new Error("Сообщение не отправлено"));
        }
      } else {
        data.pendingRetries = (data.pendingRetries || 0) + 1;
        data.pendingAfter = Date.now() + Math.min(60000, 2500 * 2 ** Math.min(data.pendingRetries, 5));
      }
    } finally { data.recovering = false; }
  }
  function renderPollStatus() {
    const error = listPollError || historyPollError;
    $("#chat-sync-state").textContent = error ? "Ожидаем соединение…" : selected && !cache.get(selected)?.loaded ? "Загружаем историю…" : "Обновлено · история на сервере";
    if (error) { if (transient(error)) notice("", $("#chat-error")); else fail(error); schedule(2500); } else notice("", $("#chat-error"));
  }
  async function poll() {
    if (!active()) return;
    if (document.hidden || bootstrapBusy) return schedule(5000);
    const epoch = state.epoch, gen = generation;
    const tasks = [], now = Date.now();
    // Tick independently of request completion. A slow list must not hold the
    // open conversation; each feed keeps at most one poll in flight and backs off separately.
    schedule();
    if (!pollBusy && now >= listPollAfter) {
      pollBusy = true;
      const request = loadList(false, true), seq = listSeq;
      tasks.push(request.then(() => {
        if (valid(epoch, gen) && active() && seq === listSeq) { listPollError = null; listPollAfter = 0; renderPollStatus(); }
      }).catch(error => {
        if (valid(epoch, gen) && active() && seq === listSeq && !ignored(error)) {
          listPollError = error; listPollAfter = Date.now() + 8000; renderPollStatus();
        }
      }).finally(() => { if (valid(epoch, gen)) pollBusy = false; }));
    }
    const id = selected, data = cache.get(id), choice = selectionSeq;
    if (id && data && !data.polling && now >= (data.pollAfter || 0)) {
      data.polling = true;
      tasks.push(loadHistory(id).then(async () => {
        if (valid(epoch, gen) && active() && choice === selectionSeq) {
          historyPollError = null; data.pollAfter = 0; renderPollStatus();
          await recoverPending(id, data);
        }
      }).catch(error => {
        if (valid(epoch, gen) && active() && choice === selectionSeq && !ignored(error)) {
          historyPollError = error; data.pollAfter = Date.now() + 8000; renderPollStatus();
        }
      }).finally(() => { data.polling = false; }));
    }
    await Promise.all(tasks);
  }
  // The failed text goes through the ordinary send path: a new request, the same checks.
  async function resendFailed(id, text, editFirst) {
    if (selected !== id || historyBusy) return;
    const input = $("#chat-input"), draft = input.value;
    if (editFirst) {
      input.value = draft.trim() ? `${draft}\n\n${text}` : text; resizeCompose(); renderHeader(); input.focus();
      return;
    }
    input.value = text; resizeCompose();
    await submit();
    if (draft.trim() && selected === id && !input.value.trim()) { input.value = draft; const data = cache.get(id); if (data) data.draft = draft; resizeCompose(); renderHeader(); }
  }
  async function submit() {
    const id = selected, data = cache.get(id); if (!data || historyBusy || !data.meta?.can_send) return;
    if (!data.pending && !$("#chat-input").value.trim()) return;
    if ($("#chat-input").value.trim().length > 4000) { renderHeader(); return; }
    const epoch = state.epoch, gen = generation, choice = selectionSeq;
    historyBusy = true; renderHeader();
    let payload = null, acknowledged = false;
    const refresh = async () => {
      try {
        await loadHistory(id);
        if (selected === id) renderMessages(id, { bottom: true });
        await loadList(false, true, true);
      } catch (error) {
        // The POST already committed. A failed read is not a failed send.
        if (valid(epoch, gen) && selected === id && !ignored(error)) {
          historyPollError = error; renderPollStatus();
        }
      }
    };
    try {
      if (data.pending) {
        payload = data.pending;
        await api(`${chatPath(id)}/messages`, { method: "POST", data: payload, timeoutMs: 20000 });
        if (!valid(epoch, gen)) return;
        acknowledged = true;
        if (data.pending === payload) data.pending = null;
        await refresh();
        if (selected !== id || choice !== selectionSeq) return;
      }
      const content = $("#chat-input").value.trim();
      if (!content || content.length > 4000) return;
      payload = { request_id: crypto.randomUUID(), text: content, seen_inbound_seq: data.meta.inbound_seq, seen_checkpoint: data.meta.seen_checkpoint };
      acknowledged = false;
      data.pending = payload; data.pendingAfter = 0; data.pendingRetries = 0; data.draft = "";
      $("#chat-input").value = ""; resizeCompose(); renderHeader();
      await api(`${chatPath(id)}/messages`, { method: "POST", data: payload, timeoutMs: 20000 });
      if (!valid(epoch, gen)) return;
      acknowledged = true;
      if (data.pending === payload) data.pending = null;
      await refresh();
    } catch (error) {
      if (!valid(epoch, gen)) return;
      // HTTP rejection is definite; loss of the HTTP response keeps the same UUID.
      if (!acknowledged && payload && error.status && error.status < 500 && !transient(error)) {
        if (data.pending === payload) data.pending = null;
        data.draft = [payload.text, data.draft].filter(Boolean).join("\n\n");
        if (selected === id) { $("#chat-input").value = data.draft; resizeCompose(); }
      }
      // A transport failure cannot prove that the message was not sent.
      // Keep its UUID and let history reconcile it; never show browser/server diagnostics here.
      if (selected === id) {
        if (!acknowledged && error.status >= 400 && error.status < 500 && !transient(error)) {
          fail(new Error("Сообщение не отправлено"));
        } else {
          notice("", $("#chat-error"));
          historyPollError = error; schedule(2500);
        }
      }
      if (!acknowledged && error.status === 409) { try { await loadHistory(id); } catch (_) { /* Preserve the draft and error. */ } }
    } finally { if (valid(epoch, gen) && selected === id && choice === selectionSeq) { historyBusy = false; renderHeader(); } }
  }
  let mirrorNode = null;
  function composeMirror(input) {
    const style = getComputedStyle(input);
    if (!mirrorNode) {
      mirrorNode = document.createElement("div"); mirrorNode.setAttribute("aria-hidden", "true");
      Object.assign(mirrorNode.style, { position: "absolute", visibility: "hidden", left: "-9999px", top: "0",
        whiteSpace: "pre-wrap", overflowWrap: "break-word", wordBreak: "normal" });
      document.body.append(mirrorNode);
    }
    for (const key of ["fontFamily", "fontSize", "fontWeight", "fontStyle", "letterSpacing", "lineHeight", "textTransform",
      "paddingTop", "paddingBottom", "paddingLeft", "paddingRight", "borderTopWidth", "borderBottomWidth", "boxSizing", "tabSize"])
      mirrorNode.style[key] = style[key];
    mirrorNode.style.borderStyle = "solid"; mirrorNode.style.borderColor = "transparent";
    mirrorNode.style.borderLeftWidth = style.borderLeftWidth; mirrorNode.style.borderRightWidth = style.borderRightWidth;
    mirrorNode.style.width = `${input.getBoundingClientRect().width}px`;
    mirrorNode.textContent = `${input.value}\u200b`;
    return Math.ceil(mirrorNode.getBoundingClientRect().height);
  }
  function resizeCompose() {
    const input = $("#chat-input"); if (!input.clientWidth) return;
    const position = rememberScroll();
    // Height from a mirror of the text rather than scrollHeight alone: Safari on
    // iPhone reports a taller scrollHeight for an auto-sized textarea, which left
    // an empty band above the text. scrollHeight still wins if text would clip.
    const mirror = composeMirror(input);
    input.style.height = `${Math.max(mirror, 44)}px`;
    if (input.scrollHeight > input.clientHeight + 1) input.style.height = `${Math.max(input.scrollHeight, 44)}px`;
    restoreScroll(position, { follow: true });
    const canvas = input.closest(".canvas");
    if (canvas && document.activeElement === input && input.selectionEnd === input.value.length)
      canvas.scrollTop = canvas.scrollHeight;
  }
  if (typeof ResizeObserver !== "undefined") {
    let width = 0;
    new ResizeObserver(([entry]) => {
      if (width !== entry.contentRect.width) {
        width = entry.contentRect.width; if (width > 0) resizeCompose();
      }
    // Watch the text area itself: it narrows when «Очистить» appears beside it
    // (the field around it does not change), and the text must re-wrap in full.
    }).observe($("#chat-input"));
    // When the composer grows or shrinks (a new line, the ✕ appearing), the newest
    // message must stay in view above it instead of sliding underneath.
    const stream = $("#chat-stream");
    let pinned = true, composeHeight = 0;
    stream.addEventListener("scroll", () => { pinned = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 48; }, { passive: true });
    new ResizeObserver(([entry]) => {
      const height = entry.contentRect.height;
      if (height !== composeHeight) { composeHeight = height; if (pinned) stream.scrollTop = stream.scrollHeight; }
    }).observe($("#chat-compose"));
  }
  document.fonts?.addEventListener("loadingdone", resizeCompose);
  $("#chat-compose").addEventListener("submit", event => { event.preventDefault(); submit(); });
  $("#chat-input").addEventListener("input", () => { const data = cache.get(selected); if (data) data.draft = $("#chat-input").value; resizeCompose(); renderHeader(); unlockSound(); });
  $("#chat-templates").addEventListener("click", () => {
    const target = selected, data = cache.get(target); if (!data || $("#chat-input").disabled) return;
    const options = $("#chat-template-options"); options.replaceChildren();
    for (const [title, text] of quickReplies) {
      const item = button("", "item", () => {
        if (target !== selected || $("#chat-input").disabled) return;
        $("#chat-input").value = text;
        $("#chat-input").dispatchEvent(new Event("input", { bubbles: true }));
        $("#chat-templates-dialog").close(); $("#chat-input").focus();
      });
      const body = node("span", undefined, "item-main"); body.append(node("strong", title), node("small", text)); item.append(body); options.append(item);
    }
    $("#chat-templates-dialog").showModal();
  });
  $("#chat-input").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing && matchMedia("(min-width: 761px)").matches) { event.preventDefault(); submit(); } });
  let previousHistoryTop = 0;
  const historyThreshold = () => state.chatProtocol >= 2 ? Math.max(180, $("#chat-stream").clientHeight * 1.5) : 180;
  $("#chat-stream").addEventListener("scroll", () => {
    const top = $("#chat-stream").scrollTop;
    if (top <= 180 || (top < previousHistoryTop && top <= historyThreshold())) loadOlder();
    previousHistoryTop = top;
  }, { passive: true });
  $("#chat-stream").addEventListener("wheel", event => { if (event.deltaY < 0 && $("#chat-stream").scrollTop <= historyThreshold()) loadOlder(); }, { passive: true });
  window.addEventListener("resize", () => requestAnimationFrame(() => { resizeCompose(); maybeFillHistory(); }));
  $("#chat-history-more").addEventListener("click", () => loadOlder({ retry: true }));
  // Explicit filters and their reset control stay in Profile.
  function renderFilterChip() {
    const picked = [["chat-branch", ""], ["chat-channel", ""]].map(([id]) => $(`#${id}`)).filter(select => select.value).map(select => select.selectedOptions[0]?.textContent || "");
    const status = $("#chat-status");
    if (status.value) picked.push(status.selectedOptions[0]?.textContent || "");
    const chip = $("#chat-filter-chip"); show(chip, picked.length > 0);
    chip.textContent = picked.length ? `Фильтр: ${picked.join(" · ")} ✕` : "";
  }
  $("#chat-filter-chip").addEventListener("click", () => {
    $("#chat-branch").value = ""; $("#chat-channel").value = "";
    $("#chat-status").value = "";
    filtersChanged();
  });
  function filtersChanged() {
    renderFilterChip();
    listHeights.clear(); listGeometry = null; lastViewportKey = "";
    if (selected && cache.has(selected) && !cache.get(selected).pending) cache.get(selected).draft = $("#chat-input").value;
    cache.get(selected)?.controller?.abort(); listPollAfter = 0; listPollError = null; historyPollError = null;
    listToken = null; listTask = null;
    selected = null; historyBusy = false; listLoading = false; listRetryRequired = false; hasMore = false; page = 0;
    updateListLoader();
    listSeq += 1; selectionSeq += 1; clearTimeout(searchTimer); notice("", $("#chat-error"));
    $("#chat-layout").classList.remove("has-selection"); $("#chat-input").value = "";
    $("#chat-history").replaceChildren(); show($("#chat-stream"), false); show($("#chat-compose"), false);
    $("#chat-person").replaceChildren(node("strong", "Выберите диалог"));
    if (detailsDialog.open) detailsDialog.close();
    loadList().catch(error => fail(error));
  }
  // Search opens in the header, right of the logo, where the search button was. While it
  // is open the view chips and the phone's bottom menu step aside, and it always searches
  // every dialog; closing it brings back the view the operator had before.
  let viewBeforeSearch = null;
  function setSearching(open) {
    const wrap = $("#chat-search-wrap"), input = $("#chat-search"), toggle = $("#chat-search-toggle");
    if (open === !wrap.hidden) return;
    show(wrap, open); $("#app").classList.toggle("is-searching", open);
    toggle.setAttribute("aria-expanded", String(open)); toggle.setAttribute("aria-label", open ? "Закрыть поиск" : "Поиск");
    toggle.replaceChildren(icon(open ? "close" : "search"));
    if (open) {
      const current = state.chatView || "all";
      if (current !== "all" && current !== "queue") { viewBeforeSearch = current; state.chatView = "all"; setView("all"); }
      input.focus();
      return;
    }
    const hadQuery = Boolean(input.value); input.value = "";
    if (viewBeforeSearch) { const back = viewBeforeSearch; viewBeforeSearch = null; state.chatView = back; setView(back); }
    else if (hadQuery) filtersChanged();
  }
  $("#chat-search-toggle").addEventListener("click", () => setSearching($("#chat-search-wrap").hidden));
  $("#chat-search").addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); setSearching(false); } });
  $("#chat-search").addEventListener("input", () => { clearTimeout(searchTimer); listSeq += 1; searchTimer = setTimeout(filtersChanged, 250); });
  ["branch", "status"].forEach(id => $(`#chat-${id}`).addEventListener("change", filtersChanged));
  $("#chat-channel").addEventListener("change", () => { filtersChanged(); });
  function setView(next, reload = true) {
    // Normalize obsolete tabs without clearing a deliberately selected Profile filter.
    if (next === "lead") $("#chat-status").value = "";
    state.chatView = "all";
    if (reload) filtersChanged();
  }
  $(".chat-list-scroll").addEventListener("scroll", maybeLoadMore, { passive: true });
  $(".chat-list-scroll").addEventListener("scroll", queueListViewport, { passive: true });
  if (window.ResizeObserver) new ResizeObserver(queueListViewport).observe($(".chat-list-scroll"));
  document.fonts?.ready.then(() => { listHeights.clear(); listGeometry = null; lastViewportKey = ""; queueListViewport(); });
  $("#chat-retry").addEventListener("click", () => { listRetryRequired = false; loadList(true).catch(error => fail(error)); });
  const handledButton = $("#chat-handled");
  handledButton.title = "Обработать диалог. Удерживайте или нажмите Shift+Enter для всех непрочитанных диалогов";
  handledButton.setAttribute("aria-keyshortcuts", "Shift+Enter");
  const cancelHandledPress = () => { clearTimeout(handledTimer); handledTimer = null; handledPointer = null; };
  const abortHandledPress = () => { if (handledPointer) handledConsumed = true; cancelHandledPress(); };
  handledButton.addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.isPrimary === false || actionBusy || bulkLoading || bulkDialog.open) return;
    handledConsumed = false; handledPointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
    try { handledButton.setPointerCapture(event.pointerId); } catch (_) {}
    handledTimer = setTimeout(() => { handledConsumed = true; cancelHandledPress(); openBulkHandled(); }, 700);
  });
  handledButton.addEventListener("pointermove", event => {
    if (handledPointer && event.pointerId === handledPointer.id && Math.hypot(event.clientX-handledPointer.x,event.clientY-handledPointer.y)>12) {
      handledConsumed = true; cancelHandledPress();
    }
  });
  handledButton.addEventListener("pointerup", cancelHandledPress);
  for (const type of ["pointercancel", "lostpointercapture"]) handledButton.addEventListener(type, abortHandledPress);
  handledButton.addEventListener("contextmenu", event => event.preventDefault());
  handledButton.addEventListener("keydown", event => {
    if (event.key === "Enter" && event.shiftKey) { event.preventDefault(); handledConsumed = true; openBulkHandled(); }
  });
  handledButton.addEventListener("click", event => {
    if (handledConsumed && event.detail !== 0) { handledConsumed = false; event.preventDefault(); return; }
    if (!bulkDialog.open && !bulkLoading) markHandled();
  });
  const clearBulk = () => { bulkSeq += 1; bulkOperation = null; bulkLoading = false; notice("", $("#chat-bulk-error")); renderHeader(); };
  const closeBulk = () => { clearBulk(); if (bulkDialog.open) bulkDialog.close(); };
  const showBulk = seq => { bulkOpenedSeq = seq; if (!bulkDialog.open) bulkDialog.showModal(); };
  for (const id of ["#chat-bulk-cancel", "#chat-bulk-close"]) $(id).addEventListener("click", () => { if (!bulkBusy) closeBulk(); });
  bulkDialog.addEventListener("cancel", event => { if (bulkBusy) event.preventDefault(); else clearBulk(); });
  // Native close events are queued. A previous dialog's event must not clear
  // the confirmation already being prepared or displayed after reopening.
  bulkDialog.addEventListener("close", () => { if (!bulkDialog.open && bulkOpenedSeq === bulkSeq) clearBulk(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) abortHandledPress(); });
  window.addEventListener("blur", abortHandledPress);
  $("#chat-bulk-retry").addEventListener("click", openBulkHandled);
  $("#chat-bulk-confirm").addEventListener("click", confirmBulkHandled);
  // The name plate always opens the card; the info button opens and closes it.
  document.addEventListener("cm:chat-details-open", () => {
    if (detailsDialog.open && !detailsDialog.classList.contains("is-closing")) return;
    openDetails().catch(error => fail(error));
  });
  $("#chat-details").addEventListener("click", () => {
    if (detailsDialog.open && detailsDialog.dataset.mode === "docked") closeDetails(); else openDetails().catch(error => fail(error));
  });
  $("#chat-detach").addEventListener("click", () => { window.open("/?view=messages", "center-messenger-chat", "popup=yes,width=1280,height=860,resizable=yes,scrollbars=yes"); });
  $("#chat-list-toggle").addEventListener("click", () => { const layout = $("#chat-layout"); if (matchMedia("(max-width: 719px)").matches) layout.classList.remove("has-selection"); else { layout.classList.toggle("list-collapsed"); syncDetailsMode(); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active()) schedule(150); });

  async function markHandled() {
    const id = selected, item = currentItem(), data = cache.get(selected);
    if (!id || actionBusy || !data?.meta?.unread) return;
    const epoch = state.epoch, gen = generation;
    actionBusy = true; $("#chat-handled").disabled = true;
    try {
      const result = await api(`${chatPath(id)}/handled`, { method: "POST", data: { seen_checkpoint: data.meta.seen_checkpoint } });
      if (!valid(epoch, gen)) return;
      // Only the server knows whether an unseen incoming arrived during the action.
      if (result.conversation && currentMeta(result.conversation, data.meta)) {
        data.meta = result.conversation; mergeMeta(result.conversation);
      }
      renderList(true); renderHeader();
      toast(result.conversation?.unread ? "Просмотренные сообщения обработаны. Есть новые входящие." : "Диалог отмечен как обработанный.");
      loadList(false, true, true).catch(error => fail(error));
    } catch (error) { if (valid(epoch, gen)) { fail(error); if (error.status === 409) await loadHistory(id).catch(() => {}); } }
    finally { if (valid(epoch, gen)) { actionBusy = false; renderHeader(); } }
  }

  async function openBulkHandled() {
    if (!active() || actionBusy || bulkBusy || bulkLoading || state.workspace.role === "observer") return;
    const epoch = state.epoch, gen = generation, seq = ++bulkSeq;
    bulkLoading = true; bulkOperation = null; renderHeader();
    $("#chat-bulk-confirm").disabled = true; $("#chat-bulk-summary").replaceChildren();
    notice("", $("#chat-bulk-error")); show($("#chat-bulk-retry"), false);
    try {
      const result = await api(`${chatPath()}/handled/prepare`, { method: "POST" });
      if (!valid(epoch, gen) || seq !== bulkSeq || !active()) return;
      bulkOperation = result;
      for (const [label, value] of [["Клиентов", result.client_count], ["Диалогов", result.conversation_count], ["Сообщений без ответа", result.message_count]])
        $("#chat-bulk-summary").append(node("span", `${label}: ${Number(value).toLocaleString("ru-RU")}`));
      $("#chat-bulk-confirm").disabled = !result.conversation_count;
      if (!result.conversation_count) $("#chat-bulk-summary").append(node("span", "Все диалоги уже обработаны."));
      // The server snapshot precedes opening: incoming after this dialog opens
      // can never be included by the confirmation button.
      showBulk(seq);
      $("#chat-bulk-cancel").focus();
    } catch (error) {
      if (valid(epoch, gen) && seq === bulkSeq) {
        if (error.status === 403) { closeBulk(); fail(error); }
        else { notice(error.message, $("#chat-bulk-error")); show($("#chat-bulk-retry")); showBulk(seq); }
      }
    } finally { if (valid(epoch, gen) && seq === bulkSeq) { bulkLoading = false; renderHeader(); } }
  }

  async function confirmBulkHandled() {
    if (!bulkOperation || !bulkDialog.open || bulkBusy || !bulkOperation.conversation_count) return;
    const epoch = state.epoch, gen = generation, seq = bulkSeq, operation = bulkOperation;
    const controls = ["#chat-bulk-confirm", "#chat-bulk-cancel", "#chat-bulk-close", "#chat-bulk-retry"];
    bulkBusy = true; actionBusy = true; controls.forEach(id => { $(id).disabled = true; }); renderHeader();
    notice("", $("#chat-bulk-error")); $("#chat-bulk-confirm").textContent = "Обрабатываем…";
    try {
      const result = await api(`${chatPath()}/handled/confirm`, { method: "POST", data: { operation_id: operation.operation_id, confirmed: true } });
      if (!valid(epoch, gen) || seq !== bulkSeq) return;
      bulkBusy = false; closeBulk();
      toast(`Обработано диалогов: ${result.processed}.${result.already_handled ? ` Уже обработаны: ${result.already_handled}.` : ""}${result.skipped ? ` Пропущено из-за изменений: ${result.skipped}.` : ""}`);
      await loadList(false, true, true);
      if (selected) await loadHistory(selected);
    } catch (error) {
      if (valid(epoch, gen) && seq === bulkSeq) {
        if (error.status === 403) { bulkBusy = false; closeBulk(); fail(error); }
        else {
          notice(error.message, $("#chat-bulk-error"));
          if ([404,409].includes(error.status)) { bulkOperation = null; show($("#chat-bulk-retry")); }
          // A lost response retries this same operation; it never captures new incoming.
        }
      }
    } finally {
      if (valid(epoch, gen)) {
        bulkBusy = false; actionBusy = false; controls.forEach(id => { $(id).disabled = false; });
        $("#chat-bulk-confirm").textContent = "Подтвердить"; $("#chat-bulk-confirm").disabled = !bulkOperation?.conversation_count;
        renderHeader();
      }
    }
  }

  const crmUrl = value => {
    try { const url = new URL(value); return url.protocol === "https:" && /^[a-z0-9-]+\.s20\.online$/i.test(url.hostname) ? url.href : null; }
    catch (_) { return null; }
  };
  // Client card. One column that reads top to bottom: who the parent is and how to reach
  // them, which child we are talking about, what needs attention now, then the detail.
  const detailsDialog = $("#chat-details-dialog"), detailsPanel = detailsDialog.querySelector(".family-panel");
  const isAdmin = () => ["owner", "admin"].includes(state.workspace?.role);
  const plural = (count, one, few, many) => { const a = Math.abs(count) % 100, b = a % 10; return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many; };
  const familyToday = () => family?.today || new Date().toISOString().slice(0, 10);
  const parseDay = value => { if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return null; const [y, m, d] = value.split("-").map(Number); return new Date(y, m - 1, d); };
  const daysBetween = (from, to) => Math.round((parseDay(to) - parseDay(from)) / 86400000);
  const familyNumber = value => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value));
  const familyMoney = value => `${familyNumber(value)} ₽`;
  const familyDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? value.split("-").reverse().join(".") : null;
  const known = value => value !== null && value !== undefined && value !== "" && !Number.isNaN(Number(value));
  const subName = item => item.name || `Абонемент №${item.tariff_id}`;
  const initialsOf = name => (name || "").trim().split(/\s+/).slice(0, 2).map(part => part[0] || "").join("").toLocaleUpperCase("ru");
  function familyAge(dob) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dob || "")) return "Возраст не указан";
    const [year, month, day] = dob.split("-").map(Number), now = parseDay(familyToday()) || new Date();
    const age = now.getFullYear() - year - (now.getMonth() + 1 < month || (now.getMonth() + 1 === month && now.getDate() < day) ? 1 : 0);
    return age >= 0 && age < 120 ? `${age} ${plural(age, "год", "года", "лет")}` : "Возраст не указан";
  }
  function dayLabel(value, time) {
    const day = parseDay(value); if (!day) return null;
    const diff = daysBetween(familyToday(), value);
    let text = diff === 0 ? "Сегодня" : diff === 1 ? "Завтра" : diff === -1 ? "Вчера"
      : new Intl.DateTimeFormat("ru-RU", { weekday: "short", day: "numeric", month: "short" }).format(day).replace(/\.$/, "");
    text = text.charAt(0).toLocaleUpperCase("ru") + text.slice(1);
    return time ? `${text}, ${time}` : text;
  }
  const activeSubscriptions = student => (student.subscriptions || (student.cards || []).flatMap(card => (card.subscriptions || []).map(item => ({ ...item, branch_name: card.branch_name })))).filter(item => !item.archived && (!item.begin || item.begin <= familyToday()) && (!item.end || item.end >= familyToday()));
  function el(tag, className, ...children) {
    const result = node(tag, undefined, className);
    for (const child of children) if (child !== null && child !== undefined && child !== false && child !== "") result.append(child);
    return result;
  }
  function glyph(name, className = "family-ic") { const holder = node("span", undefined, className); holder.setAttribute("aria-hidden", "true"); holder.append(icon(name)); return holder; }
  function familyLink(url, title, label) {
    const href = crmUrl(url); if (!href) return null;
    const link = node("a", undefined, label ? "family-crm" : "family-crm icon-only");
    link.href = href; link.target = "center-messenger-crm"; link.rel = "noopener noreferrer"; link.title = title; link.setAttribute("aria-label", title);
    if (label) link.append(node("span", label));
    link.append(icon("out")); return link;
  }
  const chip = (text, tone) => node("span", text, `family-chip ${tone || ""}`.trim());
  function contactRow(kind, value) {
    const link = node("a", value); link.href = kind === "phone" ? `tel:${value.replace(/[^\d+]/g, "")}` : `mailto:${value}`;
    const copy = button("", "family-copy", async () => {
      try { await navigator.clipboard.writeText(value); toast(kind === "phone" ? "Телефон скопирован." : "Почта скопирована."); }
      catch (_) { toast("Не удалось скопировать. Выделите текст вручную.", true); }
    });
    copy.append(icon("copy")); copy.setAttribute("aria-label", `Скопировать ${value}`); copy.title = "Скопировать";
    return el("div", "family-contact", glyph(kind === "phone" ? "phone" : "mail"), link, copy);
  }
  function note(name, title, text, tone) {
    return el("div", `family-note ${tone}`, glyph(name), el("div", "family-note-text", node("strong", title), text ? node("span", text) : null));
  }
  function stat(label, value, hint, tone) {
    const cell = el("div", `family-stat${tone ? ` ${tone}` : ""}`, node("span", label), node("strong", value));
    if (hint) cell.title = `${label}: ${value} — ${hint}`;
    return cell;
  }
  // Russian names are "Surname Name Patronymic"; a child switcher reads best by first name.
  const firstName = name => { const parts = (name || "").trim().split(/\s+/); return parts.length >= 2 ? parts[1] : parts[0] || "Ребёнок"; };
  const part = (title, ...children) => el("section", "family-part", node("h4", title, "family-label"), ...children);
  function fact(label, ...value) { return el("div", "family-fact", node("dt", label), el("dd", undefined, ...value)); }
  // The child who actually attends comes first and opens by default: a lesson ahead
  // outranks an active subscription or group, which outrank a lead with neither.
  function attendance(student) {
    const groups = student.groups || [];
    if (groups.some(group => group.next_lesson?.date)) return 3;
    if (activeSubscriptions(student).length) return 2;
    if (groups.length) return 1;
    return 0;
  }
  const orderStudents = list => list.map((student, index) => ({ student, index, rank: attendance(student) }))
    .sort((a, b) => b.rank - a.rank || a.index - b.index).map(item => item.student);
  function familyStatus() {
    const all = (family.students || []).flatMap(student => (student.cards || []).map(card => card.status));
    return ["client", "lead", "archive"].filter(status => all.includes(status));
  }
  function familySync(text, details = text) {
    const sync = $("#chat-details-sync");
    sync.textContent = text; sync.title = details;
    sync.setAttribute("aria-label", details);
  }
  function renderFamily() {
    if (!family) return;
    detailsDialog.classList.remove("is-loading");
    const students = orderStudents(family.students || []);
    // A newly preferred CRM student replaces a formerly selected archive card.
    // Ordinary cache refreshes retain the operator's choice within this family.
    if (family.selection?.student && family.selection.student !== familyPreferredStudent
        && students.some(student => student.id === family.selection.student)) {
      familyPreferredStudent = family.selection.student; familyChildChoice = false;
    }
    if (!familyChildChoice && students.some(student => student.id === family.selection?.student)) familySelected = family.selection.student;
    if (!students.some(student => student.id === familySelected)) familySelected = students[0]?.id || null;
    renderFamilySync(students);
    // Queue progress and freshness change more often than the actual card facts.
    // Keep its focused controls, child choice and scroll position when only the footer changes.
    const { cache: progress, snapshot_at: snapshot, version: viewVersion, ...facts } = family;
    const detailStates = Object.fromEntries(Object.entries(progress?.sections || {}).map(([name, section]) => [name, section.status]));
    const key = JSON.stringify([facts, detailStates, familySelected, familyToday(), state.workspace?.timezone, currentItem()?.status]);
    if (familyRenderKey === key) return;
    const scroll = detailsDialog.querySelector(".family-scroll"), top = scroll.scrollTop;
    familyRenderKey = key;
    renderFamilyContent(students);
    scroll.scrollTop = top;
  }
  function renderFamilySync(students) {
    const synced = family.snapshot_at ? new Date(family.snapshot_at) : null;
    const selected = students.find(student => student.id === familySelected) || students[0];
    const times = (selected?.cards || []).map(card => card.refreshed_at).filter(Boolean).sort();
    familySync(times.length ? `Обновлено ${date(times[0])}` : synced && !Number.isNaN(synced.getTime()) ? `Справочник от ${date(family.snapshot_at)}` : "Справочник CRM");
    if (family.cache) {
      const info = family.cache;
      const updated = info.updated_at ? `Баланс и абонементы: ${date(info.updated_at)}` : `Баланс и абонементы: данные не получены`;
      const lessons = "lessons_updated_at" in info ? info.lessons_updated_at ? `Занятия: ${date(info.lessons_updated_at)}` : "Занятия: данные не получены" : "";
      const history = "lessons_history_updated_at" in info ? info.lessons_history_updated_at ? `Год занятий проверен ${date(info.lessons_history_updated_at)}` : "Год занятий: данные не получены" : "";
      const groups = "groups_updated_at" in info ? info.groups_updated_at ? `Группы: ${date(info.groups_updated_at)}` : "Группы: из справочника CRM" : "";
      const active = Object.values(info.sections || {}).find(item => item.status === "loading");
      const waiting = Object.values(info.sections || {}).find(item => ["busy", "retry_wait", "partial", "queued"].includes(item.status));
      const phase = info.state || (info.refreshing ? "loading" : info.error ? "error" : info.pending ? "queued" : "ready");
      const status = phase === "loading" ? `Загружаем ${active?.label?.toLowerCase() || "карточку"}`
        : phase === "busy" ? "Канал AlfaCRM занят"
        : phase === "retry_wait" ? `Ожидаем повтор${info.retry_at ? ` · ${date(info.retry_at)}` : ""}`
        : ["queued", "partial"].includes(phase) ? `В очереди${info.queue_position ? ` · позиция ${info.queue_position}` : ""}`
        : phase === "error" ? "Ошибка обновления"
        : info.complete ? "Карточка актуальна" : info.overdue ? "Данные требуют обновления"
        : info.auto_refresh ? "Обновляем каждые 30 минут" : "Обновляем при открытии карточки";
      const issue = info.failures?.[0]?.code;
      const legacyBrief = issue === "credentials_required" ? "проверьте ключ CRM" : issue === "access_denied" ? "ограничен доступ CRM" : issue === "not_found" ? "клиент не найден в CRM" : info.retry_at && info.error ? `повтор ${date(info.retry_at)}` : info.error ? "есть ошибка обновления" : info.refreshing ? "обновляем" : info.overdue ? "ждёт обновления" : info.auto_refresh ? "каждые 30 мин" : "при открытии";
      const headline = !info.state ? `${info.updated_at ? `Обновлено ${date(info.updated_at)}` : "Загружаем карточку"} · ${legacyBrief}`
        : phase === "loading" ? status
        : phase === "busy" ? `Канал AlfaCRM занят${info.retry_at ? ` · повтор ${date(info.retry_at)}` : ""}`
        : ["queued", "partial"].includes(phase) ? status
        : phase === "retry_wait" ? status
        : phase === "error" ? `Ошибка обновления · ${issue === "credentials_required" ? "проверьте ключ CRM" : issue === "access_denied" ? "ограничен доступ CRM" : issue === "not_found" ? "клиент не найден в CRM" : "Повторить"}`
        : info.updated_at ? `Карточка обновлена ${date(info.updated_at)}` : "Показаны данные справочника";
      const savedAt = info.snapshot?.updated_at || info.snapshot?.published_at;
      const saved = savedAt ? `Сохранённая карточка от ${date(savedAt)}` : "";
      const delayed = info.wait_exceeded && phase !== "ready" ? `Ожидание дольше ${info.max_wait_seconds || 60} с` : "";
      familySync([saved, saved && phase === "ready" ? "" : headline, delayed].filter(Boolean).join(" · "),
        [saved, saved ? "Состояние следующей версии:" : "", updated, lessons, history, groups, status, delayed, waiting?.error_message, info.error].filter(Boolean).join(" · "));
      familyRefreshState();
    }
    if (!family.cache) familyRefreshState();
  }
  function renderFamilyContent(students) {
    $("#chat-details-name").textContent = family.name;
    const found = familyStatus(), main = found[0] || currentItem()?.status || "unlinked";
    const avatar = $("#chat-details-avatar"); avatar.textContent = initialsOf(family.name); avatar.className = `ava family-ava ${statuses[main] ? main : "unlinked"}`;
    const badges = $("#chat-details-badges"); badges.replaceChildren();
    for (const status of found) badges.append(chip(statuses[status], status));
    if (family.max_connected || family.max_bot_connected) { const max = chip("MAX", "max"); max.title = "Родитель подключён к MAX"; badges.append(max); }
    if (family.needs_review) {
      const review = chip("Нужна проверка в CRM", "unlinked");
      review.title = (family.review_reasons || []).map(reason => reason.text +
        (reason.customer_ids?.length ? ` ID AlfaCRM: ${reason.customer_ids.join(", ")}.` : "") +
        (reason.branch_ids?.length ? ` Филиалы: ${reason.branch_ids.join(", ")}.` : "")).join("\n") || "Неоднозначная связь карточек CRM.";
      badges.append(review);
    }
    const contact = $("#chat-details-contact"); contact.replaceChildren();
    for (const phone of family.phones || []) contact.append(contactRow("phone", phone));
    for (const email of family.emails || []) contact.append(contactRow("email", email));
    if (!contact.childNodes.length) contact.append(node("p", "Телефон и почта в CRM не указаны.", "family-empty"));
    $("#chat-details-count").textContent = students.length > 1 ? String(students.length) : "";
    const list = $("#chat-details-children"), detail = $("#chat-details-student"); detail.replaceChildren();
    const previousKids = new Map([...list.children].map(kid => [kid.dataset.student, kid])), kids = [];
    detailsDialog.querySelector(".family-kids-block").hidden = students.length < 2;
    if (!students.length) { list.replaceChildren(); detail.append(part("Дети", node("p", "В CRM у этого контакта нет детей.", "family-empty"))); return; }
    if (!students.some(student => student.id === familySelected)) familySelected = students[0].id;
    for (const student of students) {
      const chosen = student.id === familySelected, status = (student.cards || [])[0]?.status;
      const key = JSON.stringify([student.id, student.name, student.dob, status, familyToday()]);
      let kid = previousKids.get(student.id);
      if (!kid || kid.kidKey !== key) {
        kid = button("", "family-kid", () => { familyChildChoice = true; if (familySelected === student.id) return; familySelected = student.id; renderFamily(); });
        kid.dataset.student = student.id; kid.kidKey = key;
        kid.title = `${student.name}${statuses[status] ? ` · ${statuses[status]}` : ""}`;
        kid.append(firstName(student.name), node("small", familyAge(student.dob)));
      }
      kid.setAttribute("aria-pressed", String(chosen));
      kids.push(kid);
    }
    patchChildren(list, kids);
    const student = students.find(person => person.id === familySelected), cards = student.cards || [];
    const today = familyToday(), subs = activeSubscriptions(student), groups = student.groups || [];
    const subscriptionsLoaded = cards.every(card => card.subscriptions_ready ?? Array.isArray(card.subscriptions)), subscriptionConflict = subs.some(item => item.needs_review);
    const subscriptionsReady = subscriptionsLoaded && !subscriptionConflict;
    const lessonsReady = cards.every(card => card.lessons_ready ?? Array.isArray(card.lessons));
    const sectionStatus = name => family.cache?.sections?.[name]?.status;
    const groupsReady = sectionStatus("groups") === "ready";
    const relationsReady = subscriptionsReady && lessonsReady && groupsReady && cards.every(card => !card.needs_mapping_refresh);
    const born = familyDate(student.dob);
    detail.append(el("div", "family-kid-head",
      el("div", "family-kid-name", node("h3", student.name), node("p", [familyAge(student.dob), born && `род. ${born}`].filter(Boolean).join(" · "), "family-sub")),
      familyLink(cards[0]?.url, `Открыть ${student.name} в АльфаCRM`, "В CRM")));

    const notes = el("div", "family-notes");
    const upcoming = groups.filter(group => group.next_lesson?.date).sort((a, b) => `${a.next_lesson.date}${a.next_lesson.time || ""}`.localeCompare(`${b.next_lesson.date}${b.next_lesson.time || ""}`))[0];
    if (upcoming) notes.append(note("cal", `Ближайшее: ${dayLabel(upcoming.next_lesson.date, upcoming.next_lesson.time)}`, upcoming.name, "next"));
    for (const item of subs) {
      if (!item.end) continue;
      const left = daysBetween(today, item.end);
      if (left >= 0 && left <= 7) notes.append(note("warn", left === 0 ? "Абонемент заканчивается сегодня" : `Абонемент заканчивается через ${left} ${plural(left, "день", "дня", "дней")}`, `${subName(item)} · до ${familyDate(item.end)}`, "warn"));
    }
    if (known(student.balance) && Number(student.balance) < 0) notes.append(note("wallet", `Задолженность ${familyMoney(Math.abs(Number(student.balance)))}`, "Общий баланс клиента отрицательный", "debt"));
    const pendingDetails = [sectionStatus("finance"), sectionStatus("lessons"), sectionStatus("groups")].some(value => ["queued", "loading", "busy", "retry_wait", "partial"].includes(value));
    if ((!subscriptionsLoaded || !lessonsReady) && pendingDetails) notes.prepend(note("clock", "Готовим подробную карточку", "Абонементы и занятия поставлены в очередь. Уже полученные сведения доступны ниже.", ""));
    else if (!subscriptionsLoaded || !lessonsReady) notes.prepend(note("warn", "Подробные данные не получены", "Обновите карточку или проверьте состояние подключения AlfaCRM.", "warn"));
    if (subscriptionConflict) notes.prepend(note("warn", "Уточните сведения об абонементе", "Один ID вернулся из филиалов с разными данными. Обновите карточку.", ""));
    if (cards.some(card => card.needs_mapping_refresh) && pendingDetails) notes.prepend(note("clock", "Обновляем связи абонементов", "Проверяем предметы и филиалы по занятиям. Сохранённые суммы доступны.", ""));
    else if (cards.some(card => card.needs_mapping_refresh)) notes.prepend(note("warn", "Связи абонементов не получены", "Сохранённые суммы доступны. Запустите повторное обновление.", "warn"));
    if (notes.childNodes.length) detail.append(notes);

    const paid = cards.filter(card => known(card.paid_lesson_count));
    const paidTotal = student.paid_lesson_count === undefined ? (paid.length ? paid.reduce((sum, card) => sum + Number(card.paid_lesson_count), 0) : null) : student.paid_lesson_count;
    const negative = known(student.balance) && Number(student.balance) < 0;
    detail.append(el("div", "family-stats",
      stat("Баланс", known(student.balance) ? familyMoney(student.balance) : "—", known(student.balance) ? "Общий баланс в AlfaCRM: базовый счёт и абонементы" : "требует уточнения", negative ? "negative" : known(student.balance) ? "positive" : ""),
      stat("Оплачено", known(paidTotal) ? `${paidTotal} ${plural(paidTotal, "занятие", "занятия", "занятий")}` : "—", known(paidTotal) ? "к посещению" : "нет данных"),
      stat("Абонементы", subscriptionsReady ? String(subs.length) : "—", subscriptionConflict ? "уточните данные" : !subscriptionsReady ? "загружаем" : subs.length ? plural(subs.length, "активный", "активных", "активных") : "нет активных")));

    if (subs.length) {
      const rows = el("div", "family-list");
      for (const item of subs) {
        const left = item.end ? daysBetween(today, item.end) : null, soon = left !== null && left <= 7;
        const until = item.end ? `до ${familyDate(item.end)}` : "срок не указан";
        rows.append(el("div", `family-item${soon ? " soon" : ""}`,
          el("div", "family-item-main", node("strong", subName(item)), node("small", until)),
          node("span", item.balance === null || item.balance === undefined ? "—" : familyMoney(item.balance), `family-item-value${known(item.balance) && Number(item.balance)<0 ? " negative" : ""}`)));
      }
      detail.append(part("Абонементы", rows));
    }

    const groupCards = el("div", "family-groups");
    for (const group of groups) {
      const linked = (group.subscriptions || []).filter(item => !item.archived && (!item.begin || item.begin <= today) && (!item.end || item.end >= today)), previous = group.previous_lesson;
      const mark = !previous ? null : previous.attended === true ? node("span", "Был", "family-mark ok") : previous.attended === false ? node("span", "Не был", "family-mark miss") : node("span", "Не отмечено", "family-mark");
      const pay = linked.length ? linked.map(item => `${item.balance === null || item.balance === undefined ? "Остаток не указан" : familyMoney(item.balance)} · ${item.end ? `до ${familyDate(item.end)}` : "срок не указан"}`).join("\n") : relationsReady ? "Связь с абонементом не подтверждена" : "Проверяем связь с абонементом";
      const facts = el("dl", "family-facts",
        fact("Прошлое", previous ? node("span", dayLabel(previous.date, previous.time)) : node("span", lessonsReady ? "За год занятий не было" : "Сведения загружаются", "family-muted"), mark),
        fact("Следующее", group.next_lesson ? node("span", dayLabel(group.next_lesson.date, group.next_lesson.time)) : node("span", lessonsReady ? "Не запланировано на 90 дней" : "Сведения загружаются", "family-muted")),
        fact("Оплата", node("span", pay, linked.length ? "" : "family-muted")));
      if (linked.length) facts.lastChild.title = linked.map(item => `${subName(item)}: ${item.balance === null || item.balance === undefined ? "остаток не указан" : familyMoney(item.balance)}${item.end ? ` · до ${familyDate(item.end)}` : ""}`).join("\n");
      groupCards.append(el("article", "family-group",
        el("div", "family-group-head", el("div", "family-group-title", node("strong", group.name), node("small", group.branch_name || "Филиал не указан")), familyLink(group.url, `Открыть группу ${group.name} в АльфаCRM`)),
        facts));
    }
    if (!groups.length) groupCards.append(node("p", "Ребёнок пока не записан ни в одну группу.", "family-empty"));
    detail.append(part("Группы", groupCards));

    if (cards.length) {
      const rows = el("div", "family-list");
      for (const card of cards) {
        rows.append(el("div", "family-item",
          el("div", "family-item-main", node("strong", card.branch_name || "Филиал не указан")),
          familyLink(card.url, `Открыть карточку в филиале ${card.branch_name} в АльфаCRM`)));
      }
      detail.append(part(cards.length > 1 ? "Филиалы" : "Филиал", rows));
    }
  }
  function rememberFamily(contact, value) {
    if (familyCache.size >= 40) familyCache.delete(familyCache.keys().next().value);
    familyCache.set(contact, { value, at: Date.now() });
  }
  function cancelFamilyRequests(keepConversation = null) {
    for (const [path, entry] of familyRequests) {
      if (keepConversation && entry.conversation === keepConversation) continue;
      entry.controller?.abort(); familyRequests.delete(path);
    }
  }
  function preparedFamilyRequest(conversation, cachedOnly = false, contact = null) {
    const params = new URLSearchParams();
    if (cachedOnly) params.set("cached_only", "true");
    if (state.chatProtocol >= 2) {
      params.set("compact", "true");
      const version = familyCache.get(contact)?.value?.version;
      if (version) params.set("known_version", version);
    }
    const path = `${chatPath(conversation)}/family${params.size ? "?" + params : ""}`;
    if (!familyRequests.has(path)) {
      const controller = new AbortController();
      const entry = { controller, conversation, contact, promise: null };
      const request = api(path, { timeoutMs: 20000, signal: controller.signal }).then(result => {
        if (!result.not_modified) return result;
        const saved = familyCache.get(contact)?.value;
        if (saved && saved.version === result.version) return saved;
        params.delete("known_version");
        return api(`${chatPath(conversation)}/family?${params}`, { timeoutMs: 20000, signal: controller.signal });
      }).finally(() => {
        if (familyRequests.get(path) === entry) familyRequests.delete(path);
      });
      entry.promise = request; familyRequests.set(path, entry);
    }
    return familyRequests.get(path).promise;
  }
  function watchFamily(conversation, contact, epoch, gen, seq, retryAfterError = false) {
    clearTimeout(familyTimer);
    if (!family?.cache || (!family.cache.pending && !family.cache.incomplete && !family.cache.auto_refresh && !retryAfterError)) return;
    // Poll prepared facts only; the server owns CRM work and the 30-minute clock.
    const phase = family.cache.state || (family.cache.refreshing ? "loading" : family.cache.pending ? "queued" : family.cache.error ? "error" : "ready");
    const delay = retryAfterError ? 10000 : !family.cache.state && family.cache.paused ? 60000 : phase === "loading" ? 3000 : ["queued", "partial"].includes(phase) ? 5000
      : ["busy", "retry_wait"].includes(phase) ? 10000 : phase === "error" ? 30000 : family.cache.incomplete ? 10000 : 60000;
    familyTimer = setTimeout(async () => {
      if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || selected !== conversation || !active()) return;
      if (document.hidden) { watchFamily(conversation, contact, epoch, gen, seq, retryAfterError); return; }
      let failed = false;
      try {
        const updated = await preparedFamilyRequest(conversation, true, contact);
        if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || selected !== conversation) return;
        family = updated; rememberFamily(contact, updated); renderFamily();
      } catch (_) {
        if (valid(epoch, gen) && seq === detailsSeq) {
          if (familyAccessDenied(_, contact)) return;
          failed = true; familyReadUnavailable();
        }
      }
      if (valid(epoch, gen) && seq === detailsSeq) watchFamily(conversation, contact, epoch, gen, seq, failed);
    }, delay);
  }
  function familyRefreshState() {
    const refreshing = Boolean(family?.cache?.loading ?? family?.cache?.refreshing);
    detailsDialog.classList.toggle("is-refreshing", refreshing);
    const refresh = $("#chat-details-refresh");
    refresh.setAttribute("aria-busy", String(refreshing));
    refresh.disabled = familyBusy || !isAdmin();
    refresh.title = refreshing ? "Сервер загружает сведения. Сохранённые данные доступны."
      : familyBusy ? "Ставим карточку в очередь" : "Обновить сведения из CRM";
  }
  async function refreshSelected(force) {
    if (!force || !isAdmin() || familyBusy) return;
    if (!family) return openDetails();
    const epoch = state.epoch, gen = generation, seq = detailsSeq, conversation = selected, contact = family.source_contact_id || family.id;
    familyBusy = true; familyRefreshState();
    try {
      const updated = await api(`${chatPath(conversation)}/family/refresh${state.chatProtocol >= 2 ? "?compact=true" : ""}`, { method: "POST" });
      if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || selected !== conversation) return;
      family = updated; rememberFamily(contact, updated); renderFamily(); watchFamily(conversation, contact, epoch, gen, seq);
    } catch (error) { if (valid(epoch, gen) && seq === detailsSeq) toast(error.message, true); }
    finally { if (valid(epoch, gen) && seq === detailsSeq) { familyBusy = false; familyRefreshState(); } }
  }
  function familySkeleton() {
    $("#chat-details-avatar").textContent = ""; $("#chat-details-avatar").className = "ava family-ava me";
    $("#chat-details-badges").replaceChildren(); familySync(""); $("#chat-details-count").textContent = "";
    $("#chat-details-children").replaceChildren(); detailsDialog.querySelector(".family-kids-block").hidden = true;
    $("#chat-details-contact").replaceChildren(node("div", undefined, "family-skel"), node("div", undefined, "family-skel"));
    $("#chat-details-student").replaceChildren(node("div", undefined, "family-skel tall"), node("div", undefined, "family-skel"), node("div", undefined, "family-skel tall"));
  }
  function familyPending(name) {
    delete detailsDialog.dataset.identityPending;
    detailsSeq += 1; family = null; familyRenderKey = ""; familySelected = null; familyPreferredStudent = null; familyChildChoice = false; familyBusy = false; clearTimeout(familyTimer); familyTimer = null;
    detailsDialog.querySelector(".family-scroll").scrollTop = 0;
    $("#chat-details-name").textContent = name || "Загружаем сведения…";
    detailsDialog.classList.add("is-loading"); detailsDialog.classList.remove("is-refreshing"); familySkeleton();
  }
  function familyUnlinked() {
    detailsDialog.dataset.identityPending = "true";
    const item = currentItem(), data = cache.get(selected), name = item?.name || data?.meta?.peer_name || "Контакт MAX";
    family = null; detailsDialog.classList.remove("is-loading");
    $("#chat-details-name").textContent = name;
    const avatar = $("#chat-details-avatar"); avatar.textContent = initialsOf(name); avatar.className = "ava family-ava unlinked";
    $("#chat-details-badges").replaceChildren(chip("Не связан с CRM", "unlinked"));
    const resolution = ({ ...cache.get(selected)?.meta, ...currentItem() }).identity_resolution;
    const identityText = resolution?.text || "Диалог пока не связан с карточкой родителя в АльфаCRM, поэтому сведений о семье нет.";
    $("#chat-details-contact").replaceChildren(node("p", identityText + (resolution?.customer_id ? ` · ID CRM: ${resolution.customer_id}` : ""), "family-empty"));
    familySync(resolution?.next_at ? `Следующая проверка: ${new Date(resolution.next_at).toLocaleString("ru-RU", { timeZone: state.workspace?.timezone || "Europe/Moscow" })}` : "Нет данных CRM"); $("#chat-details-count").textContent = "";
    $("#chat-details-children").replaceChildren(); detailsDialog.querySelector(".family-kids-block").hidden = true;
    const detail = $("#chat-details-student"); detail.replaceChildren();
    if (isAdmin()) { const link = button("Связать с CRM", "family-link-btn", () => openLink(selected)); detail.append(link); }
  }
  function refreshIdentityDetails() {
    if (!detailsDialog.open || detailsDialog.dataset.identityPending !== "true" || family || detailsDialog.classList.contains("is-loading") || detailsDialog.classList.contains("is-closing")) return;
    const meta = { ...cache.get(selected)?.meta, ...currentItem() };
    if (meta.contact_id) openDetails({ auto: true }).catch(error => fail(error));
    else familyUnlinked();
  }
  async function openDetails(options = {}) {
    const meta = { ...cache.get(selected)?.meta, ...currentItem() }, contactId = meta.contact_id;
    if (!selected || !meta) return;
    if (!contactId && !meta.identity_resolution && !options.auto && !pinnedDocked()) { openLink(selected); return; }
    const epoch = state.epoch, gen = generation, conversation = selected;
    cancelFamilyRequests(conversation);
    const refresh = $("#chat-details-refresh"); refresh.hidden = !contactId; refresh.disabled = !isAdmin();
    familyPending(meta.name); const seq = detailsSeq;
    clearTimeout(closingTimer); detailsDialog.classList.remove("is-closing"); presentDetails();
    if (!contactId) { familyUnlinked(); return; }
    const saved = familyCache.get(contactId);
    if (saved && saved.value.selection?.signature === meta.card_selection?.signature) { family = saved.value; renderFamily(); }
    await readOpenedFamily(conversation, contactId, epoch, gen, seq);
  }
  function familyAccessDenied(error, contact) {
    if (![401, 403, 404].includes(error.status)) return false;
    familyCache.delete(contact); family = null; familyRenderKey = "";
    detailsDialog.classList.remove("is-loading", "is-refreshing");
    familySkeleton(); $("#chat-details-refresh").setAttribute("aria-busy", "false");
    $("#chat-details-contact").replaceChildren();
    $("#chat-details-student").replaceChildren(node("p", error.message, "family-empty"));
    familySync("Карточка недоступна", error.message); return true;
  }
  function familyReadUnavailable() {
    detailsDialog.classList.remove("is-loading", "is-refreshing");
    $("#chat-details-refresh").setAttribute("aria-busy", "false");
    familySync(family ? "Сохранённая карточка · повторяем подключение" : "Карточка пока недоступна · повторяем подключение",
      "Сервер временно не отвечает. Сохранённые данные остаются доступны; повторим запрос автоматически.");
  }
  async function readOpenedFamily(conversation, contactId, epoch, gen, seq) {
    try {
      // Show local facts first. Scheduling never waits for AlfaCRM.
      const cached = await preparedFamilyRequest(conversation, true, contactId);
      if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || conversation !== selected) return;
      family = cached; rememberFamily(contactId, cached); renderFamily();
      const result = await preparedFamilyRequest(conversation, false, contactId);
      if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || conversation !== selected) return;
      family = result; rememberFamily(contactId, result); renderFamily();
      watchFamily(conversation, contactId, epoch, gen, seq);
    } catch (error) {
      if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || conversation !== selected) return;
      if (familyAccessDenied(error, contactId)) return;
      familyReadUnavailable();
      if (!family) {
        $("#chat-details-contact").replaceChildren();
        $("#chat-details-student").replaceChildren(node("p", "Сведения временно недоступны. Повторим загрузку автоматически. Можно нажать стрелку обновления.", "family-empty"));
      }
      clearTimeout(familyTimer);
      const retry = () => {
        if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || conversation !== selected || !active()) return;
        if (document.hidden) { familyTimer = setTimeout(retry, 5000); return; }
        void readOpenedFamily(conversation, contactId, epoch, gen, seq);
      };
      familyTimer = setTimeout(retry, 5000);
    }
  }
  // Where the card lives. With room for list, chat and card side by side it docks as a
  // third column and the chat narrows between them; with less room it floats over the
  // page; on a phone it is a bottom sheet. Pinning only applies to the docked column:
  // a pinned card stays open and follows the operator from client to client.
  let detailsPinned = false, quietCloses = 0;
  try { detailsPinned = localStorage.getItem("cm-family-pinned") === "true"; } catch (_) { /* Optional preference. */ }
  const chatLayout = $("#chat-layout");
  function canDock() {
    if (!chatLayout || matchMedia("(max-width: 719px)").matches || !chatLayout.clientWidth) return false;
    const list = chatLayout.classList.contains("list-collapsed") ? 0 : chatLayout.querySelector(".pane-list")?.getBoundingClientRect().width || 0;
    return chatLayout.clientWidth - list >= 790;
  }
  function pinnedDocked() { return detailsPinned && canDock(); }
  function currentItemFor(id) { return items.find(item => item.id === id); }
  function renderPin() {
    const pin = $("#chat-details-pin");
    pin.hidden = detailsDialog.dataset.mode !== "docked";
    pin.setAttribute("aria-pressed", String(detailsPinned));
    pin.setAttribute("aria-label", detailsPinned ? "Открепить окно" : "Закрепить окно");
    pin.title = detailsPinned ? "Открепить: окно будет закрываться при переходе к другому клиенту" : "Закрепить: окно останется открытым при переходе к другим клиентам";
    $("#chat-details").setAttribute("aria-pressed", String(detailsDialog.open && detailsDialog.dataset.mode === "docked"));
  }
  function presentDetails() {
    const docked = canDock();
    if (detailsDialog.open && (detailsDialog.dataset.mode === "docked") === docked) { renderPin(); return; }
    // The close event arrives later, as a task; count the moves so it does not reset the card.
    if (detailsDialog.open) { quietCloses += 1; detailsDialog.close(); }
    detailsDialog.dataset.mode = docked ? "docked" : "overlay";
    detailsDialog.classList.toggle("docked", docked);
    // Opening focuses the panel itself (autofocus), never the refresh arrow, so no
    // button lights up. A docked card sits beside the chat: keep the operator's focus.
    const before = document.activeElement;
    if (docked) {
      chatLayout.append(detailsDialog); detailsDialog.show();
      if (before && before !== document.body && before.isConnected && !detailsDialog.contains(before)) before.focus({ preventScroll: true });
    } else { document.body.append(detailsDialog); detailsDialog.showModal(); }
    renderPin();
  }
  function syncDetailsMode() {
    if (!detailsDialog.open || detailsDialog.classList.contains("is-closing")) return;
    // A hidden Chats section has no width; that is not a reason to float the card over
    // whichever section is on screen now.
    if (!active() || !chatLayout.offsetParent || !chatLayout.clientWidth) return;
    if ((detailsDialog.dataset.mode === "docked") !== canDock()) presentDetails();
  }
  if (window.ResizeObserver && chatLayout) new ResizeObserver(() => syncDetailsMode()).observe(chatLayout);
  else window.addEventListener("resize", syncDetailsMode);
  $("#chat-details-pin").addEventListener("click", () => {
    detailsPinned = !detailsPinned;
    try { localStorage.setItem("cm-family-pinned", String(detailsPinned)); } catch (_) { /* Optional preference. */ }
    renderPin();
    toast(detailsPinned ? "Окно закреплено: оно останется открытым при переходе к другим клиентам." : "Закрепление снято.");
  });
  // Opening and closing slide the panel; Esc, the backdrop and the close button all
  // share one animated close. On a phone the sheet can also be pulled down by its handle.
  let closingTimer = null, sheetDrag = null;
  const sheetMode = () => matchMedia("(max-width: 719px)").matches;
  function closeDetails() {
    if (!detailsDialog.open || detailsDialog.classList.contains("is-closing")) return;
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) { detailsDialog.close(); return; }
    detailsDialog.classList.add("is-closing");
    closingTimer = setTimeout(() => { if (detailsDialog.open) detailsDialog.close(); }, 200);
  }
  detailsDialog.addEventListener("click", event => {
    if ((event.target === detailsDialog && detailsDialog.dataset.mode !== "docked") || event.target.closest("[data-close]")) { event.stopPropagation(); closeDetails(); }
  }, true);
  detailsDialog.addEventListener("cancel", event => { event.preventDefault(); closeDetails(); });
  detailsDialog.addEventListener("keydown", event => { if (event.key === "Escape" && detailsDialog.dataset.mode === "docked") { event.preventDefault(); closeDetails(); } });
  detailsPanel.addEventListener("pointerdown", event => {
    if (!sheetMode() || event.pointerType === "mouse" || !event.target.closest(".family-grab, .family-head") || event.target.closest("button, a")) return;
    sheetDrag = { id: event.pointerId, y: event.clientY, dy: 0 }; try { detailsPanel.setPointerCapture(event.pointerId); } catch (_) { /* Capture is optional. */ } detailsPanel.classList.add("dragging");
  });
  detailsPanel.addEventListener("pointermove", event => {
    if (!sheetDrag || event.pointerId !== sheetDrag.id) return;
    sheetDrag.dy = Math.max(0, event.clientY - sheetDrag.y); detailsPanel.style.setProperty("--sheetDrag", `${sheetDrag.dy}px`);
  });
  const endSheetDrag = event => {
    if (!sheetDrag || event.pointerId !== sheetDrag.id) return;
    const pulled = sheetDrag.dy; sheetDrag = null; detailsPanel.classList.remove("dragging");
    if (pulled > 110) closeDetails(); else detailsPanel.style.removeProperty("--sheetDrag");
  };
  detailsPanel.addEventListener("pointerup", endSheetDrag); detailsPanel.addEventListener("pointercancel", endSheetDrag);
  $("#chat-details-refresh").addEventListener("click", () => refreshSelected(true).catch(error => fail(error)));
  detailsDialog.addEventListener("close", () => {
    clearTimeout(closingTimer); detailsDialog.classList.remove("is-closing"); detailsPanel.style.removeProperty("--drag");
    if (quietCloses > 0) { quietCloses -= 1; return; }
    if (detailsDialog.open) return; // A late event from a close that a reopen has already replaced.
    detailsDialog.classList.remove("is-refreshing");
    detailsSeq += 1; family = null; clearTimeout(familyTimer); familyTimer = null; cancelFamilyRequests(); renderPin();
  });
  async function openCards(item) {
    if (!item.contact_id) return openLink(item.id);
    await select(item.id, false); await openDetails();
  }
  async function openLink(id) {
    if (!id) return;
    if (!["owner", "admin"].includes(state.workspace?.role)) { toast("Попросите администратора связать диалог с карточкой CRM.", true); return; }
    linkConversation = id; linkSeq += 1; $("#chat-link-search").value = ""; notice("", $("#chat-link-error"));
    const data = entry(id); show($("#chat-unlink"), Boolean(data.meta?.contact_id));
    $("#chat-link-results").replaceChildren(); $("#chat-link-dialog").showModal();
    if (data.meta?.contact_id) {
      const epoch = state.epoch, gen = generation, seq = linkSeq;
      try {
        const contact = await api(`${base()}/directory/contacts/${data.meta.contact_id}`);
        if (!valid(epoch, gen) || seq !== linkSeq || !$("#chat-link-dialog").open) return;
        const card = node("div", undefined, "chat-linked-family"); card.append(node("strong", contact.name));
        for (const student of contact.students) {
          const child = node("div", undefined, "chat-linked-child"); child.append(node("strong", student.name));
          child.append(node("small", student.groups.map(g => g.name).join(" · ") || "Актуальные группы не указаны")); card.append(child);
        }
        card.append(node("p", "Для замены связи найдите другого родителя выше. Карточка CRM открывается кружком в списке.", "form-note")); $("#chat-link-results").replaceChildren(card);
      } catch (error) { if (valid(epoch, gen) && seq === linkSeq) fail(error, $("#chat-link-error")); }
    }
  }
  $("#chat-link-search").addEventListener("input", () => {
    clearTimeout(linkTimer); const seq = ++linkSeq, epoch = state.epoch, gen = generation;
    linkTimer = setTimeout(async () => {
      const query = $("#chat-link-search").value.trim(); if (!query) return $("#chat-link-results").replaceChildren();
      try {
        const result = await api(`${base()}/directory?q=${encodeURIComponent(query)}`);
        if (!valid(epoch, gen) || seq !== linkSeq || !$("#chat-link-dialog").open) return;
        const list = $("#chat-link-results"); list.replaceChildren();
        for (const contact of result.contacts) {
          const control = button("", "chat-link-option", () => linkContact(contact.id));
          control.append(node("strong", contact.name), node("small", contact.students.join(", ")), node("span", statuses[contact.status], `status-label status-${contact.status}`)); list.append(control);
        }
        if (!result.contacts.length) list.append(node("p", "Контакты не найдены. Проверьте поиск или загрузку клиентов из CRM.", "muted small"));
        if (result.has_more) list.append(node("p", "Уточните поиск, чтобы найти нужного родителя.", "muted small"));
      } catch (error) { if (valid(epoch, gen) && seq === linkSeq) fail(error, $("#chat-link-error")); }
    }, 250);
  });
  async function linkContact(contact) {
    const id = linkConversation, epoch = state.epoch, gen = generation; if (!id || actionBusy) return;
    if (!await confirm(contact ? "Связать с этим родителем?" : "Убрать связь с CRM?", "Убедитесь, что выбран именно этот собеседник. MAX-получатель и история переписки останутся прежними.", contact ? "Связать" : "Убрать связь")) return;
    if (!valid(epoch, gen) || id !== linkConversation) return;
    actionBusy = true;
    try {
      await api(`${chatPath(id)}/contact`, { method: "PUT", data: { contact_id: contact } });
      if (valid(epoch, gen)) { $("#chat-link-dialog").close(); await loadHistory(id); await loadList(false, true, true); }
    } catch (error) { if (valid(epoch, gen)) fail(error, $("#chat-link-error")); }
    finally { if (valid(epoch, gen)) actionBusy = false; }
  }
  $("#chat-unlink").addEventListener("click", () => linkContact(null));
  $("#chat-link-dialog").addEventListener("close", () => { linkSeq += 1; clearTimeout(linkTimer); linkConversation = null; $("#chat-link-search").value = ""; $("#chat-link-results").replaceChildren(); });
  $("#crm-card-dialog").addEventListener("close", () => { cardSeq += 1; });

  // Only non-personal display preferences are kept across browser restarts.
  const defaults = { enabled: false, volume: 55, client: "soft", lead: "clear", archive: "low" };
  let sounds = { ...defaults }, taskSound = false;
  try {
    const saved = JSON.parse(localStorage.getItem("cm-sound") || "null");
    if (saved && typeof saved === "object") {
      sounds.enabled = saved.enabled === true;
      if (Number.isFinite(saved.volume)) sounds.volume = Math.max(0, Math.min(100, saved.volume));
      for (const key of ["client", "lead", "archive"]) if (["soft", "clear", "low", "off"].includes(saved[key])) sounds[key] = saved[key];
    }
  } catch (_) { /* Storage is optional. */ }
  function hydrateSounds() {
    $("#chat-sound-enabled").checked = sounds.enabled; $("#chat-volume").value = String(sounds.volume);
    for (const key of ["client", "lead", "archive"]) $(`#chat-tone-${key}`).value = sounds[key];
    if ($("#profile-sound")) $("#profile-sound").checked = sounds.enabled;
  }
  hydrateSounds();
  let previewEnabled = true;
  try { previewEnabled = localStorage.getItem("cm-notification-preview") !== "false"; } catch (_) { /* Optional preference. */ }
  if ($("#profile-preview")) {
    $("#profile-preview").checked = previewEnabled;
    $("#profile-preview").addEventListener("change", () => {
      previewEnabled = $("#profile-preview").checked;
      try { localStorage.setItem("cm-notification-preview", String(previewEnabled)); } catch (_) { /* Optional preference. */ }
      if (!previewEnabled) { $("#chat-new-marker").textContent = "Новое сообщение"; $("#chat-new-marker").title = "Открыть новое сообщение"; }
    });
  }
  function unlockSound() {
    if (!sounds.enabled && !taskSound) return;
    try { soundContext ||= new AudioContext(); if (soundContext.state === "suspended") soundContext.resume().catch(() => {}); } catch (_) { /* Browser audio may be unavailable. */ }
  }
  function playTone(status = "client", preview = false, selectedTone = null) {
    if (!sounds.enabled && !preview) return;
    const tone = selectedTone || sounds[status === "unlinked" ? "lead" : status] || "soft"; if (tone === "off") return;
    try {
      soundContext ||= new AudioContext(); if (soundContext.state !== "running") return;
      const frequencies = { soft: [660, 880], clear: [880, 1175, 880], low: [440, 554] }[tone];
      const start = soundContext.currentTime;
      frequencies.forEach((frequency, index) => {
        const oscillator = soundContext.createOscillator(), gain = soundContext.createGain();
        oscillator.type = "sine"; oscillator.frequency.value = frequency;
        const at = start + index * .14, level = sounds.volume / 100 * .16;
        gain.gain.setValueAtTime(0, at); gain.gain.linearRampToValueAtTime(level, at + .015); gain.gain.exponentialRampToValueAtTime(.0001, at + .20);
        oscillator.connect(gain); gain.connect(soundContext.destination); oscillator.start(at); oscillator.stop(at + .22);
      });
    } catch (_) { /* A blocked sound never blocks message intake. */ }
  }
  function saveSounds() {
    sounds = { enabled: $("#chat-sound-enabled").checked, volume: Number($("#chat-volume").value), client: $("#chat-tone-client").value, lead: $("#chat-tone-lead").value, archive: $("#chat-tone-archive").value };
    try { localStorage.setItem("cm-sound", JSON.stringify(sounds)); } catch (_) { /* Optional preference storage. */ }
    if ($("#profile-sound")) $("#profile-sound").checked = sounds.enabled;
    unlockSound();
  }
  document.addEventListener("cm:notification-preferences", event => {
    const p = event.detail; if (!p || p.epoch !== state.epoch) return;
    sounds = { enabled: p.sound_enabled, volume: p.volume, client: p.client, lead: p.lead, archive: p.archive };
    taskSound = Boolean(p.task_sound); hydrateSounds(); unlockSound();
    try { localStorage.setItem("cm-sound", JSON.stringify(sounds)); } catch (_) { /* Local playback cache only. */ }
  });
  document.addEventListener("cm:notification-task-sound", async event => {
    try { soundContext ||= new AudioContext(); await soundContext.resume(); playTone("client", true, "clear"); }
    catch (_) { if (event.detail?.preview) toast("Браузер не разрешил звук. Проверьте разрешение звука для сайта.", true); }
  });
  $("#chat-settings-open").addEventListener("click", () => {
    $("#chat-sound-enabled").checked = sounds.enabled; $("#chat-volume").value = String(sounds.volume);
    for (const key of ["client", "lead", "archive"]) $(`#chat-tone-${key}`).value = sounds[key];
    unlockSound(); $("#chat-settings-dialog").showModal();
  });
  ["#chat-sound-enabled", "#chat-volume", "#chat-tone-client", "#chat-tone-lead", "#chat-tone-archive"].forEach(id => $(id).addEventListener("input", saveSounds));
  $("#chat-sound-test").addEventListener("click", async () => { saveSounds(); try { soundContext ||= new AudioContext(); await soundContext.resume(); playTone("client", true); } catch (_) { toast("Браузер не разрешил звук. Проверьте разрешение звука для сайта.", true); } });
  document.addEventListener("pointerdown", unlockSound, { passive: true });

  // A workspace change cursor covers every conversation, independently of list filters.
  function stopStream() {
    const previous = stream; stream = null; streamHealthy = false;
    clearTimeout(streamTimer); clearTimeout(streamRetry); streamTimer = null; streamRetry = null;
    previous?.close();
  }
  function startStream() {
    if (state.chatProtocol < 2 || !window.EventSource || stream || streamRetry || !state.user || !state.workspace) return;
    const epoch = state.epoch, gen = generation;
    const source = new EventSource(`/api${base()}/chat-events`); stream = source;
    const current = () => stream === source && valid(epoch, gen);
    const fallback = () => {
      if (!current()) return;
      stopStream();
      if (active()) schedule(100);
      if (!notifyBusy) { clearTimeout(notifyTimer); notifyTimer = setTimeout(notifyPoll, 100); }
      streamRetry = setTimeout(() => { streamRetry = null; if (valid(epoch, gen)) startStream(); }, 30000);
    };
    const seen = () => {
      if (!current()) return false;
      const connecting = !streamHealthy;
      streamLastSeen = Date.now(); streamHealthy = true;
      if (connecting) { clearTimeout(notifyTimer); notifyTimer = null; }
      return true;
    };
    const readEvent = event => {
      try {
        const data = JSON.parse(event.data); streamVersion = typeof data.version === "string" ? data.version : null;
        if (typeof data.write_access === "boolean" && data.write_access !== (state.workspace.role !== "observer")) document.dispatchEvent(new CustomEvent("cm:chat-permissions-changed"));
      } catch (_) { streamVersion = null; }
    };
    source.addEventListener("ready", event => { if (seen()) {
      readEvent(event);
      notifyPoll();
      if (active() && !bootstrapBusy && (!streamVersion || !lastViewVersion || streamVersion !== lastViewVersion)) schedule(100);
    } });
    source.addEventListener("change", event => { if (seen()) {
      readEvent(event);
      notifyPoll(); if (active()) schedule(80);
    } });
    source.addEventListener("pulse", seen);
    source.addEventListener("retrying", fallback);
    source.addEventListener("access_lost", () => {
      if (!current()) return;
      reset(); document.dispatchEvent(new CustomEvent("cm:chat-access-lost"));
    });
    source.onerror = fallback;
    const health = () => {
      if (!current()) return;
      if (!streamHealthy || Date.now() - streamLastSeen > 35000) { fallback(); return; }
      streamTimer = setTimeout(health, 20000);
    };
    streamTimer = setTimeout(health, 6000);
  }
  function watch() {
    if (!state.user || !state.workspace || !(["owner", "admin"].includes(state.workspace.role) || state.workspace.sections?.includes("messages"))) { clearTimeout(notifyTimer); notifyTimer = null; stopStream(); return; }
    if (notifyEpoch !== state.epoch) { stopStream(); cache.get(selected)?.controller?.abort(); clearTimeout(notifyTimer); notifyTimer = null; notifySeq = null; notifyBusy = false; notifyAgain = false; pollBusy = false; historyBusy = false; notifyEpoch = state.epoch; }
    startStream();
    if (!streamHealthy && !notifyBusy && (notifyTimer === null || notifyTimer === undefined)) notifyTimer = setTimeout(notifyPoll, 300);
  }
  async function notifyPoll() {
    if (notifyBusy) { notifyAgain = true; return; }
    const epoch = state.epoch, gen = generation;
    clearTimeout(notifyTimer); notifyTimer = null;
    if (!state.user || !state.workspace || !(["owner", "admin"].includes(state.workspace.role) || state.workspace.sections?.includes("messages"))) return;
    let delay = 3500, catchup = false; notifyBusy = true;
    try {
      const result = await api(`${base()}/chat-updates${notifySeq === null ? "" : `?after_seq=${notifySeq}`}`, { timeoutMs: 20000 });
      if (!valid(epoch, gen)) return;
      notifySeq = result.next_seq;
      let latest = null;
      for (const update of result.updates) {
        const data = cache.get(update.id);
        // Notifications are not a history snapshot and cannot advance a seen boundary
        // or replace the row preview with a partial event payload.
        if (update.new_inbound && active()) schedule(100);
        if (update.new_inbound) latest = update;
      }
      if (latest) {
        notifyTarget = latest.id;
        // No pop-up banner for new messages: the list, the counters and the sound are enough.
        // Coordinate two open chat windows without storing message content or IDs.
        if (navigator.locks) navigator.locks.request("cm-message-sound", { ifAvailable: true }, async lock => { if (lock) { playTone(latest.status); await new Promise(resolve => setTimeout(resolve, 1000)); } }).catch(() => {});
        else playTone(latest.status);
        if (active()) schedule(100);
      } else if (result.updates.length && active()) schedule(100);
      if (result.has_more) { delay = 150; catchup = true; }
    } catch (error) {
      delay = 10000; catchup = true;
      if (error.status === 401 || error.status === 403) { if (valid(epoch, gen)) reset(); return; }
    } finally { if (valid(epoch, gen)) {
      notifyBusy = false;
      if (state.user && state.workspace && (notifyAgain || catchup || !streamHealthy)) notifyTimer = setTimeout(notifyPoll, notifyAgain ? 100 : delay);
      notifyAgain = false;
    } }
  }
  $("#chat-new-marker").addEventListener("click", async () => {
    const id = notifyTarget; if (!id) return;
    notifyTarget = null; show($("#chat-new-marker"), false);
    try { await renderPane("messages"); if (active()) await select(id); }
    catch (error) { if (!ignored(error)) toast(error.message, true); }
  });
  // Tasks open their conversation and, when it is loaded, show the source message.
  document.addEventListener("cm:open-chat", async event => {
    const { id, message } = event.detail || {}; if (!id) return;
    try {
      await renderPane("messages"); if (!active()) return;
      await select(id);
      if (!message) return;
      for (let attempt = 0; attempt < 20; attempt += 1) {
        const bubble = [...$("#chat-history").children].find(child => child.dataset.id === message);
        if (bubble) { bubble.scrollIntoView({ block: "center" }); bubble.classList.add("task-flash"); setTimeout(() => bubble.classList.remove("task-flash"), 1600); return; }
        await new Promise(resolve => setTimeout(resolve, 150));
      }
    } catch (error) { if (!ignored(error)) toast(error.message, true); }
  });
  const layout = $("#chat-layout"), resizer = $("#chat-resizer"); let width = 450, drag = null;
  function resize(value) {
    width = Math.max(300, Math.min(640, value, layout.clientWidth - 320));
    layout.style.setProperty("--chat-width", `${width}px`); resizer.setAttribute("aria-valuenow", String(Math.round(width)));
  }
  resizer.addEventListener("pointerdown", event => { if (event.button !== 0) return; drag = { x: event.clientX, width }; resizer.setPointerCapture(event.pointerId); layout.classList.add("resizing"); event.preventDefault(); });
  resizer.addEventListener("pointermove", event => { if (drag) resize(drag.width + event.clientX - drag.x); });
  function endDrag() { drag = null; layout.classList.remove("resizing"); }
  resizer.addEventListener("pointerup", endDrag); resizer.addEventListener("pointercancel", endDrag); resizer.addEventListener("lostpointercapture", endDrag);
  resizer.addEventListener("keydown", event => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); resize(event.key === "Home" ? 300 : event.key === "End" ? 640 : width + (event.key === "ArrowRight" ? 20 : -20)); } });
  return { reset, connections, enter, watch, setView, leave: () => {
    clearTimeout(timer); selectionSeq += 1; historyBusy = false; cache.get(selected)?.controller?.abort();
    // The docked card lives inside the Chats section and hides with it; a floating card
    // sits above every section, so it closes when the operator leaves Chats.
    if (detailsDialog.open && detailsDialog.dataset.mode !== "docked") detailsDialog.close();
  } };
};
