const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(config,fail=false){
 const html=fs.readFileSync(path.join(__dirname,'../webui/real-n4.html'),'utf8');const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,{hidden:false,disabled:false,textContent:''});return elements.get(id);};let saved;
 const context=vm.createContext({document:{getElementById:get},window:{location:{origin:'http://localhost:1234'}},console,fetch:async(url,options)=>{if(fail)throw Error('offline');if(options?.method==='PUT'){saved=JSON.parse(options.body);return {ok:true,json:async()=>({config:saved})};}return {ok:true,json:async()=>config};}});
 const script=html.slice(html.indexOf('<script>')+8,html.indexOf("      $('connect').onclick=connect;"));
 vm.runInContext(script+'globalThis.onboard={renderReasoningSetup,confirmReasoningSetup};})();',context);
 return {api:context.onboard,get,saved:()=>saved};
}
test('public defaults preassign reasoning and show first-use instructions until confirmed',()=>{
 const config=JSON.parse(JSON.stringify(require('../examples/n4-calibrated.json')));assert.equal(config.knobs[2].mode,'reasoning');
 const p=page(config);p.api.renderReasoningSetup(config);assert.equal(p.get('reasoning-setup').hidden,false);
 config.knobs[2].reasoningConfigured=true;p.api.renderReasoningSetup(config);assert.equal(p.get('reasoning-setup').hidden,true);
 config.knobs[2].mode='brightness';config.knobs[2].reasoningConfigured=false;p.api.renderReasoningSetup(config);assert.equal(p.get('reasoning-setup').hidden,true);
});
test('explicit confirmation persists only the third knob acknowledgement',async()=>{
 const config=JSON.parse(JSON.stringify(require('../examples/n4-calibrated.json')));config.extra='preserve';const p=page(config);await p.api.confirmReasoningSetup();
 assert.equal(p.saved().knobs[2].reasoningConfigured,true);assert.equal(p.saved().extra,'preserve');assert.deepEqual(p.saved().buttons,config.buttons);assert.equal(p.get('reasoning-setup').hidden,true);
});
test('failure or changed assignment never pretends setup is saved',async()=>{
 const cfg=JSON.parse(JSON.stringify(require('../examples/n4-calibrated.json')));
 const p=page(cfg,true);await p.api.confirmReasoningSetup();assert.equal(p.saved(),undefined);assert.equal(p.get('reasoning-confirm').disabled,false);assert.match(p.get('reasoning-setup-status').textContent,/offline/);
 cfg.knobs[2].mode='scroll';const q=page(cfg);await q.api.confirmReasoningSetup();assert.equal(q.saved(),undefined);
});
