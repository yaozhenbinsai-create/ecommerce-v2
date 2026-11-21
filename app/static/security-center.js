(() => {
  const labels={https_config:'HTTPS访问',runtime_enabled:'安全模式',identity:'账号与登录',amazon_secrets:'亚马逊凭据',model_secrets:'模型凭据',secret_storage:'凭据保护',amazon_ai_egress:'客户数据与AI',security_audit:'安全事件记录',cloud_network:'云网络防护',storage_encryption:'数据与备份加密',retention:'数据保留与清理',incident_response:'事件响应制度',cloud_endpoint:'公网访问验证',staff_devices:'员工终端'};
  Object.assign(labels,{public_media:'商品图片公网访问',pii_access:'订单收件信息权限'});
  const states={configured:'已配置，待现场验证',missing:'待配置',unknown:'未确认',implemented:'已实现',not_implemented:'待开发',envelope_present_unverified:'加密封装待验证',external_evidence_required:'需部署及执行证据'};
  const button=document.querySelector('#refresh');
  async function refresh(){
    button.disabled=true;document.querySelector('#error').textContent='';
    try{
      const response=await fetch('/api/commerce/security-readiness',{credentials:'same-origin',cache:'no-store'});
      if(response.status===401||response.status===403){document.querySelector('#checks').replaceChildren();document.querySelector('#scope').textContent='';}
      if(response.status===401){location.replace('/commerce-login');return;}
      if(!response.ok)throw new Error(response.status===403?'需要管理员账号查看。':'检查未完成，请稍后重试。');
      const report=await response.json();
      document.querySelector('#scope').textContent=report.scope+' · '+new Date(report.checked_at).toLocaleString();
      const table=document.querySelector('#checks');table.replaceChildren();
      for(const item of report.checks){
        const row=document.createElement('tr');
        for(const value of [labels[item.key]||item.key,states[item.status]||item.status,item.message]){
          const cell=document.createElement('td');cell.textContent=value;row.append(cell);
        }
        table.append(row);
      }
    }catch(error){document.querySelector('#error').textContent=error.message;}
    finally{button.disabled=false;}
  }
  button.addEventListener('click',refresh);refresh();
})();
