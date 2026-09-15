'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const {MinuteVisualModelCache} = require('../src/visual-model-cache.cjs');

test('minute visual cache reuses models at the same minute and refreshes once at the next minute', () => {
  let minute = '2026-09-15 09:07';
  let builds = 0;
  const cache = new MinuteVisualModelCache({
    clock: () => ({minute, time: minute.slice(-5), date: minute.slice(0, 10)}),
    build: (phase, clock) => ({phase, clock, build: ++builds}),
  });

  const first = cache.get();
  assert.strictEqual(cache.get(), first);
  assert.equal(builds, 1);

  const animated = cache.get(.25);
  assert.notStrictEqual(animated, first);
  assert.strictEqual(cache.get(.25), animated);
  assert.strictEqual(cache.get(), first);
  assert.equal(builds, 2);

  minute = '2026-09-15 09:08';
  const next = cache.get();
  assert.notStrictEqual(next, first);
  assert.equal(next.clock.minute, minute);
  assert.strictEqual(cache.get(), next);
  assert.equal(builds, 3);
  assert.notStrictEqual(cache.get(.25), animated);
  assert.equal(builds, 4);
});

test('minute visual cache invalidates immediately on explicit state changes', () => {
  let builds = 0;
  const cache = new MinuteVisualModelCache({
    clock: () => ({minute: '2026-09-15 09:07', time: '09:07', date: '2026-09-15'}),
    build: (phase, clock) => ({phase, clock, build: ++builds}),
  });
  const initial = cache.get();
  const animated = cache.get(.25);
  const changed = cache.refresh(.5);
  assert.notStrictEqual(changed, initial);
  assert.equal(builds, 3);
  assert.strictEqual(cache.get(), changed);
  assert.notStrictEqual(cache.get(.25), animated);
});

test('minute visual cache stays bounded under high-cardinality phase polling', () => {
  let builds = 0;
  const cache = new MinuteVisualModelCache({
    maxEntries: 4,
    phaseSteps: 20,
    clock: () => ({minute: '2026-09-15 09:07', time: '09:07', date: '2026-09-15'}),
    build: (phase, clock) => ({phase, clock, build: ++builds}),
  });

  for (let index = 0; index < 1000; index += 1) {
    cache.get((index % 997) / 996);
    assert.ok(cache.size <= 4);
  }

  const first = cache.get(.501);
  const sameSampledPhase = cache.get(.499);
  assert.strictEqual(sameSampledPhase, first);
  assert.equal(first.phase, .5);
  assert.ok(cache.size <= 4);
});

test('default animation sampling has eight circular phases without rebuilding after a full cycle', () => {
  let builds = 0;
  const cache = new MinuteVisualModelCache({
    clock: () => ({minute: '2026-09-15 09:07', time: '09:07', date: '2026-09-15'}),
    build: phase => ({phase, build: ++builds}),
  });

  const zero = cache.get(0);
  for (let index = 1; index < 8; index += 1) cache.get(index / 8);
  assert.equal(cache.size, 8);
  assert.equal(builds, 8);
  assert.strictEqual(cache.get(1), zero);

  for (let index = 0; index <= 8; index += 1) cache.get(index / 8);
  assert.equal(cache.size, 8);
  assert.equal(builds, 8);
});
