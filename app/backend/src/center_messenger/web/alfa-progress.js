"use strict";
window.CenterAlfaProgress = function ({ $, state, api, show, date }) {
  let timer = null, generation = 0, pending = null, failures = 0, lastDetail = "";
  function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function reset() {
    generation += 1; clearTimer(); pending = null; failures = 0; lastDetail = "";
    show($("#alfa-max-progress"), false);
    $("#alfa-max-progress-streams").replaceChildren();
  }
  function schedule(delay) {
    clearTimer();
    timer = setTimeout(() => { timer = null; start(); }, delay);
  }
  function classes({ complete = false, paused = false, offline = false } = {}) {
    const target = $("#alfa-max-progress");
    target.classList.toggle("is-complete", complete);
    target.classList.toggle("is-paused", paused);
    target.classList.toggle("is-offline", offline);
    show(target);
  }
  function render(payload) {
    const streams = payload.streams || [];
    const done = streams.filter(s => s.complete && !s.missing_rows).length;
    const issue = payload.error || streams.find(s => s.error)?.error
      || (payload.unidentified_rows ? "Есть старые сообщения без подтверждённого исходного ID: " + payload.unidentified_rows : "");
    const paused = Boolean(payload.history_paused && !payload.backfill_pending);
    const complete = Boolean(payload.history_complete && !issue);
    classes({ complete, paused });
    $("#alfa-max-progress-text").textContent = payload.client_repair && !payload.client_repair.complete
      ? "Восстанавливаем историю клиентов: проверено " + payload.client_repair.processed
      : paused ? "Проверка архива приостановлена"
      : issue ? "История загружена не полностью"
      : complete ? "Обход всей истории завершён" : "Загружаем историю за весь период…";
    const period = payload.oldest_at ? " · С " + date(payload.oldest_at) + " по " + date(payload.newest_at) : "";
    lastDetail = "В переписках сообщений: " + payload.messages + " · Всего записей в базе: "
      + (payload.stored_messages ?? payload.messages) + " · Диалогов: " + payload.conversations
      + " · Журналов проверено: " + done + " из " + streams.length + period
      + (payload.last_sync_at ? " · Последнее чтение: " + date(payload.last_sync_at) : "")
      + (issue ? " · " + issue : "");
    $("#alfa-max-progress-detail").textContent = lastDetail;
    $("#alfa-max-progress-streams").replaceChildren(...streams.map(stream => {
      const line = document.createElement("li");
      let value = "Филиал " + stream.branch_id + " — " + (stream.direction === "sent" ? "исходящие" : "входящие")
        + ": сохранено записей " + stream.source_rows
        + (stream.expected_rows === null || stream.expected_rows === undefined ? "" : " из " + stream.expected_rows);
      if (stream.checked_page !== null && stream.checked_page !== undefined) value += "; проверена страница " + (stream.checked_page + 1);
      if (stream.checked_at) value += " (" + date(stream.checked_at) + ")";
      if (stream.catchup_page) value += "; догружаем новые сообщения, страница " + (stream.catchup_page + 1);
      if (stream.retry) value += "; повторная проверка";
      if (stream.unidentified_rows) value += "; без исходного ID: " + stream.unidentified_rows;
      if (stream.paused_until) value += (stream.missing_rows ? "; недостача " + stream.missing_rows : "; проверка приостановлена")
        + ", повтор после " + date(stream.paused_until);
      else if (stream.complete && !stream.missing_rows) value += "; проверено";
      else if (stream.error) value += "; " + stream.error;
      line.textContent = value;
      return line;
    }));
  }
  function start() {
    if (pending) return pending;
    const workspace = state.workspace, epoch = state.epoch, token = generation;
    if (!workspace || (state.pane && state.pane !== "connections")) { reset(); return Promise.resolve(); }
    clearTimer();
    const current = () => token === generation && epoch === state.epoch && workspace.id === state.workspace?.id
      && (!state.pane || state.pane === "connections");
    pending = (async () => {
      try {
        const payload = await api("/workspaces/" + workspace.id + "/integrations/alfa-max/sync-progress", { timeoutMs: 15000 });
        if (!current()) return;
        if (!payload.configured || payload.state !== "active") { reset(); return; }
        failures = 0; render(payload);
        // Keep checking after completion: the worker also imports new messages
        // and periodically rechecks older rows while this screen stays open.
        schedule(payload.backfill_pending || payload.syncing ? 5000 : 15000);
      } catch (error) {
        if (!current() || error.name === "AbortError" || error.status === 401) return;
        classes({ offline: true });
        $("#alfa-max-progress-text").textContent = "Статус синхронизации временно недоступен";
        if (error.status === 403) {
          $("#alfa-max-progress-detail").textContent = "Для просмотра нужны права администратора.";
          return;
        }
        failures += 1;
        const delay = Math.min(30000, 2000 * 2 ** Math.min(failures - 1, 4));
        $("#alfa-max-progress-detail").textContent = (lastDetail ? lastDetail + " · " : "")
          + "Последние полученные данные. Повторяем запрос через " + delay / 1000 + " с.";
        schedule(delay);
      } finally {
        if (token === generation) pending = null;
      }
    })();
    return pending;
  }
  return { start, reset };
};
