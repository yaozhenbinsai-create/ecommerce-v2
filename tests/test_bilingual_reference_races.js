const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');

const pending=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
function setup(){
  const dom=new JSDOM('<dialog open><form></form></dialog>',{runScripts:'outside-only'});
  const w=dom.window,d=w.document,dialog=d.querySelector('dialog'),saved=pending(),generated=pending();
  w.eval(fs.readFileSync(path.join(process.env.SHOP_TEST_STATIC_ROOT||path.join(__dirname,'../app/static'),'listing-bilingual.js'),'utf8'));
  const ui=w.createListingBilingual({E:v=>String(v??''),api:()=>saved.promise,call:()=>generated.promise});
  const original={title:'Old title',bullets:['Old point'],description:'Old description',search_terms:'old words'};
  d.querySelector('form').innerHTML=ui.render({content:original,language:'en_US'});
  ui.mount(dialog,9);
  const reference=title=>({source_content:{title,description:'Old description',search_terms:'old words',bullets:['Old point']},content:{title:'中文'+title,bullets:['卖点'],description:'描述',search_terms:'搜索词'}});
  return {dom,w,d,dialog,saved,generated,reference,original,status:()=>d.querySelector('.c-reference-status').textContent,translation:()=>d.querySelector('[data-reference-field="title"]').textContent};
}
async function tick(){await new Promise(resolve=>setImmediate(resolve));}
test('late saved translation cannot describe changed editor text',async()=>{
  const s=setup();const input=s.d.querySelector('[name="title"]');input.value='New title';input.dispatchEvent(new s.w.Event('input'));
  s.saved.resolve(s.reference('Old title'));await tick();
  assert.doesNotMatch(s.translation(),/中文Old title/);assert.match(s.status(),/修改|不匹配/);s.dom.window.close();
});
test('late saved lookup cannot overwrite a newer explicit translation',async()=>{
  const s=setup();const request=s.d.querySelector('[data-reference-refresh]').onclick();
  s.generated.resolve(s.reference('Old title'));await request;
  s.saved.resolve({available:false});await tick();
  assert.equal(s.translation(),'中文Old title');assert.match(s.status(),/当前原文对应/);s.dom.window.close();
});
test('equivalent saved source does not depend on JSON object key order',async()=>{
  const s=setup();s.saved.resolve(s.reference('Old title'));await tick();
  assert.equal(s.translation(),'中文Old title');
  s.d.querySelector('[name="title"]').dispatchEvent(new s.w.Event('input'));
  assert.equal(s.translation(),'中文Old title');s.dom.window.close();
});
