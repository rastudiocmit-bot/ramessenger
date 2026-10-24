"use strict";
window.CenterAlfaProgress = function ({ $, state, api, show, date, onStatus = () => {} }) {
  let timer = null, generation = 0, pending = null, failures = 0, lastDetail = "", autoExpanded = false;
  const number = value => new Intl.NumberFormat("ru-RU").format(Number(value) || 0);
  const node = (tag, value, className) => { const e = document.createElement(tag); if (value !== undefined) e.textContent = value; if (className) e.className = className; return e; };
  function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function reset() {
    generation += 1; clearTimer(); pending = null; failures = 0; lastDetail = ""; autoExpanded = false;
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
    const complete = Boolean(payload.history_complete && !issue && !unidentified && !streams.some(s => s.missing_rows));
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
      : running ? payload.worker?.phase === "saving" ? "Сохраняем прочитанные сообщения" : "Читаем и проверяем историю CRM"
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
      : activity.phase === "saving" ? "Записываем сообщения и сохраняем позицию обхода."
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
        const payload = await api("/workspaces/" + workspace.id + "/integrations/alfa-max/sync-progress", { timeoutMs: 15000 });
        if (!current()) return;
        if (!payload.configured || payload.state === "not_configured") { onStatus(payload, { mode: "idle", title: "Сначала подключите MAX" }); reset(); return; }
        failures = 0; render(payload);
        schedule(payload.backfill_pending || payload.syncing && !payload.history_complete ? 5000 : 15000);
      } catch (error) {
        if (!current() || error.status === 401) return;
        mode("offline"); $("#alfa-max-progress-text").textContent = "Статус синхронизации временно недоступен";
        $("#alfa-max-progress-note").textContent = "Показаны последние полученные данные. Ошибка просмотра статуса не означает остановку сервера.";
        if (error.status === 403) {
          $("#alfa-max-progress-detail").textContent = "Для просмотра нужны права администратора.";
          $("#alfa-max-progress-stats").replaceChildren(); $("#alfa-max-progress-streams").replaceChildren();
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
  return { start, reset };
};
