"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web');
async function setup(t){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',url:'https://cabinet.example.test'}),w=dom.window,$=s=>w.document.querySelector(s),calls=[];
 w.HTMLElement.prototype.scrollIntoView=()=>{};
 let settings={folder_id:'folder',configured:true,voice_enabled:true,voice_max_seconds:180,vault_ready:true,status:'active',deepseek:{configured:true,status:'active'},
  gateways:{recognition_provider:'yandex',recognition_language:'ru',synthesis_provider:'disabled',synthesis_voice:'Mia',synthesis_voices:['Mia'],mimo:{configured:false},mimo_plan:{configured:false}},tasks:[{id:'voice_input',title:'Голосовой ввод'},{id:'text_processing',title:'Обработка текста'}],default_model:'yandexgpt-5.1',default_prompt:'Исправь текст',models:[
   {id:'yandexgpt-5.1',title:'Yandex Pro',provider:'yandex'},{id:'deepseek-flash',title:'DeepSeek Flash',provider:'deepseek'},{id:'deepseek-v4-pro',title:'DeepSeek Pro',provider:'deepseek'},{id:'mimo-v2.6-flash',title:'MiMo Flash',provider:'mimo'},{id:'mimo-plan-v2.6-pro',title:'MiMo Pro Plan',provider:'mimo_plan'}]};
 let agents=[],failures=new Map(),checkGate=null;
 const api=async(url,args={})=>{calls.push({url,...args});if(url.endsWith('/settings')){if(args.data)settings={...settings,...args.data};return settings;}
  if(url.endsWith('/gateways/routes')){settings.gateways={...settings.gateways,...args.data};return settings;}
  if(/\/gateways\/(mimo|mimo_plan)(\/verify)?$/.test(url)){if(url.endsWith('/verify')&&checkGate)await checkGate;const provider=url.includes('mimo_plan')?'mimo_plan':'mimo';if(url.endsWith('/verify'))settings.gateways[provider].checks={...settings.gateways[provider].checks,[args.data.capability]:{ok:!failures.has(provider+':'+args.data.capability),error_code:failures.get(provider+':'+args.data.capability)||null}};else settings.gateways[provider]={...settings.gateways[provider],configured:!args.data.clear_key,key_hint:args.data.api_key?.slice(-4)||'',...args.data,api_key:undefined};return settings;}
  if(url.endsWith('/agents/test'))return {text:'Готово',cost_rub:.01};
  if(url.endsWith('/agents')){if(args.data){agents=[{id:'agent',...args.data,task_title:'Голосовой ввод',model_title:'DeepSeek Flash',version:1}];return agents[0];}return {agents};}
  throw Error(url);};
 w.eval(fs.readFileSync(web+'/ai.js','utf8'));
 const app=w.CenterAi({$,state:{workspace:{id:'w',role:'owner'}},api,node:(tag,text,cls)=>{const el=w.document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el;},show:(el,on=true)=>el.hidden=!on,notice:()=>{},toast:()=>{},confirm:async()=>true});
 t.after(()=>w.close());await app.load();
 const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
 return {w,$,calls,app,flush,holdChecks:()=>{let release;checkGate=new Promise(resolve=>release=resolve);return ()=>{checkGate=null;release();};},fail:(provider,capability,code)=>{if(code)failures.set(provider+':'+capability,code);else failures.delete(provider+':'+capability);}};
}
test('voice providers and agent editor occupy separate connection tabs',async t=>{
 const s=await setup(t);
 assert.equal(s.$('#ai-cloud-card').closest('[role=tabpanel]').id,'connections-voice-view');
 assert.equal(s.$('#ai-deepseek-card').closest('[role=tabpanel]').id,'connections-voice-view');
 assert.equal(s.$('#ai-agent-form').closest('[role=tabpanel]').id,'connections-agents-view');
 assert.equal(s.$('#ai-cloud-card').closest('.pane').id,'pane-connections');
 assert.equal(s.$('#ai-agent-form').closest('.pane').id,'pane-connections');
 assert.equal(s.$('#max-add').closest('[role=tabpanel]').id,'connections-settings-view');
 assert.equal(s.w.document.querySelectorAll('.connection-tabs [role=tab]').length,4);
});
test('provider switches available models and the exact selected model is tested and saved',async t=>{
 const s=await setup(t);s.$('#ai-agent-new').click();
 const form=s.$('#ai-agent-form'),radio=form.querySelector('[name=provider][value=deepseek]');radio.click();
 assert.equal(form.elements.provider.value,'deepseek');
 assert.deepEqual([...form.elements.model.options].map(o=>o.value),['deepseek-flash','deepseek-v4-pro']);
 s.$('#ai-agent-sample').value='ну привет';s.$('#ai-agent-test').click();await s.flush();
 assert.equal(s.calls.find(c=>c.url.endsWith('/agents/test')).data.model,'deepseek-flash');
 form.dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await s.flush();
 assert.equal(s.calls.find(c=>c.method==='POST'&&c.url.endsWith('/agents')).data.model,'deepseek-flash');
 await s.app.load();s.$('#ai-agent-list button').click();
 assert.equal(form.elements.provider.value,'deepseek');assert.equal(form.elements.model.value,'deepseek-flash');
 form.querySelector('[name=provider][value=yandex]').click();assert.equal(form.elements.model.value,'yandexgpt-5.1');
});
test('recording duration is submitted and restored from saved settings',async t=>{
 const s=await setup(t),form=s.$('#ai-voice-options-form');
 assert.equal(form.elements.voice_max_seconds.value,'180');form.elements.voice_max_seconds.value='90';
 form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 assert.equal(s.calls.find(c=>c.method==='PUT'&&c.url.endsWith('/settings')).data.voice_max_seconds,90);
 await s.app.load();assert.equal(form.elements.voice_max_seconds.value,'90');
});

test('MiMo accounts expose separate cards and tests for each capability',async t=>{
 const s=await setup(t),card=s.$('[data-gateway="mimo_plan"]'),form=card.querySelector('form');
 assert.match(s.$('#connections-voice-tab').textContent,/ИИ-шлюзы/);
 assert.equal(s.$('#voice-agent-link'),null);
 form.elements.api_key.value='tp-synthetic-test-only';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 const saved=s.calls.find(c=>c.url.endsWith('/gateways/mimo_plan'));
 assert.equal(saved.data.api_key,'tp-synthetic-test-only');assert.equal(saved.data.protocol,'chat');
 assert.equal(form.elements.api_key.value,'');assert.equal(card.querySelector('[data-verify="recognition"]').disabled,false);
 card.querySelector('[data-verify="recognition"]').click();await s.flush();
 assert.equal(s.calls.find(c=>c.url.endsWith('/mimo_plan/verify')).data.capability,'recognition');
 assert.match(card.querySelector('[data-check="recognition"]').textContent,/Работает/);
 assert.equal(card.querySelector('[data-check="text"]').textContent,'Не проверено');
 assert.equal(s.$('[data-gateway="mimo"] [data-verify="text"]').disabled,true);
});
test('recognition routing carries provider and language and remains separate from agent model',async t=>{
 const s=await setup(t),form=s.$('#ai-routes-form');
 form.elements.recognition_provider.value='mimo_plan';form.elements.recognition_provider.dispatchEvent(new s.w.Event('change'));
 assert.equal(form.elements.recognition_language.value,'auto');
 form.elements.recognition_language.value='en';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 const call=s.calls.find(c=>c.url.endsWith('/gateways/routes'));
 assert.equal(call.data.recognition_provider,'mimo_plan');assert.equal(call.data.recognition_language,'en');
 s.$('#ai-agent-new').click();const agent=s.$('#ai-agent-form');agent.querySelector('[name=provider][value=mimo_plan]').click();
 agent.elements.task.value='text_processing';s.$('#ai-agent-sample').value='Hello';s.$('#ai-agent-test').click();await s.flush();
 const tested=s.calls.find(c=>c.url.endsWith('/agents/test'));
 assert.equal(tested.data.model,'mimo-plan-v2.6-pro');assert.equal(tested.data.task,'text_processing');
 assert.equal(agent.elements.provider.value,'mimo_plan');
});

test('both MiMo badges become active only when all capabilities pass and survive reload',async t=>{
 const s=await setup(t);
 for(const provider of ['mimo','mimo_plan']){
  const card=s.$(`[data-gateway="${provider}"]`),form=card.querySelector('form');
  form.elements.api_key.value=provider==='mimo'?'sk-synthetic-test':'tp-synthetic-test';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
  assert.equal(card.querySelector('.gateway-status').textContent,'Не проверено');
  card.querySelector('[data-verify=text]').click();await s.flush();
  assert.equal(card.querySelector('.gateway-status').textContent,'Проверено 1 из 3');
  assert.equal(card.querySelector('.gateway-status').dataset.state,'partial');
  card.querySelector('.gateway-verify-all').click();
  assert.equal(card.querySelector('.gateway-status').dataset.state,'pending');
  assert.equal(card.querySelector('[data-verify=synthesis]').disabled,true);
  await s.flush();
  assert.equal(card.querySelector('.gateway-status').textContent,'Работает');assert.equal(card.querySelector('.gateway-status').dataset.state,'active');
  assert.deepEqual(s.calls.filter(c=>c.url.endsWith(`/gateways/${provider}/verify`)).slice(-3).map(c=>c.data.capability),['text','recognition','synthesis']);
  await s.app.load();assert.equal(card.querySelector('.gateway-status').dataset.state,'active');
 }
});
test('provider forms and common recording settings save independently',async t=>{
 const s=await setup(t),voice=s.$('#ai-voice-options-form'),cloud=s.$('#ai-settings-form');
 assert.equal(cloud.elements.voice_enabled,undefined);assert.equal(voice.elements.api_key,undefined);
 cloud.elements.api_key.value='synthetic-yandex';cloud.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 const credentials=s.calls.find(c=>c.method==='PUT'&&c.url.endsWith('/settings'));
 assert.equal(credentials.data.api_key,'synthetic-yandex');assert.equal('voice_enabled' in credentials.data,false);
 voice.elements.voice_max_seconds.value='60';voice.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 const options=s.calls.filter(c=>c.method==='PUT'&&c.url.endsWith('/settings')).at(-1);
 assert.equal(options.data.voice_max_seconds,60);assert.equal('api_key' in options.data,false);
 assert.equal(s.$('#ai-cloud-card').closest('.gateway-section').querySelector('h3').textContent,'Подключённые сервисы');
});

test('a failed capability marks the whole gateway as error and a successful retry restores active',async t=>{
 const s=await setup(t),card=s.$('[data-gateway="mimo_plan"]'),form=card.querySelector('form');
 form.elements.api_key.value='tp-synthetic';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 s.fail('mimo_plan','synthesis','no_balance');card.querySelector('.gateway-verify-all').click();await s.flush();
 assert.equal(card.querySelector('.gateway-status').dataset.state,'error');
 assert.equal(card.querySelector('.gateway-status').textContent,'Есть ошибки');
 assert.match(card.querySelector('[data-check=synthesis]').textContent,/Недостаточно средств/);
 assert.equal(card.querySelector('[data-check=text]').textContent,'Работает');
 s.fail('mimo_plan','synthesis',null);card.querySelector('[data-verify=synthesis]').click();await s.flush();
 assert.equal(card.querySelector('.gateway-status').dataset.state,'active');
});

test('reloading settings ends the old check-all sequence before any further paid requests',async t=>{
 const s=await setup(t),card=s.$('[data-gateway="mimo_plan"]'),form=card.querySelector('form');
 form.elements.api_key.value='tp-synthetic';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 const release=s.holdChecks();card.querySelector('.gateway-verify-all').click();await s.flush();
 assert.equal(card.querySelector('.gateway-status').dataset.state,'pending');
 await s.app.load();release();await s.flush();
 assert.equal(s.calls.filter(c=>c.url.endsWith('/mimo_plan/verify')).length,1);
 assert.notEqual(card.querySelector('.gateway-status').dataset.state,'pending');
});
