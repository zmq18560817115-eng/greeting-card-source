const {test}=require('node:test');
const assert=require('node:assert/strict');
const {apply,monthDay}=require('../app/static/staff-filters.js');
const rows=[
  {id:1,name:'甲',employee_no:'001',department:'研发',active:1,join_date:'2021-02-01',birth_date:'2000-02-29'},
  {id:2,name:'乙',employee_no:'002',department:'研发',active:1,join_date:'2021-02-28',birth_date:'1990-03-01'},
  {id:3,name:'丙',employee_no:'003',department:'市场',active:0,join_date:'2021-03-01',birth_date:'1990-12-31'},
  {id:4,name:'丁',department:'市场',active:1,join_date:null,birth_date:null}
];
const ids=f=>apply(rows,f).rows.map(e=>e.id);
test('join dates include both boundaries and combine with department and keyword',()=>{
  assert.deepEqual(ids({joinFrom:'2021-02-01',joinTo:'2021-02-28',department:'研发'}),[1,2]);
  assert.deepEqual(ids({joinFrom:'2021-02-01',joinTo:'2021-02-28',keyword:'001'}),[1]);
  assert.deepEqual(ids({joinFrom:'2021-03-01',state:'all'}),[3]);
});
test('birth month range includes leap days, end months and year wrap',()=>{
  assert.deepEqual(ids({birthFrom:'2',birthTo:'3'}),[1,2]);
  assert.deepEqual(ids({birthFrom:'11',birthTo:'2',state:'all'}),[1,3]);
  assert.match(apply(rows,{birthFrom:'11',birthTo:'2'}).hint,/跨年/);
  assert.deepEqual(ids({birthFrom:'3',birthTo:'3'}),[2]);
  assert.deepEqual(ids({birthTo:'2'}),[1]);
  assert.deepEqual(ids({birthFrom:'12',state:'all'}),[3]);
});
test('invalid date interval yields no bulk targets, and state filtering excludes departures',()=>{
  const invalid=apply(rows,{joinFrom:'2022-01-01',joinTo:'2021-01-01'});
  assert.deepEqual(invalid.rows,[]);assert.match(invalid.hint,/不能晚于/);
  assert.deepEqual(ids({state:'inactive'}),[3]);assert.deepEqual(ids({}),[1,2,4]);
});

test('birthday display and month filtering work with or without a birth year',()=>{
  for(const value of ['02-29','2-29','2000-02-29','1896-02-29'])assert.equal(monthDay(value),'02-29');
  for(const value of ['',null,'02-30','13-01','2026-09'])assert.equal(monthDay(value),'');
  const mixed=[{id:1,active:1,birth_date:'02-29'},{id:2,active:1,birth_date:'1990-02-01'},
    {id:3,active:1,birth_date:'03-01'}];
  assert.deepEqual(apply(mixed,{birthFrom:'2',birthTo:'2'}).rows.map(row=>row.id),[1,2]);
  assert.equal(monthDay('1995-09-11'),monthDay('09-11'));
});

test('open_id search combines with department and excludes unrelated employees',()=>{
  const staff=rows.map(row=>({...row,feishu_open_id:'ou_employee_'+row.id}));
  assert.deepEqual(apply(staff,{keyword:'ou_employee_2'}).rows.map(row=>row.id),[2]);
  assert.deepEqual(apply(staff,{keyword:'ou_employee_2',department:'市场'}).rows,[]);
  assert.deepEqual(apply(staff,{keyword:'missing_open_id'}).rows,[]);
});
