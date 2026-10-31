// Synthetic device/audio check. No provider, call, ASR or customer requests.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const reports=[];
  for(const width of [1440,390]){
   const context=await browser.newContext({viewport:{width,height:900},permissions:['microphone'],reducedMotion:'reduce'}),data=await fixture(context),page=await context.newPage();
   page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');await page.waitForSelector('.chat-bubble');
   // The static entry exists for all fixture roles; open it through the same delegated click.
   await page.locator('[data-phone-devices]').first().evaluate(e=>e.click());await page.waitForSelector('#phone-devices-dialog[open]');
   const before=data.mutations.length;
   await page.locator('[data-phone-check=speaker]').click();await page.waitForFunction(()=>{const a=document.querySelector('#phone-devices-dialog audio');return !a.paused&&a.currentTime>0});
   const tone=await page.locator('#phone-devices-dialog audio').evaluate(async audio=>{const bytes=await(await fetch(audio.src)).arrayBuffer(),ctx=new AudioContext();try{const b=await ctx.decodeAudioData(bytes),d=b.getChannelData(0);return{seconds:b.duration,rms:Math.sqrt(d.reduce((s,v)=>s+v*v,0)/d.length)};}finally{await ctx.close();}});
   assert.ok(tone.rms>.05&&tone.seconds>.9);await page.locator('[data-phone-check=microphone]').click();
   await page.waitForFunction(()=>document.querySelector('[data-phone-check=microphone]').textContent==='Закончить запись');
   await page.waitForTimeout(1200);await page.locator('[data-phone-check=microphone]').click();
   await page.waitForFunction(()=>document.querySelector('.phone-devices-message').textContent.includes('ваш голос'));
   const recording=await page.locator('#phone-devices-dialog audio').evaluate(async audio=>{const b=await(await fetch(audio.src)).blob();return{size:b.size,type:b.type}});assert.ok(recording.size>100);
   const overflow=await page.locator('#phone-devices-dialog').evaluate(e=>e.scrollWidth-e.clientWidth);assert.ok(overflow<=1);
   if(process.env.CM_APPEARANCE_SCREENSHOT_DIR){fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`devices-${width}.png`)});}
   await page.locator('.phone-devices-heading button').click();assert.equal(await page.locator('#phone-devices-dialog audio').getAttribute('src'),null);
   assert.equal(data.mutations.length,before);assert.deepEqual(data.errors,[]);reports.push({width,tone,recording,overflow,remoteMutations:0});await page.close();
  }
  console.log(JSON.stringify({browser:browser.version(),reports},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
