(() => {
  'use strict';
  const main=document.querySelector('main'),message=document.querySelector('#message');
  const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const statuses={queued:'待开工',started:'生产中',completed:'已完成',cancelled:'已停止',stop_requested:'待停产'};
  let ended=false,loading=false,changing=false;
  const request=async(url,options={})=>{
    const response=await fetch(url,{credentials:'same-origin',cache:'no-store',...options});
    if(response.status===401||response.status===403){
      ended=true;main.replaceChildren();message.textContent='会话已失效，请重新登录。';
      location.replace('/commerce-login');throw new Error(message.textContent);
    }
    const data=await response.json().catch(()=>null);
    if(!response.ok)throw new Error(typeof data?.detail==='string'?data.detail:'操作未完成，请稍后重试。');
    return data;
  };
  const actions=row=>{
    const id=escape(row.id),button=(action,label)=>`<button type="button" data-id="${id}" data-action="${action}">${label}</button>`;
    if(row.status==='stop_requested')return button('stop','确认停止生产');
    if(!['queued','started'].includes(row.status))return '';
    return (row.status==='queued'?button('start','开工'):'')+button('complete','完成入库')+button('stop','停止生产');
  };
  const render=row=>`<article><h2>${escape(row.name)} · ${escape(row.quantity)} 件</h2><p>状态：${escape(statuses[row.status]||row.status)}　期限：${escape(row.deadline||'未提供')}</p><pre>${escape(row.facts)}</pre>${row.detail_page?`<p>图文详情页：版本 ${escape(row.detail_page.run_id)} · ${escape(row.detail_page.sections)} 段 · ${escape(row.detail_page.language)}，已包含在生产资料下载包中。</p>`:''}<table><tr><th>组件</th><th>总数量</th><th>尺寸 / 材质</th></tr>${(row.components||[]).map(component=>`<tr><td>${escape(component.name)}</td><td>${escape(component.total_quantity)}</td><td>${escape(component.size)} ${escape(component.material)}</td></tr>`).join('')}</table><p><a href="/api/commerce/export/production/${encodeURIComponent(row.id)}">下载生产资料</a>${actions(row)}</p></article>`;
  async function load(){
    if(ended||loading)return;
    loading=true;
    try{
      const rows=await request('/api/commerce/production-queue');
      if(ended)return;
      if(!Array.isArray(rows))throw new Error('生产任务读取失败，请刷新重试。');
      main.innerHTML=rows.map(render).join('');
      message.textContent=rows.length?'仅显示生产所需资料':'暂无生产任务';
    }catch(error){message.textContent=error instanceof TypeError?'连接失败，请检查网络后重试。':error.message;}
    finally{loading=false;}
  }
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-action]');
    if(!button||ended||changing||loading)return;
    changing=true;button.disabled=true;
    try{
      await request(`/api/commerce/production/${encodeURIComponent(button.dataset.id)}/${encodeURIComponent(button.dataset.action)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      await load();
    }catch(error){message.textContent=error instanceof TypeError?'连接失败，请刷新核对生产状态后重试。':error.message;}
    finally{changing=false;button.disabled=false;}
  });
  load();setInterval(()=>{if(!changing)load();},30000);
})();
