(() => {
  const list=document.querySelector('#user-list'),message=document.querySelector('#users-message');
  const form=document.querySelector('#create-user-form'),dialog=document.querySelector('#reset-password-dialog');
  let csrf='',resetTarget='',creating=false,resetting=false;
  const show=(text,error=false)=>{message.textContent=text;message.dataset.error=String(error);};
  const endSession=()=>{csrf='';list.replaceChildren();form.reset();document.querySelector('#reset-password-form').reset();if(dialog.open)dialog.close();location.replace('/commerce-login');};
  async function api(url,method='GET',data){
    const response=await fetch(url,{method,credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},...(data?{body:JSON.stringify(data)}:{})});
    if(response.status===401){endSession();throw new Error('请重新登录');}
    const result=await response.json().catch(()=>({}));if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'操作失败，请稍后重试');return result;
  }
  function button(text,action){const el=document.createElement('button');el.type='button';el.textContent=text;el.addEventListener('click',async()=>{el.disabled=true;try{await action();}catch(error){show(error.message,true);}finally{el.disabled=false;}});return el;}
  async function load(){
    const state=await api('/api/auth/session');
    if(!state.authenticated){endSession();throw new Error('请重新登录');}
    if(state.role!=='admin'){endSession();throw new Error('仅超级管理员可进入用户管理');}
    csrf=state.csrf_token;
    const result=await api('/api/users');list.replaceChildren();
    for(const user of result.users){
      const row=document.createElement('tr'),name=document.createElement('td'),role=document.createElement('td'),status=document.createElement('td'),actions=document.createElement('td');
      name.textContent=user.username;status.textContent=user.disabled?'已停用':(user.must_change_password?'下次登录须改密':'正常');
      const url='/api/users/'+encodeURIComponent(user.username);
      if(user.role==='admin'){role.textContent='超级管理员';actions.textContent='唯一管理账号 · 本人更改密码';}
      else{
        const select=document.createElement('select');select.setAttribute('aria-label',user.username+'的角色');
        for(const [value,label] of [['shareholder','股东（业务全功能）'],['operator','运营'],['production','生产']]){const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option);}select.value=user.role;role.append(select);
        actions.append(button('保存角色',async()=>{await api(url,'PATCH',{role:select.value});await load();show('角色已更新，旧会话已失效。');}));
        actions.append(button(user.disabled?'启用':'停用',async()=>{await api(url,'PATCH',{disabled:!user.disabled});await load();show(user.disabled?'用户已启用。':'用户已停用，旧会话已失效。');}));
        actions.append(button('重置密码',()=>{resetTarget=user.username;document.querySelector('#reset-target').textContent='账号：'+resetTarget;document.querySelector('#reset-error').textContent='';document.querySelector('#reset-password-form').reset();dialog.showModal();}));
      }
      row.append(name,role,status,actions);list.append(row);
    }
  }
  form.addEventListener('submit',async event=>{event.preventDefault();if(creating)return;if(!csrf){show('账号状态尚未读取，请刷新用户列表后重试。',true);return;}creating=true;const submit=form.querySelector('button');submit.disabled=true;try{await api('/api/users','POST',{username:form.elements.username.value.trim(),role:form.elements.role.value,password:form.elements.password.value});form.reset();await load();show('用户已创建。请通过安全方式告知本人初始密码。');}catch(error){show(error.message,true);}finally{creating=false;submit.disabled=false;}});
  document.querySelector('#refresh-users').addEventListener('click',()=>load().catch(error=>show(error.message,true)));
  document.querySelector('#cancel-reset').addEventListener('click',()=>{if(!resetting)dialog.close();});
  dialog.addEventListener('cancel',event=>{if(resetting)event.preventDefault();});
  dialog.addEventListener('close',()=>{document.querySelector('#reset-password-form').reset();resetTarget='';});
  document.querySelector('#reset-password-form').addEventListener('submit',async event=>{event.preventDefault();if(resetting||!resetTarget)return;resetting=true;const resetForm=event.currentTarget,submit=resetForm.querySelector('button[type=submit]');submit.disabled=true;try{await api('/api/users/'+encodeURIComponent(resetTarget),'PATCH',{password:resetForm.elements.password.value});dialog.close();await load();show('密码已重置，用户下次登录须更改密码。');}catch(error){document.querySelector('#reset-error').textContent=error.message;}finally{resetting=false;submit.disabled=false;}});
  window.addEventListener('pagehide',()=>{form.reset();document.querySelector('#reset-password-form').reset();});
  window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
  load().catch(error=>show(error.message,true));
})();
