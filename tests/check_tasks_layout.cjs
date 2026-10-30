// Synthetic data; every request is intercepted, with no CRM or delivery traffic.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{for(const width of [1440,390,320])for(const theme of ['light','dark']){
  const ctx=await browser.newContext({viewport:{width,height:900},isMobile:width<720,hasTouch:width<720});const data=await fixture(ctx,'owner',undefined,{tasks:[{id:'sample',title:'Уточнить время пробного занятия и перезвонить родителю',client_label:'Анна Смирнова',due_at:new Date(Date.now()+3600e3).toISOString()}]});
  await ctx.addInitScript(t=>{try{localStorage.setItem('cm-theme',t);}catch(_){}},theme);
  const page=await ctx.newPage();await page.goto('https://cabinet.example.test/?view=messages');
  if(width<720){await page.locator('#more-tab').click();await page.locator('#navigation-options [data-pane=connections]').click();}else await page.locator('#navrail [data-pane=connections]').click();
  await page.waitForSelector('#connections-wizard-view:not([hidden])');await page.evaluate(()=>document.fonts.ready);
  assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
  const connection=await page.locator('#connections-wizard-view').boundingBox();
  if(width<720){await page.locator('#tabbar [data-pane=tasks]').click();}else await page.locator('#navrail [data-pane=tasks]').click();
  await page.waitForSelector('#pane-tasks .task-row');
  const list=await page.locator('#tasks-list-panel').boundingBox(),card=await page.locator('#pane-tasks .task-card').first().boundingBox();
  assert.ok(Math.abs(list.x-connection.x)<1&&Math.abs(list.width-connection.width)<1,'Tasks and Connections share edges: '+JSON.stringify({width,list,connection}));
  const save=async(name)=>{if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`${name}-${width}-${theme}.png`)});}};
  const toolbar=await page.locator('.task-list-toolbar').evaluate(e=>{const a=e.querySelector('.task-scope').getBoundingClientRect(),b=e.querySelector('.task-new').getBoundingClientRect();return {sameRow:Math.abs(a.top-b.top)<1&&Math.abs(a.height-b.height)<1,separate:a.right+4<=b.left,overflow:e.scrollWidth-e.clientWidth};});
  assert.ok(toolbar.sameRow&&toolbar.separate&&toolbar.overflow<=1,'New task shares the scope row without overlap: '+JSON.stringify({width,...toolbar}));
  await save('list');await page.locator('.task-new').click();await page.waitForSelector('#task-dialog[open]');await page.locator('#task-dialog [data-task-close]').click();
  await page.locator('#tasks-settings-tab').click();await page.waitForFunction(()=>!document.querySelector('.task-settings-save').disabled);
  await page.locator('#tasks-settings-tab').press('Home');assert.equal(await page.locator('#tasks-list-tab').evaluate(e=>e===document.activeElement),true);
  await page.locator('#tasks-list-tab').press('End');assert.equal(await page.locator('#tasks-settings-tab').evaluate(e=>e===document.activeElement),true);
  const personal=await page.locator('.task-personal-settings').boundingBox(),studio=await page.locator('.task-studio-settings').boundingBox();
  assert.ok(Math.abs(personal.width-card.width)<1&&Math.abs(studio.width-card.width)<1&&Math.abs(personal.x-card.x)<1,'Both settings cards use full page width');
  const bad=await page.locator('#pane-tasks').evaluate(p=>{const sc=p.querySelector('.scroll');return {overflow:sc.scrollWidth-sc.clientWidth};});
  assert.ok(bad.overflow<=1,'No overflow or header overlap: '+JSON.stringify({width,...bad}));
  await page.locator('[name=day_start]').fill('09:00');await page.locator('#tasks-list-tab').click();await page.locator('#tasks-settings-tab').click();assert.equal(await page.locator('[name=day_start]').inputValue(),'09:00');
  await page.locator('#pane-tasks .scroll').evaluate(e=>e.scrollTop=0);await save('settings');
  await page.locator('.task-settings-save').click();await page.waitForSelector('.task-settings-status:has-text("Настройки сохранены")');await save('saved');
  assert.ok(data.mutations.some(m=>m.path.endsWith('/tasks/settings')&&m.body.day_start==='09:00'));
  assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);console.log(`tasks layout ${width} ${theme}: ok`);await ctx.close();
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
