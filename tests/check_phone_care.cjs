// Real Chromium, synthetic local records only; no CRM/provider requests or sends.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
const family='00000000-0000-4000-8000-000000000001',member='00000000-0000-4000-8000-000000000002',call='00000000-0000-4000-8000-000000000003',signal='a'.repeat(32);
const uuid=n=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
function wav(){const size=16000*2,b=Buffer.alloc(44+size);b.write('RIFF');b.writeUInt32LE(36+size,4);b.write('WAVEfmt ',8);b.writeUInt32LE(16,16);b.writeUInt16LE(1,20);b.writeUInt16LE(1,22);b.writeUInt32LE(8000,24);b.writeUInt32LE(16000,28);b.writeUInt16LE(2,32);b.writeUInt16LE(16,34);b.write('data',36);b.writeUInt32LE(size,40);for(let i=0;i<size/2;i++)b.writeInt16LE(Math.round(Math.sin(i/8000*440*2*Math.PI)*12000),44+i*2);return b;}
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{const reports=[];
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:1000},timezoneId:'America/New_York',reducedMotion:'reduce'}),data=await fixture(context,'owner',['messages','tasks','connections','security']),writes=[],reads=[];
  let version=1,task=null,campaigns=[];
  const labels={kinds:{callback:'Обещание перезвонить',missed:'Пропущенный звонок'},courses:{robotics:'Робототехника'},statuses:{open:'Нужно действие',working:'В работе',waiting:'Ожидание',resolved:'Доведено до результата',dismissed:'Не требуется',observed:'Без вопросов'},stages:{contact:'Обращение',offered:'Предложена группа',trial_booked:'Запись на пробное',trial_attended:'Пробное посещено',subscription:'Абонемент',renewed:'Продление'},campaigns:{trial:'После пробного'}};
  const summary={id:family,label:'Анна Смирнова · синтетический пример',phone:'+79990000000',status:'open',pending:1,reasons:['callback'],due_at:null,campaigns:[]};
  const metrics={open:2,overdue:0,recovered_missed:1,reasons:{callback:1,missed:1},demand:{robotics:2},confirmed_stages:{contact:2,offered:1,trial_booked:1,trial_attended:0,subscription:0,renewed:0},new_journey_count:2,unclassified:1};
  const board=()=>({cases:[{...summary,version},...['Семья Соколовых','Семья Ивановых'].map((label,i)=>({...summary,id:uuid(10+i),version:1,label}))],next_offset:null,has_more:false,scope:'scope-'+version,manager:true,read_only:false,settings:{version:0,enabled:true},progress:{complete:true,last_scan_at:'2026-11-01T09:00:00Z'},metrics,members:[{id:member,name:'Мария Петрова',role:'operator',sections:['messages','tasks']},{id:uuid(22),name:'Сотрудник без задач',role:'operator',sections:['messages']}],campaigns,branches:[{id:'1',name:'Центральный'}],filter_sources:['Сайт'],labels,coverage_note:'По телефонным обращениям и этапам, подтверждённым сотрудником. Не полный отчёт продаж.'});
  const detail=()=>({...summary,version,display_name:summary.label,label:summary.label,stage:'contact',journey:'new',can_edit:true,can_create_task:true,assignee_id:member,campaigns:[],events:[],linked_tasks:task?[task]:[],crm:{contacts:[],groups:[{branch_id:'1',external_id:'10',name:'Робототехника · суббота'}],refreshed_at:'2026-11-01T09:00:00Z',note:'Группы из снимка CRM. Свободные места нужно проверить.'},signals:[{id:signal,kind:'callback',title:'Уточнить места и перезвонить',call_id:call,run_id:null,quote:'Я уточню свободные места на субботу и перезвоню завтра до обеда.',at:'2026-11-01T09:00:00Z',channel:1,offset_seconds:12,state:task?'confirmed':'proposed',task_id:task?.id,suggested_due_at:'2026-11-02T13:00:00+03:00'}]});
  await context.route('**/api/workspaces/*/integrations/novofon**',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({version:0,auto_new:false,counts:{},archive:{enabled:false,version:0,imported:0},configured:false,vault_ready:true,status:'not_configured',checks:{}})}));
  await context.route('**/api/workspaces/*/chats/*/calls**',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({calls:[],connected:false})}));
  await context.route('**/api/workspaces/*/phone-care**',async route=>{
   const r=route.request(),url=new URL(r.url()),p=url.pathname,body=r.method()==='GET'?null:r.postDataJSON();reads.push(p+url.search);if(body)writes.push({p,body});let result;
   if(p.endsWith('/board'))result=board();
   else if(p.endsWith('/tasks')){assert.equal(body.assignee_id,member);assert.equal(body.due_at,'2026-11-02T10:00:00.000Z');assert.equal(body.expected_version,version);version++;task={id:uuid(40),title:body.title,status:'open',sources:[]};data.tasks.items.push({...task,assignee_id:'test',created_by:'test'});result={task,version};}
   else if(/\/cases\/[^/]+$/.test(p))result=detail();
   else if(p.endsWith('/calls'))result={calls:[{id:call,direction:'in',started_at:'2026-11-01T09:00:00Z',missed:false,talk_seconds:38}],next:null};
   else if(p.endsWith('/transcript'))result={run_id:uuid(50),text:'Канал 1 (роль не подтверждена): Здравствуйте!\nКанал 2 (роль не подтверждена): Я уточню места и перезвоню.',next_offset:null};
   else if(p.endsWith('/recording')){const b=wav();return route.fulfill({status:206,headers:{'content-type':'audio/wav','content-range':`bytes 0-${b.length-1}/${b.length}`,'accept-ranges':'bytes'},body:b});}
   else if(p.endsWith('/campaigns')){const c={id:uuid(100+campaigns.length),name:body.name,kind:body.kind};campaigns.push(c);result=c;}
   else if(p.endsWith('/library'))result={examples:[{title:'Уточнение условий',case_id:family,evidence:{quote:'Я уточню места и перезвоню.'},good:'Обещан следующий шаг',improve:'Согласовать точное время'}],next_offset:null};
   else if(p.endsWith('/digest'))result={ready:true,date:'2026-11-01',generated_at:'2026-11-01T09:00:00Z',items:[summary],metrics,note:'Сводка по известным обращениям.'};
   else throw Error('Unmocked family endpoint '+p);
   return route.fulfill({contentType:'application/json',body:JSON.stringify(result)});
  });
  const page=await context.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');await page.locator('#telephony-tab').evaluate(e=>e.click());await page.locator('.care-case-row').first().click();
  await page.getByRole('heading',{name:summary.label,exact:true}).waitFor();assert.equal(reads.some(x=>/transcript|recording/.test(x)),false);
  const taskButton=page.getByRole('button',{name:'Подготовить задачу',exact:true});await taskButton.click();const form=page.locator('.care-task-form');assert.equal(await form.locator('[name=assignee_id] option').count(),1);await form.locator('[name=due_at]').fill('2026-11-02T13:00');
  if(process.env.CM_APPEARANCE_SCREENSHOT_DIR){fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR,{recursive:true});await page.locator('#pane-telephony').screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`family-case-${width}.png`)});}
  await form.getByRole('button',{name:'Подтвердить и создать задачу',exact:true}).click();await form.waitFor({state:'detached'});assert.equal(writes.filter(x=>x.p.endsWith('/tasks')).length,1);
  await page.getByRole('button',{name:'Открыть звонок',exact:true}).click();await page.getByRole('button',{name:'Показать полный текст',exact:true}).click();await page.locator('.care-transcript').filter({hasText:'Канал 1'}).waitFor();assert.equal(page.frames().length,1);assert.equal(context.pages().length,1);
  await page.getByRole('button',{name:'Прослушать запись',exact:true}).click();await page.waitForFunction(()=>{const a=document.querySelector('.care-audio');return a&&a.currentTime>0&&a.volume===1&&!a.muted;});
  assert.equal(reads.filter(x=>x.includes('/transcript')).length,1);assert.ok(reads.some(x=>x.includes('/recording')));
  await page.getByRole('button',{name:'Открыть задачу',exact:true}).first().click();await page.locator('#task-dialog').waitFor({state:'visible'});await page.locator('#task-dialog .task-close').click();
  async function view(name,label){if(width<820)await page.locator('#phone-care-view-select').selectOption(name);else await page.getByRole('tab',{name:label,exact:true}).click();}
  await view('analytics','Результаты');await page.getByRole('heading',{name:'Семьи с упоминаниями направлений'}).waitFor();
  if(process.env.CM_APPEARANCE_SCREENSHOT_DIR)await page.locator('#pane-telephony').screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`family-results-${width}.png`)});
  await view('campaigns','Обзвон');const create=page.locator('#phone-care-content > .care-card > .care-form');
  for(const name of ['После пробного','Продолжение занятий']){await create.locator('[name=name]').fill(name);await create.getByRole('button',{name:'Создать очередь',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('#phone-care-refresh').disabled);}
  const requests=writes.filter(x=>x.p.endsWith('/campaigns'));assert.equal(requests.length,2);assert.notEqual(requests[0].body.request_id,requests[1].body.request_id);
  await view('library','Примеры разговоров');await page.getByRole('heading',{name:'Уточнение условий'}).waitFor();await view('digest','Сводка руководителя');await page.locator('#phone-care-content').filter({hasText:'Сводка по известным обращениям'}).waitFor();
  const overflow=await page.locator('#phone-care-root').evaluate(e=>e.scrollWidth-e.clientWidth);assert.ok(overflow<=1,'Horizontal overflow '+overflow);assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);reports.push({width,taskConfirmed:true,lazyAudioPlayed:true,inlineTranscript:true,campaignRequestIdsDistinct:true,overflow});await page.close();
 }
 console.log(JSON.stringify({checks:reports}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
