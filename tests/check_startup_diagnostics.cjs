// Synthetic timing data only. This probe never sends diagnostics to a server.
const assert=require('node:assert/strict'),{chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:900}}),data=await fixture(context,'owner',['messages','connections'],{protocol:2,voice:true});
  await context.addInitScript(()=>{window.EventSource=undefined;Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{if(window.rejectClipboard)throw Error('Synthetic refusal');window.copiedStartup=value;}}});});
  await context.route('**/auth/session',async route=>{await new Promise(resolve=>setTimeout(resolve,350));return route.fallback();});
  await context.route('**/workspaces/private-workspace-id/chats/bootstrap?*',async route=>{await new Promise(resolve=>setTimeout(resolve,400));return route.fallback();});
  const page=await context.newPage();page.setDefaultTimeout(10000);
  await page.goto('https://cabinet.example.test/');await page.locator('.chat-row').first().waitFor();
  await page.evaluate(async()=>{document.querySelector('#ai-grok-card [name=api_key]').value='xai-SYNTHETIC-secret-never-export';await fetch('/api/workspaces/private-workspace-id/chats/bootstrap?token=SYNTHETIC-secret-never-export');const end=performance.now()+250;while(performance.now()<end){};});
  await page.locator('#profile-tab').click();await page.locator('#startup-copy').click();
  const text=await page.locator('#startup-report').inputValue(),report=JSON.parse(text);
  assert.equal(report.version,'startup-1');assert.equal(report.embedded,false);
  assert.ok(report.phases.some(x=>x.stage==='cabinet-visible'));assert.ok(report.phases.some(x=>x.stage==='chat-list'));
  assert.ok(report.long_tasks.some(x=>x.duration_ms>=200),'record a real main-thread stall');
  assert.ok(report.heartbeat_gap_ms>=150,'heartbeat records the same stall');
  assert.ok(report.requests.some(x=>x.resource==='auth/session'&&x.duration_ms>=300),'record slow session request');
  assert.ok(report.requests.some(x=>x.resource==='chats/bootstrap'));
  assert.ok(report.requests.length<=12);assert.ok(report.long_tasks.length<=8);
  assert.doesNotMatch(text,/SYNTHETIC-secret|private-workspace-id|Анна|Смирнова|example\.test|\/api\/|"password"\s*:|"api_key"\s*:/);
  assert.equal(await page.evaluate(()=>window.copiedStartup),text);
  assert.match(await page.locator('#startup-report-status').textContent(),/скопирована/);
  assert.equal(await page.evaluate(()=>Object.keys(localStorage).some(key=>/startup|diagnostic/.test(key))),false);
  assert.equal(await page.evaluate(()=>sessionStorage.length),0);
  await page.evaluate(()=>window.rejectClipboard=true);await page.locator('#startup-copy').click();
  assert.match(await page.locator('#startup-report-status').textContent(),/Ctrl\+C/);
  assert.equal(await page.locator('#startup-report').evaluate(e=>e.selectionEnd-e.selectionStart), (await page.locator('#startup-report').inputValue()).length);
  assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);assert.equal(data.mutations.length,0);
  console.log(JSON.stringify({ok:true,slow_session_ms:report.requests.find(x=>x.resource==='auth/session').duration_ms,main_thread_stall_ms:Math.max(...report.long_tasks.map(x=>x.duration_ms)),heartbeat_gap_ms:report.heartbeat_gap_ms,phases:report.phases.map(x=>x.stage),private_values_exported:false,automatic_telemetry:false,clipboard_fallback:true}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
