const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
test('public example never inherits a user-confirmed reasoning binding',()=>{
 const config=require('../examples/n4-calibrated.json');
 assert(config.knobs.every(k=>k.reasoningConfigured===false));
 assert.deepEqual(config.buttons.slice(0,10).map(b=>b.hardwareCode),[1,2,3,4,5,6,7,8,9,10]);
});
test('release construction uses example config and excludes private inspection script',()=>{
 const build=fs.readFileSync(path.join(root,'scripts/build-portable.ps1'),'utf8');
 assert(build.includes('examples\\n4-calibrated.json'));
 assert(!build.includes('$project\\data\\config.json'));
 const exporter=fs.readFileSync(path.join(root,'scripts/export-source.cjs'),'utf8');
 assert(exporter.includes('inspect-asar.cjs'));
 assert(exporter.includes('TransportDLL'));
});
