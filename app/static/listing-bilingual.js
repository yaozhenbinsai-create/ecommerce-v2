window.createListingBilingual=function({E,call,api}){
  const fields=[['title','商品标题',3],['bullets','卖点（每行一条）',7],['description','商品描述',8],['search_terms','搜索词',3]];
  const value=(content,key)=>key==='bullets'?(content.bullets||[]).join('\n'):content[key]||'';
  const searchBudget=terms=>{const bytes=new Blob([terms||'']).size;return `已用 ${bytes} / 249 字节${bytes>249?`，超出 ${bytes-249} 字节，请精简相关词`:'；优先覆盖相关叫法、日常需求与使用场景，可与标题适度重复'}`;};
  function skuKeywords(d){
    const skus=d.skus||[];
    if(!skus.some(sku=>Object.hasOwn(sku,'search_terms')))return '';
    const rows=skus.map(sku=>`<dt>${E(sku.seller_sku)}</dt><dd>${E((sku.search_terms??d.content.search_terms)||'（空）')}</dd>`).join('');
    return `<details><summary>已保存的各规格搜索词</summary><p class="c-hint">这里显示当前资料拟提交的搜索词。修改上方共用搜索词并保存后，新内容应用到全部规格。</p><dl>${rows}</dl></details>`;
  }
  function render(d){
    const language=E(d.localization?.language_name||({en:'英语',zh:'中文'})[d.language]||d.language||'未配置');
    return `<section class="c-bilingual"><div class="c-bilingual-heading"><div><h3>上架内容 · 站点原文与中文对照</h3><p class="c-hint">左侧${language}用于站点上架；右侧中文仅供内部核对，不会提交到平台。机器翻译请结合商品实际资料核对。</p></div><button type="button" data-reference-refresh>更新中文对照</button></div><p class="c-hint">一键上架会按商品档案统一生成标题、卖点、描述和搜索词，并保存在本页；可直接查看或手工修改。</p><p class="c-reference-status" role="status" aria-live="polite">正在读取中文对照…</p><div class="c-bilingual-grid">${fields.map(([key,title,rows])=>`<label class="c-field"><span>${E(title)} · ${language}原文</span><textarea name="${key}" rows="${rows}" ${key==='title'?'maxlength="75"':''} data-reference-source>${E(value(d.content,key))}</textarea>${key==='search_terms'?`<small class="c-hint" data-search-budget role="status" aria-live="polite">${searchBudget(value(d.content,key))}</small>`:''}</label><div class="c-reference-field"><span>${E(title)} · 中文参考</span><div class="c-reference-text" data-reference-field="${key}" style="min-height:${rows*22+22}px">正在读取…</div></div>`).join('')}</div>${skuKeywords(d)}</section>`;
  }
  function mount(dialog,id){
    const section=dialog.querySelector('.c-bilingual');
    const button=section.querySelector('[data-reference-refresh]'),status=section.querySelector('.c-reference-status');
    const keywords=section.querySelector('[name="search_terms"]');
    keywords.addEventListener('input',()=>{section.querySelector('[data-search-budget]').textContent=searchBudget(keywords.value);});
    let busy=false,translatedSource='',referenceRequest=0;
    const read=()=>Object.fromEntries(fields.map(([key])=>{const v=section.querySelector(`[name="${key}"]`).value;return [key,key==='bullets'?v.split('\n').filter(Boolean):v];}));
    const sourceKey=content=>JSON.stringify(fields.map(([key])=>value(content||{},key)));
    const active=()=>dialog.open&&dialog.contains(section);
    const clear=message=>section.querySelectorAll('[data-reference-field]').forEach(x=>x.textContent=message);
    section.querySelectorAll('[data-reference-source]').forEach(input=>input.addEventListener('input',()=>{
      if(sourceKey(read())===translatedSource)return;
      clear('原文已修改，请更新中文对照');
      status.textContent='站点原文已修改，点击“更新中文对照”后查看对应译文。';
    }));
    async function refresh(){
      if(busy)return;
      const content=read(),snapshot=sourceKey(content),request=++referenceRequest;busy=true;button.disabled=true;
      status.textContent='正在生成中文对照，首次翻译可能需要一点时间，可继续编辑其他资料。';
      clear('正在读取…');
      try{
        const result=await call(`/listings/${id}/chinese-reference`,{content});
        if(!active()||request!==referenceRequest)return;
        if(snapshot!==sourceKey(read())){clear('原文已修改，请更新中文对照');status.textContent='原文已修改，请更新中文对照以匹配当前内容。';return;}
        translatedSource=snapshot;
        for(const [key] of fields)section.querySelector(`[data-reference-field="${key}"]`).textContent=value(result.content,key)||'原文未填写';
        status.textContent='中文对照已与当前原文对应。译文保存在本机，再次查看无需重复翻译。';
      }catch(error){if(active()){clear('中文对照暂不可用');status.textContent=error.message||'中文对照暂未生成，请重试。';}}
      finally{busy=false;if(active())button.disabled=false;}
    }
    button.onclick=refresh;
    async function loadSaved(){
      const request=++referenceRequest;
      try{
        const result=await api(`/api/commerce/listings/${id}/chinese-reference`);
        if(!active()||request!==referenceRequest)return;
        if(!result.content){clear('尚未生成中文对照');status.textContent='可点击“更新中文对照”生成译文；查看资料不会自动调用模型。';return;}
        if(sourceKey(result.source_content)!==sourceKey(read())){clear('原文已修改，请更新中文对照');status.textContent='原文已修改，请更新中文对照以匹配当前内容。';return;}
        translatedSource=sourceKey(result.source_content);
        for(const [key] of fields)section.querySelector(`[data-reference-field="${key}"]`).textContent=value(result.content,key)||'原文未填写';
        status.textContent='已读取保存的中文对照，未调用模型。';
      }catch(error){if(active()&&request===referenceRequest){clear('未读取到中文对照');status.textContent=error.message||'中文对照读取失败';}}
    }
    loadSaved();
  }
  return {render,mount};
};
