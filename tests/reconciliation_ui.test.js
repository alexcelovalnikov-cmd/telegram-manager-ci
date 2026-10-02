const test=require('node:test');const assert=require('node:assert/strict');
const {ReconciliationController}=require('../tm_api/ui/reconciliation.js');
function view(){
 return {reconciliation_ui:true,findings:[{id:'q1',revision:2,item_key:'reconciliation:-1:5:reply',title:'Нужен ответ',context:'Вопрос',uncertainty:'Ответ не найден',source:{chat_id:-1,message_id:5},evidence:[],options:[
  {label:'Уточнить',intent:'clarify',answer:{review_id:'q1',revision:2,action_id:'clarify'}},
  {label:'Пропустить',intent:'dismiss',answer:{review_id:'q1',revision:2,action_id:'dismiss'}}
 ]}]};
}
function harness(tool){
 const calls=[],paints=[];const bridge={uuid:()=> 'stable-key',context:async d=>calls.push(['context',d]),message:async t=>{calls.push(['message',t]);return{};},tool:async(n,a)=>{calls.push([n,a]);return tool(n,a);}};
 const c=new ReconciliationController(bridge,s=>paints.push(JSON.parse(JSON.stringify(s))));c.snapshot(view());return{c,calls,paints};
}
test('one click sends user message, answers exact finding, then refreshes list',async()=>{
 const h=harness(async n=>n==='answer_reconciliation_item'?{decision_saved:true,action:'dismiss'}:view());
 await h.c.choose(0,1);
 assert.deepEqual(h.calls.map(x=>x[0]),['context','message','answer_reconciliation_item','show_reconciliation','context']);
 const write=h.calls.find(x=>x[0]==='answer_reconciliation_item');
 assert.equal(write[1].request_key,'ui-reconciliation-stable-key');
 assert.equal(write[1].answer.review_id,'q1');assert.equal(write[1].answer.action_id,'dismiss');assert.equal(write[1].answer.confirmed,true);
 assert.equal(h.calls.filter(x=>x[0]==='message').length,1);
});
test('unknown outcome retries exact key and payload without a second message',async()=>{
 let attempts=0;const h=harness(async n=>{if(n==='answer_reconciliation_item'){if(++attempts===1)throw Error('timeout');return{decision_saved:true,action:'dismiss'};}return view();});
 await h.c.choose(0,1);assert.ok(h.c.state.error);await h.c.retry();
 const writes=h.calls.filter(x=>x[0]==='answer_reconciliation_item');assert.equal(writes.length,2);assert.deepEqual(writes[0],writes[1]);assert.equal(h.calls.filter(x=>x[0]==='message').length,1);
});
test('clarify publishes dialogue context and keeps no duplicate write',async()=>{
 const h=harness(async n=>n==='answer_reconciliation_item'?{decision_saved:true,action:'clarify',needs_dialogue:true}:view());
 await h.c.choose(0,0);
 assert.equal(h.calls.filter(x=>x[0]==='answer_reconciliation_item').length,1);
 assert.ok(h.calls.filter(x=>x[0]==='context').some(x=>x[1].reconciliation_submission?.needs_dialogue===true));
});
test('stale guard does not apply another finding',async()=>{
 const h=harness(async n=>{if(n==='answer_reconciliation_item'){const e=Error('guard');e.guard=true;throw e;}return view();});
 await h.c.choose(0,1);assert.equal(h.c.state.stale,true);assert.equal(h.calls.filter(x=>x[0]==='show_reconciliation').length,0);
});
test('unwrap accepts MCP JSON text fallback',()=>{
 const {unwrap}=require('../tm_api/ui/reconciliation.js');assert.deepEqual(unwrap({content:[{type:'text',text:'{"reconciliation_ui":true,"findings":[]}'}]}),{reconciliation_ui:true,findings:[]});
});
