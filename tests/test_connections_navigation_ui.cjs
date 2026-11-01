"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),{JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),flush=()=>new Promise(r=>setImmediate(r));
async function settle(){for(let i=0;i<6;i++)await flush();}
const member='00000000-0000-4000-8000-000000000001';
async function setup(t,handlers={}){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{url:'https://cabinet.example.test',runScripts:'outside-only'}),w=dom.window,$=s=>w.document.querySelector(s),u={w,$,calls:[],loads:{},state:null};
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};w.HTMLElement.prototype.scrollIntoView=function(){};
 w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
 const readTimers=new Map(),setTimer=w.setTimeout.bind(w),clearTimer=w.clearTimeout.bind(w);
 w.setTimeout=(fn,delay,...args)=>{const id=setTimer(fn,delay,...args);if(delay===12000)readTimers.set(id,fn);return id;};
 w.clearTimeout=id=>{readTimers.delete(id);clearTimer(id);};u.readTimers=readTimers;
 const factory=name=>options=>{u.state=options.state;u.api=options.api;return new Proxy({}, {get:(_,method)=>(...args)=>{if(method==='load'||method==='enter'||method==='connections'){const key=name+'.'+method;u.loads[key]=(u.loads[key]||0)+1;return handlers[key]?.(...args)||Promise.resolve();}}});};
 for(const name of ['Chat','Team','Directory','Voice','Tasks','CardLoader','SyncMaster','Ai','AlfaProgress'])w['Center'+name]=factory(name);
 w.fetch=async(url,options={})=>{u.calls.push({url,...options});const custom=await handlers.fetch?.(url,options);if(custom!==undefined)return custom;
  const p=url.split('?')[0];let data={};
  if(p==='/api/auth/status')data={setup_required:false};
  else if(p==='/api/auth/session')data={user:{id:member,display_name:'Synthetic Owner',email:'owner@example.test'},csrf:'synthetic',workspaces:[{id:'workspace',name:'Synthetic Studio',role:'owner',timezone:'Europe/Moscow'}]};
  else if(p.endsWith('/sip-lines'))data={version:0,lines:[],members:[{id:member,name:'Synthetic Owner'}],vault_ready:true,browser_call_ready:false};
  else if(p.endsWith('/integrations/novofon'))data={configured:false,vault_ready:true,version:0,sample_hours:24,status:'not_configured',checks:{}};
  else if(p.endsWith('/integrations/alfa'))data={configured:true,vault_ready:true,status:'active',branches:[]};
  else if(p.endsWith('/integrations/alfa-max'))data={configured:true,vault_ready:true,state:'active',branches:[]};
  return {ok:true,status:200,json:async()=>data};
 };
 for(const file of ['icons.js','cabinet.js','sip-lines.js','novofon.js','app.js'])w.eval(fs.readFileSync(web+'/'+file,'utf8'));
 t.after(()=>w.close());await settle();assert.ok(u.state.user,'Synthetic session started');return u;
}
test('returning directly to Novofon does not wait for hidden AI or CRM settings',async t=>{
 let finish;const u=await setup(t,{'Ai.load':()=>new Promise(r=>finish=r)});u.state.connectionsView='novofon';u.$('#connections-tab').click();await settle();
 assert.equal(u.$('#sip-lines-add').disabled,false);assert.equal(u.loads['Ai.load']||0,0);assert.equal(u.calls.filter(c=>c.url.endsWith('/integrations/alfa')).length,0);finish?.();
});
test('each connections subtab loads its own data without an unrelated settings fan-out',async t=>{
 const u=await setup(t);u.state.connectionsView='novofon';u.$('#connections-tab').click();await settle();
 assert.equal(u.loads['Ai.load']||0,0);u.$('#connections-voice-tab').click();await settle();assert.equal(u.loads['Ai.load'],1);
 u.$('#connections-settings-tab').click();await settle();assert.equal(u.loads['Chat.connections'],1);assert.equal(u.loads['CardLoader.load']||0,0);
});
test('a delayed old pane load cannot reenter the newly selected pane a second time',async t=>{
 let finish;const u=await setup(t,{'Ai.load':()=>new Promise(r=>finish=r)});u.state.connectionsView='voice';u.$('#connections-tab').click();await settle();
 u.$('#tasks-tab').click();await settle();assert.equal(u.loads['Tasks.enter'],1);finish();await settle();assert.equal(u.loads['Tasks.enter'],1);assert.equal(u.$('#pane-tasks').hidden,false);
});
test('a late MAX status cannot replace an explicitly selected Novofon tab',async t=>{
 let finish;const u=await setup(t,{fetch:(url)=>url.endsWith('/integrations/alfa-max')?new Promise(r=>finish=r):undefined});
 u.state.connectionsView='wizard';u.$('#connections-tab').click();await settle();u.$('#connections-novofon-tab').click();await settle();
 finish({ok:true,status:200,json:async()=>({configured:true,vault_ready:true,state:'reauth_required',branches:[]})});await settle();
 assert.equal(u.state.connectionsView,'novofon');assert.equal(u.$('#connections-novofon-view').hidden,false);
});
test('navigation remains usable while a SIP save is awaiting its response',async t=>{
 let finish;const u=await setup(t,{fetch:(url,opt)=>url.endsWith('/sip-lines')&&opt.method==='PUT'?new Promise(r=>finish=r):undefined});
 u.state.connectionsView='novofon';u.$('#connections-tab').click();await settle();u.$('#sip-lines-add').click();const f=u.$('#sip-lines-form').elements;f.sip_number.value='000123';f.password.value='synthetic-local-test';u.$('#sip-lines-form').dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await settle();
 assert.equal(u.$('#sip-lines-save').disabled,true);u.$('#tasks-tab').click();await settle();assert.equal(u.$('#pane-tasks').hidden,false);assert.equal(u.loads['Tasks.enter'],1);
 finish({ok:true,status:200,json:async()=>({version:1,lines:[],members:[{id:member,name:'Synthetic Owner'}],vault_ready:true})});await settle();assert.equal(u.calls.some(c=>c.url.endsWith('/verify')),false);
});
test('ordinary settings reads have a finite timeout and leave navigation usable',async t=>{
 const u=await setup(t,{fetch:(url,opt)=>url.endsWith('/integrations/alfa')?new Promise((_r,reject)=>opt.signal?.addEventListener('abort',()=>reject(new Error('aborted')))):undefined});
 u.state.connectionsView='wizard';u.$('#connections-tab').click();await settle();assert.ok(u.readTimers.size>=1); // The read must time out even if a diagnostic sample shares this delay.
 for(const [id,fn] of [...u.readTimers]){u.w.clearTimeout(id);fn();}await settle();assert.match(u.$('#alfa-notice').textContent,/не ответил вовремя/);
 u.$('#tasks-tab').click();await settle();assert.equal(u.$('#pane-tasks').hidden,false);
});
