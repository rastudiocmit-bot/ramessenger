// Synthetic APIs only; no real CRM reads or sends.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{for(const width of [1440,390,320])for(const theme of ['light','dark']){
  const ctx=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720});const data=await fixture(ctx);
  await ctx.addInitScript(t=>{try{localStorage.setItem('cm-theme',t);}catch(_){}},theme);
  const page=await ctx.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');
  if(width<720){await page.locator('#more-tab').click();await page.locator('#navigation-options [data-pane=connections]').click();}else await page.locator('#navrail [data-pane=connections]').click();
  await page.waitForSelector('#connections-wizard-view:not([hidden])');await page.evaluate(()=>document.fonts.ready);
  const reference=await page.locator('#connections-wizard-view .connection-page-header').boundingBox();
  await page.locator(`${width<720?'#tabbar':'#navrail'} [data-pane=summary]`).click();await page.waitForSelector('#summary-content .kpi');
  assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
  assert.deepEqual(await page.locator('.kpi dd').allTextContents(),['268','3','4 мин','18']);
  const geometry=await page.locator('#pane-summary').evaluate(p=>{const cards=[...p.querySelectorAll('#summary-content>.card')],grid=p.querySelector('.kpi-grid'),box=grid.getBoundingClientRect(),sc=p.querySelector('.scroll');return {x:box.x,width:box.width,columns:getComputedStyle(grid).gridTemplateColumns.split(' ').length,overflow:sc.scrollWidth-sc.clientWidth,cards:cards.map(c=>{const r=c.getBoundingClientRect(),h=c.querySelector('.card-h').getBoundingClientRect(),b=c.querySelector('.card-b').getBoundingClientRect();return {x:r.x,width:r.width,radius:getComputedStyle(c).borderRadius,shadow:getComputedStyle(c).boxShadow,separate:b.top>=h.bottom-1};})};});
  assert.ok(Math.abs(geometry.x-reference.x)<1&&Math.abs(geometry.width-reference.width)<1,'Summary has the Connections content edges: '+JSON.stringify({width,reference,geometry}));
  assert.equal(geometry.columns,width<720?2:4);assert.ok(geometry.overflow<=1,'No sideways scrolling');
  assert.equal(geometry.cards.length,3);assert.ok(geometry.cards.every(c=>Math.abs(c.x-geometry.x)<1&&Math.abs(c.width-geometry.width)<1&&c.radius==='14px'&&c.separate),'Equal card widths, corners and separate headings');
  assert.equal(new Set(geometry.cards.map(c=>c.shadow)).size,1);
  const save=async(name)=>{if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`${name}-${width}-${theme}.png`)});}};
  await save('summary');await page.locator('#pane-summary .scroll').evaluate(e=>e.scrollTop=e.scrollHeight);await save('summary-bottom');
  assert.equal(data.mutations.length,0);assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);console.log(`summary ${width} ${theme}: ok`);await ctx.close();
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
