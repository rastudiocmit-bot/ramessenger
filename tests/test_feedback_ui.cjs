"use strict";
const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');const {JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web');
function setup(t,notices=null){
  const dom=new JSDOM(fs.readFileSync(path.join(web,'index.html'),'utf8'),{runScripts:'outside-only',url:'https://test.example.test'}),w=dom.window;
  for(const name of ['Directory','Chat','Voice','Ai','AlfaProgress','Cabinet','Team'])w['Center'+name]=()=>({});
  if(notices)w.CenterNotices=()=>notices;
  let source=fs.readFileSync(path.join(web,'app.js'),'utf8');source=source.replace('  start();\n})();','  window.testApp = {api, feedback, busy, state, refreshConnectionNotices};\n})();');
  w.eval(source);t.after(()=>dom.window.close());return{w,$:s=>w.document.querySelector(s),...w.testApp};
}
test('success creates no notification; errors stay in a form and never steal focus',async t=>{
  const s=setup(t);s.$('#chat-compose').hidden=false;s.$('#chat-input').focus();const active=s.w.document.activeElement;
  s.feedback('Сохранено');assert.equal(s.$('.action-feedback'),null);assert.equal(s.$('#toast'),null);
  s.feedback('Нет сети',true);s.feedback('Нет сети',true);
  assert.equal(s.w.document.querySelectorAll('.action-feedback').length,1);assert.equal(s.$('.action-feedback').parentElement,s.$('#chat-compose'));
  assert.equal(s.w.document.activeElement,active);assert.equal(s.$('.action-feedback').textContent,'Нет сети');
  s.feedback('Отправлено');assert.equal(s.$('.action-feedback'),null);
});
test('busy shows an error in its own form and restores controls; next action clears it',async t=>{
  const s=setup(t),form=s.$('#workspace-form');await s.busy(form,async()=>{throw new Error('Ошибка сохранения');});
  assert.equal(form.querySelector('.action-feedback').textContent,'Ошибка сохранения');assert.equal(form.querySelector('button').disabled,false);
  await s.busy(form,async()=>{});assert.equal(form.querySelector('.action-feedback'),null);
});
test('API separates network, explicit cancellation and server failures',async t=>{
  const s=setup(t);s.w.fetch=async()=>{throw new TypeError('fetch failed');};await assert.rejects(s.api('/x'),e=>e.code==='network');
  s.w.fetch=async()=>({status:503,ok:false,json:async()=>({detail:'Сервис распознавания недоступен'})});
  await assert.rejects(s.api('/x'),e=>e.status===503&&e.message.includes('распознавания'));
  const controller=new s.w.AbortController();s.w.fetch=async(url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new s.w.DOMException('Aborted','AbortError'))));
  const request=s.api('/x',{signal:controller.signal});controller.abort();await assert.rejects(request,e=>e.name==='AbortError');
});
test('API timeout includes reading the response and is not reported as a network outage',async t=>{
  const s=setup(t);s.w.fetch=async(url,{signal})=>({status:200,ok:true,json:()=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted body'))))});
  await assert.rejects(s.api('/x',{timeoutMs:5}),e=>e.name==='TimeoutError'&&e.code==='timeout');
});
test('no stylesheet or markup contains a toast overlay',()=>{
  const html=fs.readFileSync(path.join(web,'index.html'),'utf8');assert.ok(!html.includes('id="toast"'));
  for(const name of ['app.css','cabinet.css'])assert.doesNotMatch(fs.readFileSync(path.join(web,name),'utf8'),/(?:#toast|\.toast(?:\{|\.|:))/);
});
test('voice and send feedback remain inline even when the notification centre is available',t=>{
 const calls=[],s=setup(t,{notify:arg=>calls.push(arg)});s.state.pane='messages';s.$('#chat-compose').hidden=false;
 s.feedback('Проверьте доступ к микрофону',true);assert.equal(calls.length,0);assert.match(s.$('#chat-compose .action-feedback').textContent,/микрофону/);
});
test('only an actionable connection cause creates an administrator question; recovery through waiting clears it',t=>{
 const created=[],resolved=[],s=setup(t,{notify:arg=>created.push(arg),resolve:(...arg)=>resolved.push(arg)});
 s.state.user={id:'owner'};s.state.workspace={id:'centre',role:'owner'};
 const crm={configured:true,status:'active'};
 s.refreshConnectionNotices(crm,{configured:true,state:'active'});assert.equal(created.length,0);
 s.refreshConnectionNotices(crm,{configured:true,state:'otp_required'});assert.equal(created[0].key,'connection:max');assert.equal(created[0].action.label,'Ввести код');
 s.refreshConnectionNotices(crm,{configured:true,state:'reauth_required',recovery:{phase:'waiting'}});
 assert.equal(created.length,1);assert.ok(resolved.some(([key,text])=>key==='connection:max'&&text.includes('автоматически')));
 s.refreshConnectionNotices({...crm,status:'error',error_code:'network'},{configured:true,state:'active'});assert.equal(created.length,1);
 s.refreshConnectionNotices({...crm,status:'error',error_code:'unauthorized'},{configured:true,state:'active'});assert.equal(created.at(-1).key,'connection:alfa');
 s.state.workspace.role='operator';s.refreshConnectionNotices(crm,{configured:true,state:'otp_required'});assert.equal(created.length,2);
});
