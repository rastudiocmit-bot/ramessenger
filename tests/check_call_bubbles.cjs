// Full cabinet and recording/transcript fixtures only; no provider or paid AI traffic.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
function recording(){
 const samples=8*8000,b=Buffer.alloc(44+samples*4);b.write('RIFF');b.writeUInt32LE(b.length-8,4);b.write('WAVEfmt ',8);b.writeUInt32LE(16,16);b.writeUInt16LE(1,20);b.writeUInt16LE(2,22);b.writeUInt32LE(8000,24);b.writeUInt32LE(32000,28);b.writeUInt16LE(4,32);b.writeUInt16LE(16,34);b.write('data',36);b.writeUInt32LE(samples*4,40);
 for(let i=0;i<samples;i++){b.writeInt16LE(Math.round(10000*Math.sin(2*Math.PI*440*i/8000)),44+i*4);b.writeInt16LE(Math.round(10000*Math.sin(2*Math.PI*660*i/8000)),46+i*4);}
 return execFileSync('ffmpeg',['-v','error','-f','wav','-i','pipe:0','-ar','8000','-ac','2','-c:a','libmp3lame','-b:a','24k','-write_xing','0','-f','mp3','pipe:1'],{input:b});
}
const mp3=recording();
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const reports=[];
  for(const width of [1440,1024,390]){
   const context=await browser.newContext({viewport:{width,height:1000},hasTouch:width<700,reducedMotion:'reduce'}),data=await fixture(context);
   const call=(id,direction,minute,ai)=>({id,source:'novofon',version:1,direction,missed:false,talk_seconds:38,recording_available:true,employees:['Мария Петрова','Олег Иванов'],occurred_at:`2026-10-24T10:0${minute}:30Z`,ai});
   const ready=id=>({run_id:id,transcript_run_id:id,phase:'complete',asr_complete:true,summary:'Краткое описание хранится в подробностях',version:1});
   const calls=[call('incoming','in',1,ready('in-run')),call('outgoing','out',2,ready('out-run')),call('night','in',3,{phase:'queued',waiting_for_night:true}),{...call('missed','in',4,null),missed:true,talk_seconds:0,recording_available:false}];
   const transcripts=[],requests=[],blocked=[];let pending=true;
   await context.route('**/api/workspaces/*/chats/*/calls**',async route=>{
    const url=new URL(route.request().url());requests.push({path:url.pathname,method:route.request().method()});
    if(route.request().method()!=='GET'){blocked.push(url.pathname);return route.abort();}
    if(url.pathname.endsWith('/transcript')){if(pending)await new Promise(resolve=>transcripts.push(resolve));return route.fulfill({contentType:'application/json',body:JSON.stringify({complete:true,next:null,segments:[{channel:1,start:0,text:'Здравствуйте! Хотела уточнить расписание занятий и договориться о пробном уроке. Нам удобнее в четверг после школы.'},{channel:2,start:12,text:'Добрый день! В четверг есть место на 17:00. Запишем вас на пробное занятие, а подробности отправим в чате.'}]})});}
    if(url.pathname.endsWith('/recording')){
     const range=route.request().headers().range||'bytes=0-',match=/^bytes=(\d+)-(\d*)$/.exec(range),start=Number(match?.[1]||0),end=Math.min(match?.[2]?Number(match[2]):mp3.length-1,mp3.length-1);
     if(start>=mp3.length)return route.fulfill({status:416,headers:{'Content-Range':`bytes */${mp3.length}`}});
     return route.fulfill({status:206,contentType:'audio/mpeg',headers:{'Content-Range':`bytes ${start}-${end}/${mp3.length}`,'Accept-Ranges':'bytes','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'},body:mp3.subarray(start,end+1)});
    }
    return route.fulfill({contentType:'application/json',body:JSON.stringify({calls,scope:'synthetic-current-phone',has_more:false})});
   });
   const page=await context.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');if(width<700)await page.locator('.chat-select').first().click();
   await page.waitForSelector('.call-event[data-call-id=incoming]');const incoming=page.locator('[data-call-id=incoming]'),outgoing=page.locator('[data-call-id=outgoing]');
   const before=await incoming.boundingBox();pending=false;transcripts.splice(0).forEach(resolve=>resolve());await page.waitForFunction(()=>document.querySelector('[data-call-id=incoming] .call-transcript-preview').textContent.startsWith('Здравствуйте'));
   const after=await incoming.boundingBox();assert.ok(Math.abs(before.height-after.height)<=1,`Transcript changed height by ${after.height-before.height}`);
   const first=await incoming.boundingBox(),second=await outgoing.boundingBox();assert.ok(first.x<second.x,'Incoming left; outgoing right');
   const incomingActions=await incoming.locator('.call-event-actions').boundingBox(),outgoingActions=await outgoing.locator('.call-event-actions').boundingBox();assert.ok(incomingActions.x>first.x+first.width/2);assert.ok(outgoingActions.x<second.x+second.width/2);
   assert.equal(await incoming.locator('.call-event-subscriber').textContent(),'Абонент: Анна Смирнова');assert.match(await incoming.locator('.call-event-details').textContent(),/13:01·00:38/);
   const preview=await incoming.locator('.call-transcript-preview').evaluate(e=>({height:e.getBoundingClientRect().height,line:parseFloat(getComputedStyle(e).lineHeight),color:getComputedStyle(e.parentElement.parentElement).backgroundColor}));assert.ok(Math.abs(preview.height-preview.line*4)<1);
   assert.equal(requests.filter(r=>r.path.endsWith('/recording')).length,0);await page.locator('#chat-stream').evaluate(e=>e.scrollTop=0);if(process.env.CM_APPEARANCE_SCREENSHOT_DIR){fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`before-play-${width}.png`)});}await page.evaluate(()=>{
    const audio=document.querySelector('[data-call-id=incoming] audio'),ctx=new AudioContext(),source=ctx.createMediaElementSource(audio),splitter=ctx.createChannelSplitter(2),meters=[ctx.createAnalyser(),ctx.createAnalyser()];
    source.connect(ctx.destination);source.connect(splitter);meters.forEach((meter,i)=>splitter.connect(meter,i));
    window.callAudioMeter={ctx,meters};audio.muted=true;audio.volume=0;
    document.querySelector('[data-call-id=incoming] [data-call-action=play]').addEventListener('pointerdown',()=>ctx.resume(),{once:true});
   });
   await incoming.locator('[data-call-action=play]').click();
   await page.waitForFunction(()=>{const audio=document.querySelector('[data-call-id=incoming] audio');return !audio.paused&&audio.currentTime>.1&&!audio.muted&&audio.volume>0;});
   await page.waitForFunction(()=>window.callAudioMeter.meters.every(m=>{const values=new Float32Array(m.fftSize);m.getFloatTimeDomainData(values);return Math.sqrt(values.reduce((s,v)=>s+v*v,0)/values.length)>.01;}));
   const signal=await page.evaluate(()=>window.callAudioMeter.meters.map(m=>{const v=new Float32Array(m.fftSize);m.getFloatTimeDomainData(v);return Math.sqrt(v.reduce((s,x)=>s+x*x,0)/v.length);}));
   const audioHandle=await incoming.locator('audio').elementHandle();await incoming.locator('[data-call-action=transcript]').click();await page.waitForSelector('dialog.call-transcript-dialog[open]');assert.match(await page.locator('dialog.call-transcript-dialog[open]').textContent(),/Канал 2/);assert.equal(await audioHandle.evaluate(e=>e.paused),false);
   await page.locator('dialog.call-transcript-dialog[open] button').first().click();assert.equal(await incoming.locator('audio').evaluate(e=>e.paused),false);
   await outgoing.locator('[data-call-action=play]').click();await page.waitForFunction(()=>document.querySelector('[data-call-id=incoming] audio').paused&&!document.querySelector('[data-call-id=outgoing] audio').paused);
   const overflow=await page.locator('#chat-stream').evaluate(e=>e.scrollWidth-e.clientWidth);assert.ok(overflow<=1,`Overflow ${width}: ${overflow}`);
   if(process.env.CM_APPEARANCE_SCREENSHOT_DIR){fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR,{recursive:true});await page.locator('#chat-stream').evaluate(e=>e.scrollTop=0);await page.screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`calls-${width}.png`)});if(width===1440){await page.evaluate(()=>document.documentElement.dataset.theme='dark');await page.screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`calls-dark-${width}.png`)});}}
   assert.deepEqual(blocked,[]);assert.deepEqual(data.errors,[]);reports.push({width,overflow,previewHeight:preview.height,transcriptHeightDrift:after.height-before.height,localTranscriptReads:requests.filter(r=>r.path.endsWith('/transcript')).length,paidRequests:blocked.length,decodedMp3Rms:signal});await page.evaluate(()=>window.callAudioMeter.ctx.close());await page.close();
  }
  console.log(JSON.stringify({browser:browser.version(),reports},null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
