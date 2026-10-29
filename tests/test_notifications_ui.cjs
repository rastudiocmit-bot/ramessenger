'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),tick=()=>new Promise(r=>setImmediate(r));
const defaults={configured:true,sound_enabled:false,volume:55,client:'soft',lead:'clear',archive:'low',service_popups:false,task_reminders:true,task_sound:false};
async function setup(t,handler,options={}){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',url:'https://example.test',pretendToBeVisual:true}),w=dom.window,$=s=>w.document.querySelector(s),calls=[],timers=new Map();let id=0,saved={...defaults};
 w.setTimeout=(fn,ms)=>{timers.set(++id,{fn,ms});return id;};w.clearTimeout=id=>timers.delete(id);
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 w.CenterIcon=()=>w.document.createElement('span');
 const state={user:{id:'one'},workspace:{id:'centre',role:'owner'},pane:'connections',epoch:1};
 const api=async(url,options={})=>{calls.push({url,...options});const r=await handler?.(url,options);if(r!==undefined)return r;if(options.method==='PATCH')Object.assign(saved,options.data);return {...saved};};
 w.eval(fs.readFileSync(web+'/notices.js','utf8'));const app=w.CenterNotices({$,state,api,...options});app.sync();await tick();await tick();
 t.after(()=>w.close());return {w,$,state,app,calls,timers,async run(ms){const jobs=[...timers].filter(([,v])=>v.ms===ms);for(const [id,v] of jobs){timers.delete(id);v.fn();}await tick();await tick();}};
}
test('the bell counts unique open questions; calm mode never creates a service banner',async t=>{
 const s=await setup(t);for(let n=0;n<6;n++)s.app.notify({key:'connection:max',title:'Нужен код',text:'Проверка '+n,action:{label:'Ввести код'}});
 assert.equal(s.$('.notification-badge').textContent,'1');assert.equal(s.$('.notice-capsule').hidden,true);
 s.app.open();assert.equal(s.$('#notification-center').open,true);assert.equal(s.w.document.querySelectorAll('.notification-row').length,1);
 assert.doesNotMatch(s.$('.notification-row').textContent,/раза/);
 s.$('.notification-ack').click();assert.equal(s.$('.notification-badge').textContent,'1');
 assert.match(s.$('.notification-row').textContent,/Просмотрено/);
 s.app.notify({key:'connection:max',title:'Нужен код',action:{label:'Ввести код'}});
 assert.equal(s.$('.notice-capsule').hidden,true);
 s.app.resolve('connection:max','Подключение восстановлено');assert.equal(s.$('.notification-badge').hidden,true);
 assert.match(s.$('.notification-row').textContent,/Подключение восстановлено/);
});
test('opted-in popup occurs once per cause, closes without resolving it, and opening an action is not a false success',async t=>{
 const s=await setup(t);s.$('#notices-enabled').checked=true;s.$('#notices-enabled').dispatchEvent(new s.w.Event('input'));await s.run(400);
 let attempts=0;const issue={key:'connection:max',title:'Войти заново',revision:'login',action:{label:'Войти',run:async()=>{attempts++;throw Error('still offline');}}};
 s.app.notify(issue);assert.equal(s.$('.notice-capsule').hidden,false);const n=s.timers.size;
 s.app.notify(issue);assert.equal(s.timers.size,n,'unchanged polling cannot restart the timer');
 s.$('.notice-del').click();s.app.notify(issue);assert.equal(s.$('.notice-capsule').hidden,true);
 s.app.open();s.$('.notification-row-actions button').click();await tick();assert.equal(attempts,1);assert.equal(s.$('.notification-badge').textContent,'1');
 s.app.notify({...issue,revision:'otp',title:'Нужен код'});assert.equal(s.$('.notice-capsule').hidden,false);
 await s.run(5000);assert.equal(s.$('.notice-capsule').hidden,true);assert.equal(s.$('.notification-badge').textContent,'1');
});
test('only administrators receive service issues; task actions remain available to an operator',async t=>{
 const s=await setup(t);s.state.workspace.role='operator';s.state.workspace.sections=['messages','tasks'];s.app.sync();
 assert.equal(s.app.notify({title:'Проверьте ключ',action:{label:'Проверить'}}),false);
 assert.equal(s.app.notify({key:'task:reminder:one',title:'Перезвонить',actions:[{label:'Открыть'}]}),true);
 assert.equal(s.$('.notification-badge').textContent,'1');
 s.app.notify({key:'task:reminder:one',title:'Пора перезвонить',revision:'due',actions:[{label:'Открыть'}]});
 assert.equal(s.$('.notification-badge').textContent,'1');
 s.app.reconcileTasks([],{complete:false});assert.equal(s.$('.notification-badge').hidden,false);
 s.app.reconcileTasks([],{complete:true});assert.equal(s.$('.notification-badge').hidden,true);
});
test('failed settings read never overwrites unknown account choices, retry preserves edits and saves only changed fields',async t=>{
 let fail=true;const s=await setup(t,async()=>{if(fail)throw Error('offline');});
 s.$('#chat-volume').value='70';s.$('#chat-volume').dispatchEvent(new s.w.Event('input'));await s.run(400);
 assert.equal(s.calls.filter(c=>c.method==='PATCH').length,0);assert.match(s.$('#notification-preferences-status').textContent,/Не удалось загрузить/);
 fail=false;s.$('#notification-preferences-retry').click();await tick();await tick();await s.run(400);
 assert.deepEqual(JSON.parse(JSON.stringify(s.calls.find(c=>c.method==='PATCH').data)),{volume:70});assert.equal(s.$('#chat-volume').value,'70');
 assert.equal(s.$('#notices-enabled').checked,false);
});
test('edits made while saving survive the old response; failed saves retain the choice for explicit retry',async t=>{
 let resolve,fail=false,first=true;
 const s=await setup(t,async(url,o)=>{if(o.method==='PATCH'){if(fail)throw Error('offline');if(first){first=false;return new Promise(r=>resolve=r);}}});
 s.$('#chat-volume').value='60';s.$('#chat-volume').dispatchEvent(new s.w.Event('input'));await s.run(400);
 s.$('#chat-tone-client').value='low';s.$('#chat-tone-client').dispatchEvent(new s.w.Event('input'));
 resolve({...defaults,volume:60});await tick();await tick();assert.equal(s.$('#chat-tone-client').value,'low');await s.run(400);
 fail=true;s.$('#notices-enabled').checked=true;s.$('#notices-enabled').dispatchEvent(new s.w.Event('input'));await s.run(400);
 assert.equal(s.$('#notices-enabled').checked,true);assert.equal(s.$('#notification-preferences-retry').hidden,false);
 fail=false;s.$('#notification-preferences-retry').click();await tick();await tick();assert.match(s.$('#notification-preferences-status').textContent,/сохранены/);
});
test('workspace change clears private items and ignores a delayed preference response from the old account',async t=>{
 let resolve,first=true;const s=await setup(t,async()=>{if(first){first=false;return new Promise(r=>resolve=r);}});
 s.app.notify({key:'connection:private',title:'Private',action:{label:'Open'}});
 s.state.workspace={id:'other',role:'owner'};s.state.epoch++;s.app.sync();await tick();await tick();
 resolve({...defaults,volume:99,service_popups:true});await tick();await tick();
 assert.equal(s.$('.notification-badge').hidden,true);s.app.open();assert.doesNotMatch(s.$('#notification-center').textContent,/Private/);
 assert.equal(s.$('#chat-volume').value,'55');assert.equal(s.$('#notices-enabled').checked,false);
});
test('contained examples never create incidents or change account settings',async t=>{
 const s=await setup(t),before=s.calls.length;
 s.$('#notification-service-preview').click();assert.match(s.$('#notification-service-example').textContent,/только пример/);
 assert.equal(s.$('.notification-badge').hidden,true);assert.equal(s.$('.notice-capsule').hidden,true);assert.equal(s.calls.length,before);
});
test('a known unresolved task is restored quietly without losing the badge',async t=>{
 const s=await setup(t);s.app.notify({key:'task:reminder:old',title:'Перезвонить',quiet:true,actions:[{label:'Открыть'}]});
 assert.equal(s.$('.notification-badge').textContent,'1');assert.equal(s.$('.notice-capsule').hidden,true);
 await s.app.load(true);assert.equal(s.$('.notice-capsule').hidden,true);
 s.app.resolve('task:reminder:old','Выполнена');assert.equal(s.$('.notification-badge').hidden,true);
});
test('a failed service status read cannot invent recovery; access loss clears administrator issues',async t=>{
 let deny=false,broken=false,reads=0;const reports=[];
 const s=await setup(t,null,{readServices:async()=>{reads++;if(deny)throw Object.assign(Error('denied'),{status:403});if(broken)return {};return {crm:null,max:null};},onServices:(...args)=>reports.push(args)});
 assert.equal(reports.length,1);s.app.notify({key:'connection:max',title:'Нужен код',action:{label:'Ввести'}});
 broken=true;await s.run(60000);assert.equal(reports.length,1);assert.equal(s.$('.notification-badge').textContent,'1');
 deny=true;await s.run(60000);assert.equal(s.$('.notification-badge').hidden,true);const before=reads;
 await s.run(60000);assert.equal(reads,before);
});
test('recording suppresses a banner without discarding its question; losing task access clears task titles',async t=>{
 const s=await setup(t);s.$('#notices-enabled').checked=true;s.$('#notices-enabled').dispatchEvent(new s.w.Event('input'));await s.run(400);
 s.$('#chat-voice-panel').hidden=false;s.app.notify({key:'connection:max',title:'Нужен код',action:{label:'Ввести'}});
 assert.equal(s.$('.notice-capsule').hidden,true);assert.equal(s.$('.notification-badge').textContent,'1');
 s.app.notify({key:'task:reminder:private',title:'Private task',actions:[{label:'Открыть'}]});
 s.state.workspace.role='observer';s.state.workspace.sections=['security'];s.app.sync();s.app.open();
 assert.doesNotMatch(s.$('#notification-center').textContent,/Private task|Нужен код/);
});
