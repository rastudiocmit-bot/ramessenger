// Synthetic cards only; no live CRM requests or customer data.
const assert=require('node:assert/strict');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');

async function scenario(width){
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
  args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const context=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720,reducedMotion:'reduce'});
  const data=await fixture(context,'owner',['messages']);
  let needsReview=true;
  await context.route('**/chats/*/family*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({
   id:'contact-0',source_contact_id:'contact-0',name:'Тестовый родитель',phones:[],emails:[],today:'2026-10-26',
   needs_review:needsReview,review_reasons:needsReview?[{code:'customer_name_conflict',text:'Различаются ФИО ребёнка в филиалах.',customer_ids:['9160'],branch_ids:['1','2']}]:[],
   cache:{ready:true,auto_refresh:false,refreshing:false,sources:1,cached_sources:1,updated_at:'2026-10-26T03:00:00Z'},
   students:[{id:'student-0',name:'Тестовый ребёнок',balance:'487.50',paid_lesson_count:1,subscriptions:[],groups:[],
    cards:[{id:'9160',branch_id:'1',branch_name:'Тестовый филиал',status:'client',lessons:[],subscriptions:[]}]}]
  })}));
  const page=await context.newPage();page.setDefaultTimeout(12000);
  await page.goto('http://cabinet.example.test/?view=messages');
  if(width<720){await page.waitForSelector('.chat-row');await page.locator('.chat-select').first().click();}
  await page.waitForSelector('.chat-bubble');await page.locator('#chat-person-open').click();
  const badge=page.locator('#chat-details-badges').getByText('Нужна проверка в CRM',{exact:true});
  await badge.waitFor({state:'visible'});
  const title=await badge.getAttribute('title');
  assert.match(title,/Различаются ФИО/);assert.match(title,/ID AlfaCRM: 9160/);assert.match(title,/Филиалы: 1, 2/);
  needsReview=false;
  await page.locator('[data-close="chat-details-dialog"]').click();
  await page.waitForFunction(()=>!document.querySelector('#chat-details-dialog').open);
  await page.locator('#chat-person-open').click();
  await badge.waitFor({state:'detached'});
  assert.match(await page.locator('#chat-details-name').textContent(),/Тестовый родитель/);
  assert.match(await page.locator('.family-stats').textContent(),/487,5/);
  assert.match(await page.locator('#chat-details-badges').textContent(),/Клиент/);
  assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);assert.equal(data.mutations.length,0);
  return {width,reason_with_customer_and_branches:true,resolved_badge_removed:true,card_balance_retained:true};
 }finally{await browser.close();}
}
(async()=>{const checks=[];for(const width of [1440,390])checks.push(await scenario(width));console.log(JSON.stringify({checks,ok:true},null,2));})().catch(error=>{console.error(error);process.exitCode=1;});
