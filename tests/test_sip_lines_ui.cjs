"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),{JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),flush=()=>new Promise(r=>setImmediate(r));
const member='00000000-0000-4000-8000-000000000001',other='00000000-0000-4000-8000-000000000002',line='00000000-0000-4000-8000-000000000010';
const base=()=>({version:0,lines:[],members:[{id:member,name:'Первый сотрудник'},{id:other,name:'Второй сотрудник'}],vault_ready:true,browser_call_ready:false});
const row=()=>({id:line,member_id:member,member_name:'Первый сотрудник',member_active:true,sip_number:'0001234',extension:'001',server:'sip.novofon.ru',port:5060,enabled:true,primary:true,password_configured:true});
function setup(t,handler,confirm=async()=>true){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{url:'https://example.test',runScripts:'outside-only'}),w=dom.window,$=s=>w.document.querySelector(s),calls=[];
 const state={workspace:{id:'org-one',role:'owner'},user:{id:member},epoch:1};
 const api=async(url,options={})=>{calls.push({url,...options});return handler?handler(url,options):base()};
 w.eval(fs.readFileSync(web+'/sip-lines.js','utf8'));const ui=w.CenterSipLines({$,state,api,confirm,notice:(s,e)=>{e.textContent=s||'';e.hidden=!s}});
 t.after(()=>w.close());return{w,$,ui,state,calls,fields:$('#sip-lines-form').elements};
}
test('SIP setup loads its own version and shows distinct employee and SIP fields',async t=>{
 const u=setup(t);assert.equal(u.$('#sip-lines-add').disabled,true);await u.ui.load();assert.equal(u.$('#sip-lines-add').disabled,false);u.$('#sip-lines-add').click();
 assert.equal(u.fields.member_id.options.length,2);assert.equal(u.fields.sip_number.type,'text');assert.equal(u.fields.extension.type,'text');assert.equal(u.fields.password.type,'password');assert.equal(u.fields.server.value,'sip.novofon.ru');assert.equal(u.fields.port.value,'5060');
});
test('save preserves leading zeros, sends employee UUID and never puts a stored password back into the form',async t=>{
 const u=setup(t,(_url,opt)=>opt.method==='PUT'?{...base(),version:1,lines:[row()]}:base());await u.ui.load();u.$('#sip-lines-add').click();
 u.fields.member_id.value=other;u.fields.sip_number.value='0001234';u.fields.extension.value='001';u.fields.password.value='synthetic-password';
 u.$('#sip-lines-form').dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();
 const save=u.calls.find(c=>c.method==='PUT');assert.equal(save.data.member_id,other);assert.equal(save.data.sip_number,'0001234');assert.equal(save.data.extension,'001');assert.equal(save.data.expected_version,0);
 assert.equal(u.fields.password.value,'');assert.equal(u.$('#sip-lines-form').hidden,true);assert.doesNotMatch(u.$('#sip-lines-list').textContent,/synthetic-password/);assert.match(u.$('#sip-lines-message').textContent,/ещё не проверены/);
});
test('editing retains a blank password and submits an explicit line ID and version',async t=>{
 const u=setup(t,(_url,opt)=>opt.method==='PUT'?{...base(),version:6,lines:[row()]}:{...base(),version:5,lines:[row()]});await u.ui.load();u.$('.sip-line-actions button').click();
 assert.equal(u.fields.password.value,'');assert.equal(u.fields.password.required,false);u.fields.extension.value='002';u.$('#sip-lines-form').dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();
 const save=u.calls.find(c=>c.method==='PUT');assert.equal(save.data.line_id,line);assert.equal(save.data.password,null);assert.equal(save.data.expected_version,5);
});
test('a delayed old-workspace save cannot clear a new workspace password or inject old rows',async t=>{
 let done;const u=setup(t,(_url,opt)=>opt.method==='PUT'?new Promise(r=>done=r):base());await u.ui.load();u.$('#sip-lines-add').click();u.fields.sip_number.value='0001';u.fields.password.value='old-password';
 u.$('#sip-lines-form').dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();u.state.workspace={id:'org-two',role:'owner'};u.state.epoch++;u.ui.reset();await u.ui.load();u.$('#sip-lines-add').click();u.fields.password.value='new-password';
 done({...base(),version:9,lines:[row()]});await flush();await flush();assert.equal(u.fields.password.value,'new-password');assert.equal(u.$('#sip-lines-list .sip-line-row'),null);
});
test('an old-workspace delete confirmation cannot delete in the new workspace',async t=>{
 let yes;const u=setup(t,()=>({...base(),version:1,lines:[row()]}),()=>new Promise(r=>yes=r));await u.ui.load();u.$('.sip-line-actions button:last-child').click();await flush();u.state.workspace={id:'org-two'};u.state.epoch++;u.ui.reset();yes(true);await flush();assert.equal(u.calls.some(c=>c.method==='DELETE'),false);
});
test('provider and employee strings remain plain text',async t=>{
 const u=setup(t,()=>({...base(),version:1,lines:[{...row(),member_name:'<img src=x onerror=alert(1)>'}]}));await u.ui.load();assert.equal(u.$('#sip-lines-list img'),null);assert.match(u.$('#sip-lines-list').textContent,/<img/);
});
test('a background version change preserves dirty input and the original optimistic version',async t=>{
 let version=1;const u=setup(t,()=>({...base(),version,lines:[row()]}));await u.ui.load();u.$('.sip-line-actions button').click();u.fields.extension.value='009';u.fields.extension.dispatchEvent(new u.w.Event('input',{bubbles:true}));version=2;await u.ui.load();
 assert.equal(u.fields.extension.value,'009');assert.match(u.$('#sip-lines-error').textContent,/изменились/);assert.equal(u.$('#sip-lines-form').hidden,false);
});
test('duplicate saves are fenced and failed saves remove entered plaintext',async t=>{
 let done;const u=setup(t,(_url,opt)=>opt.method==='PUT'?new Promise((_r,reject)=>done=reject):base());await u.ui.load();u.$('#sip-lines-add').click();u.fields.sip_number.value='0001';u.fields.password.value='synthetic-secret';
 for(let i=0;i<2;i++)u.$('#sip-lines-form').dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();assert.equal(u.calls.filter(c=>c.method==='PUT').length,1);done(Error('Сохранение не удалось'));await flush();await flush();assert.equal(u.fields.password.value,'');assert.match(u.$('#sip-lines-error').textContent,/не удалось/);
});
test('same-name employees are identified by email while assignment still uses UUID',async t=>{
 const value={...base(),members:[{id:member,name:'Тёзка',email:'one@example.test'},{id:other,name:'Тёзка',email:'two@example.test'}]};
 const u=setup(t,()=>value);await u.ui.load();u.$('#sip-lines-add').click();
 assert.deepEqual([...u.fields.member_id.options].map(o=>[o.textContent,o.value]),[['Тёзка · one@example.test',member],['Тёзка · two@example.test',other]]);
});
test('deleting a different line preserves both a new draft and edits to an existing line',async t=>{
 const second={...row(),id:'00000000-0000-4000-8000-000000000011',sip_number:'0005678'};
 for(const existing of [false,true]){
  const u=setup(t,(_url,opt)=>opt.method==='DELETE'?{...base(),version:2,lines:[second]}:{...base(),version:1,lines:[row(),second]});await u.ui.load();
  if(existing)u.$('.sip-line-row:last-child button').click();else u.$('#sip-lines-add').click();
  u.fields.sip_number.value='0005678';u.fields.extension.value='009';u.fields.password.value='synthetic-draft';u.fields.password.dispatchEvent(new u.w.Event('input',{bubbles:true}));
  u.$('.sip-line-row:first-child button:last-child').click();await flush();
  assert.equal(u.$('#sip-lines-form').hidden,false);assert.equal(u.fields.extension.value,'009');assert.equal(u.fields.password.value,'synthetic-draft');
  u.$('#sip-lines-form').dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();
  const save=u.calls.find(c=>c.method==='PUT');assert.equal(save.data.expected_version,2);assert.equal(save.data.line_id,existing?second.id:null);assert.equal(save.data.extension,'009');
 }
});
test('deleting the edited line clears its form and entered password',async t=>{
 const u=setup(t,(_url,opt)=>opt.method==='DELETE'?{...base(),version:2}:{...base(),version:1,lines:[row()]});await u.ui.load();u.$('.sip-line-actions button').click();u.fields.password.value='synthetic-draft';
 u.$('.sip-line-actions button:last-child').click();await flush();assert.equal(u.$('#sip-lines-form').hidden,true);assert.equal(u.fields.password.value,'');
});
test('a background refresh cannot advance the save version of an untouched open form',async t=>{
 let version=1;const u=setup(t,(_url,opt)=>opt.method==='PUT'?{...base(),version:3,lines:[row()]}:{...base(),version,lines:[{...row(),extension:version===1?'001':'008'}]});
 await u.ui.load();u.$('.sip-line-actions button').click();version=2;await u.ui.load();
 assert.equal(u.fields.extension.value,'001');assert.match(u.$('#sip-lines-error').textContent,/изменились/);
 u.fields.extension.value='009';u.$('#sip-lines-form').dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();assert.equal(u.calls.find(c=>c.method==='PUT').data.expected_version,1);
});
test('deleting the primary line updates an untouched primary flag in another open draft',async t=>{
 const promoted={...row(),id:'00000000-0000-4000-8000-000000000011',sip_number:'0005678',primary:false};
 const third={...row(),id:'00000000-0000-4000-8000-000000000012',sip_number:'0009999',primary:false};
 const u=setup(t,(_url,opt)=>opt.method==='DELETE'?{...base(),version:2,lines:[{...promoted,primary:true},third]}:{...base(),version:1,lines:[row(),promoted,third]});
 await u.ui.load();u.$('.sip-line-row:nth-child(2) button').click();u.fields.extension.value='009';u.fields.extension.dispatchEvent(new u.w.Event('input',{bubbles:true}));
 assert.equal(u.fields.primary.checked,false);u.$('.sip-line-row:first-child button:last-child').click();await flush();
 assert.equal(u.fields.extension.value,'009');assert.equal(u.fields.primary.checked,true);
 u.$('#sip-lines-form').dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await flush();
 assert.equal(u.calls.find(c=>c.method==='PUT').data.primary,true);
});
test('deleting another line preserves an explicit primary choice or a changed employee',async t=>{
 const second={...row(),id:'00000000-0000-4000-8000-000000000011',sip_number:'0005678',primary:false};
 for(const changedMember of [false,true]){
  const u=setup(t,(_url,opt)=>opt.method==='DELETE'?{...base(),version:2,lines:[{...second,primary:true}]}:{...base(),version:1,lines:[row(),second]});
  await u.ui.load();u.$('.sip-line-row:nth-child(2) button').click();
  if(changedMember){u.fields.member_id.value=other;}else{u.fields.primary.checked=true;}
  u.fields.member_id.dispatchEvent(new u.w.Event('input',{bubbles:true}));
  u.$('.sip-line-row:first-child button:last-child').click();await flush();
  assert.equal(u.fields.primary.checked,!changedMember);
 }
});
test('unavailable vault does not lock cancel or the password visibility control',async t=>{
 let vault_ready=true;const u=setup(t,()=>({...base(),vault_ready}));await u.ui.load();u.$('#sip-lines-add').click();u.fields.password.value='synthetic-draft';u.fields.password.dispatchEvent(new u.w.Event('input',{bubbles:true}));u.$('#sip-password-show').click();
 vault_ready=false;await u.ui.load();assert.equal(u.$('#sip-lines-save').disabled,true);
 assert.equal(u.$('#sip-password-show').disabled,false);u.$('#sip-password-show').click();assert.equal(u.fields.password.type,'password');
 assert.equal(u.$('#sip-lines-cancel').disabled,false);u.$('#sip-lines-cancel').click();assert.equal(u.$('#sip-lines-form').hidden,true);assert.equal(u.fields.password.value,'');
});
