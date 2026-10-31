"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web');
const defaults={enabled:true,history_days:7,poll_seconds:60,version:1,running:false,call_count:12};
const flush=()=>new Promise(r=>setImmediate(r));
function setup(t,api){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only'}),w=dom.window,$=s=>w.document.querySelector(s),state={workspace:{id:'one',timezone:'Europe/Moscow'}};
 w.eval(fs.readFileSync(web+'/telephony-loader.js','utf8'));
 const ui=w.CenterTelephonyLoader({$,state,api,notice:(text,e)=>{e.textContent=text||'';e.hidden=!text;}});
 t.after(()=>w.close());return {w,$,state,ui};
}
test('loader defaults are prefilled and editable only after a known server version',async t=>{
 const s=setup(t,async()=>defaults),form=s.$('#telephony-loader-form');
 assert.equal(form.elements.history_days.value,'7');assert.equal(form.elements.poll_seconds.value,'60');assert.equal(form.elements.enabled.disabled,true);
 await s.ui.load();assert.equal(form.elements.enabled.disabled,false);assert.match(s.$('#telephony-loader-count').textContent,/12/);
});
test('saving uses the loaded version and blocks duplicate submissions',async t=>{
 const calls=[];let save;
 const s=setup(t,async(url,opt={})=>{calls.push(opt);return opt.method==='PUT'?new Promise(r=>save=r):defaults;});
 await s.ui.load();const form=s.$('#telephony-loader-form');form.elements.history_days.value='14';
 for(let i=0;i<2;i++)form.dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));
 await flush();assert.equal(calls.filter(c=>c.method==='PUT').length,1);assert.equal(calls.at(-1).data.expected_version,1);assert.equal(calls.at(-1).data.history_days,14);
 save({...defaults,version:2,history_days:14});await flush();assert.equal(form.elements.history_days.disabled,false);
});
test('a late loader response cannot overwrite a different organization',async t=>{
 let complete;const s=setup(t,()=>new Promise(r=>complete=r)),read=s.ui.load();
 s.state.workspace.id='two';s.ui.reset();complete({...defaults,history_days:90});await read;
 assert.equal(s.$('#telephony-loader-form').elements.history_days.value,'7');assert.equal(s.$('#telephony-loader-form').elements.enabled.disabled,true);
});
test('quota waits show a clear reason, balances and a dated automatic retry',async t=>{
 const s=setup(t,async()=>({...defaults,archive_done:true,last_error_code:'quota_exhausted',retry_at:'2026-11-01T10:00:00Z',
  api_limits:{minute_remaining:8,minute_limit:15,day_remaining:0,day_limit:500}}));
 await s.ui.load();
 assert.match(s.$('#telephony-loader-status').textContent,/Пауза по лимиту API/);
 assert.match(s.$('#telephony-loader-progress').textContent,/8 из 15/);
 assert.match(s.$('#telephony-loader-progress').textContent,/0 из 500/);
 assert.match(s.$('#telephony-loader-code').textContent,/автоматически/);
 assert.match(s.$('#telephony-loader-code').textContent,/01\.11\.2026/);
 assert.equal(s.$('#telephony-loader-form').elements.poll_seconds.disabled,false);
});
