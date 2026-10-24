"use strict";
window.CenterChat = ({ $, state, api, node, show, notice, date, toast, confirm, renderPane }) => {
  const statuses = { client: "Клиент", lead: "Лид", archive: "Архивный", unlinked: "Связать с CRM" };
  const sendLabels = { queued: "В очереди", sending: "Отправляем…", accepted: "Принято MAX", delivered: "Доставлено", read: "Прочитано", failed: "Не отправлено", unknown: "Результат неизвестен", cancelled: "Отменено" };
  let generation = 0, listSeq = 0, selectionSeq = 0, linkSeq = 0, cardSeq = 0, detailsSeq = 0;
  let family = null, familySelected = null, familyBusy = false;
  const familyAttempted = new Set();
  let channels = [], items = [], selected = null, page = 0, hasMore = false, view = "all";
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
    pollBusy = false; actionBusy = false; historyBusy = false; listLoading = false; listRetryRequired = false; lastList = ""; linkConversation = null; tokenChannel = null; view = "all";
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
    show($("#chat-blank")); show($("#chat-stream"), false); show($("#chat-compose"), false);
    $("#chat-person").replaceChildren(node("strong", "Выберите диалог"), node("small", "Новые сообщения и ваши ответы"));
    document.querySelectorAll("[data-chat-view]").forEach(el => el.classList.toggle("active", el.dataset.chatView === view));
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
        const card = node("section", undefined, "max-channel");
        const heading = node("div", undefined, "max-channel-heading");
        const info = node("div"); info.append(node("strong", channel.name), node("small", channel.bot_name || "Доступ ещё не проверен"));
        const status = channel.can_send ? (channel.last_inbound_at ? "Приём включён" : "Ожидаем первое сообщение") : channel.error ? "Нужна проверка" : channel.verified ? "Доступ проверен" : "Токен сохранён";
        heading.append(info, node("span", status, `small-tag${channel.error ? " integration-error" : ""}`)); card.append(heading);
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
            tokenChannel = channel.id; const form = $("#max-token-form"); form.reset(); form.elements.name.value = channel.name;
            notice("", $("#max-token-error")); $("#max-token-dialog").showModal();
          }));
        actions.querySelectorAll("button").forEach(el => { el.disabled = actionBusy; }); card.append(actions); list.append(card);
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
      cache.set(id, { messages: new Map(), next: 0, more: false, loaded: false, meta: null, draft: "", pending: null });
    }
    return cache.get(id);
  }
  function renderList(force = false) {
    const signature = JSON.stringify([items, selected]); if (!force && signature === lastList) return; lastList = signature;
    const list = $("#chat-list"), scroller = $(".chat-list-scroll"), top = scroller.scrollTop; list.replaceChildren();
    for (const item of items) {
      const row = node("div", undefined, `chat-row${item.unread ? " unread" : ""}${item.id === selected ? " selected" : ""}`); row.dataset.id = item.id;
      const avatar = button("", `contact-avatar status-${item.status}`, () => openCards(item));
      avatar.title = item.contact_id ? `Открыть карточку АльфаCRM: ${item.name}` : "Связать диалог с контактом АльфаCRM"; avatar.setAttribute("aria-label", avatar.title);
      const letter = item.unread_count > 0 ? String(Math.min(item.unread_count, 99)) : item.name.trim().slice(0, 1).toLocaleUpperCase("ru");
      avatar.append(node("span", letter, "avatar-letter"), node("span", item.contact_id ? "↗" : "+", "avatar-open"));
      const body = button("", "chat-select", () => select(item.id)); body.setAttribute("aria-label", `Открыть переписку: ${item.name}`); body.setAttribute("aria-pressed", String(item.id === selected));
      const heading = node("span", undefined, "chat-row-heading"); heading.append(node("strong", item.name), node("time", item.last_message_at ? time(item.last_message_at) : ""));
      body.append(heading);
      if (item.students.length) body.append(node("span", item.students.join(", "), "contact-students"));
      body.append(node("span", item.preview || "Вложение или начало диалога", "chat-preview"));
      const meta = node("span", undefined, "contact-meta");
      if (channels.length > 1) meta.append(node("small", item.channel_name, "muted"));
      body.append(meta); row.append(avatar, body); list.append(row);
    }
    scroller.scrollTop = top;
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
  function mergeMeta(meta) {
    const item = items.find(item => item.id === meta.id); if (item) Object.assign(item, meta);
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
    const params = new URLSearchParams({ view, through_page: targetPage });
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
    const caption = item?.students?.join(", ") || data.meta?.channel_name || "MAX";
    person.append(node("small", caption));
    show($("#chat-handled"), Boolean(data.meta?.unread));
    show($("#chat-details"), Boolean(data.meta));
    $("#chat-handled").disabled = actionBusy;
    const unavailable = !data.meta?.can_send;
    $("#chat-input").disabled = unavailable || Boolean(data.pending);
    const send = $("#chat-send");
    send.disabled = unavailable || historyBusy;
    send.classList.toggle("is-retry", Boolean(data.pending));
    send.setAttribute("aria-label", data.pending ? "Проверить отправку" : "Отправить");
    send.title = data.pending ? "Проверить отправку" : "Отправить";
    const hint = $("#chat-send-hint");
    hint.hidden = !unavailable && !data.pending;
    hint.textContent = unavailable ? "Проверьте подключение MAX, чтобы отправить ответ." : data.pending ? "Связь прервалась. Кнопка проверит тот же запрос без повторного сообщения." : "";
  }
  function compareMessages(a, b) {
    const value = message => BigInt(message.sort_time ?? Math.trunc(new Date(message.occurred_at).getTime()) * 1000);
    const order = message => /^[0-9]{1,19}$/.test(String(message.crm_order ?? "")) ? BigInt(message.crm_order) : 0n;
    const at = value(a), bt = value(b), ao = order(a), bo = order(b);
    return at < bt ? -1 : at > bt ? 1 : ao < bo ? -1 : ao > bo ? 1 : a.id.localeCompare(b.id);
  }
  function renderMessages(id, { bottom = false, older = false } = {}) {
    const data = cache.get(id); if (id !== selected || !data) return;
    const renderKey = `${id}/${data.next}/${data.meta?.revision}/${data.messages.size}/${data.more}`;
    if (!bottom && !older && data.renderKey === renderKey) { renderHeader(); return; }
    data.renderKey = renderKey;
    const stream = $("#chat-stream"), previousHeight = stream.scrollHeight, previousTop = stream.scrollTop;
    const nearBottom = previousHeight - previousTop - stream.clientHeight < 100;
    const history = $("#chat-history"); history.replaceChildren(); let day = "";
    const todayKey = dayKey(new Date());
    const messages = [...data.messages.values()].sort(compareMessages);
    for (let i = 0; i < messages.length; i++) {
      const message = messages[i];
      const messageDay = dayKey(message.occurred_at);
      const label = new Intl.DateTimeFormat("ru-RU", { timeZone: state.workspace?.timezone, day: "numeric", month: "long" }).format(new Date(message.occurred_at));
      if (messageDay !== day) { history.append(node("div", label, "chat-day")); day = messageDay; }
      if (message.actor_type === "automation") {
        const bubble = node("div", undefined, "chat-bubble system"); bubble.dataset.id = message.id;
        const body = node("p", message.text || "", "system-body");
        body.hidden = true;
        const glyph = node("span", "+", "system-toggle-glyph");
        const toggle = button("", "system-toggle", () => {
          const opening = body.hidden;
          body.hidden = !opening;
          glyph.textContent = opening ? "−" : "+";
          toggle.setAttribute("aria-expanded", String(opening));
        });
        toggle.append(glyph);
        toggle.type = "button"; toggle.setAttribute("aria-expanded", "false");
        const when = (dayKey(message.occurred_at) !== todayKey ? shortDate(message.occurred_at) + " " : "") + time(message.occurred_at);
        const row = node("div", undefined, "system-row");
        const right = node("span", undefined, "system-right");
        right.append(node("time", when, "system-time"), toggle);
        row.append(
          node("span", `Системное сообщение: ${message.template_name || "Системное сообщение"}`, "system-label"),
          right
        );
        bubble.append(row, body);
        if (message.error) bubble.append(node("small", message.error, "chat-message-error"));
        history.append(bubble);
        continue;
      }
      const next = messages[i + 1];
      const grouped = Boolean(next && next.actor_type !== "automation" && next.direction === message.direction
        && dayKey(next.occurred_at) === messageDay);
      const bubble = node("div", undefined, `chat-bubble ${message.direction === "out" ? "outgoing" : "incoming"}${grouped ? "" : " has-tail"}`); bubble.dataset.id = message.id;
      if (message.forwarded) bubble.append(node("small", "Пересланное сообщение", "chat-forwarded"));
      bubble.append(node("p", message.deleted ? "Сообщение удалено" : message.text || (message.forwarded ? "Пересланное содержимое пока недоступно" : message.attachments.length ? "" : "Содержимое сообщения недоступно"), message.deleted ? "muted" : ""));
      if (!message.deleted) for (const [index, type] of message.attachments.entries()) {
        const names = { image: "Изображение", video: "Видео", audio: "Аудио", file: "Файл", contact: "Контакт", location: "Геопозиция", sticker: "Стикер" };
        bubble.append(node("span", `${message.attachment_names?.[index] || names[type] || "Вложение"} · открыть в MAX`, "chat-attachment"));
      }
      const meta = node("div", undefined, "chat-bubble-meta");
      if (message.edited && !message.deleted) meta.append(node("span", "изменено"));
      if (dayKey(message.occurred_at) !== todayKey) meta.append(node("time", shortDate(message.occurred_at), "chat-date"));
      meta.append(node("time", time(message.occurred_at)));
      if (message.direction === "out") meta.append(node("span", sendLabels[message.state] || "", `send-state state-${message.state}`));
      bubble.append(meta);
      if (message.error) bubble.append(node("small", message.error, "chat-message-error"));
      history.append(bubble);
    }
    show($("#chat-blank"), false); show(stream); show($("#chat-compose"));
    if (older) stream.scrollTop = previousTop + stream.scrollHeight - previousHeight;
    else if (bottom || nearBottom) stream.scrollTop = stream.scrollHeight;
    renderHeader();
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
      const first = [...data.messages.values()].sort(compareMessages)[0];
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
    if (result.reset) { data.messages.clear(); data.loaded = false; }
    for (const message of result.messages) {
      const old = data.messages.get(message.id); if (!old || old.version <= message.version) data.messages.set(message.id, message);
    }
    for (const removed of result.removed_ids || []) data.messages.delete(removed);
    // Earlier pages must not advance the live cursor past unseen intervening updates.
    if (mode !== "older") data.next = result.reset ? result.next_seq : Math.max(data.next, result.next_seq);
    if (!data.loaded || mode === "older") data.more = result.has_more;
    data.loaded = true;
    if (!data.meta || result.conversation.revision >= data.meta.revision) { data.meta = result.conversation; mergeMeta(result.conversation); }
    reconcilePending(data);
    renderMessages(id, { bottom: mode === "initial", older: mode === "older" }); renderList();
    if (result.more_changes && mode !== "older") await loadHistoryPage(id);
  }
  async function select(id, mobile = true) {
    if (id !== selected && $("#chat-details-dialog").open) $("#chat-details-dialog").close();
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
  }
  async function enter() {
    const epoch = state.epoch, gen = generation;
    clearTimeout(timer); notice("", $("#chat-error"));
    await connections();
    try {
      const alfa = await api(`${base()}/integrations/alfa`);
      if (!valid(epoch, gen) || !active()) return;
      const old = $("#chat-branch").value;
      $("#chat-branch").replaceChildren(new Option("Все филиалы", ""), ...alfa.branches.map(b => new Option(b.name, b.id)));
      if (alfa.branches.some(b => b.id === old)) $("#chat-branch").value = old;
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
    const payload = data.pending || { request_id: crypto.randomUUID(), text: content, seen_inbound_seq: data.meta.inbound_seq };
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
    const input = $("#chat-input");
    input.style.height = "auto";
    input.style.height = `${Math.min(Math.max(input.scrollHeight, 44), 160)}px`;
  }
  $("#chat-compose").addEventListener("submit", event => { event.preventDefault(); submit(); });
  $("#chat-input").addEventListener("input", () => { const data = cache.get(selected); if (data && !data.pending) data.draft = $("#chat-input").value; resizeCompose(); unlockSound(); });
  $("#chat-input").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing && matchMedia("(min-width: 761px)").matches) { event.preventDefault(); submit(); } });
  $("#chat-stream").addEventListener("scroll", () => {
    if (historyBusy || !selected || !active()) return;
    const stream = $("#chat-stream");
    if (stream.scrollTop > 180) return;
    const data = cache.get(selected);
    if (!data || !data.more) return;
    const epoch = state.epoch, gen = generation;
    historyBusy = true;
    loadHistory(selected, "older").catch(error => { if (valid(epoch, gen)) fail(error); })
      .finally(() => { if (valid(epoch, gen)) historyBusy = false; });
  }, { passive: true });
  function filtersChanged() {
    if (selected && cache.has(selected) && !cache.get(selected).pending) cache.get(selected).draft = $("#chat-input").value;
    selected = null; historyBusy = false; listLoading = false; listRetryRequired = false; hasMore = false; page = 0;
    updateListLoader();
    listSeq += 1; selectionSeq += 1; clearTimeout(searchTimer); notice("", $("#chat-error"));
    $("#chat-layout").classList.remove("has-selection"); $("#chat-input").value = "";
    $("#chat-history").replaceChildren(); show($("#chat-stream"), false); show($("#chat-compose"), false); show($("#chat-blank"));
    $("#chat-person").replaceChildren(node("strong", "Выберите диалог"));
    loadList().catch(error => fail(error));
  }
  $("#chat-search").addEventListener("input", () => { clearTimeout(searchTimer); listSeq += 1; searchTimer = setTimeout(filtersChanged, 250); });
  ["branch", "status"].forEach(id => $(`#chat-${id}`).addEventListener("change", filtersChanged));
  $("#chat-channel").addEventListener("change", () => { filtersChanged(); });
  document.querySelectorAll("[data-chat-view]").forEach(el => el.addEventListener("click", () => { view = el.dataset.chatView; document.querySelectorAll("[data-chat-view]").forEach(b => b.classList.toggle("active", b === el)); filtersChanged(); }));
  $(".chat-list-scroll").addEventListener("scroll", maybeLoadMore, { passive: true });
  $("#chat-retry").addEventListener("click", () => { listRetryRequired = false; loadList(true).catch(error => fail(error)); });
  $("#chat-handled").addEventListener("click", markHandled);
  $("#chat-details").addEventListener("click", openDetails);
  $("#chat-go-connections").addEventListener("click", () => renderPane("connections").catch(error => fail(error)));
  $("#chat-detach").addEventListener("click", () => { window.open("/?view=messages", "center-messenger-chat", "popup=yes,width=1280,height=860,resizable=yes,scrollbars=yes"); });
  $("#chat-list-toggle").addEventListener("click", () => { const layout = $("#chat-layout"); if (matchMedia("(max-width: 1100px)").matches) layout.classList.remove("has-selection"); else layout.classList.toggle("list-collapsed"); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active()) schedule(150); });

  async function markHandled() {
    const id = selected, item = currentItem(), data = cache.get(selected);
    if (!id || actionBusy || !data?.meta?.unread) return;
    const epoch = state.epoch, gen = generation;
    actionBusy = true; $("#chat-handled").disabled = true;
    try {
      await api(`${chatPath(id)}/handled`, { method: "POST" });
      if (!valid(epoch, gen)) return;
      if (item) { item.unread = false; item.handled = true; }
      data.meta.unread = false;
      if (view === "unread") items = items.filter(row => row.id !== id);
      renderList(true); renderHeader();
      toast("Диалог отмечен как обработанный.");
      loadList().catch(error => fail(error));
    } catch (error) { if (valid(epoch, gen)) fail(error); }
    finally { if (valid(epoch, gen)) { actionBusy = false; renderHeader(); } }
  }

  const crmUrl = value => {
    try { const url = new URL(value); return url.protocol === "https:" && /^[a-z0-9-]+\.s20\.online$/i.test(url.hostname) ? url.href : null; }
    catch (_) { return null; }
  };
  function familyAge(dob) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dob || "")) return "Дата не указана";
    const [year, month, day] = dob.split("-").map(Number), now = new Date();
    const age = now.getFullYear() - year - (now.getMonth() + 1 < month || (now.getMonth() + 1 === month && now.getDate() < day) ? 1 : 0);
    return age >= 0 && age < 120 ? `${age} лет` : "Дата не указана";
  }
  function familyLink(url, title, className = "family-open") {
    const href = crmUrl(url);
    if (!href) return node("span", "", className);
    const link = node("a", "↗", className); link.href = href; link.target = "center-messenger-crm"; link.rel = "noopener noreferrer"; link.title = title; link.setAttribute("aria-label", title);
    return link;
  }
  function familyMetric(label, value, title) {
    const cell = node("div", undefined, "family-metric");
    const strong = node("strong", value);
    if (title) strong.title = title;
    cell.append(node("span", label), strong); return cell;
  }
  const shortBranch = name => name.length > 20 ? name.slice(0, 20).trimEnd() + "…" : name;
  const familyNumber = value => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value));
  const familyMoney = value => `${familyNumber(value)} руб`;
  const familyDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? value.split("-").reverse().join(".") : null;
  const familyLesson = lesson => lesson ? `${familyDate(lesson.date)}${lesson.time ? ` · ${lesson.time}` : ""}` : null;
  function familyBalance(student) {
    return student.balance !== null && student.balance !== undefined ? familyMoney(student.balance) : "Баланс требует уточнения";
  }
  const activeSubscriptions = cards => cards.flatMap(card => (card.subscriptions || []).filter(item => !item.archived && (!item.begin || item.begin <= (family?.today || new Date().toISOString().slice(0, 10))) && (!item.end || item.end >= (family?.today || new Date().toISOString().slice(0, 10)))).map(item => ({ ...item, branch_name: card.branch_name })));
  function renderFamily() {
    if (!family) return;
    $("#chat-details-name").textContent = family.name;
    const contact = $("#chat-details-contact"); contact.replaceChildren();
    for (const phone of family.phones || []) contact.append(node("span", `☎ ${phone}`));
    for (const email of family.emails || []) contact.append(node("span", `✉ ${email}`));
    if (!contact.childNodes.length) contact.append(node("span", "Контакты не указаны"));
    const synced = family.snapshot_at ? new Date(family.snapshot_at) : null;
    const selected = (family.students || []).find(student => student.id === familySelected);
    const times = (selected?.cards || []).map(card => card.refreshed_at).filter(Boolean).sort();
    $("#chat-details-sync").textContent = times.length ? `Сведения CRM · ${date(times[0])}` : synced && !Number.isNaN(synced.getTime()) ? `Справочник CRM · ${date(family.snapshot_at)}` : "Справочник CRM";
    const list = $("#chat-details-children"); list.replaceChildren();
    const students = family.students || [];
    if (!students.length) { list.append(node("p", "Дети не найдены в CRM.", "muted")); $("#chat-details-student").replaceChildren(); return; }
    if (!students.some(student => student.id === familySelected)) familySelected = students[0].id;
    for (const student of students) {
      const row = node("div", undefined, `family-child${student.id === familySelected ? " selected" : ""}`);
      const select = button("", "family-child-select", () => { familySelected = student.id; renderFamily(); refreshSelected(false).catch(error => fail(error)); });
      select.setAttribute("aria-pressed", String(student.id === familySelected));
      const courses = [...new Set((student.groups || []).map(group => group.name))].join(", ") || "Группа не определена";
      const info = node("span", undefined, "family-child-info"); info.append(node("strong", student.name), node("small", `${familyAge(student.dob)} · ${courses}`));
      select.append(info); row.append(select, familyLink(student.cards?.[0]?.url, `Открыть ${student.name} в АльфаCRM`)); list.append(row);
    }
    const student = students.find(person => person.id === familySelected), detail = $("#chat-details-student"); detail.replaceChildren();
    detail.append(node("h3", student.name));
    const branches = [...new Set((student.cards || []).map(card => card.branch_name))];
    const subs = activeSubscriptions(student.cards || []);
    const subscriptions = node("div", undefined, "family-subscriptions");
    subscriptions.hidden = true;
    for (const item of subs) subscriptions.append(node("div", `${item.name || `Абонемент №${item.tariff_id}`} · ${item.branch_name} · ${item.balance === null ? "остаток не указан" : familyMoney(item.balance)}${item.end ? ` · до ${familyDate(item.end)}` : ""}`));
    let subscriptionsTile;
    if (subs.length) {
      subscriptionsTile = familyMetric("Абонементы", String(subs.length));
      subscriptionsTile.classList.add("family-metric-button");
      subscriptionsTile.setAttribute("role", "button");
      subscriptionsTile.setAttribute("tabindex", "0");
      subscriptionsTile.setAttribute("aria-pressed", "false");
      const toggleSubscriptions = () => {
        const opening = subscriptions.hidden;
        subscriptions.hidden = !opening;
        subscriptionsTile.classList.toggle("open", opening);
        subscriptionsTile.setAttribute("aria-pressed", String(opening));
      };
      subscriptionsTile.addEventListener("click", toggleSubscriptions);
      subscriptionsTile.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleSubscriptions(); } });
    } else {
      subscriptionsTile = familyMetric("Абонементы", "0");
    }
    const metrics = node("div", undefined, "family-metrics");
    metrics.append(familyMetric("Возраст", familyAge(student.dob)), subscriptionsTile, familyMetric("Общий баланс", familyBalance(student)), familyMetric("Филиалы", branches.map(shortBranch).join(", ") || "Филиал не указан", branches.join(", ")));
    detail.append(metrics);
    const paid = (student.cards || []).filter(card => card.paid_lesson_count !== null && card.paid_lesson_count !== undefined);
    if (paid.length) detail.append(node("p", `Остаток занятий: ${paid.map(card => `${card.branch_name} — ${card.paid_lesson_count}`).join(" · ")}`, "family-summary"));
    if (subs.length) detail.append(subscriptions);
    detail.append(node("h4", "Группы и абонементы"));
    const groups = student.groups || [];
    if (!groups.length) { detail.append(node("span", "ГРУППА НЕ ОПРЕДЕЛЕНА", "family-unknown")); return; }
    for (const group of groups) {
      const block = node("article", undefined, "family-group");
      const title = node("div", undefined, "family-group-heading");
      const name = node("span"); name.append(node("strong", group.name), node("small", group.branch_name || "Филиал не указан"));
      title.append(familyLink(group.url, `Открыть группу ${group.name} в АльфаCRM`), name); block.append(title);
      const facts = node("div", undefined, "family-group-facts");
      const today = family.today || new Date().toISOString().slice(0, 10);
      const linked = (group.subscriptions || []).filter(item => !item.archived && (!item.begin || item.begin <= today) && (!item.end || item.end >= today)), previous = group.previous_lesson;
      const attend = previous ? `${familyLesson(previous)} · ${previous.attended === true ? "был" : previous.attended === false ? "не был" : "посещение не отмечено"}` : "За год занятий нет";
      const remaining = linked.length ? linked.map(item => item.balance === null ? "Остаток не указан" : familyMoney(item.balance)).join("\n") : "Нет связи с абонементом";
      const until = linked.length ? linked.map(item => familyDate(item.end) || "Срок не указан").join("\n") : "Срок не указан";
      const balanceMetric = familyMetric("Остаток на балансе", remaining), untilMetric = familyMetric("Оплачено до", until);
      const subscriptionHint = linked.map(item => `${item.name || `Абонемент №${item.tariff_id}`}: ${item.balance === null ? "остаток не указан" : familyMoney(item.balance)}${item.end ? ` · до ${familyDate(item.end)}` : ""}`).join("\n");
      balanceMetric.title = subscriptionHint; untilMetric.title = subscriptionHint;
      facts.append(familyMetric("На прошлом занятии", attend), familyMetric("Ближайшее занятие", familyLesson(group.next_lesson) || "На 90 дней не запланировано"), balanceMetric, untilMetric);
      block.append(facts); detail.append(block);
    }
  }
  async function refreshSelected(force) {
    if (familyBusy || !family || !familySelected) return;
    const student = family.students.find(item => item.id === familySelected);
    if (!student) return;
    const target = student.cards.filter(card => force || card.needs_refresh).filter(card => !familyAttempted.has(`${student.id}:${card.branch_id}:${card.id}`) || force);
    if (!target.length) return;
    const epoch = state.epoch, gen = generation, seq = detailsSeq, conversation = selected;
    familyBusy = true; $("#chat-details-refresh").disabled = true;
    try {
      for (const card of target) {
        if (!valid(epoch, gen) || seq !== detailsSeq || !$("#chat-details-dialog").open || conversation !== selected) return;
        familyAttempted.add(`${student.id}:${card.branch_id}:${card.id}`);
        $("#chat-details-sync").textContent = `Обновляем сведения · ${card.branch_name}`;
        const updated = await api(`${base()}/directory/contacts/${family.id}/students/${student.id}/cards/${card.branch_id}/${card.id}/refresh`, { method: "POST" });
        if (!valid(epoch, gen) || seq !== detailsSeq || !$("#chat-details-dialog").open || conversation !== selected) return;
        family = updated; renderFamily();
      }
    } catch (error) {
      if (valid(epoch, gen) && seq === detailsSeq && $("#chat-details-dialog").open) { renderFamily(); toast(error.message, true); }
    } finally {
      familyBusy = false; $("#chat-details-refresh").disabled = false;
      if (familySelected !== student.id && valid(epoch, gen) && seq === detailsSeq && $("#chat-details-dialog").open) refreshSelected(false).catch(error => fail(error));
    }
  }
  async function openDetails() {
    const data = cache.get(selected), contactId = data?.meta?.contact_id;
    if (!data?.meta) return;
    if (!contactId) { openLink(selected); return; }
    const epoch = state.epoch, gen = generation, seq = ++detailsSeq, conversation = selected;
    const dialog = $("#chat-details-dialog"); family = null; familySelected = null; familyAttempted.clear();
    $("#chat-details-name").textContent = "Загружаем сведения…";
    $("#chat-details-contact").replaceChildren(); $("#chat-details-sync").textContent = "";
    $("#chat-details-children").replaceChildren(); $("#chat-details-student").replaceChildren();
    if (!dialog.open) dialog.showModal();
    try {
      const result = await api(`${base()}/directory/contacts/${contactId}`);
      if (!valid(epoch, gen) || seq !== detailsSeq || !dialog.open || conversation !== selected) return;
      family = result; renderFamily(); refreshSelected(false).catch(error => fail(error));
    } catch (error) { if (valid(epoch, gen) && seq === detailsSeq && dialog.open) $("#chat-details-name").textContent = error.message; }
  }
  $("#chat-details-refresh").addEventListener("click", () => refreshSelected(true).catch(error => fail(error)));
  $("#chat-details-dialog").addEventListener("close", () => { detailsSeq += 1; family = null; });
  async function openCards(item) {
    if (!item.contact_id) return openLink(item.id);
    const epoch = state.epoch, gen = generation, seq = ++cardSeq;
    const dialog = $("#crm-card-dialog"), options = $("#crm-card-options");
    options.replaceChildren(node("p", "Загружаем карточки…", "muted")); if (!dialog.open) dialog.showModal();
    try {
      const contact = await api(`${base()}/directory/contacts/${item.contact_id}`);
      if (!valid(epoch, gen) || seq !== cardSeq || !dialog.open) return;
      options.replaceChildren();
      for (const student of contact.students) for (const card of student.cards) {
        const url = new URL(card.url); if (url.protocol !== "https:" || !/^[a-z0-9-]+\.s20\.online$/i.test(url.hostname)) continue;
        const link = node("a", undefined, "crm-card-link"); link.href = url.href; link.target = "center-messenger-crm"; link.rel = "noopener noreferrer";
        link.append(node("strong", student.name), node("small", `${card.branch_name} · ${statuses[card.status]}`), node("span", "↗", "crm-link-arrow")); options.append(link);
      }
    } catch (error) { if (valid(epoch, gen) && seq === cardSeq && dialog.open) options.replaceChildren(node("p", error.message, "notice error")); }
  }
  async function openLink(id) {
    if (!id) return;
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
    if (!state.user || !state.workspace || !["owner", "admin"].includes(state.workspace.role)) { clearTimeout(notifyTimer); notifyTimer = null; return; }
    if (notifyEpoch !== state.epoch) { clearTimeout(notifyTimer); notifyTimer = null; notifySeq = null; notifyBusy = false; pollBusy = false; historyBusy = false; notifyEpoch = state.epoch; }
    if (!notifyBusy && (notifyTimer === null || notifyTimer === undefined)) notifyTimer = setTimeout(notifyPoll, 300);
  }
  async function notifyPoll() {
    const epoch = state.epoch, gen = generation;
    notifyTimer = null;
    if (!state.user || !state.workspace || !["owner", "admin"].includes(state.workspace.role)) return;
    let delay = 3500; notifyBusy = true;
    try {
      const result = await api(`${base()}/chat-updates${notifySeq === null ? "" : `?after_seq=${notifySeq}`}`);
      if (!valid(epoch, gen)) return;
      notifySeq = result.next_seq;
      let latest = null;
      for (const update of result.updates) {
        const data = cache.get(update.id);
        if (data?.meta) Object.assign(data.meta, { unread: update.unread, inbound_seq: update.inbound_seq });
        mergeMeta(update);
        if (update.new_inbound) latest = update;
      }
      if (latest) {
        notifyTarget = latest.id;
        const marker = $("#chat-new-marker"); marker.textContent = `Новое: ${latest.name}`; marker.title = `Открыть новое сообщение: ${latest.name}`; show(marker);
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
  return { reset, connections, enter, watch, leave: () => { clearTimeout(timer); selectionSeq += 1; } };
};
