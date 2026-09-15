const {test}=require('node:test');const assert=require('node:assert/strict');
const {defaultConfig,mapN4HardwareEvent}=require('../src/micro-config.cjs');
const {createN4RenderModel}=require('../src/n4-visuals.cjs');
const {infoStripGroup}=require('../src/n4-theme-svg.cjs');
test('information strip renders four roles and consumes former shortcut touches',()=>{
 const c=defaultConfig();c.visual.stripMode='knobs';c.knobs.forEach((k,i)=>k.mode=['micro','scroll','reasoning','brightness'][i]);
 const model=createN4RenderModel({config:c});
 for(const code of [0x40,0x41,0x42,0x43])assert.deepEqual(mapN4HardwareEvent(code,1,c),[]);
 assert.ok(!model.buttons[10].svg.includes('导航'));
 ['聊天滚动','推理强度','亮度'].forEach((label,i)=>assert.ok(model.buttons[11+i].svg.includes(label)));
 assert.ok(model.buttons[12].svg.includes('待绑定'));assert.ok(!model.buttons[13].svg.includes('100%'));
 const live=createN4RenderModel({config:c,localActions:{brightness:65,brightnessApplied:true}});
 assert.ok(live.buttons[13].svg.includes('65%'));
 c.visual.stripMode='buttons';assert.equal(mapN4HardwareEvent(0x40,1,c)[0].params.k,'ACT10');
});

test('left information strip shows a deterministic clock and keeps knob hint',()=>{
 const c=defaultConfig();c.visual.stripMode='knobs';
 const model=createN4RenderModel({
   config:c,
   clock:{time:'09:07',date:'2026-09-15',weekday:'周二'},
 });
 assert.match(model.buttons[10].svg,/data-strip-clock/);
 assert.match(model.buttons[10].svg,/09:07/);
 assert.match(model.buttons[10].svg,/2026-09-15/);
 assert.match(model.buttons[10].svg,/>旋钮 1<\/text>/);
 assert.doesNotMatch(model.buttons[10].svg,/导航/);
});

test('generic Micro knob never claims a Codex-selected function',()=>{
 const svg=infoStripGroup({knobInfo:{index:0,mode:'micro',enabled:true}},0,0);
 assert.doesNotMatch(svg,/导航/);
 assert.match(svg,/功能由 Codex 决定/);
});
