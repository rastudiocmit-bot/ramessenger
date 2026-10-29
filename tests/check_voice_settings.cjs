// Synthetic settings only. No requests reach a speech or model provider.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const checks=[];
 for(const width of [1440,390]){
  const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
  try{
   const context=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720,reducedMotion:'reduce'});
   const data=await fixture(context,'owner',['messages','connections'],{voice:true});
   const page=await context.newPage();page.setDefaultTimeout(15000);
   await page.goto('https://cabinet.example.test/?view=connections');
   await page.waitForSelector('.chat-bubble');
   if(width<720){await page.locator('#more-tab').click();await page.locator('#navigation-options [data-pane="connections"]').click();}
   else await page.locator('#connections-tab').click();
   await page.locator('#connections-voice-tab').click();await page.waitForSelector('#ai-cloud-card');
   assert.equal(await page.locator('#ai-voice-options-form [name=voice_max_seconds]').inputValue(),'180');
   await page.locator('#ai-voice-options-form [name=voice_max_seconds]').fill('90');
   await page.locator('#ai-voice-options-form button[type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('#voice-active-summary').textContent.includes('90 сек.'));
   assert.equal(await page.locator('#alfa-form').isVisible(),false);
   assert.equal(await page.locator('#ai-agent-new').isVisible(),false);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`voice-settings-${width}.png`)});}
   await page.locator('#connections-agents-tab').click();await page.locator('#ai-agent-new').click();
   await page.locator('#ai-agent-form [name=provider][value=deepseek]').check();
   assert.equal(await page.locator('#ai-agent-form [name=model]').inputValue(),'deepseek-flash');
   assert.equal(await page.locator('#ai-cloud-card').isVisible(),false);
   await page.locator('#ai-agent-form button[type=submit]').click();
   await page.waitForSelector('#ai-agent-list .ai-agent');
   await page.locator('#ai-agent-list .ai-agent').click();
   assert.equal(await page.locator('#ai-agent-form [name=provider][value=deepseek]').isChecked(),true);
   await page.locator('#connections-agents-tab').focus();await page.keyboard.press('Home');
   assert.equal(await page.locator('#connections-wizard-tab').getAttribute('aria-selected'),'true');
   await page.keyboard.press('End');assert.equal(await page.locator('#connections-agents-tab').getAttribute('aria-selected'),'true');
   assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
   checks.push({width,duration_saved:true,deepseek_selected_and_retained:true,keyboard_tabs:true,no_page_overflow:true});
  }finally{await browser.close();}
 }
 console.log(JSON.stringify({checks,ok:true},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
