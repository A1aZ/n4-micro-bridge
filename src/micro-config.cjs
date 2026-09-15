'use strict';

/**
 * Configuration and routing helpers for the N4 -> Codex Micro bridge.
 *
 * This module deliberately contains no HID I/O.  It turns the bytes already
 * decoded by the StreamDock N4 transport into the JSON events understood by
 * the Micro wire protocol, and provides a versioned configuration document
 * that can be edited by the WebUI or stored by a host process.
 */

const {inputEvent} = require('./micro-protocol.cjs');
const {n4HardwareEvent} = require('./n4-input.cjs');
const themeCatalog = require('../assets/themes/catalog.json');
const {KNOB_MODES,independentRotation}=require('./n4-knob-actions.cjs');

const CONFIG_SCHEMA = 'mirabox.codex.micro';
const CONFIG_VERSION = 1;
const N4_DEVICE = Object.freeze({
  vendorId: 0x6602,
  productId: 0x1001,
  usagePage: 0xffa0,
  usage: 1,
  reportId: 0,
});

// The mappings below are taken from the StreamDock C++ SDK's N4
// `dispatchEvent` table and verified against the N4 reports captured in the
// WebHID page.  There are two different numbering schemes in the upstream
// SDK: the Python image API uses logical image ids (11..15, 6..10, 1..4),
// while input reports use the C++ hardware codes (0x01..0x0A and 0x40..0x43).
// Keep both profiles explicit so an older firmware/SDK can be selected rather
// than silently interpreting image ids as input codes.
const KNOB_LAYOUT = Object.freeze([
  Object.freeze({index: 0, name: '旋钮 1', ccw: 0xa0, cw: 0xa1, press: 0x37}),
  Object.freeze({index: 1, name: '旋钮 2', ccw: 0x50, cw: 0x51, press: 0x35}),
  Object.freeze({index: 2, name: '旋钮 3', ccw: 0x90, cw: 0x91, press: 0x33}),
  Object.freeze({index: 3, name: '旋钮 4', ccw: 0x70, cw: 0x71, press: 0x36}),
]);

// Button order is deliberately main-screen first, then the four secondary
// screen buttons.  Micro has thirteen key slots, so the final N4 button is
// intentionally unassigned by default and remains editable.
const N4_BUTTON_PROFILES = Object.freeze({
  cpp: Object.freeze([
    0x06, 0x07, 0x08, 0x09, 0x0a,
    0x01, 0x02, 0x03, 0x04, 0x05,
    0x40, 0x41, 0x42, 0x43,
  ]),
  // Python SDK image-key numbering (kept for firmware/tools that emit it).
  python: Object.freeze([11, 12, 13, 14, 15, 6, 7, 8, 9, 10, 1, 2, 3, 4]),
});
const INPUT_PROFILES = Object.freeze(['cpp', 'python', 'custom']);
const DEFAULT_INPUT_PROFILE = 'cpp';
const N4_BUTTON_CODES = N4_BUTTON_PROFILES[DEFAULT_INPUT_PROFILE];
const MICRO_BUTTON_KEYS = Object.freeze([
  'AG00', 'AG01', 'AG02', 'AG03', 'AG04', 'AG05',
  'ACT06', 'ACT07', 'ACT08', 'ACT09', 'ACT10', 'ACT11', 'ACT12',
]);
const MICRO_ENCODER_KEYS = Object.freeze(['ENC_CW', 'ENC_CC', 'ENC']);
const MICRO_KEYS = Object.freeze([...MICRO_BUTTON_KEYS, ...MICRO_ENCODER_KEYS]);

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function profileCodes(profile) {
  if (profile === 'custom') return N4_BUTTON_PROFILES[DEFAULT_INPUT_PROFILE];
  if (!Object.hasOwn(N4_BUTTON_PROFILES, profile)) {
    throw new Error(`Unsupported input profile: ${String(profile)}`);
  }
  return N4_BUTTON_PROFILES[profile];
}

function inferInputProfile(buttons) {
  if (!Array.isArray(buttons) || buttons.length !== 14) return DEFAULT_INPUT_PROFILE;
  const codes = buttons.map((item) => item && item.hardwareCode);
  for (const profile of Object.keys(N4_BUTTON_PROFILES)) {
    if (N4_BUTTON_PROFILES[profile].every((code, index) => codes[index] === code)) return profile;
  }
  return 'custom';
}

function defaultConfig(options = {}) {
  const requested = typeof options === 'string' ? options : options?.inputProfile;
  const inputProfile = requested || DEFAULT_INPUT_PROFILE;
  const buttonCodes = profileCodes(inputProfile);
  return {
    schema: CONFIG_SCHEMA,
    version: CONFIG_VERSION,
    inputProfile,
    device: {...N4_DEVICE},
    release: {synthesize: true, delayMs: 100},
    visual: {theme: themeCatalog.default, stripMode: 'buttons'},
    buttons: buttonCodes.map((hardwareCode, index) => ({
      id: `button-${index + 1}`,
      label: `N4 按键 ${index + 1}`,
      hardwareCode,
      // The 14th physical key has no matching Micro key by default.
      targetKey: MICRO_BUTTON_KEYS[index] || null,
      enabled: Boolean(MICRO_BUTTON_KEYS[index]),
    })),
    knobs: KNOB_LAYOUT.map((knob) => ({
      id: `knob-${knob.index + 1}`,
      label: knob.name,
      mode: 'micro',
      reasoningConfigured: false,
      hardware: {ccw: knob.ccw, cw: knob.cw, press: knob.press},
      rotateCcw: 'ENC_CC',
      rotateCw: 'ENC_CW',
      press: 'ENC',
      enabled: true,
    })),
  };
}

function assertObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
}

function assertByte(value, label) {
  if (!Number.isInteger(value) || value < 0 || value > 0xff) {
    throw new Error(`${label} must be an integer between 0 and 255`);
  }
}

function assertMicroKey(value, label, {allowNull = true} = {}) {
  if (value === null && allowNull) return;
  if (typeof value !== 'string' || !MICRO_KEYS.includes(value)) {
    throw new Error(`${label} must be one of ${MICRO_KEYS.join(', ')}`);
  }
}

function assertButtonKey(value, label) {
  if (value === null) return;
  if (typeof value !== 'string' || !MICRO_BUTTON_KEYS.includes(value)) {
    throw new Error(`${label} must be an AGxx/ACTxx key or null`);
  }
}

function normalizeConfig(value) {
  assertObject(value, 'config');
  const requestedProfile = value.inputProfile === undefined
    ? inferInputProfile(value.buttons)
    : value.inputProfile;
  if (typeof requestedProfile !== 'string' || !INPUT_PROFILES.includes(requestedProfile)) {
    throw new Error(`Unsupported input profile: ${String(requestedProfile)}`);
  }
  const base = defaultConfig({inputProfile: requestedProfile});
  const out = clone(base);

  if (value.schema !== undefined && value.schema !== CONFIG_SCHEMA) {
    throw new Error(`Unsupported config schema: ${String(value.schema)}`);
  }
  if (value.version !== undefined && value.version !== CONFIG_VERSION) {
    throw new Error(`Unsupported config version: ${String(value.version)}`);
  }
  out.schema = CONFIG_SCHEMA;
  out.version = CONFIG_VERSION;
  out.inputProfile = requestedProfile;
  if (value.visual !== undefined) {
    assertObject(value.visual, 'visual');
    if(value.visual.stripMode!==undefined){
      if(!['buttons','knobs'].includes(value.visual.stripMode))throw new Error('Unsupported strip mode');
      out.visual.stripMode=value.visual.stripMode;
    }
    if (value.visual.theme !== undefined) {
      if (!themeCatalog.themes.some(item => item.id === value.visual.theme)) throw new Error('Unsupported visual theme');
      out.visual.theme = value.visual.theme;
    }
  }

  if (value.device !== undefined) {
    assertObject(value.device, 'device');
    for (const field of ['vendorId', 'productId', 'usagePage', 'usage', 'reportId']) {
      if (value.device[field] !== undefined) {
        if (!Number.isInteger(value.device[field]) || value.device[field] < 0 || value.device[field] > 0xffff) {
          throw new Error(`device.${field} must be an integer between 0 and 65535`);
        }
        out.device[field] = value.device[field];
      }
    }
  }

  if (value.release !== undefined) {
    assertObject(value.release, 'release');
    if (value.release.synthesize !== undefined) {
      if (typeof value.release.synthesize !== 'boolean') throw new Error('release.synthesize must be boolean');
      out.release.synthesize = value.release.synthesize;
    }
    if (value.release.delayMs !== undefined) {
      if (!Number.isInteger(value.release.delayMs) || value.release.delayMs < 0 || value.release.delayMs > 2000) {
        throw new Error('release.delayMs must be an integer between 0 and 2000');
      }
      out.release.delayMs = value.release.delayMs;
    }
  }

  if (value.buttons !== undefined) {
    if (!Array.isArray(value.buttons) || value.buttons.length !== base.buttons.length) {
      throw new Error(`buttons must contain exactly ${base.buttons.length} entries`);
    }
    const seenCodes = new Set();
    out.buttons = value.buttons.map((entry, index) => {
      assertObject(entry, `buttons[${index}]`);
      const item = {...base.buttons[index], ...entry};
      if (typeof item.id !== 'string' || !item.id) throw new Error(`buttons[${index}].id must be non-empty`);
      if (typeof item.label !== 'string') throw new Error(`buttons[${index}].label must be a string`);
      assertByte(item.hardwareCode, `buttons[${index}].hardwareCode`);
      if (seenCodes.has(item.hardwareCode)) throw new Error(`Duplicate button hardwareCode: ${item.hardwareCode}`);
      seenCodes.add(item.hardwareCode);
      assertButtonKey(item.targetKey, `buttons[${index}].targetKey`);
      if (typeof item.enabled !== 'boolean') throw new Error(`buttons[${index}].enabled must be boolean`);
      if (item.enabled && item.targetKey === null) throw new Error(`buttons[${index}].targetKey is required when enabled`);
      return item;
    });
  }

  if (value.knobs !== undefined) {
    if (!Array.isArray(value.knobs) || value.knobs.length !== base.knobs.length) {
      throw new Error(`knobs must contain exactly ${base.knobs.length} entries`);
    }
    const seenCodes = new Set();
    out.knobs = value.knobs.map((entry, index) => {
      assertObject(entry, `knobs[${index}]`);
      const item = {...base.knobs[index], ...entry,
        hardware: {...base.knobs[index].hardware, ...(entry.hardware || {})}};
      if (typeof item.id !== 'string' || !item.id) throw new Error(`knobs[${index}].id must be non-empty`);
      if (typeof item.label !== 'string') throw new Error(`knobs[${index}].label must be a string`);
      if (!KNOB_MODES.includes(item.mode)) throw new Error(`knobs[${index}].mode is invalid`);
      if (typeof item.reasoningConfigured !== 'boolean') throw new Error(`knobs[${index}].reasoningConfigured must be boolean`);
      assertObject(item.hardware, `knobs[${index}].hardware`);
      for (const field of ['ccw', 'cw', 'press']) {
        assertByte(item.hardware[field], `knobs[${index}].hardware.${field}`);
        if (seenCodes.has(item.hardware[field])) throw new Error(`Duplicate knob hardwareCode: ${item.hardware[field]}`);
        seenCodes.add(item.hardware[field]);
      }
      assertMicroKey(item.rotateCcw, `knobs[${index}].rotateCcw`, {allowNull: false});
      assertMicroKey(item.rotateCw, `knobs[${index}].rotateCw`, {allowNull: false});
      assertMicroKey(item.press, `knobs[${index}].press`, {allowNull: false});
      if (item.rotateCcw !== 'ENC_CC' && item.rotateCcw !== 'ENC_CW') {
        throw new Error(`knobs[${index}].rotateCcw must be ENC_CC or ENC_CW`);
      }
      if (item.rotateCw !== 'ENC_CW' && item.rotateCw !== 'ENC_CC') {
        throw new Error(`knobs[${index}].rotateCw must be ENC_CW or ENC_CC`);
      }
      if (item.press !== 'ENC' && !MICRO_BUTTON_KEYS.includes(item.press)) {
        throw new Error(`knobs[${index}].press must be ENC or a button key`);
      }
      if (typeof item.enabled !== 'boolean') throw new Error(`knobs[${index}].enabled must be boolean`);
      return item;
    });
  }
  return out;
}

function serializeConfig(config, pretty = true) {
  return JSON.stringify(normalizeConfig(config), null, pretty ? 2 : 0);
}

function parseConfig(text) {
  if (typeof text !== 'string' || !text.trim()) throw new Error('Configuration JSON is empty');
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new Error(`Invalid configuration JSON: ${error.message}`);
  }
  return normalizeConfig(parsed);
}

function eventForKey(key, action) {
  return inputEvent(key, action);
}

function buttonBinding(config, hardwareCode) {
  return config.buttons.find((entry) => entry.enabled && entry.hardwareCode === hardwareCode) || null;
}

function fastTouchBinding(config, hardwareCode) {
  if (config.visual.stripMode !== 'knobs') return null;
  const index = config.buttons.findIndex((entry) => entry.enabled && entry.hardwareCode === hardwareCode);
  // The N4 secondary screen is split into four touch zones.  In information
  // strip mode only the right-most zone is a real Micro action; the other
  // zones are display-only.  Firmware reports this touch as a one-shot
  // state=0 packet (there is no separate release packet).
  if (index !== 13) return null;
  const binding = config.buttons[index];
  return binding.targetKey === 'ACT06' ? binding : null;
}

function knobBinding(config, field, hardwareCode) {
  return config.knobs.find((entry) => entry.enabled && entry.hardware?.[field] === hardwareCode) || null;
}

/**
 * Map one raw N4 report to zero or more valid Micro events.
 *
 * The N4 firmware emits encoder presses as one-shot state=0x01 packets.  If
 * release.synthesize is enabled, a synthetic release is returned alongside
 * the press.  `meta` is intentionally outside `params`; callers that put the
 * event on the wire can strip it without changing the Micro payload.
 */
function mapN4HardwareEvent(hardwareCode, state = 0, config = defaultConfig()) {
  const cfg = normalizeConfig(config);
  if (!Number.isInteger(hardwareCode) || hardwareCode < 0 || hardwareCode > 0xff) return [];
  if (!Number.isInteger(state) || state < 0 || state > 0xff) return [];
  const code = hardwareCode;
  const rotateCcw = knobBinding(cfg, 'ccw', code);
  if (rotateCcw) return independentRotation(rotateCcw,-1) || [eventForKey(rotateCcw.rotateCcw, 2)];
  const rotateCw = knobBinding(cfg, 'cw', code);
  if (rotateCw) return independentRotation(rotateCw,1) || [eventForKey(rotateCw.rotateCw, 2)];

  const press = knobBinding(cfg, 'press', code);
  if (press) {
    // Do not turn presses of independently assigned knobs into accidental
    // focused-control selection, chat submission or approval actions.
    if (press.mode !== 'micro') return [];
    const action = state === 0x01 ? 1 : 0;
    const event = eventForKey(press.press, action);
    if (action === 1 && cfg.release.synthesize) {
      return [
        event,
        {...eventForKey(press.press, 0), meta: {synthetic: true, delayMs: cfg.release.delayMs}},
      ];
    }
    return [event];
  }

  const fastTouch = fastTouchBinding(cfg, code);
  if (fastTouch && state === 0x00) {
    const event = eventForKey(fastTouch.targetKey, 1);
    if (cfg.release.synthesize) {
      return [
        event,
        {...eventForKey(fastTouch.targetKey, 0), meta: {synthetic: true, delayMs: cfg.release.delayMs}},
      ];
    }
    return [event];
  }

  const button = buttonBinding(cfg, code);
  if (!button) return [];
  if(cfg.visual.stripMode==='knobs'&&cfg.buttons.indexOf(button)>=10){
    if(cfg.buttons.indexOf(button)!==13||button.targetKey!=='ACT06')return [];
  }
  const action = state === 0x01 ? 1 : 0;
  return [eventForKey(button.targetKey, action)];
}

function mapN4Report(report, config = defaultConfig()) {
  const extracted = n4HardwareEvent(report);
  if (!extracted) return [];
  return mapN4HardwareEvent(extracted.hardwareCode, extracted.state, config);
}

function wireEvent(event) {
  if (!event || typeof event !== 'object') throw new Error('Invalid event');
  const {meta, ...wire} = event;
  return wire;
}

function mapN4ReportToWire(report, config = defaultConfig()) {
  return mapN4Report(report, config).filter(event=>!event.localAction).map(wireEvent);
}

module.exports = {
  CONFIG_SCHEMA,
  CONFIG_VERSION,
  N4_DEVICE,
  KNOB_LAYOUT,
  N4_BUTTON_PROFILES,
  INPUT_PROFILES,
  DEFAULT_INPUT_PROFILE,
  N4_BUTTON_CODES,
  MICRO_BUTTON_KEYS,
  MICRO_ENCODER_KEYS,
  MICRO_KEYS,
  defaultConfig,
  profileCodes,
  inferInputProfile,
  normalizeConfig,
  serializeConfig,
  parseConfig,
  mapN4HardwareEvent,
  mapN4Report,
  mapN4ReportToWire,
  wireEvent,
};
