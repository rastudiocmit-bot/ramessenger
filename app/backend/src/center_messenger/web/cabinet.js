"use strict";
window.CenterCabinet = ({ $, state, api, node, show, roles, renderPane }) => {
  let sections = {}, summaryTimer = null, summaryRequest = 0, context = null, contextEpoch = null;
  const icon = name => window.CenterIcon(name);
  const definition = [
    ["messages", "Чаты", "chats"], ["summary", "Сводка", "chart"],
    ["clients", "Клиенты", "people"], ["team", "Сотрудники", "team"],
    ["connections", "Подключения", "plug"], ["overview", "Организация", "building"],
    ["security", "Безопасность", "shield"], ["profile", "Профиль", "user"]
  ];
  function fillIcons(root = document) {
    for (const target of root.querySelectorAll("[data-icon]")) target.replaceChildren(icon(target.dataset.icon));
  }
  function navigate(pane, chatView) {
    if ($("#navigation-dialog").open) $("#navigation-dialog").close();
    state.chatView = "all";
    if (["messages", "queue"].includes(pane)) $("#chat-layout").classList.remove("has-selection");
    return renderPane(pane === "queue" ? "messages" : pane);
  }
  function navButton([pane, title, glyph], rail = false) {
    const button = node("button", undefined, "tab"); button.type = "button";
    button.dataset.pane = pane; button.id = pane + (rail ? "-tab" : "-mobile-tab");
    button.setAttribute("role", "tab");
    const active = state.pane === pane;
    button.setAttribute("aria-selected", String(active));
    button.append(icon(glyph), node("span", title));
    return button;
  }
  function update(next) {
    sections = next;
    if (contextEpoch !== state.epoch) {
      context = null; contextEpoch = state.epoch;
      $("#profile-channels").textContent = "";
      if ($("#organization-branches")) $("#organization-branches").replaceChildren(node("p", "Загружаем филиалы…", "note"));
      if ($("#organization-branch-count")) $("#organization-branch-count").textContent = "—";
      $("#summary-content").replaceChildren(node("p", "Загружаем показатели…", "note"));
      $("#summary-error").textContent = ""; show($("#summary-error"), false);
    }
    const all = definition.filter(([key]) => sections[key]);
    $("#navrail").replaceChildren(...all.map(t => navButton(t, true)));
    const primary = all.slice(0, 3), rest = all.slice(3);
    $("#tabbar").replaceChildren(...primary.map(t => navButton(t)));
    if (rest.length) {
      const more = node("button", undefined, "tab"); more.type = "button"; more.id = "more-tab";
      more.setAttribute("aria-label", "Другие разделы");
      more.setAttribute("aria-selected", String(rest.some(([key]) => key === state.pane)));
      more.append(icon("more"), node("span", "Ещё"));
      more.addEventListener("click", () => {
        const list = $("#navigation-options"); list.replaceChildren();
        for (const [pane, title, glyph] of rest) {
          const button = node("button", undefined, "item"); button.type = "button"; button.dataset.pane = pane;
          const image = node("span", undefined, "item-icon"); image.append(icon(glyph));
          const text = node("span", undefined, "item-main"); text.append(node("strong", title));
          button.append(image, text); list.append(button);
        }
        $("#navigation-dialog").showModal();
      });
      $("#tabbar").append(more);
    }
    $("#app").dataset.tab = ({ summary: "home", messages: "chats", clients: "clients", team: "team", connections: "conn", overview: "org", security: "sec", profile: "prof" })[state.pane] || "org";
    if (state.workspace) {
      $("#profile-role").textContent = roles[state.workspace.role] || "Сотрудник";
      $("#profile-workspace").textContent = state.workspace.name;
      $("#profile-workspace-note").textContent = `${state.workspace.timezone === "Europe/Moscow" ? "Москва · UTC+3" : state.workspace.timezone} · ${roles[state.workspace.role] || "Сотрудник"}`;
    }
    show($("#profile-connections"), Boolean(sections.messages));
    if (sections.connections) $("#profile-connections").dataset.pane = "connections";
    else $("#profile-connections").removeAttribute("data-pane");
    fillIcons();
    if (state.pane !== "summary") leaveSummary();
  }
  function leaveSummary() { clearTimeout(summaryTimer); summaryTimer = null; summaryRequest += 1; }
  function metric(label, value, note, direction) {
    const card = node("div", undefined, "kpi"); card.append(node("dt", label), node("dd", value));
    if (note) card.append(node("div", note, "trend " + (direction || "flat")));
    return card;
  }
  function card(title, content, tag) {
    const card = node("div", undefined, "card"), head = node("div", undefined, "card-h"), body = node("div", undefined, "card-b");
    head.append(node("h2", title)); if (tag) head.append(node("span", tag, "tag"));
    body.append(content); card.append(head, body); return card;
  }
  const duration = value => value === null || value === undefined ? "—" : value < 60 ? Math.round(value) + " с" : Math.round(value / 60) + " мин";
  async function metadata() {
    const epoch = state.epoch, workspace = state.workspace?.id;
    const result = await api(`/workspaces/${workspace}/chats/context`);
    if (epoch !== state.epoch || workspace !== state.workspace?.id) return;
    context = result; contextEpoch = epoch;
    $("#profile-channels").textContent = `${(result.channels || []).filter(c => c.enabled).length} активных`;
    const branches = $("#organization-branches");
    if (branches) {
      branches.replaceChildren();
      for (const branch of result.branches || []) { const row = node("div", undefined, "split"); row.append(node("span", branch.name, "nm")); branches.append(row); }
      if (!(result.branches || []).length) branches.append(node("p", "Филиалы появятся после подключения AlfaCRM.", "note"));
      $("#organization-branch-count").textContent = String((result.branches || []).length);
    }
  }
  async function summary() {
    clearTimeout(summaryTimer); const seq = ++summaryRequest, workspace = state.workspace?.id, epoch = state.epoch;
    const current = () => seq === summaryRequest && epoch === state.epoch && workspace === state.workspace?.id && state.pane === "summary";
    try {
      const result = await api(`/workspaces/${workspace}/summary`);
      if (!current()) return;
      const content = $("#summary-content"); content.replaceChildren(); show($("#summary-error"), false);
      $("#summary-subtitle").textContent = (result.scope || "Все филиалы") + " · за последние 7 дней";
      const kpis = node("div", undefined, "kpi-grid");
      const trend = result.previous_incoming ? Math.round((result.incoming / result.previous_incoming - 1) * 100) : null;
      kpis.append(metric("Обращений за неделю", String(result.incoming), trend === null ? "За последние 7 дней" : `${trend > 0 ? "↑" : trend < 0 ? "↓" : "→"} ${Math.abs(trend)}% к прошлой`, trend > 0 ? "up" : trend < 0 ? "down" : "flat"),
        metric("Ждут ответа сейчас", String(result.waiting), result.waiting ? "↓ нужен разбор" : "↑ очередь пуста", result.waiting ? "down" : "up"),
        metric("Среднее время ответа", duration(result.average_response_seconds), "По ответам сотрудников"),
        metric("Новых лидов", String(result.new_leads), "С первым обращением за неделю"));
      const chart = node("div", undefined, "chart"), max = Math.max(1, ...result.days.map(d => d.incoming));
      for (const day of result.days) {
        const col = node("div"), bar = node("span", undefined, "bar"), label = node("small", day.label);
        bar.style.height = `calc((100% - 20px) * ${Math.max(0, day.incoming / max)})`; bar.title = `${day.label}: ${day.incoming}`;
        col.append(bar, label); chart.append(col);
      }
      const branches = node("div");
      for (const branch of result.branches) {
        const row = node("div", undefined, "split"), meter = node("span", undefined, "meter"), fill = node("i");
        fill.style.width = `${Math.round(branch.incoming / Math.max(1, result.incoming) * 100)}%`; meter.append(fill);
        row.append(node("span", branch.name, "nm"), meter, node("span", String(branch.incoming), "vl")); branches.append(row);
      }
      if (!result.branches.length) branches.append(node("p", "Обращений за этот период пока нет.", "note"));
      const operators = node("div");
      for (const operator of result.operators) { const row = node("div", undefined, "split"); row.append(node("span", operator.name, "nm"), node("span", duration(operator.average_response_seconds), "vl muted"), node("span", String(operator.answered), "vl")); operators.append(row); }
      if (!result.operators.length) operators.append(node("p", "Ответов сотрудников за этот период пока нет.", "note"));
      content.append(kpis, card("Обращения по дням", chart), card("Разбивка по филиалам", branches), card("Операторы за неделю", operators, "отв. · ср. время"), node("p", "Показатели рассчитываются по сохранённым сообщениям. Во время загрузки истории значения могут уточняться.", "note summary-note"));
    } catch (error) { if (current()) { $("#summary-error").textContent = error.message; show($("#summary-error")); } }
    finally { if (current()) summaryTimer = setTimeout(summary, 60000); }
  }
  $("#chat-search-toggle").addEventListener("click", () => { const target = $("#chat-search-wrap"), open = target.hidden; show(target, open); $("#chat-search-toggle").setAttribute("aria-expanded", String(open)); if (open) $("#chat-search").focus(); });
  $("#chat-person-open").addEventListener("click", () => document.dispatchEvent(new CustomEvent("cm:chat-details-open")));
  $("#profile-sound-settings").addEventListener("click", () => $("#chat-settings-open").click());
  $("#profile-connections").addEventListener("click", async () => {
    if (sections.connections) return;
    if (!context) { try { await metadata(); } catch (_) { return; } }
    const list = $("#channel-info-list"); list.replaceChildren();
    for (const channel of context?.channels || []) { const row = node("div", undefined, "split"); row.append(node("span", channel.name, "nm"), node("span", channel.enabled ? "Активен" : "Отключён", "dotline " + (channel.enabled ? "on" : "off"))); list.append(row); }
    $("#channel-info-dialog").showModal();
  });
  $("#profile-sound").addEventListener("change", () => { const target = $("#chat-sound-enabled"); target.checked = $("#profile-sound").checked; target.dispatchEvent(new Event("input", { bubbles: true })); });
  new MutationObserver(() => { $("#app").dataset.view = $("#chat-layout").classList.contains("has-selection") ? "chat" : "list"; }).observe($("#chat-layout"), { attributes: true, attributeFilter: ["class"] });
  if (window.visualViewport) {
    const fit = () => { const gap = Math.max(0, window.innerHeight - visualViewport.height - visualViewport.offsetTop); $("#app").style.setProperty("--kb", gap > 90 ? gap + "px" : "0px"); };
    visualViewport.addEventListener("resize", fit); visualViewport.addEventListener("scroll", fit);
  }
  fillIcons();
  return { update, summary, metadata, leave: leaveSummary, navigate, fillIcons };
};
