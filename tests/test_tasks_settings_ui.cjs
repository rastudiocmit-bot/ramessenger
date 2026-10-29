'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),tick=()=>new Promise(r=>setImmediate(r));
async function setup(t,role='owner',handler){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',url:'https://example.test',pretendToBeVisual:true}),w=dom.window,$=s=>w.document.querySelector(s),calls=[];
 w.setInterval=()=>1;w.setTimeout=()=>1;$('#cabinet').hidden=false;$('#app').hidden=false;$('#pane-tasks').hidden=false;
 const state={user:{id:'one'},workspace:{id:'first',role,sections:['messages','tasks']},pane:'tasks',epoch:1};
 const ctx={admin:role==='owner',personal:{press:'split',voice_task:'instant',reminders:true,hints:true,digest:false},studio:{day_start:'10:00',day_end:'20:00',work_days:[1,2,3,4,5],default_due:'end_of_day',visibility:'all',alfa_copy:false}};
 const api=async(url,opts={})=>{calls.push({url,...opts});const r=await handler?.(url,opts);if(r!==undefined)return r;if(url.endsWith('/tasks/options'))return structuredClone(ctx);if(opts.method==='PUT')Object.assign(url.endsWith('/preferences')?ctx.personal:ctx.studio,opts.data);return {tasks:[],upcoming:[],conversations:{}};};
 w.eval(fs.readFileSync(web+'/tasks.js','utf8'));const app=w.CenterTasks({$,state,api,node:(tag,text,cls)=>{const e=w.document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;},show:(e,v)=>e.hidden=!v});
 t.after(()=>w.close());
 const flush=async()=>{await tick();await tick();};
 return {w,$,state,app,calls,ctx,flush,async open(){ $('#tasks-settings-tab').click();await flush();},async save(){$('#task-settings-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await flush();}};
}
test('task settings are a page tab with saved values and keyboard navigation, without opening a modal',async t=>{
 const s=await setup(t);await s.open();assert.equal(s.$('#task-settings-dialog'),null);assert.equal(s.$('#task-settings-panel').hidden,false);
 assert.equal(s.$('#task-settings-form').elements.day_start.value,'10:00');assert.equal(s.$('[name=reminders]').checked,true);
 assert.equal(s.$('.task-settings-save').disabled,false);assert.equal(s.calls.filter(c=>c.method).length,0);
 s.$('#tasks-settings-tab').dispatchEvent(new s.w.KeyboardEvent('keydown',{key:'Home',bubbles:true}));
 assert.equal(s.$('#tasks-list-panel').hidden,false);assert.equal(s.w.document.activeElement.id,'tasks-list-tab');
 s.$('#tasks-list-tab').dispatchEvent(new s.w.KeyboardEvent('keydown',{key:'End',bubbles:true}));
 assert.equal(s.$('#task-settings-panel').hidden,false);assert.equal(s.$('#tasks-settings-tab').tabIndex,0);
});
test('switching tabs preserves draft fields and does not reread or write settings',async t=>{
 const s=await setup(t);await s.open();const f=s.$('#task-settings-form');f.elements.day_start.value='09:00';f.elements.day_start.dispatchEvent(new s.w.Event('input',{bubbles:true}));
 const n=s.calls.length;s.$('#tasks-list-tab').click();await s.open();assert.equal(f.elements.day_start.value,'09:00');assert.equal(s.calls.length,n);assert.match(s.$('.task-settings-status').textContent,/несохранённые/);
});
test('operator saves personal preferences and cannot change studio settings',async t=>{
 const s=await setup(t,'operator');await s.open();const f=s.$('#task-settings-form');assert.equal(f.elements.day_start.disabled,true);assert.equal(f.querySelector('[name=press]').disabled,false);
 f.elements.press.value='menu';await s.save();assert.equal(s.calls.filter(c=>c.url.endsWith('/tasks/settings')).length,0);assert.equal(s.ctx.personal.press,'menu');assert.match(s.$('.task-settings-status').textContent,/сохранены/);assert.equal(s.$('#task-settings-panel').hidden,false);
});
test('studio validation prevents any partial write, while successful save keeps values on the page',async t=>{
 const s=await setup(t);await s.open();const f=s.$('#task-settings-form');f.elements.day_end.value='09:00';await s.save();assert.equal(s.calls.filter(c=>c.method).length,0);assert.match(s.$('.task-settings-error').textContent,/позже начала/);
 f.elements.day_start.value='08:00';await s.save();assert.equal(s.ctx.studio.day_start,'08:00');assert.equal(s.calls.filter(c=>c.method==='PUT').length,2);assert.equal(s.$('.task-settings-error').hidden,true);
});
test('failed settings loading disables empty fields and offers an inline retry',async t=>{
 let fail=true;const s=await setup(t,'owner',url=>{if(fail&&url.endsWith('/tasks/options'))throw new Error('Недоступно');});await s.open();assert.equal(s.$('.task-settings-save').disabled,true);assert.equal(s.$('.task-settings-retry').hidden,false);
 fail=false;s.$('.task-settings-retry').click();await s.flush();assert.equal(s.$('.task-settings-save').disabled,false);assert.equal(s.$('.task-settings-retry').hidden,true);
});
test('failed saving retains the draft and a reset discards it; old responses cannot refill another workspace',async t=>{
 let pending;const s=await setup(t,'owner',(url,opts)=>{if(opts.method==='PUT')throw new Error('Попробуйте ещё раз');if(url.includes('/second/')&&url.endsWith('/tasks/options'))return new Promise(r=>pending=r);});
 await s.open();const f=s.$('#task-settings-form');f.elements.press.value='menu';await s.save();assert.equal(f.elements.press.value,'menu');assert.equal(s.$('.task-settings-error').hidden,false);assert.equal(s.$('.task-settings-save').disabled,false);
 s.state.epoch++;s.state.workspace.id='second';s.app.reset();await s.open();s.state.epoch++;s.state.workspace.id='third';s.app.reset();pending(structuredClone(s.ctx));await s.flush();assert.equal(f.elements.day_start.value,'');assert.equal(s.$('#task-settings-panel').hidden,true);assert.equal(s.$('.task-settings-save').disabled,true);
});
