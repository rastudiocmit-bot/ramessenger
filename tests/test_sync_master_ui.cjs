"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web');
function setup(t){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window,$=s=>w.document.querySelector(s);
 w.eval(fs.readFileSync(web+'/sync-master.js','utf8'));
 const state={workspace:{id:'one',timezone:'Europe/Moscow'},alfa:null,alfaMax:null},refresh=[];
 for(let el=$('.sync-master-tabs');el;el=el.parentElement)el.hidden=false;
 const app=w.CenterSyncMaster({$,state,show:(e,visible=true)=>e.hidden=!visible,refreshStatus:()=>refresh.push(true)});
 t.after(()=>w.close());return {w,$,state,app,refresh,scroll:$('#pane-connections > .scroll')};
}
test('master defaults to status with one monitor and three accessible internal tabs',t=>{
 const s=setup(t);
 assert.equal(s.$('#sync-master-status-tab').getAttribute('aria-selected'),'true');
 assert.equal(s.$('#sync-master-status-panel').hidden,false);
 assert.equal(s.$('#sync-master-policy-panel').hidden,true);
 assert.equal(s.$('#sync-master-connections-panel').hidden,true);
 assert.equal(s.w.document.querySelectorAll('.sync-master-tabs [role=tab]').length,3);
 assert.equal(s.w.document.querySelectorAll('#alfa-max-progress').length,1);
 assert.equal(s.$('#sync-master-alert').hidden,true);
});
test('internal tabs retain policy and credential drafts without requesting any refresh',t=>{
 const s=setup(t),node=s.$('#alfa-max-progress');
 s.$('#card-loader-form').elements.finance_interval_minutes.value='75';
 s.$('#alfa-max-connect-form').elements.password.value='synthetic draft';
 for(const view of ['connections','policy','status','policy','connections'])s.app.select(view);
 assert.equal(s.$('#card-loader-form').elements.finance_interval_minutes.value,'75');
 assert.equal(s.$('#alfa-max-connect-form').elements.password.value,'synthetic draft');
 assert.equal(s.$('#alfa-max-progress'),node);assert.equal(s.refresh.length,0);
});
test('each inner section and outer navigation restore its own scroll position',t=>{
 const s=setup(t);s.scroll.scrollTop=120;s.app.select('policy');
 assert.equal(s.scroll.scrollTop,0);s.scroll.scrollTop=640;s.app.select('status');
 assert.equal(s.scroll.scrollTop,120);s.app.select('policy');assert.equal(s.scroll.scrollTop,640);
 s.app.leave();s.scroll.scrollTop=5;s.app.enter();assert.equal(s.scroll.scrollTop,640);
});
test('arrow, home and end keys move selection and focus within the internal tablist',t=>{
 const s=setup(t),send=(name,key)=>s.$('#sync-master-'+name+'-tab').dispatchEvent(new s.w.KeyboardEvent('keydown',{key,bubbles:true}));
 s.$('#sync-master-status-tab').focus();send('status','ArrowRight');
 assert.equal(s.w.document.activeElement.id,'sync-master-connections-tab');
 assert.equal(s.$('#sync-master-status-tab').tabIndex,-1);
 send('connections','End');assert.equal(s.w.document.activeElement.id,'sync-master-policy-tab');
 send('policy','Home');assert.equal(s.w.document.activeElement.id,'sync-master-status-tab');
});
test('connection problem offers the exact setup form without interrupting the active draft',t=>{
 const s=setup(t);s.app.select('policy');
 s.state.alfa={configured:true,status:'active'};s.state.alfaMax={configured:true,state:'otp_required'};
 s.app.render();assert.equal(s.$('#sync-master-policy-panel').hidden,false);
 assert.match(s.$('#sync-master-alert-title').textContent,/код из письма/);
 s.$('#sync-master-alert-action').click();
 assert.equal(s.$('#sync-master-connections-panel').hidden,false);assert.equal(s.$('#sync-max-config').open,true);
 assert.equal(s.refresh.length,0);
});
test('temporary status failure keeps the last successful CRM timestamp and explicitly retries only on action',t=>{
 const s=setup(t);s.state.alfa={configured:true,status:'active'};s.state.alfaMax={configured:true,state:'active'};
 s.app.progress({last_sync_at:'2026-10-30T09:00:00Z'},{mode:'complete'});
 const at=s.$('#sync-master-last-sync').textContent;assert.match(at,/12:00/);
 s.app.progress(null,{mode:'offline'});assert.equal(s.$('#sync-master-last-sync').textContent,at);
 assert.match(s.$('#sync-master-alert-text').textContent,/не означает/);assert.equal(s.refresh.length,0);
 s.$('#sync-master-alert-action').click();assert.equal(s.refresh.length,1);
});
test('workspace reset clears the previous report and returns the master to status',t=>{
 const s=setup(t);s.app.progress({last_sync_at:'2026-10-30T09:00:00Z'},{mode:'complete'});s.app.select('policy');
 s.state.workspace={id:'two'};s.state.alfa=null;s.state.alfaMax=null;s.app.reset();
 assert.equal(s.$('#sync-master-last-sync').textContent,'Ожидаем первую проверку');
 assert.equal(s.$('#sync-master-status-panel').hidden,false);assert.equal(s.$('#sync-master-alert').hidden,true);
});
