const {test}=require('node:test');
const assert=require('node:assert/strict');
require('../app/static/review-status.js');
const {people,events,preferredState}=require('../app/static/review-search.js');
const staff=[
  {id:1,name:'同名',department:'研发',employee_no:'001',feishu_open_id:'ou_one'},
  {id:11,name:'同名',department:'市场',employee_no:'011',feishu_open_id:'ou_eleven'},
  {id:3,name:'尚无贺卡',department:'研发',employee_no:'003',feishu_open_id:'ou_three'}
];
const cards=[
  {id:1,employee_id:1,employee:staff[0],status:'ready',event_type:'birthday'},
  {id:2,employee_id:11,employee:staff[1],status:'ready',event_type:'birthday'},
  {id:3,employee_id:1,employee:staff[0],status:'pushed',event_type:'anniversary'},
  {id:4,employee_id:1,employee:staff[0],status:'failed',event_type:'anniversary',exception_hint:'失败'}
];
test('employee search includes people without posters and matches employee number/open_id',()=>{
  assert.deepEqual(people(staff,'尚无贺卡').map(row=>row.id),[3]);
  assert.deepEqual(people(staff,'ou_eleven').map(row=>row.id),[11]);
  assert.deepEqual(people(staff,'003').map(row=>row.id),[3]);
  assert.deepEqual(people(staff,'同名').map(row=>row.id),[1,11]);
  assert.deepEqual(people(staff,'研发').map(row=>row.id),[1,3]);
  assert.deepEqual(people(staff,''),[]);
  assert.deepEqual(people(staff,'不存在'),[]);
});
test('selecting one employee excludes all other same-name employees, including ID prefixes',()=>{
  assert.deepEqual(events(cards,{employeeId:1,keyword:'同名'}).map(row=>row.id),[1]);
  assert.deepEqual(events(cards,{employeeId:'11',keyword:'同名'}).map(row=>row.id),[2]);
  assert.deepEqual(events(cards,{employeeId:99}),[]);
  assert.deepEqual(events(cards,{employeeId:3}),[]);
});
test('clearing selection restores keyword filtering while type/status/department filters still compose',()=>{
  assert.deepEqual(events(cards,{keyword:'同名'}).map(row=>row.id),[1,2]);
  assert.deepEqual(events(cards,{employeeId:1,state:'sent'}).map(row=>row.id),[3]);
  assert.deepEqual(events(cards,{employeeId:1,state:'failed',issues:'issues',type:'anniversary'}).map(row=>row.id),[4]);
  assert.deepEqual(events(cards,{employeeId:1,department:'市场'}),[]);
  assert.deepEqual(events(cards,{employeeId:1,type:'anniversary'}),[]);
});
test('personal review initially shows a populated status and keeps only three statuses',()=>{
  assert.equal(preferredState(cards,1),'pending');
  assert.equal(preferredState(cards.slice(2),1),'failed');
  assert.equal(preferredState([cards[2]],1),'sent');
  assert.equal(preferredState([],3),'pending');
  assert.deepEqual(Object.keys(globalThis.ReviewStatus.labels),['pending','sent','failed']);
});
