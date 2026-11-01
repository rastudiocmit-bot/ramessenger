"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web');
async function setup(t){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',url:'https://cabinet.example.test'}),w=dom.window,$=s=>w.document.querySelector(s),calls=[];
 w.HTMLElement.prototype.scrollIntoView=()=>{};
 let settings={folder_id:'folder',configured:true,voice_enabled:true,voice_max_seconds:180,vault_ready:true,status:'active',deepseek:{configured:true,status:'active'},
  gateways:{recognition_provider:'yandex',recognition_language:'ru',synthesis_provider:'disabled',synthesis_voice:'Mia',synthesis_voices:['Mia'],mimo:{configured:false},mimo_plan:{configured:false},grok:{configured:false}},tasks:[{id:'voice_input',title:'Голосовой ввод'},{id:'text_processing',title:'Обработка текста'},{id:'tasks',title:'Задачи из сообщений'}],default_model:'yandexgpt-5.1',default_prompt:'Исправь текст',models:[
   {id:'yandexgpt-5.1',title:'Yandex Pro',provider:'yandex'},{id:'deepseek-flash',title:'DeepSeek Flash',provider:'deepseek'},{id:'deepseek-v4-pro',title:'DeepSeek Pro',provider:'deepseek'},{id:'mimo-v2.6-flash',title:'MiMo Flash',provider:'mimo'},{id:'mimo-plan-v2.6-pro',title:'MiMo Pro Plan',provider:'mimo_plan'},{id:'grok-4.7',title:'Grok 4.7',provider:'grok'},{id:'grok-4.6',title:'Grok 4.6',provider:'grok'}]};
 let agents=[],failures=new Map(),checkGate=null,diagnostic={ok:false,http_status:403,code:'forbidden',message:'xAI запретил доступ с сервера.',models:[],checked_at:'2026-11-01T08:26:56Z'};
 const api=async(url,args={})=>{calls.push({url,...args});if(url.endsWith('/settings')){if(args.data)settings={...settings,...args.data};return settings;}
  if(url.endsWith('/gateways/grok/diagnostic')||url.endsWith('/gateways/grok/proxy-diagnostic')){if(checkGate)await checkGate;return diagnostic;}
  if(url.endsWith('/gateways/grok/proxy')){if(checkGate)await checkGate;if(failures.has('grok:proxy'))throw Error('Не удалось сохранить настройки.');const item=settings.gateways.grok;
   item.proxy=args.data.clear_proxy?{configured:false,enabled:false,protocol:'socks5',endpoint:''}:{configured:!!args.data.proxy||item.proxy?.configured,enabled:args.data.enabled,protocol:args.data.protocol,endpoint:'8.8.4.4:8080'};
   delete item.checks;delete item.models;return settings;}
  if(url.endsWith('/gateways/routes')){settings.gateways={...settings.gateways,...args.data};return settings;}
  if(/\/gateways\/(mimo|mimo_plan|grok)(\/verify)?$/.test(url)){if(url.endsWith('/verify')&&checkGate)await checkGate;const provider=url.split('/gateways/')[1].split('/')[0];if(url.endsWith('/verify'))settings.gateways[provider].checks={...settings.gateways[provider].checks,[args.data.capability]:{ok:!failures.has(provider+':'+args.data.capability),error_code:failures.get(provider+':'+args.data.capability)||null}};else settings.gateways[provider]={...settings.gateways[provider],configured:!args.data.clear_key,key_hint:args.data.api_key?.slice(-4)||'',...args.data,api_key:undefined};return settings;}
  if(url.endsWith('/agents/test'))return {text:'Готово',cost_rub:.01};
  if(url.endsWith('/agents')){if(args.data){agents=[{id:'agent',...args.data,task_title:'Голосовой ввод',model_title:'DeepSeek Flash',version:1}];return agents[0];}return {agents};}
  throw Error(url);};
 w.eval(fs.readFileSync(web+'/ai.js','utf8'));
 const state={workspace:{id:'w',role:'owner'}};const notices=[];
 const app=w.CenterAi({$,state,api,node:(tag,text,cls)=>{const el=w.document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el;},show:(el,on=true)=>el.hidden=!on,notice:(text,target)=>{notices.push(text);if(target)target.textContent=text;},toast:()=>{},confirm:async()=>true});
 t.after(()=>w.close());await app.load();
 const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
 return {w,$,calls,app,flush,state,notices,setDiagnostic:value=>{diagnostic=value;},holdChecks:()=>{let release;checkGate=new Promise(resolve=>release=resolve);return ()=>{checkGate=null;release();};},fail:(provider,capability,code)=>{if(code)failures.set(provider+':'+capability,code);else failures.delete(provider+':'+capability);}};
}
test('voice providers and agent editor occupy separate connection tabs',async t=>{
 const s=await setup(t);
 assert.equal(s.$('#ai-cloud-card').closest('[role=tabpanel]').id,'connections-voice-view');
 assert.equal(s.$('#ai-deepseek-card').closest('[role=tabpanel]').id,'connections-voice-view');
 assert.equal(s.$('#ai-agent-form').closest('[role=tabpanel]').id,'connections-agents-view');
 assert.equal(s.$('#ai-cloud-card').closest('.pane').id,'pane-connections');
 assert.equal(s.$('#ai-agent-form').closest('.pane').id,'pane-connections');
 assert.equal(s.$('#max-add').closest('[role=tabpanel]').id,'connections-settings-view');
 assert.equal(s.w.document.querySelectorAll('.connection-tabs [role=tab]').length,5);
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

test('Grok key is saved separately and its single text check becomes active',async t=>{
 const s=await setup(t),card=s.$('[data-gateway="grok"]'),form=card.querySelector('form');
 assert.equal(card.closest('[role=tabpanel]').id,'connections-voice-view');
 assert.equal(card.querySelector('[data-verify=text]').disabled,true);
 form.elements.api_key.value='xai-synthetic-test-only';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 const saved=s.calls.find(c=>c.url.endsWith('/gateways/grok'));
 assert.equal(saved.data.protocol,'responses');assert.equal(saved.data.base_url,'https://api.x.ai/v1');
 assert.equal(form.elements.api_key.value,'');assert.equal(s.calls.filter(c=>c.url.endsWith('/verify')).length,0);
 card.querySelector('[data-verify=text]').click();await s.flush();
 assert.equal(s.calls.find(c=>c.url.endsWith('/grok/verify')).data.capability,'text');
 assert.equal(card.querySelector('.gateway-status').dataset.state,'active');
 assert.match(card.querySelector('.gateway-status').title,/1 из 1/);
 assert.equal(s.$('#ai-routes-form').elements.recognition_provider.querySelector('[value=grok]'),null);
 assert.equal(s.$('#ai-routes-form').elements.synthesis_provider.querySelector('[value=grok]'),null);
 card.querySelector('.gateway-clear').click();await s.flush();
 assert.equal(card.querySelector('[data-verify=text]').disabled,true);
 assert.equal(card.querySelector('.gateway-status').textContent,'Не подключено');
});

test('every generic agent task can select and test the exact Grok model',async t=>{
 const s=await setup(t);s.$('#ai-agent-new').click();const form=s.$('#ai-agent-form');
 for(const task of ['voice_input','text_processing','tasks']){
  form.elements.task.value=task;form.querySelector('[name=provider][value=grok]').click();
  assert.deepEqual([...form.elements.model.options].map(o=>o.value),['grok-4.7','grok-4.6']);
  form.elements.model.value='grok-4.6';s.$('#ai-agent-sample').value='Пример текста';
  s.$('#ai-agent-test').click();await s.flush();
  const call=s.calls.filter(c=>c.url.endsWith('/agents/test')).at(-1);
  assert.equal(call.data.task,task);assert.equal(call.data.model,'grok-4.6');
 }
 form.dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await s.flush();
 assert.equal(s.calls.find(c=>c.method==='POST'&&c.url.endsWith('/agents')).data.model,'grok-4.6');
});

test('Grok access report shows exact HTTP refusal without an inference request',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),form=card.querySelector('form'),button=card.querySelector('.gateway-diagnostic');
 assert.equal(button.disabled,true);
 form.elements.api_key.value='xai-synthetic-test-only';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 button.click();await s.flush();const result=card.querySelector('.gateway-diagnostic-result');
 assert.match(result.textContent,/HTTP 403/);assert.match(result.textContent,/генерация текста не запускалась/);
 assert.equal(result.dataset.state,'error');assert.equal(s.calls.filter(c=>c.url.endsWith('/verify')).length,0);
 const request=s.calls.find(c=>c.url.endsWith('/grok/diagnostic'));
 assert.equal(request.method,'POST');assert.equal(request.data,undefined);
 s.setDiagnostic({ok:true,http_status:200,message:'Доступен каталог.',models:['grok-4.7'],checked_at:'2026-11-01T08:30:00Z'});
 button.click();await s.flush();assert.match(result.textContent,/HTTP 200/);assert.match(result.textContent,/grok-4.7/);
 assert.equal(card.querySelector('.gateway-status').dataset.state,'','catalogue access never marks inference as verified');
 card.querySelector('[data-verify=text]').click();await s.flush();
 assert.equal(result.hidden,true);assert.equal(result.textContent,'','a fresh inference check clears the older access report');
});

test('an unsaved Grok key cannot accidentally test the previously saved key',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),form=card.querySelector('form');
 form.elements.api_key.value='xai-original-test-only';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 form.elements.api_key.value='xai-new-unsaved-test-only';
 card.querySelector('[data-verify=text]').click();card.querySelector('.gateway-diagnostic').click();await s.flush();
 assert.equal(s.calls.filter(c=>c.url.endsWith('/verify')||c.url.endsWith('/diagnostic')).length,0);
 assert.match(s.notices.at(-1),/Сначала сохраните/);assert.equal(form.elements.api_key.value,'xai-new-unsaved-test-only');
});

test('legacy Grok unauthorized never falsely proves an invalid key and account diagnostics remain read only',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),form=card.querySelector('form');
 form.elements.api_key.value='xai-synthetic-test-only';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 s.fail('grok','text','unauthorized');card.querySelector('[data-verify=text]').click();await s.flush();
 assert.match(card.querySelector('[data-check=text]').textContent,/Доступ к xAI не подтверждён/);
 assert.doesNotMatch(card.querySelector('.gateway-status').title,/Ключ отклонён/);
 s.setDiagnostic({ok:false,http_status:403,message:'Каталог запрещён.',account:{http_status:200,code:'recognized',message:'xAI распознал сохранённый ключ.'},models:[],checked_at:'2026-11-01T09:30:00Z'});
 card.querySelector('.gateway-diagnostic').click();await s.flush();
 const result=card.querySelector('.gateway-diagnostic-result');
 assert.match(result.textContent,/Ответ xAI: HTTP 403/);assert.match(result.textContent,/Состояние ключа: HTTP 200/);
 assert.match(result.textContent,/распознал сохранённый ключ/);assert.equal(card.querySelector('.gateway-status').dataset.state,'error');
 assert.equal(s.calls.filter(c=>c.url.endsWith('/verify')).length,1);
 const mimo=s.$('[data-gateway=mimo]'),mimoForm=mimo.querySelector('form');
 mimoForm.elements.api_key.value='synthetic-mimo';mimoForm.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 s.fail('mimo','text','unauthorized');mimo.querySelector('[data-verify=text]').click();await s.flush();
 assert.equal(mimo.querySelector('[data-check=text]').textContent,'Ключ отклонён');
});

test('a late Grok access report cannot enter a different workspace',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),form=card.querySelector('form');
 form.elements.api_key.value='xai-synthetic-test-only';form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 const release=s.holdChecks();card.querySelector('.gateway-diagnostic').click();await s.flush();
 s.state.workspace.id='other';await s.app.load();release();await s.flush();
 assert.equal(card.querySelector('.gateway-diagnostic-result').hidden,true);
 assert.doesNotMatch(card.querySelector('.gateway-diagnostic-result').textContent,/403/);
});

test('one-off proxy check sends secrets only in its POST and clears the draft without verifying inference',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),keyForm=card.querySelector('.gateway-form'),form=card.querySelector('.grok-proxy-form');
 assert.equal(form.querySelector('[type=submit]').disabled,true);
 keyForm.elements.api_key.value='xai-synthetic-key-only';keyForm.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 assert.equal(form.querySelector('[type=submit]').disabled,false);
 s.setDiagnostic({ok:true,http_status:200,message:'Каталог доступен через прокси. Рабочие запросы агентов пока не изменены.',proxy:{protocol:'socks5',tls_verified:true,message:'TLS проверен.'},models:['grok-4.7'],checked_at:'2026-11-01T10:00:00Z'});
 form.elements.proxy.value='8.8.4.4:8080:synthetic:synthetic-password-only';form.elements.protocol.value='socks5';
 const release=s.holdChecks();form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));
 assert.equal(form.elements.proxy.value,'');assert.equal(form.elements.proxy.type,'password');
 assert.equal(card.querySelector('.gateway-diagnostic').disabled,true);assert.equal(card.querySelector('[data-verify=text]').disabled,true);
 release();await s.flush();
 const request=s.calls.find(c=>c.url.endsWith('/proxy-diagnostic'));
 assert.equal(request.method,'POST');assert.equal(request.data.protocol,'socks5');assert.match(request.data.proxy,/synthetic-password-only$/);
 const result=card.querySelector('.grok-proxy-result');assert.match(result.textContent,/HTTP 200/);assert.match(result.textContent,/SOCKS5/);
 assert.doesNotMatch(card.textContent,/synthetic-password-only/);assert.equal(card.querySelector('.gateway-status').dataset.state,'');
 assert.equal(s.calls.filter(c=>c.url.endsWith('/verify')).length,0);assert.equal(s.w.localStorage.length,0);
 form.elements.proxy.value='another-sensitive-draft';s.state.workspace.id='other';await s.app.load();
 assert.equal(form.elements.proxy.value,'');assert.equal(result.hidden,true);assert.equal(result.textContent,'');
});

test('late proxy response is discarded and an unsaved Grok key blocks the proxy check',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),keyForm=card.querySelector('.gateway-form'),form=card.querySelector('.grok-proxy-form');
 keyForm.elements.api_key.value='xai-synthetic-key-only';keyForm.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 keyForm.elements.api_key.value='xai-unsaved-key-only';form.elements.proxy.value='8.8.4.4:8080:u:synthetic-password';
 form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 assert.equal(s.calls.filter(c=>c.url.endsWith('/proxy-diagnostic')).length,0);assert.match(s.notices.at(-1),/Сначала сохраните/);
 keyForm.elements.api_key.value='';const release=s.holdChecks();form.dispatchEvent(new s.w.Event('submit',{cancelable:true}));
 s.state.workspace.id='other';await s.app.load();release();await s.flush();
 assert.equal(card.querySelector('.grok-proxy-result').hidden,true);assert.equal(form.elements.proxy.value,'');
});

test('saving Grok proxy changes the shared route without an inference request and removal restores direct mode',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),keyForm=card.querySelector('.gateway-form'),form=card.querySelector('.grok-proxy-form');
 keyForm.elements.api_key.value='xai-synthetic-key-only';keyForm.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 form.elements.proxy.value='8.8.4.4:8080:u:synthetic-password';form.elements.enabled.checked=true;form.elements.protocol.value='socks5';
 card.querySelector('.grok-proxy-save').click();await s.flush();
 const request=s.calls.find(c=>c.url.endsWith('/grok/proxy'));assert.equal(request.method,'PUT');assert.equal(request.data.enabled,true);
 assert.equal(form.elements.proxy.value,'');assert.match(card.querySelector('.grok-route-note').textContent,/SOCKS5.*8.8.4.4:8080/);
 assert.equal(card.querySelector('.grok-proxy-saved-address').value,'8.8.4.4:8080');
 assert.equal(card.querySelector('.grok-proxy-saved-address').readOnly,true);
 assert.equal(card.querySelector('.grok-proxy-saved-state').textContent,'Сохранён · включён');
 await s.app.load();
 assert.equal(card.querySelector('.grok-proxy-saved-address').value,'8.8.4.4:8080');
 assert.match(card.querySelector('.grok-proxy-saved-description').textContent,/SOCKS5.*пароль сохранён/);
 assert.equal(card.querySelector('.gateway-status').textContent,'Не проверено');assert.equal(s.calls.filter(c=>c.url.endsWith('/verify')).length,0);
 assert.doesNotMatch(card.textContent,/synthetic-password/);assert.equal(card.querySelector('.grok-proxy-remove').disabled,false);
 card.querySelector('.grok-proxy-remove').click();await s.flush();
 assert.equal(s.calls.filter(c=>c.url.endsWith('/grok/proxy')).at(-1).data.clear_proxy,true);
 assert.match(card.querySelector('.grok-route-note').textContent,/прямое подключение/);assert.equal(form.elements.enabled.checked,false);
 assert.equal(card.querySelector('.grok-proxy-remove').disabled,true);
 assert.equal(card.querySelector('.grok-proxy-saved-address').value,'');
 assert.equal(card.querySelector('.grok-proxy-saved-state').textContent,'Не сохранён');
});

test('saved disabled proxy remains visible after reload and a failed save keeps its password draft private',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),keyForm=card.querySelector('.gateway-form'),form=card.querySelector('.grok-proxy-form');
 keyForm.elements.api_key.value='xai-synthetic-key-only';keyForm.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 form.elements.proxy.value='8.8.4.4:8080:u:synthetic-password';form.elements.enabled.checked=false;
 card.querySelector('.grok-proxy-save').click();await s.flush();await s.app.load();
 assert.equal(card.querySelector('.grok-proxy-saved-address').value,'8.8.4.4:8080');
 assert.equal(card.querySelector('.grok-proxy-saved-state').textContent,'Сохранён · выключен');
 form.elements.proxy.value='8.8.4.4:8080:u:replacement-password';s.fail('grok','proxy','network');
 card.querySelector('.grok-proxy-save').click();await s.flush();
 assert.equal(form.elements.proxy.value,'8.8.4.4:8080:u:replacement-password');
 assert.equal(form.elements.proxy.type,'password');assert.doesNotMatch(card.textContent,/replacement-password/);
 assert.equal(card.querySelector('.grok-proxy-saved-address').value,'8.8.4.4:8080');
 s.state.workspace.id='other';const loaded=s.app.load();
 assert.equal(card.querySelector('.grok-proxy-saved-address').value,'');
 assert.equal(card.querySelector('.grok-proxy-saved-description').textContent,'');
 assert.equal(card.querySelector('.grok-key-label').textContent,'API-ключ');
 assert.equal(card.querySelector('.grok-proxy-address').hidden,true);
 await loaded;assert.equal(form.elements.proxy.value,'');
});

test('Grok proxy protocol detection cannot become a permanent route and late saves cannot enter another workspace',async t=>{
 const s=await setup(t),card=s.$('[data-gateway=grok]'),keyForm=card.querySelector('.gateway-form'),form=card.querySelector('.grok-proxy-form');
 keyForm.elements.api_key.value='xai-synthetic-key-only';keyForm.dispatchEvent(new s.w.Event('submit',{cancelable:true}));await s.flush();
 form.elements.protocol.value='auto';form.elements.proxy.value='8.8.4.4:8080:u:synthetic-password';card.querySelector('.grok-proxy-save').click();await s.flush();
 assert.equal(s.calls.filter(c=>c.url.endsWith('/grok/proxy')).length,0);assert.match(s.notices.at(-1),/выберите HTTP CONNECT или SOCKS5/);
 form.elements.protocol.value='socks5';form.elements.enabled.checked=true;const release=s.holdChecks();card.querySelector('.grok-proxy-save').click();
 s.state.workspace.id='other';await s.app.load();release();await s.flush();
 assert.equal(form.elements.proxy.value,'');assert.doesNotMatch(card.querySelector('.grok-route-note').textContent,/8.8.4.4/);
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
 assert.equal(form.elements.recognition_language.value,'ru');
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
