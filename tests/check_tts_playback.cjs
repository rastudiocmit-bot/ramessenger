// Full browser decoding/playback under the actual production CSP; synthetic audio only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
const policySource=fs.readFileSync(path.join(__dirname,'../app/backend/src/center_messenger/http_security.py'),'utf8');
const policy=policySource.slice(policySource.indexOf('b"default-src'),policySource.indexOf('b"frame-ancestors')+100).match(/b"[^"]*"/g).map(s=>s.slice(2,-1)).join('');
(async()=>{
 const checks=[];
 for(const width of [1440,390]){
  const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
  try{
   const context=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720,reducedMotion:'reduce'});
   const csp=process.env.CM_TTS_OLD_CSP?policy.replace("media-src 'self' blob:; ",''):policy;
   const data=await fixture(context,'owner',['messages','connections'],{voice:true,csp});
   Object.assign(data.ai.settings.gateways,{synthesis_provider:'mimo_plan',synthesis_voice:'Dean',synthesis_voices:['Mia','Dean'],
    synthesis_voice_catalog:{yandex:[{id:'marina',title:'Марина · русский'},{id:'john',title:'John · английский'}],mimo_plan:[{id:'Mia',title:'Mia'},{id:'Dean',title:'Dean'}]},mimo_plan:{configured:true}});
   const page=await context.newPage();page.setDefaultTimeout(20000);await page.goto('https://cabinet.example.test/');
   await page.waitForSelector('.chat-bubble');
   if(width<720){await page.locator('#more-tab').click();await page.locator('#navigation-options [data-pane="connections"]').click();}
   else await page.locator('#connections-tab').click();
   await page.locator('#connections-voice-tab').click();
   assert.equal(await page.locator('[name=synthesis_voice]').inputValue(),'Dean');
   await page.locator('#ai-synthesis-text').fill('Hello. This is a playback test.');await page.locator('#ai-synthesis-test').click();
   if(process.env.CM_TTS_OLD_CSP){
    await page.waitForFunction(()=>Boolean(document.querySelector('#ai-synthesis-audio').error));
    assert.equal(await page.locator('#ai-synthesis-play').isVisible(),false);
    checks.push({width,old_policy_blocks_audio:true});continue;
   }
   await page.waitForSelector('#ai-synthesis-play:not([hidden])');
   assert.ok(await page.locator('#ai-synthesis-audio').evaluate(a=>Number.isFinite(a.duration)&&a.duration>0));
   await page.locator('#ai-synthesis-play').click();await page.waitForFunction(()=>document.querySelector('#ai-synthesis-audio').currentTime>0);
   assert.match(await page.locator('#ai-synthesis-result').textContent(),/Аудио готово/);
   await page.locator('[name=synthesis_provider]').selectOption('yandex');await page.locator('[name=synthesis_voice]').selectOption('john');
   await page.locator('#ai-routes-form button[type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('#ai-synthesis-play').hidden);
   await page.locator('#ai-yandex-tts-verify').click();await page.waitForFunction(()=>document.querySelector('#ai-yandex-tts-status').dataset.state==='active');
   await page.locator('#ai-synthesis-text').fill('Hello from Yandex.');await page.locator('#ai-synthesis-test').click();
   await page.waitForSelector('#ai-synthesis-play:not([hidden])');await page.locator('#ai-synthesis-play').click();
   await page.waitForFunction(()=>document.querySelector('#ai-synthesis-audio').currentTime>0);
   assert.equal(data.ai.settings.gateways.synthesis_provider,'yandex');assert.equal(data.ai.settings.gateways.synthesis_voice,'john');
   assert.match(await page.locator('#ai-synthesis-result').textContent(),/Оценка стоимости/);
   data.ai.synthesisBroken=true;await page.locator('#ai-synthesis-test').click();
   await page.waitForSelector('#ai-synthesis-error:not([hidden])');
   assert.equal(await page.locator('#ai-synthesis-play').isVisible(),false);
   assert.doesNotMatch(await page.locator('#ai-synthesis-result').textContent(),/Аудио готово/);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   assert.deepEqual(data.unexpected,[]);assert.deepEqual(data.errors,[]);
   checks.push({width,mimo_dean_audio_decodes_and_plays:true,yandex_route_and_playback:true,production_csp:true});
  }finally{await browser.close();}
 }
 console.log(JSON.stringify({checks,ok:true},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
