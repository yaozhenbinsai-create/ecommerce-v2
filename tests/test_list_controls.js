const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context={window:{}};vm.runInNewContext(fs.readFileSync('app/static/list-controls.js','utf8'),context);
const {select,compare}=context.window.ListControls;
const rows=Array.from({length:47},(_,i)=>({id:i+1,value:47-i}));
const ids=result=>Array.from(result.rows,x=>x.id);
test('sorting happens over all matches before pagination and leaves source order intact',()=>{
  const state={page:2,size:10,sort:0,direction:'asc'};const result=select(rows,state,x=>x.value);
  assert.deepEqual(ids(result),[37,36,35,34,33,32,31,30,29,28]);assert.equal(result.total,47);assert.equal(result.pages,5);
  assert.equal(rows[0].id,1);assert.equal(rows[46].id,47);
});
test('empty lists and deletion on the last page clamp safely',()=>{
  const state={page:5,size:10,sort:-1};const empty=select([],state);assert.equal(empty.page,1);assert.equal(empty.pages,1);assert.equal(empty.start,0);assert.equal(empty.end,0);
  const result=select(rows.slice(0,21),state);assert.equal(result.page,3);assert.deepEqual(ids(result),[21]);
});
test('stable ties, natural text order, numbers, and missing values',()=>{
  assert.ok(compare('商品2','商品10')<0);assert.ok(compare(2,10)<0);
  assert.equal(compare(null,2,'desc'),1);assert.equal(compare(NaN,2,'asc'),1);
  const result=select([{id:1,v:3},{id:2,v:null},{id:3,v:3}],{page:1,size:20,sort:0,direction:'desc'},r=>r.v);
  assert.deepEqual(ids(result),[1,3,2]);
});
test('different currencies remain grouped instead of mixing nominal amounts',()=>{
  const data=[{id:1,v:{currency:'USD',amount:2}},{id:2,v:{currency:'CNY',amount:10}},{id:3,v:{currency:'USD',amount:100}}];
  const result=select(data,{page:1,size:20,sort:0,direction:'desc'},r=>r.v);assert.deepEqual(ids(result),[2,3,1]);
});
test('invalid page size and page input use safe defaults',()=>{
  const result=select(rows,{page:-9,size:0,sort:-1});assert.equal(result.size,20);assert.equal(result.page,1);assert.equal(result.rows.length,20);
});
