"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),flush=()=>new Promise(r=>setImmediate(r));
const defaults={version:0,auto_new:false,counts:{},archive:{enabled:false,version:0,imported:0,next_at:null}};
function setup(t,handler=()=>defaults){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only'}),w=dom.window,$=s=>w.document.querySelector(s),calls=[];
 const state={user:{id:'user'},workspace:{id:'one',role:'owner'},pane:'connections'};
 w.eval(fs.readFileSync(web+'/telephony-processing.js','utf8'));
 const ui=w.CenterTelephonyProcessing({$,state,api:async(url,opt={})=>{calls.push({url,...opt});return handler(url,opt);},notice:(text,e)=>{e.textContent=text||'';e.hidden=!text;},confirm:async()=>true});
 t.after(()=>{ui.reset();w.close();});return {w,$,state,ui,calls};
}
test('night archive has an explicit start button and its exact historical and time bounds',async t=>{
 const s=setup(t);await s.ui.load();assert.equal(s.$('#telephony-archive-start').disabled,false);
 const text=s.$('#telephony-processing-card').textContent;
 for(const expected of ['31 октября 2020','00:00','08:00','120 секунд','минус 7 суток'])assert.ok(text.includes(expected));
 assert.equal(s.calls.filter(c=>c.method).length,0);
});
test('daytime start stores a single intent and displays the Moscow midnight schedule',async t=>{
 let complete;const s=setup(t,(_url,opt)=>opt.method==='PUT'?new Promise(r=>complete=r):defaults);await s.ui.load();
 s.$('#telephony-archive-start').click();s.$('#telephony-archive-start').click();await flush();
 assert.equal(s.calls.filter(c=>c.method==='PUT').length,1);assert.equal(s.calls.at(-1).data.enabled,true);assert.equal(s.calls.at(-1).data.expected_version,0);
 complete({...defaults,archive:{...defaults.archive,enabled:true,version:1,waiting_for_night:true,next_at:'2026-10-31T21:00:00Z'}});await flush();
 assert.match(s.$('#telephony-archive-status').textContent,/Ожидаем ночное окно/);assert.match(s.$('#telephony-archive-next').textContent,/00:00/);
 assert.equal(s.$('#telephony-archive-start').disabled,true);assert.equal(s.$('#telephony-archive-pause').disabled,false);
});
test('pause retains the imported counter and does not reset the historical cursor',async t=>{
 const active={...defaults,archive:{...defaults.archive,enabled:true,version:4,imported:17}};
 const s=setup(t,(_url,opt)=>opt.method==='PUT'?{...active,archive:{...active.archive,enabled:false,version:5}}:active);await s.ui.load();
 s.$('#telephony-archive-pause').click();await flush();assert.equal(s.calls.at(-1).data.expected_version,4);assert.equal(s.calls.at(-1).data.enabled,false);
 assert.match(s.$('#telephony-archive-progress').textContent,/17/);assert.match(s.$('#telephony-processing-message').textContent,/Курсор.*сохранены/);
});
test('unsaved new-call preference does not become archive authorization or get overwritten by polling',async t=>{
 const s=setup(t);await s.ui.load();s.$('#telephony-processing-form').elements.auto_new.checked=true;await s.ui.load();
 assert.equal(s.calls.length,1);assert.equal(s.$('#telephony-processing-form').elements.auto_new.checked,true);
 assert.equal(s.$('#telephony-archive-pause').disabled,true);
});
test('late responses cannot fill another organization monitor or enable its start button',async t=>{
 let complete;const s=setup(t,()=>new Promise(r=>complete=r));const pending=s.ui.load();s.state.workspace.id='two';s.ui.reset();complete(defaults);await pending;
 assert.equal(s.$('#telephony-archive-start').disabled,true);assert.equal(s.$('#telephony-archive-progress').textContent,'');
});
test('monitor polling refreshes archive counters while retaining the unsaved new-call flag and original version',async t=>{
 let reads=0;const s=setup(t,(_url,opt)=>opt.method==='PUT'?{...defaults,version:3,auto_new:opt.data.auto_new}:++reads===1?defaults:
  {...defaults,version:2,counts:{complete:9},archive:{...defaults.archive,version:4,imported:17}});
 await s.ui.load();const flag=s.$('#telephony-processing-form').elements.auto_new;flag.checked=true;flag.dispatchEvent(new s.w.Event('input',{bubbles:true}));
 await s.ui.load('monitor');assert.equal(flag.checked,true);assert.match(s.$('#telephony-archive-progress').textContent,/17/);
 assert.match(s.$('#telephony-processing-metrics').textContent,/Готово9/);
 s.$('#telephony-processing-form').dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await flush();
 assert.equal(s.calls.at(-1).data.expected_version,0);assert.equal(s.calls.at(-1).data.auto_new,true);
});
