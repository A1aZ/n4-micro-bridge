'use strict';

/**
 * Pure rendering/state helpers for the N4 side of the Codex Micro bridge.
 *
 * The Micro device kit reports lighting with two host -> device messages:
 *
 *   v.oai.thstatus  [{id, c, b, e, s, sk, sa}]
 *   v.oai.rgbcfg    {ambient: {c, b, e, s, m}, keys: {c, b, e, s, m}}
 *
 * This module does not open HID handles and does not rasterise or upload an
 * image.  It merges those messages with the same sticky-field semantics used
 * by openmicrokbd, maps the six agent slots/command backlight onto the N4's
 * fourteen logical image keys, and produces deterministic SVG/HTML previews.
 * A later transport adapter can rasterise the SVGs and call the Python SDK's
 * set_key_image()/set_touchscreen_image() methods.
 */

const {defaultConfig, normalizeConfig, N4_BUTTON_PROFILES} = require('./micro-config.cjs');
const {themeKeyGroup,infoStripGroup} = require('./n4-theme-svg.cjs');

const EFFECT_NAMES = Object.freeze([
  'off',
  'solid',
  'snake',
  'rainbow',
  'breath',
  'gradient',
  'shallowBreath',
]);

const EFFECT_IDS = Object.freeze(EFFECT_NAMES.reduce((out, name, id) => {
  out[name] = id;
  out[name.toLowerCase()] = id;
  return out;
}, {}));

const N4_RENDER = Object.freeze({
  schema: 'mirabox.n4.render',
  version: 1,
  screen: Object.freeze({
    width: 800,
    height: 480,
    format: 'JPEG',
    rotation: 180,
  }),
  mainKey: Object.freeze({
    width: 112,
    height: 112,
    format: 'JPEG',
    rotation: 180,
  }),
  secondaryKey: Object.freeze({
    width: 176,
    height: 112,
    format: 'JPEG',
    rotation: 180,
  }),
  keyCount: 14,
  mainKeyCount: 10,
  secondaryKeyCount: 4,
});

// The Python SDK's image API uses a different hardware numbering table from
// the C++/WebHID input table. Image uploads should normally use the logical
// key (1-14) through set_key_image(); this table is retained for adapters that
// call the lower-level transport directly.
const N4_IMAGE_HARDWARE_CODES = Object.freeze(
  N4_BUTTON_PROFILES?.python || [11, 12, 13, 14, 15, 6, 7, 8, 9, 10, 1, 2, 3, 4]
);

const LIGHT_DEFAULT = Object.freeze({
  c: 0,
  b: 0,
  e: 0,
  s: 0,
  sk: 0,
  sa: 0,
  m: 0,
  set: false,
});

const SVG_COLORS = Object.freeze({
  background: '#090d14',
  surface: '#111a27',
  surfaceAlt: '#172335',
  foreground: '#e8eef7',
  muted: '#8b9ab0',
  border: '#314157',
  disabled: '#293343',
});

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function finiteNumber(value, label) {
  const number = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(number)) throw new Error(`${label} must be a finite number`);
  return number;
}

const CLOCK_WEEKDAYS = Object.freeze(['周日', '周一', '周二', '周三', '周四', '周五', '周六']);

function normalizeClock(value = undefined) {
  if (isObject(value)
      && typeof value.time === 'string'
      && typeof value.date === 'string') {
    return {
      time: value.time,
      date: value.date,
      weekday: typeof value.weekday === 'string' ? value.weekday : '',
      minute: typeof value.minute === 'string' ? value.minute : `${value.date} ${value.time}`,
    };
  }
  const date = value instanceof Date ? value : new Date(value === undefined ? Date.now() : value);
  const safe = Number.isNaN(date.getTime()) ? new Date() : date;
  const pad = number => String(number).padStart(2, '0');
  const day = `${safe.getFullYear()}-${pad(safe.getMonth() + 1)}-${pad(safe.getDate())}`;
  const time = `${pad(safe.getHours())}:${pad(safe.getMinutes())}`;
  return {
    time,
    date: day,
    weekday: CLOCK_WEEKDAYS[safe.getDay()],
    minute: `${day} ${time}`,
  };
}

function normalizeUnit(value, label) {
  if (typeof value === 'boolean') return value ? 1 : 0;
  return clamp(finiteNumber(value, label), 0, 1);
}

function normalizeColor(value, label = 'color') {
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || value < 0) throw new Error(`${label} must be a non-negative color`);
    return Math.trunc(value) & 0x00ffffff;
  }
  if (typeof value !== 'string') throw new Error(`${label} must be a number or #RRGGBB string`);
  const text = value.trim();
  let hex = text;
  if (hex.startsWith('#')) hex = hex.slice(1);
  else if (/^0x/i.test(hex)) hex = hex.slice(2);
  if (/^[0-9a-f]{3}$/i.test(hex)) {
    hex = hex.split('').map((part) => part + part).join('');
  }
  // Alpha is intentionally ignored; Micro's wire colour is 24-bit RGB.
  if (/^[0-9a-f]{6,8}$/i.test(hex)) return parseInt(hex.slice(0, 6), 16);
  throw new Error(`${label} must be a #RRGGBB color`);
}

function normalizeEffect(value, label = 'effect') {
  if (typeof value === 'string') {
    const key = value.trim();
    const id = EFFECT_IDS[key] ?? EFFECT_IDS[key.toLowerCase()];
    if (id !== undefined) return id;
    // Keep unknown numeric effects accepted by the device kit.
    if (/^\d+$/.test(key)) return clamp(Number(key), 0, 255);
    throw new Error(`${label} is not a known effect`);
  }
  const number = finiteNumber(value, label);
  return clamp(Math.trunc(number), 0, 255);
}

function effectName(effect) {
  return EFFECT_NAMES[effect] || `effect-${effect}`;
}

function colorHex(color) {
  return `#${(color & 0x00ffffff).toString(16).padStart(6, '0').toUpperCase()}`;
}

function firstDefined(value, names) {
  for (const name of names) {
    if (Object.hasOwn(value, name) && value[name] !== undefined) return value[name];
  }
  return undefined;
}

/**
 * Merge one wire light object into a canonical light.  Missing fields remain
 * unchanged, matching the firmware's sticky update behaviour.
 */
function mergeLight(base = LIGHT_DEFAULT, patch = undefined, options = {}) {
  const markSet = options.markSet !== false;
  const out = {
    ...LIGHT_DEFAULT,
    ...(isObject(base) ? base : {}),
  };
  if (patch === undefined || patch === null) return out;
  if (!isObject(patch)) throw new Error('light must be an object');

  const color = firstDefined(patch, ['c', 'color']);
  if (color !== undefined) out.c = normalizeColor(color, 'light.c');
  const brightness = firstDefined(patch, ['b', 'brightness']);
  if (brightness !== undefined) out.b = normalizeUnit(brightness, 'light.b');
  const effect = firstDefined(patch, ['e', 'effect']);
  if (effect !== undefined) out.e = normalizeEffect(effect, 'light.e');
  const speed = firstDefined(patch, ['s', 'speed']);
  if (speed !== undefined) out.s = normalizeUnit(speed, 'light.s');
  for (const field of ['sk', 'sa', 'm']) {
    if (patch[field] !== undefined) out[field] = normalizeUnit(patch[field], `light.${field}`);
  }
  if (markSet) out.set = true;
  return out;
}

function createLightingState(seed = undefined) {
  if (seed !== undefined && !isObject(seed)) throw new Error('lighting state must be an object');
  const source = seed || {};
  if (source.agents !== undefined && !Array.isArray(source.agents)) {
    throw new Error('lighting.agents must be an array');
  }
  const agents = Array.from({length: 6}, (_, id) =>
    mergeLight(LIGHT_DEFAULT, source.agents?.[id], {markSet: false}));
  return {
    agents,
    keys: mergeLight(LIGHT_DEFAULT, source.keys, {markSet: false}),
    ambient: mergeLight(LIGHT_DEFAULT, source.ambient, {markSet: false}),
  };
}

function applyThstatus(state, params) {
  if (!Array.isArray(params)) throw new Error('v.oai.thstatus params must be an array');
  const next = createLightingState(state);
  for (const update of params) {
    if (!isObject(update) || !Number.isInteger(update.id) || update.id < 0 || update.id > 5) {
      throw new Error('v.oai.thstatus slot id must be an integer between 0 and 5');
    }
    const patch = {...update};
    delete patch.id;
    next.agents[update.id] = mergeLight(next.agents[update.id], patch);
  }
  return next;
}

function applyRgbcfg(state, params) {
  if (!isObject(params)) throw new Error('v.oai.rgbcfg params must be an object');
  const next = createLightingState(state);
  for (const side of ['ambient', 'keys']) {
    if (!Object.hasOwn(params, side) || params[side] === null || params[side] === undefined) continue;
    if (!isObject(params[side])) throw new Error(`v.oai.rgbcfg ${side} must be an object or null`);
    next[side] = mergeLight(next[side], params[side]);
  }
  return next;
}

/**
 * Input-app lighting preview shape. It uses long names (`backlight` and
 * `underglow`) while retaining the same sticky light fields as rgbcfg.
 */
function applyLightsPreview(state, params) {
  if (!isObject(params)) throw new Error('lights.preview params must be an object');
  const next = createLightingState(state);
  if (Object.hasOwn(params, 'backlight') && params.backlight !== null) {
    if (!isObject(params.backlight)) throw new Error('lights.preview backlight must be an object or null');
    next.keys = mergeLight(next.keys, params.backlight);
  }
  if (Object.hasOwn(params, 'underglow') && params.underglow !== null) {
    if (!isObject(params.underglow)) throw new Error('lights.preview underglow must be an object or null');
    next.ambient = mergeLight(next.ambient, params.underglow);
  }
  return next;
}

/** Apply one decoded Micro message; unrelated notifications are ignored. */
function applyMicroMessage(state, message) {
  if (Array.isArray(message)) return applyThstatus(state, message);
  if (!isObject(message)) throw new Error('Micro message must be an object');
  if (message.method === 'v.oai.thstatus') return applyThstatus(state, message.params);
  if (message.method === 'v.oai.rgbcfg') return applyRgbcfg(state, message.params);
  if (message.method === 'lights.preview') return applyLightsPreview(state, message.params);
  // A convenience shape makes WebUI replay easy without manufacturing RPC
  // wrappers: {thstatus: [...], rgbcfg: {...}}.
  let next = createLightingState(state);
  if (Object.hasOwn(message, 'thstatus')) next = applyThstatus(next, message.thstatus);
  if (Object.hasOwn(message, 'rgbcfg')) next = applyRgbcfg(next, message.rgbcfg);
  return next;
}

function reduceMicroLighting(messages, initial = undefined) {
  let state = createLightingState(initial);
  if (messages === undefined || messages === null) return state;
  if (Array.isArray(messages) && messages.every((item) =>
    isObject(item) && Object.hasOwn(item, 'id') && !Object.hasOwn(item, 'method'))) {
    return applyThstatus(state, messages);
  }
  const list = Array.isArray(messages) ? messages : [messages];
  for (const message of list) state = applyMicroMessage(state, message);
  return state;
}

function animationIntensity(light, phase = 0.5) {
  const p = clamp(finiteNumber(phase, 'phase'), 0, 1);
  const base = clamp(light.b, 0, 1);
  switch (light.e) {
    case 0: return 0;
    case 4: // breath: 0..1 sinusoid
      return base * (0.15 + 0.85 * (0.5 + 0.5 * Math.sin(p * Math.PI * 2 - Math.PI / 2)));
    case 6: // shallow breath: never below half brightness
      return base * (0.5 + 0.5 * (0.5 + 0.5 * Math.sin(p * Math.PI * 2 - Math.PI / 2)));
    default: return base;
  }
}

function describeLight(light, phase = 0.5) {
  const hasStoredSet = isObject(light) && Object.hasOwn(light, 'set');
  const canonical = mergeLight(LIGHT_DEFAULT, light, {markSet: !hasStoredSet});
  if (hasStoredSet) canonical.set = Boolean(light.set);
  const intensity = animationIntensity(canonical, phase);
  return {
    ...canonical,
    effectName: effectName(canonical.e),
    colorHex: colorHex(canonical.c),
    intensity,
    visible: intensity > 0.001,
    animated: [2, 3, 4, 5, 6].includes(canonical.e),
  };
}

function xmlEscape(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&apos;');
}

function shortLabel(label, max = 17) {
  const text = String(label ?? '');
  return text.length > max ? `${text.slice(0, Math.max(1, max - 1))}…` : text;
}

function opacity(value, fallback = 1) {
  const number = Number(value);
  return Number.isFinite(number) ? clamp(number, 0, 1) : fallback;
}

function effectPattern(light, id) {
  const colour = xmlEscape(light.colorHex);
  const safeId = xmlEscape(id);
  if (light.e === 3) {
    return `<linearGradient id="${safeId}" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="#ff4d6d"/><stop offset=".25" stop-color="#ffd166"/><stop offset=".5" stop-color="#06d6a0"/><stop offset=".75" stop-color="#4dabf7"/><stop offset="1" stop-color="#c77dff"/></linearGradient>`;
  }
  if (light.e === 5) {
    return `<linearGradient id="${safeId}" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="${colour}" stop-opacity=".25"/><stop offset=".5" stop-color="${colour}"/><stop offset="1" stop-color="#ffffff" stop-opacity=".35"/></linearGradient>`;
  }
  return `<linearGradient id="${safeId}" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="${colour}" stop-opacity=".94"/><stop offset="1" stop-color="${colour}" stop-opacity=".25"/></linearGradient>`;
}

function keyGroup(key, x, y, options = {}) {
  if(key.knobInfo)return infoStripGroup(key,x,y);
  if (key.theme && key.theme !== 'debug') return themeKeyGroup(key,x,y);
  const width = key.width;
  const height = key.height;
  const light = key.light || describeLight(LIGHT_DEFAULT);
  const id = options.gradientId || `n4-gradient-${String(key.index ?? 0).replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  const accent = xmlEscape(light.colorHex);
  const fill = light.e === 3 || light.e === 5 ? `url(#${id})` : accent;
  const active = opacity(light.intensity);
  const enabled = key.enabled !== false;
  const bg = enabled ? SVG_COLORS.surface : SVG_COLORS.background;
  const border = enabled ? accent : SVG_COLORS.disabled;
  const target = key.targetKey || '未映射';
  const label = shortLabel(key.label || target, width >= 176 ? 23 : 14);
  const textColour = enabled ? SVG_COLORS.foreground : SVG_COLORS.muted;
  const subtitle = enabled ? `${light.effectName} · ${Math.round(light.b * 100)}%` : 'disabled';
  const innerWidth = Math.max(16, width - 12);
  const titleY = Math.round(height * 0.55);
  const labelY = Math.round(height * 0.72);
  const subtitleY = Math.round(height * 0.88);
  const patternLines = light.e === 2
    ? `<path d="M${x + 8} ${y + height - 16}L${x + width - 8} ${y + 16}" stroke="#ffffff" stroke-opacity=".18" stroke-width="6"/><path d="M${x + 26} ${y + height - 16}L${x + width - 8} ${y + 34}" stroke="#ffffff" stroke-opacity=".12" stroke-width="4"/>`
    : '';
  const glow = active > 0
    ? `<rect x="${x + 3}" y="${y + 3}" width="${Math.max(1, width - 6)}" height="${Math.max(1, height - 6)}" rx="10" fill="${fill}" fill-opacity="${active.toFixed(3)}"/>`
    : '';
  const action = key.sdkKey ? `data-sdk-key="${xmlEscape(key.sdkKey)}"` : '';
  return `<g class="n4-key" data-key-index="${xmlEscape(key.index)}" data-target="${xmlEscape(target)}" ${action}>
  <title>${xmlEscape(`${target} ${key.label || ''}`)}</title>
  <rect x="${x}" y="${y}" width="${width}" height="${height}" rx="10" fill="${bg}" stroke="${border}" stroke-opacity="${enabled ? '.85' : '.45'}" stroke-width="2"/>
  ${glow}
  ${patternLines}
  <rect x="${x + 6}" y="${y + 6}" width="${innerWidth}" height="6" rx="3" fill="${accent}" fill-opacity="${enabled ? Math.max(.2, active) : .12}"/>
  <rect class="n4-label-backplate" x="${x + 8}" y="${y + 28}" width="${width - 16}" height="${height - 34}" rx="7" fill="${SVG_COLORS.surface}"/>
  <text x="${x + width / 2}" y="${y + titleY}" text-anchor="middle" fill="${textColour}" font-family="Segoe UI, sans-serif" font-size="${width >= 176 ? 26 : 22}" font-weight="600">${xmlEscape(target)}</text>
  <text x="${x + width / 2}" y="${y + labelY}" text-anchor="middle" fill="${textColour}" font-family="Segoe UI, sans-serif" font-size="${width >= 176 ? 15 : 12}">${xmlEscape(label)}</text>
  <text x="${x + width / 2}" y="${y + subtitleY}" text-anchor="middle" fill="${SVG_COLORS.muted}" font-family="Segoe UI, sans-serif" font-size="${width >= 176 ? 12 : 10}">${xmlEscape(subtitle)}</text>
</g>`;
}

/** Render one N4 key image at its SDK-native dimensions. */
function renderKeySvg(key, options = {}) {
  if (!isObject(key)) throw new Error('key model must be an object');
  const width = Number(key.width) || (Number(key.index) >= 11 ? N4_RENDER.secondaryKey.width : N4_RENDER.mainKey.width);
  const height = Number(key.height) || N4_RENDER.mainKey.height;
  const normalised = {
    knobInfo:key.knobInfo,
    theme: key.theme || 'debug',
    index: key.index ?? 1,
    label: key.label || key.targetKey || `N4 按键 ${key.index ?? ''}`,
    targetKey: key.targetKey || null,
    enabled: key.enabled !== false,
    width,
    height,
    light: describeLight(key.light || LIGHT_DEFAULT, options.phase ?? 0.5),
    sdkKey: key.sdkKey,
  };
  const id = `single-${String(normalised.index).replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${xmlEscape(normalised.targetKey || normalised.label)}">
  <defs>${effectPattern(normalised.light, id)}</defs>
  ${keyGroup(normalised, 0, 0, {...options, gradientId: id})}
</svg>`;
}

function buttonPosition(index) {
  if (index < N4_RENDER.mainKeyCount) {
    const gap = 8;
    const total = N4_RENDER.mainKeyCount / 2 * N4_RENDER.mainKey.width + 4 * gap;
    const originX = Math.round((N4_RENDER.screen.width - total) / 2);
    return {
      kind: 'main',
      x: originX + (index % 5) * (N4_RENDER.mainKey.width + gap),
      y: 56 + Math.floor(index / 5) * (N4_RENDER.mainKey.height + gap),
      width: N4_RENDER.mainKey.width,
      height: N4_RENDER.mainKey.height,
    };
  }
  const secondaryIndex = index - N4_RENDER.mainKeyCount;
  return {
    kind: 'secondary',
    x: 48 + secondaryIndex * N4_RENDER.secondaryKey.width,
    y: 294,
    width: N4_RENDER.secondaryKey.width,
    height: N4_RENDER.secondaryKey.height,
  };
}

function sourceLightForTarget(targetKey, lighting) {
  if (typeof targetKey !== 'string') return {source: 'none', light: LIGHT_DEFAULT, agent: null};
  const match = /^AG0([0-5])$/.exec(targetKey);
  if (match) {
    const agent = Number(match[1]);
    return {source: 'thstatus', light: lighting.agents[agent], agent};
  }
  if (/^ACT(?:0[6-9]|1[0-2])$/.test(targetKey)) {
    return {source: 'rgbcfg.keys', light: lighting.keys, agent: null};
  }
  return {source: 'none', light: LIGHT_DEFAULT, agent: null};
}

function knobView(config, index) {
  const knob = config.knobs[index];
  return {
    index,
    id: knob.id,
    label: knob.label,
    hardware: {...knob.hardware},
    rotateCcw: knob.rotateCcw,
    rotateCw: knob.rotateCw,
    press: knob.press,
    mode:knob.mode,
    reasoningConfigured:knob.reasoningConfigured,
    enabled: knob.enabled,
    // These positions are only for the preview legend; the physical knobs
    // sit outside the 800×480 image on the N4 chassis.
    preview: {x: 110 + index * 195, y: 443},
  };
}

function stripTouchAction(binding, index) {
  if (index !== 13 || !binding.enabled || binding.targetKey !== 'ACT06') return null;
  return {
    targetKey: 'ACT06',
    eyebrow: '快捷',
    label: '加速',
    icon: 'lightning',
    scope: 'strip',
    layout: 'split',
  };
}

/**
 * Build a serialisable N4 render model from Micro lighting state and config.
 */
function createN4RenderModel(options = {}) {
  if (!isObject(options)) throw new Error('render options must be an object');
  const config = normalizeConfig(options.config || defaultConfig());
  const lighting = createLightingState(options.lighting || options.state);
  const phase = options.phase === undefined ? 0.5 : clamp(finiteNumber(options.phase, 'phase'), 0, 1);
  const clock = normalizeClock(options.clock);
  const buttons = config.buttons.map((binding, index) => {
    const position = buttonPosition(index);
    const source = sourceLightForTarget(binding.targetKey, lighting);
    const light = describeLight(binding.enabled ? source.light : LIGHT_DEFAULT, phase);
    const button = {
      knobInfo:index>=10&&config.visual.stripMode==='knobs'?{index:index-10,...config.knobs[index-10],touchAction:stripTouchAction(binding,index),brightness:options.localActions?.brightnessApplied===true?options.localActions.brightness:null,clock:index===10?clock:null}:null,
      theme: config.visual.theme,
      index: index + 1,
      id: binding.id,
      label: binding.label,
      hardwareCode: binding.hardwareCode,
      // Input report code selected by micro-config versus the Python SDK's
      // lower-level image id. The public SDK call still takes logicalKey.
      imageHardwareCode: N4_IMAGE_HARDWARE_CODES[index],
      targetKey: binding.targetKey,
      enabled: binding.enabled,
      source: source.source,
      agent: source.agent,
      kind: position.kind,
      x: position.x,
      y: position.y,
      width: position.width,
      height: position.height,
      light,
      sdkKey: index + 1,
      sdkImageFormat: position.kind === 'secondary' ? {...N4_RENDER.secondaryKey} : {...N4_RENDER.mainKey},
    };
    button.svg = renderKeySvg(button, {phase});
    return button;
  });

  const model = {
    stripMode:config.visual.stripMode,
    stripBrightness:options.localActions?.brightnessApplied===true?options.localActions.brightness:null,
    theme: config.visual.theme,
    schema: N4_RENDER.schema,
    version: N4_RENDER.version,
    phase,
    clock,
    sdk: {
      screen: {...N4_RENDER.screen, method: 'set_touchscreen_image'},
      key: {...N4_RENDER.mainKey, method: 'set_key_image'},
      secondaryKey: {...N4_RENDER.secondaryKey, method: 'set_key_image'},
    },
    lighting: clone(lighting),
    ambient: describeLight(lighting.ambient, phase),
    keyBacklight: describeLight(lighting.keys, phase),
    buttons,
    knobs: config.knobs.map((_, index) => knobView(config, index)),
    screen: {
      ...N4_RENDER.screen,
      layout: {
        main: {columns: 5, rows: 2, gap: 8},
        secondary: {columns: 4, gap: 0},
      },
    },
  };
  model.screen.svg = renderScreenSvg(model, {phase});
  return model;
}

/** Render the complete 800×480 screen preview from a render model. */
function renderScreenSvg(model, options = {}) {
  if (!isObject(model) || !Array.isArray(model.buttons)) throw new Error('render model must contain buttons');
  const phase = options.phase ?? model.phase ?? 0.5;
  const ambient = describeLight(model.lighting?.ambient || model.ambient || LIGHT_DEFAULT, phase);
  const ambientId = 'n4-ambient-gradient';
  const bg = ambient.e === 3 || ambient.e === 5 ? `url(#${ambientId})` : ambient.colorHex;
  const bgOpacity = ambient.intensity > 0 ? Math.max(.12, ambient.intensity) : .95;
  const buttons = model.buttons.map((button) => keyGroup(button, button.x, button.y, {phase})).join('\n');
  const knobLegend = (model.knobs || []).map((knob) => {
    const x = knob.preview?.x ?? 110;
    const label = shortLabel(knob.label || `旋钮 ${knob.index + 1}`, 13);
    return `<g class="n4-knob" data-knob-index="${knob.index}">
      <circle cx="${x}" cy="${knob.preview?.y ?? 443}" r="16" fill="${SVG_COLORS.surfaceAlt}" stroke="${SVG_COLORS.border}"/>
      <path d="M${x} ${(knob.preview?.y ?? 443) - 12}V${(knob.preview?.y ?? 443) - 4}" stroke="${SVG_COLORS.foreground}" stroke-width="3" stroke-linecap="round"/>
      <text x="${x}" y="468" text-anchor="middle" fill="${SVG_COLORS.muted}" font-family="Segoe UI, sans-serif" font-size="12">${xmlEscape(label)}</text>
      <text x="${x}" y="430" text-anchor="middle" fill="${SVG_COLORS.foreground}" font-family="Segoe UI, sans-serif" font-size="10">${xmlEscape(knob.rotateCw || 'ENC_CW')}</text>
    </g>`;
  }).join('\n');
  const title = options.title || 'Codex Micro · Mirabox N4';
  const subtitle = `${model.buttons.filter((button) => button.enabled).length}/14 keys mapped · ambient ${ambient.effectName}`;
  const ambientDef = effectPattern(ambient, ambientId);
  const keyDefs = model.buttons.map((button) => effectPattern(
    button.light || LIGHT_DEFAULT,
    `n4-gradient-${String(button.index ?? 0).replace(/[^a-zA-Z0-9_-]/g, '-')}`
  )).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${N4_RENDER.screen.width}" height="${N4_RENDER.screen.height}" viewBox="0 0 ${N4_RENDER.screen.width} ${N4_RENDER.screen.height}" role="img" aria-labelledby="n4-screen-title n4-screen-desc">
  <title id="n4-screen-title">${xmlEscape(title)}</title>
  <desc id="n4-screen-desc">${xmlEscape(subtitle)}</desc>
  <defs>${ambientDef}${keyDefs}</defs>
  <rect width="800" height="480" rx="16" fill="${bg}" fill-opacity="${bgOpacity.toFixed(3)}"/>
  <rect x="14" y="14" width="772" height="32" rx="8" fill="${SVG_COLORS.surface}" fill-opacity=".84" stroke="${SVG_COLORS.border}"/>
  <text x="30" y="36" fill="${SVG_COLORS.foreground}" font-family="Segoe UI, sans-serif" font-size="18" font-weight="600">${xmlEscape(title)}</text>
  <text x="770" y="35" text-anchor="end" fill="${SVG_COLORS.muted}" font-family="Segoe UI, sans-serif" font-size="12">${xmlEscape(subtitle)}</text>
  ${buttons}
  <rect x="42" y="420" width="716" height="48" rx="12" fill="${SVG_COLORS.surface}" fill-opacity=".88" stroke="${SVG_COLORS.border}"/>
  <text x="58" y="438" fill="${SVG_COLORS.muted}" font-family="Segoe UI, sans-serif" font-size="11">N4 encoder mapping</text>
  ${knobLegend}
</svg>`;
}

/**
 * Return a standalone HTML preview.  It intentionally has no network or HID
 * dependencies, so it can be opened from disk or embedded in the WebUI.
 */
function renderPreviewHtml(input, options = {}) {
  const model = input?.schema === N4_RENDER.schema ? input : createN4RenderModel(input || {});
  const title = options.title || 'Mirabox N4 · Codex Micro preview';
  const keyCards = model.buttons.map((button) =>
    `<figure class="key-card" data-key="${xmlEscape(button.index)}"><figcaption>${xmlEscape(button.targetKey || button.label || '未映射')} · ${xmlEscape(button.kind)}</figcaption>${button.svg}</figure>`
  ).join('\n');
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${xmlEscape(title)}</title>
<style>
:root{color-scheme:dark;font-family:Segoe UI,system-ui,sans-serif;background:#090d14;color:#e8eef7}
body{margin:0;padding:24px;background:#090d14;color:#e8eef7}
main{max-width:1120px;margin:0 auto} h1{font-size:22px;margin:0 0 16px}
.screen{width:min(800px,100%);aspect-ratio:5/3;margin:0 auto 20px}.screen svg{display:block;width:100%;height:auto}
.keys{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:12px;align-items:start}
.key-card{margin:0;padding:8px;background:#111a27;border:1px solid #314157;border-radius:10px}
.key-card figcaption{height:28px;color:#8b9ab0;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.key-card svg{display:block;width:100%;height:auto}
@media(max-width:560px){body{padding:12px}.keys{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main><h1>${xmlEscape(title)}</h1><div class="screen">${model.screen.svg}</div><section class="keys" aria-label="N4 key previews">${keyCards}</section></main></body></html>`;
}

/**
 * Describe the eventual Python SDK calls without touching the filesystem.
 * `keyPaths` may be an array indexed by logical key (1..14) or a function.
 */
function buildSdkImagePlan(model, options = {}) {
  if (!isObject(model) || !Array.isArray(model.buttons)) throw new Error('render model must contain buttons');
  const keyPaths = options.keyPaths;
  const pathForKey = (button) => {
    if (typeof keyPaths === 'function') return keyPaths(button);
    if (Array.isArray(keyPaths)) return keyPaths[button.index - 1] ?? null;
    return null;
  };
  return {
    screen: {
      method: 'set_touchscreen_image',
      path: options.screenPath ?? null,
      width: N4_RENDER.screen.width,
      height: N4_RENDER.screen.height,
      format: N4_RENDER.screen.format,
      rotation: N4_RENDER.screen.rotation,
    },
    keys: model.buttons.map((button) => ({
      method: 'set_key_image',
      logicalKey: button.index,
      sdkKey: button.sdkKey,
      hardwareCode: button.hardwareCode,
      imageHardwareCode: button.imageHardwareCode,
      path: pathForKey(button),
      width: button.width,
      height: button.height,
      format: button.sdkImageFormat.format,
      rotation: button.sdkImageFormat.rotation,
      enabled: button.enabled,
    })),
  };
}

module.exports = {
  EFFECT_NAMES,
  EFFECT_IDS,
  LIGHT_DEFAULT,
  N4_RENDER,
  N4_IMAGE_HARDWARE_CODES,
  normalizeColor,
  normalizeEffect,
  normalizeClock,
  effectName,
  colorHex,
  mergeLight,
  createLightingState,
  applyThstatus,
  applyRgbcfg,
  applyLightsPreview,
  applyMicroMessage,
  reduceMicroLighting,
  animationIntensity,
  describeLight,
  buttonPosition,
  createN4RenderModel,
  renderKeySvg,
  renderScreenSvg,
  renderPreviewHtml,
  buildSdkImagePlan,
};
