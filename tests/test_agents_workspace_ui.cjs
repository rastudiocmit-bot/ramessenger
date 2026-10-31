"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),copy=v=>JSON.parse(JSON.stringify(v));
async function setup(t,options={}){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',url:'https://cabinet.example.test'}),w=dom.window,$=s=>w.document.querySelector(s);
 w.HTMLElement.prototype.scrollIntoView=()=>{};
 const state={user:{id:'user'},workspace:{id:'one',role:options.role||'owner'},pane:'connections'},calls=[],confirmations=[];let allow=true,held=null,failSettings=Boolean(options.failSettings);
 const settings={configured:true,deepseek:{configured:true},voice_enabled:true,voice_max_seconds:180,vault_ready:true,status:'active',month_spent_rub:12,monthly_limit_rub:500,
  gateways:{recognition_provider:'yandex',synthesis_provider:'disabled',synthesis_voices:[],mimo:{configured:false},mimo_plan:{configured:false}},
  tasks:[{id:'voice_input',title:'Голосовой ввод'},{id:'tasks',title:'Задачи из сообщений'},{id:'text_processing',title:'Обработка текста'}],
  default_model:'model-one',default_prompt:'Исправь текст',task_prompts:{tasks:'Сформулируй задачу'},models:[{id:'model-one',title:'Модель',provider:'yandex',provider_title:'Yandex'}]};
 let agents=[{id:'voice',name:'Чистовик',task:'voice_input',task_title:'Голосовой ввод',model:'model-one',model_title:'Модель',prompt:'Исправь текст',temperature:.2,enabled:true,is_default:true,version:1},
 {id:'tasks',name:'Задачи',task:'tasks',task_title:'Задачи из сообщений',model:'model-one',model_title:'Модель',prompt:'Сформулируй задачу',temperature:.1,enabled:true,is_default:true,version:3}];
 let phone={name:'Телефонные разговоры',version:2,model:'model-one',recognition_provider:'yandex',temperature:.1,prompt:'Только подтверждённые факты звонка',history:[],models:settings.models,recognizers:[{id:'yandex',title:'Yandex',configured:true}]};
 let processing={version:1,auto_new:false,counts:{complete:5,queued:2,needs_review:1},archive:{enabled:false,version:1,imported:7,next_at:null}};
 const api=async(url,opt={})=>{
  calls.push({url,...copy(opt)});
  if(held&&held.matches(url,opt))await held.promise;
  if(url.endsWith('/integrations/novofon/agent/test'))return {summary:'Проверенный текст',version:phone.version,cost_rub:.01};
  if(url.endsWith('/integrations/novofon/agent')){if(opt.method==='PUT')phone={...phone,...copy(opt.data),version:phone.version+1};return copy(phone);}
  if(url.endsWith('/integrations/novofon/processing')){if(opt.method==='PUT')processing={...processing,...copy(opt.data),version:processing.version+1};return copy(processing);}
  if(url.endsWith('/integrations/novofon/archive')){processing.archive={...processing.archive,enabled:opt.data.enabled,version:processing.archive.version+1,waiting_for_night:true,next_at:'2026-10-31T21:00:00Z'};return copy(processing);}
  if(url.endsWith('/settings')){if(failSettings){failSettings=false;throw Error('Нет связи с сервисом');}return copy(settings);}
  if(url.endsWith('/agents/test'))return {text:'<img src=x onerror=alert(1)>',cost_rub:.01};
  if(url.endsWith('/agents')){if(opt.method==='POST'){const value={...copy(opt.data),id:'new',version:1,model_title:'Модель'};agents.push(value);return copy(value);}return {agents:copy(agents)};}
  if(/\/agents\/[^/]+$/.test(url)){const id=url.split('/').at(-1),i=agents.findIndex(a=>a.id===id);if(opt.method==='DELETE'){agents.splice(i,1);return {};}
   if(opt.method==='PUT'){agents[i]={...agents[i],...copy(opt.data),version:agents[i].version+1};return copy(agents[i]);}}
  throw Error('Unexpected '+url);
 };
 for(const file of ['agents-workspace.js','telephony-processing.js','telephony-agent.js','ai.js'])w.eval(fs.readFileSync(web+'/'+file,'utf8'));
 const app=w.CenterAi({$,state,api,node:(tag,text,cls)=>{const e=w.document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e;},
  show:(e,on=true)=>e.hidden=!on,notice:(text,e)=>{e.textContent=text||'';e.hidden=!text;},toast:()=>{},confirm:async(...args)=>{confirmations.push(args);return allow;}});
 const flush=async()=>{for(let i=0;i<50;i++)await Promise.resolve();};
 t.after(()=>w.close());await app.load();await flush();
 return {w,$,state,calls,confirmations,app,flush,setAllow:v=>allow=v,choose:async task=>{$(`[data-agent-purpose="${task}"]`).click();await flush();},section:async value=>{$(`[data-agent-section="${value}"]`).click();await flush();},
  input:(selector,value)=>{const e=$(selector);if(e.type==='checkbox')e.checked=value;else e.value=value;e.dispatchEvent(new w.Event('input',{bubbles:true}));},
  hold:(matches)=>{let release;const promise=new Promise(r=>release=r);held={matches,promise};return ()=>{held=null;release();};},updateProcessing:value=>processing={...processing,...value},updateSettings:value=>Object.assign(settings,value)};
}

test('the initial workspace selects the assigned voice agent and opening it starts no paid work',async t=>{
 const s=await setup(t);assert.equal(s.$('#ai-agent-form').elements.name.value,'Чистовик');
 assert.equal(s.$('[data-agent-purpose="voice_input"]').getAttribute('aria-pressed'),'true');
 assert.equal(s.$('#agents-phone-workarea').hidden,true);assert.match(s.$('#agents-budget-value').textContent,/12/);
 assert.equal(s.calls.filter(c=>c.method).length,0);assert.equal(s.$('#ai-agent-form [type=submit]').disabled,true);
});

test('each purpose lists its own agents and a new variant keeps that purpose',async t=>{
 const s=await setup(t);await s.choose('tasks');assert.equal(s.$('#ai-agent-form').elements.name.value,'Задачи');
 assert.equal(s.$('#ai-agent-list').children.length,1);s.$('#ai-agent-new').click();await s.flush();
 assert.equal(s.$('#ai-agent-form').elements.task.value,'tasks');assert.equal(s.$('#ai-agent-form').elements.prompt.value,'Сформулируй задачу');
 assert.equal(s.$('#ai-agent-form').elements.is_default.checked,false);assert.equal(s.calls.filter(c=>c.method).length,0);
});

test('cancelling a purpose switch preserves unsaved generic text and approval discards it explicitly',async t=>{
 const s=await setup(t);s.input('#ai-agent-form [name=prompt]','Несохранённые правки');s.setAllow(false);await s.choose('telephone_conversations');
 assert.equal(s.$('#agents-phone-workarea').hidden,true);assert.equal(s.$('#ai-agent-form').elements.prompt.value,'Несохранённые правки');
 assert.equal(s.confirmations.length,1);s.setAllow(true);await s.choose('telephone_conversations');await s.choose('voice_input');
 assert.equal(s.$('#ai-agent-form').elements.prompt.value,'Исправь текст');assert.equal(s.calls.filter(c=>c.method).length,0);
});

test('the separate phone draft survives navigation and cancel restores its saved version',async t=>{
 const s=await setup(t);await s.choose('telephone_conversations');s.input('#telephony-agent-form [name=prompt]','Правки только телефона');
 await s.choose('tasks');await s.choose('telephone_conversations');assert.equal(s.$('#telephony-agent-form').elements.prompt.value,'Правки только телефона');
 await s.section('check');assert.equal(s.$('#telephony-agent-test').disabled,true);assert.match(s.$('#telephony-agent-test-version').textContent,/Сначала сохраните/);
 await s.section('settings');s.$('#telephony-agent-cancel').click();assert.equal(s.$('#telephony-agent-form').elements.prompt.value,'Только подтверждённые факты звонка');
 assert.equal(s.calls.filter(c=>c.method).length,0);
});

test('generic save blocks duplicate submits and keeps the saved agent open',async t=>{
 const s=await setup(t);s.input('#ai-agent-form [name=prompt]','Новая инструкция');
 const release=s.hold((url,opt)=>url.endsWith('/agents/voice')&&opt.method==='PUT');const form=s.$('#ai-agent-form');
 for(let i=0;i<2;i++)form.dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await s.flush();
 assert.equal(s.calls.filter(c=>c.method==='PUT').length,1);assert.equal(form.elements.prompt.disabled,true);assert.equal(s.$('[data-agent-purpose=tasks]').disabled,true);
 release();await s.flush();assert.equal(form.hidden,false);assert.equal(form.elements.prompt.value,'Новая инструкция');
 assert.match(s.$('#ai-agent-draft-status').textContent,/Сохранена версия 2/);assert.equal(form.querySelector('[type=submit]').disabled,true);
});

test('explicit generic testing uses the visible draft and renders the answer as plain text',async t=>{
 const s=await setup(t);s.input('#ai-agent-form [name=prompt]','Тестовая инструкция');await s.section('check');
 s.input('#ai-agent-sample','Текст примера');s.$('#ai-agent-test').click();await s.flush();
 const call=s.calls.find(c=>c.url.endsWith('/agents/test'));assert.equal(call.data.prompt,'Тестовая инструкция');
 assert.equal(s.$('#ai-agent-result img'),null);assert.match(s.$('#ai-agent-result').textContent,/<img/);
 assert.match(s.$('#ai-agent-test-version').textContent,/несохранённые/);assert.equal(s.calls.filter(c=>c.method==='PUT').length,0);
});

test('phone testing submits the saved phone version through its independent endpoint',async t=>{
 const s=await setup(t);await s.choose('telephone_conversations');await s.section('check');s.input('#telephony-agent-sample','Разговор');
 s.$('#telephony-agent-test').click();await s.flush();const call=s.calls.find(c=>c.url.endsWith('/novofon/agent/test'));
 assert.equal(call.data.expected_version,2);assert.equal(call.data.text,'Разговор');
 assert.equal(s.calls.filter(c=>c.url.endsWith('/agents/test')).length,0);assert.match(s.$('#telephony-agent-result').textContent,/Проверенный/);
});

test('archive start is explicit and never saves an unsaved new-call preference',async t=>{
 const s=await setup(t);await s.choose('telephone_conversations');await s.section('processing');
 assert.equal(s.calls.filter(c=>c.method).length,0);s.input('#telephony-processing-form [name=auto_new]',true);
 s.$('#telephony-archive-start').click();await s.flush();const writes=s.calls.filter(c=>c.method);
 assert.equal(writes.length,1);assert.ok(writes[0].url.endsWith('/archive'));assert.equal(writes[0].data.expected_version,1);
 assert.equal(s.$('#telephony-processing-form').elements.auto_new.checked,true);assert.equal(s.$('#telephony-archive-pause').disabled,false);
 assert.match(s.$('#telephony-processing-metrics').textContent,/Готово5/);assert.match(s.$('#telephony-archive-next').textContent,/00:00/);
});

test('a late save cannot refresh or overwrite the next workspace editor',async t=>{
 const s=await setup(t);s.input('#ai-agent-form [name=prompt]','Старый ответ организации');
 const release=s.hold((url,opt)=>url.includes('/workspaces/one/')&&opt.method==='PUT');
 s.$('#ai-agent-form').dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await s.flush();
 s.state.workspace.id='two';await s.app.load();const before=s.calls.length;release();await s.flush();
 assert.equal(s.$('#ai-agent-form').elements.prompt.value,'Исправь текст');assert.equal(s.calls.length,before);
});

test('a non-admin cannot load, select or start agents and processing',async t=>{
 const s=await setup(t,{role:'observer'});await s.choose('telephone_conversations');assert.equal(s.calls.length,0);
 assert.equal(s.$('[data-agent-purpose="telephone_conversations"]').disabled,true);assert.equal(s.$('#ai-agent-new').disabled,true);
});
test('reloading common settings preserves the selected phone processing section',async t=>{
 const s=await setup(t);await s.choose('telephone_conversations');await s.section('processing');await s.app.load();await s.flush();
 assert.equal(s.$('[data-agent-purpose=telephone_conversations]').getAttribute('aria-pressed'),'true');
 assert.equal(s.$('[data-agent-section=processing]').getAttribute('aria-pressed'),'true');assert.equal(s.$('#telephony-processing-card').hidden,false);
 assert.equal(s.calls.filter(c=>c.method).length,0);
});
test('saved settings without a model key show an actionable warning instead of a working status',async t=>{
 const s=await setup(t);s.updateSettings({configured:false});await s.app.load();await s.flush();
 assert.match(s.$('#agents-current-status').textContent,/Подключите модель/);assert.match(s.$('[data-agent-purpose=voice_input] .agents-purpose-state').textContent,/нужен ключ/);
 await s.choose('telephone_conversations');assert.match(s.$('#agents-current-status').textContent,/Подключите модель описания/);
});
test('saving an additional variant does not claim it is assigned to the task',async t=>{
 const s=await setup(t);await s.choose('tasks');s.$('#ai-agent-new').click();await s.flush();s.input('#ai-agent-form [name=name]','Эксперимент');
 s.$('#ai-agent-form').dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await s.flush();
 assert.equal(s.calls.find(c=>c.method==='POST').data.is_default,false);assert.match(s.$('#agents-current-status').textContent,/Дополнительный вариант/);
 assert.equal(s.$('[data-agent-purpose=tasks] .agents-purpose-state').textContent,'Задачи');
});
test('a failed initial load has an explicit recovery action that does not start paid work',async t=>{
 const s=await setup(t,{failSettings:true});assert.match(s.$('#ai-agents-error').textContent,/Нет связи/);assert.equal(s.$('#ai-agent-new').disabled,true);
 s.$('#agents-reload').click();await s.flush();assert.equal(s.$('#ai-agents-error').textContent,'');assert.equal(s.$('#ai-agent-form').elements.name.value,'Чистовик');
 assert.equal(s.calls.filter(c=>c.method).length,0);
});
