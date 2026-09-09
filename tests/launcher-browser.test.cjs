const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
test('starting services never launches browser; manual entry remains',()=>{
 const source=fs.readFileSync(path.join(__dirname,'../scripts/MiraboxLauncher.cs'),'utf8');
 const start=source.slice(source.indexOf('async Task StartAll()'),source.indexOf('void StopAll()'));
 assert(!start.includes('Process.Start('));
 assert(source.includes('web.Click+=(s,e)=>OpenControlPage()'));
 assert(source.includes('trayMenu.Items.Add("打开控制页",null,(s,e)=>OpenControlPage())'));
});
