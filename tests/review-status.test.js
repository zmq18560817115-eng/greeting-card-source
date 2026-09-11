const {test}=require('node:test');
const assert=require('node:assert/strict');
const {labels,of,note}=require('../app/static/review-status.js');

test('all workflow states belong to one of three visible filters',()=>{
  assert.deepEqual(Object.values(labels),['待推送','已推送','推送失败']);
  for(const status of ['ready','confirmed','generating','needs_regeneration','pushing','skipped','simulated','delivery_unknown'])assert.equal(of({status}),'pending',status);
  for(const status of ['failed','gen_failed','blocked','expired'])assert.equal(of({status}),'failed',status);
  assert.equal(of({status:'pushed'}),'sent');
});
test('uncertain receipts and rehearsals never become delivered or retryable failures',()=>{
  for(const status of ['delivery_unknown','simulated'])assert.equal(of({status,pushed_at:'2026-09-11',delivery_state:'sent'}),'pending');
  assert.match(note({status:'delivery_unknown'}),/可能已收到.*暂勿重发/);
  assert.match(note({status:'simulated'}),/未向飞书发送/);
  assert.match(note({status:'skipped'}),/不会自动推送/);
});
test('delivery evidence is preserved and concrete exception remains visible',()=>{
  assert.equal(of({status:'failed',pushed_at:'2026-09-11'}),'sent');
  assert.equal(of({delivery_state:'sent'}),'sent');
  assert.equal(note({status:'failed',exception_hint:'飞书权限不足'}),'飞书权限不足');
});
