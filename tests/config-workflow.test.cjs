'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../webui/config.html'), 'utf8');
function editor(options={}) {
  const nodes=new Map(), requests=[], clipboard=[], downloads=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:'',disabled:false,checked:false,dataset:{},addEventListener(){}});return nodes.get(id);};
  function query(selector) {
    const match=/\[([^=]+)="(\d+)"\]/.exec(selector);
    const markup=get(match[1].startsWith('data-button')?'buttons':'knobs').innerHTML;
    const element=markup.match(new RegExp('<(?:select|input)[^>]*'+match[1]+'="'+match[2]+'"[^>]*>(?:[\\s\\S]*?<\\/select>)?'))?.[0] || '';
    const value=element.match(/<option value="([^"]*)" selected/)?.[1] || '';
    return {value,checked:/ checked/.test(element)};
  }
  const context=vm.createContext({
    document:{getElementById:get,querySelector:query,querySelectorAll:()=>[...nodes.values()],addEventListener(){},createElement:()=>({click(){downloads.push(true);}}),body:{classList:{toggle(){}}}},
    window:{addEventListener(){}},confirm:()=>true,
    localStorage:{getItem:()=>options.draft||null,setItem(){if(options.storageFails)throw new Error('storage denied');}},
    fetch:async(url,init)=>{requests.push({url,init});if(options.offline)throw new Error('offline');return {ok:true,json:async()=>init ? {config:JSON.parse(init.body)} : JSON.parse(get('json').value)};},
    navigator:{clipboard:{writeText:async value=>clipboard.push(value)}},
    Blob,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},setTimeout:()=>0,
  });
  let script=html.slice(html.indexOf('<script>')+8,html.lastIndexOf('</script>'));
  script=script.replace('      load();',`render(); globalThis.api={save,load,loadBrowserDraft,render,mapTest,readForm,
    setDraft(v){config=normalize(v);render();markDirty();},getConfig:()=>clone(config),isDirty:()=>dirty};`);
  vm.runInContext(script,context);
  return {api:context.api,get,requests,clipboard,downloads};
}
test('copy, export and draft mapping tests do not save or contact server',async()=>{
  const e=editor();
  await e.get('copy').onclick(); e.get('export').onclick();
  e.get('test-code').value='A1';e.get('test-state').value='00';e.get('test').onclick();
  assert.equal(e.clipboard.length,1);assert.equal(e.downloads.length,1);
  assert.equal(e.requests.length,0);
  assert.match(e.get('test-output').textContent,/ENC_CW/);
});
test('explicit save alone applies configuration and storage failure does not pretend save failed',async()=>{
  const e=editor({storageFails:true});
  assert.equal(await e.api.save(),true);
  assert.equal(e.requests.length,1);assert.equal(e.requests[0].init.method,'PUT');
  assert.match(e.get('status').textContent,/已保存并应用，但浏览器/);
  assert.equal(e.api.isDirty(),false);
});
test('offline load remains an unapplied draft, and export still works',async()=>{
  const e=editor({offline:true});await e.api.load();
  assert.match(e.get('status').textContent,/本地服务不可用/);
  assert.equal(e.get('status').className,'status warn');assert.equal(e.api.isDirty(),true);
  e.get('export').onclick();assert.equal(e.downloads.length,1);assert.equal(e.requests.length,1);
});
test('browser backup loads locally without fetching the server',()=>{
  const sample=editor().api.getConfig();sample.buttons[0].label='Saved backup';
  const e=editor({draft:JSON.stringify(sample)});e.api.loadBrowserDraft();
  assert.equal(e.api.getConfig().buttons[0].label,'Saved backup');assert.equal(e.requests.length,0);
  assert.equal(e.api.isDirty(),true);
});
test('imported labels are escaped and targets use accessible Chinese names',()=>{
  const e=editor();const sample=e.api.getConfig();sample.buttons[0].label='<img src=x onerror=alert(1)>';
  e.api.setDraft(sample);
  assert.ok(!e.get('buttons').innerHTML.includes('<img'));
  assert.match(e.get('buttons').innerHTML,/&lt;img/);
  assert.match(e.get('buttons').innerHTML,/aria-label="按键 1 用途"/);
  assert.match(e.get('buttons').innerHTML,/任务槽 1/);
});
test('invalid JSON cannot silently save previous form and byte tests reject trailing garbage',async()=>{
  const e=editor();e.get('test-code').value='A1xyz';e.get('test-state').value='00';e.api.mapTest();
  assert.match(e.get('test-output').textContent,/error/);
  e.get('json').value='{invalid';e.get('json').onchange();
  assert.equal(await e.api.save(),false);assert.equal(e.requests.length,0);
  assert.match(e.get('status').textContent,/JSON 尚有错误/);
});
