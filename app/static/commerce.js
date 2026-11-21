/* Commerce UI: persistent server jobs, ordinary business actions, no review gate. */
(() => {
  const titles={commerce_home:'运营指挥台',commerce_products:'商品库',commerce_listings:'上架工作台',commerce_orders:'订单与发货',commerce_aftersales:'售后处理',commerce_support:'客户消息',commerce_operations:'运营计划',commerce_finance:'财务与报表'};
  let data=null,loading=null,reloadRequested=false,renderedPage="",renderedContent="",lastSupportRefresh=0,lastRefreshAt=0,pollFailures=0,checkingRevision=false;
  let overviewError='';
  const baseRender=render,baseEditor=openEditor;
  const escape=esc, E=escape;
  const labels={mock:'模拟',sandbox:'官方沙盒',production:'正式',queued:'等待处理',running:'处理中',retry_wait:'等待重试',paused:'已暂停',unknown:'待核实',completed:'已完成',ready:'待处理',preparing:'备货生产中',ready_to_ship:'待发货',shipped:'已发货',partially_shipped:'部分发货',needs_attention:'待补充',draft:'草稿',failed:'失败',confirmed:'已回填',started:'生产中',stop_requested:'待停产',cancelled:'已取消',not_submitted:'未上架',processing:'平台处理中',buyable:'接口可售',mock_buyable:'模拟可售',not_buyable:'暂不可售',partial_buyable:'接口部分可售',partial_error:'部分失败',open:'待办理',resolved:'已处理',ordered:'待到货',received:'已入库',planned:'待办理',active:'进行中',saved:'已保存',needs_fields:'待补充',unreconciled:'待对账',reconciled:'已核对',available:'可下载',pause_pending:'暂停处理中',identity_conflict:'商品标识冲突',platform_error:'平台返回错误',search_suppressed:'搜索结果受限'};
  const label=x=>labels[x]||x||'—';
  Object.assign(labels,{mock_submitted:'模拟已接收',mock_not_buyable:'模拟不可售',sandbox_sample:'沙箱固定样例'});
  const button=(action,id,text,extra='')=>`<button type="button" data-c-action="${action}" data-c-id="${id}" ${extra}>${E(text)}</button>`;
  const badge=x=>`<span class="badge ${['failed','needs_attention','partial_error','unknown'].includes(x)?'off':''}">${E(label(x))}</span>`;
  const find=(kind,id)=>(data?.[kind]||[]).find(x=>x.id===Number(id));
  const languageOptions=()=>[['','按国家地区默认'],...(data?.languages||[]).map(x=>[x.value,x.label])];
  const targetLanguageValue=t=>t.language_mode==='marketplace'||(!t.language_mode&&(!t.language||t.language==='en'))?'':t.language==='zh'?'zh_CN':t.language||'';
  const amount=(v,c)=>(v===null||v===undefined?'待配置':`${v} ${c||'币种待确认'}`);
  const input=(name,title,value='',type='text',extra='')=>`<label class="c-field"><span>${E(title)}</span><input name="${name}" type="${type}" value="${E(value??'')}" ${extra}></label>`;
  const textarea=(name,title,value='')=>`<label class="c-field c-wide"><span>${E(title)}</span><textarea name="${name}" rows="4">${E(value??'')}</textarea></label>`;
  const select=(name,title,value,options)=>`<label class="c-field"><span>${E(title)}</span><select name="${name}">${options.map(([v,t])=>`<option value="${E(v)}" ${String(value)===String(v)?'selected':''}>${E(t)}</option>`).join('')}</select></label>`;
  const table=(head,rows)=>rows.length?`<div class="c-scroll"><table><thead><tr>${head.map(x=>`<th>${E(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(x=>`<td>${x}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<div class="empty">暂无相关记录。完成对应业务后将在这里显示。</div>';
  const options=(kind,field='name')=>(data?.[kind]||[]).map(x=>[x.id,x[field]||x.external_id||String(x.id)]);
  async function call(path,body,method='POST',options={}){return api('/api/commerce'+path,{...options,method,body:JSON.stringify(body??{})})}
  function reload(){
    // Inbox access must not wait for image/catalog readiness in the overview.
    if(page==='commerce_support')return supportUI.refresh(false).then(()=>{lastSupportRefresh=Date.now();if(page==='commerce_support')render();});
    // A refresh requested after a write must wait for a snapshot taken after it.
    if(loading){reloadRequested=true;return loading;}
    loading=(async()=>{
      try{
        do{
          reloadRequested=false;
          data=await api('/api/commerce/overview?summary=1');
          overviewError='';
          pollFailures=0;
          if(page==='commerce_support'){await supportUI.refresh(false);lastSupportRefresh=Date.now();}
          if(page.startsWith('commerce_'))render();
        }while(reloadRequested);
      }catch(error){
        pollFailures++;
        overviewError=error.message;
        if(!data&&page.startsWith('commerce_')&&page!=='commerce_support')render();
        throw error;
      }finally{loading=null;lastRefreshAt=Date.now();}
    })();
    return loading;
  }
  function openPage(value){
    page=value;$('#search').value='';render();
    if(page.startsWith('commerce_')&&!loading&&(data||overviewError||page==='commerce_support'))reload().catch(error=>notice(error.message,true));
  }
  for(const [key,title] of Object.entries(titles)){
    if($('#nav').querySelector(`[data-page="${key}"]`))continue;
    const b=document.createElement('button');b.className='nav-item';b.dataset.page=key;b.innerHTML=`<span>${E(title[0])}</span><span class="nav-label">${E(title)}</span>`;$('#nav').appendChild(b);
  }
  const dialog=document.createElement('dialog');dialog.id='commerce-dialog';dialog.className='c-dialog';document.body.appendChild(dialog);
  let dialogRequest=0;
  function guardDialog(){
    const request=++dialogRequest,form=dialog.querySelector('form'),open=dialog.open,originPage=page;
    return ()=>request===dialogRequest&&dialog.querySelector('form')===form&&dialog.open===open&&page===originPage;
  }
  function show(title,body,onSave=null,saveLabel='保存'){
    dialogRequest++;
    dialog.innerHTML=`<form class="c-form"><div class="dialog-head"><div><p class="eyebrow">COMMERCE OPERATIONS</p><h2>${E(title)}</h2></div><button type="button" data-c-close class="icon-btn">×</button></div><div class="c-body">${body}</div><div class="dialog-foot"><button type="button" data-c-close>关闭</button>${onSave?`<button class="primary" type="submit">${E(saveLabel)}</button>`:''}</div></form>`;
    dialog.querySelectorAll('[data-c-close]').forEach(x=>x.onclick=()=>dialog.close());
    const form=dialog.querySelector('form');let saving=false;
    form.onsubmit=async event=>{
      event.preventDefault();if(!onSave||saving)return;
      saving=true;const b=event.submitter||form.querySelector('[type="submit"]');if(b){b.disabled=true;b.setAttribute?.('aria-busy','true');}
      const current=()=>dialog.querySelector('form')===form;
      try{
        const message=await onSave(new FormData(form));
        if(current())dialog.close();
        await reload();
        if(message?.afterSave){if(current())await message.afterSave();}
        else notice(typeof message==='string'?message:'操作已保存');
      }catch(error){
        notice(error.message,true);
        if(current()){
          let box=form.querySelector('.c-error');
          if(!box){box=document.createElement('p');box.className='c-error';form.querySelector('.c-body').prepend(box);}
          box.textContent=error.message;
        }
      }finally{saving=false;if(b){b.disabled=false;b.removeAttribute?.('aria-busy');}}
    };
    window.WorkspaceShell?.sectionArchive(dialog,title);
    if(!dialog.open)dialog.showModal();
  }
  function actions(...values){return `<div class="actions">${values.join('')}</div>`}
  const targetUI=window.createTargetSelection({E,select,show,call,api,dialog,guardDialog,notice,getData:()=>data,reload});
  const orderUI=window.createOrderManagement({E,input,select,table,button,badge,show,call,api,dialog,guardDialog,getData:()=>data,targetUI,render:()=>render()});
  const aftersaleUI=window.createAftersaleManagement({E,input,textarea,select,table,button,show,call,api,dialog,guardDialog,reload,getData:()=>data,targetUI,render:()=>render()});
  const supportUI=window.createSupportCenter({E,input,textarea,select,table,button,show,call,api,dialog,notice,render:()=>render(),getPlans:()=>items.plans||[],getLanguages:()=>data?.languages||[]});
  const shippingUI=window.createShippingTemplates({E,select,input,show,call,api,dialog,guardDialog});
  const categoryUI=window.createCategoryMaintenance({E,input,textarea,select,table,button,actions,show,call,api,dialog,notice,getData:()=>data});
  const archiveUI=window.CommerceProductArchive.create({E,input,textarea,select,table,button,show,call,api,dialog,notice,getData:()=>data,reload,openPage});
  window.commerceEditSku=async id=>{const current=guardDialog();await reload();if(current())await editCatalog('sku',id);};
  window.commerceGtinEvidence=async accountId=>{const current=guardDialog();try{await reload();if(!current())return;const target=data.target.find(t=>t.environment==='production'&&t.enabled&&(!accountId||t.account_id===Number(accountId)));if(!target)throw Error('此账号暂无已启用的正式店铺与站点');await archiveUI.evidence(null,target.id);}catch(error){notice(error.message,true);}};
  const listingUI=window.createListingOverview({E,button,badge,find,getData:()=>data,platformLinks});
  const bilingualUI=window.createListingBilingual({E,call,api});
  const listingFilters=window.createListingFilters({E,label,find,getData:()=>data,onApply:()=>render(),onReset:()=>{$('#search').value='';render()}});
  const workspaceUI=window.WorkspaceUI?.create({E,button,badge,getData:()=>data,getProjects:()=>items.design_projects||[],jobSummary,jobName:kind=>jobNames[kind]||kind});
  window.workspaceSearchProducts=query=>(data?.product||[]).filter(p=>`${p.name} ${(data.sku||[]).filter(s=>s.product_id===p.id).map(s=>`${s.sku} ${s.color} ${s.size}`).join(' ')}`.toLowerCase().includes(query)).slice(0,6).map(p=>({name:p.name,subtitle:`${(data.sku||[]).filter(s=>s.product_id===p.id&&s.active!==false).length} 个规格 · 商品档案`}));
  document.addEventListener('click',event=>{const control=event.target.closest('[data-workspace-product-view]');if(control&&archiveUI.setView){archiveUI.setView(control.dataset.workspaceProductView);render();}});
  render=function(){
    document.body.classList.toggle('commerce-page',page.startsWith('commerce_'));
    document.body.classList.toggle('listing-page',page==='commerce_listings');
    listingFilters.visibility(page==='commerce_listings');
    if(!page.startsWith('commerce_')){renderedPage='';$('#add').hidden=false;return baseRender()}
    document.body.classList.remove('design-journey-page');document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
    $('#title').textContent=titles[page]||'电商经营';$('#subtitle').textContent='商品、订单与经营记录互相关联，平台状态以实际返回为准。';$('#listTitle').textContent=titles[page];$('#panelHint').textContent='按已配置的平台、账号和地区处理业务';$('#add').hidden=false;$('#addLabel').textContent=page==='commerce_categories'?'新增本地分类':page==='commerce_home'?'刷新工作台':page==='commerce_products'?'智能导入 Excel':page==='commerce_listings'?'配置上架目标':page==='commerce_aftersales'?'刷新售后':page==='commerce_operations'?'创建活动': '刷新数据';
    $('#search').placeholder=page==='commerce_support'?'搜索主题、消息、订单或处理人':'搜索名称或配置内容';
    if(page==='commerce_support'){
      const html=supportUI.listing($('#search').value.trim().toLowerCase());
      $('#stats').innerHTML=supportUI.stats();$('#subtitle').textContent='通过官方API读取亚马逊支持Case，统一查看原始沟通和本地处理记录。';$('#panelHint').textContent='官方Case API · 买家收件箱读取暂不支持 · 北京时间';$('#addLabel').textContent='亚马逊API连接';
      if(renderedPage!==page||renderedContent!==html){$('#content').innerHTML=html;renderedPage=page;renderedContent=html;}
      return;
    }
    if(!data){
      $('#stats').replaceChildren();$('#add').hidden=true;
      if(overviewError){
        $('#content').innerHTML='<div class="empty" role="status"><p>'+E(overviewError)+'</p><button type="button" class="secondary" data-retry-overview>重新读取经营数据</button></div>';
        $('#content [data-retry-overview]').onclick=event=>{event.currentTarget.disabled=true;reload().catch(()=>{});};
      }else{
        $('#content').innerHTML='<div class="empty">正在读取经营数据…</div>';
        if(!loading)reload().catch(()=>{});
      }
      return;
    }
    const pending=data.jobs.filter(x=>['queued','running','retry_wait'].includes(x.status)).length;
    $('#stats').innerHTML=[['平台商品',data.listing.length],['待处理订单',data.order.filter(x=>!['shipped','cancelled'].includes(x.status)).length],['后台任务',pending]].map(([t,n])=>`<div class="stat"><small>${E(t)}</small><b>${n}</b></div>`).join('');
    const q=$('#search').value.trim().toLowerCase();const rows=kind=>q?(data[kind]||[]).filter(x=>JSON.stringify(x).toLowerCase().includes(q)):(data[kind]||[]);
    let html='';
    if(page==='commerce_categories'){
      $('#subtitle').textContent='维护本地分类与各平台站点映射，同类商品重复使用。';$('#panelHint').textContent='分类代码、来源和默认属性集中维护';
      $('#stats').innerHTML=[['本地分类',data.category.length],['平台映射',data.category_mapping.length],['已关联商品',data.product.filter(x=>x.category_id||data.category.some(c=>c.business_ids.includes(x.business_id))).length]].map(([t,n])=>`<div class="stat"><small>${E(t)}</small><b>${n}</b></div>`).join('');
      html=categoryUI.render(rows);
    } else if(page==='commerce_home'){
      $('#subtitle').textContent='先看进展，再处理重要的事。让商品从资料走到设计、上架与交付。';
      $('#listTitle').textContent='今日工作';$('#panelHint').textContent='真实进度与下一步操作，集中在一个工作空间';$('#addLabel').textContent='刷新工作台';
      $('#stats').innerHTML='';
      html=workspaceUI?workspaceUI.home(q):'<div class="empty">工作台视图暂不可用，请刷新页面。</div>';
    } else if(page==='commerce_products'){
      $('#subtitle').textContent='先看商品，再看规格。把真实资料、价格来源与设计准备放在一起。';$('#panelHint').textContent='点击商品维护档案，展开规格查看原图、价格和来源说明';$('#search').placeholder='搜索商品名称、规格或供应商';
      const ready=data.product.filter(p=>p.design_readiness?.skus?.length&&p.design_readiness.skus.every(s=>s.ready)).length;
      $('#stats').innerHTML=[['当前商品',data.product.length],['实际规格',data.sku.filter(s=>s.active!==false).length],['已有设计项目',data.product.filter(p=>p.project_id).length],['基础设计资料齐全',ready]].map(([t,n])=>`<div class="stat"><small>${E(t)}</small><b>${n}</b></div>`).join('');
      html=archiveUI.render(rows('product'));
    }
    else if(page==='commerce_listings'){
      $('#subtitle').textContent='集中查看商品规格、成本与站点售价，跟进资料和平台状态。';
      $('#panelHint').textContent='价格按对应币种展示 · 创建和更新时间为北京时间';
      $('#search').placeholder='搜索商品、品牌、SKU 或店铺';
      $('#stats').innerHTML=[['上架资料记录',data.listing.length],['实际销售选项',data.listing.reduce((n,x)=>n+(x.draft?.skus?.length||0),0)],['生产资料齐备',data.listing.filter(x=>x.material_status==='ready').length],['有待处理事项',data.listing.filter(x=>x.publication_progress?.needs_attention??x.publication_progress?.incomplete??(x.platform_issues||x.issues||[]).length>0).length]].map(([t,n])=>`<div class="stat"><small>${E(t)}</small><b>${n}</b></div>`).join('');
      listingFilters.mount();
      html=((data.listing_batches||[]).length?`<details class="target-batch-history"><summary>最近上架批次（${data.listing_batches.length}）</summary>`+table(['批次','账号与地区','任务进度'],data.listing_batches.map(b=>[`#${b.id}`,b.groups.map(g=>`${E(g.account_name)} · ${E(g.name)} · ${E(label(g.environment))}（${g.listing_ids.length} 条）`).join('<br>'),b.job_statuses.map(status=>badge(status)).join(' ')]))+'</details>':'')+listingUI.render(data.listing.filter(x=>listingUI.matches(x,q)&&listingFilters.matches(x)));
    }
    else if(page==='commerce_orders'){html=orderUI.listing(rows('order'));$('#subtitle').textContent='按已配置店铺获取待发货订单，线下发货后直接回填物流并提交所属平台。';$('#addLabel').textContent='刷新店铺订单'}
    else if(page==='commerce_aftersales')html=aftersaleUI.listing(rows('aftersale'));
    else if(page==='commerce_operations')html=`<div class="c-toolbar">${button('campaign-new',0,'创建活动计划')}${button('rule-new',0,'设置无销量规则')}${button('rules-evaluate',0,'运行观察')}${button('sync-finance',0,'同步平台交易')}</div><h3>活动计划</h3>`+table(['活动','时间 / 预算','执行方式','操作'],rows('campaign').map(x=>[E(x.name),`${E(x.starts_at)} ～ ${E(x.ends_at)}<small>${E(amount(x.budget,x.currency))}</small>`,`平台后台办理 · ${badge(x.status)}`,button('campaign-record',x.id,'登记 / 撤销结果')]))+'<h3>运营规则</h3>'+table(['规则','观察期','状态'],rows('rule').map(x=>[E(x.name),`${x.days} 天`,x.enabled?'启用观察建议':'未启用']))+'<h3>平台交易 · 待对账</h3>'+table(['平台交易ID','状态','详情'],rows('transaction').map(x=>[E(x.external_id),badge(x.status),`<details><summary>查看交易明细</summary><pre>${E(JSON.stringify(x.raw,null,2))}</pre></details>`]));
    if(page==='commerce_finance')html=`<div class="c-toolbar">${button('sync-finance',0,'同步平台交易')}${button('report-new',0,'申请退货报表')}${button('report-discover',0,'查询结算报表')}</div><p class="c-hint">平台净额与订单收入分开核对。费用已登记时关联原费用，避免重复计入。平台结算释放不等于银行已到账。</p>`+table(['交易ID / 环境','平台类型 / 金额','状态','操作'],rows('transaction').map(x=>[E(x.external_id)+badge(x.environment),`${E(x.raw.transactionType)}<small>${E(x.raw.totalAmount?.currencyAmount)} ${E(x.raw.totalAmount?.currencyCode)} · ${E(x.raw.transactionStatus)}</small>`,badge(x.status),actions(button('transaction-reconcile',x.id,'关联订单 / 核对'),`<details><summary>明细</summary><pre>${E(JSON.stringify(x.raw,null,2))}</pre></details>`)]))+'<h3>退货与结算报表</h3>'+table(['报表 / 环境','时间范围','状态 / 行数','操作'],rows('report').map(x=>[E(x.report_type)+badge(x.environment),E(x.start)+' ～ '+E(x.end),badge(x.status)+' '+E(x.row_count??''),actions(button('report-fetch',x.id,'下载 / 继续解析'),button('report-reconcile',x.id,'核对申请'),x.status==='completed'?`<a href="/api/commerce/reports/${x.id}/download">导出表格</a>`:'')]));
    if(['commerce_home','commerce_listings'].includes(page)&&data.settings.listing_automation_paused!==false){html=`<div class="c-toolbar" role="status"><strong>自动上架已暂停</strong><span>保留当前进度；查看状态和人工维护仍可使用。</span>${window.workspaceRole==='operator'?'':button('settings',0,'经营设置')}</div>`+html;}
    if(renderedPage!==page||renderedContent!==html){$('#content').innerHTML=html;renderedPage=page;renderedContent=html;}
  };
  openEditor=function(id=null){if(!page.startsWith('commerce_'))return baseEditor(id);const action=({commerce_categories:'category-new',commerce_home:'refresh',commerce_products:'import-product',commerce_listings:'targets',commerce_orders:'orders-sync-selected',commerce_aftersales:'aftersales-sync',commerce_support:'support-connections',commerce_operations:'campaign-new'})[page]||'refresh';dispatch(action,0).catch(e=>notice(e.message,true))};window.openEditor=openEditor;
  const previousSearchInput=$('#search').oninput;
  $('#search').oninput=event=>page.startsWith('commerce_')?render():previousSearchInput?.call($('#search'),event);

  function customTargetDialog(accountId=null){
    const accounts=items.platform_accounts||[];
    show('配置上架目标',`<div class="c-grid">${select('account_id','平台同步中的账号',accountId||accounts[0]?.id,accounts.map(x=>[x.id,x.account_name]))}${input('name','站点名称','美国站')}${input('marketplace_id','站点标识','ATVPDKIKX0DER')}<input type="hidden" name="environment" value="production">${select('region','区域','NA',[['NA','北美'],['EU','欧洲'],['FE','远东']])}${input('currency','销售币种','USD')}${select('language','上架资料语言','',languageOptions())}</div><p class="c-hint">目标来自平台同步账号。同商品在不同店铺或站点各有一条记录。正式上架前需验证账号和站点。</p>`,async f=>{await call('/targets',{...Object.fromEntries(f),account_id:Number(f.get('account_id')),enabled:true})});
  }
  function settings(){show('经营设置',`<div class="c-grid">${input('media_public_base_url','公网图片根地址（HTTPS）',data.settings.media_public_base_url)}${select('listing_automation_paused','自动上架推进',String(data.settings.listing_automation_paused!==false),[['true','暂停：保留进度，停止后续自动步骤'],['false','运行：按现有流程继续']])}${select('live_writes_enabled','正式平台写入',String(!!data.settings.live_writes_enabled),[['false','关闭：禁止正式提交'],['true','启用：允许更新正式店铺']])}</div><p class="c-hint">自动推进与平台写入分别控制。暂停后已发出的请求会保存结果，后续自动步骤停止；只读同步和手动资料维护仍可使用。设计工坊中已经运行的设计可在工坊单独暂停。恢复推进不会自动重试已失败或结果未知的任务。</p><p class="c-hint">图片上传与本地预览不依赖公网地址。临时隧道用于有限测试，正式部署使用稳定域名。</p>`,f=>call('/settings',{media_public_base_url:f.get('media_public_base_url'),listing_automation_paused:f.get('listing_automation_paused')==='true',live_writes_enabled:f.get('live_writes_enabled')==='true'}))}
  function archiveFact(record,field){
    const aliases=({material:['material','材质','材料','材料组成'],fabric:['fabric','面料','面料成分'],
      fill_material:['fill_material','filling','填充','填充物','填充材质'],
      package_quantity:['package_quantity','number_of_items','item_package_quantity','销售件数','每包件数','销售单位件数','包装数量'],
      item_shape:['item_shape','shape','形状','外形'],pattern:['pattern','花型','图案','纹样'],
      item_weight:['weight','item_weight','重量','净重'],package_weight:['package_weight','包装重量','毛重']})[field]||[field];
    const empty=value=>value==null||['','-','—','未知','待补充','未填写','暂无','unknown'].includes(String(value).trim().toLowerCase());
    const conflict=()=>({value:'',status:'conflict'});
    const evidence=aliases.map(key=>record.structured_facts?.[key]).filter(value=>value&&typeof value==='object'&&!Array.isArray(value));
    const explicit=evidence.filter(value=>['manual','cleared','not_applicable'].includes(value.status));
    const candidates=explicit.length?explicit:evidence;
    if(candidates.length){
      if(candidates.some(value=>value.status==='conflict'))return conflict();
      const values=candidates.flatMap(value=>value.status==='not_applicable'?[{value:'不适用',status:'not_applicable'}]:
        ['cleared','missing'].includes(value.status)||value.value==null||value.value===''?[{value:'',status:'cleared'}]:
        empty(value.value)?[]:[{value:String(value.value),status:value.status||'source',source:value.source,unit:value.unit,qualifiers:value.qualifiers,qualification_evidence:value.qualification_evidence}]);
      if(new Set(values.map(value=>value.value.trim())).size>1)return conflict();
      return values[0]||{value:'',status:'cleared'};
    }
    const legacy=aliases.flatMap(key=>[key==='components'?null:record[key],record.normalized_fields?.[key]]).filter(value=>!empty(value)&&typeof value!=='object').map(String);
    if(legacy.length)return new Set(legacy.map(value=>value.trim())).size>1?conflict():{value:legacy[0],status:'archive'};
    const labels=new Set(aliases.map(value=>value.toLowerCase())),values=[];
    for(const line of String(record.facts||'').split('\n')){
      const match=line.match(/^\s*([^:：]+)[:：]\s*(.*)$/);
      if(match&&labels.has(match[1].trim().toLowerCase())&&!empty(match[2]))values.push(match[2].trim());
    }
    return new Set(values).size>1?conflict():{value:values[0]||'',status:values.length?'fact_text':'missing'};
  }
  async function editCatalog(kind,id){
    const x=find(kind,id);
    if(!x)throw Error('商品档案不存在，请刷新后重试');
    show('读取商品档案','<p>正在读取完整商品资料…</p>');
    const waiting=dialog.querySelector('form');
    const fresh=await api(`/api/commerce/records/${kind}/${id}`);
    if(!dialog.open||dialog.querySelector('form')!==waiting)return;
    Object.assign(x,fresh);delete x._summary;
    const factDefs=[['material','材质组成'],['fabric','面料成分'],['fill_material','填充材料'],['package_quantity','每个销售单位包含件数'],['components','实际随售组成（如枕芯 × 1；枕套 × 1）'],['item_shape','形状'],['pattern','图案'],['item_length','商品长度（含单位）'],['item_width','商品宽度（含单位）'],['item_height','商品高度（含单位）'],['natural_height','自然高度（含单位，可填写范围）'],['compressed_height','受压高度（含单位，可填写范围）'],['fill_weight','填充重量（含单位）'],['item_weight','商品重量（含单位）'],['package_dimensions','包装尺寸（含单位）'],['package_weight','包装重量（含单位）'],['manufacturer','制造商'],['model_name','型号名称'],['model_number','制造商型号编号']];
    const facts=Object.fromEntries(factDefs.map(([field])=>[field,archiveFact(x,field)]));
    const common=`${input('name',kind==='sku'?'本款 SKU 名称':'商品名称',x.name)}<label class="c-field"><span>名称核对</span><span><input type="checkbox" name="confirm_archive_name">已按实物核对名称（名称未修改时也可确认）</span></label>${input('cost','采购成本',x.cost,'number','step="0.01" min="0"')}${input('cost_currency','采购币种',x.cost_currency)}${input('price','默认售价',x.price,'number','step="0.01" min="0"')}${input('currency','售价币种',x.currency)}`;
    const variants=kind==='sku'?input('color','颜色/款式',x.color)+input('size','实际尺寸（明确单位）',x.size)+input('gtin','真实 GTIN/UPC/EAN',x.gtin)+input('asin','已有完全相同商品的 ASIN',x.asin)+select('sale_mode','销售备货模式',x.sale_mode||'stock',[['stock','现货销售'],['made_to_order','按单生产']])+input('production_capacity','按单生产可承接总量',x.production_capacity||0,'number','min="0"'):select('business_id','所属业务',x.business_id||'',[['','待选择'],...(items.businesses||[]).map(b=>[b.id,b.name])])+select('brand_status','商品品牌状态',x.brand_status||(x.brand?'branded':'unknown'),[['branded','有明确商品品牌'],['unbranded','确认无品牌且实物包装无品牌 LOGO'],['logo_only','仅见 LOGO，品牌归属未确认'],['unknown','品牌资料未知']])+input('brand','商品品牌',x.brand)+input('brand_logo_text','实物 LOGO 线索',x.brand_logo_text||'')+categoryUI.productField(x)+input('product_type','Amazon 商品类型',x.product_type||'')+input('production_days','生产天数',x.production_days,'number','min="0"')+input('packing_days','打包备货天数',x.packing_days,'number','min="0"');
    const factFields=factDefs.map(([field,title])=>{
      const fact=facts[field],column=String(fact.source?.column||'');
      const unit=fact.unit||column.match(/[（(]\s*(kg|g|lb|oz|cm|mm|m|in|千克|克|公斤|磅|盎司|厘米|毫米|米|英寸)\s*[）)]/i)?.[1];
      const sourceHint=fact.source?.row?`来源：${fact.source.sheet||'原表'}第 ${fact.source.row} 行 · ${column}${unit?'；数值单位：'+unit:''}${fact.source.excerpt?'；原文：'+fact.source.excerpt:''}${fact.qualifiers?.length?'；保留限定：'+fact.qualifiers.map(q=>q==='estimated'?'估算值':q==='approximate'?'近似值':q).join('、'):''}`:'';
      return `${input('fact_'+field,unit?title.replace('（含单位）','（原表单位：'+unit+'）'):title,fact.value)}${select('state_'+field,title+'适用性',['cleared','not_applicable'].includes(fact.status)?fact.status:'manual',[['manual','依据实物填写；修改为空白表示清空'],['cleared','明确清空，不使用原表或父项值'],['not_applicable','明确不适用']])}${sourceHint?`<p class="c-hint">${E(sourceHint)}</p>`:''}${fact.status==='conflict'?'<p class="c-hint">该字段来源有冲突，请核对；未修改保存会保留原始证据。</p>':''}`;
    }).join('');
    const sourceNotes=archiveFact(x,'description').value;
    const notesHint=sourceNotes?`<p class="c-hint"><b>原表备注${/估算|估计|约|参考|待实测/.test(sourceNotes)?'（含估算或待核实说明）':''}：</b>${E(sourceNotes)}</p>`:'';
    const images=(x.source_images||[]).map(url=>`<img ${window.designImage(url,320)} alt="${x.design_reference?'AI 修正设计参考图':'本款原图'}">`).join('');
    const correctedReference=kind==='sku'?`<p class="c-hint">${x.design_reference?'当前设计输入为 AI 修正参考图，供应商原图和旧设计已保留。它不是厂家实拍或 GTIN 证明。':''}</p><button type="button" data-corrected-reference>采用已确认实货的修正设计参考图</button>${x.design_reference?`<details><summary>修正依据与保留的供应商原图</summary><pre>${E(JSON.stringify(x.design_reference,null,2))}</pre><div class="c-images">${(x.design_reference.retained_source_images||[]).map(url=>`<img ${window.designImage(url,320)} alt="保留的供应商原图">`).join('')}</div></details>`:''}`:'';
    const initial={};
    show(kind==='sku'?'实际 SKU 档案':'商品档案',`<p class="c-hint">供应商、制造商、品牌和型号分别维护。销售价按该批次已保存的供货倍率和物流预留规则计算，原表售价和业务销售预设只留存记录；真实供货价缺失或无效时需补齐，已有手工售价保留。</p>${notesHint}<div class="c-grid">${common}${variants}${factFields}${textarea('facts','其他有来源的实物参数',x.facts)}${textarea('components','生产组件：每行 名称 | 数量 | 尺寸 | 材质',(x.components||[]).map(v=>[v.name,v.quantity,v.size||'',v.material||''].join(' | ')).join('\n'))}</div><div class="c-images">${images}</div>${kind==='sku'?'<label class="c-upload">绑定本款真实原图（先保存其他字段）<input type="file" accept="image/jpeg,image/png,image/webp" data-archive-source-upload></label>':'<p class="c-hint">每款照片请在对应 SKU 档案绑定，避免混用颜色。</p>'}<details><summary>原表证据及识别问题</summary><pre>${E(JSON.stringify({source:x.supplier_source||x.source_variant_id,fields:x.source_fields||x.normalized_fields,issues:x.source_parse_issues,price_source:x.price_source,cost_source:x.cost_source},null,2))}</pre></details>`,async f=>{
      const raw=Object.fromEntries(f),d={revision:x.revision,structured_facts:{}};
      for(const [key,value] of Object.entries(raw)){
        if(key.startsWith('fact_')||key.startsWith('state_')||key==='components'||key==='confirm_archive_name')continue;
        let normalized=value;
        if(['cost','price'].includes(key))normalized=value===''?null:value;
        if(['production_days','packing_days','production_capacity','category_id','business_id'].includes(key))normalized=value===''?null:Number(value);
        if(String(normalized??'')!==String(x[key]??''))d[key]=normalized;
      }
      if(f.get('confirm_archive_name')==='on')d.name=raw.name;
      for(const [field] of factDefs){
        if(!('fact_'+field in raw)||!('state_'+field in raw))continue;
        const value=raw['fact_'+field],state=raw['state_'+field];
        if(value!==initial['fact_'+field]||state!==initial['state_'+field]){
          const unit=facts[field].unit||String(facts[field].source?.column||'').match(/[（(]\s*(kg|g|lb|oz|cm|mm|m|in|千克|克|公斤|磅|盎司|厘米|毫米|米|英寸)\s*[）)]/i)?.[1];
          const measured=unit&&/^\s*\d+(?:\.\d+)?\s*$/.test(value)?`${value.trim()} ${unit}`:value;
          d.structured_facts[field]={value:state==='cleared'?'':measured,status:state==='not_applicable'?'not_applicable':state==='cleared'||!value?'cleared':'manual'};
        }
      }
      if(!Object.keys(d.structured_facts).length)delete d.structured_facts;
      if('components' in raw&&raw.components!==initial.components)d.components=raw.components.split('\n').filter(v=>v.trim()).map(line=>{const [name,quantity,size,material]=line.split('|').map(v=>v.trim());return {name,quantity:Number(quantity),size,material}});
      await call(`/${kind==='sku'?'skus':'products'}/${id}`,d,'PATCH');
    });
    Object.assign(initial,Object.fromEntries(new FormData(dialog.querySelector('form'))));
    if(kind==='sku'){
      const slot=document.createElement('div');slot.innerHTML=correctedReference;
      dialog.querySelector('form').append(slot);
      slot.querySelector('[data-corrected-reference]').onclick=async()=>{
        const current=Object.fromEntries(new FormData(dialog.querySelector('form')));
        if(Object.keys(initial).some(key=>String(current[key])!==String(initial[key]))){notice('请先保存商品字段，再更换设计参考图',true);return}
        try{await editCorrectedReference(id)}catch(error){notice(error.message,true)}
      };
    }
    const upload=dialog.querySelector('[data-archive-source-upload]');
    const catalogForm=dialog.querySelector('form');
    if(upload)upload.onchange=async()=>{
      if(upload.disabled||!upload.files[0]||dialog.querySelector('form')!==catalogForm)return;
      const current=Object.fromEntries(new FormData(dialog.querySelector('form')));
      if(Object.keys(initial).some(key=>String(current[key])!==String(initial[key]))){notice('请先保存本页商品字段，再选择原图',true);upload.value='';return}
      const form=new FormData();form.append('file',upload.files[0]);upload.disabled=true;
      try{const response=await fetch(`/api/commerce/skus/${id}/source-images`,{method:'POST',body:form});const saved=await response.json();if(!response.ok)throw new Error(typeof saved.detail==='string'?saved.detail:JSON.stringify(saved.detail));await reload();if(dialog.open&&dialog.querySelector('form')===catalogForm)await editCatalog(kind,id);notice('已保留原始图片字节并绑定本款 SKU')}catch(error){notice(error.message,true)}finally{upload.disabled=false;upload.value='';}
    };
  }
  async function editCorrectedReference(id){
    const stillCurrent=guardDialog();let referenceProgress;
    const view=await api(`/api/commerce/skus/${id}/corrected-design-reference`);
    if(!view.parents.length)throw new Error('此规格尚无生成依据完整的旧设计，暂不能采用修正图');
    const sku=find('sku',id),siblings=(data.sku||[]).filter(x=>x.id!==id&&x.product_id===sku?.product_id&&x.active!==false&&!x.design_reference&&JSON.stringify(x.source_images||[])===JSON.stringify(sku.source_images||[])&&(x.source_images||[]).length);
    const shared=[];
    for(const sibling of siblings){const current=await api(`/api/commerce/skus/${sibling.id}/corrected-design-reference`);if(current.parents.length)shared.push(current);}
    if(!stillCurrent())return;
    const parentChoice=current=>current.parents.find(x=>x.key.startsWith('main-'))?.task_id||current.parents[0].task_id;
    const choices=current=>current.parents.map(v=>[v.task_id,`${v.key} · 任务#${v.task_id}`]);
    show(`修正设计参考图 · ${view.name}`,`<p class="c-hint">依据已确认的实货状态修正设计输入。原供应商图和旧设计保留，修正图明确标注为 AI 设计参考，不能作为厂家实拍、包装照片或 GTIN 证明。保存后当前设计和资料须按新输入继续。</p>${select('parent_task_id','修图对应的旧设计',parentChoice(view),choices(view))}${shared.length?`<label class="c-field"><span><input type="checkbox" name="shared_reference">同时登记共享同一原图的 ${shared.length+1} 个规格；确认修正图适用于全部这些规格</span></label><div data-shared-reference hidden>${shared.map(current=>select('parent_task_'+current.sku_id,current.name+' · 对应旧设计',parentChoice(current),choices(current))).join('')}<p>每个规格分别核对父设计、原图哈希及档案版本；已登记的规格保留，不重复替换。</p></div>`:''}${textarea('basis','实货确认与修图授权依据','')}<label class="c-field"><span>上传已核对的修正设计参考图</span><input name="file" type="file" accept="image/png,image/jpeg,image/webp" required></label><label class="c-field"><span><input type="checkbox" name="confirmed" required>确认修正图符合已确认的实货状态</span></label><p data-reference-progress role="status"></p>`,async f=>{
      const selected=[view,...(f.get('shared_reference')==='on'?shared:[])];let registered=0;
      for(const current of selected){
        const form=new FormData();form.append('file',f.get('file'));form.append('revision',String(current.revision));form.append('parent_task_id',String(f.get(current===view?'parent_task_id':'parent_task_'+current.sku_id)));form.append('basis',String(f.get('basis')||''));form.append('confirmed',f.get('confirmed')==='on'?'true':'false');
        const response=await fetch(`/api/commerce/skus/${current.sku_id}/corrected-design-reference`,{method:'POST',body:form});const saved=await response.json();if(!response.ok)throw new Error(`已登记 ${registered} 个规格；${current.name}：`+(typeof saved.detail==='string'?saved.detail:JSON.stringify(saved.detail)));
        registered++;if(referenceProgress?.isConnected)referenceProgress.textContent=`已登记 ${registered}/${selected.length} 个规格`;
      }
      notice(`已登记 ${registered} 个规格的 AI 修正设计参考，供应商原图和旧设计保留`);
    });
    referenceProgress=dialog.querySelector('[data-reference-progress]');
    const sharedControl=dialog.querySelector('[name=shared_reference]');if(sharedControl)sharedControl.onchange=()=>{dialog.querySelector('[data-shared-reference]').hidden=!sharedControl.checked;};
  }
  function priceSourceHint(sku,currency){const p=sku.price_source||{};if(sku.price!=null&&!['manual','same_currency','converted'].includes(p.status))return `<p class="c-hint">历史售价来源未记录，请从档案补报价或核对当前售价。</p><label class="c-field"><span>确认本款售价</span><input type="checkbox" name="confirm_price_${sku.sku_id}">我已核对 ${E(sku.price)} ${E(currency)}</label>`;const cost=p.cost_pricing||{};const basis=cost.status==='source_cost'?`原表成本 ${E(cost.cost)} ${E(cost.cost_currency||'币种待确认')} → ${E(cost.price)}；`:cost.status==='fallback'?`${E(cost.message)} ${E(cost.price)} ${E(cost.currency||'币种待确认')}；`:'';if(p.status==='converted')return `<p class="c-hint">${basis}原售价 ${E(p.source_amount)} ${E(p.source_currency)} → ${E(sku.price)} ${E(currency)}；1 ${E(p.source_currency)} = ${E(Number(p.rate).toFixed(6))} ${E(currency)}，${E(p.provider)} · ${E(p.date)}。仅作资料换算，未调整利润或运费。</p>`;if(p.message||basis)return `<p class="c-hint">${basis}${E(p.message||'')}</p>`;return ''}
  const listingMedia=window.createListingMedia({E,api});
  function repairImagePicker(d){
    const card=(sku,url,sha,title)=>sha?`<figure><img ${window.designImage(url,320)} alt="${E(title)}"><figcaption><label><input type="checkbox" data-correct-image data-sku-id="${sku.sku_id}" value="${E(sha)}">${E(title)}</label></figcaption></figure>`:'';
    return d.skus.map(sku=>`<details><summary>${E(sku.seller_sku)} · ${E(sku.name)}</summary><div class="c-images">`+
      d.images.filter(item=>item.variant_key===sku.variant_key&&item.source!=='design_master').map(item=>{
        const asset=find('media',item.asset_id);
        return asset?card(sku,asset.url,asset.original_sha256,`图库 · ${item.source==='design_main_candidate'?'平台首图':item.role==='detail'?'介绍图':'效果图'}`):'';
      }).join('')+(d.detail_page?.sections||[]).filter(item=>item.sku_id===sku.sku_id).map(item=>card(sku,item.url,item.sha256,`详情 · ${item.section_label||item.title}`)).join('')+'</div></details>').join('');
  }
  function groupingSummary(x){
    const report=x.variant_structure||find('product',x.product_id)?.variant_structure;
    const rows=report?.rows||[];
    return `<section data-grouping-summary><h3>商品分组与规格选项</h3><p>${E(report?.label||'按实际销售选项整理')}</p>${report?.distinct_products?`<p class="c-hint">${E(report.note)}</p>`:''}${rows.map(row=>`<p><b>${E(row.name)}</b><br>${row.details.map(v=>`${E(v.label)}：${E(v.value)}`).join(' · ')}</p>`).join('')}<button type="button" data-variation-preview>预览商品分组与上架方案</button><div data-variation-plan></div></section>`;
  }
  function completedFieldSummary(d){
    const proof=d.source?.attribute_completion;if(!proof)return '';
    const labels={item_shape:'形状',model_name:'型号名称',model_number:'厂家型号编号',pattern:'图案',special_feature:'产品特点',batteries_required:'是否需要电池',batteries_included:'是否含电池',required_product_compliance_certificate:'证书适用性',supplier_declared_dg_hz_regulation:'危险品法规',pillow_type:'枕头类型',item_thickness:'商品厚度',recommended_uses_for_product:'建议用途',indoor_outdoor_usage:'室内外用途',item_firmness_description:'软硬程度',style:'风格'};
    const readable=value=>Array.isArray(value)?value.map(readable).join('；'):value&&typeof value==='object'?(Object.hasOwn(value,'value')?readable(value.value):JSON.stringify(value)):value===''?'资料未提供，平台允许留空':value===true?'是':value===false?'否':value==='unknown'?'未知':value==='Not Applicable'||value==='not_applicable'?'不适用':String(value??'');
    return `<details><summary>自动补齐结果与来源依据</summary><p>${proof.status==='ready'?'本版本的官方必填属性已补齐，平台预检及实际显示另行确认':'仍有来源缺项，见下方记录'}</p>${Object.entries(proof.improvements_remaining||{}).map(([sku,names])=>`<p class="c-hint">${E(sku)}：${E(names.map(name=>labels[name]||name).join('、'))}仍缺少可用来源；保留待补，不编造数值。</p>`).join('')}${Object.entries(proof.values||{}).map(([key,values])=>`<h4>${E(key==='parent'?'系列共用信息':d.skus.find(v=>String(v.sku_id)===key)?.name||key)}</h4><dl>${Object.entries(values).map(([field,evidence])=>`<dt>${E(labels[field]||field)}</dt><dd>${E(readable(evidence.value))}<small>${E(evidence.basis)}</small></dd>`).join('')}</dl>`).join('')}</details>`;
  }
  async function fullListing(id){
    const fresh=await api(`/api/commerce/records/listing/${id}`);
    if(fresh?.id!==Number(id))throw Error('商品资料读取不完整，请刷新后重试');
    const cached=find('listing',id);
    if(cached)Object.assign(cached,fresh);
    return fresh;
  }
  async function listingDialog(id,explainIssues=false){
    const current=guardDialog();
    const fresh=await fullListing(id);
    if(!current())return;
    if(!fresh.draft?.skus?.length)throw Error('请先完成资料生成');
    if(!fresh.draft.content||!Array.isArray(fresh.draft.images))throw Error('完整上架资料尚未读取成功，请刷新后重试');
    renderListingDialog(fresh);
    if(explainIssues&&fresh.issues?.length){
      const box=document.createElement('p');box.className='c-error';box.setAttribute('role','alert');
      box.textContent='本次尚未提交，请先处理：'+fresh.issues.join('；');
      dialog.querySelector('.c-body').prepend(box);
    }
  }
  function renderListingDialog(x){const id=x.id,d=structuredClone(x.draft);window.commerceEditingDraft=d;
    const fields=(x.environment==='sandbox'?'<p class="c-hint">当前为官方静态沙箱：接口自检使用Amazon固定样例，不能验证本商品真实可售，也不会自动产生本商品订单。该历史目标已停止执行，请新增真实授权账号地区关联。</p>':'')+`<div class="c-grid">${input('brand','商品品牌（在商品档案中维护）',d.brand,'text','readonly')}<input type="hidden" name="product_type" value="${E(d.product_type||'')}">${input('currency','销售币种',d.currency)}${select('price_shipping_mode','商品价与运费',d.price_shipping_mode||'unspecified',[['unspecified','尚未明确'],['separate','商品价不含运费，结账按模板收费'],['included','商品价已包含运费']])}${input('handling_days','生产与备货总天数',d.handling_days,'number','min="1"')}${d.handling_days!=null&&!['manual','archive','confirmed_config'].includes(d.handling_source?.status)?'<label class="c-field"><span>历史备货期来源待核对</span><input type="checkbox" name="confirm_handling_days">我已核对当前备货天数</label>':''}${input('shipping_group','配送模板标识',d.shipping_group)+input('variation_theme','变体主题（使用平台枚举；独立商品留空）',d.variation_theme||'')}</div>`;
    const skuFields=d.skus.map((v,i)=>`<section class="c-sku"><h3>${E(find("sku",v.sku_id)?.name||v.name)} <small class="ws-mono">${E(v.seller_sku)}</small></h3>${d.skus.length>1||Object.hasOwn(v,"title")?input(`title_${i}`,`本规格上架标题 · ${v.seller_sku}`,v.title??d.content.title,'text','maxlength="75"'):""}${priceSourceHint(v,d.currency)}${v.quantity_source?.policy==='amazon_max'?`<p class="c-hint">${E(v.quantity_source.message)}：${E(v.quantity??'待同步')} 件。</p>`:''}<div class="c-grid">${input(`price_${i}`,`${d.price_shipping_mode==='separate'?'商品价（不含运费）':'资料售价'}（${d.currency}）`,v.price,'number','step="0.01" min="0"')}${input(`quantity_${i}`,'发布可售数量',v.quantity,'number','min="0"')}${v.quantity!=null&&!['manual','stock','archive_capacity','confirmed_config'].includes(v.quantity_source?.status)?`<label class="c-field"><span>历史可售量来源待核对</span><input type="checkbox" name="confirm_quantity_${v.sku_id}">我已核对本款可售量</label>`:''}${input(`color_${i}`,'平台颜色名称',v.color,'text','readonly')}${input(`size_${i}`,'实际尺寸',v.size,'text','readonly')}${input(`gtin_${i}`,'GTIN（暂无可留空）',v.gtin,'text','readonly')}${input(`asin_${i}`,'已有ASIN（暂无可留空）',v.asin,'text','readonly')}<p class="c-hint">商品事实从档案读取。无 GTIN 时请先在 Seller Central 办理豁免，再登记当前店铺与商品范围的批准回执。</p></div><h4>平台首图</h4><p class="c-hint">亚马逊通常要求首图纯白；销售封面与完整故事另行编排。平台首图已选定不代表整套展示已完成。</p><div class="c-images" data-sku-images="${i}">${v.images.map(a=>{const asset=find('media',a.asset_id);return asset?`<img ${window.designImage(asset.url,320)} alt="主图">`:''}).join('')}</div><label class="c-upload">上传此 SKU 商品照片并自动准备主图<input type="file" accept="image/jpeg,image/png,image/webp" data-upload-sku="${i}"></label><h4>本规格附图 · 按上架顺序</h4><div class="c-images" data-aux-images="${i}">${d.images.filter(a=>a.variant_key===v.variant_key&&!v.images.some(m=>m.role==='main'&&m.asset_id===a.asset_id)).map(a=>{const asset=find('media',a.asset_id);return asset?`<figure><img ${window.designImage(asset.url,960)} alt="${E(a.role)}"><figcaption>${a.source==='design_master'?'历史母稿 · 内部参考（不提交）':a.display_role==='sales_cover'?'销售封面 · 场景与价值':a.source==='design_main_candidate'?'未选用的平台首图候选':a.role==='effect'?'效果图':'介绍图'}</figcaption><button type="button" data-media-move="${d.images.indexOf(a)}">前移</button><button type="button" data-media-remove="${d.images.indexOf(a)}">移除</button></figure>`:''}).join('')}</div><label class="c-upload">添加附图<input type="file" accept="image/jpeg,image/png,image/webp" data-upload-aux="${i}"></label></section>`).join('');
    show('生产资料 · '+x.name,`<section><button type="button" data-parameter-history-load>参数变更记录</button><div data-parameter-history></div></section><p>上架地区：${E(d.localization?.country_region||find('target',x.target_id)?.name||'')} · 文案语言：${E(d.localization?.language_name||d.language||'未配置')}</p><p class="c-hint">原设计图片语言：${E(d.image_language||'未记录')}。图片内文字沿用原设计，需单独生成目标语言版本。</p><p class="c-hint">内部 SKU 由商品记录和变体标识自动生成，保存及重新生成资料会沿用。GTIN 和 ASIN 属于平台商品标识，暂无可留空，不影响生产资料生成与下载。</p>`+categoryUI.listingPanel(d)+fields+bilingualUI.render(d)+(d.content_review_required?'<label class="c-field"><span>商品资料已变更，请核对保留的人工文案</span><input type="checkbox" name="confirm_content_review">我已按新档案核对文案</label>':'')+`<section class="detail-publication-summary">${listingMedia.publication(x.detail_publication,id)}</section><section class="detail-material-summary">${listingMedia.render(d.detail_page,d.language)}</section>`+(d.skus.length>1?'<p class="c-hint">每个规格使用下方独立上架标题；上方系列文案由全部规格共用。</p>':'')+`<section><h3>完整展示编排</h3><p class="c-hint">逐规格核对首图、销售封面、图库顺序与数量、详情内容。预览反映保存的资料；亚马逊最终显示另需回查。</p><button type="button" data-presentation-preview>查看逐规格展示编排</button><div data-presentation-plan></div><p class="c-hint">新设计标准：每个规格7张图库、1张完整详情图。图内内容区域不拆成多个生成任务。历史分段版本保留原格式；母稿只作内部参考。</p><button type="button" data-presentation-repair>保留成品并补齐完整展示</button><details><summary>修正图内事实冲突</summary><label class="c-field">填写真实冲突和正确依据<textarea data-fact-correction maxlength="1000"></textarea></label>${repairImagePicker(d)}<p class="c-hint">仅勾选确实有事实冲突的图片；同规格其他成品原样保留。旧图和旧版本保留。</p></details>${d.skus.length>1?'<button type="button" data-variation-family>按来源重新整理商品分组</button>':''}</section>`+groupingSummary(x)+skuFields+`<section><h3>平台属性</h3>${completedFieldSummary(d)}<p class="c-hint">商品属性从档案冻结，修改后重新组装资料。</p>${button('schema-load',id,'到档案补充官方字段')}<button type="button" data-complete-fields>自动补齐官方字段与规格关系</button><button type="button" data-archive-reassemble>重新组装档案字段</button><button type="button" data-identity-review>核对平台编号变化</button><div id="c-schema-fields"></div><details><summary>高级属性数据</summary><pre>${E(JSON.stringify(d.attributes||{},null,2))}</pre><p>父体共用事实从商品档案读取</p><pre>${E(JSON.stringify(d.parent_attributes||{},null,2))}</pre></details></section><details><summary>以后上架时待处理的字段与平台结果</summary><pre>${E(JSON.stringify({production_issues:x.material_issues,platform_issues:x.platform_issues,results:x.sku_results,observed:x.platform_observed,preview:x.preview_results,identifier_check:x.identifier_check,preflight:x.preflight_results,media_exclusions:x.media_exclusions},null,2))}</pre></details>`,async f=>{
      d.content={title:f.get('title'),bullets:f.get('bullets').split('\n').filter(Boolean),description:f.get('description'),search_terms:f.get('search_terms')};
      for(const key of ['brand','product_type','currency','shipping_group','variation_theme','price_shipping_mode'])d[key]=f.get(key);d.handling_days=f.get('handling_days')?Number(f.get('handling_days')):null;
      for(const element of dialog.querySelectorAll('[data-schema-attribute]')){if(!element.value.trim())continue;const key=element.dataset.schemaAttribute,prop=element.dataset.schemaProperty||'value';const value=element.dataset.schemaType==='number'?Number(element.value):element.value;d.attributes[key]=[{...(d.attributes[key]?.[0]||{}),[prop]:value,marketplace_id:find('target',x.target_id).marketplace_id}]}
      d.skus.forEach((v,i)=>{if(f.has(`title_${i}`))v.title=String(f.get(`title_${i}`));v.price=f.get(`price_${i}`)||null;v.quantity=f.get(`quantity_${i}`)===''?null:Number(f.get(`quantity_${i}`));});
      await call(`/listings/${id}/draft`,{revision:x.revision,draft:d,confirm_content_review:f.get('confirm_content_review')==='on',confirm_handling_days:f.get('confirm_handling_days')==='on',confirmed_quantity_sku_ids:d.skus.filter(v=>f.get(`confirm_quantity_${v.sku_id}`)==='on').map(v=>v.sku_id),confirmed_price_sku_ids:d.skus.filter(v=>f.get(`confirm_price_${v.sku_id}`)==='on').map(v=>v.sku_id)},'PATCH');
    });
    
    window.WorkspaceShell?.sectionListing(dialog);
    const editForm=dialog.querySelector('form'),editingCurrent=()=>dialog.open&&dialog.querySelector('form')===editForm;
    const archiveForm=dialog.querySelector('form');
    const archiveFieldSnapshot=()=>JSON.stringify([...new FormData(archiveForm).entries()].filter(([,value])=>typeof value==='string'));
    const archiveInitialFields=archiveFieldSnapshot(),archiveInitialDraft=JSON.stringify(d);
    dialog.querySelector('[data-archive-reassemble]').onclick=async event=>{
      if(archiveFieldSnapshot()!==archiveInitialFields||JSON.stringify(d)!==archiveInitialDraft){notice('请先保存本页修改，再重新组装档案字段',true);return}
      const control=event.currentTarget;control.disabled=true;
      try{await call(`/listings/${id}/archive-fields`,{revision:x.revision});await reload();if(editingCurrent())await listingDialog(id);notice('已按当前档案生成新资料版本；报价、库存、文案和图片保留，上架时重新完整校验');}
      catch(error){notice(error.message,true);control.disabled=false;}
    };
    dialog.querySelector('[data-identity-review]').onclick=async event=>{
      if(archiveFieldSnapshot()!==archiveInitialFields||JSON.stringify(d)!==archiveInitialDraft){notice('请先保存本页修改，再核对平台编号',true);return}
      const control=event.currentTarget;control.disabled=true;
      try{
        const review=await call(`/listings/${id}/identity-review`,{});
        if(!editingCurrent())return;
        show('核对平台商品编号变化',`<p>旧身份记录永久保留。当前编号已按已成功提交的标题、尺寸、重量、材质等事实逐项核对；请继续核对每款图片。</p>${review.rows.map(row=>`<section><h3>${E(row.seller_sku)}</h3><p>原编号 ${E(row.old_asin)} → 当前编号 ${E(row.asin)}</p><div class="c-images"><figure><img ${window.ShopImages.attributes(row.submitted_image,320,{remote:true})} alt="已提交图片"><figcaption>已提交图片</figcaption></figure><figure><img ${window.ShopImages.attributes(row.current_image,320,{remote:true})} alt="平台当前图片"><figcaption>平台当前图片</figcaption></figure></div><details><summary>已逐项匹配的规格事实</summary><pre>${E(JSON.stringify(row.expected,null,2))}</pre></details></section>`).join('')}${textarea('reason','核对依据','')}<label class="c-field"><span><input type="checkbox" name="confirmed" required>已核对新旧编号、规格事实和图片，确认对应同一实际 SKU</span></label>`,async f=>{
          await call(`/listings/${id}/identity-review/confirm`,{review_id:review.id,confirmed:f.get('confirmed')==='on',reason:String(f.get('reason')||'')});
          notice('身份核对记录已保存，旧记录保留，正在重新回查平台状态');
        });
      }catch(error){notice(error.message,true);control.disabled=false;}
    };
    dialog.querySelector('[data-parameter-history-load]').onclick=async event=>{
      const control=event.currentTarget;control.disabled=true;
      try{
        const history=await api(`/api/commerce/listings/${id}/parameter-history`);
        if(!editingCurrent())return;
        const value=x=>x===null||x===undefined?'（未填写）':Array.isArray(x)?x.join('\n'):String(x);
        dialog.querySelector('[data-parameter-history]').innerHTML=`<p class="c-hint">${E(history.message)}</p>`+history.entries.map(entry=>`<details><summary>v${E(entry.before_version)} → v${E(entry.version)} · ${E(entry.saved_at)} UTC · ${entry.changes.length}项变化</summary>${table(['参数 / SKU','修改前','修改后'],entry.changes.map(change=>[`${E(change.parameter)}<small>${E(change.seller_sku||'系列共用')}</small>`,`<pre style="white-space:pre-wrap;overflow-wrap:anywhere">${E(value(change.before))}</pre>`,`<pre style="white-space:pre-wrap;overflow-wrap:anywhere">${E(value(change.after))}</pre>`]))}</details>`).join('')+(history.entries.length?'':'<p>尚无这些参数的版本变化记录。</p>');
      }catch(error){notice(error.message,true)}finally{control.disabled=false}
    };
    bilingualUI.mount(dialog,id);
    listingMedia.mount(dialog,id,d);
    dialog.querySelector('[data-presentation-preview]').onclick=async event=>{
      const control=event.currentTarget;control.disabled=true;
      try{const plan=await api(`/api/commerce/listings/${id}/presentation-plan`);
        if(!editingCurrent())return;
        dialog.querySelector('[data-presentation-plan]').innerHTML=`<p>${E(plan.message)}</p>`+plan.skus.map(sku=>`<section><h4>${E(sku.seller_sku)} · 已保存图库 ${sku.gallery_count} 张 / 详情 ${sku.detail_count} ${sku.detail_unit||'段'}</h4><p class="c-hint">${E(sku.message)}</p><div class="c-images">${sku.gallery.map((image,index)=>`<figure><img ${window.designImage(image.url,320)} alt="${E(image.label)}"><figcaption>${index+1}. ${E(image.label)}</figcaption></figure>`).join('')}</div><details><summary>本规格详情内容 · ${sku.detail_count} ${sku.detail_unit||'段'}</summary><div class="c-images">${sku.detail.map(image=>`<figure><img ${window.designImage(image.url,320)} alt="${E(image.title)}"><figcaption>${E(image.title)}</figcaption></figure>`).join('')}</div></details></section>`).join('');
      }catch(error){notice(error.message,true)}finally{control.disabled=false}
    };
    const repairControl=dialog.querySelector('[data-presentation-repair]');
    const localPictureRepair=x.paused===true && x.status==='not_submitted' && !x.published_version && !x.retirement_id;
    if(localPictureRepair)repairControl.textContent='修正所选图片并更新本地资料';
    const repairForm=dialog.querySelector('form');
    const savedFields=()=>JSON.stringify([...new FormData(repairForm).entries()].filter(([,v])=>typeof v==='string'));
    const initialRepairFields=savedFields(),initialRepairDraft=JSON.stringify(d);
    const repairClean=()=>{if(savedFields()!==initialRepairFields||JSON.stringify(d)!==initialRepairDraft){notice('请先保存本页修改，再执行展示或规格修复',true);return false}return true};
    repairControl.onclick=async()=>{
      if(!repairClean())return;
      repairControl.disabled=true;
      try{const correction=dialog.querySelector('[data-fact-correction]').value.trim();
        const selected=[...dialog.querySelectorAll('[data-correct-image]:checked')];
        const correction_sku_ids=[...new Set(selected.map(e=>Number(e.dataset.skuId)))];
        const correction_sha256=[...new Set(selected.map(e=>e.value))];
        const result=await call(`/listings/${id}/presentation-repair`,{revision:x.revision,correction,correction_sku_ids,correction_sha256,local_only:localPictureRepair});
        if(editingCurrent())dialog.close();notice(result.message);reload().catch(error=>notice(error.message,true));
      }catch(error){notice(error.message,true)}finally{repairControl.disabled=false}
    };
    const batteryControl=document.createElement('button');batteryControl.type='button';
    batteryControl.textContent=d.source?.battery_declaration?.batteries_required===false?'已确认本商品全部规格无需电池':'确认本商品全部规格无需电池';
    batteryControl.disabled=d.source?.battery_declaration?.batteries_required===false;
    repairControl.parentElement.append(batteryControl);
    batteryControl.onclick=async()=>{
      if(!repairClean())return;
      batteryControl.disabled=true;
      try{await call(`/listings/${id}/no-battery-declaration`,{revision:x.revision,sku_ids:d.skus.map(s=>s.sku_id),confirmed:true});if(editingCurrent())await listingDialog(id);notice('本商品全部规格无需电池的确认已保存；图片保持不变，提交仍做完整校验');reload().catch(error=>notice(error.message,true))}
      catch(error){notice(error.message,true);batteryControl.disabled=false}
    };
    const completeFieldsControl=dialog.querySelector('[data-complete-fields]');
    if(completeFieldsControl)completeFieldsControl.onclick=async()=>{
      if(!repairClean())return;
      completeFieldsControl.disabled=true;
      try{const result=await call(`/listings/${id}/complete-fields`,{revision:x.revision});if(editingCurrent())dialog.close();await reload();notice(result.already_complete?'当前官方字段与规格关系已补齐':'已开始按原文、原图和当前类目规则自动补齐；无需重做设计')}
      catch(error){notice(error.message,true)}finally{completeFieldsControl.disabled=false}
    };
    const familyControl=dialog.querySelector('[data-variation-family]');
    if(familyControl)familyControl.onclick=async()=>{
      if(!repairClean())return;
      familyControl.disabled=true;
      try{await call(`/listings/${id}/variation-family`,{revision:x.revision});if(editingCurrent())await listingDialog(id);notice('已保存本地分组方案；已发布关系和买家页面仍需另行确认，未重新设计');reload().catch(error=>notice(error.message,true))}
      catch(error){notice(error.message,true)}finally{familyControl.disabled=false}
    };
    const groupingPreview=dialog.querySelector('[data-variation-preview]');
    if(groupingPreview)groupingPreview.onclick=async()=>{
      groupingPreview.disabled=true;
      try{
        const result=await api(`/api/commerce/listings/${id}/variation-plan`),plan=result.plan;
        if(!editingCurrent())return;
        const title=plan.status==='ready'?`作为同款的 ${d.skus.length} 个规格选项上架`:plan.status==='independent'?`作为 ${d.skus.length} 个独立商品分别上架`:'实际差异还需补充来源';
        const subgroup=(plan.groups||[]).map((group,index)=>`<section><h5>商品组 ${index+1} · ${group.sku_ids.length} 个销售选项</h5><p>${E(group.status==='ready'?'保留为同款规格：'+group.theme:group.reason||'单规格商品')}</p><p>${E(group.sku_ids.map(sku=>result.source_variants.rows.find(row=>row.sku_id===sku)?.name||'实际规格').join('、'))}</p></section>`).join('');
        dialog.querySelector('[data-variation-plan]').innerHTML=`<h4>${E(title)}</h4><p>${E(plan.reason||'')}</p>${subgroup}${(plan.rows||[]).map(row=>{const source=result.source_variants.rows.find(v=>v.sku_id===row.sku_id);return `<p><b>${E(source?.name||'实际规格')}</b><br>${E([row.color,row.size,row.set_name].filter(Boolean).join(' · '))}</p>`}).join('')}${result.published_relationship_change?'<p class="c-hint">新方案与已提交的亚马逊规格关系不同，需要单独更正并回读；本次预览不会修改线上商品。</p>':''}<p class="c-hint">${E(result.note)}</p>`;
      }catch(error){notice(error.message,true)}finally{groupingPreview.disabled=false}
    };
    mountMainImageTools(id,d);
    function renderAux(){d.skus.forEach((v,i)=>{dialog.querySelector(`[data-aux-images="${i}"]`).innerHTML=d.images.map((a,n)=>{if(a.variant_key!==v.variant_key||v.images.some(m=>m.role==='main'&&m.asset_id===a.asset_id))return '';const asset=find('media',a.asset_id);return `<figure><img ${window.designImage(asset?.url,960)} alt="附图"><figcaption>${a.source==='design_master'?'历史母稿 · 内部参考（不提交）':a.display_role==='sales_cover'?'销售封面 · 场景与价值':a.source==='design_main_candidate'?'未选用的平台首图候选':a.role==='detail'?'介绍图':'效果图'}</figcaption><button type="button" data-media-move="${n}">前移</button><button type="button" data-media-remove="${n}">移除</button></figure>`}).join('')})}
    dialog.querySelector('.c-body').addEventListener('click',event=>{const b=event.target.closest('[data-media-move],[data-media-remove]');if(!b)return;const n=Number(b.dataset.mediaMove??b.dataset.mediaRemove);if('mediaRemove' in b.dataset)d.images.splice(n,1);else{const previous=d.images.map((a,i)=>i<n&&a.variant_key===d.images[n].variant_key?i:-1).filter(i=>i>=0).pop();if(previous!==undefined)[d.images[n],d.images[previous]]=[d.images[previous],d.images[n]]}renderAux()});
    dialog.querySelectorAll('[data-upload-aux]').forEach(element=>element.onchange=async()=>{
      if(element.disabled||!element.files[0])return;
      const i=Number(element.dataset.uploadAux),form=new FormData();form.append('file',element.files[0]);form.append('sku_id',d.skus[i].sku_id);element.disabled=true;
      try{const r=await fetch('/api/commerce/media',{method:'POST',body:form}),asset=await r.json();if(!r.ok)throw new Error(asset.detail||'上传失败');data.media.push(asset);
        if(!editingCurrent()){notice('图片已上传到原规格，请重新打开原商品选择附图');return;}
        d.images.push({asset_id:asset.id,role:'detail',variant_key:d.skus[i].variant_key});renderAux();
      }catch(e){notice(e.message,true)}finally{element.disabled=false;element.value='';}
    });
    shippingUI.listingField(x.target_id,d.shipping_group).catch(error=>notice(error.message,true));
    dialog.querySelectorAll('[data-upload-sku]').forEach(element=>element.onchange=async()=>{
      const i=Number(element.dataset.uploadSku);if(element.disabled||!element.files[0])return;
      const form=new FormData();form.append('file',element.files[0]);form.append('sku_id',d.skus[i].sku_id);element.disabled=true;
      try{const response=await fetch('/api/commerce/media',{method:'POST',body:form});const asset=await response.json();if(!response.ok)throw new Error(asset.detail||'上传失败');data.media.push(asset);
        if(!editingCurrent()){notice('图片已上传到原规格，请重新打开原商品准备主图');return;}
        editForm.dispatchEvent(new CustomEvent('commerce-main-upload',{detail:{i,assetId:asset.id}}));
      }catch(error){notice(error.message,true)}finally{element.disabled=false;element.value='';}
    });
  }
  function mountMainImageTools(id,d){
    const form=dialog.querySelector('form');
    const originalDraft=JSON.stringify(d);
    const fieldSnapshot=()=>JSON.stringify([...new FormData(form).entries()].filter(([,value])=>typeof value==='string'));
    const originalFields=fieldSnapshot();
    const dirty=()=>JSON.stringify(d)!==originalDraft||fieldSnapshot()!==originalFields;
    for(const [i,sku] of d.skus.entries()){
      const box=document.createElement('div');box.className='c-main-image-tools';box.dataset.mainImageTools=String(i);
      box.innerHTML=`<details data-main-picker="${i}"><summary>${sku.images.some(v=>v.role==='main')?'更换主图（可选）':'选择主图'}</summary><div class="actions"><button type="button" data-main-prepare="${i}">选用此 SKU 设计主图</button><button type="button" data-main-refresh="${i}">查看已有图片</button></div><p class="c-hint">从本 SKU 已完成的设计主图或已绑定的上传图片中选择；需要新图时，请到设计工作台生成。</p><div data-main-candidates="${i}"></div></details>`;
      dialog.querySelector(`[data-sku-images="${i}"]`).after(box);
    }
    async function refresh(){
      const report=await api(`/api/commerce/listings/${id}/main-images`);
      if(dialog.querySelector('form')!==form)return;
      for(const [i,sku] of d.skus.entries()){
        const list=report.skus.find(v=>v.sku_id===sku.sku_id)?.candidates||[];
        const target=box=>box.querySelector(`[data-main-candidates="${i}"]`);
        target(form).innerHTML=list.length?`<div class="c-images">${list.map(v=>{const reason=v.assessment?.reason||v.screen?.reason||'尚未检查';const ready=v.screen?.eligible&&(v.screen?.generation_protocol==='generated-assets-v1'||v.screen?.source_bound||v.assessment?.decision==='eligible');return `<figure><img ${window.designImage(v.url,320)} alt="${E(sku.seller_sku)} 候选主图"><figcaption>${E(v.source)} · ${E(reason)}</figcaption>${ready?`<button type="button" data-main-choose="${i}" data-asset-id="${v.asset_id}">使用此图</button>`:''}</figure>`}).join('')}</div>`:'<p class="c-hint">此 SKU 暂无可选主图，请先完成设计或上传本款图片。</p>';
      }
    }
    async function startJob(i,assetId=null){
      if(dirty()){notice('先保存本页修改，再选择主图，避免覆盖未保存内容',true);return}
      const button=form.querySelector(`[data-main-prepare="${i}"]`);
      button.disabled=true;
      try{
        const sku=d.skus[i];
        const payload={sku_id:sku.sku_id};if(assetId)payload.candidate_asset_id=assetId;
        const job=await call(`/listings/${id}/main-images/prepare`,payload);
        notice('正在选择本 SKU 已完成的主图');
        let checks=0;
        const watch=async()=>{
          if(!dialog.open||dialog.querySelector('form')!==form)return;
          try{
            const current=await api(`/api/commerce/jobs/${job.id}`);
            if(['queued','running','retry_wait'].includes(current.status)){
              if(++checks<150)setTimeout(watch,4000);
              else{button.disabled=false;notice('主图任务仍在后台处理，请稍后查看已有图片；本次任务不会重复创建',true);}
              return;
            }
            button.disabled=false;
            if(current.status==='completed'){
              await reload();
              if(dialog.open&&dialog.querySelector('form')===form)await listingDialog(id);
              const item=(current.result?.skus||[]).find(v=>v.sku_id===sku.sku_id);
              notice(item?.status==='attached'?'主图已自动绑定':item?.reason||'主图任务已完成，仍缺合适图片',item?.status!=='attached');
            }else notice(current.error||'主图任务未完成',true);
          }catch(error){button.disabled=false;notice(error.message,true)}
        };
        setTimeout(watch,4000);
      }catch(error){button.disabled=false;notice(error.message,true)}
    }
    form.addEventListener('commerce-main-upload',event=>{
      if(dialog.querySelector('form')!==form)return;
      startJob(event.detail.i,event.detail.assetId).catch(error=>notice(error.message,true));
    },{once:false});
    form.addEventListener('click',async event=>{
      const choose=event.target.closest('[data-main-choose]');
      if(choose){const i=Number(choose.dataset.mainChoose),assetId=Number(choose.dataset.assetId),asset=find('media',assetId);d.skus[i].images=[{asset_id:assetId,role:'main'}];dialog.querySelector(`[data-sku-images="${i}"]`).innerHTML=asset?`<img ${window.designImage(asset.url,320)} alt="SKU 主图">`:'';notice('已选为主图，保存资料后生效');return}
      const refreshButton=event.target.closest('[data-main-refresh]');
      if(refreshButton){try{await refresh()}catch(error){notice(error.message,true)}return}
      const button=event.target.closest('[data-main-prepare]');
      if(!button)return;
      await startJob(Number(button.dataset.mainPrepare));
    });
  }
  async function schemaFields(id){const listing=find('listing',id);await archiveUI.handle('archive-fields',listing.product_id,listing.target_id)}

  function platformLinks(x){if(x.environment!=='production')return '';const observed=x.platform_observed||{};const asins=new Map();for(const [sku,v] of Object.entries(observed)){if(/^[A-Z0-9]{10}$/.test(v.confirmed_asin||''))asins.set(v.confirmed_asin,sku);for(const summary of v.response?.body?.summaries||[])if(/^[A-Z0-9]{10}$/.test(summary.asin||''))asins.set(summary.asin,sku)}return [...asins].map(([asin,sku])=>`<a href="https://www.amazon.com/dp/${asin}" target="_blank" rel="noopener noreferrer">${E(sku)} · 已确认 ASIN ${E(asin)}</a>`).join(' ')}
  const jobNames={listing_workflow:'推进商品同步流程',import_classification:'查询商品类目',detail_submit:'提交图文详情',detail_check:'回查图文详情',main_image:'准备平台主图',aftersale_sync:'刷新售后',aftersale_submit:'提交售后处理',aftersale_check:'核对售后结果',aftersale_import:'导入平台售后',business_mapping:'业务分类映射',materials:'生成资料',submit:'平台提交',refresh:'回查商品',sync:'平台同步',shipment:'发货回填',availability:'库存/暂停更新'};
  function jobSummary(job){
    const text=job.error||(job.kind==='listing_workflow'?job.result?.message:'');
    if(text)return String(text).length>240?String(text).slice(0,240)+'…（完整原因见详情）':String(text);
    if(job.status!=='completed')return label(job.status);
    if(job.kind==='submit'&&job.payload?.preview)return '当前资料的平台检查已完成；本次没有发布商品';
    if(job.kind==='refresh')return '平台回查结果已保存；实际购买情况需在买家页核实';
    if(job.kind==='submit')return '本次提交处理已结束；平台接收与买家可购买分别核实';
    return '结果已保存，可查看详情';
  }
  async function jobHistory(before=null){
    const current=guardDialog();
    const result=await api('/api/commerce/job-history'+(before?'?before='+encodeURIComponent(before):''));
    if(!current())return;
    show('历史后台任务',`<p>完整任务 ${result.total} 条，本页 ${result.jobs.length} 条。普通成功轮询保留最近 ${result.retention?.ordinary_poll_days||7} 天，较早的按实际状态变化汇总；正式写入、错误及未明确结果的必要记录保留。</p>`+table(['任务','状态','原始记录'],result.jobs.map(job=>[`${E(jobNames[job.kind]||job.kind)} · ${job.id}`,badge(job.status),button('job-details',job.id,'查看完整记录')]))+(result.next_before?'<button type="button" data-job-history-next>更早的任务</button>':'<p>已到最早完整记录</p>')+((result.changes||[]).length?'<h3>较早轮询的状态变化</h3>'+table(['商品','首次 / 最近观测','合并观测次数','规格状态'],result.changes.map(change=>[E(find('listing',change.listing_id)?.name||change.listing_id),E(change.first_seen+' / '+change.last_seen),E(change.observations),(change.states||[]).map(state=>E(state.sku)+' '+badge(state.status)).join('<br>')])):'')+(result.changes_next_before?'<button type="button" data-poll-history-next>更早的变化摘要</button>':''));
    const earlier=dialog.querySelector('[data-poll-history-next]');
    if(earlier)earlier.onclick=()=>{earlier.disabled=true;jobHistory(result.changes_next_before).catch(error=>{earlier.disabled=false;notice(error.message,true)})};
    const next=dialog.querySelector('[data-job-history-next]');
    if(next)next.onclick=()=>{next.disabled=true;jobHistory(result.next_before).catch(error=>{next.disabled=false;notice(error.message,true)})};
  }
  async function dispatch(action,id){
    if(action==='notice-history')return show('历史通知',`<p class="c-hint">通知保留当时发生的状态，不能代替当前商品资料与平台结果。当前进度请到对应商品查看。</p>`+table(['原始通知','操作'],(data.notice||[]).filter(n=>!n.read).map(n=>[E(n.title),(n.support_thread_id?button('support-detail',n.support_thread_id,'查看客户消息'):'')+button('notice-read',n.id,'已查看')])));
    if(action==='job-history')return jobHistory();
    if(action==='job-details'){
      const current=guardDialog();
      const job=await api('/api/commerce/jobs/'+encodeURIComponent(id));
      if(!current())return;
      show((jobNames[job.kind]||job.kind)+' · '+job.id,`<p>${E(jobSummary(job))}</p><details><summary>请求、结果与错误记录</summary><pre>${E(JSON.stringify(job,null,2))}</pre></details>`);
      return;
    }
    if(action.startsWith('support-')){await supportUI.dispatch(action,id);return;}
    if(['detail-check','detail-render','listing-continue'].includes(action)){
      const originForm=dialog.open?dialog.querySelector('form'):null;
      await call(action==='listing-continue'?`/listings/${id}/continue`:`/listings/${id}/detail-publication/${action==='detail-check'?'check':'render'}`);
      await reload();const box=originForm&&dialog.open&&dialog.querySelector('form')===originForm&&originForm.querySelector('.detail-publication-summary');if(box)box.innerHTML=listingMedia.publication(find('listing',id)?.detail_publication,id);notice(action==='listing-continue'?'已继续完成本款上架，沿用已提交商品及现有详情进度':'详情处理任务已提交，结果以平台回查为准');return;
    }
    if(action==='aftersales-sync'){await aftersaleUI.refresh();return}
    if(action==='aftersales-detail'){await aftersaleUI.details(id);return}
    if(action==='aftersales-process'){await aftersaleUI.process(id);return}
    if(['aftersales-submit','aftersales-check','aftersales-cancel','aftersales-refresh-case'].includes(action)){await aftersaleUI.act(action,id);return}
    if(action.startsWith('category-')){await categoryUI.dispatch(action,id);return}
    if(!data)await reload();
    if(action==='refresh'){await reload();return}
    if(action==='orders-sync-selected'){await orderUI.refreshOrders();return}
    if(action==='order-logistics'){await orderUI.details(id);return}
    if(action==='order-package-new'){await orderUI.newPackage(id);return}
    if(action.startsWith('order-ship-')){await orderUI.shipAction(action,id);return}
    if(action==='target-shipping'){await shippingUI.open(id);return}
    if(action==='settings'){if(window.workspaceRole==='operator')throw new Error('经营设置需要管理员账号');settings();return}
    if(action==='import-product'){dialog.close();openPage('supplier_imports');return}
    if(await archiveUI.handle(action,id))return;
    if(action==='generate'){await targetUI.generate(id);return}
    if(action==='batch-publish'){await targetUI.publish();return}
    if(action==='target-custom'){customTargetDialog();return}
    if(action==='product-edit'||action==='sku-edit'){await editCatalog(action==='product-edit'?'product':'sku',id);return}
    if(action==='targets'){show('上架目标与授权',`<div class="c-toolbar">${button('target-new',0,'账号与地区多选')}${button('target-custom',0,'添加自定义地区')}</div>`+table(['账号 / 站点','环境 / 连接','操作'],data.target.map(t=>[`${E(t.account_name)} · ${E(t.name)}<small>资料语言：${E(t.language_name||t.language)}</small>`,`${badge(t.environment)} · ${t.environment==='production'?`${t.verified?'站点已连通':'未验证'} · ${t.enabled?'启用':'停用'}`:'历史测试记录 · 已停止执行'}`,actions(button('target-integration',t.id,'接入检查'),...(t.environment==='production'?[button('target-edit',t.id,'设置'),button('target-shipping',t.id,'配送模板'),button('target-credentials',t.id,'配置授权'),button('target-connect',t.id,'验证连接'),button('target-orders',t.id,'同步订单'),button('target-finance',t.id,'同步交易')]:[]))])));return}
    if(action==='target-integration'){const current=guardDialog();const report=await api(`/api/commerce/targets/${id}/integration`);if(!current())return;show('平台接入检查',`<p>${E(report.account_name)} · ${E(report.platform)}</p><p>${E(report.endpoint||'尚未配置真实接口目标')}</p><p>${E(report.message)}</p>`+table(['参数 / 条件','配置状态','说明'],report.checks.map(c=>[E(c.title),c.status==='configured'?'已配置':'待补充',E(c.message)]))+table(['业务能力','当前状态'],Object.entries(report.capabilities).map(([k,v])=>[E(({listing:'商品上架',orders:'订单与物流',detail:'图文详情',aftersales:'售后处理',support:'客服消息'})[k]||k),E(v)])));return}
    if(action==='target-edit'){const x=find('target',id);show('上架目标设置',`<div class="c-grid">${input('name','站点名称',x.name)}${select('language','上架资料语言',targetLanguageValue(x),languageOptions())}${select('enabled','生成资料与平台写入',String(x.enabled),[['true','启用'],['false','停用']])}${select('auto_sync_orders','每15分钟同步正式订单',String(!!x.auto_sync_orders),[['false','关闭'],['true','开启']])}${select('include_recipient','请求收件资料（需要相应权限）',String(!!x.include_recipient),[['false','不请求'],['true','请求']])}${input('orders_since','首次同步起点（含时区；留空近两年）',x.orders_since||'')}</div>`,f=>call('/targets',{...x,name:f.get('name'),language:f.get('language'),language_mode:f.get('language')?'explicit':'marketplace',orders_since:f.get('orders_since'),enabled:f.get('enabled')==='true',auto_sync_orders:f.get('auto_sync_orders')==='true',include_recipient:f.get('include_recipient')==='true'}));return}
    if(action==='target-new'){await targetUI.configure();return}
    if(action==='target-credentials'){show('Amazon授权配置',`<div class="c-grid">${input('client_id','应用Client ID')}${input('client_secret','应用密钥','','password','autocomplete="new-password"')}${input('refresh_token','Refresh Token','','password','autocomplete="new-password"')}</div><p class="c-hint">仅保存后端。留空表示保留已有值，保存后点击验证连接。</p>`,f=>call(`/targets/${id}/credentials`,Object.fromEntries(f)));return}
    if(action.startsWith('target-')&&['connect','orders','finance'].includes(action.slice(7))){await call(`/targets/${id}/sync`,{scope:action.slice(7)});notice('同步任务已提交');await reload();return}
    if(action==='sync-orders'||action==='sync-finance'){show('选择同步目标',select('id','上架目标','',data.target.map(x=>[x.id,`${x.account_name} · ${x.name} · ${label(x.environment)}`])),f=>call(`/targets/${f.get('id')}/sync`,{scope:action==='sync-orders'?'orders':'finance'}),'开始同步');return}
    if(action==='listing-edit'){await listingDialog(id);return}
    if(action==='schema-load'){await schemaFields(id);return}

    if(action==='listing-readiness'){
      const current=guardDialog(),r=await api(`/api/commerce/listings/${id}/release-readiness`);if(!current())return;
      const t=r.shipping_check;
      const rates=t?`<p>已核对模板：${E(t.template_name)} · 国际地区选项 ${E(t.international_options??'未记录')} 个</p><p class="c-hint">模板配置核对记录；承运报价和买家端购买验证分别记录。</p>${table(['配送区域','每件运费','运输时效'],t.regions.map(x=>[E(x.label||x.destinations.join('、')),E(x.per_item_fee+' '+t.currency),E(x.transit||'按模板')]))}<details><summary>查看本模板已选目的地</summary>${t.regions.map(x=>`<p><b>${E(x.label||'配送地区')}</b>：${E(x.destinations.join('、'))}</p>`).join('')}</details>`:'<p>尚无本店收费配置核对记录。配送模板是否可选以平台预检为准。</p>';
      show('上架基础核对',`<p>${E(r.message)}</p><p>当前状态：${E(label(r.status))} · 刊登批次 ${E(r.publication_cycle_id||'原批次')}</p><p>${E(r.packaging_rule)}</p><p>计价：${E(({separate:'商品价不含运费，结账按模板收费',included:'商品价已包含运费',unspecified:'尚未明确'})[r.price_shipping_mode])} · 备货 ${E(r.handling_days??'待填')} 天</p>${table(['销售SKU','商品价','币种'],r.items.map(x=>[E(x.seller_sku),E(x.item_price??'待填'),E(x.currency||'待填')]))}${rates}<p class="c-hint">${E(r.buyer_acceptance)}</p>`);return;
    }
    if(action==='listing-prepare-relisting'){
      const current=guardDialog(),x=await fullListing(id);if(!current())return;
      show('准备重新刊登草稿',`<p>此操作只准备本地草稿，保留原资料及历史记录，不向亚马逊提交商品。原销售SKU及父体需先核实删除。</p>${select('sku_strategy','卖家SKU处理','retain',[['retain','核实删除后沿用卖家SKU，使用新刊登批次'],['new','为新批次生成新的卖家SKU']])}<p class="c-hint">ASIN由平台按实际目录匹配或分配；更换卖家SKU不保证产生新ASIN。自动上架保持原设置。</p>`,async f=>{await call(`/listings/${id}/prepare-relisting`,{revision:x.revision,sku_strategy:f.get('sku_strategy')});return '已准备本地重新刊登草稿，尚未向亚马逊提交';},'准备本地草稿');return;
    }
    if(['listing-submit','listing-preview'].includes(action)){
      const current=guardDialog();
      const fresh=await fullListing(id);
      if(!current())return;
      if(fresh.issues?.length){await listingDialog(id,true);return;}
      const job=await call(`/listings/${id}/${action.slice(8)}`);
      await reload();
      if(current())await listingJobProgress(id,job,action);
      else notice('原商品的操作已接收，可从该商品查看处理进度');
      return;
    }
    if(['listing-refresh','listing-reconcile','listing-pause','listing-resume'].includes(action)){
      await call(`/listings/${id}/${action.slice(8)}`);await reload();
      notice('平台任务已提交，处理结果在经营概览显示');
      return;
    }
    if(action==='expense'){const o=find('order',id);show('登记实际费用',`<div class="c-grid">${select('type','费用类别','shipping',[['shipping','运费'],['packaging','包装'],['platform_fee','平台费用'],['advertising','广告'],['production','额外生产费']])}${input('amount','金额',0,'number','min="0" step="0.01"')}${input('currency','币种',o.currency)}${input('reference','凭据 / 说明')}</div>`,f=>call('/expenses',{...Object.fromEntries(f),order_id:id,request_id:crypto.randomUUID()}));return}
    if(action==='transaction-reconcile'){const x=find('transaction',id),orders=data.order.filter(o=>o.target_id===x.target_id);show('核对平台交易',`<p>平台净额 ${E(x.raw.totalAmount?.currencyAmount)} ${E(x.raw.totalAmount?.currencyCode)}</p><div class="c-grid">${select('order_id','关联订单',x.order_id||'',orders.map(o=>[o.id,o.external_id]))}${select('category','本次核对类别',x.category||'settlement',[['settlement','结算净额核对（不重复录收入）'],['platform_fee','平台费用'],['refund','退款'],['other','其他核对记录']])}${input('amount','其中费用或退款金额；结算核对无需填写',0,'number','min="0" step="0.01"')}${select('expense_id','已录费用（有则关联，避免重复）',x.expense_id||'',[['','未录过费用'],...data.expense.filter(e=>orders.some(o=>o.id===e.order_id)).map(e=>[e.id,`订单${e.order_id} ${e.type} ${e.amount} ${e.currency}`])])}${textarea('note','核对依据',x.note||'')}</div>`,f=>call(`/transactions/${id}/reconcile`,{...Object.fromEntries(f),revision:x.revision,order_id:Number(f.get('order_id')),expense_id:f.get('expense_id')?Number(f.get('expense_id')):null}));return}
    if(action==='report-new'){show('申请退货报表',`<div class="c-grid">${select('target_id','上架目标','',options('target'))}${input('start','开始时间（包含时区）',new Date(Date.now()-7*86400000).toISOString())}${input('end','结束时间（包含时区）',new Date().toISOString())}</div>`,f=>call('/reports',{...Object.fromEntries(f),target_id:Number(f.get('target_id'))}),'申请报表');return}
    if(action==='report-discover'){show('查询最近结算报表',select('target_id','上架目标','',options('target')),f=>call('/reports/discover',{target_id:Number(f.get('target_id'))}),'查询');return}
    if(action==='report-fetch'||action==='report-reconcile'){await call(`/reports/${id}/${action.slice(7)}`);notice('报表任务已提交');await reload();return}
    if(action==='campaign-new'){show('创建活动计划',`<div class="c-grid">${input('name','活动名称')}${select('listing_id','平台商品','',options('listing'))}${select('type','活动类型','coupon',[['coupon','优惠券'],['sale','限时价格'],['deal','平台活动']])}${input('starts_at','开始时间（含站点时区）')}${input('ends_at','结束时间（含站点时区）')}${input('budget','预算',0,'number')}${textarea('notes','活动内容 / 办理说明')}</div>`,f=>call('/campaigns',{...Object.fromEntries(f),listing_id:Number(f.get('listing_id')),request_id:crypto.randomUUID()}));return}
    if(action==='campaign-record'){show('登记活动结果',`<div class="c-grid">${select('status','平台当前状态','active',[['active','已开始'],['cancelled','已撤销'],['completed','已结束']])}${input('platform_reference','平台活动ID / 办理依据')}</div>`,f=>call(`/campaigns/${id}/record`,Object.fromEntries(f)));return}
    if(action==='rule-new'){show('长期无销量观察',`<div class="c-grid">${select('target_id','上架目标','',options('target'))}${input('days','观察天数',90,'number','min="1"')}${select('action','满足完整观察条件后','suggest',[['suggest','提醒检查'],['pause','自动暂停销售']])}${select('enabled','规则状态','false',[['false','停用'],['true','启用观察建议']])}</div><p class="c-hint">数据不足不会判定无销量。建议显示在经营待办，可在平台上架页暂停相应商品。</p>`,f=>call('/rules',{target_id:Number(f.get('target_id')),days:Number(f.get('days')),action:f.get('action'),enabled:f.get('enabled')==='true'}));return}
    if(action==='rules-evaluate'){const r=await call('/rules/evaluate');notice(r.some(x=>x.status==='insufficient_data')?'部分规则观察期数据不足':'已完成运营观察');await reload();return}
    if(action==='notice-read'){await call(`/notices/${id}/read`);await reload();return}
    if(action==='job-retry'){await call(`/jobs/${id}/retry`);await reload();return}
    throw Error('此操作暂不可用，请刷新页面后重试');
  }
  async function listingJobProgress(id,initial,action){
    show(action==='listing-preview'?'平台验证进度':'商品上架处理进度','<div data-listing-job-progress aria-live="polite"></div>');
    const box=dialog.querySelector('[data-listing-job-progress]');let checks=0;
    const current=()=>dialog.open&&dialog.querySelector('[data-listing-job-progress]')===box;
    async function update(job){
      if(!current())return;
      try{
        if(!job&&initial?.id)job=await api(`/api/commerce/jobs/${initial.id}`);
        if(!current())return;
        const listing=find('listing',id)||{},detail=listing.detail_publication||{},progress=listing.publication_progress||{};
        box.innerHTML=`<p><b>${E(job?label(job.status):progress.label||'已接收本款操作')}</b></p>${job?.error?`<p class="c-error">${E(job.error)}</p>`:''}<p>${E(progress.label||listing.status||'等待平台结果')}</p><p>${E(detail.label||'详情状态以本款处理结果为准')}</p>${button('listing-edit',id,'查看本款资料')}${job?.id?button('job-details',job.id,'查看本次处理记录'):''}<p class="c-hint">提交接收、平台销售和详情生效分别记录。关闭此窗口不会取消已接收的任务。</p>`;
        const active=['queued','running','retry_wait'].includes(job?.status)||detail.own_steps_pending;
        if(active&&++checks<180)setTimeout(async()=>{if(current()){await reload().catch(()=>{});await update();}},5000);
        else if(active)box.insertAdjacentHTML('beforeend','<p>任务仍在后台处理，请稍后从本款资料查看结果。已停止本窗口的自动查询。</p>');
      }catch(error){if(current()){box.textContent='读取处理进度失败：'+error.message;notice(error.message,true);}}
    }
    await update(initial?.id?initial:null);
  }
  const pendingActions=new Map();
  document.addEventListener('click',event=>{
    const b=event.target.closest('[data-c-action]');if(!b)return;event.preventDefault();
    const id=Number(b.dataset.cId),key=b.dataset.cAction+':'+id;
    if(pendingActions.has(key))return pendingActions.get(key);
    const form=dialog.querySelector('form');b.disabled=true;b.setAttribute?.('aria-busy','true');
    const task=dispatch(b.dataset.cAction,id).catch(e=>{notice(e.message,true);if(dialog.open&&dialog.querySelector('form')===form){let box=dialog.querySelector('.c-error');if(!box){box=document.createElement('p');box.className='c-error';dialog.querySelector('.c-body').prepend(box)}box.textContent=e.message}}).finally(()=>{pendingActions.delete(key);b.disabled=false;b.removeAttribute?.('aria-busy');});
    pendingActions.set(key,task);return task;
  });
  window.commerceGenerate=async pid=>{try{await dispatch('generate',pid)}catch(error){notice(error.message,true)}};
  const accountAuth=window.createPlatformAuthorization({E,input,select,table,show,call,api,dialog,guardDialog,refresh:()=>load()});
  window.commerceAuthorization=async id=>{try{await accountAuth.open(id)}catch(error){notice(error.message,true)}};
  window.commerceTarget=async aid=>{try{await targetUI.configure(aid)}catch(error){notice(error.message,true)}};
  window.commerceSyncTargets=async()=>{try{await targetUI.sync()}catch(error){notice(error.message,true)}};
  window.commerceLanguages=async accountId=>{const current=guardDialog();try{await reload();if(!current())return;show('店铺地区 · 上架资料语言',table(['店铺 / 地区','当前语言','操作'],data.target.filter(t=>t.account_id===accountId&&t.environment==='production').map(t=>[`${E(t.account_name)} · ${E(t.name)} · ${E(label(t.environment))}`,E(t.language_name||t.language),button('target-edit',t.id,'设置语言')]))+'<p class="c-hint">各店铺地区分别配置，修改后重新生成对应目标的上架资料。</p>')}catch(error){notice(error.message,true)}};
  window.commerceShipping=async id=>{try{await shippingUI.account(id)}catch(error){notice(error.message,true)}};
  window.commerceEditProductArchive=async id=>{
    const productId=Number(id),current=guardDialog();
    if(!Number.isSafeInteger(productId)||productId<1)throw Error('商品档案标识无效');
    await reload();if(!current())return;openPage('commerce_products');
    if(loading)await loading;
    if(!find('product',productId))throw Error('商品档案不存在或当前账号无法访问');
    await editCatalog('product',productId);
  };
  window.commerceOpen=openPage;
  async function pollOverview(){
    // Passive polling must never queue another full snapshot behind a slow read
    // or compete with a form submission. Explicit post-write reloads still do.
    if(document.hidden||loading||checkingRevision||dialog.open||!page.startsWith('commerce_')||page==='commerce_support')return;
    const busy=(data?.jobs||[]).some(job=>['queued','running','retry_wait'].includes(job.status));
    const interval=pollFailures?Math.min(300000,15000*2**Math.min(pollFailures,5)):(page==='commerce_support'?15000:busy?10000:30000);
    if(Date.now()-lastRefreshAt<interval)return;
    checkingRevision=true;
    const previousFailures=pollFailures;
    try{
      if(!data?.snapshot_revision){await reload();return;}
      const probe=await api('/api/commerce/overview/revision');
      if(probe.revision!==data?.snapshot_revision)await reload();
      else{lastRefreshAt=Date.now();pollFailures=0;}
    }catch{lastRefreshAt=Date.now();if(pollFailures===previousFailures)pollFailures++;}
    finally{checkingRevision=false;}
  }
  setInterval(pollOverview,1000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)pollOverview();});
})();
