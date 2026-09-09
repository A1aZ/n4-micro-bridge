'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {Decoder, encode} = require('../src/micro-protocol.cjs');

const project = path.join(__dirname, '..');

function freePort() {
  return new Promise((resolve, reject) => {
    const socket = net.createServer();
    socket.once('error', reject);
    socket.listen(0, '127.0.0.1', () => {
      const address = socket.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      socket.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

async function waitForServer(base, child) {
  const started = Date.now();
  while (Date.now() - started < 5000) {
    if (child.exitCode !== null) throw new Error(`server exited early (${child.exitCode})`);
    try {
      const response = await fetch(`${base}/api/transport/state`);
      if (response.ok) return;
    } catch (_) { /* retry */ }
    await new Promise((resolve) => setTimeout(resolve, 40));
  }
  throw new Error('server did not start');
}

async function request(base, route, method = 'GET', body) {
  const response = await fetch(`${base}${route}`, {
    method,
    headers: body === undefined ? undefined : {'content-type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let value;
  try { value = JSON.parse(text); } catch (_) { value = text; }
  return {response, value, text};
}

function decodeReports(reports) {
  const decoder = new Decoder();
  return reports.flatMap((item) => decoder.feed(item.bytes || item));
}

test('WebUI transport hub handles Codex output, N4 input, and visual preview', async (t) => {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const child = spawn(process.execPath, ['webui/server.cjs'], {
    cwd: project,
    env: {...process.env, MIRABOX_WEBUI_PORT: String(port)},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', (chunk) => { output += chunk.toString(); });
  child.stderr.on('data', (chunk) => { output += chunk.toString(); });
  t.after(() => { if (child.exitCode === null) child.kill('SIGTERM'); });
  await waitForServer(base, child);

  const visualPage = await request(base, '/visual');
  assert.equal(visualPage.response.status, 200, output);
  assert.match(visualPage.text, /N4.*Codex Micro/i);
  const visualScriptStart = visualPage.text.indexOf('<script>');
  const visualScriptEnd = visualPage.text.lastIndexOf('</script>');
  assert.ok(visualScriptStart >= 0 && visualScriptEnd > visualScriptStart);
  // The page is intentionally self-contained; this catches accidental
  // template interpolation or a broken inline script before opening a browser.
  assert.doesNotThrow(() => new Function(visualPage.text.slice(visualScriptStart + 8, visualScriptEnd)));

  const reset = await request(base, '/api/transport/reset', 'POST', {endpoint: true});
  assert.equal(reset.response.status, 200);
  const initial = await request(base, '/api/visual/state');
  assert.equal(initial.response.status, 200);
  assert.equal(initial.value.model.buttons.length, 14);
  assert.match(initial.value.model.screen.svg, /<svg/);

  // Host output can arrive as one HTTP request per 64-byte HID report.  The
  // server must retain Decoder state across requests and emit the message only
  // after the final fragment, rather than requiring callers to batch reports.
  const fragmentedRequest = {
    method: 'host.focused_app',
    params: {name: 'fragment-test-' + 'x'.repeat(180)},
  };
  const fragments = encode(fragmentedRequest);
  assert.ok(fragments.length > 1);
  const fragmentedMessages = [];
  for (let index = 0; index < fragments.length; index += 1) {
    const part = await request(base, '/api/transport/output', 'POST', {
      reports: [[...fragments[index]]],
      source: 'fragment-test',
    });
    assert.equal(part.response.status, 200);
    // Decoder intentionally accepts a complete JSON object without waiting
    // for the optional trailing newline, so completion may occur on the
    // penultimate report when the final report contains only that newline.
    assert.ok(part.value.messages.length <= 1);
    fragmentedMessages.push(...part.value.messages);
    assert.deepEqual(part.value.replies, []);
  }
  assert.deepEqual(fragmentedMessages, [fragmentedRequest]);

  const status = await request(base, '/api/rpc', 'POST', {method: 'sys.version', params: {}, id: 11});
  assert.equal(status.response.status, 200);
  assert.equal(status.value.replies[0].id, 11);
  assert.equal(status.value.transport.queuedInputReports, 1);
  const statusReports = await request(base, '/api/transport/input?limit=8');
  const statusMessages = decodeReports(statusReports.value.reports);
  assert.equal(statusMessages[0].id, 11);
  assert.match(statusMessages[0].result.version, /n4-emulator/);

  const lightReports = encode({
    method: 'v.oai.thstatus',
    params: [{id: 0, c: 0xff2200, b: 1, e: 'solid'}],
  }).map((report) => [...report]);
  const light = await request(base, '/api/transport/output', 'POST', {reports: lightReports});
  assert.equal(light.response.status, 200);
  assert.equal(light.value.messages.length, 1);
  assert.equal(light.value.replies.length, 0);
  assert.equal(light.value.visual.model.buttons[0].light.colorHex, '#FF2200');

  const rotation = await request(base, '/api/bridge/hardware', 'POST', {hardwareCode: 0xa1, state: 0});
  assert.deepEqual(rotation.value.events[0].params, {k: 'ENC_CW', act: 2});
  const rotationReports = await request(base, '/api/transport/input?limit=8');
  assert.deepEqual(decodeReports(rotationReports.value.reports).map((event) => event.params), [{k: 'ENC_CW', act: 2}]);

  const press = await request(base, '/api/bridge/hardware', 'POST', {hardwareCode: 0x37, state: 1});
  assert.deepEqual(press.value.events[0].params, {k: 'ENC', act: 1});
  await new Promise((resolve) => setTimeout(resolve, 140));
  const pressReports = await request(base, '/api/transport/input?limit=8');
  assert.deepEqual(decodeReports(pressReports.value.reports).map((event) => event.params.act), [1, 0]);

  // A bridge reset must also discard already queued Micro input reports and
  // cancel a delayed synthetic release, so events from the previous session
  // cannot leak into a newly attached Codex host.
  const queuedBeforeReset = await request(base, '/api/bridge/hardware', 'POST', {hardwareCode: 0xa0, state: 0});
  assert.equal(queuedBeforeReset.value.transport.queuedInputReports, 1);
  const bridgeReset = await request(base, '/api/bridge/reset', 'POST', {});
  assert.equal(bridgeReset.response.status, 200);
  assert.equal(bridgeReset.value.transport.queuedInputReports, 0);
  const afterReset = await request(base, '/api/transport/input?limit=8&drain=0');
  assert.equal(afterReset.value.reports.length, 0);

  const pendingBeforeReset = await request(base, '/api/bridge/hardware', 'POST', {hardwareCode: 0x37, state: 1});
  assert.equal(pendingBeforeReset.value.transport.queuedInputReports, 1);
  const pendingReset = await request(base, '/api/bridge/reset', 'POST', {});
  assert.equal(pendingReset.value.transport.queuedInputReports, 0);
  await new Promise((resolve) => setTimeout(resolve, 140));
  const afterPendingReset = await request(base, '/api/transport/input?limit=8&drain=0');
  assert.equal(afterPendingReset.value.reports.length, 0);

  const preview = await request(base, '/api/visual/preview');
  assert.equal(preview.response.status, 200);
  assert.match(preview.response.headers.get('content-type'), /text\/html/);
  assert.match(preview.text, /Codex Micro/);
});
