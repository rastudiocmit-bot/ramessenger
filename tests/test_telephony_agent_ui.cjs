"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),flush=()=>new Promise(r=>setImmediate(r));
const defaults={name:'Телефонные разговоры',model:'model-one',recognition_provider:'yandex',prompt:'Описание фактов звонка',temperature:0.1,version:1,history:[],
 models:[{id:'model-one',title:'Модель',provider_title:'Шлюз'}],recognizers:[{id:'yandex',title:'Yandex',configured:true}]};
function setup(t,handler=()=>defaults){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only'}),w=dom.window,$=s=>w.document.querySelector(s);
 const state={user:{id:'user-one'},workspace:{id:'workspace-one',role:'owner'}},calls=[];
 w.eval(fs.readFileSync(web+'/telephony-agent.js','utf8'));
 const ui=w.CenterTelephonyAgent({$,state,api:async(url,opt={})=>{calls.push({url,...opt});return handler(url,opt);},
  notice:(text,el)=>{el.textContent=text||'';el.hidden=!text;},confirm:async()=>true});
 t.after(()=>w.close());return {w,$,state,ui,calls};
}
test('phone agent is a separate card in AI agents and starts disabled until settings are known',async t=>{
 const s=setup(t);assert.ok(s.$('#connections-agents-view #telephony-agent-card'));
 assert.equal(s.$('#telephony-agent-form button').disabled,true);await s.ui.load();
 assert.equal(s.$('#telephony-agent-form').elements.agent_name.value,defaults.name);
 assert.equal(s.calls.length,1);assert.equal(s.calls[0].method,undefined);
 assert.ok(s.calls[0].url.endsWith('/integrations/novofon/agent'));
});
test('saving edits uses the known version and never starts a model request',async t=>{
 let done;const s=setup(t,(_url,opt)=>opt.method==='PUT'?new Promise(r=>done=r):defaults);await s.ui.load();
 const form=s.$('#telephony-agent-form');form.elements.prompt.value='Инструкция только для звонков';
 for(let i=0;i<2;i++)form.dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));
 await flush();const writes=s.calls.filter(c=>c.method==='PUT');assert.equal(writes.length,1);
 assert.equal(writes[0].data.expected_version,1);assert.equal(writes[0].data.prompt,'Инструкция только для звонков');
 assert.equal(s.calls.filter(c=>c.method==='POST').length,0);
 done({...defaults,version:2,prompt:'Инструкция только для звонков'});await flush();
 assert.match(s.$('#telephony-agent-message').textContent,/ещё не запущена/);
});
test('unsaved instruction cannot be tested against the old saved version',async t=>{
 const s=setup(t);await s.ui.load();const form=s.$('#telephony-agent-form');
 s.$('#telephony-agent-sample').value='Обсудили занятие';s.$('#telephony-agent-sample').dispatchEvent(new s.w.Event('input'));
 assert.equal(s.$('#telephony-agent-test').disabled,false);form.elements.prompt.value='Новые правила';form.dispatchEvent(new s.w.Event('input'));
 assert.equal(s.$('#telephony-agent-test').disabled,true);
 s.$('#telephony-agent-test').click();await flush();assert.equal(s.calls.length,1);
});
test('test result is plain text and never changes a call or a generic agent',async t=>{
 const payload='<img src=x onerror=alert(1)>',s=setup(t,(_url,opt)=>opt.method==='POST'?{summary:payload,version:1,cost_rub:0.01}:defaults);await s.ui.load();
 const sample=s.$('#telephony-agent-sample');sample.value='Текст разговора';sample.dispatchEvent(new s.w.Event('input'));
 s.$('#telephony-agent-test').click();await flush();
 assert.equal(s.$('#telephony-agent-result img'),null);assert.equal(s.$('#telephony-agent-result').textContent,payload);
 assert.ok(s.calls.at(-1).url.endsWith('/novofon/agent/test'));assert.match(s.$('#telephony-agent-message').textContent,/Звонки не изменены/);
});
test('late response cannot expose a previous workspace profile',async t=>{
 let done;const s=setup(t,()=>new Promise(r=>done=r)),pending=s.ui.load();
 s.state.workspace.id='workspace-two';s.ui.reset();done(defaults);await pending;
 assert.equal(s.$('#telephony-agent-form').elements.prompt.value,'');assert.equal(s.$('#telephony-agent-test').disabled,true);
});
test('a non-admin cannot load or test the dedicated agent',async t=>{
 const s=setup(t);s.state.workspace.role='observer';await s.ui.load();assert.equal(s.calls.length,0);
 assert.equal(s.$('#telephony-agent-refresh').disabled,true);
});
test('background load preserves unsaved edits and version history never injects markup',async t=>{
 const s=setup(t,()=>({...defaults,history:[{version:1,name:'<b>agent</b>',prompt:'<script>bad()</script>',model:'model-one'}]}));
 await s.ui.load();const form=s.$('#telephony-agent-form');form.elements.prompt.value='Несохранённая инструкция';await s.ui.load();
 assert.equal(s.calls.length,1);assert.equal(form.elements.prompt.value,'Несохранённая инструкция');
 assert.equal(s.$('#telephony-agent-history script'),null);assert.match(s.$('#telephony-agent-history').textContent,/<script>/);
});
