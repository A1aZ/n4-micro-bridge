'use strict';
// Presentation of native Micro colour families, never a local task state.
function microStatus(light,enabled=true){
 // Keep colour meaning separate from visibility; dimming is not unassignment.
 if(!enabled||!light.c)return 'off';
 const c=light.c,r=(c>>16&255)/255,g=(c>>8&255)/255,b=(c&255)/255;
 const max=Math.max(r,g,b),min=Math.min(r,g,b),d=max-min;
 if(d/Math.max(max,.001)<.18)return 'idle';
 let h=d===0?0:max===r?60*((g-b)/d%6):max===g?60*((b-r)/d+2):60*((r-g)/d+4);if(h<0)h+=360;
 if(h<=15||h>=345)return 'error';
 if(h>=25&&h<=65)return 'attention';
 if(h>=90&&h<=165)return 'complete';
 if(h>=180&&h<=250)return 'working';
 return 'unknown';
}
module.exports={microStatus};
