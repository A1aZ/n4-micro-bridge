const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs'),path=require('node:path');
test('launcher budgets exceed backend native process probe timeout',()=>{
 const code=fs.readFileSync(path.join(__dirname,'../scripts/MiraboxLauncher.cs'),'utf8');
 assert.match(code,/path=="\/api\/native\/start"\?15000:5000/);
 assert.match(code,/r\.ReadWriteTimeout=RequestTimeout\(path\)/);
 assert.match(code,/step="检查设备占用并启动 N4"/);
});
