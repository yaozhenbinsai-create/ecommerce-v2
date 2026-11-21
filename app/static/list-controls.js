/* Shared in-memory list ordering and pagination; never changes business action scope. */
(function(){
  const collator=new Intl.Collator('zh-CN',{numeric:true,sensitivity:'base'});
  const timestamp=raw=>{const text=String(raw||'');return Date.parse(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text)?text.replace(' ','T')+'Z':text)};
  const missing=v=>v===null||v===undefined||v===''||typeof v==='number'&&!Number.isFinite(v);
  function compare(a,b,direction='asc'){
    const emptyA=missing(a),emptyB=missing(b);
    if(emptyA||emptyB)return emptyA===emptyB?0:emptyA?1:-1;
    const sign=direction==='desc'?-1:1;
    if(typeof a==='object'&&typeof b==='object')return collator.compare(a.currency,b.currency)||compare(a.amount,b.amount,direction);
    return sign*(typeof a==='number'&&typeof b==='number'?a-b:collator.compare(String(a),String(b)));
  }
  function select(rows,state,value){
    const size=[10,20,50,100].includes(Number(state.size))?Number(state.size):20;
    const total=rows.length,pages=Math.max(1,Math.ceil(total/size));
    const page=Math.min(pages,Math.max(1,Math.floor(Number(state.page)||1)));
    const ordered=rows.map((row,index)=>({row,index,value:state.sort>=0?value(row,state.sort):null}));
    if(state.sort>=0)ordered.sort((a,b)=>compare(a.value,b.value,state.direction)||a.index-b.index);
    return {rows:ordered.slice((page-1)*size,page*size).map(x=>x.row),page,size,total,pages,start:total?(page-1)*size+1:0,end:Math.min(total,page*size)};
  }
  window.ListControls={compare,select};
  if(typeof document==='undefined')return;
  const states=new Map(),managed=new WeakMap(),queries=new Map();
  const scope=()=>typeof page==='string'?page:'default';
  function reset(){const prefix=scope()+'|';for(const [key,state] of states)if(key.startsWith(prefix))state.page=1;}
  function node(tag,text){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;}
  function cellValue(cell,heading){
    if(!cell)return null;
    const text=cell.textContent.trim();
    if(!text||/^(?:—|待补充|待生成|未记录|暂无|尚未)/.test(text))return null;
    const date=cell.querySelector('time[datetime]');
    if(date)return Date.parse(date.dateTime);
    if(/时间|日期|创建|更新/.test(heading)){
      const found=text.match(/\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?/);
      if(found)return timestamp(found[0]);
    }
    if(/价|金额|费用|收入|成本|利润|预算/.test(heading)){
      const money=text.match(/(-?\d[\d,]*(?:\.\d+)?)\s*([A-Z]{3})(?![A-Z])/);
      if(money)return {amount:Number(money[1].replaceAll(',','')),currency:money[2]};
    }
    if(/数量|件数|天数|行数|实物|预留|可售|金额|费用|收入|成本|利润/.test(heading)&&/^-?[\d,]+(?:\.\d+)?(?:\s|$|天|件|行)/.test(text)){
      const amount=Number(text.match(/^-?[\d,]+(?:\.\d+)?/)[0].replaceAll(',',''));
      const currency=/金额|费用|收入|成本|利润/.test(heading)&&cell.closest('tr')?.cells[0]?.textContent.match(/\b[A-Z]{3}\b/)?.[0];
      return currency?{amount,currency}:amount;
    }
    return text;
  }
  function controls(container,rows,key,fields,getValue,headers=[]){
    const existing=managed.get(container);
    if(existing){if(existing.signature!==JSON.stringify(existing.state))existing.apply();return;}
    const state=states.get(key)||{page:1,size:20,sort:-1,direction:'asc'};states.set(key,state);
    const footer=node('div');footer.className='list-controls';footer.setAttribute('aria-label','列表分页与排序');
    const count=node('span');count.className='list-count';count.setAttribute('aria-live','polite');
    const sort=node('select');sort.setAttribute('aria-label','排序字段');
    const option=(select,value,title)=>{const item=node('option',title);item.value=value;select.append(item)};
    option(sort,-1,'默认顺序');fields.forEach((field,index)=>{if(field)option(sort,index,field)});sort.value=String(state.sort);
    const direction=node('button');direction.type='button';direction.setAttribute('aria-label','切换排序方向');
    const size=node('select');size.setAttribute('aria-label','每页条数');[10,20,50,100].forEach(n=>option(size,n,`${n} 条 / 页`));size.value=String(state.size);
    const previous=node('button','上一页'),next=node('button','下一页');previous.type=next.type='button';
    const jump=node('input');jump.type='number';jump.min='1';jump.step='1';jump.setAttribute('aria-label','跳转页码');
    const pages=node('span');
    const go=node('button','跳转');go.type='button';
    footer.append(count,sort,direction,size,previous,jump,pages,go,next);
    const parent=container.tagName==='TBODY'?container.closest('table'):container;
    (parent.closest('.c-scroll,.lo-scroll')||parent).after(footer);
    const binding={state,apply,signature:''};
    function apply(){
      const result=select(rows,state,getValue);state.page=result.page;state.size=result.size;
      container.replaceChildren(...result.rows);
      count.textContent=`共 ${result.total} 条 · ${result.start}–${result.end} 条`;
      jump.value=String(result.page);jump.max=String(result.pages);pages.textContent=`/ ${result.pages} 页`;
      previous.disabled=result.page<=1;next.disabled=result.page>=result.pages;
      direction.textContent=state.direction==='asc'?'升序 ↑':'降序 ↓';direction.disabled=state.sort<0;
      sort.value=String(state.sort);
      binding.signature=JSON.stringify(state);
      headers.forEach((header,i)=>header.setAttribute('aria-sort',state.sort===i?(state.direction==='asc'?'ascending':'descending'):'none'));
    }
    sort.onchange=()=>{state.sort=Number(sort.value);state.page=1;apply()};
    direction.onclick=()=>{state.direction=state.direction==='asc'?'desc':'asc';state.page=1;apply()};
    size.onchange=()=>{state.size=Number(size.value);state.page=1;apply()};
    previous.onclick=()=>{state.page--;apply()};next.onclick=()=>{state.page++;apply()};
    const jumpTo=()=>{state.page=Number(jump.value);apply()};go.onclick=jumpTo;
    jump.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();jumpTo()}};
    headers.forEach((header,index)=>{
      if(!fields[index])return;
      const button=node('button',header.textContent);button.type='button';button.className='list-sort-heading';button.title='按此列排序';
      header.replaceChildren(button);button.onclick=()=>{state.direction=state.sort===index&&state.direction==='asc'?'desc':'asc';state.sort=index;state.page=1;apply()};
    });
    managed.set(container,binding);apply();
  }
  function enhance(){
    const root=document.querySelector('#content');if(!root)return;
    [...root.querySelectorAll('table')].filter(table=>!table.parentElement.closest('table')).forEach((table,index)=>{
      const headers=[...(table.tHead?.rows[0]?.cells||[])];const body=table.tBodies[0];if(!headers.length||!body)return;
      const names=headers.map(h=>h.textContent.trim());
      const fields=names.map(n=>/^(操作|详情|结果 \/ 原因)$/.test(n)?null:/创建.*更新/.test(n)?'创建时间':n);
      const dates=[['createdAt','创建时间'],['updatedAt','更新时间']].filter(([key,title])=>body.rows[0]?.dataset[key]!==undefined&&!fields.includes(title));
      fields.push(...dates.map(x=>x[1]));
      controls(body,[...body.rows],scope()+'|table|'+index+'|'+names.join('|'),fields,(row,index)=>index<names.length?cellValue(row.cells[index],names[index]):timestamp(row.dataset[dates[index-names.length][0]]),headers);
    });
    const cards=root.querySelector('.flow-products');
    if(cards)controls(cards,[...cards.children],scope()+'|cards',['产品名称','创建时间','更新时间','设计状态'],(card,index)=>{
      if(index===0)return card.querySelector('h3')?.textContent||'';
      if(index===3)return card.querySelector('.flow-product-status')?.textContent||'';
      return timestamp(card.dataset[index===1?'createdAt':'updatedAt']);
    });
    root.querySelectorAll('[data-list-cards]').forEach(cards=>controls(cards,[...cards.children],scope()+'|cards|'+cards.dataset.listCards,['商品名称','创建时间','更新时间','当前状态'],(card,index)=>index===0?card.dataset.listName:index===3?card.dataset.listState:timestamp(card.dataset[index===1?'createdAt':'updatedAt'])));
  }
  // Wrap once after all page renderers. Recreated lists retain their own state.
  const previousRow=row;
  const attr=value=>String(value||'').replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
  row=function(record){return previousRow(record).replace(/<tr(?=[ >])/,`<tr data-created-at="${attr(record.created_at)}" data-updated-at="${attr(record.updated_at)}"`)};window.row=row;
  const previousRender=render;
  render=function(){const key=scope(),query=document.querySelector('#search')?.value||'';if(queries.get(key)!==query)reset();queries.set(key,query);const result=previousRender();enhance();return result};window.render=render;
  document.querySelector('#search')?.addEventListener('input',reset,true);
  document.addEventListener('change',event=>{if(/^(order|aftersale|support)_(view|target)$/.test(event.target.name||''))reset()},true);
  document.addEventListener('submit',event=>{if(event.target.matches('.lo-filter-form'))reset()},true);
  document.addEventListener('click',event=>{if(event.target.closest('[data-filter-reset],[data-workspace-product-view]'))reset()},true);
  queries.set(scope(),document.querySelector('#search')?.value||'');
  enhance();
})();
