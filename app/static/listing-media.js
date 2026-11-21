window.createListingMedia=function({E,api}){
  const safe=url=>/^\/design-outputs\//.test(url||'')?E(url):'';
  function publication(state,id){
    const p=state||{label:'详情待随商品提交'};
    const waiting=!p.continuation_held&&[p,...(p.children||[])].some(child=>['waiting_asin','waiting_catalog'].includes(child.status));
    const next=Math.min(...[p,...(p.children||[])].map(child=>Number(child.next_at)).filter(value=>value>0));
    const when=Number.isFinite(next)?new Date(next*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'';
    return `<h3>图文详情发布进度</h3><p>${E(p.label)}${p.version?' · 上架资料 v'+E(p.version):''}</p>${p.total_count?`<p>已发布 ${E(p.published_count||0)} / ${E(p.total_count)} 份详情</p>`:''}${p.error?`<p class="c-error">${E(p.error)}</p>`:''}<p class="c-hint">完整详情属于本款上架流程，随商品自动提交；中断后继续原进度。平台销售生效和详情审核分别显示，已上传图片和原草稿会复用。</p>${waiting?`<p>正在等待亚马逊识别商品，系统会自动继续${when?'；下次检查约 '+E(when)+'（北京时间）':''}。无需反复点击。</p>`:''}${p.reference?`<p class="c-hint">平台详情编号：${E(p.reference)}</p>`:''}${p.children?`<p>共 ${p.children.length} 份规格详情，逐项显示实际发布结果。</p>${p.children.map(child=>`<p>${E(child.sku_label||'规格 #'+(child.section_sku_ids||[]).join('、'))}：${E(child.label)}${child.error?' · '+E(child.error):''}</p>`).join('')}`:''}${p.can_update_render?`<p class="c-hint">原完整详情图保持不变，发布时自动适配连续平台模块。沿用原商品编号和已提交资料。</p><button type="button" data-c-action="detail-render" data-c-id="${id}">更新完整详情展示</button>`:''}${p.render_protocol==='standard-strips-v2'?'<p class="c-hint">现有成品已按原顺序适配平台模块；原详情回执保留。</p>':''}${p.id?`<button type="button" data-c-action="detail-check" data-c-id="${id}">回查详情状态</button>${p.can_continue_listing?` <button type="button" data-c-action="listing-continue" data-c-id="${id}">继续完成上架</button>`:''}`:''}`;
  }
  function render(detail,language,mode='included',stale=false){
    const url=safe(detail?.url);
    const note=mode==='preview'?'当前设计预览 · 尚未包含在这份上架资料中。需要先重新生成生产资料，才会进入下载和上架资料。':'当前上架资料包含的详情页';
    if(detail?.presentation_protocol==='one-detail-per-sku-v1'){
      const documents=detail.sku_documents||[];
      return `<h3>各规格完整详情图</h3><p>${note}</p><p class="c-hint">详情版本 ${E(detail.run_id)} · ${documents.length} 个规格各1张完整详情图 · 图内语言 ${E(detail.language)}</p>${documents.map(doc=>{const own=safe(doc.url);return `<details><summary>${E(doc.name||doc.color||doc.sku_id)}</summary>${own?`<a href="${own}" target="_blank" rel="noopener">打开本规格完整详情图</a><div class="detail-continuous"><img ${window.designImage(doc.url,960)} alt="${E(doc.name||doc.sku_id)}完整详情图"></div>`:'<p>本规格图片不可用</p>'}</details>`}).join('')}`;
    }
    return `<h3>商品图文详情页</h3>${url?`<p>${note}</p>${stale?'<p class="c-hint">商品资料已有变化，此设计需要重新核对。</p>':''}<p class="c-hint">详情页版本 ${E(detail.run_id)} · ${(detail.sections||[]).length} 段 · 图内语言 ${E(detail.language)}${String(detail.language).split(/[-_]/)[0]!==String(language).split(/[-_]/)[0]?' · 与站点语言不同，使用前请适配':''}</p><a href="${url}" target="_blank" rel="noopener">打开完整原图</a><div class="detail-continuous"><img ${window.designImage(detail.url,960)} alt="完整商品长详情页"></div>`:'<p>暂未找到已完成的详情页，请在设计工作台完成详情页后重新生成生产资料。</p>'}`;
  }
  async function mount(dialog,id,draft){
    const box=dialog.querySelector('.detail-material-summary');
    if(draft.detail_page)return;
    box.innerHTML='<h3>商品图文详情页</h3><p>正在读取商品设计的详情页…</p>';
    try{
      const result=await api(`/api/commerce/listings/${id}/design-preview`);
      if(!dialog.open||!dialog.contains(box))return;
      box.innerHTML=render(result.detail,draft.language,'preview',result.stale);
    }catch(error){if(dialog.open&&dialog.contains(box))box.innerHTML=`<h3>商品图文详情页</h3><p>${E(error.message||'详情页读取失败，请重新打开查看')}</p>`;}
  }
  return {render,mount,publication};
};
