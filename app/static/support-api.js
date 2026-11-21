/* Official Support API only. Customer browser sessions are never an intake source. */
window.createSupportApi=function({E,button,table,select,modal,call,api,dialog,refresh,render,getTargets}){
  let editor=null,version=0;
  const pending=new Set(),form=()=>dialog?.querySelector('form');
  const action=(name,id,title,extra='')=>button('support-api-'+name,Number(id),title,extra);
  async function request(url,options={},perform=null){
    const controller=new AbortController();let timer;
    const write=options.method&&options.method!=='GET';
    try{return await Promise.race([perform?perform(controller.signal):api(url,{...options,signal:controller.signal}),new Promise((resolve,reject)=>{
      timer=setTimeout(()=>{controller.abort();reject(Error(write?'请求暂未取得回执，请先刷新连接状态核实，不要重复点击。':'连接状态读取超时，请点击“刷新连接状态”重试。'));},write?12000:8000);
    })]);}finally{clearTimeout(timer);}
  }
  function valid(context){return editor===context&&context.form===form()&&dialog?.open&&context.version===version;}
  function showStatus(context,value,error=false){if(!valid(context))return;const node=context.form?.querySelector('[data-api-status]');if(node){node.textContent=value;node.setAttribute('role',error?'alert':'status');}}
  async function connections(){
    const stamp=++version;await refresh(false);if(stamp!==version)return;
    const targets=getTargets().filter(t=>t.enabled!==false&&t.environment==='production'&&['亚马逊','Amazon','amazon'].includes(t.platform));
    const accounts=[...new Map(targets.map(t=>[t.account_id||t.id,t])).values()];
    modal('亚马逊API连接',`<p>卖家支持Case通过官方Support API读取，复用现有店铺应用授权。</p>${accounts.length?table(['卖家账号','支持Case API','操作'],accounts.map(t=>[E([t.platform,t.account_name].filter(Boolean).join(' · ')),`${E(caseLabels[t.case_connection?.status]||'未核验Case API权限')}<small>账号范围（API未提供地区）</small>${t.case_connection?.last_error?`<small role="alert">${E(t.case_connection.last_error)}</small>`:''}`,action('case-open',t.case_connection?.managed_by_target_id||t.id,'管理官方Case API')])):'<p>请先在平台同步中配置实际店铺账号。</p>'}<p role="status"><strong>买家咨询：官方API暂不支持收件箱读取。</strong>公开Messaging API提供订单相关发信动作，不能用于获取完整买家消息。</p><p>当前先接入读取，回复功能后续处理。已有会话、工单与原始归档继续保留。</p>`);
  }
  const caseRoute=id=>`/support/targets/${Number(id)}/case-api`;
  const caseGet=id=>request('/api/commerce'+caseRoute(id));
  const caseLabels={unconfigured:'未核验Case API权限',unverified:'等待核验',queued:'Case读取已排队',permission_required:'需要申请销售伙伴支持权限',syncing:'正在同步Case',retry_wait:'等待API重试',connected:'Case API已接通',needs_attention:'Case读取需核实',identity_changed:'Case连接身份已变化',paused:'Case读取已暂停',unsupported:'当前账号不支持此API连接'};
  function caseStatus(p){return [caseLabels[p.status]||p.status,p.phase,p.busy?'后台读取进行中':null,p.status==='retry_wait'&&p.next_retry_at?'下次重试 '+new Date(p.next_retry_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):null,p.completed_at?'最近完整读取 '+new Date(p.completed_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):null,`累计收录 ${p.case_count||0} 个Case、${p.contact_count||0} 条沟通记录`,p.last_error,p.request_id?'Amazon请求号：'+p.request_id:null].filter(Boolean).join(' · ');}
  async function caseOpen(id){
    const stamp=++version,previous=form(),wasOpen=dialog?.open,p=await caseGet(id);if(stamp!==version||form()!==previous||dialog?.open!==wasOpen)return;
    const target=getTargets().find(t=>t.id===Number(id))||{};
    if(p.managed_by_target_id&&p.managed_by_target_id!==Number(id))return caseOpen(p.managed_by_target_id);
    modal('官方Case API · '+[target.platform,target.account_name].filter(Boolean).join(' · '),`<p>通过现有亚马逊应用读取当前卖家账号的支持工单、详情及邮件／电话／聊天沟通记录。API没有返回工单地区，不把账号Case标为美国站或其他地区。</p><p data-api-status role="status">${E(caseStatus(p))}</p><p>需要开发者资料获批并在应用中选择“销售伙伴支持（受限）”角色，然后更新卖家授权。登录卖家网页或切换地区不能授予API权限。</p><div class="support-actions">${action('case-sync',id,p.status==='connected'?'立即同步Case':'核验API权限并同步',p.busy?'disabled aria-busy="true"':'')}${action('case-refresh',id,'刷新Case连接状态')}${p.enabled&&!p.busy&&(p.has_more||p.status==='needs_attention')?action('case-rescan',id,'保存原位置，从首页重新核对'):''}${p.enabled?action('case-pause',id,'暂停Case读取'):''}</div>${p.enabled?`<div class="c-grid">${select('case_auto_sync','Case自动读取',String(p.auto_sync),[['true','自动读取Case更新'],['false','仅手动同步']])}</div>`:''}<p>页面会跟踪有界读取进度，关闭对话框后可重新查看。这里仅保存本系统读取设置；读取权限由亚马逊审批；回复功能后续接入，当前请在亚马逊后台处理。</p>`,p.enabled?async values=>{
      await request(null,{method:'PUT'},signal=>call(caseRoute(id),{revision:editor.config.revision,enabled:true,auto_sync:values.get('case_auto_sync')==='true'},'PUT',{signal}));await refresh(false);return {afterSave:()=>caseOpen(id)};
    }:null,'保存Case同步设置');
    const context=editor={id:Number(id),kind:'case',config:p,form:form(),version:stamp};
    if(p.busy)caseTrack(context,Date.now()+3*60*1000);
    return p;
  }
  function caseTrack(context,deadline){
    if(typeof window.setTimeout!=='function')return;
    window.setTimeout(async()=>{
      if(!valid(context))return;
      if(Date.now()>deadline)return showStatus(context,'界面已停止等待；Case任务仍可从连接状态查看，未将等待标记为完成。');
      try{const p=await caseGet(context.id);if(!valid(context))return;
        if(!p.busy){await refresh(false);render();if(valid(context))await caseOpen(context.id);return;}
        context.config=p;showStatus(context,caseStatus(p),!!p.last_error);caseTrack(context,deadline);
      }catch(error){showStatus(context,error.message,true);}
    },2000);
  }
  async function caseDispatch(name,id){
    const context=editor;if(!context||context.kind!=='case'||context.id!==Number(id)||!valid(context))throw Error('请重新打开当前账号的Case连接。');
    if(name==='refresh')return caseOpen(id);
    if(name==='sync'||name==='rescan'){
      const result=await request(null,{method:'POST'},signal=>call(caseRoute(id)+'/'+name,{revision:context.config.revision},'POST',{signal}));
      if(valid(context)){if(!result.job?.id)throw Error('未取得Case读取任务，请刷新连接状态核实。');await caseOpen(id);}return;
    }
    if(name==='pause'){
      await request(null,{method:'PUT'},signal=>call(caseRoute(id),{revision:context.config.revision,enabled:false,auto_sync:false},'PUT',{signal}));
      await refresh(false);if(valid(context))await caseOpen(id);render();return;
    }
    throw Error('未知的Case API操作');
  }
  async function dispatch(name,id){
    const key=name+':'+id;if(pending.has(key))return;pending.add(key);
    try{
      if(name==='case-open')return await caseOpen(id);
      if(name.startsWith('case-'))return await caseDispatch(name.slice(5),id);
      throw Error('未知的API连接操作');
    }catch(error){if(editor&&editor.id===Number(id))showStatus(editor,error.message,true);throw error;}
    finally{pending.delete(key);}
  }
  return {connections,open:caseOpen,dispatch};
};
