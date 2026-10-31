"use strict";
window.CenterTelephonyProcessing = ({$,state,api,notice,confirm,onChange=()=>{}}) => {
  const form=$("#telephony-processing-form");let saved=null,busy=false,generation=0,subject="",timer=null;
  const who=()=>`${state.user?.id}:${state.workspace?.id}:${state.workspace?.role}`;
  const admin=()=>["owner","admin"].includes(state.workspace?.role);
  const current=c=>c.generation===generation&&c.subject===who()&&admin();
  const context=()=>({generation,subject:who(),workspace:state.workspace?.id});
  const endpoint=c=>`/workspaces/${c.workspace}/integrations/novofon`;
  const dirty=()=>saved&&form.elements.auto_new.checked!==saved.auto_new;
  function controls(){
    form.querySelectorAll("input,button").forEach(e=>e.disabled=busy||!saved||!admin());
    $("#telephony-processing-refresh").disabled=busy||!admin();
    $("#telephony-archive-start").disabled=busy||!saved||saved.archive.enabled||!admin();
    $("#telephony-archive-pause").disabled=busy||!saved||!saved.archive.enabled||!admin();
    form.querySelector('[type=submit]').disabled=busy||!saved||!dirty()||!admin();
    $("#telephony-processing-draft-status").textContent=dirty()?"Есть несохранённое изменение":"";
    onChange({value:saved,dirty:Boolean(dirty()),busy});
  }
  function monitor(value){
    const archive=value.archive,counts=value.counts||{};
    $("#telephony-processing-counts").textContent=`Готовых описаний: ${counts.complete||0} · Нужна проверка: ${(counts.needs_review||0)+(counts.error||0)} · Ожидают бюджета: ${counts.budget_wait||0}`;
    const metrics=[["Готово",counts.complete||0],["В очереди",(counts.queued||0)+(counts.waiting_recording||0)],
      ["Обработка начата",(counts.fetching||0)+(counts.recognizing||0)+(counts.summarizing||0)],
      ["Нужна проверка",(counts.error||0)+(counts.needs_review||0)],["Ожидают бюджет",counts.budget_wait||0]];
    $("#telephony-processing-metrics").replaceChildren(...metrics.map(([title,n])=>{const item=document.createElement('div'),label=document.createElement('span'),value=document.createElement('strong');label.textContent=title;value.textContent=n;item.append(label,value);return item;}));
    $("#telephony-archive-status").textContent=!archive.enabled?"Ночной архив приостановлен":archive.waiting_for_night?"Ожидаем ночное окно 00:00–08:00":"Ночная очередь включена";
    $("#telephony-archive-progress").textContent=`Загружено уникальных старых звонков: ${archive.imported||0}`;
    const time=archive.next_at?new Intl.DateTimeFormat("ru-RU",{dateStyle:"short",timeStyle:"short",timeZone:"Europe/Moscow"}).format(new Date(archive.next_at)):null;
    $("#telephony-archive-next").textContent=archive.enabled&&time?`Следующая допустимая загрузка архива: ${time} · московское время`:"Прогресс сохраняется; повторный запуск продолжит архив с прежнего места.";
    const errors={quota_exhausted:"Ожидаем восстановления лимита API Новофона",archive_wait:"Проверьте подключение Новофона и настройки телефонного агента",provider_result_unknown:"Результат запроса ИИ не подтверждён. Автоматический повтор остановлен",processing_not_ready:"Проверьте выбранные ИИ-шлюзы и права запуска",archive_interrupted:"Чтение архива прервалось; прогресс сохранён"};
    $("#telephony-archive-error").textContent=archive.error_code?(errors[archive.error_code]||"Архив ожидает проверки подключения или ответа сервиса")+(archive.retry_at?". Повтор возможен после паузы в ночном окне.":"."):"";
  }
  function render(value){saved=value;form.elements.auto_new.checked=value.auto_new;monitor(value);controls();}
  function reset(){generation++;subject=who();clearTimeout(timer);saved=null;busy=false;form.reset();
    for(const id of ["telephony-processing-counts","telephony-processing-metrics","telephony-archive-progress","telephony-archive-next","telephony-archive-error","telephony-processing-message"])$("#"+id).textContent="";
    $("#telephony-archive-status").textContent="Состояние ещё не получено";notice("",$("#telephony-processing-error"));controls();}
  async function run(action){
    if(subject!==who())reset();if(busy||!admin()||!state.workspace)return;
    const c=context();busy=true;controls();notice("",$("#telephony-processing-error"));
    try{await action(c);}catch(error){if(current(c))notice(error.message,$("#telephony-processing-error"));}
    finally{if(current(c)){busy=false;controls();}}
  }
  async function load(force=false){
    if(subject!==who())reset();if(!force&&dirty())return;
    const initial=context();await run(async c=>{const value=await api(endpoint(c)+"/processing");if(current(c)){
      if(force==="monitor"&&dirty()){saved={...saved,archive:value.archive,counts:value.counts};monitor(value);controls();}else render(value);
    }});if(!current(initial))return;
    clearTimeout(timer);const c=context();timer=setTimeout(()=>{if(current(c)&&state.pane==="connections")void load("monitor");},15000);
  }
  form.addEventListener("submit",event=>{event.preventDefault();if(!saved)return;
    void run(async c=>{const value=await api(endpoint(c)+"/processing",{method:"PUT",data:{expected_version:saved.version,auto_new:form.elements.auto_new.checked}});
      if(current(c)){render(value);$("#telephony-processing-message").textContent=value.auto_new?"Новые звонки будут обрабатываться в фоне. Уже сохранённая история не запускается повторно.":"Автоматическая обработка новых звонков выключена. Готовые тексты сохранены.";}});
  });
  form.addEventListener('input',controls);form.addEventListener('change',controls);
  async function archive(enabled){
    if(!saved)return;
    await run(async c=>{const value=await api(endpoint(c)+"/archive",{method:"PUT",data:{expected_version:saved.archive.version,enabled}});
      if(current(c)){if(dirty()){saved.archive=value.archive;monitor(value);}else render(value);
        $("#telephony-processing-message").textContent=enabled?"Ночной архив запущен: 00:00–08:00 по Москве, не чаще одного старого звонка за 120 секунд.":"Ночной архив приостановлен. Курсор и готовые результаты сохранены.";}});
  }
  $("#telephony-archive-start").addEventListener("click",()=>void archive(true));
  $("#telephony-archive-pause").addEventListener("click",()=>void archive(false));
  $("#telephony-processing-refresh").addEventListener("click",async()=>{const c=context();if(dirty()&&!await confirm("Обновить настройки обработки?","Несохранённое изменение флажка будет сброшено.","Обновить"))return;if(current(c))await load(true);});
  reset();return {load,reset};
};
