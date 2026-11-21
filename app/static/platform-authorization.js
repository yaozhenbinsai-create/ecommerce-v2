window.createPlatformAuthorization=function({E,input,select,table,show,call,api,dialog,refresh,guardDialog=()=>()=>true}) {
  async function open(id) {
    const current=guardDialog();
    const r=await api(`/api/commerce/accounts/${id}/connection`);
    if(!current())return;
    if(!r.supported){show('平台授权配置',`<p>${E(r.platform)}：授权适配器尚未接入。</p>`);return}
    const fields=[['client_id','LWA Client ID'],['client_secret','LWA Client Secret'],['refresh_token','店铺授权 Refresh Token']];
    const checks=fields.map(([key,label])=>[E(label),r.credentials.fields[key]?'已配置':'待填写']);
    show('平台账号 · 授权配置',`<p>${E(r.account_name)} · ${E(r.platform)}</p>`+
      `<div class="c-grid">${input('seller_id','商家 / Seller ID',r.seller_id,'text','required maxlength="160"')}${select('mode','凭据配置方式',r.mode==='environment'&&!r.credentials.error?'environment':'file',[['file','在此页面填写并保存'],['environment','使用服务环境变量（高级）']])}</div>`+
      `<div class="c-grid" data-auth-file>${fields.map(([key,label])=>input(key,label,'',key==='client_id'?'text':'password',`autocomplete="new-password" placeholder="${r.mode==='file'&&r.credentials.fields[key]?'已保存，留空保留':'请输入'}"`)).join('')}${input('client_secret_expires_on','Amazon控制台的密钥轮换截止日',r.credentials.rotation?.expires_on||'','date')}</div>`+
      `<div data-auth-env>${input('credential_env','服务环境变量名称',r.credential_env)}<p class="c-hint">该变量需在服务环境中配置，内容为三项 LWA 参数的 JSON。选择页面填写时不需要环境变量。</p></div>`+
      '<p class="c-hint">仅管理本公司店铺：在Amazon创建私有应用并自授权，再填写这里的参数，无需OAuth回调地址。密钥保存后不回显。更换应用密钥后，共用该应用的每个店铺都需更新。</p>'+
      `<p class="c-hint">${E(r.credentials.rotation?.message||'请登记Amazon控制台显示的密钥轮换截止日。')} 截止日用于提醒，不代表Amazon已验证。</p>`+
      table(['授权参数','当前状态'],checks)+(r.credentials.error?`<p class="c-hint">${E(r.credentials.error)}</p>`:'')+
      (r.targets.length?table(['已配置地区','连接状态','操作'],r.targets.map(t=>[E(t.name),t.verified?'已验证':t.enabled?'待验证':'已停用',t.enabled?`<button type="button" data-c-action="target-integration" data-c-id="${t.id}">接入检查</button><button type="button" data-c-action="target-connect" data-c-id="${t.id}">验证连接</button>`:''])):'')+
      `<p class="c-hint">地区与语言、配送模板在平台同步的对应账号行配置。${r.targets.length?'':'尚未配置真实店铺地区，保存后点击“选择地区”。'}</p>`,async f=>{
        const payload={revision:r.revision,seller_id:f.get('seller_id'),mode:f.get('mode'),credential_env:f.get('credential_env')||''};
        for(const [key] of fields)payload[key]=f.get(key)||'';
        payload.client_secret_expires_on=f.get('client_secret_expires_on')||'';
        const result=await call(`/accounts/${id}/credentials`,payload);await refresh();return result.message;
      },'保存授权配置');
    const mode=dialog.querySelector('[name=mode]');
    const update=()=>{for(const [selector,active] of [['[data-auth-file]',mode.value==='file'],['[data-auth-env]',mode.value==='environment']]){const box=dialog.querySelector(selector);box.hidden=!active;box.querySelectorAll('input').forEach(x=>x.disabled=!active)}};
    mode.onchange=update;update();
  }
  return {open};
};
