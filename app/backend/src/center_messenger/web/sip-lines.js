"use strict";
window.CenterSipLines = ({ $, state, api, notice, confirm }) => {
  const root = $("#sip-lines-card"); if (!root) return { load() {}, reset() {} };
  const form = $("#sip-lines-form"), list = $("#sip-lines-list"), error = $("#sip-lines-error"), message = $("#sip-lines-message");
  const fields = form.elements;
  let saved = null, editing = null, busy = false, generation = 0, dirty = false;
  const context = () => ({ generation, workspace: state.workspace?.id, user: state.user?.id, epoch: state.epoch });
  const current = ctx => ctx.generation === generation && ctx.workspace === state.workspace?.id && ctx.user === state.user?.id && ctx.epoch === state.epoch;
  const endpoint = ctx => `/workspaces/${ctx.workspace}/integrations/novofon/sip-lines`;
  const node = (tag, text, cls) => { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; };
  const button = (text, action) => { const e = node("button", text, "text-button"); e.type = "button"; e.addEventListener("click", action); return e; };
  function controls() {
    const writable = Boolean(saved?.vault_ready && saved?.members?.length);
    $("#sip-lines-add").disabled = busy || !writable;
    $("#sip-lines-refresh").disabled = busy;
    form.querySelectorAll("input,select,button").forEach(e => { e.disabled = busy || !writable; });
    $("#sip-lines-cancel").disabled = busy;
    $("#sip-password-show").disabled = busy;
    fields.primary.disabled ||= !fields.enabled.checked;
    list.querySelectorAll("button").forEach(e => { e.disabled = busy; });
    $("#sip-lines-save").textContent = busy ? "Подождите…" : "Сохранить линию";
    root.setAttribute("aria-busy", String(busy));
  }
  function clearForm() {
    form.reset(); fields.password.value = ""; fields.password.type = "password";
    fields.password.placeholder = "SIP-пароль из настроек линии Новофона";
    $("#sip-password-show").textContent = "Показать"; $("#sip-password-show").setAttribute("aria-pressed", "false");
    editing = null; dirty = false; form.hidden = true;
  }
  function edit(line = null) {
    clearForm(); editing = line?.id || null;
    fields.password.required = !line;
    fields.member_id.replaceChildren(...saved.members.map(member => new Option([member.name, member.email].filter(Boolean).join(" · "), member.id)));
    if (line) {
      if (![...fields.member_id.options].some(o => o.value === line.member_id)) fields.member_id.append(new Option("Сотрудник отключён — выберите действующего", line.member_id));
      for (const key of ["member_id", "sip_number", "extension", "server", "port"]) fields[key].value = line[key] ?? "";
      fields.enabled.checked = line.enabled; fields.primary.checked = line.primary;
      fields.password.placeholder = "Пароль сохранён — оставьте поле пустым, чтобы сохранить его";
    }
    $("#sip-lines-form-title").textContent = line ? "Редактирование SIP-линии" : "Новая SIP-линия";
    $("#sip-password-note").textContent = line ? "Для смены сотрудника, SIP-номера, сервера или порта введите пароль заново. Сохранённый пароль не показывается." : "Пароль шифруется на сервере и не возвращается в браузер.";
    form.hidden = false; notice("", error); notice("", message); controls(); fields.member_id.focus();
  }
  async function startEdit(line = null) {
    if (busy || !saved) return; const ctx = context(), version = saved.version;
    if (dirty && !await confirm("Открыть другую линию?", "Несохранённые изменения будут очищены.", "Продолжить")) return;
    if (current(ctx) && !busy && saved?.version === version) edit(line);
  }
  async function remove(line) {
    if (busy || !saved) return; const ctx = context(), version = saved.version;
    if (!await confirm("Удалить SIP-линию?", `Линия ${line.sip_number} сотрудника ${line.member_name} будет удалена из мессенджера. Настройки в Новофоне сохранятся.`, "Удалить")) return;
    if (!current(ctx) || busy) return;
    await run(async active => {
      const value = await api(`${endpoint(active)}/${line.id}`, { method: "DELETE", timeoutMs: 10000, data: { expected_version: version } });
      if (!current(active)) return;
      if (editing === line.id) clearForm();
      else if (editing && !form.hidden) {
        const before = saved.lines.find(item => item.id === editing), after = value.lines.find(item => item.id === editing);
        // Deleting a primary profile may promote the edited line. Keep an
        // untouched flag in sync without replacing this draft's other fields.
        if (before && after && fields.member_id.value === before.member_id && fields.primary.checked === before.primary) {
          fields.primary.checked = after.primary && fields.enabled.checked;
        }
      }
      render(value); notice("SIP-линия удалена из мессенджера.", message);
    });
  }
  function render(value) {
    if (!Array.isArray(value.lines) || !Array.isArray(value.members) || !Number.isInteger(value.version)) throw new Error("Не удалось получить настройки SIP-линий.");
    saved = value; list.replaceChildren();
    if (!value.members.length) list.append(node("p", "Сначала добавьте сотрудников в разделе «Сотрудники».", "form-note"));
    if (!value.lines.length) list.append(node("p", "SIP-линии ещё не добавлены. Назначьте каждому сотруднику его SIP-номер и пароль.", "form-note"));
    for (const line of value.lines) {
      const row = node("article", "", "sip-line-row"); row.dataset.lineId = line.id;
      const copy = node("div", "", "sip-line-copy"), name = node("strong", line.member_name);
      const title = node("div", "", "sip-line-title"); title.append(name);
      if (line.primary) title.append(node("span", "Основная", "sip-line-badge"));
      copy.append(title, node("p", `SIP ${line.sip_number}${line.extension ? ` · внутренний ${line.extension}` : ""}`), node("small", `${line.server}:${line.port} · ${line.enabled ? "Включена" : "Отключена"} · ${!line.member_active ? "Сотрудник отключён" : line.password_configured ? "Пароль сохранён" : "Пароль не задан"}`));
      const actions = node("div", "", "sip-line-actions"); actions.append(button("Изменить", () => void startEdit(line)), button("Удалить", () => void remove(line))); row.append(copy, actions); list.append(row);
    }
    if (!value.vault_ready) notice("Хранилище паролей недоступно. Обратитесь к администратору сервера.", error);
    controls();
  }
  async function run(action) {
    if (busy || !state.workspace) return;
    const ctx = context(); busy = true; controls(); notice("", error); notice("", message);
    try { await action(ctx); }
    catch (e) { if (current(ctx)) notice(e.message || "Не удалось сохранить SIP-линии.", error); }
    finally { if (current(ctx)) { busy = false; controls(); } }
  }
  async function load() {
    return run(async ctx => {
      const value = await api(endpoint(ctx), { timeoutMs: 10000 }); if (!current(ctx)) return;
      if (!form.hidden && saved && value.version !== saved.version) { notice("SIP-линии изменились. Данные формы сохранены. Обновите список перед сохранением.", error); return; }
      render(value);
    });
  }
  function reset() {
    generation++; saved = null; busy = false; clearForm(); fields.member_id.replaceChildren(); list.replaceChildren(); notice("", error); notice("", message); controls();
  }
  form.addEventListener("input", () => { dirty = true; if (!fields.enabled.checked) fields.primary.checked = false; controls(); });
  form.addEventListener("submit", e => {
    e.preventDefault(); if (busy || !saved || !form.reportValidity()) return;
    const submission = context();
    void run(async ctx => {
      const value = await api(endpoint(ctx), { method: "PUT", timeoutMs: 10000, data: {
        expected_version: saved.version, line_id: editing, member_id: fields.member_id.value,
        sip_number: fields.sip_number.value.trim(), extension: fields.extension.value.trim(), server: fields.server.value.trim(), port: Number(fields.port.value),
        password: fields.password.value || null, enabled: fields.enabled.checked, primary: fields.primary.checked
      } });
      if (!current(ctx)) return; clearForm(); render(value); notice("SIP-линия сохранена. Регистрация и разговор через микрофон ещё не проверены.", message);
    }).finally(() => { if (current(submission) && !busy) { fields.password.value = ""; fields.password.type = "password"; $("#sip-password-show").textContent = "Показать"; $("#sip-password-show").setAttribute("aria-pressed", "false"); } });
  });
  $("#sip-lines-add").addEventListener("click", () => void startEdit());
  $("#sip-lines-cancel").addEventListener("click", () => { if (!busy) { clearForm(); controls(); } });
  $("#sip-lines-refresh").addEventListener("click", async () => {
    if (busy) return; const ctx = context();
    if (dirty && !await confirm("Обновить список?", "Несохранённые изменения будут очищены.", "Обновить")) return;
    if (!current(ctx) || busy) return; clearForm(); await load();
  });
  $("#sip-password-show").addEventListener("click", () => {
    const reveal = fields.password.type === "password"; fields.password.type = reveal ? "text" : "password";
    $("#sip-password-show").textContent = reveal ? "Скрыть" : "Показать"; $("#sip-password-show").setAttribute("aria-pressed", String(reveal));
  });
  reset(); return { load, reset };
};
