// Narrow phone layout and non-blocking history while recognition is pending.
// Only a fake microphone and synthetic, intercepted API requests are used.
const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');
const {chromium}=require('playwright');const {fixture}=require('./browser_fixture.cjs');
(async()=>{
  const checks=[];
  for(const width of [320,390]){
    const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
      args:['--no-sandbox','--disable-dev-shm-usage','--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream','--autoplay-policy=user-gesture-required',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
    try{
      const context=await browser.newContext({viewport:{width,height:844},permissions:['microphone'],isMobile:true,hasTouch:true});
      const data=await fixture(context,'operator',['messages'],{voice:true});
      const page=await context.newPage();page.setDefaultTimeout(10000);
      await page.goto('https://cabinet.example.test/');await page.locator('.chat-select').first().tap();
      await page.locator('#chat-voice:not([hidden])').waitFor();
      let pending;
      await context.route('**/voice/transcribe',route=>{pending=route;});
      await page.locator('#chat-voice').tap();await page.waitForSelector('#chat-voice-panel[data-phase="recording"]');
      await page.waitForTimeout(800);await page.locator('#chat-voice-done').tap();
      await page.waitForSelector('#chat-voice-panel[data-phase="working"]');
      assert.equal(await page.evaluate(()=>document.documentElement.classList.contains('voice-holding')),false);
      assert.equal(await page.locator('#voice-hold').isVisible(),false);
      assert.equal(await page.locator('#chat-input').isVisible(),true);
      // History remains available while the server response is outstanding.
      const bubble=page.locator('.chat-bubble').first();await bubble.tap();
      await pending.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Сервис распознавания временно недоступен.'})});
      await page.waitForSelector('#chat-voice-panel[data-phase="error"]');
      const layout=await page.evaluate(()=>{
        const panel=document.querySelector('#chat-voice-panel'),r=panel.getBoundingClientRect();
        return{left:r.left,right:r.right,position:getComputedStyle(panel).position,width:document.documentElement.scrollWidth,
          retry:document.querySelector('#chat-voice-retry').getBoundingClientRect().width};
      });
      assert.equal(layout.position,'static');assert.ok(layout.left>=0&&layout.right<=width);assert.equal(layout.width,width);assert.ok(layout.retry>=44);
      assert.equal(await page.locator('#toast,.toast').count(),0);
      if(process.env.CM_TEST_SCREENSHOTS){fs.mkdirSync(process.env.CM_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOTS,`voice-inline-error-${width}.png`)});}
      await page.locator('#chat-voice-cancel').tap();await page.waitForSelector('#chat-voice-panel',{state:'hidden'});
      assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);checks.push({width,layout,noToasts:true});
    }finally{await browser.close();}
  }
  console.log(JSON.stringify({checks,ok:true},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
