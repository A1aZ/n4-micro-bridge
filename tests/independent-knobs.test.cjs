const {test}=require('node:test');
const assert=require('node:assert/strict');
const {MicroBridge}=require('../src/micro-bridge.cjs');
const {defaultConfig,normalizeConfig,mapN4HardwareEvent,mapN4ReportToWire}=require('../src/micro-config.cjs');
function split(){const c=defaultConfig();c.knobs.forEach((k,i)=>k.mode=['micro','scroll','reasoning','brightness'][i]);return c;}
test('four modes are separate and local actions never enter the Micro wire queue',()=>{
  const config=split(),events=[];const b=new MicroBridge({config,onEvent:e=>events.push(e)});
  assert.equal(b.handleHardwareEvent(0xa1,0)[0].params.k,'ENC_CW');
  assert.deepEqual(b.handleHardwareEvent(0x51,0),[]);
  assert.deepEqual(b.drainLocalActions(),[{type:'scroll',direction:1}]);
  assert.deepEqual(b.handleHardwareEvent(0x71,0),[]);
  assert.deepEqual(b.drainLocalActions(),[{type:'brightness',delta:5}]);
  assert.equal(events.length,1);b.close();
});
test('reasoning stick events require explicit setup and release after every detent',()=>{
  const config=split();assert.equal(mapN4HardwareEvent(0x91,0,config)[0].localAction.type,'setup-required');
  config.knobs[2].reasoningConfigured=true;
  assert.deepEqual(mapN4HardwareEvent(0x91,0,config),[{method:'v.oai.rad',params:{a:0,d:1}},{method:'v.oai.rad',params:{a:0,d:0}}]);
  assert.deepEqual(mapN4HardwareEvent(0x90,0,config),[{method:'v.oai.rad',params:{a:.5,d:1}},{method:'v.oai.rad',params:{a:.5,d:0}}]);
  for(const code of [0x35,0x33,0x36])assert.deepEqual(mapN4HardwareEvent(code,1,config),[]);
});
test('mode configuration rejects unknown actions and hides local actions from wire export',()=>{
  const config=split();config.knobs[2].mode='shell';assert.throws(()=>normalizeConfig(config),/mode/);
  const r=Buffer.alloc(512);r.set([65,67,75,0,0,79,75]);r[9]=0x70;
  assert.deepEqual(mapN4ReportToWire(r,split()),[]);
});
