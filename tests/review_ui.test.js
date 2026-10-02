const test=require('node:test');const assert=require('node:assert/strict');
const {ReviewController}=require('../tm_api/ui/review.js');
function view(id='q1',action='dismiss'){return {review_ui:true,focus:{version:4},question:{id,revision:2,title:'Synthetic',options:[{label:'Пропустить',answer:{review_id:id,review_revision:2,expected_version:4,action,user_text:'Synthetic — Пропустить',confirmed:true,effect:null}}]}};}
function harness(tool){const calls=[],paints=[];const bridge={uuid:()=> 'stable-key',context:async data=>calls.push(['context',data]),message:async text=>{calls.push(['message',text]);return{};},tool:async(n,a)=>{calls.push([n,a]);return tool(n,a);}};const c=new ReviewController(bridge,s=>paints.push(JSON.parse(JSON.stringify(s))));c.snapshot(view());return{c,calls,paints,bridge};}
test('one click: context, immediate chat, guarded answer, next; blue persists while awaiting',async()=>{
 let release;const waiting=new Promise(r=>release=r);const h=harness(async n=>n==='answer_review_question'?(await waiting,{decision_saved:true}):view('q2'));
 const run=h.c.choose(0);await new Promise(r=>setImmediate(r));
 assert.equal(h.c.busy,true);assert.equal(h.c.state.selected,0);await h.c.choose(0);
 assert.deepEqual(h.calls.map(c=>c[0]),['context','message','answer_review_question']);
 release();await run;assert.equal(h.c.state.view.question.id,'q2');assert.equal(h.c.state.selected,null);
});
test('unknown outcome retries exact payload/key without another chat message',async()=>{
 let attempts=0;const h=harness(async n=>{if(n==='answer_review_question'){if(++attempts===1)throw Error('timeout');return{decision_saved:true};}return view('q2');});
 await h.c.choose(0);assert.equal(h.c.state.view.question.id,'q1');assert.ok(h.c.state.error);await h.c.retry();
 const writes=h.calls.filter(x=>x[0]==='answer_review_question');assert.equal(writes.length,2);assert.deepEqual(writes[0],writes[1]);assert.equal(h.calls.filter(x=>x[0]==='message').length,1);
});
test('chat failure cannot mutate the database',async()=>{const h=harness(()=>{throw Error('unexpected tool');});h.bridge.message=async()=>({isError:true});await h.c.choose(0);assert.equal(h.calls.filter(x=>x[0]==='answer_review_question').length,0);assert.equal(h.c.state.view.question.id,'q1');});
test('clarification retains focus, then advances only when server says resolved',async()=>{let resolved=false;const h=harness(async n=>n==='answer_review_question'?{decision_saved:true}:n==='get_current_review_question'?{question:resolved?null:{id:'q1',revision:2}}:view('q2'));h.c.state.view=view('q1','clarify');await h.c.choose(0);assert.equal(h.c.state.view.question.id,'q1');await h.c.poll();assert.equal(h.c.state.view.question.id,'q1');resolved=true;await h.c.poll();assert.equal(h.c.state.view.question.id,'q2');});
test('failure fetching next never replays a committed action',async()=>{let reads=0;const h=harness(async n=>{if(n==='answer_review_question')return{decision_saved:true};if(++reads===1)throw Error('offline');return view('q2');});await h.c.choose(0);assert.equal(h.c.state.pending.committed,true);await h.c.retry();assert.equal(h.calls.filter(x=>x[0]==='answer_review_question').length,1);assert.equal(h.c.state.view.question.id,'q2');});
test('stale answer remains on current card and does not auto-apply to next',async()=>{const h=harness(async()=>({error:'guard_rejected'}));await h.c.choose(0);assert.equal(h.c.state.stale,true);assert.equal(h.c.state.view.question.id,'q1');assert.equal(h.calls.filter(x=>x[0]==='show_review_question').length,0);});
test('accepts MCP JSON text fallback and rejects embedded business errors',()=>{const {unwrap}=require('../tm_api/ui/review.js');assert.deepEqual(unwrap({content:[{type:'text',text:'{"decision_saved":true}'}]}),{decision_saved:true});assert.throws(()=>unwrap({content:[{type:'text',text:'{"error":"guard_rejected"}'}]}));});
