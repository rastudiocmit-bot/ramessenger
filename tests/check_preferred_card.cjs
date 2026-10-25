// Synthetic source/target IDs only; every API call is intercepted.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {fixture}=require('./browser_fixture.cjs');

(async()=>{
  for(const [width,theme] of [[1440,'light'],[390,'light'],[320,'dark']]){
    const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
      args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
    try{
      const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
      const data=await fixture(context),page=await context.newPage();page.setDefaultTimeout(10000);
      const selection={mode:'auto',source:'contact-0',contact:'active-family',customer:'9160',branch:'2',
        student:'active-child',reason:'scheduled_lesson',signature:'current-active-card'};
      Object.assign(data.conversations[0],{name:'Наталья Тестовая',parent_name:'Наталья Тестовая',students:['Мария Тестовая','Иван Тестовый'],
        status:'client',family_contact_id:'active-family',card_selection:selection});
      const child=(id,name,customer,balance)=>({id,name,balance,groups:[],subscriptions:[],paid_lesson_count:1,
        cards:[{id:customer,branch_id:'2',branch_name:'Филиал 2',status:'client',subscriptions:[],lessons:[],needs_refresh:false,
          url:'https://example.s20.online/company/2/customer/view?id='+customer}]});
      await context.route('**/chats/*/family*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({
        id:'active-family',source_contact_id:'contact-0',selection,name:'Наталья Тестовая',phones:['+79990000001'],emails:[],
        today:'2026-10-25',max_connected:true,students:[child('sibling','Тестовый Иван','9162','900.00'),child('active-child','Тестовая Мария','9160','487.50')],
        cache:{ready:true,refreshing:false,auto_refresh:true,updated_at:'2026-10-25T16:00:00Z',sources:2,cached_sources:2}})}));
      await page.goto('https://cabinet.example.test/');await page.waitForSelector('.chat-row');
      await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
      assert.equal(await page.locator('.chat-row').first().locator('.row-name').textContent(),'Наталья Тестовая');
      if(width<720)await page.locator('.chat-select').first().click();
      await page.locator('#chat-person-open').click();await page.waitForSelector('.family-stats');
      assert.equal(await page.locator('#chat-details-name').textContent(),'Наталья Тестовая');
      assert.equal(await page.locator('#chat-details-student h3').textContent(),'Тестовая Мария');
      assert.equal(await page.locator('.family-stats strong').first().textContent().then(s=>s.replace(/\s/g,'')),'487,5₽');
      assert.match(await page.locator('.family-kid-head a').getAttribute('href'),/id=9160$/);
      assert.equal(await page.locator('.family-kid').count(),2);
      assert.equal(await page.locator('.family-kid[aria-pressed="true"]').textContent().then(s=>s.startsWith('Мария')),true);
      const line=await page.locator('#chat-details-sync').evaluate(e=>({height:e.getBoundingClientRect().height,line:parseFloat(getComputedStyle(e).lineHeight)}));
      assert.ok(line.height<=line.line+1);
      if(process.env.CM_TEST_SCREENSHOT_DIR){
        fs.mkdirSync(process.env.CM_TEST_SCREENSHOT_DIR,{recursive:true});
        await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOT_DIR,`preferred-card-${width}-${theme}.png`),fullPage:true});
      }
      await page.locator('.family-kid').first().click();
      assert.equal(await page.locator('#chat-details-student h3').textContent(),'Тестовый Иван');
      assert.equal(await page.locator('.family-stats strong').first().textContent().then(s=>s.replace(/\s/g,'')),'900₽');
      assert.match(await page.locator('.family-kid-head a').getAttribute('href'),/id=9162$/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
      assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
      assert.equal(data.mutations.filter(c=>c.path.endsWith('/messages')||c.path.endsWith('/handled')).length,0);
      console.log(JSON.stringify({width,theme,preferredCustomer:'9160',siblings:2,errors:[]}));
    }finally{await browser.close();}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
