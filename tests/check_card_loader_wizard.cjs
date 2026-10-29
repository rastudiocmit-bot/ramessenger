// Full-app synthetic APIs: master navigation, defaults, drafts and saved policy.
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000}}),data=await fixture(context),page=await context.newPage();
  let unavailable=true;
  await context.route('**/directory/card-loader',route=>unavailable?route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Сервис временно недоступен'})}):route.fallback());
  await page.goto('https://cabinet.example.test/');await page.locator('#connections-tab').click();
  assert.equal(await page.locator('#connections-wizard-tab').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('#sync-master-status-tab').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('#card-loader-form').isVisible(),false);
  assert.equal(await page.locator('#alfa-form').isVisible(),false);
  assert.equal(await page.locator('#connections-monitor-tab,#connections-monitor-view').count(),0);
  assert.equal(await page.locator('.sync-master-tabs [role=tab]').count(),3);
  const monitor=await page.locator('#connection-monitor-content').elementHandle();
  await page.locator('#sync-master-policy-tab').click();await page.waitForSelector('#card-loader-error');
  assert.equal(await page.locator('[name=finance_interval_minutes]').inputValue(),'30');
  assert.equal(await page.locator('#card-loader-form button[type=submit]').isDisabled(),true);
  unavailable=false;await page.locator('#card-loader-retry').click();
  await page.waitForFunction(()=>!document.querySelector('#card-loader-form button[type=submit]').disabled);
  await page.locator('#card-loader-form .advanced > summary').click();
  await page.locator('[name=requests_per_turn]').fill('6');await page.locator('#card-loader-form button[type=submit]').click();
  await page.waitForFunction(()=>/Настройки сохранены/.test(document.querySelector('#card-loader-status').textContent));
  assert.equal(data.mutations.at(-1).body.requests_per_turn,6);
  await page.locator('[name=finance_interval_minutes]').fill('75');
  const calls=data.calls.filter(c=>c.path.endsWith('/directory/card-loader')).length;
  await page.locator('#sync-master-connections-tab').click();
  await page.locator('#alfa-config > summary').click();
  await page.locator('#alfa-form [name=origin]').fill('https://draft.example.test');
  await page.locator('#sync-master-status-tab').click();await page.locator('#sync-master-connections-tab').click();
  assert.equal(await page.locator('#alfa-form [name=origin]').inputValue(),'https://draft.example.test');
  await page.locator('#messages-tab').click();
  await Promise.all([page.waitForResponse(r=>r.url().endsWith('/integrations/alfa')&&r.request().method()==='GET'),page.locator('#connections-tab').click()]);
  await page.evaluate(()=>new Promise(requestAnimationFrame));
  assert.equal(await page.locator('#alfa-form [name=origin]').inputValue(),'https://draft.example.test');
  await page.locator('#sync-master-policy-tab').click();assert.equal(await page.locator('[name=finance_interval_minutes]').inputValue(),'75');
  await page.locator('#connections-settings-tab').click();await page.locator('#connections-settings-tab').focus();await page.keyboard.press('ArrowLeft');
  assert.equal(await page.locator('#sync-master-policy-panel').isVisible(),true);
  assert.equal(await page.locator('[name=finance_interval_minutes]').inputValue(),'75');
  assert.equal(data.calls.filter(c=>c.path.endsWith('/directory/card-loader')).length,calls);
  assert.equal(await monitor.evaluate(el=>el===document.querySelector('#connection-monitor-content')),true);
  await page.locator('#sync-master-status-tab').focus();await page.keyboard.press('End');
  assert.equal(await page.locator('#sync-master-policy-tab').getAttribute('aria-selected'),'true');
  await context.route('**/integrations/alfa-max',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({configured:true,state:'reauth_required',vault_ready:true,crm_configured:true,branches:[]})}));
  await page.locator('#messages-tab').click();await page.locator('#connections-tab').click();
  assert.equal(await page.locator('#sync-master-policy-panel').isVisible(),true);
  assert.equal(await page.locator('[name=finance_interval_minutes]').inputValue(),'75');
  await page.locator('#card-loader-form button[type=submit]').click();await page.waitForFunction(()=>/Настройки сохранены/.test(document.querySelector('#card-loader-status').textContent));
  await context.unroute('**/integrations/alfa-max');
  await page.reload();await page.locator('#connections-tab').click();
  assert.equal(await page.locator('#sync-master-status-tab').getAttribute('aria-selected'),'true');
  await page.locator('#sync-master-policy-tab').click();await page.waitForFunction(()=>!document.querySelector('#card-loader-form button[type=submit]').disabled);
  assert.equal(await page.locator('[name=finance_interval_minutes]').inputValue(),'75');
  assert.equal(await page.locator('#card-loader-form input:disabled,#card-loader-form select:disabled').count(),0);
  // Scroll offsets belong to each inner view, not to the whole long master.
  await page.locator('[name=finance_interval_minutes]').scrollIntoViewIfNeeded();
  const policyScroll=await page.locator('#pane-connections > .scroll').evaluate(e=>e.scrollTop);
  await page.locator('#sync-master-status-tab').click();await page.locator('#sync-master-policy-tab').click();
  assert.ok(Math.abs(await page.locator('#pane-connections > .scroll').evaluate(e=>e.scrollTop)-policyScroll)<2);
  await page.locator('#sync-master-connections-tab').click();
  const sizes=await page.locator('#wizard-data-settings,#wizard-native-settings').evaluateAll(elements=>elements.map(el=>el.getBoundingClientRect().width));
  assert.ok(Math.abs(sizes[0]-sizes[1])<1);
  assert.equal(await page.locator('#alfa-config').getAttribute('open'),null);
  assert.equal(await page.locator('#sync-max-config').getAttribute('open'),null);
  await page.screenshot({path:'/tmp/compact-sync-connections-desktop.png',animations:'disabled'});
  await page.locator('#sync-master-status-tab').click();await page.locator('#alfa-max-progress').waitFor({state:'visible'});
  assert.equal(await page.locator('[id=connection-monitor-content]').count(),1);
  await page.locator('#pane-connections > .scroll').evaluate(el=>el.scrollTop=0);
  for(const notice of await page.locator('.notice-capsule:not([hidden]) .notice-del').all())await notice.click();
  await page.screenshot({path:'/tmp/compact-sync-status-desktop.png',animations:'disabled'});
  await page.evaluate(()=>document.documentElement.dataset.theme='dark');
  await page.screenshot({path:'/tmp/compact-sync-status-dark.png',animations:'disabled'});
  await page.evaluate(()=>document.documentElement.dataset.theme='light');
  for(const width of [390,320]){
   await page.setViewportSize({width,height:844});
   for(const view of ['status','connections','policy']){
    await page.locator('#sync-master-'+view+'-tab').click();
    assert.ok(await page.locator('#connections-wizard-view').evaluate(el=>el.scrollWidth<=el.clientWidth+1),'overflow '+width+' '+view);
    await page.locator('#pane-connections > .scroll').evaluate(el=>el.scrollTop=0);
    await page.screenshot({path:'/tmp/compact-sync-'+view+'-'+width+'.png',animations:'disabled'});
   }
   await page.locator('#card-loader-form button[type=submit]').scrollIntoViewIfNeeded();
   assert.ok(await page.locator('#card-loader-form button[type=submit]').evaluate(el=>{const b=el.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight;}));
  }
  assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
  console.log('Compact master: three views, drafts, persistence, scroll, keyboard and 1440/390/320px passed');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
