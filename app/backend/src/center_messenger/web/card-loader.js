"use strict";
window.CenterCardLoader = ({ $, state, api, show }) => {
  const form = $("#card-loader-form"), error = $("#card-loader-error");
  const errorText = $("#card-loader-error-text"), retry = $("#card-loader-retry");
  const status = $("#card-loader-status"), limits = $("#card-loader-limits");
  const submit = form.querySelector('button[type="submit"]');
  const fields = Array.from(form.querySelectorAll("input[name],select[name]"));
  const value = field => field.type === "checkbox" ? field.checked : Number(field.value);
  const values = () => Object.fromEntries(fields.map(field => [field.name, value(field)]));
  const defaults = values(), touched = new Set();
  let baseline = { ...defaults }, loadedWorkspace = null, generation = 0, pendingLoad = null, saving = false;

  const canManage = () => ["owner", "admin"].includes(state.workspace?.role);
  const current = (workspace, epoch, gen) => workspace === state.workspace?.id && epoch === state.epoch && gen === generation;
  const changed = () => fields.some(field => value(field) !== baseline[field.name]);
  function assign(field, next) {
    if (field.type === "checkbox") field.checked = Boolean(next);
    else {
      if (field.tagName === "SELECT" && !Array.from(field.options).some(option => option.value === String(next))) {
        field.add(new Option(field.name === "history_interval_minutes" ? `Каждые ${next} мин` : String(next), String(next)));
      }
      field.value = String(next);
    }
  }
  function describe(message) {
    fields.forEach(field => { field.disabled = !canManage(); });
    submit.disabled = !canManage() || loadedWorkspace !== state.workspace?.id || saving || Boolean(pendingLoad);
    submit.textContent = saving ? "Сохраняем…" : "Сохранить настройки загрузчика";
    form.classList.toggle("has-unsaved-changes", changed());
    status.textContent = message || (saving ? "Сохраняем настройки…" : changed()
      ? "Есть несохранённые изменения."
      : loadedWorkspace ? "Настройки загружены. Значения можно изменить." : "Рекомендуемые значения заполнены. Настройки можно изменить.");
  }
  function failure(message, loading = false) {
    errorText.textContent = message; show(error); show(retry, loading);
  }
  function renderServer(payload, preserve) {
    const before = values();
    baseline = { ...defaults, ...payload };
    for (const field of fields) if (!preserve(field, before)) assign(field, baseline[field.name]);
    touched.clear();
    for (const field of fields) if (value(field) !== baseline[field.name]) touched.add(field.name);
    const count = payload.parallel_card_jobs ?? 1;
    limits.textContent = `${count} ${count === 1 ? "карточка" : "карточки"} одновременно · лимит ${payload.requests_per_minute ?? 120} запросов в минуту`;
    errorText.textContent = ""; show(error, false);
  }
  function load({ force = false } = {}) {
    const workspace = state.workspace;
    if (!workspace || !canManage()) { describe(); return Promise.resolve(); }
    const epoch = state.epoch, gen = generation;
    if (pendingLoad && pendingLoad.workspace === workspace.id && pendingLoad.epoch === epoch && pendingLoad.gen === gen) return pendingLoad.promise;
    // Switching tabs must not reload over a draft already being edited.
    if (!force && loadedWorkspace === workspace.id) return Promise.resolve();
    const entry = { workspace: workspace.id, epoch, gen, values: values(), promise: null };
    pendingLoad = entry; retry.disabled = true;
    describe("Проверяем сохранённые настройки. Поля можно редактировать.");
    entry.promise = (async () => {
      try {
        const payload = await api(`/workspaces/${workspace.id}/directory/card-loader`);
        if (!current(workspace.id, epoch, gen)) return;
        loadedWorkspace = workspace.id;
        renderServer(payload, (field, before) => touched.has(field.name) || before[field.name] !== entry.values[field.name]);
      } catch (cause) {
        if (!current(workspace.id, epoch, gen)) return;
        loadedWorkspace = null;
        failure(`Не удалось прочитать настройки загрузчика. ${cause.message}`, true);
      } finally {
        if (pendingLoad === entry) pendingLoad = null;
        if (current(workspace.id, epoch, gen)) {
          retry.disabled = false;
          describe(loadedWorkspace ? undefined : "Поля доступны для редактирования. Повторите загрузку настроек, чтобы разрешить сохранение на сервере.");
        }
      }
    })();
    return entry.promise;
  }
  function reset() {
    generation += 1; pendingLoad = null; loadedWorkspace = null; saving = false; touched.clear();
    form.reset(); baseline = { ...defaults };
    for (const field of fields) assign(field, defaults[field.name]);
    errorText.textContent = ""; show(error, false); show(retry, false); retry.disabled = false;
    limits.textContent = "1 карточка одновременно · лимит 120 запросов в минуту";
    describe();
  }
  form.addEventListener("input", event => {
    if (fields.includes(event.target)) { touched.add(event.target.name); describe(); }
  });
  form.addEventListener("change", event => {
    if (fields.includes(event.target)) { touched.add(event.target.name); describe(); }
  });
  retry.addEventListener("click", () => { void load({ force: true }); });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const workspace = state.workspace;
    if (!workspace || !canManage() || loadedWorkspace !== workspace.id || saving || pendingLoad) return;
    if (!form.checkValidity()) { form.reportValidity(); return; }
    const payload = values();
    if (payload.manual_max_wait_seconds < payload.manual_priority_seconds) {
      failure("Порог задержки должен быть не меньше времени повышения приоритета."); return;
    }
    if (payload.retry_max_seconds < payload.retry_initial_seconds) {
      failure("Максимальная пауза должна быть не меньше первого повтора."); return;
    }
    const epoch = state.epoch, gen = generation;
    saving = true; describe();
    let succeeded = false;
    try {
      const saved = await api(`/workspaces/${workspace.id}/directory/card-loader`, { method: "PUT", data: payload });
      if (!current(workspace.id, epoch, gen)) return;
      // Keep any newer edit made while the save request was in flight.
      renderServer(saved, (field, before) => before[field.name] !== payload[field.name]);
      succeeded = true;
    } catch (cause) {
      if (current(workspace.id, epoch, gen)) failure(cause.message);
    } finally {
      if (current(workspace.id, epoch, gen)) {
        saving = false;
        describe(succeeded ? changed() ? "Сохранено. Более новые изменения ещё не сохранены." : "Настройки сохранены." : undefined);
      }
    }
  });
  reset();
  return { load, reset };
};
