'use strict';
const BATCH_COLUMNS={name:'姓名',department:'部门',employee_no:'工号',join_date:'入职日期',birth_date:'出生年月',active:'在职状态'};
let BATCH_ORIGINAL=[],BATCH_DRAFT=[];
function batchChanges(){
  return BATCH_DRAFT.map((row,index)=>{
    const original=BATCH_ORIGINAL[index],changes={};
    for(const key of Object.keys(BATCH_COLUMNS)){
      const value=key==='active'?Number(row[key]):String(row[key]??'').trim();
      const before=key==='active'?Number(original[key]):String(original[key]??'').trim();
      if(value!==before)changes[key]=value;
    }
    return {id:original.id,original:Object.fromEntries(Object.keys(BATCH_COLUMNS).map(k=>[k,original[k]??null])),changes};
  }).filter(item=>Object.keys(item.changes).length);
}
function updateBatchDirty(){
  const count=batchChanges().length;
  $('#staff-batch-dirty').textContent=count?`已修改 ${count} 人 · 共选中 ${BATCH_DRAFT.length} 人`:'尚未修改';
  $('#staff-batch-save').textContent=count?`保存 ${count} 人的更正`:'保存更正';
  $('#staff-batch-save').disabled=!count;
}
function renderBatchTable(){
  $('#staff-batch-table').innerHTML='<table class="data-table"><thead><tr><th>序号</th>'+Object.values(BATCH_COLUMNS).map(label=>`<th>${esc(label)}</th>`).join('')+'</tr></thead><tbody>'+BATCH_DRAFT.map((row,index)=>`<tr><td>${index+1}</td>`+Object.entries(BATCH_COLUMNS).map(([key,label])=>{
    const attr=`data-batch-index="${index}" data-batch-key="${key}" aria-label="第 ${index+1} 行${label}"`;
    if(key==='active')return `<td><select ${attr}><option value="1" ${Number(row.active)===1?'selected':''}>在职</option><option value="0" ${Number(row.active)===0?'selected':''}>离职</option></select></td>`;
    return `<td><input ${attr} type="${key.endsWith('_date')?'date':'text'}" value="${esc(row[key]??'')}" ${['name','department'].includes(key)?'required':''}></td>`;
  }).join('')+'</tr>').join('')+'</tbody></table>';
  updateBatchDirty();
}
$('#edit-selected').onclick=()=>{
  const selected=STAFF.filter(row=>STAFF_SELECTED.has(String(row.id)));
  if(!selected.length)return;
  if(selected.length>1000){report('staff','请缩小更正范围',['每次最多更正 1000 人，请按部门或日期筛选后再选择。'],true);return;}
  BATCH_ORIGINAL=clone(selected);BATCH_DRAFT=clone(selected);
  $('#staff-batch-form').reset();$('#staff-batch-field').onchange();$('#staff-batch-error').textContent='';
  $('#staff-batch-count').textContent=`已选 ${selected.length} 人`;
  renderBatchTable();$('#staff-batch-dialog').showModal();
};
$('#staff-batch-field').onchange=()=>{
  const key=$('#staff-batch-field').value,status=key==='active';
  $('#staff-batch-value').hidden=status;$('#staff-batch-value').disabled=status;
  $('#staff-batch-active').hidden=!status;$('#staff-batch-active').disabled=!status;
  $('#staff-batch-value').type=key.endsWith('_date')?'date':'text';$('#staff-batch-value').value='';
};
$('#staff-batch-table').addEventListener('input',e=>{
  const key=e.target.dataset.batchKey,index=Number(e.target.dataset.batchIndex);if(!key||!BATCH_DRAFT[index])return;
  BATCH_DRAFT[index][key]=key==='active'?Number(e.target.value):e.target.value;
  $('#staff-batch-error').textContent='';updateBatchDirty();
});
$('#staff-batch-apply').onclick=()=>{
  const key=$('#staff-batch-field').value,input=key==='active'?$('#staff-batch-active'):$('#staff-batch-value');
  if(!input.value.trim()){$('#staff-batch-error').textContent='请填写要统一应用的内容；清空资料请在对应行操作。';return;}
  for(const row of BATCH_DRAFT)row[key]=key==='active'?Number(input.value):input.value.trim();
  $('#staff-batch-error').textContent='';renderBatchTable();
};
$('#staff-batch-reset').onclick=()=>{BATCH_DRAFT=clone(BATCH_ORIGINAL);$('#staff-batch-error').textContent='';renderBatchTable();};
$('#staff-batch-form').onsubmit=e=>{
  e.preventDefault();if(!e.target.reportValidity())return;
  const updates=batchChanges();if(!updates.length)return;
  busy('staff','staff',async()=>{
    try{
      const result=requireOK(await post('/api/employees/batch-update',{updates}));
      $('#staff-batch-dialog').close();STAFF_SELECTED.clear();
      report('staff',`批量更正完成：已更新 ${result.updated} 人`,[
        ...updates.map(item=>`${item.original.name}：${Object.keys(item.changes).map(k=>BATCH_COLUMNS[k]).join('、')}`),
        ...bindingDetails(result)
      ],Boolean(result.binding?.errors?.length));
      await loadStaff();
    }catch(error){$('#staff-batch-error').textContent=error.message;}
  });
};
