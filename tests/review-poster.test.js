const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

// Load the real workspace functions without a browser, database, timers or network.
function workspace(){
  const elements=new Map();
  const context=vm.createContext({URL,location:{origin:'http://localhost:8848'},
    document:{querySelector(selector){
      if(!elements.has(selector))elements.set(selector,{value:'',addEventListener(){},add(){}});
      return elements.get(selector);
    },querySelectorAll:()=>[],addEventListener(){}},
    window:{addEventListener(){}},localStorage:{getItem:()=>null},
    Option:function(){},setInterval(){},RowSelection:{attach(){}},
    StaffFilters:require('../app/static/staff-filters.js'),
    ReviewSearch:require('../app/static/review-search.js'),
    ReviewStatus:require('../app/static/review-status.js')});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../app/static/workspace.js'),'utf8'),context);
  return context;
}
function event(overrides={}){
  return {id:10,status:'ready',selected_card_id:null,event_type:'anniversary',
    employee:{id:1,name:'界面测试',department:'测试',active:1,open_id:'ou_test',identity_status:'verified'},
    cards:[1,2,3,4,5].map(id=>({id,status:'ok',url:`/files/cards/test-${id}.png`})),...overrides};
}

test('a sent legacy record shows only its sent poster and keeps zoom and logs',()=>{
  const app=workspace(),record=event({status:'pushed',selected_card_id:3});
  const html=app.renderEventDetails(record);
  assert.equal(app.previewCard(record).id,3);
  assert.equal((html.match(/<img /g)||[]).length,1);
  assert.match(html,/data-zoom="http:\/\/localhost:8848\/files\/cards\/test-3.png"/);
  assert.match(html,/data-action="logs"/);
  assert.doesNotMatch(html,/全部方案|待选择|选用|已选中|data-action="(?:select|confirm|push|regenerate)"/);
});

test('confirm automatically links the displayed poster even for legacy multiple cards',async()=>{
  const app=workspace(),record=event();record.cards[0].status='failed';
  assert.equal(app.previewCard(record).id,2);
  assert.equal(app.canConfirm(record),true);
  const calls=[];
  app.post=async(url,body)=>{calls.push({url,cardId:body.card_id});return {ok:true};};
  app.freshEvent=async()=>({...record,selected_card_id:2});
  const confirmed=await app.ensureSelected(record);
  assert.deepEqual(calls,[{url:'/api/events/10/select',cardId:2}]);
  assert.equal(confirmed.selected_card_id,2);
  const html=app.renderEventDetails(record);
  assert.equal((html.match(/<img /g)||[]).length,1);
  assert.match(html,/标记已核查/);
  assert.match(html,/无需逐条确认/);
  assert.doesNotMatch(html,/data-action="confirm"/);
  assert.doesNotMatch(html,/方案|data-action="select"|请先选择/);
});

test('an existing selected poster is retained without making another selection request',async()=>{
  const app=workspace(),record=event({selected_card_id:4});
  app.post=()=>assert.fail('must not reselect an existing poster');
  assert.equal(app.previewCard(record).id,4);
  assert.equal(await app.ensureSelected(record),record);
});

test('missing confirmed or sent posters never fall back to a different old card',async()=>{
  const app=workspace();
  for(const status of ['ready','confirmed','pushed','delivery_unknown']){
    const record=event({status,selected_card_id:99});
    assert.equal(app.previewCard(record),undefined);
    assert.equal(app.canConfirm(record),false);
    assert.equal(app.canPush(record),false);
    assert.doesNotMatch(app.renderEventDetails(record),/<img /);
    await assert.rejects(app.ensureSelected(record),/重新生成后审核/);
  }
  assert.equal(app.previewCard(event({status:'pushed'})),undefined);
});

test('changing an unselected preview invalidates the previous review snapshot',()=>{
  const app=workspace(),record=event(),snapshot=app.reviewSnapshot(record);
  record.cards.reverse();
  assert.notEqual(app.reviewSnapshot(record),snapshot);
  const changed=app.reviewSnapshot(record);record.cards[0].url='/files/cards/changed.png';
  assert.notEqual(app.reviewSnapshot(record),changed);
});

test('unavailable cards show a single empty state and regeneration instead of options',()=>{
  const app=workspace(),record=event({status:'gen_failed',cards:[{id:1,status:'failed',error:'测试错误'}]});
  const html=app.renderEventDetails(record);
  assert.match(html,/尚无可用海报/);
  assert.match(html,/重新生成/);
  assert.doesNotMatch(html,/方案|<img /);
  assert.equal(app.canConfirm(record),false);
});
