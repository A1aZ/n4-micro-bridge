const {test}=require('node:test');const assert=require('node:assert/strict');
const {themeKeyGroup}=require('../src/n4-theme-svg.cjs');
const {microStatus}=require('../src/micro-status.cjs');
test('dimming preserves retained colour meaning and displays visibility marker',()=>{
 for(const c of [0x32d074,0x309fff,0xffffff]){
  assert.equal(microStatus({c,b:0,e:0}),microStatus({c,b:1,e:1}));
  const svg=themeKeyGroup({theme:'pixel',width:112,targetKey:'AG00',enabled:true,light:{c,b:0,e:0,s:0,intensity:0}},0,0);
  assert.match(svg,/data-light-dimmed="true"/);
  assert.doesNotMatch(svg,/class="agent-pulse"/);
 }
});
