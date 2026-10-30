/* Workspace-scoped setup; plaintext keys exist only in the unsaved input. */
"use strict";
window.CenterNovofon = function ({ $, state, api, show, notice, confirm }) {
  const form = $("#novofon-form"), key = form.elements.api_key, hours = form.elements.sample_hours;
  let saved = null, busy = false, generation = 0;
  const current = context => context.generation === generation && context.workspace === state.workspace?.id && context.user === state.user?.id;
  const snapshot = () => ({ generation, workspace: state.workspace?.id, user: state.user?.id });
  const context = () => { generation++; return snapshot(); };
  const endpoint = value => `/workspaces/${value.workspace}/integrations/novofon`;
  function controls() {
    const canSave = Boolean(saved?.vault_ready), configured = Boolean(saved?.configured);
    form.querySelectorAll("input,select,button").forEach(control => { control.disabled = busy || !canSave; });
    $("#novofon-reload").disabled = busy;
    const dirty = Boolean(key.value) || Number(hours.value) !== saved?.sample_hours;
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
    key.placeholder = payload.configured ? "Ключ сохранён — введите новый только для замены" : "Вставьте Secret API Новофона";
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
      let text = "Ещё не проверено";
      if (check?.status === "ok") {
        text = name === "account" ? `Аккаунт ${check.account_id} · ${check.timezone}`
          : name === "journal" ? `В выборке: ${check.sampled} · с записями: ${check.recordings_in_sample}`
          : `Линий в выборке: ${check.sampled} · с исходящими: ${check.outgoing_capable}`;
      } else if (check?.status === "error") text = check.message || "Не удалось проверить";
      else if (check?.status === "prefix_detected") text = "Фрагмент записи доступен. Полное воспроизведение ещё не проверено.";
      else if (check?.status === "no_recording_in_sample") text = "В проверенной выборке записей нет. Можно увеличить период проверки.";
      detail.textContent = text; row.dataset.state = check?.status || "waiting"; row.append(heading, detail); list.append(row);
    }
    $("#novofon-checked-at").textContent = payload.checked_at ? `Последняя проверка: ${new Date(payload.checked_at).toLocaleString("ru-RU")}` : "Подключение ещё не проверялось.";
    if (!payload.vault_ready) notice("Хранилище ключей недоступно. Обратитесь к администратору сервера.", $("#novofon-error"));
    controls();
  }
  function reset() {
    generation++; saved = null; busy = false; form.reset(); key.type = "password";
    hours.querySelectorAll('[data-custom]').forEach(option => option.remove()); hours.value = "24";
    key.placeholder = "Вставьте Secret API Новофона";
    $("#novofon-key-note").textContent = "Ключ шифруется на сервере и доступен только этому центру.";
    $("#novofon-show-key").textContent = "Показать"; $("#novofon-show-key").setAttribute("aria-pressed", "false");
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
        data: { expected_version: saved.version, sample_hours: Number(hours.value), api_key: key.value.trim() || null } });
      if (!current(ctx)) return;
      key.value = ""; key.type = "password"; $("#novofon-show-key").textContent = "Показать";
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
    if (key.value && !await confirm("Обновить настройки?", "Введённый новый ключ будет очищен без сохранения.", "Обновить")) return;
    if (!current(ctx) || busy) return;
    key.value = ""; await load();
  });
  $("#novofon-disconnect").addEventListener("click", async () => {
    const ctx = snapshot();
    if (!saved?.configured || busy || !await confirm("Отключить Новофон?", "Сохранённый API-ключ будет удалён. Другие подключения продолжат работать.", "Отключить")) return;
    if (!current(ctx) || busy) return;
    void run(async ctx => {
      const value = await api(endpoint(ctx), { method: "DELETE", data: { expected_version: saved.version }, timeoutMs: 10000 });
      if (current(ctx)) { key.value = ""; render(value); }
    });
  });
  $("#novofon-show-key").addEventListener("click", () => {
    const reveal = key.type === "password"; key.type = reveal ? "text" : "password";
    $("#novofon-show-key").textContent = reveal ? "Скрыть" : "Показать";
    $("#novofon-show-key").setAttribute("aria-pressed", String(reveal));
  });
  form.addEventListener("input", controls);
  reset(); return { load, reset };
};
