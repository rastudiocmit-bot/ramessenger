"use strict";
// Presentation/navigation only. No requests, credentials, queue writes or paid tests.
window.CenterAgentsWorkspace = ({$,state,confirm}) => {
  const root=$("#agents-workspace"), purposes={
    voice_input:{title:"Голосовой ввод",description:"Приводит диктовку в порядок перед вставкой в сообщение. Распознавание речи настраивается в ИИ-шлюзах."},
    tasks:{title:"Задачи из сообщений",description:"Помогает формулировать задачи из переписки. Результат проверяет сотрудник."},
    telephone_conversations:{title:"Телефонные разговоры",description:"Отдельный агент распознаёт записи и готовит описание. Сохранение настроек не запускает обработку звонков."},
    text_processing:{title:"Обработка текста",description:"Пользовательские инструкции для работы с текстом. Назначение и включение определяются настройками выбранного агента."}
  };
  let selected="voice_input",section="settings",subject="",generic=null,phone=null,processing=null,adapter={},changing=false;
  const who=()=>`${state.user?.id||""}:${state.workspace?.id||""}:${state.workspace?.role||""}`;
  const admin=()=>["owner","admin"].includes(state.workspace?.role);
  const set=(id,text)=>$(id).textContent=text||"";
  const ready=(provider,settings)=>provider==="yandex"?settings?.configured:provider==="deepseek"?settings?.deepseek?.configured:settings?.gateways?.[provider]?.configured;
  function reset(){subject=who();generic=phone=processing=null;selected="voice_input";section="settings";changing=false;root.dataset.mobileView="list";paint();}
  function ensure(){if(subject!==who())reset();}
  function paint(){
    const isPhone=selected==="telephone_conversations";
    $("#agents-reload").disabled=!admin()||changing||Boolean(generic?.busy);
    for(const button of root.querySelectorAll('[data-agent-purpose]')){
      const task=button.dataset.agentPurpose;button.setAttribute("aria-pressed",String(task===selected));button.disabled=!admin()||changing||Boolean(generic?.busy);
      const rows=generic?.agents?.filter(a=>a.task===task)||[],active=rows.find(a=>a.enabled&&a.is_default);
      const model=active&&generic?.settings.models.find(m=>m.id===active.model),missing=active&&model?.provider&&!ready(model.provider,generic.settings);
      const value=task==="telephone_conversations"?phone?(phone.dirty?"Есть изменения":phone.value?.version?"Настройки сохранены":"Не настроен"):"Получаем состояние…":generic?(active?active.name+(missing?" · нужен ключ модели":""):rows.length?"Нет активного агента":"Агент не добавлен"):"Получаем состояние…";
      button.querySelector('.agents-purpose-state').textContent=value;
      button.dataset.state=task==="telephone_conversations"?(phone?.dirty?"draft":phone?.value?.version?"configured":"empty"):(missing?"warning":active?"configured":"empty");
    }
    set("#agents-current-title",purposes[selected].title);set("#agents-current-description",purposes[selected].description);
    $("#agents-generic-workarea").hidden=isPhone;$("#agents-phone-workarea").hidden=!isPhone;
    if(!isPhone&&section==="processing")section="settings";
    for(const button of root.querySelectorAll('[data-agent-section]')){
      button.hidden=button.dataset.agentSection==="processing"&&!isPhone;
      button.setAttribute("aria-pressed",String(button.dataset.agentSection===section));
      button.disabled=!admin()||button.dataset.agentSection==="check"&&!isPhone&&!generic?.editorOpen;
    }
    for(const container of [$("#agents-generic-workarea"),$("#agents-phone-workarea")])for(const panel of container.querySelectorAll('[data-agent-panel]'))panel.hidden=panel.dataset.agentPanel!==section;
    $("#telephony-agent-card").hidden=isPhone&&section==="processing";
    const badge=$("#agents-current-status");let status="Получаем состояние…",meta="",mode="";
    if(isPhone&&phone){
      const p=phone.value;
      status=phone.busy?"Выполняем запрос…":phone.dirty?"Есть несохранённые изменения":p?.version?"Настройки сохранены":"Сначала сохраните агента";
      mode=phone.dirty?"draft":p?.version?"configured":"";
      if(p){const model=p.models?.find(m=>m.id===p.model),recognizer=p.recognizers?.find(r=>r.id===p.recognition_provider);
        meta=`Распознавание: ${recognizer?.title||p.recognition_provider} · Описание: ${model?.title||p.model}${p.version?` · Версия ${p.version}`:""}`;
        if(p.version&&!recognizer?.configured){status="Подключите распознавание";mode="warning";}
        else if(p.version&&generic&&model?.provider&&!ready(model.provider,generic.settings)){status="Подключите модель описания";mode="warning";}
      }
      if(section==="processing"&&processing){status=processing.value.archive.enabled?(processing.value.archive.waiting_for_night?"Архив ожидает ночи":"Ночная очередь включена"):"Ночной архив приостановлен";mode=processing.value.archive.enabled?"configured":"";}
    }else if(!isPhone&&generic){
      const rows=generic.agents.filter(a=>a.task===selected),active=rows.find(a=>a.enabled&&a.is_default),editing=generic.editing;
      status=generic.busy?"Выполняем запрос…":generic.dirty?"Есть несохранённые изменения":generic.editorOpen&&!editing?"Новый агент":editing?!editing.enabled?"Агент выключен":editing.is_default?"Используется для задачи":"Дополнительный вариант":active?"Агент выбран для задачи":rows.length?"Нет активного агента":"Добавьте агента";
      mode=generic.dirty?"draft":active?"configured":"";
      if(editing)meta=`${editing.name} · ${editing.model_title||editing.model} · Версия ${editing.version}`;
      else if(generic.editorOpen)meta="Новый агент · ещё не сохранён";
      else if(active)meta=`Используется: ${active.name} · ${active.model_title||active.model}`;
      const model=generic.settings?.models.find(m=>m.id===(editing||active)?.model);
      if(!generic.dirty&&!generic.busy&&model?.provider&&!ready(model.provider,generic.settings)){status="Подключите модель";mode="warning";}
    }
    if(generic?.settings?.vault_ready===false&&!generic.busy){status="Проверьте подключение модели";mode="warning";}
    badge.textContent=status;badge.dataset.state=mode;set("#agents-current-meta",meta);
    const settings=generic?.settings,spent=settings?.month_spent_rub;
    set("#agents-budget-value",settings?(Number.isFinite(Number(spent))&&spent!=null?`${Number(spent).toLocaleString("ru-RU",{maximumFractionDigits:2})} ₽ за месяц`:"Расход доступен в ИИ-шлюзах")+(settings.monthly_limit_rub!=null?` · лимит ${Number(settings.monthly_limit_rub).toLocaleString("ru-RU")} ₽`:""):"Получаем состояние…");
  }
  async function choose(task){
    ensure();if(!purposes[task]||!admin()||changing)return false;
    if(task===selected){root.dataset.mobileView="editor";return true;}
    if(generic?.busy)return false;
    const owner=who();changing=true;paint();
    try{
      if(selected!=="telephone_conversations"&&adapter.dirty?.()){
        const discard=await confirm("Есть несохранённые изменения","Перейти к другому назначению и отменить правки текущего агента?","Перейти без сохранения");
        if(who()!==owner||!discard)return false;
        adapter.discard?.();
      }
      if(who()!==owner)return false;
      selected=task;section="settings";
      if(task!=="telephone_conversations")adapter.selectTask?.(task);
      root.dataset.mobileView="editor";paint();return true;
    }finally{if(who()===owner){changing=false;paint();}}
  }
  function showSection(value){ensure();if(!["settings","check","processing"].includes(value))return;
    if(value==="processing"&&selected!=="telephone_conversations"||value==="check"&&selected!=="telephone_conversations"&&!generic?.editorOpen)return;
    section=value;paint();}
  for(const button of root.querySelectorAll('[data-agent-purpose]'))button.addEventListener('click',()=>void choose(button.dataset.agentPurpose));
  root.querySelector('.agents-purpose-list').addEventListener('keydown',event=>{
    if(!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;event.preventDefault();
    const buttons=[...root.querySelectorAll('[data-agent-purpose]')],index=buttons.findIndex(b=>b===event.target),next=event.key==='Home'?buttons[0]:event.key==='End'?buttons.at(-1):buttons[(index+(event.key==='ArrowUp'?buttons.length-1:1))%buttons.length];
    void choose(next.dataset.agentPurpose).then(ok=>{if(ok)next.focus();});
  });
  for(const button of root.querySelectorAll('[data-agent-section]'))button.addEventListener('click',()=>showSection(button.dataset.agentSection));
  root.querySelector('.agents-section-tabs').addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();
    const buttons=[...root.querySelectorAll('[data-agent-section]')].filter(b=>!b.hidden&&!b.disabled),i=buttons.indexOf(event.target),next=event.key==='Home'?buttons[0]:event.key==='End'?buttons.at(-1):buttons[(i+(event.key==='ArrowLeft'?buttons.length-1:1))%buttons.length];
    if(next){showSection(next.dataset.agentSection);next.focus();}
  });
  $("#agents-back").addEventListener('click',()=>{root.dataset.mobileView="list";root.querySelector(`[data-agent-purpose="${selected}"]`).focus();});
  window.addEventListener('beforeunload',event=>{if(admin()&&(adapter.dirty?.()||phone?.dirty||processing?.dirty)){event.preventDefault();event.returnValue="";}});
  reset();return {bind:value=>{adapter=value;},purpose:()=>selected,currentSection:()=>section,section:showSection,choose,reset,
    savedTask:task=>{if(purposes[task]){selected=task;section="settings";paint();}},
    generic:value=>{ensure();generic=value;paint();},phone:value=>{ensure();phone=value;paint();},processing:value=>{ensure();processing=value.value?value:null;paint();}};
};
