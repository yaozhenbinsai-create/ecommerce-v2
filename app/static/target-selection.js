/* Account/region configuration and batch actions; materials use all enabled bindings. */
window.createTargetSelection = function({E,select,show,call,api,dialog,notice,getData,reload,guardDialog=()=>()=>true}) {
  const envName = value => ({production:'正式',sandbox:'官方沙盒',mock:'本地模拟'})[value] || value;
  const leaf = (name,value,text,checked=false,disabled=false) => `<label class="target-choice"><input type="checkbox" name="${name}" value="${E(value)}" ${checked?'checked':''} ${disabled?'disabled':''}><span>${E(text)}</span></label>`;
  function bindGroups(root) {
    root.querySelectorAll('[data-choice-group]').forEach(group => {
      const master=group.querySelector('[data-group-check]'), children=[...group.querySelectorAll('input[data-leaf]:not(:disabled)')];
      const update=()=>{const count=children.filter(x=>x.checked).length;master.checked=!!count&&count===children.length;master.indeterminate=count>0&&count<children.length;master.disabled=!children.length;root.dispatchEvent(new Event('selectionchange'))};
      master.onchange=()=>{children.forEach(x=>x.checked=master.checked);update()};
      children.forEach(x=>x.onchange=update);update();
    });
  }
  async function configure(accountId=null) {
    const current=guardDialog();
    const config=await api('/api/commerce/target-bindings');
    if(!current())return;
    const accounts=config.accounts.filter(a=>a.enabled);
    if(!accounts.length){notice('请先新增并启用平台账号',true);return}
    const environment='production';
    const scope=accountId?accounts.filter(a=>a.id===accountId):accounts;
    if(!scope.length){notice('该账号已停用，请先启用',true);return}
    show('账号与地区 · 多选关联',
      `<p class="c-hint">配置正式店铺。勾选账号后，为每个账号分别选择地区。同一地区可对应多个账号。取消地区勾选会停用该关联，已有商品和订单保留。</p><div data-binding-grid></div><p data-binding-count class="c-hint"></p>`,
      async()=>{
        const state=Object.fromEntries([...grid.querySelectorAll('[data-account]')].map(card=>[card.dataset.account,{
          active:card.querySelector('[data-account-check]').checked,
          sites:[...card.querySelectorAll('[data-marketplace]:checked')].map(x=>x.value),
        }]));
        const selected=scope.filter(a=>state[a.id].active);
        if(selected.some(a=>!state[a.id].sites.length))throw new Error('已勾选的账号至少需要选择一个地区');
        const scoped=config.targets.filter(t=>scope.some(a=>a.id===t.account_id)&&t.environment===environment);
        await call('/target-bindings',{environment,account_ids:scope.map(a=>a.id),revisions:Object.fromEntries(scoped.map(t=>[t.id,t.revision])),
          selections:selected.map(a=>({account_id:a.id,marketplace_ids:state[a.id].sites}))},'PUT');
        await load();
        return '账号与地区关联已保存';
      },'保存账号与地区');
    const grid=dialog.querySelector('[data-binding-grid]');
    function choices(account) {
      const choices=new Map((account.platform==='亚马逊'?config.marketplaces:[]).map(m=>[m.marketplace_id,m]));
      const platformIds=new Set(config.accounts.filter(a=>a.platform===account.platform).map(a=>a.id));
      config.targets.filter(t=>t.environment===environment&&platformIds.has(t.account_id)).forEach(t=>{if(!choices.has(t.marketplace_id))choices.set(t.marketplace_id,t)});
      return [...choices.values()];
    }
    function render() {
      grid.innerHTML=scope.map(a=>{
        const sites=config.targets.filter(t=>t.account_id===a.id&&t.environment===environment&&t.enabled).map(t=>t.marketplace_id);
        const active=sites.length>0||a.id===accountId;
        const available=choices(a),unsupported=a.platform!=='亚马逊';
        return `<section class="target-account" data-account="${a.id}"><label class="target-account-heading"><input type="checkbox" data-account-check ${active&&!unsupported?'checked':''} ${unsupported?'disabled':''}><b>${E(a.account_name)}</b><span>${E(a.platform)}</span></label><div class="target-marketplaces">${available.map(m=>`<label class="target-choice"><input type="checkbox" data-marketplace value="${E(m.marketplace_id)}" ${sites.includes(m.marketplace_id)?'checked':''}><span>${E(m.name)} <small>${E(m.currency)}</small></span></label>`).join('')||'<p class="c-hint">暂无地区选项，可在“上架目标与授权”中添加自定义地区。</p>'}</div>${unsupported?'<p class="c-hint">此平台接口尚未接入，需开发对应平台适配器。</p>':''}</section>`;
      }).join('');
      const update=()=>{
        let count=0, accountCount=0;
        grid.querySelectorAll('[data-account]').forEach(card=>{
          const active=card.querySelector('[data-account-check]').checked;
          card.querySelectorAll('[data-marketplace]').forEach(x=>{x.disabled=!active;if(active&&x.checked)count++});
          if(active)accountCount++;
        });
        dialog.querySelector('[data-binding-count]').textContent=`已选 ${accountCount} 个账号 · ${count} 个账号地区组合`;
      };
      grid.onchange=update;update();
    }
    render();
  }
  function targetGrid(targets) {
    const groups=new Map();
    targets.forEach(t=>{if(!groups.has(t.account_id))groups.set(t.account_id,[]);groups.get(t.account_id).push(t)});
    return [...groups.values()].map(rows=>`<section class="target-account" data-choice-group><label class="target-account-heading"><input type="checkbox" data-group-check><b>${E(rows[0].account_name)}</b></label><div class="target-marketplaces">${rows.map(t=>leaf('target_ids',t.id,`${t.name} · ${envName(t.environment)} · ${t.language_name||t.language||'语言待配置'}`).replace('type="checkbox"','type="checkbox" data-leaf')).join('')}</div></section>`).join('');
  }
  async function chooseTargets(title,onSubmit,saveLabel) {
    const config=await api('/api/commerce/target-bindings');
    const active=new Set(config.accounts.filter(a=>a.enabled).map(a=>a.id));
    const targets=config.targets.filter(t=>t.enabled&&t.runtime_available!==false&&active.has(t.account_id));
    if(!targets.length){notice('请先在平台同步中配置账号与地区',true);return}
    show(title,'<p class="c-hint">按账号分别勾选本批地区，只处理下方勾选的组合。</p>'+targetGrid(targets)+'<p class="c-hint" data-selection-count></p>',async f=>{
      const ids=f.getAll('target_ids').map(Number);
      if(!ids.length)throw new Error('请至少选择一个账号地区组合');
      return onSubmit(ids);
    },saveLabel);
    dialog.addEventListener('selectionchange',()=>{const count=dialog.querySelector('[data-selection-count]');if(count)count.textContent=`本批已选 ${dialog.querySelectorAll('[name=target_ids]:checked').length} 个账号地区组合`},{signal:selectionSignal()});
    bindGroups(dialog);
  }
  let controller;
  function selectionSignal(){controller?.abort();controller=new AbortController();return controller.signal}
  const generatingProjects=new Set();
  async function watchMaterials(pid,initial=null) {
    const labels={generating:'正在生成标题、卖点和商品资料',filling:'文案已保存，正在补齐平台字段',ready:'上架资料已生成完成',needs_fields:'资料已保存，字段补齐已暂停',paused:'资料生成已暂停',failed:'资料生成失败',unknown:'请求结果待确认，请勿重复生成'};
    show('上架资料生成进度','<div data-material-progress aria-live="polite">正在读取本次生成结果…</div>');
    const box=dialog.querySelector('[data-material-progress]');
    let checks=0;
    const current=()=>dialog.open&&dialog.querySelector('[data-material-progress]')===box;
    async function update(plan=null) {
      if(!current())return;
      try {
        plan=plan||await api(`/api/commerce/design-projects/${pid}/materials-plan`);
        if(!current())return;
        const progress=plan.progress;
        if(!progress){box.innerHTML='<p>尚未找到资料生成记录，请关闭后重新查看。</p>';return}
        box.innerHTML=progress.rows.map(row=>`<section class="c-card"><h3>${E(row.target_name)}</h3><p><b>${E(labels[row.status]||row.status)}</b></p>${row.error?`<p class="c-error">${E(row.error)}</p>`:''}${row.draft_version?`<p>已保存资料 v${E(row.draft_version)}</p><button type="button" data-c-action="listing-edit" data-c-id="${row.listing_id}">查看已有资料</button>`:''}${row.can_continue?`<button type="button" data-material-continue="${row.listing_id}">继续补齐资料</button>`:''}</section>`).join('')+'<p class="c-hint">本操作只生成资料；商品与详情尚未因此提交到平台。</p>';
        box.querySelectorAll('[data-material-continue]').forEach(button=>{
          button.onclick=async()=>{
            button.disabled=true;
            const row=progress.rows.find(item=>item.listing_id===Number(button.dataset.materialContinue));
            try {
              await call(`/listings/${row.listing_id}/complete-fields`,{revision:row.revision});
              if(current()){checks=0;await update()}
            }catch(error){if(current()){notice(error.message,true);button.disabled=false}}
          };
        });
        if(progress.active){
          if(++checks<180)setTimeout(()=>update(),5000);
          else {box.insertAdjacentHTML('beforeend','<p>任务仍在后台处理，可重新查看进度。</p><button type="button" data-material-refresh>刷新进度</button>');box.querySelector('[data-material-refresh]').onclick=()=>{checks=0;update()}}
        }else await reload();
      }catch(error){if(current()){box.textContent='读取进度失败：'+error.message;notice(error.message,true)}}
    }
    await update(initial);
  }
  async function generate(pid) {
    if(generatingProjects.has(pid))return;
    generatingProjects.add(pid);
    try {await generateOnce(pid)}finally{generatingProjects.delete(pid)}
  }
  async function generateOnce(pid) {
    const current=guardDialog();
    const plan=await api(`/api/commerce/design-projects/${pid}/materials-plan`);
    if(!current())return;
    if(plan.progress&&(plan.progress.active||plan.progress.rows.some(row=>row.status!=='ready'))){await watchMaterials(pid,plan);return}
    if(plan.active_jobs.length){notice('该商品正在生成资料，请等待本次任务完成');return}
    const requestId=crypto.randomUUID();
    const submit=async overwrite=>{
      await call(`/design-projects/${pid}/materials`,{fingerprint:plan.fingerprint,overwrite,request_id:requestId});
      return {afterSave:()=>watchMaterials(pid)};
    };
    if(!plan.has_existing){await submit(false);await reload();if(current())await watchMaterials(pid);else notice('原商品的资料生成任务已接收，可从该商品查看结果');return}
    show('是否覆盖原来的上架资料？',`<p>将按当前配置对全部 <b>${plan.target_count}</b> 个启用店铺站点重新生成上架资料。</p><p>成功后更新当前资料；历史版本、已保存的手工文案和手工价格保留，失败的站点继续保留原资料。</p><p class="c-hint">这是单独重新生成资料；自动生成和上架请使用设计工作台的“完成设计并上架”。</p>`,()=>submit(true),'重新生成上架资料');
    dialog.querySelector('.dialog-foot [data-c-close]').textContent='否，不生成';
  }
  async function sync() {
    await chooseTargets('选择本批同步的账号与地区',async ids=>{
      const result=await call('/target-sync-batch',{target_ids:ids});
      return `已提交 ${result.target_count} 个账号地区组合的同步任务`;
    },'同步所选地区');
  }
  async function publish() {
    const current=guardDialog();
    const plan=await api('/api/commerce/listing-publish-plan');
    if(!current())return;
    const requestId=crypto.randomUUID();
    const existing=plan.overwrite_count>0;
    show(existing?'是否覆盖原来的平台商品资料？':'确认一键上架全部商品',
      `<p>按当前配置处理全部启用账号地区中已有资料的商品，共 <b>${plan.listing_count}</b> 条商品目标、<b>${plan.groups.length}</b> 个账号地区。</p>`+
      plan.groups.map(g=>`<p>${E(g.account_name)} · ${E(g.name)} · ${envName(g.environment)}：${g.listing_ids.length} 条</p>`).join('')+
      `<p>${existing?`其中 ${plan.overwrite_count} 条已有平台记录，选择“是”将以当前资料更新原商品。`:''}正式店铺将实际提交平台，包含的详情页会继续上传并提交平台审核，商品和详情分别显示结果。资料不完整时会提示补齐；本次不受页面筛选条件影响。</p>`,async()=>{
        const batch=await call('/listing-publish-all',{request_id:requestId,fingerprint:plan.fingerprint,overwrite:existing,confirmed:true});
        return `上架批次 #${batch.id} 已提交：${batch.groups.length} 个账号地区、${batch.job_ids.length} 条商品任务`;
      },existing?'是，覆盖并上架':'是，全部上架');
    dialog.querySelector('.dialog-foot [data-c-close]').textContent='否，不上架';
  }

  return {configure,generate,publish,sync,chooseTargets};
};
