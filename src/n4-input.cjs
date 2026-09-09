'use strict';
// The public Python SDK's StreamDockN4 class currently decodes only keys.
// N4/N4Pro-style rotary reports use the same base StreamDock packet fields
// (hardware code=data[9], state=data[10]). Keep this mapping separate so it
// can be corrected after capturing one real N4 report.
const KNOB_ROTATE = new Map([
  [0xa0, ['ENC_CC', 2]], [0xa1, ['ENC_CW', 2]],
  [0x50, ['ENC_CC', 2]], [0x51, ['ENC_CW', 2]],
  [0x90, ['ENC_CC', 2]], [0x91, ['ENC_CW', 2]],
  [0x70, ['ENC_CC', 2]], [0x71, ['ENC_CW', 2]],
]);
const KNOB_PRESS = new Map([[0x37, 'ENC'], [0x35, 'ENC'], [0x33, 'ENC'], [0x36, 'ENC']]);
// The upstream C++ StreamDockN4 dispatcher identifies these as secondary-
// screen swipes. They are intentionally diagnostic-only: Codex Micro's
// v.oai.hid vocabulary has no canonical swipe key to emit on the wire.
const N4_SWIPE = new Map([[0x38, 'left'], [0x39, 'right']]);

// hidapi commonly returns a report-ID byte in front of the N4 vendor report
// (513 bytes total: ID + 512-byte body), while WebHID and the StreamDock C
// transport normally expose the body itself.  Keep the offset detection in
// one place so every JS input path uses the same data[9]/data[10] coordinates.
// The size guard is intentional: the WebUI's 64-byte synthetic/raw reports
// must retain their historical no-prefix interpretation even if their payload
// happens to contain an ACK-like byte sequence near the beginning.
function hasAckOkHeader(bytes, offset) {
  return bytes.length >= offset + 7
    && bytes[offset] === 0x41 // A
    && bytes[offset + 1] === 0x43 // C
    && bytes[offset + 2] === 0x4b // K
    && bytes[offset + 5] === 0x4f // O
    && bytes[offset + 6] === 0x4b; // K
}

function n4ReportOffset(data) {
  const bytes = Buffer.from(data || []);
  // A real N4 vendor report is 512 bytes; hidapi may prepend report ID 0.
  return bytes.length >= 512 && hasAckOkHeader(bytes, 1) ? 1 : 0;
}

function n4HardwareEvent(data) {
  const bytes = Buffer.from(data || []);
  const offset = n4ReportOffset(bytes);
  if (bytes.length < offset + 11) return null;
  return {
    hardwareCode: bytes[offset + 9],
    state: bytes[offset + 10],
    offset,
  };
}

function decodeN4Report(data) {
  const extracted = n4HardwareEvent(data);
  if (!extracted) return null;
  const {hardwareCode: code, state} = extracted;
  if (KNOB_ROTATE.has(code)) {
    const [key, act] = KNOB_ROTATE.get(code);
    return {method: 'v.oai.hid', params: {k: key, act}};
  }
  if (KNOB_PRESS.has(code)) {
    // Match StreamDock SDK normalization: only 0x01 is pressed; every
    // other state is released (some N4 firmware uses 0x00 for release).
    return {method: 'v.oai.hid', params: {k: 'ENC', act: state === 0x01 ? 1 : 0}};
  }
  if (N4_SWIPE.has(code)) {
    return {
      kind: 'swipe',
      hardwareCode: code,
      state,
      direction: N4_SWIPE.get(code),
      microKey: null,
      microSupported: false,
    };
  }
  // Normal N4 keys are handled by the existing SDK mapping. Return null here.
  return null;
}

function synthesizeClick(event, releaseMs = 100) {
  if (!event || event.method !== 'v.oai.hid' || event.params?.k !== 'ENC' || event.params?.act !== 1)
    return [];
  if (!Number.isFinite(releaseMs) || releaseMs < 0 || releaseMs > 2000) throw new Error('Invalid release delay');
  return [event, {method: 'v.oai.hid', params: {...event.params, act: 0, synthetic: true, delayMs: releaseMs}}];
}
module.exports = {
  decodeN4Report,
  synthesizeClick,
  n4ReportOffset,
  n4HardwareEvent,
  KNOB_ROTATE,
  KNOB_PRESS,
  N4_SWIPE,
};
