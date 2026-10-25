// All API calls are intercepted. This never marks production conversations.
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
      const data=await fixture(context),page=await context.newPage();page.setDefaultTimeout(12000);
      data.conversations[2].status='archive';
      let balance='8062.50',prepares=0,confirms=0;
      const subscription={id:40,name:'Художественная школа',balance:'8062.50',begin:'2026-10-01',end:'2027-07-04',
        branch_name:'Центральный, ул. Пушкина'};
      await context.route('**/chats/*/family*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({
        id:'contact-0',name:'Тестовая семья',today:'2026-10-25',phones:[],emails:[],
        students:[{id:'student-0',name:'Тестовый ребёнок',balance,paid_lesson_count:9,subscriptions:[subscription],groups:[],
          cards:[{id:'7',branch_id:'1',branch_name:'Центральный, ул. Пушкина',status:'client',subscriptions:[subscription],lessons:[],needs_refresh:false}]}]})}));
      await context.route('**/chats/handled/*',async route=>{
        assert.equal(route.request().method(),'POST');
        if(route.request().url().endsWith('/prepare')){
          prepares++;
          return route.fulfill({contentType:'application/json',body:JSON.stringify({operation_id:'00000000-0000-0000-0000-000000000801',
            client_count:71,conversation_count:72,message_count:93,expires_at:new Date(Date.now()+600000).toISOString()})});
        }
        confirms++;
        assert.deepEqual(route.request().postDataJSON(),{operation_id:'00000000-0000-0000-0000-000000000801',confirmed:true});
        data.conversations.forEach((c,i)=>{c.unread=i===0;c.unread_count=i===0?1:0;c.revision++;});
        return route.fulfill({contentType:'application/json',body:JSON.stringify({processed:72,already_handled:0,skipped:0,messages:93})});
      });
      const open=async()=>{
        await page.goto('https://cabinet.example.test/');await page.waitForSelector('.chat-row');
        await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
      };
      const screenshot=async name=>{
        if(!process.env.CM_TEST_SCREENSHOT_DIR)return;
        fs.mkdirSync(process.env.CM_TEST_SCREENSHOT_DIR,{recursive:true});
        await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOT_DIR,`${name}-${width}-${theme}.png`),fullPage:true});
      };
      await open();
      const colors=await page.evaluate(()=>Object.fromEntries(['client','lead','archive'].map(status=>[
        status,getComputedStyle(document.querySelector('.chat-row .ava.'+status)).backgroundColor])));
      const rgb=color=>color.match(/[\d.]+/g).map(Number);
      const [cr,cg,cb]=rgb(colors.client),[lr,lg,lb]=rgb(colors.lead),[ar,ag,ab]=rgb(colors.archive);
      assert.ok(cg>cr&&cg>cb,'clients have a pastel green circle');
      assert.ok(lb>lr&&lb>lg,'leads have a pastel blue circle');
      assert.ok(ar>ag&&ar>ab,'archived clients have a pastel red circle');
      if(width<720)await page.locator('.chat-select').first().click();
      await page.waitForSelector('.chat-bubble.system.sys');
      const glass=await page.locator('.chat-bubble.system.sys').first().evaluate(e=>({background:getComputedStyle(e).backgroundColor,blur:getComputedStyle(e).backdropFilter}));
      assert.equal(glass.background,'rgba(255, 255, 255, 0.1)');assert.equal(glass.blur,'blur(10px)');
      await page.locator('.chat-bubble.system.sys .system-toggle').first().click();
      await page.locator('.chat-bubble.system.sys .system-body').first().waitFor({state:'visible'});
      const expanded=await page.locator('.chat-bubble.system.sys').first().evaluate(e=>({
        background:getComputedStyle(e).backgroundColor,blur:getComputedStyle(e).backdropFilter,
        textFilter:getComputedStyle(e.querySelector('.system-body')).filter,opacity:getComputedStyle(e).opacity}));
      assert.deepEqual(expanded,{...glass,textFilter:'none',opacity:'1'});
      await screenshot('system-glass');
      await page.locator('#chat-person-open').click();await page.waitForSelector('.family-stats');
      const sub=page.locator('.family-part').filter({has:page.locator('.family-label',{hasText:/^Абонементы$/})});
      assert.doesNotMatch(await sub.textContent(),/Пушкина|Центральный/);
      assert.match(await sub.textContent(),/до 04\.07\.2027/);
      const branches=page.locator('.family-part').filter({has:page.locator('.family-label',{hasText:/^Филиал/})});
      assert.match(await branches.textContent(),/Пушкина/);assert.doesNotMatch(await branches.textContent(),/Клиент|Лид|Архивный/);
      assert.equal(await page.locator('.family-stat.positive').evaluate(e=>getComputedStyle(e).backgroundColor),colors.client);
      assert.equal(await page.locator('.family-chip.client').evaluate(e=>getComputedStyle(e).backgroundColor),colors.client);
      await screenshot('card-positive');
      balance='-120.75';await open();
      if(width<720)await page.locator('.chat-select').first().click();
      await page.locator('#chat-person-open').click();await page.waitForSelector('.family-stat.negative');
      const debt=await page.locator('.family-stat.negative').evaluate(e=>({background:getComputedStyle(e).backgroundColor,ink:getComputedStyle(e.querySelector('strong')).color}));
      const [dr,dg,db]=rgb(debt.background),[ir,ig,ib]=rgb(debt.ink);
      assert.ok(dr>dg&&dr>db&&ir>ig&&ir>ib,'negative balance is pastel red with red text');
      await screenshot('card-negative');
      await page.locator('#chat-details-dialog [data-close="chat-details-dialog"]').click();
      await page.waitForSelector('#chat-details-dialog:not([open])',{state:'attached'});
      const handled=page.locator('#chat-handled'),box=await handled.boundingBox();
      await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();
      await page.waitForSelector('#chat-bulk-dialog[open]');await page.mouse.up();
      assert.equal(confirms,0);assert.equal(prepares,1);
      assert.match(await page.locator('#chat-bulk-summary').textContent(),/Клиентов: 71.*Диалогов: 72/);
      assert.equal(await page.evaluate(()=>document.activeElement.id),'chat-bulk-cancel');
      await page.locator('#chat-bulk-cancel').click();
      assert.equal(confirms,0);
      await handled.focus();await page.keyboard.press('Shift+Enter');await page.waitForSelector('#chat-bulk-dialog[open]');
      await screenshot('bulk-confirmation');
      await page.locator('#chat-bulk-confirm').click();await page.waitForSelector('#chat-bulk-dialog:not([open])',{state:'attached'});
      assert.equal(confirms,1);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
      assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
      assert.equal(data.mutations.filter(c=>c.path.endsWith('/messages')).length,0);
      assert.equal(data.mutations.filter(c=>c.path.endsWith('/handled')).length,0,'long press must not mark the individual chat');
      console.log(JSON.stringify({width,theme,colors,glass,bulk:{prepares,confirms},errors:[]}));
    }finally{await browser.close();}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
