// Real Chromium wheel/touch gestures against intercepted synthetic APIs only.
const assert=require('node:assert/strict'),{chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{for(const width of JSON.parse(process.env.CM_TEST_WIDTHS||'[1920,1440,800,390]')){
  const context=await browser.newContext({viewport:{width,height:900},isMobile:width<720,hasTouch:width<720});const data=await fixture(context);
  const contacts=Array.from({length:60},(_,i)=>({id:'contact-'+i,name:'Клиент '+String(i+1).padStart(2,'0'),status:i%2?'lead':'client',students:['Ученик '+(i+1)],student_count:1,groups:[],group_count:0,max_connected:true,max_bot_connected:true}));
  let reads=0;
  await context.route('**/api/workspaces/test/directory?*',route=>{reads++;return route.fulfill({contentType:'application/json',body:JSON.stringify({contacts,total:60,branches:[],snapshot:'run-1',page:0,has_more:false})});});
  const page=await context.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');
  if(width<720){await page.locator('#more-tab').click();await page.locator('#navigation-options [data-pane=clients]').click();}else await page.locator('#navrail [data-pane=clients]').click();
  await page.waitForSelector('#directory-list .contact-row:nth-child(60)');await page.evaluate(()=>document.fonts.ready);
  const metrics=()=>page.locator('#pane-clients').evaluate(p=>{const s=p.querySelector('.scroll'),list=p.querySelector('.directory-list-panel');return {outer:s.scrollTop,max:s.scrollHeight-s.clientHeight,list:list.scrollTop,listMax:list.scrollHeight-list.clientHeight,overflow:getComputedStyle(list).overflowY,overscroll:getComputedStyle(list).overscrollBehaviorY,body:document.scrollingElement.scrollTop};});
  const before=await metrics();assert.ok(before.max>600,'A long list must overflow the page');
  const box=await page.locator('#directory-list .contact-row').first().boundingBox();
  await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.wheel(0,550);await page.waitForTimeout(350);
  const down=await metrics();assert.ok(down.outer>before.outer+100,'Wheel over client rows must scroll the page: '+JSON.stringify({width,before,down}));assert.equal(down.list,0,'No hidden nested list scrollbar');assert.equal(down.body,0,'The document stays fixed');
  const stream=await page.locator('#pane-clients .scroll').boundingBox();await page.mouse.move(stream.x+stream.width/2,stream.y+stream.height/2);await page.mouse.wheel(0,-350);await page.waitForTimeout(350);assert.ok((await metrics()).outer<down.outer-100,'Wheel also moves up over the list');
  // Rows and buttons under the pointer must not swallow the wheel, even near the end.
  await page.mouse.wheel(0,10000);await page.waitForTimeout(500);const end=await metrics();assert.ok(end.outer>=end.max-2,'The final client is reachable');
  await page.locator('#directory-list .contact-select').last().click();await page.waitForSelector('#directory-contact-dialog .student-card');await page.locator('[data-close=directory-contact-dialog]').click();
  const afterModal=await metrics();assert.ok(Math.abs(afterModal.outer-end.outer)<2,'Closing a client keeps the current list position');await page.mouse.move(stream.x+stream.width/2,stream.y+stream.height/2);await page.mouse.wheel(0,-500);await page.waitForTimeout(350);assert.ok((await metrics()).outer<end.outer-100,'Wheel resumes after closing the client');
  if(width<720){
   const touch=await context.newCDPSession(page);const y=stream.y+stream.height*.7,x=stream.x+stream.width/2;const initial=await metrics();
   await touch.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
   for(let step=1;step<=8;step++){await touch.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y-step*30}]});await page.waitForTimeout(16);}
   await touch.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await page.waitForTimeout(400);
   assert.ok((await metrics()).outer>initial.outer+50,'Touch swipes also scroll the clients list');
  }
  assert.equal(reads,1,'Scrolling cached rows must not read CRM or refetch the directory');assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);console.log('clients wheel '+width+': ok');await context.close();
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
