"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web');
async function setup(t){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',url:'https://cabinet.example.test'}),w=dom.window,$=s=>w.document.querySelector(s),calls=[];
 w.HTMLElement.prototype.scrollIntoView=()=>{};
 let settings={folder_id:'folder',configured:true,voice_enabled:true,voice_max_seconds:180,vault_ready:true,status:'active',deepseek:{configured:true,status:'active'},
  gateways:{recognition_provider:'yandex',recognition_language:'ru',synthesis_provider:'disabled',synthesis_voice:'Mia',synthesis_voices:['Mia'],mimo:{configured:false},mimo_plan:{configured:false}},tasks:[{id:'voice_input',title:'Голосовой ввод'},{id:'text_processing',title:'Обработка текста'}],default_model:'yandexgpt-5.1',default_prompt:'Исправь текст',models:[
   {id:'yandexgpt-5.1',title:'Yandex Pro',provider:'yandex'},{id:'deepseek-flash',title:'DeepSeek Flash',provider:'deepseek'},{id:'deepseek-v4-pro',title:'DeepSeek Pro',provider:'deepseek'},{id:'mimo-v2.6-flash',title:'MiMo Flash',provider:'mimo'},{id:'mimo-plan-v2.6-pro',title:'MiMo Pro Plan',provider:'mimo_plan'}]};
 let agents=[];
 const api=async(url,args={})=>{calls.push({url,...args});if(url.endsWith('/settings')){if(args.data)settings={...settings,...args.data};return settings;}
  if(url.endsWith('/gateways/routes')){settings.gateways={...settings.gateways,...args.data};return settings;}
  if(/\/gateways\/(mimo|mimo_plan)(\/verify)?$/.test(url)){const provider=url.includes('mimo_plan')?'mimo_plan':'mimo';if(url.endsWith('/verify'))settings.gateways[provider].checks={[args.data.capability]:{ok:true}};else settings.gateways[provider]={...settings.gateways[provider],configured:!args.data.clear_key,key_hint:args.data.api_key?.slice(-4)||'',...args.data,api_key:undefined};return settings;}
  if(url.endsWith('/agents/test'))return {text:'Готово',cost_rub:.01};
  if(url.endsWith('/agents')){if(args.data){agents=[{id:'agent',...args.data,task_title:'Голосовой ввод',model_title:'DeepSeek Flash',version:1}];return agents[0];}return {agents};}
  throw Error(url);};
 w.eval(fs.readFileSync(web+'/ai.js','utf8'));
 const app=w.CenterAi({$,state:{workspace:{id:'w',role:'owner'}},api,node:(tag,text,cls)=>{const el=w.document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el;},show:(el,on=true)=>el.hidden=!on,notice:()=>{},toast:()=>{},confirm:async()=>true});
 t.after(()=>w.close());await app.load();
 const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
 return {w,$,calls,app,flush};
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
 const s=await setup(t),form=s.$('#ai-settings-form');
 assert.equal(form.elements.voice_max_seconds.value,'180');form.elements.voice_max_seconds.value='90';
 form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 assert.equal(s.calls.find(c=>c.method==='PUT'&&c.url.endsWith('/settings')).data.voice_max_seconds,90);
 await s.app.load();assert.equal(form.elements.voice_max_seconds.value,'90');
});

test('MiMo accounts expose separate cards and tests for each capability',async t=>{
 const s=await setup(t),card=s.$('[data-gateway="mimo_plan"]'),form=card.querySelector('form');
 assert.match(s.$('#connections-voice-tab').textContent,/Страница и шлюзы/);
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
