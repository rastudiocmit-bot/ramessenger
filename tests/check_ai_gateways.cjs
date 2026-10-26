// Synthetic gateway settings only. No requests reach a speech or model provider.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const checks=[];
 for(const width of [1440,390]){
  const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
  try{
   const context=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720,reducedMotion:'reduce'});
   const data=await fixture(context,'owner',['messages','connections'],{voice:true});
   data.ai.settings.deepseek={configured:true,status:'active',key_hint:'test'};
   data.ai.settings.gateways.mimo={configured:true,key_hint:'test',checks:Object.fromEntries(['text','recognition','synthesis'].map(key=>[key,{ok:true,checked_at:'2026-10-26T09:00:00Z'}]))};
   const page=await context.newPage();page.setDefaultTimeout(15000);
   await page.goto('https://cabinet.example.test/?view=connections');
   await page.waitForSelector('.chat-bubble');
   if(width<720){await page.locator('#more-tab').click();await page.locator('#navigation-options [data-pane="connections"]').click();}
   else await page.locator('#connections-tab').click();
   await page.locator('#connections-voice-tab').click();await page.waitForSelector('#ai-cloud-card');
   assert.match(await page.locator('#connections-voice-tab').innerText(),/ИИ-шлюзы/);
   const card=page.locator('[data-gateway="mimo_plan"]');
   assert.equal(await page.locator('#ai-cloud-card #ai-voice-options-form').count(),0);
   await card.locator('[name=api_key]').fill('tp-synthetic-browser-test-only');
   await card.locator('button[type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('[data-gateway="mimo_plan"] .gateway-status').textContent==='Не проверено');
   assert.equal(await card.locator('[name=api_key]').inputValue(),'');
   await card.locator('[data-verify=text]').click();
   await page.waitForFunction(()=>document.querySelector('[data-gateway="mimo_plan"] [data-check="text"]').textContent.includes('Работает'));
   assert.equal(await card.locator('[data-check=recognition]').textContent(),'Не проверено');
   assert.equal(await card.locator('.gateway-status').getAttribute('data-state'),'partial');
   await card.locator('.gateway-verify-all').click();
   await page.waitForFunction(()=>document.querySelector('[data-gateway="mimo_plan"] .gateway-status').dataset.state==='active');
   assert.equal(await card.locator('.gateway-status').textContent(),'Работает');
   assert.equal(await page.locator('[data-gateway="mimo"] .gateway-status').getAttribute('data-state'),'active');
   if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.locator('#gateway-providers-title').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`ai-gateway-providers-${width}.png`)});await card.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`ai-gateway-status-${width}.png`)});}
   await page.locator('#ai-routes-form [name=recognition_provider]').selectOption('mimo_plan');
   assert.equal(await page.locator('#ai-routes-form [name=recognition_language]').inputValue(),'auto');
   await page.locator('#ai-routes-form [name=recognition_language]').selectOption('en');
   await page.locator('#ai-routes-form button[type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('#voice-active-summary').textContent.includes('MiMo Token Plan'));
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`ai-gateways-${width}.png`)});}
   await page.locator('#connections-agents-tab').click();await page.locator('#ai-agent-new').click();
   await page.locator('#ai-agent-form [name=provider][value=mimo_plan]').check();
   assert.equal(await page.locator('#ai-agent-form [name=model]').inputValue(),'mimo-plan-v2.6-pro');
   await page.locator('#ai-agent-form [name=task]').selectOption('text_processing');
   await page.locator('#ai-agent-form button[type=submit]').click();
   await page.waitForSelector('#ai-agent-list .ai-agent');
   await page.locator('#ai-agent-list .ai-agent').click();
   assert.equal(await page.locator('#ai-agent-form [name=provider][value=mimo_plan]').isChecked(),true);
   assert.equal(await page.locator('#ai-agent-form [name=task]').inputValue(),'text_processing');
   await page.locator('#connections-agents-tab').focus();await page.keyboard.press('Home');
   assert.equal(await page.locator('#connections-monitor-tab').getAttribute('aria-selected'),'true');
   await page.keyboard.press('End');assert.equal(await page.locator('#connections-agents-tab').getAttribute('aria-selected'),'true');
   assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
   checks.push({width,gateway_saved:true,capability_checks_independent:true,routes_saved:true,mimo_agent_retained:true,keyboard_tabs:true,no_page_overflow:true});
  }finally{await browser.close();}
 }
 console.log(JSON.stringify({checks,ok:true},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
