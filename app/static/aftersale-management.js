window.createAftersaleManagement=function({E,input,textarea,select,table,button,show,call,api,getData,targetUI,render,reload=async()=>{},dialog,guardDialog=()=>()=>true}) {
  let view='open',target='';
  const closed=s=>['resolved','cancelled'].includes(s);
  const labels={open:'待处理',processing:'处理中',needs_attention:'待核对',resolved:'已完成',cancelled:'已取消',draft:'处理草稿',queued:'等待提交',sending:'提交中',pending_confirmation:'待平台确认',unknown:'结果待核实',failed:'提交失败',confirmed:'平台已确认'};
  const actionLabels={approve_return:'同意退货',reject:'拒绝申请',refund:'退款',replacement:'回填补发物流'};
  const state=s=>E(labels[s]||s||'待核对');
  const name=id=>{const t=(getData().target||[]).find(t=>t.id===id);return t?`${t.account_name} · ${t.name} · ${({mock:'模拟',sandbox:'沙盒',production:'正式'})[t.environment]}`:'历史手工记录'};
  function listing(rows){
    const selected=rows.filter(c=>(!target||c.target_id===Number(target))&&(view==='all'||(view==='open'?!closed(c.status):closed(c.status))));
    return `<div class="c-toolbar">${button('aftersales-sync',0,'刷新所选店铺售后')}${button('refresh',0,'刷新本地列表')}${select('aftersale_view','售后范围',view,[['open','待处理 / 待确认'],['history','已完成 / 已取消'],['all','全部本地记录']])}${select('aftersale_target','店铺与地区',target,[['','全部已配置店铺'],...(getData().target||[]).map(t=>[t.id,name(t.id)])])}</div><p class="c-hint">获取未处理售后，办理后提交所属平台并核对结果；收货、验货和实际补发在线下完成。</p>`+
      (selected.length?table(['售后 / 原订单','所属平台与店铺','类型 / 商品 / 原因','状态','操作'],selected.map(c=>[
        `${E(c.external_id||'手工登记 #'+c.id)}<small>订单 ${E(c.external_order_id)}</small>`,`${E(c.platform||'')}<small>${E(name(c.target_id))}</small>`,
        `${E(({return:'退货',refund:'退款',replacement:'补发'})[c.type]||c.type)}${(c.items||[]).map(i=>`<small>${E(i.name||i.seller_sku)} × ${E(i.quantity)}</small>`).join('')}<small>${E(c.reason||'')}</small>`,
        `${state(c.status)}<small>${E(c.platform_status_raw||'')}</small>`,button('aftersales-detail',c.id,'查看 / 处理'),
      ])):'<div class="empty">当前范围没有售后，可按已配置的平台、店铺与地区刷新。</div>');
  }
  document.addEventListener('change',event=>{if(event.target.name==='aftersale_view'){view=event.target.value;render()}if(event.target.name==='aftersale_target'){target=event.target.value;render()}});
  async function refresh(){
    show('刷新售后',`<p class="c-hint">选择退货申请日期范围，再勾选平台、店铺与地区。留空默认最近60天；更早的申请可分段刷新。平台已关闭的售后只更新已有记录。</p><div class="c-grid">${input('start','申请起始日期（选填）','','date')}${input('end','申请截止日期（选填）','','date')}</div>`,async f=>{
      const range={};if(f.get('start'))range.start=new Date(f.get('start')+'T00:00:00').toISOString();if(f.get('end'))range.end=new Date(Math.min(new Date(f.get('end')+'T23:59:59').getTime(),Date.now())).toISOString();
      return {afterSave:()=>targetUI.chooseTargets('选择刷新售后的平台店铺与地区',async ids=>{
        const result=await call('/aftersales/sync',{...range,target_ids:ids});return `已提交 ${result.target_count} 个店铺地区的售后刷新任务，进度和失败原因见经营概览`;
      },'刷新待处理售后')};
    },'选择店铺与地区');
  }
  async function details(id){
    const current=guardDialog();
    const result=await api(`/api/commerce/aftersales/${id}/details`),c=result.case;
    if(!current())return;
    const actions=result.actions.length?table(['处理动作 / 内容','状态 / 原因','操作'],result.actions.map(a=>[
      `${E(actionLabels[a.action]||a.action)}<small>${E(a.payload.note)}</small>${a.action==='refund'?`${E(a.payload.amount)} ${E(a.payload.currency)}`:''}${a.action==='replacement'?`${E(a.payload.carrier)} · ${E(a.payload.tracking)}`:''}`,
      `${state(a.status)}<small>${E(a.last_error||'')}</small>`,
      ['draft','failed'].includes(a.status)?`${button('aftersales-submit',a.id,'提交处理到平台')}${button('aftersales-cancel',a.id,'作废草稿')}`:['sending','unknown','pending_confirmation'].includes(a.status)?button('aftersales-check',a.id,'核对平台结果'):'',
    ])):'<p class="c-hint">尚未保存处理草稿。</p>';
    show('售后 '+(c.external_id||'#'+c.id),`<p>${E(c.platform||'')} · ${E(name(c.target_id))} · ${state(c.status)}</p><p>原订单：${E(c.external_order_id)}${c.order_id?` ${button('order-logistics',c.order_id,'查看关联订单')}`:''}</p><p>${E(c.reason||'')}</p><p class="c-hint">${E(result.message)}</p>`+
      (c.items?.length?table(['商品 / SKU','申请数量','原因 / 处理结果'],c.items.map(i=>[E(i.name||i.seller_sku),E(i.quantity),`${E(i.reason||'')}<small>${E(i.resolution||'')}</small>`])):'')+
      `<div class="actions">${result.available_actions.length?button('aftersales-process',id,'填写处理结果'):''}${c.execution==='platform'?button('aftersales-refresh-case',id,'刷新平台售后状态'):''}</div><h3>处理与提交记录</h3>${actions}`+
      (c.execution!=='platform'?`<p>原办理凭据：${E(c.platform_reference||'未记录')}</p>`:''));
  }
  async function process(id){
    const current=guardDialog();
    const result=await api(`/api/commerce/aftersales/${id}/details`),c=result.case;
    if(!current())return;
    if(!result.available_actions.length)throw new Error(result.message||'当前没有可用处理动作');
    const requestId=crypto.randomUUID();
    show('填写售后处理 · '+c.external_id,`<p>${E(name(c.target_id))} · 订单 ${E(c.external_order_id)}</p><div class="c-grid">${select('action','处理动作',result.available_actions[0].value,result.available_actions.map(a=>[a.value,a.label]))}${input('amount',`退款金额（仅退款时填写，可退 ${c.refundable_amount??'未知'} ${c.currency}）`,'','number','min="0.01" step="0.01"')}${input('carrier','补发物流公司（仅补发时填写）')}${input('tracking','补发物流单号（仅补发时填写）')}${textarea('note','处理说明 / 拒绝原因')}</div><p class="c-hint">保存后检查处理草稿，再点击“提交处理到平台”。退款金额及补发物流以本次实际办理内容为准。</p>`,async f=>{
      await call(`/aftersales/${id}/actions`,{revision:c.revision,request_id:requestId,action:f.get('action'),payload:{note:f.get('note'),amount:f.get('amount'),currency:c.currency,carrier:f.get('carrier'),tracking:f.get('tracking')}});
      return {afterSave:()=>details(id)};
    },'保存处理草稿');
    if(dialog){
      const picker=dialog.querySelector('[name=action]');
      const update=()=>{
        for(const key of ['amount','carrier','tracking']){
          const field=dialog.querySelector(`[name=${key}]`),active=key==='amount'?picker.value==='refund':picker.value==='replacement';
          field.closest('label').style.display=active?'':'none';field.disabled=!active;field.required=active;
        }
      };
      picker.onchange=update;update();
    }
  }
  async function act(action,id){
    const current=guardDialog();
    if(action==='aftersales-refresh-case'){await call(`/aftersales/${id}/refresh`,{});await reload();if(current())await details(id);return}
    const operation=await api(`/api/commerce/aftersale-actions/${id}`);
    if(!current())return;
    const suffix=({'aftersales-submit':'submit','aftersales-check':'check','aftersales-cancel':'cancel'})[action];
    if(!suffix)throw new Error('未知售后操作');
    await call(`/aftersale-actions/${id}/${suffix}`,{revision:operation.revision});await reload();if(current())await details(operation.case_id);
  }
  return {listing,refresh,details,process,act};
};
