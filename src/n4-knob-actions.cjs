'use strict';
const KNOB_MODES=Object.freeze(['micro','scroll','reasoning','brightness']);
function independentRotation(knob,direction){
  const mode=knob.mode||'micro';
  if(mode==='micro')return null;
  if(mode==='reasoning'){
    if(knob.reasoningConfigured!==true)return [{localAction:{type:'setup-required',feature:'reasoning'}}];
    const a=direction>0?0:0.5; // Native Micro stick right/left, followed by centre.
    return [{method:'v.oai.rad',params:{a,d:1}},{method:'v.oai.rad',params:{a,d:0}}];
  }
  if(mode==='scroll')return [{localAction:{type:'scroll',direction}}];
  if(mode==='brightness')return [{localAction:{type:'brightness',delta:direction*5}}];
  throw new Error('Unsupported knob mode');
}
module.exports={KNOB_MODES,independentRotation};
