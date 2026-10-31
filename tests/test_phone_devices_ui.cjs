"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const source=fs.readFileSync(path.join(__dirname,'../app/backend/src/center_messenger/web/phone-devices.js'),'utf8');
const flush=()=>new Promise(r=>setImmediate(r));
function setup(t,opts={}){
 const dom=new JSDOM('<button data-phone-devices>Проверить звук</button><div class="call-recording"><audio></audio></div>',{url:'https://cabinet.example.test',runScripts:'outside-only'}),w=dom.window;
 const tracks=[{stopped:0,stop(){this.stopped++}}],stream={getTracks:()=>tracks},blobs=new Map(),revoked=[],calls=[];
 Object.defineProperty(w,'isSecureContext',{value:true});
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
 w.HTMLMediaElement.prototype.pause=function(){calls.push('pause')};w.HTMLMediaElement.prototype.load=function(){};w.HTMLMediaElement.prototype.play=function(){calls.push('play');return opts.play?opts.play():Promise.resolve();};
 w.HTMLMediaElement.prototype.setSinkId=async function(id){calls.push(['sink',id]);if(opts.sink)await opts.sink(id);this.sinkId=id;};
 w.URL.createObjectURL=b=>{const id='blob:local-'+blobs.size;blobs.set(id,b);return id;};w.URL.revokeObjectURL=id=>revoked.push(id);
 Object.defineProperty(w.navigator,'mediaDevices',{value:{enumerateDevices:async()=>[{kind:'audioinput',deviceId:'mic-one',label:'Тестовый микрофон'},{kind:'audiooutput',deviceId:'speaker-one',label:'Тестовая гарнитура'}],getUserMedia:opts.microphone||async function(data){calls.push(['microphone',data]);return stream;},addEventListener(){}}});
 class Recorder extends w.EventTarget{static isTypeSupported(){return true}constructor(){super();this.state='inactive';this.mimeType='audio/webm'}start(){this.state='recording'}stop(){this.state='inactive';queueMicrotask(()=>{const e=new w.Event('dataavailable');e.data=new w.Blob(['synthetic-audio'],{type:this.mimeType});this.dispatchEvent(e);this.dispatchEvent(new w.Event('stop'));});}}
 w.MediaRecorder=Recorder;w.fetch=()=>{throw Error('No network allowed')};w.eval(source);
 const state={user:{id:'u1'},workspace:{id:'w1'},epoch:1},ui=w.CenterPhoneDevices({state});
 t.after(()=>{ui.reset();w.close()});const $=s=>w.document.querySelector(s);
 return{w,$,state,ui,tracks,blobs,revoked,calls};
}
test('device check opens only for an authenticated workspace and never claims line readiness',async t=>{
 const u=setup(t);u.state.user=null;u.$('[data-phone-devices]').click();assert.equal(u.$('dialog'),null);
 u.state.user={id:'u1'};u.$('[data-phone-devices]').click();await flush();assert.equal(u.$('dialog').open,true);
 assert.match(u.$('dialog').textContent,/Разговоры через браузер ещё не подключены/);assert.equal(u.calls.some(x=>Array.isArray(x)&&x[0]==='microphone'),false);
});
test('speaker signal is generated locally without microphone permission and released on close',async t=>{
 const u=setup(t);u.ui.open();u.$('[data-phone-check=speaker]').click();await flush();assert.equal(u.blobs.size,1);
 assert.equal([...u.blobs.values()][0].type,'audio/wav');assert.equal(u.calls.includes('play'),true);assert.equal(u.calls.some(x=>Array.isArray(x)&&x[0]==='microphone'),false);
 u.$('.phone-devices-heading button').click();assert.equal(u.$('dialog').open,false);assert.equal(u.$('dialog audio').hasAttribute('src'),false);assert.equal(u.revoked.length,1);
});
test('an unresolved microphone permission cannot keep recording after close',async t=>{
 let done;const u=setup(t,{microphone:()=>new Promise(r=>done=r)});u.ui.open();u.$('[data-phone-check=microphone]').click();await flush();
 u.$('.phone-devices-heading button').click();done({getTracks:()=>u.tracks});await flush();assert.ok(u.tracks[0].stopped>0);assert.equal(u.blobs.size,0);
});
test('recording is bounded, stopped, locally replayed and deleted on workspace reset',async t=>{
 const u=setup(t);u.ui.open();u.$('[data-phone-check=microphone]').click();await flush();assert.match(u.$('.phone-devices-message').textContent,/5 секунд/);
 u.$('[data-phone-check=microphone]').click();await flush();await flush();assert.ok(u.tracks[0].stopped>0);assert.equal(u.blobs.size,1);assert.match(u.$('.phone-devices-message').textContent,/ваш голос/);
 u.state.workspace={id:'w2'};u.state.epoch++;u.ui.reset();assert.equal(u.$('dialog').open,false);assert.equal(u.$('dialog audio').hasAttribute('src'),false);assert.equal(u.revoked.length,1);
});
test('permission denial and autoplay rejection stay visible without reporting success',async t=>{
 const u=setup(t,{microphone:async()=>{throw new u.w.DOMException('Denied','NotAllowedError')},play:async()=>{throw new Error('blocked')}});u.ui.open();
 u.$('[data-phone-check=microphone]').click();await flush();assert.match(u.$('.phone-devices-message').textContent,/запрещён/);assert.equal(u.$('[data-phone-check=microphone]').disabled,false);
 u.$('[data-phone-check=speaker]').click();await flush();assert.match(u.$('.phone-devices-message').textContent,/Нажмите ▶/);
});
test('output choice applies to call players and future playback, then resets with the session',async t=>{
 const u=setup(t);u.ui.open();await flush();const select=u.$('[aria-label="Устройство вывода звука"]');select.value='speaker-one';select.dispatchEvent(new u.w.Event('change'));await flush();
 assert.equal(u.$('.call-recording audio').sinkId,'speaker-one');const later=u.w.document.createElement('audio');await u.state.prepareCallAudio(later);assert.equal(later.sinkId,'speaker-one');
 u.ui.reset();await u.state.prepareCallAudio(later);assert.equal(later.sinkId,'');
});
test('a disconnected output is rolled back instead of silently reporting a new device',async t=>{
 const u=setup(t,{sink:async id=>{if(id)throw Error('unavailable')}});u.ui.open();await flush();const select=u.$('[aria-label="Устройство вывода звука"]');select.value='speaker-one';select.dispatchEvent(new u.w.Event('change'));await flush();await flush();
 assert.equal(select.value,'');assert.match(u.$('.phone-devices-message').textContent,/Не удалось/);assert.equal(u.$('[data-phone-check=speaker]').disabled,false);
});
test('a stale microphone result cannot enter a different workspace',async t=>{
 let done;const u=setup(t,{microphone:()=>new Promise(r=>done=r)});u.ui.open();u.$('[data-phone-check=microphone]').click();await flush();u.state.workspace={id:'w2'};u.state.epoch++;u.ui.reset();u.ui.open();
 done({getTracks:()=>u.tracks});await flush();assert.ok(u.tracks[0].stopped>0);assert.equal(u.blobs.size,0);assert.equal(u.$('.phone-devices-message').textContent,'');
});
