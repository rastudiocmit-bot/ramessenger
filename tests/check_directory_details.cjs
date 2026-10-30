// Native layout and saved-data checks; every request is synthetic/intercepted.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{chromium}=require('playwright');
const {fixture}=require('./browser_fixture.cjs'),{family}=require('./directory_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{for(const width of [1440,800,390,320])for(const theme of ['light','dark']){
  const ctx=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720}),data=await fixture(ctx);let details=0;
  await ctx.addInitScript(t=>{try{localStorage.setItem('cm-theme',t);}catch(_){}},theme);
  await ctx.route('**/api/workspaces/test/directory?*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({contacts:[{id:'contact-0',name:'Тестовый родитель',status:'client',students:['Первый тестовый ученик','Второй тестовый ученик'],student_count:2,groups:['Python'],group_count:1,phones:['+79000000000'],emails:['parent@example.test'],branches:[{id:'1',name:'Коломна 1'},{id:'2',name:'Коломна 2'}],max_connected:true}],total:1,branches:[],snapshot:'run-1',page:0,has_more:false})}));
  await ctx.route('**/api/workspaces/test/directory/contacts/*',route=>{details++;return route.fulfill({contentType:'application/json',body:JSON.stringify(family())});});
  const page=await ctx.newPage();page.setDefaultTimeout(12000);await page.goto('https://cabinet.example.test/');
  if(width<720){await page.locator('#more-tab').click();await page.locator('#navigation-options [data-pane=clients]').click();}else await page.locator('#navrail [data-pane=clients]').click();
  await page.waitForSelector('#directory-list .contact-row');await page.evaluate(()=>document.fonts.ready);
  assert.equal(await page.locator('#pane-clients details.sync-options').count(),0);assert.equal(await page.locator('#sync-interval').isVisible(),true);assert.equal(await page.locator('#sync-interval').inputValue(),'30');
  assert.match(await page.locator('.directory-list-facts').innerText(),/79000000000|parent@example.test|Python|Коломна 2/);assert.equal(details,0,'List never preloads every full family');
  const geometry=await page.locator('#pane-clients').evaluate(p=>{const sc=p.querySelector('.scroll'),bar=p.querySelector('.sync-bar').getBoundingClientRect();return {overflow:sc.scrollWidth-sc.clientWidth,header:bar.height};});assert.ok(geometry.overflow<=1);if(width>=720)assert.ok(geometry.header<200,'The always-open header stays compact');
  const shot=async(name)=>{if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`${name}-${width}-${theme}.png`)});}};
  await shot('clients');await page.locator('.contact-select').click();await page.waitForSelector('#directory-contact-dialog .directory-student');
  assert.equal(await page.locator('#directory-contact-dialog .directory-student').count(),2);assert.equal(await page.locator('.directory-source-card').count(),3);assert.equal(await page.locator('.directory-subscription').count(),2);assert.equal(await page.locator('.directory-lesson').count(),20);assert.equal(details,1);
  const view=await page.locator('#directory-contact-dialog').evaluate(d=>{const s=d.querySelector('.directory-detail'),r=d.getBoundingClientRect();return {overflow:s.scrollWidth-s.clientWidth,x:r.x,right:r.right,top:r.y,bottom:r.bottom,height:innerHeight};});assert.ok(view.overflow<=1&&view.x>=-1&&view.right<=width+1&&view.top>=-1&&view.bottom<=view.height+1,'The complete family fits: '+JSON.stringify({width,view}));
  await shot('family');await page.locator('.directory-lessons-more').click();await page.locator('.directory-lessons-more').click();assert.equal(await page.locator('.directory-lesson').count(),45);assert.equal(details,1,'More history uses only saved response data');
  await page.locator('#directory-detail').evaluate(e=>e.scrollTop=e.scrollHeight);await shot('family-bottom');await page.locator('[data-close=directory-contact-dialog]').click();
  assert.equal(await page.locator('.contact-select').isVisible(),true);await page.locator('.contact-select').click();await page.waitForSelector('.directory-student');assert.equal(details,1,'A quick reopen uses the saved client response');
  assert.equal(data.mutations.length,0);assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);console.log(`directory details ${width} ${theme}: ok`);await ctx.close();
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
