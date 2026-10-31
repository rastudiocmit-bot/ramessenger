"use strict";
const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path");
const {JSDOM}=require("jsdom");
const web=path.join(__dirname,"../app/backend/src/center_messenger/web");
const blank={configured:false,vault_ready:true,version:0,sample_hours:24,status:"not_configured",checks:{},browser_call_ready:false};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function setup(t,handler,confirm=async()=>true){
 const dom=new JSDOM(fs.readFileSync(web+"/index.html","utf8"),{runScripts:"outside-only"}), w=dom.window;
 const $=s=>w.document.querySelector(s), calls=[], state={workspace:{id:"org-one",role:"owner"},user:{id:"owner"}};
 const api=async(url,options={})=>{calls.push({url,...options});return handler?handler(url,options):{...blank};};
 w.eval(fs.readFileSync(web+"/novofon.js","utf8"));
 const ui=w.CenterNovofon({$,state,api,show:(e,v=true)=>{e.hidden=!v;},notice:(text,e)=>{e.textContent=text||"";e.hidden=!text;},confirm});
 t.after(()=>w.close()); return {w,$,calls,state,ui};
}
test("Novofon is one accessible connection tab with safe useful defaults",async t=>{
 const u=setup(t);assert.equal(u.$("#connections-novofon-tab").getAttribute("aria-controls"),"connections-novofon-view");
 assert.equal(u.w.document.querySelectorAll("#novofon-form").length,1);
 assert.equal(u.$('#novofon-form [name="sample_hours"]').value,"24");
 assert.equal(u.$('#novofon-form [name="api_key"]').type,"password");
 assert.equal(u.$("#novofon-save").disabled,true);await u.ui.load();assert.equal(u.$("#novofon-save").disabled,false);
 assert.equal(u.$("#novofon-check").disabled,true);
});
test("failed settings load never lets an unknown version overwrite settings",async t=>{
 const u=setup(t,()=>{throw new Error("network unavailable")});await u.ui.load();
 assert.equal(u.$("#novofon-save").disabled,true);assert.match(u.$("#novofon-error").textContent,/network/);
 assert.equal(u.$("#novofon-reload").disabled,false);
});
test("save clears plaintext before verification and never claims browser readiness",async t=>{
 let resolveCheck;
 const u=setup(t,(_url,opt)=> opt.method==="PUT"?{...blank,configured:true,version:1,status:"saved"}:
  opt.method==="POST"?new Promise(resolve=>{resolveCheck=resolve}):{...blank});
 await u.ui.load();const input=u.$('#novofon-form [name="api_key"]');input.value="synthetic-api-key-123";
 u.$("#novofon-form").dispatchEvent(new u.w.Event("submit",{bubbles:true,cancelable:true}));await flush();
 assert.equal(input.value,"");assert.equal(u.$("#novofon-save").disabled,true);
 assert.equal(u.calls.filter(c=>c.method==="PUT").length,1);
 assert.equal(u.calls.at(-1).data.expected_version,1);
 resolveCheck({...blank,configured:true,version:1,status:"api_verified",checks:{account:{status:"ok",account_id:"7",timezone:"Europe/Moscow"}}});
 await flush();assert.equal(u.$("#novofon-status").textContent,"Доступ к API проверен");
 assert.match(u.$(".novofon-next").textContent,/Ещё не подключены/);assert.equal(u.$("#novofon-save").disabled,false);
});
test("duplicate submit cannot save twice",async t=>{
 let complete;const u=setup(t,(_url,opt)=>opt.method==="PUT"?new Promise(resolve=>{complete=resolve}):{...blank});
 await u.ui.load();u.$('#novofon-form [name="api_key"]').value="synthetic-api-key-123";
 for(let n=0;n<2;n++)u.$("#novofon-form").dispatchEvent(new u.w.Event("submit",{bubbles:true,cancelable:true}));
 await flush();assert.equal(u.calls.filter(c=>c.method==="PUT").length,1);
 u.ui.reset();complete({...blank,configured:true,version:1});await flush();assert.equal(u.$('#novofon-form [name="api_key"]').value,"");
});
test("workspace changes discard outstanding responses and remove the entered key",async t=>{
 let complete;const u=setup(t,()=>new Promise(resolve=>{complete=resolve}));
 const read=u.ui.load();u.$('#novofon-form [name="api_key"]').value="synthetic-api-key-123";
 u.state.workspace={id:"org-two",role:"owner"};u.ui.reset();
 complete({...blank,configured:true,version:9,status:"api_verified"});await read;
 assert.equal(u.$('#novofon-form [name="api_key"]').value,"");assert.equal(u.$("#novofon-status").textContent,"Не подключено");
});
test("provider errors are plain text and a changed key requires saving first",async t=>{
 const u=setup(t,()=>({...blank,configured:true,version:1,status:"error",checks:{account:{status:"error",message:'<img src=x onerror="alert(1)">'}}}));
 await u.ui.load();assert.equal(u.$("#novofon-checks img"),null);assert.match(u.$("#novofon-checks").textContent,/<img/);
 assert.equal(u.$("#novofon-check").disabled,false);
 u.$('#novofon-form [name="api_key"]').value="new-synthetic-key";u.$("#novofon-form").dispatchEvent(new u.w.Event("input",{bubbles:true}));
 assert.equal(u.$("#novofon-check").disabled,true);
});

test("a confirmation from the old workspace cannot disconnect the new workspace",async t=>{
 let confirmAction;
 const u=setup(t,()=>({...blank,configured:true,version:1}),()=>new Promise(resolve=>{confirmAction=resolve}));
 await u.ui.load();u.$("#novofon-disconnect").click();await flush();
 u.state.workspace={id:"org-two",role:"owner"};u.ui.reset();await u.ui.load();
 confirmAction(true);await flush();assert.equal(u.calls.filter(call=>call.method==="DELETE").length,0);
});

test("both entered credentials are saved separately and cleared before checking",async t=>{
 const u=setup(t,(_url,opt)=>({...blank,configured:true,support_secret_configured:true,version:opt.method==="PUT"?2:1}));
 await u.ui.load();u.$('#novofon-form [name="api_key"]').value="synthetic-api-key";
 u.$('#novofon-form [name="support_secret"]').value="synthetic-support-secret";
 u.$("#novofon-form").dispatchEvent(new u.w.Event("submit",{bubbles:true,cancelable:true}));await flush();
 const save=u.calls.find(call=>call.method==="PUT");
 assert.equal(save.data.api_key,"synthetic-api-key");assert.equal(save.data.support_secret,"synthetic-support-secret");
 assert.equal(u.$('#novofon-form [name="api_key"]').value,"");assert.equal(u.$('#novofon-form [name="support_secret"]').value,"");
 assert.match(u.$("#novofon-support-note").textContent,/ещё не подтверждено/);
});

test("support secret has its own reveal and is cleared on workspace reset",async t=>{
 const u=setup(t);await u.ui.load();const support=u.$('#novofon-form [name="support_secret"]');
 support.value="synthetic-support-secret";u.$("#novofon-show-support").click();
 assert.equal(support.type,"text");assert.equal(u.$('#novofon-form [name="api_key"]').type,"password");
 u.ui.reset();assert.equal(support.value,"");assert.equal(support.type,"password");
});

test("provider mnemonic and observed IP appear as plain diagnostic text",async t=>{
 const u=setup(t,()=>({...blank,configured:true,checks:{account:{status:"error",rpc_code:-32003,
  mnemonic:"ip_not_whitelisted",observed_ip:"83.222.25.109",message:"Разрешите IP"}}}));
 await u.ui.load();const text=u.$("#novofon-checks").textContent;
 assert.match(text,/-32003/);assert.match(text,/ip_not_whitelisted/);assert.match(text,/83\.222\.25\.109/);
});
