/* Shared by the staff table and boundary tests; never mutates employee data. */
(function(root){
  function monthDay(value){
    const match=String(value??'').trim().match(/^(?:\d{4}-)?(\d{1,2})-(\d{1,2})$/);
    if(!match)return '';
    const month=Number(match[1]),day=Number(match[2]),parsed=new Date(Date.UTC(2000,month-1,day));
    if(parsed.getUTCMonth()+1!==month||parsed.getUTCDate()!==day)return '';
    return String(month).padStart(2,'0')+'-'+String(day).padStart(2,'0');
  }
  function apply(rows,f={}){
    const {joinFrom='',joinTo='',birthFrom='',birthTo='',state='active',department=''}=f;
    if(joinFrom&&joinTo&&joinFrom>joinTo)return {rows:[],hint:'入职开始日期不能晚于结束日期，请调整日期区间。'};
    const from=Number(birthFrom),to=Number(birthTo),keyword=String(f.keyword||'').trim().toLowerCase();
    const cross=from&&to&&from>to;
    return {hint:cross?`出生月份按跨年区间筛选：${from} 月至次年 ${to} 月。`:'',rows:rows.filter(e=>{
      if(state!=='all'&&(Number(e.active)===1)!==(state==='active'))return false;
      if(department&&e.department!==department)return false;
      if(keyword&&![e.name,e.employee_no,e.department,e.feishu_open_id].some(v=>String(v??'').toLowerCase().includes(keyword)))return false;
      if((joinFrom||joinTo)&&(!e.join_date||(joinFrom&&e.join_date<joinFrom)||(joinTo&&e.join_date>joinTo)))return false;
      if(from||to){
        const birthday=monthDay(e.birth_date),month=birthday?Number(birthday.slice(0,2)):0;
        if(!month)return false;
        if(cross?!(month>=from||month<=to):((from&&month<from)||(to&&month>to)))return false;
      }
      return true;
    })};
  }
  root.StaffFilters={apply,monthDay};
  if(typeof module!=='undefined')module.exports=root.StaffFilters;
})(globalThis);
