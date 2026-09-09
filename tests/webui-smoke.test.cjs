'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const vm = require('node:vm');

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

async function waitForServer(base, child, timeoutMs = 5000) {
  const started = Date.now();
  let lastError;
  while (Date.now() - started < timeoutMs) {
    if (child.exitCode !== null) throw new Error(`server exited early (${child.exitCode})`);
    try {
      const response = await fetch(`${base}/api/config`);
      if (response.ok) return;
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) { lastError = error; }
    await new Promise((resolve) => setTimeout(resolve, 40));
  }
  throw new Error(`server did not start: ${lastError?.message || 'timeout'}`);
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

function report(code, state = 1) {
  const bytes = Buffer.alloc(512);
  bytes.set([0x41, 0x43, 0x4b, 0, 0, 0x4f, 0x4b]);
  bytes[9] = code;
  bytes[10] = state;
  return [...bytes];
}

function reportWithLeadingId(code, state = 1) {
  return [0, ...report(code, state)];
}

function openSse(base) {
  let requestHandle;
  let responseHandle;
  let buffer = '';
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const messages = [];
  const done = new Promise((resolve, reject) => {
    requestHandle = http.get(`${base}/api/bridge/events`, (response) => {
      responseHandle = response;
      if (response.statusCode !== 200) {
        rejectReady(new Error(`SSE HTTP ${response.statusCode}`));
        reject(new Error(`SSE HTTP ${response.statusCode}`));
        return;
      }
      resolveReady();
      response.setEncoding('utf8');
      response.on('data', (chunk) => {
        buffer += chunk;
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() || '';
        for (const chunk of chunks) {
          let eventName = 'message';
          let data = '';
          for (const line of chunk.split('\n')) {
            if (line.startsWith('event:')) eventName = line.slice(6).trim();
            if (line.startsWith('data:')) data += line.slice(5).trim();
          }
          if (!data) continue;
          try { messages.push({event: eventName, data: JSON.parse(data)}); }
          catch (_) { messages.push({event: eventName, data}); }
          const latest = messages.at(-1);
          if (latest.event === 'micro-event' && latest.data?.event?.params?.act === 0) {
            resolve(messages);
            requestHandle.destroy();
          }
        }
      });
      response.on('error', reject);
      response.on('aborted', () => reject(new Error('SSE aborted')));
    });
    requestHandle.on('error', (error) => {
      rejectReady(error);
      reject(error);
    });
  });
  return {
    ready,
    done,
    messages,
    close() {
      requestHandle?.destroy();
      responseHandle?.destroy();
    },
  };
}

function checkEmbeddedScript(file) {
  const html = fs.readFileSync(file, 'utf8');
  const start = html.indexOf('<script>');
  const end = html.lastIndexOf('</script>');
  assert.ok(start >= 0 && end > start, `${path.basename(file)} should contain an inline script`);
  assert.doesNotThrow(() => new vm.Script(html.slice(start + '<script>'.length, end)), `${path.basename(file)} script syntax`);
  return html;
}

test('WebUI smoke: config/API, bridge report, SSE synthetic release, and page scripts', async (t) => {
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
  t.after(() => {
    if (child.exitCode === null) child.kill('SIGTERM');
  });

  await waitForServer(base, child);

  const page = await request(base, '/config');
  assert.equal(page.response.status, 200);
  assert.match(page.text, /Codex Micro 配置/);
  const configScript = checkEmbeddedScript(path.join(project, 'webui', 'config.html'));
  assert.match(configScript, /mirabox\.codex\.micro/);
  const realPage = await request(base, '/real-n4');
  const diagnosticPage=await request(base,'/screen-test');
  assert.equal(diagnosticPage.response.status,200);
  checkEmbeddedScript(path.join(project,'webui','screen-test.html'));
  const buttonTestPage = await request(base, '/button-test');
  assert.equal(buttonTestPage.response.status, 200);
  assert.match(buttonTestPage.text, /按一下，就知道对应哪里/);
  checkEmbeddedScript(path.join(project, 'webui', 'button-test.html'));
  assert.equal(realPage.response.status, 200);
  assert.match(realPage.text, /WebHID.*hidapi|hidapi.*WebHID/i);
  assert.match(realPage.text, /n4-webui-bridge\.py/);
  assert.match(realPage.text, /backend-status/);
  assert.match(realPage.text, /server\.apiVersion/);
  assert.match(realPage.text, /featureLevel/);
  assert.match(realPage.text, /dynamicInputMapping/);
  assert.match(realPage.text, /native-status/);
  assert.match(realPage.text, /native-runtime-diagnostics/);
  assert.match(realPage.text, /native-transport/);
  assert.match(realPage.text, /native-visuals/);
  assert.match(realPage.text, /native-visual-target/);
  assert.match(realPage.text, /native-input-profile/);
  assert.match(realPage.text, /start-native/);
  assert.match(realPage.text, /stop-native/);
  assert.match(realPage.text, /api\/native\/start/);
  assert.match(realPage.text, /api\/native\/stop/);
  assert.match(realPage.text, /api\/native\/status/);
  assert.match(realPage.text, /api\/native\/lease/);
  assert.match(realPage.text, /function n4ReportOffset/);
  assert.match(realPage.text, /bytes\[offset \+ 9\]/);
  assert.match(realPage.text, /nativeOwnerHints/);
  assert.match(realPage.text, /rawCallbackRegistered/);
  assert.match(realPage.text, /api\/native\/state/);
  assert.match(realPage.text, /probe-sideband/);
  assert.match(realPage.text, /probe-companion/);
  assert.match(realPage.text, /run-compat-handshake/);
  assert.match(realPage.text, /api\/compat\/handshake/);
  assert.match(realPage.text, /microFsRpc/);
  assert.match(realPage.text, /只调用.*enumerate|只枚举.*不打开设备|不打开设备.*hid\.enumerate/i);
  assert.match(realPage.text, /if \(!response\.ok\) throw new Error/);
  assert.doesNotMatch(realPage.text, /!response\.ok \|\| body\.error/);
  assert.match(realPage.text, /python -m pip install hidapi/);
  assert.match(realPage.text, /knob-controls/);
  assert.match(realPage.text, /button-controls/);
  assert.match(realPage.text, /buildDebugMappings/);
  assert.match(realPage.text, /activeKnobRotate/);
  assert.match(realPage.text, /value\.config|input-profile-title/);
  assert.match(realPage.text, /quick-cw/);
  assert.match(realPage.text, /quick-ccw/);
  assert.match(realPage.text, /quick-press/);
  assert.match(realPage.text, /raw-report-input/);
  assert.match(realPage.text, /raw-report-id/);
  assert.match(realPage.text, /raw-sample-512/);
  assert.match(realPage.text, /raw-sample-513/);
  assert.match(realPage.text, /raw-decode/);
  assert.match(realPage.text, /raw-forward/);
  assert.match(realPage.text, /function parseRawReport/);
  assert.match(realPage.text, /api\/raw/);
  assert.match(realPage.text, /api\/bridge\/report/);
  assert.match(realPage.text, /value\.ageMs\s*!=\s*null\s*&&\s*Number\.isFinite\(age\)/);
  assert.match(realPage.text, /sse-status/);
  assert.match(realPage.text, /source\.onerror\s*=\s*\(\)\s*=>\s*setSseStatus/);
  // The diagnostics page must expose the complete CPP input table, including
  // the currently-unmapped 14th physical key (0x43), so captured reports can
  // be classified instead of falling back to “未匹配”.
  assert.match(realPage.text, /0x43/);
  assert.match(realPage.text, /0x38/);
  assert.match(realPage.text, /0x39/);
  assert.match(realPage.text, /CPP_KNOBS/);
  assert.match(realPage.text, /CPP_BUTTONS/);
  checkEmbeddedScript(path.join(project, 'webui', 'real-n4.html'));
  const visualPage = await request(base, '/visual');
  assert.equal(visualPage.response.status, 200);
  assert.match(visualPage.text, /N4 800 × 480 屏幕预览/);
  assert.match(visualPage.text, /物理 N4 原生桥接/);
  assert.match(visualPage.text, /不会启动进程/);
  assert.match(visualPage.text, /backend-status/);
  assert.match(visualPage.text, /server\.apiVersion/);
  assert.match(visualPage.text, /\.hidapi-venv\\\\Scripts\\\\python\.exe/);
  const visualScript = checkEmbeddedScript(path.join(project, 'webui', 'visual.html'));
  assert.match(visualScript, /api\/visual\/state/);
  assert.match(visualScript, /transport-input/);
  const visualState = await request(base, '/api/visual/state');
  assert.equal(visualState.response.status, 200);
  assert.equal(visualState.value.model.screen.width, 800);
  assert.equal(visualState.value.model.screen.height, 480);
  assert.equal(visualState.value.model.buttons.length, 14);
  assert.equal(visualState.value.sdkPlan.keys.length, 14);
  const transportState = await request(base, '/api/transport/state');
  assert.equal(transportState.response.status, 200);
  assert.equal(transportState.value.transport.queuedInputReports, 0);
  const sidebandState = await request(base, '/api/sideband/state');
  assert.equal(sidebandState.response.status, 200);
  assert.equal(sidebandState.value.status, 'not-probed');
  assert.equal(sidebandState.value.architecture, 'hid-minidriver-scaffold');
  assert.equal(sidebandState.value.operational, false);
  assert.match(sidebandState.value.architectureWarning, /companion control device/);
  const sidebandProbe = await request(base, '/api/sideband/probe', 'POST', {});
  assert.equal(sidebandProbe.response.status, 200);
  // With the root virtual device actually installed, the legacy custom GUID
  // may enumerate but reject CreateFileW. This is the documented scaffold
  // boundary, not a successful HID/Companion probe.
  if (sidebandProbe.value.status === 'error') {
    assert.equal(sidebandProbe.value.ok, false);
    assert.equal(sidebandProbe.value.operational, false);
    assert.match(sidebandProbe.value.message, /^CreateFileW\(.+\) failed/);
    assert.ok(sidebandProbe.value.paths?.length > 0);
    assert.equal(sidebandProbe.value.processExitCode, 2);
  } else assert.ok([
    'compatible', 'interface-not-found', 'access-denied', 'identity-mismatch',
    'timeout', 'unsupported-platform', 'python-unavailable', 'probe-error',
  ].includes(sidebandProbe.value.status), JSON.stringify(sidebandProbe.value));
  assert.equal(sidebandProbe.value.driverTouched, false);
  assert.equal(sidebandProbe.value.architecture, 'hid-minidriver-scaffold');
  assert.equal(sidebandProbe.value.operational, false);
  const companionState = await request(base, '/api/companion/state');
  assert.equal(companionState.response.status, 200);
  assert.equal(companionState.value.status, 'not-probed');
  assert.equal(companionState.value.opened, false);
  assert.equal(companionState.value.driverTouched, false);
  assert.equal(companionState.value.expected.reportId, 7);
  assert.equal(companionState.value.expected.reportLength, 64);
  const companionProbe = await request(base, '/api/companion/probe', 'POST', {});
  assert.equal(companionProbe.response.status, 200);
  assert.ok([
    'compatible', 'path-unavailable', 'interface-not-found',
    'hidapi-unavailable', 'enumeration-error', 'invalid-arguments',
  ].includes(companionProbe.value.status), JSON.stringify(companionProbe.value));
  assert.equal(companionProbe.value.opened, false);
  assert.equal(companionProbe.value.driverTouched, false);
  const apiState = await request(base, '/api/state');
  assert.equal(apiState.response.status, 200);
  assert.equal(apiState.value.server.schema, 'mirabox.codex.micro.webui');
  assert.equal(apiState.value.server.apiVersion, 2);
  assert.equal(apiState.value.server.port, port);
  assert.equal(apiState.value.server.pid, child.pid);
  assert.equal(apiState.value.server.buildId, 'capability-contract-v2');
  assert.equal(apiState.value.features.transportApi, true);
  assert.equal(apiState.value.features.nativeBridgeStatus, true);
  assert.equal(apiState.value.features.nativeBridgeControl, true);
  assert.equal(apiState.value.features.nativeOwnerHints, true);
  assert.equal(apiState.value.features.webhidLease, true);
  assert.equal(apiState.value.features.companionProbe, true);
  assert.equal(apiState.value.features.microFsRpc, true);
  assert.equal(apiState.value.features.compatibilityHandshake, true);
  assert.ok(Array.isArray(apiState.value.files.files));
  assert.ok(apiState.value.companion);
  assert.equal(apiState.value.companion.opened, false);
  assert.equal(apiState.value.nativeBridge.status, 'not-running');
  assert.equal(apiState.value.nativeBridge.n4.opened, false);

  const compatState = await request(base, '/api/compat/state');
  assert.equal(compatState.response.status, 200);
  assert.equal(compatState.value.files.files[0].name, 'keymap.json');
  const handshake = await request(base, '/api/compat/handshake', 'POST', {drain: true});
  assert.equal(handshake.response.status, 200);
  assert.equal(handshake.value.ok, true);
  assert.equal(handshake.value.checks.replyCount, 5);
  assert.equal(handshake.value.checks.keymapListed, true);
  assert.equal(handshake.value.checks.keymapReadAsObject, true);

  const nativeStatus = await request(base, '/api/native/status');
  assert.equal(nativeStatus.response.status, 200);
  assert.equal(nativeStatus.value.ok, true);
  assert.equal(nativeStatus.value.status, 'not-running');
  assert.equal(nativeStatus.value.control.managed, false);
  assert.equal(nativeStatus.value.webhidLease.active, false);
  const safeStop = await request(base, '/api/native/stop', 'POST', {});
  assert.equal(safeStop.response.status, 200);
  assert.equal(safeStop.value.stopped, false);
  assert.match(safeStop.value.message, /没有由当前 WebUI 启动/);
  const invalidTransport = await request(base, '/api/native/start', 'POST', {transport: 'other'});
  assert.equal(invalidTransport.response.status, 400);
  assert.equal(invalidTransport.value.error, 'invalid-transport');
  const invalidTarget = await request(base, '/api/native/start', 'POST', {visualTarget: 'wall'});
  assert.equal(invalidTarget.response.status, 400);
  assert.equal(invalidTarget.value.error, 'invalid-visual-target');
  const invalidLeaseAction = await request(base, '/api/native/lease', 'POST', {action: 'takeover'});
  assert.equal(invalidLeaseAction.response.status, 400);
  assert.equal(invalidLeaseAction.value.error, 'invalid-lease-action');
  const foreignOrigin = await fetch(`${base}/api/native/stop`, {
    method: 'POST',
    headers: {'content-type': 'application/json', origin: 'https://example.invalid'},
    body: '{}',
  });
  assert.equal(foreignOrigin.status, 403);
  const lease = await request(base, '/api/native/lease', 'POST', {action: 'claim', page: 'http://127.0.0.1/test'});
  assert.equal(lease.response.status, 200);
  assert.equal(lease.value.ok, true);
  assert.equal(typeof lease.value.token, 'string');
  assert.equal(lease.value.lease.active, true);
  const leaseConflict = await request(base, '/api/native/start', 'POST', {
    transport: 'hidapi', visuals: false, inputProfile: 'cpp', webuiUrl: base,
  });
  assert.equal(leaseConflict.response.status, 409);
  assert.equal(leaseConflict.value.error, 'n4-busy');
  assert.ok(leaseConflict.value.conflicts.some((item) => item.kind === 'webhid'));
  const release = await request(base, '/api/native/lease', 'POST', {action: 'release', token: lease.value.token});
  assert.equal(release.response.status, 200);
  assert.equal(release.value.lease.active, false);
  const nativeState = await request(base, '/api/native/state');
  assert.equal(nativeState.response.status, 200);
  assert.equal(nativeState.value.status, 'not-running');
  const nativeUpdate = await request(base, '/api/native/state', 'POST', {
    status: 'running',
    message: 'fake native bridge',
    pid: 1234,
      n4: {
      opened: true,
      ready: true,
      path: 'fake://n4',
      deviceIndex: 0,
      transport: 'hidapi-direct',
      usagePage: 0xFFA0,
      usage: 1,
      reportId: 0,
      reportLength: 512,
      readTimeoutMs: 100,
      readBufferSize: 1024,
      reads: 4,
      timeouts: 2,
      lastReadAt: '2026-09-07T00:00:00.000Z',
      lastError: null,
      devicePathRequested: 'fake://requested',
      initialized: true,
      reader: {threadAlive: true, runFlag: true, rawCallbackRegistered: true},
        heartbeat: {threadAlive: true, runFlag: true},
        externalOwnerHints: {
          available: true,
          status: 'detected',
          suspected: true,
          confidence: 'process-presence-only',
          checkedAt: '2026-09-07T00:00:00.000Z',
          knownProcessNames: ['StreamDock.exe', 'CefViewWing.exe'],
          processes: [{name: 'StreamDock.exe', pid: 44188, likelyOwner: true}],
          message: 'detected for test',
        },
      },
    counters: {reportsSeen: 2, reportsForwarded: 2},
    visual: {enabled: true, target: 'both', fetches: 3, updates: 1, errors: 1, lastPollError: 'poll timeout', uploads: 4},
  });
  assert.equal(nativeUpdate.response.status, 200);
  assert.equal(nativeUpdate.value.nativeBridge.status, 'running');
  assert.equal(nativeUpdate.value.nativeBridge.n4.ready, true);
  assert.equal(nativeUpdate.value.nativeBridge.n4.reader.rawCallbackRegistered, true);
  assert.equal(nativeUpdate.value.nativeBridge.n4.heartbeat.runFlag, true);
  assert.equal(nativeUpdate.value.nativeBridge.n4.externalOwnerHints.status, 'detected');
  assert.equal(nativeUpdate.value.nativeBridge.n4.externalOwnerHints.processes[0].pid, 44188);
  assert.equal(nativeUpdate.value.control?.managed, false);
  const nativeStatusWithHints = await request(base, '/api/native/status');
  assert.equal(nativeStatusWithHints.response.status, 200);
  assert.equal(nativeStatusWithHints.value.externalOwnerHints.status, 'detected');
  assert.equal(nativeUpdate.value.nativeBridge.n4.devicePathRequested, 'fake://requested');
  assert.equal(nativeUpdate.value.nativeBridge.n4.initialized, true);
  assert.equal(nativeUpdate.value.nativeBridge.n4.transport, 'hidapi-direct');
  assert.equal(nativeUpdate.value.nativeBridge.n4.reportLength, 512);
  assert.equal(nativeUpdate.value.nativeBridge.n4.readBufferSize, 1024);
  assert.equal(nativeUpdate.value.nativeBridge.n4.reads, 4);
  assert.equal(nativeUpdate.value.nativeBridge.counters.reportsSeen, 2);
  assert.equal(nativeUpdate.value.nativeBridge.visual.uploads, 4);
  assert.equal(nativeUpdate.value.nativeBridge.visual.enabled, true);
  assert.equal(nativeUpdate.value.nativeBridge.visual.target, 'both');
  assert.equal(nativeUpdate.value.nativeBridge.visual.lastPollError, 'poll timeout');

  const conflict = spawn(process.execPath, ['webui/server.cjs'], {
    cwd: project,
    env: {...process.env, MIRABOX_WEBUI_PORT: String(port)},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let conflictOutput = '';
  conflict.stdout.on('data', (chunk) => { conflictOutput += chunk.toString(); });
  conflict.stderr.on('data', (chunk) => { conflictOutput += chunk.toString(); });
  const conflictExit = Promise.race([
    once(conflict, 'exit'),
    new Promise((_, reject) => setTimeout(() => reject(new Error('conflicting server did not exit')), 3000)),
  ]);
  const [conflictCode] = await conflictExit;
  assert.equal(conflictCode, 1, conflictOutput);
  assert.match(conflictOutput, /already in use/i);
  assert.match(conflictOutput, new RegExp(String(port)));
  const thstatus = await request(base, '/api/rpc', 'POST', {
    method: 'v.oai.thstatus', params: [{id: 0, c: 0xff3366, b: 1, e: 'solid', s: 0}], id: 41,
  });
  assert.equal(thstatus.response.status, 200);
  assert.equal(thstatus.value.visual.lighting.agents[0].c, 0xff3366);
  const rgbcfg = await request(base, '/api/rpc', 'POST', {
    method: 'v.oai.rgbcfg', params: {keys: {c: 0x22cc88, b: .5, e: 'breath', s: .4}}, id: 42,
  });
  assert.equal(rgbcfg.response.status, 200);
  assert.equal(rgbcfg.value.visual.lighting.keys.c, 0x22cc88);

  const config = await request(base, '/api/config');
  assert.equal(config.response.status, 200);
  assert.equal(config.value.schema, 'mirabox.codex.micro');
  assert.equal(config.value.buttons.length, 14);
  assert.equal(config.value.knobs.length, 4);

  const rotation = await request(base, '/api/bridge/report', 'POST', {bytes: report(0xa1, 0)});
  assert.equal(rotation.response.status, 200);
  assert.deepEqual(rotation.value.events, [{method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2}}]);
  assert.ok(rotation.value.wireReports.length >= 1);

  // The raw-report replay contract must decode both the normal 512-byte body
  // and hidapi's 513-byte report-ID-prefixed form without changing offsets.
  const rawReplay = await request(base, '/api/raw', 'POST', {
    hex: Buffer.from(reportWithLeadingId(0xa1, 0)).toString('hex'),
  });
  assert.equal(rawReplay.response.status, 200);
  assert.equal(rawReplay.value.bytes, 513);
  assert.deepEqual(rawReplay.value.sdkEvent, {
    method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2},
  });
  assert.deepEqual(rawReplay.value.configuredEvents, [
    {method: 'v.oai.hid', params: {k: 'ENC_CW', act: 2}},
  ]);
  const rawForward = await request(base, '/api/bridge/report', 'POST', {
    bytes: reportWithLeadingId(0xa0, 0), reportId: 0,
  });
  assert.equal(rawForward.response.status, 200);
  assert.deepEqual(rawForward.value.events, [{method: 'v.oai.hid', params: {k: 'ENC_CC', act: 2}}]);
  assert.deepEqual(rawForward.value.sdkEvent, {
    method: 'v.oai.hid', params: {k: 'ENC_CC', act: 2},
  });

  const stream = openSse(base);
  t.after(() => stream.close());
  await stream.ready;
  // The initial SSE snapshot carries the same read-only companion state as
  // /api/state so a freshly opened diagnostics page can render immediately.
  const initialState = await new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = setInterval(() => {
      const state = stream.messages.find((item) => item.event === 'state');
      if (state) { clearInterval(timer); resolve(state); }
      else if (Date.now() - started > 1000) { clearInterval(timer); reject(new Error('SSE initial state timeout')); }
    }, 10);
  });
  assert.equal(initialState.data.companion.opened, false);
  const press = await request(base, '/api/bridge/hardware', 'POST', {hardwareCode: 0x37, state: 1});
  assert.equal(press.response.status, 200);
  assert.deepEqual(press.value.events, [{method: 'v.oai.hid', params: {k: 'ENC', act: 1}}]);
  const messages = await Promise.race([
    stream.done,
    new Promise((_, reject) => setTimeout(() => reject(new Error('SSE synthetic release timeout')), 2000)),
  ]);
  const micro = messages.filter((item) => item.event === 'micro-event').map((item) => item.data.event?.params?.act);
  assert.ok(micro.includes(1), `SSE should include press; output=${output}`);
  assert.ok(micro.includes(0), `SSE should include synthetic release; output=${output}`);
});
