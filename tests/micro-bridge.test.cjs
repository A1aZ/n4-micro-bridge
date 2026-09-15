const {test} = require('node:test');
const assert = require('node:assert/strict');
const {defaultConfig} = require('../src/micro-config.cjs');
const {MicroBridge} = require('../src/micro-bridge.cjs');

const report = (code, state = 1) => {
  const bytes = Buffer.alloc(512);
  bytes.set([0x41, 0x43, 0x4b, 0, 0, 0x4f, 0x4b]);
  bytes[9] = code;
  bytes[10] = state;
  return bytes;
};
const prefixedReport = (code, state = 1, reportId = 0) =>
  Buffer.concat([Buffer.from([reportId]), report(code, state)]);

function fakeClock() {
  const timers = [];
  return {
    timers,
    schedule(fn, delay) {
      const timer = {fn, delay, cancelled: false};
      timers.push(timer);
      return timer;
    },
    cancel(timer) { if (timer) timer.cancelled = true; },
    run(timer = timers.find((item) => !item.cancelled)) {
      if (!timer) return;
      timer.fn();
    },
  };
}

test('rotation is emitted immediately as a valid Micro wire event', () => {
  const clock = fakeClock();
  const bridge = new MicroBridge({schedule: clock.schedule, cancel: clock.cancel});
  assert.deepEqual(bridge.handleReport(report(0xa1)), [
    {method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2}},
  ]);
  assert.deepEqual(bridge.drain(), [
    {method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2}},
  ]);
  assert.equal(clock.timers.length, 0);
  assert.equal(bridge.snapshot().stats.emitted, 1);
});

test('513-byte hidapi reports preserve button and knob source codes', () => {
  const clock = fakeClock();
  const bridge = new MicroBridge({schedule: clock.schedule, cancel: clock.cancel});
  assert.deepEqual(bridge.handleReport(prefixedReport(0xa1, 0, 4)), [
    {method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2}},
  ]);
  assert.deepEqual(bridge.handleReport(prefixedReport(0x37, 1, 0)), [
    {method: 'v.oai.hid', params: {k: 'ENC', act: 1}},
  ]);
  assert.equal(clock.timers.length, 1);
  assert.deepEqual(bridge.handleReport(prefixedReport(0x06, 1, 0)), [
    {method: 'v.oai.hid', params: {k: 'AG00', act: 1, ag: 0}},
  ]);
});

test('one-shot knob press emits now and schedules a release', () => {
  const clock = fakeClock();
  const seen = [];
  const bridge = new MicroBridge({schedule: clock.schedule, cancel: clock.cancel, onEvent: (event) => seen.push(event)});
  assert.deepEqual(bridge.handleHardwareEvent(0x37, 1), [
    {method: 'v.oai.hid', params: {k: 'ENC', act: 1}},
  ]);
  assert.equal(clock.timers.length, 1);
  assert.equal(clock.timers[0].delay, 100);
  assert.deepEqual(bridge.drain(), [{method: 'v.oai.hid', params: {k: 'ENC', act: 1}}]);
  clock.run(clock.timers[0]);
  assert.deepEqual(bridge.drain(), [{method: 'v.oai.hid', params: {k: 'ENC', act: 0}}]);
  assert.deepEqual(seen.map((event) => event.params.act), [1, 0]);
  assert.equal(bridge.snapshot().pendingReleases, 0);
});

test('state-zero information-strip touch becomes an ACT06 click', () => {
  const clock = fakeClock();
  const config = defaultConfig();
  config.visual.stripMode = 'knobs';
  config.buttons[13].targetKey = 'ACT06';
  config.buttons[13].enabled = true;
  const bridge = new MicroBridge({config, schedule: clock.schedule, cancel: clock.cancel});
  assert.deepEqual(bridge.handleHardwareEvent(0x43, 0), [
    {method: 'v.oai.hid', params: {k: 'ACT06', act: 1}},
  ]);
  assert.equal(clock.timers.length, 1);
  assert.equal(clock.timers[0].delay, 100);
  clock.run(clock.timers[0]);
  assert.deepEqual(bridge.drain().map((event) => event.params), [
    {k: 'ACT06', act: 1},
    {k: 'ACT06', act: 0},
  ]);
});

test('a real release report cancels the synthetic timer and is not duplicated', () => {
  const clock = fakeClock();
  const bridge = new MicroBridge({schedule: clock.schedule, cancel: clock.cancel});
  bridge.handleHardwareEvent(0x37, 1);
  bridge.handleHardwareEvent(0x37, 0);
  assert.equal(clock.timers[0].cancelled, true);
  assert.deepEqual(bridge.drain().map((event) => event.params.act), [1, 0]);
  // Even if a scheduler races cancellation, the identity guard prevents a
  // stale callback from emitting another release.
  clock.timers[0].fn();
  assert.deepEqual(bridge.drain(), []);
});

test('different physical knobs keep independent pending releases', () => {
  const clock = fakeClock();
  const bridge = new MicroBridge({schedule: clock.schedule, cancel: clock.cancel});
  bridge.handleHardwareEvent(0x37, 1);
  bridge.handleHardwareEvent(0x35, 1);
  assert.equal(clock.timers.length, 2);
  assert.equal(clock.timers[0].cancelled, false);
  assert.equal(clock.timers[1].cancelled, false);
  clock.run(clock.timers[0]); clock.run(clock.timers[1]);
  assert.deepEqual(bridge.drain().map((event) => event.params.act), [1, 1, 0, 0]);
});

test('flush preserves AG metadata for a remapped encoder press', () => {
  const clock = fakeClock();
  const config = defaultConfig();
  config.knobs[0].press = 'AG03';
  const bridge = new MicroBridge({config, schedule: clock.schedule, cancel: clock.cancel});
  bridge.handleHardwareEvent(0x37, 1);
  bridge.drain();
  assert.deepEqual(bridge.flushPending(), [
    {method: 'v.oai.hid', params: {k: 'AG03', act: 0, ag: 3}},
  ]);
});

test('malformed reports are ignored and changing config cancels timers', () => {
  const clock = fakeClock();
  const bridge = new MicroBridge({schedule: clock.schedule, cancel: clock.cancel});
  assert.deepEqual(bridge.handleReport(Buffer.alloc(10)), []);
  bridge.handleReport(report(0x37, 1));
  assert.equal(bridge.snapshot().pendingReleases, 1);
  const config = defaultConfig(); config.release.delayMs = 50;
  bridge.setConfig(config);
  assert.equal(clock.timers[0].cancelled, true);
  assert.equal(bridge.snapshot().pendingReleases, 0);
});
