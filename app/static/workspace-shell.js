/* Local navigation, accessible workspaces and form sections. No business side effects. */
(() => {
  'use strict';
  const $=selector=>document.querySelector(selector);
  const escape=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const icon=name=>window.WorkspaceUI.icon(name);
  const meta={commerce_home:['运营指挥台','YOUR OPERATIONS, IN FOCUS','home','先看当前进展和待办，再进入对应的工作空间。','首页数量来自当前业务记录；平台提交与买家可购买分别查看。'],supplier_imports:['导入与整理','START WITH REAL PRODUCTS','upload','上传原始货盘，核对字段和各规格，再确认入库。','原表、图片和来源说明可在导入记录中查看。'],commerce_products:['商品库','YOUR PRODUCT COLLECTION','box','点击“维护商品档案”修改共用信息，展开规格维护各款差异。','原图、规格、采购价和销售价都有独立来源；不要把默认数量当作仓库实物。'],design_projects:['AI 设计工坊','DESIGN A REASON TO BUY','spark','先选实际规格与原图，再按母稿、独立方案、销售与详情图推进。','完成后先查看成品；重新设计和清理位于更多操作中。'],commerce_listings:['上架工作台','FROM DESIGN TO STOREFRONT','store','按商品查看资料、当前平台检查、已提交版本与买家显示。','编辑资料里的各个分区保留在同一份表单中，最后统一保存。'],commerce_orders:['订单与发货','DELIVER EVERY PROMISE','order','按店铺同步订单，跟进备货和发货，再回填真实物流。','发布可售量、仓库实物和已下订单是不同的记录。'],commerce_aftersales:['售后处理','TAKE CARE OF THE FOLLOW THROUGH','undo','查看每笔售后原因，核对原订单与平台办理结果。','退款、退货和消息记录沿用现有业务流程。'],commerce_support:['客户消息','KEEP THE CONVERSATION CONNECTED','chat','先查看客户消息与对应订单，再决定使用 AI 建议或人工处理。','收件和发送能力以各平台实际接入状态为准。'],commerce_operations:['运营计划','TURN PLANS INTO PROGRESS','chart','集中管理活动计划、观察规则和平台处理记录。','平台执行与本地计划分别记录，按实际回执跟进。'],commerce_finance:['财务与报表','KNOW THE NUMBERS BEHIND THE WORK','chart','按币种核对订单、平台交易、费用和报表。','结算释放、估算利润和银行到账分别确认。'],platform_accounts:['店铺与平台','CONNECT YOUR CHANNELS','link','维护账号、店铺站点、授权和同步状态。','启用店铺定义后续自动流程的目标范围。'],businesses:['业务配置','THE FOUNDATION OF YOUR WORK','settings','维护业务范围、商品方向和可复用的运营规则。','已有业务配置会继续用于商品与设计流程。'],models:['模型管理','YOUR AI CAPABILITIES','spark','维护文本、图片通道和连接配置，在方案里安排主备顺序。','已保存的调用顺序继续生效，界面调整不改变模型路由。'],plans:['方案配置','BUILD REPEATABLE WORKFLOWS','layers','按文本或图片任务选择模型，并调整主模型与备用顺序。','规则供不同商品复用，商品事实从各自档案读取。'],agents:['Agent 管理','GIVE EVERY ROLE A CLEAR JOB','settings','配置岗位职责、执行方案与业务范围。','保持职责清楚，避免重复安排同一项工作。']};
  const role=()=>document.body.dataset.workspaceRole||'admin';
  const allowed=key=>role()!=='operator'||key.startsWith('commerce_');
  function go(destination,query='') {
    if(!meta[destination]||!allowed(destination))return;
    if(typeof window.commerceOpen==='function')window.commerceOpen(destination);
    else document.querySelector(`.nav-item[data-page="${destination}"]`)?.click();
    if(query){$('#search').value=query;window.render();}
    $('#content')?.focus({preventScroll:true});
  }
  function sync() {
    const key=typeof page==='string'?page:document.body.dataset.initialPage;
    const m=meta[key];if(!m)return;
    document.body.dataset.workspacePage=key;
    $('#workspace-location').textContent=m[0];$('#workspace-eyebrow').textContent=m[1];
    $('#title').textContent=m[0];document.title=m[0]+' · 织造台';
    const addIcon=document.querySelector('#add>b');if(addIcon)addIcon.textContent=key==='commerce_home'||key==='commerce_orders'||key==='commerce_aftersales'?'↻':key==='supplier_imports'||key==='commerce_products'?'↑':key==='commerce_listings'?'⚙':'＋';
    const home=key==='commerce_home';
    document.querySelector('.panel').classList.toggle('ws-home-panel',home);
    $('#search').placeholder=home?'搜索待办或最近动态':$('#search').placeholder;
    $('#search').closest('label').setAttribute('aria-label',home?'搜索工作台':'搜索当前工作区');
    if(key==='supplier_imports'){$('#subtitle').textContent='把供应商货盘整理成清楚、可追溯的商品资料。';$('#panelHint').textContent='先看导入进度，再核对原表、图片与实际规格';}
    if(key==='platform_accounts')$('#panelHint').textContent='账号连接、站点绑定与实际同步结果集中管理';
    for(const node of document.querySelectorAll('[data-nav-icon]'))node.innerHTML=icon(meta[node.dataset.navIcon]?.[2]||'box');
  }
  const previousRender=render;
  render=function(){
    const blocked=typeof renderWorkspaceReadState==='function'&&renderWorkspaceReadState();
    if(!blocked)previousRender();
    sync();
  };window.render=render;
  const command=document.createElement('dialog');command.id='workspace-command-dialog';command.className='ws-command-dialog';
  command.innerHTML=`<div class="ws-command-head">${icon('search')}<input type="search" aria-label="搜索页面或商品" placeholder="搜索页面、商品，或输入你要做的事…" autocomplete="off"><button type="button" data-command-close aria-label="关闭快速前往">Esc</button></div><div class="ws-command-results" aria-live="polite"></div><div class="ws-command-foot"><span>↑ ↓ 选择 · Enter 打开</span><span>只查找，不执行业务操作</span></div>`;
  document.body.append(command);
  const search=command.querySelector('input'),results=command.querySelector('.ws-command-results');
  let previousFocus=null,selected=0;
  const searchWords={supplier_imports:'excel 表格 上传 导入 货盘',commerce_products:'商品 产品 规格 sku 原图 价格 档案',design_projects:'图片 设计 母稿 方案 AI',commerce_listings:'上架 发布 标题 文案 详情 平台',commerce_orders:'订单 发货 物流',platform_accounts:'店铺 账号 亚马逊 amazon 授权'};
  function select(index){const choices=[...results.querySelectorAll('button')];if(!choices.length)return;selected=(index+choices.length)%choices.length;choices.forEach((n,i)=>n.classList.toggle('is-selected',i===selected));choices[selected].scrollIntoView({block:'nearest'});}
  function drawResults(){
    const q=search.value.trim().toLowerCase();selected=0;
    const pages=Object.entries(meta).filter(([key,m])=>allowed(key)&&(!q||`${m[0]} ${searchWords[key]||''}`.toLowerCase().includes(q))).slice(0,q?8:6);
    const products=q&&typeof window.workspaceSearchProducts==='function'?window.workspaceSearchProducts(q).slice(0,6):[];
    results.innerHTML=(pages.length?'<p class="ws-command-group">工作空间</p>'+pages.map(([key,m])=>`<button type="button" data-workspace-go="${key}">${icon(m[2])}<span>${escape(m[0])}<small>${escape(m[3])}</small></span><span>↵</span></button>`).join(''):'')+(products.length?'<p class="ws-command-group">当前商品</p>'+products.map(p=>`<button type="button" data-workspace-go="commerce_products" data-workspace-query="${escape(p.name)}">${icon('box')}<span>${escape(p.name)}<small>${escape(p.subtitle)}</small></span><span>↵</span></button>`).join(''):'')||'<div class="ws-command-empty">没有找到匹配的页面或商品，试试“图片”“订单”或商品名称。</div>';
    select(0);
  }
  function openCommand(){if(document.querySelector('dialog[open]'))return;previousFocus=document.activeElement;search.value='';drawResults();command.showModal();search.focus();}
  command.querySelector('[data-command-close]').onclick=()=>command.close();command.addEventListener('close',()=>previousFocus?.focus?.());
  search.addEventListener('input',drawResults);
  search.addEventListener('keydown',event=>{if(['ArrowDown','ArrowUp','Enter'].includes(event.key)){event.preventDefault();if(event.key==='Enter')results.querySelectorAll('button')[selected]?.click();else select(selected+(event.key==='ArrowDown'?1:-1));}});
  document.addEventListener('click',event=>{
    if(event.target.closest('[data-command-open]')){openCommand();return;}
    const control=event.target.closest('[data-workspace-go]');if(control){if(command.open)command.close();go(control.dataset.workspaceGo,control.dataset.workspaceQuery||'');return;}
    if(event.target.closest('[data-workspace-help]'))openHelp();
  });
  const help=document.createElement('dialog');help.className='ws-help-dialog';document.body.append(help);
  function openHelp(){
    if(document.querySelector('dialog[open]'))return;
    const m=meta[typeof page==='string'?page:'commerce_home']||meta.commerce_home;
    help.innerHTML=`<div class="dialog-head"><div><p class="eyebrow">WORKSPACE GUIDE</p><h2>${escape(m[0])} · 使用指引</h2></div><button type="button" class="icon-btn" aria-label="关闭使用指引">×</button></div><div class="ws-help-body"><span class="ws-help-icon">${icon(m[2])}</span><h3>在这里，你可以做什么</h3><p>${escape(m[3])}</p><p>${escape(m[4])}</p><div class="ws-help-shortcuts"><span><kbd>Ctrl K</kbd> 快速前往</span><span><kbd>/</kbd> 搜索当前页</span><span><kbd>Esc</kbd> 关闭窗口</span></div></div>`;
    help.querySelector('button').onclick=()=>help.close();help.showModal();
  }
  document.addEventListener('keydown',event=>{
    const editing=event.target.closest('input,textarea,select,[contenteditable="true"]');
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();openCommand();return;}
    if(!editing&&!document.querySelector('dialog[open]')&&event.key==='/'){event.preventDefault();$('#search').focus();}
    if(!editing&&!document.querySelector('dialog[open]')&&event.key==='?'){event.preventDefault();openHelp();}
  });
  let sectionSequence=0;
  function mountTabs(dialog,groups) {
    const body=dialog.querySelector('.c-body');if(!body||body.dataset.wsSections)return;
    const active=groups.filter(g=>g.nodes.length);if(active.length<2)return;
    body.dataset.wsSections='true';dialog.classList.add('ws-sectioned-dialog');
    const tablist=document.createElement('div');tablist.className='ws-editor-tabs';tablist.setAttribute('role','tablist');tablist.setAttribute('aria-label','编辑分区');
    const id='ws-editor-'+(++sectionSequence),buttons=[],panels=[];
    active.forEach((g,i)=>{
      const panel=document.createElement('section');panel.className='ws-editor-panel';panel.id=id+'-panel-'+i;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',id+'-tab-'+i);panel.tabIndex=0;
      const heading=document.createElement('div');heading.className='ws-editor-section-heading';heading.innerHTML=`<span>0${i+1}</span><div><h3>${escape(g.title)}</h3><p>${escape(g.hint)}</p></div>`;panel.append(heading);
      if(g.grid){const grid=document.createElement('div');grid.className='c-grid';grid.append(...g.nodes);panel.append(grid);}else panel.append(...g.nodes);
      const b=document.createElement('button');b.type='button';b.id=id+'-tab-'+i;b.setAttribute('role','tab');b.setAttribute('aria-controls',panel.id);b.innerHTML=`<span>0${i+1}</span>${escape(g.title)}`;tablist.append(b);buttons.push(b);panels.push(panel);
    });
    const switchTo=index=>{buttons.forEach((b,i)=>{b.setAttribute('aria-selected',String(index===i));b.tabIndex=index===i?0:-1;panels[i].hidden=index!==i;});body.scrollTop=0;};
    buttons.forEach((b,i)=>{b.onclick=()=>switchTo(i);b.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const n=e.key==='Home'?0:e.key==='End'?buttons.length-1:(i+(e.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;switchTo(n);buttons[n].focus();}};});
    body.replaceChildren(...panels);body.before(tablist);switchTo(0);
    dialog.querySelector('form')?.addEventListener('invalid',event=>{const index=panels.findIndex(p=>p.contains(event.target));if(index>=0)switchTo(index);},true);
  }
  function sectionListing(dialog){
    const body=dialog.querySelector('.c-body');if(!body||body.dataset.wsSections||!body.querySelector('.c-sku'))return;
    const groups=[{title:'基础与文案',hint:'系列共用信息与目标站点的销售文案。',nodes:[]},{title:'规格与售价',hint:'每个实际规格的标题、售价与数量单独维护。',nodes:[]},{title:'图片与详情',hint:'逐规格查看销售封面、图库顺序和完整详情。',nodes:[]},{title:'平台属性',hint:'根据商品档案与当前平台规则维护字段和规格关系。',nodes:[]},{title:'平台记录',hint:'当前资料、历史提交与平台返回各自保留。',nodes:[]}];
    for(const node of [...body.children]){
      if(node.matches('.c-sku')){
        const children=[...node.children],start=children.findIndex(n=>n.tagName==='H4');
        if(start>=0){
          const art=document.createElement('section');art.className='c-sku ws-sku-art';
          const heading=node.querySelector('h3')?.cloneNode(true)||document.createElement('h3');if(!heading.textContent)heading.textContent='本规格图片';
          art.append(heading,...children.slice(start));groups[2].nodes.push(art);
        }
      }
      const index=node.matches('.c-sku')?1:node.matches('.detail-publication-summary,.detail-material-summary')||node.querySelector('[data-presentation-preview]')?2:node.querySelector('[data-complete-fields]')?3:node.tagName==='DETAILS'&&node.querySelector('pre')?4:0;
      groups[index].nodes.push(node);
    }
    const skuSets=[groups[1].nodes.filter(n=>n.matches('.c-sku')),groups[2].nodes.filter(n=>n.matches('.ws-sku-art'))];
    if(skuSets[0].length>1){
      const selectors=[];
      const choose=index=>{skuSets.forEach(set=>set.forEach((n,i)=>n.hidden=i!==index));selectors.forEach(s=>s.value=String(index));};
      [1,2].forEach((groupIndex,setIndex)=>{
        const set=skuSets[setIndex];if(!set.length)return;
        const chooser=document.createElement('label');chooser.className='c-field ws-sku-switcher';
        const label=document.createElement('span');label.textContent='切换要查看或编辑的规格';
        const select=document.createElement('select');select.setAttribute('aria-label','当前规格');
        set.forEach((n,i)=>{const option=document.createElement('option');option.value=String(i);const heading=n.querySelector('h3');option.textContent=heading?[...heading.childNodes].filter(c=>c.nodeType===3).map(c=>c.textContent).join('').trim()||heading.textContent:'规格 '+(i+1);select.append(option);});
        select.onchange=()=>choose(Number(select.value));selectors.push(select);chooser.append(label,select);
        const rest=groups[groupIndex].nodes.filter(n=>!set.includes(n));groups[groupIndex].nodes=[chooser,...set,...rest];
      });
      choose(0);
      dialog.querySelector('form')?.addEventListener('invalid',event=>{const index=skuSets[0].findIndex(n=>n.contains(event.target));if(index>=0)choose(index);},true);
    }
    mountTabs(dialog,groups);
  }
  function sectionArchive(dialog,title){
    if(!['商品档案','实际 SKU 档案'].includes(title))return;
    const body=dialog.querySelector('.c-body'),grid=body?.querySelector(':scope > .c-grid');if(!grid)return;
    const groups=[{title:'基础信息',hint:'名称、品牌与商品识别信息。',nodes:[],grid:true},{title:'价格与供货',hint:'采购成本、销售价格和供货设置。',nodes:[],grid:true},{title:'实物参数',hint:'各项参数按实际来源维护，未修改的来源说明继续保留。',nodes:[],grid:true},{title:'原图与来源',hint:'原始图片、供应商备注和原表证据。',nodes:[]}];
    let index=0;
    for(const node of [...grid.children]){
      const name=node.querySelector('[name]')?.name;
      if(name)index=/^(fact_|state_|facts$|components$)/.test(name)?2:/^(cost|cost_currency|price|currency|sale_mode|production_capacity|production_days|packing_days)$/.test(name)?1:0;
      groups[index].nodes.push(node);
    }
    for(const node of [...body.children])if(node!==grid)groups[3].nodes.push(node);
    grid.remove();mountTabs(dialog,groups);
  }
  window.WorkspaceShell={sectionListing,sectionArchive};
  sync();
})();
