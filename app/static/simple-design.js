/* Integrated design UI. Retains the existing workspace layout, owns all design events. */
(function(){
  const baseRender=render;
  const regularAddAction=$('#add').onclick;
  $('#add').onclick=function(event){
    if(page!=='design_projects')return regularAddAction?.call(this,event);
    if(typeof window.commerceOpen==='function')return window.commerceOpen('supplier_imports');
    page='supplier_imports';$('#search').value='';render();
  };
  let opened=null, refreshing=false, lastMarkup='', lastPoll=0, revision=0;
  const pending=new Set(),cleanupPlans=new Map();
  const stages=['masters','plans','images'];
  const labels=['原图与资料 → 内部母稿','每规格7+1独立方案','逐图生成销售图与完整详情图'];
  const statuses={queued:'等待开始',running:'处理中',retry_wait:'等待重连',paused:'已暂停',completed:'已完成',superseded:'已重新设计'};
  const safeUrl=u=>(/^\/(uploads|design-outputs)\//.test(u||'')||/^\/api\/commerce\/media\/file\/[a-f0-9]{64}\.jpg$/.test(u||''))?u:'';
  const safeMainUrl=u=>/^\/api\/commerce\/media\/file\/[A-Za-z0-9_.-]+$/.test(u||'')?u:'';
  const btn=(id,action,label,disabled=false,extra='')=>`<button type="button" data-simple-id="${id}" data-simple-action="${action}" ${disabled?'disabled':''} ${extra}>${esc(label)}</button>`;
  const active=f=>['queued','running','retry_wait'].includes(f?.run?.status);
  const isRetired=f=>Boolean(f?.run&&((f.run.current_pipeline===false&&!f.run.can_resume_with_current_policy)||(f.run.workflow&&f.run.workflow!==(f.current_workflow||'archive-story-v5'))));
  const outputCount=f=>f?.output_count??f?.outputs?.length??0;
  const generatedCount=f=>f?.preview?.length??f?.run?.completed_count??0;
  const imageCounts=f=>`${f?.run?`本轮已生成${generatedCount(f)}张 · `:''}已交付${outputCount(f)}张`;
  const hasDetail=f=>f?.has_detail??Boolean(f?.detail_page?.current);
  const detailCount=f=>f?.detail_count??f?.detail_page?.current?.sections?.length??0;
  const working=f=>active(f)||['queued','running','retry_wait'].includes((f?.detail_run||f?.detail_page?.run)?.status);
  const summary=f=>f?.run?`${f.run.stage_label} · ${statuses[f.run.status]||f.run.status}${f.run.stage_total?` · ${f.run.stage_completed}/${f.run.stage_total}${f.run.stage==='plans'?'份方案':'项'}`:''}${f.run.error?' · '+f.run.error:''}`:'点击AI设计，自动读取商品资料与原图';
  function skuProgress(f){
    const products=f.recognition?.products||[],pages=f.storyboard||[];
    if(!f.run?.design_contract||!products.length)return '';
    const cell=(tasks,total,completeLabel,planning=[])=>{
      const done=tasks.filter(t=>t.status==='completed').length;
      const stateTasks=[...tasks,...planning];
      let label=total&&done===total?completeLabel:'';
      if(!label){
        const state=['paused','running','retry_wait'].find(s=>stateTasks.some(t=>t.status===s));
        label=state?(statuses[state]||state):done?'待继续':'未开始';
      }
      return `<td><b>${done}/${total}</b><span>${esc(label)}</span></td>`;
    };
    return `<section class="flow-sku-progress"><h3>按规格查看进度</h3><div class="flow-progress-table"><table><thead><tr><th scope="col">规格</th><th scope="col">设计方案</th><th scope="col">销售图</th><th scope="col">完整详情图</th></tr></thead><tbody>${products.map(product=>{
      const own=(f.tasks||[]).filter(t=>t.product_key===product.key);
      const slots=pages.filter(p=>p.product_key===product.key);
      const planTasks=own.filter(t=>t.kind==='plan');
      const bundle=own.find(t=>t.kind==='story'&&t.status==='completed')?.result?.plans||{};
      const plans=slots.map(page=>planTasks.find(t=>t.task_key===page.page_key)||
        (Object.hasOwn(bundle,page.page_key)?{status:'completed'}:null)).filter(Boolean);
      const sales=slots.filter(p=>p.display_role!=='detail_page');
      const details=slots.filter(p=>p.display_role==='detail_page');
      const images=own.filter(t=>t.kind==='image');
      return `<tr data-product-key="${esc(product.key)}"><th scope="row">${esc(product.name||product.key)}</th>${cell(plans,slots.length,'已完成',own.filter(t=>t.kind==='story'))}${cell(images.filter(t=>sales.some(p=>p.page_key===t.task_key)),sales.length,'已生成')}${cell(images.filter(t=>details.some(p=>p.page_key===t.task_key)),details.length,'已生成')}</tr>`;
    }).join('')}</tbody></table></div></section>`;
  }
  function beijingTime(value){
    if(!value)return '未记录';
    const text=String(value).replace(' ','T');
    const date=new Date(text+(/(?:Z|[+-]\d{2}:?\d{2})$/.test(text)?'':'Z'));
    return Number.isNaN(date.getTime())?'未记录':new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date);
  }
  function actions(p,f){
    const id=p.id,busy=pending.has(id),a=active(f),retired=isRetired(f);
    return (a?btn(id,'view','查看进度',busy,'class="primary"')+btn(id,'pause','暂停',busy):f?.run?.status==='paused'?btn(id,'view','查看进度',busy)+(retired||f.run.quality_blocked?'':btn(id,'resume','继续设计',busy,'class="primary"')):f?.run?.status==='completed'&&hasDetail(f)?btn(id,'view','查看设计成果',busy,'class="primary"'):btn(id,retired?'redesign':'design',retired?'新建整组设计（保留历史）':outputCount(f)?'继续设计':'AI设计',busy,'class="primary"'))+
      (f?.run?.status==='completed'?btn(id,'materials','生成上架资料',busy):'')+
      btn(id,'listing','完成设计并同步商品',busy)+
      ((f?.run||f?.cleanup_pending)?`<details class="flow-more"><summary>更多操作</summary><div>${f?.run?btn(id,'redesign',retired?'新建整组设计（保留历史）':'整组重新设计',busy||a):''}${((f?.run&&!a)||f?.cleanup_pending)?btn(id,'cleanup',f?.cleanup_pending?'重试清理文件':'清除不合格设计',busy):''}</div></details>`:'');
  }
  render=function(){
    const enteringDesign=!document.body.classList.contains('design-journey-page');
    document.body.classList.toggle('design-journey-page',page==='design_projects');
    if(page!=='design_projects'){lastMarkup='';return baseRender()}
    document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
    $('#title').textContent='AI 设计工坊';$('#subtitle').textContent='从真实商品出发，让每个规格拥有自己的销售故事。';
    $('#addLabel').textContent='导入商品';$('#listTitle').textContent='选择要设计的产品';$('#panelHint').textContent='保留产品真实样貌，用图片表达购买理由';
    const q=$('#search').value.trim().toLowerCase();
    const list=(items.design_projects||[]).filter(p=>`${p.product_name} ${p.supplier_name}`.toLowerCase().includes(q));
    const projects=items.design_projects||[];
    $('#stats').innerHTML=[['设计项目',projects.length],['正在生成',projects.filter(p=>active(p.flow)).length],['设计已完成',projects.filter(p=>p.flow?.run?.status==='completed').length],['等待继续',projects.filter(p=>p.flow?.run?.status==='paused').length]].map(([title,n])=>`<div class="stat"><small>${esc(title)}</small><b>${n}</b></div>`).join('');
    $('#search').placeholder='搜索商品或供应商';
    const markup=`<div class="ws-design-intro"><div><span>01 <b>真实资料与原图</b></span><i>→</i><span>02 <b>每规格内部母稿</b></span><i>→</i><span>03 <b>7 张销售图 + 1 张完整详情图</b></span></div><p>每个规格先从原图建立内部母稿，后续方案与成图只参考对应母稿，并结合商品资料呈现销售故事与完整详情。</p></div>`+(list.length?`<div class="c-toolbar"><button type="button" data-focus-listings>集中处理已有上架资料的商品</button></div><div class="flow-products">${list.map(p=>{
      const f=p.flow;
      const whole=f?.detail_page?.current?.presentation_protocol==='one-detail-per-sku-v1'||['three-stage-eight-plans-v1','three-stage-eight-plans-v2'].includes(f?.run?.design_contract);
      return `<article class="flow-product" data-created-at="${esc(p.created_at||'')}" data-updated-at="${esc(p.updated_at||'')}"><div class="flow-product-info"><div class="ws-design-source">${safeUrl(p.source_images?.[0])?`<img ${window.designImage(p.source_images[0],640)} alt="${esc(p.product_name)}">`:'<span class="flow-no-image">待补参考图</span>'}<span>商品原图 · 设计起点</span></div><div><div class="flow-product-heading"><h3>${esc(p.product_name)}</h3><div>${btn(p.id,'facts','产品资料')}</div></div><p>${esc(p.supplier_name||'未设置供应商')} · ${p.source_images?.length||0}张参考图</p><div class="flow-product-meta"><b class="flow-product-status ${active(f)?'processing':''}">${esc(f?.run?statuses[f.run.status]:'待设计')}</b><span>${esc(imageCounts(f))}</span></div><div class="ws-design-stages">${labels.map((label,i)=>`<span class="${f?.run?.status==='completed'||stages.indexOf(f?.run?.stage)>i?'is-done':''}"><i>${f?.run?.status==='completed'||stages.indexOf(f?.run?.stage)>i?'✓':i+1}</i>${esc(['内部母稿','独立设计方案','销售与详情成品'][i])}</span>`).join('')}</div><p class="flow-card-status ${active(f)?'running':''}">${esc(summary(f))}</p><p class="flow-created-at">${hasDetail(f)?detailCount(f)+(whole?'张完整详情图':'段历史详情图'):'详情页待完成'} · 创建于 ${esc(beijingTime(p.created_at))}</p></div></div><div class="flow-product-actions">${actions(p,f)}</div></article>`;
    }).join('')}</div>`:'<div class="empty"><b>从一个真实商品开始设计</b>先导入货盘，在商品库选择实际规格和对应原图。</div>');
    if(enteringDesign||lastMarkup!==markup){$('#content').innerHTML=markup;lastMarkup=markup}
  };
  function dialog(){
    let d=$('#simpleDesignDialog');
    if(!d){d=document.createElement('dialog');d.id='simpleDesignDialog';d.className='flow-dialog';document.body.append(d);d.addEventListener('close',()=>opened=null)}
    return d;
  }
  function imageCard(card,id,redo,currentPipeline,correctCandidate=false){
    const check=card.verification||{},generated=card.pipeline?.generation_protocol==='generated-assets-v1'&&check.decision==='not_performed',current=currentPipeline&&check.protocol==='image-fidelity-v3'&&check.independent_review_protocol==='independent-visual-review-v1',scope='商品与本页内容检查',label=generated?'已生成':current?({eligible:scope+'通过',reject:scope+'未通过',uncertain:scope+'待核实'}[check.decision]||'待检查'):'历史审核记录 · 需要按当前要求重新检查';
    const mode={alpha_subject:'保留原图透明主体',original_photo_panel:'保留完整原照片面板'}[card.preservation?.mode]||'';
    return `<article><a href="${esc(safeUrl(card.url))}" target="_blank" rel="noopener"><img ${window.designImage(card.url)} alt="${esc(card.type||card.product||'设计图片')}">${esc(card.display_role==='sales_cover'?'销售封面':card.type||card.product||'成品图')} ${esc(card.color||'')}</a><p class="flow-muted">${esc(label)}${check.reason?' · '+esc(check.reason):''}${!generated&&(!current||check.decision!=='eligible')?' · 暂不可用于刊登':''}</p>${mode?`<p class="flow-muted">${esc(mode)} · 未生成新视角或重画商品</p>`:''}${correctCandidate?`<label><input type="checkbox" data-candidate-correction data-page-key="${esc(card.page_key)}" value="${esc(card.sha256)}">选择这张事实错误的候选</label>`:''}${redo?btn(id,'single','单张重新设计',pending.has(id),`data-page-key="${esc(card.page_key)}"`):''}${card.prompt?`<details><summary>本张提示词（${Array.from(card.prompt).length}字）</summary><p class="simple-prompt">${esc(card.prompt)}</p></details>`:''}</article>`;
  }
  function drawDetail(p,f){
    const d=dialog();const signature=JSON.stringify(f);
    if(d.dataset.signature===signature&&Number(d.dataset.project)===p.id)return;
    const openDetails=[...d.querySelectorAll('details[open]')].map(e=>e.dataset.section).filter(Boolean),scroll=d.scrollTop;
    const note=Number(d.dataset.project)===p.id?(d.querySelector('[data-design-note]')?.value||''):'';
    const selected=Number(d.dataset.project)===p.id?[...d.querySelectorAll('[data-candidate-correction]:checked')].map(x=>x.value):[];
    const selectedMasters=Number(d.dataset.project)===p.id?[...d.querySelectorAll('[data-master-correction]:checked')].map(x=>x.value):[];
    d.dataset.signature=signature;d.dataset.project=p.id;
    const candidateRepair=f.run?.status==='paused'&&['images','detail'].includes(f.run?.stage)&&!isRetired(f)&&!(f.detail_page?.preview?.length);
    const r=f.run,modern=!r||Boolean(r.design_contract);
    const shownStages=modern?stages:['inputs','masters','plans','images'];
    const shownLabels=modern?labels:['完整原图和资料','多视角产品母稿','整组创意设计','设计出图'];
    const index=shownStages.indexOf(r?.stage==='detail'?'images':r?.stage||'masters');
    const direction=f.creative_direction||{};
    const visual=direction.visual_identity||{};
    const creativeNotes=(direction.purchase_reason?`<p><b>本款购买理由：</b>${esc(direction.purchase_reason)}</p>`:'')+
      (Object.values(visual).filter(Boolean).length?`<p><b>视觉方向：</b>${Object.values(visual).filter(Boolean).map(esc).join(' · ')}</p>`:'')+
      (direction.creative_rationale?`<p class="flow-muted">${esc(direction.creative_rationale)}</p>`:'');
    d.innerHTML=`<div class="dialog-head"><h2>${esc(p.product_name)}</h2><button type="button" class="icon-btn" data-simple-close>×</button></div><div class="flow-dialog-body"><ol class="flow-progress">${shownLabels.map((label,i)=>`<li class="${r?.stage==='complete'||i<index?'done':i===index?'current':''}"><span>${i+1}</span>${label}</li>`).join('')}</ol><p class="flow-status" role="status">${esc(summary(f))}</p>${f.validation_policy==='direct-design-no-rejection-v1'?'<p class="flow-muted">母稿生成后自动规划，成图可直接用于组装上架资料。</p>':''}${p.platform==='亚马逊'?`<p class="flow-muted">${modern?'新设计以有场景、有商品视觉焦点的销售封面作为实际指定首图，七张图库完整编排。商品、环境与随售组成须清楚区分。':'当前为历史设计，保留当时的首图和图片顺序。'} 目标类目要求、Amazon接受与买家实际显示分别核实。</p>`:''}
      ${skuProgress(f)}
      ${direction.theme?`<section class="flow-story"><h3>本次整组创意 · ${esc(direction.theme)}</h3><p>${esc(direction.promise||'')}</p>${creativeNotes}<p class="flow-muted">${esc(direction.audience||'')}</p><p>${esc(direction.arc||'')}</p><details data-section="story"><summary>查看每张图片的重点与镜头</summary><div class="flow-delivery-grid">${(f.storyboard||[]).map(x=>`<article><b>${esc(x.type)} · ${esc(x.takeaway||x.purpose)}</b><p>${esc(x.visual_hook||'')}</p><p class="flow-muted">${esc(x.camera||'')} · ${esc(x.story_role||'')}</p></article>`).join('')}</div></details></section>`:''}
      ${f.detail_recovery?.available?`<section class="flow-story"><h3>保留成品并补齐缺图</h3><p>已有 ${f.detail_recovery.preserved_master_count||0} 张内部母稿、${f.detail_recovery.preserved_gallery_count} 张图库与 ${f.detail_recovery.preserved_detail_count} 张详情将原样保留，仅为缺失的 ${f.detail_recovery.missing_count} 张${f.detail_recovery.stage==='masters'?'内部母稿':'图片'}建立新请求。${f.detail_recovery.unstarted_count?`另外 ${f.detail_recovery.unstarted_count} 张尚未发起过请求，将按原计划继续。`:""}</p><p>${esc(f.detail_recovery.reason)}</p>${btn(p.id,'replace-missing-detail','确认新建缺图请求（可能产生费用）',pending.has(p.id))}</section>`:''}
      ${f.preview.length&&r?.status!=='completed'?`<h3>本轮已生成（${generatedCount(f)}张）</h3><p class="flow-muted">这些图片已生成；整组设计（含详情页）完成后才计入已交付。</p><div class="flow-delivery-grid">${f.preview.map(x=>imageCard(x,p.id,false,!isRetired(f),candidateRepair)).join('')}</div>`:''}
      <h3>已交付成品（${outputCount(f)}张）</h3>${f.outputs.length?`<div class="flow-delivery-grid">${[...f.outputs].sort((a,b)=>Number(b.display_role==='sales_cover')-Number(a.display_role==='sales_cover')).map(x=>imageCard(x,p.id,f.can_redo_single&&!active(f)&&f.run?.status!=='paused'&&!isRetired(f),!isRetired(f))).join('')}</div>`:'<p class="flow-muted">整组设计（含详情页）完成后，成品会显示在这里。</p>'}
      ${window.renderDetailPage(p.id,f.detail_page,active(f)||f.run?.status==='paused'||isRetired(f))}
      <h3>平台主图</h3>${(f.main_images||[]).length?`<div class="flow-delivery-grid">${f.main_images.map(x=>`<article>${safeMainUrl(x.url)?`<img ${window.designImage(safeMainUrl(x.url))} alt="${esc(x.color||x.seller_sku)}主图">`:'<p>正在准备对应实物主图</p>'}<p>${esc(x.target_name)} · ${esc(x.seller_sku)} · ${esc(x.color)} · ${x.status==='ready'?'已选定':'待准备'}</p></article>`).join('')}</div>`:'<p class="flow-muted">点击“完成设计并同步商品”后，系统会在本项目逐规格选择与检查主图。</p>'}
      <label>设计要求（可选，留空即可直接设计）<textarea data-design-note rows="2" placeholder="例如：更突出使用场景，光线自然">${esc(note)}</textarea></label>
      <details data-section="sources"><summary>完整原图（${p.source_images?.length||0}张，可用一张或多张）</summary><div class="flow-delivery-grid">${(p.source_images||[]).filter(safeUrl).map(url=>`<article><a href="${esc(safeUrl(url))}" target="_blank" rel="noopener"><img ${window.designImage(url)} alt="完整商品原图"></a></article>`).join('')}</div></details>
      <details data-section="masters"><summary>查看多视角产品母稿（${f.masters.length}张）</summary><p class="flow-muted">母稿仅供内部设计参考，不作为商品主图、附图或详情成品提交。每张母稿中的多个视图属于同一款商品。</p>${f.master_repair?.available?`<p>${esc(f.master_repair.reason)}在下方勾选待修母稿，并在“设计要求”说明修改位置。</p>`:''}<div class="flow-delivery-grid">${f.masters.map(x=>{const repair=f.master_repair?.masters?.find(m=>m.url===x.url);return `<div>${imageCard(x,p.id,false,!isRetired(f))}${f.master_repair?.available&&repair?`<label><input type="checkbox" data-master-correction data-master-key="${esc(repair.page_key)}" value="${esc(repair.sha256)}">返修 ${esc(repair.name)}</label>`:''}</div>`}).join('')}</div></details>
      <details data-section="tasks"><summary>详细记录（母稿 / 方案 / 出图）</summary>${window.renderDesignTaskLog(f.tasks,Boolean(f.run?.retry_policy))}</details></div><div class="dialog-foot flow-product-actions">${f.master_repair?.available?btn(p.id,'correct-masters','保留其他母稿并返修所选',pending.has(p.id)):''}${candidateRepair?btn(p.id,'correct-candidates','保留其他图片并返修所选',pending.has(p.id)):''}${actions(p,f)}${f.can_export&&!active(f)&&f.run?.status!=='paused'?`<a class="primary simple-export" href="/api/design-projects/${p.id}/export">下一步 · 下载生产资料</a>`:''}</div>`;
    openDetails.forEach(k=>d.querySelector(`[data-section="${k}"]`)?.setAttribute('open',''));
    d.querySelectorAll('[data-candidate-correction]').forEach(x=>{x.checked=selected.includes(x.value)});
    d.querySelectorAll('[data-master-correction]').forEach(x=>{x.checked=selectedMasters.includes(x.value)});
    d.scrollTop=scroll;
  }
  async function loadGapRecovery(id,f){
    if(f.run?.status==='paused'&&['masters','images','detail'].includes(f.run?.stage)&&!isRetired(f))
      f.detail_recovery=await api(`/api/commerce/design-projects/${id}/flows/${f.run.id}/missing-image-recovery`);
    if(f.run?.status==='paused'&&['masters','plans'].includes(f.run?.stage))
      f.master_repair=await api(`/api/commerce/design-projects/${id}/flows/${f.run.id}/master-repairs`);
  }
  async function view(id){
    opened=id;const requestRevision=++revision,originPage=page;
    const f=await api(`/api/design-projects/${id}/flow`);
    const p=(items.design_projects||[]).find(x=>x.id===id);
    if(opened!==id||revision!==requestRevision||page!==originPage||!p)return;
    await loadGapRecovery(id,f);
    if(opened!==id||revision!==requestRevision||page!==originPage)return;
    p.flow=f;
    drawDetail(p,f);if(!dialog().open)dialog().showModal();
  }
  async function viewCleanup(id){
    opened=null;const requestRevision=++revision,originPage=page;
    const d=dialog(),wasOpen=d.open;d.dataset.signature='';
    const plan=await api(`/api/design-projects/${id}/cleanup`);cleanupPlans.set(id,plan);
    if(revision!==requestRevision||page!==originPage||(wasOpen&&!d.open))return;
    d.innerHTML=`<div class="dialog-head"><h2>清除不合格旧设计</h2><button type="button" class="icon-btn" data-simple-close>×</button></div><div class="flow-dialog-body"><p>永久删除本商品 ${plan.run_count} 轮旧设计对应的 ${plan.files} 个生成图片和预览文件，约 ${(plan.bytes/1048576).toFixed(1)} MB。</p><p>原始Excel、供应商原图、商品规格、价格运费、商品身份及请求编号和回执保留。已有未上架且无图片引用的刊登草稿保持原样。旧设计图片实际删除，不另存图片副本；清理后回到待设计，需重新生成。</p>${plan.error?`<p>${esc(plan.error)}</p>`:''}</div><div class="dialog-foot">${btn(id,'cleanup-confirm','永久清除这些设计')}</div>`;
    if(!d.open)d.showModal();
  }
  window.designOperationStarted=id=>{revision++;pending.add(id)};
  window.refreshOpenDesign=id=>{
    revision++;pending.delete(id);
    const p=(items.design_projects||[]).find(x=>x.id===id);
    if(opened===id&&p?.flow?.tasks){dialog().dataset.signature='';drawDetail(p,p.flow)}
  };
  async function runAction(button){
    const id=Number(button.dataset.simpleId),action=button.dataset.simpleAction;
    if(pending.has(id))return;
    const p=(items.design_projects||[]).find(x=>x.id===id);if(!p)return;
    if(action==='view')return view(id);
    if(action==='cleanup')return viewCleanup(id);
    if(action==='facts'){dialog().close();return window.commerceOpenDesignFacts(id)}
    if(action==='listing')return openListingFlow(id);
    if(action==='materials'){dialog().close();return window.commerceGenerate(id)}
    revision++;pending.add(id);button.disabled=true;
    try{
      const base=`/api/design-projects/${id}/flow`,note=opened===id?($('[data-design-note]')?.value||''):'';
      if(action==='cleanup-confirm'){
        const plan=cleanupPlans.get(id);if(!plan)throw Error('请重新查看待清理范围');
        const result=await api(`/api/design-projects/${id}/cleanup`,{method:'POST',body:JSON.stringify({expected_run_id:plan.expected_run_id,request_id:plan.request_id,scope_sha256:plan.scope_sha256,confirmed_delete:true})});
        notice(`已清理 ${result.files} 个文件，释放约 ${(result.bytes/1048576).toFixed(1)} MB`);
        p.flow=await api(base);
      }
      else if(action==='correct-masters'){
        const candidates=[...dialog().querySelectorAll('[data-master-correction]:checked')].map(x=>({page_key:x.dataset.masterKey,sha256:x.value}));
        if(!candidates.length||note.trim().length<8)throw Error('请勾选待修母稿，并在设计要求中说明具体事实和修改位置');
        await api(`/api/commerce/design-projects/${id}/flows/${p.flow.run.id}/master-repairs`,{method:'POST',body:JSON.stringify({candidates,instruction:note.trim()})});
        p.flow=await api(base);
      }
      else if(action==='correct-candidates'){
        const candidates=[...dialog().querySelectorAll('[data-candidate-correction]:checked')].map(x=>({page_key:x.dataset.pageKey,sha256:x.value}));
        if(!candidates.length||note.trim().length<8)throw Error('请选择本轮错误图片，并在设计要求中说明已核实的事实及修改位置');
        await api(`/api/commerce/design-projects/${id}/flows/${p.flow.run.id}/image-repairs`,{method:'POST',body:JSON.stringify({candidates,instruction:note.trim()})});
        p.flow=await api(base);
      }
      else if(action==='replace-missing-detail'){
        const gap=p.flow.detail_recovery;
        if(!gap?.available)throw Error('缺图状态已变化，请刷新后核对');
        await api(`/api/commerce/design-projects/${id}/flows/${p.flow.run.id}/missing-image-recovery`,{method:'POST',body:JSON.stringify({detail_run_id:gap.detail_run_id,task_ids:gap.task_ids,confirmed_new_request:true})});
        p.flow=await api(base);
      }
      else if(action==='pause'||action==='resume')p.flow=await api(`${base}/${p.flow.run.id}/${action}`,{method:'POST'});
      else p.flow=await api(base,{method:'POST',body:JSON.stringify({action:action==='design'&&outputCount(p.flow)&&hasDetail(p.flow)?'redesign':action,note,page_key:button.dataset.pageKey||'',reuse_matching_masters:true})});
      render();await view(id);
    }catch(e){notice(e.message,true)}finally{pending.delete(id);button.disabled=false;render();if(opened===id&&p.flow?.tasks){dialog().dataset.signature='';drawDetail(p,p.flow)}}
  }
  let listingPoll=null, listingSession=0;
  async function openListingFlow(id){
    const session=++listingSession,originPage=page;
    if(listingPoll){clearInterval(listingPoll);listingPoll=null}
    const [config,existing]=await Promise.all([
      api('/api/commerce/target-bindings'),
      api(`/api/commerce/design-projects/${id}/listing-workflows`),
    ]);
    const accountIds=new Set((config.accounts||[]).filter(x=>x.enabled&&x.platform==='亚马逊').map(x=>x.id));
    const choices=(config.targets||[]).filter(x=>x.environment==='production'&&x.enabled&&x.runtime_available&&accountIds.has(x.account_id));
    const configured=(config.targets||[]).filter(x=>x.environment==='production'&&accountIds.has(x.account_id));
    const unavailable=configured.length?'正式店铺与站点已配置但未启用，请在平台同步的“选择地区”中启用。':'尚未关联可用的正式店铺与站点，请在平台同步中启用账号并选择地区。';
    let d=document.querySelector('#oneClickListingDialog');
    if(!d){d=document.createElement('dialog');d.id='oneClickListingDialog';d.className='flow-dialog';document.body.append(d)}
    if(session!==listingSession||page!==originPage)return;
    d.innerHTML=`<div class="dialog-head"><h2>同步商品到亚马逊</h2><button type="button" class="icon-btn" data-listing-close>×</button></div><div class="flow-dialog-body"><p>按已配置并启用的全部店铺、站点自动处理：完成设计 → 生成各站点标题、文案和商品资料 → 校验 → 同步并回读结果。已有商品沿用原卖家 SKU 更新图片与资料；尚未建立的商品首次发布。各站点分别记录进度，买家是否能购买另行核实。</p>${choices.length?`<p>本次 ${new Set(choices.map(t=>t.account_id)).size} 个店铺 · ${choices.length} 个店铺站点组合</p>`:`<p>${esc(unavailable)}</p>`}<div data-listing-progress role="status"></div></div><div class="dialog-foot"><button type="button" data-listing-materials>查看上架资料</button><button type="button" data-listing-resume hidden>补齐后自动继续</button><button type="button" data-listing-retry hidden>重试启动</button></div>`;
    if(!d.open)d.showModal();
    const current=()=>session===listingSession&&d.open;
    const stop=()=>{if(listingPoll){clearInterval(listingPoll);listingPoll=null}};
    const close=()=>{d.close();stop()};
    d.querySelector('[data-listing-close]').onclick=close;
    d.querySelector('[data-listing-materials]').onclick=()=>{close();window.commerceOpen?.('commerce_listings')};
    let states=existing,reading=false;
    const latest=()=>choices.map(t=>({target:t,state:states.filter(w=>w.target_id===t.id).sort((a,b)=>b.id-a.id)[0]}));
    const savedContent=w=>{
      const saved=w?.listing_content;if(!saved)return '';
      const c=saved.content||{};
      return `<details open class="flow-listing-copy"><summary>已保存上架内容 · v${esc(saved.version)}</summary><h4>商品标题</h4><p>${esc(c.title||'')}</p><h4>卖点</h4><ul>${(c.bullets||[]).map(text=>`<li>${esc(text)}</li>`).join('')}</ul><h4>商品描述</h4><p style="white-space:pre-wrap">${esc(c.description||'')}</p><h4>搜索词</h4><p>${esc(c.search_terms||'')}</p>${(saved.sku_titles||[]).length?`<h4>规格标题</h4><ul>${saved.sku_titles.map(row=>`<li>${esc(row.seller_sku)}：${esc(row.title)}</li>`).join('')}</ul>`:''}</details>`;
    };
    const draw=()=>{
      if(!current())return;
      const names={running:'处理中',waiting:'处理中',needs_action:'待补充',buyable:'接口显示可售，买家购买待核实',submitted:'已提交',sample_only:'隔离测试完成'};
      d.querySelector('[data-listing-progress]').innerHTML=latest().map(({target:t,state:w})=>`<section class="flow-listing-progress"><h3>${esc(t.account_name||'亚马逊')} · ${esc(t.name||t.marketplace_id)}</h3><p>${w?`${esc(names[w.status]||w.status)} · ${esc(w.message||'')}${w.listing_id?' · 上架资料 #'+w.listing_id:''}`:'正在启动自动流程…'}</p>${savedContent(w)}</section>`).join('');
      d.querySelector('[data-listing-resume]').hidden=!latest().some(x=>x.state?.status==='needs_action');
    };
    const refresh=async()=>{
      if(!current()||reading)return;
      reading=true;
      try{states=await api(`/api/commerce/design-projects/${id}/listing-workflows`);draw();if(!latest().some(x=>['running','waiting'].includes(x.state?.status)))stop()}
      catch(e){notice(e.message,true)}finally{reading=false}
    };
    const follow=()=>{stop();if(current())listingPoll=setInterval(refresh,4000)};
    const requestId=crypto.randomUUID();
    const start=async()=>{
      const retry=d.querySelector('[data-listing-retry]');retry.hidden=true;
      try{
        const batch=await api(`/api/commerce/design-projects/${id}/listing-workflow-batches`,{method:'POST',body:JSON.stringify({request_id:requestId})});
        if(!current())return;
        states=batch.workflows;draw();follow();
      }catch(e){if(current()){retry.hidden=false;d.querySelector('[data-listing-progress]').textContent=e.message}}
    };
    d.querySelector('[data-listing-retry]').onclick=start;
    d.querySelector('[data-listing-resume]').onclick=async()=>{
      const button=d.querySelector('[data-listing-resume]');button.disabled=true;
      try{
        const results=await Promise.allSettled(latest().filter(x=>x.state?.status==='needs_action').map(x=>api(`/api/commerce/listing-workflows/${x.state.id}/resume`,{method:'POST'})));
        results.filter(x=>x.status==='rejected').forEach(x=>notice(x.reason.message,true));
        await refresh();follow();
      }finally{button.disabled=false}
    };
    draw();
    // Keep the current blocked workflow available for its explicit resume button.
    // Opening the dialog must not replace it with a second automatic request.
    if(choices.length&&!latest().every(x=>x.state?.status==='needs_action'))await start();
  }
  async function focusExistingListings(){
    const overview=await api('/api/commerce/overview');
    const keepProducts=new Set((overview.listing||[]).filter(x=>x.environment==='production').map(x=>x.product_id));
    const products=(overview.product||[]).filter(x=>keepProducts.has(x.id));
    if(!products.length)throw Error('尚无正式上架资料，请先选择要处理的商品');
    const keepProjects=new Set(products.map(x=>x.project_id).filter(Boolean));
    const projects=await api('/api/design_projects?summary=1');
    const others=projects.filter(p=>!keepProjects.has(p.id)&&working(p.flow));
    const d=dialog();
    d.innerHTML=`<div class="dialog-head"><h2>集中处理 ${products.length} 款商品</h2><button type="button" class="icon-btn" data-simple-close>×</button></div><div class="flow-dialog-body"><p>继续处理已有正式上架资料的商品：</p><ul>${products.map(x=>`<li>${esc(x.name)} · 商品 #${x.id}</li>`).join('')}</ul><p>暂停其余 ${others.length} 款正在进行的设计及后续自动流程。原图、已生成图片和历史记录保留；已经发出的请求返回后保存结果。</p><p data-focus-progress role="status"></p></div><div class="dialog-foot"><button type="button" data-focus-confirm>暂停其他商品，继续这 ${products.length} 款</button></div>`;
    if(!d.open)d.showModal();
    d.querySelector('[data-focus-confirm]').onclick=async event=>{
      event.currentTarget.disabled=true;
      others.forEach(p=>pending.add(p.id));
      const failures=[];let paused=0;
      try{
        for(const p of others){
          try{
            const fresh=await api(`/api/design-projects/${p.id}/flow`);
            const run=fresh.run;
            if(run&&active(fresh))await api(`/api/design-projects/${p.id}/flow/${run.id}/pause`,{method:'POST'});
            if(fresh.detail_run&&!fresh.detail_run.parent_run_id&&['queued','running','retry_wait'].includes(fresh.detail_run.status))await api(`/api/design-projects/${p.id}/flow/${fresh.detail_run.id}/pause`,{method:'POST'});
            paused++;
          }catch(error){failures.push(`${p.product_name}：${error.message}`)}
          d.querySelector('[data-focus-progress]').textContent=`已处理 ${paused}/${others.length} 款${failures.length?`；${failures.length} 款需核对`:''}`;
        }
        items.design_projects=await api('/api/design_projects?summary=1');revision++;render();
        const remaining=items.design_projects.filter(p=>!keepProjects.has(p.id)&&working(p.flow));
        d.querySelector('[data-focus-progress]').textContent=`已暂停 ${paused} 款；保留 ${products.length} 款继续处理。${remaining.length?`仍有 ${remaining.length} 款活动任务，请核对。`:''}${failures.length?failures.join('；'):''}`;
      }finally{others.forEach(p=>pending.delete(p.id));}
    };
  }
  document.addEventListener('click',e=>{if(e.target.closest('[data-focus-listings]')){focusExistingListings().catch(e=>notice(e.message,true));return}if(e.target.closest('[data-simple-close]')){dialog().close();return}const b=e.target.closest('[data-simple-action]');if(b)runAction(b).catch(e=>notice(e.message,true))});
  setInterval(async()=>{
    if(page!=='design_projects'||document.hidden||refreshing||pending.size)return;
    const busy=(items.design_projects||[]).some(p=>working(p.flow));
    if(Date.now()-lastPoll<(busy?3000:30000))return;
    refreshing=true;lastPoll=Date.now();const requestRevision=revision;
    try{
      const list=await api('/api/design_projects?summary=1');
      if(revision!==requestRevision)return;
      items.design_projects=list;render();
      const p=list.find(x=>x.id===opened);
      // Only an open product needs full prompts and generation records.
      if(p){
        const f=await api(`/api/design-projects/${p.id}/flow`);
        await loadGapRecovery(p.id,f);
        if(opened===p.id&&revision===requestRevision){
          p.flow=f;
          // Do not replace a textarea while the user is writing a design request.
          if(document.activeElement?.tagName!=='TEXTAREA')drawDetail(p,f);
        }
      }
    }catch(e){notice(e.message,true)}finally{refreshing=false}
  },3000);
  render();
})();
