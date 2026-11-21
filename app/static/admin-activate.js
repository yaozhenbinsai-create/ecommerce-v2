(() => {
  const form=document.querySelector('#owner-password-form');
  if(!form)return;
  const password=document.querySelector('#owner-password'),confirm=document.querySelector('#owner-confirm');
  const error=document.querySelector('#activation-error'),button=form.querySelector('button');
  form.addEventListener('submit',async event=>{
    event.preventDefault();error.hidden=true;
    if(password.value!==confirm.value){error.textContent='两次密码不一致';error.hidden=false;return;}
    button.disabled=true;
    try{
      const response=await fetch('/admin-activate/finish',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Setup-CSRF':document.querySelector('#activation-csrf').value},body:JSON.stringify({password:password.value,confirm_password:confirm.value})});
      const result=await response.json();
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'初始密码未设置成功');
      password.value='';confirm.value='';location.replace('/commerce-login?setup=complete');
    }catch(exception){error.textContent=exception instanceof TypeError?'连接失败，请刷新核对是否已经设置完成。':exception.message;error.hidden=false;}
    finally{button.disabled=false;}
  });
  window.addEventListener('pagehide',()=>{password.value='';confirm.value='';});
})();
