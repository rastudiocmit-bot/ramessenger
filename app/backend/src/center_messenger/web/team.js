"use strict";
// Employee access is edited in place. No CRM calls or changes to login sessions.
window.CenterTeam = ({ $, state, api, node, show, roles, initials, confirm }) => {
  const keys = ["messages", "tasks", "overview", "security"];
  const sections = {
    messages: ["Чаты", "Переписка с клиентами в MAX"],
    tasks: ["Задачи", "Поручения и напоминания. Нужен доступ к чатам"],
    overview: ["Организация", "Название и общая информация о центре"],
    security: ["Безопасность", "Свой пароль и активные сеансы"],
  };
  const descriptions = {
    owner: "Полный доступ к центру и управление сотрудниками.",
    admin: "Все разделы центра, подключения и управление сотрудниками. Назначает владелец.",
    operator: "Отправка сообщений и работа с задачами в выбранных разделах.",
    observer: "Просмотр выбранных разделов. Нельзя отправлять сообщения или изменять задачи.",
    branch_manager: "Работа с чатами, задачами и справочником клиентов в выбранных разделах.",
  };
  const list = $("#members-list"), invitePanel = $("#team-invite"), inviteForm = $("#create-invite-form");
  let entries = new Map(), active = null, context = "", generation = 0, sequence = 0, loading = false, inviting = false, invited = false;
  const key = () => `${state.user?.id}:${state.workspace?.id}:${state.workspace?.role}`;
  const scope = () => `${key()}:${state.epoch}`;
  const current = () => ({ key: key(), epoch: state.epoch, generation });
  const valid = token => token.key === key() && token.epoch === state.epoch && token.generation === generation;
  const admin = role => ["owner", "admin"].includes(role);
  const allowed = () => state.workspace?.role === "owner" ? ["operator", "observer", "branch_manager", "admin"] : ["operator", "observer", "branch_manager"];
  const effective = m => admin(m.role) ? [] : (m.sections?.length ? m.sections : ["overview", "security"]);
  const summary = (m, exact = false) => {
    if (admin(m.role)) return "Все разделы центра";
    const grant = exact ? m.sections : effective(m);
    const names = keys.filter(k => grant.includes(k) && (k !== "tasks" || grant.includes("messages"))).map(k => sections[k][0]);
    if (["branch_manager", "observer"].includes(m.role) && grant.includes("messages")) names.push("Клиенты");
    if (m.role !== "operator" && grant.includes("messages") && grant.includes("overview")) names.push("Сводка");
    return names.join(" · ") || "Только профиль";
  };
  function line(el, text, error = false) { el.textContent = text; el.hidden = !text; el.classList.toggle("is-error", error); }
  function button(text, cls = "text-button") { const b = node("button", text, cls); b.type = "button"; return b; }
  function sectionChecks(box, chosen, prefix, changed) {
    box.replaceChildren();
    keys.forEach(k => {
      const label = node("label", undefined, "team-section-option"), text = node("span"), input = node("input");
      input.type = "checkbox"; input.name = "sections"; input.value = k; input.id = `${prefix}-${k}`; input.checked = chosen.includes(k);
      text.append(node("strong", sections[k][0]), node("small", sections[k][1])); label.append(input, text); box.append(label);
      input.addEventListener("change", () => {
        if (k === "tasks" && input.checked) box.querySelector('[value="messages"]').checked = true;
        if (k === "messages" && !input.checked) box.querySelector('[value="tasks"]').checked = false;
        changed();
      });
    });
  }
  const chosen = box => [...box.querySelectorAll("input:checked")].map(e => e.value);
  const draft = e => ({ role: e.select.value, sections: admin(e.select.value) ? [] : chosen(e.checks) });
  const equal = (a, b) => a.role === b.role && [...a.sections].sort().join() === [...b.sections].sort().join();
  const dirty = e => e?.form && !equal(draft(e), {role: e.member.role, sections: effective(e.member)});
  function controls(e) {
    show(e.checks, !admin(e.select.value)); show(e.full, admin(e.select.value));
    e.hint.textContent = descriptions[e.select.value];
    e.preview.textContent = `Будет доступно: ${summary(draft(e), true)}. Профиль доступен всегда.`;
    e.save.disabled = e.busy || !dirty(e); e.cancel.disabled = e.busy; e.remove.disabled = e.busy;
    e.form.setAttribute("aria-busy", String(Boolean(e.busy)));
  }
  function fill(e) {
    e.select.value = e.member.role;
    const selected = effective(e.member);
    // When demoting a full-access administrator, start with an explicit small grant.
    sectionChecks(e.checks, selected.length ? selected : ["messages", "tasks", "security"], `team-${e.member.user_id}`, () => { line(e.status, "Есть несохранённые изменения"); controls(e); });
    line(e.status, ""); controls(e);
  }
  function close(e) { if (!e) return; show(e.panel, false); e.toggle.setAttribute("aria-expanded", "false"); e.row.classList.remove("is-open"); if (active === e) active = null; }
  async function releaseActive() {
    if (active?.busy) return false;
    const e = active, token = current();
    if (dirty(e) && !await confirm("Не сохранять изменения?", `Настройки сотрудника «${e.member.display_name}» изменены. Их можно сохранить в открытой карточке.`, "Не сохранять")) return false;
    if (!valid(token) || active !== e) return false;
    close(e); if (e?.form) fill(e); return true;
  }
  async function toggle(e) {
    if (active === e) { await releaseActive(); return; }
    if (!await releaseActive()) return;
    active = e; show(e.panel); e.toggle.setAttribute("aria-expanded", "true"); e.row.classList.add("is-open");
  }
  function build(member, i) {
    const e = {member, busy: false};
    e.row = node("article", undefined, "team-member card"); e.row.dataset.member = member.user_id;
    const header = node("div", undefined, "mrow member-row"), avatar = node("span", initials(member.display_name), "ava me"), info = node("div", undefined, "mrow-main");
    avatar.setAttribute("aria-hidden", "true");
    const name = node("strong", member.display_name); if (member.user_id === state.user?.id) name.append(node("span", "Вы", "team-self"));
    e.short = node("small", summary(member), "team-access-summary"); info.append(name, node("small", member.email), e.short);
    e.pill = node("span", roles[member.role] || "Сотрудник", `rolepill ${member.role === "branch_manager" ? "branch" : member.role}`);
    e.toggle = button("Доступы", "team-access-toggle"); const arrow = window.CenterIcon("down"); arrow.classList.add("team-chevron"); e.toggle.append(arrow);
    e.toggle.setAttribute("aria-label", `Доступы: ${member.display_name}`); e.toggle.setAttribute("aria-expanded", "false");
    e.panel = node("div", undefined, "team-access-panel"); e.panel.id = `team-access-${i}`; e.panel.hidden = true; e.panel.setAttribute("role", "region"); e.panel.setAttribute("aria-label", `Настройки доступа: ${member.display_name}`); e.toggle.setAttribute("aria-controls", e.panel.id);
    header.append(avatar, info, e.pill, e.toggle); e.row.append(header, e.panel); e.toggle.addEventListener("click", () => toggle(e));
    const manageable = member.user_id !== state.user?.id && member.role !== "owner" && (state.workspace?.role === "owner" || member.role !== "admin");
    if (!manageable) {
      e.panel.append(node("p", descriptions[member.role], "team-help"), node("p", admin(member.role) ? "Полный доступ предусмотрен ролью. Отдельные разделы не ограничиваются." : `Доступно: ${summary(member)}.`, "team-help"), node("p", member.role === "owner" ? "Права владельца защищены от изменения." : member.user_id === state.user?.id ? "Свой доступ нельзя изменить в этой панели." : "Доступ администратора может изменить только владелец.", "team-help"));
      return e;
    }
    e.form = node("form", undefined, "team-access-form"); const grid = node("div", undefined, "team-access-grid"), left = node("div"), right = node("fieldset", undefined, "team-section-list"), label = node("label", "Роль сотрудника", "fld");
    e.select = node("select"); e.select.name = "role"; allowed().forEach(r => { const option = node("option", roles[r]); option.value = r; e.select.append(option); }); label.append(e.select);
    e.hint = node("p", "", "team-help"); left.append(label, e.hint);
    right.append(node("legend", "Доступные разделы")); e.checks = node("div", undefined, "team-section-checks"); e.full = node("p", "Администратору доступны все разделы центра. Ограничить отдельные разделы можно, выбрав другую роль.", "team-full-access"); right.append(e.checks, e.full); grid.append(left, right);
    e.preview = node("p", "", "team-effective"); e.status = node("p", "", "team-save-status"); e.status.setAttribute("role", "status"); e.status.setAttribute("aria-live", "polite");
    const actions = node("div", undefined, "team-save-actions"), saves = node("div", undefined, "btn-row"); e.save = button("Сохранить", "btn primary"); e.save.type = "submit"; e.cancel = button("Отмена", "btn sec"); e.remove = button("Отозвать доступ", "text-button team-revoke"); saves.append(e.save, e.cancel); actions.append(saves, e.remove);
    e.form.append(grid, e.preview, e.status, actions); e.panel.append(e.form); fill(e);
    e.select.addEventListener("change", () => { line(e.status, "Есть несохранённые изменения"); controls(e); });
    e.cancel.addEventListener("click", () => { fill(e); close(e); e.toggle.focus(); });
    e.form.addEventListener("submit", async event => {
      event.preventDefault(); if (e.busy || !dirty(e)) return;
      const value = draft(e), token = current(), workspace = state.workspace.id;
      if (!admin(value.role) && !value.sections.length) { line(e.status, "Выберите хотя бы один раздел.", true); return; }
      e.busy = true; controls(e); [...e.form.querySelectorAll("input,select")].forEach(c => c.disabled = true); line(e.status, "Сохраняем доступ…");
      try {
        await api(`/workspaces/${workspace}/members/${member.user_id}/access`, {method: "POST", data: value, timeoutMs: 20000});
        if (!valid(token)) return;
        Object.assign(member, value); e.short.textContent = summary(member); e.pill.textContent = roles[member.role]; e.pill.className = `rolepill ${member.role === "branch_manager" ? "branch" : member.role}`;
        line(e.status, "Доступ сохранён");
      } catch (error) { if (valid(token) && error.name !== "AbortError") line(e.status, error.message, true); }
      finally { if (valid(token)) { e.busy = false; [...e.form.querySelectorAll("input,select")].forEach(c => c.disabled = false); controls(e); } }
    });
    e.remove.addEventListener("click", async () => {
      const token = current(), workspace = state.workspace.id;
      if (e.busy || !await confirm("Отозвать доступ?", `${member.display_name} (${member.email}) потеряет доступ к этому центру.`, "Отозвать доступ") || !valid(token)) return;
      e.busy = true; controls(e);
      try { await api(`/workspaces/${workspace}/members/${member.user_id}/remove`, {method: "POST", timeoutMs: 20000}); if (!valid(token)) return; close(e); entries.delete(member.user_id); e.row.remove(); count(); }
      catch (error) { if (valid(token) && error.name !== "AbortError") line(e.status, error.message, true); }
      finally { if (valid(token)) { e.busy = false; controls(e); } }
    });
    return e;
  }
  function count() { $("#team-count").textContent = `Сотрудников в центре: ${entries.size}`; show($("#team-empty"), !entries.size); }
  function reset() {
    generation++; sequence++; context = scope(); loading = inviting = invited = false; active = null; entries.clear(); list.replaceChildren();
    show(invitePanel, false); $("#invite-open").setAttribute("aria-expanded", "false"); inviteForm.reset(); $("#invite-url").value = "";
    show(inviteForm); show($("#invite-result"), false); $("#members-refresh").disabled = false;
    inviteForm.querySelectorAll("input,select,button").forEach(c => c.disabled = false);
    line($("#team-error"), ""); line($("#invite-error"), ""); line($("#invite-copy-status"), ""); $("#team-count").textContent = ""; show($("#team-empty"), false);
  }
  async function load(force = false) {
    if (context !== scope()) reset();
    if (!admin(state.workspace?.role)) { reset(); return; }
    if (loading || [...entries.values()].some(e => e.busy)) return;
    if (dirty(active)) { if (!force || !await releaseActive()) return; }
    const token = current(), seq = ++sequence, workspace = state.workspace.id; loading = true; $("#members-refresh").disabled = true; line($("#team-error"), "");
    if (!entries.size) line($("#team-error"), "Загружаем сотрудников…");
    try {
      const payload = await api(`/workspaces/${workspace}/members`, {timeoutMs: 15000});
      if (!valid(token) || seq !== sequence) return;
      // A user may start editing while the list request is in flight.
      if (dirty(active) || [...entries.values()].some(e => e.busy)) return;
      const openId = active?.member.user_id; active = null; entries.clear(); list.replaceChildren();
      payload.members.forEach((member, i) => { const e = build(member, i); entries.set(member.user_id, e); list.append(e.row); if (member.user_id === openId) { active = e; show(e.panel); e.toggle.setAttribute("aria-expanded", "true"); e.row.classList.add("is-open"); } });
      count(); line($("#team-error"), "");
    } catch (error) { if (valid(token) && error.name !== "AbortError") line($("#team-error"), error.message, true); }
    finally { if (valid(token) && seq === sequence) { loading = false; $("#members-refresh").disabled = false; } }
  }
  function inviteControls() {
    const role = inviteForm.elements.role.value, full = admin(role);
    show($("#invite-sections"), !full); show($("#invite-full-access"), full);
    $("#invite-role-hint").textContent = descriptions[role];
    $("#invite-effective").textContent = `Будет доступно: ${summary({role, sections: chosen($("#invite-sections"))}, true)}. Профиль доступен всегда.`;
  }
  async function closeInvite() {
    if (inviting) return;
    const token = current();
    if (!invited && inviteForm.elements.email.value.trim() && !await confirm("Закрыть приглашение?", "Приглашение ещё не создано. Введённые настройки не сохранятся.", "Закрыть")) return;
    if (!valid(token)) return;
    show(invitePanel, false); $("#invite-open").setAttribute("aria-expanded", "false"); $("#invite-url").value = ""; $("#invite-open").focus();
  }
  $("#members-refresh").addEventListener("click", () => load(true));
  $("#invite-open").addEventListener("click", async () => {
    if (!invitePanel.hidden) { await closeInvite(); return; }
    if (!await releaseActive()) return;
    inviteForm.reset(); inviteForm.elements.role.replaceChildren(); allowed().forEach(r => { const o = node("option", roles[r]); o.value = r; inviteForm.elements.role.append(o); });
    sectionChecks($("#invite-sections"), ["messages", "tasks", "security"], "invite-section", inviteControls);
    invited = false; show(inviteForm); show($("#invite-result"), false); line($("#invite-error"), ""); line($("#invite-copy-status"), ""); $("#invite-url").value = "";
    inviteControls(); show(invitePanel); $("#invite-open").setAttribute("aria-expanded", "true"); inviteForm.elements.email.focus();
  });
  $("#team-invite-close").addEventListener("click", closeInvite);
  inviteForm.elements.role.addEventListener("change", inviteControls);
  inviteForm.addEventListener("submit", async event => {
    event.preventDefault(); if (inviting) return;
    const token = current(), workspace = state.workspace.id, role = inviteForm.elements.role.value, value = {email: inviteForm.elements.email.value.trim(), role, sections: admin(role) ? [] : chosen($("#invite-sections"))};
    if (!admin(role) && !value.sections.length) { line($("#invite-error"), "Выберите хотя бы один раздел.", true); return; }
    inviting = true; line($("#invite-error"), ""); inviteForm.querySelectorAll("input,select,button").forEach(c => c.disabled = true);
    try { const payload = await api(`/workspaces/${workspace}/invitations`, {method: "POST", data: value, timeoutMs: 20000}); if (!valid(token)) return; invited = true; $("#invite-url").value = payload.url; show(inviteForm, false); show($("#invite-result")); $("#invite-result-summary").textContent = `${roles[role]} · ${summary(value)}`; }
    catch (error) { if (valid(token) && error.name !== "AbortError") line($("#invite-error"), error.message, true); }
    finally { if (valid(token)) { inviting = false; inviteForm.querySelectorAll("input,select,button").forEach(c => c.disabled = false); } }
  });
  $("#invite-copy").addEventListener("click", async () => {
    const input = $("#invite-url"), token = current();
    try { await navigator.clipboard.writeText(input.value); if (valid(token)) line($("#invite-copy-status"), "Ссылка скопирована"); }
    catch (_) { if (valid(token)) { input.focus(); input.select(); line($("#invite-copy-status"), "Ссылка выделена. Скопируйте её через меню или Ctrl+C."); } }
  });
  return {load, reset};
};
