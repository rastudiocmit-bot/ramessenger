// Full cabinet with synthetic API interception. No live accounts or paid calls.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const reports=[];
  for(const width of [1440,1024,390]){
   const context=await browser.newContext({viewport:{width,height:1000},hasTouch:width<700,reducedMotion:'reduce'}),data=await fixture(context,'owner',undefined,{voice:true});
   data.ai.settings.tasks.push({id:'tasks',title:'Задачи из сообщений'});data.ai.settings.task_prompts={tasks:'Сформулируй задачу'};data.ai.settings.monthly_limit_rub=500;
   data.ai.agents.push({id:'agent-voice',name:'Чистовик голосового ответа',task:'voice_input',task_title:'Голосовой ввод',model:'yandexgpt-5.1',model_title:'YandexGPT Pro 5.1',prompt:'Сохрани смысл, исправь ошибки и убери слова-паразиты.',temperature:.2,enabled:true,is_default:true,version:1},
    {id:'agent-tasks',name:'Задачи из сообщений',task:'tasks',task_title:'Задачи из сообщений',model:'yandexgpt-5.1',model_title:'YandexGPT Pro 5.1',prompt:'Сформулируй понятную задачу по сообщению.',temperature:.1,enabled:true,is_default:true,version:2});
   const phone={name:'Телефонные разговоры',version:2,model:'yandexgpt-5.1',recognition_provider:'yandex',temperature:.1,prompt:'Кратко опиши телефонный разговор: цель обращения, важные факты, договорённости и дальнейшие действия. Не назначай роли говорящим, если они не подтверждены. Не составляй ответ клиенту и не запускай никаких действий.',history:[],models:data.ai.settings.models,recognizers:[{id:'yandex',title:'Yandex Cloud',configured:true}]};
   let processing={version:1,auto_new:false,counts:{complete:12,queued:4,needs_review:1,budget_wait:2},archive:{enabled:false,version:1,imported:17,next_at:null}};const phoneWrites=[];
   await context.route('**/api/workspaces/*/integrations/novofon/*',async route=>{
    const p=new URL(route.request().url()).pathname;let value;
    if(route.request().method()!=='GET')phoneWrites.push({path:p,body:route.request().postDataJSON()});
    if(p.endsWith('/agent'))value=phone;
    else if(p.endsWith('/processing'))value=processing;
    else if(p.endsWith('/archive')){processing={...processing,archive:{...processing.archive,enabled:route.request().postDataJSON().enabled,version:2,waiting_for_night:true,next_at:'2026-10-31T21:00:00Z'}};value=processing;}
    else if(p.endsWith('/agent/test'))value={summary:'Обсудили занятие, время пока не согласовано.',version:2,cost_rub:.01};
    else return route.fallback();
    return route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
   });
   const page=await context.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');
   await page.evaluate(()=>document.querySelector('#connections-tab').click());await page.locator('#connections-agents-tab').click();
   await page.waitForFunction(()=>document.querySelector('#ai-agent-form [name=name]').value==='Чистовик голосового ответа');
   const choose=async task=>{if(width<700&&!await page.locator(`[data-agent-purpose="${task}"]`).isVisible())await page.locator('#agents-back').click();await page.locator(`[data-agent-purpose="${task}"]`).click();};
   await choose('voice_input');assert.equal(await page.locator('#ai-agent-form').isVisible(),true);
   assert.equal(await page.locator('#telephony-agent-card').isVisible(),false);assert.equal(data.mutations.length,0);assert.equal(phoneWrites.length,0);
   await page.locator('[data-agent-section=check]').click();await page.locator('#ai-agent-sample').fill('Ну здравствуйте, завтра занятие в пять часов.');await page.locator('#ai-agent-test').click();await page.waitForSelector('#ai-agent-result:not([hidden])');
   assert.equal(data.mutations.filter(c=>c.path.endsWith('/agents/test')).length,1);
   await page.locator('[data-agent-section=settings]').click();
   const save=async name=>{if(process.env.CM_APPEARANCE_SCREENSHOT_DIR){fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR,{recursive:true});await page.locator('#agents-workspace').screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`${name}-${width}.png`),animations:'disabled'});}};
   await save('agents-voice');
   await choose('tasks');assert.equal(await page.locator('#ai-agent-form [name=task]').inputValue(),'tasks');
   await choose('telephone_conversations');await page.waitForFunction(()=>document.querySelector('#telephony-agent-form [name=prompt]').value.length>0);
   const dimensions=await page.locator('#telephony-agent-form').evaluate(form=>{const a=form.elements.prompt.getBoundingClientRect(),b=form.getBoundingClientRect();return {instruction:a.width,form:b.width,height:a.height};});
   assert.ok(Math.abs(dimensions.instruction-dimensions.form)<2);await save('agents-phone-settings');
   await page.locator('[data-agent-section=check]').click();assert.equal(await page.locator('#telephony-agent-sample').isVisible(),true);assert.equal(await page.locator('#telephony-agent-form').isVisible(),false);await save('agents-phone-check');
   await page.locator('[data-agent-section=processing]').click();assert.equal(await page.locator('#telephony-processing-card').isVisible(),true);assert.equal(await page.locator('#telephony-agent-card').isVisible(),false);
   await page.locator('#telephony-archive-start').click();await page.waitForFunction(()=>!document.querySelector('#telephony-archive-pause').disabled);
   assert.equal(phoneWrites.length,1);assert.ok(phoneWrites[0].path.endsWith('/archive'));assert.equal(await page.locator('#telephony-archive-pause').isDisabled(),false);
   if(width<700)await page.locator('#agents-back').click();
   await page.locator('#agents-reload').click();await page.waitForFunction(()=>document.querySelector('#telephony-agent-version').textContent==='Версия 2');
   await choose('telephone_conversations');assert.equal(await page.locator('[data-agent-section=processing]').getAttribute('aria-pressed'),'true');
   assert.equal(phoneWrites.length,1);
   await save('agents-phone-processing');
   const overflow=await page.locator('#pane-connections>.scroll').evaluate(e=>e.scrollWidth-e.clientWidth);assert.ok(overflow<=1,`Horizontal overflow ${width}: ${overflow}`);
   if(width===1440){await page.evaluate(()=>document.documentElement.dataset.theme='dark');await save('agents-phone-processing-dark');}
   assert.deepEqual(data.errors,[]);reports.push({width,instruction:dimensions,overflow,paidTests:data.mutations.filter(c=>c.path.endsWith('/agents/test')).length,archiveStarts:phoneWrites.length});
   // Single-process serverless Chromium keeps contexts until browser.close().
   await page.close();
  }
  console.log(JSON.stringify({browser:browser.version(),reports},null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
