'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom'),{family}=require('./directory_fixture.cjs');
const web=path.join(__dirname,'../app/backend/src/center_messenger/web'),tick=()=>new Promise(r=>setImmediate(r));
async function setup(t,contactHandler){
 const dom=new JSDOM(fs.readFileSync(web+'/index.html','utf8'),{runScripts:'outside-only',url:'https://example.test'}),w=dom.window,$=s=>w.document.querySelector(s),calls=[],timers=new Map();let now=0,n=0;
 w.setTimeout=(f,ms)=>{timers.set(++n,{f,ms});return n;};w.clearTimeout=id=>timers.delete(id);w.Date.now=()=>now;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 const state={user:{id:'one'},workspace:{id:'centre',role:'owner'},pane:'clients',epoch:1};
 const api=async(url,opts={})=>{calls.push({url,...opts});if(url.includes('/contacts/'))return contactHandler?contactHandler():family();if(url.endsWith('/sync'))return {configured:true,state:'idle',last_success_at:'2026-10-30T08:00:00Z',stats:{}};return {snapshot:'run-1',contacts:[{id:'contact-0',name:'Тестовый родитель',students:['Ребёнок'],student_count:1,groups:['Python'],group_count:1,phones:['+79000000000'],emails:['parent@example.test'],branches:[{id:'1',name:'Коломна 1'}],status:'client',max_connected:true}],total:1,branches:[],page:0,has_more:false};};
 for(const name of ['directory-card','directory'])w.eval(fs.readFileSync(`${web}/${name}.js`,'utf8'));
 const app=w.CenterDirectory({$,state,api,node:(tag,text,cls)=>{const e=w.document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;},show:(e,v=true)=>e.hidden=!v,notice:(text,e)=>{e.textContent=text;e.hidden=!text;},date:v=>v});
 t.after(()=>w.close());await app.enter();return {w,$,state,app,calls,timers,advance:ms=>now+=ms,flush:async()=>{await tick();await tick();},open(){ $('.contact-select').click(); }};
}
test('the update controls stay expanded and rows show saved phones, groups and branches without preloading details',async t=>{
 const s=await setup(t);assert.equal(s.$('details.sync-options'),null);assert.ok(s.$('div.sync-options #sync-interval'));assert.match(s.$('.directory-list-facts').textContent,/79000000000|parent@example.test|Python|Коломна 1|MAX через AlfaCRM/);assert.equal(s.calls.filter(c=>c.url.includes('/contacts/')).length,0);
});
test('detail caching is bounded, reopens instantly, and refreshing the list cannot reopen a closed modal',async t=>{
 const s=await setup(t);s.open();await s.flush();assert.equal(s.$('#directory-detail').querySelectorAll('.student-card').length,2);s.$('#directory-contact-dialog').close();s.open();await s.flush();assert.equal(s.calls.filter(c=>c.url.includes('/contacts/')).length,1);
 s.$('#directory-contact-dialog').close();await s.app.enter();assert.equal(s.$('#directory-contact-dialog').open,false);
 s.advance(30001);s.open();await s.flush();assert.equal(s.calls.filter(c=>c.url.includes('/contacts/')).length,2);assert.ok(s.calls.every(c=>!c.method));
});
test('in-flight detail reads are shared, and a late response after workspace reset cannot repopulate the card',async t=>{
 let resolve;const s=await setup(t,()=>new Promise(r=>resolve=r));s.open();s.$('#directory-contact-dialog').close();s.open();await s.flush();assert.equal(s.calls.filter(c=>c.url.includes('/contacts/')).length,1);
 s.state.epoch++;s.state.workspace.id='second';s.app.reset();resolve(family());await s.flush();assert.equal(s.$('#directory-contact-dialog').open,false);assert.equal(s.$('#directory-detail').querySelectorAll('.student-card').length,0);assert.doesNotMatch(s.$('#directory-detail').textContent,/Тестовый родитель/);
});
