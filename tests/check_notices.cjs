// Real UI with synthetic local-only service states and personal preferences.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{
  const checked=[];
  for(const width of [1440,390,320])for(const theme of ['light','dark']){
   const context=await browser.newContext({viewport:{width,height:900},isMobile:width<720,hasTouch:width<720,reducedMotion:'reduce'}),data=await fixture(context,'owner',['messages','tasks','connections']);
   let phase='otp_required';
   await context.route('**/notification-status',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({crm:{configured:true,status:'active'},max:{configured:true,state:phase,recovery:{phase:phase==='otp_required'?'confirmation_required':'active'}}})}));
   await context.route('**/integrations/alfa-max',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({configured:true,state:phase,vault_ready:true,crm_configured:true,branches:[{id:'1',name:'Центральный'}]})}));
   const page=await context.newPage();page.setDefaultTimeout(15000);await page.goto('https://cabinet.example.test/');await page.waitForSelector('.chat-bubble');
   await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
   await page.waitForFunction(()=>document.querySelector('.notification-badge').textContent==='1');
   assert.equal(await page.locator('.notice-capsule').isVisible(),false,'calm by default');
   if(width<720)await page.locator('.chat-select').first().click();
   await page.locator('#notification-bell').waitFor({state:'visible'});
   assert.ok(await page.locator('#notification-bell').evaluate(el=>{const b=el.getBoundingClientRect();return b.left>=0&&b.right<=innerWidth&&b.top>=0;}));
   await page.locator('#notification-bell').click();await page.waitForSelector('#notification-center[open]');
   assert.equal(await page.locator('.notification-row').count(),1);
   assert.match(await page.locator('.notification-row').innerText(),/Нужен код подтверждения MAX/);
   assert.doesNotMatch(await page.locator('.notification-row').innerText(),/раза/);
   if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`notifications-${width}-${theme}.png`)});}
   await page.locator('.notification-row-actions button').filter({hasText:'Ввести код'}).click();
   await page.waitForSelector('#alfa-max-otp-form:not([hidden])');
   assert.equal(await page.locator('#sync-master-connections-tab').getAttribute('aria-selected'),'true');
   assert.equal(await page.locator('#sync-max-config').getAttribute('open'),'');
   await page.locator('#notification-bell').click();assert.equal(await page.locator('.notification-row').count(),1);
   await page.getByRole('button',{name:'Настройки уведомлений',exact:true}).click();await page.waitForSelector('#notification-settings-dialog[open]');
   await page.waitForFunction(()=>document.querySelector('#notification-preferences-status').textContent.includes('Сохранено'));
   assert.equal(await page.locator('#notices-enabled').isChecked(),false);
   await page.locator('#chat-tone-client').selectOption('low');
   await page.waitForFunction(()=>document.querySelector('#notification-preferences-status').textContent==='Настройки сохранены в аккаунте.');
   await page.locator('#notification-task-reminders').uncheck();
   await page.waitForFunction(()=>document.querySelector('#notification-preferences-status').textContent==='Настройки сохранены в аккаунте.');
   assert.equal(data.tasks.personal.reminders,false);
   await page.locator('#notification-service-preview').click();assert.match(await page.locator('#notification-service-example').innerText(),/Спокойный режим/);
   assert.equal(await page.locator('.notice-capsule').isVisible(),false);
   assert.ok(await page.locator('#notification-settings-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
   if(process.env.CM_TEST_SCREENSHOTS){await page.locator('#notification-settings-dialog').evaluate(el=>el.scrollTop=0);await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`notification-settings-${width}-${theme}.png`)});}
   await page.locator('[data-close="notification-settings-dialog"]').click();
   phase='active';await page.reload();await page.waitForSelector('.chat-bubble');
   await page.locator('#notification-bell').click();await page.getByRole('button',{name:'Настройки уведомлений',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#notification-preferences-status').textContent.includes('Сохранено'));
   assert.equal(await page.locator('#chat-tone-client').inputValue(),'low');assert.equal(await page.locator('#notification-task-reminders').isChecked(),false);
   await page.locator('[data-close="notification-settings-dialog"]').click();assert.equal(await page.locator('.notification-badge').isVisible(),false);
   assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);checked.push({width,theme,calm:true,exact_action:true,personal_settings_retained:true});await context.close();
  }
  console.log(JSON.stringify({ok:true,checked},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
