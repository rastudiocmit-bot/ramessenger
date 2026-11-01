"use strict";
window.CenterPhoneCare = ({ $, state, api, renderPane, openTask, confirm=async()=>true }) => {
  const root = $("#phone-care-root"); if (!root) return { enter() {}, leave() {}, reset() {}, openCase() {} };
  const filters = $("#phone-care-filters"), content = $("#phone-care-content"), error = $("#phone-care-error"), message = $("#phone-care-message");
  let board = null, selected = null, detail = null, tab = "queue", generation = 0, boardSeq = 0, detailSeq = 0, reportSeq = 0;
  const historyLoads = new Set();
  let scope = null, timer = null, busy = false, dirty = false, rows = [], listScope = null, casePanel = null;
  const views = [["queue","Обращения"],["campaigns","Обзвон"],["analytics","Результаты"],["library","Примеры разговоров"],["digest","Сводка руководителя"]];
  const node = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
  const button = (text, action, cls="btn sec") => { const b=node("button",text,cls); b.type="button"; b.addEventListener("click",()=>{const ctx=capture();try{const result=action();result?.catch?.(e=>{if(current(ctx))note(e.message,error);});}catch(e){if(current(ctx))note(e.message,error);}}); return b; };
  const scopeKey = () => `${state.user?.id}:${state.workspace?.id}:${state.epoch}`;
  const capture = () => ({ generation, scope: scopeKey() });
  const current = ctx => ctx.generation===generation && ctx.scope===scopeKey();
  const active = () => state.pane==="telephony" && Boolean(state.workspace && state.user);
  const endpoint = () => `/workspaces/${state.workspace.id}/phone-care`;
  const note = (text, target=message) => { target.textContent=text || ""; target.hidden=!text; };
  const zone = () => state.workspace?.timezone || "Europe/Moscow";
  function date(value) { if (!value) return "Не указан"; return new Intl.DateTimeFormat("ru-RU",{dateStyle:"short",timeStyle:"short",timeZone:zone()}).format(new Date(value)); }
  function localInput(value) {
    const parts=Object.fromEntries(new Intl.DateTimeFormat("en-CA",{timeZone:zone(),year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(new Date(value)).map(p=>[p.type,p.value]));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  }
  function instant(value) {
    if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(value)) throw Error("Укажите дату и время.");
    const target=Date.parse(value+"Z"); let guess=target;
    for(let i=0;i<4;i++) { const wall=Date.parse(localInput(guess)+"Z"); if(wall===target) return new Date(guess).toISOString(); guess+=target-wall; }
    throw Error("Это время недоступно в часовом поясе центра. Выберите другое время.");
  }
  const section = title => { const c=node("section",undefined,"care-card"); if(title)c.append(node("h3",title)); return c; };
  const field = (label, input, hint) => { const e=node("label",label,"fld"); e.append(input); if(hint)e.append(node("small",hint)); return e; };
  function input(name, type="text", value="", max=200) { const e=node("input"); e.name=name;e.type=type;e.value=value??"";e.maxLength=max; return e; }
  function select(name, options, value="") { const e=node("select");e.name=name;for(const [key,label] of options)e.append(new Option(label,key));e.value=value??"";return e; }
  function textarea(name, value="", max=1000) { const e=node("textarea");e.name=name;e.rows=3;e.maxLength=max;e.value=value;return e; }
  function memberSelect(value, nullable=false, task=false) {
    const members=board?.manager?board.members.filter(p=>!task||["owner","admin"].includes(p.role)||p.sections?.includes("tasks")):[{id:state.user.id,name:state.user.display_name||state.user.email||"Я"}];
    const options=members.map(p=>[p.id,[p.name,p.email].filter(Boolean).join(" · ")]);
    if(nullable) options.unshift(["","Не назначен"]);
    if(value && !task && !options.some(x=>x[0]===value))options.push([value,"Сотрудник отключён — выберите другого"]);
    const chosen=options.some(x=>x[0]===value)?value:(nullable?"":options.find(x=>x[0]===state.user.id)?.[0]||options[0]?.[0]||"");
    const result=select("assignee_id",options,chosen);result.required=!nullable;return result;
  }
  function controls() {
    root.setAttribute("aria-busy",String(busy)); $("#phone-care-refresh").disabled=busy;
    root.querySelectorAll("button,input,select,textarea").forEach(e=>{e.disabled=busy||e.dataset.forbidden==="true"||Boolean(e.closest('[data-care-readonly="true"]'))||(e.hasAttribute("data-care-mutation")&&Boolean(board?.read_only));});
  }
  async function action(fn) {
    if(busy || !state.workspace)return;const ctx=capture();busy=true;controls();note("",error);note("");
    try{await fn(ctx);}catch(e){if(current(ctx))note(e.message||"Не удалось выполнить действие.",error);}
    finally{if(current(ctx)){busy=false;controls();}}
  }
  function pausePlayers() { root.querySelectorAll("audio").forEach(a=>{a.pause();a.removeAttribute("src");a.load();}); }
  function reset() {
    generation++; boardSeq++; detailSeq++;reportSeq++;historyLoads.clear();clearTimeout(timer);timer=null;pausePlayers();board=null;selected=null;detail=null;rows=[];listScope=null;busy=false;dirty=false;casePanel=null;scope=scopeKey();tab="queue";
    content.replaceChildren();$("#phone-care-tabs").replaceChildren();$("#phone-care-view-select").replaceChildren();$("#phone-care-stats").replaceChildren();$("#phone-care-coverage").textContent="";filters.reset();note("",error);note("");controls();
  }
  function leave() { clearTimeout(timer);timer=null;pausePlayers(); }
  function schedule() { clearTimeout(timer);if(active())timer=setTimeout(()=>void loadBoard().finally(schedule),30000); }
  async function enter() { if(scope!==scopeKey())reset();await loadBoard();schedule(); }
  function options(selectElement, pairs, first) {
    const old=selectElement.value;selectElement.replaceChildren(new Option(first,""),...pairs.map(([id,label])=>new Option(label,id)));
    if([...selectElement.options].some(o=>o.value===old))selectElement.value=old;
  }
  function paintTabs() {
    const allowed=views.filter(([key])=>board.manager||!["library","digest"].includes(key));
    if(!allowed.some(v=>v[0]===tab))tab="queue";
    $("#phone-care-tabs").replaceChildren(...allowed.map(([key,label])=>{const b=button(label,()=>switchTab(key),"care-tab");b.setAttribute("role","tab");b.setAttribute("aria-selected",String(tab===key));b.tabIndex=tab===key?0:-1;
      b.addEventListener("keydown",e=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(e.key))return;e.preventDefault();let i=allowed.findIndex(x=>x[0]===key);i=e.key==="Home"?0:e.key==="End"?allowed.length-1:(i+(e.key==="ArrowRight"?1:-1)+allowed.length)%allowed.length;void switchTab(allowed[i][0]).then(()=>$("#phone-care-tabs").children[i]?.focus());});return b;}));
    $("#phone-care-view-select").replaceChildren(...allowed.map(([key,label])=>new Option(label,key)));$("#phone-care-view-select").value=tab;
  }
  function params(offset=0) {
    const data={offset};for(const name of ["status","campaign","interest","branch","source","reason"])if(filters.elements[name].value)data[name]=filters.elements[name].value;
    if(["analytics","digest","library"].includes(tab))delete data.status;
    const days=Number(filters.elements.period.value);if(days)data.since=new Date(Date.now()-days*86400000).toISOString();return data;
  }
  async function loadBoard(more=false) {
    if(!active())return;const ctx=capture(),seq=++boardSeq;
    try{
      const value=await api(endpoint()+"/board",{method:"POST",timeoutMs:12000,data:params(more?board.next_offset:0)});
      if(!current(ctx)||seq!==boardSeq||!active())return;
      if(more&&listScope!==value.scope){note("Список изменился. Показана актуальная первая страница.");return loadBoard(false);}
      board=value;rows=more?rows.concat(value.cases.filter(x=>!rows.some(r=>r.id===x.id))):value.cases;listScope=value.scope;note("",error);
      paintTabs();options(filters.elements.reason,Object.entries(value.labels.kinds),"Все поводы");options(filters.elements.interest,Object.entries(value.labels.courses),"Все направления");
      options(filters.elements.branch,(value.branches||[]).map(x=>[x.id,x.name]),"Все филиалы");options(filters.elements.source,(value.filter_sources||[]).map(x=>[x,x]),"Все источники");
      options(filters.elements.campaign,value.campaigns.map(x=>[x.id,x.name]),"Все очереди");
      const m=value.metrics;$("#phone-care-stats").replaceChildren(...[["Нужно довести до результата",m.open],["Срок обращения истёк",m.overdue],["Связались после пропущенного",m.recovered_missed]].map(([label,count])=>{const c=node("div",undefined,"care-stat");c.append(node("strong",String(count)),node("span",label));return c;}));
      $("#phone-care-coverage").textContent=[value.coverage_note,value.report_limited?"Отчёт ограничен первыми 5000 обращений; уточните период.":"",value.progress.complete?`Первичная обработка завершена. Последний проход: ${date(value.progress.last_scan_at)}.`:"Идёт первичная обработка уже загруженных звонков."].filter(Boolean).join(" ");
      filters.hidden=["library","digest"].includes(tab);
      filters.elements.status.closest("label").hidden=tab==="analytics";
      if(["queue","campaigns"].includes(tab))paintQueue();else if(tab==="analytics")paintAnalytics();else if(tab==="library")await paintLibrary();else await paintDigest();
      controls();
    }catch(e){if(current(ctx)&&seq===boardSeq)note(e.message||"Не удалось получить обращения.",error);}
  }
  async function switchTab(next) {
    if(busy)return;const ctx=capture();if(dirty&&!await confirm("Открыть другой раздел?","Несохранённые изменения обращения будут очищены.","Продолжить"))return;if(!current(ctx))return;dirty=false;leave();boardSeq++;detailSeq++;reportSeq++;tab=next;pausePlayers();content.replaceChildren();casePanel=null;
    await loadBoard();schedule();
  }
  function paintQueue() {
    if(!casePanel||!content.contains(casePanel)){
      pausePlayers();content.replaceChildren();
      if(tab==="campaigns"&&board.manager)content.append(campaignForms());
      const grid=node("div",undefined,"phone-care-grid"),left=section("Обращения семьи");left.classList.add("care-list-wrap");left.id="phone-care-list";
      casePanel=section();casePanel.id="phone-care-detail";casePanel.addEventListener("input",()=>{dirty=true;});casePanel.append(node("p","Выберите семью, чтобы посмотреть звонки, предложения и следующий шаг.","form-note"));
      grid.append(left,casePanel);content.append(grid);
      if(selected&&detail)paintDetail();
    }
    const left=$("#phone-care-list");left.replaceChildren(node("h3",tab==="campaigns"?"Семьи в очереди":"Обращения семьи"));
    if(!rows.length)left.append(node("p","По выбранным фильтрам обращений нет.","form-note"));
    for(const row of rows){const b=button("",()=>openCase(row.id),"care-case-row");b.setAttribute("aria-pressed",String(row.id===selected));b.append(node("strong",row.label||row.phone||"Скрытый номер"),node("span",board.labels.statuses[row.status]||row.status),node("small",`${row.pending} предложений · срок: ${date(row.due_at)}`));if(row.reasons?.length)b.append(node("small",row.reasons.map(x=>board.labels.kinds[x]||x).join(" · ")));left.append(b);}
    if(board.has_more)left.append(button("Показать ещё обращения",()=>loadBoard(true)));
    if(selected)content.querySelector(".phone-care-grid").classList.add("has-case");
    const settings=content.querySelector('[data-care-settings]');
    if(settings)settings.textContent=board.settings.enabled?"Приостановить фоновый разбор":"Включить фоновый разбор";
    if(detail&&rows.some(r=>r.id===selected&&r.version!==detail.version))note("Обращение изменилось. Обновите его перед сохранением; введённые поля оставлены в форме.");
  }
  async function openCase(id) {
    if(!active())return;const ctx=capture();if(dirty&&!await confirm("Открыть обращение?","Несохранённые изменения будут очищены.","Продолжить"))return;if(!current(ctx))return;pausePlayers();const seq=++detailSeq;dirty=false;
    try{const value=await api(endpoint()+`/cases/${id}`,{timeoutMs:12000});if(!current(ctx)||seq!==detailSeq||!active())return;detail=value;selected=value.id;
      if(!["queue","campaigns"].includes(tab)){tab="queue";content.replaceChildren();casePanel=null;paintTabs();paintQueue();}
      else paintDetail();paintQueue();
    }catch(e){if(current(ctx)&&seq===detailSeq)note(e.message,error);}
  }
  function mutationButton(label, fn, allowed=true, cls="btn sec") { const b=button(label,fn,cls);b.dataset.careMutation="";b.dataset.forbidden=String(!allowed);b.disabled=busy||!allowed||board.read_only;return b; }
  function paintDetail() {
    if(!detail||!casePanel)return;pausePlayers();casePanel.replaceChildren();
    casePanel.append(button("← К списку",async()=>{const ctx=capture();if(dirty&&!await confirm("Вернуться к списку?","Несохранённые изменения будут очищены.","Продолжить"))return;if(!current(ctx))return;detailSeq++;selected=null;detail=null;dirty=false;pausePlayers();content.querySelector(".phone-care-grid")?.classList.remove("has-case");casePanel.replaceChildren();},"text-button care-back"));
    const head=node("div",undefined,"care-detail-head");head.append(node("h3",detail.display_name));head.append(button("Обновить обращение",()=>openCase(selected),"text-button"));casePanel.append(head);
    casePanel.append(node("p",`${detail.phone||"Номер скрыт"} · ${board.labels.statuses[detail.status]||detail.status}`,"form-note"));
    casePanel.append(node("p","Предложения требуют проверки смысла цитаты и роли говорящего. Назначение задачи не подтверждает запись в CRM или отправку сообщения.","form-note"));
    const proposals=section("Предложения и договорённости");
    if(!detail.signals.length)proposals.append(node("p","Подтверждённых договорённостей пока нет. Запись может ещё ожидать расшифровку.","form-note"));
    for(const signal of detail.signals){const e=node("article",undefined,"care-evidence");e.append(node("strong",signal.title));
      if(signal.quote)e.append(node("blockquote",signal.quote));
      e.append(node("small",`${date(signal.at)}${signal.channel?` · канал ${signal.channel}, роль не подтверждена`:""}${signal.offset_seconds?` · фрагмент от ${Math.floor(signal.offset_seconds/60)}:${String(signal.offset_seconds%60).padStart(2,"0")}`:""}`));
      const actions=node("div",undefined,"btn-row");
      if(signal.call_id)actions.append(button("Открыть звонок",()=>showCall(signal.call_id,{run_id:signal.run_id,offset:signal.offset_seconds}),"text-button"));
      if(signal.task_id)actions.append(button("Открыть задачу",()=>openTask(signal.task_id),"text-button"));
      else if(signal.state==="proposed")actions.append(mutationButton("Подготовить задачу",()=>discardAnd(()=>taskForm(signal,e)),detail.can_create_task),mutationButton("Не требуется",()=>discardAnd(()=>dismiss(signal)),detail.can_edit,"text-button"));
      else e.append(node("p",({contacted:"После пропущенного состоялся разговор",reviewed:"Рассмотрено",dismissed:"Сотрудник отметил: действие не требуется",historical:"Архивный фрагмент. Актуальность следующего действия не подтверждена."})[signal.state]||"Предложение рассмотрено","form-note"));
      if(["historical","reviewed","dismissed"].includes(signal.state)&&!signal.task_id)actions.append(mutationButton("Подтвердить актуальность",()=>reactivate(signal),detail.can_edit,"text-button"));
      if(board.manager&&signal.quote&&signal.run_id)actions.append(mutationButton("В примеры для обучения",()=>discardAnd(()=>trainingForm(signal,e)),true,"text-button"));
      e.append(actions);proposals.append(e);
    }
    casePanel.append(proposals,caseForm(),crmCard());
    if(detail.linked_tasks?.length){const linked=section("Связанные задачи");for(const task of detail.linked_tasks)linked.append(button(`${task.title} · ${{open:"открыта",done:"выполнена",cancelled:"отменена"}[task.status]||task.status}`,()=>openTask(task.id),"text-button"));casePanel.append(linked);}
    const draft=section("Черновик ответа родителю"),text=textarea("reply","Здравствуйте! Возвращаюсь к вашему вопросу о занятиях. [Добавьте проверенный ответ и согласованный следующий шаг.]",2000);
    draft.append(field("Проверьте и дополните перед использованием",text),button("Скопировать черновик",async()=>{try{await navigator.clipboard.writeText(text.value);note("Черновик скопирован. Сообщение не отправлялось.");}catch(_){text.focus();text.select();note("Выделен текст черновика — скопируйте его вручную.");}}));casePanel.append(draft);
    const history=section("Звонки и результаты контактов");history.id="phone-care-history";casePanel.append(history);void loadHistory();
    if(detail.events?.length){const events=section("Отметки сотрудников");for(const item of [...detail.events].reverse())events.append(node("p",`${date(item.at)} · ${item.note||board.labels.statuses[item.status]||"Изменение обращения"}`));casePanel.append(events);}
    dirty=false;controls();
  }
  function caseForm() {
    const wrap=section("Ответственный, срок и подтверждённый результат"),form=node("form",undefined,"care-form"),value=detail;
    const status=select("status",Object.entries(board.labels.statuses),value.status);for(const option of status.options)if(option.value==="observed")option.disabled=true;
    const assignee=memberSelect(value.assignee_id,true),due=input("due_at","datetime-local",value.due_at?localInput(value.due_at):"");
    const stage=select("stage",Object.entries(board.labels.stages),value.stage),journey=select("journey",[["unknown","Не уточнён"],["new","Первое обращение"],["existing","Действующая семья"],["return","Возвращение"]],value.journey);
    const branch=select("branch",[["","Не уточнён"],...(board.branches||[]).map(x=>[x.id,x.name])],value.branch||"");
    const source=input("source","text",value.source||"",100),label=input("label","text",value.label||"",200),notes=textarea("note");
    form.append(field("Состояние",status),field("Ответственный",assignee),field(`Срок · ${zone()}`,due),field("Этап после проверки CRM",stage),field("Тип пути семьи",journey),field("Филиал",branch),field("Источник обращения",source),field("Метка семьи",label),field("Результат или следующий шаг",notes,"Для завершения обращения или изменения этапа пояснение обязательно."));
    const save=node("button","Подтвердить изменения","btn primary");save.type="submit";save.dataset.careMutation="";save.dataset.forbidden=String(!value.can_edit);form.append(save);
    form.addEventListener("submit",e=>{e.preventDefault();if(!value.can_edit)return;void action(async ctx=>{
      const data={case_id:value.id,expected_version:value.version,journey:journey.value,branch:branch.value,source:source.value,label:label.value,note:notes.value};
      if(status.value!==value.status)data.status=status.value;
      if(stage.value!==value.stage)data.stage=stage.value;
      if(assignee.value!==(value.assignee_id||""))data.assignee_id=assignee.value||null;
      if((due.value||"")!==(value.due_at?localInput(value.due_at):""))data.due_at=due.value?instant(due.value):null;
      await api(endpoint()+`/cases/${value.id}`,{method:"PATCH",timeoutMs:10000,data});if(current(ctx)){dirty=false;await openCase(value.id);await loadBoard();note("Изменения подтверждены.");}
    });});
    form.dataset.careReadonly=String(!value.can_edit);if(!value.can_edit)form.querySelectorAll("input,textarea,select,button").forEach(e=>{e.disabled=true;});wrap.append(form);return wrap;
  }
  async function discardAnd(fn) {
    const ctx=capture();if(dirty&&!await confirm("Продолжить?","Несохранённые изменения формы останутся до обновления обращения. После подтверждения действия обращение обновится.","Продолжить"))return;if(!current(ctx))return;return fn();
  }
  function crmCard() {
    const c=section("Данные для следующего разговора");
    if(!detail.crm?.refreshed_at)c.append(node("p","Актуального опубликованного справочника CRM нет. Проверьте карточку перед ответом.","form-note"));
    else c.append(node("p",`Снимок CRM: ${date(detail.crm.refreshed_at)}`,"form-note"));
    for(const contact of detail.crm?.contacts||[])c.append(node("p",contact.display_name));
    for(const group of detail.crm?.groups||[])c.append(node("p",`${group.name} · филиал ${board.branches?.find(b=>b.id===String(group.branch_id))?.name||group.branch_id}`));
    c.append(node("p",detail.crm?.note||"Нет подтверждённых данных о подходящих группах.","form-note"));return c;
  }
  function taskForm(signal, parent) {
    parent.querySelector(".care-task-form")?.remove();const value=detail,form=node("form",undefined,"care-task-form care-form");
    const title=input("title","text",signal.title),assignee=memberSelect(value.assignee_id||state.user.id,false,true),due=input("due_at","datetime-local",localInput(signal.suggested_due_at||new Date(Date.now()+2*3600000))),details=textarea("details",signal.quote||"",2000);title.required=true;due.required=true;
    form.append(node("p","Проверьте текст, ответственного и срок. Задача появится только после подтверждения.","form-note"),field("Задача",title),field("Ответственный",assignee),field(`Срок · ${zone()}`,due),field("Пояснение",details));
    const save=node("button","Подтвердить и создать задачу","btn primary");save.type="submit";save.dataset.careMutation="";form.append(save,button("Отмена",()=>form.remove(),"text-button"));
    form.addEventListener("submit",e=>{e.preventDefault();if(!form.reportValidity())return;void action(async ctx=>{await api(endpoint()+"/tasks",{method:"POST",timeoutMs:10000,data:{case_id:value.id,expected_version:value.version,signal_id:signal.id,title:title.value,details:details.value,assignee_id:assignee.value,due_at:instant(due.value)}});if(current(ctx)){dirty=false;await openCase(value.id);await loadBoard();note("Задача создана. Она доступна в разделе «Задачи».");}});});parent.append(form);title.focus();controls();
  }
  async function dismiss(signal) {const value=detail;await action(async ctx=>{await api(endpoint()+`/cases/${value.id}`,{method:"PATCH",timeoutMs:10000,data:{case_id:value.id,expected_version:value.version,dismiss_signal:signal.id,note:"Предложение проверено: действие не требуется."}});if(current(ctx)){dirty=false;await openCase(value.id);await loadBoard();}});}
  async function reactivate(signal) {const value=detail,guard=capture();if(!await confirm("Считать действие актуальным?","Архивный фрагмент будет возвращён в работу. Проверьте, что вопрос семьи ещё не решён.","Подтвердить"))return;if(!current(guard)||detail?.id!==value.id)return;await action(async ctx=>{await api(endpoint()+`/cases/${value.id}`,{method:"PATCH",timeoutMs:10000,data:{case_id:value.id,expected_version:value.version,reactivate_signal:signal.id,note:"Сотрудник проверил: действие по архивному фрагменту актуально."}});if(current(ctx)){dirty=false;await openCase(value.id);await loadBoard();}});}
  function trainingForm(signal,parent) {
    parent.querySelector(".care-training-form")?.remove();const value=detail,form=node("form",undefined,"care-training-form care-form"),request_id=crypto.randomUUID();
    const title=input("title","text",signal.title,160),category=select("category",[["makeup","Правила отработки"],["course","Программа"],["price","Стоимость"],["conflict","Претензия"],["agreement","Договорённость"]]),good=textarea("good"),improve=textarea("improve");title.required=true;
    form.append(field("Название примера",title),field("Тема",category),field("Что получилось",good),field("Что улучшить",improve));const save=node("button","Сохранить пример","btn primary");save.type="submit";save.dataset.careMutation="";form.append(save,button("Отмена",()=>form.remove(),"text-button"));
    form.addEventListener("submit",e=>{e.preventDefault();void action(async ctx=>{await api(endpoint()+"/library",{method:"POST",timeoutMs:10000,data:{case_id:value.id,signal_id:signal.id,request_id,title:title.value,category:category.value,good:good.value,improve:improve.value}});if(current(ctx)){form.remove();note("Пример сохранён с цитатой и ссылкой на исходный звонок.");}});});parent.append(form);controls();
  }
  async function loadHistory(next=null) {
    if(!detail)return;const id=detail.id,ctx=capture(),seq=detailSeq,box=$("#phone-care-history");if(!box)return;
    const key=`${generation}:${seq}:${id}:${JSON.stringify(next)}`;if(historyLoads.has(key))return;historyLoads.add(key);
    try{const query=next?"?"+new URLSearchParams(next):"",value=await api(endpoint()+`/cases/${id}/calls`+query,{timeoutMs:10000});if(!current(ctx)||seq!==detailSeq||detail?.id!==id||!box.isConnected)return;
      if(!next)box.replaceChildren(node("h3","Звонки и результаты контактов"));box.querySelector(".care-history-more")?.remove();
      for(const call of value.calls){const row=node("div",undefined,"care-call-row");row.append(node("strong",`${call.direction==="in"?"Входящий":"Исходящий"} звонок`),node("small",`${date(call.started_at)} · ${call.missed?"Пропущен":`${Math.floor((call.talk_seconds||0)/60)}:${String((call.talk_seconds||0)%60).padStart(2,"0")}`}`),button("Открыть запись и текст",()=>showCall(call.id),"text-button"));box.append(row);}
      if(!value.calls.length&&!next)box.append(node("p","Зарегистрированных звонков пока нет. Добавление семьи в обзвон не создаёт телефонный вызов.","form-note"));
      if(value.next)box.append(button("Ещё звонки",()=>loadHistory(value.next),"btn sec care-history-more"));
    }catch(e){if(current(ctx)&&seq===detailSeq)note(e.message,error);}finally{historyLoads.delete(key);}
  }
  async function showCall(callId,reference={}) {
    if(!detail||!active())return;const id=detail.id,ctx=capture(),seq=detailSeq;
    const previous=casePanel.querySelector(".care-call-reader");previous?.querySelectorAll("audio").forEach(a=>{a.pause();a.removeAttribute("src");a.load();});previous?.remove();
    const holder=section("Запись и полная расшифровка");holder.classList.add("care-call-reader","call-recording");casePanel.append(holder);
    const audio=node("audio");audio.controls=true;audio.preload="none";audio.setAttribute("aria-label","Аудиозапись звонка");audio.className="care-audio";
    const valid=()=>current(ctx)&&seq===detailSeq&&active()&&holder.isConnected;
    const release=()=>{audio.pause();audio.removeAttribute("src");audio.load();};
    const listen=button("Прослушать запись",async()=>{try{if(!valid())return;audio.src="/api"+endpoint()+`/cases/${id}/calls/${callId}/recording`;audio.muted=false;audio.volume=1;await state.prepareCallAudio?.(audio);if(!valid()){release();return;}await audio.play();if(!valid()){release();return;}if(reference.offset)audio.currentTime=reference.offset;}catch(_){if(valid())note("Запись пока недоступна. Проверьте звук и повторите загрузку.",error);}});
    holder.append(listen,audio);const text=node("pre",undefined,"care-transcript");holder.append(text);
    let run=reference.run_id||null,reading=false,started=false;
    const full=button("Показать полный текст",()=>read(),"btn sec");
    async function read(offset=0){if(reading||(offset===0&&started))return;reading=true;try{const params=new URLSearchParams({offset:String(offset)});if(run)params.set("run_id",run);const value=await api(endpoint()+`/cases/${id}/calls/${callId}/transcript?`+params,{timeoutMs:10000});if(!valid())return;run=value.run_id;holder.querySelector(".care-transcript-more")?.remove();text.textContent+=value.text||"Расшифровка ещё не готова. Настройки обработки доступны в «AI-агенты → Телефонные разговоры».";started=true;full.hidden=true;if(value.next_offset!==null)holder.append(button("Загрузить ещё текст",()=>read(value.next_offset),"btn sec care-transcript-more"));}catch(e){if(valid())note(e.message,error);}finally{reading=false;}}
    holder.append(full);holder.scrollIntoView?.({block:"nearest"});
  }
  function paintAnalytics() {
    pausePlayers();content.replaceChildren();casePanel=null;
    const m=board.metrics,grid=node("div",undefined,"care-report-grid");
    for(const [title,values,labels,filter] of [["Поводы для следующего действия",m.reasons,board.labels.kinds,"reason"],["Семьи с упоминаниями направлений",m.demand,board.labels.courses,"interest"],["Подтверждённые этапы новых обращений",m.confirmed_stages,board.labels.stages,null]]){
      const box=section(title),max=Math.max(1,...Object.values(values));for(const [key,count] of Object.entries(values)){const row=button("",async()=>{if(!filter)return;filters.elements[filter].value=key;tab="queue";content.replaceChildren();casePanel=null;await loadBoard();},"care-metric-row");row.disabled=!filter;row.dataset.forbidden=String(!filter);row.append(node("span",labels[key]||key),node("strong",String(count)));const bar=node("i");bar.style.setProperty("--care-bar",`${100*count/max}%`);row.append(bar);box.append(row);}grid.append(box);
    }
    content.append(grid,node("p",`Первое обращение подтверждено для ${m.new_journey_count} семей. Тип пути не уточнён: ${m.unclassified}. Этапы могут пропускаться: это подтверждённые факты, а не оценка эффективности сотрудников по длительности звонка.`,"form-note"));
  }
  async function paintLibrary(offset=0) {
    const ctx=capture(),seq=boardSeq,request=++reportSeq,valid=()=>current(ctx)&&active()&&seq===boardSeq&&request===reportSeq&&tab==="library";
    try{const value=await api(endpoint()+`/library?offset=${offset}`,{timeoutMs:10000});if(!valid())return;if(!offset){pausePlayers();content.replaceChildren();casePanel=null;}content.querySelector(".care-library-more")?.remove();
      if(!value.examples.length&&!offset)content.append(node("p","Примеров пока нет. Откройте цитату в обращении и сохраните её для обучения.","form-note"));
      for(const item of value.examples){const c=section(item.title);c.append(node("blockquote",item.evidence.quote),node("p",`Что получилось: ${item.good||"Не указано"}`),node("p",`Что улучшить: ${item.improve||"Не указано"}`),button("К исходному обращению",()=>openCase(item.case_id)));content.append(c);}if(value.next_offset!==null)content.append(button("Ещё примеры",()=>paintLibrary(value.next_offset),"btn sec care-library-more"));
    }catch(e){if(valid())note(e.message,error);}
  }
  async function paintDigest() {
    const ctx=capture(),seq=boardSeq,request=++reportSeq,valid=()=>current(ctx)&&active()&&seq===boardSeq&&request===reportSeq&&tab==="digest";
    try{const value=await api(endpoint()+"/digest",{timeoutMs:10000});if(!valid())return;pausePlayers();content.replaceChildren();casePanel=null;
      const c=section(value.ready?`Сводка за ${value.date}`:"Сводка готовится");c.append(node("p",value.note,"form-note"));if(value.ready){c.append(node("p",`Нужно довести до результата: ${value.metrics.open}. Срок истёк: ${value.metrics.overdue}. После пропущенного связались: ${value.metrics.recovered_missed}.`));for(const item of value.items)c.append(button(`${item.label||item.phone||"Скрытый номер"} · ${board.labels.statuses[item.status]} · ${date(item.due_at)}`,()=>openCase(item.id),"care-case-row"));c.append(node("p",`Подготовлено ${date(value.generated_at)}${value.report_limited?". Отчёт ограничен объёмом данных.":""}`,"form-note"));}content.append(c);
    }catch(e){if(valid())note(e.message,error);}
  }
  function campaignForms() {
    const wrap=section("Очереди обзвона"),form=node("form",undefined,"care-form");let request_id=crypto.randomUUID();
    const name=input("name","text","",100),kind=select("kind",Object.entries(board.labels.campaigns));name.required=true;form.append(field("Название очереди",name),field("Повод",kind));const create=node("button","Создать очередь","btn sec");create.type="submit";create.dataset.careMutation="";form.append(create);
    form.addEventListener("submit",e=>{e.preventDefault();void action(async ctx=>{const result=await api(endpoint()+"/campaigns",{method:"POST",timeoutMs:10000,data:{request_id,name:name.value,kind:kind.value}});if(current(ctx)){request_id=crypto.randomUUID();form.reset();filters.elements.campaign.append(new Option(result.name,result.id));filters.elements.campaign.value=result.id;await loadBoard();note("Очередь создана. Добавьте семью из CRM.");}});});wrap.append(form);
    const search=node("form",undefined,"care-search"),query=input("query","search","",100);query.required=true;query.minLength=3;query.placeholder="Фамилия или телефон из CRM";const submit=node("button","Найти семью","btn sec");submit.type="submit";search.append(field("Добавить семью",query),submit);const results=node("div");wrap.append(search,results);
    search.addEventListener("submit",e=>{e.preventDefault();void action(async ctx=>{const found=await api(endpoint()+"/contacts/search",{method:"POST",timeoutMs:10000,data:{query:query.value}});if(!current(ctx))return;results.replaceChildren();for(const contact of found.contacts)for(const phone of contact.phones){const b=button(`${contact.display_name} · ${phone}`,()=>addContactForm(contact,phone,results),"care-case-row");results.append(b);}if(!found.contacts.length)results.append(node("p","Контакты не найдены в опубликованном справочнике CRM.","form-note"));});});
    const settings=button(board.settings.enabled?"Приостановить фоновый разбор":"Включить фоновый разбор",()=>action(async ctx=>{await api(endpoint()+"/settings",{method:"PUT",timeoutMs:10000,data:{expected_version:board.settings.version,enabled:!board.settings.enabled}});if(current(ctx))await loadBoard();}),"text-button");settings.dataset.careMutation="";settings.dataset.careSettings="";wrap.append(settings);return wrap;
  }
  function addContactForm(contact,phone,parent) {
    parent.querySelector(".care-add-contact")?.remove();const form=node("form",undefined,"care-add-contact care-form"),campaign=select("campaign_id",board.campaigns.map(x=>[x.id,x.name]),filters.elements.campaign.value||board.campaigns[0]?.id),assignee=memberSelect(state.user.id),due=input("due_at","datetime-local",localInput(new Date(Date.now()+2*3600000)));campaign.required=true;due.required=true;
    form.append(node("p",`${contact.display_name} · ${phone}`),field("Очередь",campaign),field("Ответственный",assignee),field(`Срок · ${zone()}`,due));const save=node("button","Подтвердить добавление","btn primary");save.type="submit";save.dataset.careMutation="";form.append(save,button("Отмена",()=>form.remove(),"text-button"));
    form.addEventListener("submit",e=>{e.preventDefault();void action(async ctx=>{const result=await api(endpoint()+"/campaigns/members",{method:"POST",timeoutMs:10000,data:{campaign_id:campaign.value,contact_id:contact.contact_id,phone,assignee_id:assignee.value,due_at:instant(due.value),expected_version:contact.cases?.[phone]?.version??null}});if(current(ctx)){contact.cases||={};contact.cases[phone]={id:result.id,version:result.version};form.remove();filters.elements.campaign.value=campaign.value;await loadBoard();await openCase(result.id);note(result.already?"Семья уже в этой очереди. Срок и ответственный остались прежними.":"Семья добавлена. Телефонный вызов не инициировался.");}});});parent.append(form);controls();
  }
  $("#phone-care-refresh").addEventListener("click",()=>void loadBoard());
  $("#phone-care-view-select").addEventListener("change",e=>void switchTab(e.target.value));
  filters.addEventListener("submit",e=>e.preventDefault());filters.addEventListener("change",()=>{rows=[];listScope=null;void loadBoard();});
  $("#phone-care-connection").addEventListener("click",async()=>{state.connectionsView="novofon";await renderPane("connections");$("#sip-lines-card")?.scrollIntoView({block:"start"});});
  reset();return { enter,leave,reset,openCase };
};
