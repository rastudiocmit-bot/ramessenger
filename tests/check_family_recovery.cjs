// Synthetic API only: the actual browser must recover a first-load outage and keep a good card.
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{
  for(const width of [1440,390]){
   const context=await browser.newContext({viewport:{width,height:900},reducedMotion:'reduce'});
   const data=await fixture(context),page=await context.newPage();page.setDefaultTimeout(12000);
   let failures=1,reads=0;
   await context.route('**/chats/*/family*',route=>{
    reads++;
    if(failures-->0)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Сервис временно недоступен'})});
    return route.fallback();
   });
   await page.goto('https://cabinet.example.test/');await page.waitForSelector('.chat-row');
   if(width<720)await page.locator('.chat-select').first().click();
   await page.locator('#chat-person-open').click();
   await page.getByText('Сведения временно недоступны.',{exact:false}).waitFor();
   assert.equal(await page.locator('#chat-details-name').textContent(),data.conversations[0].name);
   await page.waitForSelector('.family-stats');assert.ok(reads>=3,'automatic retry reads cache and requests due work');
   const before=await page.locator('.family-stats').textContent();
   failures=1;
   await page.locator('#chat-details-dialog [data-close]').click();
   await page.locator('#chat-person-open').click();
   await page.waitForFunction(()=>/Сохранённая карточка/.test(document.querySelector('#chat-details-sync').textContent));
   assert.equal(await page.locator('.family-stats').textContent(),before);
   assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
   await page.close();await context.close();
  }
  console.log('Family recovery: desktop and mobile retry automatically and retain cached facts');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
