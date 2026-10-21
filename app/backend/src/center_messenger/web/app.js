"use strict";
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const state = { user: null, csrf: null, workspaces: [], workspace: null, pane: new URLSearchParams(location.search).get("view") === "messages" ? "messages" : "overview", invite: null, alfa: null, alfaBusy: false, alfaMax: null, alfaMaxBusy: false, epoch: 0 };
  const roles = { owner: "Владелец", admin: "Администратор", branch_manager: "Руководитель филиала", operator: "Оператор", observer: "Наблюдатель" };
  const pageHeadings = new Map([...document.querySelectorAll(".pane")].map(pane => [pane.id.replace(/^pane-/, ""), $(".page-heading", pane)]).filter(([, heading]) => heading));
  let toastTimer;

  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  }
  function show(element, visible = true) { element.hidden = !visible; }
  function toast(message, error = false) {
    const target = $("#toast");
    target.textContent = message;
    target.classList.toggle("error-toast", error);
    show(target);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => show(target, false), 6500);
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
  }
  function clearSession(message = "") {
    state.epoch += 1; state.user = null; state.csrf = null; state.workspaces = []; state.workspace = null;
    document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
    clearPasswords();
    clearPrivateViews();
    $("#invite-url").value = "";
    showLogin(message);
  }
  function clearPrivateViews() {
    $("#members-list").replaceChildren(); $("#sessions-list").replaceChildren(); resetAlfa(); resetAlfaMax(); directory.reset(); chat.reset();
    const activeHeading = $("#topbar-context .page-heading");
    if (activeHeading) $("#page-heading-storage").append(activeHeading);
    $("#workspace-select").replaceChildren(); $("#workspace-form").reset();
    ["#user-name", "#user-email", "#user-avatar", "#workspace-role", "#workspace-timezone"].forEach(selector => { $(selector).textContent = ""; });
    $("#invite-url").value = ""; show($("#toast"), false);
  }
  function clearPasswords() {
    document.querySelectorAll('input[type="password"], .password-field input').forEach(input => { input.value = ""; input.type = "password"; });
    document.querySelectorAll(".reveal").forEach(button => { button.textContent = "Показать"; button.setAttribute("aria-label", "Показать пароль"); button.setAttribute("aria-pressed", "false"); });
  }
  async function api(path, { method = "GET", data, track = true } = {}) {
    const epoch = state.epoch;
    const headers = { "Accept": "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
    }
    let response;
    try {
      response = await fetch(`/api${path}`, { method, headers, credentials: "same-origin", cache: "no-store", body: method === "GET" ? undefined : JSON.stringify(data || {}) });
    } catch (_) { throw new Error("Не удалось связаться с сервисом. Проверьте соединение."); }
    if (track && epoch !== state.epoch) throw new DOMException("Устаревший запрос", "AbortError");
    let payload = null;
    if (response.status !== 204) {
      try { payload = await response.json(); } catch (_) { /* Generic failure below. */ }
    }
    if (track && epoch !== state.epoch) throw new DOMException("Устаревший запрос", "AbortError");
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
    if (errorTarget) notice("", errorTarget);
    try { await action(); }
    catch (error) {
      if (error.name !== "AbortError") {
        if (errorTarget) notice(error.message, errorTarget); else toast(error.message, true);
      }
    } finally { disabled.forEach(([control, previous]) => { control.disabled = previous; }); }
  }
  function initials(name) { return name.trim().split(/\s+/).slice(0, 2).map(part => part[0] || "").join("").toLocaleUpperCase("ru"); }
  function date(value) {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(parsed);
  }
  function deviceLabel(raw) {
    const browser = /Edg\//.test(raw) ? "Microsoft Edge" : /OPR\//.test(raw) ? "Opera" : /Firefox\//.test(raw) ? "Firefox" : /Chrome\//.test(raw) ? "Chrome" : /Safari\//.test(raw) ? "Safari" : "Браузер";
    const platform = /Android/.test(raw) ? "Android" : /iPhone|iPad/.test(raw) ? "iOS" : /Windows/.test(raw) ? "Windows" : /Macintosh/.test(raw) ? "macOS" : /Linux/.test(raw) ? "Linux" : "";
    return platform ? `${browser} · ${platform}` : browser;
  }
  function setSession(payload) {
    if (state.user && state.user.id !== payload.user.id) clearPrivateViews();
    const previousWorkspace = state.workspace?.id;
    state.epoch += 1; state.user = payload.user; state.csrf = payload.csrf; state.workspaces = payload.workspaces;
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
    if (state.workspace) select.value = state.workspace.id;
    else select.append(node("option", "Нет доступных центров"));
    await renderPane(state.pane);
  }
  async function refreshSession() { await enter(await api("/auth/session")); }
  function grantedSections() {
    const workspace = state.workspace;
    const none = { overview: false, team: false, messages: false, clients: false, connections: false, security: true };
    if (!workspace) return none;
    if (["owner", "admin"].includes(workspace.role)) return { overview: true, team: true, messages: true, clients: true, connections: true, security: true };
    const granted = new Set(workspace.sections && workspace.sections.length ? workspace.sections : ["overview", "security"]);
    return { overview: granted.has("overview"), team: false, messages: granted.has("messages"), clients: false, connections: false, security: granted.has("security") };
  }
  function messagesOnly() {
    const workspace = state.workspace;
    return Boolean(workspace && !["owner", "admin"].includes(workspace.role) && Array.isArray(workspace.sections) && workspace.sections.length === 1 && workspace.sections[0] === "messages");
  }
  function renderChatUserCard() {
    const card = node("div", undefined, "chat-user-card");
    card.append(node("span", initials(state.user.display_name), "avatar"));
    const text = node("div", undefined, "chat-user-text");
    text.append(node("strong", state.user.display_name), node("small", state.user.email));
    const out = node("button", "Выйти", "secondary compact");
    out.type = "button"; out.addEventListener("click", () => $("#logout").click());
    card.append(text, out);
    return card;
  }
  async function renderPane(pane) {
    state.pane = pane;
    const sections = grantedSections();
    if (!sections[state.pane]) state.pane = sections.messages ? "messages" : "overview";
    show($("#overview-tab"), sections.overview); show($("#team-tab"), sections.team); show($("#messages-tab"), sections.messages); show($("#clients-tab"), sections.clients); show($("#connections-tab"), sections.connections); show($("#security-tab"), sections.security);
    if (state.pane !== "clients") directory.leave();
    if (state.pane !== "messages") chat.leave();
    const only = messagesOnly();
    $("#cabinet").classList.toggle("chat-mode", state.pane === "messages");
    $("#cabinet").classList.toggle("messages-only", only);
    show($("#no-workspaces"), !state.workspace && state.pane !== "security");
    document.querySelectorAll(".pane").forEach(element => show(element, (Boolean(state.workspace) || state.pane === "security") && element.id === `pane-${state.pane}`));
    const heading = (state.workspace || state.pane === "security") ? pageHeadings.get(state.pane) : null;
    const topbarContext = $("#topbar-context"), activeHeading = $(".page-heading", topbarContext);
    const chatHeader = $("#chat-list-header");
    if (activeHeading && activeHeading !== heading) $("#page-heading-storage").append(activeHeading);
    if (only) {
      topbarContext.replaceChildren();
      chatHeader.replaceChildren(...(heading ? [heading] : []), renderChatUserCard());
      show(chatHeader);
    } else {
      show(chatHeader, false);
      topbarContext.replaceChildren(...(heading ? [heading] : []));
    }
    document.querySelectorAll("[data-pane]").forEach(button => button.classList.toggle("active", button.dataset.pane === state.pane));
    chat.watch();
    if (!state.workspace) { if (state.pane === "security") await loadSessions(); return; }
    const manage = Boolean(state.workspace && ["owner", "admin"].includes(state.workspace.role));
    $("#workspace-role").textContent = roles[state.workspace.role] || "Сотрудник";
    $("#workspace-timezone").textContent = state.workspace.timezone === "Europe/Moscow" ? "Москва · UTC+3" : state.workspace.timezone;
    const form = $("#workspace-form");
    form.elements.name.value = state.workspace.name;
    form.elements.name.disabled = !manage;
    show($("button[type=submit]", form), Boolean(manage));
    show($("#workspace-readonly"), !manage);
    if (state.pane === "connections") await Promise.allSettled([loadAlfa().catch(error => { notice(error.message, $("#alfa-notice")); }), loadAlfaMax().catch(error => { notice(error.message, $("#alfa-max-error")); }), chat.connections()]);
    if (state.pane === "messages") await chat.enter();
    if (state.pane === "clients") await directory.enter();
    if (state.pane === "team") await loadMembers();
    if (state.pane === "security") await loadSessions();
  }
  const sectionLabels = { overview: "Организация", messages: "Сообщения", security: "Безопасность" };
  function memberSectionControls(member, workspace) {
    const wrap = node("div", undefined, "member-sections");
    const checks = ["overview", "messages", "security"].map(key => {
      const label = node("label", undefined, "check-label");
      const input = node("input"); input.type = "checkbox"; input.dataset.section = key;
      input.checked = (member.sections && member.sections.length ? member.sections : ["overview", "security"]).includes(key);
      label.append(input, node("span", sectionLabels[key]));
      return label;
    });
    wrap.append(...checks);
    const save = node("button", "Сохранить", "secondary compact"); save.type = "button";
    save.addEventListener("click", () => busy(wrap, async () => {
      const selected = checks.map(check => $("input", check)).filter(input => input.checked).map(input => input.dataset.section);
      await api(`/workspaces/${workspace.id}/members/${member.user_id}/sections`, { method: "POST", data: { sections: selected } });
      toast("Права сотрудника сохранены."); await loadMembers();
    }));
    wrap.append(save);
    return wrap;
  }
  async function loadMembers() {
    const workspace = state.workspace;
    const payload = await api(`/workspaces/${workspace.id}/members`);
    if (workspace.id !== state.workspace?.id) return;
    const list = $("#members-list"); list.replaceChildren();
    payload.members.forEach(member => {
      const row = node("div", undefined, "member-row");
      const info = node("div", undefined, "member-info");
      const details = node("div"); details.append(node("strong", member.display_name), node("small", member.email));
      info.append(node("span", initials(member.display_name), "avatar"), details);
      row.append(info, node("span", roles[member.role] || "Сотрудник", "role-badge"));
      if (member.role !== "owner" && (workspace.role === "owner" || member.role !== "admin")) {
        const button = node("button", "×", "icon-button remove-member"); button.type = "button";
        button.setAttribute("aria-label", `Отозвать доступ: ${member.display_name}`);
        button.addEventListener("click", async () => {
          if (!await confirm("Отозвать доступ?", `${member.display_name} потеряет доступ к этому центру.`, "Отозвать доступ")) return;
          await busy(row, async () => { await api(`/workspaces/${workspace.id}/members/${member.user_id}/remove`, { method: "POST" }); toast("Доступ сотрудника отозван."); await loadMembers(); });
        });
        row.append(button);
      }
      if (!["owner", "admin"].includes(member.role)) row.append(memberSectionControls(member, workspace));
      list.append(row);
    });
    show($("#team-empty"), payload.members.length === 0);
  }
  async function loadSessions() {
    const payload = await api("/auth/sessions");
    const list = $("#sessions-list"); list.replaceChildren();
    payload.sessions.forEach(session => {
      const row = node("div", undefined, "session-row"), details = node("div");
      details.append(node("strong", deviceLabel(session.device_label)), node("small", `Активность: ${date(session.last_seen_at)}`));
      if (session.current) details.append(node("span", "Текущая сессия", "small-tag"));
      row.append(details);
      if (!session.current) {
        const button = node("button", "Завершить", "secondary"); button.type = "button";
        button.addEventListener("click", () => busy(row, async () => { await api(`/auth/sessions/${session.id}/revoke`, { method: "POST" }); toast("Сессия завершена."); await loadSessions(); }));
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
      state.invite = null; await enter(payload); form.reset(); toast("Доступ к центру предоставлен.");
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
  document.querySelectorAll("[data-pane]").forEach(button => button.addEventListener("click", () => renderPane(button.dataset.pane).catch(error => { if (error.name !== "AbortError") toast(error.message, true); })));
  $("#workspace-select").addEventListener("change", async event => {
    state.epoch += 1; state.workspace = state.workspaces.find(item => item.id === event.target.value) || null; resetAlfa(); resetAlfaMax(); directory.reset(); chat.reset();
    try { await renderPane(state.pane); } catch (error) { if (error.name !== "AbortError") toast(error.message, true); }
  });
  $("#workspace-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, workspace = state.workspace;
    busy(form, async () => { const name = form.elements.name.value.trim(); await api(`/workspaces/${workspace.id}`, { method: "PATCH", data: { name } }); workspace.name = name; await refreshSession(); toast("Название центра сохранено."); });
  });
  $("#logout").addEventListener("click", async () => {
    try { await api("/auth/logout", { method: "POST" }); clearSession(); }
    catch (error) { if (error.name !== "AbortError") toast(error.message, true); }
  });
  $("#password-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    if (form.elements.new_password.value !== form.elements.confirm_password.value) return toast("Новые пароли не совпадают.", true);
    busy(form, async () => { await api("/auth/password", { method: "POST", data: { current_password: form.elements.current_password.value, new_password: form.elements.new_password.value } }); form.reset(); clearSession("Пароль изменён. Войдите с новым паролем."); });
  });
  $("#logout-all").addEventListener("click", async () => {
    if (!await confirm("Выйти на всех устройствах?", "Все текущие сессии будут завершены, включая эту.", "Выйти везде")) return;
    try { await api("/auth/logout-all", { method: "POST" }); clearSession("Все сессии завершены."); } catch (error) { if (error.name !== "AbortError") toast(error.message, true); }
  });
  $("#members-refresh").addEventListener("click", () => loadMembers().catch(error => { if (error.name !== "AbortError") toast(error.message, true); }));
  $("#sessions-refresh").addEventListener("click", () => loadSessions().catch(error => { if (error.name !== "AbortError") toast(error.message, true); }));
  $("#access-refresh").addEventListener("click", () => refreshSession().catch(error => { if (error.name !== "AbortError") toast(error.message, true); }));
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
    try { await navigator.clipboard.writeText(input.value); toast("Ссылка скопирована."); }
    catch (_) { input.focus(); input.select(); toast("Ссылка выделена. Скопируйте её через меню или Ctrl+C."); }
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
    alfaButtons();
  }
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
      await loadAlfa(); toast("Подключение обновлено.");
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

  function resetAlfaMax() {
    state.alfaMax = null; state.alfaMaxBusy = false;
    $("#alfa-max-connect-form").reset(); $("#alfa-max-otp-form").reset();
    $("#alfa-max-status").textContent = "Не подключено";
    $("#alfa-max-info").textContent = "";
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
  }
  async function loadAlfaMax() {
    const workspace = state.workspace;
    const payload = await api("/workspaces/" + workspace.id + "/integrations/alfa-max");
    if (workspace.id !== state.workspace?.id) return;
    state.alfaMax = payload;
    const labels = {
      not_configured: "Не подключено",
      otp_required: "Нужен код из письма",
      active: "Подключено",
      reauth_required: "Нужно войти заново",
      error: "Ошибка подключения",
    };
    $("#alfa-max-status").textContent = labels[payload.state] || "Состояние не определено";
    $("#alfa-max-status").classList.toggle("integration-error", ["reauth_required", "error"].includes(payload.state));
    const form = $("#alfa-max-connect-form");
    if (payload.email) form.elements.email.value = payload.email;
    form.elements.password.value = "";
    show(form, payload.state !== "otp_required" && payload.state !== "active");
    show($("#alfa-max-otp-form"), payload.state === "otp_required");
    show($("#alfa-max-actions"), payload.configured);
    const details = [];
    if (payload.last_sync_at) details.push("Последняя синхронизация: " + date(payload.last_sync_at));
    if (payload.crm_configured && payload.branches?.length) details.push("Филиалов: " + payload.branches.length);
    if (!payload.crm_configured) details.push("Сначала настройте подключение AlfaCRM выше.");
    else if (!payload.branches?.length) details.push("Сначала загрузите филиалы AlfaCRM.");
    $("#alfa-max-info").textContent = details.join(" · ");
    notice(!payload.vault_ready ? "На сервере не настроен ключ хранения веб-сессий." : (payload.error || ""), $("#alfa-max-error"));
    alfaMaxButtons();
  }
  async function alfaMaxAction(action, success) {
    if (state.alfaMaxBusy || !state.workspace) return;
    const epoch = state.epoch, workspace = state.workspace;
    state.alfaMaxBusy = true; alfaMaxButtons(); notice("", $("#alfa-max-error"));
    try {
      await action("/workspaces/" + workspace.id + "/integrations/alfa-max");
      if (epoch !== state.epoch) return;
      await loadAlfaMax(); await chat.connections(); toast(success);
    } catch (error) {
      if (error.name !== "AbortError" && epoch === state.epoch) {
        try { await loadAlfaMax(); } catch (_) { /* Keep the action error visible. */ }
        if (epoch === state.epoch) notice(error.message, $("#alfa-max-error"));
      }
    } finally {
      if (epoch === state.epoch) { state.alfaMaxBusy = false; alfaMaxButtons(); }
    }
  }
  $("#alfa-max-connect-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = { email: form.elements.email.value.trim(), password: form.elements.password.value };
    alfaMaxAction(async path => {
      const result = await api(path + "/connect", { method: "POST", data });
      form.elements.password.value = "";
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
  $("#alfa-max-sync").addEventListener("click", () => alfaMaxAction(path => api(path + "/sync", { method: "POST" }), "Синхронизация истории поставлена в очередь."));
  $("#alfa-max-disconnect").addEventListener("click", async () => {
    const epoch = state.epoch;
    if (!await confirm("Отключить штатный MAX?", "Зашифрованная веб-сессия AlfaCRM будет удалена. История переписки останется в кабинете.", "Отключить")) return;
    if (epoch === state.epoch) await alfaMaxAction(path => api(path, { method: "DELETE" }), "Веб-сессия AlfaCRM MAX отключена.");
  });

  async function start() {
    const raw = new URLSearchParams(location.hash.slice(1)).get("invite");
    if (raw) { history.replaceState(null, "", location.pathname + location.search); if (/^[A-Za-z0-9_-]{43}$/.test(raw)) state.invite = raw; }
    try {
      const status = await api("/auth/status");
      let payload = null;
      try { payload = await api("/auth/session"); } catch (error) { if (error.status !== 401) throw error; }
      if (payload) return await enter(payload);
      if (state.invite) return await showInvitation();
      if (status.setup_required && status.setup_enabled) {
        showLogin(); show($("#login-form"), false); show($("#setup-form"));
        $("#auth-eyebrow").textContent = "Начнём знакомство"; $("#auth-title").textContent = "Создать кабинет";
        $("#auth-description").textContent = "Первый аккаунт владельца организации.";
      } else showLogin(status.setup_required ? "Первичная настройка пока недоступна. Обратитесь к владельцу сервиса." : "");
    } catch (error) { showLogin(error.message); }
  }
  const directory = window.CenterDirectory({ $, state, api, node, show, notice, date });
  const chat = window.CenterChat({ $, state, api, node, show, notice, date, toast, confirm, renderPane });
  start();
})();
