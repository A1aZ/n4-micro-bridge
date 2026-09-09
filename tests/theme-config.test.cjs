const {test}=require('node:test');
const assert=require('node:assert/strict');
const {defaultConfig,normalizeConfig,mapN4HardwareEvent}=require('../src/micro-config.cjs');
const {createN4RenderModel}=require('../src/n4-visuals.cjs');
test('themes validate, survive config roundtrip and never change hardware actions',()=>{
  const original=defaultConfig();const events=mapN4HardwareEvent(0xa1,0,original);
  for(const theme of ['symbols','atlas','pixel','debug']){
    const config=normalizeConfig({...original,visual:{theme}});
    assert.equal(normalizeConfig(JSON.parse(JSON.stringify(config))).visual.theme,theme);
    assert.deepEqual(config.buttons,original.buttons);
    assert.deepEqual(mapN4HardwareEvent(0xa1,0,config),events);
    const model=createN4RenderModel({config});assert.equal(model.theme,theme);
    assert.equal(model.buttons.length,14);assert.ok(model.buttons.every(key=>key.svg.includes('<svg')));
    if(theme!=='debug')assert.ok(model.buttons[0].svg.includes('data-theme="'+theme+'"'));
    if(theme==='pixel')assert.ok(model.buttons[0].svg.includes('data:image/png;base64'));
  }
  assert.throws(()=>normalizeConfig({...original,visual:{theme:'../../anything'}}),/theme/);
});
