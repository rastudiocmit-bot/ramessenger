"use strict";
// A bell counts open questions, not polling attempts. Routine progress stays in
// its own screen. Only task reminders and opted-in service issues may pop up.
window.CenterNotices = ({ $, state = {}, api = null, readServices = null, onServices = () => {} }) => {
  const defaults = {sound_enabled:false,volume:55,client:"soft",lead:"clear",archive:"low",service_popups:false,task_reminders:true,task_sound:false};
  let preferences = {...defaults}, items = [], shown = null, timer = null, seq = 0;
  let context = null, epoch = null, generation = 0, ready = false, loading = null, saving = false, pending = {}, saveTimer = null;
  let serviceTimer = null, serviceBusy = false, serviceAgain = false, serviceBlocked = false;
  const icon = name => window.CenterIcon ? window.CenterIcon(name) : document.createElement("span");
  const button = (label, cls, handler) => { const b = document.createElement("button"); b.type="button"; b.className=cls; b.textContent=label; b.addEventListener("click", handler); return b; };
  const el = (tag, text, cls) => { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e; };
  const admin = () => ["owner","admin"].includes(state.workspace?.role);
  const tasksAllowed = () => admin() || ["messages","tasks"].every(section=>(state.workspace?.sections||[]).includes(section));
  const active = item => !item.resolved && (item.category!=="service" || admin()) && (!item.reminder || preferences.task_reminders);
  const root = el("div", undefined, "notices"); root.setAttribute("aria-live","polite");
  root.innerHTML='<div class="notice-capsule" role="status" hidden><span class="notice-ic"></span><div class="notice-body"><strong class="notice-title"></strong><span class="notice-text"></span></div><button type="button" class="notice-del" aria-label="Скрыть уведомление"></button><div class="notice-acts" hidden></div></div>';
  document.body.append(root);
  const capsule=root.firstElementChild; capsule.querySelector(".notice-del").append(icon("close"));
  const trigger=button("","notification-trigger iconbtn plain",()=>open()); trigger.id="notification-bell"; trigger.setAttribute("aria-label","Уведомления"); trigger.setAttribute("aria-haspopup","dialog");
  trigger.append(icon("bell"),el("span","","notification-badge")); trigger.lastChild.hidden=true;
  const center=el("dialog",undefined,"live-sheet notification-center"); center.id="notification-center"; center.setAttribute("aria-labelledby","notification-center-title");
  const head=el("div",undefined,"notification-center-head"); head.append(el("h2","Уведомления")); head.firstChild.id="notification-center-title";
  const close=button("","iconbtn plain",()=>center.close()); close.setAttribute("aria-label","Закрыть уведомления"); close.append(icon("close")); head.append(close);
  const list=el("div",undefined,"notification-center-list");
  const settings=button("Настройки уведомлений","text-button",()=>{center.close();openSettings();});
  center.append(head,el("p","Здесь только вопросы, требующие решения. Ход загрузки — в мастере синхронизации.","notification-center-note"),list,settings); document.body.append(center);
  center.addEventListener("click",event=>{if(event.target===center)center.close();});
  const settingsDialog=$("#notification-settings-dialog"), status=$("#notification-preferences-status"), retry=$("#notification-preferences-retry");
  const fields={sound_enabled:"chat-sound-enabled",volume:"chat-volume",client:"chat-tone-client",lead:"chat-tone-lead",archive:"chat-tone-archive",service_popups:"notices-enabled",task_reminders:"notification-task-reminders",task_sound:"notification-task-sound"};
  const statusText = (text,error=false) => { if(status){status.textContent=text;status.dataset.error=String(error);} if(retry)retry.hidden=!error;const profile=$("#profile-sound-settings .val");if(profile)profile.textContent=error?"Не сохранено":text.startsWith("Сохраня")?"Сохраняем":ready?"Настроить":"Загрузка"; };
  const current = (mine,key) => generation===mine && context===key;
  function dispatchPreferences(){document.dispatchEvent(new CustomEvent("cm:notification-preferences",{detail:{...preferences,epoch:state.epoch}}));}
  function hydrate(){
    for(const [key,id] of Object.entries(fields)){const field=$("#"+id);if(!field)continue;if(field.type==="checkbox")field.checked=Boolean(preferences[key]);else field.value=String(preferences[key]);}
    dispatchPreferences(); render();if(!$("#notification-service-example")?.hidden)renderExample();
  }
  function place(){
    const pane=state.pane||"messages",phoneChat=innerWidth<720&&$("#chat-layout")?.classList.contains("has-selection");
    const host=pane==="messages" ? $(phoneChat?"#chat-header":"#pane-messages .title-row") : $("#pane-"+pane+" .page-head, #pane-"+pane+" .list-top");
    if(host && trigger.parentElement!==host)host.append(trigger);
    trigger.hidden=!state.user || !state.workspace;
  }
  function render(){
    if(shown&&(!items.includes(shown)||!active(shown)))collapse();
    place();
    const serviceSettings=$("#notices-enabled")?.closest("fieldset");if(serviceSettings)serviceSettings.hidden=!admin();
    const count=items.filter(active).length;
    trigger.lastChild.textContent=count>99?"99+":String(count);trigger.lastChild.hidden=!count;
    trigger.setAttribute("aria-label",count?`Уведомления: нерешённых вопросов ${count}`:"Уведомления: нерешённых вопросов нет");
    if(center.open)renderList();
  }
  function renderList(){
    const focused=document.activeElement, focusRow=focused?.closest(".notification-row"), focusKey=focusRow?.dataset.key, focusIndex=focused?.dataset.notificationAction;
    list.replaceChildren();
    const openItems=items.filter(active), recent=items.filter(item=>item.resolved&&(item.category!=="service"||admin())).slice(0,8);
    list.append(el("h3",openItems.length?`Требует внимания · ${openItems.length}`:"Всё спокойно"));
    if(!openItems.length)list.append(el("p","Сейчас от вас не требуется никаких действий.","notification-empty"));
    for(const item of openItems)list.append(row(item));
    if(recent.length){list.append(el("h3","Недавние"));for(const item of recent)list.append(row(item));}
    if(focusKey && focusIndex){const target=[...list.querySelectorAll('.notification-row')].find(row=>row.dataset.key===focusKey);target?.querySelector(`[data-notification-action="${focusIndex}"]`)?.focus({preventScroll:true});}
  }
  function row(item){
    const box=el("article",undefined,"notification-row"+(item.acknowledged?" acknowledged":""));box.dataset.key=item.key;
    box.append(el("strong",item.title));
    if(item.text)box.append(el("p",item.text));
    box.append(el("small",item.resolved ? (item.resolution||"Вопрос решён") : item.acknowledged?"Просмотрено · вопрос ещё не решён":"Требуется действие"));
    const acts=el("div",undefined,"notification-row-actions");
    for(const [index,action] of (item.actions||[]).entries()){const b=button(action.label,"text-button",()=>run(item,action));b.dataset.notificationAction=String(index);acts.append(b);}
    if(!item.resolved){const b=button(item.acknowledged?"Просмотрено":"Понятно","text-button notification-ack",()=>acknowledge(item));b.dataset.notificationAction="ack";acts.append(b);}
    box.append(acts);return box;
  }
  function open(){collapse();renderList();if(!center.open)center.showModal();}
  function collapse(){clearTimeout(timer);timer=null;shown=null;capsule.hidden=true;}
  function acknowledge(item){item.acknowledged=true;if(shown===item)collapse();render();}
  async function run(item,action){
    acknowledge(item);if(center.open)center.close();
    try{await action.run?.();}catch(_){/* The action owns its inline error; never claim it resolved. */}
    // Opening a destination or a rejected request cannot resolve the cause.
  }
  function showPopup(item,{preview=false}={}){
    if(!preview && (item.acknowledged||!active(item)||!ready||(!item.reminder && !preferences.service_popups)))return;
    if(document.querySelector("dialog[open],#chat-voice-panel:not([hidden]),.voice-hold:not([hidden])"))return;
    collapse();shown=item;item.presented=true;
    capsule.querySelector(".notice-title").textContent=item.title;
    capsule.querySelector(".notice-text").textContent=item.text||"";
    capsule.querySelector(".notice-ic").replaceChildren(icon(item.category==="task"?"task":"info"));
    const acts=capsule.querySelector(".notice-acts");acts.replaceChildren();
    for(const action of item.actions||[])acts.append(button(action.label,"notice-act",()=>run(item,action)));
    acts.hidden=!acts.children.length;capsule.classList.toggle("has-acts",Boolean(acts.children.length));
    capsule.hidden=false;timer=setTimeout(collapse,5000);
    if(item.reminder && preferences.task_sound && !preview)document.dispatchEvent(new CustomEvent("cm:notification-task-sound"));
  }
  function notify({type="error",title,text="",key=null,action=null,actions=null,category=null,attention=null,revision=null,quiet=false,always=false,glyph=null}={}){
    if(!title || !state.user || !state.workspace)return false;
    key=key||`${type}|${title}`;
    category ||= key.startsWith("task:")?"task":"service";
    if(category==="service"&&!admin())return false;
    if(category==="task"&&!tasksAllowed())return false;
    const reminder=category==="task"&&/^task:(due|soon|digest|reminder)/.test(key);
    if(reminder&&!preferences.task_reminders)return false;
    const actionable=attention ?? (type!=="success"&&(category==="service"||Boolean(action||(actions||[]).length)));
    if(!actionable&&!action&&!(actions||[]).length)return true;
    const signature=revision||`${type}|${title}`;
    let item=items.find(entry=>entry.key===key);
    const changed=!item||item.signature!==signature||Boolean(item.resolved)!==!actionable;
    if(!item){item={key,id:++seq,at:Date.now(),acknowledged:false};items.unshift(item);}
    if(changed){item.acknowledged=false;item.presented=false;item.at=Date.now();}
    if(quiet)item.presented=true;
    Object.assign(item,{type,title:String(title),text:String(text),category,reminder,signature,resolved:!actionable,actions:actions|| (action?[action]:[])});
    // No repeat counter and no timer restart on an unchanged polling result.
    items=items.filter(entry=>!entry.resolved).concat(items.filter(entry=>entry.resolved).slice(0,12)).slice(0,100);
    render();if(changed&&actionable&&!quiet)showPopup(item);
    return true;
  }
  function resolve(key,resolution="Вопрос решён"){
    const item=items.find(entry=>entry.key===key);if(!item)return;
    if(item.resolved&&item.resolution===resolution)return;
    item.resolved=true;item.resolution=resolution;item.actions=[];if(shown===item)collapse();render();
  }
  function reconcileTasks(tasks,{complete=false}={}){
    const byId=new Map((tasks||[]).map(task=>[String(task.id),task]));
    for(const item of items){
      const match=/^task:reminder:(.+)$/.exec(item.key);if(!match||item.resolved)continue;
      const task=byId.get(match[1]);
      if((!task&&complete)||(task&&Date.parse(task.due_at)>Date.now()+15*60000))resolve(item.key,"Задача выполнена, перенесена или больше не назначена вам");
    }
  }
  async function load(force=false){
    if(!api||!context)return;
    if(loading)return loading;
    if(ready&&!force)return;
    const mine=generation,key=context;
    statusText("Загружаем личные настройки…");
    const task=(async()=>{
      try{
        const result=await api(`/workspaces/${state.workspace.id}/notification-preferences`,{timeoutMs:10000});
        if(!current(mine,key))return;
        preferences={...defaults,...result,...pending};ready=true;hydrate();
        statusText(Object.keys(pending).length?"Есть изменения для сохранения":"Сохранено в аккаунте для этого центра. Действует на ваших устройствах.");
        if(Object.keys(pending).length)scheduleSave();
        const newest=items.find(item=>active(item)&&!item.presented&&!item.acknowledged);if(newest)showPopup(newest);
      }catch(error){if(current(mine,key)&&error.name!=="AbortError")statusText("Не удалось загрузить настройки. Изменения пока не сохранены. Повторите загрузку.",true);}
      finally{if(current(mine,key))loading=null;}
    })();loading=task;return task;
  }
  async function refreshServices(){
    if(!context||!admin()||!readServices||serviceBlocked)return;
    clearTimeout(serviceTimer);serviceTimer=null;
    if(serviceBusy){serviceAgain=true;return;}
    if(document.hidden){serviceTimer=setTimeout(refreshServices,60000);return;}
    const mine=generation,key=context;serviceBusy=true;
    try{const result=await readServices();if(current(mine,key)&&result&&"crm" in result&&"max" in result)onServices(result.crm,result.max);}
    catch(error){if([401,403,404].includes(error.status)){serviceBlocked=true;items=items.filter(item=>item.category!=="service");render();}/* A missing report is not proof of a broken connection. */}
    finally{if(current(mine,key)){serviceBusy=false;serviceTimer=setTimeout(refreshServices,serviceAgain?1000:60000);serviceAgain=false;}}
  }
  function scheduleSave(){clearTimeout(saveTimer);if(ready&&!saving)saveTimer=setTimeout(save,400);}
  async function save(){
    if(!ready||saving||!Object.keys(pending).length)return;
    const mine=generation,key=context,patch={...pending};pending={};saving=true;statusText("Сохраняем…");
    try{
      const result=await api(`/workspaces/${state.workspace.id}/notification-preferences`,{method:"PATCH",data:patch,timeoutMs:10000});
      if(!current(mine,key))return;
      preferences={...defaults,...result,...pending};hydrate();statusText("Настройки сохранены в аккаунте.");
    }catch(error){if(current(mine,key)&&error.name!=="AbortError"){pending={...patch,...pending};statusText("Не удалось сохранить. Ваш выбор оставлен на экране; нажмите «Повторить».",true);}}
    finally{if(current(mine,key)){saving=false;if(Object.keys(pending).length&&status?.dataset.error!=="true")scheduleSave();}}
  }
  function change(key,value){pending[key]=value;preferences[key]=value;statusText(ready?"Сохраняем…":"Загружаем настройки перед сохранением…");dispatchPreferences();render();if(key==='service_popups'&&!$("#notification-service-example")?.hidden)renderExample();if(ready)scheduleSave();else load();}
  for(const [key,id] of Object.entries(fields))$("#"+id)?.addEventListener("input",()=>{
    const field=$("#"+id);change(key,field.type==="checkbox"?field.checked:key==="volume"?Number(field.value):field.value);
    if(key==="task_reminders")document.dispatchEvent(new CustomEvent("cm:task-reminders-change",{detail:{enabled:field.checked,epoch:state.epoch}}));
  });
  retry?.addEventListener("click",()=>ready?save():load(true));
  document.addEventListener("cm:task-reminders-saved",event=>{if(event.detail?.epoch===state.epoch)load(true);});
  async function openSettings(){if(settingsDialog&&!settingsDialog.open)settingsDialog.showModal();await load(true);}
  $("#profile-sound-settings")?.addEventListener("click",openSettings);
  addEventListener("resize",place);
  const appRoot=$("#app");if(appRoot)new MutationObserver(place).observe(appRoot,{attributes:true,attributeFilter:["data-tab","data-view"]});
  $("#notification-task-preview")?.addEventListener("click",()=>document.dispatchEvent(new CustomEvent("cm:notification-task-sound",{detail:{preview:true}})));
  function renderExample(){
    const preview=$("#notification-service-example");if(!preview)return;
    const heading=el("small",preferences.service_popups?"Короткое предупреждение · исчезнет через 5 секунд":"Спокойный режим · вопрос под колокольчиком");
    const card=el("div",undefined,"notification-row");card.append(el("strong","Нужен код подтверждения MAX"),el("p","Сохранённая переписка доступна. Введите код из письма AlfaCRM."));
    const action=button("Ввести код","text-button",()=>{});action.disabled=true;card.append(action);
    preview.replaceChildren(heading,card,el("small","Это только пример. Подключение и счётчик не меняются."));
  }
  $("#notification-service-preview")?.addEventListener("click",()=>{const preview=$("#notification-service-example");preview.hidden=!preview.hidden;renderExample();});
  capsule.querySelector(".notice-del").addEventListener("click",()=>{if(shown)acknowledge(shown);});
  new MutationObserver(()=>{if(shown&&document.querySelector("dialog[open],#chat-voice-panel:not([hidden]),.voice-hold:not([hidden])"))collapse();})
    .observe(document.body,{subtree:true,attributes:true,attributeFilter:["hidden","open"]});
  document.addEventListener("keydown",event=>{if(event.key==="Escape"&&shown)collapse();});
  function reset(){generation++;context=null;epoch=null;ready=false;loading=null;saving=false;pending={};items=[];preferences={...defaults};clearTimeout(saveTimer);clearTimeout(serviceTimer);serviceTimer=null;serviceBusy=false;serviceAgain=false;serviceBlocked=false;collapse();if(center.open)center.close();if(settingsDialog?.open)settingsDialog.close();hydrate();}
  function sync(){
    const next=state.user&&state.workspace?`${state.user.id}:${state.workspace.id}`:null;
    if(next!==context){reset();context=next;epoch=state.epoch;}
    else if(epoch!==state.epoch){generation++;loading=null;saving=false;serviceBusy=false;serviceBlocked=false;clearTimeout(serviceTimer);serviceTimer=null;epoch=state.epoch;}
    items=items.filter(item=>(item.category!=="service"||admin())&&(item.category!=="task"||tasksAllowed()));
    render();if(next)load();if(next&&!serviceTimer&&!serviceBusy)refreshServices();
  }
  render();
  return {notify,resolve,reconcileTasks,open,openSettings,sync,reset,load,refreshServices,get enabled(){return preferences.service_popups;}};
};
