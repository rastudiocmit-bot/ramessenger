// Real DOM/state transitions with synthetic data; all network requests intercepted.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
(async () => {
 const browser = await chromium.launch({ executablePath: process.env.CM_TEST_CHROMIUM_EXECUTABLE || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage', ...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS || '[]')] });
 try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } }), data = await fixture(context);
  const branches = [{ id:'1', name:'Центральный' },{ id:'2', name:'Северный' }];
  const now = () => new Date().toISOString();
  const streams = [{ branch_id:'1', branch_name:'Центральный',direction:'receive',source_rows:5606,expected_rows:5606,complete:true,checked_page:280 },
   {branch_id:'1',branch_name:'Центральный',direction:'sent',source_rows:5184,expected_rows:5184,complete:true,checked_page:259},
   {branch_id:'2',branch_name:'Северный',direction:'receive',source_rows:5607,expected_rows:5607,complete:true,checked_page:280},
   {branch_id:'2',branch_name:'Северный',direction:'sent',source_rows:19535,expected_rows:19536,complete:false,checked_page:245}];
  let report = { configured:true,state:'active',syncing:true,scheduled:true,messages:82770,stored_messages:88760,conversations:2411,
   backfill_pending:true,history_complete:false,history_paused:true,unidentified_rows:0,client_repair:{processed:1920,total:3000,complete:false},
   streams:[...streams,{branch_id:'4',branch_name:'Южный',direction:'sent',source_rows:675,expected_rows:684,missing_rows:9,
    paused_until:new Date(Date.now()+21600000).toISOString(),error:'В источнике остаётся расхождение. Повторная проверка запланирована.'}],
   last_sync_at:now(),next_attempt_at:now(),worker:{phase:'reading',branch_id:'2',direction:'sent',page:246,completed_pages:5,total_pages:16,updated_at:now()} };
  const syncRequests=[];let statusReads=0;
  await context.route('**/integrations/alfa-max**', route => {
   const req=route.request(), p=new URL(req.url()).pathname;let payload;
   if(p.endsWith('/sync-progress'))payload=report;
   else if(p.endsWith('/sync')){syncRequests.push({method:req.method(),csrf:req.headers()['x-csrf-token']});payload={queued:true,already_running:true};}
   else if(p.endsWith('/integrations/alfa-max')){statusReads++;payload={configured:true,state:report.state,vault_ready:true,crm_configured:true,branches,email:'demo@example.test',error:report.state==='reauth_required'?'Сессия завершена. Войдите заново.':null};}
   else return route.abort();
   return route.fulfill({contentType:'application/json',body:JSON.stringify(payload)});
  });
  const page=await context.newPage();page.setDefaultTimeout(12000);
  const save=async name=>{if(process.env.CM_HISTORY_SCREENSHOT_DIR){fs.mkdirSync(process.env.CM_HISTORY_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.CM_HISTORY_SCREENSHOT_DIR,name+'.png'),animations:'disabled',timeout:20000});}};
  const refresh=async mode=>{await page.locator('#alfa-max-progress-refresh').click();await page.waitForFunction(m=>document.querySelector('#alfa-max-progress').dataset.mode===m,mode);};
  await page.goto('https://cabinet.example.test/');await page.locator('#connections-tab').click();
  await page.waitForFunction(()=>document.querySelector('#alfa-max-progress').dataset.mode==='running');
  assert.match(await page.locator('#alfa-max-progress-activity').textContent(),/страница 247/);
  assert.equal(await page.locator('#alfa-max-progress-track').getAttribute('aria-valuenow'),'64');
  assert.match(await page.locator('#alfa-max-progress-warning').textContent(),/На паузе журналов: 1/);
  await page.locator('#alfa-max-progress-journals').evaluate(e=>{e.open=false;});await save('running-desktop');
  report.worker.page=247;await refresh('running');await page.waitForFunction(()=>document.querySelector('#alfa-max-progress-activity').textContent.includes('страница 248'));
  assert.equal(await page.locator('[data-metric="messages"] strong').getAttribute('data-count'),'82770');
  await page.locator('#alfa-max-sync').click();await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('уже обрабатывается'));
  assert.deepEqual(syncRequests,[{method:'POST',csrf:'synthetic-csrf'}]);
  await page.locator('#toast').waitFor({state:'hidden'});
  report={...report,backfill_pending:false,client_repair:{processed:3000,total:3000,complete:true},streams:[report.streams.at(-1)]};
  await refresh('paused');assert.equal(await page.locator('.sync-dashboard .sync-spinner').evaluate(e=>getComputedStyle(e).display),'none');await save('paused-desktop');
  report={...report,history_complete:true,history_paused:false,streams:streams.map(s=>({...s,source_rows:s.expected_rows,missing_rows:0,complete:true})),worker:{...report.worker,page:0}};
  await refresh('complete');assert.equal(await page.locator('.sync-dashboard .sync-spinner').evaluate(e=>getComputedStyle(e).display),'none');await save('complete-desktop');
  report={...report,state:'reauth_required',syncing:false,error:'Сессия AlfaCRM завершилась.'};await refresh('error');
  await page.waitForSelector('#alfa-max-connect-form');assert.equal(await page.locator('#alfa-max-sync').isDisabled(),true);await save('reauth-desktop');
  report={...report,state:'active',error:null,history_complete:false,backfill_pending:true,syncing:true,streams,
    client_repair:{processed:2040,total:3000,complete:false},worker:{phase:'repair',updated_at:now()}};
  await refresh('running');await page.waitForSelector('#alfa-max-connect-form',{state:'hidden'});
  await page.locator('#profile-tab').click();await page.locator('#profile-theme').check();await page.locator('#connections-tab').click();
  await page.waitForFunction(()=>document.querySelector('#alfa-max-progress').dataset.mode==='running');
  await page.locator('#pane-connections>.scroll').evaluate(e=>{e.scrollTop=0;});await save('running-dark-desktop');
  await page.setViewportSize({width:390,height:844});await page.locator('#alfa-max-progress').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),390);
  assert.equal(await page.locator('#pane-connections').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
  assert.equal(await page.locator('.sync-steps li>span').evaluateAll(items=>items.every(e=>
    e.getBoundingClientRect().right<=e.parentElement.getBoundingClientRect().right-2)),true);
  await save('running-dark-mobile');
  assert.ok(statusReads<12,'A state change must not create a configuration polling loop');
  assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
  console.log(JSON.stringify({browser:browser.version(),running:true,paused:true,completeDuringLivePolling:true,sessionExpiry:true,
   idempotentSync:true,mobileWidth:390,statusReads,errors:data.errors},null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
