// Synthetic browser UI only; no CRM login or delivery.
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {fixture}=require('./browser_fixture.cjs');
(async()=>{
 const checks=[];
 for(const width of [1440,390]){
  const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE,args:['--no-sandbox',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
  try{
   const context=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720});
   const data=await fixture(context);
   let phase='active', failNext=0;
   const preferenceConfigured=width===390;
   await context.route('**/integrations/alfa-max',r=>{
    if(failNext){failNext--;return r.fulfill({status:503,contentType:'application/json',body:'{"detail":"Synthetic temporary failure"}'});}
    return r.fulfill({contentType:'application/json',body:JSON.stringify({configured:true,state:'active',vault_ready:true,crm_configured:true,branches:[{id:1,name:'Test'}],email:'synthetic@example.test',recovery:{phase,enabled:false,credentials_saved:false,configured:preferenceConfigured,recent:[{event:'authorization_check_requested',at:'2026-10-26T13:00:00Z'}]}})});
   });
   const page=await context.newPage();page.setDefaultTimeout(12000);
   await page.clock.install({time:new Date('2026-10-26T14:00:00Z')});
   await page.goto('https://cabinet.example.test/');await page.waitForSelector('.chat-row');
   if(width<720){await page.locator('#more-tab').click();await page.locator('#navigation-options [data-pane="connections"]').click();}
   else await page.locator('#connections-tab').click();
   await page.locator('#connections-settings-tab').click();
   await page.locator('#alfa-max-recovery-setup').click();
   await page.locator('#alfa-max-connect-form input[name="password"]').fill('synthetic-unsent');
   assert.equal(await page.locator('#alfa-max-auto-reconnect').isChecked(),!preferenceConfigured);
   await page.locator('#alfa-max-auto-reconnect').check();
   assert.match(await page.locator('#alfa-max-session-events').textContent(),/Запрошена проверка авторизации/);
   await page.clock.fastForward(16000);
   assert.equal(await page.locator('#alfa-max-connect-form input[name="password"]').inputValue(),'synthetic-unsent');
   assert.equal(await page.locator('#alfa-max-connect-form').isVisible(),true);
   phase='waiting';failNext=1;
   const failed=page.waitForResponse(r=>r.url().endsWith('/integrations/alfa-max')&&r.status()===503);
   await page.clock.fastForward(16000);
   await (await failed).finished();
   await page.evaluate(()=>Promise.resolve());
   await page.clock.fastForward(6000);
   await page.waitForFunction(()=>document.querySelector('#alfa-max-status').textContent.includes('Восстанавливаем'));
   assert.equal(await page.locator('#alfa-max-connect-form input[name="password"]').inputValue(),'synthetic-unsent');
   const geometry=await page.locator('#alfa-max-auto-reconnect').evaluate(el=>({width:el.getBoundingClientRect().width,height:el.getBoundingClientRect().height}));
   assert.ok(geometry.width<80,'Checkbox must not occupy the full form width');
   assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
   checks.push({width,unsent_password_preserved_on_refresh:true,recovery_state_visible:true,disabled_preference_preserved:preferenceConfigured,poll_recovers_after_503:true,geometry});
  }finally{await browser.close();}
 }
 console.log(JSON.stringify({ok:true,checks},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
