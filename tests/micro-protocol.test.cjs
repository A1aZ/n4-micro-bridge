const {test} = require('node:test');
const assert = require('node:assert/strict');
const {encode, Decoder, inputEvent, MicroEndpoint, MicroFileStore} = require('../src/micro-protocol.cjs');
function decode(reports) { const d = new Decoder(); return reports.flatMap(r => d.feed(r)); }
test('64-byte reports and multi-report UTF-8 roundtrip', () => {
  const msg = {method: 'test', params: {text: '中文{}\\"'.repeat(100)}, id: 0};
  const reports = encode(msg);
  assert.ok(reports.length > 1);
  for (const r of reports) { assert.equal(r.length, 64); assert.ok(r[2] <= 61); }
  assert.deepEqual(decode(reports), [msg]);
});
test('accept reports without report ID', () => {
  assert.deepEqual(decode(encode({id: 1}).map(r => r.subarray(1))), [{id: 1}]);
});
test('accept JSON with no newline', () => {
  const r = encode({id: 3})[0]; r[2]--; r[3 + r[2]] = 0;
  assert.deepEqual(decode([r]), [{id: 3}]);
});
test('multiple objects in one report', () => {
  const r = Buffer.alloc(64), payload = Buffer.from('{"id":1}\n{"id":2}\n');
  r.set([6, 2, payload.length]); payload.copy(r, 3);
  assert.deepEqual(decode([r]), [{id: 1}, {id: 2}]);
});
test('malformed frames and oversized messages reset decoder', () => {
  const d = new Decoder(100);
  assert.throws(() => d.feed(Buffer.alloc(64)));
  assert.throws(() => encode({text: 'x'.repeat(200)}).forEach(r => d.feed(r)));
  assert.deepEqual(d.feed(encode({ok: true})[0]), [{ok: true}]);
});
test('invalid JSON resets decoder', () => {
  const r = Buffer.alloc(64); r.set([6, 2, 3, 123, 120, 125]);
  const d = new Decoder(); assert.throws(() => d.feed(r));
  assert.deepEqual(d.feed(encode({id: 1})[0]), [{id: 1}]);
});
test('key press/release and rotary mapping', () => {
  assert.deepEqual(inputEvent('AG02', 1).params, {k: 'AG02', act: 1, ag: 2});
  assert.equal(inputEvent('ACT06', 0).params.act, 0);
  assert.equal(inputEvent('ENC_CW', 2).params.act, 2);
  assert.throws(() => inputEvent('AG06', 1));
  assert.throws(() => inputEvent('ENC_CW', 1));
});
test('malformed UTF-8 and non-finite JSON cannot enter the wire decoder; next request recovers', () => {
  const payloads = [Buffer.from([123,34,120,34,58,34,0xc3,0x28,34,125]),
    ...['{"x":NaN}','{"x":Infinity}','{"x":1e400}'].map(value=>Buffer.from(value))];
  for(const payload of payloads) {
    const report=Buffer.alloc(64); report.set([6,2,payload.length]); payload.copy(report,3);
    const decoder=new Decoder(); assert.throws(()=>decoder.feed(report));
    assert.deepEqual(decoder.feed(encode({id:7})[0]),[{id:7}]);
  }
});
test('status response echoes zero id; notification has no reply', () => {
  const endpoint = new MicroEndpoint();
  assert.equal(endpoint.handle({method: 'device.status', id: 0}).result.battery, 100);
  assert.equal(endpoint.handle({method: 'sys.version', id: 0}).id, 0);
  assert.equal(endpoint.handle({method: 'host.focused_app'}), null);
});
test('slot updates preserve omitted fields and reject invalid updates atomically', () => {
  const endpoint = new MicroEndpoint();
  const update = params => endpoint.handle({method: 'v.oai.thstatus', params, id: 7});
  assert.deepEqual(update([{id: 2, c: 0xff0000, b: 1}]).result, {ok: true});
  update([{id: 2, e: 4}]); assert.equal(endpoint.slots[2].c, 0xff0000);
  assert.equal(update([{id: 2, c: 1}, {id: 8}]).error.code, -32602);
  assert.equal(endpoint.slots[2].c, 0xff0000);
  assert.equal(update([{id: 2, b: 2}]).error.code, -32602);
});
test('RGB config merges and rejects invalid input', () => {
  const e = new MicroEndpoint();
  e.handle({method: 'v.oai.rgbcfg', params: {keys: {c: 123, e: 'breath'}}, id: 1});
  e.handle({method: 'v.oai.rgbcfg', params: {keys: {b: 0.5}}, id: 2});
  assert.deepEqual(e.lighting.keys, {c: 123, e: 'breath', b: 0.5});
  assert.equal(e.handle({method: 'v.oai.rgbcfg', params: null, id: 3}).error.code, -32602);
});
test('compatibility RPCs and lights.preview accept the device-kit spelling', () => {
  const e = new MicroEndpoint();
  const preview = e.handle({method: 'lights.preview', params: {
    backlight: {color: 0xff0000, brightness: 0.5, effect: 'breath', speed: 0.4, magic: 0},
    underglow: {color: 0x0000ff, brightness: 1, effect: 'solid'},
  }, id: 4});
  assert.deepEqual(preview.result, {ok: true});
  assert.equal(e.lighting.keys.c, 0xff0000);
  assert.equal(e.lighting.keys.b, 0.5);
  assert.equal(e.lighting.keys.m, 0);
  assert.equal(e.lighting.ambient.e, 'solid');
  assert.deepEqual(e.handle({method: 'ui.active_screen', id: 5}).result, {screen_name: 'home'});
  assert.deepEqual(e.handle({method: 'appmgr.list_installed', id: 6}).result, []);
  assert.deepEqual(e.handle({method: 'sys.selftest', id: 7}).result, {ok: true});
});
test('file RPC exposes the built-in keymap and bounded binary chunks', () => {
  const e = new MicroEndpoint();
  const listing = e.handle({method: 'fs.list', params: {checksum: true}, id: 8});
  assert.equal(listing.error, undefined);
  assert.deepEqual(listing.result.map((item) => item.name), ['keymap.json']);
  assert.equal(typeof listing.result[0].size, 'string');
  const first = e.handle({method: 'fs.readbin', params: {
    file: '/keymap.json', offset: 0, len: 9999,
  }, id: 9});
  assert.equal(first.result.total_size, Number(listing.result[0].size));
  assert.ok(first.result.data.length <= 512); // 384 raw bytes, base64 encoded
  const keymap = e.handle({method: 'fs.read', params: {file: 'keymap.json'}, id: 10});
  assert.equal(keymap.result.version, 1);
  assert.equal(keymap.result.profiles[0].layers[0].layout.encoders[0][1], 'KV_OAI_ENC_CW');
});
test('file RPC supports streamed writes, whole JSON writes, delete fallback and errors', () => {
  const e = new MicroEndpoint();
  const b64 = Buffer.from('{"version":1,"smartActions":{}}').toString('base64');
  assert.deepEqual(e.handle({method: 'fs.writebin', params: {
    file: 'smart_actions.json', data: b64.slice(0, 8), completed: false,
  }, id: 11}).result, {data_written: 6});
  assert.deepEqual(e.handle({method: 'fs.writebin', params: {
    file: 'smart_actions.json', data: b64.slice(8), completed: true,
  }, id: 12}).result, {data_written: 25});
  assert.deepEqual(e.handle({method: 'fs.read', params: {file: 'smart_actions.json'}, id: 13}).result,
    {version: 1, smartActions: {}});
  assert.deepEqual(e.handle({method: 'fs.write', params: {
    file: 'smart_actions.json', data: {version: 2, smartActions: {SA_0: {type: 'URL_STEP'}}},
  }, id: 14}).result, {ok: true});
  assert.deepEqual(e.handle({method: 'fs.delete', params: {file: 'smart_actions.json'}, id: 15}).result, {ok: true});
  assert.equal(e.handle({method: 'fs.read', params: {file: 'smart_actions.json'}, id: 16}).error.code, -2);
  assert.equal(e.handle({method: 'fs.writebin', params: {file: 'other.bin', data: '', completed: true}, id: 17}).error.code, -3);
  assert.equal(e.handle({method: 'fs.readbin', params: {file: 'other.bin'}, id: 18}).error.code, -2);
  assert.deepEqual(e.handle({method: 'fs.txbegin', id: 19}).result, {tx: 1});
  assert.deepEqual(e.handle({method: 'fs.txcommit', id: 20}).result, {ok: true});
});
test('file RPC rejects malformed UTF-8 with the firmware file error', () => {
  // Invalid UTF-8 inside a JSON string would be silently replaced by
  // Buffer#toString('utf8') and incorrectly accepted by JSON.parse().
  const files = new MicroFileStore({
    'smart_actions.json': Buffer.from([0x7b, 0x22, 0x78, 0x22, 0x3a, 0x22,
      0xc3, 0x28, 0x22, 0x7d]),
  });
  const e = new MicroEndpoint({files});
  const reply = e.handle({method: 'fs.read', params: {file: 'smart_actions.json'}, id: 21});
  assert.equal(reply.error.code, -2);
  assert.equal(reply.error.message, 'File does not exist');
});
test('unknown method and full wire request/response loop', () => {
  const e = new MicroEndpoint();
  const [request] = decode(encode({method: 'nonexistent', id: 99}));
  const [response] = decode(encode(e.handle(request)));
  assert.equal(response.id, 99); assert.equal(response.error.code, -32601);
});
