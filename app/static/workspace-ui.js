/* Human-oriented views over the existing records. No writes, model calls or invented KPIs. */
(() => {
  const paths={home:'M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z',box:'m3 7 9-4 9 4-9 4Zm0 0v10l9 4 9-4V7M12 11v10',spark:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z',arrow:'M5 12h14m-6-6 6 6-6 6',upload:'M12 16V3m-5 5 5-5 5 5M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5',store:'M3 10h18l-2-7H5Zm1 0v11h16V10M9 21v-7h6v7',order:'M6 3h12v18l-3-2-3 2-3-2-3 2Zm3 5h6m-6 4h6',chat:'M21 11a8 8 0 0 1-8 8H6l-4 3V11a9 9 0 0 1 19 0ZM7 9h10M7 13h7',chart:'M4 3v18h17M8 17v-5m5 5V7m5 10V4',settings:'M4 7h16M4 17h16M8 4v6m8 4v6',clock:'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',check:'m5 12 4 4L19 6',alert:'M12 8v5m0 4h.01M10 3 1 19h22L14 3Z',layers:'m3 7 9-4 9 4-9 4Zm0 5 9 4 9-4M3 17l9 4 9-4',link:'m10 13 4-4m-6 7-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2 1 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',undo:'M3 10h11a6 6 0 0 1 0 12M3 10l6-6m-6 6 6 6',search:'m21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0'};
  const icon=(name='box')=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.box}"/></svg>`;
  function derive(data={},projects=[]) {
    const products=(data.product||[]).filter(p=>p.active!==false),skus=(data.sku||[]).filter(s=>s.active!==false&&products.some(p=>p.id===s.product_id));
    const currentProjects=projects.filter(p=>products.some(x=>x.project_id===p.id));
    const designed=currentProjects.filter(p=>p.flow?.run?.status==='completed');
    const listings=(data.listing||[]).filter(l=>products.some(p=>p.id===l.product_id));
    const materials=new Set(listings.filter(l=>l.material_status==='ready').map(l=>l.product_id));
    const submitted=new Set(listings.filter(l=>l.published_version||Object.keys(l.published_skus||{}).length).map(l=>l.product_id));
    const busy=(data.jobs||[]).filter(j=>['queued','running','retry_wait'].includes(j.status));
    const waiting=(data.jobs||[]).filter(j=>{
      if(j.status==='unknown')return true; // Unknown submissions still need reconciliation even after an edit.
      if(j.status!=='paused')return false;
      const listing=listings.find(l=>l.id===j.payload?.listing_id);
      if(listing&&['detail_submit','detail_check'].includes(j.kind)&&j.payload?.publication_id&&listing.detail_publication?.id){
        const current=listing.detail_publication;
        if(Number(j.payload.publication_id)!==Number(current.id)&&!(current.children||[]).some(child=>Number(child.id)===Number(j.payload.publication_id)))return false;
      }
      return !listing||j.payload?.version==null||Number(j.payload.version)===Number(listing.draft_version);
    });
    const pendingOrders=(data.order||[]).filter(o=>!['shipped','cancelled'].includes(o.status));
    const notices=(data.notice||[]).filter(n=>!n.read);
    return {products,skus,projects:currentProjects,designed,listings,materials,submitted,busy,waiting,pendingOrders,notices};
  }
  function create({E,button,badge,getData,getProjects,jobSummary,jobName}) {
    const nav=(destination,label,name='arrow',extra='')=>`<button type="button" class="ws-link${extra==='primary'?' primary':''}" data-workspace-go="${E(destination)}">${E(label)}${icon(name)}</button>`;
    const metric=(name,value,caption,ico,destination)=>`<button type="button" class="ws-metric" ${destination==='commerce_home'?'data-c-action="job-history" data-c-id="0"':`data-workspace-go="${destination}"`}><span class="ws-metric-icon">${icon(ico)}</span><span><span class="ws-metric-label">${E(name)}</span><strong>${E(value)}</strong><small>${E(caption)}</small></span>${icon('arrow')}</button>`;
    function home(query='') {
      const data=getData()||{},s=derive(data,getProjects?.()||[]);
      const current=s.listings.find(l=>!l.published_version)||s.listings[0];
      const project=s.projects.find(p=>['running','queued','retry_wait','paused'].includes(p.flow?.run?.status))||s.designed[0]||s.projects[0];
      const product=current?s.products.find(p=>p.id===current.product_id):s.products.find(p=>p.project_id===project?.id);
      const target=(data.target||[]).find(t=>t.id===current?.target_id);
      const progress=current?.publication_progress,live=progress?.live;
      const currentError=progress?.local?progress.local.error:progress?.error||progress?.validation?.error;
      const headline=current?(live?live.label:current.published_version?'查看亚马逊商品状态':current.material_status==='ready'?'资料就绪，等待发布':'补齐资料，继续推进'):(project?'继续当前商品设计':'从一份货盘，开始下一步');
      const description=current?`${product?.name||current.name} · ${target?.name||'对应店铺'} · ${current.variant_structure?.label||((current.draft?.skus?.length||0)+' 个规格')}`:project?`${project.product_name} · ${project.flow?.run?.stage_label||'查看设计进度'}`:'先导入供应商资料，在商品库中核对实际规格，再开始设计。';
      const mainAction=current?button('listing-edit',current.id,'打开上架资料','class="primary"'):project?nav('design_projects','进入设计工坊','arrow','primary'):nav('supplier_imports','导入第一份货盘','upload','primary');
      const coverId=current?.draft?.images?.find(x=>x.display_role==='sales_cover')?.asset_id||current?.draft?.images?.[0]?.asset_id;
      const cover=(data.media||[]).find(m=>m.id===coverId)?.url;
      const preview=cover?`<img ${window.designImage(cover,640)} alt="${E(product?.name||current?.name||'当前商品')}销售设计">`:`<div class="ws-orbit" aria-hidden="true"><span>${icon('box')}</span><i>${icon('spark')}</i><i>${icon('store')}</i><i>${icon('check')}</i></div>`;
      const steps=[['供货归档',s.products.length,'个供货分组','commerce_products','box'],['设计成品',s.designed.length,'款已完成','design_projects','spark'],['上架资料',s.materials.size,'款资料齐备','commerce_listings','layers'],['平台已提交',s.submitted.size,'款有提交记录','commerce_listings','store']];
      const attention=[];
      if(currentError)attention.push({title:'当前上架资料需要处理',text:currentError,action:button('listing-edit',current.id,'查看并处理')});
      if(s.pendingOrders.length)attention.push({title:`${s.pendingOrders.length} 笔订单待交付`,text:'查看订单的备货与物流进度。',action:nav('commerce_orders','处理订单')});
      if(s.waiting.length)attention.push({title:`${s.waiting.length} 个任务等待处理`,text:'打开任务记录查看暂停原因或未知结果。',action:button('job-details',s.waiting[0].id,'查看任务')});
      for(const n of s.notices.filter(n=>n.support_thread_id).slice(0,3))attention.push({title:n.title,text:n.message||n.body||'查看客户消息与对应处理记录。',action:button('support-detail',n.support_thread_id,'查看消息')});
      const queue=attention.filter(a=>!query||`${a.title} ${a.text}`.toLowerCase().includes(query));
      const tiles=s.projects.filter(p=>!query||`${p.product_name} ${p.supplier_name}`.toLowerCase().includes(query)).slice(0,3).map(p=>{
        const f=p.flow||{},complete=f.run?.status==='completed',status=({paused:'已暂停',running:'正在生成',queued:'等待开始',retry_wait:'等待重试',completed:'设计已完成'})[f.run?.status]||'尚未开始';
        const image=f.preview?.find(x=>x.url)?.url||p.source_images?.[0];
        return `<article class="ws-project-tile">${image?`<img ${window.designImage(image,320)} alt="${E(p.product_name)}">`:`<span class="ws-project-placeholder">${icon('box')}</span>`}<div><span class="ws-label">${E(status)}</span><h4>${E(p.product_name)}</h4><p>${E(p.supplier_name||'供应商未填写')} · ${complete?(f.output_count??f.outputs?.length??0)+' 张图库 · '+(f.detail_count??f.detail_page?.current?.sections?.length??0)+(['three-stage-eight-plans-v1','three-stage-eight-plans-v2'].includes(f.run?.design_contract)?' 张完整详情图':' 项详情内容'):'按实际规格独立设计'}</p></div>${nav('design_projects','打开')}</article>`;
      }).join('');
      const activity=(data.jobs||[]).filter(j=>!query||`${jobName?.(j.kind)||j.kind} ${jobSummary?.(j)||''}`.toLowerCase().includes(query)).slice(0,4);
      return `<div class="ws-dashboard"><div class="ws-dashboard-context"><span><i></i>${data.settings?.live_writes_enabled?'正式平台写入已启用':'正式平台写入已关闭'}<small>${data.settings?.live_writes_enabled?'发布动作仍按对应商品流程执行':'可整理资料与查看结果'}</small></span>${window.workspaceRole==='operator'?'':button('settings',0,'经营设置')}</div>
        <section class="ws-focus ws-bento"><div class="ws-focus-copy"><span class="ws-overline">当前进展</span><h2>${E(headline)}</h2><p>${E(description)}</p><div class="ws-focus-actions">${mainAction}${nav('commerce_products','查看全部商品')}</div></div><div class="ws-focus-art">${preview}<span class="ws-art-caption">${cover?'当前商品 · 销售设计预览':'从商品到生意，步步清楚'}</span></div><p class="ws-focus-note">${currentError?'有待处理事项，查看提醒':progress?.local?E(progress.local.label):progress?.validation?.label?E(progress.validation.label):'每一步基于实际商品资料与任务记录'}</p></section>
        <section class="ws-attention ws-bento"><div class="ws-section-head"><div><span class="ws-overline">NEEDS ATTENTION</span><h3>需要你关注</h3></div><span class="ws-counter">${queue.length}</span></div>${queue.length?`<div class="ws-attention-list">${queue.slice(0,4).map(a=>`<article><span class="ws-task-dot"></span><div><h4>${E(a.title)}</h4><p>${E(a.text)}</p><div>${a.action}</div></div></article>`).join('')}</div>`:`<div class="ws-calm">${icon('check')}<strong>${query?'没有匹配的待办':'当前资料没有新的处理项'}</strong><p>${query?'换个关键词，或清空搜索。':'在上架工作台同步平台状态，按实际结果处理。'}</p></div>`}${s.notices.length?`<div class="ws-notice-history">${button('notice-history',0,`查看 ${s.notices.length} 条未读历史通知`)}<span>历史通知保留原始发生状态</span></div>`:''}</section>
        <section class="ws-metrics" aria-label="经营概况">${metric('供货分组与销售选项',s.products.length,`${s.skus.length} 个实际销售选项`,'box','commerce_products')}${metric('已完成设计',s.designed.length,`${s.projects.length} 款进入设计工作台`,'spark','design_projects')}${metric('待处理订单',s.pendingOrders.length,'按店铺跟进交付','order','commerce_orders')}${metric('正在执行',s.busy.length,'仅统计当前等待或执行的任务','clock','commerce_home')}</section>
        <section class="ws-pipeline ws-bento"><div class="ws-section-head"><div><span class="ws-overline">YOUR PRODUCT JOURNEY</span><h3>商品走到哪一步了</h3></div><span class="ws-muted">同一商品可在多个阶段留下记录</span></div><div class="ws-pipeline-steps">${steps.map(([title,count,unit,destination,ico],i)=>`<button type="button" data-workspace-go="${destination}" class="ws-pipeline-step"><span class="ws-step-number">0${i+1}</span><span class="ws-step-icon">${icon(ico)}</span><span><b>${E(title)}</b><span><strong>${count}</strong> ${unit}</span></span>${icon('arrow')}</button>`).join('')}</div><p class="ws-pipeline-foot">已提交数量依据实际提交记录统计；在售与详情发布状态在上架工作台按平台结果显示。</p></section>
        <section class="ws-in-progress ws-bento"><div class="ws-section-head"><div><span class="ws-overline">IN YOUR WORKSPACE</span><h3>正在推进的商品</h3></div>${nav('design_projects','全部设计')}</div>${tiles||`<div class="ws-inline-empty">${icon('spark')}<div><strong>${query?'没有匹配的设计项目':'商品已归档，等待选择设计'}</strong><p>从商品库选择实际规格，按你的节奏开始。</p></div>${nav('commerce_products','打开商品库')}</div>`}</section>
        <section class="ws-activity ws-bento"><div class="ws-section-head"><div><span class="ws-overline">ACTIVITY</span><h3>最近动态</h3></div>${button('job-history',0,'全部记录')}</div>${activity.length?`<ol class="ws-timeline">${activity.map(j=>`<li><span class="ws-timeline-dot ${E(j.status)}"></span><div><b>${E(j.kind==='submit'&&j.payload?.preview?'平台资料检查':jobName?.(j.kind)||j.kind)}</b><p>${E(jobSummary?.(j)||'查看任务记录了解进度')}</p></div><div>${badge(j.status)}${button('job-details',j.id,'详情')}</div></li>`).join('')}</ol>`:'<div class="ws-inline-empty">暂无任务记录。开始整理商品后，进展会显示在这里。</div>'}</section>
      </div>`;
    }
    return {home};
  }
  window.WorkspaceUI={icon,derive,create};
})();
