"use strict";
// Connections → service settings: the Yandex Cloud key and the workspace's AI agents.
// Owners and administrators only; the server enforces the same rule.
window.CenterAi = ({ $, state, api, node, show, notice, toast, confirm }) => {
  const form = $("#ai-settings-form"), agentForm = $("#ai-agent-form"), list = $("#ai-agent-list");
  const money = value => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value || 0));
  const statusText = { active: "Работает", error: "Ошибка", not_configured: "Не проверено" };
  let settings = null, agents = [], editing = null, seq = 0;
  const admin = () => ["owner", "admin"].includes(state.workspace?.role);
  const base = () => `/workspaces/${state.workspace.id}/ai`;

  function renderSettings() {
    if (!settings) return;
    form.elements.folder_id.value = settings.folder_id || "";
    form.elements.api_key.value = "";
    form.elements.api_key.placeholder = settings.key_hint ? `Сохранён ключ …${settings.key_hint}` : "Вставьте API-ключ";
    form.elements.monthly_limit_rub.value = settings.monthly_limit_rub ?? "";
    form.elements.voice_enabled.checked = Boolean(settings.voice_enabled);
    $("#ai-spent").textContent = `Потрачено в этом месяце: ${money(settings.month_spent_rub)} ₽`
      + (settings.monthly_limit_rub != null ? ` из ${money(settings.monthly_limit_rub)} ₽` : "");
    const badge = $("#ai-status"), configured = settings.configured;
    badge.textContent = !configured ? "Не подключено" : statusText[settings.status] || "Не проверено";
    badge.dataset.state = !configured ? "" : settings.status === "active" ? "active" : settings.status === "error" ? "error" : "";
    show($("#ai-clear"), Boolean(settings.key_hint));
    $("#ai-verify").disabled = !configured;
    notice(settings.error || (!settings.vault_ready ? "Хранилище ключей на сервере недоступно: ключ нельзя сохранить." : ""), $("#ai-error"));
    const fill = (select, items) => {
      const chosen = select.value;
      select.replaceChildren(...items.map(item => new Option(item.title, item.id)));
      if (items.some(item => item.id === chosen)) select.value = chosen;
    };
    fill(agentForm.elements.task, settings.tasks); fill(agentForm.elements.model, settings.models);
  }

  function renderAgents() {
    list.replaceChildren();
    if (!agents.length) {
      list.append(node("p", "Агентов пока нет. Без агента в поле вставляется распознанный текст как есть.", "muted small"));
      return;
    }
    for (const agent of agents) {
      const row = node("button", undefined, `ai-agent${agent.enabled ? "" : " off"}${editing?.id === agent.id ? " selected" : ""}`); row.type = "button";
      const main = node("span", undefined, "ai-agent-main");
      main.append(node("strong", agent.name), node("small", `${agent.task_title} · ${agent.model_title} · версия ${agent.version}`));
      row.append(main);
      if (agent.is_default) row.append(node("span", "Основной", "connection-status ai-default"));
      if (!agent.enabled) row.append(node("span", "Выключен", "connection-status"));
      row.addEventListener("click", () => edit(agent));
      list.append(row);
    }
  }

  async function load() {
    if (!admin() || !state.workspace) return;
    const mine = ++seq;
    try {
      const [loaded, listed] = await Promise.all([api(`${base()}/settings`), api(`${base()}/agents`)]);
      if (mine !== seq) return;
      settings = loaded; agents = listed.agents; renderSettings(); renderAgents();
    } catch (error) {
      if (mine === seq && error.name !== "AbortError") notice(error.message, $("#ai-error"));
    }
  }

  function temperature(value) { $("#ai-agent-temperature-value").textContent = Number(value).toFixed(2).replace(".", ","); }
  function edit(agent) {
    editing = agent || null;
    const f = agentForm.elements;
    $("#ai-agent-form-title").textContent = agent ? `Агент «${agent.name}»` : "Новый агент";
    f.name.value = agent?.name || "Чистовик голосового ответа";
    f.task.value = agent?.task || "voice_input";
    f.model.value = agent?.model || settings?.default_model || "yandexgpt-5.1";
    f.prompt.value = agent?.prompt || settings?.default_prompt || "";
    f.temperature.value = agent?.temperature ?? 0.2; temperature(f.temperature.value);
    f.enabled.checked = agent ? agent.enabled : true;
    f.is_default.checked = agent ? agent.is_default : !agents.some(item => item.is_default);
    show($("#ai-agent-delete"), Boolean(agent)); show($("#ai-agent-result"), false);
    show($("#ai-agent-versions"), Boolean(agent)); $("#ai-agent-versions").open = false; $("#ai-agent-version-list").replaceChildren();
    notice("", $("#ai-agents-error")); show(agentForm); renderAgents();
    agentForm.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  function close() { editing = null; show(agentForm, false); renderAgents(); }
  function values() {
    const f = agentForm.elements;
    return { name: f.name.value.trim(), task: f.task.value, model: f.model.value, prompt: f.prompt.value.trim(),
      temperature: Number(f.temperature.value), enabled: f.enabled.checked, is_default: f.is_default.checked };
  }

  async function guarded(button, action, errorTarget) {
    if (button.disabled) return;
    button.disabled = true; notice("", errorTarget);
    try { await action(); } catch (error) { if (error.name !== "AbortError") notice(error.message, errorTarget); }
    finally { button.disabled = false; }
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    guarded($("button[type=submit]", form), async () => {
      const f = form.elements, limit = f.monthly_limit_rub.value.trim();
      settings = await api(`${base()}/settings`, { method: "PUT", data: {
        folder_id: f.folder_id.value.trim(), api_key: f.api_key.value.trim() || null,
        voice_enabled: f.voice_enabled.checked, monthly_limit_rub: limit === "" ? null : Number(limit) } });
      renderSettings(); toast("Настройки Yandex Cloud сохранены.");
    }, $("#ai-error"));
  });
  $("#ai-verify").addEventListener("click", () => guarded($("#ai-verify"), async () => {
    settings = await api(`${base()}/settings/verify`, { method: "POST", timeoutMs: 40000 });
    renderSettings(); toast(settings.ok ? "Yandex Cloud отвечает: распознавание и модель работают." : "Проверка не прошла.", !settings.ok);
  }, $("#ai-error")));
  $("#ai-clear").addEventListener("click", () => guarded($("#ai-clear"), async () => {
    if (!(await confirm("Удалить ключ Yandex Cloud?", "Голосовой ввод отключится у всех сотрудников.", "Удалить"))) return;
    settings = await api(`${base()}/settings`, { method: "PUT", data: { folder_id: form.elements.folder_id.value.trim(), clear_key: true, voice_enabled: false,
      monthly_limit_rub: settings?.monthly_limit_rub ?? null } });
    renderSettings(); toast("Ключ удалён.");
  }, $("#ai-error")));

  $("#ai-agent-new").addEventListener("click", () => edit(null));
  $("#ai-agent-cancel").addEventListener("click", close);
  agentForm.elements.temperature.addEventListener("input", event => temperature(event.target.value));
  agentForm.addEventListener("submit", event => {
    event.preventDefault();
    guarded($("button[type=submit]", agentForm), async () => {
      const saved = editing
        ? await api(`${base()}/agents/${editing.id}`, { method: "PUT", data: values() })
        : await api(`${base()}/agents`, { method: "POST", data: values() });
      agents = (await api(`${base()}/agents`)).agents;
      toast(editing ? "Агент сохранён." : "Агент создан.");
      editing = agents.find(agent => agent.id === saved.id) || null; close();
    }, $("#ai-agents-error"));
  });
  $("#ai-agent-delete").addEventListener("click", () => guarded($("#ai-agent-delete"), async () => {
    if (!editing || !(await confirm(`Удалить агента «${editing.name}»?`, "История версий сохранится в базе.", "Удалить"))) return;
    await api(`${base()}/agents/${editing.id}`, { method: "DELETE" });
    agents = (await api(`${base()}/agents`)).agents; close(); toast("Агент удалён.");
  }, $("#ai-agents-error")));
  $("#ai-agent-test").addEventListener("click", () => guarded($("#ai-agent-test"), async () => {
    const sample = $("#ai-agent-sample").value.trim(), result = $("#ai-agent-result");
    if (!sample) { notice("Введите фразу для проверки.", $("#ai-agents-error")); return; }
    const { name, enabled, is_default, task, ...rest } = values();
    const answer = await api(`${base()}/agents/test`, { method: "POST", timeoutMs: 30000, data: { text: sample, ...rest } });
    result.replaceChildren(node("strong", answer.fallback ? "Модель не справилась, текст без изменений:" : "Результат:"), node("span", answer.text),
      node("small", `Стоимость проверки: ${money(answer.cost_rub)} ₽`));
    show(result);
  }, $("#ai-agents-error")));
  $("#ai-agent-versions").addEventListener("toggle", async () => {
    const details = $("#ai-agent-versions"), target = $("#ai-agent-version-list");
    if (!details.open || !editing) return;
    try {
      const { versions } = await api(`${base()}/agents/${editing.id}/versions`);
      target.replaceChildren(...versions.map(version => {
        const item = node("li"), when = new Date(version.created_at);
        item.append(node("strong", `Версия ${version.version}`),
          node("small", `${Number.isNaN(when.getTime()) ? "" : when.toLocaleString("ru-RU")}${version.author ? ` · ${version.author}` : ""}`),
          node("span", version.prompt));
        const restore = node("button", "Вернуть в форму", "text-button"); restore.type = "button";
        restore.addEventListener("click", () => {
          const f = agentForm.elements; f.name.value = version.name; f.model.value = version.model; f.prompt.value = version.prompt;
          f.temperature.value = version.temperature; temperature(version.temperature);
          toast("Версия перенесена в форму. Нажмите «Сохранить агента», чтобы применить.");
        });
        item.append(restore); return item;
      }));
    } catch (error) { notice(error.message, $("#ai-agents-error")); }
  });
  return { load };
};
