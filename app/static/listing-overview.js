/* Read-only listing overview; all actions use the existing commerce handlers. */
window.createListingOverview=function({E,button,badge,find,getData,platformLinks}){
  const money=(value,currency)=>value===null||value===undefined||value===''?'待补充':`${Number.isFinite(Number(value))?Number(value).toFixed(2):value} ${currency||'币种待确认'}`;
  function timestamp(value){
    if(!value)return '<span class="lo-muted">—</span>';
    const raw=String(value).trim();const iso=typeof value==='number'?value*1000:/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(raw)?raw.replace(' ','T')+'Z':raw;
    const time=new Date(iso);if(Number.isNaN(time.getTime()))return '<span class="lo-muted">—</span>';
    const parts=Object.fromEntries(new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(time).map(p=>[p.type,p.value]));
    return `<time datetime="${E(time.toISOString())}">${parts.year}-${parts.month}-${parts.day}<span>${parts.hour}:${parts.minute}:${parts.second}</span></time>`;
  }
  function variants(x){return x.draft?.skus||getData().sku.filter(v=>v.product_id===x.product_id).map(v=>({...v,seller_sku:v.sku}));}
  function variantLabels(x,skus){
    const report=x.variant_structure||{};
    return skus.slice(0,5).map(v=>{
      const source=(report.rows||[]).find(row=>row.sku_id===(v.sku_id||v.id));
      const details=source?.details?.filter(row=>['color','size','natural_height','compressed_height','fill_weight','function','purpose'].includes(row.field))||[];
      return `<span><b>${E(source?.name||v.color||v.name||'待补充')}</b>${details.length?details.map(row=>`${E(row.label)}：${E(row.value)}`).join(' · '):`${E(v.size||'尺寸待补充')}`}</span>`;
    }).join('')+(skus.length>5?`<span>另 ${skus.length-5} 个销售选项</span>`:'');
  }
  function prices(x,skus){
    const d=x.draft||{};
    const entries=skus.map(v=>{const source=v.price_source||{},cost=source.cost_pricing||{},sku=find('sku',v.sku_id||v.id)||{};return {
      sku:v.seller_sku,sale:Array.isArray(d.skus)?money(v.price,d.currency):'待生成',
      cost:money(Object.hasOwn(cost,'cost')?cost.cost:sku.cost,Object.hasOwn(cost,'cost_currency')?cost.cost_currency:sku.cost_currency),
      original:money(source.source_amount??sku.price,source.source_currency||sku.currency)
    }});
    if(!entries.length)return '<span class="lo-muted">尚未生成售价</span>';
    const same=entries.every(v=>v.sale===entries[0].sale&&v.cost===entries[0].cost&&v.original===entries[0].original);
    if(same){const p=entries[0];return `<dl class="lo-prices"><div><dt>站点售价</dt><dd class="lo-sale">${E(p.sale)}</dd></div><div><dt>成本价</dt><dd>${E(p.cost)}</dd></div><div><dt>原币售价</dt><dd>${E(p.original)}</dd></div></dl>${entries.length>1?`<small>${entries.length} 个 SKU 同价</small>`:''}`;}
    return entries.map(p=>`<div class="lo-sku-price"><small>${E(p.sku)}</small><strong>${E(p.sale)}</strong><small>成本 ${E(p.cost)}</small></div>`).join('');
  }
  function progress(x){
    const p=x.publication_progress;if(!p)return `<div class="lo-platform">${badge(x.status)}</div>`;
    const errors=p.platform_errors||[];
    const warnings=p.platform_warnings||[];
    const labels={pillow_type:'枕头类型',item_thickness:'商品厚度',recommended_uses_for_product:'建议用途',indoor_outdoor_usage:'室内外用途',item_firmness_description:'软硬程度',style:'风格'};
    const improvements=warnings.length?`<details class="lo-issues"><summary>平台信息待完善 · ${warnings.length} 项提醒</summary><p>这些提醒与销售状态、图文详情审核分别记录；亚马逊的 AI 文案建议不会自动接受。</p><ul>${warnings.map(issue=>`<li>${E(issue.sku)} · ${E((issue.attribute_names||[]).map(name=>labels[name]||name).join('、')||issue.code)}：${E(issue.message)}</li>`).join('')}</ul>${button('listing-edit',x.id,'查看并补齐有来源的字段')}</details>`:'';
    const live=p.live,local=p.local;
    const validationLabel=p.validation?.status==='not_checked'?'本地版本没有单独的检查记录':p.validation?.status==='passed'&&live?.submitted?'提交前检查记录：已通过':p.validation?.label;
    const validation=p.validation?`<p>${E(validationLabel)}</p>${p.validation.error?`<p class="lo-warning">${E(p.validation.error)}</p>`:''}`:'';
    const versions=`<p>${p.published_version?`平台已提交资料 v${E(p.published_version)}`:'尚无已提交资料版本'} · 本地资料 v${E(x.draft_version||p.draft_version||0)}</p>`;
    const localRecord=`<details class="lo-local-record" ${!live||!live.submitted||local?.active?'open':''}><summary>本地修改与检查记录</summary>${versions}<p>${E(local?.label||p.label)}</p>${local?.active?`<p class="lo-warning">${E(p.label)}</p>`:''}${p.error?`<p class="lo-warning">${E(p.error)}</p>`:''}${validation}${x.last_error&&x.last_error!==p.error?`<p class="lo-warning">${E(x.last_error)}</p>`:''}${p.can_retry?button('job-retry',p.job_id,'处理后重试本次提交'):''}</details>`;
    const liveRows=live?.rows?.length?`<details class="lo-platform-rows"><summary>各销售选项的平台状态 · ${live.total} 项</summary><ul>${live.rows.map(row=>`<li><b>${E(row.seller_sku)}</b>${row.asin?` · ${E(row.asin)}`:''}<span class="${['buyable'].includes(row.status)?'':'lo-warning'}">${E(row.label)}</span></li>`).join('')}</ul></details>`:'';
    return `<div class="lo-platform ${live?.status==='buyable'?'is-live':''}">${live?`<strong>${E(live.label)}</strong>`:badge(x.status)}${button('listing-refresh',x.id,'同步亚马逊状态')}</div>${live?.checked_at?`<small class="lo-sync-time">最近同步（北京时间）${timestamp(live.checked_at)}</small>`:''}${local?.changed||local?.active?`<small class="lo-warning">${E(local.label)}${local.error?`：${E(local.error)}`:''}</small>`:''}${liveRows}${improvements}${errors.length?`<details class="lo-issues"><summary>${p.qualification_required?'销售资质待处理':'平台仍有未解决问题'} · ${errors.length} 项</summary><ul>${errors.map(issue=>`<li>${E(issue.sku)} · ${E(issue.code)}：${E(issue.message)}</li>`).join('')}</ul></details>`:''}${p.variation?`<small>${E(p.variation.label)}</small>${!['independent','confirmed'].includes(p.variation.status)?button('listing-refresh',x.id,'回查规格关系'):''}`:''}${localRecord}`;
  }
  function row(x){
    const d=x.draft||{},p=find('product',x.product_id)||{},t=find('target',x.target_id)||{},skus=variants(x);
    const assetId=d.images?.[0]?.asset_id||d.skus?.find(v=>v.images?.length)?.images?.[0]?.asset_id;
    const asset=find('media',assetId),url=asset?.url?.startsWith('/api/commerce/media/file/')?asset.url:'';
    const preview=url?`<img ${window.designImage(url,320)} alt="${E(x.name)} 成品预览">`:'<span class="lo-image-empty">暂无图片</span>';
    const missing=x.material_issues||[],platform=x.platform_issues||x.issues||[];
    const pub=x.publication_progress||{},live=pub.live,submitted=Boolean(x.published_version||Object.keys(x.published_skus||{}).length);
    const writesOff=x.environment==='production'&&getData().settings?.live_writes_enabled===false;
    const detail=x.detail_publication||{},detailDone=Boolean(detail.complete||detail.status==='published');
    const continuing=Boolean(detail.can_continue_listing);
    const completing=Boolean(detail.own_steps_pending&&!continuing);
    const submitDone=live?live.total>0&&live.submitted===live.total:submitted;
    const submitLabel=live?.total?live.submitted?`${live.submitted}/${live.total} 个销售选项已提交`:'尚未提交':submitted?'已有商品提交记录':'尚未提交';
    const steps=[['本地资料',x.material_status==='ready',x.material_status==='ready'?'资料已准备':'资料待补充'],['商品提交',submitDone,submitLabel],['亚马逊销售',live?.status==='buyable',live?.label||'等待同步平台状态'],['图文详情',detailDone,detail.label||'尚未发布']];
    return `<article class="ws-listing-card" data-list-name="${E(x.name)}" data-list-state="${E(live?.label||pub.label||x.status||'')}" data-created-at="${E(x.created_at||'')}" data-updated-at="${E(x.updated_at||'')}">
      <div class="ws-listing-summary"><div class="ws-listing-cover">${preview}</div><div class="ws-listing-identity"><div class="ws-card-topline"><span class="ws-label">${E(t.name||'站点待配置')}</span>${badge(x.environment)}<span class="ws-muted">${E(x.variant_structure?.label||skus.length+' 个规格')}</span></div><h3>${E(x.name)}</h3><p>${E(t.account_name||'未关联账号')} <span>·</span> 品牌 ${E(d.brand||p.brand||'待补充')}</p><span class="ws-muted">${E(d.product_type||p.product_type||'类型待补充')} · 当前资料 v${x.draft_version||0} · ${d.handling_days?E(d.handling_days)+' 天备货':'备货待补'}</span><div class="ws-listing-variants">${variantLabels(x,skus)}</div></div><div class="ws-listing-prices">${prices(x,skus)}</div></div>
      ${x.variant_structure?.distinct_products?`<p class="lo-warning">${E(x.variant_structure.note)}${submitted&&d.variation_theme?' 当前已发布资料仍有合并关系，需单独更正并回读。':''}</p>`:''}
      <div class="ws-listing-journey" aria-label="上架阶段">${steps.map(([title,done,detail],i)=>`<div class="${done?'is-done':''}"><span>${done?'✓':i+1}</span><div><b>${title}</b><small>${E(detail)}</small></div></div>`).join('')}</div>
      <div class="ws-listing-status">${missing.length?`<small class="lo-warning">本地资料：${E(missing.join('；'))}</small>`:''}${progress(x)}${x.detail_publication?.error?`<small class="lo-warning">图文详情：${E(x.detail_publication.error)}</small>`:''}${platform.length?`<details class="lo-issues"><summary>本地上架资料待处理 ${platform.length} 项</summary><ul>${platform.map(v=>`<li>${E(v)}</li>`).join('')}</ul></details>`:pub.information_incomplete?'<small class="lo-warning">平台仍有属性待完善，见上方具体提醒</small>':''}</div>
      <details class="ws-listing-details"><summary>规格、来源与更新时间</summary><div class="ws-listing-detail-grid"><div>${skus.length?skus.map(v=>`<div class="lo-variant"><strong>${E(v.color||v.name||'颜色待补充')}</strong><span>${E(v.size||'尺寸待补充')}</span><small>${E(v.seller_sku)}</small></div>`).join(''):'尚未生成 SKU'}</div><div><span>创建时间（北京时间）</span>${timestamp(x.created_at)}<span>更新时间（北京时间）</span>${timestamp(x.updated_at)}<small>商品 #${E(x.product_id)} · 资料 #${E(x.id)}</small></div></div></details>
      <div class="ws-listing-actions"><div>${button('listing-edit',x.id,'查看 / 编辑','class="primary"')}${x.retirement_id?button('listing-prepare-relisting',x.id,'准备重新刊登草稿'):button(continuing?'listing-continue':'listing-submit',x.id,continuing?'继续完成上架':completing?'上架处理中':submitted?'更新原商品':'首次发布',writesOff?'disabled title="正式平台写入已关闭，可在经营设置中查看"':completing?'disabled title="本款剩余步骤自动继续，结果以平台回查为准"':'')}${button('listing-readiness',x.id,'上架基础核对')}${writesOff?'<span class="ws-muted">正式发布已关闭</span>':''}</div><div><a href="/api/commerce/export/listing/${x.id}">下载资料</a><details class="lo-more"><summary>更多操作</summary><div>${p.project_id?button('generate',p.project_id,'重新生成上架资料'):''}${button('listing-preview',x.id,'平台验证')}${button('listing-refresh',x.id,'刷新状态')}${button('listing-reconcile',x.id,'核对未知提交')}${x.published_version&&!x.retirement_id?button(x.paused?'listing-resume':'listing-pause',x.id,x.paused?'恢复销售':'暂停销售'):''}${platformLinks(x)}</div></details></div></div>
    </article>`;
  }
  return {
    matches:(x,q)=>JSON.stringify([x,find('product',x.product_id),find('target',x.target_id)]).toLowerCase().includes(q),
    render(listings){return (listings.length?`<div class="ws-listing-stack" data-list-cards="listings">${listings.map(row).join('')}</div>`:'<div class="empty"><b>没有匹配的上架商品</b>调整搜索或筛选；完成设计后可生成对应店铺的上架资料。</div>');}
  };
};
