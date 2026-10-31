"use strict";
const {test}=require('node:test'), assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const script=fs.readFileSync(path.join(__dirname,'../app/backend/src/center_messenger/web/call-history.js'),'utf8');
const call={id:'00000000-0000-4000-8000-000000000001',occurred_at:'2026-10-31T00:00:00Z',direction:'in',missed:false,talk_seconds:65,employees:['Synthetic operator'],recording_available:true,version:1};
function setup(t,api){
 const dom=new JSDOM('<div id="history"></div>',{runScripts:'outside-only'}),w=dom.window;
 w.CSS={escape:value=>value};w.HTMLMediaElement.prototype.pause=function(){this.pausedByTest=true;};w.HTMLMediaElement.prototype.play=function(){this.playedByTest=true;return Promise.resolve();};
 const node=(tag,text,cls)=>{const e=w.document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
 const state={epoch:1,workspace:{id:'workspace-one',timezone:'Europe/Moscow'}};let selected='chat-one',changes=0;
 w.eval(script);const ui=w.CenterCallHistory({state,api,node,context:()=>selected,changed:()=>changes++});
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
 assert.equal(bubble.querySelector('img'),null);bubble.querySelector('audio').dispatchEvent(new s.w.Event('error'));
 assert.match(bubble.textContent,/Повторить загрузку аудио/);assert.equal(bubble.querySelector('.call-audio-error').hidden,false);
});
