// Full cabinet, synthetic keys and intercepted API. No real provider requests.
const assert=require('node:assert/strict'),{chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const reports=[];
 for(const width of [1440,390,320]){
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),data=await fixture(context,'owner',['messages','connections'],{voice:true});
  data.ai.settings.tasks.push({id:'tasks',title:'Задачи из сообщений'});
  for(const task of ['voice_input','text_processing','tasks'])data.ai.agents.push({id:'agent-'+task,name:'Synthetic '+task,task,task_title:task,model:'yandexgpt-5.1',model_title:'Yandex',prompt:'Сохраняй факты.',temperature:.1,enabled:true,is_default:true,version:1});
  const phone={name:'Телефонные разговоры',version:1,model:'yandexgpt-5.1',recognition_provider:'yandex',temperature:.1,prompt:'Кратко опиши разговор.',history:[],models:data.ai.settings.models,recognizers:[{id:'yandex',title:'Yandex Cloud',configured:true}]},phoneWrites=[];
  await context.route('**/api/workspaces/*/integrations/novofon/**',async route=>{
   const request=route.request(),p=new URL(request.url()).pathname,body=request.method()==='GET'?null:request.postDataJSON();let value;
   if(body)phoneWrites.push({path:p,body});
   if(p.endsWith('/agent')){if(body)Object.assign(phone,body,{version:phone.version+1});value=phone;}
   else if(p.endsWith('/agent/test'))value={summary:'Обсудили перенос занятия.',version:phone.version,cost_rub:.01};
   else if(p.endsWith('/processing'))value={version:0,auto_new:false,counts:{},archive:{enabled:false,version:0,imported:0}};
   else return route.fallback();
   return route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
  });
  const page=await context.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');await page.locator('.chat-row').first().waitFor();
  await page.locator('#connections-tab').evaluate(e=>e.click());await page.locator('#connections-voice-tab').click();
  const card=page.locator('[data-gateway=grok]');await card.waitFor();await card.locator('[name=api_key]').fill('xai-synthetic-browser-key-only');await card.locator('[type=submit]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-gateway=grok] [data-verify=text]').disabled);
  assert.equal(await card.locator('[name=api_key]').inputValue(),'');
  assert.equal(data.mutations.filter(c=>c.path.endsWith('/grok/verify')).length,0);
  await card.locator('[data-verify=text]').click();await page.waitForFunction(()=>document.querySelector('[data-gateway=grok] .gateway-status').dataset.state==='active');
  assert.match(await card.locator('[data-grok-models]').textContent(),/grok-4.7/);
  assert.ok(await card.evaluate(e=>e.scrollWidth<=e.clientWidth+1),'gateway card overflow at '+width);
  if(width===1440||width===390)await card.screenshot({path:'/tmp/cm-grok-card-'+width+'.png'});
  await page.locator('#connections-agents-tab').click();
  const choose=async task=>{if(width<700&&!await page.locator(`[data-agent-purpose="${task}"]`).isVisible())await page.locator('#agents-back').click();await page.locator(`[data-agent-purpose="${task}"]`).click();await page.locator('[data-agent-section=settings]').click();};
  for(const task of ['voice_input','text_processing','tasks']){
   await choose(task);await page.locator('#ai-agent-form [name=provider][value=grok]').check();
   await page.locator('#ai-agent-form [name=model]').selectOption('grok-4.6');await page.locator('#ai-agent-form [type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('#ai-agent-draft-status').textContent.includes('Сохранена версия 2'));
   await page.locator('[data-agent-section=check]').click();await page.locator('#ai-agent-sample').fill('Завтра нужно уточнить расписание.');await page.locator('#ai-agent-test').click();await page.locator('#ai-agent-result:not([hidden])').waitFor();
   const request=data.mutations.filter(c=>c.path.endsWith('/agents/test')).at(-1);
   assert.equal(request.body.task,task);assert.equal(request.body.model,'grok-4.6');
  }
  await choose('telephone_conversations');await page.locator('#telephony-agent-form [name=model]').selectOption('grok-4.7');await page.locator('#telephony-agent-form [type=submit]').click();
  await page.waitForFunction(()=>document.querySelector('#telephony-agent-version').textContent==='Версия 2');
  assert.equal(phone.model,'grok-4.7');assert.equal(phone.recognition_provider,'yandex');
  await page.locator('[data-agent-section=check]').click();await page.locator('#telephony-agent-sample').fill('Обсудили перенос занятия.');await page.locator('#telephony-agent-test').click();
  await page.waitForFunction(()=>document.querySelector('#telephony-agent-result').textContent==='Обсудили перенос занятия.');
  assert.equal(phoneWrites.length,2);assert.equal(phoneWrites[0].body.model,'grok-4.7');assert.equal(phoneWrites[1].body.expected_version,2);
  assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'page overflow');
  reports.push({width,savedKey:true,textVerified:true,genericTasks:3,phoneModelSaved:true,phoneTested:true,noOverflow:true});await context.close();
 }finally{await browser.close();}
 }
 console.log(JSON.stringify({reports}));
})().catch(error=>{console.error(error);process.exitCode=1;});
