'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../webui/real-n4.html'), 'utf8');

// Evaluate the real page's functions without starting USB, HTTP or timers.
function page() {
  class Element {
    constructor() { this.children = []; this.textContent = ''; this.dataset = {}; }
    append(...items) { this.children.push(...items); items.forEach(item => { if (typeof item === 'object') item.parent = this; }); }
    prepend(item) { item.parent = this; this.children.unshift(item); }
    replaceChildren(...items) { this.children = []; this.append(...items); }
    get lastElementChild() { return this.children.at(-1); }
    remove() { this.parent.children = this.parent.children.filter(item => item !== this); }
  }
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const context = vm.createContext({document: {getElementById:get, createElement:()=>new Element(), createTextNode:text=>({textContent:text})}, window:{location:{origin:'http://localhost:1234'}}, console});
  const script = html.slice(html.indexOf('<script>') + 8, html.indexOf("      $('connect').onclick=connect;"));
  vm.runInContext(script + `
    globalThis.guideTest = {updateGuideConnection, guideInput, renderCompat, renderBackendStatus,
      state(v) { guideBackendReady = v.ready ?? true; guideNative = v.native || {}; guideControl = v.control || {}; guideConflicts = v.conflicts || []; dev = v.device || null; }};
  })();`, context);
  return {api:context.guideTest, get};
}

test('friendly guide keeps advanced controls collapsed and preserves unique IDs', () => {
  assert.match(html, /<details class="advanced-shell" id="advanced-tools">/);
  const ids = [...html.matchAll(/\bid="([\w-]+)"/g)].map(m=>m[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ['guide-connect','guide-input','guide-screen','guide-start','guide-check']) assert.ok(ids.includes(id));
});
test('never-started stale state is not reported as a lost connection', () => {
  const {api,get} = page();
  api.state({native:{status:'not-running',stale:true}}); api.updateGuideConnection();
  assert.match(get('guide-connection').textContent, /尚未连接/);
  assert.equal(get('guide-start').disabled, false);
  assert.equal(get('guide-stop').disabled, true);
  api.state({conflicts:[{kind:'process'}],native:{status:'not-running',stale:true}}); api.updateGuideConnection();
  assert.match(get('guide-connection').textContent, /其他程序/);
});
test('old service blocks beginner actions and explains that page reload is insufficient', () => {
  const {api,get} = page(); api.renderBackendStatus({});
  assert.equal(get('guide-start').disabled, true);
  assert.equal(get('guide-check').disabled, true);
  assert.match(get('guide-service').textContent, /刷新网页本身无法升级后台/);
});
test('device readiness, lost heartbeat and visual upload evidence stay distinct', () => {
  const {api,get} = page();
  api.state({native:{status:'running',n4:{ready:true,opened:true},visual:{enabled:true,uploads:3}},control:{managed:true,status:'running'}}); api.updateGuideConnection();
  assert.match(get('guide-connection').textContent,/设备已连接/);
  assert.match(get('guide-screen-status').textContent,/目视确认/);
  assert.equal(get('guide-start').disabled,true);
  assert.equal(get('guide-stop').disabled,false);
  api.state({native:{status:'running',stale:true,n4:{ready:true,opened:true}}}); api.updateGuideConnection();
  assert.match(get('guide-connection').textContent,/失去响应/);
  assert.match(get('guide-screen-status').textContent,/仅软件预览/);
});
test('friendly feed distinguishes simulated input and replay, decodes prefixed reports and is bounded', () => {
  const {api,get} = page();
  const events = [{method:'v.oai.hid'}];
  api.guideInput('hardware',{hardwareCode:0xa1,state:0,events});
  assert.match(get('guide-input-status').textContent,/软件模拟：旋钮 1 · 顺时针/);
  assert.match(get('guide-input-status').textContent,/尚未确认 Codex 接收/);
  const bytes = Buffer.alloc(513); bytes.set([0x41,0x43,0x4b,0,0,0x4f,0x4b],1); bytes[10]=0xa0;
  api.guideInput('report',{hex:bytes.toString('hex'),events});
  assert.match(get('guide-input-status').textContent,/报告输入（设备或回放）：旋钮 1 · 逆时针/);
  for(let i=0;i<20;i++) api.guideInput('hardware',{hardwareCode:0x37,state:1,events});
  assert.equal(get('guide-feed').children.length,12);
  api.guideInput('hardware',{hardwareCode:0x43,state:1,events:[]});
  assert.match(get('guide-input-status').textContent,/未映射/);
});
test('software handshake success is not advertised as a hardware or Codex connection', () => {
  const {api,get} = page();
  api.renderCompat({ok:true,mode:'compatibility-handshake',checks:{replyCount:5,keymapListed:true,keymapReadAsObject:true}});
  assert.match(get('guide-check-status').textContent,/软件自检通过/);
  assert.match(get('guide-check-status').textContent,/不代表实体 N4 或 Codex 已连接/);
});
