/* Workspace-scoped setup; plaintext keys exist only in the unsaved input. */
"use strict";
window.CenterNovofon = function ({ $, state, api, show, notice, confirm }) {
  const form = $("#novofon-form"), key = form.elements.api_key, support = form.elements.support_secret, hours = form.elements.sample_hours;
  let saved = null, busy = false, generation = 0;
  const current = context => context.generation === generation && context.workspace === state.workspace?.id && context.user === state.user?.id;
  const snapshot = () => ({ generation, workspace: state.workspace?.id, user: state.user?.id });
  const context = () => { generation++; return snapshot(); };
  const endpoint = value => `/workspaces/${value.workspace}/integrations/novofon`;
  function controls() {
    const canSave = Boolean(saved?.vault_ready), configured = Boolean(saved?.configured);
    form.querySelectorAll("input,select,button").forEach(control => { control.disabled = busy || !canSave; });
    $("#novofon-reload").disabled = busy;
    const dirty = Boolean(key.value || support.value) || Number(hours.value) !== saved?.sample_hours;
    $("#novofon-check").disabled = busy || !configured || dirty;
    $("#novofon-audio-check").disabled = busy || !configured || dirty;
    $("#novofon-disconnect").disabled = busy || !configured;
    $("#novofon-save").textContent = busy ? "Подождите…" : "Сохранить и проверить";
    $("#novofon-loading").hidden = !busy;
    $("#connections-novofon-view").setAttribute("aria-busy", String(busy));
  }
  const titles = { account: "Аккаунт", journal: "Журнал звонков", sip_lines: "Линии АТС", audio: "Аудиозапись" };
  function render(payload, replaceInputs = true) {
    saved = payload;
    if (replaceInputs) {
      hours.querySelectorAll('[data-custom]').forEach(option => option.remove());
      if (![...hours.options].some(option => option.value === String(payload.sample_hours))) {
        const option = document.createElement("option"); option.value = String(payload.sample_hours);
        option.textContent = `${payload.sample_hours} ч`; option.dataset.custom = "true"; hours.append(option);
      }
      hours.value = String(payload.sample_hours);
    }
    key.placeholder = payload.configured ? "Ключ сотрудника сохранён — введите новый для замены" : "Ключ из настроек сотрудника Новофона";
    support.placeholder = payload.support_secret_configured ? "Секрет из поддержки сохранён — введите новый для замены" : "Реквизит, выданный поддержкой Новофона";
    $("#novofon-support-note").textContent = payload.support_secret_configured
      ? "Секрет из поддержки сохранён отдельно. Его использование в авторизации ещё не подтверждено; проверка ниже использует ключ сотрудника."
      : "Реквизит из поддержки отличается от ключа сотрудника. Он будет сохранён зашифрованно; его использование в авторизации ещё не подтверждено.";
    $("#novofon-key-note").textContent = payload.configured
      ? "Сохранённый ключ скрыт. Пустое поле оставляет его без изменений."
      : "Ключ шифруется на сервере и доступен только этому центру.";
    const status = { not_configured: "Не подключено", saved: "Ключ сохранён · нужна проверка",
      checking: "Проверяем доступ…", api_verified: "Доступ к API проверен", error: "Нужна проверка подключения" };
    $("#novofon-status").textContent = status[payload.status] || "Нужна проверка";
    $("#novofon-status").dataset.state = payload.status === "api_verified" ? "active" : payload.status === "error" ? "error" : "waiting";
    const list = $("#novofon-checks"); list.replaceChildren();
    for (const [name, title] of Object.entries(titles)) {
      const check = payload.checks?.[name], row = document.createElement("li"), heading = document.createElement("strong"), detail = document.createElement("span");
      heading.textContent = title;
      const mark = document.createElement("span"); mark.className = "novofon-check-icon"; mark.setAttribute("aria-hidden", "true");
      if (window.CenterIcon) mark.append(window.CenterIcon(check?.status === "ok" ? "check" : ["error", "prefix_detected", "no_recording_in_sample"].includes(check?.status) ? "info" : "clock"));
      let text = "Ещё не проверено";
      if (check?.status === "ok") {
        text = name === "account" ? `Аккаунт ${check.account_id} · ${check.timezone}`
          : name === "journal" ? `В выборке: ${check.sampled} · с записями: ${check.recordings_in_sample}`
          : `Линий в выборке: ${check.sampled} · с исходящими: ${check.outgoing_capable}`;
      } else if (check?.status === "error") text = check.message || "Не удалось проверить";
      else if (check?.status === "prefix_detected") text = "Фрагмент записи доступен. Полное воспроизведение ещё не проверено.";
      else if (check?.status === "no_recording_in_sample") text = "В проверенной выборке записей нет. Можно увеличить период проверки.";
      detail.textContent = text; row.dataset.state = check?.status || "waiting"; row.append(mark, heading, detail);
      if (check?.status === "error") {
        const codes = [];
        if (Number.isInteger(check.rpc_code)) codes.push(`Код Новофона: ${check.rpc_code}`);
        if (check.mnemonic) codes.push(check.mnemonic);
        if (check.observed_ip) codes.push(`IP запроса: ${check.observed_ip}`);
        if (Number.isInteger(check.http_status)) codes.push(`HTTP ${check.http_status}`);
        if (codes.length) { const code = document.createElement("small"); code.textContent = codes.join(" · "); row.append(code); }
      }
      list.append(row);
    }
    $("#novofon-checked-at").textContent = payload.checked_at ? `Последняя проверка: ${new Date(payload.checked_at).toLocaleString("ru-RU")}` : "Подключение ещё не проверялось.";
    if (!payload.vault_ready) notice("Хранилище ключей недоступно. Обратитесь к администратору сервера.", $("#novofon-error"));
    controls();
  }
  function reset() {
    generation++; saved = null; busy = false; form.reset(); key.type = "password";
    support.value = ""; support.type = "password";
    hours.querySelectorAll('[data-custom]').forEach(option => option.remove()); hours.value = "24";
    key.placeholder = "Ключ из настроек сотрудника Новофона";
    support.placeholder = "Реквизит, выданный поддержкой Новофона";
    $("#novofon-support-note").textContent = "Реквизит из поддержки отличается от ключа сотрудника. Он сохраняется зашифрованно; его использование в авторизации ещё не подтверждено.";
    $("#novofon-key-note").textContent = "Ключ шифруется на сервере и доступен только этому центру.";
    $("#novofon-show-key").textContent = "Показать"; $("#novofon-show-key").setAttribute("aria-pressed", "false");
    $("#novofon-show-support").textContent = "Показать"; $("#novofon-show-support").setAttribute("aria-pressed", "false");
    $("#novofon-checks").replaceChildren(); $("#novofon-checked-at").textContent = "";
    $("#novofon-status").textContent = "Не подключено";
    $("#novofon-status").dataset.state = "waiting";
    notice("", $("#novofon-error")); notice("", $("#novofon-message")); controls();
  }
  async function run(action) {
    if (busy || !state.workspace) return;
    const ctx = context(); busy = true; controls(); notice("", $("#novofon-error")); notice("", $("#novofon-message"));
    try { await action(ctx); }
    catch (error) { if (current(ctx)) notice(error.message || "Не удалось выполнить действие. Повторите позже.", $("#novofon-error")); }
    finally { if (current(ctx)) { busy = false; controls(); } }
  }
  async function load() {
    return run(async ctx => { const value = await api(endpoint(ctx), { timeoutMs: 10000 }); if (current(ctx)) render(value); });
  }
  async function verify(ctx, audio) {
    const value = await api(endpoint(ctx) + "/verify", { method: "POST", timeoutMs: 75000,
      data: { expected_version: saved.version, probe_recording: audio } });
    if (current(ctx)) render(value, false);
  }
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!saved || !form.reportValidity()) return;
    void run(async ctx => {
      const value = await api(endpoint(ctx), { method: "PUT", timeoutMs: 10000,
        data: { expected_version: saved.version, sample_hours: Number(hours.value), api_key: key.value.trim() || null,
          support_secret: support.value.trim() || null } });
      if (!current(ctx)) return;
      key.value = ""; key.type = "password"; $("#novofon-show-key").textContent = "Показать";
      support.value = ""; support.type = "password"; $("#novofon-show-support").textContent = "Показать";
      $("#novofon-show-support").setAttribute("aria-pressed", "false");
      $("#novofon-show-key").setAttribute("aria-pressed", "false"); render(value);
      notice("Ключ сохранён. Проверяем доступ к Новофону…", $("#novofon-message"));
      await verify(ctx, false);
      if (current(ctx)) notice(saved.status === "api_verified" ? "API подключён. Результаты отдельных проверок — ниже." : "Ключ сохранён. Проверьте результат подключения ниже.", $("#novofon-message"));
    });
  });
  $("#novofon-check").addEventListener("click", () => { void run(ctx => verify(ctx, false)); });
  $("#novofon-audio-check").addEventListener("click", () => { void run(ctx => verify(ctx, true)); });
  $("#novofon-reload").addEventListener("click", async () => {
    const ctx = snapshot();
    if ((key.value || support.value) && !await confirm("Обновить настройки?", "Введённые новые реквизиты будут очищены без сохранения.", "Обновить")) return;
    if (!current(ctx) || busy) return;
    key.value = ""; support.value = ""; await load();
  });
  $("#novofon-disconnect").addEventListener("click", async () => {
    const ctx = snapshot();
    if (!saved?.configured || busy || !await confirm("Отключить Новофон?", "Сохранённый ключ сотрудника и секрет из поддержки будут удалены. Другие подключения продолжат работать.", "Отключить")) return;
    if (!current(ctx) || busy) return;
    void run(async ctx => {
      const value = await api(endpoint(ctx), { method: "DELETE", data: { expected_version: saved.version }, timeoutMs: 10000 });
      if (current(ctx)) { key.value = ""; support.value = ""; render(value); }
    });
  });
  $("#novofon-show-key").addEventListener("click", () => {
    const reveal = key.type === "password"; key.type = reveal ? "text" : "password";
    $("#novofon-show-key").textContent = reveal ? "Скрыть" : "Показать";
    $("#novofon-show-key").setAttribute("aria-pressed", String(reveal));
  });
  $("#novofon-show-support").addEventListener("click", () => {
    const reveal = support.type === "password"; support.type = reveal ? "text" : "password";
    $("#novofon-show-support").textContent = reveal ? "Скрыть" : "Показать";
    $("#novofon-show-support").setAttribute("aria-pressed", String(reveal));
  });
  form.addEventListener("input", controls);
  reset(); return { load, reset };
};
