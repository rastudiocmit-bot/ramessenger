"use strict";
window.CenterCardLoader = ({ $, state, api, show }) => {
  const form = $("#card-loader-form");
  const error = $("#card-loader-error");
  const status = $("#card-loader-status");
  let loadedWorkspace = null;

  const number = name => Number(form.elements[name].value);
  function render(payload) {
    for (const [name, value] of Object.entries(payload)) {
      const field = form.elements[name];
      if (!field) continue;
      if (field.type === "checkbox") field.checked = Boolean(value);
      else field.value = String(value);
    }
    status.textContent = `${payload.parallel_card_jobs} карточка одновременно · пауза API не менее ${payload.host_request_gap_ms} мс`;
    error.textContent = ""; show(error, false);
  }
  async function load() {
    const workspace = state.workspace;
    if (!workspace || !["owner", "admin"].includes(workspace.role)) return;
    const payload = await api(`/workspaces/${workspace.id}/directory/card-loader`);
    if (workspace.id !== state.workspace?.id) return;
    loadedWorkspace = workspace.id; render(payload);
  }
  function reset() {
    loadedWorkspace = null; form?.reset();
    if (error) { error.textContent = ""; show(error, false); }
    if (status) status.textContent = "";
  }
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    const workspace = state.workspace;
    if (!workspace || loadedWorkspace !== workspace.id) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true; button.textContent = "Сохраняем…";
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
      render(await api(`/workspaces/${workspace.id}/directory/card-loader`, { method: "PUT", data: payload }));
      status.textContent += " · сохранено";
    } catch (cause) {
      error.textContent = cause.message; show(error);
    } finally {
      button.disabled = false; button.textContent = "Сохранить настройки загрузчика";
    }
  });
  return { load, reset };
};
