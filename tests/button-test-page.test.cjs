const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const page=fs.readFileSync(require('node:path').join(__dirname,'../webui/button-test.html'),'utf8');
const script=page.match(/<script>([\s\S]*?)<\/script>/)[1];
test('button monitor script parses and stays read-only',()=>{
 new vm.Script(script);
 assert(!/method\s*:\s*['"](?:POST|PUT|DELETE)/.test(script));
 assert(!script.includes('transport/input'));
 assert(page.includes('实体操作仍会触发 Codex 功能'));
});
const extract=vm.runInNewContext('('+script.slice(script.indexOf('function extract('),script.indexOf('function meaning('))+')');
test('monitor recognizes raw 512 and prefixed 513 byte reports',()=>{
 const bytes=Buffer.alloc(512);Buffer.from([65,67,75,0,0,79,75]).copy(bytes);bytes[9]=6;bytes[10]=1;
 const hex=b=>Array.from(b,x=>x.toString(16).padStart(2,'0')).join(' ');
 for(const b of [bytes,Buffer.concat([Buffer.from([0]),bytes])]){
   const r=extract({hex:hex(b)});assert.equal(r.code,6);assert.equal(r.state,1);
 }
 assert.equal(extract({hex:'41 43'}),null);
 assert.equal(extract({hardwareCode:0xa1,state:0}).code,0xa1);
});
