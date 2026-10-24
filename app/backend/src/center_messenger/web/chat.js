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
  const sendLabels = { queued: "В очереди", sending: "Отправляем…", accepted: "Принято MAX", delivered: "Доставлено", read: "Прочитано", failed: "Не отправлено", unknown: "Результат неизвестен", cancelled: "Отменено" };
  let generation = 0, listSeq = 0, selectionSeq = 0, linkSeq = 0, cardSeq = 0, detailsSeq = 0;
  let family = null, familySelected = null, familyBusy = false;
  const familyAttempted = new Set();
  let channels = [], items = [], selected = null, page = 0, hasMore = false;
  let timer, searchTimer, linkTimer, pollBusy = false, actionBusy = false, historyBusy = false, listLoading = false, listRetryRequired = false;
  let notifyTimer, notifySeq = null, notifyEpoch = null, notifyTarget = null, notifyBusy = false;
  let lastList = "", linkConversation = null, tokenChannel = null, soundContext = null;
  const cache = new Map();
  const active = () => state.user && state.workspace && state.pane === "messages";
  const valid = (epoch, gen) => state.epoch === epoch && generation === gen;
  const base = () => `/workspaces/${state.workspace.id}`;
  const channelPath = () => `${base()}/integrations/max`;
  const chatPath = id => `${base()}/chats${id ? `/${id}` : ""}`;
  const ignored = error => error.name === "AbortError";
  const currentItem = () => items.find(item => item.id === selected);
  function fail(error, target = $("#chat-error")) { if (!ignored(error)) notice(error.message, target); }
  function button(text, className, handler) {
    const control = node("button", text, className); control.type = "button";
    control.addEventListener("click", handler); return control;
  }
  function reset() {
    generation += 1; listSeq += 1; selectionSeq += 1; linkSeq += 1; cardSeq += 1; detailsSeq += 1; family = null; familySelected = null; familyBusy = false; familyAttempted.clear();
    clearTimeout(timer); clearTimeout(searchTimer); clearTimeout(linkTimer); clearTimeout(notifyTimer);
    notifyTimer = null; notifyBusy = false; notifySeq = null; notifyEpoch = null; notifyTarget = null; show($("#chat-new-marker"), false);
    cache.clear(); channels = []; items = []; selected = null; page = 0; hasMore = false;
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
    show($("#chat-blank")); show($("#chat-stream"), false); show($("#chat-compose"), false);
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
      cache.set(id, { messages: new Map(), next: 0, oldest: null, expanded: new Set(), more: false, loaded: false, meta: null, draft: "", pending: null });
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
  function renderList(force = false) {
    items.sort(compareConversations);
    const signature = JSON.stringify([items, selected]); if (!force && signature === lastList) return; lastList = signature;
    const list = $("#chat-list"), scroller = $(".chat-list-scroll"), top = scroller.scrollTop; list.replaceChildren();
    for (const item of items) {
      const row = node("div", undefined, `row chat-row${item.unread ? " unread waiting" : ""}${item.id === selected ? " selected" : ""}`); row.dataset.id = item.id; row.setAttribute("aria-current", String(item.id === selected));
      const avatar = button("", `contact-avatar ava ${item.status}${item.unread ? " pulsing" : ""} status-${item.status}`, () => openCards(item));
      avatar.title = item.contact_id ? `Открыть карточку АльфаCRM: ${item.name}` : "Связать диалог с контактом АльфаCRM"; avatar.setAttribute("aria-label", avatar.title);
      const letter = item.name.trim().split(/\s+/).slice(0, 2).map(part => part.slice(0, 1)).join("").toLocaleUpperCase("ru");
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
      heading.append(node("strong", item.name, "row-name"), timestamp);
      body.append(heading);
      if (item.students.length) body.append(node("span", item.students.join(" · "), "row-kids contact-students"));
      const bottom = node("span", undefined, "row-bot");
      bottom.append(node("span", item.preview || "Переписка ещё не началась", "row-prev"));
      if (item.last_direction === "out" && !item.unread && item.last_state) { const tick = node("span", undefined, "tick" + (item.last_state === "read" ? " read" : "")); tick.title = sendLabels[item.last_state] || ""; tick.append(icon(["read", "delivered"].includes(item.last_state) ? "checks" : "check")); bottom.append(tick); }
      body.append(bottom);
      const meta = node("span", undefined, "contact-meta");
      if (channels.length > 1) meta.append(node("small", item.channel_name, "muted"));
      if (meta.childNodes.length) body.append(meta);
      row.append(avatar, body); const wrap = node("div", undefined, "row-wrap");
      const action = node("div", "Обработано", "row-action"); action.prepend(icon("check"));
      wrap.append(action, row); list.append(wrap); wireSwipe(row, item);
    }
    scroller.scrollTop = top;
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
    let start = null, distance = 0;
    const checkpoint = item.seen_checkpoint;
    row.addEventListener("pointerdown", event => { if (event.pointerType !== "mouse" && item.unread && state.workspace?.role !== "observer") { start = event.clientX; distance = 0; } });
    row.addEventListener("pointermove", event => { if (start === null) return; distance = Math.min(0, event.clientX - start); row.style.transform = `translateX(${Math.max(-120, distance)}px)`; });
    const end = async () => {
      if (start === null) return; start = null; row.style.transform = "";
      if (distance > -70 || actionBusy) return;
      const epoch = state.epoch, gen = generation;
      actionBusy = true;
      try {
        const result = await api(`${chatPath(item.id)}/handled`, { method: "POST", data: { seen_checkpoint: checkpoint } });
        if (!valid(epoch, gen)) return;
        if (result.conversation) mergeMeta(result.conversation);
        renderList(true);
        await loadList(false, true);
        if (selected === item.id) await loadHistory(item.id);
      } catch (error) { if (valid(epoch, gen)) fail(error); }
      finally { if (valid(epoch, gen)) { actionBusy = false; renderHeader(); } }
    };
    row.addEventListener("pointerup", end); row.addEventListener("pointercancel", () => { start = null; row.style.transform = ""; });
  }
  function time(value) {
    const d = new Date(value); return Number.isNaN(d.getTime()) ? "" : new Intl.DateTimeFormat("ru-RU", { timeZone: state.workspace?.timezone, hour: "2-digit", minute: "2-digit" }).format(d);
  }
  const dayKey = value => {
    const d = new Date(value); if (Number.isNaN(d.getTime())) return "";
    return new Intl.DateTimeFormat("en-CA", { timeZone: state.workspace?.timezone, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
  };
  const shortDate = value => {
    const d = new Date(value); if (Number.isNaN(d.getTime())) return "";
    return new Intl.DateTimeFormat("en-GB", { timeZone: state.workspace?.timezone, day: "2-digit", month: "2-digit" }).format(d).replace("/", ".");
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
    if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 180) {
      loadList(true).catch(error => fail(error));
    }
  }
  async function loadList(append = false, preservePages = false) {
    if (append && (listLoading || listRetryRequired || !hasMore)) return;
    const epoch = state.epoch, gen = generation, seq = ++listSeq;
    const targetPage = append ? page + 1 : preservePages ? page : 0;
    // Refresh the whole loaded prefix in one server snapshot. Keeping stale
    // lower pages after messages move between unread/all skipped conversations.
    const params = new URLSearchParams({ through_page: targetPage });
    for (const [key, id] of [["q", "chat-search"], ["branch", "chat-branch"], ["status", "chat-status"], ["connection", "chat-channel"]]) if ($(`#${id}`).value) params.set(key, $(`#${id}`).value);
    listLoading = true;
    if (append) updateListLoader({ loading: true });
    try {
      const payload = await api(`${chatPath()}?${params}`);
      if (!valid(epoch, gen) || seq !== listSeq || !active()) return;
      items = payload.conversations;
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
      if (!selected && items.length) await select((items.find(item => item.unread) || items[0]).id, false);
      else if (selected && currentItem()) renderHeader();
      if (hasMore) requestAnimationFrame(() => maybeLoadMore());
    } catch (error) {
      if (append && valid(epoch, gen) && seq === listSeq) { listRetryRequired = true; updateListLoader({ failed: true }); }
      throw error;
    } finally {
      if (seq === listSeq) listLoading = false;
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
    $("#chat-handled").disabled = actionBusy;
    const unavailable = !data.meta?.can_send || state.workspace?.role === "observer";
    $("#chat-input").disabled = unavailable || Boolean(data.pending);
    if ($("#chat-templates")) $("#chat-templates").disabled = unavailable || Boolean(data.pending);
    const send = $("#chat-send");
    send.disabled = unavailable || historyBusy || (!data.pending && !$("#chat-input").value.trim());
    send.classList.toggle("is-retry", Boolean(data.pending));
    send.setAttribute("aria-label", data.pending ? "Проверить отправку" : "Отправить");
    send.title = data.pending ? "Проверить отправку" : "Отправить";
    const hint = $("#chat-send-hint");
    hint.hidden = !unavailable && !data.pending;
    hint.textContent = state.workspace?.role === "observer" ? "Роль «Наблюдатель» открывает переписку только для чтения." : unavailable ? "Проверьте подключение MAX, чтобы отправить ответ." : data.pending ? "Связь прервалась. Кнопка проверит тот же запрос без повторного сообщения." : "";
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
  function renderHistoryControls(data) {
    const position = rememberScroll();
    const more = $("#chat-history-more"), status = $("#chat-history-status");
    show(more, Boolean(data.more || data.olderError));
    more.disabled = Boolean(data.olderLoading);
    more.textContent = data.olderLoading ? "Загрузка истории…" : data.olderError ? "Повторить загрузку истории" : "Загрузить более ранние сообщения";
    show(status, Boolean(data.olderLoading || data.olderError));
    status.textContent = data.olderError || (data.olderLoading ? "Загружаем сообщения…" : "");
    $("#chat-stream").setAttribute("aria-busy", String(Boolean(data.olderLoading)));
    const total = Math.max(data.total || 0, data.messages.size);
    const count = `Показано сообщений: ${data.messages.size} из ${total}.`;
    const progress = data.meta?.history_error ? ` История AlfaCRM пока загружена не полностью. ${data.meta.history_error}`
      : data.meta?.history_loading ? " Загружаем более раннюю историю из AlfaCRM…" : "";
    $("#chat-history-note").textContent = count + progress;
    restoreScroll(position);
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
    renderHistoryControls(data);
    const renderKey = `${id}/${data.next}/${data.meta?.revision}/${data.messages.size}/${data.more}`;
    if (!bottom && !older && data.renderKey === renderKey) { renderHeader(); return; }
    data.renderKey = renderKey;
    const stream = $("#chat-stream"), position = rememberScroll();
    const history = $("#chat-history"); history.replaceChildren(); let day = "";
    const todayKey = dayKey(new Date());
    const messages = [...data.messages.values()].sort(compareMessages);
    for (let i = 0; i < messages.length; i++) {
      const message = messages[i];
      const messageDay = dayKey(message.occurred_at);
      const label = new Intl.DateTimeFormat("ru-RU", { timeZone: state.workspace?.timezone, day: "numeric", month: "long", year: "numeric" }).format(new Date(message.occurred_at));
      if (messageDay !== day) { history.append(node("div", messageDay === todayKey ? "Сегодня" : label, "chat-day sep")); day = messageDay; }
      if (message.actor_type === "automation") {
        const bubble = node("div", undefined, "chat-bubble system sys"); bubble.dataset.id = message.id;
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
        if (message.error) bubble.append(node("small", message.error, "chat-message-error"));
        history.append(bubble);
        continue;
      }
      const next = messages[i + 1], previous = messages[i - 1];
      const grouped = Boolean(next && next.actor_type !== "automation" && next.direction === message.direction && next.actor_type === message.actor_type && dayKey(next.occurred_at) === messageDay);
      const first = !previous || previous.actor_type !== message.actor_type || previous.direction !== message.direction || dayKey(previous.occurred_at) !== messageDay;
      const bubble = node("div", undefined, `chat-bubble msg ${message.direction === "out" ? "out outgoing" : "in incoming"}${first ? " first" : ""}${grouped ? "" : " last"}${message.state === "failed" ? " failed" : ""}`); bubble.dataset.id = message.id;
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
        status.append(icon(["delivered", "read"].includes(message.state) ? "checks" : ["queued", "sending", "unknown"].includes(message.state) ? "clock" : message.state === "failed" ? "warn" : "check"), node("span", sendLabels[message.state] || "", "sr")); meta.append(status);
      }
      content.append(meta); bubble.append(content);
      if (message.error) bubble.append(node("small", message.error, "chat-message-error"));
      history.append(bubble);
    }
    show($("#chat-blank"), false); show(stream); show($("#chat-compose"));
    resizeCompose();
    restoreScroll(position, { bottom, older, follow: true });
    renderHeader();
    requestAnimationFrame(maybeFillHistory);
  }
  function reconcilePending(data) {
    if (!data.pending) return;
    const canonical = [...data.messages.values()].find(m => m.request_id === data.pending.request_id);
    if (canonical) { data.pending = null; data.draft = ""; if (cache.get(selected) === data) { $("#chat-input").value = ""; resizeCompose(); } }
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
    const params = new URLSearchParams();
    if (mode === "delta" && data.loaded) params.set("after_seq", data.next);
    if (mode === "older") {
      // Live updates may include an ancient edited/backfilled row beyond an
      // unloaded interval. Only page responses advance the contiguous boundary.
      const first = data.oldest;
      if (!first || !data.more) return;
      params.set("before_time", first.occurred_at); params.set("before_id", first.id);
    }
    const result = await api(`${chatPath(id)}/messages?${params}`);
    if (!valid(epoch, gen) || cache.get(id) !== data || choice !== selectionSeq || selected !== id) return;
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
      await select(result.conversation.id, false);
      return;
    }
    if (result.reset) { data.messages.clear(); data.loaded = false; data.oldest = null; }
    for (const message of result.messages) {
      const old = data.messages.get(message.id); if (!old || old.version <= message.version) data.messages.set(message.id, message);
    }
    for (const removed of result.removed_ids || []) data.messages.delete(removed);
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
    if (result.more_changes && mode !== "older") await loadHistoryPage(id);
  }
  async function select(id, mobile = true) {
    const keepDetails = id !== selected && pinnedDocked();
    if (id !== selected && detailsDialog.open && !keepDetails) detailsDialog.close();
    if (keepDetails && detailsDialog.open) familyPending(currentItemFor(id)?.name);
    if (selected && cache.has(selected) && !cache.get(selected).pending) cache.get(selected).draft = $("#chat-input").value;
    selectionSeq += 1; selected = id; const data = entry(id); historyBusy = false;
    if (mobile) $("#chat-layout").classList.add("has-selection");
    $("#chat-input").value = data.draft; resizeCompose();
    renderList(); notice("", $("#chat-error"));
    if (data.loaded) renderMessages(id, { bottom: true });
    else {
      show($("#chat-blank"), false); show($("#chat-stream")); show($("#chat-compose"), false);
      $("#chat-history").replaceChildren(node("p", "Открываем переписку…", "empty"));
      $("#chat-person").replaceChildren(node("strong", currentItem()?.name || "MAX"));
    }
    try { await loadHistory(id, data.loaded ? "delta" : "initial"); }
    catch (error) { if (selected === id && active()) fail(error); }
    if (keepDetails && selected === id && active()) openDetails({ auto: true }).catch(error => fail(error));
  }
  async function enter() {
    const epoch = state.epoch, gen = generation;
    clearTimeout(timer); notice("", $("#chat-error"));
    try {
      const context = await api(`${chatPath()}/context`);
      if (!valid(epoch, gen) || !active()) return;
      channels = context.channels || [];
      const channel = $("#chat-channel"), chosen = channel.value;
      channel.replaceChildren(new Option("Все каналы MAX", ""), ...channels.map(c => new Option(c.name, c.id)));
      if (channels.some(c => c.id === chosen)) channel.value = chosen;
      const old = $("#chat-branch").value;
      const branches = context.branches || [];
      $("#chat-branch").replaceChildren(new Option("Все филиалы", ""), ...branches.map(b => new Option(b.name, b.id)));
      if (branches.some(b => String(b.id) === old)) $("#chat-branch").value = old;
      renderFilterChip();
      await loadList();
      if (selected && !currentItem()) await select(selected, false);
    } catch (error) { if (valid(epoch, gen)) fail(error); }
    finally { if (valid(epoch, gen)) schedule(); }
  }
  function schedule(delay = 2500) {
    clearTimeout(timer); if (!active()) return;
    timer = setTimeout(poll, delay);
  }
  async function poll() {
    if (!active()) return;
    if (document.hidden || pollBusy) return schedule(5000);
    const epoch = state.epoch, gen = generation;
    pollBusy = true;
    let delay = 2500;
    try {
      await loadList(false, true);
      if (selected && valid(epoch, gen) && active()) await loadHistory(selected);
      if (valid(epoch, gen)) { $("#chat-sync-state").textContent = "Обновлено · история на сервере"; notice("", $("#chat-error")); }
    } catch (error) { delay = 8000; if (valid(epoch, gen)) { fail(error); $("#chat-sync-state").textContent = "Ожидаем соединение…"; } }
    finally { if (valid(epoch, gen)) { pollBusy = false; schedule(delay); } }
  }
  async function submit() {
    const id = selected, data = cache.get(id); if (!data || historyBusy || !data.meta?.can_send) return;
    const content = $("#chat-input").value.trim(); if (!data.pending && !content) return;
    const epoch = state.epoch, gen = generation;
    data.draft = content;
    const payload = data.pending || { request_id: crypto.randomUUID(), text: content, seen_inbound_seq: data.meta.inbound_seq, seen_checkpoint: data.meta.seen_checkpoint };
    data.pending = payload; historyBusy = true; renderHeader(); let acknowledged = false;
    try {
      await api(`${chatPath(id)}/messages`, { method: "POST", data: payload });
      if (!valid(epoch, gen)) return;
      acknowledged = true; data.pending = null; data.draft = ""; if (selected === id) { $("#chat-input").value = ""; resizeCompose(); }
      await loadHistory(id); if (selected === id) renderMessages(id, { bottom: true }); await loadList();
    } catch (error) {
      if (!valid(epoch, gen)) return;
      // HTTP rejection is definite; loss of the HTTP response keeps the same UUID.
      if (!acknowledged && error.status && error.status < 500 && error.status !== 408) { data.pending = null; data.draft = payload.text; }
      if (selected === id) { $("#chat-input").value = data.draft; resizeCompose(); fail(error); }
      if (!acknowledged && error.status === 409) { try { await loadHistory(id); } catch (_) { /* Preserve the draft and error. */ } }
    } finally { if (valid(epoch, gen)) { historyBusy = false; renderHeader(); } }
  }
  function resizeCompose() {
    const input = $("#chat-input"); if (!input.clientWidth) return;
    const position = rememberScroll();
    input.style.height = "auto";
    input.style.height = `${Math.max(input.scrollHeight, 44)}px`;
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
    }).observe($("#chat-input").parentElement);
  }
  document.fonts?.addEventListener("loadingdone", resizeCompose);
  $("#chat-compose").addEventListener("submit", event => { event.preventDefault(); submit(); });
  $("#chat-input").addEventListener("input", () => { const data = cache.get(selected); if (data && !data.pending) data.draft = $("#chat-input").value; resizeCompose(); renderHeader(); unlockSound(); });
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
  $("#chat-stream").addEventListener("scroll", () => { if ($("#chat-stream").scrollTop <= 180) loadOlder(); }, { passive: true });
  $("#chat-stream").addEventListener("wheel", event => { if (event.deltaY < 0 && $("#chat-stream").scrollTop <= 180) loadOlder(); }, { passive: true });
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
    if (selected && cache.has(selected) && !cache.get(selected).pending) cache.get(selected).draft = $("#chat-input").value;
    selected = null; historyBusy = false; listLoading = false; listRetryRequired = false; hasMore = false; page = 0;
    updateListLoader();
    listSeq += 1; selectionSeq += 1; clearTimeout(searchTimer); notice("", $("#chat-error"));
    $("#chat-layout").classList.remove("has-selection"); $("#chat-input").value = "";
    $("#chat-history").replaceChildren(); show($("#chat-stream"), false); show($("#chat-compose"), false); show($("#chat-blank"));
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
  $("#chat-retry").addEventListener("click", () => { listRetryRequired = false; loadList(true).catch(error => fail(error)); });
  $("#chat-handled").addEventListener("click", markHandled);
  // The name plate always opens the card; the info button opens and closes it.
  document.addEventListener("cm:chat-details-open", () => {
    if (detailsDialog.open && !detailsDialog.classList.contains("is-closing")) return;
    openDetails().catch(error => fail(error));
  });
  $("#chat-details").addEventListener("click", () => {
    if (detailsDialog.open && detailsDialog.dataset.mode === "docked") closeDetails(); else openDetails().catch(error => fail(error));
  });
  $("#chat-go-connections").addEventListener("click", () => renderPane("connections").catch(error => fail(error)));
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
      loadList(false, true).catch(error => fail(error));
    } catch (error) { if (valid(epoch, gen)) { fail(error); if (error.status === 409) await loadHistory(id).catch(() => {}); } }
    finally { if (valid(epoch, gen)) { actionBusy = false; renderHeader(); } }
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
  const activeSubscriptions = cards => cards.flatMap(card => (card.subscriptions || []).filter(item => !item.archived && (!item.begin || item.begin <= familyToday()) && (!item.end || item.end >= familyToday())).map(item => ({ ...item, branch_name: card.branch_name })));
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
    if (activeSubscriptions(student.cards || []).length) return 2;
    if (groups.length) return 1;
    return 0;
  }
  const orderStudents = list => list.map((student, index) => ({ student, index, rank: attendance(student) }))
    .sort((a, b) => b.rank - a.rank || a.index - b.index).map(item => item.student);
  function familyStatus() {
    const all = (family.students || []).flatMap(student => (student.cards || []).map(card => card.status));
    return ["client", "lead", "archive"].filter(status => all.includes(status));
  }
  function renderFamily() {
    if (!family) return;
    detailsDialog.classList.remove("is-loading");
    $("#chat-details-name").textContent = family.name;
    const found = familyStatus(), main = found[0] || currentItem()?.status || "unlinked";
    const avatar = $("#chat-details-avatar"); avatar.textContent = initialsOf(family.name); avatar.className = `ava family-ava ${statuses[main] ? main : "unlinked"}`;
    const badges = $("#chat-details-badges"); badges.replaceChildren();
    for (const status of found) badges.append(chip(statuses[status], status));
    if (family.max_connected || family.max_bot_connected) { const max = chip("MAX", "max"); max.title = "Родитель подключён к MAX"; badges.append(max); }
    if (family.needs_review) badges.append(chip("Нужна проверка в CRM", "unlinked"));
    const contact = $("#chat-details-contact"); contact.replaceChildren();
    for (const phone of family.phones || []) contact.append(contactRow("phone", phone));
    for (const email of family.emails || []) contact.append(contactRow("email", email));
    if (!contact.childNodes.length) contact.append(node("p", "Телефон и почта в CRM не указаны.", "family-empty"));
    const students = orderStudents(family.students || []);
    const synced = family.snapshot_at ? new Date(family.snapshot_at) : null;
    const selected = students.find(student => student.id === familySelected) || students[0];
    const times = (selected?.cards || []).map(card => card.refreshed_at).filter(Boolean).sort();
    $("#chat-details-sync").textContent = times.length ? `Сведения из CRM обновлены ${date(times[0])}` : synced && !Number.isNaN(synced.getTime()) ? `Справочник CRM от ${date(family.snapshot_at)}` : "Справочник CRM";
    $("#chat-details-count").textContent = students.length > 1 ? String(students.length) : "";
    const list = $("#chat-details-children"), detail = $("#chat-details-student"); list.replaceChildren(); detail.replaceChildren();
    detailsDialog.querySelector(".family-kids-block").hidden = students.length < 2;
    if (!students.length) { detail.append(part("Дети", node("p", "В CRM у этого контакта нет детей.", "family-empty"))); return; }
    if (!students.some(student => student.id === familySelected)) familySelected = students[0].id;
    for (const student of students) {
      const chosen = student.id === familySelected, status = (student.cards || [])[0]?.status;
      const kid = button("", "family-kid", () => { if (familySelected === student.id) return; familySelected = student.id; renderFamily(); refreshSelected(false).catch(error => fail(error)); });
      kid.setAttribute("aria-pressed", String(chosen));
      kid.title = `${student.name}${statuses[status] ? ` · ${statuses[status]}` : ""}`;
      kid.append(firstName(student.name), node("small", familyAge(student.dob)));
      list.append(kid);
    }
    const student = students.find(person => person.id === familySelected), cards = student.cards || [];
    const today = familyToday(), subs = activeSubscriptions(cards), groups = student.groups || [];
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
    if (known(student.balance) && Number(student.balance) < 0) notes.append(note("wallet", `Задолженность ${familyMoney(Math.abs(Number(student.balance)))}`, "Баланс по абонементам отрицательный", "debt"));
    if (notes.childNodes.length) detail.append(notes);

    const paid = cards.filter(card => known(card.paid_lesson_count)), paidTotal = paid.reduce((sum, card) => sum + Number(card.paid_lesson_count), 0);
    const negative = known(student.balance) && Number(student.balance) < 0;
    detail.append(el("div", "family-stats",
      stat("Баланс", known(student.balance) ? familyMoney(student.balance) : "—", known(student.balance) ? "" : "требует уточнения", negative ? "negative" : ""),
      stat("Оплачено", paid.length ? `${paidTotal} ${plural(paidTotal, "занятие", "занятия", "занятий")}` : "—", paid.length ? "к посещению" : "нет данных"),
      stat("Абонементы", String(subs.length), subs.length ? plural(subs.length, "активный", "активных", "активных") : "нет активных")));

    if (subs.length) {
      const rows = el("div", "family-list");
      for (const item of subs) {
        const left = item.end ? daysBetween(today, item.end) : null, soon = left !== null && left <= 7;
        const until = item.end ? `до ${familyDate(item.end)}` : "срок не указан";
        rows.append(el("div", `family-item${soon ? " soon" : ""}`,
          el("div", "family-item-main", node("strong", subName(item)), node("small", `${item.branch_name} · ${until}`)),
          node("span", item.balance === null || item.balance === undefined ? "—" : familyMoney(item.balance), "family-item-value")));
      }
      detail.append(part("Абонементы", rows));
    }

    const groupCards = el("div", "family-groups");
    for (const group of groups) {
      const linked = (group.subscriptions || []).filter(item => !item.archived && (!item.begin || item.begin <= today) && (!item.end || item.end >= today)), previous = group.previous_lesson;
      const mark = !previous ? null : previous.attended === true ? node("span", "Был", "family-mark ok") : previous.attended === false ? node("span", "Не был", "family-mark miss") : node("span", "Не отмечено", "family-mark");
      const pay = linked.length ? linked.map(item => `${item.balance === null || item.balance === undefined ? "Остаток не указан" : familyMoney(item.balance)} · ${item.end ? `до ${familyDate(item.end)}` : "срок не указан"}`).join("\n") : "Не привязана к абонементу";
      const facts = el("dl", "family-facts",
        fact("Прошлое", previous ? node("span", dayLabel(previous.date, previous.time)) : node("span", "За год занятий не было", "family-muted"), mark),
        fact("Следующее", group.next_lesson ? node("span", dayLabel(group.next_lesson.date, group.next_lesson.time)) : node("span", "Не запланировано на 90 дней", "family-muted")),
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
        const lessons = known(card.paid_lesson_count) ? ` · оплачено ${card.paid_lesson_count} ${plural(Number(card.paid_lesson_count), "занятие", "занятия", "занятий")}` : "";
        rows.append(el("div", "family-item",
          el("div", "family-item-main", node("strong", card.branch_name || "Филиал не указан"), node("small", `${statuses[card.status] || "Статус не указан"}${lessons}`)),
          familyLink(card.url, `Открыть карточку в филиале ${card.branch_name} в АльфаCRM`)));
      }
      detail.append(part(cards.length > 1 ? "Филиалы" : "Филиал", rows));
    }
  }
  async function refreshSelected(force) {
    if (!isAdmin()) return;
    if (familyBusy || !family || !familySelected) return;
    const student = family.students.find(item => item.id === familySelected);
    if (!student) return;
    const target = student.cards.filter(card => force || card.needs_refresh).filter(card => !familyAttempted.has(`${student.id}:${card.branch_id}:${card.id}`) || force);
    if (!target.length) return;
    const epoch = state.epoch, gen = generation, seq = detailsSeq, conversation = selected;
    familyBusy = true; $("#chat-details-refresh").disabled = true; detailsDialog.classList.add("is-refreshing");
    try {
      for (const card of target) {
        if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || conversation !== selected) return;
        familyAttempted.add(`${student.id}:${card.branch_id}:${card.id}`);
        $("#chat-details-sync").textContent = `Обновляем сведения · ${card.branch_name}`;
        const updated = await api(`${base()}/directory/contacts/${family.id}/students/${student.id}/cards/${card.branch_id}/${card.id}/refresh`, { method: "POST" });
        if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || conversation !== selected) return;
        family = updated; renderFamily();
      }
    } catch (error) {
      if (valid(epoch, gen) && seq === detailsSeq && detailsDialog.open) { renderFamily(); toast(error.message, true); }
    } finally {
      familyBusy = false; $("#chat-details-refresh").disabled = !isAdmin(); detailsDialog.classList.remove("is-refreshing");
      if (familySelected !== student.id && valid(epoch, gen) && seq === detailsSeq && detailsDialog.open) refreshSelected(false).catch(error => fail(error));
    }
  }
  function familySkeleton() {
    $("#chat-details-avatar").textContent = ""; $("#chat-details-avatar").className = "ava family-ava me";
    $("#chat-details-badges").replaceChildren(); $("#chat-details-sync").textContent = ""; $("#chat-details-count").textContent = "";
    $("#chat-details-children").replaceChildren(); detailsDialog.querySelector(".family-kids-block").hidden = true;
    $("#chat-details-contact").replaceChildren(node("div", undefined, "family-skel"), node("div", undefined, "family-skel"));
    $("#chat-details-student").replaceChildren(node("div", undefined, "family-skel tall"), node("div", undefined, "family-skel"), node("div", undefined, "family-skel tall"));
  }
  function familyPending(name) {
    detailsSeq += 1; family = null; familySelected = null; familyAttempted.clear();
    $("#chat-details-name").textContent = name || "Загружаем сведения…";
    detailsDialog.classList.add("is-loading"); detailsDialog.classList.remove("is-refreshing"); familySkeleton();
  }
  function familyUnlinked() {
    const item = currentItem(), data = cache.get(selected), name = item?.name || data?.meta?.peer_name || "Контакт MAX";
    family = null; detailsDialog.classList.remove("is-loading");
    $("#chat-details-name").textContent = name;
    const avatar = $("#chat-details-avatar"); avatar.textContent = initialsOf(name); avatar.className = "ava family-ava unlinked";
    $("#chat-details-badges").replaceChildren(chip("Не связан с CRM", "unlinked"));
    $("#chat-details-contact").replaceChildren(node("p", "Диалог пока не связан с карточкой родителя в АльфаCRM, поэтому сведений о семье нет.", "family-empty"));
    $("#chat-details-sync").textContent = "Нет данных CRM"; $("#chat-details-count").textContent = "";
    $("#chat-details-children").replaceChildren(); detailsDialog.querySelector(".family-kids-block").hidden = true;
    const detail = $("#chat-details-student"); detail.replaceChildren();
    if (isAdmin()) { const link = button("Связать с CRM", "family-link-btn", () => openLink(selected)); detail.append(link); }
  }
  async function openDetails(options = {}) {
    const data = cache.get(selected), contactId = data?.meta?.contact_id;
    if (!data?.meta) return;
    if (!contactId && !options.auto && !pinnedDocked()) { openLink(selected); return; }
    const epoch = state.epoch, gen = generation, conversation = selected;
    const refresh = $("#chat-details-refresh"); refresh.hidden = !isAdmin() || !contactId; refresh.disabled = !isAdmin();
    familyPending(); const seq = detailsSeq;
    clearTimeout(closingTimer); detailsDialog.classList.remove("is-closing"); presentDetails();
    if (!contactId) { familyUnlinked(); return; }
    try {
      const result = await api(`${chatPath(conversation)}/family`);
      if (!valid(epoch, gen) || seq !== detailsSeq || !detailsDialog.open || conversation !== selected) return;
      family = result; renderFamily(); refreshSelected(false).catch(error => fail(error));
    } catch (error) {
      if (valid(epoch, gen) && seq === detailsSeq && detailsDialog.open) {
        detailsDialog.classList.remove("is-loading");
        $("#chat-details-name").textContent = error.message;
        $("#chat-details-contact").replaceChildren(); $("#chat-details-student").replaceChildren(node("p", "Сведения не загрузились. Попробуйте открыть карточку ещё раз.", "family-empty"));
      }
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
    if (docked) { chatLayout.append(detailsDialog); detailsDialog.show(); }
    else { document.body.append(detailsDialog); detailsDialog.showModal(); }
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
    detailsSeq += 1; family = null; renderPin();
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
      if (valid(epoch, gen)) { $("#chat-link-dialog").close(); await loadHistory(id); await loadList(); }
    } catch (error) { if (valid(epoch, gen)) fail(error, $("#chat-link-error")); }
    finally { if (valid(epoch, gen)) actionBusy = false; }
  }
  $("#chat-unlink").addEventListener("click", () => linkContact(null));
  $("#chat-link-dialog").addEventListener("close", () => { linkSeq += 1; clearTimeout(linkTimer); linkConversation = null; $("#chat-link-search").value = ""; $("#chat-link-results").replaceChildren(); });
  $("#crm-card-dialog").addEventListener("close", () => { cardSeq += 1; });

  // Only non-personal display preferences are kept across browser restarts.
  const defaults = { enabled: false, volume: 55, client: "soft", lead: "clear", archive: "low" };
  let sounds = { ...defaults };
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
    if (!sounds.enabled) return;
    try { soundContext ||= new AudioContext(); if (soundContext.state === "suspended") soundContext.resume().catch(() => {}); } catch (_) { /* Browser audio may be unavailable. */ }
  }
  function playTone(status = "client", preview = false) {
    if (!sounds.enabled && !preview) return;
    const tone = sounds[status === "unlinked" ? "lead" : status] || "soft"; if (tone === "off") return;
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
  $("#chat-settings-open").addEventListener("click", () => {
    $("#chat-sound-enabled").checked = sounds.enabled; $("#chat-volume").value = String(sounds.volume);
    for (const key of ["client", "lead", "archive"]) $(`#chat-tone-${key}`).value = sounds[key];
    unlockSound(); $("#chat-settings-dialog").showModal();
  });
  ["#chat-sound-enabled", "#chat-volume", "#chat-tone-client", "#chat-tone-lead", "#chat-tone-archive"].forEach(id => $(id).addEventListener("input", saveSounds));
  $("#chat-sound-test").addEventListener("click", async () => { saveSounds(); try { soundContext ||= new AudioContext(); await soundContext.resume(); playTone("client", true); } catch (_) { toast("Браузер не разрешил звук. Проверьте разрешение звука для сайта.", true); } });
  document.addEventListener("pointerdown", unlockSound, { passive: true });

  // A workspace change cursor covers every conversation, independently of list filters.
  function watch() {
    if (!state.user || !state.workspace || !(["owner", "admin"].includes(state.workspace.role) || state.workspace.sections?.includes("messages"))) { clearTimeout(notifyTimer); notifyTimer = null; return; }
    if (notifyEpoch !== state.epoch) { clearTimeout(notifyTimer); notifyTimer = null; notifySeq = null; notifyBusy = false; pollBusy = false; historyBusy = false; notifyEpoch = state.epoch; }
    if (!notifyBusy && (notifyTimer === null || notifyTimer === undefined)) notifyTimer = setTimeout(notifyPoll, 300);
  }
  async function notifyPoll() {
    const epoch = state.epoch, gen = generation;
    notifyTimer = null;
    if (!state.user || !state.workspace || !(["owner", "admin"].includes(state.workspace.role) || state.workspace.sections?.includes("messages"))) return;
    let delay = 3500; notifyBusy = true;
    try {
      const result = await api(`${base()}/chat-updates${notifySeq === null ? "" : `?after_seq=${notifySeq}`}`);
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
      if (result.has_more) delay = 150;
    } catch (error) {
      delay = 10000;
      if (error.status === 401 || error.status === 403) { if (valid(epoch, gen)) reset(); return; }
    } finally { if (valid(epoch, gen)) { notifyBusy = false; if (state.user && state.workspace) notifyTimer = setTimeout(notifyPoll, delay); } }
  }
  $("#chat-new-marker").addEventListener("click", async () => {
    const id = notifyTarget; if (!id) return;
    notifyTarget = null; show($("#chat-new-marker"), false);
    try { await renderPane("messages"); if (active()) await select(id); }
    catch (error) { if (!ignored(error)) toast(error.message, true); }
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
    clearTimeout(timer); selectionSeq += 1;
    // The docked card lives inside the Chats section and hides with it; a floating card
    // sits above every section, so it closes when the operator leaves Chats.
    if (detailsDialog.open && detailsDialog.dataset.mode !== "docked") detailsDialog.close();
  } };
};
