"use strict";
window.CenterCardLoader = ({ $, state, api, show }) => {
  const form = $("#card-loader-form");
  const error = $("#card-loader-error");
  const errorText = $("#card-loader-error-text");
  const retry = $("#card-loader-retry");
  const status = $("#card-loader-status");
  const submit = form.querySelector('button[type="submit"]');
  let loadedWorkspace = null, generation = 0, pendingLoad = null;

  const number = name => Number(form.elements[name].value);
  const canManage = () => ["owner", "admin"].includes(state.workspace?.role);
  const current = (workspace, epoch, gen) => workspace === state.workspace?.id && epoch === state.epoch && gen === generation;
  function controls(disabled) {
    form.querySelectorAll("input,select,button").forEach(field => { field.disabled = disabled; });
  }
  function failure(message) {
    errorText.textContent = message; show(error);
  }
  function render(payload) {
    for (const [name, value] of Object.entries(payload)) {
      const field = form.elements[name];
      if (!field) continue;
      if (field.type === "checkbox") field.checked = Boolean(value);
      else {
        // An API-saved interval may be valid without being one of the presets.
        if (field.tagName === "SELECT" && !Array.from(field.options).some(option => option.value === String(value))) {
          field.add(new Option(`Каждые ${value} мин`, String(value)));
        }
        field.value = String(value);
      }
    }
    status.textContent = `${payload.parallel_card_jobs ?? 1} карточка одновременно · пауза API не менее ${payload.host_request_gap_ms ?? 350} мс`;
    errorText.textContent = ""; show(error, false);
  }
  function load() {
    const workspace = state.workspace;
    if (!workspace || !canManage()) return Promise.resolve();
    const epoch = state.epoch, gen = generation;
    if (pendingLoad && pendingLoad.workspace === workspace.id && pendingLoad.epoch === epoch && pendingLoad.gen === gen) return pendingLoad.promise;
    const entry = { workspace: workspace.id, epoch, gen, promise: null };
    pendingLoad = entry;
    controls(true); retry.disabled = true;
    status.textContent = "Проверяем сохранённые настройки · значения по умолчанию уже заполнены";
    entry.promise = (async () => {
      try {
        const payload = await api(`/workspaces/${workspace.id}/directory/card-loader`);
        if (!current(workspace.id, epoch, gen)) return;
        loadedWorkspace = workspace.id; render(payload); controls(false);
      } catch (cause) {
        if (!current(workspace.id, epoch, gen)) return;
        loadedWorkspace = null;
        status.textContent = "Показаны рекомендуемые или ранее полученные значения. Сохранение доступно после проверки настроек.";
        failure(`Не удалось прочитать настройки загрузчика. ${cause.message}`);
      } finally {
        if (pendingLoad === entry) pendingLoad = null;
        if (current(workspace.id, epoch, gen)) retry.disabled = false;
      }
    })();
    return entry.promise;
  }
  function reset() {
    generation += 1; pendingLoad = null; loadedWorkspace = null;
    form.reset(); controls(true); retry.disabled = false;
    submit.textContent = "Сохранить настройки загрузчика";
    errorText.textContent = ""; show(error, false);
    status.textContent = "Рекомендуемые значения · 1 карточка одновременно · пауза API не менее 350 мс";
  }
  retry.addEventListener("click", () => { void load(); });
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    const workspace = state.workspace;
    if (!workspace || !canManage() || loadedWorkspace !== workspace.id || submit.disabled) return;
    const epoch = state.epoch, gen = generation;
    controls(true); submit.textContent = "Сохраняем…";
    try {
      const payload = {
        preload_unanswered: form.elements.preload_unanswered.checked,
        preload_active_groups: form.elements.preload_active_groups.checked,
        finance_interval_minutes: number("finance_interval_minutes"),
        lessons_interval_minutes: number("lessons_interval_minutes"),
        groups_interval_minutes: number("groups_interval_minutes"),
        history_interval_minutes: number("history_interval_minutes"),
        requests_per_turn: number("requests_per_turn"),
        busy_retry_seconds: number("busy_retry_seconds"),
        retry_initial_seconds: number("retry_initial_seconds"),
        retry_max_seconds: number("retry_max_seconds"),
        manual_priority_seconds: number("manual_priority_seconds"),
        manual_interest_minutes: number("manual_interest_minutes"),
        manual_burst: number("manual_burst"),
      };
      const saved = await api(`/workspaces/${workspace.id}/directory/card-loader`, { method: "PUT", data: payload });
      if (!current(workspace.id, epoch, gen)) return;
      render(saved);
      status.textContent += " · сохранено";
    } catch (cause) {
      if (current(workspace.id, epoch, gen)) failure(cause.message);
    } finally {
      if (current(workspace.id, epoch, gen)) { controls(false); submit.textContent = "Сохранить настройки загрузчика"; }
    }
  });
  reset();
  return { load, reset };
};
