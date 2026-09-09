const {test}=require('node:test');const assert=require('node:assert/strict');
const {microStatus}=require('../src/micro-status.cjs');
test('robot state follows native hue, not slot or animation',()=>{
 for(const [c,state] of [[0xffffff,'idle'],[0x309fff,'working'],[0x32d074,'complete'],[0xffb340,'attention'],[0xf34f5f,'error'],[0xb000ff,'unknown']]){
  for(const e of [1,4])assert.equal(microStatus({c,b:1,e,s:.4}),state);
 }
 assert.equal(microStatus({c:0x32d074,b:0,e:1}),'complete');
 assert.equal(microStatus({c:0,b:0,e:0}),'off');
});
