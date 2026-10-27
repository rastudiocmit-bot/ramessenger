"use strict";
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const state = { user: null, csrf: null, workspaces: [], workspace: null, pane: "messages", chatView: "all", invite: null, alfa: null, alfaBusy: false, alfaMax: null, alfaMaxBusy: false, epoch: 0 };
  const roles = { owner: "Владелец", admin: "Администратор", branch_manager: "Руководитель филиала", operator: "Оператор", observer: "Наблюдатель" };

  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  }
  function show(element, visible = true) { element.hidden = !visible; }
  // Light glass capsule for service notices and errors (notices.js); it can be
  // switched off in «Настройки мессенджера», then errors stay inline as before.
  const notices = window.CenterNotices ? window.CenterNotices({ $ }) : null;
  // Routine successes need no notification. Errors stay in the relevant form;
  // this helper never creates a floating layer, timer or focus change.
  function feedback(message, error = false, target = null) {
    if (!message) return;
    const dialog = [...document.querySelectorAll("dialog[open]")].at(-1);
    // Errors outside an open dialog or a named form go to the notice capsule; an
    // error inside a dialog stays next to the fields the operator is looking at.
    if (error && !target && !dialog && notices?.notify({ type: "error", title: String(message) })) {
      document.querySelectorAll(".action-feedback").forEach(line => line.remove());
      return;
    }
    const pane = document.querySelector(`[data-page="${state.pane}"]:not([hidden])`);
    target ||= dialog?.querySelector("form, .family-scroll, .family-panel") || dialog
      || (state.pane === "messages" ? (!$("#chat-compose").hidden ? $("#chat-compose") : $("#chat-list-notice")) : pane)
      || $("#auth");
    let line = target.querySelector(":scope > .action-feedback");
    if (!error) { line?.remove(); return; }
    if (!line) {
      line = node("p", "", "action-feedback"); line.setAttribute("role", "status");
      line.setAttribute("aria-live", "polite"); target.append(line);
    }
    line.textContent = message;
  }
  function notice(message, target = $("#auth-error")) {
    target.textContent = message || "";
    show(target, Boolean(message));
  }
  function showLogin(message = "") {
    show($("#loading"), false); show($("#cabinet"), false); show($("#auth"));
    show($("#login-form")); show($("#setup-form"), false); show($("#invite-form"), false);
    $("#auth-eyebrow").textContent = "Рады видеть вас";
    $("#auth-title").textContent = "Вход в кабинет";
    $("#auth-description").textContent = "Войдите с рабочей почтой и паролем.";
    notice(message);
    setTimeout(clearOrphanPassword, 400); setTimeout(clearOrphanPassword, 1500);
  }
  // A browser may fill in a password it saved for another rastudio.org page with no
  // user name ("Имя пользователя не указано"). That leaves dots in the field and an
  // empty e-mail, and some tablets make it hard to clear. Drop such a password so the
  // operator can simply type both fields; a suggestion is still one tap away.
  function clearOrphanPassword() {
    const form = $("#login-form"); if (form.hidden) return;
    const email = form.elements.email, password = form.elements.password;
    if (email.value || document.activeElement === password || document.activeElement === email) return;
    const filled = [":autofill", ":-webkit-autofill"].some(selector => { try { return password.matches(selector); } catch (_) { return false; } });
    if (filled) password.value = "";
  }
  function clearSession(message = "") {
    state.epoch += 1; state.user = null; state.csrf = null; state.workspaces = []; state.workspace = null;
    state.pane = "messages"; state.chatView = "all"; state.chatProtocol = 1;
    document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
    clearPasswords();
    clearPrivateViews();
    $("#invite-url").value = "";
    showLogin(message);
  }
  function clearPrivateViews() {
    $("#members-list").replaceChildren(); $("#sessions-list").replaceChildren(); resetAlfa(); resetAlfaMax(); directory.reset(); chat.reset();
    cabinet.reset();
    $("#workspace-select").replaceChildren(); $("#workspace-form").reset();
    ["#user-name", "#user-email", "#user-avatar", "#workspace-role", "#workspace-timezone"].forEach(selector => { $(selector).textContent = ""; });
    $("#invite-url").value = "";
    document.querySelectorAll(".action-feedback").forEach(line => line.remove());
  }
  function clearPasswords() {
    document.querySelectorAll('input[type="password"], .password-field input').forEach(input => { input.value = ""; input.type = "password"; });
    document.querySelectorAll(".reveal").forEach(button => { button.textContent = "Показать"; button.setAttribute("aria-label", "Показать пароль"); button.setAttribute("aria-pressed", "false"); });
  }
  async function api(path, { method = "GET", data, track = true, timeoutMs = 0, signal } = {}) {
    const epoch = state.epoch;
    const headers = { "Accept": "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
    }
    let response, payload = null;
    const controller = timeoutMs || signal ? new AbortController() : null;
    let timedOut = false;
    const abort = () => controller?.abort();
    if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
    const timeout = timeoutMs ? setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs) : null;
    try {
      response = await fetch(`/api${path}`, { method, headers, credentials: "same-origin", cache: "no-store", signal: controller?.signal, body: method === "GET" ? undefined : JSON.stringify(data || {}) });
      if (response.status !== 204) {
        try { payload = await response.json(); } catch (error) { if (controller?.signal.aborted) throw error; }
      }
    } catch (_) {
      if (signal?.aborted || (track && epoch !== state.epoch)) throw new DOMException("Отменено", "AbortError");
      const error = new Error(timedOut ? "Сервис не ответил вовремя. Попробуйте ещё раз." : "Не удалось связаться с сервисом. Проверьте соединение.");
      error.name = timedOut ? "TimeoutError" : "NetworkError"; error.code = timedOut ? "timeout" : "network";
      throw error;
    } finally { if (timeout !== null) clearTimeout(timeout); signal?.removeEventListener("abort", abort); }
    if (signal?.aborted || (track && epoch !== state.epoch)) throw new DOMException("Устаревший запрос", "AbortError");
    if (!response.ok) {
      const error = new Error(typeof payload?.detail === "string" ? payload.detail : "Не удалось выполнить действие. Попробуйте позже.");
      error.status = response.status;
      if (response.status === 401 && state.user) clearSession("Сессия завершена. Войдите снова.");
      throw error;
    }
    return payload;
  }
  async function busy(form, action, errorTarget = null) {
    const disabled = [...form.querySelectorAll("input,select,button")].map(control => [control, control.disabled]);
    disabled.forEach(([control]) => { control.disabled = true; });
    form.querySelectorAll(".action-feedback").forEach(line => line.remove());
    if (errorTarget) notice("", errorTarget);
    try { await action(); }
    catch (error) {
      if (error.name !== "AbortError") {
        if (errorTarget) notice(error.message, errorTarget); else feedback(error.message, true, form);
      }
    } finally { disabled.forEach(([control, previous]) => { control.disabled = previous; }); }
  }
  function initials(name) { return name.trim().split(/\s+/).slice(0, 2).map(part => part[0] || "").join("").toLocaleUpperCase("ru"); }
  function date(value) {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: state.workspace?.timezone || "Europe/Moscow" }).format(parsed);
  }
  function deviceLabel(raw) {
    const browser = /Edg\//.test(raw) ? "Microsoft Edge" : /OPR\//.test(raw) ? "Opera" : /Firefox\//.test(raw) ? "Firefox" : /Chrome\//.test(raw) ? "Chrome" : /Safari\//.test(raw) ? "Safari" : "Браузер";
    const platform = /Android/.test(raw) ? "Android" : /iPhone|iPad/.test(raw) ? "iOS" : /Windows/.test(raw) ? "Windows" : /Macintosh/.test(raw) ? "macOS" : /Linux/.test(raw) ? "Linux" : "";
    return platform ? `${browser} · ${platform}` : browser;
  }
  function setSession(payload) {
    if (state.user && state.user.id !== payload.user.id) clearPrivateViews();
    const previousWorkspace = state.workspace?.id;
    state.epoch += 1; state.user = payload.user; state.csrf = payload.csrf; state.workspaces = payload.workspaces; state.chatProtocol = payload.chat_protocol || 1;
    state.workspace = state.workspaces.find(item => item.id === state.workspace?.id) || state.workspaces[0] || null;
    if (previousWorkspace !== state.workspace?.id) { resetAlfa(); resetAlfaMax(); directory.reset(); chat.reset(); }
  }
  async function enter(payload) {
    setSession(payload);
    clearPasswords();
    if (state.invite) return showInvitation();
    show($("#loading"), false); show($("#auth"), false); show($("#cabinet"));
    $("#user-name").textContent = state.user.display_name;
    $("#user-email").textContent = state.user.email;
    $("#user-avatar").textContent = initials(state.user.display_name);
    const select = $("#workspace-select"); select.replaceChildren();
    state.workspaces.forEach(item => {
      const option = node("option", item.name); option.value = item.id; select.append(option);
    });
    select.disabled = state.workspaces.length < 2;
    show(select, state.workspaces.length > 1);
    if (state.workspace) select.value = state.workspace.id;
    else select.append(node("option", "Нет доступных центров"));
    await renderPane(state.pane);
  }
  async function refreshSession() { await enter(await api("/auth/session", { timeoutMs: 20000 })); }
  let accessRefresh = null;
  const refreshAccess = () => { accessRefresh ||= refreshSession().catch(() => {}).finally(() => { accessRefresh = null; }); };
  document.addEventListener("cm:chat-access-lost", refreshAccess);
  document.addEventListener("cm:chat-permissions-changed", refreshAccess);
  function grantedSections() {
    const workspace = state.workspace;
    const none = { overview: false, team: false, messages: false, clients: false, connections: false, security: true };
    if (!workspace) return none;
    if (["owner", "admin"].includes(workspace.role)) return { overview: true, team: true, messages: true, clients: true, connections: true, security: true };
    const granted = new Set(workspace.sections && workspace.sections.length ? workspace.sections : ["overview", "security"]);
    return { overview: granted.has("overview"), team: false, messages: granted.has("messages"), clients: ["branch_manager", "observer"].includes(workspace.role) && granted.has("messages"), connections: false, security: granted.has("security") };
  }
  const cabinet = window.CenterCabinet({ $, state, api, node, show, roles, renderPane });
  async function renderPane(pane) {
    state.pane = pane === "queue" ? "messages" : pane;
    const sections = grantedSections();
    sections.profile = true;
    sections.summary = sections.overview && sections.messages && state.workspace?.role !== "operator";
    if (!sections[state.pane]) state.pane = ["messages", "summary", "clients", "overview", "security", "profile"].find(key => sections[key]);
    if (state.pane !== "connections") alfaMaxProgress.reset();

    if (state.pane !== "clients") directory.leave();
    if (state.pane !== "messages") chat.leave();
    show($("#no-workspaces"), !state.workspace && state.pane !== "security" && state.pane !== "profile");
    document.querySelectorAll("[data-page]").forEach(element => show(element, (Boolean(state.workspace) || ["security", "profile"].includes(state.pane)) && element.id === `pane-${state.pane}`));
    cabinet.update(sections);
    chat.watch();
    if (!state.workspace) { if (state.pane === "security") await loadSessions(); return; }
    const manage = Boolean(state.workspace && ["owner", "admin"].includes(state.workspace.role));
    $("#workspace-role").textContent = roles[state.workspace.role] || "Сотрудник";
    $("#workspace-timezone").textContent = state.workspace.timezone === "Europe/Moscow" ? "Москва · UTC+3" : state.workspace.timezone;
    if ($("#workspace-timezone-input")) $("#workspace-timezone-input").value = $("#workspace-timezone").textContent;
    const form = $("#workspace-form");
    form.elements.name.value = state.workspace.name;
    form.elements.name.disabled = !manage;
    show($("button[type=submit]", form), Boolean(manage));
    show($("#workspace-readonly"), !manage);
    if (state.pane === "connections") await Promise.allSettled([ai.load(), loadAlfa().catch(error => { notice(error.message, $("#alfa-notice")); }), loadAlfaMax().catch(error => { notice(error.message, $("#alfa-max-error")); }), chat.connections()]);
    if (state.pane === "messages") { chat.setView(state.chatView || "all", false); await chat.enter(); }
    if (state.pane === "summary") await cabinet.summary();
    if (state.pane === "clients") await directory.enter();
    if (["profile", "overview"].includes(state.pane) && sections.messages) await cabinet.metadata();
    if (state.pane === "team") await loadMembers();
    if (state.pane === "security") await loadSessions();
  }
  const sectionMeta = {
    overview: { icon: "◈", title: "Организация", hint: "Название центра и общая информация" },
    messages: { icon: "▤", title: "Сообщения", hint: "Переписка с клиентами в MAX" },
    security: { icon: "◇", title: "Безопасность", hint: "Пароль и сессии сотрудника" },
  };
  let sectionsMember = null;
  function openMemberSections(member) {
    sectionsMember = member;
    $("#member-sections-avatar").textContent = initials(member.display_name);
    $("#member-sections-name").textContent = member.display_name;
    $("#member-sections-role").textContent = `${roles[member.role] || "Сотрудник"} · ${member.email}`;
    const box = $("#member-sections-checks"); box.replaceChildren();
    ["overview", "messages", "security"].forEach(key => {
      const meta = sectionMeta[key];
      const row = node("div", undefined, "section-option");
      const icon = node("span", meta.icon, "section-option-icon");
      const text = node("div", undefined, "section-option-text");
      text.append(node("strong", meta.title), node("small", meta.hint));
      const label = node("label", undefined, "switch");
      const input = node("input"); input.type = "checkbox"; input.value = key;
      input.checked = (member.sections && member.sections.length ? member.sections : ["overview", "security"]).includes(key);
      label.append(input, node("span", undefined, "switch-slider"));
      row.append(icon, text, label);
      box.append(row);
    });
    notice("", $("#member-sections-error"));
    $("#member-sections-dialog").showModal();
  }
  async function setMemberRole(member, role) {
    const workspace = state.workspace;
    await busy($("#members-list"), async () => {
      await api(`/workspaces/${workspace.id}/members/${member.user_id}/role`, { method: "POST", data: { role } });
      feedback("Роль сотрудника обновлена."); await loadMembers();
    });
  }
  async function loadMembers() {
    const workspace = state.workspace;
    const payload = await api(`/workspaces/${workspace.id}/members`);
    if (workspace.id !== state.workspace?.id) return;
    const list = $("#members-list"); list.replaceChildren();
    if ($("#team-count")) $("#team-count").textContent = `${payload.members.length} человек в центре`;
    payload.members.forEach(member => {
      const row = node("div", undefined, "mrow member-row");
      const avatar = node("span", initials(member.display_name), "ava me");
      const info = node("span", undefined, "mrow-main"); info.append(node("strong", member.display_name), node("small", member.email));
      const pill = node("span", roles[member.role], `rolepill ${member.role === "branch_manager" ? "branch" : member.role}`);
      row.append(avatar, info, pill);
      const canManage = member.user_id !== state.user.id && member.role !== "owner" && (workspace.role === "owner" || member.role !== "admin");
      if (canManage) {
        const gear = node("button", undefined, "iconbtn plain"); gear.type = "button"; gear.setAttribute("aria-label", `Действия с сотрудником: ${member.display_name}`); gear.append(window.CenterIcon("cog"));
        gear.addEventListener("click", () => openMemberOptions(member)); row.append(gear);
      } else row.append(node("span", undefined, "member-action-space"));
      list.append(row);
    });
    show($("#team-empty"), payload.members.length === 0);
  }
  function openMemberOptions(member) {
    const workspace = state.workspace, dialog = $("#member-options-dialog"), body = $("#member-options-body");
    $("#member-options-name").textContent = member.display_name; body.replaceChildren();
    const label = node("label", "Роль сотрудника", "fld"), select = node("select"); select.setAttribute("aria-label", "Роль сотрудника");
    const allowed = workspace.role === "owner" ? ["admin", "branch_manager", "operator", "observer"] : ["branch_manager", "operator", "observer"];
    for (const role of allowed) select.append(new Option(roles[role], role)); select.value = member.role; label.append(select);
    const save = node("button", "Сохранить роль", "btn"); save.type = "button";
    save.addEventListener("click", () => busy(dialog, async () => { await setMemberRole(member, select.value); dialog.close(); }));
    body.append(label, save);
    if (!["owner", "admin"].includes(member.role)) {
      const sections = node("button", "Доступные разделы", "btn sec"); sections.type = "button";
      sections.addEventListener("click", () => { dialog.close(); openMemberSections(member); }); body.append(sections);
    }
    const remove = node("button", "Отозвать доступ", "btn sec danger-text"); remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!await confirm("Отозвать доступ?", `${member.display_name} потеряет доступ к этому центру.`, "Отозвать доступ")) return;
      await busy(dialog, async () => { await api(`/workspaces/${workspace.id}/members/${member.user_id}/remove`, { method: "POST" }); dialog.close(); feedback("Доступ сотрудника отозван."); await loadMembers(); });
    }); body.append(remove); dialog.showModal();
  }
  async function loadSessions() {
    const payload = await api("/auth/sessions");
    const list = $("#sessions-list"); list.replaceChildren();
    payload.sessions.forEach(session => {
      const row = node("div", undefined, "session-row split"), details = node("div");
      details.append(node("strong", deviceLabel(session.device_label)), node("small", `Активность: ${date(session.last_seen_at)}`));
      if (session.current) details.append(node("span", "Текущая сессия", "small-tag"));
      row.append(details);
      if (!session.current) {
        const button = node("button", "Завершить", "secondary"); button.type = "button";
        button.addEventListener("click", () => busy(row, async () => { await api(`/auth/sessions/${session.id}/revoke`, { method: "POST" }); feedback("Сессия завершена."); await loadSessions(); }));
        row.append(button);
      }
      list.append(row);
    });
  }
  function confirm(title, description, yes) {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title; $("#confirm-description").textContent = description;
    $("#confirm-yes").textContent = yes; dialog.returnValue = ""; dialog.showModal();
    return new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue === "yes"), { once: true }));
  }
  async function showInvitation() {
    const payload = await api("/invitations/preview", { method: "POST", data: { token: state.invite } });
    show($("#loading"), false); show($("#cabinet"), false); show($("#auth"));
    show($("#login-form"), false); show($("#setup-form"), false); show($("#invite-form"));
    $("#auth-eyebrow").textContent = "Присоединиться к команде";
    $("#auth-title").textContent = "Вас пригласили";
    $("#auth-description").textContent = `Ваша роль: ${roles[payload.role] || "Сотрудник"}.`;
    const form = $("#invite-form"); form.elements.email.value = payload.email;
    const needAccount = !state.user;
    show($("#invite-new-fields"), needAccount);
    form.elements.display_name.required = needAccount;
    form.elements.password.required = needAccount;
    form.elements.confirm_password.required = needAccount;
    show($("#invite-login"), !state.user || state.user.email !== payload.email);
    $("#invite-login").textContent = state.user ? "Выйти и продолжить приглашение" : "У меня уже есть аккаунт";
    notice(state.user && state.user.email !== payload.email ? "Для принятия приглашения войдите с указанной почтой." : "");
  }

  $("#login-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    busy(form, async () => { const data = { email: form.elements.email.value, password: form.elements.password.value }; await enter(await api("/auth/login", { method: "POST", data })); form.reset(); }, $("#auth-error"));
  });
  $("#setup-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    if (form.elements.password.value !== form.elements.confirm_password.value) return notice("Пароли не совпадают.");
    busy(form, async () => {
      const fields = ["email", "password", "setup_code", "display_name", "workspace_name"];
      const data = Object.fromEntries(fields.map(name => [name, form.elements[name].value]));
      await enter(await api("/auth/setup", { method: "POST", data })); form.reset();
    }, $("#auth-error"));
  });
  $("#invite-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    busy(form, async () => {
      if (!state.user && form.elements.password.value !== form.elements.confirm_password.value) { notice("Пароли не совпадают."); return; }
      const data = { token: state.invite };
      if (!state.user) { data.display_name = form.elements.display_name.value; data.password = form.elements.password.value; }
      const payload = await api("/invitations/accept", { method: "POST", data });
      state.invite = null; await enter(payload); form.reset(); feedback("Доступ к центру предоставлен.");
    }, $("#auth-error"));
  });
  $("#invite-login").addEventListener("click", async () => {
    const email = $("#invite-form").elements.email.value;
    if (state.user) {
      try { await api("/auth/logout", { method: "POST" }); clearSession(); await showInvitation(); }
      catch (error) { notice(error.message); }
      return;
    }
    showLogin("Войдите с почтой приглашения. Затем вы сможете принять его.");
    $("#login-form").elements.email.value = email;
  });
  $("#invite-cancel").addEventListener("click", async () => {
    state.invite = null;
    if (state.user) { try { await refreshSession(); } catch (error) { clearSession(error.message); } }
    else showLogin();
  });
  document.querySelectorAll(".reveal").forEach(button => button.addEventListener("click", () => {
    const input = $("input", button.parentElement), reveal = input.type === "password";
    input.type = reveal ? "text" : "password"; button.textContent = reveal ? "Скрыть" : "Показать";
    button.setAttribute("aria-label", reveal ? "Скрыть пароль" : "Показать пароль"); button.setAttribute("aria-pressed", String(reveal));
  }));
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-pane]");
    if (!button || button.disabled) return;
    state.chatView = "all";
    cabinet.navigate(button.dataset.pane).catch(error => { if (error.name !== "AbortError") feedback(error.message, true); });
  });
  $("#workspace-select").addEventListener("change", async event => {
    state.epoch += 1; state.workspace = state.workspaces.find(item => item.id === event.target.value) || null; resetAlfa(); resetAlfaMax(); directory.reset(); chat.reset();
    try { await renderPane(state.pane); } catch (error) { if (error.name !== "AbortError") feedback(error.message, true); }
  });
  $("#workspace-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, workspace = state.workspace;
    busy(form, async () => { const name = form.elements.name.value.trim(); await api(`/workspaces/${workspace.id}`, { method: "PATCH", data: { name } }); workspace.name = name; await refreshSession(); feedback("Название центра сохранено."); });
  });
  $("#logout").addEventListener("click", async () => {
    try { await api("/auth/logout", { method: "POST" }); clearSession(); }
    catch (error) { if (error.name !== "AbortError") feedback(error.message, true); }
  });
  $("#password-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    if (form.elements.new_password.value !== form.elements.confirm_password.value) return feedback("Новые пароли не совпадают.", true);
    busy(form, async () => { await api("/auth/password", { method: "POST", data: { current_password: form.elements.current_password.value, new_password: form.elements.new_password.value } }); form.reset(); clearSession("Пароль изменён. Войдите с новым паролем."); });
  });
  $("#logout-all").addEventListener("click", async () => {
    if (!await confirm("Выйти на всех устройствах?", "Все текущие сессии будут завершены, включая эту.", "Выйти везде")) return;
    try { await api("/auth/logout-all", { method: "POST" }); clearSession("Все сессии завершены."); } catch (error) { if (error.name !== "AbortError") feedback(error.message, true); }
  });
  $("#members-refresh").addEventListener("click", () => loadMembers().catch(error => { if (error.name !== "AbortError") feedback(error.message, true); }));
  $("#member-sections-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, member = sectionsMember, workspace = state.workspace;
    if (!member) return;
    const selected = [...$("#member-sections-checks").querySelectorAll("input")].filter(input => input.checked).map(input => input.value);
    busy(form, async () => {
      await api(`/workspaces/${workspace.id}/members/${member.user_id}/sections`, { method: "POST", data: { sections: selected } });
      $("#member-sections-dialog").close(); feedback("Права сотрудника сохранены."); await loadMembers();
    }, $("#member-sections-error"));
  });
  $("#sessions-refresh").addEventListener("click", () => loadSessions().catch(error => { if (error.name !== "AbortError") feedback(error.message, true); }));
  $("#access-refresh").addEventListener("click", () => refreshSession().catch(error => { if (error.name !== "AbortError") feedback(error.message, true); }));
  $("#invite-open").addEventListener("click", () => {
    $("#create-invite-form").reset(); show($("#create-invite-form")); show($("#invite-result"), false); notice("", $("#invite-error"));
    $("#admin-role-option").hidden = state.workspace.role !== "owner";
    $("#invite-url").value = ""; $("#invitation-dialog").showModal();
  });
  $("#create-invite-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    busy(form, async () => {
      const payload = await api(`/workspaces/${state.workspace.id}/invitations`, { method: "POST", data: { email: form.elements.email.value, role: form.elements.role.value } });
      $("#invite-url").value = payload.url; show(form, false); show($("#invite-result"));
    }, $("#invite-error"));
  });
  $("#invite-copy").addEventListener("click", async () => {
    const input = $("#invite-url");
    try { await navigator.clipboard.writeText(input.value); feedback("Ссылка скопирована."); }
    catch (_) { input.focus(); input.select(); feedback("Ссылка выделена. Скопируйте её через меню или Ctrl+C."); }
  });
  document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => document.getElementById(button.dataset.close).close()));
  $("#invitation-dialog").addEventListener("close", () => { $("#invite-url").value = ""; });
  $("#confirm-cancel").addEventListener("click", () => $("#confirm-dialog").close("no"));
  $("#confirm-yes").addEventListener("click", () => $("#confirm-dialog").close("yes"));

  function resetAlfa() {
    state.alfa = null; state.alfaBusy = false;
    $("#alfa-form").reset(); $("#alfa-branches").replaceChildren();
    ["#alfa-status", "#alfa-cache-time"].forEach(selector => { $(selector).textContent = ""; });
    $("#alfa-branch-count").textContent = "0"; notice("", $("#alfa-notice"));
    connectionSummary("#alfa-connection-summary", "Проверяем подключение…", "waiting");
    $("#alfa-config").open = true;
    alfaButtons();
  }
  const connectionNames = { "#alfa-connection-summary": "AlfaCRM", "#alfa-max-connection-summary": "MAX через AlfaCRM", "#alfa-history-summary": "История MAX" };
  const connectionModes = {};
  function connectionSummary(selector, text, mode) {
    const label = $(selector); label.textContent = text; label.closest(".connection-tile").dataset.state = mode;
    // A service that breaks says so once; when it is back, the notice goes away.
    const before = connectionModes[selector]; connectionModes[selector] = mode;
    if (mode === "error" && before !== "error") notices?.notify({ type: "warning", title: connectionNames[selector] || "Подключение", text,
      key: "connection:" + selector, action: { label: "Открыть", run: () => { $("#connections-tab")?.click(); } } });
    else if (mode === "active" && before === "error") notices?.resolve("connection:" + selector);
  }
  function connectionView(view) {
    if (!["monitor", "settings", "voice", "agents"].includes(view)) view = "monitor";
    state.connectionsView = view;
    for (const name of ["monitor", "settings", "voice", "agents"]) {
      show($("#connections-" + name + "-view"), name === view);
      $("#connections-" + name + "-tab").setAttribute("aria-selected", String(name === view));
      $("#connections-" + name + "-tab").tabIndex = name === view ? 0 : -1;
    }
  }
  for (const button of document.querySelectorAll("[data-connection-view]")) button.addEventListener("click", () => connectionView(button.dataset.connectionView));
  $(".connection-tabs").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const names = ["monitor", "settings", "voice", "agents"], index = names.indexOf(state.connectionsView || "monitor");
    const view = event.key === "Home" ? names[0] : event.key === "End" ? names.at(-1)
      : names[(index + (event.key === "ArrowLeft" ? names.length - 1 : 1)) % names.length];
    connectionView(view); $("#connections-" + view + "-tab").focus();
  });
  function alfaButtons() {
    const ready = Boolean(state.alfa?.vault_ready), configured = Boolean(state.alfa?.configured);
    $("#alfa-form").querySelectorAll("input,button").forEach(control => { control.disabled = !ready || state.alfaBusy; });
    $("#alfa-check").disabled = !ready || !configured || state.alfaBusy;
    $("#alfa-disconnect").disabled = !configured || state.alfaBusy;
    $("#alfa-save").textContent = state.alfaBusy ? "Обновляем…" : "Сохранить и подключить";
  }
  async function loadAlfa() {
    const workspace = state.workspace;
    const payload = await api(`/workspaces/${workspace.id}/integrations/alfa`);
    if (workspace.id !== state.workspace?.id) return;
    if (state.alfa?.configured !== payload.configured) $("#alfa-config").open = !payload.configured;
    state.alfa = payload;
    const form = $("#alfa-form"); form.elements.origin.value = payload.origin;
    form.elements.email.value = payload.email; form.elements.seed_branch_id.value = payload.seed_branch_id || "";
    form.elements.api_key.value = ""; form.elements.api_key.type = "password";
    const reveal = $(".reveal", form); reveal.textContent = "Показать"; reveal.setAttribute("aria-label", "Показать пароль"); reveal.setAttribute("aria-pressed", "false");
    form.elements.api_key.required = !payload.configured;
    form.elements.api_key.placeholder = payload.configured ? "Ключ сохранён" : "Введите ключ API";
    $("#alfa-key-note").textContent = payload.configured ? "Оставьте пустым, чтобы сохранить действующий ключ." : "Ключ находится в профиле пользователя АльфаCRM.";
    const statuses = { active: "Подключено", reauth_required: "Нужна проверка доступа", error: "Ошибка связи" };
    $("#alfa-status").textContent = payload.configured ? (statuses[payload.status] || "Требует проверки") : "Не настроено";
    $("#alfa-status").dataset.state = payload.status === "active" ? "active" : payload.configured ? "error" : "waiting";
    connectionSummary("#alfa-connection-summary", payload.configured && payload.status === "active"
      ? `Подключено · филиалов ${payload.branches.length}` : payload.configured ? "Нужна проверка доступа" : "Не подключена", $("#alfa-status").dataset.state);
    $("#alfa-status").classList.toggle("integration-error", ["reauth_required", "error"].includes(payload.status));
    $("#alfa-cache-time").textContent = payload.last_success_at ? `Список сохранён: ${date(payload.last_success_at)}` : "Список ещё не загружен.";
    $("#alfa-branch-count").textContent = String(payload.branches.length);
    const list = $("#alfa-branches"); list.replaceChildren();
    payload.branches.forEach(branch => { const row = node("div", undefined, "branch-row"); row.append(node("span", "", "branch-dot"), node("span", branch.name, "branch-name")); list.append(row); });
    show($("#alfa-empty"), payload.branches.length === 0);
    notice(!payload.vault_ready ? "Хранилище подключений ещё не подготовлено. Требуется настройка ключа на сервере." : payload.error, $("#alfa-notice"));
    alfaButtons();
  }
  async function alfaAction(action) {
    if (state.alfaBusy) return;
    const epoch = state.epoch, workspace = state.workspace;
    state.alfaBusy = true; alfaButtons(); notice("", $("#alfa-notice"));
    try {
      await action(`/workspaces/${workspace.id}/integrations/alfa`);
      directory.reset();
      await loadAlfa(); feedback("Подключение обновлено.");
    } catch (error) {
      if (error.name !== "AbortError" && epoch === state.epoch) {
        try { await loadAlfa(); } catch (_) { /* Preserve the original actionable error. */ }
        if (epoch === state.epoch) notice(error.message, $("#alfa-notice"));
      }
    } finally {
      if (epoch === state.epoch) { state.alfaBusy = false; alfaButtons(); }
    }
  }
  $("#alfa-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    const data = { origin: form.elements.origin.value.trim(), email: form.elements.email.value.trim(), seed_branch_id: form.elements.seed_branch_id.value ? Number(form.elements.seed_branch_id.value) : null };
    if (form.elements.api_key.value.trim()) data.api_key = form.elements.api_key.value.trim();
    alfaAction(async path => {
      await api(path, { method: "PUT", data }); form.elements.api_key.value = "";
      await api(`${path}/check`, { method: "POST" });
    });
  });
  $("#alfa-check").addEventListener("click", () => alfaAction(path => api(`${path}/check`, { method: "POST" })));
  $("#alfa-disconnect").addEventListener("click", async () => {
    const epoch = state.epoch;
    if (!await confirm("Отключить АльфаCRM?", "Ключ API будет удалён из подключения. История мессенджера сохранится.", "Отключить")) return;
    if (epoch !== state.epoch) return;
    await alfaAction(path => api(path, { method: "DELETE" }));
  });

  let alfaSessionTimer = null, alfaReconnectEditing = false;
  function resetAlfaMax() {
    clearTimeout(alfaSessionTimer); alfaSessionTimer = null; alfaReconnectEditing = false;
    alfaMaxProgress.reset();
    connectionView("monitor"); show($("#history-monitor-empty"), false);
    state.alfaMax = null; state.alfaMaxBusy = false;
    $("#alfa-max-connect-form").reset(); $("#alfa-max-otp-form").reset();
    $("#alfa-max-status").textContent = "Не подключено";
    $("#alfa-max-info").textContent = "";
    connectionSummary("#alfa-max-connection-summary", "Проверяем веб-сессию…", "waiting");
    connectionSummary("#alfa-history-summary", "Получаем состояние…", "waiting");
    notice("", $("#alfa-max-error"));
    show($("#alfa-max-otp-form"), false); show($("#alfa-max-actions"), false);
    show($("#alfa-max-connect-form"));
    alfaMaxButtons();
  }
  function alfaMaxButtons() {
    const configured = Boolean(state.alfaMax?.configured);
    const active = state.alfaMax?.state === "active";
    const vault = Boolean(state.alfaMax?.vault_ready);
    const crmReady = Boolean(state.alfaMax?.crm_configured && state.alfaMax?.branches?.length);
    $("#alfa-max-connect-form").querySelectorAll("input,button").forEach(control => {
      control.disabled = state.alfaMaxBusy || !vault || !crmReady;
    });
    $("#alfa-max-otp-form").querySelectorAll("input,button").forEach(control => {
      control.disabled = state.alfaMaxBusy || !vault;
    });
    $("#alfa-max-sync").disabled = state.alfaMaxBusy || !active;
    $("#alfa-max-disconnect").disabled = state.alfaMaxBusy || !configured;
    $("#alfa-max-connect").textContent = state.alfaMaxBusy ? "Подключаем…" : state.alfaMax?.state === "reauth_required" ? "Войти заново" : "Подключить MAX";
  }
  async function loadAlfaMax() {
    const workspace = state.workspace, loadEpoch = state.epoch;
    const payload = await api("/workspaces/" + workspace.id + "/integrations/alfa-max");
    if (workspace.id !== state.workspace?.id || loadEpoch !== state.epoch) return;
    const previousState = state.alfaMax?.state;
    state.alfaMax = payload;
    const recovery = payload.recovery || {};
    const restoring = ["checking", "reconnecting", "waiting"].includes(recovery.phase);
    const sessionLabel = restoring ? (recovery.phase === "checking" ? "Проверяем авторизацию" : "Восстанавливаем подключение автоматически") : null;
    const labels = {
      not_configured: "Не подключено",
      otp_required: "Нужен код из письма",
      active: "Подключено",
      reauth_required: "Нужно войти заново",
      error: "Ошибка подключения",
    };
    $("#alfa-max-status").textContent = sessionLabel || labels[payload.state] || "Состояние не определено";
    $("#alfa-max-status").dataset.state = restoring ? "waiting" : payload.state === "active" ? "active" : ["error", "reauth_required"].includes(payload.state) ? "error" : "waiting";
    connectionSummary("#alfa-max-connection-summary", sessionLabel || labels[payload.state] || "Нужна проверка", $("#alfa-max-status").dataset.state);
    $("#alfa-max-status").classList.toggle("integration-error", ["reauth_required", "error"].includes(payload.state));
    const form = $("#alfa-max-connect-form");
    if (!alfaReconnectEditing) {
      if (payload.email) form.elements.email.value = payload.email;
      form.elements.password.value = "";
    }
    show(form, alfaReconnectEditing || (!restoring && payload.state !== "otp_required" && payload.state !== "active"));
    if (!alfaReconnectEditing && form.elements.auto_reconnect) form.elements.auto_reconnect.checked = recovery.configured ? Boolean(recovery.enabled) : true;
    show($("#alfa-max-otp-form"), payload.state === "otp_required");
    show($("#alfa-max-actions"), payload.configured);
    const details = [];
    details.push(recovery.configured && !recovery.enabled ? "Автоматическое восстановление выключено" : recovery.credentials_saved && recovery.enabled ? "Автоматический вход включён" : "Для автоматического входа один раз сохраните данные подключения");
    if (recovery.after && restoring) details.push("Следующая проверка: " + date(recovery.after));
    if (payload.last_sync_at) details.push("Последняя синхронизация: " + date(payload.last_sync_at));
    if (payload.crm_configured && payload.branches?.length) details.push("Филиалов: " + payload.branches.length);
    if (!payload.crm_configured) details.push("Сначала настройте подключение AlfaCRM в соседнем блоке.");
    else if (!payload.branches?.length) details.push("Сначала загрузите филиалы AlfaCRM.");
    $("#alfa-max-info").textContent = details.join(" · ");
    notice(!payload.vault_ready ? "На сервере не настроен ключ хранения веб-сессий." : ((alfaReconnectEditing && recovery.manual_error) || payload.error || ""), $("#alfa-max-error"));
    const events = $("#alfa-max-session-events");
    if (events) {
      const names = { authorization_check_requested: "Запрошена проверка авторизации", automatic_login_started: "Начат автоматический вход", confirmation_completed: "Код принят, проверяем подключение", manual_disconnect: "Подключение отключено администратором", active: "Авторизация подтверждена", checking: "Проверяем новую сессию", manual_login: "Вход через настройки", manual_login_failed: "Попытка входа не завершена", otp_required: "Нужен код подтверждения", recovery_credentials_required: "Нужны данные для автоматического входа", recovery_retry: "Проверка отложена до повторной попытки", credentials_rejected: "Данные входа не приняты" };
      events.replaceChildren(...(recovery.recent || []).slice().reverse().map(item => node("p", `${date(item.at)} · ${names[item.event] || "Изменение подключения"}`, "form-note")));
      if (!events.children.length) events.append(node("p", "События появятся при следующей проверке подключения.", "form-note"));
    }
    alfaMaxButtons();
    const hasSession = payload.configured && payload.state !== "not_configured";
    show($("#history-monitor-empty"), !hasSession);
    if (previousState !== payload.state && payload.state !== "active" && !["voice", "agents"].includes(state.connectionsView)) connectionView("settings");
    if (hasSession) alfaMaxProgress.start();
    else { alfaMaxProgress.reset(); connectionSummary("#alfa-history-summary", "Сначала подключите MAX", "waiting"); }
    clearTimeout(alfaSessionTimer);
    const epoch = state.epoch;
    alfaSessionTimer = setTimeout(() => {
      const poll = () => {
        if (epoch !== state.epoch || state.pane !== "connections") return;
        loadAlfaMax().catch(() => { alfaSessionTimer = setTimeout(poll, 5000); });
      };
      poll();
    }, restoring || payload.state === "otp_required" ? 5000 : 15000);
  }
  $("#alfa-max-recovery-setup")?.addEventListener("click", () => {
    alfaReconnectEditing = true;
    show($("#alfa-max-connect-form")); $("#alfa-max-connect-form").elements.password.focus();
  });
  async function alfaMaxAction(action, success) {
    if (state.alfaMaxBusy || !state.workspace) return;
    const epoch = state.epoch, workspace = state.workspace;
    state.alfaMaxBusy = true; alfaMaxButtons(); notice("", $("#alfa-max-error"));
    try {
      const result = await action("/workspaces/" + workspace.id + "/integrations/alfa-max");
      if (epoch !== state.epoch) return;
      await loadAlfaMax(); await chat.connections(); feedback(typeof success === "function" ? success(result) : success);
    } catch (error) {
      if (error.name !== "AbortError" && epoch === state.epoch) {
        try { await loadAlfaMax(); } catch (_) { /* Keep the action error visible. */ }
        if (epoch === state.epoch) notice(error.message, $("#alfa-max-error"));
      }
    } finally {
      if (epoch === state.epoch) { state.alfaMaxBusy = false; alfaMaxButtons(); }
    }
  }
  $("#alfa-max-connect-form").addEventListener("input", () => { alfaReconnectEditing = true; });
  $("#alfa-max-connect-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = { email: form.elements.email.value.trim(), password: form.elements.password.value, auto_reconnect: Boolean(form.elements.auto_reconnect?.checked) };
    alfaMaxAction(async path => {
      const result = await api(path + "/connect", { method: "POST", data });
      form.elements.password.value = "";
      alfaReconnectEditing = false;
      state.alfaMax = result;
    }, "Запрос AlfaCRM MAX выполнен.");
    form.elements.password.value = "";
  });
  $("#alfa-max-otp-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = { code: form.elements.code.value };
    alfaMaxAction(path => api(path + "/verify", { method: "POST", data }), "Вход в AlfaCRM подтверждён.");
    form.reset();
  });
  $("#alfa-max-sync").addEventListener("click", () => alfaMaxAction(path => api(path + "/sync", { method: "POST" }),
    result => result?.already_running ? "Проверка уже выполняется на сервере." : "Проверка новых сообщений поставлена в очередь."));
  $("#alfa-max-disconnect").addEventListener("click", async () => {
    const epoch = state.epoch;
    if (!await confirm("Отключить штатный MAX?", "Зашифрованная веб-сессия AlfaCRM будет удалена. История переписки останется в кабинете.", "Отключить")) return;
    if (epoch === state.epoch) await alfaMaxAction(path => api(path, { method: "DELETE" }), "Веб-сессия AlfaCRM MAX отключена.");
  });

  async function start() {
    const raw = new URLSearchParams(location.hash.slice(1)).get("invite");
    if (raw) { history.replaceState(null, "", location.pathname + location.search); if (/^[A-Za-z0-9_-]{43}$/.test(raw)) state.invite = raw; }
    try {
      let payload = null;
      try { payload = await api("/auth/session", { timeoutMs: 20000 }); } catch (error) { if (error.status !== 401) throw error; }
      if (payload) return await enter(payload);
      const status = await api("/auth/status", { timeoutMs: 20000 });
      if (state.invite) return await showInvitation();
      if (status.setup_required && status.setup_enabled) {
        showLogin(); show($("#login-form"), false); show($("#setup-form"));
        $("#auth-eyebrow").textContent = "Начнём знакомство"; $("#auth-title").textContent = "Создать кабинет";
        $("#auth-description").textContent = "Первый аккаунт владельца организации.";
      } else showLogin(status.setup_required ? "Первичная настройка пока недоступна. Обратитесь к владельцу сервиса." : "");
    } catch (error) { showLogin(error.message); }
  }
  const directory = window.CenterDirectory({ $, state, api, node, show, notice, date });
  const chat = window.CenterChat({ $, state, api, node, show, notice, date, toast: feedback, confirm, renderPane });
  window.CenterVoice({ $, state, api, toast: feedback });
  const ai = window.CenterAi({ $, state, api, node, show, notice, toast: feedback, confirm });
  const alfaMaxProgress = window.CenterAlfaProgress({ $, state, api, show, date, onStatus: (payload, view) => {
    connectionSummary("#alfa-history-summary", view.title, view.mode === "complete" ? "active" : ["error", "offline", "attention"].includes(view.mode) ? "error" : "waiting");
    if (payload && state.alfaMax && payload.state !== state.alfaMax.state) loadAlfaMax().catch(error => { if (error.name !== "AbortError") notice(error.message, $("#alfa-max-error")); });
  } });
  start();
})();
