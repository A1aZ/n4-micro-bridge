'use strict';
// Independent implementation based on openmicrokbd's documented wire format.
// No Codex or Work Louder application code is imported or redistributed.
const IDENTITY = Object.freeze({vendorId: 0x303a, productId: 0x8360,
  usagePage: 0xff00, usage: 1, release: 0x0100, reportId: 6});
const {
  MicroFileStore,
  READ_CHUNK,
  jsonBytes,
  strictUtf8,
} = require('./micro-files.cjs');

// File offsets/lengths are unsigned numeric JSON values, not JS-coercible
// strings/booleans/null. Mirror the reference's u32 bound and decimal truncation.
function fileUint(value, fallback) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && Math.trunc(value) <= 0xffffffff
    ? Math.trunc(value) : fallback;
}

function parseProtocolJson(text) {
  return JSON.parse(text, (_key, value) => {
    if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('non-finite JSON number');
    return value;
  });
}

function encode(message) {
  const payload = Buffer.from(JSON.stringify(message) + '\n');
  const reports = [];
  for (let i = 0; i < payload.length; i += 61) {
    const chunk = payload.subarray(i, i + 61);
    const report = Buffer.alloc(64);
    report.set([6, 2, chunk.length]);
    chunk.copy(report, 3);
    reports.push(report);
  }
  return reports;
}

class Decoder {
  constructor(limit = 16384) { this.limit = limit; this.buffer = Buffer.alloc(0); }
  reset() { this.buffer = Buffer.alloc(0); }
  feed(value) {
    let report = Buffer.from(value);
    if (report.length === 64 && report[0] === 6) report = report.subarray(1);
    if (report.length !== 63 || report[0] !== 2 || report[1] > 61) {
      this.reset(); throw new Error('Invalid Micro HID report');
    }
    this.buffer = Buffer.concat([this.buffer, report.subarray(2, 2 + report[1])]);
    if (this.buffer.length > this.limit) { this.reset(); throw new Error('Message too large'); }
    const result = [];
    // Hosts may omit the newline; detect complete top-level JSON objects.
    let start = -1, depth = 0, quoted = false, escape = false, consumed = 0;
    try {
      for (let i = 0; i < this.buffer.length; i++) {
        const c = this.buffer[i];
        if (start < 0) {
          if ([9, 10, 13, 32].includes(c)) { consumed = i + 1; continue; }
          if (c !== 123) throw new Error('Expected JSON object');
          start = i; depth = 1; continue;
        }
        if (quoted) {
          if (escape) escape = false;
          else if (c === 92) escape = true;
          else if (c === 34) quoted = false;
        } else if (c === 34) quoted = true;
        else if (c === 123 || c === 91) depth++;
        else if (c === 125 || c === 93) {
          if (--depth === 0) {
            result.push(parseProtocolJson(strictUtf8(this.buffer.subarray(start, i + 1))));
            consumed = i + 1; start = -1;
          }
        }
      }
      this.buffer = this.buffer.subarray(consumed);
      return result;
    } catch (error) { this.reset(); throw error; }
  }
}

function inputEvent(key, action) {
  if (!/^(AG0[0-5]|ACT0[6-9]|ACT1[0-2]|ENC|ENC_CW|ENC_CC)$/.test(key))
    throw new Error('Unsupported Micro key');
  const rotary = key === 'ENC_CW' || key === 'ENC_CC';
  if (rotary ? action !== 2 : ![0, 1].includes(action)) throw new Error('Invalid action');
  const params = {k: key, act: action};
  if (key.startsWith('AG')) params.ag = Number(key.slice(2));
  return {method: 'v.oai.hid', params};
}

function lightPatch(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid light');
  const patch = {};
  const aliases = {
    c: ['c', 'color'],
    b: ['b', 'brightness'],
    e: ['e', 'effect'],
    s: ['s', 'speed'],
    sk: ['sk'],
    sa: ['sa'],
    m: ['m', 'magic'],
  };
  for (const [field, names] of Object.entries(aliases)) {
    const name = names.find((candidate) => Object.hasOwn(value, candidate));
    if (name === undefined) continue;
    const v = value[name];
    let valid;
    if (field === 'c') valid = Number.isInteger(v) && v >= 0 && v <= 0xffffff;
    else if (field === 'e') valid = (Number.isInteger(v) && v >= 0 && v <= 6) ||
      ['off', 'solid', 'snake', 'rainbow', 'breath', 'gradient', 'shallowBreath'].includes(v);
    else valid = typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1;
    if (!valid) throw new Error('Invalid light field: ' + field);
    patch[field] = v;
  }
  return patch;
}

class MicroEndpoint {
  constructor(options = {}) {
    this.files = options.files instanceof MicroFileStore ? options.files : new MicroFileStore();
    this.reset();
  }
  reset() {
    this.slots = Array.from({length: 6}, (_, id) => ({id, c: 0, b: 0, e: 0, s: 0}));
    this.lighting = {keys: {}, ambient: {}};
  }
  handle(request) {
    const hasId = request && Object.hasOwn(request, 'id');
    const reply = body => hasId ? {id: request.id, ...body} : null;
    if (!request || typeof request.method !== 'string')
      return reply({error: {code: -32600, message: 'Invalid request'}});
    try {
      let result;
      switch (request.method) {
        case 'sys.version': result = {version: '0.1.0-n4-emulator'}; break;
        case 'device.status': result = {version: '0.1.0-n4-emulator', profile_index: 0,
          layer_index: 0, battery: 100, is_charging: false}; break;
        case 'v.oai.thstatus': {
          if (!Array.isArray(request.params)) throw new Error('Expected light array');
          const next = this.slots.map(s => ({...s}));
          for (const value of request.params) {
            if (!value || !Number.isInteger(value.id) || value.id < 0 || value.id > 5)
              throw new Error('Invalid slot');
            Object.assign(next[value.id], lightPatch(value));
          }
          this.slots = next; result = {ok: true}; break;
        }
        case 'v.oai.rgbcfg': {
          const params = request.params;
          if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('Invalid lighting');
          const next = structuredClone(this.lighting);
          for (const side of ['keys', 'ambient']) if (side in params) {
            if (params[side] === null) continue;
            Object.assign(next[side], lightPatch(params[side]));
          }
          this.lighting = next; result = {ok: true}; break;
        }
        case 'lights.preview': {
          const params = request.params;
          if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('Invalid lighting');
          const next = structuredClone(this.lighting);
          if (Object.hasOwn(params, 'backlight') && params.backlight !== null)
            Object.assign(next.keys, lightPatch(params.backlight));
          if (Object.hasOwn(params, 'underglow') && params.underglow !== null)
            Object.assign(next.ambient, lightPatch(params.underglow));
          this.lighting = next; result = {ok: true}; break;
        }
        case 'fs.list':
          result = this.files.list(); break;
        case 'fs.readbin': {
          const params = request.params;
          const file = params && typeof params === 'object' && !Array.isArray(params) ? params.file : undefined;
          const offsetValue = params && typeof params === 'object' && !Array.isArray(params) ? params.offset : undefined;
          const lengthValue = params && typeof params === 'object' && !Array.isArray(params) ? params.len : undefined;
          const offset = fileUint(offsetValue, 0);
          const length = fileUint(lengthValue, READ_CHUNK);
          result = this.files.readBinary(file, offset, length);
          if (!result) throw Object.assign(new Error('File does not exist'), {code: -2});
          break;
        }
        case 'fs.read': {
          const params = request.params;
          const file = params && typeof params === 'object' && !Array.isArray(params) ? params.file : undefined;
          const body = this.files.read(file);
          if (!body) throw Object.assign(new Error('File does not exist'), {code: -2});
          try {
            const text = strictUtf8(body).trim();
            if (!text.startsWith('{') && !text.startsWith('['))
              throw new Error('not a JSON object or array');
            result = parseProtocolJson(text);
          } catch (_) {
            throw Object.assign(new Error('File does not exist'), {code: -2});
          }
          break;
        }
        case 'fs.writebin': {
          const params = request.params;
          if (!params || typeof params !== 'object' || Array.isArray(params) || !Object.hasOwn(params, 'file')) {
            this.files.abortWrite();
            throw Object.assign(new Error('write failed'), {code: -3});
          }
          const outcome = this.files.writeBinaryChunk(params.file, params.data ?? '', params.completed === true || params.completed === 1);
          if (!outcome.ok) throw Object.assign(new Error('write failed'), {code: -3});
          result = {data_written: outcome.dataWritten};
          break;
        }
        case 'fs.write': {
          const params = request.params;
          if (!params || typeof params !== 'object' || Array.isArray(params) ||
              !Object.hasOwn(params, 'file') || !Object.hasOwn(params, 'data'))
            throw Object.assign(new Error('params are not correct'), {code: -1});
          const file = this.files.normalizeName(params.file);
          if (!file || !this.files.beginWrite(file))
            throw Object.assign(new Error('write failed'), {code: -3});
          try {
            if (!this.files.write(jsonBytes(params.data)) || !this.files.finishWrite())
              throw new Error('write failed');
          } catch (error) {
            this.files.abortWrite();
            throw Object.assign(new Error(error.message || 'write failed'), {code: -3});
          }
          result = {ok: true};
          break;
        }
        case 'fs.delete': {
          const params = request.params;
          const file = params && typeof params === 'object' && !Array.isArray(params) ? params.file : undefined;
          if (!this.files.delete(file)) throw Object.assign(new Error('File does not exist'), {code: -2});
          result = {ok: true};
          break;
        }
        case 'fs.rmdir':
        case 'fs.txcommit':
          result = {ok: true}; break;
        case 'fs.txbegin':
          result = {tx: 1}; break;
        case 'ui.active_screen': result = {screen_name: 'home'}; break;
        case 'appmgr.list_active':
        case 'appmgr.list_installed': result = []; break;
        case 'host.focused_app': result = {ok: true}; break;
        case 'ui.home_accent_color':
        case 'mp.write_info':
        case 'mp.write_artwork':
        case 'sys.selftest': result = {ok: true}; break;
        default: return reply({error: {code: -32601, message: 'Method not found'}});
      }
      return reply({result});
    } catch (error) {
      return reply({error: {code: Number.isInteger(error.code) ? error.code : -32602,
        message: error.message}});
    }
  }
}
module.exports = {IDENTITY, encode, Decoder, inputEvent, MicroEndpoint, MicroFileStore};
