'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const {family}=require('./directory_fixture.cjs'),web=path.join(__dirname,'../app/backend/src/center_messenger/web');
function render(t,value=family()){
 const dom=new JSDOM('<body><div id="directory-contact-dialog"></div></body>',{runScripts:'outside-only',url:'https://example.test'}),w=dom.window;
 const node=(tag,text,cls)=>{const e=w.document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
 w.eval(fs.readFileSync(web+'/directory-card.js','utf8'));const view=w.CenterDirectoryCard({node,date:v=>v});const root=w.document.querySelector('#directory-contact-dialog');root.append(view(value));t.after(()=>w.close());return {w,root};
}
test('the complete saved family keeps pupils and CRM IDs separate and preserves server totals',t=>{
 const {root}=render(t);assert.equal(root.querySelectorAll('.student-card').length,2);assert.equal(root.querySelectorAll('.directory-source-card').length,3);
 assert.match(root.textContent,/Тестовый родитель|Коломна 1|Коломна 2/);assert.match(root.textContent,/Customer ID100/);assert.match(root.textContent,/Customer ID200/);
 assert.equal(root.querySelector('.directory-totals dd').textContent,'3 962,5 ₽');assert.equal(root.querySelectorAll('.directory-subscription').length,2,'branch mirrors do not multiply server subscriptions');
 assert.match(root.textContent,/14.05.2015|05.11.2026, 17:00|посещено/);assert.equal(root.querySelectorAll('a[href^="tel:"]').length,1);assert.match(root.textContent,/\+0 · номер требует проверки/);
 assert.match(root.querySelectorAll('.directory-totals')[1].className,/negative/);assert.match(root.textContent,/Получена часть данных/);
});
test('all retained subscriptions, including archived ones, stay visible and null differs from zero',t=>{
 const f=family();f.students[0].balance='0';f.students[1].balance=null;const {root}=render(t,f);
 assert.equal(root.querySelector('.directory-totals dd').textContent,'0 ₽');assert.equal(root.querySelectorAll('.directory-totals')[1].querySelector('dd').textContent,'Нет данных');
 assert.match(root.querySelectorAll('.directory-subscription')[1].textContent,/Архивный абонемент|Архивный|01.02.2025/);
});
test('saved lesson history is paged locally, newest first, until every canonical row is shown',t=>{
 const {root}=render(t);assert.equal(root.querySelectorAll('.directory-lesson').length,20);assert.match(root.querySelector('.directory-lesson small').textContent,/1044/);
 const more=root.querySelector('.directory-lessons-more');more.click();assert.equal(root.querySelectorAll('.directory-lesson').length,40);more.click();assert.equal(root.querySelectorAll('.directory-lesson').length,45);assert.equal(more.hidden,true);
 assert.match(root.querySelectorAll('.directory-lesson small')[44].textContent,/1000/);
});
test('unloaded data never masquerades as zero or an active load; links and supplied text are safe',t=>{
 const f=family();f.students[0].subscriptions=undefined;f.students[0].lessons=undefined;f.students[0].cards[0].url='javascript:alert(1)';f.students[0].groups[0].url='https://other.example/group';f.name='<img src=x onerror=alert(1)>';delete f.cache;
 const {root}=render(t,f);assert.equal(root.querySelectorAll('img,script').length,0);assert.match(root.querySelector('h2').textContent,/<img/);assert.equal(root.querySelectorAll('a[href^="javascript:"]').length,0);assert.equal(root.querySelectorAll('a[href*="other.example"]').length,0);assert.match(root.textContent,/Сведения об абонементах пока не получены|История занятий пока не получена/);assert.doesNotMatch(root.textContent,/Загружается/);
});
