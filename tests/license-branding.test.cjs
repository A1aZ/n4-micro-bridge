const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');const root=path.resolve(__dirname,'..');
test('original app is A1aZ AGPL-only, driver retains independent MS-PL',()=>{
 const p=require('../package.json');assert.equal(p.license,'AGPL-3.0-only');assert.equal(p.author,'A1aZ');
 assert.match(fs.readFileSync(path.join(root,'LICENSE'),'utf8'),/GNU AFFERO GENERAL PUBLIC LICENSE/);
 assert.match(fs.readFileSync(path.join(root,'COPYRIGHT'),'utf8'),/2026 A1aZ/);
 assert.match(fs.readFileSync(path.join(root,'driver/codexmicro-umdf/MS-PL.txt'),'utf8'),/Microsoft Public License/);
});
test('launcher icon includes Windows small and large sizes',()=>{
 const data=fs.readFileSync(path.join(root,'assets/app/app.ico'));
 assert.equal(data.readUInt16LE(2),1);const count=data.readUInt16LE(4);assert(count>=7);
 const widths=Array.from({length:count},(_,i)=>data[6+i*16]||256);
 for(const width of [16,32,48,256])assert(widths.includes(width));
});
