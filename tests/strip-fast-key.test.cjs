const {test}=require('node:test');const assert=require('node:assert/strict');
const {defaultConfig,mapN4HardwareEvent}=require('../src/micro-config.cjs');
const {createN4RenderModel}=require('../src/n4-visuals.cjs');
test('rightmost information zone exposes only an explicitly assigned fast key',()=>{
 const c=defaultConfig();c.visual.stripMode='knobs';c.buttons[13].targetKey='ACT06';c.buttons[13].enabled=true;
 const oneShot=mapN4HardwareEvent(0x43,0,c);
 assert.deepEqual(oneShot.map(event=>event.params),[
  {k:'ACT06',act:1},
  {k:'ACT06',act:0},
 ]);
 assert.deepEqual(oneShot[1].meta,{synthetic:true,delayMs:100});
 assert.deepEqual(mapN4HardwareEvent(0x43,1,c).map(event=>event.params),[{k:'ACT06',act:1}]);
 for(const code of [0x40,0x41,0x42])assert.deepEqual(mapN4HardwareEvent(code,1,c),[]);
 const model=createN4RenderModel({config:c});
 assert.deepEqual(model.buttons[13].knobInfo.touchAction,{
  targetKey:'ACT06',eyebrow:'快捷',label:'加速',icon:'lightning',scope:'strip',layout:'stacked',
 });
 assert.match(model.buttons[13].svg,/data-strip-layout="dashboard"/);
 assert.match(model.buttons[13].svg,/data-panel-kind="action"/);
 assert.match(model.buttons[13].svg,/data-knob-hint="true"/);
 assert.match(model.buttons[13].svg,/data-touch-action="ACT06"/);
 assert.match(model.buttons[13].svg,/data-touch-scope="strip"/);
 assert.match(model.buttons[13].svg,/<rect x="1" y="1" width="174" height="76" rx="8"/);
 assert.match(model.buttons[13].svg,/<rect x="1" y="84" width="174" height="27" rx="7"/);
 assert.match(model.buttons[13].svg,/M48 15L32 42H43L38 66L62 35H51Z/);
 assert.doesNotMatch(model.buttons[13].svg,/data-fast-touch-divider/);
 c.buttons[13].enabled=false;assert.deepEqual(mapN4HardwareEvent(0x43,1,c),[]);
 c.buttons[13].enabled=true;c.buttons[13].targetKey='ACT07';assert.deepEqual(mapN4HardwareEvent(0x43,1,c),[]);
});
