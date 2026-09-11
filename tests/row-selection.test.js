const {test}=require('node:test');
const assert=require('node:assert/strict');
const {range,edgeSpeed}=require('../app/static/row-selection.js');
const ids=['a','b','c','d','e','f'];
const values=value=>[...value].sort();

test('drag selects an inclusive range while preserving selections outside it',()=>{
  assert.deepEqual(values(range(new Set(['f']),ids,1,3,true)),['b','c','d','f']);
});
test('dragging upward has the same bounds and reversing shrinks to the original selection',()=>{
  const base=new Set(['f']);
  assert.deepEqual(values(range(base,ids,3,1,true)),['b','c','d','f']);
  assert.deepEqual(values(range(base,ids,1,2,true)),['b','c','f']);
  assert.deepEqual(values(base),['f']);
});
test('starting from a selected row removes the range without clearing other selections',()=>{
  assert.deepEqual(values(range(new Set(ids),ids,3,1,false)),['a','e','f']);
});
test('range uses filtered and sorted row order and never adds hidden records',()=>{
  assert.deepEqual(values(range(new Set(['hidden']),['f','d','b'],0,1,true)),['d','f']);
});
test('a missing anchor does not select unrelated records',()=>{
  assert.deepEqual(values(range(new Set(['a']),ids,-1,4,true)),['a']);
  assert.deepEqual(values(range(new Set(['a']),ids,0,20,true)),['a']);
});
test('edge scrolling stops in the middle and accelerates toward either edge',()=>{
  assert.equal(edgeSpeed(200,100,300),0);
  assert.equal(edgeSpeed(0,100,300),-900);
  assert.equal(edgeSpeed(400,100,300),900);
  assert.ok(edgeSpeed(130,100,300)<0);
  assert.ok(edgeSpeed(280,100,300)>0);
  assert.equal(edgeSpeed(100,100,100),0);
});
