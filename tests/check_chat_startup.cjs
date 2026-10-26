// Synthetic authenticated startup, native EventSource, no customer data.
const assert=require('node:assert/strict'),http=require('node:http');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');

async function scenario(width,partial){
 const streams=new Set(),version='1:2026-10-26';
 const server=http.createServer((request,response)=>{
  response.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store'});
  const write=type=>response.write(`event: ${type}\ndata: ${JSON.stringify({version,card_version:'1',write_access:true})}\n\n`);
  streams.add(response);write('ready');const heartbeat=setInterval(()=>write('pulse'),1000);
  request.on('close',()=>{clearInterval(heartbeat);streams.delete(response);});
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
  args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const context=await browser.newContext({viewport:{width,height:1000},isMobile:width<720,hasTouch:width<720});
  const data=await fixture(context,'owner',['messages'],{protocol:2,hostname:'127.0.0.1'});
  let bootstraps=0,histories=0,lists=0;
  const listing=()=>({version,conversations:data.conversations,page:0,total:40,unread:3,has_more:false});
  await context.route('**/chat-events',route=>route.continue());
  await context.route('**/chats/bootstrap?*',async route=>{
   bootstraps++;
   if(!partial)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Сервис временно недоступен.'})});
   await route.fulfill({contentType:'application/json',body:JSON.stringify({context:{version,channels:[{id:'native',kind:'alfa_max_browser',name:'CRM'}],branches:[]},listing:listing(),history:null,history_pending:true})});
  });
  await context.route('**/chats?*',route=>{lists++;return route.fulfill({contentType:'application/json',body:JSON.stringify(listing())});});
  await context.route('**/chats/*/messages?*',route=>{
   histories++;
   if(!partial&&histories===1)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Сервис временно недоступен.'})});
   const id=new URL(route.request().url()).pathname.split('/').at(-2);
   return route.fulfill({contentType:'application/json',body:JSON.stringify({conversation:data.conversations.find(row=>row.id===id),messages:data.messages,total:4,next_seq:4,has_more:false,more_changes:false})});
  });
  const page=await context.newPage();page.setDefaultTimeout(15000);
  await page.goto(`http://127.0.0.1:${server.address().port}/?view=messages`);
  await page.waitForSelector('.chat-row');
  if(partial){
   assert.equal(histories,0,'the list paints before the deferred timeline request');
   assert.equal(await page.locator('#chat-error').textContent(),'');
  }else{
   await page.waitForFunction(()=>document.querySelector('#chat-error').textContent.includes('Сервис временно недоступен'));
   await page.waitForTimeout(3000);
   assert.ok(lists>=2,'a successful list refresh ran while history was backing off');
   assert.match(await page.locator('#chat-error').textContent(),/Сервис временно недоступен/);
  }
  await page.waitForFunction(()=>document.querySelectorAll('.chat-bubble').length===4);
  await page.waitForFunction(()=>document.querySelector('#chat-error').textContent==='');
  assert.equal(bootstraps,1);assert.equal(await page.locator('.chat-row').count(),40);
  assert.equal(streams.size,1);assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
  assert.equal(data.mutations.length,0);assert.equal(await page.locator('#toast').count(),0);
  return {width,startup:partial?'partial-history':'bootstrap-503-and-history-503',bootstraps,histories,lists,dialogs:40,messages:4,recovered:true};
 }finally{
  await browser.close();for(const response of streams)response.end();
  await new Promise(resolve=>server.close(resolve));
 }
}
(async()=>{const checks=[];checks.push(await scenario(1440,false));checks.push(await scenario(390,true));console.log(JSON.stringify({checks,ok:true},null,2));})().catch(error=>{console.error(error);process.exitCode=1;});
