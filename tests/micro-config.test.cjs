const {test} = require('node:test');
const assert = require('node:assert/strict');
const {
  CONFIG_SCHEMA,
  defaultConfig,
  N4_BUTTON_PROFILES,
  INPUT_PROFILES,
  inferInputProfile,
  normalizeConfig,
  serializeConfig,
  parseConfig,
  mapN4HardwareEvent,
  mapN4Report,
  mapN4ReportToWire,
} = require('../src/micro-config.cjs');

const report = (code, state = 1) => {
  const bytes = Buffer.alloc(512);
  bytes.set([0x41, 0x43, 0x4b, 0, 0, 0x4f, 0x4b]);
  bytes[9] = code;
  bytes[10] = state;
  return bytes;
};

test('default config covers thirteen Micro buttons and four verified N4 knobs', () => {
  const config = defaultConfig();
  assert.equal(config.schema, CONFIG_SCHEMA);
  assert.equal(config.inputProfile, 'cpp');
  assert.equal(config.buttons.length, 14);
  assert.equal(config.buttons.filter((item) => item.enabled).length, 13);
  assert.deepEqual(config.buttons.map((item) => item.hardwareCode), [...N4_BUTTON_PROFILES.cpp]);
  assert.deepEqual(config.buttons.slice(0, 3).map((item) => item.targetKey), ['AG00', 'AG01', 'AG02']);
  assert.equal(config.buttons.at(-1).targetKey, null);
  assert.deepEqual(config.knobs.map((item) => item.hardware), [
    {ccw: 0xa0, cw: 0xa1, press: 0x37},
    {ccw: 0x50, cw: 0x51, press: 0x35},
    {ccw: 0x90, cw: 0x91, press: 0x33},
    {ccw: 0x70, cw: 0x71, press: 0x36},
  ]);
});

test('config JSON round-trips and omitted optional sections use defaults', () => {
  const parsed = parseConfig(serializeConfig(defaultConfig()));
  assert.deepEqual(parsed, defaultConfig());
  const partial = normalizeConfig({schema: CONFIG_SCHEMA, version: 1, release: {delayMs: 75}});
  assert.equal(partial.release.delayMs, 75);
  assert.equal(partial.release.synthesize, true);
  assert.equal(partial.buttons.length, 14);
  assert.deepEqual(normalizeConfig({schema: CONFIG_SCHEMA, version: 1, inputProfile: 'python'}).buttons.map((item) => item.hardwareCode), [...N4_BUTTON_PROFILES.python]);
  assert.deepEqual(INPUT_PROFILES, ['cpp', 'python', 'custom']);
  assert.equal(inferInputProfile(defaultConfig('python').buttons), 'python');
});

test('invalid configuration is rejected with bounded values and known keys', () => {
  assert.throws(() => parseConfig('{'), /Invalid configuration JSON/);
  assert.throws(() => normalizeConfig({version: 2}), /Unsupported config version/);
  assert.throws(() => normalizeConfig({release: {delayMs: 2001}}), /release.delayMs/);
  const badButton = defaultConfig();
  badButton.buttons[0].targetKey = 'AG99';
  assert.throws(() => normalizeConfig(badButton), /targetKey/);
  const unassignedButton = defaultConfig();
  unassignedButton.buttons[0].targetKey = null;
  assert.throws(() => normalizeConfig(unassignedButton), /required when enabled/);
  const duplicateButton = defaultConfig();
  duplicateButton.buttons[1].hardwareCode = duplicateButton.buttons[0].hardwareCode;
  assert.throws(() => normalizeConfig(duplicateButton), /Duplicate button/);
  const badKnob = defaultConfig();
  badKnob.knobs[0].rotateCw = 'AG00';
  assert.throws(() => normalizeConfig(badKnob), /rotateCw/);
});

test('verified N4 rotary reports map to direction-specific Micro events', () => {
  assert.deepEqual(mapN4ReportToWire(report(0xa1)), [
    {method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2}},
  ]);
  assert.deepEqual(mapN4ReportToWire(report(0x50)), [
    {method: 'v.oai.hid', params: {k: 'ENC_CC', act: 2}},
  ]);
});

test('513-byte hidapi reports map rotary and button events at the normalized offset', () => {
  const prefixedRotate = Buffer.concat([Buffer.from([0]), report(0xa1, 0)]);
  const prefixedButton = Buffer.concat([Buffer.from([0]), report(0x06, 1)]);
  assert.deepEqual(mapN4ReportToWire(prefixedRotate), [
    {method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2}},
  ]);
  assert.deepEqual(mapN4ReportToWire(prefixedButton), [
    {method: 'v.oai.hid', params: {k: 'AG00', act: 1, ag: 0}},
  ]);
});

test('hardware-code mapper is usable without constructing a full HID report', () => {
  assert.deepEqual(mapN4HardwareEvent(0xa0, 0), [
    {method: 'v.oai.hid', params: {k: 'ENC_CC', act: 2}},
  ]);
  assert.deepEqual(mapN4HardwareEvent(0x06, 1).map((event) => event.params.k), ['AG00']);
  assert.deepEqual(mapN4HardwareEvent(0x1ff, 1), []);
});

test('one-shot N4 knob press gets a configurable synthetic release', () => {
  const events = mapN4Report(report(0x37, 1));
  assert.deepEqual(events.map((event) => event.params.act), [1, 0]);
  assert.deepEqual(events[1].meta, {synthetic: true, delayMs: 100});
  assert.equal('meta' in mapN4ReportToWire(report(0x37, 1))[1], false);

  const config = defaultConfig();
  config.release.synthesize = false;
  assert.deepEqual(mapN4Report(config.knobs[0].hardware.press === 0x37 ? report(0x37, 1) : null, config), [
    {method: 'v.oai.hid', params: {k: 'ENC', act: 1}},
  ]);
});

test('N4 key reports honor editable Micro mappings and disabled bindings', () => {
  const config = defaultConfig();
  assert.deepEqual(mapN4ReportToWire(report(0x06, 1), config), [
    {method: 'v.oai.hid', params: {k: 'AG00', act: 1, ag: 0}},
  ]);
  assert.deepEqual(mapN4ReportToWire(report(0x06, 2), config), [
    {method: 'v.oai.hid', params: {k: 'AG00', act: 0, ag: 0}},
  ]);
  config.buttons[0].targetKey = 'ACT12';
  assert.equal(mapN4ReportToWire(report(0x06), config)[0].params.k, 'ACT12');
  config.buttons[0].enabled = false;
  assert.deepEqual(mapN4ReportToWire(report(0x06), config), []);
  assert.deepEqual(mapN4ReportToWire(report(0xee), config), []);
  assert.deepEqual(mapN4ReportToWire(Buffer.alloc(10), config), []);
});

test('encoder direction and press targets can be customized safely', () => {
  const config = defaultConfig();
  config.knobs[0].rotateCw = 'ENC_CC';
  config.knobs[0].press = 'AG03';
  assert.equal(mapN4ReportToWire(report(0xa1), config)[0].params.k, 'ENC_CC');
  const press = mapN4ReportToWire(report(0x37), config);
  assert.deepEqual(press.map((event) => event.params), [
    {k: 'AG03', act: 1, ag: 3},
    {k: 'AG03', act: 0, ag: 3},
  ]);
});
