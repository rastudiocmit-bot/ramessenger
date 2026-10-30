'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),tick=()=>new Promise(r=>setImmediate(r));
async function setup(t,role='owner',handler){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',url:'https://example.test'}),w=dom.window,$=s=>w.document.querySelector(s),calls=[],confirmations=[];
 const state={user:{id:'self'},workspace:{id:'centre',role},epoch:1};
 const members=[{user_id:'self',display_name:'Владелец',email:'owner@example.test',role}, {user_id:'one',display_name:'Первый сотрудник',email:'one@example.test',role:'operator',sections:['messages','tasks','security']},{user_id:'two',display_name:'Второй сотрудник',email:'two@example.test',role:'observer',sections:['messages']},{user_id:'admin',display_name:'Администратор',email:'admin@example.test',role:'admin',sections:[]}];
 let accept=true;
 const api=async(url,options={})=>{calls.push({url,...options});const result=await handler?.(url,options);if(result!==undefined)return result;if(url.endsWith('/members'))return {members:structuredClone(members)};if(url.endsWith('/invitations'))return {url:'https://example.test/#invite=synthetic-only'};return {};};
 w.eval(fs.readFileSync(web+'/icons.js','utf8'));w.eval(fs.readFileSync(web+'/team.js','utf8'));
 const app=w.CenterTeam({$,state,api,node:(tag,text,cls)=>{const e=w.document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;},show:(e,v=true)=>e.hidden=!v,roles:{owner:'Владелец',operator:'Оператор',observer:'Наблюдатель',admin:'Администратор',branch_manager:'Руководитель филиала'},initials:n=>n[0],confirm:async(...args)=>{confirmations.push(args);return accept;}});
 t.after(()=>w.close());const flush=async()=>{await tick();await tick();};
 const row=id=>$(`[data-member=${id}]`),open=async id=>{row(id).querySelector('.team-access-toggle').click();await flush();},save=async id=>{row(id).querySelector('form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await flush();};
 await app.load();return {w,$,state,app,calls,confirmations,row,open,save,flush,accept:v=>accept=v};
}
test('employee access expands inline with accurate roles, owner protections and one atomic save',async t=>{
 const s=await setup(t);assert.equal(s.$('#member-options-dialog'),null);assert.equal(s.$('#invitation-dialog'),null);await s.open('self');assert.match(s.row('self').textContent,/Все разделы|Права владельца защищены/);assert.equal(s.row('self').querySelector('select'),null);
 await s.open('one');assert.equal(s.row('self').querySelector('.team-access-panel').hidden,true);const select=s.row('one').querySelector('select');select.value='observer';select.dispatchEvent(new s.w.Event('change'));await s.save('one');
 const writes=s.calls.filter(c=>c.method);assert.equal(writes.length,1);assert.ok(writes[0].url.endsWith('/one/access'));assert.deepEqual(JSON.parse(JSON.stringify(writes[0].data)),{role:'observer',sections:['messages','tasks','security']});assert.match(s.row('one').querySelector('.team-save-status').textContent,/сохранён/);
});
test('switching employees protects unsaved grants; cancel restores exact saved values',async t=>{
 const s=await setup(t);await s.open('one');s.row('one').querySelector('[value=tasks]').click();s.accept(false);await s.open('two');assert.equal(s.row('one').querySelector('.team-access-panel').hidden,false);assert.equal(s.row('two').querySelector('.team-access-panel').hidden,true);assert.equal(s.confirmations.length,1);
 s.row('one').querySelector('.team-save-actions .sec').click();await s.open('one');assert.equal(s.row('one').querySelector('[value=tasks]').checked,true);assert.equal(s.calls.filter(c=>c.method).length,0);
});
test('tasks always imply chats, empty grants cannot be saved, and errors keep the draft',async t=>{
 const s=await setup(t,'owner',(url,o)=>{if(o.method)throw new Error('Сервис недоступен');});await s.open('one');s.row('one').querySelector('[value=messages]').click();assert.equal(s.row('one').querySelector('[value=tasks]').checked,false);
 s.row('one').querySelector('[value=security]').click();await s.save('one');assert.match(s.row('one').textContent,/Выберите хотя бы один/);assert.equal(s.calls.filter(c=>c.method).length,0);
 s.row('one').querySelector('[value=tasks]').click();assert.equal(s.row('one').querySelector('[value=messages]').checked,true);await s.save('one');assert.match(s.row('one').querySelector('.team-save-status').textContent,/Сервис недоступен/);assert.equal(s.row('one').querySelector('[value=tasks]').checked,true);assert.equal(s.row('one').querySelector('select').disabled,false);
});
test('admin grants cannot be partially edited, and an administrator cannot promote or edit peers',async t=>{
 const s=await setup(t,'admin');await s.open('admin');assert.equal(s.row('admin').querySelector('select'),null);await s.open('one');assert.equal(s.row('one').querySelector('option[value=admin]'),null);
 const owner=await setup(t);await owner.open('admin');assert.equal(owner.row('admin').querySelector('.team-section-checks').hidden,true);assert.equal(owner.row('admin').querySelector('.team-full-access').hidden,false);assert.equal(owner.row('admin').querySelector('[type=submit]').disabled,true);
});
test('the invitation appears above the list with real section grants and no write until submitted',async t=>{
 const s=await setup(t);s.$('#invite-open').click();await s.flush();assert.equal(s.$('#team-invite').hidden,false);assert.equal(s.calls.filter(c=>c.method).length,0);const f=s.$('#create-invite-form');f.elements.email.value='new@example.test';f.dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await s.flush();
 const write=s.calls.find(c=>c.url.endsWith('/invitations'));assert.deepEqual(JSON.parse(JSON.stringify(write.data)),{email:'new@example.test',role:'operator',sections:['messages','tasks','security']});assert.match(s.$('#invite-url').value,/synthetic-only/);assert.equal(s.$('#invite-result').hidden,false);
 s.$('#team-invite-close').click();await s.flush();assert.equal(s.$('#invite-url').value,'');
});
test('an old save cannot repopulate another workspace, and reset clears invitation links and employee data',async t=>{
 let resolve;const s=await setup(t,'owner',(url,o)=>{if(o.method)return new Promise(r=>resolve=r);});await s.open('one');s.row('one').querySelector('[value=tasks]').click();s.save('one');await s.flush();s.state.workspace.id='second';s.state.epoch++;s.app.reset();resolve({});await s.flush();assert.equal(s.$('#members-list').children.length,0);assert.equal(s.$('#invite-url').value,'');assert.equal(s.$('#team-invite').hidden,true);
});
test('a changed authorization epoch restarts loading instead of leaving a stale busy state',async t=>{
 let pending,slow=false;const s=await setup(t,'owner',url=>{if(slow&&url.endsWith('/members'))return new Promise(r=>pending=r);});slow=true;const old=s.app.load();await s.flush();s.state.epoch++;s.app.reset();slow=false;await s.app.load();pending({members:[]});await old;assert.equal(s.$('#members-refresh').disabled,false);assert.equal(s.$('#members-list').children.length,4);
});
