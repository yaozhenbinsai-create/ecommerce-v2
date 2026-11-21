window.createListingFilters=function({E,label,find,getData,onApply,onReset}){
  const defaults={brand:'',target:'',environment:'',material:'',status:'',pending:'',color:'',size:'',sale_currency:'',sale_min:'',sale_max:'',cost_currency:'',cost_min:'',cost_max:'',date_field:'created_at',date_from:'',date_to:''};
  let applied={...defaults},host=null,signature='';
  const skuRows=x=>x.draft?.skus||getData().sku.filter(v=>v.product_id===x.product_id);
  const brand=x=>x.draft?.brand||find('product',x.product_id)?.brand||'';
  const cost=v=>{const src=v.price_source?.cost_pricing||{},sku=find('sku',v.sku_id||v.id)||{};return {value:Object.hasOwn(src,'cost')?src.cost:sku.cost,currency:Object.hasOwn(src,'cost_currency')?src.cost_currency:sku.cost_currency}};
  const stamp=raw=>{if(!raw)return NaN;const text=String(raw);return Date.parse(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text)?text.replace(' ','T')+'Z':text)};
  const includes=(value,query)=>String(value||'').toLowerCase().includes(query.trim().toLowerCase());
  const normalizedSize=value=>String(value||'').toLowerCase().replace(/[×x*]/g,'x').replace(/\s/g,'');
  function validate(f){
    for(const key of ['sale','cost']){
      const min=f[key+'_min'],max=f[key+'_max'];
      if((min!==''||max!=='')&&!f[key+'_currency'])return `请先选择${key==='sale'?'售价':'成本'}币种，再填写金额范围`;
      if([min,max].some(v=>v!==''&&(!Number.isFinite(Number(v))||Number(v)<0)))return '金额必须为有效非负数';
      if(min!==''&&max!==''&&Number(min)>Number(max))return '最低金额不能大于最高金额';
    }
    if(f.date_from&&f.date_to&&f.date_from>f.date_to)return '开始日期不能晚于结束日期';
    return '';
  }
  function matches(x,f=applied){
    if(validate(f))return false;
    if(f.brand&&(f.brand==='__missing__'?!!brand(x):brand(x)!==f.brand))return false;
    if(f.target&&String(x.target_id)!==f.target)return false;
    if(f.environment&&x.environment!==f.environment)return false;
    if(f.material&&(f.material==='ready'?x.material_status!=='ready':x.material_status==='ready'))return false;
    if(f.status&&(x.publication_progress?.live?.status||x.status)!==f.status)return false;
    const pending=x.publication_progress?.needs_attention??x.publication_progress?.incomplete??(x.platform_issues||x.issues||[]).length>0;
    if(f.pending&&(f.pending==='yes'?!pending:pending))return false;
    if(f.date_from||f.date_to){const time=stamp(x[f.date_field]);if(!Number.isFinite(time))return false;
      if(f.date_from&&time<Date.parse(f.date_from+'T00:00:00+08:00'))return false;
      if(f.date_to&&time>Date.parse(f.date_to+'T23:59:59.999+08:00'))return false;
    }
    if(!['color','size','sale_currency','sale_min','sale_max','cost_currency','cost_min','cost_max'].some(k=>f[k]!==''))return true;
    return skuRows(x).some(v=>{
      if(f.color&&!includes(v.color,f.color))return false;
      if(f.size&&!normalizedSize(v.size).includes(normalizedSize(f.size)))return false;
      const c=cost(v),values={sale:{value:x.draft?.skus?v.price:null,currency:x.draft?.currency},cost:c};
      for(const key of ['sale','cost']){const item=values[key],min=f[key+'_min'],max=f[key+'_max'];
        if(f[key+'_currency']&&item.currency!==f[key+'_currency'])return false;
        if(min!==''||max!==''){
          if(item.value===null||item.value===undefined||item.value===''||!Number.isFinite(Number(item.value)))return false;
          if(min!==''&&Number(item.value)<Number(min))return false;
          if(max!==''&&Number(item.value)>Number(max))return false;
        }
      }
      return true;
    });
  }
  const input=(key,title,value,type='text',extra='')=>`<label><span>${E(title)}</span><input name="${key}" type="${type}" value="${E(value||'')}" ${extra}></label>`;
  const select=(key,title,value,options)=>`<label><span>${E(title)}</span><select name="${key}">${options.map(([id,text])=>`<option value="${E(id)}" ${id===value?'selected':''}>${E(text)}</option>`).join('')}</select></label>`;
  function mount(){
    const data=getData();if(!data)return;
    const unique=values=>[...new Set(values.filter(Boolean))].sort();
    const brands=unique(data.listing.map(brand));
    const targets=data.target.map(t=>[String(t.id),`${t.account_name||''} · ${t.name} · ${label(t.environment)}`]);
    const stateLabels={buyable:'在售',partial_buyable:'部分在售',issues:'平台有待处理问题',review:'亚马逊审核中',not_buyable:'当前不可售',not_submitted:'尚未提交',waiting:'等待同步',sample:'模拟 / 沙盒'};
    const states=unique(data.listing.map(x=>x.publication_progress?.live?.status||x.status)).map(v=>[v,stateLabels[v]||label(v)]);
    const currencies=unique(data.listing.map(x=>x.draft?.currency).concat(data.target.map(t=>t.currency)));
    const costs=unique(data.listing.flatMap(x=>skuRows(x).map(v=>cost(v).currency)).concat(data.sku.map(v=>v.cost_currency)));
    const writesOff=data.settings?.live_writes_enabled===false;
    const next=JSON.stringify([brands,targets,states,currencies,costs,writesOff]);
    if(!host){host=document.createElement('div');host.className='lo-filter-panel';document.querySelector('#content').before(host);}
    host.hidden=false;
    if(signature===next&&host.children.length)return;
    if(host.contains(document.activeElement))return;
    const previous=host.querySelector('form'),f=previous?{...defaults,...Object.fromEntries(new FormData(previous))}:applied;
    const expanded=host.querySelector('.lo-filter-advanced')?.open,filtersOpen=host.querySelector('.ws-filter-disclosure')?.open;signature=next;
    const moneyFields=(key,title,pool)=>`<fieldset><legend>${title}</legend><div class="lo-filter-range">${select(key+'_currency',title+'币种',f[key+'_currency'],[['','选择币种'],...pool.map(v=>[v,v])])}${input(key+'_min','最低'+title,f[key+'_min'],'number','min="0" step="0.01" placeholder="不限"')}${input(key+'_max','最高'+title,f[key+'_max'],'number','min="0" step="0.01" placeholder="不限"')}</div></fieldset>`;
    host.innerHTML=`<details class="ws-filter-disclosure" ${filtersOpen?'open':''}><summary><span><strong>按店铺、状态或规格筛选</strong><span class="lo-filter-count"></span></span><span>筛选条件 ⌄</span></summary><form class="lo-filter-form"><div class="lo-filter-common">
      ${select('brand','品牌',f.brand,[['','全部品牌'],...brands.map(v=>[v,v]),['__missing__','品牌待补充']])}
      ${select('target','店铺 / 站点',f.target,[['','全部店铺 / 站点'],...targets])}
      ${select('environment','环境',f.environment,[['','全部环境'],['sandbox','官方沙盒'],['production','正式'],['mock','本地模拟']])}
      ${select('material','生产资料',f.material,[['','全部资料状态'],['ready','资料齐备'],['needs_fields','资料待补充']])}
      ${select('status','平台状态',f.status,[['','全部平台状态'],...states])}
      ${select('pending','待处理事项',f.pending,[['','全部'],['yes','有待处理事项'],['no','无待处理事项']])}
      ${input('color','颜色',f.color,'text','placeholder="如：白色"')}${input('size','尺寸',f.size,'text','placeholder="如：48 × 74"')}
      </div><details class="lo-filter-advanced" ${expanded?'open':''}><summary>更多条件：金额范围、创建 / 更新时间</summary><div class="lo-filter-extra">
      ${moneyFields('sale','售价',currencies)}${moneyFields('cost','成本',costs)}
      <fieldset class="lo-filter-dates"><legend>时间范围（北京时间，包含起止当天）</legend><div class="lo-filter-range">${select('date_field','时间字段',f.date_field,[['created_at','创建时间'],['updated_at','更新时间']])}${input('date_from','开始日期',f.date_from,'date')}${input('date_to','结束日期',f.date_to,'date')}</div></fieldset></div></details>
      <p class="lo-filter-error" role="alert" hidden></p><div class="lo-filter-footer"><span>匹配任一 SKU 的商品会显示全部规格。可与上方关键词组合查询。</span><div><button type="button" data-filter-reset>重置条件</button><button type="submit" class="primary">查询</button><button type="button" data-c-action="batch-publish" data-c-id="0" ${writesOff?'disabled title="正式平台写入已关闭"':''}>一键上架</button></div></div></form></details>`;
    const updateCount=()=>{host.querySelector('.lo-filter-count').textContent=`已应用 ${Object.keys(defaults).filter(k=>k!=='date_field'&&applied[k]!=='').length} 项条件`;};
    host.querySelector('form').onsubmit=e=>{e.preventDefault();const f={...defaults,...Object.fromEntries(new FormData(e.target))};const error=validate(f),box=host.querySelector('.lo-filter-error');box.hidden=!error;box.textContent=error;if(error)return;applied=f;updateCount();onApply();};
    host.querySelector('[data-filter-reset]').onclick=()=>{applied={...defaults};signature='';host.innerHTML='';mount();onReset();};
    updateCount();
  }
  return {matches,validate,mount,visibility:visible=>{if(host)host.hidden=!visible;},defaults:()=>({...defaults})};
};
