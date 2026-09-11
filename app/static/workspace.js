
'use strict';
// All API/user text enters HTML through esc; event handlers never contain interpolated data.
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = value => JSON.parse(JSON.stringify(value));
const sameId = (a,b) => a != null && b != null && String(a) === String(b);
const isActive = e => e?.active === true || e?.active === 1 || e?.active === '1';
const openId = e => e?.feishu_open_id || e?.open_id || '';
const STATUS = Object.freeze({generating:'生成中',ready:'待确认',confirmed:'已确认待推送',pushing:'推送中',pushed:'已推送',failed:'推送失败',gen_failed:'生成失败',skipped:'已跳过',blocked:'核验阻止',simulated:'演练完成',needs_regeneration:'资料变更，需重新生成',delivery_unknown:'发送回执不明',expired:'日期已过期，停止发送'});
const DELIVERY = Object.freeze({sent:'已推送',not_sent:'未推送',sending:'推送中',unknown:'回执不明',simulated:'演练未发送'});
const own = (object,key) => Object.prototype.hasOwnProperty.call(object,key);
const labelFor = (map,key) => own(map,key) ? map[key] : String(key || '未知');
function badge(status, labels=STATUS){return `<span class="badge b-${own(labels,status)?esc(status):'unknown'}">${esc(labelFor(labels,status))}</span>`;}
function message(value){return typeof value === 'string' ? value : JSON.stringify(value ?? '未知错误');}
function toast(msg){$('#toast').textContent=msg;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,4500);}
function report(area,title,rows=[],hasError=false){
  const box=$('#'+area+'-reports');
  let toolbar=box.querySelector('.reports-toolbar');
  if(!toolbar){
    toolbar=document.createElement('div');toolbar.className='reports-toolbar';
    const label=document.createElement('span');label.className='meta';label.textContent='操作提醒 · 最多保留最近 5 条';
    const clear=document.createElement('button');clear.type='button';clear.textContent='清空提醒';clear.onclick=()=>box.replaceChildren();
    toolbar.append(label,clear);box.append(toolbar);
  }
  const card=document.createElement('div');card.className='report'+(hasError?' has-error':'');
  const details=document.createElement('details');details.open=true;
  const summary=document.createElement('summary');summary.textContent=title+' ';
  const time=document.createElement('time');time.textContent=new Date().toLocaleTimeString('zh-CN');summary.append(time);details.append(summary);
  const list=document.createElement('ul');for(const row of rows){const li=document.createElement('li');li.textContent=message(row);list.append(li);}details.append(list);
  const close=document.createElement('button');close.type='button';close.className='report-close';close.textContent='关闭';close.setAttribute('aria-label','关闭提醒：'+title);
  close.onclick=()=>{card.remove();if(!box.querySelector('.report'))box.replaceChildren();};
  card.append(details,close);for(const item of box.querySelectorAll('details'))item.open=false;toolbar.after(card);
  for(const item of [...box.querySelectorAll('.report')].slice(5))item.remove();
}
async function api(path, options={}){
  const headers=new Headers(options.headers||{});headers.set('X-Admin-Token',$('#token').value);
  if(options.body && !(options.body instanceof FormData))headers.set('Content-Type','application/json');
  const response=await fetch(path,{...options,headers,cache:'no-store'});const raw=await response.text();let data;
  try{data=raw?JSON.parse(raw):{};}catch{throw new Error(`接口未返回 JSON（HTTP ${response.status}）：${raw.slice(0,500)}`);}
  if(!response.ok)throw new Error(message(data.detail ?? data.msg ?? data.error ?? `HTTP ${response.status}`));
  return data;
}
function post(path,body){return api(path,{method:'POST',body:JSON.stringify(body)});}
function requireOK(result){if(result?.ok===false)throw new Error(message(result.msg ?? result.detail ?? result.error ?? '操作未成功'));return result;}
function fileURL(value){
  if(typeof value!=='string' || !value.trim())return '';
  try{const u=new URL(value,location.origin);return u.origin===location.origin && /^https?:$/.test(u.protocol) && u.pathname.startsWith('/files/') ? u.href : '';}catch{return '';}
}
const BUSY=new Set();let TAB='events';
async function busy(key,area,work){
  if(BUSY.has(key))return;BUSY.add(key);updateBusy();
  try{return await work();}catch(error){report(area,'操作未完成',[error.message],true);toast(error.message);}finally{BUSY.delete(key);updateBusy();}
}
function updateBusy(){
  for(const id of ['scan','confirm-all'])$('#'+id).disabled=BUSY.has('events') || (id==='confirm-all' && !EVENTS.some(e=>EVENT_SELECTED.has(String(e.id))&&canConfirm(e)));
  for(const id of ['add-employee','import-employees','download-template','sync-employees','kw','staff-filter','staff-department','staff-join-from','staff-join-to','staff-birth-from','staff-birth-to','staff-reset-filters'])$('#'+id).disabled=BUSY.has('staff');
  $('#employee-fields').disabled=BUSY.has('staff');
  $('#staff-batch-fields').disabled=BUSY.has('staff');
  for(const b of document.querySelectorAll('#staff button,#staff input'))b.disabled=BUSY.has('staff') || b.dataset.locked==='true';
  for(const b of document.querySelectorAll('#events button[data-action],#event-detail button[data-action]'))b.disabled=BUSY.has('events') || b.dataset.locked==='true';
  $('#tpl-work').disabled=!DRAFT || BUSY.has('tpl');$('#preview-tpl').disabled=BUSY.has('preview') || !DRAFT;
  updateSelection();updateEventSelection();
}
let confirmResolve=null;
function askConfirm(title,description,items=[],accept='确认'){
  if(confirmResolve)return Promise.resolve(false);
  $('#confirm-title').textContent=title;$('#confirm-message').textContent=description;$('#confirm-accept').textContent=accept;
  $('#confirm-items').replaceChildren(...items.map(text=>{const li=document.createElement('li');li.textContent=text;return li;}));
  $('#confirm-dialog').showModal();$('#confirm-cancel').focus();return new Promise(resolve=>confirmResolve=resolve);
}
function finishConfirm(ok){$('#confirm-dialog').close();const done=confirmResolve;confirmResolve=null;done?.(ok);}
$('#confirm-accept').onclick=()=>finishConfirm(true);$('#confirm-cancel').onclick=()=>finishConfirm(false);
$('#confirm-dialog').addEventListener('cancel',e=>{e.preventDefault();finishConfirm(false);});
document.addEventListener('click',e=>{const b=e.target.closest('[data-close]');if(b&&(!['employee-dialog','staff-batch-dialog','background-dialog'].includes(b.dataset.close)||!BUSY.has(b.dataset.close==='background-dialog'?'tpl':'staff')))$('#'+b.dataset.close).close();});
for(const id of ['employee-dialog','staff-batch-dialog'])$('#'+id).addEventListener('cancel',e=>{if(BUSY.has('staff'))e.preventDefault();});
try{$('#token').value=localStorage.getItem('gc_token')||'';}catch{/* The input also works when browser storage is unavailable. */}
$('#token').oninput=()=>{try{localStorage.setItem('gc_token',$('#token').value);}catch{}};
document.querySelectorAll('nav button').forEach(b=>b.onclick=async()=>{
  TAB=b.dataset.tab;for(const t of ['events','staff','tpl','sys'])$('#tab-'+t).hidden=t!==TAB;
  document.querySelectorAll('nav button').forEach(n=>{if(n===b)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});await refresh();
});
async function refresh(){if(TAB==='events')await loadEvents();if(TAB==='staff')await loadStaff();if(TAB==='tpl')await loadTpl();if(TAB==='sys'){await loadFeishuConfig();await loadHealth();}}
$('#refresh').onclick=refresh;

// Event actions re-read state immediately before posting. Terminal deliveries remain read-only.
let EVENTS=[],ALL_EVENTS=[],EVENT_SELECTED=new Set(),eventLoad=0,eventLoading=false;const EVENT_LOGS=new Map();
function anniversaryText(years){return Number.isInteger(years)&&years>=0?(years===0?'未满 1 年':years+' 周年'):'待补入职日期';}
function deliveryState(e){return e.delivery_state||(e.status==='delivery_unknown'?'unknown':e.status==='simulated'?'simulated':e.pushed_at||e.status==='pushed'?'sent':e.status==='pushing'?'sending':'not_sent');}
function pushDates(e){
  const actual=e.actual_push_at??(deliveryState(e)==='sent'?e.pushed_at:null),planned=e.planned_push_at||e.trigger_at;
  return `${actual?`<span>实际 ${esc(actual)}</span>`:deliveryState(e)==='sent'?'<span>实际时间未记录</span>':'<span class="muted">尚无实际发送时间</span>'}${planned?`<small class="muted">计划 ${esc(planned)}</small>`:''}`;
}
function identityProblem(emp){
  if(!emp || !isActive(emp))return '员工已停用，或无法取得最新在职资料';
  if(!String(emp.name||'').trim() || !String(openId(emp)).trim())return '请补全姓名，连接飞书后同步名单以自动对应 ID';
  if(emp.identity_status==='failed')return emp.identity_hint || emp.identity_error || '飞书姓名与 ID 对应异常，请同步名单后处理提示';return '';
}
function terminal(e){return Boolean(e.pushed_at) || ['pushed','pushing','delivery_unknown','expired'].includes(e.status);}
function selectedCard(e){return (e.cards||[]).find(c=>sameId(c.id,e.selected_card_id) && c.status==='ok' && fileURL(c.url));}
function previewCard(e){
  // Keep the confirmed/sent poster; never replace a missing selection with a different legacy option.
  if(e.selected_card_id!=null||terminal(e))return selectedCard(e);
  return (e.cards||[]).find(c=>c.status==='ok'&&fileURL(c.url));
}
function canConfirm(e){return e.status==='ready'&&!terminal(e)&&!identityProblem(e.employee)&&Boolean(previewCard(e));}
function canPush(e){return ['confirmed','failed'].includes(e.status)&&!terminal(e)&&!identityProblem(e.employee)&&Boolean(selectedCard(e));}
function canRegen(e){return !terminal(e)&&['ready','confirmed','failed','gen_failed','blocked','needs_regeneration','simulated'].includes(e.status)&&isActive(e.employee);}
function canSkip(e){return !terminal(e)&&['ready','confirmed','failed','gen_failed','blocked','needs_regeneration'].includes(e.status);}
function eventButton(action,id,text,enabled,primary=false){return `<button data-action="${action}" data-id="${esc(id)}" data-locked="${!enabled}" ${enabled?'':'disabled'} class="${primary?'primary':''}">${text}</button>`;}
function renderEventDetails(e){
  const emp=e.employee||{},reason=identityProblem(emp),preview=previewCard(e);
  let instruction='';
  if(e.status==='delivery_unknown')instruction='发送请求已发出，但回执不明。请到飞书核实员工是否已收到消息；禁止重推和重新生成。';
  else if(e.status==='pushed'||e.pushed_at)instruction='已完成推送，不能再次确认、重新生成或重发。';
  else if(e.status==='expired')instruction='事件日期已过期，已停止发送，不能确认或推送。';
  else if(e.status==='ready')instruction=reason||(preview?'请放大核对文案和收件人，再确认发送排期。':'当前海报不可用，请重新生成后审核。');
  else if(e.status==='needs_regeneration')instruction='员工资料已变更，请重新生成海报后再次确认。';
  else if(e.status==='blocked')instruction=reason||'核验已更新，请重新生成后再审核。';
  else if(e.status==='simulated')instruction='演练完成，未向飞书发送消息。可重新生成后再次审核。';
  else if(e.status==='gen_failed')instruction='生成失败，可查看错误原因后重新生成。';
  else if(e.status==='generating')instruction='正在生成，页面将自动更新。';
  else if(['confirmed','failed'].includes(e.status))instruction=reason||(selectedCard(e)?'已确认收件人与海报，可立即推送。':'当前海报不可用，请重新生成并审核。');
  const actions=terminal(e)?'':eventButton('confirm',e.id,'确认发送排期',canConfirm(e),true)+eventButton('push',e.id,e.status==='failed'?'重试推送':'立即推送',canPush(e))+eventButton('regenerate',e.id,'重新生成',canRegen(e))+eventButton('skip',e.id,'跳过本次',canSkip(e));
  return `<article class="event-review">
    <div class="review-heading"><strong>${esc(emp.name||'员工资料不可用')}</strong><span class="badge">${esc(e.event_type==='birthday'?'生日':e.event_type==='anniversary'?'入职周年':e.event_type)}</span>${badge(ReviewStatus.of(e),ReviewStatus.labels)}<span class="meta">${esc(e.event_date)}${e.years!=null?' · '+esc(e.years)+(e.event_type==='birthday'?' 岁':' 周年'):''}</span></div>
    <div class="review-layout"><div class="review-information">
      <dl class="review-facts"><div><dt>工号</dt><dd>${esc(emp.employee_no||'未填写')}</dd></div><div><dt>部门</dt><dd>${esc(emp.department||'未填写')}</dd></div><div><dt>入职日期</dt><dd>${esc(emp.join_date||'未填写')}</dd></div><div><dt>生日</dt><dd>${esc(emp.birth_date_display||emp.birth_date||'未填写')}</dd></div><div><dt>入职周年数 · 自动计算</dt><dd>${esc(anniversaryText(e.anniversary_years))}</dd></div><div><dt>员工 ID</dt><dd>${esc(emp.id??'未记录')}</dd></div><div><dt>是否推送</dt><dd>${badge(deliveryState(e),DELIVERY)}</dd></div><div class="full"><dt>飞书 open_id</dt><dd class="review-open-id">${esc(openId(emp)||'未填写')}</dd></div><div class="full"><dt>推送日期</dt><dd class="audit-stacked">${pushDates(e)}</dd></div></dl>
      <p class="review-instruction">${esc(instruction||'可查看海报和推送记录。')}</p>
      ${e.exception_hint?`<p class="error">异常提醒：${esc(e.exception_hint)}</p>`:''}
      ${e.last_error||emp.identity_error?`<details class="review-error"><summary>技术详情</summary><p class="error">${esc([e.last_error,emp.identity_error].filter(Boolean).join('\n'))}</p></details>`:''}
    </div><aside class="review-posters"><p class="meta">海报预览</p>
      ${preview?`<button class="review-preview" data-zoom="${esc(fileURL(preview.url))}" aria-label="查看${esc(emp.name||'')}海报大图"><img src="${esc(fileURL(preview.url))}" alt="${esc(emp.name||'')}海报缩略图"></button>`:'<p class="review-no-poster">尚无可用海报</p>'}
    </aside></div>
    <div class="review-actions">${actions}${eventButton('logs',e.id,EVENT_LOGS.has(String(e.id))?'收起推送记录':'推送记录',true)}</div>
    <div class="review-logs" data-log-id="${esc(e.id)}">${EVENT_LOGS.has(String(e.id))?'<pre>'+esc(EVENT_LOGS.get(String(e.id)))+'</pre>':''}</div>
  </article>`;
}
async function loadEvents(quiet=false){
  if(quiet&&RowSelection.isDragging($('#events')))return;
  const seq=++eventLoad;eventLoading=true;$('#events-loading').textContent='正在刷新…';
  try{
    const scope=$('#scope').value;
    const results=await Promise.allSettled([api('/api/events?scope='+encodeURIComponent(scope==='pending'?'all':scope)),api('/api/employees?keyword=&only_active=false')]);
    if(seq!==eventLoad||(quiet&&RowSelection.isDragging($('#events'))))return;if(results[0].status==='rejected')throw results[0].reason;if(!Array.isArray(results[0].value))throw new Error('事件列表格式错误');
    const employees=results[1].status==='fulfilled'&&Array.isArray(results[1].value)?results[1].value:null;
    if(!employees&&!quiet)report('event','员工核验信息加载失败',['当前海报仅供查看，请刷新后再执行确认或推送。'],true);
    let events=results[0].value.map(e=>({...e,employee:employees?.find(emp=>sameId(emp.id,e.employee_id??e.employee?.id))||{...e.employee,active:0,identity_status:'pending'}}));
    if(scope==='pending')events=events.filter(e=>ReviewStatus.of(e)!=='sent');
    ALL_EVENTS=events;refreshFilterOptions($('#event-department'),events.map(e=>e.employee?.department));renderEvents();
    $('#events-loading').textContent=employees?'已更新 '+new Date().toLocaleTimeString('zh-CN'):'核验信息不可用，操作已锁定';
  }catch(error){if(seq===eventLoad){ALL_EVENTS=[];EVENTS=[];EVENT_SELECTED.clear();$('#events').innerHTML='<div class="panel empty">加载失败，请检查管理口令或网络后刷新。</div>';$('#events-loading').textContent='加载失败';if(!quiet)report('event','海报加载失败',[error.message],true);}}
  finally{if(seq===eventLoad){eventLoading=false;updateBusy();}}
}

function refreshFilterOptions(select,values){
  const previous=select.value;select.innerHTML='<option value="">全部部门</option>'+[...new Set(values.filter(Boolean))].sort().map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
  if([...select.options].some(o=>o.value===previous))select.value=previous;
}
function renderEvents(){
  const kw=$('#event-search').value.trim().toLowerCase(),state=$('#event-status').value,type=$('#event-type').value,dept=$('#event-department').value,issues=$('#event-issues').value;
  EVENTS=ALL_EVENTS.filter(e=>ReviewStatus.of(e)===state&&(!type||e.event_type===type)&&(!dept||e.employee?.department===dept)&&(!issues||(issues==='issues'?Boolean(e.exception_hint):!e.exception_hint))&&(!kw||[e.employee?.name,e.employee?.employee_no,e.employee?.department,e.employee?.id,openId(e.employee)].some(v=>String(v||'').toLowerCase().includes(kw))));
  const direction=$('#event-sort').value==='desc'?-1:1;
  EVENTS.sort((a,b)=>direction*String(a.event_date).localeCompare(String(b.event_date)));
  EVENT_SELECTED=new Set([...EVENT_SELECTED].filter(id=>EVENTS.some(e=>sameId(e.id,id))));
  $('#evsum').textContent=EVENTS.length+' 条记录';
  $('#events').innerHTML='<div class="table-wrap audit-table-wrap"><table class="data-table audit-table"><thead><tr><th class="check-col"><input type="checkbox" id="event-all" aria-label="选择当前筛选的全部海报"></th><th class="audit-person">姓名</th><th>工号</th><th>部门</th><th>入职日期</th><th>生日</th><th title="根据入职日期自动计算，截至本条贺卡日期已满的周年数，无需导入">入职周年数 ⓘ</th><th>飞书 open_id</th><th>贺卡 / 日期</th><th>推送状态</th><th>推送日期</th><th>异常提醒</th><th>海报</th><th>操作</th></tr></thead><tbody>'+EVENTS.map((e,i)=>{
    const card=previewCard(e),url=card&&fileURL(card.url),emp=e.employee||{};
    return `<tr class="${EVENT_SELECTED.has(String(e.id))?'is-selected':''}"><td class="selection-cell"><input type="checkbox" aria-describedby="event-selection-help" data-event-check="${esc(e.id)}" aria-label="选择${esc(emp.name)}的海报" ${EVENT_SELECTED.has(String(e.id))?'checked':''}></td><td class="audit-person"><button class="person-link" data-action="detail" data-id="${esc(e.id)}">${esc(emp.name||'未知员工')}</button><small class="muted">员工 ID：${esc(emp.id??'—')}</small></td><td>${esc(emp.employee_no||'—')}</td><td>${esc(emp.department||'—')}</td><td>${esc(emp.join_date||'未填写')}</td><td>${esc(emp.birth_date_display||emp.birth_date||'未填写')}</td><td title="截至 ${esc(e.event_date)}，由入职日期自动计算">${esc(anniversaryText(e.anniversary_years))}</td><td class="id-cell" title="${esc(openId(emp))}">${esc(openId(emp)||'未绑定')}</td><td class="audit-stacked"><span class="type-tag ${e.event_type==='birthday'?'birthday':'anniversary'}">${e.event_type==='birthday'?'生日':'入职周年'}</span><small class="muted">${esc(e.event_date)}</small></td><td>${badge(ReviewStatus.of(e),ReviewStatus.labels)}</td><td class="audit-stacked audit-push-dates">${pushDates(e)}</td><td class="audit-notice">${ReviewStatus.note(e)?`<button class="audit-notice-link" data-action="detail" data-id="${esc(e.id)}" title="${esc(ReviewStatus.note(e))}">${esc(ReviewStatus.note(e))}</button>`:'<span class="muted">—</span>'}</td><td class="audit-poster-cell">${url?`<button class="audit-poster-image" data-zoom="${esc(url)}" aria-label="放大${esc(emp.name)}的海报"><img src="${esc(url)}" alt="${esc(emp.name)}海报缩略图" loading="lazy"></button>`:'<span class="muted" aria-label="尚无可用海报">—</span>'}</td><td class="row-actions"><div class="bar">${eventButton('detail',e.id,'核查',true)}${canConfirm(e)?eventButton('confirm',e.id,'确认',true):canPush(e)?eventButton('push',e.id,'推送',true):''}</div></td></tr>`;
  }).join('')+(EVENTS.length?'':'<tr><td colspan="14" class="empty">当前筛选下暂无海报</td></tr>')+'</tbody></table></div>';
  RowSelection.refresh($('#events'));updateEventSelection();
}
function updateEventSelection(){
  const chosen=EVENTS.filter(e=>EVENT_SELECTED.has(String(e.id)));
  $('#event-selected-count').textContent='已选 '+chosen.length+' 条';
  for(const [id,predicate] of [['confirm-all',canConfirm],['regenerate-selected',canRegen],['skip-selected',canSkip]])$('#'+id).disabled=BUSY.has('events')||!chosen.some(predicate);
  $('#clear-events').disabled=BUSY.has('events')||!chosen.length;
  const all=$('#event-all');if(all){all.checked=EVENTS.length>0&&chosen.length===EVENTS.length;all.indeterminate=chosen.length>0&&chosen.length<EVENTS.length;}
}
for(const id of ['event-type','event-department','event-sort','event-issues'])$('#'+id).onchange=renderEvents;
function clearDeliveryFilter(){
  $('#event-delivery').value='';$('#clear-delivery-filter').hidden=true;
}
$('#event-status').onchange=()=>{clearDeliveryFilter();renderEvents();};
$('#event-delivery').onchange=()=>{
  const result=$('#event-delivery').value;if(!['sent','failed'].includes(result))return;
  $('#event-status').value=result;$('#clear-delivery-filter').hidden=false;
  if(result==='sent'&&$('#scope').value==='pending'){$('#scope').value='all';loadEvents();}else renderEvents();
};
$('#clear-delivery-filter').onclick=()=>{clearDeliveryFilter();$('#event-status').value='pending';renderEvents();};
$('#event-search').oninput=renderEvents;
function showEventDetail(e){$('#event-detail').innerHTML=renderEventDetails(e);if(!$('#event-detail-dialog').open)$('#event-detail-dialog').showModal();}
async function ensureSelected(e){
  if(selectedCard(e))return e;
  const card=previewCard(e);
  if(!canConfirm(e)||!card)throw new Error('当前海报不可用，请重新生成后审核');
  requireOK(await post('/api/events/'+encodeURIComponent(e.id)+'/select',{card_id:card.id}));
  return freshEvent(e.id);
}
function batchEvents(action){return busy('events','event',async()=>{
  const check={confirm:canConfirm,regenerate:canRegen,skip:canSkip}[action];
  const chosen=EVENTS.filter(e=>EVENT_SELECTED.has(String(e.id))),list=chosen.filter(check);
  if(!list.length)throw new Error('所选记录没有可执行此操作的海报');
  const label={confirm:'确认排期',regenerate:'重新生成',skip:'跳过'}[action];
  if(!await askConfirm('批量'+label,'仅处理勾选且符合条件的记录。',list.map(recipient),action==='confirm'?'确认排期':'确认'+label))return;
  let ok=0;const details=[];
  for(const item of list){try{
    const latest=await unchangedEvent(item,check);
    if(action==='confirm')await ensureSelected(latest);
    requireOK(await post('/api/events/'+encodeURIComponent(item.id)+'/'+action,action==='confirm'?{operator:'hr'}:action==='regenerate'?{count:1}:{}));
    ok++;details.push(item.employee?.name+'：'+label+'成功');
  }catch(error){details.push(item.employee?.name+'：'+error.message);}}
  if(chosen.length>list.length)details.push((chosen.length-list.length)+' 条状态不符合，已跳过');
  report('event',label+'：成功 '+ok+' / '+list.length,details,ok!==list.length);
  EVENT_SELECTED.clear();await loadEvents();
});}
async function freshEvent(id){
  const [event,employees]=await Promise.all([api('/api/events/'+encodeURIComponent(id)),api('/api/employees?keyword=&only_active=false')]);
  if(!Array.isArray(employees)||!sameId(event.id,id))throw new Error('无法确认最新事件或员工资料，请刷新后重试');
  return {...event,employee:employees.find(e=>sameId(e.id,event.employee_id??event.employee?.id))};
}
function recipient(e){return `${e.employee?.name||'未知员工'} ｜ ${e.employee?.department||'未填部门'} ｜ ${openId(e.employee)||'未填飞书 open_id'} ｜ ${e.event_date||''}`;}
function reviewSnapshot(e){const card=previewCard(e);return JSON.stringify([e.id,e.status,e.employee?.id,e.employee?.name,e.employee?.department,openId(e.employee),e.employee?.join_date,e.employee?.birth_date,e.event_date,e.trigger_at,e.selected_card_id,card?.id,card?.url]);}
async function unchangedEvent(e,allowed){const latest=await freshEvent(e.id);if(!allowed(latest)||reviewSnapshot(e)!==reviewSnapshot(latest))throw new Error('员工、图片或事件状态已变化，请刷新后重新审核');return latest;}
async function eventAction(action,id){
  const cached=EVENTS.find(e=>sameId(e.id,id));if(!cached)return;
  return busy('events','event',async()=>{
    try{
      const base='/api/events/'+encodeURIComponent(id);
      if(action==='detail'){showEventDetail(cached);return;}
      if(action==='logs'){
        if(EVENT_LOGS.has(String(id)))EVENT_LOGS.delete(String(id));else{const rows=await api(base+'/logs');if(!Array.isArray(rows))throw new Error('推送记录格式错误');EVENT_LOGS.set(String(id),rows.length?rows.map(r=>`${r.created_at||''}  第 ${r.attempt??''} 次  ${({notice_success:'简短通知已发送',success:'完整贺卡已发送',simulated:'演练完成',failed:'推送失败',delivery_unknown:'发送结果待确认',blocked:'已暂停推送'})[r.status]||r.status||''}\n操作人：${r.operator||'—'}  消息 ID：${r.message_id||'—'}\n${r.error||r.msg||''}`).join('\n\n'):'暂无推送记录');}showEventDetail(cached);return;
      }
      const ev=await freshEvent(id);
      if(action==='confirm'){
        if(!canConfirm(ev))throw new Error('只有员工资料对应正常且海报可用的「待确认」事件可确认');
        if(reviewSnapshot(ev)!==reviewSnapshot(cached))throw new Error('资料或图片已变化，请刷新后重新审核');
        if(!await askConfirm('确认海报并排入发送','确认后将按计划自动推送，请核对收件人、海报和计划时间。',[recipient(ev),'计划时间：'+(ev.trigger_at||'未排期')],'确认发送排期'))return;
        await unchangedEvent(ev,canConfirm);await ensureSelected(ev);const r=requireOK(await post(base+'/confirm',{operator:'hr'}));report('event','海报已确认',[recipient(ev),'计划推送：'+(r.trigger_at||ev.trigger_at||'以服务端排期为准')]);
      }else if(action==='push'){
        if(!canPush(ev))throw new Error('只有核验通过的「已确认待推送」或「推送失败」事件可立即推送');
        if(reviewSnapshot(ev)!==reviewSnapshot(cached))throw new Error('资料或图片已变化，请刷新后重新审核');
        if(!await askConfirm('立即推送海报','此操作会请求立即发送给下列员工；演练模式下由服务端模拟发送。',[recipient(ev)],'确认立即推送'))return;
        await unchangedEvent(ev,canPush);const r=await post(base+'/push',{force:true,operator:'hr'});
        if(r.dry_run===true&&r.ok===true){report('event','演练完成，未向飞书发送消息',[recipient(ev),r.msg||'演练完成，未向飞书发送消息']);return;}
        if(r.ok!==true)throw new Error(message(r.msg||'服务端未确认发送结果，请先到飞书核实并查看推送记录'));
        const after=await freshEvent(id);
        report('event',after.status==='delivery_unknown'?'发送回执不明，请到飞书核实':after.status==='simulated'?'演练完成，未向飞书发送消息':after.status==='pushed'?'推送成功':'推送请求已处理',[recipient(ev),r.msg||labelFor(STATUS,after.status)],after.status==='delivery_unknown');
      }else if(action==='regenerate'){
        if(!canRegen(ev))throw new Error('当前状态或核验结果不允许重新生成');
        if(!await askConfirm('重新生成海报','将按当前员工资料和已保存模板生成 1 张海报，之后需要重新审核确认。',[recipient(ev)],'生成 1 张'))return;
        await unchangedEvent(ev,canRegen);requireOK(await post(base+'/regenerate',{count:1}));report('event','已提交重新生成',[recipient(ev),'生成完成后请重新查看并确认海报。']);
      }else if(action==='skip'){
        if(!canSkip(ev))throw new Error('当前状态不能跳过');if(!await askConfirm('跳过本次海报','本次事件将不再按原排期推送。',[recipient(ev)],'确认跳过'))return;
        await unchangedEvent(ev,canSkip);requireOK(await post(base+'/skip',{}));report('event','已跳过本次海报',[recipient(ev)]);
      }
    }finally{await loadEvents();if($('#event-detail-dialog').open){const current=EVENTS.find(e=>sameId(e.id,id));if(current)showEventDetail(current);}}
  });
}
$('#event-detail').addEventListener('click',e=>{const b=e.target.closest('button[data-action]');if(b&&!b.disabled)eventAction(b.dataset.action,b.dataset.id);});
$('#events').addEventListener('click',e=>{const b=e.target.closest('button[data-action]');if(b&&!b.disabled)eventAction(b.dataset.action,b.dataset.id);});
document.addEventListener('click',e=>{const b=e.target.closest('[data-zoom]');if(b){const url=fileURL(b.dataset.zoom);if(url){$('#zoom-img').src=url;$('#zoom-dialog').showModal();}}});
$('#scope').onchange=()=>{if($('#scope').value==='pending'&&$('#event-status').value==='sent'){clearDeliveryFilter();$('#event-status').value='pending';}loadEvents();};
$('#confirm-all').onclick=()=>batchEvents('confirm');
$('#regenerate-selected').onclick=()=>batchEvents('regenerate');
$('#skip-selected').onclick=()=>batchEvents('skip');
$('#clear-events').onclick=()=>{EVENT_SELECTED.clear();renderEvents();};
let generationTask=null,pollingTask=false;
$('#scan').onclick=()=>busy('events','event',async()=>{
  const scope=$('#scope').value==='this'?'this':'next';if(!await askConfirm('扫描并生成海报',`扫描${scope==='this'?'本周':'下周'}，为符合条件的员工生成海报，生成后仍需审核确认。`,[],'开始扫描'))return;
  const r=requireOK(await post('/api/run-weekly',{scope,count:1}));generationTask=r.task_id||null;report('event','扫描已提交',[`新建 ${r.created??0} 个事件，本次待生成 ${r.total??r.created??0} 个事件（含已扫描事件）；每个事件生成 1 张海报。`]);await loadEvents();
});

// Employee selection is scoped to the visible filter; changes are saved explicitly.
let STAFF=[],ALL_STAFF=[],STAFF_SELECTED=new Set(),staffLoad=0,staffTimer=null,EDIT_EMPLOYEE=null;
function updateSelection(){
  const chosen=STAFF.filter(e=>STAFF_SELECTED.has(String(e.id))),active=chosen.filter(isActive);$('#selected-count').textContent=`已选 ${chosen.length} 人`;
  $('#disable-selected').disabled=BUSY.has('staff')||!active.length;$('#clear-selection').disabled=BUSY.has('staff')||!chosen.length;
  $('#edit-selected').disabled=BUSY.has('staff')||!chosen.length;
  const all=$('#staff-all');if(all){all.checked=STAFF.length>0&&chosen.length===STAFF.length;all.indeterminate=chosen.length>0&&chosen.length<STAFF.length;}
}
function renderStaff(){
  const filters={keyword:$('#kw').value,state:$('#staff-filter').value,department:$('#staff-department').value,joinFrom:$('#staff-join-from').value,joinTo:$('#staff-join-to').value,birthFrom:$('#staff-birth-from').value,birthTo:$('#staff-birth-to').value};
  const result=StaffFilters.apply(ALL_STAFF,filters);STAFF=result.rows;
  $('#staff-filter-hint').textContent=result.hint;$('#staff-filter-hint').hidden=!result.hint;
  STAFF_SELECTED=new Set([...STAFF_SELECTED].filter(id=>STAFF.some(e=>sameId(e.id,id))));
  $('#staff-count').textContent=STAFF.length+' 条记录';
  $('#staff').innerHTML='<div class="table-wrap"><table class="data-table"><thead><tr><th class="check-col"><input type="checkbox" id="staff-all" aria-label="选择当前筛选的全部员工"></th><th>姓名</th><th>部门</th><th>工号</th><th>入职日期</th><th>出生年月</th><th>在职状态</th><th>操作</th></tr></thead><tbody>'+STAFF.map((e,i)=>`<tr class="${STAFF_SELECTED.has(String(e.id))?'is-selected':''}"><td class="selection-cell"><input type="checkbox" aria-describedby="staff-selection-help" data-employee-check="${esc(e.id)}" aria-label="选择${esc(e.name)}" ${STAFF_SELECTED.has(String(e.id))?'checked':''}></td><td><button class="person-link" data-staff-action="edit" data-id="${esc(e.id)}"><span class="avatar tone-${i%4}">${esc(String(e.name||'?').slice(-1))}</span>${esc(e.name)}</button></td><td>${esc(e.department||'—')}</td><td class="muted">${esc(e.employee_no||'—')}</td><td>${esc(e.join_date||'—')}</td><td>${esc(e.birth_date_display||e.birth_date||'—')}</td><td><span class="status-dot ${isActive(e)?'active':''}"></span>${isActive(e)?'在职':'离职'}</td><td class="row-actions"><button data-staff-action="edit" data-id="${esc(e.id)}">编辑员工资料</button></td></tr>`).join('')+(STAFF.length?'':'<tr><td colspan="8" class="empty">'+(ALL_STAFF.length?'没有符合筛选条件的员工':'暂无员工，请新增或导入真实员工名单')+'</td></tr>')+'</tbody></table></div>';
  RowSelection.refresh($('#staff'));updateBusy();
}
async function loadStaff(){
  const seq=++staffLoad;$('#staff-count').textContent='正在加载…';
  try{const rows=await api('/api/employees?only_active=false');if(seq!==staffLoad)return;if(!Array.isArray(rows))throw new Error('员工列表格式错误');ALL_STAFF=rows;refreshFilterOptions($('#staff-department'),rows.map(e=>e.department));renderStaff();}
  catch(error){if(seq===staffLoad){ALL_STAFF=[];STAFF_SELECTED.clear();renderStaff();report('staff','员工加载失败',[error.message],true);}}
}
$('#kw').oninput=()=>{clearTimeout(staffTimer);staffTimer=setTimeout(renderStaff,150);};$('#staff-filter').onchange=()=>{STAFF_SELECTED.clear();renderStaff();};
for(const id of ['staff-department','staff-join-from','staff-join-to','staff-birth-from','staff-birth-to'])$('#'+id).onchange=renderStaff;
for(const id of ['staff-birth-from','staff-birth-to'])for(let month=1;month<=12;month++)$('#'+id).add(new Option(month+' 月',String(month)));
$('#staff-reset-filters').onclick=()=>{for(const id of ['kw','staff-department','staff-join-from','staff-join-to','staff-birth-from','staff-birth-to'])$('#'+id).value='';$('#staff-filter').value='active';STAFF_SELECTED.clear();renderStaff();};
$('#clear-selection').onclick=()=>{STAFF_SELECTED.clear();renderStaff();};
function editEmployee(emp=null){
  EDIT_EMPLOYEE=emp?clone(emp):null;$('#employee-form').reset();$('#employee-title').textContent=emp?'编辑员工资料':'新增员工';$('#employee-error').textContent='';
  for(const key of ['name','department','employee_no','join_date','birth_date'])$('#employee-form').elements.namedItem(key).value=emp?.[key]??'';
  $('#employee-form').elements.namedItem('active').value=String(emp?.active??1);
  $('#employee-dialog').showModal();$('#employee-form').elements.namedItem('name').focus();
}
$('#add-employee').onclick=()=>editEmployee();
$('#employee-form').onsubmit=e=>{e.preventDefault();if(!e.target.reportValidity())return;busy('staff','staff',async()=>{
  const payload={};for(const key of ['name','department','employee_no','join_date','birth_date'])payload[key]=$('#employee-form').elements.namedItem(key).value.trim();
  if(!payload.name||!payload.department){$('#employee-error').textContent='姓名和部门不能为空。';return;}if(EDIT_EMPLOYEE)payload.id=EDIT_EMPLOYEE.id;payload.active=Number($('#employee-form').elements.namedItem('active').value);
  try{const r=requireOK(await post('/api/employees',payload));$('#employee-dialog').close();report('staff','员工资料已保存',[`${payload.name} ｜ ${payload.department}`,...bindingDetails(r)],Boolean(r.binding?.errors?.length));await loadStaff();}catch(error){$('#employee-error').textContent=error.message;throw error;}
});};
function bindingDetails(result){const binding=result.binding;return binding?[...(binding.msg?[binding.msg]:[]),...(binding.errors||[]).map(e=>`${e.name||'员工'}：${e.msg}`)]:[];}
async function disableEmployees(list){
  const active=list.filter(isActive);if(!active.length)throw new Error('请选择在职员工');
  if(!await askConfirm('离职 / 停用 '+active.length+' 位员工','停用员工并取消尚未推送的任务，保留历史记录。',active.map(e=>`${e.name} ｜ ${e.department||'未填部门'} ｜ ${e.employee_no||'未填工号'}`),'确认停用'))return;
  const r=requireOK(await post('/api/employees/batch-disable',{ids:active.map(e=>e.id)}));
  let current=[],refreshError='';try{current=await api('/api/employees?keyword=&only_active=false');if(!Array.isArray(current))throw new Error('员工列表格式错误');}catch(error){current=[];refreshError=error.message;}
  const details=active.map(e=>{const after=current.find(x=>sameId(x.id,e.id));return `${e.name} ｜ ${e.department||'未填部门'}：${after?isActive(after)?'未停用，请检查并重试':'已停用':'状态尚未确认，请刷新核对'}`;});
  if(refreshError)details.push('状态刷新失败：'+refreshError);if(Array.isArray(r.errors))for(const error of r.errors)details.push(message(error));
  report('staff',`离职处理：停用 ${r.disabled??'未返回数量'} 人，取消 ${r.cancelled??'未返回数量'} 个待推送事件`,details,Boolean(refreshError)||active.some(e=>!current.some(x=>sameId(e.id,x.id)&&!isActive(x)))||Boolean(r.errors?.length));
  STAFF_SELECTED.clear();await loadStaff();
}
$('#disable-selected').onclick=()=>busy('staff','staff',()=>disableEmployees(STAFF.filter(e=>STAFF_SELECTED.has(String(e.id)))));
$('#staff').addEventListener('click',e=>{const b=e.target.closest('[data-staff-action="edit"]');if(!b||b.disabled)return;const emp=STAFF.find(r=>sameId(r.id,b.dataset.id));if(emp)editEmployee(emp);});
$('#download-template').onclick=()=>busy('staff','staff',async()=>{
  const response=await fetch('/api/employees/template.xlsx',{headers:{'X-Admin-Token':$('#token').value}});
  if(!response.ok){let detail;try{detail=await response.json();}catch{}throw new Error(message(detail?.detail||detail?.msg||'下载模板失败，请重试'));}
  const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;a.download='员工名单模板.xlsx';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Excel 导入模板已下载');
});
$('#import-employees').onclick=()=>busy('staff','staff',async()=>{
  const file=$('#csv').files[0];if(!file)throw new Error('请先选择 CSV 或 XLSX 名单');if(!/\.(csv|xlsx)$/i.test(file.name))throw new Error('仅支持 CSV 或 XLSX 文件');
  const fd=new FormData();fd.append('file',file);const r=await api('/api/employees/import',{method:'POST',body:fd});const errors=Array.isArray(r.errors)?r.errors:[];
  const failed=r.ok===false||errors.length>0,saved=Number(r.added||0)+Number(r.updated||0);
  const outcome=failed?(saved>0?'部分完成':'失败'):'完成';
  const resultDetail=errors.length?`发现 ${errors.length} 条错误；${saved>0?'成功行已提交，请只修正失败行后重新导入。':'没有新增或更新员工，请修正错误后重新导入。'}`:failed?message(r.msg||'接口报告失败，但未提供行级错误，请核对名单。'):'员工资料已导入。';
  report('staff',`导入${outcome}：共 ${r.total??'未知'} 行，新增 ${r.added??0}，更新 ${r.updated??0}，跳过 ${r.skipped??0}`,[`文件：${file.name}`,resultDetail,...errors.map(e=>`第 ${e.row??'未知'} 行：${message(e.msg||e.error)}`),...bindingDetails(r)],failed||Boolean(r.binding?.errors?.length));
  $('#csv').value='';await loadStaff();
});
$('#sync-employees').onclick=()=>busy('staff','staff',async()=>{
  if(!await askConfirm('同步飞书员工名单','同步会新增、更新或停用本地员工资料。系统会按唯一姓名自动对应飞书 open_id，请核对同步结果。',[],'开始同步'))return;
  const r=await post('/api/sync',{fill_birthday:true});const errors=Array.isArray(r.errors)?r.errors:[];
  report('staff',r.ok===false||errors.length?'飞书同步部分完成，成功项已提交':'飞书同步完成',[`新增 ${r.added??0}，更新 ${r.updated??0}，停用 ${r.disabled??0}`,...errors.map(message),...(r.warnings||[]),...(r.ok===false&&!errors.length?[message(r.msg||'接口报告部分失败，请核对名单。')]:[]),...bindingDetails(r)],r.ok===false||errors.length>0||Boolean(r.warnings?.length)||Boolean(r.binding?.errors?.length));await loadStaff();
});
let savedFeishuAppId='';
async function loadFeishuConfig(){
  try{
    const cfg=await api('/api/feishu/config');savedFeishuAppId=cfg.app_id||'';
    $('#feishu-app-id').value=savedFeishuAppId;$('#feishu-app-secret').value='';
    $('#feishu-app-secret').placeholder=cfg.secret_configured?'已保存，留空保留原密钥':'填写应用密钥';
    $('#feishu-connection-state').textContent=cfg.configured?'应用凭据已配置，点击检查通讯录连接。':'尚未连接飞书，请先创建企业自建应用并填写凭据。';
  }catch(error){$('#feishu-connection-state').textContent=error.message;}
}
$('#open-feishu-settings').onclick=()=>document.querySelector('nav [data-tab="sys"]').click();
$('#feishu-config-form').onsubmit=e=>{
  e.preventDefault();if(!e.target.reportValidity())return;
  busy('feishu','sys',async()=>{
    $('#feishu-config-fields').disabled=true;
    try{
      const result=await post('/api/feishu/config',{app_id:$('#feishu-app-id').value.trim(),app_secret:$('#feishu-app-secret').value});
      requireOK(result);await loadFeishuConfig();report('sys','飞书配置已保存',[result.msg]);
    }finally{$('#feishu-config-fields').disabled=false;}
  });
};
$('#test-feishu-connection').onclick=()=>busy('feishu','sys',async()=>{
  if($('#feishu-app-id').value.trim()!==savedFeishuAppId||$('#feishu-app-secret').value)throw new Error('请先保存当前填写的应用凭据，再检查连接');
  $('#feishu-config-fields').disabled=true;$('#feishu-connection-state').textContent='正在读取飞书通讯录授权范围…';
  try{
    const result=await post('/api/feishu/check',{});$('#feishu-connection-state').textContent=result.msg;
    report('sys',result.ok?'飞书通讯录连接检查通过':'飞书连接未就绪',[result.msg],!result.ok);
  }catch(error){$('#feishu-connection-state').textContent=error.message;throw error;}
  finally{$('#feishu-config-fields').disabled=false;}
});
async function loadHealth(){
  $('#run-mode').textContent='正在检查…';try{const h=await api('/api/health');$('#run-mode').textContent=h.dry_run===true?'当前为演练模式：完成后记录为「演练完成」，未向飞书发送消息。':h.dry_run===false?'当前为正式模式：按计划先发送简短通知，再自动发送完整贺卡，员工无需确认。立即推送需管理员确认。':'运行模式未知，请核对服务端配置。';$('#data-quality').textContent=h.missing_fields?`在职员工资料缺失：生日 ${h.missing_fields.birthday} 人，入职日期 ${h.missing_fields.join_date} 人，飞书 open_id ${h.missing_fields.feishu_id} 人。缺少对应日期的员工不会生成该类贺卡；缺少飞书 open_id 无法推送。`:'';$('#health').textContent=JSON.stringify(h,null,2);}catch(error){$('#run-mode').textContent='检查失败';$('#health').textContent=error.message;}
}
$('#check-health').onclick=loadHealth;
setInterval(async()=>{
  if(TAB==='events'&&!document.hidden&&!BUSY.has('events')&&!eventLoading)await loadEvents(true);
  if(generationTask&&!pollingTask){pollingTask=true;try{const t=await api('/api/gen-task/'+encodeURIComponent(generationTask));if(['done','failed','error'].includes(t.status)){generationTask=null;report('event',t.status==='done'?'生成任务完成':'生成任务失败',[`新建 ${t.created??0} 个事件，成功生成 ${t.generated??0} / ${t.total??t.created??0} 个事件`,t.error||'请查看每条海报的实际状态。'],t.status!=='done'||Number(t.generated)<Number(t.total??t.created));}else{$('#events-loading').textContent=`生成中：${t.generated??0} / ${t.total??t.created??0} 个事件`;}}catch(error){generationTask=null;report('event','生成进度读取失败',[error.message,'生成可能仍在服务端进行，请刷新事件列表确认。'],true);}finally{pollingTask=false;}}
},15000);
window.addEventListener("DOMContentLoaded", ()=>loadEvents());

RowSelection.attach({container:$('#events'),selector:'[data-event-check]',allId:'event-all',idOf:input=>input.dataset.eventCheck,
  ids:()=>EVENTS.map(row=>String(row.id)),selected:()=>EVENT_SELECTED,locked:()=>BUSY.has('events'),
  change:values=>{EVENT_SELECTED=values;updateEventSelection();}});
RowSelection.attach({container:$('#staff'),selector:'[data-employee-check]',allId:'staff-all',idOf:input=>input.dataset.employeeCheck,
  ids:()=>STAFF.map(row=>String(row.id)),selected:()=>STAFF_SELECTED,locked:()=>BUSY.has('staff'),
  change:values=>{STAFF_SELECTED=values;updateSelection();}});
