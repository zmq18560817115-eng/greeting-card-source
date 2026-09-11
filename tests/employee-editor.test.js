const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function editor(){
  const nodes=new Map(),listeners={},reports=[],toasts=[],bindings=[];
  function node(selector){
    if(!nodes.has(selector))nodes.set(selector,{value:'',textContent:'',hidden:false,open:false,handlers:{},
      addEventListener(type,fn){(this.handlers[type]??=[]).push(fn);},add(){},setAttribute(){},focus(){},
      showModal(){this.open=true;},close(){if(this.open){this.open=false;for(const fn of this.handlers.close||[])fn();}},
      reset(){},reportValidity(){return true;},elements:{namedItem:name=>node('[name='+name+']')}});
    return nodes.get(selector);
  }
  const context=vm.createContext({URL,location:{origin:'http://localhost:8848'},
    document:{querySelector:node,querySelectorAll:()=>[],addEventListener(type,fn){(listeners[type]??=[]).push(fn);}},
    window:{addEventListener(){}},localStorage:{getItem:()=>null},Option:function(){},setInterval(){},
    RowSelection:{attach(){}},StaffFilters:require('../app/static/staff-filters.js'),
    ReviewStatus:require('../app/static/review-status.js'),ReviewSearch:require('../app/static/review-search.js')});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../app/static/workspace.js'),'utf8'),context);
  context.updateBusy=()=>{};context.loadStaff=async()=>{};
  context.report=(...args)=>reports.push(args);context.toast=value=>toasts.push(value);
  context.trackEmployeeBinding=value=>bindings.push(value);
  context.editEmployee({id:1,name:'离线员工',department:'测试部门',employee_no:'001',active:1,birth_date:'1990-09-11',feishu_open_id:'ou_test'});
  return {context,node,reports,toasts,bindings,submit:()=>node('#employee-form').onsubmit({preventDefault(){},target:node('#employee-form')}),
    close:()=>{for(const fn of listeners.click||[])fn({target:{closest:selector=>selector==='[data-close]'?{dataset:{close:'employee-dialog'}}:null}});}};
}
const saved={ok:true,id:1,binding:{status:'running',task_id:'test-job',msg:'资料已保存，正在后台对应'}};

test('local save closes the editor and tracks binding without waiting for it',async()=>{
  const app=editor();let finish;
  app.context.post=(url,payload)=>{
    assert.equal(url,'/api/employees?defer_binding=true');assert.equal(payload.id,1);
    assert.equal(payload.birth_date,'1990-09-11');
    return new Promise(resolve=>{finish=resolve;});
  };
  const pending=app.submit();
  assert.equal(app.node('#employee-save').textContent,'保存中…');
  assert.equal(app.node('#employee-dialog').open,true);
  finish(saved);await pending;
  assert.equal(app.node('#employee-dialog').open,false);
  assert.equal(app.node('#employee-save').textContent,'保存资料');
  assert.equal(app.node('#employee-save-state').hidden,true);
  assert.deepEqual(app.bindings,[saved.binding]);
  assert.ok(app.toasts.includes('员工资料已保存'));
});
test('a validation failure keeps input and unlocks a retry',async()=>{
  const app=editor();app.context.post=async()=>{throw new Error('用户 ID 已属于其他员工');};
  await app.submit();
  assert.equal(app.node('#employee-dialog').open,true);
  assert.equal(app.node('#employee-error').textContent,'用户 ID 已属于其他员工');
  assert.equal(app.node('[name=employee_no]').value,'001');
  assert.equal(app.node('#employee-save').textContent,'保存资料');
  app.context.post=async()=>saved;await app.submit();
  assert.equal(app.node('#employee-dialog').open,false);
});
test('the close button works during a slow save and a second click cannot submit twice',async()=>{
  const app=editor();let finish,calls=0;
  app.context.post=()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
  const pending=app.submit();await app.submit();assert.equal(calls,1);
  app.close();assert.equal(app.node('#employee-dialog').open,false);
  assert.ok(app.toasts.some(message=>message.includes('保存仍在处理')));
  finish(saved);await pending;
  assert.ok(app.toasts.includes('员工资料已保存'));
});
test('background binding submission failure still reports a successful local save',async()=>{
  const app=editor();app.context.post=async()=>({...saved,binding:{status:'failed',msg:'资料已保存，对应任务启动失败'}});
  await app.submit();
  assert.equal(app.node('#employee-dialog').open,false);
  assert.equal(app.node('#employee-error').textContent,'');
  assert.equal(app.reports[0][1],'员工资料已保存');
  assert.equal(app.reports[0][3],true);
});
