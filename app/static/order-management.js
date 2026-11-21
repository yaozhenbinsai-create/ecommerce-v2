window.createOrderManagement=function({E,input,select,table,button,badge,show,call,api,dialog,getData,targetUI,render,guardDialog=()=>()=>true}) {
  let view='open',target='';
  const targetName=id=>{const t=getData().target.find(x=>x.id===id);return t?`${t.account_name} · ${t.name} · ${({mock:'模拟',sandbox:'沙盒',production:'正式'})[t.environment]}`:'原店铺已移除'};
  const states={ready:'待发货',partially_shipped:'部分发货',shipped:'已发货',cancelled:'已取消',needs_attention:'待核对',preparing:'待发货',ready_to_ship:'待发货',aftersale_needed:'待售后',draft:'待提交',queued:'等待上传',failed:'提交失败',sending:'提交中',unknown:'结果待核实',pending_confirmation:'待平台确认',confirmed:'平台已确认'};
  const state=s=>E(states[s]||s);
  function listing(rows){
    const selected=rows.filter(o=>(!target||o.target_id===Number(target))&&(view==='all'||(view==='open'?!['shipped','cancelled','aftersale_needed'].includes(o.status):['shipped','cancelled','aftersale_needed'].includes(o.status))));
    return `<div class="c-toolbar">${button('orders-sync-selected',0,'刷新所选店铺订单')}${button('refresh',0,'刷新本地列表')}${select('order_view','订单范围',view,[['open','待发货 / 待处理'],['history','已发货 / 历史'],['all','全部本地记录']])}${select('order_target','店铺与地区',target,[['','全部已配置店铺'],...getData().target.map(t=>[t.id,targetName(t.id)])])}</div><p class="c-hint">按平台配置获取待发货订单，生产和发货在线下完成；在订单内回填物流并提交所属平台。</p>`+
      (selected.length?table(['订单 / 所属店铺','商品 / 待发数量','状态 / 发货期限','操作'],selected.map(o=>[
        `${E(o.external_id)}<small>${E(o.platform||'')} · ${E(targetName(o.target_id))}</small>`,
        o.items.map(i=>`${E(i.name||i.seller_sku)}<small>${E(i.seller_sku)} · 待发 ${Math.max(0,i.quantity-i.shipped)} / 共 ${i.quantity}</small>`).join(''),
        `${state(o.status)}<small>${E(o.deadline||'未提供期限')}</small>${(o.sync_issues||[]).map(E).join('；')}`,
        button('order-logistics',o.id,'查看订单 / 回填物流'),
      ])):'<div class="empty">当前范围没有订单，可按已配置店铺刷新待发货订单。</div>');
  }
  document.addEventListener('change',event=>{if(event.target.name==='order_view'){view=event.target.value;render()}if(event.target.name==='order_target'){target=event.target.value;render()}});
  async function refreshOrders(){
    await targetUI.chooseTargets('选择刷新订单的平台店铺与地区',async ids=>{
      const result=await call('/orders/sync',{target_ids:ids});return `已提交 ${result.target_count} 个店铺地区的订单刷新任务，结果见经营概览`;
    },'刷新待发货订单');
  }
  async function details(id){
    const current=guardDialog();
    const result=await api(`/api/commerce/orders/${id}/logistics`),o=result.order;
    if(!current())return;
    const header=`<p>${E(targetName(o.target_id))} · ${state(o.status)}</p>`;
    const summary=table(['商品 / SKU','订购','已发货','可填新包裹'],o.items.map(i=>[E(i.name||i.seller_sku),i.quantity,i.shipped,result.remaining[i.external_item_id]||0]));
    const canCreate=Object.values(result.remaining).some(n=>n>0)&&!['shipped','cancelled','aftersale_needed'].includes(o.status)&&['ready','partially_shipped'].includes(o.platform_status);
    const packages=result.shipments.length?table(['物流公司 / 单号','本次商品数量','状态 / 原因','操作'],result.shipments.map(s=>[
      `${E(s.carrier)}<small>${E(s.tracking)}</small><small>${E(s.ship_date)}</small>`,s.items.map(i=>`${E(o.items.find(x=>x.external_item_id===i.item_id)?.seller_sku||i.item_id)} × ${i.quantity}`).join('<br>'),
      `${state(s.status)}<small>${E(s.last_error||'')}</small>`,
      ['draft','failed'].includes(s.status)?`${button('order-ship-submit',s.id,'提交物流')}${button('order-ship-edit',s.id,'编辑物流')}${s.fulfillment_mode==='offline'?button('order-ship-cancel',s.id,'作废草稿'):''}`:['sending','unknown','pending_confirmation'].includes(s.status)?button('order-ship-check',s.id,'核对平台结果'):'',
    ])):'<p class="c-hint">尚未回填物流。</p>';
    show('订单 '+o.external_id,header+summary+`<h3>物流回填</h3>${canCreate?button('order-package-new',id,'回填新包裹'):''}`+packages+`<h3>收件信息</h3><p class="c-hint">${E(o.pii_message||'仅用于本订单线下发货。')}</p><pre>${E(JSON.stringify(o.recipient||{},null,2))}</pre>`);
  }
  async function newPackage(id){
    const current=guardDialog();
    const {order:o,remaining}=await api(`/api/commerce/orders/${id}/logistics`);
    if(!current())return;
    const requestId=crypto.randomUUID(),localTime=new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16);
    show('回填物流 · '+o.external_id,`<p>${E(targetName(o.target_id))}</p><div class="c-grid">${input('carrier','物流公司','','text','required maxlength="100"')}${input('tracking','物流单号','','text','required maxlength="100"')}${input('service','物流服务（选填）')}${input('ship_date','实际发货时间（本机时区）',localTime,'datetime-local','required')}</div><h3>本次包裹的商品数量</h3>`+o.items.map((i,n)=>input('quantity_'+n,`${i.name||i.seller_sku} · 尚可回填 ${remaining[i.external_item_id]||0}`,remaining[i.external_item_id]||0,'number',`min="0" max="${remaining[i.external_item_id]||0}" step="1"`)).join('')+'<p class="c-hint">先保存物流，检查后点击“提交物流”。实际已交运才填写，保存不会改变平台状态。</p>',async f=>{
      await call(`/orders/${id}/logistics`,{carrier:f.get('carrier'),tracking:f.get('tracking'),service:f.get('service'),ship_date:new Date(f.get('ship_date')).toISOString(),request_id:requestId,
        items:o.items.map((i,n)=>({item_id:i.external_item_id,quantity:Number(f.get('quantity_'+n))})).filter(i=>i.quantity>0)});
      return {afterSave:()=>details(id)};
    },'保存物流');
  }
  async function shipAction(action,id){
    const current=guardDialog();
    const ship=await api(`/api/commerce/records/shipment/${id}`);
    if(!current())return;
    if(action==='order-ship-edit'){
      show('编辑物流',`<div class="c-grid">${input('carrier','物流公司',ship.carrier)}${input('tracking','物流单号',ship.tracking)}${input('service','物流服务',ship.service)}</div>`,async f=>{
        await call(`/shipments/${id}`,{...Object.fromEntries(f),revision:ship.revision},'PATCH');return {afterSave:()=>details(ship.order_id)};
      });return;
    }
    await call(`/shipments/${id}/${action==='order-ship-submit'?'submit':action==='order-ship-cancel'?'cancel':'reconcile'}`,{revision:ship.revision});
    if(!current())return;
    await details(ship.order_id);
    if(action!=='order-ship-cancel')dialog.querySelector('.c-body').insertAdjacentHTML('afterbegin','<p class="c-hint">已交给后台处理，可重新打开订单查看结果；等待平台确认期间不会重复上传。</p>');
  }
  return {listing,details,newPackage,refreshOrders,shipAction};
};
