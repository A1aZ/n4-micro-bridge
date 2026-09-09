const {test}=require('node:test');
const assert=require('node:assert/strict');
const {defaultConfig,normalizeConfig,mapN4HardwareEvent}=require('../src/micro-config.cjs');
const {createN4RenderModel}=require('../src/n4-visuals.cjs');
test('measured N4 rows: input 01–05 top and 06–0A bottom agree with displayed targets',()=>{
 const config=defaultConfig();config.inputProfile='custom';
 config.buttons.slice(0,10).forEach((b,i)=>b.hardwareCode=i+1);
 const normalized=normalizeConfig(config),model=createN4RenderModel({config:normalized});
 for(let i=0;i<10;i++){
  assert.equal(model.buttons[i].sdkKey,i+1);
  assert.equal(model.buttons[i].hardwareCode,i+1);
  for(const state of [1,0]){
   const events=mapN4HardwareEvent(i+1,state,normalized);
   assert.equal(events[0].params.k,model.buttons[i].targetKey);
   assert.equal(events[0].params.act,state);
  }
 }
 assert.deepEqual(normalized.knobs,config.knobs);
 assert.deepEqual(normalized.buttons.slice(10),config.buttons.slice(10));
});
