/* Explicit employee selection never expands to another person with the same name. */
(function(root){
  const text=value=>String(value??'').trim().toLowerCase();
  function matches(employee,keyword){
    const needle=text(keyword);
    return !needle||[employee?.name,employee?.employee_no,employee?.department,employee?.id,employee?.feishu_open_id,employee?.open_id].some(value=>text(value).includes(needle));
  }
  function people(rows,keyword){
    const needle=text(keyword);
    if(!needle)return [];
    const exact=row=>[row.name,row.employee_no,row.feishu_open_id,row.open_id].some(value=>text(value)===needle);
    return rows.filter(row=>matches(row,needle)).sort((a,b)=>Number(exact(b))-Number(exact(a)));
  }
  function events(rows,{employeeId=null,keyword='',state='pending',type='',department='',issues=''}={}){
    return rows.filter(event=>{
      const employee=event.employee||{};
      if(employeeId!=null){
        if(String(event.employee_id??employee.id)!==String(employeeId))return false;
      }else if(!matches(employee,keyword))return false;
      return root.ReviewStatus.of(event)===state&&(!type||event.event_type===type)&&(!department||employee.department===department)&&
        (!issues||(issues==='issues'?Boolean(event.exception_hint):!event.exception_hint));
    });
  }
  function preferredState(rows,employeeId){
    const states=new Set(rows.filter(row=>String(row.employee_id??row.employee?.id)===String(employeeId)).map(root.ReviewStatus.of));
    return ['pending','failed','sent'].find(state=>states.has(state))||'pending';
  }
  root.ReviewSearch={people,events,preferredState};
  if(typeof module!=='undefined')module.exports=root.ReviewSearch;
})(globalThis);
