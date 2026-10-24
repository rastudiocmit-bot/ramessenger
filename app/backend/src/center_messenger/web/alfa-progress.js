"use strict";
window.CenterAlfaProgress = function ({ $, state, api, show, date, onStatus = () => {} }) {
  let timer = null, generation = 0, pending = null, failures = 0, lastDetail = "", autoExpanded = false;
  let lastPayload = null, activityReads = 0, selectedEvent = null, feedKey = "";
  const number = value => new Intl.NumberFormat("ru-RU").format(Number(value) || 0);
  const node = (tag, value, className) => { const e = document.createElement(tag); if (value !== undefined) e.textContent = value; if (className) e.className = className; return e; };
  const crmIds = client => [...new Set((client?.cards || []).map(c => c.customer_id))].join(", ");
  const resultText = event => event.rolled_back ? "Не сохранено · повторим" : !event.committed ? "В обработке"
    : ({ imported: "Сообщение сохранено", checked: "Проверено", no_history: "Нет переписки", matched_send: "Отправка сопоставлена" })[event.result] || "Проверено";
  function clock(value) {
    if (!value || Number.isNaN(Date.parse(value))) return "—";
    return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: state.workspace?.timezone || "Europe/Moscow" }).format(new Date(value));
  }
  function renderEntity(payload) {
    const worker = payload.worker || {}, events = worker.recent || [];
    if (selectedEvent) selectedEvent = events.find(e => e.id === selectedEvent.id) || selectedEvent;
    const event = selectedEvent;
    const previous = !worker.client && !["reading", "saving", "projecting"].includes(worker.phase) ? events[0] : null;
    const detailEvent = event || previous, entity = detailEvent || worker;
    const current = !event && payload.syncing && worker.live && worker.client && !["waiting", "error"].includes(worker.phase);
    const client = detailEvent?.client || worker.client || (!worker.phase ? worker.last_client : null);
    const branch = (payload.streams || []).find(s => s.branch_id === String(entity.branch_id));
    const label = event ? "Выбранная операция" : current ? "Обрабатываем сейчас" : client ? "Последний обработанный клиент" : "Текущая операция";
    $("#alfa-max-progress-client-label").textContent = label;
    $("#alfa-max-progress-client-live").textContent = current ? "Сейчас на сервере" : "Последний отчёт";
    $("#alfa-max-progress-client-live").dataset.live = String(Boolean(current));
    $("#alfa-max-progress-client-name").textContent = client?.name || ({ reading: "Читаем журнал AlfaCRM", saving: "Готовим сохранение сообщений", projecting: "Сохраняем сообщения", repair: "Проверяем справочник клиентов", error: "Операция приостановлена" })[worker.phase] || "Ожидаем следующий проход";
    $("#alfa-max-progress-client-subtitle").textContent = client ? crmIds(client) ? "ID AlfaCRM: " + crmIds(client) : "В исходной записи клиент не указан"
      : "Клиент появится здесь после чтения страницы CRM.";
    $("#alfa-max-progress-client-avatar").textContent = client?.name ? client.name.trim().split(/\s+/).slice(0, 2).map(s => s[0]).join("").toUpperCase() : "↻";
    const operation = detailEvent ? resultText(detailEvent) : ({ check_client: "Проверяем сохранённую историю клиента", client_checked: "Клиент проверен · подтверждаем сохранение пакета", save_message: "Сопоставляем и сохраняем сообщение", message_checked: "Запись обработана · подтверждаем сохранение прохода", read_page: "Читаем страницы CRM", waiting: "Проход завершён. Ожидаем следующую проверку.", error: "Операция остановлена. Причина указана в состоянии загрузки." })[worker.operation] || "Получаем подробности операции";
    let summary = !detailEvent && !client && worker.phase === "saving" ? "Чтение завершено. Подготавливаем проверку и запись сообщений." : operation;
    if (detailEvent && detailEvent.checked_messages !== undefined) summary += ` · сообщений: ${number(detailEvent.checked_messages)}`;
    if (detailEvent?.restored_ids) summary += ` · восстановлено ID: ${number(detailEvent.restored_ids)}`;
    if (detailEvent?.hidden_duplicates) summary += ` · убрано повторов: ${number(detailEvent.hidden_duplicates)}`;
    if (detailEvent?.mirrors_updated) summary += ` · сверено копий филиалов: ${number(detailEvent.mirrors_updated)}`;
    $("#alfa-max-progress-client-operation").textContent = summary;
    const facts = $("#alfa-max-progress-client-facts"); facts.replaceChildren();
    const isRepair = detailEvent ? detailEvent.kind === "repair" : worker.phase === "repair";
    for (const [title, value] of [[isRepair ? "Клиент по счёту" : "Запись на странице", entity.position ? `${number(entity.position)}${entity.position_total ? " из " + number(entity.position_total) : ""}` : "—"],
      ["ID записи CRM", entity.record_id || "—"], ["Филиал журнала", isRepair ? "Проверка клиентов" : branch?.branch_name || (entity.branch_id ? "Филиал " + entity.branch_id : "—")],
      ["Страница журнала", !isRepair && entity.page !== undefined && entity.page !== null ? number(entity.page + 1) : "—"],
      ["Направление", isRepair ? "Вся переписка" : entity.message_direction === "out" || entity.direction === "sent" ? "Исходящие" : entity.message_direction === "in" || entity.direction === "receive" ? "Входящие" : "—"],
      ["Время операции", clock(detailEvent?.at || worker.updated_at)]]) {
      const pair = node("div"); pair.append(node("dt", title), node("dd", value)); facts.append(pair);
    }
    if (entity.occurred_at) { const pair = node("div"); pair.append(node("dt", "Время сообщения"), node("dd", date(entity.occurred_at))); facts.append(pair); }
    const cards = $("#alfa-max-progress-client-cards"); cards.replaceChildren(...(client?.cards || []).map(c => {
      const li = node("li"); li.append(node("strong", c.name || "Карточка клиента"), node("span", `ID ${c.customer_id} · ${c.branch_name || "филиал " + c.branch_id}`)); return li;
    }));
    show(cards, Boolean(client?.cards?.length)); show($("#alfa-max-progress-client-back"), Boolean(event));
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
    if (!events.length) list.append(node("p", "Операции появятся после следующего прохода. Предыдущие имена и ID не восстанавливаем по догадкам.", "sync-event-empty"));
    if (oldScroll > 0) list.scrollTop = oldScroll + list.scrollHeight - oldHeight;
    if (focus) [...list.querySelectorAll("button")].find(b => b.dataset.event === focus)?.focus({ preventScroll: true });
  }
  function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function reset() {
    generation += 1; clearTimer(); pending = null; failures = 0; lastDetail = ""; autoExpanded = false;
    lastPayload = null; activityReads = 0; selectedEvent = null; feedKey = "";
    renderEntity({}); renderRecent({});
    show($("#alfa-max-progress"), false); $("#alfa-max-progress-streams").replaceChildren();
    $("#alfa-max-progress-stats").replaceChildren(); $("#alfa-max-progress-journals").open = false;
    meter("Обход журналов", 0, 0); $("#alfa-max-progress-warning").textContent = "";
    show($("#alfa-max-progress-warning"), false); $("#alfa-max-progress-activity").textContent = "";
    $("#alfa-max-progress-detail").textContent = "";
    for (const name of ["connection", "archive", "repair"]) step(name, "waiting", "Получаем состояние");
  }
  function schedule(delay) { clearTimer(); timer = setTimeout(() => { timer = null; start(); }, delay); }
  function mode(value) {
    const target = $("#alfa-max-progress"); target.dataset.mode = value;
    target.classList.toggle("is-complete", value === "complete");
    target.classList.toggle("is-paused", value === "paused");
    target.classList.toggle("is-offline", value === "offline"); show(target);
    $("#alfa-max-progress-badge").textContent = ({ running: "В работе", queued: "В очереди", paused: "Пауза", error: "Ошибка", attention: "Нужна проверка", complete: "Проверено", offline: "Нет связи", idle: "Ожидание" })[value];
  }
  function step(name, status, description) {
    const target = $(`[data-sync-step="${name}"]`); target.dataset.state = status;
    target.querySelector("small").textContent = description;
    const badge = target.querySelector("b"); badge.replaceChildren();
    if (window.CenterIcon) badge.append(window.CenterIcon(status === "done" ? "check" : ({ connection: "plug", archive: "clock", repair: "people" })[name]));
    else badge.textContent = status === "done" ? "✓" : ({ connection: 1, archive: 2, repair: 3 })[name];
  }
  function meter(caption, value, total) {
    $("#alfa-max-progress-caption").textContent = caption;
    $("#alfa-max-progress-value").textContent = total > 0 ? `${number(value)} из ${number(total)}` : "Считаем объём";
    const track = $("#alfa-max-progress-track"), percent = total > 0 ? Math.min(100, Math.max(0, value / total * 100)) : 0;
    if (total > 0) { track.setAttribute("aria-valuenow", String(Math.round(percent))); track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100"); }
    else { track.removeAttribute("aria-valuenow"); track.removeAttribute("aria-valuemin"); track.removeAttribute("aria-valuemax"); }
    track.setAttribute("aria-valuetext", total > 0 ? `${caption}: ${value} из ${total}` : "Общий объём пока неизвестен");
    $("#alfa-max-progress-fill").style.width = percent + "%";
  }
  function render(payload) {
    const streams = payload.streams || [], repair = payload.client_repair;
    const repairing = Boolean(repair && !repair.complete), running = Boolean(payload.syncing && payload.state === "active");
    const paused = streams.filter(s => s.paused_until), passed = streams.filter(s => s.complete && !s.missing_rows).length;
    const issue = payload.error || streams.find(s => s.error)?.error || "";
    const unidentified = Number(payload.unidentified_rows) || 0;
    const complete = Boolean(payload.history_complete && !repairing && !issue && !unidentified && !streams.some(s => s.missing_rows));
    const active = payload.state === "active";
    const displayMode = !active ? "error" : complete ? "complete" : paused.length && !payload.backfill_pending ? "paused"
      : !payload.backfill_pending && unidentified ? "attention" : running ? "running"
      : payload.backfill_pending && payload.scheduled !== false ? "queued" : issue ? "error" : "idle";
    mode(displayMode);
    const title = !active ? payload.state === "otp_required" ? "Подтвердите вход в AlfaCRM" : "Нужно восстановить подключение"
      : complete ? "Вся история загружена и проверена"
      : displayMode === "paused" ? "Проверка архива приостановлена"
      : displayMode === "attention" ? "Обход завершён, остались записи для проверки"
      : repairing && (!running || !payload.worker || payload.worker.phase === "repair") ? running ? "Восстанавливаем историю клиентов" : "Восстановление истории клиентов в очереди"
      : running ? payload.worker?.phase === "projecting" ? "Обрабатываем сообщения клиентов" : payload.worker?.phase === "saving" ? "Сохраняем прочитанные сообщения" : "Читаем и проверяем историю CRM"
      : displayMode === "queued" ? "Ожидаем следующий проход"
      : issue ? "Для продолжения нужна повторная проверка"
      : payload.scheduled === false ? "Загрузка ещё не запланирована" : "Ожидаем следующий проход";
    $("#alfa-max-progress-text").textContent = title;
    $("#alfa-max-progress-note").textContent = !active ? "Сохранённая история остаётся доступна. Для новых сообщений проверьте подключения AlfaCRM и MAX."
      : repairing ? "Чтение архивов CRM чередуется с проверкой сохранённых клиентов по ID AlfaCRM. Позиции обоих этапов сохраняются."
      : complete ? "Новые сообщения продолжают поступать автоматически. Архив проверяется периодически."
      : "Сначала сохраняем сообщения, затем сверяем страницы и журналы. Повторная сверка может не увеличивать счётчик сообщений.";
    step("connection", active ? "done" : "error", active ? "Веб-сессия активна" : "Требуется вход");
    step("archive", passed === streams.length && streams.length ? "done" : displayMode === "running" && payload.worker?.phase !== "repair" ? "active" : paused.length ? "error" : "waiting", `${passed} из ${streams.length} журналов`);
    step("repair", repair?.complete ? "done" : repairing && displayMode === "running" && (!payload.worker || payload.worker.phase === "repair") ? "active" : "waiting", repair ? repair.complete ? "Проверка завершена" : `Проверено ${number(repair.processed)}` : "После загрузки справочника");
    if (repairing) meter("Проверено клиентов", Number(repair.processed) || 0, Number(repair.total) || 0);
    else meter("Журналов пройдено", passed, streams.length);
    const stats = $("#alfa-max-progress-stats"); stats.replaceChildren();
    for (const [key, label, value] of [["messages", "Сообщений в переписках", payload.messages], ["stored", "Записей сохранено в базе", payload.stored_messages ?? payload.messages], ["conversations", "Диалогов", payload.conversations]]) {
      const cell = node("div"), count = node("strong", number(value)); cell.dataset.metric = key; count.dataset.count = String(value || 0); cell.append(count, node("span", label)); stats.append(cell);
    }
    const activity = payload.worker || {}, currentStream = streams.find(s => s.branch_id === String(activity.branch_id) && s.direction === activity.direction);
    let movement = running && complete ? "Проверяем новые сообщения. Архив уже подтверждён." : running ? activity.phase === "repair" ? "Проверяем сохранённые сообщения клиентов."
      : ["saving", "projecting"].includes(activity.phase) ? "Записываем сообщения и сохраняем позицию обхода."
      : activity.page !== undefined ? `${currentStream?.branch_name || "Филиал " + activity.branch_id} · ${activity.direction === "sent" ? "исходящие" : "входящие"} · страница ${activity.page + 1}. Текущий проход: ${activity.completed_pages || 0} из ${activity.total_pages || "—"} страниц.`
      : "Сервер читает страницы AlfaCRM и сверяет историю."
      : displayMode === "paused" ? "Архивные журналы ждут повторной проверки. Время повтора указано в каждом журнале."
      : displayMode === "attention" ? "Полнота архива пока не подтверждена. Новые сообщения проверяются отдельно."
      : payload.next_attempt_at && !complete ? "Следующий проход: " + date(payload.next_attempt_at) + "."
      : complete ? "Приём новых сообщений включён." : "Нажмите «Проверить историю», чтобы поставить задачу в очередь.";
    if (activity.updated_at) movement += " Последнее движение: " + date(activity.updated_at) + ".";
    $("#alfa-max-progress-activity").textContent = movement;
    const warnings = [];
    if (paused.length) warnings.push(`На паузе журналов: ${paused.length}. Остальные журналы и новые сообщения обрабатываются независимо.`);
    if (issue) warnings.push(issue);
    if (unidentified) warnings.push(`Старых записей без подтверждённого исходного ID: ${number(unidentified)}. ${repairing ? "Сверяем их при восстановлении клиентов." : "Записи сохранены, но полнота истории ещё не подтверждена."}`);
    if (running && activity.updated_at && Date.now() - Date.parse(activity.updated_at) > 90000) warnings.push("Давно нет нового отчёта о движении. Текущая операция ещё не завершена; время последнего движения указано выше.");
    const warning = $("#alfa-max-progress-warning"); warning.textContent = warnings.join(" "); show(warning, warnings.length > 0);
    if (warnings.length && !autoExpanded) { $("#alfa-max-progress-journals").open = true; autoExpanded = true; }
    $("#alfa-max-progress-journal-count").textContent = `${passed} из ${streams.length} пройдено`;
    $("#alfa-max-progress-streams").replaceChildren(...streams.map(stream => {
      const line = node("li"), head = node("div", undefined, "sync-stream-head");
      const reading = running && activity.phase === "reading" && currentStream === stream;
      line.dataset.state = stream.paused_until ? "paused" : reading ? "reading" : stream.error ? "error" : stream.complete ? "done" : "waiting";
      head.append(node("strong", stream.branch_name || "Филиал " + stream.branch_id), node("span", stream.direction === "sent" ? "Исходящие" : "Входящие"));
      let value = "сохранено записей " + (stream.source_rows || 0) + (stream.expected_rows === null || stream.expected_rows === undefined ? "" : " из " + stream.expected_rows);
      if (stream.checked_page !== null && stream.checked_page !== undefined) value += "; проверена страница " + (stream.checked_page + 1);
      if (stream.catchup_page) value += "; новые сообщения: страница " + (stream.catchup_page + 1);
      if (stream.unidentified_rows) value += "; без исходного ID: " + stream.unidentified_rows;
      const detail = stream.paused_until ? (stream.missing_rows ? "Недостача " + stream.missing_rows + ". " : "Проверка на паузе. ") + "Повтор после " + date(stream.paused_until)
        : stream.error || (stream.complete ? "Обход журнала завершён" : reading ? "Читаем сейчас" : "Проверка продолжается");
      line.append(head, node("p", value), node("small", detail)); return line;
    }));
    lastDetail = `В переписках сообщений: ${payload.messages} · Всего записей в базе: ${payload.stored_messages ?? payload.messages} · Диалогов: ${payload.conversations}`;
    const period = payload.oldest_at ? "История с " + date(payload.oldest_at) + " по " + date(payload.newest_at) + ". " : "";
    $("#alfa-max-progress-detail").textContent = period + (payload.last_sync_at ? "Последнее чтение CRM: " + date(payload.last_sync_at) : "Успешного чтения CRM пока нет");
    renderEntity(payload); renderRecent(payload);
    onStatus(payload, { mode: displayMode, title });
  }
  function start() {
    if (pending) return pending;
    const workspace = state.workspace, epoch = state.epoch, token = generation;
    if (!workspace || (state.pane && state.pane !== "connections")) { reset(); return Promise.resolve(); }
    clearTimer();
    const current = () => token === generation && epoch === state.epoch && workspace.id === state.workspace?.id && (!state.pane || state.pane === "connections");
    pending = (async () => {
      try {
        const activityOnly = Boolean(lastPayload && activityReads < 4);
        const response = await api("/workspaces/" + workspace.id + "/integrations/alfa-max/sync-progress" + (activityOnly ? "?activity_only=true" : ""), { timeoutMs: 15000 });
        if (!current()) return;
        const payload = response.activity_only ? { ...lastPayload, ...response } : response;
        activityReads = activityOnly ? activityReads + 1 : 0;
        if (!payload.configured || payload.state === "not_configured") { onStatus(payload, { mode: "idle", title: "Сначала подключите MAX" }); reset(); return; }
        failures = 0; lastPayload = payload; render(payload);
        schedule(payload.state === "active" && state.connectionsView !== "settings" ? 1000 : 5000);
      } catch (error) {
        if (!current() || error.status === 401) return;
        mode("offline"); $("#alfa-max-progress-text").textContent = "Статус синхронизации временно недоступен";
        if (lastPayload) renderEntity({ ...lastPayload, syncing: false, worker: { ...lastPayload.worker, live: false } });
        $("#alfa-max-progress-note").textContent = "Показаны последние полученные данные. Ошибка просмотра статуса не означает остановку сервера.";
        if (error.status === 403) {
          $("#alfa-max-progress-detail").textContent = "Для просмотра нужны права администратора.";
          $("#alfa-max-progress-stats").replaceChildren(); $("#alfa-max-progress-streams").replaceChildren();
          lastPayload = null; selectedEvent = null; renderEntity({}); renderRecent({});
          onStatus(null, { mode: "offline", title: "Нужны права администратора" }); return;
        }
        failures += 1;
        const delay = Math.min(30000, 2000 * 2 ** Math.min(failures - 1, 4));
        $("#alfa-max-progress-detail").textContent = (lastDetail ? lastDetail + " · " : "") + "Последние полученные данные. Повторяем запрос через " + delay / 1000 + " с.";
        onStatus(null, { mode: "offline", title: "Статус временно недоступен" }); schedule(delay);
      } finally { if (token === generation) pending = null; }
    })(); return pending;
  }
  $("#alfa-max-progress-refresh").addEventListener("click", () => start());
  $("#alfa-max-progress-client-back").addEventListener("click", () => { selectedEvent = null; if (lastPayload) { renderEntity(lastPayload); renderRecent(lastPayload); } });
  $("#alfa-max-progress-recent").addEventListener("click", event => {
    const id = event.target.closest("[data-event]")?.dataset.event;
    const entry = lastPayload?.worker?.recent?.find(e => e.id === id);
    if (entry) { selectedEvent = entry; renderEntity(lastPayload); renderRecent(lastPayload); $("#alfa-max-progress-client").scrollIntoView?.({ block: "nearest", behavior: "smooth" }); }
  });
  return { start, reset };
};
