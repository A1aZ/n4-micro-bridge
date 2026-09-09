'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const {defaultConfig} = require('../src/micro-config.cjs');
const {
  EFFECT_NAMES,
  N4_RENDER,
  N4_IMAGE_HARDWARE_CODES,
  normalizeColor,
  normalizeEffect,
  effectName,
  createLightingState,
  applyThstatus,
  applyRgbcfg,
  applyLightsPreview,
  reduceMicroLighting,
  animationIntensity,
  createN4RenderModel,
  renderKeySvg,
  renderScreenSvg,
  renderPreviewHtml,
  buildSdkImagePlan,
} = require('../src/n4-visuals.cjs');

test('N4 visual constants mirror the Python SDK image formats', () => {
  assert.deepEqual(N4_RENDER.screen, {width: 800, height: 480, format: 'JPEG', rotation: 180});
  assert.deepEqual(N4_RENDER.mainKey, {width: 112, height: 112, format: 'JPEG', rotation: 180});
  assert.deepEqual(N4_RENDER.secondaryKey, {width: 176, height: 112, format: 'JPEG', rotation: 180});
  assert.equal(N4_RENDER.keyCount, 14);
  assert.deepEqual(N4_IMAGE_HARDWARE_CODES, [11, 12, 13, 14, 15, 6, 7, 8, 9, 10, 1, 2, 3, 4]);
});

test('wire color/effect values normalise for previews', () => {
  assert.equal(normalizeColor('#abc'), 0xaabbcc);
  assert.equal(normalizeColor('0x123456'), 0x123456);
  assert.equal(normalizeColor(0x12345678), 0x345678);
  assert.equal(normalizeEffect('shallowBreath'), 6);
  assert.equal(normalizeEffect('4'), 4);
  assert.equal(effectName(6), 'shallowBreath');
  assert.equal(effectName(99), 'effect-99');
  assert.equal(EFFECT_NAMES.length, 7);
  assert.throws(() => normalizeColor('not-a-color'), /#RRGGBB/);
  assert.throws(() => normalizeEffect('sparkle'), /known effect/);
});

test('thstatus and rgbcfg updates are sticky and independent', () => {
  let state = createLightingState();
  state = applyThstatus(state, [{id: 2, c: 0xff0088, b: 1, e: 'breath', s: .4, sk: 1}]);
  assert.deepEqual(state.agents[2], {
    c: 0xff0088, b: 1, e: 4, s: .4, sk: 1, sa: 0, m: 0, set: true,
  });
  state = applyThstatus(state, [{id: 2, e: 1}]);
  assert.equal(state.agents[2].c, 0xff0088);
  assert.equal(state.agents[2].e, 1);
  assert.equal(state.agents[0].set, false, 'untouched slots stay unset');
  state = applyRgbcfg(state, {
    keys: {c: '#00ff00', b: .5, e: 'solid'},
    ambient: {c: 0x101010, b: .2, e: 'gradient'},
  });
  state = applyRgbcfg(state, {keys: {s: .8}, ambient: null});
  assert.deepEqual(state.keys, {c: 0x00ff00, b: .5, e: 1, s: .8, sk: 0, sa: 0, m: 0, set: true});
  assert.equal(state.ambient.e, 5);
  assert.equal(state.ambient.b, .2);
});

test('lights.preview maps long device-kit fields to keys and ambient', () => {
  let state = createLightingState();
  state = applyLightsPreview(state, {
    backlight: {color: '#ff0000', brightness: .5, effect: 'breath', speed: .4, magic: 0},
    underglow: {color: 0x0000ff, brightness: 1, effect: 'solid'},
  });
  assert.deepEqual(state.keys, {c: 0xff0000, b: .5, e: 4, s: .4, sk: 0, sa: 0, m: 0, set: true});
  assert.equal(state.ambient.c, 0x0000ff);
  assert.equal(state.ambient.e, 1);
  assert.throws(() => applyLightsPreview(state, {backlight: {brightness: 'bad'}}), /finite number/);
});

test('invalid status update is atomic', () => {
  const before = createLightingState();
  assert.throws(() => applyThstatus(before, [{id: 0, c: 0xff0000}, {id: 8, c: 1}]), /between 0 and 5/);
  assert.deepEqual(before, createLightingState());
  assert.throws(() => applyRgbcfg(before, {keys: {b: 'bad'}}), /finite number/);
});

test('reduceMicroLighting accepts decoded RPC messages and convenience shape', () => {
  const state = reduceMicroLighting([
    {method: 'v.oai.thstatus', params: [{id: 0, c: 0x112233, b: 1, e: 1}]},
    {method: 'v.oai.rgbcfg', params: {keys: {c: 0xabcdef, b: .25, e: 1}}},
    {thstatus: [{id: 1, c: 0x445566, b: .5}], rgbcfg: {ambient: {c: 0x010203, b: .1}}},
    {method: 'device.status', params: {}},
  ]);
  assert.equal(state.agents[0].c, 0x112233);
  assert.equal(state.agents[1].c, 0x445566);
  assert.equal(state.keys.c, 0xabcdef);
  assert.equal(state.ambient.c, 0x010203);
  assert.equal(reduceMicroLighting([{id: 4, c: 0xaabbcc, b: 1}]).agents[4].c, 0xaabbcc);
});

test('animated light sampling exposes a deterministic preview phase', () => {
  const off = {c: 0xff0000, b: 1, e: 0};
  const breath = {c: 0xff0000, b: 1, e: 4};
  assert.equal(animationIntensity(off, .5), 0);
  assert.ok(animationIntensity(breath, 0) < animationIntensity(breath, .5));
  assert.equal(animationIntensity({c: 0, b: 2, e: 1}, .5), 1);
});

test('render model maps six agent slots and command backlight to all 14 N4 keys', () => {
  const config = defaultConfig();
  const lighting = reduceMicroLighting({
    thstatus: [
      {id: 0, c: 0xff0000, b: 1, e: 'solid'},
      {id: 5, c: 0x0000ff, b: .5, e: 'solid'},
    ],
    rgbcfg: {keys: {c: 0x00ff00, b: .25, e: 'solid'}, ambient: {c: 0x222222, b: .2, e: 'solid'}},
  });
  const model = createN4RenderModel({config, lighting});
  assert.equal(model.buttons.length, 14);
  assert.deepEqual(model.buttons.slice(0, 5).map((button) => [button.hardwareCode, button.imageHardwareCode]), [
    [6, 11], [7, 12], [8, 13], [9, 14], [10, 15],
  ]);
  assert.equal(model.buttons[0].source, 'thstatus');
  assert.equal(model.buttons[0].light.colorHex, '#FF0000');
  assert.equal(model.buttons[5].light.colorHex, '#0000FF');
  assert.equal(model.buttons[6].source, 'rgbcfg.keys');
  assert.equal(model.buttons[6].light.colorHex, '#00FF00');
  assert.equal(model.buttons[13].enabled, false);
  assert.equal(model.buttons[13].light.visible, false);
  assert.equal(model.buttons[13].light.set, false);
  assert.deepEqual(model.buttons.slice(0, 10).map((button) => [button.width, button.height]), Array(10).fill([112, 112]));
  assert.deepEqual(model.buttons.slice(10).map((button) => [button.width, button.height]), Array(4).fill([176, 112]));
  assert.equal(model.screen.width, 800);
  assert.equal(model.screen.height, 480);
  assert.match(model.screen.svg, /<svg[^>]+width="800"[^>]+height="480"/);
  assert.equal((model.screen.svg.match(/class="n4-key"/g) || []).length, 14);
});

test('custom target mappings follow config instead of slot position', () => {
  const config = defaultConfig();
  config.buttons[0].targetKey = 'ACT12';
  config.buttons[12].targetKey = 'AG03';
  const model = createN4RenderModel({
    config,
    lighting: reduceMicroLighting({
      thstatus: [{id: 3, c: 0x123456, b: 1, e: 1}],
      rgbcfg: {keys: {c: 0x654321, b: 1, e: 1}},
    }),
  });
  assert.equal(model.buttons[0].source, 'rgbcfg.keys');
  assert.equal(model.buttons[0].light.colorHex, '#654321');
  assert.equal(model.buttons[12].source, 'thstatus');
  assert.equal(model.buttons[12].agent, 3);
  assert.equal(model.buttons[12].light.colorHex, '#123456');
});

test('single-key SVG and standalone HTML previews are self-contained', () => {
  const keySvg = renderKeySvg({
    index: 11,
    label: '发送',
    targetKey: 'ACT12',
    enabled: true,
    width: 176,
    height: 112,
    light: {c: 0xffaa00, b: .75, e: 'solid'},
  });
  assert.match(keySvg, /^<svg /);
  assert.match(keySvg, /width="176" height="112"/);
  assert.match(keySvg, /#FFAA00/);
  assert.match(keySvg, /class="n4-label-backplate"[^>]+fill="#111a27"/);
  assert.ok(keySvg.indexOf('n4-label-backplate') < keySvg.indexOf('<text '));
  assert.match(keySvg, /font-size="26"/);
  const model = createN4RenderModel();
  const html = renderPreviewHtml(model);
  assert.match(html, /^<!doctype html>/i);
  assert.match(html, /Codex Micro/);
  assert.match(html, /N4 key previews/);
  assert.match(html, /<svg /);
});

test('SDK image plan distinguishes input codes from logical image calls', () => {
  const model = createN4RenderModel();
  const plan = buildSdkImagePlan(model, {
    screenPath: 'out/screen.jpg',
    keyPaths: Array.from({length: 14}, (_, i) => `out/key-${i + 1}.jpg`),
  });
  assert.deepEqual(plan.screen, {
    method: 'set_touchscreen_image', path: 'out/screen.jpg',
    width: 800, height: 480, format: 'JPEG', rotation: 180,
  });
  assert.equal(plan.keys[0].logicalKey, 1);
  assert.equal(plan.keys[0].hardwareCode, 6);
  assert.equal(plan.keys[0].imageHardwareCode, 11);
  assert.equal(plan.keys[0].path, 'out/key-1.jpg');
  assert.equal(plan.keys[10].width, 176);
  assert.equal(plan.keys[10].height, 112);
});

test('renderScreenSvg can render a serialised model again', () => {
  const model = createN4RenderModel();
  const svg = renderScreenSvg(JSON.parse(JSON.stringify(model)));
  assert.equal(svg, model.screen.svg);
});
