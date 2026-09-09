const {test}=require('node:test');
const assert=require('node:assert/strict');
const {imageName,managedProcessIds}=require('../src/native-process-info.cjs');
test('SDK argument paths are not StreamDock executables',()=>{
  assert.equal(imageName({command:'"C:\\Python\\python.exe" n4-webui-bridge.py --sdk-path "D:\\StreamDock-Device-SDK\\src"'}),'python.exe');
  assert.equal(imageName({name:'python.exe',command:'StreamDock'}),'python.exe');
  assert.equal(imageName({command:'"C:\\Program Files\\StreamDock\\StreamDock.exe"'}),'streamdock.exe');
});
test('venv launcher descendants belong to the managed native process',()=>{
  const ids=managedProcessIds([{pid:3,parentPid:2},{pid:2,parentPid:1},{pid:4,parentPid:99}],1);
  assert.ok(ids.has(1)&&ids.has(2)&&ids.has(3));assert.equal(ids.has(4),false);
  assert.equal(managedProcessIds([],null).size,0);
});
