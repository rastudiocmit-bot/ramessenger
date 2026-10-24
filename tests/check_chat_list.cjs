// Real layout/scroll regression with synthetic window patches only.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { fixture } = require('./browser_fixture.cjs');
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
    args:['--no-sandbox','--disable-dev-shm-usage',...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||'[]')]});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
    const data=await fixture(context), page=await context.newPage();
    page.setDefaultTimeout(10000);
    const prototype=data.conversations[0];
    data.conversations.splice(0,data.conversations.length,...Array.from({length:1000},(_,n)=>({...prototype,
      id:'client-'+n,contact_id:'contact-'+n,name:'Клиент '+n,unread:n<3,unread_count:n<3?1:0,
      last_message_at:new Date(Date.UTC(2026,9,24,11)-n*60000).toISOString(),queue_seq:'1'})));
    const views=new Map(), requests=[];let token=0,changed=false;
    await page.route('**/api/workspaces/test/chats?*',async route=>{
      const url=new URL(route.request().url()), size=Number(url.searchParams.get('window'));
      const ordered=[...data.conversations].sort((a,b)=>Number(b.unread)-Number(a.unread)||Date.parse(b.last_message_at)-Date.parse(a.last_message_at));
      const rows=ordered.slice(0,size), previous=views.get(url.searchParams.get('window_token'));
      const versions=new Map(rows.map(row=>[row.id,JSON.stringify(row)]));
      const upserts=rows.filter(row=>previous?.get(row.id)!==versions.get(row.id));
      const next='view-'+(++token);views.set(next,versions);
      requests.push({size,rows:upserts.length,changed});
      await route.fulfill({contentType:'application/json',body:JSON.stringify({window_token:next,reset:!previous,
        conversations:upserts,order:rows.map(row=>row.id),total:1000,unread:changed?4:3,page:size/90-1,has_more:size<1000})});
    });
    await page.goto('https://cabinet.example.test/');
    await page.waitForSelector('.chat-row');
    assert.equal(await page.locator('.chat-row').count(),90);
    const scroller=page.locator('.chat-list-scroll');
    await scroller.evaluate(el=>{el.scrollTop=el.scrollHeight-el.clientHeight-1200;});
    await page.waitForFunction(()=>document.querySelectorAll('.chat-row').length===180);
    // A row in the viewport survives insertion of a new urgent dialog above it.
    const before=await page.evaluate(()=>{
      const s=document.querySelector('.chat-list-scroll'), top=s.getBoundingClientRect().top;
      const row=[...document.querySelectorAll('.chat-row')].find(row=>row.getBoundingClientRect().top>top+30);
      window.retainedListRow=row;
      return {id:row.dataset.id,top:row.getBoundingClientRect().top};
    });
    const latest=data.conversations[999];Object.assign(latest,{unread:true,unread_count:1,queue_seq:'2',last_message_at:'2026-10-24T12:00:00Z'});changed=true;
    await page.waitForFunction(()=>document.querySelector('.chat-row')?.dataset.id==='client-999');
    const after=await page.evaluate(id=>{
      const row=document.querySelector(`.chat-row[data-id="${id}"]`);
      return {same:row===window.retainedListRow,top:row.getBoundingClientRect().top,
        ids:[...document.querySelectorAll('.chat-row')].map(row=>row.dataset.id)};
    },before.id);
    assert.equal(after.same,true);assert.ok(Math.abs(after.top-before.top)<=1,`List anchor moved ${after.top-before.top}px`);
    assert.equal(after.ids.length,new Set(after.ids).size);
    assert.equal(requests.find(r=>r.changed).rows,1);
    assert.ok(requests.some(r=>r.size===180&&r.rows===90));
    assert.deepEqual(data.errors,[]);assert.deepEqual(data.unexpected,[]);
    assert.equal(data.calls.filter(call=>call.path.endsWith('/family')).length,0);
    console.log(JSON.stringify({browser:await browser.version(),clients:1000,initialRows:90,
      appendedRows:90,changedRows:1,listAnchorDrift:after.top-before.top,errors:data.errors},null,2));
    await context.close();
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
