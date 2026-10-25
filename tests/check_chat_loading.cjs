// Synthetic CRM data and a local HTTP event stream; no real customers or deliveries.
const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
const shots=process.env.CM_CHAT_LOADING_SHOTS;
if(shots)fs.mkdirSync(shots,{recursive:true});
async function scenario(width){
 const connections=new Set();let revision=1,refuse=false;
 const stamp=()=>({version:`${revision}:2026-10-26`,card_version:'1',write_access:true});
 const write=(response,type)=>response.write(`event: ${type}\ndata: ${JSON.stringify(stamp())}\n\n`);
 const server=http.createServer((request,response)=>{
  response.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store','Access-Control-Allow-Origin':'http://cabinet.example.test','Access-Control-Allow-Credentials':'true'});
  if(refuse){response.end();return;}
  connections.add(response);write(response,'ready');
  const heartbeat=setInterval(()=>write(response,'pulse'),1000);
  request.on('close',()=>{clearInterval(heartbeat);connections.delete(response);});
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const context=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720,reducedMotion:'reduce'});
  await context.addInitScript(()=>{
   try{localStorage.setItem('cm-sound',JSON.stringify({enabled:false,volume:.5,client:'soft',lead:'soft',archive:'soft'}));}catch(_){/* Initial about:blank has no storage origin. */}
   const native=setTimeout.bind(window),clear=clearTimeout.bind(window),timers=new Map();
   window.setTimeout=(callback,delay,...args)=>{const id=native(()=>{timers.delete(id);callback(...args);},delay);timers.set(id,{callback,delay,args});return id;};
   window.clearTimeout=id=>{timers.delete(id);clear(id);};
   window.tickCard=async()=>{const entry=[...timers.entries()].find(([,timer])=>timer.delay===60000);if(!entry)throw Error('No card timer');window.clearTimeout(entry[0]);await entry[1].callback(...entry[1].args);};
  });
  const data=await fixture(context,'owner',['messages','connections'],{protocol:2,hostname:'127.0.0.1'}),requests=[],cardBodies=[];
  const make=n=>{const at=new Date(Date.UTC(2026,9,24)+n*60000).toISOString();return {id:`history-${n}`,text:`История ${n}. `+'Расписание занятий. '.repeat(n%4+1),direction:n%3?'in':'out',actor_type:n%15===0?'automation':n%3?'contact':'human',template_name:'Напоминание',state:n%3?'received':'read',occurred_at:at,sort_time:String(BigInt(Date.parse(at))*1000n),crm_order:String(n),version:1,attachments:[]};};
  const messages=Array.from({length:160},(_,n)=>make(n+1));let seq=160;
  const original=data.conversations[0];data.conversations.splice(0,data.conversations.length,...Array.from({length:1500},(_,n)=>({...original,id:`client-${n}`,contact_id:`contact-${n}`,name:`Семья ${String(n).padStart(4,'0')}`,students:[`Ребёнок ${n}`],unread:n<3,unread_count:n<3?1:0,last_message_at:new Date(Date.UTC(2026,9,25)-n*60000).toISOString()})));
  const selected=()=>data.conversations.find(row=>row.id==='client-0');
  const listing=url=>{const page=Number(url.searchParams.get('through_page')||0),limit=Number(url.searchParams.get('window')||90);return {version:stamp().version,conversations:data.conversations.slice(0,limit),page,total:1500,unread:data.conversations.filter(row=>row.unread).length,has_more:limit<1500};};
  const history=(rows,more=false)=>({conversation:selected(),messages:rows,next_seq:seq,total:messages.length,has_more:more,more_changes:false,removed_ids:[]});
  await context.route('**/chats/bootstrap?*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({context:{version:stamp().version,channels:[{id:'native',kind:'alfa_max_browser',name:'CRM'}],branches:[],preferences:{name_order:'parent_first'}},listing:listing(new URL('http://fixture/?window=90')),history:history(messages.slice(-40),true)})}));
  await context.route('**/chats?*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify(listing(new URL(route.request().url())))}));
  await context.route('**/chats/*/messages?*',async route=>{
   const url=new URL(route.request().url());let rows=[],more=false;
   if(url.searchParams.has('after_seq'))rows=Number(url.searchParams.get('after_seq'))<seq?[messages.at(-1)]:[];
   else{
    const end=url.searchParams.has('before_id')?messages.findIndex(message=>message.id===url.searchParams.get('before_id')):messages.length;
    const start=Math.max(0,end-Number(url.searchParams.get('limit')||20));rows=messages.slice(start,end);more=start>0;
    await new Promise(resolve=>setTimeout(resolve,80));
   }
   await route.fulfill({contentType:'application/json',body:JSON.stringify(history(rows,more))});
  });
  await context.route('**/chat-updates*',route=>{
   const url=new URL(route.request().url()),after=url.searchParams.get('after_seq');
   const updates=after!==null&&Number(after)<seq?[{id:'client-0',new_inbound:true,status:'client'}]:[];
   return route.fulfill({contentType:'application/json',body:JSON.stringify({updates,next_seq:seq,has_more:false})});
  });
  await context.route('**/chat-events',route=>route.continue());
  const subs=Array.from({length:16},(_,n)=>({id:`tariff-${n}`,name:`Предмет ${n+1}`,balance:'1000',begin:'2026-10-01',end:'2027-06-30'}));
  let card={id:'contact-0',source_contact_id:'contact-0',name:'Семья 0000',today:'2026-10-26',phones:[],emails:[],version:'a'.repeat(64),cache:{ready:true,refreshing:false,auto_refresh:true,updated_at:'2026-10-26T08:00:00Z'},students:[1,2].map(n=>({id:`child-${n}`,name:`Ребёнок ${n}`,dob:'2020-01-01',balance:'2000',paid_lesson_count:8,subscriptions:subs,groups:[],cards:[{id:String(n),branch_id:'1',branch_name:'Филиал',status:'client',lessons_ready:true,subscriptions_ready:true,url:`https://example.s20.online/customer/view?id=${n}`}]}))};
  await context.route('**/chats/*/family*',route=>{
   const url=new URL(route.request().url());assert.equal(url.searchParams.get('compact'),'true');
   const body=JSON.stringify(url.searchParams.get('known_version')===card.version?{not_modified:true,version:card.version}:card);cardBodies.push(body.length);
   return route.fulfill({contentType:'application/json',body});
  });
  context.on('request',request=>{const url=new URL(request.url());if(url.pathname.startsWith('/api/'))requests.push(url);});
  const page=await context.newPage();page.setDefaultTimeout(12000);
  await page.goto(`http://127.0.0.1:${server.address().port}/?view=messages`);await page.waitForSelector('.chat-row');
  await page.waitForFunction(()=>document.querySelectorAll('.chat-bubble').length===40);await page.waitForTimeout(500);
  assert.equal(connections.size,1,'native EventSource reached the local server');
  assert.equal(requests.filter(url=>url.pathname.endsWith('/auth/status')).length,0);
  assert.equal(requests.filter(url=>url.pathname.endsWith('/bootstrap')).length,1);
  assert.equal(requests.filter(url=>url.pathname.endsWith('/context')).length,0);
  assert.equal(requests.filter(url=>url.pathname.endsWith('/messages')).length,0,'no redundant initial timeline request');
  for(let count=180;count<=1530;count+=90){
   await page.locator('.chat-list-scroll').evaluate(scroll=>{scroll.scrollTop=scroll.scrollHeight;scroll.dispatchEvent(new Event('scroll'));});
   await page.waitForFunction(count=>Number(document.querySelector('#chat-visible-count').textContent.split(' ')[0])>=Math.min(count,1500),count);
  }
  const rendered=await page.locator('.chat-row').count();assert.ok(rendered<120,`${rendered} nodes for 1500 dialogs`);
  await page.locator('.chat-list-scroll').evaluate(scroll=>{scroll.scrollTop=scroll.scrollHeight/2;});await page.waitForTimeout(150);
  const anchor=await page.locator('#chat-list').evaluate(list=>{const bounds=document.querySelector('.chat-list-scroll').getBoundingClientRect(),row=[...list.children].find(node=>node.dataset.key&&node.getBoundingClientRect().top>bounds.top+15);return {id:row.dataset.key,top:row.getBoundingClientRect().top};});
  const last=data.conversations.at(-1);last.unread=true;last.unread_count=1;last.last_message_at='2026-10-26T10:00:00Z';
  data.conversations.sort((a,b)=>Number(b.unread)-Number(a.unread)||Date.parse(b.last_message_at)-Date.parse(a.last_message_at));
  revision++;for(const response of connections)write(response,'change');
  const beforeRefresh=requests.filter(url=>url.pathname.endsWith('/chats')).length;
  await page.waitForFunction(()=>document.querySelector('.chat-row[aria-current="true"]')||document.querySelector('#chat-visible-count').textContent==='1500 из 1500');
  const deadline=Date.now()+5000;while(requests.filter(url=>url.pathname.endsWith('/chats')).length<=beforeRefresh&&Date.now()<deadline)await page.waitForTimeout(30);
  await page.waitForTimeout(180);
  const anchorAfter=await page.locator(`#chat-list [data-key="${anchor.id}"]`).evaluate(row=>row.getBoundingClientRect().top);
  assert.ok(Math.abs(anchorAfter-anchor.top)<=1,`list drift ${anchorAfter-anchor.top}`);
  await page.locator('.chat-list-scroll').evaluate(scroll=>{scroll.scrollTop=0;});await page.waitForTimeout(120);
  await page.locator('.chat-row[data-id="client-0"] .chat-select').click();
  await page.waitForSelector('#chat-stream',{state:'visible'});
  const historyAnchor=await page.locator('#chat-stream').evaluate(scroll=>{
   scroll.scrollTop=900;const bounds=scroll.getBoundingClientRect(),node=[...document.querySelectorAll('.chat-bubble')].find(node=>node.getBoundingClientRect().top>bounds.top+100&&node.getBoundingClientRect().bottom<bounds.bottom-100);
   const result={id:node.dataset.id,top:node.getBoundingClientRect().top};scroll.dispatchEvent(new WheelEvent('wheel',{deltaY:-100}));return result;
  });
  await page.waitForFunction(()=>document.querySelectorAll('.chat-bubble').length>=80);
  const historyAfter=await page.locator(`.chat-bubble[data-id="${historyAnchor.id}"]`).evaluate(node=>node.getBoundingClientRect().top);
  assert.ok(Math.abs(historyAfter-historyAnchor.top)<=1,`history drift ${historyAfter-historyAnchor.top}`);
  for(let pageNumber=0;pageNumber<5&&await page.locator('.chat-bubble').count()<160;pageNumber++){
   await page.locator('#chat-stream').evaluate(scroll=>{scroll.scrollTop=0;scroll.dispatchEvent(new WheelEvent('wheel',{deltaY:-100}));});await page.waitForTimeout(160);
  }
  assert.deepEqual(await page.locator('.chat-bubble').evaluateAll(nodes=>nodes.map(node=>node.dataset.id)),messages.map(message=>message.id));
  messages.push({...make(161),text:'Новое сообщение через поток'});seq=161;selected().revision++;revision++;for(const response of connections)write(response,'change');
  await page.waitForFunction(()=>document.querySelectorAll('.chat-bubble').length===161);
  await page.locator('#chat-person-open').click();await page.waitForSelector('.family-stats');await page.locator('.family-kid').nth(1).click();
  await page.evaluate(()=>{const child=document.querySelectorAll('.family-kid')[1];child.focus();const scroll=document.querySelector('.family-scroll');scroll.scrollTop=250;window.cardBefore={child,stats:document.querySelector('.family-stats'),top:scroll.scrollTop};});
  card={...card,version:'b'.repeat(64),cache:{...card.cache,updated_at:'2026-10-26T08:01:00Z'}};
  await page.evaluate(()=>window.tickCard());await page.evaluate(()=>window.tickCard());
  const stable=await page.evaluate(()=>({same:cardBefore.stats===document.querySelector('.family-stats'),focus:cardBefore.child===document.activeElement,drift:document.querySelector('.family-scroll').scrollTop-cardBefore.top,outer:getComputedStyle(document.querySelector('.family-drawer')).boxShadow}));
  assert.deepEqual(stable,{same:true,focus:true,drift:0,outer:'none'});
  assert.ok(cardBodies.at(-1)<150,'unchanged card response is small');
  for(const theme of ['light','dark']){await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);if(shots)await page.screenshot({path:`${shots}/${width}-${theme}.png`});}
  await page.locator('[data-close="chat-details-dialog"]').click();await page.waitForFunction(()=>!document.querySelector('#chat-details-dialog').open);
  refuse=true;for(const response of connections)response.end();messages.push({...make(162),text:'Новое сообщение через резервный опрос'});seq=162;
  await page.waitForFunction(()=>document.querySelectorAll('.chat-bubble').length===162);
  assert.equal(await page.locator('#chat-error').textContent(),'');assert.equal(await page.locator('#toast').count(),0);
  assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);assert.equal(data.mutations.length,0);
  return {width,bootstrap_requests:1,dialogs:1500,rendered_rows:rendered,list_anchor_drift:anchorAfter-anchor.top,history_anchor_drift:historyAfter-historyAnchor.top,messages:162,card_stable:stable,unchanged_card_bytes:cardBodies.at(-1),native_stream:true,poll_fallback:true};
 }finally{await browser.close();for(const response of connections)response.end();await new Promise(resolve=>server.close(resolve));}
}
(async()=>{const checks=[];for(const width of [1440,390])checks.push(await scenario(width));console.log(JSON.stringify({checks,ok:true},null,2));})().catch(error=>{console.error(error);process.exitCode=1;});
