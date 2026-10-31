"use strict";
// Dedicated telephone agent. Opening/saving this form makes no AI request.
window.CenterTelephonyAgent = ({ $, state, api, notice, confirm, onChange=()=>{}, onProcessing=()=>{} }) => {
  const processing=window.CenterTelephonyProcessing?.({$,state,api,notice,confirm,onChange:onProcessing});
  const form = $("#telephony-agent-form"), sample = $("#telephony-agent-sample");
  let saved = null, busy = false, generation = 0, subject = "";
  const who = () => `${state.user?.id || ""}:${state.workspace?.id || ""}:${state.workspace?.role || ""}`;
  const admin = () => ["owner", "admin"].includes(state.workspace?.role);
  const ctx = () => ({ generation, subject: who(), workspace: state.workspace?.id });
  const current = c => c.generation === generation && c.subject === who() && admin();
  const endpoint = c => `/workspaces/${c.workspace}/integrations/novofon/agent`;
  const fields = () => ({ name: form.elements.agent_name.value.trim(), model: form.elements.model.value,
    recognition_provider: form.elements.recognition_provider.value, prompt: form.elements.prompt.value.trim(),
    temperature: Number(form.elements.temperature.value) });
  const dirty = () => saved && Object.entries(fields()).some(([key, value]) => value !== saved[key]);
  function controls() {
    form.querySelectorAll("input,select,textarea,button").forEach(el => { el.disabled = busy || !saved || !admin(); });
    $("#telephony-agent-test").disabled = busy || !saved?.version || dirty() || !sample.value.trim() || !admin();
    sample.disabled = busy || !saved || !admin();
    $("#telephony-agent-refresh").disabled = busy || !admin();
    $("#telephony-agent-cancel").disabled = busy || !saved || !dirty() || !admin();
    form.querySelector('[type=submit]').disabled=busy||!saved||!admin()||Boolean(saved.version&&!dirty());
    $("#telephony-agent-draft-status").textContent=!saved?"Получаем настройки…":dirty()?"Есть несохранённые изменения":saved.version?`Сохранена версия ${saved.version}`:"Новый агент · ещё не сохранён";
    $("#telephony-agent-test-version").textContent=dirty()?"Сначала сохраните изменения во вкладке «Настройки». Проверяется только сохранённая версия.":saved?.version?`Проверяется сохранённая версия ${saved.version}. Отправка текста может оплачиваться по тарифу шлюза; звонки не изменяются.`:"Сначала сохраните агента во вкладке «Настройки».";
    $("#telephony-agent-result-empty").hidden=Boolean($("#telephony-agent-result").textContent.trim());
    $("#telephony-agent-card").setAttribute("aria-busy", String(busy));
    onChange({value:saved,dirty:Boolean(dirty()),busy});
  }
  function options(select, values, selected) {
    select.replaceChildren(...values.map(item => {
      const option = document.createElement("option"); option.value = item.id;
      option.textContent = item.title; return option;
    })); select.value = selected;
  }
  function render(value) {
    saved = value;
    form.elements.agent_name.value = value.name;
    form.elements.prompt.value = value.prompt;
    form.elements.temperature.value = value.temperature;
    options(form.elements.model, value.models.map(item => ({id:item.id,title:`${item.provider_title} · ${item.title}`})), value.model);
    options(form.elements.recognition_provider, value.recognizers.map(item => ({id:item.id,title:`${item.title}${item.configured ? "" : " · ключ не подключён"}`})), value.recognition_provider);
    $("#telephony-agent-version").textContent = value.version ? `Версия ${value.version}` : "Не сохранён";
    $("#telephony-agent-history").replaceChildren(...[...(value.history || [])].reverse().map(item => {
      const details = document.createElement("details"), title = document.createElement("summary"), prompt = document.createElement("p");
      title.textContent = `Версия ${item.version} · ${item.name} · ${item.model}`;
      prompt.textContent = item.prompt; details.append(title,prompt); return details;
    }));
    controls();
  }
  function reset() {
    processing?.reset();
    generation++; subject = who(); saved = null; busy = false; form.reset(); sample.value = "";
    for (const id of ["telephony-agent-result","telephony-agent-message","telephony-agent-history"]) $("#"+id).replaceChildren();
    notice("", $("#telephony-agent-error")); $("#telephony-agent-version").textContent = "Состояние ещё не получено"; controls();
  }
  async function run(action) {
    if (subject !== who()) reset();
    if (busy || !admin() || !state.workspace) return;
    const c = ctx(); busy = true; controls(); notice("",$("#telephony-agent-error"));
    try { await action(c); }
    catch (error) { if (current(c)) notice(error.message || "Не удалось выполнить действие.",$("#telephony-agent-error")); }
    finally { if (current(c)) { busy = false; controls(); } }
  }
  async function load(force = false) {
    if (subject !== who()) reset();
    void processing?.load();
    if (!force && dirty()) return;
    return run(async c => { const value = await api(endpoint(c)); if (current(c)) render(value); });
  }
  form.addEventListener("submit", event => {
    event.preventDefault(); if (!saved || !form.reportValidity()) return;
    void run(async c => {
      const value = await api(endpoint(c), {method:"PUT",data:{expected_version:saved.version,...fields()}});
      if (current(c)) { render(value); $("#telephony-agent-result").textContent = "";
        $("#telephony-agent-message").textContent = "Телефонный агент сохранён. Настройки применятся к новым заданиям; готовые звонки не перерабатываются."; }
    });
  });
  $("#telephony-agent-test").addEventListener("click", () => {
    if (!saved?.version || dirty() || !sample.value.trim()) return;
    void run(async c => {
      $("#telephony-agent-result").textContent = "Готовим описание примера…";
      try {
        const value = await api(endpoint(c)+"/test",{method:"POST",timeoutMs:45000,
          data:{expected_version:saved.version,text:sample.value.trim()}});
        if (current(c)) { $("#telephony-agent-result").textContent = value.summary;
          $("#telephony-agent-message").textContent = `Проверена версия ${value.version}. ${value.billing === "subscription" ? "Расход по подписке выбранного шлюза." : `Оценка расхода: ${value.cost_rub} ₽.`} Звонки не изменены.`; }
      } catch (error) { if (current(c)) $("#telephony-agent-result").textContent = ""; throw error; }
    });
  });
  $("#telephony-agent-refresh").addEventListener("click", async () => {
    const c = ctx();
    if (dirty() && !await confirm("Обновить телефонного агента?", "Несохранённые настройки будут заменены сохранённой версией.", "Обновить")) return;
    if (current(c)) await load(true);
  });
  $("#telephony-agent-cancel").addEventListener("click",()=>{if(!busy&&saved){render(saved);notice("",$("#telephony-agent-error"));$("#telephony-agent-message").textContent="Несохранённые изменения отменены.";}});
  form.addEventListener("input", () => { $("#telephony-agent-result").textContent=""; controls(); });
  form.addEventListener("change", controls); sample.addEventListener("input", controls);
  reset(); return {load,reset,dirty:()=>Boolean(dirty())};
};
