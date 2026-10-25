// A shared CustomerTariff is one purchase even when two branch cards contain it.
// All names and IDs below are synthetic; no working CRM requests are sent.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');

(async()=>{
  const checked=[];
  for(const width of [1440,390]) {
    const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
      args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
    try {
      const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
      const data=await fixture(context), page=await context.newPage();page.setDefaultTimeout(10000);
      const subscription={id:40,customer_id:'7',tariff_id:9,name:'Абонемент 6450/8/180 Художественная школа (10–15 лет)',
        balance:'8062.50',begin:'2026-10-01',end:'2027-07-04',archived:false,
        branches:[{id:'2',name:'ЦМИТ «Развивайся», ул. Октябрьской Революции'}],
        branch_name:'ЦМИТ «Развивайся», ул. Октябрьской Революции'};
      const cards=[1,2].map(branch=>({id:'7',branch_id:String(branch),branch_name:'Филиал '+branch,status:'client',
        balance:'8062.50',paid_lesson_count:9,subscriptions:[subscription],lessons:[],needs_refresh:false}));
      const student={id:'student-0',name:'Елизавета Тестовая',balance:'8062.50',paid_lesson_count:9,
        subscriptions:[subscription],cards,groups:[]};
      await context.route('**/chats/*/family*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({
        id:'contact-0',name:'Тестовая семья',today:'2026-10-25',phones:[],emails:[],students:[student],
        cache:{ready:true,refreshing:true,auto_refresh:true,updated_at:'2026-10-25T08:00:00Z',
          lessons_updated_at:'2026-10-25T07:55:00Z',lessons_history_updated_at:'2026-10-24T12:00:00Z',
          error:'Не удалось обновить занятия. Баланс и абонементы обновляются отдельно.'}})}));
      const open=async()=>{
        await page.goto('https://cabinet.example.test/');await page.waitForSelector('.chat-row');
        if(width<720)await page.locator('.chat-select').first().click();
        await page.locator('#chat-person-open').click();await page.waitForSelector('.family-stats');
      };
      await open();
      const stats=page.locator('.family-stats .family-stat strong');
      assert.equal((await stats.nth(0).textContent()).replace(/\s/g,''),'8062,5₽');
      assert.equal(await stats.nth(1).textContent(),'9 занятий');
      assert.equal(await stats.nth(2).textContent(),'1');
      assert.equal(await page.locator('.family-item-value').count(),1);
      assert.match(await page.locator('#chat-details-sync').textContent(),/Баланс и абонементы:.*Занятия:.*Год занятий проверен/);
      assert.match(await page.locator('.family-item-main').first().textContent(),/Октябрьской/);
      assert.doesNotMatch(await page.locator('.family-item-main').first().textContent(),/Гражданская/);
      assert.equal(await page.locator('.family-scroll').evaluate(e=>e.scrollWidth<=e.clientWidth+1),true);
      // An independent purchase of the same tariff must remain visible.
      student.subscriptions.push({...subscription,id:41});
      await open();assert.equal(await stats.nth(2).textContent(),'2');
      assert.equal(await page.locator('.family-item-value').count(),2);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
      assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
      assert.equal(data.mutations.filter(c=>c.path.endsWith('/messages')).length,0);
      checked.push({width,sharedPurchase:1,independentPurchases:2,paidLessons:9});
    }finally{await browser.close();}
  }
  console.log(JSON.stringify({checked},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
