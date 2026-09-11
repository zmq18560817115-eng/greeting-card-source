const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function login(fetch){
  const fields=new Map(),saved=new Map();
  const element=id=>{
    if(!fields.has(id))fields.set(id,{value:'',textContent:'',disabled:false,open:false,opens:0,
      showModal(){this.open=true;this.opens++;},close(){this.open=false;},focus(){},reportValidity(){return true;},
      addEventListener(type,fn){this[type]=fn;}});
    return fields.get(id);
  };
  const ctx={window:{},document:{querySelector:element},fetch,sessionStorage:{setItem:(k,v)=>saved.set(k,v)}};
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../app/static/admin-login.js'),'utf8'),ctx);
  return {request:ctx.window.AdminLogin.request,element,saved};
}
test('simultaneous unauthorized requests share one login and resume on success',async()=>{
  const calls=[],app=login(async(url,init)=>{calls.push({url,init});return {ok:true,status:200};});
  const first=app.request(),second=app.request();
  assert.equal(first,second);assert.equal(app.element('#login-dialog').opens,1);
  app.element('#login-password').value='test-admin';
  await app.element('#login-form').onsubmit({preventDefault(){}});
  assert.equal(await first,true);assert.equal(app.element('#token').value,'test-admin');
  assert.equal(app.element('#login-password').value,'');assert.equal(app.element('#login-dialog').open,false);
  assert.equal(calls[0].url,'/api/auth/check');assert.equal(calls[0].init.headers['X-Admin-Token'],'test-admin');
});
test('a wrong token keeps the login open and permits correction',async()=>{
  let attempt=0;const app=login(async()=>({ok:++attempt>1,status:attempt>1?200:401}));
  const waiting=app.request();app.element('#login-password').value='wrong';
  await app.element('#login-form').onsubmit({preventDefault(){}});
  assert.equal(app.element('#login-dialog').open,true);assert.match(app.element('#login-error').textContent,/不正确/);
  assert.equal(app.element('#login-submit').disabled,false);assert.equal(app.element('#token').value,'');
  app.element('#login-password').value='correct';await app.element('#login-form').onsubmit({preventDefault(){}});
  assert.equal(await waiting,true);assert.equal(app.element('#token').value,'correct');
});
test('cancelling a login never grants access or leaves a pending request',async()=>{
  const app=login(()=>assert.fail('no request after cancellation')),waiting=app.request();
  app.element('#login-cancel').onclick();assert.equal(await waiting,false);
  assert.equal(app.element('#token').value,'');assert.equal(app.element('#login-dialog').open,false);
});
