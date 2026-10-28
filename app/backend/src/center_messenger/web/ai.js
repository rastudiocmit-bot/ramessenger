"use strict";
// Connections → service settings: Yandex Cloud and DeepSeek keys and the workspace's AI agents.
// Owners and administrators only; the server enforces the same rule.
window.CenterAi = ({ $, state, api, node, show, notice, toast, confirm }) => {
  const form = $("#ai-settings-form"), agentForm = $("#ai-agent-form"), list = $("#ai-agent-list");
  const dsForm = $("#ai-deepseek-form"), routesForm = $("#ai-routes-form"), voiceForm = $("#ai-voice-options-form");
  const verifying = new Map();
  const gatewayCards = [...document.querySelectorAll("[data-gateway]")];
  let audioUrl = null, synthesisRoute = null;
  const providerNames = { yandex: "Yandex Cloud", deepseek: "DeepSeek", mimo: "MiMo", mimo_plan: "MiMo Token Plan" };
  const providerReady = provider => provider === "yandex" ? settings.configured : provider === "deepseek" ? settings.deepseek?.configured : settings.gateways?.[provider]?.configured;
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
    voiceForm.elements.monthly_limit_rub.value = settings.monthly_limit_rub ?? "";
    voiceForm.elements.voice_enabled.checked = Boolean(settings.voice_enabled);
    voiceForm.elements.voice_max_seconds.value = settings.voice_max_seconds || 180;
    state.voiceMaxSeconds = settings.voice_max_seconds || 180;
    $("#ai-spent").textContent = `Потрачено в этом месяце: ${money(settings.month_spent_rub)} ₽`
      + (settings.monthly_limit_rub != null ? ` из ${money(settings.monthly_limit_rub)} ₽` : "");
    const badge = $("#ai-status"), configured = settings.configured;
    badge.textContent = !configured ? "Не подключено" : statusText[settings.status] || "Не проверено";
    badge.dataset.state = !configured ? "" : settings.status === "active" ? "active" : settings.status === "error" ? "error" : "";
    show($("#ai-clear"), Boolean(settings.key_hint));
    $("#ai-verify").disabled = !configured;
    notice(settings.error || (!settings.vault_ready ? "Хранилище ключей на сервере недоступно: ключ нельзя сохранить." : ""), $("#ai-error"));
    const ds = settings.deepseek || {};
    dsForm.elements.api_key.value = "";
    dsForm.elements.api_key.placeholder = ds.key_hint ? `Сохранён ключ …${ds.key_hint}` : "sk-…";
    const dsBadge = $("#ai-deepseek-status");
    dsBadge.textContent = !ds.configured ? "Не подключено" : statusText[ds.status] || "Не проверено";
    dsBadge.dataset.state = !ds.configured ? "" : ds.status === "active" ? "active" : ds.status === "error" ? "error" : "";
    show($("#ai-deepseek-clear"), Boolean(ds.configured)); $("#ai-deepseek-verify").disabled = !ds.configured;
    notice(ds.error || "", $("#ai-deepseek-error"));
    const fill = (select, items) => {
      const chosen = select.value;
      select.replaceChildren(...items.map(item => new Option(item.title, item.id)));
      if (items.some(item => item.id === chosen)) select.value = chosen;
    };
    fill(agentForm.elements.task, settings.tasks);
    if (!agentForm.dataset.taskPrompt) {
      agentForm.dataset.taskPrompt = "1";
      // A new agent for tasks starts from the studio's ready rules instead of the dictation prompt.
      agentForm.elements.task.addEventListener("change", () => {
        const f = agentForm.elements, ready = settings?.task_prompts?.[f.task.value];
        if (!editing && ready && (!f.prompt.value.trim() || f.prompt.value === settings?.default_prompt)) {
          f.prompt.value = ready;
          if (f.name.value === "Чистовик голосового ответа") f.name.value = "Задачи из сообщений";
        }
      });
    }
    renderModels(agentForm.elements.model.value || editing?.model || settings.default_model);
    for (const [selector, configured] of [["#ai-cloud-card", settings.configured], ["#ai-deepseek-card", ds.configured]]) {
      const details = $(selector).querySelector(".gateway-credentials"); if (details) details.open = !configured;
    }
    renderGateways();
    const selectedSynthesis = settings.gateways?.synthesis_provider || "disabled";
    const nextRoute = selectedSynthesis + ":" + (settings.gateways?.synthesis_voice || "Mia");
    $("#ai-synthesis-test").disabled = selectedSynthesis === "disabled" || !providerReady(selectedSynthesis);
    if (synthesisRoute !== nextRoute) {
      if (audioUrl) URL.revokeObjectURL(audioUrl); audioUrl = null; synthesisRoute = nextRoute; notice("", $("#ai-synthesis-error"));
      $("#ai-synthesis-audio").removeAttribute("src"); show($("#ai-synthesis-audio"), false); show($("#ai-synthesis-play"), false); $("#ai-synthesis-result").textContent = "";
    }
    const ttsCheck = settings.gateways?.yandex?.checks?.synthesis;
    $("#ai-yandex-tts-verify").disabled = !settings.configured;
    $("#ai-yandex-tts-status").textContent = !ttsCheck ? "Синтез не проверен" : ttsCheck.ok ? "Синтез работает" : ttsCheck.error_code === "unauthorized" ? "Нет доступа к синтезу" : checkErrors[ttsCheck.error_code] || "Ошибка синтеза";
    $("#ai-yandex-tts-status").dataset.state = !ttsCheck ? "" : ttsCheck.ok ? "active" : "error";
    renderPipeline();
  }

  function renderPipeline() {
    const main = agents.find(agent => agent.enabled && agent.is_default && agent.task === "voice_input");
    $("#voice-active-summary").textContent = `Распознавание: ${providerNames[settings?.gateways?.recognition_provider || "yandex"]} · Запись: до ${settings?.voice_max_seconds || 180} сек. · `
      + (main ? `Обработка текста: ${main.model_title} (${main.name})` : "Текст вставляется без обработки AI-агентом.");
  }
  function renderModels(chosen) {
    if (!settings) return;
    const provider = settings.models.find(item => item.id === chosen)?.provider || agentForm.elements.provider.value || "yandex";
    agentForm.elements.provider.value = provider;
    const model = agentForm.elements.model, groups = new Map();
    for (const item of settings.models) {
      if ((item.provider || "yandex") !== provider) continue;
      if (!groups.has(provider)) {
        const ready = providerReady(provider);
        const group = document.createElement("optgroup");
        group.label = (item.provider_title || "Yandex Cloud") + (ready ? "" : " — ключ не добавлен");
        groups.set(provider, group);
      }
      groups.get(provider).append(new Option(item.title, item.id));
    }
    model.replaceChildren(...groups.values());
    if (settings.models.some(item => item.id === chosen)) model.value = chosen;
    $("#ai-agent-provider-note").textContent = `Текст обрабатывает ${providerNames[provider] || provider}.`
      + (provider === "mimo_plan" ? " Используется квота подписки." : "")
      + (providerReady(provider) ? "" : " Добавьте ключ в разделе «ИИ-шлюзы».");
  }

  function renderAgents() {
    renderPipeline();
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
    const mine = ++seq; verifying.clear();
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
    renderModels(agent?.model || settings?.default_model || "yandexgpt-5.1");
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
      const f = form.elements;
      settings = await api(`${base()}/settings`, { method: "PUT", data: {
        folder_id: f.folder_id.value.trim(), api_key: f.api_key.value.trim() || null } });
      renderSettings(); toast("Настройки Yandex Cloud сохранены.");
    }, $("#ai-error"));
  });
  voiceForm.addEventListener("submit", event => {
    event.preventDefault();
    guarded(voiceForm.querySelector('[type="submit"]'), async () => {
      const f = voiceForm.elements, limit = f.monthly_limit_rub.value.trim();
      settings = await api(`${base()}/settings`, { method: "PUT", data: {
        voice_enabled: f.voice_enabled.checked, voice_max_seconds: Number(f.voice_max_seconds.value),
        monthly_limit_rub: limit === "" ? null : Number(limit) } });
      renderSettings(); $("#ai-voice-options-result").textContent = "Параметры записи сохранены.";
    }, $("#ai-voice-options-error"));
  });

  $("#ai-verify").addEventListener("click", () => guarded($("#ai-verify"), async () => {
    settings = await api(`${base()}/settings/verify`, { method: "POST", timeoutMs: 40000 });
    renderSettings(); toast(settings.ok ? "Yandex SpeechKit отвечает: распознавание работает." : "Проверка не прошла.", !settings.ok);
  }, $("#ai-error")));
  $("#ai-yandex-tts-verify").addEventListener("click", () => guarded($("#ai-yandex-tts-verify"), async () => {
    settings = await api(`${base()}/gateways/yandex/verify`, { method: "POST", timeoutMs: 40000, data: { capability: "synthesis" } });
    renderSettings();
    if (!settings.ok) {
      const code = settings.gateways?.yandex?.checks?.synthesis?.error_code;
      notice(code === "unauthorized" ? "Для синтеза речи у сервисного аккаунта Яндекса должна быть роль ai.speechkit-tts.user. Проверьте ключ и права аккаунта." : checkErrors[code] || "Проверка синтеза не прошла.", $("#ai-error"));
    }
  }, $("#ai-error")));
  $("#ai-clear").addEventListener("click", () => guarded($("#ai-clear"), async () => {
    if (!(await confirm("Удалить ключ Yandex Cloud?", "Если для распознавания выбран Яндекс, голосовой ввод отключится.", "Удалить"))) return;
    settings = await api(`${base()}/settings`, { method: "PUT", data: { folder_id: form.elements.folder_id.value.trim(), clear_key: true,
      ...((settings?.gateways?.recognition_provider || "yandex") === "yandex" ? { voice_enabled: false } : {}) } });
    renderSettings(); toast("Ключ удалён.");
  }, $("#ai-error")));

  dsForm.addEventListener("submit", event => {
    event.preventDefault();
    guarded($("button[type=submit]", dsForm), async () => {
      const key = dsForm.elements.api_key.value.trim();
      if (!key) { notice("Вставьте API-ключ DeepSeek.", $("#ai-deepseek-error")); return; }
      settings = await api(`${base()}/deepseek`, { method: "PUT", data: { api_key: key } });
      renderSettings(); toast("Ключ DeepSeek сохранён. Нажмите «Проверить связь».");
    }, $("#ai-deepseek-error"));
  });
  $("#ai-deepseek-verify").addEventListener("click", () => guarded($("#ai-deepseek-verify"), async () => {
    settings = await api(`${base()}/deepseek/verify`, { method: "POST", timeoutMs: 40000 });
    renderSettings(); toast(settings.ok ? "DeepSeek отвечает: модель работает." : "Проверка DeepSeek не прошла.", !settings.ok);
  }, $("#ai-deepseek-error")));
  $("#ai-deepseek-clear").addEventListener("click", () => guarded($("#ai-deepseek-clear"), async () => {
    if (!(await confirm("Удалить ключ DeepSeek?", "Агенты с моделью DeepSeek будут вставлять текст без обработки.", "Удалить"))) return;
    settings = await api(`${base()}/deepseek`, { method: "PUT", data: { clear_key: true } });
    renderSettings(); toast("Ключ DeepSeek удалён.");
  }, $("#ai-deepseek-error")));

  const checkNames = { text: "Обработка текста", recognition: "Распознавание речи", synthesis: "Синтез речи" };
  const checkErrors = { no_balance: "Недостаточно средств", unauthorized: "Ключ отклонён", rate_limited: "Лимит запросов — повторите позже",
    unavailable: "Сервис временно недоступен", network: "Нет связи с сервисом", bad_response: "Сервис вернул некорректный ответ",
    bad_request: "Сервис не принял запрос", not_connected: "Добавьте ключ", speech_language: "Язык не поддерживается" };
  function renderGatewayStatus(card, item) {
    const provider = card.dataset.gateway, checks = item.checks || {}, pending = verifying.get(provider)?.capability;
    const passed = Object.keys(checkNames).filter(key => checks[key]?.ok === true).length;
    const failed = Object.keys(checkNames).filter(key => checks[key]?.ok === false);
    const badge = card.querySelector(".gateway-status"), count = card.querySelector(".gateway-check-count");
    badge.textContent = pending ? "Проверяем…" : !item.configured ? "Не подключено" : failed.length ? "Есть ошибки"
      : passed === 3 ? "Работает" : passed ? `Проверено ${passed} из 3` : "Не проверено";
    badge.dataset.state = pending ? "pending" : !item.configured ? "" : failed.length ? "error" : passed === 3 ? "active" : passed ? "partial" : "";
    badge.title = pending ? `Проверяется: ${checkNames[pending] || "возможности сервиса"}` : !item.configured ? "Сначала сохраните API-ключ."
      : failed.length ? failed.map(key => `${checkNames[key]}: ${checkErrors[checks[key].error_code] || "Проверка не прошла"}`).join(". ")
      : `Успешно проверено возможностей: ${passed} из 3.`;
    if (count) count.textContent = pending ? `Проверяем: ${checkNames[pending]}` : item.configured ? `Доступно ${passed} из 3 возможностей` : "Добавьте ключ для проверки";
    card.querySelector(".gateway-clear").disabled = !item.configured || Boolean(pending);
    card.querySelector(".gateway-verify-all").disabled = !item.configured || Boolean(pending);
    card.querySelector('button[type="submit"]').disabled = Boolean(pending);
    for (const button of card.querySelectorAll("[data-verify]")) {
      const capability = button.dataset.verify, check = item.configured ? checks[capability] : null;
      button.disabled = !item.configured || Boolean(pending);
      button.textContent = pending === capability ? "Проверяем…" : "Проверить";
      const output = card.querySelector(`[data-check="${capability}"]`);
      output.dataset.state = !check ? "" : check.ok ? "active" : "error";
      output.textContent = !check ? "Не проверено" : (check.ok ? "Работает" : checkErrors[check.error_code] || "Проверка не прошла");
      output.title = check?.checked_at ? `Последняя проверка: ${new Date(check.checked_at).toLocaleString("ru-RU")}` : "Проверка ещё не запускалась";
      const row = output.closest(".gateway-capability");
      let time = row.querySelector("time");
      if (!time) { time = document.createElement("time"); row.querySelector("span").append(time); }
      time.hidden = !check?.checked_at;
      if (check?.checked_at) { time.dateTime = check.checked_at; time.textContent = new Date(check.checked_at).toLocaleString("ru-RU"); }
    }
  }
  function renderSynthesisVoices(provider, selected) {
    const gateways = settings.gateways || {};
    const voices = gateways.synthesis_voice_catalog?.[provider] || (provider === "yandex"
      ? [{ id: "marina", title: "Марина · русский" }, { id: "john", title: "John · английский" }]
      : (gateways.synthesis_voices || ["Mia"]).map(id => ({ id, title: id })));
    const field = routesForm.elements.synthesis_voice;
    field.replaceChildren(...voices.map(voice => new Option(voice.title, voice.id)));
    field.value = voices.some(voice => voice.id === selected) ? selected : voices[0]?.id || "";
  }
  function renderGateways() {
    const gateways = settings.gateways || {};
    if (routesForm) {
      for (const [key, fallback] of Object.entries({ recognition_provider: "yandex", recognition_language: "ru", synthesis_provider: "disabled" }))
        routesForm.elements[key].value = gateways[key] || fallback;
      renderSynthesisVoices(gateways.synthesis_provider || "disabled", gateways.synthesis_voice);
    }
    for (const card of gatewayCards) {
      const item = gateways[card.dataset.gateway] || {}, f = card.querySelector("form").elements;
      f.api_key.value = ""; f.api_key.placeholder = item.key_hint ? `Сохранён ключ …${item.key_hint}` : "Вставьте ключ";
      if (item.base_url) f.base_url.value = item.base_url;
      f.protocol.value = item.protocol || "chat";
      const details = card.querySelector(".gateway-credentials"); if (details) details.open = !item.configured;
      renderGatewayStatus(card, item);
    }
  }
  for (const card of gatewayCards) {
    const f = card.querySelector("form"), errorTarget = card.querySelector(".gateway-error"), path = () => `${base()}/gateways/${card.dataset.gateway}`;
    f.addEventListener("submit", event => {
      event.preventDefault();
      guarded(f.querySelector('[type="submit"]'), async () => {
        const key = f.elements.api_key.value.trim();
        settings = await api(path(), { method: "PUT", data: { api_key: key || null,
          base_url: f.elements.base_url.value, protocol: f.elements.protocol.value } });
        renderSettings();
      }, errorTarget);
    });
    card.querySelector(".gateway-clear").addEventListener("click", event => guarded(event.currentTarget, async () => {
      if (!(await confirm("Удалить ключ шлюза?", "Назначенные этому шлюзу действия станут недоступны. Другой шлюз автоматически не включится.", "Удалить"))) return;
      settings = await api(path(), { method: "PUT", data: { clear_key: true } }); renderSettings();
    }, errorTarget));
    async function verifyCapabilities(capabilities) {
      const provider = card.dataset.gateway, mine = seq, requestPath = path(), token = {};
      if (verifying.has(provider) || !settings.gateways?.[provider]?.configured) return;
      notice("", errorTarget);
      try {
        for (const capability of capabilities) {
          if (mine !== seq) return;
          verifying.set(provider, { capability, token }); renderGatewayStatus(card, settings.gateways[provider]);
          const loaded = await api(requestPath + "/verify", { method: "POST", timeoutMs: 65000, data: { capability } });
          if (mine !== seq) return;
          // Independent cards may finish out of order: merge only this provider's checks.
          settings.gateways[provider] = loaded.gateways[provider];
          renderGatewayStatus(card, settings.gateways[provider]);
        }
      } catch (error) { if (mine === seq && error.name !== "AbortError") notice(error.message, errorTarget); }
      finally {
        if (verifying.get(provider)?.token === token) verifying.delete(provider);
        if (mine === seq) renderGatewayStatus(card, settings.gateways[provider]);
      }
    }
    for (const button of card.querySelectorAll("[data-verify]")) button.addEventListener("click", () => verifyCapabilities([button.dataset.verify]));
    card.querySelector(".gateway-verify-all").addEventListener("click", () => verifyCapabilities(Object.keys(checkNames)));
  }
  routesForm?.addEventListener("submit", event => {
    event.preventDefault();
    guarded(routesForm.querySelector('[type="submit"]'), async () => {
      const data = {};
      for (const key of ["recognition_provider", "recognition_language", "synthesis_provider", "synthesis_voice"]) data[key] = routesForm.elements[key].value;
      settings = await api(`${base()}/gateways/routes`, { method: "PUT", data }); renderSettings();
    }, $("#ai-routes-error"));
  });
  routesForm?.elements.recognition_provider.addEventListener("change", () => {
    routesForm.elements.recognition_language.value = "ru";
  });
  routesForm?.elements.synthesis_provider.addEventListener("change", () => {
    renderSynthesisVoices(routesForm.elements.synthesis_provider.value);
  });
  $("#ai-synthesis-play").addEventListener("click", () => guarded($("#ai-synthesis-play"), async () => {
    await $("#ai-synthesis-audio").play();
  }, $("#ai-synthesis-error")));
  $("#ai-synthesis-test")?.addEventListener("click", event => guarded(event.currentTarget, async () => {
    const text = $("#ai-synthesis-text").value.trim(), mine = seq, route = synthesisRoute;
    if (!text) throw new Error("Введите текст для синтеза.");
    if (routesForm.elements.synthesis_provider.value !== settings.gateways.synthesis_provider || routesForm.elements.synthesis_voice.value !== settings.gateways.synthesis_voice)
      throw new Error("Сохраните выбранный шлюз и голос кнопкой «Сохранить маршруты».");
    const audio = $("#ai-synthesis-audio"), result = $("#ai-synthesis-result"), play = $("#ai-synthesis-play");
    audio.pause(); show(play, false); show(audio, false); result.textContent = "Создаём аудио…";
    if (audioUrl) URL.revokeObjectURL(audioUrl); audioUrl = null;
    audio.removeAttribute("src"); audio.load();
    let answer;
    try { answer = await api(`${base()}/synthesize`, { method: "POST", timeoutMs: 65000, data: { text } }); }
    catch (error) { if (mine !== seq || route !== synthesisRoute) return; result.textContent = ""; throw error; }
    if (mine !== seq || route !== synthesisRoute) return;
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    const bytes = Uint8Array.from(atob(answer.audio), char => char.charCodeAt(0));
    audioUrl = URL.createObjectURL(new Blob([bytes], { type: answer.content_type }));
    const billing = answer.billing === "subscription" ? "Использована квота Token Plan."
      : answer.billing === "pay_as_you_go" ? `Оценка стоимости: ${money(answer.cost_rub)} ₽.` : "Синтез выполнен по промотарифу MiMo.";
    show(audio); audio.preload = "auto";
    result.textContent = billing + " Загружаем проигрыватель…";
    try {
      await new Promise((resolve, reject) => {
        let timer;
        const clean = () => { clearTimeout(timer); audio.removeEventListener("loadedmetadata", ready); audio.removeEventListener("error", failed); };
        const ready = () => { clean(); Number.isFinite(audio.duration) && audio.duration > 0 ? resolve() : reject(new Error("Получено аудио без доступной длительности.")); };
        const failed = () => { clean(); reject(new Error("Браузер не смог открыть полученное аудио. Повторите создание записи.")); };
        audio.addEventListener("loadedmetadata", ready); audio.addEventListener("error", failed);
        timer = setTimeout(failed, 12000); audio.src = audioUrl; audio.load();
      });
      if (mine !== seq || route !== synthesisRoute) return;
      show(play); result.textContent = `Аудио готово · ${Math.ceil(audio.duration)} сек. ${billing} Нажмите «Воспроизвести».`;
    } catch (error) { if (mine !== seq || route !== synthesisRoute) return; show(audio, false); result.textContent = billing; throw error; }
  }, $("#ai-synthesis-error")));


  $("#ai-agent-new").addEventListener("click", () => edit(null));
  for (const radio of agentForm.querySelectorAll('[name="provider"]')) radio.addEventListener("change", () => {
    renderModels(settings.models.find(item => item.provider === agentForm.elements.provider.value)?.id);
  });
  agentForm.elements.model.addEventListener("change", () => renderModels(agentForm.elements.model.value));
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
    const { name, enabled, is_default, ...rest } = values();
    const answer = await api(`${base()}/agents/test`, { method: "POST", timeoutMs: 30000, data: { text: sample, ...rest } });
    result.replaceChildren(node("strong", answer.fallback ? "Модель не справилась, текст без изменений:" : "Результат:"), node("span", answer.text),
      node("small", answer.billing === "subscription" ? "Использована квота Token Plan" : `Оценка стоимости: ${money(answer.cost_rub)} ₽`));
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
          const f = agentForm.elements; f.name.value = version.name; f.task.value = version.task || "voice_input"; renderModels(version.model); f.prompt.value = version.prompt;
          f.temperature.value = version.temperature; temperature(version.temperature);
          toast("Версия перенесена в форму. Нажмите «Сохранить агента», чтобы применить.");
        });
        item.append(restore); return item;
      }));
    } catch (error) { notice(error.message, $("#ai-agents-error")); }
  });
  return { load };
};
