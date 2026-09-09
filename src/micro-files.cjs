'use strict';

/**
 * Small in-memory implementation of the file surface used by the
 * openmicrokbd/Work Louder host during device initialisation.
 *
 * The real firmware exposes two fixed flash slots.  The emulator deliberately
 * keeps the same boundary (and limits) while never touching the host file
 * system: writes are useful for protocol tests and disappear when the process
 * exits or the store is reset.
 */
const crypto = require('node:crypto');
const {TextDecoder} = require('node:util');

// ``Buffer#toString('utf8')`` replaces malformed byte sequences with U+FFFD.
// File RPCs carry JSON, so silently repairing a corrupt slot can turn an
// invalid file into a successful response.  Use a fatal decoder to match the
// Python endpoint and the firmware-facing ``-2`` error semantics.
const UTF8_DECODER = new TextDecoder('utf-8', {fatal: true});

function strictUtf8(value) {
  return UTF8_DECODER.decode(value);
}

const DEFAULT_KEYMAP =
  '{"version":1,"activeProfileId":0,"profiles":[{"id":0,"name":"Codex","layers":[{"id":0,"name":"ChatGPT","color":16711680,"os":0,"layout":{"keymap":[["KV_OAI_AG00","KV_OAI_AG01"],["KV_OAI_AG02","KV_OAI_AG03","KV_OAI_AG04","KV_OAI_AG05"],["KV_OAI_ACT06","KV_OAI_ACT07","KV_OAI_ACT08","KV_OAI_ACT09"],["KV_OAI_ACT10","KV_OAI_ACT11","KV_OAI_ACT12"]],"encoders":[["KV_OAI_ENC_CC","KV_OAI_ENC_CW","KV_OAI_ENC_CLK"]],"buttons":[["KC_MPLY"]],"joystick":{"type":"VENDOR","sectors":[]}}}],"macrosUsed":[],"multiActionsUsed":[]}],"multiActions":[],"macros":[],"macrosGroups":[],"multiActionsGroups":[],"linkedApps":[]}';

const FILE_SPECS = Object.freeze({
  'keymap.json': Object.freeze({maxBytes: 0x3000, defaultValue: DEFAULT_KEYMAP}),
  'smart_actions.json': Object.freeze({maxBytes: 0x1800, defaultValue: null}),
});
const FILE_ORDER = Object.freeze(Object.keys(FILE_SPECS));
const MAX_FILE_NAME = 32;
const READ_CHUNK = 384;

function normalizeFileName(value) {
  if (typeof value !== 'string') return null;
  let name = value;
  if (name.startsWith('/')) name = name.slice(1);
  if (!name || name.length > MAX_FILE_NAME || name.includes('/') || name.includes('\\')) return null;
  return Object.hasOwn(FILE_SPECS, name) ? name : null;
}

function sha1(value) {
  return crypto.createHash('sha1').update(value).digest('hex');
}

function strictBase64(value) {
  if (typeof value !== 'string') throw new Error('data must be base64 text');
  const compact = value.replace(/[\t\r\n ]/g, '');
  // Firmware accepts URL-safe alphabet and omitted padding.  Validate before
  // asking Buffer to decode; Buffer.from() otherwise silently drops junk.
  const unpadded = compact.replace(/=+$/g, '');
  if (!/^[A-Za-z0-9+/_-]*$/.test(unpadded) || unpadded.length % 4 === 1) {
    throw new Error('invalid base64 data');
  }
  const normalized = unpadded.replace(/-/g, '+').replace(/_/g, '/');
  const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
  return Buffer.from(padded, 'base64');
}

function jsonBytes(value) {
  let text;
  try {
    text = JSON.stringify(value);
  } catch (error) {
    throw new Error(`data is not JSON encodable: ${error.message}`);
  }
  if (text === undefined) throw new Error('data is not JSON encodable');
  return Buffer.from(text, 'utf8');
}

class MicroFileStore {
  constructor(initial = {}) {
    this.files = new Map();
    this.pending = null;
    if (initial && typeof initial === 'object' && !Array.isArray(initial)) {
      for (const [name, value] of Object.entries(initial)) {
        const normalized = normalizeFileName(name);
        if (!normalized) continue;
        const bytes = Buffer.from(value || []);
        if (bytes.length <= FILE_SPECS[normalized].maxBytes) this.files.set(normalized, bytes);
      }
    }
  }

  reset() {
    this.files.clear();
    this.pending = null;
  }

  normalizeName(value) {
    return normalizeFileName(value);
  }

  maxBytes(name) {
    const normalized = normalizeFileName(name);
    return normalized ? FILE_SPECS[normalized].maxBytes : 0;
  }

  read(name) {
    const normalized = normalizeFileName(name);
    if (!normalized) return null;
    const stored = this.files.get(normalized);
    if (stored) return Buffer.from(stored);
    const fallback = FILE_SPECS[normalized].defaultValue;
    return fallback === null ? null : Buffer.from(fallback, 'utf8');
  }

  list() {
    const entries = [];
    for (const name of FILE_ORDER) {
      const body = this.read(name);
      if (!body) continue;
      entries.push({
        name,
        // The reference firmware serialises this field as a string.
        size: String(body.length),
        checksum: sha1(body),
      });
    }
    return entries;
  }

  beginWrite(name) {
    const normalized = normalizeFileName(name);
    if (!normalized) {
      this.pending = null;
      return false;
    }
    this.pending = {name: normalized, chunks: [], size: 0};
    return true;
  }

  write(value) {
    if (!this.pending) return false;
    const bytes = Buffer.from(value || []);
    const spec = FILE_SPECS[this.pending.name];
    if (this.pending.size + bytes.length > spec.maxBytes) {
      this.pending.failed = true;
      return false;
    }
    if (bytes.length) this.pending.chunks.push(Buffer.from(bytes));
    this.pending.size += bytes.length;
    return true;
  }

  finishWrite() {
    if (!this.pending || this.pending.failed) {
      this.pending = null;
      return false;
    }
    const {name, chunks} = this.pending;
    this.files.set(name, Buffer.concat(chunks));
    this.pending = null;
    return true;
  }

  abortWrite() {
    this.pending = null;
  }

  delete(name) {
    const normalized = normalizeFileName(name);
    if (!normalized) return false;
    this.pending = null;
    // Erasing an empty slot is still a successful firmware operation.  For
    // keymap.json, read/list consequently fall back to the built-in default.
    this.files.delete(normalized);
    return true;
  }

  writeBinaryChunk(name, data, completed = false) {
    const normalized = normalizeFileName(name);
    if (!normalized) return {ok: false, dataWritten: 0};
    if (!this.pending || this.pending.name !== normalized) {
      if (!this.beginWrite(normalized)) return {ok: false, dataWritten: 0};
    }
    let bytes;
    try {
      bytes = strictBase64(data ?? '');
    } catch (_) {
      this.abortWrite();
      return {ok: false, dataWritten: 0};
    }
    if (!this.write(bytes)) {
      this.abortWrite();
      return {ok: false, dataWritten: 0};
    }
    if (completed && !this.finishWrite()) return {ok: false, dataWritten: 0};
    return {ok: true, dataWritten: bytes.length};
  }

  readBinary(name, offset = 0, length = READ_CHUNK) {
    const body = this.read(name);
    if (!body) return null;
    const safeOffset = Number.isInteger(offset) && offset >= 0 ? offset : 0;
    const safeLength = Number.isInteger(length) && length >= 0 ? Math.min(length, READ_CHUNK) : READ_CHUNK;
    const start = Math.min(safeOffset, body.length);
    const end = Math.min(start + safeLength, body.length);
    return {total_size: body.length, data: body.subarray(start, end).toString('base64')};
  }

  readJson(name) {
    const body = this.read(name);
    if (!body) return null;
    try {
      const value = JSON.parse(strictUtf8(body));
      return value;
    } catch (_) {
      return null;
    }
  }

  snapshot() {
    return {files: this.list(), pending: this.pending ? {name: this.pending.name, size: this.pending.size} : null};
  }
}

module.exports = {
  DEFAULT_KEYMAP,
  FILE_ORDER,
  FILE_SPECS,
  MAX_FILE_NAME,
  READ_CHUNK,
  MicroFileStore,
  normalizeFileName,
  sha1,
  strictUtf8,
  strictBase64,
  jsonBytes,
};
