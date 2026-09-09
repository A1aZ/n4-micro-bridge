const {test} = require('node:test');
const assert = require('node:assert/strict');
const {
  decodeN4Report,
  synthesizeClick,
  n4ReportOffset,
  n4HardwareEvent,
  N4_SWIPE,
} = require('../src/n4-input.cjs');
const {mapN4ReportToWire} = require('../src/micro-config.cjs');
const report = (code, state = 1) => { const b = Buffer.alloc(64); b[9] = code; b[10] = state; return b; };
const vendorReport = (code, state = 1) => {
  const b = Buffer.alloc(512);
  b.set([0x41, 0x43, 0x4b, 0, 0, 0x4f, 0x4b]);
  b[9] = code;
  b[10] = state;
  return b;
};
test('candidate N4/N4Pro rotary mapping', () => {
  assert.deepEqual(decodeN4Report(report(0xa1)), {method:'v.oai.hid', params:{k:'ENC_CW', act:2}});
  assert.deepEqual(decodeN4Report(report(0x50)), {method:'v.oai.hid', params:{k:'ENC_CC', act:2}});
  assert.deepEqual(decodeN4Report(report(0x37, 1)), {method:'v.oai.hid', params:{k:'ENC', act:1}});
  assert.deepEqual(decodeN4Report(report(0x37, 2)), {method:'v.oai.hid', params:{k:'ENC', act:0}});
  assert.deepEqual(decodeN4Report(report(0x37, 0)), {method:'v.oai.hid', params:{k:'ENC', act:0}});
});
test('513-byte hidapi reports strip the leading report ID for knobs and keys', () => {
  const prefixedRotate = Buffer.concat([Buffer.from([0]), vendorReport(0xa1, 0)]);
  const prefixedButton = Buffer.concat([Buffer.from([7]), vendorReport(0x06, 1)]);
  assert.equal(prefixedRotate.length, 513);
  assert.equal(n4ReportOffset(prefixedRotate), 1);
  assert.deepEqual(n4HardwareEvent(prefixedRotate), {hardwareCode: 0xa1, state: 0, offset: 1});
  assert.deepEqual(decodeN4Report(prefixedRotate), {
    method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2},
  });
  // n4-input intentionally leaves ordinary keys to the editable config
  // mapper; the shared extractor still proves the key coordinates are right.
  assert.deepEqual(n4HardwareEvent(prefixedButton), {hardwareCode: 0x06, state: 1, offset: 1});
  // Existing 64-byte/raw WebHID behavior remains offset 0.
  assert.equal(n4ReportOffset(report(0xa1, 0)), 0);
  assert.deepEqual(decodeN4Report(report(0xa1, 0)), {
    method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2},
  });
});
test('unknown and short reports are ignored', () => {
  assert.equal(decodeN4Report(Buffer.alloc(10)), null);
  assert.equal(decodeN4Report(report(0xee)), null);
});
test('one-shot N4 press can synthesize a release', () => {
  const press = decodeN4Report(report(0x37, 1));
  assert.deepEqual(synthesizeClick(press, 100).map(e => e.params.act), [1, 0]);
  assert.equal(synthesizeClick(decodeN4Report(report(0xa1))).length, 0);
});
test('secondary-screen swipes are decoded for diagnostics but never emitted as Micro keys', () => {
  assert.deepEqual([...N4_SWIPE], [[0x38, 'left'], [0x39, 'right']]);
  assert.deepEqual(decodeN4Report(report(0x38, 0)), {
    kind: 'swipe', hardwareCode: 0x38, state: 0, direction: 'left',
    microKey: null, microSupported: false,
  });
  assert.deepEqual(decodeN4Report(report(0x39, 0)), {
    kind: 'swipe', hardwareCode: 0x39, state: 0, direction: 'right',
    microKey: null, microSupported: false,
  });
  assert.deepEqual(mapN4ReportToWire(report(0x38, 0)), []);
  assert.deepEqual(mapN4ReportToWire(report(0x39, 0)), []);
});
