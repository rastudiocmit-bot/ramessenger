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
      data.conversations[0].unread=false;data.conversations[0].unread_count=0;
      const familyCalls=[];
      const subscription={id:40,customer_id:'7',tariff_id:9,name:'Абонемент 6450/8/180 Художественная школа (10–15 лет)',
        balance:'8062.50',begin:'2026-10-01',end:'2027-07-04',archived:false,
        branches:[{id:'2',name:'ЦМИТ «Развивайся», ул. Октябрьской Революции'}],
        branch_name:'ЦМИТ «Развивайся», ул. Октябрьской Революции'};
      const cards=[1,2].map(branch=>({id:'7',branch_id:String(branch),branch_name:'Филиал '+branch,status:'client',
        balance:'8062.50',paid_lesson_count:9,subscriptions:[subscription],lessons:[],needs_refresh:false}));
      const student={id:'student-0',name:'Елизавета Тестовая',balance:'8062.50',paid_lesson_count:9,
        subscriptions:[subscription],cards,groups:[]};
      await context.route('**/chats/*/family*',route=>{
        const url=new URL(route.request().url());familyCalls.push(url);
        const conversation=data.conversations.find(c=>url.pathname.includes('/'+c.id+'/'));
        return route.fulfill({contentType:'application/json',body:JSON.stringify({
        id:conversation.contact_id,name:conversation.name,today:'2026-10-25',phones:[],emails:[],students:[student],
        cache:{ready:true,refreshing:true,auto_refresh:true,updated_at:'2026-10-25T08:00:00Z',
          lessons_updated_at:'2026-10-25T07:55:00Z',lessons_history_updated_at:'2026-10-24T12:00:00Z',
          groups_updated_at:'2026-10-25T07:00:00Z',
          error:'Не удалось обновить занятия. Баланс и абонементы обновляются отдельно.'}})});
      });
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
      const sync=page.locator('#chat-details-sync');
      assert.match(await sync.textContent(),/^Обновлено .+ · есть ошибка обновления$/);
      assert.match(await sync.getAttribute('title'),/Баланс и абонементы:.*Занятия:.*Год занятий проверен.*Группы:/);
      const line=await sync.evaluate(e=>({height:e.getBoundingClientRect().height,lineHeight:parseFloat(getComputedStyle(e).lineHeight),
        whitespace:getComputedStyle(e).whiteSpace}));
      assert.equal(line.whitespace,'nowrap');assert.ok(line.height<=line.lineHeight+1,'footer occupies one line');
      assert.doesNotMatch(await page.locator('.family-item-main').first().textContent(),/Октябрьской/);
      assert.match(await page.locator('.family-item-main').first().textContent(),/до 04\.07\.2027/);
      assert.doesNotMatch(await page.locator('.family-item-main').first().textContent(),/Гражданская/);
      assert.equal(await page.locator('.family-scroll').evaluate(e=>e.scrollWidth<=e.clientWidth+1),true);
      if(width>=720){
        await page.locator('#chat-details-pin').click();
        await page.locator('.chat-row[data-id="client-3"] .chat-select').click();
        await page.waitForFunction(()=>document.querySelector('#chat-details-name').textContent==='Клиент 04');
        await page.waitForSelector('.family-stats');
        assert.equal(familyCalls.at(-1).pathname.endsWith('/client-3/family'),true);
        assert.equal(familyCalls.at(-1).searchParams.has('auto'),false);
        assert.equal(await page.getByRole('button',{name:'Открыть карточку клиента',exact:true}).count(),0);
      }
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
