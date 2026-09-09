'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const {Decoder, MicroEndpoint, encode, inputEvent} = require('../src/micro-protocol.cjs');
const {MicroTransportHub, reportBuffer} = require('../src/micro-transport.cjs');

function decodeReports(reports) {
  const decoder = new Decoder();
  return reports.flatMap((report) => decoder.feed(report));
}

test('device messages queue complete 64-byte Micro input reports', () => {
  const hub = new MicroTransportHub();
  const event = inputEvent('AG00', 1);
  const item = hub.enqueueDeviceMessage(event, 'n4');
  assert.equal(item.source, 'n4');
  assert.equal(item.reports[0].length, 64);
  assert.equal(hub.snapshot().queuedInputReports, 1);
  assert.deepEqual(decodeReports(hub.drainInputReports(1)), [event]);
  assert.equal(hub.snapshot().stats.drainedReports, 1);
});

test('host reports decode across fragments and RPC replies return through input queue', () => {
  const seen = [];
  const hub = new MicroTransportHub({onHostMessage: (item) => seen.push(item.message)});
  const request = {method: 'sys.version', params: {}, id: 7};
  const reports = encode(request);
  let result = {messages: [], replies: []};
  for (const report of reports) result = hub.feedHostReport(report);
  assert.deepEqual(seen, [request]);
  assert.equal(result.replies[0].id, 7);
  const replies = decodeReports(hub.drainInputReports(10));
  assert.equal(replies[0].id, 7);
  assert.match(replies[0].result.version, /n4-emulator/);
});

test('notifications update the endpoint without manufacturing a reply', () => {
  const endpoint = new MicroEndpoint();
  const hub = new MicroTransportHub({endpoint});
  const notification = {method: 'v.oai.thstatus', params: [{id: 2, c: 0x112233, b: 1, e: 1}]};
  const result = hub.feedHostReports(encode(notification));
  assert.deepEqual(result.replies, []);
  assert.equal(endpoint.slots[2].c, 0x112233);
  assert.equal(hub.snapshot().queuedInputReports, 0);
});

test('bounded queue drops the oldest report and malformed reports reset decoder', () => {
  const hub = new MicroTransportHub({maxQueuedReports: 2});
  hub.enqueueDeviceMessage(inputEvent('AG00', 1));
  hub.enqueueDeviceMessage(inputEvent('AG01', 1));
  hub.enqueueDeviceMessage(inputEvent('AG02', 1));
  assert.equal(hub.snapshot().queuedInputReports, 2);
  assert.equal(hub.snapshot().stats.droppedReports, 1);
  assert.deepEqual(decodeReports(hub.drainInputReports(2)).map((event) => event.params.k), ['AG01', 'AG02']);
  assert.throws(() => hub.feedHostReport(Buffer.alloc(64)), /Invalid Micro HID report/);
  assert.equal(hub.snapshot().stats.decodeErrors, 1);
});

test('report buffer accepts 63/64 bytes and rejects ambiguous lengths', () => {
  assert.equal(reportBuffer(Buffer.alloc(63)).length, 63);
  assert.equal(reportBuffer(Buffer.alloc(64)).length, 64);
  assert.throws(() => reportBuffer(Buffer.alloc(62)), /64 bytes/);
});
