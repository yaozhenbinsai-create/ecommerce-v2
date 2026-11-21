/* One click in business configuration; no classification forms in daily use. */
(() => {
  let results=[],running=false;
  let classification={categories:[],mappings:[]},detailRequest=0;
  const sourceStates={reference:'官方历史资料 · 待站点验证',manual:'本地维护',pending:'待补充',sample:'沙箱样例'};
  const detailField=(label,value)=>`<div><dt>${esc(label)}</dt><dd>${esc(value||'待补充')}</dd></div>`;
  async function loadClassification(){classification=await api('/api/commerce/categories/details')}
  function officialChoices(m){
    const report=m.official_classification||{},fields=report.fields||{};
    if(!['complete','needs_selection'].includes(report.status)||!Object.keys(fields).length)return '';
    const labels={recommended_browse_nodes:'分类节点',item_type_keyword:'分类关键词'};
    const controls=Object.entries(fields).map(([field,info])=>{const value=field==='recommended_browse_nodes'?m.category_id:m.item_type_keyword;return `<label class="c-field"><span>${esc(labels[field]||field)}</span><select data-official-field="${esc(field)}"><option value="">请选择官方分类</option>${info.options.map(o=>`<option value="${esc(o.value)}" ${o.value===value?'selected':''}>${esc(o.path?o.path+' · '+o.value:o.value)}</option>`).join('')}</select></label>`}).join('');
    const content=`<div class="business-classification-choice" data-choice-mapping="${m.id}">${controls}<button type="button" data-save-classification="${m.id}" data-revision="${m.revision}" data-target-id="${m.target_id||''}">保存分类选择</button><p data-choice-message role="status"></p></div>`;
    return report.status==='complete'?`<details><summary>更换官方分类</summary>${content}</details>`:content;
  }
  function mappingCard(m){
    const report=m.official_classification||{},complete=['complete','not_required'].includes(report.status),fields=report.fields||{};
    const node=m.category_id||(complete&&!fields.recommended_browse_nodes?'此类型不使用数字分类节点':'待获取');
    const keyword=m.item_type_keyword||(complete&&!fields.item_type_keyword?'此类型不使用分类关键词':'待获取');
    return `<section class="business-map-card"><h4>${esc(m.platform)} · ${esc(m.marketplace_name||m.marketplace_id)}${m.target_id?' · '+esc(m.target_account_name||m.target_name):''} <span class="badge ${m.enabled&&complete?'':'off'}">${!m.enabled?'已停用':complete?'官方分类已补齐':'分类待补充'}</span></h4><dl class="business-map-fields">${detailField('平台分类名称',m.name)}${detailField('产品类型代码',m.product_type)}${detailField('平台分类路径',m.category_path)}${detailField('分类节点 ID（Browse Node）',node)}${detailField('分类关键词（Item Type Keyword）',keyword)}${detailField('站点标识',m.marketplace_id)}${detailField('类型代码来源',sourceStates[m.status]||m.status)}${detailField('官方分类查询',report.source||'尚未取得官方分类资料')}</dl><p class="c-hint">${esc(report.message||'已关联产品类型，尚未获取官方分类节点或关键词。')}</p>${report.checked_at?`<p class="c-hint">查询时间：${esc(new Date(report.checked_at).toLocaleString())}</p>`:''}${officialChoices(m)}${m.notes?`<p class="c-hint">${esc(m.notes)}</p>`:''}</section>`;
  }
  function mappingDetails(id){
    const category=classification.categories.find(c=>(c.business_ids||[]).includes(Number(id)));
    if(!category)return '<p class="c-hint">尚未关联产品分类。保存业务后，在业务列表点击“一键映射”。</p>';
    const mappings=(classification.target_mappings||classification.mappings).filter(m=>m.category_id_local===category.id);
    const pending=(results.find(r=>r.business_id===Number(id))?.destinations||[]).filter(d=>d.status!=='matched');
    return `<p class="business-map-local"><strong>本地产品分类</strong><span>${esc(category.path||category.name)}${category.enabled?'':'（已停用）'}</span></p><p class="c-hint">分类关联、官方分类读取和保存均由“一键映射”完成。需要更新或重试时，在业务列表点击“一键映射”。</p>`+
      mappings.map(mappingCard).join('')+
      (!mappings.length?'<p class="c-hint">当前分类尚无平台映射，请在业务列表点击“一键映射”。</p>':'')+
      pending.map(d=>`<p class="c-hint">${esc(d.platform)} · ${esc(d.site_name)}：${esc(d.message)}</p>`).join('');
  }
  async function saveChoice(button){
    const box=button.closest('[data-choice-mapping]'),message=box.querySelector('[data-choice-message]');button.disabled=true;
    try{
      const choices=Object.fromEntries([...box.querySelectorAll('[data-official-field]')].map(el=>[el.dataset.officialField,el.value]));
      await api('/api/commerce/category-mappings/'+button.dataset.saveClassification+'/classification-choice',{method:'POST',body:JSON.stringify({revision:Number(button.dataset.revision),choices,...(button.dataset.targetId?{target_id:Number(button.dataset.targetId)}:{})})});
      await Promise.all([loadClassification(),loadResults()]);
      const mapping=(classification.target_mappings||classification.mappings).find(m=>m.id===Number(button.dataset.saveClassification)&&(!button.dataset.targetId||m.target_id===Number(button.dataset.targetId)));
      if(box.isConnected)box.closest('.business-map-card').outerHTML=mappingCard(mapping);
      notice('官方分类已保存，并已带入未提交的生产资料');
    }catch(e){message.textContent=e.message}finally{button.disabled=false}
  }
  async function showMappings(id){
    const request=++detailRequest,name=(items.businesses||[]).find(b=>b.id===id)?.name||'业务';
    drawResult(name+' · 分类映射','<p>正在读取已保存的分类映射…</p>');if(!resultDialog.open)resultDialog.showModal();
    try{await Promise.all([loadClassification(),loadResults()]);if(request===detailRequest)drawResult(name+' · 分类映射',mappingDetails(id))}
    catch(e){if(request===detailRequest)drawResult('映射详情读取失败',`<p>${esc(e.message)}</p><p>关闭后点击“查看映射”重试。</p>`)}
  }
  const previousRow=row,previousRender=render,previousEditor=openEditor;
  const allButton=document.createElement('button');allButton.type='button';allButton.className='primary';allButton.dataset.businessMap='all';allButton.textContent='一键映射';allButton.hidden=true;
  const add=document.querySelector('#add'),controls=document.createElement('div');controls.className='actions business-header-actions';add.before(controls);controls.append(add,allButton);
  const resultDialog=document.createElement('dialog');resultDialog.className='c-dialog';resultDialog.id='business-mapping-dialog';document.body.append(resultDialog);
  function drawResult(title,body){resultDialog.innerHTML=`<form class="c-form"><div class="dialog-head"><h2>${esc(title)}</h2><button class="icon-btn" type="button" data-map-close aria-label="关闭">×</button></div><div class="c-body">${body}</div><div class="dialog-foot"><button type="button" data-map-close>关闭</button></div></form>`;resultDialog.querySelectorAll('[data-map-close]').forEach(b=>b.onclick=()=>resultDialog.close());}
  function summary(values){return values.map(x=>`<section class="business-map-result"><h3>${esc(x.name)} <span class="badge ${x.status==='partial'?'off':''}">${esc(x.message)}</span></h3>${x.destinations.length?x.destinations.map(d=>`<p><strong>${esc(d.platform)} · ${esc(d.site_name)}</strong><span>${esc(d.message)}</span></p>`).join(''):'<p>请先在平台页面启用需要使用的平台。</p>'}</section>`).join('')+'<p class="c-hint">已完成的映射会自动用于后续生产资料。</p>'}
  row=function(x){const html=previousRow(x);if(page!=='businesses')return html;const state=results.find(r=>r.business_id===x.id);const button=`<button type="button" data-business-view="${x.id}">查看映射</button><button type="button" data-business-map="${x.id}" ${running||!x.enabled?'disabled':''}>一键映射</button>`;let column=0;const platforms=[...new Set((items.platform_accounts||[]).filter(a=>a.enabled).map(a=>a.platform))];return html.replace(/<td>[\s\S]*?<\/td>/g,cell=>column++===1?`<td>${esc(platforms.join('、')||'暂无启用平台')}<div class="sub">${esc(state?.message||'点击一键映射，自动关联分类')}</div></td>`:cell).replace('<div class="actions">',`<div class="actions">${button}`)};
  render=function(){const value=previousRender();allButton.hidden=page!=='businesses'||!window.workspaceDataLoaded;allButton.disabled=running;return value};
  openEditor=function(id=null){const value=previousEditor(id);if(page==='businesses'){
    for(const name of ['platforms','category_paths','amazon_product_type','content_language']){const field=document.querySelector(`[data-field="${name}"]`);if(field)field.hidden=true}
    const fields=document.querySelector('#fields'),panel=document.createElement('section');panel.className='business-map-panel c-wide';panel.dataset.mapDetails='1';
    panel.dataset.businessId=id||'';panel.innerHTML='<h3>产品分类与平台映射</h3><div data-map-content aria-live="polite">'+(id?'正在读取已保存的分类映射…':'请先保存业务，再在业务列表点击“一键映射”。')+'</div>';fields.prepend(panel);
    if(id)Promise.all([loadClassification(),loadResults()]).then(()=>{if(panel.isConnected)panel.querySelector('[data-map-content]').innerHTML=mappingDetails(id)}).catch(e=>{if(panel.isConnected)panel.querySelector('[data-map-content]').textContent='映射详情读取失败：'+e.message+'。请关闭后重新打开。'});
  }return value};window.openEditor=openEditor;
  async function loadResults(){results=await api('/api/commerce/businesses/mapping-results');if(page==='businesses')render()}
  async function map(id){if(running)return;running=true;render();drawResult('正在同步官方分类','<p>正在关联业务分类，并只读查询Amazon正式站点的分类节点和关键词。完成后自动保存。</p>');if(!resultDialog.open)resultDialog.showModal();try{
    let job=await api('/api/commerce/businesses/map',{method:'POST',body:JSON.stringify({...(id?{business_id:id}:{}),read_official:true})});
    while(['queued','running','retry_wait'].includes(job.status)){await new Promise(resolve=>setTimeout(resolve,1500));job=await api('/api/commerce/businesses/mapping-jobs/'+job.id)}
    if(job.status!=='completed')throw new Error(job.error||'映射暂未完成，请稍后重试');
    const values=job.result.businesses||[];await Promise.all([loadClassification(),loadResults()]);drawResult(values.some(x=>x.status==='partial')?'映射结果':'映射完成',summary(values)+values.map(x=>`<h3>${esc(x.name)} · 分类详情</h3>${mappingDetails(x.business_id)}`).join(''));
    document.querySelectorAll('[data-map-details][data-business-id]').forEach(panel=>{if(panel.dataset.businessId)panel.querySelector('[data-map-content]').innerHTML=mappingDetails(Number(panel.dataset.businessId))});
    await load();notice(values.some(x=>x.status==='partial')?'部分平台暂未完成，原因已列出':'平台分类已自动映射');
  }catch(e){drawResult('暂未完成',`<p>${esc(e.message)}</p>`);notice(e.message,true)}finally{running=false;render()}}
  document.addEventListener('click',e=>{const button=e.target.closest('[data-business-map]');if(!button)return;e.preventDefault();map(button.dataset.businessMap==='all'?null:Number(button.dataset.businessMap))});
  document.addEventListener('click',e=>{const button=e.target.closest('[data-business-view]');if(!button)return;e.preventDefault();showMappings(Number(button.dataset.businessView))});
  document.addEventListener('click',e=>{const button=e.target.closest('[data-save-classification]');if(!button)return;e.preventDefault();saveChoice(button)});
  loadResults().catch(()=>{});render();
})();
