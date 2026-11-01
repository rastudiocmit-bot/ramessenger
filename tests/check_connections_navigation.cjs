// Synthetic provider replies only: no real SIP registration, calls or passwords.
const assert=require('node:assert/strict'),{chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
const member='00000000-0000-4000-8000-000000000001',line='00000000-0000-4000-8000-000000000010';
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{const reports=[];
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),data=await fixture(context,'owner',['messages','tasks','connections','security']);
  let releaseAi,releaseMax,releaseSave,gets=0,saves=0,verifies=0,version=0,lines=[];
  const publicLines=()=>({version,lines,members:[{id:member,name:'Synthetic Operator'}],vault_ready:true,browser_call_ready:false});
  await context.route('**/api/workspaces/*/ai/settings',route=>{releaseAi=()=>route.fulfill({contentType:'application/json',body:JSON.stringify(data.ai.settings)});});
  await context.route('**/api/workspaces/*/integrations/alfa-max',route=>{releaseMax=()=>route.fulfill({contentType:'application/json',body:JSON.stringify({configured:true,vault_ready:true,state:'reauth_required',branches:[],error:'Synthetic test status'})});});
  await context.route('**/api/workspaces/*/chats/*/calls**',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({calls:[],connected:false})}));
  await context.route('**/api/workspaces/*/integrations/novofon**',async route=>{
   const request=route.request(),p=new URL(request.url()).pathname;
   if(p.endsWith('/verify'))verifies++;
   if(p.endsWith('/sip-lines')){
    if(request.method()==='PUT'){saves++;const value=request.postDataJSON();assert.equal(value.sip_number,'000123');releaseSave=async()=>{version++;const {password,...metadata}=value;lines=[{...metadata,id:line,member_name:'Synthetic Operator',member_active:true,password_configured:true}];await route.fulfill({contentType:'application/json',body:JSON.stringify(publicLines())});};return;}
    gets++;return route.fulfill({contentType:'application/json',body:JSON.stringify(publicLines())});
   }
   const body=p.endsWith('/agent')?{version:0,name:'Телефонные разговоры',prompt:'Синтетическая инструкция',temperature:.1,models:[],recognizers:[],history:[],model:'',recognition_provider:'yandex',vault_ready:true}:p.endsWith('/processing')?{version:0,auto_new:false,counts:{},archive:{enabled:false,version:0,imported:0,next_at:null}}:p.endsWith('/loader')?{enabled:true,history_days:7,poll_seconds:60,version:0,running:false,call_count:0}:{configured:false,vault_ready:true,version:0,sample_hours:24,status:'not_configured',checks:{}};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  });
  const page=await context.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');await page.locator('.chat-row').first().waitFor();
  await page.locator('#connections-tab').evaluate(e=>e.click());await page.waitForFunction(()=>document.querySelector('#connections-wizard-view').hidden===false);
  const aiPending=page.waitForRequest('**/api/workspaces/*/ai/settings');await page.locator('#connections-voice-tab').click();await aiPending;
  const phoneStart=Date.now();await page.locator('#connections-novofon-tab').click();await page.locator('#sip-lines-add:not([disabled])').waitFor();const phoneMs=Date.now()-phoneStart;assert.ok(phoneMs<2000,'Unrelated AI request blocked Novofon');
  await releaseMax();await page.waitForFunction(()=>document.querySelector('#alfa-max-status').dataset.state==='error');assert.equal(await page.locator('#connections-novofon-view').isVisible(),true);
  await page.locator('#sip-lines-add').click();const form=page.locator('#sip-lines-form');await form.locator('[name=sip_number]').fill('000123');await form.locator('[name=password]').fill('synthetic-local-password');await page.locator('#sip-lines-save').click();await page.locator('#sip-lines-save:disabled').waitFor();assert.equal(saves,1);
  const tasksStart=Date.now();await page.locator('#tasks-tab').evaluate(e=>e.click());await page.locator('#pane-tasks').waitFor({state:'visible'});const tasksMs=Date.now()-tasksStart;assert.ok(tasksMs<2000,'Pending SIP write blocked navigation');
  await page.locator('#connections-tab').evaluate(e=>e.click());await page.locator('#connections-novofon-view').waitFor({state:'visible'});assert.equal(saves,1);await releaseSave();await form.waitFor({state:'hidden'});
  const cachedGets=gets;await page.locator('#tasks-tab').evaluate(e=>e.click());await page.locator('#connections-tab').evaluate(e=>e.click());await page.locator('#sip-lines-add:not([disabled])').waitFor();assert.equal(gets,cachedGets,'Tab return reread the SIP list');
  await page.locator('#sip-lines-refresh').click();await page.locator('#sip-lines-add:not([disabled])').waitFor();assert.equal(gets,cachedGets+1);await releaseAi();
  assert.equal(await page.locator('#connections-novofon-view').isVisible(),true);assert.equal(verifies,0);assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
  reports.push({width,phoneMs,tasksMs,saves,manualRefreshReads:1,unrelatedReplyKeptSelectedTab:true});await page.close();
 }
 console.log(JSON.stringify({checks:reports}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
