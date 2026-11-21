window.createShippingTemplates=function({E,select,input,show,call,api,dialog,guardDialog=()=>()=>true}) {
  async function account(accountId){
    const current=guardDialog();
    const config=await api('/api/commerce/target-bindings');
    if(!current())return;
    const targets=config.targets.filter(t=>t.account_id===accountId&&t.enabled&&t.environment==='production');
    if(!targets.length)throw new Error('请先为此账号启用正式店铺地区');
    if(targets.length===1)return open(targets[0].id);
    show('选择配置配送模板的店铺地区',select('target_id','店铺地区','',targets.map(t=>[t.id,`${t.account_name} · ${t.name} · 正式`])),async f=>({afterSave:()=>open(Number(f.get('target_id')))}),'配置此地区模板');
  }
  async function open(id,productType=''){
    const current=guardDialog();
    const query=productType?`?product_type=${encodeURIComponent(productType)}`:'';
    const result=await api(`/api/commerce/targets/${id}/shipping-templates${query}`);
    if(!current())return;
    const known=[...new Set([...(result.product_types||[]),result.product_type].filter(Boolean))];
    const picker=known.length?select('shipping_product_type','Amazon商品类型',result.product_type,[['','请选择'],...known.map(t=>[t,t])]):input('shipping_product_type','Amazon商品类型',result.product_type||'');
    const requirement=({required:'平台规则要求填写',conditional:'由商品资料和履约方式决定',optional:'平台规则未强制填写',absent:'此商品类型未声明该字段',unknown:'尚未读取平台规则'})[result.requirement?.status]||'尚未读取平台规则';
    const ready=!!result.product_type;
    const saved=result.default?.id&&!result.templates.some(t=>t.id===result.default.id)?[[result.default.id,`${result.default.name||result.default.id}（已保存，待核实）`]]:[];
    show('店铺配送模板',`<p class="c-hint">为当前店铺选择上架时使用的配送模板。运输天数在亚马逊配送设置中调整，备货时间单独设置。</p>${result.store_default?.id?`<p class="c-hint">店铺全部商品默认：${E(result.store_default.name||result.store_default.id)}</p>`:''}${picker}<button type="button" data-shipping-type>查看此商品类型</button><p>${E(result.message)}</p><p class="c-hint">${E(requirement)}</p><button type="button" data-shipping-refresh ${ready?'':'disabled'}>读取当前店铺模板</button>`+
      (ready?select('template_id','默认配送模板',result.default?.id||'',[['','暂不设置'],...saved,...result.templates.map(t=>[t.id,t.name])])+
        `<label class="target-choice"><input type="checkbox" name="apply_all_product_types" ${result.default_scope==='store'?'checked':''}><span>设为此店铺全部商品的默认模板（包括以后新增商品类型）</span></label><p class="c-hint">新商品默认沿用所选模板；每种商品仍需符合平台要求。已有商品手动选择的模板保留。不勾选时仅设置当前商品类型的默认模板。</p><label class="target-choice"><input type="checkbox" name="fill_missing_drafts"><span>同时补齐此商品类型尚未提交、模板为空的资料</span></label>`:''),async f=>{
        if(!ready)throw new Error('请先选择Amazon商品类型');
        await call(`/targets/${id}/shipping-default`,{revision:result.revision,cache_revision:result.cache_revision,product_type:result.product_type,template_id:f.get('template_id'),fill_missing_drafts:f.has('fill_missing_drafts'),apply_all_product_types:f.has('apply_all_product_types')},'PATCH');
        return f.has('apply_all_product_types')?'已保存此店铺全部商品的默认配送模板':'已保存此店铺和商品类型的配送模板';
      });
    dialog.querySelector('[data-shipping-type]').onclick=async event=>{
      const b=event.currentTarget;
      const value=dialog.querySelector('[name=shipping_product_type]').value.trim();
      if(!value){b.textContent='请先选择Amazon商品类型';return}
      b.disabled=true;
      try{await open(id,value)}catch(error){b.textContent=error.message;b.disabled=false}
    };
    dialog.querySelector('[data-shipping-refresh]').onclick=async event=>{
      const current=guardDialog();
      const b=event.currentTarget;b.disabled=true;
      try{await call(`/targets/${id}/shipping-templates/refresh`,{product_type:result.product_type});if(current())await open(id,result.product_type)}catch(error){b.textContent=error.message;b.disabled=false}
    };
  }
  async function listingField(targetId,current){
    const productType=dialog.querySelector('[name=product_type]')?.value?.trim();
    if(!productType)return;
    const original=dialog.querySelector('[name=shipping_group]');
    if(!original)return;
    let result,problem='';
    try{
      result=await api(`/api/commerce/targets/${targetId}/shipping-templates?product_type=${encodeURIComponent(productType)}`);
      if(result.environment==='production'&&['pending','stale','empty','unavailable'].includes(result.status)){
        try{
          result=await call(`/targets/${targetId}/shipping-templates/refresh`,{product_type:productType});
        }catch(error){problem=error.message||'读取店铺配送模板失败'}
      }
    }catch(error){problem=error.message||'读取店铺配送模板失败'}
    // The dialog may have changed while Amazon's read-only request was running.
    const field=dialog.querySelector('[name=shipping_group]');
    if(field!==original)return;
    const selected=field.value??current??'';
    const templates=problem?[]:(result?.templates||[]);
    const existing=selected&&!templates.some(t=>t.id===selected)?[[selected,selected+'（当前已填，尚未在此商品类型验证）']]:[];
    const container=field.closest('label');
    if(!container)return;
    const message=problem||result?.message||'';
    const hint=message&&result?.status!=='ready'?`商品配送模板（${message}）`:'商品配送模板';
    container.outerHTML=select('shipping_group',hint,selected,[['','未选择'],...existing,...templates.map(t=>[t.id,t.name])]);
  }
  return {open,account,listingField};
};
