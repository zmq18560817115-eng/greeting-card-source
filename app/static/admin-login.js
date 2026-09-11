'use strict';
window.AdminLogin=(()=>{
  const dialog=document.querySelector('#login-dialog'),form=document.querySelector('#login-form');
  const password=document.querySelector('#login-password'),error=document.querySelector('#login-error'),submit=document.querySelector('#login-submit');
  let waiting=null,done=null;
  function finish(ok){dialog.close();password.value='';const resolve=done;done=null;waiting=null;resolve?.(ok);}
  form.onsubmit=async e=>{
    e.preventDefault();if(submit.disabled||!form.reportValidity())return;
    submit.disabled=true;error.textContent='';
    try{
      const response=await fetch('/api/auth/check',{method:'POST',headers:{'X-Admin-Token':password.value},cache:'no-store'});
      if(!response.ok){error.textContent=response.status===401?'管理口令不正确，请向后台管理员确认。':'登录暂时失败，请稍后重试。';return;}
      const token=document.querySelector('#token');token.value=password.value;
      try{sessionStorage.setItem('gc_session_token',password.value);}catch{}
      finish(true);
    }catch{error.textContent='无法连接后台，请确认公司网络和后台地址。';}
    finally{submit.disabled=false;}
  };
  document.querySelector('#login-cancel').onclick=()=>finish(false);
  dialog.addEventListener('cancel',e=>{e.preventDefault();finish(false);});
  return {request(){
    if(waiting)return waiting;
    waiting=new Promise(resolve=>done=resolve);error.textContent='';dialog.showModal();password.focus();return waiting;
  }};
})();
