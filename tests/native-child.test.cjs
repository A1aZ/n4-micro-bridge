'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {nativeSpawnOptions, observeNativeChild} = require('../src/native-child.cjs');

function fixture(control={managed:true,status:'starting'}) {
  const child=new EventEmitter(); child.stdout=new PassThrough(); child.stderr=new PassThrough();
  const result={starts:0,stdout:'',stderr:'',current:true};
  observeNativeChild(child,{
    isCurrent:()=>result.current,getControl:()=>control,
    onRunning:()=>{result.starts++;control.status='running';},
    onStdout:chunk=>{result.stdout+=chunk;},onStderr:chunk=>{result.stderr+=chunk;},
  });
  return {child,result,control};
}
test('spawn updates process status once without manufacturing device readiness',()=>{
  const {child,result,control}=fixture();child.emit('spawn');child.emit('spawn');
  assert.equal(result.starts,1);assert.equal(control.status,'running');assert.equal(control.n4,undefined);
});
test('late spawn cannot revive a stopped, failed, replaced or stopping child',()=>{
  for(const control of [
    {managed:true,status:'stopping'}, {managed:true,status:'error'},
    {managed:false,status:'stopped'}, {managed:true,status:'starting',stopRequested:true},
  ]) {const {child,result}=fixture(control);child.emit('spawn');assert.equal(result.starts,0);}
  const {child,result}=fixture();result.current=false;child.emit('spawn');assert.equal(result.starts,0);
});
test('UTF-8 stdout and stderr remain intact at every pipe split',()=>{
  const body=Buffer.from('{"message":"N4 按键与旋钮已连接 🎛️"}\n');
  for(let split=1;split<body.length;split++) {
    const {child,result}=fixture();
    for(const stream of [child.stdout,child.stderr]) {stream.write(body.subarray(0,split));stream.end(body.subarray(split));}
    assert.equal(result.stdout,body.toString('utf8'));assert.equal(result.stderr,body.toString('utf8'));
    assert.equal(JSON.parse(result.stdout).message,'N4 按键与旋钮已连接 🎛️');
  }
});
test('child environment changes are local and preserve other environment values',()=>{
  const env={PATH:'example',PYTHONUTF8:'0'};
  const opts=nativeSpawnOptions('/workspace',env);
  assert.equal(opts.windowsHide,true);assert.equal(opts.env.PYTHONIOENCODING,'utf-8');
  assert.equal(opts.env.PYTHONUTF8,'1');assert.equal(opts.env.PATH,'example');assert.equal(env.PYTHONUTF8,'0');
});
test('real hardware-free child emits running then intact logs',async()=>{
  const child=spawn(process.execPath,['-e',`process.stdout.write('旋钮已连接');process.stderr.write('仅测试进程');`],nativeSpawnOptions(process.cwd()));
  const control={managed:true,status:'starting'};let text='',errors='';
  observeNativeChild(child,{isCurrent:()=>true,getControl:()=>control,onRunning:()=>{control.status='running';},onStdout:v=>{text+=v;},onStderr:v=>{errors+=v;}});
  const [code]=await once(child,'close');assert.equal(code,0);assert.equal(control.status,'running');
  assert.ok(text.includes('旋钮已连接'));assert.equal(errors,'仅测试进程');
});
