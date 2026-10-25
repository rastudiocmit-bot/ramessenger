"use strict";
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const { JSDOM } = require('jsdom');
const web = path.join(__dirname, '../app/backend/src/center_messenger/web');
const source = fs.readFileSync(path.join(web, 'voice.js'), 'utf8');
const markup = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
const flush = async () => { for (let i=0;i<20;i++) await Promise.resolve(); };
const deferred = () => { let resolve,reject; const promise = new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject}; };
function setup(t, options={}) {
  const dom = new JSDOM(markup, {runScripts:'outside-only',pretendToBeVisual:true,url:'https://voice.example.test'});
  const w=dom.window,$=s=>w.document.querySelector(s),timers=new Map(),calls=[],contexts=[],media=[];
  let now=0,seq=0,hidden=false,requests=0;
  w.performance.now=()=>now;w.matchMedia=()=>({matches:true});
  Object.defineProperty(w.document,'hidden',{get:()=>hidden});
  function schedule(fn,delay,repeat=false){const id=++seq;timers.set(id,{fn,at:now+delay,delay,repeat});return id;}
  w.setTimeout=(fn,ms)=>schedule(fn,ms);w.setInterval=(fn,ms)=>schedule(fn,ms,true);
  w.clearTimeout=w.clearInterval=id=>timers.delete(id);
  const createMedia=()=>{const track={stops:0,stop(){this.stops++;}};const stream={track,getTracks:()=>[track]};media.push(stream);return stream;};
  w.AudioContext=class {
    constructor(){this.state=options.contextState||'running';this.sampleRate=48000;this.destination={};contexts.push(this);}
    resume(){if(options.resumeHang)return new Promise(()=>{});this.state='running';return Promise.resolve();}
    close(){this.closed=true;this.state='closed';return Promise.resolve();}
    createMediaStreamSource(){if(options.nodeError)throw new Error('node failed');return{connect(){},disconnect(){this.detached=true;}};}
    createScriptProcessor(){this.processor={connect(){},disconnect(){this.detached=true;}};return this.processor;}
    createGain(){return{gain:{value:1},connect(){},disconnect(){this.detached=true;}};}
  };
  Object.defineProperty(w.navigator,'mediaDevices',{value:{getUserMedia:()=>{requests++;return options.getUserMedia?options.getUserMedia(createMedia):Promise.resolve(createMedia());}}});
  const state={voiceInput:true,workspace:{id:'workspace'},pane:'messages',epoch:1};
  $('#chat-compose').hidden=false;$('#chat-input').disabled=false;
  w.eval(source);
  const app=w.CenterVoice({$,state,api:async(url,args)=>{calls.push({url,args});if(url.endsWith('/diagnostic'))return null;return options.transcribe?options.transcribe(args):{text:'Распознанный текст'};},toast:()=>assert.fail('voice must not use global notifications')});
  const pointer=(target,type,extra={})=>{const event=new w.MouseEvent(type,{bubbles:true,cancelable:true,button:0,clientX:100,clientY:100,...extra});
    Object.defineProperties(event,{pointerId:{value:extra.pointerId??1},pointerType:{value:'touch'},isPrimary:{value:true}});$(target).dispatchEvent(event);};
  const advance=async(ms)=>{const until=now+ms;let guard=0;
    while(true){const next=[...timers].filter(([,v])=>v.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;
      assert.ok(++guard<2000);now=next[1].at;timers.delete(next[0]);if(next[1].repeat)timers.set(next[0],{...next[1],at:now+next[1].delay});next[1].fn();await flush();}
    now=until;await flush();};
  const frames=(count=12,value=0.1)=>{for(let i=0;i<count;i++){const processor=contexts.at(-1)?.processor;assert.ok(processor?.onaudioprocess,'capture is attached');processor.onaudioprocess({inputBuffer:{getChannelData:()=>new w.Float32Array(4096).fill(value)}});}};
  const tap=async()=>{pointer('#chat-voice','pointerdown');pointer('#chat-voice','pointerup');await flush();};
  const hold=async(target='#chat-voice')=>{pointer(target,'pointerdown');await advance(target==='#chat-stream'?450:220);await flush();};
  const locked=()=>w.document.documentElement.classList.contains('voice-holding');
  const transcriptions=()=>calls.filter(c=>c.url.endsWith('/transcribe'));
  t.after(()=>{app.reset();dom.window.close();});
  return{w,$,state,app,pointer,advance,frames,tap,hold,locked,transcriptions,calls,contexts,media,createMedia,requests:()=>requests,
    hide:()=>{hidden=true;w.document.dispatchEvent(new w.Event('visibilitychange'));}};
}

test('cancelled tap and lost pointer do not request a microphone',async t=>{
  const s=setup(t);s.pointer('#chat-voice','pointerdown');s.pointer('#chat-voice','pointercancel');await s.advance(1000);
  assert.equal(s.requests(),0);assert.equal(s.locked(),false);
  s.pointer('#chat-voice','pointerdown');s.pointer('#chat-voice','lostpointercapture');await s.advance(1000);assert.equal(s.requests(),0);
});
test('a normal history scroll never requests microphone permission',async t=>{
  const s=setup(t);s.pointer('#chat-stream','pointerdown');s.pointer('#chat-stream','pointermove',{clientY:130});await s.advance(1000);
  assert.equal(s.requests(),0);assert.equal(s.locked(),false);assert.equal(s.$('#voice-hold').hidden,true);
});
test('cold permission is preparing; release unlocks and stops a late stream',async t=>{
  const pending=deferred();const s=setup(t,{getUserMedia:()=>pending.promise});await s.hold();assert.equal(s.$('#chat-voice-panel').dataset.phase,'starting');
  assert.equal(s.locked(),true);await s.advance(3000);assert.equal(s.$('#chat-voice-panel').dataset.phase,'starting');
  s.pointer('#chat-voice','pointerup');assert.equal(s.locked(),false);assert.equal(s.$('#voice-hold').hidden,true);
  const stream=s.createMedia();pending.resolve(stream);await flush();assert.ok(stream.track.stops>0);assert.equal(s.transcriptions().length,0);
  assert.equal(s.$('#chat-input').value,'');assert.equal(s.$('#chat-voice-panel').dataset.phase,'notice');
});
test('permission and resume have finite deadlines with inline retry',async t=>{
  const pending=deferred();const s=setup(t,{getUserMedia:()=>pending.promise});await s.tap();await s.advance(20001);
  assert.equal(s.$('#chat-voice-panel').dataset.phase,'error');assert.match(s.$('#chat-voice-label').textContent,/разрешение/);assert.equal(s.locked(),false);
  const stream=s.createMedia();pending.resolve(stream);await flush();assert.ok(stream.track.stops>0);
  const r=setup(t,{contextState:'suspended',resumeHang:true});await r.tap();await r.advance(5001);
  assert.equal(r.$('#chat-voice-panel').dataset.phase,'error');assert.match(r.$('#chat-voice-label').textContent,/приостановил/);
  assert.ok(r.media[0].track.stops>0);assert.equal(r.contexts[0].closed,true);assert.equal(r.locked(),false);
});
test('a double press has only one startup and the clock counts audio frames',async t=>{
  const s=setup(t);await s.tap();await s.tap();assert.equal(s.requests(),1);await s.advance(2800);
  assert.equal(s.$('#chat-voice-panel').dataset.phase,'starting');s.frames(6);
  assert.equal(s.$('#chat-voice-panel').dataset.phase,'recording');assert.equal(s.$('#chat-voice-time').textContent,'0:00 / 0:30');
  s.frames(6);assert.equal(s.$('#chat-voice-time').textContent,'0:01 / 0:30');
});
test('no callbacks and stalled callbacks differ from valid silent samples',async t=>{
  const s=setup(t);await s.tap();await s.advance(5100);assert.match(s.$('#chat-voice-label').textContent,/не поступает/);
  assert.equal(s.transcriptions().length,0);assert.ok(s.media[0].track.stops>0);
  const r=setup(t);await r.tap();r.frames(12,0);assert.equal(r.$('#chat-voice-panel').dataset.phase,'recording');
  r.$('#chat-voice-done').click();await flush();assert.equal(r.transcriptions().length,1,'silent frames are real capture, not missing callbacks');
  const q=setup(t);await q.tap();q.frames(6);await q.advance(2700);assert.match(q.$('#chat-voice-label').textContent,/прервалась/);
  assert.equal(q.transcriptions().length,0);assert.ok(q.media[0].track.stops>0);
});
test('cancel, interruption, background and page exit clean every capture resource',async t=>{
  for(const kind of ['cancel','interrupted','background','pagehide']){
    const s=setup(t);await s.hold();s.frames();
    if(kind==='cancel')s.pointer('#chat-voice','pointercancel');
    if(kind==='interrupted'){s.contexts[0].state='interrupted';s.contexts[0].onstatechange();}
    if(kind==='background')s.hide();
    if(kind==='pagehide')s.w.dispatchEvent(new s.w.Event('pagehide'));
    assert.equal(s.locked(),false,kind);assert.ok(s.media[0].track.stops>0,kind);assert.equal(s.contexts[0].processor.onaudioprocess,null,kind);
    assert.equal(s.transcriptions().length,0,kind);
  }
});
test('brief accidental audio is discarded quietly; deliberate short audio is inline',async t=>{
  const s=setup(t);await s.tap();s.frames(1);s.$('#chat-voice-done').click();await flush();assert.equal(s.$('#chat-voice-panel').hidden,true);assert.equal(s.transcriptions().length,0);
  const r=setup(t);await r.tap();r.frames(3);r.$('#chat-voice-done').click();await flush();assert.equal(r.$('#chat-voice-panel').dataset.phase,'error');assert.match(r.$('#chat-voice-label').textContent,/0,3/);
});
test('release unlocks immediately; retry reuses the clip and cannot automatically send',async t=>{
  let count=0;const request=deferred();const s=setup(t,{transcribe:()=>++count===1?request.promise:{text:'Повтор без отправки'}});
  let submits=0;s.$('#chat-compose').addEventListener('submit',event=>{event.preventDefault();submits++;});
  await s.hold();s.frames();s.pointer('#chat-voice','pointerup');await flush();assert.equal(s.locked(),false);assert.equal(s.$('#voice-hold').hidden,true);
  assert.equal(s.$('#chat-voice-panel').dataset.phase,'working');assert.equal(s.transcriptions()[0].args.timeoutMs,60000);
  request.reject(Object.assign(new Error('Нет связи'),{code:'network'}));await flush();assert.match(s.$('#chat-voice-label').textContent,/Запись сохранена/);
  s.$('#chat-voice-retry').click();await flush();assert.equal(s.$('#chat-input').value,'Повтор без отправки');
  assert.equal(s.transcriptions()[0].args.data.audio,s.transcriptions()[1].args.data.audio);await s.advance(30000);assert.equal(submits,0);
});
test('late permission and recognition never enter another chat/workspace/session',async t=>{
  for(const mode of ['chat','workspace','epoch']){
    const result=deferred();const s=setup(t,{transcribe:()=>result.promise});await s.tap();s.frames();s.$('#chat-voice-done').click();await flush();
    const signal=s.transcriptions()[0].args.signal;
    if(mode==='chat')s.w.document.dispatchEvent(new s.w.CustomEvent('cm:chat-selected'));
    if(mode==='workspace')s.state.workspace={id:'other'};
    if(mode==='epoch')s.state.epoch++;
    s.app.render();s.$('#chat-input').value='Другой черновик';result.resolve({text:'Нельзя вставлять'});await flush();
    assert.equal(s.$('#chat-input').value,'Другой черновик',mode);assert.equal(signal.aborted,true,mode);
  }
  const pending=deferred();const r=setup(t,{getUserMedia:()=>pending.promise});await r.tap();r.w.document.dispatchEvent(new r.w.CustomEvent('cm:chat-selected'));
  const stream=r.createMedia();pending.resolve(stream);await flush();assert.ok(stream.track.stops>0);assert.equal(r.$('#chat-voice-panel').hidden,true);
});
test('preview is inline and touching/scrolling history keeps text unsent',async t=>{
  const s=setup(t);let submits=0;s.$('#chat-compose').addEventListener('submit',event=>{event.preventDefault();submits++;});
  await s.hold();s.frames();s.pointer('#chat-voice','pointerup');await flush();assert.equal(s.locked(),false);
  assert.equal(s.$('#voice-hold').dataset.phase,'preview');assert.equal(s.$('#voice-hold').parentElement,s.$('#chat-compose'));
  s.pointer('#chat-stream','pointerdown');await s.advance(30000);assert.equal(submits,0);assert.equal(s.$('#chat-input').value,'Распознанный текст');
  assert.equal(s.$('#voice-hold').hidden,true);
});
test('typing or scrolling during recognition cancels automatic sending and preserves draft',async t=>{
  const pending=deferred();const s=setup(t,{transcribe:()=>pending.promise});await s.hold();s.frames();s.pointer('#chat-voice','pointerup');await flush();
  s.$('#chat-input').value='Новый черновик.';s.$('#chat-input').dispatchEvent(new s.w.Event('input',{bubbles:true}));
  pending.resolve({text:'Голосом'});await flush();assert.equal(s.$('#chat-input').value,'Новый черновик. Голосом');assert.equal(s.$('#voice-hold').hidden,true);
});
test('fallback never auto-sends; a preview timer sends once and only in its source chat',async t=>{
  const s=setup(t,{transcribe:()=>({text:'Без обработки',fallback:true,warning:'Проверьте текст'})});await s.hold();s.frames();s.pointer('#chat-voice','pointerup');await flush();
  assert.equal(s.$('#chat-input').value,'Без обработки');assert.equal(s.$('#chat-voice-panel').dataset.phase,'notice');assert.equal(s.$('#voice-hold').hidden,true);
  const r=setup(t);let submits=0;r.$('#chat-compose').addEventListener('submit',event=>{event.preventDefault();submits++;});
  await r.hold();r.frames();r.pointer('#chat-voice','pointerup');await flush();await r.advance(30000);assert.equal(submits,1);await r.advance(30000);assert.equal(submits,1);
});
test('node setup failure stops the stream and allows a fresh microphone attempt',async t=>{
  const s=setup(t,{nodeError:true});await s.tap();assert.ok(s.media[0].track.stops>0);assert.equal(s.$('#chat-voice-panel').dataset.phase,'error');
  assert.equal(s.locked(),false);s.$('#chat-voice-cancel').click();await s.tap();assert.equal(s.contexts.length,2);
});
test('diagnostics contain only bounded metadata, not audio or message text',async t=>{
  const s=setup(t);await s.tap();s.frames();s.$('#chat-voice-done').click();await flush();const events=s.calls.filter(c=>c.url.endsWith('/diagnostic'));
  assert.equal(events.length,1);const data=events[0].args.data;assert.equal(data.outcome,'complete');assert.ok(data.captured_ms>1000);
  assert.deepEqual(Object.keys(data).sort(),['attempt_id','outcome','code','stage','audio_state','elapsed_ms','first_frame_ms','captured_ms'].sort());assert.ok(!JSON.stringify(data).includes('Распознанный'));
});
test('background preserves an already recognised preview as an unsent draft',async t=>{
  const s=setup(t);let submits=0;s.$('#chat-compose').addEventListener('submit',event=>{event.preventDefault();submits++;});
  await s.hold();s.frames();s.pointer('#chat-voice','pointerup');await flush();assert.equal(s.$('#voice-hold').dataset.phase,'preview');
  s.hide();await s.advance(30000);assert.equal(s.$('#chat-input').value,'Распознанный текст');assert.equal(submits,0);assert.equal(s.locked(),false);
});
test('a cold touch tap resumes audio on pointerup, before requesting microphone',async t=>{
  const s=setup(t,{contextState:'suspended',resumeHang:true});s.pointer('#chat-voice','pointerdown');assert.equal(s.requests(),0);
  let resumed=0;s.contexts[0].resume=()=>{resumed++;s.contexts[0].state='running';return Promise.resolve();};
  s.pointer('#chat-voice','pointerup');await flush();assert.equal(resumed,1);assert.equal(s.requests(),1);
  s.frames(6);assert.equal(s.$('#chat-voice-panel').dataset.phase,'recording');
});
test('a normal chat tap never consumes the next keyboard microphone click',async t=>{
  const s=setup(t);s.pointer('#chat-stream','pointerdown');s.pointer('#chat-stream','pointerup');await flush();assert.equal(s.requests(),0);
  s.$('#chat-voice').click();await flush();assert.equal(s.requests(),1);s.frames(6);assert.equal(s.$('#chat-voice-panel').dataset.phase,'recording');
});
