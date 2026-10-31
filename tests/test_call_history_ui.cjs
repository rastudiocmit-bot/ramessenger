"use strict";
const {test}=require('node:test'), assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const script=fs.readFileSync(path.join(__dirname,'../app/backend/src/center_messenger/web/call-history.js'),'utf8');
const call={id:'00000000-0000-4000-8000-000000000001',occurred_at:'2026-10-31T00:00:00Z',direction:'in',missed:false,talk_seconds:65,employees:['Synthetic operator'],recording_available:true,version:1};
function setup(t,api,options={}){
 const dom=new JSDOM('<div id="history"></div>',{runScripts:'outside-only'}),w=dom.window;
 w.CSS={escape:value=>value};w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};w.HTMLMediaElement.prototype.pause=function(){this.pausedByTest=true;};w.HTMLMediaElement.prototype.play=function(){this.playedByTest=true;return Promise.resolve();};
 const node=(tag,text,cls)=>{const e=w.document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
 const state={epoch:1,workspace:{id:'workspace-one',timezone:'Europe/Moscow'}};let selected='chat-one',changes=0;
 w.eval(script);const ui=w.CenterCallHistory({state,api,node,...options,context:()=>selected,changed:()=>changes++});
 t.after(()=>{ui.reset();w.close();});return {w,state,ui,setSelected:id=>selected=id,changes:()=>changes};
}
test('one source call appears once after repeated reads and keeps older cached pages',async t=>{
 const s=setup(t,async()=>({calls:[call],scope:'scope-one',has_more:false}));
 await s.ui.refresh('chat-one');await s.ui.refresh('chat-one',false,true);assert.equal(s.ui.messages('chat-one').length,1);
 assert.equal(s.ui.messages('chat-one')[0].actor_type,'call');
});
test('audio is lazy, served through the authorized app route and never autoplayed',async t=>{
 const s=setup(t,async()=>({calls:[],scope:'one'}));const bubble=s.ui.render(call,'chat-one');s.w.document.querySelector('#history').append(bubble);
 const audio=bubble.querySelector('audio');assert.equal(audio.hasAttribute('src'),false);assert.equal(audio.preload,'none');assert.equal(audio.autoplay,false);
 bubble.querySelector('button').click();assert.match(audio.src,/\/api\/workspaces\/workspace-one\/chats\/chat-one\/calls\/.+\/recording$/);
 assert.equal(audio.playedByTest,true);assert.equal(audio.hidden,false);
 assert.equal(bubble.classList.contains('msg'),false,'Call cannot become a MAX task/send target');
});
test('a second recording pauses the first and selection changes pause playback',async t=>{
 const s=setup(t,async()=>({calls:[],scope:'one'}));const box=s.w.document.querySelector('#history');
 box.append(s.ui.render(call,'chat-one'),s.ui.render({...call,id:'00000000-0000-4000-8000-000000000002'},'chat-one'));
 const audios=box.querySelectorAll('audio');audios[1].dispatchEvent(new s.w.Event('play'));assert.equal(audios[0].pausedByTest,true);
 s.w.document.dispatchEvent(new s.w.Event('cm:chat-selected'));assert.equal(audios[1].pausedByTest,true);
});
test('late replies cannot inject another workspace or conversation history',async t=>{
 let resolve;const s=setup(t,()=>new Promise(r=>resolve=r));const read=s.ui.refresh('chat-one');s.state.epoch++;s.state.workspace.id='workspace-two';resolve({calls:[call],scope:'one'});await read;
 assert.equal(s.ui.messages('chat-one').length,0);assert.equal(s.changes(),0);
});
test('changed CRM phone scope clears old audio and rows',async t=>{
 let n=0;const s=setup(t,async()=>++n===1?{calls:[call],scope:'old'}:{calls:[],scope:'new'});
 await s.ui.refresh('chat-one');await s.ui.refresh('chat-one',false,true);assert.equal(s.ui.messages('chat-one').length,0);
});
test('provider text stays plain and failed playback has an inline retry',async t=>{
 const s=setup(t,async()=>({calls:[],scope:'one'}));const bubble=s.ui.render({...call,employees:['<img src=x onerror=alert(1)>']},'chat-one');
 s.w.document.querySelector('#history').append(bubble);assert.equal(bubble.querySelector('img'),null);bubble.querySelector('audio').dispatchEvent(new s.w.Event('error'));
 assert.equal(bubble.querySelector('[data-call-action=play]').getAttribute('aria-label'),'Повторить загрузку аудио');assert.equal(bubble.querySelector('.call-audio-error').hidden,false);bubble.querySelector('audio').dispatchEvent(new s.w.Event('pause'));assert.equal(bubble.querySelector('[data-call-action=play]').title,'Повторить загрузку аудио');
});

test('AI updates preserve the recording element and stable message identity',async t=>{
 let currentCall={...call,ai:null};const s=setup(t,async()=>({calls:[currentCall],scope:'same'}));s.state.workspace.role='owner';
 await s.ui.refresh('chat-one');const before=JSON.stringify(s.ui.messages('chat-one')[0]);
 const bubble=s.ui.render(s.ui.messages('chat-one')[0].call,'chat-one');s.w.document.querySelector('#history').append(bubble);const audio=bubble.querySelector('audio');
 currentCall={...call,ai:{run_id:'one',phase:'recognizing',version:1,summary:null,processing:true}};
 await s.ui.refresh('chat-one',false,true);
 assert.equal(bubble.querySelector('audio'),audio);assert.notEqual(audio.pausedByTest,true);assert.equal(JSON.stringify(s.ui.messages('chat-one')[0]),before);
 assert.match(bubble.textContent,/Распознаём/);
});
test('saved AI summaries are plain text and never become a MAX message target',async t=>{
 const s=setup(t,async()=>({calls:[],scope:'same'}));
 const bubble=s.ui.render({...call,ai:{run_id:'one',phase:'complete',version:1,summary:'<img src=x onerror=alert(1)>',summary_source:'current'}},'chat-one');
 assert.equal(bubble.querySelector('img'),null);assert.match(bubble.textContent,/<img/);assert.equal(bubble.classList.contains('msg'),false);
});
const tick=()=>new Promise(r=>setTimeout(r,5));
const ai=(extra={})=>({run_id:'one',phase:'complete',version:1,summary:'Краткое описание',transcript_run_id:'one',asr_complete:true,...extra});
const page=(text='Здравствуйте, когда будет занятие?',extra={})=>({run_id:'one',complete:true,next:null,segments:[{start:0,channel:1,text}],...extra});
test('saved transcript appears automatically as plain text; full view shares the authorized GET, never AI POST',async t=>{
 const requests=[];const s=setup(t,async(url,options)=>{requests.push({url,options});return page('<script>bad()</script>');});
 const bubble=s.ui.render({...call,ai:ai()},'chat-one');s.w.document.querySelector('#history').append(bubble);await tick();
 assert.equal(requests.length,1);assert.ok(requests[0].url.includes('/chats/chat-one/calls/'));assert.ok(requests[0].url.includes('/transcript?run=one'));assert.equal(requests[0].options.method,undefined);
 assert.equal(bubble.querySelector('.call-transcript-preview').textContent,'<script>bad()</script>');assert.equal(bubble.querySelector('script'),null);
 bubble.querySelector('[data-call-action=transcript]').click();const dialog=s.w.document.querySelector('.call-transcript-dialog');assert.equal(dialog.open,true);assert.match(dialog.textContent,/Канал 1:/);assert.equal(dialog.querySelector('script'),null);assert.equal(requests.length,1);
 dialog.querySelector('button').click();assert.equal(dialog.open,false);
});
test('directions keep facts, two icon controls and contextual client without a phone number or invented employee',async t=>{
 const s=setup(t,async()=>({}),{subscriber:()=> 'Анна Смирнова'});
 const incoming=s.ui.render(call,'chat-one'),outgoing=s.ui.render({...call,direction:'out',talk_seconds:3665},'chat-one');
 assert.ok(incoming.classList.contains('in'));assert.ok(outgoing.classList.contains('out'));assert.match(incoming.querySelector('.call-event-head').textContent,/Входящий звонок/);assert.match(outgoing.querySelector('.call-event-head').textContent,/Исходящий звонок/);
 assert.equal(incoming.querySelector('.call-event-actions').children.length,2);assert.equal(incoming.querySelector('time').textContent,'03:00');assert.match(incoming.querySelector('.call-event-details').textContent,/01:05/);assert.match(outgoing.querySelector('.call-event-details').textContent,/1:01:05/);
 assert.equal(incoming.querySelector('.call-event-subscriber').textContent,'Абонент: Анна Смирнова');assert.equal(outgoing.querySelector('.call-event-subscriber').textContent,'Кому: Анна Смирнова');assert.ok(!incoming.querySelector('.call-event-subscriber').textContent.includes('Synthetic operator'));
 assert.equal(outgoing.querySelector('.call-event-actions').textContent,'');
});
test('missing caller, unknown duration and missed call are explicit; unavailable controls are disabled',async t=>{
 const s=setup(t,async()=>({}),{subscriber:()=>'+7 (999) 000-00-00'}),bubble=s.ui.render({...call,talk_seconds:null,recording_available:false},'chat-one');
 assert.match(bubble.textContent,/Абонент не определён/);assert.match(bubble.textContent,/Длительность неизвестна/);assert.equal(bubble.querySelector('[data-call-action=play]').disabled,true);assert.equal(bubble.querySelector('[data-call-action=transcript]').disabled,true);assert.match(bubble.textContent,/Расшифровка не подготовлена/);
 const missed=s.ui.render({...call,missed:true,recording_available:false},'chat-one');assert.match(missed.querySelector('.call-event-head').textContent,/Входящий звонок/);assert.match(missed.textContent,/Без разговора/);
});
test('full transcript paginates and preserves channel labels without replacing audio on AI changes',async t=>{
 let currentCall={...call,ai:ai()},calls=0;const s=setup(t,async url=>url.includes('/transcript?')?(++calls===1?page('Первый фрагмент',{next:'asr:00000:1'}):page('Второй фрагмент')):{calls:[currentCall],scope:'same'});
 await s.ui.refresh('chat-one');const bubble=s.ui.render(s.ui.messages('chat-one')[0].call,'chat-one');s.w.document.querySelector('#history').append(bubble);await tick();
 bubble.querySelector('[data-call-action=transcript]').click();const dialog=s.w.document.querySelector('.call-transcript-dialog');dialog.querySelector('.call-transcript-body').scrollTop=25;
 dialog.querySelector('.call-transcript-dialog>.btn').click();await tick();assert.match(dialog.textContent,/Первый фрагмент/);assert.match(dialog.textContent,/Второй фрагмент/);assert.equal(calls,2);
 const audio=bubble.querySelector('audio');currentCall={...currentCall,ai:ai({summary:'Изменённое описание',version:2})};await s.ui.refresh('chat-one',false,true);assert.equal(dialog.open,true);assert.equal(bubble.querySelector('audio'),audio);assert.equal(calls,2);
});
test('transcript errors allow manual retry and background refresh cannot initiate paid recognition',async t=>{
 let attempts=0;const requests=[];const s=setup(t,async(url,options)=>{requests.push({url,options});if(++attempts===1)throw Error('offline');return page();});
 const bubble=s.ui.render({...call,ai:ai()},'chat-one');s.w.document.querySelector('#history').append(bubble);await tick();assert.match(bubble.querySelector('.call-transcript-preview').textContent,/Не удалось/);
 bubble.querySelector('[data-call-action=transcript]').click();await tick();assert.match(bubble.querySelector('.call-transcript-preview').textContent,/Здравствуйте/);assert.equal(attempts,2);assert.ok(requests.every(r=>!r.options.method));
});
test('late transcript is rejected after CRM phone scope changes, even for the same call and run',async t=>{
 let scope='old',finish;const s=setup(t,url=>url.includes('/transcript?')?new Promise(r=>finish=r):Promise.resolve({calls:[{...call,ai:ai()}],scope}));
 await s.ui.refresh('chat-one');const bubble=s.ui.render(s.ui.messages('chat-one')[0].call,'chat-one');s.w.document.querySelector('#history').append(bubble);await tick();const oldFinish=finish;
 scope='new';await s.ui.refresh('chat-one',false,true);await tick();oldFinish(page('Старый доступ'));await tick();assert.ok(!bubble.querySelector('.call-transcript-preview').textContent.includes('Старый доступ'));
 finish(page('Новый доступ'));await tick();assert.equal(bubble.querySelector('.call-transcript-preview').textContent,'Новый доступ');
});
test('automatic transcript reads have at most two local requests in flight',async t=>{
 let running=0,peak=0;const pending=[];const s=setup(t,()=>new Promise(resolve=>{running++;peak=Math.max(peak,running);pending.push(()=>{running--;resolve(page());});}));
 const root=s.w.document.querySelector('#history');for(let i=0;i<5;i++)root.append(s.ui.render({...call,id:`call-${i}`,ai:ai()},'chat-one'));await tick();assert.equal(pending.length,2);
 while(pending.length){pending.shift()();await tick();}assert.equal(peak,2);assert.equal(root.querySelectorAll('.call-transcript-preview').length,5);
});
test('directory transcripts keep their own authorized route and selection closes the full-text panel',async t=>{
 const urls=[];const s=setup(t,async url=>{urls.push(url);return page();},{sourcePath:'directory'});const bubble=s.ui.render({...call,ai:ai()},'chat-one');s.w.document.querySelector('#history').append(bubble);await tick();assert.match(urls[0],/\/directory\/chat-one\/calls\//);
 bubble.querySelector('[data-call-action=transcript]').click();const dialog=s.w.document.querySelector('dialog');assert.equal(dialog.open,true);s.setSelected('other');s.w.document.dispatchEvent(new s.w.Event('cm:chat-selected'));assert.equal(dialog.open,false);assert.equal(dialog.isConnected,false);
});

test('a completing ASR refresh replaces partial preview while preserving the recording',async t=>{
 let complete=false;const s=setup(t,async url=>url.includes('/transcript?')?page(complete?'Полный текст':'Часть разговора',{complete}):{calls:[{...call,ai:ai({asr_complete:complete,phase:complete?'complete':'recognizing',version:complete?2:1})}],scope:'same'});
 await s.ui.refresh('chat-one');const bubble=s.ui.render(s.ui.messages('chat-one')[0].call,'chat-one');s.w.document.querySelector('#history').append(bubble);await tick();assert.equal(bubble.querySelector('.call-transcript-preview').textContent,'Часть разговора');const audio=bubble.querySelector('audio');
 complete=true;await s.ui.refresh('chat-one',false,true);await tick();assert.equal(bubble.querySelector('.call-transcript-preview').textContent,'Полный текст');assert.equal(bubble.querySelector('audio'),audio);
});
test('paid repeat stays in call details and requires explicit cost acknowledgement',async t=>{
 const posts=[];const s=setup(t,async(url,options)=>{if(options?.method==='POST'){posts.push(options.data);return ai({phase:'queued',version:2});}if(url.includes('/transcript?'))return page();return {calls:[{...call,ai:ai()}],scope:'same'};});s.state.workspace.role='owner';
 const bubble=s.ui.render({...call,ai:ai()},'chat-one');s.w.document.querySelector('#history').append(bubble);await tick();const details=bubble.querySelector('.call-ai-management'),repeat=[...details.querySelectorAll('button')].find(e=>e.textContent==='Повторно обработать');
 assert.equal(details.open,false);assert.equal(repeat.disabled,true);assert.equal(posts.length,0);details.open=true;details.querySelector('input[type=checkbox]').click();repeat.click();await tick();assert.equal(posts.length,1);assert.equal(posts[0].acknowledge_cost,true);assert.equal(posts[0].reprocess,true);
});

test('explicit playback unmutes this recording and restores zero volume while retaining a chosen nonzero level',async t=>{
 const s=setup(t,async()=>({})),bubble=s.ui.render(call,'chat-one');s.w.document.querySelector('#history').append(bubble);const audio=bubble.querySelector('audio'),play=bubble.querySelector('[data-call-action=play]');
 audio.muted=true;audio.volume=0;play.click();assert.equal(audio.muted,false);assert.equal(audio.volume,1);assert.equal(audio.playedByTest,true);
 audio.volume=.35;play.click();assert.equal(audio.volume,.35);
});
test('mute and zero volume are visible and the inline recovery enables sound',async t=>{
 const s=setup(t,async()=>({})),bubble=s.ui.render(call,'chat-one');s.w.document.querySelector('#history').append(bubble);const audio=bubble.querySelector('audio'),play=bubble.querySelector('[data-call-action=play]'),restore=bubble.querySelector('.call-audio-unmute');
 play.click();audio.muted=true;audio.dispatchEvent(new s.w.Event('volumechange'));assert.equal(restore.hidden,false);assert.match(bubble.querySelector('.call-audio-error').textContent,/Звук плеера выключен/);
 restore.click();assert.equal(audio.muted,false);assert.equal(restore.hidden,true);assert.equal(bubble.querySelector('.call-audio-error').hidden,true);
 audio.volume=0;audio.dispatchEvent(new s.w.Event('volumechange'));assert.equal(restore.hidden,false);
});
test('browser playback rejection is shown even when the media element has no error',async t=>{
 const s=setup(t,async()=>({})),bubble=s.ui.render(call,'chat-one');s.w.document.querySelector('#history').append(bubble);const audio=bubble.querySelector('audio');
 audio.play=()=>Promise.reject(new s.w.DOMException('blocked','NotAllowedError'));bubble.querySelector('[data-call-action=play]').click();await tick();
 assert.equal(audio.error??null,null);assert.equal(bubble.dataset.audioState,'blocked');assert.match(bubble.querySelector('.call-audio-error').textContent,/Браузер не разрешил запуск/);assert.equal(audio.hidden,false);
});
test('a late playback rejection from a closed conversation does not alter the next view',async t=>{
 const s=setup(t,async()=>({})),bubble=s.ui.render(call,'chat-one');s.w.document.querySelector('#history').append(bubble);let reject;
 bubble.querySelector('audio').play=()=>new Promise((_,r)=>reject=r);bubble.querySelector('[data-call-action=play]').click();s.setSelected('other');s.w.document.dispatchEvent(new s.w.Event('cm:chat-selected'));reject(new Error('old request'));await tick();assert.equal(bubble.querySelector('.call-audio-error').hidden,true);
});
test('native pause/play updates the compact icon and restarting never pauses the same recording first',async t=>{
 const s=setup(t,async()=>({})),bubble=s.ui.render(call,'chat-one');s.w.document.querySelector('#history').append(bubble);const audio=bubble.querySelector('audio'),button=bubble.querySelector('[data-call-action=play]');
 Object.defineProperty(audio,'paused',{configurable:true,get:()=>false});audio.dispatchEvent(new s.w.Event('playing'));assert.equal(button.getAttribute('aria-label'),'Приостановить звонок');button.click();assert.equal(audio.pausedByTest,true);
 Object.defineProperty(audio,'paused',{configurable:true,get:()=>true});audio.dispatchEvent(new s.w.Event('pause'));audio.pausedByTest=false;button.click();assert.equal(audio.pausedByTest,false);assert.equal(audio.playedByTest,true);
});

test('call details expose current player state without URLs, new requests or audio replacement',async t=>{
 let currentCall={...call,ai:null};const s=setup(t,async()=>({calls:[currentCall],scope:'same'}));await s.ui.refresh('chat-one');const bubble=s.ui.render(s.ui.messages('chat-one')[0].call,'chat-one');s.w.document.querySelector('#history').append(bubble);const audio=bubble.querySelector('audio'),report=bubble.querySelector('.call-audio-diagnostic');
 report.open=true;report.dispatchEvent(new s.w.Event('toggle'));audio.muted=true;audio.volume=0;audio.dispatchEvent(new s.w.Event('volumechange'));assert.match(report.textContent,/Звук плеера: выключен/);assert.match(report.textContent,/Громкость: 0%/);assert.ok(!report.textContent.includes('/recording'));
 currentCall={...call,ai:ai({transcript_run_id:null})};await s.ui.refresh('chat-one',false,true);assert.equal(bubble.querySelector('audio'),audio);assert.equal(bubble.querySelector('.call-audio-diagnostic'),report);assert.equal(report.open,true);
});
