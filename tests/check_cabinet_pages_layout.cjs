// All requests intercepted using synthetic data. No live CRM, password or access changes.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{for(const width of JSON.parse(process.env.CM_TEST_WIDTHS||'[1440,800,390,320]'))for(const theme of ['light','dark']){
  const ctx=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720});const data=await fixture(ctx);
  await ctx.addInitScript(t=>{try{localStorage.setItem('cm-theme',t);}catch(_){}},theme);
  const page=await ctx.newPage();page.setDefaultTimeout(12000);await page.goto('https://cabinet.example.test/');
  const go=async pane=>{if(width<720){await page.locator('#more-tab').click();await page.locator(`#navigation-options [data-pane=${pane}]`).click();}else await page.locator(`#navrail [data-pane=${pane}]`).click();await page.waitForSelector(`#pane-${pane}:not([hidden])`);await page.locator(`#pane-${pane} .scroll`).evaluate(e=>e.scrollTop=0);};
  await go('connections');await page.waitForSelector('#connections-wizard-view:not([hidden])');await page.evaluate(()=>document.fonts.ready);
  const reference=await page.locator('#connections-wizard-view .connection-page-header').boundingBox();
  const save=async(name)=>{if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`${name}-${width}-${theme}.png`)});}};
  const targets={clients:'.directory-list-panel,.directory-toolbar,.sync-bar',team:'.table-card',overview:'.card',security:'.card',profile:'.cabinet-content>.card,.cabinet-content>.group'};
  for(const [pane,selector] of Object.entries(targets)){
   await go(pane);
   if(pane==='clients')await page.waitForSelector('#directory-list .contact-row');
   if(pane==='team')await page.waitForSelector('#members-list .member-row');
   if(pane==='overview')await page.waitForFunction(()=>document.querySelector('#organization-branch-count').textContent==='2');
   if(pane==='security')await page.waitForSelector('#sessions-list .session-row');
   assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
   const geometry=await page.locator(`#pane-${pane}`).evaluate((p,selector)=>{const sc=p.querySelector('.scroll');return {overflow:sc.scrollWidth-sc.clientWidth,cards:[...p.querySelectorAll(selector)].filter(e=>e.getClientRects().length).map(c=>{const r=c.getBoundingClientRect(),h=c.querySelector('.card-h'),b=c.querySelector('.card-b');return {x:r.x,width:r.width,radius:getComputedStyle(c).borderRadius,shadow:getComputedStyle(c).boxShadow,separate:!h||!b||b.getBoundingClientRect().top>=h.getBoundingClientRect().bottom-1};})};},selector);
   assert.ok(geometry.cards.length>0);assert.ok(geometry.overflow<=1,'No sideways scroll: '+JSON.stringify({pane,width,...geometry}));
   assert.ok(geometry.cards.every(c=>Math.abs(c.x-reference.x)<1&&Math.abs(c.width-reference.width)<1&&c.radius==='14px'&&c.separate),'Same Connections card edges and styles: '+JSON.stringify({pane,width,reference,geometry}));
   assert.equal(new Set(geometry.cards.map(c=>c.shadow)).size,1);
   await save(pane);
   if(pane==='clients'){
    await page.locator('#directory-status').selectOption('lead');await page.waitForTimeout(400);assert.ok(data.calls.some(c=>c.path.endsWith('/directory')&&c.query.includes('status=lead')),'Filter still requests the selected status');
    await page.locator('#directory-list .contact-select').first().click();await page.waitForSelector('#directory-contact-dialog .student-card');await page.locator('[data-close=directory-contact-dialog]').click();
    assert.equal(await page.locator('#directory-list .contact-select').first().isVisible(),true,'Closing a modal keeps the clients list visible');
   }
   if(pane==='team'){
    await page.getByRole('button',{name:'Действия с сотрудником: Мария Петрова'}).click();await page.waitForSelector('#member-options-dialog[open]');await page.locator('[data-close=member-options-dialog]').click();
    await page.locator('#invite-open').click();await page.waitForSelector('#invitation-dialog[open]');await page.locator('[data-close=invitation-dialog]').first().click();
   }
   if(pane==='overview')assert.equal(await page.locator('#workspace-form [name=name]').inputValue(),'Развивайся');
   if(pane==='security'){
    assert.equal(await page.locator('#password-form input[type=password]').count(),3);await page.locator('#sessions-refresh').click();await page.waitForSelector('#sessions-list .session-row');
   }
   if(pane==='profile'){
    await page.locator('#profile-sound-settings').click();await page.waitForSelector('#notification-settings-dialog[open]');await page.locator('[data-close=notification-settings-dialog]').click();
    const before=await page.locator('html').getAttribute('data-theme');await page.locator('#profile-theme').setChecked(before!=='dark');assert.notEqual(await page.locator('html').getAttribute('data-theme'),before);await page.locator('#profile-theme').setChecked(before==='dark');
    await page.locator('#pane-profile .scroll').evaluate(e=>e.scrollTop=e.scrollHeight);await save('profile-bottom');
   }
  }
  assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);assert.equal(data.mutations.length,0,'Presentation checks must not modify account data');console.log(`cabinet pages ${width} ${theme}: ok`);await ctx.close();
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
