'use strict';
const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_CONFIG = Object.freeze({
  version: 1,
  releaseDelayMs: 100,
  selectedEncoder: 0,
  autoRelease: true,
  keys: {},
  encoders: {
    0: {clockwise: 'ENC_CW', counterclockwise: 'ENC_CC', press: 'ENC'},
    1: {clockwise: 'ENC_CW', counterclockwise: 'ENC_CC', press: 'ENC'},
    2: {clockwise: 'ENC_CW', counterclockwise: 'ENC_CC', press: 'ENC'},
    3: {clockwise: 'ENC_CW', counterclockwise: 'ENC_CC', press: 'ENC'},
  },
  slots: Array.from({length: 6}, (_, id) => ({id, title: `Agent ${id}`, color: 0, enabled: true})),
});

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function merge(base, value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return clone(base);
  const out = clone(base);
  for (const [k, v] of Object.entries(value)) {
    if (k === 'slots' && Array.isArray(v)) out.slots = out.slots.map((slot, id) => ({...slot, ...(v[id] || {}), id}));
    else if (k === 'encoders' && v && typeof v === 'object') {
      for (const [id, map] of Object.entries(v)) {
        if (out.encoders[id]) out.encoders[id] = {...out.encoders[id], ...(map || {})};
      }
    }
    else if (['keys'].includes(k) && v && typeof v === 'object') out[k] = {...out[k], ...v};
    else if (['version', 'releaseDelayMs', 'selectedEncoder'].includes(k) && Number.isFinite(v)) out[k] = Math.trunc(v);
    else if (k === 'autoRelease' && typeof v === 'boolean') out[k] = v;
  }
  out.releaseDelayMs = Math.max(0, Math.min(2000, out.releaseDelayMs));
  out.selectedEncoder = Math.max(0, Math.min(3, out.selectedEncoder));
  return out;
}
function load(file) {
  try { return merge(DEFAULT_CONFIG, JSON.parse(fs.readFileSync(file, 'utf8'))); }
  catch (e) { if (e.code === 'ENOENT') return clone(DEFAULT_CONFIG); throw new Error(`Invalid config: ${e.message}`); }
}
function save(file, config) {
  const normalized = merge(DEFAULT_CONFIG, config);
  fs.mkdirSync(path.dirname(file), {recursive: true});
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(normalized, null, 2) + '\n', 'utf8');
  fs.renameSync(tmp, file);
  return normalized;
}
module.exports = {DEFAULT_CONFIG, merge, load, save};
