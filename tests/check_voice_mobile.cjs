// Real layout regression for recording/recognition at phone and desktop widths.
// A fake microphone and intercepted responses never contact CRM, MAX or an AI.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');const {fixture}=require('./browser_fixture.cjs');
const frames=page=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
(async()=>{
  const checks=[];
  for(const width of [320,390,1440]){
    const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
      args:['--no-sandbox','--disable-dev-shm-usage','--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream','--autoplay-policy=user-gesture-required',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
    try{
      const context=await browser.newContext({viewport:{width,height:844},permissions:['microphone'],isMobile:width<720,hasTouch:true});
      const data=await fixture(context,'operator',['messages'],{voice:true});
      for(let i=0;i<60;i++)data.messages.push({...data.messages[1],id:'history-'+i,text:`Сообщение ${i}. `+'История клиента. '.repeat(4)});
      let pending;
      await context.route('**/voice/transcribe',route=>{pending=route;});
      const page=await context.newPage();page.setDefaultTimeout(10000);
      await page.goto('https://cabinet.example.test/');await page.locator('.chat-select').first().tap();
      await page.locator('#chat-voice:not([hidden])').waitFor();await frames(page);
      const bounds=()=>page.locator('#chat-compose').boundingBox();
      const stable=(before,after,where)=>{
        assert.ok(Math.abs(before.y-after.y)<=1,`${width} ${where}: top moved ${after.y-before.y}px`);
        assert.ok(Math.abs(before.height-after.height)<=1,`${width} ${where}: height changed ${after.height-before.height}px`);
      };
      const capture=async()=>{
        pending=null;await page.locator('#chat-voice').tap();await page.waitForSelector('#chat-voice-panel[data-phase="recording"]');
        await page.waitForTimeout(650);await page.locator('#chat-voice-done').tap();await page.waitForSelector('#chat-voice-panel[data-phase="working"]');
        assert.equal(await page.evaluate(()=>document.documentElement.classList.contains('voice-holding')),false);
        assert.equal(await page.locator('#voice-hold').isVisible(),false);await frames(page);assert.ok(pending);
      };
      const complete=async payload=>{
        await pending.fulfill({status:200,contentType:'application/json',body:JSON.stringify(payload)});
        await page.waitForSelector('#chat-voice-panel',{state:'hidden'});await frames(page);
      };
      const initial=await bounds();
      await capture();const processing=await bounds();stable(initial,processing,'recognising');
      await complete({text:'',warning:'Речь не распознана. Попробуйте говорить ближе к микрофону.'});
      stable(initial,await bounds(),'empty recognition');assert.equal(await page.locator('#chat-input').inputValue(),'');
      assert.equal(await page.locator('#chat-voice-label').textContent(),'');

      // A real service failure occupies the same row, without a footer shifting
      // the message field. Explicit retry/cancel remain available.
      await capture();await pending.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Сервис распознавания временно недоступен.'})});
      await page.waitForSelector('#chat-voice-panel[data-phase="error"]');await frames(page);stable(initial,await bounds(),'service error');
      assert.equal(await page.locator('#chat-voice-panel').evaluate(e=>getComputedStyle(e).position),'absolute');
      const retryBox=await page.locator('#chat-voice-retry').boundingBox();assert.ok(retryBox.width>=44);
      await page.locator('#chat-voice-cancel').tap();await page.waitForSelector('#chat-voice-panel',{state:'hidden'});await frames(page);stable(initial,await bounds(),'error closed');

      // Voice text has exactly the same geometry as manually typed text. The
      // operator can keep reading an earlier message without an anchor jump.
      const text='Здравствуйте! Завтра занятие в 17:00.';
      await page.locator('#chat-input').fill(text);await page.locator('#chat-input').evaluate(e=>e.blur());await frames(page);const typed=await bounds();
      await page.locator('#chat-input').fill('');await page.locator('#chat-input').evaluate(e=>e.blur());await frames(page);
      await capture();
      await page.locator('#chat-stream').evaluate(e=>{e.scrollTop=800;});await frames(page);
      const anchor=await page.evaluate(()=>{
        const stream=document.querySelector('#chat-stream'),r=stream.getBoundingClientRect();
        const item=[...stream.querySelectorAll('.chat-bubble')].find(e=>e.getBoundingClientRect().top>r.top+30);
        return{id:item.dataset.id,top:item.getBoundingClientRect().top};
      });
      await complete({text});assert.equal(await page.locator('#chat-input').inputValue(),text);stable(typed,await bounds(),'recognised draft');
      const after=await page.locator(`.chat-bubble[data-id="${anchor.id}"]`).evaluate(e=>e.getBoundingClientRect().top);
      assert.ok(Math.abs(after-anchor.top)<=1,`${width}: history anchor moved ${after-anchor.top}px`);
      assert.equal(await page.locator('.voice-hold-card,.voice-hold-bar,#toast,.toast').count(),0);
      assert.equal(data.mutations.filter(m=>m.path.endsWith('/messages')).length,0,'only the operator can send');
      if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`voice-manual-stable-${width}.png`)});}
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
      assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);checks.push({width,emptyTop:initial.y,processingTop:processing.y,historyDrift:after-anchor.top,noAutomaticMessage:true});
    }finally{await browser.close();}
  }
  console.log(JSON.stringify({checks,ok:true},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
