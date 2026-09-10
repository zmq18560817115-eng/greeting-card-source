
'use strict';
// All API/user text enters HTML through esc; event handlers never contain interpolated data.
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = value => JSON.parse(JSON.stringify(value));
const sameId = (a,b) => a != null && b != null && String(a) === String(b);
const isActive = e => e?.active === true || e?.active === 1 || e?.active === '1';
const openId = e => e?.feishu_open_id || e?.open_id || '';
const STATUS = Object.freeze({generating:'生成中',ready:'待确认',confirmed:'已确认待推送',pushing:'推送中',pushed:'已推送',failed:'推送失败',gen_failed:'生成失败',skipped:'已跳过',blocked:'核验阻止',simulated:'演练完成',needs_regeneration:'资料变更，需重新生成',delivery_unknown:'发送回执不明',expired:'日期已过期，停止发送'});
const IDENTITY = Object.freeze({pending:'待核验',verified:'核验通过',failed:'核验失败'});
const own = (object,key) => Object.prototype.hasOwnProperty.call(object,key);
const labelFor = (map,key) => own(map,key) ? map[key] : String(key || '未知');
function badge(status, labels=STATUS){return `<span class="badge b-${own(labels,status)?esc(status):'unknown'}">${esc(labelFor(labels,status))}</span>`;}
function message(value){return typeof value === 'string' ? value : JSON.stringify(value ?? '未知错误');}
function toast(msg){$('#toast').textContent=msg;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,4500);}
function report(area,title,rows=[],hasError=false){
  const details=document.createElement('details');details.className='report'+(hasError?' has-error':'');details.open=true;
  const summary=document.createElement('summary');summary.textContent=title+' ';
  const time=document.createElement('time');time.textContent=new Date().toLocaleTimeString('zh-CN');summary.append(time);details.append(summary);
  const list=document.createElement('ul');for(const row of rows){const li=document.createElement('li');li.textContent=message(row);list.append(li);}details.append(list);
  const box=$('#'+area+'-reports');for(const item of box.children)item.open=false;box.prepend(details);
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
  for(const id of ['add-employee','import-employees','download-template','sync-employees','kw','staff-filter'])$('#'+id).disabled=BUSY.has('staff');
  $('#employee-fields').disabled=BUSY.has('staff');
  for(const b of document.querySelectorAll('#staff button,#staff input,#candidates button'))b.disabled=BUSY.has('staff') || b.dataset.locked==='true';
  for(const b of document.querySelectorAll('#events button[data-action]'))b.disabled=BUSY.has('events') || b.dataset.locked==='true';
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
document.addEventListener('click',e=>{const b=e.target.closest('[data-close]');if(b&&(!['employee-dialog','candidate-dialog'].includes(b.dataset.close)||!BUSY.has('staff')))$('#'+b.dataset.close).close();});
for(const id of ['employee-dialog','candidate-dialog'])$('#'+id).addEventListener('cancel',e=>{if(BUSY.has('staff'))e.preventDefault();});
try{$('#token').value=localStorage.getItem('gc_token')||'';}catch{/* The input also works when browser storage is unavailable. */}
$('#token').oninput=()=>{try{localStorage.setItem('gc_token',$('#token').value);}catch{}};
document.querySelectorAll('nav button').forEach(b=>b.onclick=async()=>{
  TAB=b.dataset.tab;for(const t of ['events','staff','tpl','sys'])$('#tab-'+t).hidden=t!==TAB;
  document.querySelectorAll('nav button').forEach(n=>{if(n===b)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});await refresh();
});
async function refresh(){if(TAB==='events')await loadEvents();if(TAB==='staff')await loadStaff();if(TAB==='tpl')await loadTpl();if(TAB==='sys')await loadHealth();}
$('#refresh').onclick=refresh;

// Event actions re-read state immediately before posting. Terminal deliveries remain read-only.
let EVENTS=[],ALL_EVENTS=[],EVENT_SELECTED=new Set(),eventLoad=0,eventLoading=false;const EVENT_LOGS=new Map();
function identityProblem(emp){
  if(!emp || !isActive(emp))return '员工已停用，或无法取得最新在职资料';
  if(!String(emp.name||'').trim() || !String(emp.department||'').trim() || !String(openId(emp)).trim())return '请补全姓名、部门和飞书 ID，并执行三重核验';
  if(emp.identity_status!=='verified')return emp.identity_error || '姓名、部门和飞书 ID 尚未通过三重核验';return '';
}
function terminal(e){return Boolean(e.pushed_at) || ['pushed','pushing','delivery_unknown','expired'].includes(e.status);}
function selectedCard(e){return (e.cards||[]).find(c=>sameId(c.id,e.selected_card_id) && c.status==='ok' && fileURL(c.url));}
function canConfirm(e){return e.status==='ready'&&!terminal(e)&&!identityProblem(e.employee)&&Boolean(selectedCard(e)||(e.cards||[]).find(c=>c.status==='ok'&&fileURL(c.url)));}
function canPush(e){return ['confirmed','failed'].includes(e.status)&&!terminal(e)&&!identityProblem(e.employee)&&Boolean(selectedCard(e));}
function canRegen(e){return !terminal(e)&&['ready','confirmed','failed','gen_failed','blocked','needs_regeneration','simulated'].includes(e.status)&&isActive(e.employee);}
function canSkip(e){return !terminal(e)&&['ready','confirmed','failed','gen_failed','blocked','needs_regeneration'].includes(e.status);}
function canSelect(e){return ['ready','confirmed','failed','blocked','simulated'].includes(e.status)&&!terminal(e)&&isActive(e.employee);}
function eventButton(action,id,text,enabled,primary=false){return `<button data-action="${action}" data-id="${esc(id)}" data-locked="${!enabled}" ${enabled?'':'disabled'} class="${primary?'primary':''}">${text}</button>`;}
function renderEventDetails(e){
  const emp=e.employee||{},reason=identityProblem(emp),cards=Array.isArray(e.cards)?e.cards:[];
  const images=cards.map((c,i)=>{
    const url=fileURL(c.url),chosen=sameId(c.id,e.selected_card_id);
    if(c.status==='ok'&&url)return `<div class="thumb ${chosen?'sel':''}"><button class="image-button" data-zoom="${esc(url)}" aria-label="放大${esc(emp.name)}的海报"><img src="${esc(url)}" alt="${esc(emp.name)}的海报" loading="lazy"></button><span class="meta">${cards.length===1?'海报':'方案 '+(i+1)} · ${chosen?'已选中':'未选中'}</span>${canSelect(e)?`<button class="select-card" data-action="select" data-id="${esc(e.id)}" data-card="${esc(c.id)}" data-locked="${chosen}" ${chosen?'disabled':''}>${chosen?'已选为推送图片':'选择此海报'}</button>`:''}</div>`;
    return `<div class="thumb"><div class="empty">${c.status==='generating'?'海报生成中…':'图片不可用'}</div><p class="error">${esc(c.error||(c.status==='ok'?'图片地址无效':''))}</p></div>`;
  }).join('');
  let instruction='';
  if(e.status==='delivery_unknown')instruction='发送请求已发出，但回执不明。请到飞书核实员工是否已收到消息；禁止重推、选图和重新生成。';
  else if(e.status==='pushed'||e.pushed_at)instruction='已完成推送，不能再次确认、选图、重新生成或重发。';
  else if(e.status==='expired')instruction='事件日期已过期，已停止发送，不能确认或推送。';
  else if(e.status==='ready')instruction=reason||(selectedCard(e)?'请放大核对文案和收件人，再确认发送排期。':'请先选择生成成功的海报，再确认发送排期。');
  else if(e.status==='needs_regeneration')instruction='员工资料已变更，请重新生成海报后再次确认。';
  else if(e.status==='blocked')instruction=reason||'核验已更新，请重新生成后再审核。';
  else if(e.status==='simulated')instruction='演练完成，未向飞书发送消息。可重新生成后再次审核。';
  else if(e.status==='gen_failed')instruction='生成失败，可查看错误原因后重新生成。';
  else if(e.status==='generating')instruction='正在生成，页面将自动更新。';
  else if(['confirmed','failed'].includes(e.status))instruction=reason||(selectedCard(e)?'已确认收件人与海报，可立即推送。':'选定图片不可用，请重新生成并审核。');
  return `<article class="ev"><div class="ev-head"><span class="name">${esc(emp.name||'员工资料不可用')}</span><span class="badge">${esc(e.event_type==='birthday'?'生日':e.event_type==='anniversary'?'入职周年':e.event_type)}</span>${badge(e.status)}<span class="spacer"></span><span class="meta">${esc(e.event_date)}${e.years!=null?' · '+esc(e.years)+(e.event_type==='birthday'?' 岁':' 周年'):''}</span></div>
    <div class="identity"><span>部门：${esc(emp.department||'未填写')}</span><span>飞书 ID：${esc(openId(emp)||'未填写')}</span>${badge(emp.identity_status||'pending',IDENTITY)}</div>
    <div class="event-body"><div class="cards">${images||'<div class="empty">尚无可用海报</div>'}</div><div class="event-info"><p class="meta">计划推送：${esc(e.trigger_at||'未排期')}${e.pushed_at?' · 已推送：'+esc(e.pushed_at):''}</p><p class="hint" style="margin-top:10px">${esc(instruction||'可查看海报和推送记录。')}</p>${e.last_error?`<p class="error">最近错误：${esc(e.last_error)}</p>`:''}<div class="acts">${eventButton('confirm',e.id,'确认发送排期',canConfirm(e),true)}${eventButton('push',e.id,e.status==='failed'?'重试推送':'立即推送',canPush(e))}${eventButton('regenerate',e.id,'重新生成 1 张',canRegen(e))}${eventButton('skip',e.id,'跳过本次',canSkip(e))}${eventButton('logs',e.id,'推送记录',true)}</div><p class="meta" style="margin-top:8px">已尝试推送 ${esc(e.push_attempts||0)} 次</p></div></div><div data-log-id="${esc(e.id)}">${EVENT_LOGS.has(String(e.id))?'<pre>'+esc(EVENT_LOGS.get(String(e.id)))+'</pre>':''}</div></article>`;
}
async function loadEvents(quiet=false){
  const seq=++eventLoad;eventLoading=true;$('#events-loading').textContent='正在刷新…';
  try{
    const scope=$('#scope').value;
    const results=await Promise.allSettled([api('/api/events?scope='+encodeURIComponent(scope==='pending'?'all':scope)),api('/api/employees?keyword=&only_active=false')]);
    if(seq!==eventLoad)return;if(results[0].status==='rejected')throw results[0].reason;if(!Array.isArray(results[0].value))throw new Error('事件列表格式错误');
    const employees=results[1].status==='fulfilled'&&Array.isArray(results[1].value)?results[1].value:null;
    if(!employees&&!quiet)report('event','员工核验信息加载失败',['当前海报仅供查看，请刷新后再执行确认或推送。'],true);
    let events=results[0].value.map(e=>({...e,employee:employees?.find(emp=>sameId(emp.id,e.employee_id??e.employee?.id))||{...e.employee,active:0,identity_status:'pending'}}));
    if(scope==='pending')events=events.filter(e=>['ready','generating','blocked','gen_failed','needs_regeneration','failed','delivery_unknown'].includes(e.status));
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
  const kw=$('#event-search').value.trim().toLowerCase(),state=$('#event-status').value,type=$('#event-type').value,dept=$('#event-department').value;
  EVENTS=ALL_EVENTS.filter(e=>(!state||e.status===state)&&(!type||e.event_type===type)&&(!dept||e.employee?.department===dept)&&(!kw||[e.employee?.name,e.employee?.department,openId(e.employee)].some(v=>String(v||'').toLowerCase().includes(kw))));
  const direction=$('#event-sort').value==='desc'?-1:1;
  EVENTS.sort((a,b)=>direction*String(a.event_date).localeCompare(String(b.event_date)));
  EVENT_SELECTED=new Set([...EVENT_SELECTED].filter(id=>EVENTS.some(e=>sameId(e.id,id))));
  $('#evsum').textContent=EVENTS.length+' 条记录';
  $('#events').innerHTML='<div class="table-wrap"><table class="data-table"><thead><tr><th class="check-col"><input type="checkbox" id="event-all" aria-label="选择当前筛选的全部海报"></th><th>员工</th><th>部门</th><th>贺卡类型</th><th>事件日期</th><th>海报</th><th>审核状态</th><th>身份核验</th><th>计划推送</th><th>操作</th></tr></thead><tbody>'+EVENTS.map((e,i)=>{
    const card=selectedCard(e)||(e.cards||[]).find(c=>c.status==='ok'&&fileURL(c.url)),url=card&&fileURL(card.url),emp=e.employee||{};
    return `<tr class="${EVENT_SELECTED.has(String(e.id))?'is-selected':''}"><td><input type="checkbox" data-event-check="${esc(e.id)}" aria-label="选择${esc(emp.name)}的海报" ${EVENT_SELECTED.has(String(e.id))?'checked':''}></td><td><button class="person-link" data-action="detail" data-id="${esc(e.id)}"><span class="avatar tone-${i%4}">${esc(String(emp.name||'?').slice(-1))}</span>${esc(emp.name||'未知员工')}</button></td><td>${esc(emp.department||'—')}</td><td><span class="type-tag ${e.event_type==='birthday'?'birthday':'anniversary'}">${e.event_type==='birthday'?'生日':'入职周年'}</span></td><td>${esc(e.event_date)}</td><td>${url?`<button class="table-poster" data-zoom="${esc(url)}" aria-label="放大${esc(emp.name)}的海报"><img src="${esc(url)}" alt="${esc(emp.name)}海报" loading="lazy"><span>查看海报 ↗</span></button>`:'<span class="muted">待生成</span>'}</td><td title="${esc(e.last_error||'')}">${badge(e.status)}</td><td>${badge(emp.identity_status||'pending',IDENTITY)}</td><td class="muted">${esc(e.trigger_at||'—')}</td><td class="row-actions">${eventButton('detail',e.id,'查看',true)}${canConfirm(e)?eventButton('confirm',e.id,'确认',true):canPush(e)?eventButton('push',e.id,'推送',true):''}</td></tr>`;
  }).join('')+(EVENTS.length?'':'<tr><td colspan="10" class="empty">暂无海报，请先维护员工名单并扫描生成</td></tr>')+'</tbody></table></div>';
  updateEventSelection();
}
function updateEventSelection(){
  const chosen=EVENTS.filter(e=>EVENT_SELECTED.has(String(e.id)));
  $('#event-selected-count').textContent='已选 '+chosen.length+' 条';
  for(const [id,predicate] of [['confirm-all',canConfirm],['regenerate-selected',canRegen],['skip-selected',canSkip]])$('#'+id).disabled=BUSY.has('events')||!chosen.some(predicate);
  $('#clear-events').disabled=BUSY.has('events')||!chosen.length;
  const all=$('#event-all');if(all){all.checked=EVENTS.length>0&&chosen.length===EVENTS.length;all.indeterminate=chosen.length>0&&chosen.length<EVENTS.length;}
}
for(const id of ['event-type','event-status','event-department','event-sort'])$('#'+id).onchange=renderEvents;
$('#event-search').oninput=renderEvents;
$('#events').addEventListener('change',e=>{if(e.target.id==='event-all')EVENT_SELECTED=e.target.checked?new Set(EVENTS.map(r=>String(r.id))):new Set();else if(e.target.dataset.eventCheck){const id=e.target.dataset.eventCheck;e.target.checked?EVENT_SELECTED.add(id):EVENT_SELECTED.delete(id);}renderEvents();});
function showEventDetail(e){$('#event-detail').innerHTML=renderEventDetails(e);if(!$('#event-detail-dialog').open)$('#event-detail-dialog').showModal();}
async function ensureSelected(e){
  if(selectedCard(e))return e;
  const cards=(e.cards||[]).filter(c=>c.status==='ok'&&fileURL(c.url));
  if(cards.length!==1)throw new Error('请先在详情中选定海报');
  requireOK(await post('/api/events/'+encodeURIComponent(e.id)+'/select',{card_id:cards[0].id}));
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
function recipient(e){return `${e.employee?.name||'未知员工'} ｜ ${e.employee?.department||'未填部门'} ｜ ${openId(e.employee)||'未填飞书 ID'} ｜ ${e.event_date||''}`;}
function reviewSnapshot(e){return JSON.stringify([e.id,e.status,e.employee?.id,e.employee?.name,e.employee?.department,openId(e.employee),e.employee?.join_date,e.employee?.birth_date,e.event_date,e.trigger_at,e.selected_card_id,selectedCard(e)?.url]);}
async function unchangedEvent(e,allowed){const latest=await freshEvent(e.id);if(!allowed(latest)||reviewSnapshot(e)!==reviewSnapshot(latest))throw new Error('员工、图片或事件状态已变化，请刷新后重新审核');return latest;}
async function eventAction(action,id,cardId){
  const cached=EVENTS.find(e=>sameId(e.id,id));if(!cached)return;
  return busy('events','event',async()=>{
    try{
      const base='/api/events/'+encodeURIComponent(id);
      if(action==='detail'){showEventDetail(cached);return;}
      if(action==='logs'){
        if(EVENT_LOGS.has(String(id)))EVENT_LOGS.delete(String(id));else{const rows=await api(base+'/logs');if(!Array.isArray(rows))throw new Error('推送记录格式错误');EVENT_LOGS.set(String(id),rows.length?rows.map(r=>`${r.created_at||''}  第 ${r.attempt??''} 次  ${r.status||''}\n操作人：${r.operator||'—'}  消息 ID：${r.message_id||'—'}\n${r.error||r.msg||''}`).join('\n\n'):'暂无推送记录');}showEventDetail(cached);return;
      }
      const ev=await freshEvent(id);
      if(action==='select'){
        const card=(ev.cards||[]).find(c=>sameId(c.id,cardId)&&c.status==='ok'&&fileURL(c.url));if(!canSelect(ev)||!card)throw new Error('当前状态不能选择图片，请刷新检查');requireOK(await post(base+'/select',{card_id:card.id}));toast('已选择海报');
      }else if(action==='confirm'){
        if(!canConfirm(ev))throw new Error('只有已核验、已选有效图片的「待确认」事件可确认');
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
$('#event-detail').addEventListener('click',e=>{const b=e.target.closest('button[data-action]');if(b&&!b.disabled)eventAction(b.dataset.action,b.dataset.id,b.dataset.card);});
$('#events').addEventListener('click',e=>{const b=e.target.closest('button[data-action]');if(b&&!b.disabled)eventAction(b.dataset.action,b.dataset.id,b.dataset.card);});
document.addEventListener('click',e=>{const b=e.target.closest('[data-zoom]');if(b){const url=fileURL(b.dataset.zoom);if(url){$('#zoom-img').src=url;$('#zoom-dialog').showModal();}}});
$('#scope').onchange=()=>loadEvents();
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
let STAFF=[],ALL_STAFF=[],STAFF_SELECTED=new Set(),staffLoad=0,staffTimer=null,EDIT_EMPLOYEE=null,CANDIDATE_EMPLOYEE=null,CANDIDATES=[];
function updateSelection(){
  const chosen=STAFF.filter(e=>STAFF_SELECTED.has(String(e.id))),active=chosen.filter(isActive);$('#selected-count').textContent=`已选 ${chosen.length} 人`;
  $('#verify-selected').disabled=BUSY.has('staff')||!active.length;$('#disable-selected').disabled=BUSY.has('staff')||!active.length;$('#clear-selection').disabled=BUSY.has('staff')||!chosen.length;
  const all=$('#staff-all');if(all){all.checked=STAFF.length>0&&chosen.length===STAFF.length;all.indeterminate=chosen.length>0&&chosen.length<STAFF.length;}
}
function renderStaff(){
  const keyword=$('#kw').value.trim().toLowerCase(),state=$('#staff-filter').value,dept=$('#staff-department').value,identity=$('#staff-identity').value;
  STAFF=ALL_STAFF.filter(e=>(state==='all'||(state==='active'?isActive(e):!isActive(e)))&&(!dept||e.department===dept)&&(!identity||e.identity_status===identity)&&(!keyword||[e.name,e.employee_no,e.department,e.email,openId(e)].some(v=>String(v||'').toLowerCase().includes(keyword))));
  STAFF_SELECTED=new Set([...STAFF_SELECTED].filter(id=>STAFF.some(e=>sameId(e.id,id))));
  $('#staff-count').textContent=STAFF.length+' 条记录';
  $('#staff').innerHTML='<div class="table-wrap"><table class="data-table"><thead><tr><th class="check-col"><input type="checkbox" id="staff-all" aria-label="选择当前筛选的全部员工"></th><th>姓名</th><th>工号</th><th>部门</th><th>入职日期</th><th>生日</th><th>飞书 ID</th><th>身份核验</th><th>在职状态</th><th>操作</th></tr></thead><tbody>'+STAFF.map((e,i)=>`<tr class="${STAFF_SELECTED.has(String(e.id))?'is-selected':''}"><td><input type="checkbox" data-employee-check="${esc(e.id)}" aria-label="选择${esc(e.name)}" ${STAFF_SELECTED.has(String(e.id))?'checked':''}></td><td><button class="person-link" data-staff-action="edit" data-id="${esc(e.id)}"><span class="avatar tone-${i%4}">${esc(String(e.name||'?').slice(-1))}</span>${esc(e.name)}</button></td><td class="muted">${esc(e.employee_no||'—')}</td><td>${esc(e.department||'—')}</td><td>${esc(e.join_date||'—')}</td><td>${esc(e.birth_date||'—')}</td><td class="id-cell" title="${esc(openId(e))}">${esc(openId(e)||'未绑定')}</td><td title="${esc(e.identity_error||'')}">${badge(e.identity_status||'pending',IDENTITY)}</td><td><span class="status-dot ${isActive(e)?'active':''}"></span>${isActive(e)?'在职':'已停用'}</td><td class="row-actions"><button data-staff-action="edit" data-id="${esc(e.id)}">编辑</button>${isActive(e)?`<button data-staff-action="verify" data-id="${esc(e.id)}">核验</button><button data-staff-action="match" data-id="${esc(e.id)}">绑定</button>`:`<button data-staff-action="restore" data-id="${esc(e.id)}">恢复</button>`}</td></tr>`).join('')+(STAFF.length?'':'<tr><td colspan="10" class="empty">暂无员工，请新增或导入真实员工名单</td></tr>')+'</tbody></table></div>';
  updateBusy();
}
async function loadStaff(){
  const seq=++staffLoad;$('#staff-count').textContent='正在加载…';
  try{const rows=await api('/api/employees?only_active=false');if(seq!==staffLoad)return;if(!Array.isArray(rows))throw new Error('员工列表格式错误');ALL_STAFF=rows;refreshFilterOptions($('#staff-department'),rows.map(e=>e.department));renderStaff();}
  catch(error){if(seq===staffLoad){ALL_STAFF=[];STAFF_SELECTED.clear();renderStaff();report('staff','员工加载失败',[error.message],true);}}
}
$('#kw').oninput=()=>{clearTimeout(staffTimer);staffTimer=setTimeout(renderStaff,150);};$('#staff-filter').onchange=()=>{STAFF_SELECTED.clear();renderStaff();};
for(const id of ['staff-department','staff-identity'])$('#'+id).onchange=renderStaff;
$('#staff').addEventListener('change',e=>{if(e.target.id==='staff-all'){STAFF_SELECTED=e.target.checked?new Set(STAFF.map(r=>String(r.id))):new Set();renderStaff();}else if(e.target.dataset.employeeCheck){const id=e.target.dataset.employeeCheck;e.target.checked?STAFF_SELECTED.add(id):STAFF_SELECTED.delete(id);renderStaff();}});
$('#clear-selection').onclick=()=>{STAFF_SELECTED.clear();renderStaff();};
function editEmployee(emp=null){
  EDIT_EMPLOYEE=emp?clone(emp):null;$('#employee-form').reset();$('#employee-title').textContent=emp?'编辑员工资料':'新增员工';$('#employee-error').textContent='';
  for(const key of ['name','department','employee_no','email','join_date','birth_date','feishu_open_id','note'])$('#employee-form').elements.namedItem(key).value=emp?.[key]??'';
  $('#employee-dialog').showModal();$('#employee-form').elements.namedItem('name').focus();
}
$('#add-employee').onclick=()=>editEmployee();
$('#employee-form').onsubmit=e=>{e.preventDefault();if(!e.target.reportValidity())return;busy('staff','staff',async()=>{
  const payload={};for(const key of ['name','department','employee_no','email','join_date','birth_date','feishu_open_id','note'])payload[key]=$('#employee-form').elements.namedItem(key).value.trim();
  if(!payload.name||!payload.department){$('#employee-error').textContent='姓名和部门不能为空。';return;}if(EDIT_EMPLOYEE)payload.id=EDIT_EMPLOYEE.id;else payload.active=1;
  try{requireOK(await post('/api/employees',payload));$('#employee-dialog').close();report('staff','员工资料已保存',[`${payload.name} ｜ ${payload.department}；请以刷新后的核验状态为准。`]);await loadStaff();invalidatePreviewEmployees();}catch(error){$('#employee-error').textContent=error.message;throw error;}
});};
async function verifyEmployees(list){
  const active=list.filter(isActive);if(!active.length)throw new Error('请选择在职员工');const r={results:[]};
  for(let start=0;start<active.length;start+=200){
    const batch=active.slice(start,start+200);
    try{const result=requireOK(await post('/api/employees/verify',{ids:batch.map(e=>e.id)}));if(!Array.isArray(result.results))throw new Error('核验接口未返回 results，无法确认核验结果');r.results.push(...result.results);}
    catch(error){r.results.push(...batch.map(e=>({id:e.id,ok:false,msg:error.message})));}
  }
  let success=0;const details=active.map(e=>{const result=r.results.find(x=>sameId(x.id,e.id)),ok=result?.ok===true;if(ok)success++;return `${e.name} ｜ ${e.department||'未填部门'} ｜ ${openId(e)||'未填飞书 ID'}：${ok?'通过':'失败'}，${result?.msg || (result?'未提供说明':'接口未返回该员工的结果')}`;});
  report('staff',`三重核验：通过 ${success}，失败 ${active.length-success}`,details,success!==active.length);await loadStaff();invalidatePreviewEmployees();
}
async function disableEmployees(list){
  const active=list.filter(isActive);if(!active.length)throw new Error('请选择在职员工');
  if(!await askConfirm('离职 / 停用 '+active.length+' 位员工','停用员工并取消尚未推送的任务，保留历史记录。',active.map(e=>`${e.name} ｜ ${e.department||'未填部门'} ｜ ${openId(e)||'未填飞书 ID'}`),'确认停用'))return;
  const r=requireOK(await post('/api/employees/batch-disable',{ids:active.map(e=>e.id)}));
  let current=[],refreshError='';try{current=await api('/api/employees?keyword=&only_active=false');if(!Array.isArray(current))throw new Error('员工列表格式错误');}catch(error){current=[];refreshError=error.message;}
  const details=active.map(e=>{const after=current.find(x=>sameId(x.id,e.id));return `${e.name} ｜ ${e.department||'未填部门'}：${after?isActive(after)?'未停用，请检查并重试':'已停用':'状态尚未确认，请刷新核对'}`;});
  if(refreshError)details.push('状态刷新失败：'+refreshError);if(Array.isArray(r.errors))for(const error of r.errors)details.push(message(error));
  report('staff',`离职处理：停用 ${r.disabled??'未返回数量'} 人，取消 ${r.cancelled??'未返回数量'} 个待推送事件`,details,Boolean(refreshError)||active.some(e=>!current.some(x=>sameId(e.id,x.id)&&!isActive(x)))||Boolean(r.errors?.length));
  STAFF_SELECTED.clear();await loadStaff();invalidatePreviewEmployees();
}
$('#verify-selected').onclick=()=>busy('staff','staff',()=>verifyEmployees(STAFF.filter(e=>STAFF_SELECTED.has(String(e.id)))));
$('#disable-selected').onclick=()=>busy('staff','staff',()=>disableEmployees(STAFF.filter(e=>STAFF_SELECTED.has(String(e.id)))));
$('#staff').addEventListener('click',e=>{const b=e.target.closest('[data-staff-action]');if(!b||b.disabled)return;const emp=STAFF.find(r=>sameId(r.id,b.dataset.id));if(!emp)return;const action=b.dataset.staffAction;
  if(action==='edit')return editEmployee(emp);busy('staff','staff',async()=>{if(action==='verify')await verifyEmployees([emp]);if(action==='disable')await disableEmployees([emp]);if(action==='match')await matchFeishu(emp);if(action==='restore'){
    if(!await askConfirm('恢复员工在职状态','恢复后请执行三重核验，再扫描生成海报。',[emp.name+' ｜ '+(emp.department||'未填部门')],'恢复在职'))return;
    requireOK(await post('/api/employees',{id:emp.id,active:1}));report('staff','已恢复在职',[emp.name+'：请重新核验身份。']);await loadStaff();invalidatePreviewEmployees();
  }});
});
function eligibleCandidate(candidate){return candidate?.eligible===true&&Boolean(candidate.open_id);}
function showCandidates(emp,result){
  CANDIDATE_EMPLOYEE=clone(emp);CANDIDATES=Array.isArray(result.candidates)?clone(result.candidates):[];
  $('#candidate-employee').textContent=`员工：${emp.name} ｜ ${emp.department||'未填部门'}\n当前飞书 ID：${openId(emp)||'未绑定'}`;$('#candidate-message').textContent=message(result.msg||'请选择候选人；此时尚未绑定');
  $('#candidates').innerHTML=CANDIDATES.length?CANDIDATES.map((c,i)=>`<div class="candidate" style="border:1px solid var(--line);padding:12px;border-radius:8px;margin-top:10px"><div class="bar"><b>${esc(c.name||'接口未提供姓名')}</b><span class="spacer"></span><button data-candidate="${i}" data-locked="${!eligibleCandidate(c)}" ${eligibleCandidate(c)?'':'disabled'}>${eligibleCandidate(c)?'选择并绑定核验':'不符合绑定条件'}</button></div><p class="details-line">部门：${esc(c.department||'接口未提供部门')}<br>飞书 ID：${esc(c.open_id||'未提供')}</p><p class="meta">工号：${esc(c.employee_no||'未提供')}</p>${c.error?`<p class="error">${esc(c.error)}</p>`:''}</div>`).join(''):'<div class="empty">没有候选人，请先补全员工资料。</div>';
  if(!$('#candidate-dialog').open)$('#candidate-dialog').showModal();updateBusy();
}
async function matchFeishu(emp){
  const r=await post('/api/employees/match-feishu',{employee_id:emp.id});const candidates=Array.isArray(r.candidates)?r.candidates:[],eligible=candidates.filter(eligibleCandidate).length;
  report('staff',eligible?`找到 ${eligible} 位可选候选人，尚未绑定`:'未找到符合条件的飞书候选人',[emp.name+'：'+message(r.msg||'请核对候选人资料后选择绑定')],!eligible);
  // ok means eligible candidates exist, never that a binding was performed.
  showCandidates(emp,r);
}
$('#candidates').addEventListener('click',e=>{const b=e.target.closest('[data-candidate]');if(!b||b.disabled)return;const c=CANDIDATES[Number(b.dataset.candidate)],emp=CANDIDATE_EMPLOYEE;if(!eligibleCandidate(c)||!emp)return;
  busy('staff','staff',async()=>{try{
    const r=await post('/api/employees/bind-feishu',{employee_id:emp.id,open_id:c.open_id});if(r.ok!==true){if(r.candidates?.length)showCandidates(emp,r);throw new Error(message(r.msg||'绑定核验失败'));}
    $('#candidate-dialog').close();report('staff','飞书绑定核验通过',[emp.name+'：'+message(r.msg||'姓名、部门和飞书 ID 核验通过')]);await loadStaff();invalidatePreviewEmployees();
  }catch(error){$('#candidate-message').textContent=error.message;throw error;}});
});
$('#download-template').onclick=()=>busy('staff','staff',async()=>{
  const r=await api('/api/employees/template.csv');if(typeof r.csv!=='string')throw new Error('导入模板未返回 csv 内容');const url=URL.createObjectURL(new Blob(['\ufeff'+r.csv.replace(/^\ufeff/,'')],{type:'text/csv;charset=utf-8'}));
  const a=document.createElement('a');a.href=url;a.download='员工名单模板.csv';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('导入模板已下载');
});
$('#import-employees').onclick=()=>busy('staff','staff',async()=>{
  const file=$('#csv').files[0];if(!file)throw new Error('请先选择 CSV 或 XLSX 名单');if(!/\.(csv|xlsx)$/i.test(file.name))throw new Error('仅支持 CSV 或 XLSX 文件');
  const fd=new FormData();fd.append('file',file);const r=await api('/api/employees/import',{method:'POST',body:fd});const errors=Array.isArray(r.errors)?r.errors:[];
  report('staff',`导入${r.ok===false||errors.length?'部分完成':'完成'}：共 ${r.total??'未知'} 行，新增 ${r.added??0}，更新 ${r.updated??0}，跳过 ${r.skipped??0}`,[`文件：${file.name}`,errors.length?`发现 ${errors.length} 条错误；成功行已提交，请只修正失败行后重新导入。`:r.ok===false?message(r.msg||'接口报告部分失败，但未提供行级错误，请核对名单。'):'未返回行级错误。请对导入员工执行三重核验。',...errors.map(e=>`第 ${e.row??'未知'} 行：${message(e.msg)}`)],r.ok===false||errors.length>0);
  $('#csv').value='';await loadStaff();invalidatePreviewEmployees();
});
$('#sync-employees').onclick=()=>busy('staff','staff',async()=>{
  if(!await askConfirm('同步飞书员工名单','同步会新增、更新或停用本地员工资料。完成后请核对变更并执行三重核验。',[],'开始同步'))return;
  const r=await post('/api/sync',{fill_birthday:true});const errors=Array.isArray(r.errors)?r.errors:[];
  report('staff',r.ok===false||errors.length?'飞书同步部分完成，成功项已提交':'飞书同步完成',[`新增 ${r.added??0}，更新 ${r.updated??0}，停用 ${r.disabled??0}`,...errors.map(message),...(r.ok===false&&!errors.length?[message(r.msg||'接口报告部分失败，请核对名单。')]:[]),'请核对在职名单与身份核验状态。'],r.ok===false||errors.length>0);await loadStaff();invalidatePreviewEmployees();
});
async function loadHealth(){
  $('#run-mode').textContent='正在检查…';try{const h=await api('/api/health');$('#run-mode').textContent=h.dry_run===true?'当前为演练模式：完成后记录为「演练完成」，未向飞书发送消息。':h.dry_run===false?'当前为正式模式：确认发送排期后会按计划推送；立即推送需再次确认。':'运行模式未知，请核对服务端配置。';$('#health').textContent=JSON.stringify(h,null,2);}catch(error){$('#run-mode').textContent='检查失败';$('#health').textContent=error.message;}
}
$('#check-health').onclick=loadHealth;
setInterval(async()=>{
  if(TAB==='events'&&!document.hidden&&!BUSY.has('events')&&!eventLoading)await loadEvents(true);
  if(generationTask&&!pollingTask){pollingTask=true;try{const t=await api('/api/gen-task/'+encodeURIComponent(generationTask));if(['done','failed','error'].includes(t.status)){generationTask=null;report('event',t.status==='done'?'生成任务完成':'生成任务失败',[`新建 ${t.created??0} 个事件，成功生成 ${t.generated??0} / ${t.total??t.created??0} 个事件`,t.error||'请查看每条海报的实际状态。'],t.status!=='done'||Number(t.generated)<Number(t.total??t.created));}else{$('#events-loading').textContent=`生成中：${t.generated??0} / ${t.total??t.created??0} 个事件`;}}catch(error){generationTask=null;report('event','生成进度读取失败',[error.message,'生成可能仍在服务端进行，请刷新事件列表确认。'],true);}finally{pollingTask=false;}}
},15000);
window.addEventListener("DOMContentLoaded", ()=>loadEvents());
