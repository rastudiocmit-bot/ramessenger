// Real Chromium, synthetic app members and passwords; every API is intercepted.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),{fixture}=require('./browser_fixture.cjs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
 try{
  const reports=[];
  for(const width of [1440,390]){
   const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),data=await fixture(context,'owner',['messages','connections','security']);
   const members=[{id:'00000000-0000-4000-8000-000000000001',name:'Анна Петрова'},{id:'00000000-0000-4000-8000-000000000002',name:'Иван Смирнов'}],writes=[];
   let version=0,lines=[];
   await context.route('**/api/workspaces/*/integrations/novofon**',async route=>{
    const req=route.request(),url=new URL(req.url());
    if(url.pathname.endsWith('/processing'))return route.fulfill({contentType:'application/json',body:JSON.stringify({version:0,auto_new:false,counts:{},archive:{enabled:false,version:0,imported:0,next_at:null}})});
    if(url.pathname.endsWith('/agent'))return route.fulfill({contentType:'application/json',body:JSON.stringify({version:0,name:'Телефонные разговоры',prompt:'Кратко опиши разговор.',temperature:.1,models:[],recognizers:[],history:[],model:'',recognition_provider:'yandex',vault_ready:true})});
    if(url.pathname.endsWith('/sip-lines')){
     if(req.method()==='PUT'){
      const value=req.postDataJSON();writes.push(value);assert.equal(value.expected_version,version);version++;
      const id=value.line_id||'00000000-0000-4000-8000-000000000010';
      const {password,...publicValue}=value;
      lines=lines.filter(x=>x.id!==id).concat([{...publicValue,id,member_name:members.find(m=>m.id===value.member_id).name,member_active:true,password_configured:true,registration_status:'not_checked'}]);
     }
     return route.fulfill({contentType:'application/json',body:JSON.stringify({version,lines,members,vault_ready:true,browser_call_ready:false})});
    }
    return route.fulfill({contentType:'application/json',body:JSON.stringify(url.pathname.endsWith('/loader')?{enabled:true,history_days:7,poll_seconds:60,version:0,running:false,call_count:0}:{configured:false,vault_ready:true,version:0,sample_hours:24,status:'not_configured',checks:{},browser_call_ready:false})});
   });
   const page=await context.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');await page.locator('#connections-tab').evaluate(e=>e.click());await page.locator('#connections-novofon-tab').click();
   await page.locator('#sip-lines-add:not([disabled])').waitFor();await page.locator('#sip-lines-add').click();
   const form=page.locator('#sip-lines-form');await form.locator('[name=member_id]').selectOption(members[1].id);
   await form.locator('[name=sip_number]').fill('0004567');await form.locator('[name=extension]').fill('004');await form.locator('[name=password]').fill('synthetic-browser-password');
   const formOverflow=await form.evaluate(e=>e.scrollWidth-e.clientWidth);assert.ok(formOverflow<=1);
   if(process.env.CM_APPEARANCE_SCREENSHOT_DIR){fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR,{recursive:true});await form.locator('[name=password]').fill('');await page.locator('#sip-lines-card').screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`sip-form-${width}.png`)});await form.locator('[name=password]').fill('synthetic-browser-password');}
   await page.locator('#sip-lines-save').click();await form.waitFor({state:'hidden'});assert.equal(writes[0].sip_number,'0004567');assert.equal(writes[0].member_id,members[1].id);assert.equal(writes[0].extension,'004');
   assert.equal(await form.locator('[name=password]').inputValue(),'');assert.ok(!(await page.locator('#sip-lines-list').textContent()).includes('synthetic-browser-password'));
   await page.locator('.sip-line-actions button').first().click();await form.locator('[name=extension]').fill('005');await page.locator('#sip-lines-save').click();await form.waitFor({state:'hidden'});assert.equal(writes[1].password,null);assert.ok(writes[1].line_id);
   const overflow=await page.locator('#sip-lines-card').evaluate(e=>e.scrollWidth-e.clientWidth);assert.ok(overflow<=1);
   if(process.env.CM_APPEARANCE_SCREENSHOT_DIR)await page.locator('#sip-lines-card').screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`sip-saved-${width}.png`)});
   assert.deepEqual(data.errors,[]);reports.push({width,writes:writes.length,formOverflow,overflow,leadingZerosKept:true,providerRequests:0});await page.close();
  }
  console.log(JSON.stringify({browser:browser.version(),reports},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
