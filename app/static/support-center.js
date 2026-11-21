/* One support workspace for every platform; transport capabilities stay server-owned. */
window.createSupportCenter=function({E,input,textarea,select,table,button,show,call,api,dialog,notice,render,getPlans=()=>[],getLanguages=()=>[]}) {
  let snapshot=null,loading=null,loadError='',refreshAgain=false,view='all',target='',kind='all',dialogVersion=0,autoRefreshTimer=null;
  const pending=new Set();
  const labels={ai_pending:'等待 AI 分析',human:'待人工处理',awaiting_customer:'等待客户回复',closed:'已完成',tracking:'跟踪中',observed:'原平台历史记录',received:'客户消息已收录',sending:'正在提交平台',unknown:'发送结果待核实',sent:'平台已确认发送',failed:'发送失败'};
  const state=value=>E(labels[value]||value||'待核对');
  const languages={en:'英语',en_US:'英语（美国）',en_GB:'英语（英国）',de:'德语',de_DE:'德语',fr:'法语',fr_FR:'法语',es:'西班牙语',es_ES:'西班牙语',it:'意大利语',it_IT:'意大利语',ja:'日语',ja_JP:'日语',zh:'中文',zh_CN:'简体中文',zh_TW:'繁体中文',pt:'葡萄牙语',pt_BR:'葡萄牙语（巴西）',ar:'阿拉伯语',ar_AE:'阿拉伯语',nl:'荷兰语',pl:'波兰语',sv:'瑞典语',tr:'土耳其语'};
  const language=value=>getLanguages().find(x=>x.value===value)?.label||languages[String(value||'').replaceAll('-','_')]||value||'按店铺地区';
  const languageChoices=()=>getLanguages().length?getLanguages().map(x=>[x.value,x.label]):Object.entries(languages);
  const action=(name,id,title,extra='')=>button('support-'+name,Number(id)||0,title,extra);
  const targets=()=>snapshot?.targets||[];
  const targetName=t=>[t?.platform,t?.account_name,t?.name].filter(Boolean).join(' · ')||'历史店铺';
  const targetFor=id=>targets().find(t=>t.id===Number(id));
  const utc=value=>/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(String(value||''))?String(value).replace(' ','T')+'Z':value;
  const time=value=>{
    if(!value)return '时间未记录';
    const date=new Date(typeof value==='number'?value*1000:utc(value));
    return Number.isNaN(date.getTime())?'时间待核对':date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  };
  const currentForm=()=>dialog?.querySelector('form');
  // An old fetch must never replace another feature's modal or reopen a closed modal.
  function guard(){const version=++dialogVersion,form=currentForm(),open=dialog?.open;return ()=>version===dialogVersion&&currentForm()===form&&dialog?.open===open;}
  function modal(title,body,save=null,label='保存'){dialogVersion++;show(title,`<div class="support-dialog">${body}</div>`,save,label);}
  async function refresh(redraw=true){
    if(loading){refreshAgain=true;return loading;}
    loadError='';
    loading=(async()=>{try{do{
      refreshAgain=false;const controller=new AbortController();let timer;
      try{snapshot=await Promise.race([api('/api/commerce/support/threads',{signal:controller.signal}),new Promise((resolve,reject)=>{
        timer=setTimeout(()=>{controller.abort();reject(Error('客户消息读取超时，请点击重试。'));},10000);
      })]);}finally{clearTimeout(timer);}
    }while(refreshAgain);return snapshot;}catch(error){loadError=error.message;throw error;}finally{loading=null;if(redraw)render();}})();
    return loading;
  }
  function stats(){
    const counts=snapshot?.stats||{};
    return [['待人工处理',counts.human||0],['支持Case跟踪',counts.tracking||0],['等待客户',counts.awaiting_customer||0],['已完成',counts.closed||0]].map(([title,count])=>`<div class="stat"><small>${E(title)}</small><b>${E(count)}</b></div>`).join('');
  }
  function listing(query=''){
    if(!autoRefreshTimer&&typeof window.setTimeout==='function'&&typeof document.querySelector==='function')autoRefreshTimer=window.setTimeout(async()=>{autoRefreshTimer=null;if(!document.querySelector('.support-center'))return;try{await refresh();}catch{}},15000);
    if(!snapshot){
      if(!loading&&!loadError)refresh().catch(()=>{});
      return `<div class="c-toolbar">${action('refresh',0,'刷新本地记录',loading?'disabled':'')}</div><div class="empty" role="status">${loadError?`读取客服记录失败：${E(loadError)}，请点击刷新本地记录。`:'正在读取客服记录…'}</div>`;
    }
    const configured=targets(),q=query.trim().toLowerCase();
    const filtered=(snapshot.threads||[]).filter(t=>(!target||(t.source_scope==='seller_account'?t.account_id===targetFor(target)?.account_id:t.target_id===Number(target)))&&(kind==='all'||(kind==='buyer'?!t.thread_type||t.thread_type==='buyer':t.thread_type===kind))&&(view==='all'||(view==='human'?['human','ai_pending'].includes(t.status):t.status===view))&&(!q||[t.subject,t.external_thread_id,t.external_order_id,t.platform_case_id,t.assignee,t.last_message?.text||t.last_message,t.last_reason,targetName(targetFor(t.target_id))].join(' ').toLowerCase().includes(q)));
    filtered.sort((a,b)=>(a.status==='human'?0:1)-(b.status==='human'?0:1)||String(b.updated_at||b.created_at||'').localeCompare(String(a.updated_at||a.created_at||''))||b.id-a.id);
    const connected=configured.filter(t=>t.capabilities?.receive);
    const toolbar=`<div class="c-toolbar support-toolbar">${action('connections',0,'亚马逊API连接')}${connected.length?action('sync',0,'同步买家消息'):''}${action('refresh',0,'刷新本地记录')}${action('settings',0,'店铺客服设置')}${configured.some(t=>!['亚马逊','Amazon','amazon'].includes(t.platform))?action('intake',0,'补录客户消息'):''}${select('support_kind','记录类型',kind,[['all','客户消息与支持Case'],['buyer','买家咨询／售后'],['seller_case','亚马逊卖家支持Case'],['source_issue','通知待核对']])}${select('support_view','会话范围',view,[['all','全部会话'],['human','待人工处理'],['tracking','支持Case跟踪'],['awaiting_customer','等待客户'],['closed','已完成']])}${select('support_target','店铺与地区',target,[['','全部平台、店铺与地区'],...configured.map(t=>[t.id,targetName(t)])])}</div>`;
    const caseLabels={unconfigured:'未核验权限',unverified:'等待核验',queued:'Case读取已排队',permission_required:'需申请受限权限',syncing:'正在同步',retry_wait:'等待API重试',connected:'已接通',needs_attention:'需核实读取',identity_changed:'连接身份已变化',paused:'已暂停'};
    const caseSources=[...new Map(configured.filter(t=>t.case_connection&&t.case_connection.status!=='unsupported').map(t=>[t.case_connection.managed_by_target_id||t.id,t])).values()];
    const caseStatus=caseSources.map(t=>`${E([t.platform,t.account_name,'支持Case（账号范围）'].filter(Boolean).join(' · '))}：${E(caseLabels[t.case_connection.status]||t.case_connection.status)}${t.case_connection.last_error?' · '+E(t.case_connection.last_error):''}`).join('<br>');
    const hasAmazon=configured.some(t=>['亚马逊','Amazon','amazon'].includes(t.platform));
    const connection=`<div class="support-summary"><strong>${configured.length?`已配置 ${E(configured.length)} 个店铺地区`:'尚未配置店铺地区'}</strong>${hasAmazon?'<span>亚马逊买家咨询：公开Messaging API没有完整收件箱读取接口，当前无法自动收录。</span>':''}<span>${caseStatus}</span>${connected.length?`<span>${E(connected.length)} 个地区具备消息读取能力</span>`:''}<span>支持Case通过官方API读取，按卖家账号归属；本地处理结论与平台状态分别记录。</span>${loadError?`<span role="alert">刷新失败：${E(loadError)}</span>`:''}</div>`;
    const rows=filtered.map(t=>[`${action('detail',t.id,t.subject||t.external_thread_id||'客户会话 #'+t.id)}<small>${t.thread_type==='seller_case'?'亚马逊支持Case '+E(t.platform_case_id||''):t.thread_type==='source_issue'?'通知待核对':'买家咨询／售后 · 订单 '+E(t.external_order_id||'未关联')}</small><small class="support-excerpt">${E(t.last_message?.text||t.last_message||'')}</small>`,`${E(t.source_scope==='seller_account'?[t.platform,t.account_name,'账号范围（API未提供地区）'].filter(Boolean).join(' · '):targetName(targetFor(t.target_id)))}<small>回复语言：${E(language(t.language))}</small>`,`${state(t.status)}<small>${E(t.assignee?'处理人：'+t.assignee:'尚未领取')}</small><small class="support-reason">${E(t.last_reason||'')}</small>`,`<time datetime="${E(utc(t.updated_at||t.created_at)||'')}">${E(time(t.updated_at||t.created_at))}</time><small>北京时间</small>`,action('detail',t.id,'查看 / 处理')]);
    return `<div class="support-center">${toolbar}${connection}${rows.length?table(['客户会话','所属店铺与地区','处理状态','最近更新','操作'],rows):`<div class="empty">${configured.length?(q||target?'没有符合筛选条件的客服记录。':'本系统尚未收录当前范围的记录。请在API连接中核验支持Case权限；买家咨询接口暂不支持，权限未通过也不代表没有工单。'):'请先在平台同步中配置账号与店铺地区，再设置客服方案。'}</div>`}</div>`;
  }
  document.addEventListener('change',event=>{if(event.target.name==='support_view'){view=event.target.value;render();}if(event.target.name==='support_target'){target=event.target.value;render();}if(event.target.name==='support_kind'){kind=event.target.value;render();}});

  function capabilityNote(c={}){
    const source=c.source_type==='amazon_support_api'?`官方Case API · ${E({connected:'已接通',syncing:'正在分页读取',retry_wait:'等待API重试',permission_required:'需申请受限权限',paused:'已暂停',needs_attention:'读取需核实'}[c.source_status]||'待核实')}`:(c.receive?'支持获取客户消息':'自动收件未接入');
    return `<div class="support-capability"><strong>${source} · ${c.send?'支持平台回复':'回复需到平台后台办理'}</strong><p>${E(c.reason||'以所属平台当前接入能力为准。')}</p>${c.analysis===false?'<p>此来源当前由人工处理；亚马逊客户数据未授权发送给外部AI。</p>':''}${!c.send?`<p>${c.analysis===false?'可在本系统整理并复制人工回复草稿。':'AI 内容为回复草稿。'}请复制后到平台后台办理，本系统不会将草稿标记为已回复。</p>`:''}</div>`;
  }
  function messageSource(m){
    if(m.source==='manual')return '<small>从平台后台手工录入</small>';
    if(m.provenance?.channel!=='amazon_browser')return '';
    return `<small>后台同步 · ${E(m.received_at||'原平台时间未提供')} · 正文原文已加密保存</small><small>${E(m.provenance.note||'')}</small>${m.provenance.body_truncated?'<small>正文过长，当前仅显示节选。</small>':''}${m.provenance.has_attachments?'<small>原平台记录含附件；请到亚马逊原会话查看。</small>':''}${m.id?action('original',m.id,'查看原始对话正文'):''}`;
  }
  function detailsBody(result){
    const {thread:t,messages=[],capabilities:c={}}=result,draft=t.ai_draft||{};
    const observed=t.platform_observed||{};
    const platformStatus=observed.checked_at?`<p>亚马逊状态：${E(observed.status||'买家会话')} · 核实于 ${E(time(observed.checked_at))}</p>${t.source_type==='amazon_support_api'&&!observed.history_complete?'<p role="status">Case沟通分页尚未读取完整，当前仅展示已收录部分；请在Case连接管理查看进度或恢复读取。</p>':''}`:'';
    const uncertain=messages.some(m=>['sending','unknown'].includes(m.delivery_status));
    const tools=[action('detail',t.id,'刷新会话')];
    if(result.source_url)tools.push(`<a href="${E(result.source_url)}" target="_blank" rel="noopener noreferrer">${t.thread_type==='seller_case'?'打开亚马逊原Case':'打开亚马逊原会话'}</a>`);
    if(t.status!=='closed'){
      tools.push(action('claim',t.id,t.assignee?'变更处理人':'领取处理'));
      if(!uncertain){if(c.analysis!==false&&t.thread_type!=='seller_case')tools.push(action('ai',t.id,'生成 AI 建议'));tools.push(action('reply',t.id,c.send?'填写回复':'查看 / 复制回复草稿'),action('escalate',t.id,'转人工 / 补充原因'),action('close',t.id,'登记处理结论并关闭'));}
    }
    if(uncertain)tools.push(action('reconcile',t.id,'核对发送结果'));
    const history=messages.length?`<ol class="support-messages">${messages.map(m=>`<li class="support-message ${m.direction==='inbound'?'support-inbound':'support-outbound'}"><div class="support-message-meta"><strong>${m.direction==='inbound'?(t.thread_type==='seller_case'?'亚马逊支持':'客户'):(m.delivery_status==='observed'?'卖家历史记录':'客服')}</strong><span>${state(m.delivery_status)}</span><time datetime="${E(utc(m.created_at)||'')}">${E(time(m.created_at))} 北京时间</time></div><div class="support-message-text" dir="auto">${E(m.text||'')}</div>${messageSource(m)}</li>`).join('')}</ol>`:'<div class="empty">暂无消息正文。</div>';
    const caseHistory=(result.case_contacts||[]).map(contact=>`<li class="support-message"><div class="support-message-meta"><strong>${E({EMAIL:'邮件沟通',PHONE:'电话沟通',CHAT:'在线聊天'}[contact.channel]||contact.channel)}</strong><time>${E(time(contact.received_at))} 北京时间</time></div><small>官方API原记录：${E(contact.contact_id)}</small>${contact.availability==='expired'?'<p>亚马逊标记此记录已过期，接口未提供正文。</p>':contact.text?`<div class="support-message-text" dir="auto">${E(contact.text)}</div>`:'<p>此记录未返回可读正文，请到原平台核对。</p>'}${contact.body_truncated?'<p>当前显示正文节选，完整内容见原始沟通。</p>':''}${contact.attachments?.length?`<p>附件：${contact.attachments.map(E).join('、')}；请到亚马逊查看附件。</p>`:''}${action('case-original',contact.id,'查看原始沟通')}</li>`).join('');
    return `<div class="support-thread-head"><h3>${E(t.subject||t.external_thread_id||'客户会话 #'+t.id)}</h3><p>${E(t.source_scope==='seller_account'?[t.platform,t.account_name,'账号范围（API未提供地区）'].filter(Boolean).join(' · '):targetName(targetFor(t.target_id)))} · ${state(t.status)}</p><p>回复语言：${E(language(t.language))} · ${E(t.assignee?'处理人：'+t.assignee:'尚未领取')}</p><p>订单：${E(t.external_order_id||'未关联')} · 平台会话：${E(t.external_thread_id||'未记录')}</p></div>${platformStatus}${capabilityNote(c)}${t.last_reason?`<div class="support-handoff"><strong>待处理原因</strong><p>${E(t.last_reason)}</p></div>`:''}${uncertain?'<div class="support-handoff"><strong>发送结果尚未确认</strong><p>请先核对平台结果。核对完成前暂停追加回复和关闭会话，以免重复联系客户。</p></div>':''}<div class="actions support-actions">${tools.join('')}</div><h3>原始对话与处理记录</h3>${history}${caseHistory?`<ol class="support-messages">${caseHistory}</ol>`:''}${c.analysis===false?'':`<section class="support-ai"><h3>AI 回复建议</h3>${draft.reply?`<div class="support-message-text" dir="auto">${E(draft.reply)}</div><p>建议语言：${E(language(draft.language||t.language))}${Number.isFinite(draft.confidence)?` · 参考置信度 ${E(Math.round(draft.confidence*100))}%`:''}</p><p>${E(draft.reason||'请核对后再处理。')}</p>`:`<p>${E(draft.reason||'尚未生成建议。资料不足、涉及退款或无法确认的情况需人工处理。')}</p>`}</section>`}`;
  }
  async function details(id){const active=guard(),[result]=await Promise.all([api(`/api/commerce/support/threads/${Number(id)}`),snapshot?Promise.resolve():refresh(false)]);if(active())modal('客服会话',detailsBody(result));return result;}

  async function intake(){
    const active=guard();if(!snapshot)await refresh(false);if(!active())return;
    const available=targets().filter(t=>t.environment==='production'&&t.enabled!==false);
    if(!available.length){modal('录入客户消息','<p>请先在平台同步中配置正式店铺账号和地区。</p>');return;}
    modal('录入客户消息',`<p class="c-hint">将平台后台收到的真实客户消息归入所属店铺与地区。保留原会话和消息编号，同一消息重复录入不会新增会话。录入后由系统判断 AI 回复或转人工。</p><div class="c-grid">${select('target_id','所属平台、店铺与地区',target||available[0].id,available.map(t=>[t.id,targetName(t)]))}${input('external_thread_id','平台会话编号','','text','required maxlength="300"')}${input('external_message_id','平台消息编号（避免重复录入）','','text','required maxlength="300"')}${input('subject','会话主题','','text','maxlength="500"')}${input('external_order_id','关联平台订单号（选填）','','text','maxlength="300"')}${select('preferred_language','客户明确要求的回复语言（选填）','',[['','未指定，按店铺地区'],...languageChoices()])}${textarea('text','客户原始消息')}</div>`,async form=>{
      const text=String(form.get('text')||'').trim();if(!text)throw Error('请填写客户原始消息。');
      const result=await call('/support/messages',{target_id:Number(form.get('target_id')),external_thread_id:String(form.get('external_thread_id')||'').trim(),external_message_id:String(form.get('external_message_id')||'').trim(),subject:form.get('subject'),external_order_id:form.get('external_order_id'),preferred_language:form.get('preferred_language')||'',text});
      await refresh(false);return {afterSave:()=>details(result.thread.id)};
    },'保存客户消息');
  }
  async function settings(){
    const active=guard();await refresh(false);if(!active())return;
    modal('店铺客服设置',`<p class="c-hint">按店铺地区管理实际收件连接与人工处理。具备相应能力的平台可另行维护回复方案和知识。</p>${targets().length?table(['店铺与地区','收件与回复','处理方式','操作'],targets().map(t=>[`${E(targetName(t))}<small>默认语言：${E(language(t.language))}</small>`,`${t.capabilities?.receive?'可获取客户消息':'自动收件未接入'}<small>${t.capabilities?.send?'可提交回复':'回复需到平台后台办理'}</small>`,t.capabilities?.analysis===false?'人工处理':t.policy?.auto_reply?(t.capabilities?.send?'自动回复已开启':'开关已开启，待接通回复能力'):'自动回复已关闭',t.capabilities?.analysis===false?action('api-case-open',t.case_connection?.managed_by_target_id||t.id,'管理官方Case API'):action('policy',t.id,'方案与客服知识')])):'<div class="empty">暂无店铺地区，请先在平台同步中配置。</div>'}`);
  }
  let apiUI=null;
  function apiConnection(){
    if(!window.createSupportApi)throw Error('API连接界面未加载，请刷新页面。');
    return apiUI||(apiUI=window.createSupportApi({E,button,table,select,modal,call,api,dialog,refresh,render,notice,targetName,getTargets:targets}));
  }
  async function connections(){return apiConnection().connections();}
  async function original(id){
    const active=guard(),result=await api(`/api/commerce/support/messages/${Number(id)}/original`);if(!active())return;
    modal('原始对话正文',`<h3>${E(result.subject||'通知正文')}</h3><p>原平台时间：${E(result.received_at||'未记录')} · 正文归档 SHA-256：${E(result.receipt_sha256||'')}</p>${result.has_attachments?'<p>此记录含附件，请到亚马逊原会话查看附件。</p>':''}<div class="support-message-text" dir="auto">${E(result.text||'正文为空')}</div>`);
  }
  async function caseOriginal(id){
    const active=guard(),result=await api(`/api/commerce/support/case-contacts/${Number(id)}/original`);if(!active())return;
    modal('Case原始沟通',`<p>原沟通编号：${E(result.contact_id)} · ${E(time(result.received_at))} 北京时间</p><p>正文归档SHA-256：${E(result.receipt_sha256)}</p>${result.sections?.map(section=>`<h3>${E(section.label)}</h3><div class="support-message-text" dir="auto">${E(section.text)}</div>`).join('')||'<p>此原记录没有可读正文；元数据已保留。</p>'}${result.attachments?.length?`<p>附件：${result.attachments.map(E).join('、')}；请到亚马逊查看。</p>`:''}`);
  }
  function faqRow(item,index,defaultLanguage){
    const lang=item.language||defaultLanguage||'en',choices=languageChoices();if(!choices.some(x=>x[0]===lang))choices.unshift([lang,language(lang)]);
    return `<fieldset class="support-faq" data-support-faq><legend>客服知识 ${E(index+1)}</legend><input type="hidden" name="faq_id_${index}" value="${E(item.id||crypto.randomUUID())}">${input('faq_question_'+index,'客户问题',item.question||'')}${textarea('faq_answer_'+index,'经确认的回复内容',item.answer||'')}${select('faq_language_'+index,'答案语言',lang,choices)}<label class="support-check"><input type="checkbox" name="faq_enabled_${index}" ${item.enabled!==false?'checked':''}>启用这条知识</label><button type="button" data-support-faq-remove>移除本条</button></fieldset>`;
  }
  async function policy(id){
    if(targetFor(id)?.capabilities?.analysis===false)return apiConnection().open(id);
    const active=guard(),result=await api(`/api/commerce/support/targets/${Number(id)}/policy`);if(!active())return;
    const t=targetFor(id)||{},p=result.policy||result,knowledge=p.knowledge||[];
    const plans=getPlans().filter(plan=>plan.enabled!==false&&(['客服回复','客服','客户服务','customer_service','通用'].includes(plan.plan_type)||Number(plan.id)===Number(p.plan_id)));
    const receiveOptions=[['false',t.capabilities?.receive?'关闭自动接收客户消息':'自动收件未接入']];if(t.capabilities?.receive)receiveOptions.push(['true','自动接收客户消息']);
    const replyOptions=[['false','关闭自动回复，交由人工处理']];if(t.capabilities?.send)replyOptions.push(['true','启用 AI 自动回复']);
    modal('客服方案与知识 · '+targetName(t),`${capabilityNote(t.capabilities)}<p>地区默认回复语言：${E(language(t.language))}；客户明确要求其他语言时优先按客户要求处理。</p><div class="c-grid">${select('auto_sync','客户消息接收',String(p.auto_sync===true&&!!t.capabilities?.receive),receiveOptions)}${select('auto_reply','收到消息后的处理方式',String(p.auto_reply===true&&!!t.capabilities?.send),replyOptions)}${select('plan_id','客服 AI 方案',p.plan_id||'',[['','使用默认客服 / 通用方案'],...plans.map(plan=>[plan.id,plan.name])])}</div><p class="c-hint">在方案配置中可维护“客服回复”方案。AI 仅使用已启用的客服知识；没有可靠答案时转人工，退款、赔付等需人工核实。</p><h3>地区客服知识</h3><p class="c-hint">请使用所选语言填写经确认的答案；为其他语言添加独立答案。移除只在保存后生效。</p><div data-support-faq-list>${knowledge.map((item,index)=>faqRow(item,index,t.language)).join('')}</div><button type="button" data-support-faq-add>添加客服知识</button>`,async form=>{
      const entries=[];
      for(const [key,value] of form.entries()){
        if(!key.startsWith('faq_id_'))continue;
        const index=key.slice(7),question=String(form.get('faq_question_'+index)||'').trim(),answer=String(form.get('faq_answer_'+index)||'').trim();
        if(!question&&!answer)continue;if(!question||!answer)throw Error('请为每条客服知识同时填写问题和答案。');
        entries.push({id:value,question,answer,language:String(form.get('faq_language_'+index)||t.language||'en').trim(),enabled:form.get('faq_enabled_'+index)==='on'});
      }
      await call(`/support/targets/${Number(id)}/policy`,{revision:p.revision||0,auto_sync:form.get('auto_sync')==='true',auto_reply:form.get('auto_reply')==='true',plan_id:form.get('plan_id')?Number(form.get('plan_id')):null,knowledge:entries},'PUT');
      await refresh(false);return '客服方案与知识已保存';
    },'保存客服设置');
    let nextIndex=knowledge.length;
    const form=currentForm();
    form?.addEventListener?.('click',event=>{
      const add=event.target.closest('[data-support-faq-add]'),remove=event.target.closest('[data-support-faq-remove]');
      if(add)form.querySelector('[data-support-faq-list]').insertAdjacentHTML('beforeend',faqRow({},nextIndex++,t.language));
      if(remove)remove.closest('[data-support-faq]').remove();
    });
  }
  async function editThread(kind,id){
    const active=guard(),result=await api(`/api/commerce/support/threads/${Number(id)}`);if(!active())return;
    const t=result.thread,c=result.capabilities||{};
    if(t.status==='closed')throw Error('此会话已完成；客户的新消息到达后会重新进入处理。');
    const uncertain=(result.messages||[]).some(m=>['sending','unknown'].includes(m.delivery_status));
    if(uncertain&&kind!=='claim')throw Error('请先核对尚未确认的发送结果。');
    const save=async (suffix,payload)=>{await call(`/support/threads/${t.id}/${suffix}`,{revision:t.revision,...payload});await refresh(false);return {afterSave:()=>details(t.id)};};
    if(kind==='claim')modal('领取 / 指派客服会话',`${input('assignee','处理人姓名',t.assignee||'','text','required maxlength="120"')}<p class="c-hint">领取后由人工负责当前会话，并保留后续处理记录。</p>`,f=>save('claim',{assignee:String(f.get('assignee')||'').trim()}),'保存处理人');
    if(kind==='escalate')modal('转人工处理',textarea('reason','需要人工处理的原因',t.last_reason||''),f=>{const reason=String(f.get('reason')||'').trim();if(!reason)throw Error('请填写转人工原因。');return save('escalate',{reason});},'提交人工队列');
    if(kind==='close')modal('登记处理结论并关闭',`<p class="c-hint">请记录实际办理结果、平台凭据或无需回复的原因。这里只关闭本系统工单；消息是否已发送仍以平台回执为准。</p>${textarea('resolution','处理结论')}`,f=>{const resolution=String(f.get('resolution')||'').trim();if(!resolution)throw Error('请填写实际处理结论。');return save('close',{resolution});},'保存结论并关闭');
    if(kind==='reply'){
      const requestId=crypto.randomUUID();
      modal(c.send?'人工回复客户':'回复草稿 · 到平台后台办理',`${capabilityNote(c)}<p>回复语言：${E(language(t.language))}</p>${textarea('text',c.send?'准备发送的回复内容':'待复制的回复草稿',t.ai_draft?.reply||'')}<button type="button" data-support-copy>复制回复内容</button><p data-support-copy-status role="status"></p>`,c.send?f=>{const text=String(f.get('text')||'').trim();if(!text)throw Error('请填写回复内容。');return save('reply',{text,request_id:requestId});}:null,'提交到平台并核对结果');
      const form=currentForm(),copy=form?.querySelector('[data-support-copy]');
      if(copy)copy.onclick=async()=>{
        const field=form.querySelector('[name=text]'),status=form.querySelector('[data-support-copy-status]');
        try{if(!field.value.trim())throw Error('请先填写回复内容。');await navigator.clipboard.writeText(field.value);if(currentForm()===form)status.textContent='回复内容已复制，请到平台后台发送。';}
        catch(error){if(currentForm()===form){field.focus();field.select();status.textContent=field.value.trim()?'无法自动复制，已选中回复内容，请手动复制。':error.message;}}
      };
    }
  }
  async function sync(){
    const active=guard();if(!snapshot)await refresh(false);if(!active())return;
    const available=targets().filter(t=>t.capabilities?.receive&&(!target||t.id===Number(target)));
    if(!available.length){modal('获取客户消息','<p>所选店铺尚未建立后台连接，请在本系统连接亚马逊账号。</p>');return;}
    modal('获取客户消息',`<p>将获取以下店铺地区的买家会话；支持Case由独立官方API连接同步：</p><ul>${available.map(t=>`<li>${E(targetName(t))}</li>`).join('')}</ul><p data-support-sync-status role="status"></p>`,async()=>{
      const form=currentForm(),result=await call('/support/sync',{target_ids:available.map(t=>t.id)});
      if(!result.jobs?.length)throw Error('未创建收件任务，请检查连接状态。');
      if(typeof window.setTimeout!=='function'){await refresh(false);return `已提交 ${result.jobs.length} 个收件任务，完成后消息会显示在客服中心。`;}
      for(let step=0;step<20;step++){
        if(currentForm()!==form||!dialog?.open)return '界面跟踪已停止，后台收件任务仍可从连接状态查看。';
        const jobs=await Promise.all(result.jobs.map(job=>api(`/api/commerce/support/sync-jobs/${job.id}`)));
        const failed=jobs.find(job=>['failed','needs_action','paused','unknown'].includes(job.status)||job.status==='completed'&&job.result?.status!=='completed');
        if(failed)throw Error(failed.result?.reason||failed.error||'收件未完成，请查看通道错误；原消息保留。');
        const done=jobs.filter(job=>job.status==='completed'),received=done.reduce((count,job)=>count+(Number(job.result?.received)||0),0);
        const status=form?.querySelector?.('[data-support-sync-status]');if(status)status.textContent=`已完成 ${done.length}/${jobs.length} 个店铺，本轮新增 ${received} 条原始对话。`;
        if(done.length===jobs.length){await refresh(false);return `本轮新增 ${received} 条原始对话。${jobs.some(job=>job.result?.has_more)?'历史会话仍在分页，后台将继续同步。':'本轮同步完成。'}`;}
        await new Promise(resolve=>window.setTimeout(resolve,1500));
      }
      await refresh(false);return '本轮仍在后台收件，已停止等待；连接状态会显示后续结果。';
    },'获取消息');
  }
  async function dispatch(name,id){
    const key=name+':'+id;if(pending.has(key))return;pending.add(key);
    try{
      if(name==='support-refresh'){await refresh();return;}
      if(name==='support-intake')return await intake();
      if(name==='support-settings')return await settings();
      if(name==='support-connections')return await connections();
      if(name.startsWith('support-api-'))return await apiConnection().dispatch(name.slice(12),id);
      if(name==='support-case-original')return await caseOriginal(id);
      if(name==='support-original')return await original(id);
      if(name==='support-policy')return await policy(id);
      if(name==='support-sync')return await sync();
      if(name==='support-detail')return await details(id);
      if(['support-claim','support-reply','support-close','support-escalate'].includes(name))return await editThread(name.slice(8),id);
      if(['support-ai','support-reconcile'].includes(name)){
        const active=guard(),result=await call(`/support/threads/${Number(id)}/${name.slice(8)}`,{});await refresh(false);render();
        if(active())await details(id);
        if(name==='support-ai')notice(result.job?'AI 分析已排队，稍后刷新会话查看建议和转人工原因。':'AI 分析状态已更新，请查看会话。');
        else notice('已核对发送状态，处理结果请查看会话记录。');
      }
    }finally{pending.delete(key);}
  }
  return {listing,stats,refresh,details,dispatch};
};
