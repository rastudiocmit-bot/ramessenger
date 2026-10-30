"use strict";
window.CenterDirectory = ({ $, state, api, node, show, notice, date }) => {
  const labels = { client: "Клиент", lead: "Лид", archive: "Архивный" };
  let generation = 0, listRequest = 0, detailRequest = 0, cardRequest = 0;
  let timer, searchTimer, status = null, snapshot = null, items = [], selected = null;
  let page = 0, total = 0, more = false, actionBusy = false, listBusy = false;
  const cache = new Map();
  const contactRequests = new Map();
  const renderSavedCard = window.CenterDirectoryCard({ node, date });
  const active = () => state.user && state.workspace && state.pane === "clients";
  const readonly = () => !["owner", "admin"].includes(state.workspace?.role);
  const path = () => `/workspaces/${state.workspace.id}/${readonly() ? "chat-contacts" : "directory"}`;
  const current = (epoch, gen) => epoch === state.epoch && gen === generation;
  const running = () => ["running", "queued"].includes(status?.state);
  function fail(error) { if (error.name !== "AbortError") notice(error.message, $("#directory-error")); }
  function resetDetail() {
    selected = null; detailRequest += 1;
    $("#directory-layout").classList.remove("has-selection");
    const blank = node("div", undefined, "detail-placeholder");
    blank.append(node("span", "◈", "detail-placeholder-icon"), node("h2", "Всё о семье — рядом"), node("p", "Выберите контакт слева, чтобы увидеть детей, их группы и статусы."), node("small", "Кружок рядом с именем открывает карточку АльфаCRM."));
    $("#directory-detail").replaceChildren(blank);
  }
  function reset() {
    generation += 1; listRequest += 1; cardRequest += 1;
    clearTimeout(timer); clearTimeout(searchTimer); cache.clear(); contactRequests.clear();
    status = null; snapshot = null; items = []; page = 0; total = 0; more = false; actionBusy = false; listBusy = false;
    $("#directory-list").replaceChildren(); $("#crm-card-options").replaceChildren();
    if ($("#crm-card-dialog").open) $("#crm-card-dialog").close();
    if ($("#directory-contact-dialog")?.open) $("#directory-contact-dialog").close();
    $("#directory-search").value = ""; $("#directory-status").value = ""; $("#directory-max").value = "connected_first";
    $("#directory-branch").replaceChildren(new Option("Все филиалы", ""));
    $("#sync-auto").checked = true; $("#sync-interval").value = "30";
    $("#directory-count").textContent = "0"; $("#directory-visible-count").textContent = "";
    $("#sync-title").textContent = "Проверяем данные…"; $("#sync-description").textContent = "";
    $("#sync-indicator").className = "connection-dot";
    show($("#directory-empty")); show($("#directory-more"), false);
    $("#directory-empty").textContent = "Загрузите клиентов из подключённой АльфаCRM. Один родитель будет отображаться один раз.";
    notice("", $("#directory-error")); resetDetail(); buttons();
  }
  function buttons() {
    const working = running();
    ["#directory-sync", "#directory-stop"].forEach(id => show($(id), !readonly()));
    if ($(".sync-options")) show($(".sync-options"), !readonly());
    if ($("#directory-readonly")) show($("#directory-readonly"), readonly());
    $("#directory-sync").disabled = !status?.configured || working || actionBusy || readonly();
    $("#directory-sync").textContent = working ? "Загрузка идёт…" : status?.last_success_at ? "Обновить из CRM" : "Загрузить из CRM";
    show($("#directory-stop"), !readonly() && (working || Boolean(status?.auto_refresh)));
    $("#directory-stop").disabled = actionBusy || readonly();
    $("#directory-stop").textContent = working ? "Остановить загрузку" : "Отключить автообновление";
    $("#sync-auto").disabled = working || actionBusy || readonly();
    $("#sync-interval").disabled = working || actionBusy || readonly();
    $("#directory-more").disabled = listBusy;
  }
  function renderStatus() {
    const working = running(), last = status.last_success_at;
    $("#sync-indicator").className = working ? "spinner" : status.error ? "connection-dot sync-failed" : "connection-dot";
    $("#sync-title").textContent = working ? (status.phase || "Готовим загрузку") : !status.configured ? readonly() ? "Справочник ещё не загружен" : "Подключите АльфаCRM в разделе «Подключения»" : status.error ? "Обновление остановлено" : last ? "Данные сохранены в базе" : "Готово к первой загрузке";
    const counts = [];
    if (working) {
      if (status.stats.customers) counts.push(`Карточек: ${status.stats.customers}`);
      if (status.stats.groups) counts.push(`Групп: ${status.stats.groups}`);
      if (status.stats.memberships) counts.push(`Связей с группами: ${status.stats.memberships}`);
    } else if (last) counts.push(`Обновлено: ${date(last)}`);
    if (status.auto_refresh && !working) counts.push(`Автоматически каждые ${status.interval_minutes} мин`);
    if (last && working) counts.push("Показана предыдущая полная копия");
    $("#sync-description").textContent = counts.join(" · ") || "Первичная загрузка получит данные из всех доступных филиалов.";
    notice(status.error || "", $("#directory-error"));
    if (working) {
      $("#sync-auto").checked = status.auto_refresh;
      $("#sync-interval").value = String(status.interval_minutes);
    }
    buttons();
  }
  function schedule() {
    clearTimeout(timer);
    if (!active()) return;
    timer = setTimeout(() => { if (!document.hidden) poll().catch(fail); else schedule(); }, running() ? 2500 : 15000);
  }
  async function poll() {
    const epoch = state.epoch, gen = generation, previous = status?.last_success_at;
    try {
      const payload = await api(`${path()}/sync`);
      if (!current(epoch, gen) || !active()) return;
      status = payload; renderStatus();
      if (previous !== payload.last_success_at) await loadList();
    } finally { if (current(epoch, gen)) schedule(); }
  }
  async function enter() {
    clearTimeout(timer);
    const epoch = state.epoch, gen = generation;
    try {
      const payload = await api(`${path()}/sync`);
      if (!current(epoch, gen) || !active()) return;
      status = payload;
      if (payload.run_id) {
        $("#sync-auto").checked = payload.auto_refresh;
        const value = String(payload.interval_minutes), select = $("#sync-interval");
        if (![...select.options].some(option => option.value === value)) select.append(new Option(`${value} минут`, value));
        select.value = value;
      }
      renderStatus(); await loadList();
    } finally { if (current(epoch, gen)) schedule(); }
  }
  function filters() {
    const params = new URLSearchParams();
    const q = $("#directory-search").value.trim();
    if (q) params.set("q", q);
    for (const field of ["branch", "status"]) if ($(`#directory-${field}`).value) params.set(field, $(`#directory-${field}`).value);
    const maxBotStatus = $("#directory-max").value;
    if (maxBotStatus) params.set("max_bot_status", maxBotStatus);
    return params;
  }
  async function loadList(append = false) {
    const epoch = state.epoch, gen = generation, seq = ++listRequest;
    const params = filters(); params.set("page", String(append ? page + 1 : 0));
    listBusy = true; buttons(); $("#directory-list").setAttribute("aria-busy", "true");
    try {
      const payload = await api(`${path()}?${params}`);
      if (!current(epoch, gen) || seq !== listRequest || !active()) return;
      if (snapshot !== payload.snapshot) { cache.clear(); cardRequest += 1; $("#crm-card-options").replaceChildren(); if ($("#crm-card-dialog").open) $("#crm-card-dialog").close(); }
      const savedSelection = selected;
      snapshot = payload.snapshot; total = payload.total; more = payload.has_more; page = payload.page;
      items = append ? [...items, ...payload.contacts.filter(item => !items.some(old => old.id === item.id))] : payload.contacts;
      const branch = $("#directory-branch"), chosen = branch.value;
      branch.replaceChildren(new Option("Все филиалы", ""), ...payload.branches.map(item => new Option(item.name, item.id)));
      branch.value = [...branch.options].some(option => option.value === chosen) ? chosen : "";
      renderList();
      if (!append && savedSelection) {
        if (!items.some(item => item.id === savedSelection)) resetDetail();
        else if ($("#directory-contact-dialog")?.open) await selectContact(savedSelection, false);
      }
    } finally {
      if (current(epoch, gen) && seq === listRequest) {
        listBusy = false; $("#directory-list").setAttribute("aria-busy", "false"); buttons();
      }
    }
  }
  function renderList() {
    const list = $("#directory-list"); list.replaceChildren();
    for (const item of items) {
      const row = node("div", undefined, "contact-row mrow"); row.dataset.id = item.id;
      row.classList.toggle("selected", item.id === selected);
      const avatar = node("button", undefined, `contact-avatar ava ${item.status} status-${item.status}`);
      avatar.type = "button"; avatar.title = `Открыть карточку АльфаCRM: ${item.name}`;
      avatar.setAttribute("aria-label", avatar.title);
      avatar.append(node("span", item.name.trim().slice(0, 1).toLocaleUpperCase("ru"), "avatar-letter"), node("span", "↗", "avatar-open"));
      avatar.addEventListener("click", () => openCards(item.id).catch(fail));
      const body = node("button", undefined, "contact-select mrow-main"); body.type = "button";
      body.setAttribute("aria-label", `Выбрать контакт: ${item.name}`);
      body.setAttribute("aria-pressed", String(item.id === selected));
      body.append(node("strong", item.name), node("span", item.students.join(", ") + (item.student_count > item.students.length ? ` и ещё ${item.student_count - item.students.length}` : ""), "contact-students"));
      const info = node("div", undefined, "directory-list-facts");
      if ((item.phones || []).length) info.append(node("span", "Тел.: " + item.phones.join(" · ")));
      if ((item.emails || []).length) info.append(node("span", "Почта: " + item.emails.join(" · ")));
      if ((item.groups || []).length) info.append(node("span", "Группы: " + item.groups.join(" · ") + (item.group_count > item.groups.length ? ` · ещё ${item.group_count - item.groups.length}` : "")));
      if ((item.branches || []).length) info.append(node("span", "Филиалы: " + item.branches.map(b => b.name).join(" · ")));
      info.append(node("span", [item.max_connected ? "MAX через AlfaCRM" : null, item.max_bot_connected ? "MAX-бот" : null].filter(Boolean).join(" · ") || "MAX не подключён"));
      if (item.needs_review) info.append(node("span", "Нужно уточнить данные", "directory-list-review"));
      body.append(info);
      body.addEventListener("click", () => selectContact(item.id).catch(fail));
      const pill = node("span", labels[item.status], `pill ${item.status}`);
      const crm = node("button", undefined, "iconbtn plain"); crm.type = "button"; crm.setAttribute("aria-label", `Открыть в АльфаCRM: ${item.name}`);
      if (window.CenterIcon) crm.append(window.CenterIcon("out")); else crm.textContent = "↗";
      crm.addEventListener("click", () => openCards(item.id).catch(fail));
      row.append(avatar, body, pill, crm); list.append(row);
    }
    $("#directory-count").textContent = String(total);
    $("#directory-visible-count").textContent = total ? `${items.length} из ${total}` : "";
    $("#directory-empty").textContent = snapshot ? "По этому запросу ничего не найдено. Попробуйте изменить поиск или фильтры." : "Загрузите клиентов из подключённой АльфаCRM. Один родитель будет отображаться один раз.";
    show($("#directory-empty"), items.length === 0); show($("#directory-more"), more);
  }
  async function getContact(id) {
    const branch = $("#directory-branch").value;
    const key = `${snapshot}/${branch}/${id}`;
    const saved = cache.get(key);
    if (saved && Date.now() - saved.at < 30000) return saved.value;
    if (contactRequests.has(key)) return contactRequests.get(key);
    const epoch = state.epoch, gen = generation;
    const request = api(`${path()}/contacts/${id}${branch ? `?branch=${encodeURIComponent(branch)}` : ""}`).then(payload => {
      if (current(epoch, gen) && key === `${snapshot}/${$("#directory-branch").value}/${id}`) {
        if (cache.size >= 60) cache.delete(cache.keys().next().value);
        cache.set(key, { value: payload, at: Date.now() });
      }
      return payload;
    });
    contactRequests.set(key, request);
    try { return await request; }
    finally { if (contactRequests.get(key) === request) contactRequests.delete(key); }
  }
  async function selectContact(id, mobile = true) {
    const epoch = state.epoch, gen = generation, seq = ++detailRequest;
    selected = id; renderList();
    if (mobile) $("#directory-layout").classList.add("has-selection");
    const detail = $("#directory-detail"); detail.setAttribute("aria-busy", "true");
    const dialog = $("#directory-contact-dialog"); if (dialog && !dialog.open) dialog.showModal();
    const loading = node("div", "Загружаем сведения…", "empty"); detail.replaceChildren(loading);
    try {
      const payload = await getContact(id);
      if (!current(epoch, gen) || seq !== detailRequest || !active()) return;
      renderDetail(payload);
    } catch (error) {
      if (current(epoch, gen) && seq === detailRequest) { detail.replaceChildren(node("p", error.message, "empty")); fail(error); }
    } finally { if (current(epoch, gen) && seq === detailRequest) detail.setAttribute("aria-busy", "false"); }
  }
  function renderDetail(contact) {
    $("#directory-detail").replaceChildren(renderSavedCard(contact));
  }
  async function openCards(id) {
    const epoch = state.epoch, gen = generation, seq = ++cardRequest;
    const dialog = $("#crm-card-dialog"), options = $("#crm-card-options");
    options.replaceChildren(node("p", "Загружаем карточки…", "muted")); dialog.showModal();
    try {
      const contact = await getContact(id);
      if (!current(epoch, gen) || seq !== cardRequest || !dialog.open) return;
      options.replaceChildren();
      for (const student of contact.students) for (const card of student.cards) {
        const link = node("a", undefined, "crm-card-link");
        const url = new URL(card.url);
        if (url.protocol !== "https:" || !/^[a-z0-9-]+\.s20\.online$/i.test(url.hostname)) continue;
        link.href = url.href; link.target = "center-messenger-crm"; link.rel = "noopener noreferrer";
        link.append(node("strong", student.name), node("small", card.branch_name), node("span", "↗", "crm-link-arrow"));
        options.append(link);
      }
      if (contact.has_more) options.append(node("p", "Выберите филиал в фильтре, чтобы увидеть остальные карточки.", "muted small"));
    } catch (error) { if (current(epoch, gen) && seq === cardRequest) options.replaceChildren(node("p", error.message, "notice error")); }
  }
  async function action(method) {
    if (!status || actionBusy) return;
    const epoch = state.epoch, gen = generation;
    actionBusy = true; clearTimeout(timer); buttons(); notice("", $("#directory-error"));
    try {
      await api(`${path()}/sync`, { method, data: method === "POST" ? { auto_refresh: $("#sync-auto").checked, interval_minutes: Number($("#sync-interval").value) } : undefined });
      if (current(epoch, gen)) await poll();
    } catch (error) { if (current(epoch, gen)) fail(error); }
    finally { if (current(epoch, gen)) { actionBusy = false; buttons(); schedule(); } }
  }
  $("#directory-sync").addEventListener("click", () => action("POST"));
  $("#directory-stop").addEventListener("click", () => action("DELETE"));
  $("#directory-search").addEventListener("input", () => {
    clearTimeout(searchTimer); listRequest += 1; detailRequest += 1; cardRequest += 1;
    searchTimer = setTimeout(() => { resetDetail(); loadList().catch(fail); }, 250);
  });
  for (const field of ["branch", "status", "max"]) $(`#directory-${field}`).addEventListener("change", () => {
    clearTimeout(searchTimer); resetDetail(); cardRequest += 1;
    if ($("#crm-card-dialog").open) $("#crm-card-dialog").close();
    loadList().catch(fail);
  });
  $("#directory-more").addEventListener("click", () => { if (!listBusy) loadList(true).catch(fail); });
  $("#crm-card-dialog").addEventListener("close", () => { cardRequest += 1; $("#crm-card-options").replaceChildren(); });
  const layout = $("#directory-layout"), resizer = $("#directory-resizer");
  let width = 450, drag = null;
  function resize(value) {
    width = Math.max(300, Math.min(640, Math.min(value, layout.clientWidth - 300)));
    layout.style.setProperty("--directory-width", `${width}px`);
    resizer.setAttribute("aria-valuenow", String(Math.round(width)));
  }
  resizer.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    drag = { x: event.clientX, width }; resizer.setPointerCapture(event.pointerId);
    layout.classList.add("resizing"); event.preventDefault();
  });
  resizer.addEventListener("pointermove", event => { if (drag) resize(drag.width + event.clientX - drag.x); });
  const endDrag = () => { drag = null; layout.classList.remove("resizing"); };
  resizer.addEventListener("pointerup", endDrag); resizer.addEventListener("lostpointercapture", endDrag);
  resizer.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault(); resize(event.key === "Home" ? 300 : event.key === "End" ? 640 : width + (event.key === "ArrowLeft" ? -20 : 20));
  });
  return { reset, enter, leave: () => clearTimeout(timer) };
};
