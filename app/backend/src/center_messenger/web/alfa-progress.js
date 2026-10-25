"use strict";
window.CenterAlfaProgress = function ({ $, state, api, show, date, onStatus = () => {} }) {
  let timer = null, generation = 0, pending = null, failures = 0, lastDetail = "";
  let lastPayload = null, lastFullAt = 0, selectedEvent = null, feedKey = "", mutationError = "";
  const number = value => new Intl.NumberFormat("ru-RU").format(Number(value) || 0);
  const node = (tag, value, className) => { const e = document.createElement(tag); if (value !== undefined) e.textContent = value; if (className) e.className = className; return e; };
  const crmIds = client => [...new Set((client?.cards || []).map(c => c.customer_id))].join(", ");
  const resultText = event => event.rolled_back ? "Не сохранено · повторим" : !event.committed ? "В обработке"
    : ({ imported: "Сообщение сохранено", checked: "Проверено", no_history: "Нет переписки", matched_send: "Отправка сопоставлена" })[event.result] || "Проверено";
  function clock(value) {
    if (!value || Number.isNaN(Date.parse(value))) return "—";
    return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: state.workspace?.timezone || "Europe/Moscow" }).format(new Date(value));
  }
  function freshness(payload) {
    const now = Date.parse(payload.server_at) || Date.now(), at = Date.parse(payload.worker?.updated_at);
    return { age: Number.isFinite(at) ? Math.max(0, now - at) : Infinity,
      fresh: Boolean(payload.worker?.live && Number.isFinite(at) && now - at < 90000) };
  }
  const phaseNames = { reading:"Читаем журнал AlfaCRM", saving:"Подготавливаем пакет", templates:"Получаем шаблоны уведомлений",
    waiting_db:"Ожидаем доступ к базе", projecting:"Сохраняем новые сообщения", committing:"Подтверждаем сохранение пакета",
    repair:"Проверяем связи клиентов по ID", waiting:"Ожидаем следующую проверку", error:"Повтор после ошибки" };
  function renderEntity(payload) {
    const worker = payload.worker || {}, events = worker.recent || [], fresh = freshness(payload).fresh;
    if (selectedEvent) selectedEvent = events.find(e => e.id === selectedEvent.id) || selectedEvent;
    const previous = !worker.client && !["reading","saving","templates","waiting_db","projecting","committing"].includes(worker.phase) ? events[0] : null;
    const event = selectedEvent || previous, current = !event && fresh && payload.syncing && worker.client && ["projecting","repair"].includes(worker.phase);
    const entity = event || worker, client = event?.client || worker.client || (!worker.phase ? worker.last_client : null);
    const flow = event ? event.flow || "archive" : worker.phase === "repair" ? "repair" : worker.flow || "archive";
    const slot = $(flow === "repair" ? "#alfa-loader-review-operation" : ["live", "catchup"].includes(flow) ? "#alfa-loader-live-operation" : "#alfa-loader-archive-operation");
    const panel = $("#alfa-max-progress-client");
    if (panel.parentElement !== slot) slot.append(panel);
    show(panel, Boolean(event || client || fresh && payload.syncing && !["waiting", "error"].includes(worker.phase)));

    $("#alfa-max-progress-client-label").textContent = selectedEvent ? "Выбранная операция" : current ? "Обрабатываем сейчас" : client ? "Последний клиент в отчёте" : "Текущая операция";
    $("#alfa-max-progress-client-live").textContent = fresh && !event ? "Сейчас на сервере" : "Последний отчёт";
    $("#alfa-max-progress-client-live").dataset.live = String(Boolean(fresh && !event));
    $("#alfa-max-progress-client-name").textContent = client?.name || phaseNames[worker.phase] || "Ожидаем следующий проход";
    $("#alfa-max-progress-client-subtitle").textContent = client ? crmIds(client) ? "ID AlfaCRM: " + crmIds(client) : "Клиент без подтверждённой связи" : payload.next_attempt_at && worker.phase === "waiting" ? "Следующая проверка: " + date(payload.next_attempt_at) : "Страница и сообщение обрабатываются отдельными этапами";
    $("#alfa-max-progress-client-avatar").textContent = client?.name ? client.name.trim().split(/\s+/).slice(0,2).map(v=>v[0]).join("").toUpperCase() : "↻";
    let operation = event ? resultText(event) : ({save_message:"Сохраняем новое сообщение",message_checked:"Запись обработана; подтверждаем сохранение",check_client:"Проверяем сохранённые связи по ID",client_checked:"Связь клиента проверена",waiting:"Сервер продолжит проверку новых сообщений автоматически"})[worker.operation] || phaseNames[worker.phase] || "Получаем состояние сервера";
    if (event?.checked_messages !== undefined) operation += " · сообщений: " + number(event.checked_messages);
    if (event?.restored_ids) operation += " · восстановлено ID: " + number(event.restored_ids);
    if (event?.hidden_duplicates) operation += " · сопоставлено повторов: " + number(event.hidden_duplicates);
    $("#alfa-max-progress-client-operation").textContent = operation;
    const facts = $("#alfa-max-progress-client-facts"), values = [], branch = (payload.streams||[]).find(v=>v.branch_id===String(entity.branch_id)&&v.direction===entity.direction);
    if (entity.branch_id) values.push(["Филиал",branch?.branch_name || "Филиал " + entity.branch_id]);
    if (entity.direction) values.push(["Направление",entity.direction==="sent"?"Исходящие":"Входящие"]);
    if (entity.page != null && (!client || entity.record_id)) values.push(["Страница журнала",number(entity.page+1)]);
    if (entity.record_id) values.push(["ID записи CRM",entity.record_id]);
    if (entity.position) values.push(["Запись в пакете",number(entity.position)+(entity.position_total?" из "+number(entity.position_total):"")]);
    if (entity.occurred_at) values.push(["Время сообщения",date(entity.occurred_at)]);
    if (event?.at || worker.updated_at) values.push([event ? "Время операции" : "Последний отчёт",clock(event?.at||worker.updated_at)]);
    facts.replaceChildren(...values.map(([label,value])=>{const e=node("div");e.append(node("dt",label),node("dd",value));return e;}));
    const cards=$("#alfa-max-progress-client-cards"); cards.replaceChildren(...(client?.cards||[]).map(c=>{const e=node("li");e.append(node("strong",c.name||"Карточка клиента"),node("span",`ID ${c.customer_id} · ${c.branch_name||"филиал "+c.branch_id}`));return e;}));
    show(cards,Boolean(client?.cards?.length));show($("#alfa-max-progress-client-back"),Boolean(selectedEvent));
  }
  function renderRecent(payload) {
    const events = payload.worker?.recent || [], key = JSON.stringify(events) + (selectedEvent?.id || "");
    if (key === feedKey) return; feedKey = key;
    const list = $("#alfa-max-progress-recent"), oldHeight = list.scrollHeight, oldScroll = list.scrollTop;
    const focus = document.activeElement?.dataset.event;
    $("#alfa-max-progress-recent-count").textContent = String(events.length);
    list.replaceChildren(...events.map(event => {
      const button = node("button", undefined, "sync-event"); button.type = "button"; button.dataset.event = event.id;
      button.dataset.state = event.rolled_back ? "error" : event.committed ? "done" : "pending";
      button.setAttribute("aria-pressed", String(event.id === selectedEvent?.id));
      const who = node("span", undefined, "sync-event-who"); who.append(node("strong", event.client?.name || "Клиент не указан"), node("small", crmIds(event.client) ? "ID AlfaCRM " + crmIds(event.client) : "Без ID клиента"));
      button.append(node("time", clock(event.at)), who, node("span", event.kind === "repair" ? "История клиента" : "Запись CRM " + event.record_id, "sync-event-what"), node("span", resultText(event), "sync-event-result")); return button;
    }));
    if (!events.length) list.append(node("p", "Последние операции появятся после обработки первого пакета.", "sync-event-empty"));
    if (oldScroll > 0) list.scrollTop = oldScroll + list.scrollHeight - oldHeight;
    if (focus) [...list.querySelectorAll("button")].find(b => b.dataset.event === focus)?.focus({ preventScroll: true });
  }
  const mutationKeys = new Set();
  const isActive = payload => payload.state === "active";
  const isFuture = (value, payload) => Date.parse(value) > (Date.parse(payload.server_at) || Date.now());
  const badStreams = payload => (payload.streams || []).filter(s => s.paused_until || s.error_code || s.error || s.live_error_code);
  const branchLabel = stream => stream.branch_name || "Филиал " + stream.branch_id;
  const journalLabel = stream => branchLabel(stream) + " · " + (stream.direction === "sent" ? "Исходящие" : "Входящие");
  function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function mode(value) {
    const target = $("#alfa-max-progress"); target.dataset.mode = value;
    for (const [name, enabled] of [["complete", value === "complete"], ["paused", value === "paused"], ["offline", value === "offline"]]) target.classList.toggle("is-" + name, enabled);
    $("#alfa-max-progress-badge").textContent = ({ running: "В работе", queued: "В очереди", paused: "Архив на паузе", attention: "Требует внимания", complete: "Проверено", offline: "Нет свежих данных", error: "Требуется вход", idle: "На связи", stale: "Нет свежего отчёта" })[value] || "Ожидание";
    show(target);
  }
  function reset() {
    generation += 1; clearTimer(); pending = null; failures = 0; lastDetail = ""; lastPayload = null;
    lastFullAt = 0; selectedEvent = null; feedKey = ""; mutationError = ""; mutationKeys.clear();
    for (const id of ["alfa-max-progress-streams", "alfa-max-progress-stats", "alfa-loader-history-alerts", "alfa-max-progress-track"]) $("#" + id).replaceChildren();
    for (const id of ["alfa-loader-live-note", "alfa-loader-cards-current", "alfa-loader-cards-error", "alfa-loader-review-detail", "alfa-max-progress-warning", "alfa-max-progress-detail", "alfa-max-progress-activity"]) $("#" + id).textContent = "";
    $("#alfa-loader-live-count").textContent = "—"; $("#alfa-loader-cards-value").textContent = "—";
    $("#alfa-max-progress-value").textContent = "—"; $("#alfa-loader-cards-queue").textContent = "";
    $("#alfa-loader-cards-fill").style.width = "0%"; show($("#alfa-loader-cards-track"), false);
    for (const id of ["alfa-max-progress-track", "alfa-loader-cards-track"])
      for (const attr of ["aria-valuenow", "aria-valuemin", "aria-valuemax", "aria-valuetext"]) $("#" + id).removeAttribute(attr);
    for (const id of ["alfa-loader-live-badge", "alfa-loader-history-badge", "alfa-loader-cards-badge"]) $("#" + id).textContent = "Ожидаем";
    $("#alfa-loader-repair-note").textContent = ""; show($("#alfa-loader-repair-note"), false);
    $("#alfa-max-progress-journals").open = false; $("#alfa-loader-review").open = false;
    renderEntity({}); renderRecent({}); show($("#alfa-max-progress"), false);
  }
  function schedule(delay) { clearTimer(); if (!document.hidden) timer = setTimeout(() => { timer = null; start(); }, delay); }
  function retryButton(stream) {
    const button = node("button", "Повторить этот журнал", "text-button"); button.type = "button";
    button.dataset.retryBranch = stream.branch_id; button.dataset.retryDirection = stream.direction;
    button.disabled = Boolean(lastPayload?.archive_paused || !isActive(lastPayload || {}) || mutationKeys.has("retry:" + stream.branch_id + ":" + stream.direction));
    return button;
  }
  function renderHistory(payload, busy) {
    const focused = document.activeElement?.closest("[data-retry-branch]");
    const focus = focused ? { branch: focused.dataset.retryBranch, direction: focused.dataset.retryDirection,
      alerts: Boolean(focused.closest("#alfa-loader-history-alerts")) } : null;
    const streams = payload.streams || [], passed = streams.filter(s => s.complete).length, problems = badStreams(payload);
    const loaded = payload.history_loaded ?? Boolean(streams.length && passed === streams.length);
    const paused = Boolean(payload.archive_paused);
    $("#alfa-max-progress-value").textContent = streams.length ? number(passed) + " из " + number(streams.length) + " журналов сохранено" : "Нет доступных журналов";
    $("#alfa-max-progress-caption").textContent = paused ? "История на паузе" : loaded ? "Обход завершён" : busy ? "Загружаем оставшиеся" : "Ожидаем следующую порцию";
    $("#alfa-loader-history-badge").textContent = paused ? "На паузе" : loaded ? "Сохранена" : problems.length ? "Журналов с задержкой: " + problems.length : busy ? "Загружается" : "В очереди";
    $("#alfa-loader-history-note").textContent = loaded
      ? "Первичная история сохранена. Старые журналы повторно не обходим; новые сообщения продолжают поступать."
      : paused ? "Архив продолжится с сохранённой позиции. Получение новых сообщений и обновление карточек включены."
      : "Однократная загрузка архива. Новые сообщения поступают независимо.";
    const repair = payload.client_repair, repairing = repair && !repair.complete;
    $("#alfa-loader-repair-note").textContent = repairing ? "Сопоставлено клиентов по ID: " + number(repair.processed)
      + (repair.total ? " из " + number(repair.total) + "." : ". Общий объём пока неизвестен.") + " Это проверка сохранённых связей, а не повторная загрузка журналов." : "";
    show($("#alfa-loader-repair-note"), Boolean(repairing));
    const track = $("#alfa-max-progress-track");
    if (streams.length) {
      track.setAttribute("aria-valuenow", String(passed)); track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", String(streams.length));
    } else for (const attr of ["aria-valuenow", "aria-valuemin", "aria-valuemax"]) track.removeAttribute(attr);
    track.setAttribute("aria-valuetext", streams.length ? passed + " из " + streams.length + " журналов сохранено" : "Нет доступных журналов");
    track.replaceChildren(...streams.map(s => {
      const segment = node("span"); segment.dataset.state = s.complete ? "done" : paused || s.paused_until || s.history_retry_at ? "waiting" : "active"; segment.setAttribute("aria-hidden", "true"); return segment;
    }));
    const pause = $("#alfa-loader-pause"); pause.textContent = paused ? "Продолжить историю" : "Приостановить историю";
    pause.setAttribute("aria-pressed", String(paused)); pause.disabled = !isActive(payload) || !streams.length || (loaded && !paused) || mutationKeys.has("pause");
    const alerts = $("#alfa-loader-history-alerts");
    alerts.replaceChildren(...problems.map(stream => {
      const line = node("div", undefined, "alfa-loader-alert");
      line.append(node("strong", journalLabel(stream) + (stream.live_error_code ? ": ошибка чтения новых" : ": ожидаем повтор")));
      const retry = stream.live_retry_at || stream.history_retry_at || stream.paused_until;
      const text = stream.error || (stream.live_error_code ? "Не удалось прочитать новые записи." : "Обход этого журнала временно приостановлен.");
      line.append(node("p", text + (paused ? " Продолжите архив для ручного повтора." : retry ? isFuture(retry, payload) ? " Следующая попытка после " + date(retry) + "." : " Повтор ожидает выполнения." : "") + " Остальные журналы продолжают работать."));
      line.append(retryButton(stream)); return line;
    }));
    $("#alfa-max-progress-journal-count").textContent = streams.length ? "(" + streams.length + ")" : "";
    $("#alfa-max-progress-streams").replaceChildren(...streams.map(stream => {
      const line = node("li"), head = node("div", undefined, "alfa-loader-stream-head");
      const problem = problems.includes(stream);
      head.append(node("strong", journalLabel(stream)), node("span", stream.complete ? "Сохранено" : paused ? "На паузе" : problem ? "Ожидает повтора" : "Загружается")); line.append(head);
      const facts = ["Уникальных ID в БД: " + number(stream.source_rows)];
      if (stream.expected_rows != null) facts.push("счётчик CRM: " + number(stream.expected_rows) + " строк");
      if (stream.checked_page != null) facts.push("последняя страница первичного обхода " + number(stream.checked_page + 1));
      if (stream.catchup_page) facts.push("догружаем новые: страница " + number(stream.catchup_page + 1));
      if (stream.new_messages_at) facts.push("новые проверены: " + clock(stream.new_messages_at));
      line.append(node("p", facts.join(" · ")));
      const proof = stream.verification || {};
      const proofText = ({ verified: "Полнота подтверждена по исходным ID.", legacy: "Прежний обход завершён; полная проверка исходных ID отдельно не подтверждена.", source_repeats: "В источнике повторяются ID: " + number(proof.duplicate_rows) + ".", limited: "Проверка ограничена: страницы или счётчики источника изменялись." })[proof.status];
      if (proofText) line.append(node("small", proofText));
      if (stream.missing_rows) line.append(node("small", "Прочитанных ID без сохранённой записи: " + number(stream.missing_rows)));
      if (stream.unidentified_rows) line.append(node("small", "Без исходного ID: " + number(stream.unidentified_rows)));
      if (problem) line.append(retryButton(stream));
      return line;
    }));
    if (focus) {
      const scope = $(focus.alerts ? "#alfa-loader-history-alerts" : "#alfa-max-progress-streams");
      [...scope.querySelectorAll("[data-retry-branch]")].find(b => b.dataset.retryBranch === focus.branch && b.dataset.retryDirection === focus.direction && !b.disabled)?.focus({ preventScroll: true });
    }
    return loaded;
  }
  function renderLive(payload, busy) {
    const streams = payload.streams || [], errors = streams.filter(s => s.live_error_code);
    const cycle = payload.live_cycle || payload.last_live_cycle;
    $("#alfa-loader-live-count").textContent = cycle ? number(cycle.added_records) : "—";
    $("#alfa-loader-live-count-label").textContent = cycle ? "записей добавлено при получении новых сообщений" : "Ожидаем первую проверку";
    $("#alfa-loader-live-badge").textContent = !isActive(payload) ? "Требуется вход" : errors.length ? "Есть задержка" : busy && ["live", "catchup"].includes(payload.worker?.flow) ? "Получаем" : "На связи";
    const lines = [];
    if (cycle) {
      lines.push("Проверено " + number(cycle.checked) + " из " + number(cycle.journals || streams.length) + " журналов");
      if (cycle.failed) lines.push("Не удалось прочитать: " + number(cycle.failed));
      if (cycle.completed_at) lines.push("Проход завершён в " + clock(cycle.completed_at));
      else if (cycle.started_at) lines.push("Текущий проход с " + clock(cycle.started_at));
    } else {
      const read = streams.filter(s => s.new_messages_at);
      lines.push(read.length ? "Журналов с успешным чтением: " + read.length + " из " + streams.length : "Ожидаем первое успешное чтение");
    }
    const last = streams.map(s => s.new_messages_at).filter(Boolean).sort().at(-1);
    if (last && !cycle) lines.push("Последний ответ: " + clock(last));
    if (!payload.syncing && payload.next_attempt_at) lines.push(isFuture(payload.next_attempt_at, payload) ? "Следующая проверка: " + clock(payload.next_attempt_at) : "Следующая проверка ожидает выполнения");
    $("#alfa-loader-live-note").replaceChildren(...lines.flatMap((text, i) => i ? [document.createElement("br"), document.createTextNode(text)] : [document.createTextNode(text)]));
  }
  function renderCards(payload, offline = false) {
    const cards = payload.cards, worker = offline ? null : payload.card_worker;
    const badge = $("#alfa-loader-cards-badge"), value = $("#alfa-loader-cards-value"), queue = $("#alfa-loader-cards-queue"), track = $("#alfa-loader-cards-track");
    badge.textContent = offline ? "Последние данные" : worker ? "Обновляются" : cards?.errors ? "Ожидают повтора" : cards?.total && cards.ready < cards.total ? "В очереди" : cards?.configured ? "Актуальны" : "Нужен справочник";
    value.textContent = !cards ? "Получаем состояние кэша" : !cards.configured ? "Сначала загрузите справочник" : cards.total ? number(cards.ready) + " из " + number(cards.total) + " клиентов готовы" : "Нет клиентов, ожидающих ответа";
    queue.textContent = cards?.queued ? "В очереди: " + number(cards.queued) : "";
    show(track, Boolean(cards?.total));
    for (const [attr, content] of [["aria-valuenow", cards?.ready], ["aria-valuemin", 0], ["aria-valuemax", cards?.total]]) {
      if (cards?.total) track.setAttribute(attr, String(content)); else track.removeAttribute(attr);
    }
    $("#alfa-loader-cards-fill").style.width = cards?.total ? Math.max(0, Math.min(100, cards.ready / cards.total * 100)) + "%" : "0%";
    const current = $("#alfa-loader-cards-current"); current.replaceChildren();
    if (worker) current.append(document.createTextNode(worker.manual ? "По запросу оператора: " : "Сейчас: "), node("strong", worker.name || "Клиент"), document.createTextNode(" · ID " + worker.customer_id + " · филиал " + worker.branch_id));
    else if (cards?.as_of) current.append(document.createTextNode("Состояние кэша на " + clock(cards.as_of)));
    const error = $("#alfa-loader-cards-error");
    error.textContent = cards?.errors ? "Не удалось обновить клиентов: " + number(cards.errors) + ". " + (cards.next_retry_at ? isFuture(cards.next_retry_at, payload) ? "Повтор после " + date(cards.next_retry_at) + ". " : "Повтор ожидает выполнения. " : "") + "Последние сохранённые сведения остаются доступны." : "";
    show(error, Boolean(cards?.errors));
  }
  function render(payload) {
    const streams = payload.streams || [], worker = payload.worker || {}, review = payload.active_review || {};
    const fresh = freshness(payload), active = isActive(payload);
    const busy = Boolean(active && payload.syncing && fresh.fresh && !["waiting", "error"].includes(worker.phase));
    const stale = Boolean(active && payload.syncing && !fresh.fresh);
    const proofIssues = streams.filter(s => s.complete && s.verification && s.verification.status !== "verified");
    const attention = Boolean(badStreams(payload).length || payload.unidentified_rows || proofIssues.length || review.error || review.unresolved || streams.some(s => s.missing_rows));
    const value = !active ? "error" : stale ? "stale" : payload.archive_paused ? "paused" : busy ? "running" : attention ? "attention" : payload.history_complete ? "complete" : payload.backfill_pending ? "queued" : "idle";
    mode(value);
    const loaded = renderHistory(payload, busy);
    renderLive(payload, busy); renderCards(payload);
    const title = !active ? "Нужно восстановить подключение MAX" : stale ? "Ожидаем свежий отчёт сервера" : busy ? phaseNames[worker.phase] || "Получаем новые сообщения" : loaded ? "История в базе · получаем новые сообщения" : "Первичная загрузка истории";
    $("#alfa-max-progress-text").textContent = title;
    $("#alfa-max-progress-note").textContent = !active ? payload.error || "Восстановите веб-сессию в настройках сервисов. Сохранённая история остаётся в базе." : "Новые сообщения, архив и карточки обновляются отдельными процессами.";
    const server = Date.parse(payload.server_at) || Date.now(), reviewAt = Date.parse(review.updated_at);
    const reviewStale = review.state === "running" && (!Number.isFinite(reviewAt) || server - reviewAt >= 90000);
    $("#alfa-loader-review-summary").textContent = reviewStale ? "Действующие клиенты · нет свежего отчёта" : review.state === "running" ? "Действующие клиенты · проверено " + number(review.processed) : review.next_at ? "Действующие клиенты · следующая сверка " + date(review.next_at) : "Действующие клиенты · сверка раз в 6 часов";
    const reviewNotes = [review.last_success_at ? "Последняя успешная сверка: " + date(review.last_success_at) + "." : "Ожидаем первую сверку.", "Только действующие клиенты; архив не входит в периодическую выборку."];
    if (review.last_client && review.state === "running" && !reviewStale) reviewNotes.push((review.last_client.name || "Клиент") + " · ID " + review.last_client.customer_id + " · филиал " + review.last_client.branch_id + ".");
    if (review.error) reviewNotes.push("Не удалось завершить сверку. Сервер повторит запрос.");
    if (review.unresolved) reviewNotes.push("Не найдены по ID: " + number(review.unresolved) + ". Требуется проверка.");
    $("#alfa-loader-review-detail").textContent = reviewNotes.join(" ");
    $("#alfa-active-review").disabled = !active || mutationKeys.has("review");
    const warnings = [];
    if (mutationError) warnings.push(mutationError);
    if (payload.unidentified_rows) warnings.push("Без исходного ID: " + number(payload.unidentified_rows) + " записей. Они сохранены, но требуют проверки.");
    if (proofIssues.length) warnings.push("Полнота импорта требует отдельного подтверждения в журналах: " + proofIssues.length + ". Это не запускает повторную загрузку архива.");
    if (streams.some(s => s.paused_until)) warnings.push("История отдельных журналов приостановлена после ошибок. Новые сообщения остальных журналов продолжают поступать.");
    if (stale) warnings.push("Нет свежего подтверждения работы. Показан последний отчёт, а не текущая операция.");
    if (reviewStale) warnings.push("Сверка действующих клиентов не прислала свежий отчёт.");
    $("#alfa-max-progress-warning").textContent = warnings.join(" "); show($("#alfa-max-progress-warning"), Boolean(warnings.length));
    const stats = $("#alfa-max-progress-stats");
    stats.replaceChildren(...[["messages", "Сообщений в переписках", payload.messages], ["stored", "Всего сохранённых записей", payload.stored_messages ?? payload.messages], ["conversations", "Диалогов", payload.conversations]].map(([key, label, count]) => {
      const cell = node("div"), strong = node("strong", count == null ? "—" : number(count)); cell.dataset.metric = key; strong.dataset.count = String(count ?? 0); cell.append(strong, node("span", label)); return cell;
    }));
    let movement = busy ? phaseNames[worker.phase] || "Обрабатываем записи" : payload.next_attempt_at ? "Следующий проход: " + date(payload.next_attempt_at) : "Ожидаем следующий серверный цикл";
    if (busy && worker.phase === "reading" && worker.page != null) movement += " · страница " + number(worker.page + 1);
    if (worker.updated_at) movement += ". Последнее движение: " + clock(worker.updated_at) + ".";
    $("#alfa-max-progress-activity").textContent = movement;
    lastDetail = "В переписках сообщений: " + payload.messages + " · Всего записей в базе: " + (payload.stored_messages ?? payload.messages) + " · Диалогов: " + payload.conversations;
    $("#alfa-max-progress-detail").textContent = (payload.last_sync_at ? "Последнее чтение CRM: " + date(payload.last_sync_at) : "Успешного чтения пока нет") + (payload.counters_as_of ? " · Счётчики на " + date(payload.counters_as_of) : "");
    renderEntity(payload); renderRecent(payload); onStatus(payload, { mode: value, title });
  }
  function start() {
    if (pending) return pending;
    const workspace = state.workspace, epoch = state.epoch, token = generation;
    if (!workspace || (state.pane && state.pane !== "connections")) { reset(); return Promise.resolve(); }
    clearTimer();
    const current = () => token === generation && epoch === state.epoch && workspace.id === state.workspace?.id && (!state.pane || state.pane === "connections");
    pending = (async () => {
      try {
        const activityOnly = Boolean(lastPayload && Date.now() - lastFullAt < 15000);
        const response = await api("/workspaces/" + workspace.id + "/integrations/alfa-max/sync-progress" + (activityOnly ? "?activity_only=true" : ""), { timeoutMs: 15000 });
        if (!current()) return;
        const payload = response.activity_only ? { ...lastPayload, ...response } : response;
        if (!activityOnly) lastFullAt = Date.now();
        if (!payload.configured || payload.state === "not_configured") { onStatus(payload, { mode: "idle", title: "Сначала подключите MAX" }); reset(); return; }
        failures = 0; lastPayload = payload; render(payload);
        const running = payload.syncing && freshness(payload).fresh || payload.card_worker || payload.active_review?.state === "running";
        schedule(isActive(payload) && running && state.connectionsView !== "settings" ? 1000 : 10000);
      } catch (error) {
        if (!current() || error.status === 401) return;
        if (error.status === 403) {
          reset();
          mode("offline"); $("#alfa-max-progress-text").textContent = "Для просмотра нужны права администратора.";
          $("#alfa-max-progress-detail").textContent = ""; $("#alfa-max-progress-warning").textContent = ""; show($("#alfa-max-progress-warning"), false);
          $("#alfa-max-sync").disabled = true; $("#alfa-loader-pause").disabled = true; $("#alfa-active-review").disabled = true;
          onStatus(null, { mode: "offline", title: "Нужны права администратора" }); return;
        }
        if (lastPayload) { renderEntity({ ...lastPayload, syncing: false, worker: { ...lastPayload.worker, live: false } }); renderCards(lastPayload, true); }
        mode("offline"); $("#alfa-max-progress-text").textContent = "Статус синхронизации временно недоступен";
        $("#alfa-max-progress-note").textContent = "Показаны последние полученные данные. Ошибка просмотра статуса не означает остановку сервера.";
        $("#alfa-loader-live-badge").textContent = "Последние данные"; $("#alfa-loader-history-badge").textContent = "Последние данные";
        failures += 1; const delay = Math.min(30000, 2000 * 2 ** Math.min(failures - 1, 4));
        $("#alfa-max-progress-detail").textContent = (lastDetail ? lastDetail + " · " : "") + "Повторяем запрос через " + delay / 1000 + " с.";
        onStatus(null, { mode: "offline", title: "Статус временно недоступен" }); schedule(delay);
      } finally { if (token === generation) pending = null; }
    })(); return pending;
  }
  async function refresh() {
    const token = generation;
    if (pending) await pending;
    if (token !== generation) return;
    lastFullAt = 0; return start();
  }
  async function requestTask(button, data, key) {
    const workspace = state.workspace, epoch = state.epoch, token = generation;
    if (!workspace || mutationKeys.has(key)) return;
    mutationKeys.add(key); button.disabled = true;
    mutationError = "";
    try {
      const result = await api("/workspaces/" + workspace.id + "/integrations/alfa-max/sync", { method: "POST", data });
      if (state.epoch !== epoch || token !== generation) return;
      if (lastPayload && result.archive_paused !== undefined) { lastPayload = { ...lastPayload, archive_paused: result.archive_paused }; render(lastPayload); }
      await refresh();
    } catch (error) {
      if (state.epoch === epoch && token === generation) {
        if (error.status === 403) {
          reset(); mode("offline"); $("#alfa-max-progress-text").textContent = "Для просмотра нужны права администратора.";
          $("#alfa-max-sync").disabled = true; $("#alfa-loader-pause").disabled = true; $("#alfa-active-review").disabled = true;
          onStatus(null, { mode: "offline", title: "Нужны права администратора" });
        } else mutationError = error.message;
      }
    } finally {
      mutationKeys.delete(key);
      if (state.epoch === epoch && token === generation) { button.disabled = false; if (lastPayload) render(lastPayload); }
    }
  }
  $("#alfa-max-progress").addEventListener("click", event => {
    const button = event.target.closest("[data-retry-branch]");
    if (button && !button.disabled) requestTask(button, { branch_id: Number(button.dataset.retryBranch), direction: button.dataset.retryDirection }, "retry:" + button.dataset.retryBranch + ":" + button.dataset.retryDirection);
  });
  $("#alfa-loader-pause").addEventListener("click", event => requestTask(event.currentTarget, { archive_paused: !lastPayload?.archive_paused }, "pause"));
  $("#alfa-active-review").addEventListener("click", event => requestTask(event.currentTarget, { review_active: true }, "review"));
  $("#alfa-max-progress-refresh").addEventListener("click", refresh);
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimer(); else if (state.pane === "connections") refresh(); });
  $("#alfa-max-progress-client-back").addEventListener("click", () => { selectedEvent = null; if (lastPayload) { renderEntity(lastPayload); renderRecent(lastPayload); } });
  $("#alfa-max-progress-recent").addEventListener("click", event => {
    const id = event.target.closest("[data-event]")?.dataset.event, entry = lastPayload?.worker?.recent?.find(e => e.id === id);
    if (entry) { selectedEvent = entry; renderEntity(lastPayload); renderRecent(lastPayload); $("#alfa-max-progress-client").scrollIntoView?.({ block: "nearest" }); }
  });
  return { start, reset };
};
