'use strict';
function assertLocalRequest(req,port){
 const fail=()=>{const e=new Error('Only this local control page may access the API');e.status=403;e.code='origin-not-allowed';throw e;};
 const hosts=new Set([`127.0.0.1:${port}`,`localhost:${port}`,`[::1]:${port}`]);
 if(!hosts.has(String(req.headers.host||'').toLowerCase()))fail();
 if(req.headers['sec-fetch-site']==='cross-site')fail();
 const origin=req.headers.origin;
 if(origin){try {const u=new URL(origin);if(u.protocol!=='http:'||!hosts.has(u.host.toLowerCase())||u.username||u.password)fail();}catch {fail();}}
}
function assertMutation(req){
 if(req.headers['sec-fetch-mode']==='navigate'||['image','script','iframe'].includes(req.headers['sec-fetch-dest'])){const e=new Error('Navigation cannot change device state');e.status=403;e.code='origin-not-allowed';throw e;}
 if((Number(req.headers['content-length'])>0||req.headers['transfer-encoding'])&&!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type']||'')){const e=new Error('JSON content type required');e.status=415;e.code='unsupported-media-type';throw e;}
}
module.exports={assertLocalRequest,assertMutation};
