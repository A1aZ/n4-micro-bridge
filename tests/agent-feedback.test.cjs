const {test}=require('node:test');
const assert=require('node:assert/strict');
const {agentFeedback,themeKeyGroup}=require('../src/n4-theme-svg.cjs');
test('agent feedback labels describe lights, never infer task completion',()=>{
 for(const [e,label] of [[0,'未亮'],[1,'常亮'],[2,'流动'],[3,'彩虹'],[4,'呼吸'],[5,'渐变'],[6,'微呼吸']]){
  const f=agentFeedback({c:0x33bbcc,b:1,e,s:.4});assert.equal(f.label,label);assert.equal(f.animated,e>1);
 }
 assert.equal(agentFeedback({c:0xffffff,b:1,e:4,s:0}).animated,false);
 assert.equal(agentFeedback({c:0xffffff,b:0,e:4,s:.4}).animated,false);
 assert.equal(agentFeedback({c:0xffffff,b:1,e:4,s:.4},false).label,'停用');
});
test('themes use fixed-position pulse without status text or moving geometry',()=>{
 for(const theme of ['symbols','atlas','pixel']){
  const svg=themeKeyGroup({theme,width:112,enabled:true,targetKey:'AG00',light:{c:0x33bbcc,b:1,e:4,s:.4,intensity:1}},0,0);
  assert.doesNotMatch(svg,/>呼吸<\/text>/);assert.match(svg,/class="agent-pulse"/);assert.doesNotMatch(svg,/class="agent-(travel|float)"/);
 }
});
