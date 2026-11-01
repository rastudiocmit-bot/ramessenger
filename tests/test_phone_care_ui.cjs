"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),{JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),flush=()=>new Promise(r=>setImmediate(r));
const id='00000000-0000-4000-8000-000000000001',user='00000000-0000-4000-8000-000000000002',call='00000000-0000-4000-8000-000000000003',signal='a'.repeat(32);
function detail(){return {id,version:1,display_name:'Синтетическая семья',phone:'+79990000000',label:'Синтетическая семья',status:'open',stage:'contact',journey:'unknown',can_edit:true,can_create_task:true,assignee_id:null,due_at:null,campaigns:[],events:[],linked_tasks:[],crm:{contacts:[],groups:[],refreshed_at:null},signals:[{id:signal,kind:'callback',title:'Проверить обещание перезвонить',call_id:call,run_id:null,quote:'Я уточню и перезвоню завтра.',at:'2026-11-01T09:00:00Z',state:'proposed',suggested_due_at:'2026-11-02T13:00:00+03:00'}]};}
function board(){return {cases:[{id,version:1,label:'Синтетическая семья',phone:'+79990000000',status:'open',pending:1,reasons:['callback'],due_at:null}],next_offset:null,has_more:false,scope:'v1',manager:true,read_only:false,settings:{version:0,enabled:true},progress:{complete:true,last_scan_at:'2026-11-01T09:00:00Z'},metrics:{open:1,overdue:0,recovered_missed:0,reasons:{callback:1},demand:{},confirmed_stages:{contact:0},new_journey_count:0,unclassified:1},members:[{id:user,name:'Сотрудник',role:'owner',sections:['messages','tasks']}],campaigns:[],branches:[],filter_sources:[],labels:{kinds:{callback:'Перезвонить'},courses:{robotics:'Робототехника'},statuses:{open:'Нужно действие',working:'В работе',resolved:'Доведено до результата',observed:'Без вопросов'},stages:{contact:'Обращение',trial_booked:'Запись на пробное'},campaigns:{trial:'После пробного'}},coverage_note:'По известным обращениям'};}
function setup(t,handler,confirm=async()=>true){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{url:'https://cabinet.example.test',runScripts:'outside-only'}),w=dom.window,$=s=>w.document.querySelector(s),calls=[],media=[];
 w.HTMLMediaElement.prototype.pause=function(){media.push(['pause',this]);};w.HTMLMediaElement.prototype.load=function(){};w.HTMLMediaElement.prototype.play=async function(){media.push(['play',this]);};
 const state={user:{id:user,display_name:'Сотрудник'},workspace:{id:'w1',timezone:'Europe/Moscow'},epoch:1,pane:'telephony'};
 const api=async(url,opt={})=>{calls.push({url,...opt});if(handler){const r=handler(url,opt);if(r!==undefined)return r;}if(url.endsWith('/board'))return board();if(url.endsWith('/cases/'+id))return detail();if(url.includes('/transcript'))return {run_id:null,text:'Полная синтетическая расшифровка',next_offset:null};if(url.endsWith('/calls'))return {calls:[],next:null};return {};};
 w.eval(fs.readFileSync(web+'/phone-care.js','utf8'));const ui=w.CenterPhoneCare({$,state,api,confirm,renderPane:async()=>{},openTask:async()=>{}});t.after(()=>{ui.reset();w.close();});
 return{w,$,state,ui,calls,media};
}
const clickText=(u,text,root=u.w.document)=>{const b=[...root.querySelectorAll('button')].find(x=>x.textContent===text);assert.ok(b,'Missing '+text);b.click();return b;};
test('family queue reads only local API and does not fetch recordings or full transcripts on open',async t=>{
 const u=setup(t);await u.ui.enter();await u.ui.openCase(id);await flush();assert.match(u.$('#phone-care-detail').textContent,/Синтетическая семья/);assert.equal(u.calls.some(c=>/recording|transcript/.test(c.url)),false);assert.ok(u.calls.every(c=>c.url.startsWith('/workspaces/w1/phone-care')));
 clickText(u,'Открыть звонок');await flush();assert.equal(u.$('audio').hasAttribute('src'),false);clickText(u,'Показать полный текст');await flush();assert.match(u.$('.care-transcript').textContent,/Полная синтетическая/);
});
test('task requires confirmation and uses the workspace timezone, with duplicate submissions fenced',async t=>{
 let done;const u=setup(t,(url,opt)=>url.endsWith('/tasks')?new Promise(r=>done=r):undefined);await u.ui.enter();await u.ui.openCase(id);clickText(u,'Подготовить задачу');await flush();
 assert.equal(u.calls.some(c=>c.url.endsWith('/tasks')),false);const form=u.$('.care-task-form');form.elements.due_at.value='2026-11-02T13:00';
 for(let i=0;i<2;i++)form.dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();const writes=u.calls.filter(c=>c.url.endsWith('/tasks'));assert.equal(writes.length,1);assert.equal(writes[0].data.due_at,'2026-11-02T10:00:00.000Z');assert.equal(writes[0].data.expected_version,1);assert.equal(u.$('.care-case-row').disabled,true);done({task:{id:'synthetic'},version:2});await flush();await flush();
});
test('background refresh preserves unsaved case fields and their original version',async t=>{
 let version=1;const u=setup(t,(url,opt)=>{if(url.endsWith('/board')){const b=board();b.cases[0].version=version;return b;}if(opt.method==='PATCH')throw Error('Обращение изменилось.');},async()=>false);
 await u.ui.enter();await u.ui.openCase(id);const form=u.$('#phone-care-detail .care-form');form.elements.note.value='Несохранённый результат';form.elements.note.dispatchEvent(new u.w.Event('input',{bubbles:true}));version=2;u.$('#phone-care-refresh').click();await flush();
 assert.equal(form.elements.note.value,'Несохранённый результат');assert.match(u.$('#phone-care-message').textContent,/изменилось/);await u.ui.openCase('other');assert.equal(form.elements.note.value,'Несохранённый результат');
 form.dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();assert.equal(u.calls.find(c=>c.method==='PATCH').data.expected_version,1);assert.equal(form.elements.note.value,'Несохранённый результат');
});
test('delayed old workspace detail cannot reveal data in the next workspace',async t=>{
 let done;const u=setup(t,url=>url.endsWith('/cases/'+id)?new Promise(r=>done=r):undefined);await u.ui.enter();const old=u.ui.openCase(id);u.state.workspace={id:'w2',timezone:'Europe/Moscow'};u.state.epoch++;u.ui.reset();await u.ui.enter();done({...detail(),display_name:'OLD PRIVATE FAMILY'});await old;assert.doesNotMatch(u.$('#phone-care-root').textContent,/OLD PRIVATE FAMILY/);
});
test('observer can inspect evidence but cannot confirm a task or change the case',async t=>{
 const u=setup(t,url=>{if(url.endsWith('/board'))return {...board(),manager:false,read_only:true,members:[]};if(url.endsWith('/cases/'+id))return {...detail(),can_edit:false};});await u.ui.enter();await u.ui.openCase(id);assert.equal([...u.$('#phone-care-tabs').children].some(x=>x.textContent==='Сводка руководителя'),false);assert.ok([...u.$('#phone-care-detail').querySelectorAll('[data-care-mutation]')].every(e=>e.disabled));
});
test('names and quotes are rendered as text, not HTML',async t=>{
 const u=setup(t,url=>url.endsWith('/cases/'+id)?{...detail(),display_name:'<img src=x onerror=bad()>',signals:[{...detail().signals[0],quote:'<script>bad()</script>'}]}:undefined);await u.ui.enter();await u.ui.openCase(id);assert.equal(u.$('#phone-care-detail img'),null);assert.equal(u.$('#phone-care-detail script'),null);assert.match(u.$('#phone-care-detail').textContent,/<script>/);
});
test('recording has the API prefix and an old player is stopped before another call opens',async t=>{
 const u=setup(t);await u.ui.enter();await u.ui.openCase(id);clickText(u,'Открыть звонок');clickText(u,'Прослушать запись');await flush();const old=u.$('audio');assert.match(old.getAttribute('src'),/^\/api\/workspaces\/w1\/phone-care/);assert.ok(u.media.some(x=>x[0]==='play'&&x[1]===old));clickText(u,'Открыть звонок');assert.equal(old.hasAttribute('src'),false);assert.ok(u.media.some(x=>x[0]==='pause'&&x[1]===old));
});
test('delayed output-device preparation cannot play a previous case or workspace',async t=>{
 let prepared;const u=setup(t);u.state.prepareCallAudio=()=>new Promise(r=>prepared=r);await u.ui.enter();await u.ui.openCase(id);clickText(u,'Открыть звонок');clickText(u,'Прослушать запись');await flush();u.state.workspace={id:'w2',timezone:'Europe/Moscow'};u.state.epoch++;u.ui.reset();prepared();await flush();assert.equal(u.media.some(x=>x[0]==='play'),false);
});
test('full transcript loads once per page even after repeated clicks',async t=>{
 let done;const u=setup(t,url=>url.includes('/transcript')?new Promise(r=>done=r):undefined);await u.ui.enter();await u.ui.openCase(id);clickText(u,'Открыть звонок');const b=clickText(u,'Показать полный текст');b.click();await flush();assert.equal(u.calls.filter(c=>c.url.includes('/transcript')).length,1);done({run_id:null,text:'Единственный текст',next_offset:null});await flush();b.click();await flush();assert.equal(u.$('.care-transcript').textContent,'Единственный текст');assert.equal(u.calls.filter(c=>c.url.includes('/transcript')).length,1);
});
test('unavailable worker remains an error, not an empty successful queue',async t=>{
 const u=setup(t,()=>{throw Error('Контейнер телефонии ещё не обновлён.');});await u.ui.enter();assert.match(u.$('#phone-care-error').textContent,/не обновлён/);assert.equal(u.$('#phone-care-stats').textContent,'');
});
