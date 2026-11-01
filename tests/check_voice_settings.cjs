// Synthetic settings only. No requests reach a speech or model provider.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const checks=[];
 for(const width of [1440,1024,390,320]){
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
   const coreCards=page.locator('.gateway-primary-grid>.gateway-flat');assert.equal(await coreCards.count(),2);
   for(const id of ['ai-cloud-card','ai-deepseek-card']){
    const card=page.locator('#'+id);assert.equal(await card.locator('details').count(),0);
    assert.equal(await card.locator('[name=api_key]').isVisible(),true);
    assert.equal(await card.locator('button[type=submit]').isVisible(),true);
   }
   assert.equal(await page.locator('#ai-settings-form [name=folder_id]').isVisible(),true);
   const coreLayout=await coreCards.evaluateAll(nodes=>nodes.map(e=>({height:e.getBoundingClientRect().height,top:e.getBoundingClientRect().top,left:e.getBoundingClientRect().left,overflow:e.scrollWidth-e.clientWidth})));
   assert.ok(Math.abs(coreLayout[0].height-coreLayout[1].height)<1,'Yandex and DeepSeek heights match at '+width+': '+JSON.stringify(coreLayout));
   assert.ok(coreLayout.every(e=>e.overflow<=1),'no card overflow at '+width);
   if(width>1000)assert.ok(Math.abs(coreLayout[0].top-coreLayout[1].top)<1,'cards share one row');
   else assert.ok(coreLayout[1].top>coreLayout[0].top+coreLayout[0].height,'cards wrap independently');
   await page.locator('#ai-cloud-card .reveal').click();assert.equal(await page.locator('#ai-settings-form [name=api_key]').getAttribute('type'),'text');
   await page.locator('#ai-cloud-card .reveal').click();
   await page.locator('#ai-deepseek-card .reveal').click();assert.equal(await page.locator('#ai-deepseek-form [name=api_key]').getAttribute('type'),'text');
   await page.locator('#ai-deepseek-card .reveal').click();
   await page.locator('.gateway-primary-grid').screenshot({path:'/tmp/cm-primary-cards-'+width+'.png'});
   await page.locator('#ai-settings-form [name=folder_id]').fill('b1g-synthetic-folder');
   await page.locator('#ai-settings-form [name=api_key]').fill('AQVN-synthetic-layout-key');
   await page.locator('#ai-settings-form [type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('#ai-settings-form [name=api_key]').value==='');
   assert.equal(await page.locator('#ai-settings-form [name=folder_id]').inputValue(),'b1g-synthetic-folder');
   const cloudSave=data.mutations.filter(m=>m.method==='PUT'&&m.path.endsWith('/ai/settings')).at(-1);
   assert.equal(cloudSave.body.folder_id,'b1g-synthetic-folder');assert.equal(cloudSave.body.api_key,'AQVN-synthetic-layout-key');
   await page.locator('#ai-verify').click();await page.waitForFunction(()=>document.querySelector('#ai-yandex-stt-status').textContent==='Работает');
   await page.locator('#ai-yandex-tts-verify').click();await page.waitForFunction(()=>document.querySelector('#ai-yandex-tts-status').textContent==='Синтез работает');
   await page.locator('#ai-deepseek-form [name=api_key]').fill('sk-synthetic-separate-layout-key');
   await page.locator('#ai-deepseek-form [type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('#ai-deepseek-form [name=api_key]').value==='');
   assert.equal(data.mutations.filter(m=>m.path.endsWith('/ai/deepseek')).at(-1).body.api_key,'sk-synthetic-separate-layout-key');
   await page.locator('#ai-deepseek-verify').click();await page.waitForFunction(()=>document.querySelector('#ai-deepseek-check-status').textContent==='Работает');
   assert.equal(await page.locator('.gateway-primary-grid details').count(),0);


   assert.equal(await page.locator('#ai-voice-options-form [name=voice_max_seconds]').inputValue(),'180');
   await page.locator('#ai-voice-options-form [name=voice_max_seconds]').fill('90');
   await page.locator('#ai-voice-options-form button[type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('#voice-active-summary').textContent.includes('90 сек.'));
   assert.equal(await page.locator('#alfa-form').isVisible(),false);
   assert.equal(await page.locator('#ai-agent-new').isVisible(),false);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`voice-settings-${width}.png`)});}
   await page.locator('#connections-agents-tab').click();await page.locator('[data-agent-purpose=voice_input]').click();await page.locator('#ai-agent-new').click();
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
