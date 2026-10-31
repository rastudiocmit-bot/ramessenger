"use strict";
window.CenterTelephonyLoader = ({ $, state, api, notice }) => {
  const form = $("#telephony-loader-form"); let saved = null, busy = false, generation = 0;
  const valid = (gen, workspace) => gen === generation && workspace === state.workspace?.id;
  function controls() { form.querySelectorAll("input,button").forEach(control => { control.disabled = busy || !saved; }); $("#telephony-loader-refresh").disabled = busy; }
  function render(value) {
    const quotaWait = ["quota_exhausted", "limit_exceeded"].includes(value.last_error_code);
    saved = value; form.elements.enabled.checked = value.enabled; form.elements.history_days.value = value.history_days; form.elements.poll_seconds.value = value.poll_seconds;
    $("#telephony-loader-status").textContent = !value.enabled ? "Загрузка выключена" : value.running ? "Загружаем страницу звонков" : quotaWait ? "Пауза по лимиту API Новофона" : value.last_error_code ? "Ожидаем повторной попытки" : value.last_sync_at ? "История обновляется автоматически" : "Ожидаем первый проход";
    const time = value.last_sync_at ? new Intl.DateTimeFormat("ru-RU", {dateStyle:"short",timeStyle:"medium",timeZone:state.workspace?.timezone || "Europe/Moscow"}).format(new Date(value.last_sync_at)) : "ещё не было";
    $("#telephony-loader-count").textContent = `Сохранено звонков: ${value.call_count || 0} · Последняя полная сверка: ${time}`;
    $("#telephony-loader-progress").textContent = value.archive_done ? "Первичная история загружена" : "Первичная история загружается порциями; новые звонки имеют приоритет";
    const limits = value.api_limits || {}, balance = [];
    if (Number.isInteger(limits.minute_remaining)) balance.push(`за минуту ${limits.minute_remaining} из ${limits.minute_limit}`);
    if (Number.isInteger(limits.day_remaining)) balance.push(`за сутки ${limits.day_remaining} из ${limits.day_limit}`);
    if (balance.length) $("#telephony-loader-progress").textContent += ` · Учтённый остаток API: ${balance.join("; ")}`;
    $("#telephony-loader-code").textContent = value.last_error_code ? `${quotaWait ? "Загрузка продолжится автоматически после восстановления квоты" : `Причина повтора: ${value.last_error_code}`}${value.retry_at ? ` · не раньше ${new Intl.DateTimeFormat("ru-RU",{dateStyle:"short",timeStyle:"short",timeZone:state.workspace?.timezone || "Europe/Moscow"}).format(new Date(value.retry_at))}` : ""}` : "";
  }
  async function load() {
    if (busy || !state.workspace) return;
    const gen = generation, workspace = state.workspace.id; busy = true; controls();
    try { const value = await api(`/workspaces/${workspace}/integrations/novofon/loader`); if (valid(gen,workspace)) { render(value); notice("",$("#telephony-loader-error")); } }
    catch (error) { if (valid(gen,workspace)) { saved = null; notice(error.message,$("#telephony-loader-error")); } }
    finally { if (valid(gen,workspace)) { busy = false; controls(); } }
  }
  function reset() { generation++; saved = null; busy = false; form.reset(); controls(); notice("",$("#telephony-loader-error")); $("#telephony-loader-message").textContent = ""; $("#telephony-loader-status").textContent = "Состояние ещё не получено"; $("#telephony-loader-count").textContent = ""; $("#telephony-loader-progress").textContent = ""; $("#telephony-loader-code").textContent = ""; }
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !saved || !form.reportValidity()) return;
    const gen = generation, workspace = state.workspace.id; busy = true; controls();
    try {
      const value = await api(`/workspaces/${workspace}/integrations/novofon/loader`, {method:"PUT",data:{expected_version:saved.version,enabled:form.elements.enabled.checked,history_days:Number(form.elements.history_days.value),poll_seconds:Number(form.elements.poll_seconds.value)}});
      if (valid(gen,workspace)) { render(value); notice("",$("#telephony-loader-error")); $("#telephony-loader-message").textContent = "Настройки сохранены. Загрузчик продолжит работу в отдельном контейнере."; }
    } catch (error) { if (valid(gen,workspace)) notice(error.message,$("#telephony-loader-error")); }
    finally { if (valid(gen,workspace)) { busy = false; controls(); } }
  });
  $("#telephony-loader-refresh").addEventListener("click", () => { if (!form.reportValidity()) return; load(); });
  reset(); return {load,reset};
};
