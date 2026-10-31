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
   let version=0,lines=[],vault_ready=true,deletes=0;
   await context.route('**/api/workspaces/*/integrations/novofon**',async route=>{
    const req=route.request(),url=new URL(req.url());
    if(url.pathname.endsWith('/processing'))return route.fulfill({contentType:'application/json',body:JSON.stringify({version:0,auto_new:false,counts:{},archive:{enabled:false,version:0,imported:0,next_at:null}})});
    if(url.pathname.endsWith('/agent'))return route.fulfill({contentType:'application/json',body:JSON.stringify({version:0,name:'Телефонные разговоры',prompt:'Кратко опиши разговор.',temperature:.1,models:[],recognizers:[],history:[],model:'',recognition_provider:'yandex',vault_ready:true})});
    if(/\/sip-lines(?:\/[^/]+)?$/.test(url.pathname)){
     if(req.method()==='PUT'){
      const value=req.postDataJSON();writes.push(value);assert.equal(value.expected_version,version);version++;
      const id=value.line_id||'00000000-0000-4000-8000-000000000010';
      const {password,...publicValue}=value;
      lines=lines.filter(x=>x.id!==id).concat([{...publicValue,id,member_name:members.find(m=>m.id===value.member_id).name,member_active:true,password_configured:true,registration_status:'not_checked'}]);
     }
     if(req.method()==='DELETE'){
      assert.equal(req.postDataJSON().expected_version,version);assert.ok(url.pathname.endsWith('/00000000-0000-4000-8000-000000000010'));version++;deletes++;
      lines=lines.filter(x=>x.id!=='00000000-0000-4000-8000-000000000010').map(x=>({...x,primary:x.id==='00000000-0000-4000-8000-000000000011'}));
     }
     return route.fulfill({contentType:'application/json',body:JSON.stringify({version,lines,members,vault_ready,browser_call_ready:false})});
    }
    return route.fulfill({contentType:'application/json',body:JSON.stringify(url.pathname.endsWith('/loader')?{enabled:true,history_days:7,poll_seconds:60,version:0,running:false,call_count:0}:{configured:false,vault_ready:true,version:0,sample_hours:24,status:'not_configured',checks:{},browser_call_ready:false})});
   });
   const page=await context.newPage();page.setDefaultTimeout(10000);await page.goto('https://cabinet.example.test/');await page.locator('#connections-tab').evaluate(e=>e.click());await page.locator('#connections-novofon-tab').click();
   await page.locator('#sip-lines-add:not([disabled])').waitFor();await page.locator('#sip-lines-add').click();
   const form=page.locator('#sip-lines-form');await form.locator('[name=member_id]').selectOption(members[1].id);
   await form.locator('[name=extension]').fill('004');await form.locator('[name=password]').fill('synthetic-browser-password');
   const sipNumber=form.locator('[name=sip_number]');
   for(const invalid of ['sip:user@example','000 123','000/123']){
    await sipNumber.fill(invalid);assert.equal(await sipNumber.evaluate(e=>e.checkValidity()),false);
    await page.locator('#sip-lines-save').click();assert.equal(writes.length,0);
   }
   for(const valid of ['0004567','146000-100','office_001.test']){
    await sipNumber.fill(valid);assert.equal(await sipNumber.evaluate(e=>e.checkValidity()),true);
   }
   await sipNumber.fill('0004567');
   const formOverflow=await form.evaluate(e=>e.scrollWidth-e.clientWidth);assert.ok(formOverflow<=1);
   if(process.env.CM_APPEARANCE_SCREENSHOT_DIR){fs.mkdirSync(process.env.CM_APPEARANCE_SCREENSHOT_DIR,{recursive:true});await form.locator('[name=password]').fill('');await page.locator('#sip-lines-card').screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`sip-form-${width}.png`)});await form.locator('[name=password]').fill('synthetic-browser-password');}
   await page.locator('#sip-lines-save').click();await form.waitFor({state:'hidden'});assert.equal(writes[0].sip_number,'0004567');assert.equal(writes[0].member_id,members[1].id);assert.equal(writes[0].extension,'004');
   assert.equal(await form.locator('[name=password]').inputValue(),'');assert.ok(!(await page.locator('#sip-lines-list').textContent()).includes('synthetic-browser-password'));
   await page.locator('.sip-line-actions button').first().click();await form.locator('[name=extension]').fill('005');await page.locator('#sip-lines-save').click();await form.waitFor({state:'hidden'});assert.equal(writes[1].password,null);assert.ok(writes[1].line_id);
   const overflow=await page.locator('#sip-lines-card').evaluate(e=>e.scrollWidth-e.clientWidth);assert.ok(overflow<=1);
   if(process.env.CM_APPEARANCE_SCREENSHOT_DIR)await page.locator('#sip-lines-card').screenshot({path:path.join(process.env.CM_APPEARANCE_SCREENSHOT_DIR,`sip-saved-${width}.png`)});
   // Three profiles of one employee: deletion promotes the line currently in a draft.
   const first=lines[0],promotedId='00000000-0000-4000-8000-000000000011';
   lines=[first,{...first,id:promotedId,sip_number:'0006789',primary:false},{...first,id:'00000000-0000-4000-8000-000000000012',sip_number:'0009999',primary:false}];version++;
   await page.locator('#sip-lines-refresh').click();await page.locator('.sip-line-row:nth-child(3)').waitFor();
   await page.locator(`[data-line-id="${promotedId}"] button`).first().click();await form.locator('[name=extension]').fill('009');
   await page.locator('.sip-line-row:first-child button').last().click();await page.locator('#confirm-yes').click();
   await page.locator('[data-line-id="00000000-0000-4000-8000-000000000010"]').waitFor({state:'hidden'});
   assert.equal(await form.locator('[name=primary]').isChecked(),true);assert.equal(await form.locator('[name=extension]').inputValue(),'009');
   await page.locator('#sip-lines-save').click();await form.waitFor({state:'hidden'});assert.equal(writes[2].primary,true);assert.equal(deletes,1);
   // A vault outage blocks saving, while the user can still hide and discard a password.
   await page.locator('#sip-lines-add').click();await form.locator('[name=password]').fill('synthetic-unsaved-password');await page.locator('#sip-password-show').click();
   vault_ready=false;await page.locator('#connections-novofon-tab').click();await page.locator('#sip-lines-save:disabled').waitFor();
   await page.locator('#sip-password-show').click();assert.equal(await form.locator('[name=password]').getAttribute('type'),'password');
   await page.locator('#sip-lines-cancel').click();await form.waitFor({state:'hidden'});assert.equal(await form.locator('[name=password]').inputValue(),'');
   assert.deepEqual(data.errors,[]);reports.push({width,writes:writes.length,deletes,formOverflow,overflow,leadingZerosKept:true,invalidSipBlocked:true,primaryPromotionKept:true,cancelAvailableWithoutVault:true,providerRequests:0});await page.close();
  }
  console.log(JSON.stringify({browser:browser.version(),reports},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
