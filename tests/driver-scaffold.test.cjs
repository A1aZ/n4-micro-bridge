'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..', 'driver', 'codexmicro-umdf');
const read = (...parts) => fs.readFileSync(path.join(root, ...parts), 'utf8');

function arrayBytes(source, symbol) {
  const escaped = symbol.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = source.match(new RegExp(`${escaped}[^=]*=\\s*\\{([\\s\\S]*?)\\};`));
  assert.ok(match, `array ${symbol} should exist`);
  const body = match[1].replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  return [...body.matchAll(/0x([0-9a-f]{2})/gi)].map((item) => Number.parseInt(item[1], 16));
}

const expectedDescriptor = [
  0x06, 0x00, 0xff, 0x09, 0x01, 0xa1, 0x01, 0x85, 0x06,
  0x15, 0x00, 0x26, 0xff, 0x00, 0x75, 0x08, 0x95, 0x3f,
  0x09, 0x01, 0x81, 0x02, 0x95, 0x3f, 0x09, 0x02, 0x91, 0x02, 0xc0,
];
const expectedCompanionDescriptor = [
  0x06, 0x70, 0xff, 0x09, 0x01, 0xa1, 0x01, 0x85, 0x07,
  0x15, 0x00, 0x26, 0xff, 0x00, 0x75, 0x08, 0x95, 0x3f,
  0x09, 0x01, 0x81, 0x02, 0x95, 0x3f, 0x09, 0x02, 0x91, 0x02, 0xc0,
];
const expectedDualDescriptor = expectedDescriptor.concat(expectedCompanionDescriptor);
const sameBytes = (left, right) => left.length === right.length && left.every((value, index) => value === right[index]);

test('UMDF scaffold carries Codex identity and an optional companion descriptor', () => {
  const protocol = read('protocol', 'codexmicro_protocol.h');
  const driver = read('driver', 'codexmicro.c');
  const common = read('inc', 'common.h');
  assert.match(protocol, /MIRABOX_CODEX_MICRO_VID\s+0x303A/);
  assert.match(protocol, /MIRABOX_CODEX_MICRO_PID\s+0x8360/);
  assert.match(protocol, /MIRABOX_CODEX_MICRO_RELEASE\s+0x0100/);
  assert.match(protocol, /MIRABOX_CODEX_MICRO_REPORT_ID\s+0x06/);
  assert.match(common, /L"Work Louder"/);
  assert.match(common, /L"Codex Micro"/);
  assert.deepEqual(arrayBytes(protocol, 'MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR'), expectedDescriptor);
  const active = arrayBytes(driver, 'G_DefaultReportDescriptor');
  assert.ok(
    sameBytes(active, expectedDescriptor) || sameBytes(active, expectedDualDescriptor),
    'active descriptor must be the original or the fully routed dual TLC descriptor',
  );
  if (sameBytes(active, expectedDualDescriptor)) {
    assert.deepEqual(
      arrayBytes(protocol, 'MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR'),
      expectedCompanionDescriptor,
    );
    assert.deepEqual(
      arrayBytes(protocol, 'MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR'),
      expectedDualDescriptor,
    );
    assert.match(protocol, /MIRABOX_CODEX_COMPANION_USAGE_PAGE\s+0xFF70/);
    assert.match(protocol, /MIRABOX_CODEX_COMPANION_REPORT_ID\s+0x07/);
  }
});

test('UMDF project packages the bridge source and a root-enumerated INF', () => {
  const project = read('driver', 'umdf2', 'CodexMicroUm.vcxproj');
  const inf = read('driver', 'umdf2', 'CodexMicroUm.inx');
  const bridge = read('driver', 'bridge.c');
  assert.match(project, /WindowsUserModeDriver10\.0/);
  assert.match(project, /\.\.\\bridge\.c/);
  assert.match(project, /TargetName>CodexMicroUm</);
  assert.match(inf, /root\\MiraboxCodexMicro/i);
  assert.match(inf, /MsHidUmdf\.inf/i);
  assert.match(inf, /ServiceBinary="%13%\\CodexMicroUm\.dll"/);
  assert.match(bridge, /IOCTL_MIRABOX_CODEX_PUSH_INPUT|BridgePushInput/);
  assert.match(bridge, /BridgePublishOutput/);
});

test('RESET establishes a barrier and completes pending sideband reads', () => {
  const protocol = read('driver', 'codexmicro.h');
  const bridge = read('driver', 'bridge.c');
  assert.match(protocol, /BridgeResetting/);
  // HID READ_REPORT requests are parked in ManualQueue; the legacy
  // diagnostic READ_OUTPUT requests use BridgeOutputQueue. RESET must cancel
  // both classes of pending request while the barrier is active.
  assert.match(bridge, /WdfIoQueueRetrieveNextRequest\(\s*DeviceContext->ManualQueue/);
  assert.match(bridge, /DeviceContext->BridgeResetting\s*=\s*TRUE/);
  assert.match(bridge, /WdfIoQueueRetrieveNextRequest\(\s*DeviceContext->BridgeOutputQueue/);
  assert.equal((bridge.match(/WdfRequestComplete\(request,\s*STATUS_CANCELLED\)/g) || []).length, 2);
  assert.match(bridge, /DeviceContext->BridgeResetting\s*=\s*FALSE/);
});

test('input sideband and HID reads share an atomic ring/queue handoff', () => {
  const driver = read('driver', 'codexmicro.c');
  const bridge = read('driver', 'bridge.c');
  assert.match(bridge, /BridgeTryPopInputLocked/);
  assert.match(bridge, /BridgeStoreInputLocked/);
  assert.match(bridge, /BridgePushInput(?:Report)?[\s\S]*WdfWaitLockAcquire[\s\S]*WdfIoQueueRetrieveNextRequest/);
  assert.match(bridge, /BridgeReadInput[\s\S]*BridgeTryPopInputLocked[\s\S]*WdfRequestForwardToIoQueue/);
  assert.match(driver, /ReadReport[\s\S]*BridgeReadInput/);
  assert.match(driver, /GetInputReport[\s\S]*report\[1\]\s*=\s*MIRABOX_CODEX_MICRO_MESSAGE_TYPE/);
  assert.match(bridge, /MIRABOX_CODEX_MICRO_REPORT_ID/);

  const active = arrayBytes(driver, 'G_DefaultReportDescriptor');
  if (sameBytes(active, expectedDualDescriptor)) {
    // IOCTL_HID_READ_REPORT has no requested report ID; a dual TLC driver must
    // use the shared HID input stream and let HIDClass route returned reports.
    const readMatch = /(?:^|\r?\n)ReadReport\(\s*[\r\n]/.exec(driver);
    const writeMatch = /(?:^|\r?\n)WriteReport\(\s*[\r\n]/.exec(driver);
    const readStart = readMatch ? readMatch.index : -1;
    const writeStart = writeMatch ? writeMatch.index : -1;
    const readBody = readStart >= 0
      ? driver.slice(readStart, writeStart > readStart ? writeStart : undefined)
      : '';
    assert.doesNotMatch(
      readBody,
      /RequestGetHidXferPacket_ToReadFromDevice|BridgeReadCompanionOutput/,
    );
    assert.doesNotMatch(bridge, /CompanionReadQueue|CompanionOutputRing/);
    assert.match(driver, /WriteReport[\s\S]*COMPANION_COLLECTION_REPORT_ID[\s\S]*BridgePushInputReport/);
    assert.match(driver, /SetOutputReport[\s\S]*COMPANION_COLLECTION_REPORT_ID[\s\S]*BridgePushInputReport/);
  }
});
