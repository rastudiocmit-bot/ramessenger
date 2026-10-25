// Real layout and persistence checks with intercepted synthetic CRM data only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');

(async () => {
  const checked=[];
  for (const width of [1440,390,320]) {
    const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
      args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
    try {
      const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
      const data=await fixture(context), page=await context.newPage();page.setDefaultTimeout(12000);
      Object.assign(data.conversations[0],{parent_name:'Анна Смирнова',children:[
        {id:'student-2',name:'Михаил Смирнов'},{id:'student-1',name:'Арина Смирнова'}]});
      let report={configured:true,state:'active',syncing:true,messages:82770,stored_messages:88760,conversations:2411,
        history_loaded:true,history_complete:false,backfill_pending:false,unidentified_rows:283,
        client_repair:{complete:true,processed:3741},counters_as_of:new Date().toISOString(),
        streams:Array.from({length:8},(_,i)=>({branch_id:String((i/2|0)+1),direction:i%2?'sent':'receive',
          source_rows:5000,expected_rows:5000,complete:true,verification:{status:'legacy'},new_messages_at:new Date().toISOString()})),
        worker:{live:true,phase:'saving',updated_at:new Date(Date.now()-180000).toISOString()},
        active_review:{state:'complete',processed:1200,last_success_at:new Date().toISOString(),next_at:new Date(Date.now()+21600000).toISOString()}};
      const tasks=[];
      await context.route('**/integrations/alfa-max**',route=>{
        const request=route.request(), p=new URL(request.url()).pathname;
        if(p.endsWith('/sync-progress'))return route.fulfill({contentType:'application/json',body:JSON.stringify({...report,server_at:new Date().toISOString()})});
        if(p.endsWith('/sync')){tasks.push({body:request.postDataJSON(),csrf:request.headers()['x-csrf-token']});return route.fulfill({contentType:'application/json',body:'{"queued":true}'});}
        return route.fallback();
      });
      const navigate=async pane=>{
        if(width>=720)return page.locator(`#${pane}-tab`).click();
        if(pane==='messages')return page.locator('#messages-mobile-tab').click();
        await page.locator('#more-tab').click();await page.locator(`#navigation-options [data-pane="${pane}"]`).click();
      };
      const save=async name=>{
        if(!process.env.CM_INCREMENTAL_SCREENSHOT_DIR)return;
        fs.mkdirSync(process.env.CM_INCREMENTAL_SCREENSHOT_DIR,{recursive:true});
        await page.screenshot({path:path.join(process.env.CM_INCREMENTAL_SCREENSHOT_DIR,`${width}-${name}.png`),animations:'disabled'});
      };
      await page.goto('https://cabinet.example.test/');await page.waitForSelector('.chat-row');
      const first=()=>page.locator('.chat-row[data-id="client-0"]');
      assert.equal(await first().locator('.row-name').textContent(),'Анна Смирнова');
      if(width>=720)await page.locator('#chat-input').fill('Сохранённый черновик');
      const beforeUnread=await first().locator('.avatar-count').textContent();
      const primaryInk=await first().locator('.row-name').evaluate(e=>getComputedStyle(e).color);
      await navigate('profile');await page.locator('#chat-name-order').selectOption('child_first');
      await page.waitForFunction(()=>document.querySelector('#chat-name-save-status').textContent.includes('Сохранено'));
      await save('profile');
      const pref=data.mutations.find(c=>c.path.endsWith('/chats/preferences'));
      assert.deepEqual(pref.body,{name_order:'child_first'});assert.equal(pref.csrf,'synthetic-csrf');
      await navigate('messages');
      assert.equal(await first().locator('.row-name').textContent(),'Арина Смирнова · Михаил Смирнов');
      assert.equal(await first().locator('.row-kids').textContent(),'Анна Смирнова');
      assert.equal(await first().locator('.avatar-count').textContent(),beforeUnread);
      assert.equal(await first().locator('.row-name').evaluate(e=>Number(getComputedStyle(e).fontWeight)>=600),true);
      assert.equal(await first().locator('.row-name').evaluate(e=>getComputedStyle(e).color),primaryInk);
      if(width>=720)assert.equal(await page.locator('#chat-input').inputValue(),'Сохранённый черновик');
      await save('child-first');
      await page.reload();await page.waitForSelector('.chat-row');
      assert.match(await first().locator('.row-name').textContent(),/^Арина/);
      const other=await context.newPage();await other.goto('https://cabinet.example.test/');await other.waitForSelector('.chat-row');
      assert.match(await other.locator('.chat-row[data-id="client-0"] .row-name').textContent(),/^Арина/);await other.close();
      await navigate('connections');await page.waitForFunction(()=>document.querySelector('#alfa-max-progress').dataset.mode==='stale');
      assert.equal(await page.locator('#alfa-max-progress-value').textContent(),'8 из 8');
      assert.equal(await page.locator('.sync-service-card').count(),4);
      assert.equal(await page.locator('.sync-dashboard .sync-spinner').evaluate(e=>getComputedStyle(e).display),'none');
      assert.match(await page.locator('#alfa-max-progress-warning').textContent(),/283/);
      assert.match(await page.locator('.sync-service-card').nth(2).textContent(),/1\s?200/);
      await save('stale-monitor');
      report={...report,syncing:false,worker:{live:false,phase:'waiting',updated_at:new Date().toISOString()},
        streams:report.streams.map((s,i)=>i===7?{...s,live_error_code:'crm_unavailable'}:s)};
      await page.locator('#alfa-max-progress-refresh').click();
      await page.locator('#alfa-max-progress-journals').evaluate(e=>{e.open=true;});
      await page.locator('[data-retry-branch="4"][data-retry-direction="sent"]').click();
      await page.waitForFunction(()=>!document.querySelector('[data-retry-branch="4"]').disabled);
      await page.locator('#alfa-active-review').click();await page.waitForFunction(()=>!document.querySelector('#alfa-active-review').disabled);
      assert.deepEqual(tasks.map(t=>t.body),[{branch_id:4,direction:'sent'},{review_active:true}]);
      assert.ok(tasks.every(t=>t.csrf==='synthetic-csrf'));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
      assert.equal(await page.locator('#pane-connections').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
      assert.equal(data.calls.filter(c=>c.path.endsWith('/family')).length,0);
      assert.equal(data.mutations.filter(c=>c.path.endsWith('/messages')).length,0);
      assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
      checked.push({width,persistedPreference:true,multiChild:true,staleStopped:true,loadedJournals:8,targetedRetry:true});
    }finally{await browser.close();}
  }
  console.log(JSON.stringify({checked},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
