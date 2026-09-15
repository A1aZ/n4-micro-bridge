'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn, execFile} = require('node:child_process');
const {nativeSpawnOptions, observeNativeChild} = require('../src/native-child.cjs');
const themeCatalog = require('../assets/themes/catalog.json');
const {imageName,managedProcessIds} = require('../src/native-process-info.cjs');
const {promisify} = require('node:util');
const {
  IDENTITY,
  MicroEndpoint,
  encode,
  inputEvent,
} = require('../src/micro-protocol.cjs');
const {
  defaultConfig,
  normalizeConfig,
  parseConfig,
  serializeConfig,
  mapN4ReportToWire,
} = require('../src/micro-config.cjs');
const {decodeN4Report} = require('../src/n4-input.cjs');
const {MicroBridge} = require('../src/micro-bridge.cjs');
const {MicroTransportHub} = require('../src/micro-transport.cjs');
const {MinuteVisualModelCache} = require('../src/visual-model-cache.cjs');
const {
  createLightingState,
  applyMicroMessage,
  createN4RenderModel,
  normalizeClock,
  renderPreviewHtml,
  buildSdkImagePlan,
} = require('../src/n4-visuals.cjs');

const root = __dirname;
const projectRoot = path.resolve(root, '..');
const nativeBridgeScript = path.join(projectRoot, 'scripts', 'n4-webui-bridge.py');
const nativeSdkPath = path.join(projectRoot, 'upstream', 'StreamDock-Device-SDK', 'Python-SDK', 'src');
const execFileAsync = promisify(execFile);
const NATIVE_LEASE_TTL_MS = 5000;
const NATIVE_OUTPUT_TAIL_MAX = 32 * 1024;
const configFile = process.env.MIRABOX_WEBUI_CONFIG || path.join(__dirname, '..', 'data', 'config.json');
const requestedPort = Number(process.env.MIRABOX_WEBUI_PORT || 8787);
if (!Number.isInteger(requestedPort) || requestedPort < 1 || requestedPort > 65535) {
  throw new Error('MIRABOX_WEBUI_PORT must be an integer between 1 and 65535');
}
const port = requestedPort;
const serverStartedAt = new Date().toISOString();
const SERVER_IDENTITY = Object.freeze({
  schema: 'mirabox.codex.micro.webui',
  apiVersion: 2,
  buildId: 'capability-contract-v2',
});
const SERVER_FEATURES = Object.freeze({
  bridgeApi: true,
  transportApi: true,
  visualApi: true,
  eventStream: true,
  dynamicInputMapping: true,
  syntheticEncoderRelease: true,
  nativeBridgeStatus: true,
  nativeBridgeControl: true,
  nativeOwnerHints: true,
  webhidLease: true,
  sidebandProbe: true,
  companionProbe: true,
  microFsRpc: true,
  compatibilityHandshake: true,
  visualThemes: true,
  independentKnobs: true,
  informationStrip: true,
});
const endpoint = new MicroEndpoint();
const subscribers = new Set();

function serverSnapshot() {
  return {
    ...SERVER_IDENTITY,
    pid: process.pid,
    port,
    startedAt: serverStartedAt,
  };
}

function capabilitiesSnapshot() {
  return {...SERVER_FEATURES};
}

function loadPersistedConfig(file) {
  try {
    return parseConfig(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return defaultConfig();
    // Keep the server usable if an older/hand-edited file is broken. The
    // configuration page can replace it with a validated document.
    console.warn(`Ignoring invalid config ${file}: ${error.message}`);
    return defaultConfig();
  }
}

function savePersistedConfig(file, value) {
  const normalized = normalizeConfig(value);
  fs.mkdirSync(path.dirname(file), {recursive: true});
  const temporary = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, `${serializeConfig(normalized)}\n`, 'utf8');
  try {
    fs.renameSync(temporary, file);
  } catch (error) {
    // Windows does not always replace an existing destination with rename.
    if (error.code !== 'EEXIST' && error.code !== 'EPERM') throw error;
    fs.rmSync(file, {force: true});
    fs.renameSync(temporary, file);
  }
  return normalized;
}

let config = loadPersistedConfig(configFile);
let bridge;
let visualLighting = createLightingState();
let visualModel = null;
let visualModelCache = null;
let screenTest = {mode:'off',revision:0,expiresAt:0};
function screenTestSnapshot(){
  if(screenTest.mode!=='off' && Date.now()>=screenTest.expiresAt)screenTest={mode:'off',revision:screenTest.revision+1,expiresAt:0};
  return {...screenTest};
}
let transport;
const sidebandProbeScript = path.join(root, '..', 'scripts', 'codexmicro-probe.py');
const companionProbeScript = path.join(root, '..', 'scripts', 'codexmicro-companion-probe.py');
let sidebandProbe = {
  ok: false,
  status: 'not-probed',
  message: '尚未执行 sideband 只读探测',
  driverTouched: false,
  // The provisional GUID/IOCTL path lives on the HID minidriver scaffold.
  // Keep this explicit in the initial WebUI state so an unprobed page cannot
  // be mistaken for an end-to-end companion-device check.
  architecture: 'hid-minidriver-scaffold',
  operational: false,
  architectureWarning: '当前自定义 IOCTL 仅为 HID minidriver 合同 scaffold；需要独立 companion control device，GET_INFO 不等于 Codex HID 已可达。',
  interfaceFound: false,
  identityCompatible: false,
  paths: [],
  path: null,
  info: null,
  compatibilityErrors: [],
};
let sidebandProbePromise = null;
let companionProbe = {
  ok: false,
  status: 'not-probed',
  message: '尚未执行 Companion HID 只读枚举',
  platform: process.platform,
  driverTouched: false,
  opened: false,
  enumerated: false,
  hidapiAvailable: false,
  interfaceFound: false,
  identityCompatible: false,
  pathReady: false,
  expected: {
    vendorId: 0x303A,
    productId: 0x8360,
    usagePage: 0xFF70,
    usage: 1,
    reportId: 7,
    reportLength: 64,
  },
  candidates: [],
  matches: [],
  paths: [],
  path: null,
  candidateCount: 0,
  matchCount: 0,
};
let companionProbePromise = null;

// Best-effort status cache for the optional Python native bridge.  The bridge
// deliberately reports into the WebUI instead of the WebUI spawning it: this
// keeps USB ownership explicit and makes it possible to run the browser
// WebHID path and the native SDK path as mutually exclusive test modes.
let nativeBridge = {
  status: 'not-running',
  message: '尚未启动 Python 原生 N4 桥接',
  pid: null,
  process: 'n4-webui-bridge',
  startedAt: null,
  stoppedAt: null,
  lastSeenAt: null,
  stale: true,
  ageMs: null,
  webuiUrl: null,
  n4: {
    transport: null,
    opened: false,
    ready: false,
    path: null,
    vendorId: 0x6602,
    productId: 0x1001,
    usagePage: 0xFFA0,
    usage: 1,
    reportId: 0,
    reportLength: 512,
    readTimeoutMs: null,
    readBufferSize: null,
    reads: 0,
    timeouts: 0,
    lastReadAt: null,
    lastError: null,
    deviceIndex: 0,
    devicePathRequested: null,
    initialized: false,
    openMode: null,
    transportDetails: null,
    externalOwnerHints: null,
    reader: {
      threadAlive: false,
      runFlag: false,
      rawCallbackRegistered: false,
    },
    heartbeat: {
      threadAlive: false,
      runFlag: false,
    },
  },
  counters: {
    reportsSeen: 0,
    reportsForwarded: 0,
    reportsIgnored: 0,
    httpPosts: 0,
    httpFailures: 0,
  },
  visual: {
    enabled: false,
    target: null,
    fetches: 0,
    updates: 0,
    errors: 0,
    lastError: null,
    lastPollError: null,
    uploads: 0,
  },
  lastError: null,
  driverTouched: false,
};

// The optional native bridge is intentionally managed as a child of this
// WebUI only when the user explicitly clicks "启动".  A process started in a
// terminal (or by StreamDock) is never adopted or terminated by these fields.
let nativeBridgeProcess = null;
let nativeStartPromise = null;
let nativeStopPromise = null;
let nativeControl = {
  status: 'not-running',
  managed: false,
  pid: null,
  transport: 'sdk',
  visuals: true,
  visualTarget: 'both',
  inputProfile: 'cpp',
  command: null,
  startedAt: null,
  stoppedAt: null,
  exitCode: null,
  signal: null,
  outputTail: '',
  lastError: null,
  conflicts: [],
};
let webhidLease = {
  active: false,
  token: null,
  lastSeenAt: null,
  page: null,
};

function sidebandProbeSnapshot() {
  return {...sidebandProbe};
}

function companionProbeSnapshot() {
  return {...companionProbe};
}

function nativeBridgeSnapshot() {
  const value = {
    ...nativeBridge,
    n4: {
      ...nativeBridge.n4,
      reader: {...(nativeBridge.n4.reader || {})},
      heartbeat: {...(nativeBridge.n4.heartbeat || {})},
    },
    counters: {...nativeBridge.counters},
    visual: {...nativeBridge.visual},
  };
  const timestamp = value.lastSeenAt ? Date.parse(value.lastSeenAt) : NaN;
  if (Number.isFinite(timestamp)) {
    value.ageMs = Math.max(0, Date.now() - timestamp);
    value.stale = value.ageMs > 3500;
  } else {
    value.ageMs = null;
    value.stale = true;
  }
  return value;
}

function clearExpiredWebhidLease() {
  if (!webhidLease.active || !webhidLease.lastSeenAt) return false;
  const timestamp = Date.parse(webhidLease.lastSeenAt);
  if (!Number.isFinite(timestamp) || Date.now() - timestamp > NATIVE_LEASE_TTL_MS) {
    webhidLease = {active: false, token: null, lastSeenAt: null, page: null};
    return true;
  }
  return false;
}

function webhidLeaseSnapshot() {
  clearExpiredWebhidLease();
  const timestamp = webhidLease.lastSeenAt ? Date.parse(webhidLease.lastSeenAt) : NaN;
  return {
    active: Boolean(webhidLease.active),
    lastSeenAt: webhidLease.lastSeenAt,
    ageMs: Number.isFinite(timestamp) ? Math.max(0, Date.now() - timestamp) : null,
    expiresInMs: Number.isFinite(timestamp)
      ? Math.max(0, NATIVE_LEASE_TTL_MS - (Date.now() - timestamp))
      : null,
    page: webhidLease.page,
  };
}

function nativeControlSnapshot() {
  return {
    ...nativeControl,
    command: Array.isArray(nativeControl.command) ? [...nativeControl.command] : nativeControl.command,
    conflicts: Array.isArray(nativeControl.conflicts)
      ? nativeControl.conflicts.map((item) => ({...item}))
      : [],
  };
}

// Keep the flat native bridge fields for existing clients while exposing the
// WebUI-owned process and WebHID lease state additively.
function nativeStatusSnapshot() {
  const native = nativeBridgeSnapshot();
  return {
    ok: true,
    ...native,
    nativeBridge: native,
    externalOwnerHints: native.n4?.externalOwnerHints || null,
    control: nativeControlSnapshot(),
    conflicts: nativeControlSnapshot().conflicts,
    webhidLease: webhidLeaseSnapshot(),
  };
}

function numberOr(value, fallback, {min = 0, max = Number.MAX_SAFE_INTEGER} = {}) {
  const number = Number(value);
  return Number.isFinite(number) && Number.isInteger(number)
    ? Math.max(min, Math.min(max, number))
    : fallback;
}

function textOr(value, fallback, maxLength = 1024) {
  if (value == null) return fallback;
  const text = String(value);
  return text.length > maxLength ? text.slice(0, maxLength) : text;
}

function sanitizeNativeTransportDetails(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const numberOrNull = (input, {min = 0, max = Number.MAX_SAFE_INTEGER} = {}) => {
    if (input == null) return null;
    const number = Number(input);
    return Number.isInteger(number) && number >= min && number <= max ? number : null;
  };
  return {
    present: has(value, 'present') ? Boolean(value.present) : false,
    handlePresent: has(value, 'handlePresent') ? Boolean(value.handlePresent) : false,
    isOpen: has(value, 'isOpen') ? Boolean(value.isOpen) : false,
    inputReportSize: numberOrNull(value.inputReportSize, {max: 65536}),
    outputReportSize: numberOrNull(value.outputReportSize, {max: 65536}),
    featureReportSize: numberOrNull(value.featureReportSize, {max: 65536}),
    reportId: numberOrNull(value.reportId, {max: 0xff}),
    readBufferSize: numberOrNull(value.readBufferSize, {max: 65536}),
    lastError: has(value, 'lastError')
      ? (value.lastError == null ? null : textOr(value.lastError, null, 2048))
      : null,
  };
}

// The Python reader deliberately reports process-name hints only.  Keep the
// WebUI contract equally conservative: no command lines or handles cross the
// boundary, and malformed/oversized values are reduced to a small JSON-safe
// shape before they are echoed through REST/SSE.
function sanitizeExternalOwnerHints(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const names = Array.isArray(value.knownProcessNames)
    ? value.knownProcessNames
      .map((item) => textOr(item, '', 128))
      .filter(Boolean)
      .slice(0, 16)
    : [];
  const processes = Array.isArray(value.processes)
    ? value.processes.map((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
      const pid = item.pid == null ? null : numberOr(item.pid, null, {min: 0, max: 0x7fffffff});
      return {
        name: textOr(item.name, '', 128),
        pid,
        likelyOwner: Boolean(item.likelyOwner),
      };
    }).filter((item) => item && item.name).slice(0, 32)
    : [];
  return {
    available: Boolean(value.available),
    status: textOr(value.status, 'unknown', 40),
    suspected: Boolean(value.suspected),
    confidence: textOr(value.confidence, 'process-presence-only', 80),
    checkedAt: has(value, 'checkedAt')
      ? (value.checkedAt == null ? null : textOr(value.checkedAt, null, 80))
      : null,
    knownProcessNames: names,
    processes,
    message: textOr(value.message, '', 2048),
  };
}

function mergeNativeBridgeStatus(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new Error('native status must be an object');
  }
  const patch = input.nativeBridge && typeof input.nativeBridge === 'object'
    ? input.nativeBridge
    : input;
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const next = {
    ...nativeBridge,
    status: textOr(patch.status, nativeBridge.status, 40),
    message: textOr(patch.message, nativeBridge.message, 2048),
    pid: patch.pid == null ? nativeBridge.pid : numberOr(patch.pid, nativeBridge.pid, {min: 0, max: 0x7fffffff}),
    process: textOr(patch.process, nativeBridge.process, 128),
    startedAt: patch.startedAt == null ? nativeBridge.startedAt : textOr(patch.startedAt, nativeBridge.startedAt, 80),
    stoppedAt: has(patch, 'stoppedAt') ? (patch.stoppedAt == null ? null : textOr(patch.stoppedAt, null, 80)) : nativeBridge.stoppedAt,
    lastSeenAt: patch.lastSeenAt == null ? new Date().toISOString() : textOr(patch.lastSeenAt, nativeBridge.lastSeenAt, 80),
    webuiUrl: patch.webuiUrl == null ? nativeBridge.webuiUrl : textOr(patch.webuiUrl, nativeBridge.webuiUrl, 2048),
    lastError: has(patch, 'lastError') ? (patch.lastError == null ? null : textOr(patch.lastError, null, 2048)) : nativeBridge.lastError,
    driverTouched: has(patch, 'driverTouched') ? Boolean(patch.driverTouched) : nativeBridge.driverTouched,
  };
  if (patch.n4 && typeof patch.n4 === 'object' && !Array.isArray(patch.n4)) {
    next.n4 = {
      ...nativeBridge.n4,
      transport: has(patch.n4, 'transport')
        ? (patch.n4.transport == null
          ? null
          : (typeof patch.n4.transport === 'object'
            ? (nativeBridge.n4.transport || 'streamdock-sdk')
            : textOr(patch.n4.transport, null, 40)))
        : nativeBridge.n4.transport,
      opened: patch.n4.opened == null ? nativeBridge.n4.opened : Boolean(patch.n4.opened),
      ready: patch.n4.ready == null ? nativeBridge.n4.ready : Boolean(patch.n4.ready),
      path: has(patch.n4, 'path') ? (patch.n4.path == null ? null : textOr(patch.n4.path, null, 2048)) : nativeBridge.n4.path,
      vendorId: numberOr(patch.n4.vendorId, nativeBridge.n4.vendorId, {min: 0, max: 0xffff}),
      productId: numberOr(patch.n4.productId, nativeBridge.n4.productId, {min: 0, max: 0xffff}),
      usagePage: numberOr(patch.n4.usagePage, nativeBridge.n4.usagePage, {min: 0, max: 0xffff}),
      usage: numberOr(patch.n4.usage, nativeBridge.n4.usage, {min: 0, max: 0xffff}),
      reportId: numberOr(patch.n4.reportId, nativeBridge.n4.reportId, {min: 0, max: 0xff}),
      reportLength: numberOr(patch.n4.reportLength, nativeBridge.n4.reportLength, {min: 1, max: 65536}),
      readTimeoutMs: patch.n4.readTimeoutMs == null
        ? nativeBridge.n4.readTimeoutMs
        : numberOr(patch.n4.readTimeoutMs, nativeBridge.n4.readTimeoutMs, {min: 1, max: 5000}),
      readBufferSize: patch.n4.readBufferSize == null
        ? nativeBridge.n4.readBufferSize
        : numberOr(patch.n4.readBufferSize, nativeBridge.n4.readBufferSize, {min: 11, max: 65536}),
      reads: numberOr(patch.n4.reads, nativeBridge.n4.reads),
      timeouts: numberOr(patch.n4.timeouts, nativeBridge.n4.timeouts),
      lastReadAt: has(patch.n4, 'lastReadAt')
        ? (patch.n4.lastReadAt == null ? null : textOr(patch.n4.lastReadAt, null, 80))
        : nativeBridge.n4.lastReadAt,
      lastError: has(patch.n4, 'lastError')
        ? (patch.n4.lastError == null ? null : textOr(patch.n4.lastError, null, 2048))
        : nativeBridge.n4.lastError,
      deviceIndex: numberOr(patch.n4.deviceIndex, nativeBridge.n4.deviceIndex, {min: 0, max: 0x7fffffff}),
      devicePathRequested: has(patch.n4, 'devicePathRequested')
        ? (patch.n4.devicePathRequested == null ? null : textOr(patch.n4.devicePathRequested, null, 2048))
        : nativeBridge.n4.devicePathRequested,
      initialized: has(patch.n4, 'initialized') ? Boolean(patch.n4.initialized) : nativeBridge.n4.initialized,
      openMode: has(patch.n4, 'openMode')
        ? (patch.n4.openMode == null ? null : textOr(patch.n4.openMode, null, 80))
        : nativeBridge.n4.openMode,
      transportDetails: has(patch.n4, 'transportDetails')
        ? (patch.n4.transportDetails == null ? null : sanitizeNativeTransportDetails(patch.n4.transportDetails))
        : nativeBridge.n4.transportDetails,
      externalOwnerHints: has(patch.n4, 'externalOwnerHints')
        ? (patch.n4.externalOwnerHints == null ? null : sanitizeExternalOwnerHints(patch.n4.externalOwnerHints))
        : nativeBridge.n4.externalOwnerHints,
      reader: {...nativeBridge.n4.reader},
      heartbeat: {...nativeBridge.n4.heartbeat},
    };
    if (patch.n4.localActions && typeof patch.n4.localActions === 'object' && !Array.isArray(patch.n4.localActions)) {
      const local=patch.n4.localActions;
      next.n4.localActions={executed:numberOr(local.executed,0),ignored:numberOr(local.ignored,0),errors:numberOr(local.errors,0),brightness:numberOr(local.brightness,100,{min:10,max:100}),brightnessApplied:local.brightnessApplied===true,lastAction:textOr(local.lastAction,null,60),lastReason:textOr(local.lastReason,null,500)};
    }
    if (patch.n4.reader && typeof patch.n4.reader === 'object' && !Array.isArray(patch.n4.reader)) {
      next.n4.reader = {
        threadAlive: patch.n4.reader.threadAlive == null ? nativeBridge.n4.reader.threadAlive : Boolean(patch.n4.reader.threadAlive),
        runFlag: patch.n4.reader.runFlag == null ? nativeBridge.n4.reader.runFlag : Boolean(patch.n4.reader.runFlag),
        rawCallbackRegistered: patch.n4.reader.rawCallbackRegistered == null
          ? nativeBridge.n4.reader.rawCallbackRegistered
          : Boolean(patch.n4.reader.rawCallbackRegistered),
      };
    }
    if (patch.n4.heartbeat && typeof patch.n4.heartbeat === 'object' && !Array.isArray(patch.n4.heartbeat)) {
      next.n4.heartbeat = {
        threadAlive: patch.n4.heartbeat.threadAlive == null ? nativeBridge.n4.heartbeat.threadAlive : Boolean(patch.n4.heartbeat.threadAlive),
        runFlag: patch.n4.heartbeat.runFlag == null ? nativeBridge.n4.heartbeat.runFlag : Boolean(patch.n4.heartbeat.runFlag),
      };
    }
    // A live WebUI can receive a new bridge heartbeat after switching from
    // the SDK transport to the direct hidapi fallback (or vice versa).  Do
    // not carry transport-specific diagnostics across that boundary when
    // the new snapshot does not provide a field.
    if (has(patch.n4, 'transport') && next.n4.transport !== nativeBridge.n4.transport) {
      if (!has(patch.n4, 'openMode')) next.n4.openMode = null;
      if (!has(patch.n4, 'transportDetails')) next.n4.transportDetails = null;
      if (!has(patch.n4, 'readBufferSize')) next.n4.readBufferSize = null;
      if (!has(patch.n4, 'readTimeoutMs')) next.n4.readTimeoutMs = null;
      if (!has(patch.n4, 'initialized')) next.n4.initialized = false;
      if (!has(patch.n4, 'reader')) {
        next.n4.reader = {threadAlive: false, runFlag: false, rawCallbackRegistered: false};
      }
      if (!has(patch.n4, 'heartbeat')) {
        next.n4.heartbeat = {threadAlive: false, runFlag: false};
      }
    }
  }
  if (patch.counters && typeof patch.counters === 'object' && !Array.isArray(patch.counters)) {
    next.counters = Object.fromEntries(Object.keys(nativeBridge.counters).map((key) => [
      key,
      numberOr(patch.counters[key], nativeBridge.counters[key]),
    ]));
    if (patch.counters.http && typeof patch.counters.http === 'object') {
      next.counters.httpPosts = numberOr(patch.counters.http.posts, next.counters.httpPosts);
      next.counters.httpFailures = numberOr(patch.counters.http.failures, next.counters.httpFailures);
    }
  }
  if (patch.visual && typeof patch.visual === 'object' && !Array.isArray(patch.visual)) {
    next.visual = {
      ...nativeBridge.visual,
      enabled: has(patch.visual, 'enabled') ? Boolean(patch.visual.enabled) : nativeBridge.visual.enabled,
      target: has(patch.visual, 'target')
        ? (patch.visual.target == null ? null : textOr(patch.visual.target, null, 40))
        : nativeBridge.visual.target,
      fetches: numberOr(patch.visual.fetches, nativeBridge.visual.fetches),
      updates: numberOr(patch.visual.updates, nativeBridge.visual.updates),
      errors: numberOr(patch.visual.errors, nativeBridge.visual.errors),
      lastError: has(patch.visual, 'lastError')
        ? (patch.visual.lastError == null ? null : textOr(patch.visual.lastError, null, 2048))
        : nativeBridge.visual.lastError,
      lastPollError: has(patch.visual, 'lastPollError')
        ? (patch.visual.lastPollError == null ? null : textOr(patch.visual.lastPollError, null, 2048))
        : nativeBridge.visual.lastPollError,
      uploads: numberOr(patch.visual.uploads, nativeBridge.visual.uploads),
    };
  }
  const brightnessChanged=JSON.stringify(nativeBridge.n4.localActions)!==JSON.stringify(next.n4.localActions);
  nativeBridge = next;
  if(brightnessChanged)refreshVisualModel();
  const snapshot = nativeBridgeSnapshot();
  publish('native-status', nativeStatusSnapshot());
  return snapshot;
}

class HttpError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function publishNativeStatus() {
  const snapshot = nativeStatusSnapshot();
  publish('native-status', snapshot);
  return snapshot;
}

function setNativeControl(patch, {publishEvent = true} = {}) {
  nativeControl = {...nativeControl, ...(patch || {})};
  if (publishEvent) publishNativeStatus();
  return nativeControlSnapshot();
}

function appendNativeOutput(stream, chunk) {
  const text = String(chunk ?? '');
  if (!text) return;
  const prefix = stream === 'stderr' ? '[stderr] ' : '[stdout] ';
  nativeControl.outputTail = `${nativeControl.outputTail}${prefix}${text}`.slice(-NATIVE_OUTPUT_TAIL_MAX);
  publish('native-process', {
    stream,
    text: text.slice(-8192),
    control: nativeControlSnapshot(),
    at: new Date().toISOString(),
  });
}

function pathIsInside(base, candidate) {
  const basePath = path.resolve(base);
  const candidatePath = path.resolve(candidate);
  const relative = path.relative(basePath, candidatePath);
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function resolveProjectPython() {
  const requested = process.env.MIRABOX_PYTHON;
  const candidates = requested
    ? [requested]
    : [
      path.join(projectRoot, '.hidapi-venv', 'Scripts', 'python.exe'),
      path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
      path.join(projectRoot, '.hidapi-venv', 'bin', 'python'),
      path.join(projectRoot, '.venv', 'bin', 'python'),
    ];
  for (const candidate of candidates) {
    const resolved = path.isAbsolute(candidate) ? path.resolve(candidate) : path.resolve(projectRoot, candidate);
    if (!pathIsInside(projectRoot, resolved) || !fs.existsSync(resolved)) continue;
    try {
      if (fs.statSync(resolved).isFile()) return resolved;
    } catch (_) { /* race with a removed environment; try the next candidate */ }
  }
  throw new HttpError(
    503,
    'native-python-unavailable',
    '未找到项目内 Python；请先创建 .hidapi-venv，或设置 MIRABOX_PYTHON 指向项目目录内的 python.exe',
  );
}

function parseStrictBoolean(value, fallback, label) {
  if (value == null) return fallback;
  if (typeof value === 'boolean') return value;
  if (value === 0 || value === 1) return Boolean(value);
  const text = String(value).trim().toLowerCase();
  if (text === 'true' || text === '1' || text === 'yes') return true;
  if (text === 'false' || text === '0' || text === 'no') return false;
  throw new HttpError(400, 'invalid-arguments', `${label} 必须是布尔值`);
}

function localWebUiOrigin(value) {
  const expected = `http://127.0.0.1:${port}`;
  if (value == null || String(value).trim() === '') return expected;
  let parsed;
  try { parsed = new URL(String(value)); } catch (_) {
    throw new HttpError(400, 'invalid-arguments', 'webuiUrl 必须是本机 HTTP origin');
  }
  const localHost = parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost' || parsed.hostname === '::1';
  if (parsed.protocol !== 'http:' || !localHost || parsed.port !== String(port) || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new HttpError(400, 'invalid-arguments', `webuiUrl 必须是当前本机 origin（${expected}）`);
  }
  return expected;
}

function assertLocalControlOrigin(req) {
  const origin = req?.headers?.origin;
  if (!origin) return;
  try {
    const parsed = new URL(origin);
    const localHost = parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost' || parsed.hostname === '::1';
    if (parsed.protocol !== 'http:' || !localHost || parsed.port !== String(port)) {
      throw new Error('non-local origin');
    }
  } catch (_) {
    throw new HttpError(403, 'origin-not-allowed', '原生桥接控制接口只接受当前本机 WebUI origin');
  }
}

function normalizeNativeStartOptions(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new HttpError(400, 'invalid-arguments', '启动参数必须是 JSON 对象');
  }
  const transport = String(input.transport ?? 'sdk').trim().toLowerCase();
  if (!['sdk', 'hidapi'].includes(transport)) {
    throw new HttpError(400, 'invalid-transport', 'transport 只能是 sdk 或 hidapi');
  }
  const visuals = parseStrictBoolean(input.visuals ?? input.visual, true, 'visuals');
  const visualTarget = String(input.visualTarget ?? 'both').trim().toLowerCase();
  if (!['both', 'screen', 'keys'].includes(visualTarget)) {
    throw new HttpError(400, 'invalid-visual-target', 'visualTarget 只能是 both、screen 或 keys');
  }
  const inputProfile = String(input.inputProfile ?? 'cpp').trim().toLowerCase();
  if (!['cpp', 'python'].includes(inputProfile)) {
    throw new HttpError(400, 'invalid-input-profile', 'inputProfile 只能是 cpp 或 python');
  }
  if (transport === 'hidapi' && visuals) {
    throw new HttpError(400, 'hidapi-no-visuals', 'hidapi 是 input-only；请关闭 visuals（它不能写 N4 屏幕或按键图像）');
  }
  return {
    transport,
    visuals,
    visualTarget: visuals ? visualTarget : null,
    inputProfile,
    webuiUrl: localWebUiOrigin(input.webuiUrl),
  };
}

function nativeCommandFor(options, python) {
  const args = [nativeBridgeScript, '--live', '--webui-url', options.webuiUrl,
    '--input-profile', options.inputProfile, '--jsonl'];
  if (options.transport === 'hidapi') {
    args.splice(2, 0, '--hidapi-input', '--no-visuals');
  } else {
    args.push('--sdk-path', nativeSdkPath, '--visual-target', options.visualTarget || 'both');
    if (!options.visuals) args.push('--no-visuals');
  }
  return {python, args};
}

function processAlive(pid) {
  const value = Number(pid);
  if (!Number.isInteger(value) || value <= 0 || value === process.pid) return false;
  try {
    process.kill(value, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM';
  }
}

async function listProcessRows() {
  try {
    if (process.platform === 'win32') {
      const command = '$ErrorActionPreference="SilentlyContinue"; Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress';
      const result = await execFileAsync('powershell.exe', [
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command,
      ], {windowsHide: true, timeout: 2500, maxBuffer: 1024 * 1024});
      const parsed = JSON.parse(String(result.stdout || '[]').trim() || '[]');
      const rows = Array.isArray(parsed) ? parsed : [parsed];
      return rows.map((row) => ({pid: Number(row?.ProcessId), parentPid: Number(row?.ParentProcessId), name: String(row?.Name || ''), command: String(row?.CommandLine || '')}))
        .filter((row) => Number.isInteger(row.pid) && row.pid > 0);
    }
    const result = await execFileAsync('ps', ['-eo', 'pid=,args='], {timeout: 2500, maxBuffer: 1024 * 1024});
    return String(result.stdout || '').split(/\r?\n/).map((line) => {
      const match = line.trim().match(/^(\d+)\s+(.*)$/);
      return match ? {pid: Number(match[1]), command: match[2]} : null;
    }).filter(Boolean);
  } catch (_) {
    // Process diagnostics are best-effort. A missing PowerShell/ps utility
    // must not make a safe start impossible; WebHID lease and managed-child
    // checks still protect the common conflict paths.
    return [];
  }
}

function knownConflictReason(command) {
  const text = String(command || '');
  const lower = text.toLowerCase();
  // Do not classify the PowerShell process running our own read-only query;
  // its command line contains the script names as string literals.
  if (lower.includes('get-ciminstance') || lower.includes('convertto-json')) return null;
  for (const script of [
    'n4-webui-bridge.py', 'n4_webui_bridge.py', 'n4-sdk-capture.py',
    'n4-hidapi-capture.py', 'n4-native-adapter.py', 'n4_native_adapter.py',
  ]) {
    if (lower.includes(script)) {
      const origin = text.match(/--webui-url\s+["']?(https?:\/\/[^\s"']+)/i)?.[1];
      return origin ? `N4 已被设备助手 ${origin} 使用，请先在那里断开` : `检测到已运行的 N4 原生桥接脚本（${script}）`;
    }
  }
  if (imageName({command:text}) === 'streamdock.exe') return '检测到可能占用 N4 的 StreamDock 进程';
  return null;
}

async function collectNativeConflicts() {
  clearExpiredWebhidLease();
  const conflicts = [];
  const seen = new Set();
  const add = (value) => {
    const item = {
      kind: String(value.kind || 'process'),
      pid: value.pid == null ? null : Number(value.pid),
      command: textOr(value.command, '', 4096),
      reason: textOr(value.reason, '设备可能已被其他输入路径占用', 1024),
    };
    const key = `${item.kind}:${item.pid ?? ''}:${item.command}`;
    if (seen.has(key)) return;
    seen.add(key);
    conflicts.push(item);
  };
  if (webhidLease.active) {
    add({kind: 'webhid', pid: null, command: webhidLease.page || '浏览器 WebHID', reason: '当前页面持有 N4 WebHID lease；请先断开设备'});
  }
  if (nativeBridgeProcess && nativeBridgeProcess.exitCode == null) {
    add({kind: 'native-status', pid: nativeBridgeProcess.pid, command: nativeControl.command?.join(' ') || 'WebUI managed n4-webui-bridge', reason: '当前 WebUI 已经启动了一个原生桥接进程'});
  }
  const rows = await listProcessRows();
  const ownedPids = managedProcessIds(rows,nativeBridgeProcess?.pid);
  const streamdockRows = [];
  const cefRows = [];
  for (const row of rows) {
    if (row.pid === process.pid || ownedPids.has(row.pid)) continue;
    const lower = String(row.command || '').toLowerCase();
    // StreamDock launches a large CefViewWing process tree.  Reporting every
    // renderer as an independent conflict makes the UI unreadable and does
    // not improve the ownership signal, so retain the rows for one compact
    // aggregate below.
    if (imageName(row) === 'cefviewwing.exe') {
      cefRows.push(row);
      continue;
    }
    if (imageName(row) === 'streamdock.exe') {
      streamdockRows.push(row);
      continue;
    }
    const reason = knownConflictReason(row.command);
    if (reason) add({kind: 'process', pid: row.pid, command: row.command, reason});
  }
  if (streamdockRows.length) {
    // Prefer the actual StreamDock.exe row over a plugin/helper command line.
    const main = streamdockRows.find((row) => /[\\/"\s]streamdock\.exe(?:["\s]|$)/i.test(row.command))
      || streamdockRows[0];
    add({
      kind: 'process',
      pid: main.pid,
      command: main.command,
      reason: cefRows.length
        ? `检测到可能占用 N4 的 StreamDock 进程（另有 ${cefRows.length} 个 CefView 子进程）`
        : '检测到可能占用 N4 的 StreamDock 进程',
    });
  } else if (cefRows.length) {
    // CefViewWing can be left behind after StreamDock exits.  Keep one
    // representative row so the user gets a useful hint without a false
    // claim that every renderer owns the HID handle.
    const representative = cefRows[0];
    add({
      kind: 'process',
      pid: representative.pid,
      command: representative.command,
      reason: `检测到 CefViewWing 相关进程（共 ${cefRows.length} 个）；未确认其是否占用 N4`,
    });
  }
  const reportedPid = Number(nativeBridge.pid);
  if (['starting', 'opening', 'running', 'stopping'].includes(nativeBridge.status)
      && processAlive(reportedPid)
      && !ownedPids.has(reportedPid)) {
    add({kind: 'native-status', pid: reportedPid, command: nativeBridge.process || 'n4-webui-bridge', reason: 'WebUI 心跳报告了仍存活的外部原生桥接进程'});
  }
  nativeControl.conflicts = conflicts;
  return conflicts;
}

function conflictError(conflicts) {
  const descriptions = conflicts.map((item) => `${item.kind}${item.pid ? ` PID ${item.pid}` : ''}: ${item.reason}`);
  return new HttpError(409, 'n4-busy', descriptions.length
    ? `N4 当前可能被占用：${descriptions.join('；')}。请手动关闭占用程序后重试。`
    : 'N4 当前可能被占用；请手动关闭占用程序后重试。', {conflicts});
}

function randomLeaseToken() {
  return typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : crypto.randomBytes(18).toString('hex');
}

async function handleNativeLease(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new HttpError(400, 'invalid-arguments', 'lease 参数必须是 JSON 对象');
  }
  const action = String(input.action ?? input.op ?? 'claim').trim().toLowerCase();
  clearExpiredWebhidLease();
  if (!['claim', 'heartbeat', 'release'].includes(action)) {
    throw new HttpError(400, 'invalid-lease-action', 'lease action 只能是 claim、heartbeat 或 release');
  }
  if (action === 'claim') {
    if (webhidLease.active) {
      throw new HttpError(409, 'webhid-busy', '已有浏览器页面持有 N4 WebHID lease；请先断开该页面', {
        conflicts: [{kind: 'webhid', pid: null, command: webhidLease.page || '浏览器 WebHID', reason: '已有 lease'}],
      });
    }
    if (nativeBridgeProcess && nativeBridgeProcess.exitCode == null) {
      throw conflictError([{kind: 'native-status', pid: nativeBridgeProcess.pid, command: 'WebUI managed n4-webui-bridge', reason: '原生桥接正在运行'}]);
    }
    webhidLease = {
      active: true,
      token: randomLeaseToken(),
      lastSeenAt: new Date().toISOString(),
      page: textOr(input.page, '浏览器 WebHID', 512),
    };
    publishNativeStatus();
    return {ok: true, action, token: webhidLease.token, lease: webhidLeaseSnapshot()};
  }
  const token = String(input.token || '');
  if (!webhidLease.active || !token || token !== webhidLease.token) {
    throw new HttpError(403, 'invalid-lease', 'WebHID lease token 无效或已过期');
  }
  if (action === 'heartbeat') {
    webhidLease.lastSeenAt = new Date().toISOString();
    publishNativeStatus();
    return {ok: true, action, lease: webhidLeaseSnapshot()};
  }
  if (action === 'release') {
    webhidLease = {active: false, token: null, lastSeenAt: null, page: null};
    publishNativeStatus();
    return {ok: true, action, lease: webhidLeaseSnapshot()};
  }
}

function nativeTransportLabel(transport) {
  return transport === 'hidapi' ? 'hidapi-direct' : 'streamdock-sdk';
}

function mergeManagedNativeStatus(patch) {
  try {
    return mergeNativeBridgeStatus(patch);
  } catch (error) {
    nativeControl.lastError = error.message;
    publishNativeStatus();
    return nativeBridgeSnapshot();
  }
}

async function startNativeBridge(input = {}) {
  if (nativeStartPromise) return nativeStartPromise;
  nativeStartPromise = (async () => {
    const options = normalizeNativeStartOptions(input);
    if (nativeBridgeProcess && nativeBridgeProcess.exitCode == null) {
      throw conflictError([{
        kind: 'native-status',
        pid: nativeBridgeProcess.pid,
        command: nativeControl.command?.join(' ') || 'WebUI managed n4-webui-bridge',
        reason: '当前 WebUI 已经启动了一个原生桥接进程',
      }]);
    }
    const conflicts = await collectNativeConflicts();
    if (conflicts.length) throw conflictError(conflicts);
    if (!fs.existsSync(nativeBridgeScript)) {
      throw new HttpError(500, 'native-script-missing', `找不到项目内桥接脚本：${nativeBridgeScript}`);
    }
    const python = resolveProjectPython();
    const command = nativeCommandFor(options, python);
    let child;
    try {
      child = spawn(command.python, command.args, nativeSpawnOptions(projectRoot));
    } catch (error) {
      setNativeControl({
        status: 'error', managed: false, pid: null, command: [command.python, ...command.args],
        stoppedAt: new Date().toISOString(), lastError: error.message,
      });
      throw new HttpError(500, 'native-spawn-error', `启动原生桥接失败：${error.message}`);
    }
    nativeBridgeProcess = child;
    const startedAt = new Date().toISOString();
    const commandLine = [command.python, ...command.args];
    setNativeControl({
      status: 'starting',
      managed: true,
      pid: child.pid ?? null,
      transport: options.transport,
      visuals: options.visuals,
      visualTarget: options.visualTarget,
      inputProfile: options.inputProfile,
      command: commandLine,
      startedAt,
      stoppedAt: null,
      exitCode: null,
      signal: null,
      outputTail: '',
      lastError: null,
      conflicts: [],
    });
    mergeManagedNativeStatus({
      status: 'starting',
      message: 'WebUI 正在启动原生 N4 桥接',
      pid: child.pid ?? null,
      process: 'n4-webui-bridge',
      startedAt,
      stoppedAt: null,
      lastSeenAt: new Date().toISOString(),
      webuiUrl: options.webuiUrl,
      n4: {
        transport: nativeTransportLabel(options.transport),
        opened: false,
        ready: false,
        initialized: false,
        openMode: null,
        transportDetails: null,
      },
      visual: {enabled: options.visuals, target: options.visualTarget},
      lastError: null,
      driverTouched: false,
    });

    let stdoutRemainder = '';
    let finalized = false;
    const consumeJsonl = (chunk) => {
      stdoutRemainder += String(chunk || '');
      const lines = stdoutRemainder.split(/\r?\n/);
      stdoutRemainder = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const value = JSON.parse(line);
          if (value?.nativeBridge && typeof value.nativeBridge === 'object') mergeManagedNativeStatus(value.nativeBridge);
          else if (value && typeof value === 'object' && (value.status || value.n4 || value.counters || value.visual)) {
            mergeManagedNativeStatus(value);
          }
        } catch (_) { /* JSONL may contain human diagnostics; retain it in outputTail */ }
      }
    };
    const finalize = (code, signal, error) => {
      if (finalized) return;
      finalized = true;
      const stopping = nativeControl.status === 'stopping' || nativeControl.stopRequested === true;
      const failed = Boolean(error) || (!stopping && code !== 0 && code !== null);
      const message = error
        ? `原生桥接进程错误：${error.message || error}`
        : (failed ? `原生桥接进程已退出（exit=${code ?? 'unknown'}${signal ? `, signal=${signal}` : ''}）` : '原生桥接进程已停止');
      const at = new Date().toISOString();
      if (error) appendNativeOutput('stderr', `${message}\n`);
      if (nativeBridgeProcess === child) nativeBridgeProcess = null;
      setNativeControl({
        status: failed ? 'error' : 'stopped',
        managed: false,
        pid: null,
        stoppedAt: at,
        exitCode: code,
        signal: signal || null,
        lastError: failed ? message : null,
        stopRequested: undefined,
      });
      mergeManagedNativeStatus({
        status: failed ? 'error' : 'stopped',
        message,
        pid: null,
        stoppedAt: at,
        lastError: failed ? message : null,
        n4: {opened: false, ready: false},
      });
    };
    observeNativeChild(child, {
      isCurrent: () => !finalized && nativeBridgeProcess === child,
      getControl: () => nativeControl,
      onRunning: () => setNativeControl({status: 'running'}),
      onStdout: (chunk) => { appendNativeOutput('stdout', chunk); consumeJsonl(chunk); },
      onStderr: (chunk) => appendNativeOutput('stderr', chunk),
    });
    child.once('error', (error) => finalize(null, null, error));
    child.once('exit', (code, signal) => finalize(code, signal, null));
    return {
      ok: true,
      started: true,
      command: commandLine,
      control: nativeControlSnapshot(),
      nativeBridge: nativeBridgeSnapshot(),
    };
  })();
  try {
    return await nativeStartPromise;
  } finally {
    nativeStartPromise = null;
  }
}

function waitForNativeChild(child, timeoutMs) {
  if (!child || child.exitCode != null) return Promise.resolve(true);
  return new Promise((resolve) => {
    let finished = false;
    const done = (value) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => done(false), timeoutMs);
    child.once('exit', () => done(true));
    child.once('close', () => done(true));
  });
}

async function stopNativeBridge() {
  if (nativeStopPromise) return nativeStopPromise;
  nativeStopPromise = (async () => {
    const child = nativeBridgeProcess;
    if (!child || child.exitCode != null) {
      const value = setNativeControl({managed: false, pid: null}, {publishEvent: false});
      publishNativeStatus();
      return {
        ok: true,
        stopped: false,
        message: '没有由当前 WebUI 启动的原生桥接进程；未停止任何外部程序',
        control: value,
        nativeBridge: nativeBridgeSnapshot(),
      };
    }
    setNativeControl({status: 'stopping', stopRequested: true, lastError: null});
    mergeManagedNativeStatus({status: 'stopping', message: '正在停止 WebUI 启动的原生 N4 桥接'});
    try { child.kill('SIGTERM'); } catch (error) { appendNativeOutput('stderr', `停止请求失败：${error.message}\n`); }
    let exited = await waitForNativeChild(child, 2500);
    if (!exited && process.platform === 'win32' && child.pid) {
      // This PID came from the child object we created above. Never apply
      // taskkill to a PID discovered during conflict scanning.
      try {
        await execFileAsync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
          windowsHide: true, timeout: 3000, maxBuffer: 64 * 1024,
        });
      } catch (error) {
        appendNativeOutput('stderr', `taskkill 失败：${error.message}\n`);
      }
      exited = await waitForNativeChild(child, 1000);
    } else if (!exited) {
      try { child.kill('SIGKILL'); } catch (_) {}
      exited = await waitForNativeChild(child, 1000);
    }
    if (!exited && nativeBridgeProcess === child) {
      setNativeControl({status: 'error', managed: true, lastError: '原生桥接进程未能在停止超时内退出'});
      throw new HttpError(504, 'native-stop-timeout', '原生桥接进程未能在停止超时内退出；未尝试停止外部进程');
    }
    return {
      ok: true,
      stopped: true,
      control: nativeControlSnapshot(),
      nativeBridge: nativeBridgeSnapshot(),
    };
  })();
  try {
    return await nativeStopPromise;
  } finally {
    nativeStopPromise = null;
  }
}

function classifyProbeProcessError(error) {
  if (error?.code === 'ENOENT') {
    return {status: 'python-unavailable', message: '找不到 Python；可设置 MIRABOX_PYTHON 指向 python.exe'};
  }
  return {status: 'probe-error', message: error?.message || String(error)};
}

function parseProbeJson(stdout) {
  const lines = String(stdout || '').trim().split(/\r?\n/).filter(Boolean);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const value = JSON.parse(lines[index]);
      if (value && typeof value === 'object') return value;
    } catch (_) { /* ignore non-JSON diagnostics */ }
  }
  return null;
}

function runSidebandProbe(options = {}) {
  if (sidebandProbePromise) return sidebandProbePromise;
  const requestedTimeout = Number(options.timeoutMs ?? 5000);
  const timeoutMs = Number.isFinite(requestedTimeout)
    ? Math.max(100, Math.min(30000, Math.trunc(requestedTimeout)))
    : 5000;
  const explicitPath = options.sidebandPath == null ? '' : String(options.sidebandPath).trim();
  if (explicitPath.length > 1024) {
    return Promise.reject(new Error('sidebandPath is too long'));
  }
  const python = String(process.env.MIRABOX_PYTHON || process.env.PYTHON || 'python');
  const args = [sidebandProbeScript, '--timeout-ms', String(timeoutMs)];
  if (explicitPath) args.push('--sideband-path', explicitPath);
  sidebandProbe = {
    ...sidebandProbe,
    ok: false,
    status: 'probing',
    message: '正在执行只读 GET_INFO 探测…',
    checkedAt: new Date().toISOString(),
    driverTouched: false,
  };
  publish('sideband', sidebandProbeSnapshot());
  sidebandProbePromise = new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
    let settled = false;
    let timer;
    let child;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      const normalized = {
        ...sidebandProbe,
        ...(value && typeof value === 'object' ? value : {}),
        checkedAt: new Date().toISOString(),
        driverTouched: false,
      };
      if (!normalized.message && stderr.trim()) normalized.message = stderr.trim().split(/\r?\n/).filter(Boolean).at(-1) || '';
      sidebandProbe = normalized;
      sidebandProbePromise = null;
      publish('sideband', sidebandProbeSnapshot());
      resolve(sidebandProbeSnapshot());
    };
    try {
      child = spawn(python, args, {
        cwd: path.join(root, '..'),
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      finish(classifyProbeProcessError(error));
      return;
    }
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
      if (stdout.length > 128 * 1024) {
        try { child.kill(); } catch (_) {}
        finish({status: 'probe-error', message: 'probe 输出过大'});
      }
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
      if (stderr.length > 64 * 1024) stderr = stderr.slice(-64 * 1024);
    });
    child.once('error', (error) => finish(classifyProbeProcessError(error)));
    child.once('close', (code, signal) => {
      const parsed = parseProbeJson(stdout);
      if (parsed) {
        finish({...parsed, processExitCode: code, processSignal: signal || null});
      } else {
        finish({
          status: 'probe-error',
          message: stderr.trim() || `probe 未返回 JSON（exit=${code ?? 'unknown'}${signal ? `, signal=${signal}` : ''}）`,
          processExitCode: code,
          processSignal: signal || null,
        });
      }
    });
    timer = setTimeout(() => {
      try { child.kill(); } catch (_) {}
      finish({status: 'timeout', message: `sideband 探测超过 ${timeoutMs} ms`});
    }, timeoutMs + 2000);
  });
  return sidebandProbePromise;
}

function companionProbeArgument(value, label) {
  if (value == null || value === '') return null;
  const text = String(value).trim();
  let number;
  if (/^0x[0-9a-f]+$/i.test(text)) number = Number.parseInt(text.slice(2), 16);
  else if (/^\d+$/.test(text)) number = Number(text);
  else if (/^[0-9a-f]+$/i.test(text)) number = Number.parseInt(text, 16);
  else number = Number(text);
  if (!Number.isInteger(number) || number < 0 || number > 0xFFFF) {
    throw new Error(`${label} must be an integer between 0 and 65535`);
  }
  return String(number);
}

function runCompanionProbe(options = {}) {
  if (companionProbePromise) return companionProbePromise;
  const requestedTimeout = Number(options.timeoutMs ?? 5000);
  const timeoutMs = Number.isFinite(requestedTimeout)
    ? Math.max(100, Math.min(30000, Math.trunc(requestedTimeout)))
    : 5000;
  const python = String(process.env.MIRABOX_PYTHON || process.env.PYTHON || 'python');
  const args = [companionProbeScript];
  for (const [name, value, label] of [
    ['--vendor-id', options.vendorId, 'vendorId'],
    ['--product-id', options.productId, 'productId'],
    ['--usage-page', options.usagePage, 'usagePage'],
    ['--usage', options.usage, 'usage'],
  ]) {
    const argument = companionProbeArgument(value, label);
    if (argument != null) args.push(name, argument);
  }
  companionProbe = {
    ...companionProbe,
    ok: false,
    status: 'probing',
    message: '正在只读枚举 Companion HID；不会打开设备…',
    checkedAt: new Date().toISOString(),
    driverTouched: false,
    opened: false,
  };
  publish('companion', companionProbeSnapshot());
  companionProbePromise = new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
    let settled = false;
    let timer;
    let child;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      const normalized = {
        ...companionProbe,
        ...(value && typeof value === 'object' ? value : {}),
        checkedAt: new Date().toISOString(),
        driverTouched: false,
        opened: false,
      };
      if (!normalized.message && stderr.trim()) normalized.message = stderr.trim().split(/\r?\n/).filter(Boolean).at(-1) || '';
      companionProbe = normalized;
      companionProbePromise = null;
      publish('companion', companionProbeSnapshot());
      resolve(companionProbeSnapshot());
    };
    try {
      child = spawn(python, args, {
        cwd: path.join(root, '..'),
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      finish({
        status: error?.code === 'ENOENT' ? 'hidapi-unavailable' : 'enumeration-error',
        message: error?.code === 'ENOENT'
          ? '找不到 Python，无法加载 hidapi 执行只读枚举'
          : (error?.message || String(error)),
      });
      return;
    }
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
      if (stdout.length > 256 * 1024) {
        try { child.kill(); } catch (_) {}
        finish({status: 'enumeration-error', message: 'Companion probe 输出过大'});
      }
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
      if (stderr.length > 64 * 1024) stderr = stderr.slice(-64 * 1024);
    });
    child.once('error', (error) => finish({
      status: error?.code === 'ENOENT' ? 'hidapi-unavailable' : 'enumeration-error',
      message: error?.code === 'ENOENT'
        ? '找不到 Python，无法加载 hidapi 执行只读枚举'
        : (error?.message || String(error)),
    }));
    child.once('close', (code, signal) => {
      const parsed = parseProbeJson(stdout);
      if (parsed) {
        finish({...parsed, processExitCode: code, processSignal: signal || null});
      } else {
        finish({
          status: 'enumeration-error',
          message: stderr.trim() || `Companion probe 未返回 JSON（exit=${code ?? 'unknown'}${signal ? `, signal=${signal}` : ''}）`,
          processExitCode: code,
          processSignal: signal || null,
        });
      }
    });
    timer = setTimeout(() => {
      try { child.kill(); } catch (_) {}
      finish({status: 'enumeration-error', message: `Companion 只读枚举超过 ${timeoutMs} ms`});
    }, timeoutMs + 2000);
  });
  return companionProbePromise;
}

function json(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  });
  res.end(body);
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    let text = '';
    req.on('data', (chunk) => {
      text += chunk;
      if (text.length > 2 * 1024 * 1024) {
        reject(new Error('Request body too large'));
        req.destroy();
      }
    });
    req.on('end', () => {
      try { resolve(text ? JSON.parse(text) : {}); }
      catch (error) { reject(new Error(`Invalid JSON body: ${error.message}`)); }
    });
    req.on('error', reject);
  });
}

function hex(value) {
  return Buffer.from(value || []).toString('hex').replace(/(..)/g, '$1 ').trim();
}

function parseBytes(input) {
  if (Array.isArray(input)) {
    if (input.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) {
      throw new Error('bytes must contain integers between 0 and 255');
    }
    return Buffer.from(input);
  }
  const text = String(input ?? '').replace(/[^0-9a-f]/gi, '');
  if (text.length % 2) throw new Error('hex must contain complete bytes');
  if (!text) return Buffer.alloc(0);
  return Buffer.from(text, 'hex');
}

function parseReportValue(value) {
  if (Array.isArray(value) || typeof value === 'string') return parseBytes(value);
  if (value && typeof value === 'object') return parseBytes(value.hex ?? value.bytes);
  throw new Error('report must be a byte array, hex string, or {bytes|hex} object');
}

function parseReports(input) {
  if (Array.isArray(input?.reports)) {
    if (!input.reports.length) throw new Error('reports cannot be empty');
    return input.reports.map(parseReportValue);
  }
  return [parseReportValue(input)];
}

function wireReports(events) {
  return events.flatMap((event) => encode(event).map(hex));
}

function sse(res, event, value) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(value)}\n\n`);
}

function publish(event, value) {
  for (const client of [...subscribers]) {
    try { sse(client, event, value); }
    catch (_) { subscribers.delete(client); }
  }
}

function eventEnvelope(event, source = 'n4') {
  return {
    source,
    at: new Date().toISOString(),
    event,
    wireReports: wireReports([event]),
  };
}

function modelCache() {
  if (!visualModelCache) {
    visualModelCache = new MinuteVisualModelCache({
      clock: () => normalizeClock(),
      build: (phase, clock) => createN4RenderModel({
        config,
        lighting: visualLighting,
        phase,
        clock,
        localActions: nativeBridge.n4.localActions,
      }),
    });
  }
  return visualModelCache;
}

function refreshVisualModel(phase = visualModel?.phase ?? 0.5) {
  visualModel = modelCache().refresh(phase);
  return visualModel;
}

function currentVisualModel() {
  visualModel = modelCache().get(visualModel?.phase ?? 0.5);
  return visualModel;
}

// Preserve the concise render-model shape used by older pages while exposing
// a nested model for transport/RPC consumers that treat visual state as an
// envelope.  Both forms describe the same snapshot and remain JSON-safe.
function visualSnapshot() {
  // Checking the minute is cheap; the SVG model is reused until the clock
  // crosses a minute boundary or state/configuration explicitly invalidates it.
  const model = currentVisualModel();
  return {
    ...model,
    lighting: visualLighting,
    model,
    sdkPlan: buildSdkImagePlan(model),
  };
}

function applyVisualMessage(message, source = 'codex') {
  try {
    visualLighting = applyMicroMessage(visualLighting, message);
    refreshVisualModel();
    publish('visual', {
      source,
      at: new Date().toISOString(),
      lighting: visualLighting,
      model: visualModel,
    });
  } catch (error) {
    // A visual preview must never break the protocol path. The endpoint still
    // owns validation and replies; this event is only diagnostic/rendering.
    publish('visual-error', {source, at: new Date().toISOString(), error: error.message});
  }
}

function enqueueDeviceEvent(event, source = 'n4') {
  const queued = transport.enqueueDeviceMessage(event, source);
  publish('transport-input', {
    source,
    at: new Date().toISOString(),
    sequence: queued.sequence,
    message: queued.message,
    reports: queued.reports.map(hex),
    transport: transport.snapshot(),
  });
  return queued;
}

// Construct the transport before the bridge.  MicroBridge does not emit in its
// constructor today, but this order guarantees its onEvent callback can never
// observe an uninitialised transport if that implementation changes.
transport = new MicroTransportHub({
  endpoint,
  onHostMessage: ({source, message}) => applyVisualMessage(message, source),
});

bridge = new MicroBridge({
  config,
  onEvent: (event) => {
    enqueueDeviceEvent(event, 'n4');
    publish('micro-event', eventEnvelope(event));
    // The bridge keeps a queue for non-callback consumers. The WebUI uses
    // SSE, so clear the queue after publishing to avoid unbounded growth.
    bridge.drain();
  },
});

refreshVisualModel();

function bridgeResult(events, extra = {}) {
  return {
    ok: true,
    localActions: bridge.drainLocalActions(),
    events,
    wireReports: wireReports(events),
    bridge: bridge.snapshot(),
    transport: transport.snapshot(),
    ...extra,
  };
}

const server = http.createServer(async (req, res) => {
  const requestUrl = new URL(req.url, 'http://127.0.0.1');
  const route = requestUrl.pathname;
  try {
    if(route.startsWith('/api/')){
      const {assertLocalRequest,assertMutation}=require('../src/local-api-guard.cjs');
      assertLocalRequest(req,port);
      if(!['GET','HEAD','OPTIONS'].includes(req.method)||(route==='/api/transport/input'&&requestUrl.searchParams.get('drain')!=='0'))assertMutation(req);
    }
    if(req.method==='GET' && route==='/screen-test'){
      res.writeHead(200,{'content-type':'text/html; charset=utf-8','cache-control':'no-store'});
      return res.end(fs.readFileSync(path.join(root,'screen-test.html')));
    }
    if(req.method==='GET' && route==='/api/visual/screen-test')return json(res,200,screenTestSnapshot());
    if(req.method==='POST' && route==='/api/visual/screen-test'){
      assertLocalControlOrigin(req);
      const input=await parseBody(req);
      if(!['off','black','white','box'].includes(input.mode))return json(res,400,{error:'Invalid screen test mode'});
      screenTest={mode:input.mode,revision:screenTest.revision+1,expiresAt:input.mode==='off'?0:Date.now()+600000};
      return json(res,200,screenTestSnapshot());
    }
    if (req.method === 'GET' && route === '/button-test') {
      res.writeHead(200, {'content-type':'text/html; charset=utf-8','cache-control':'no-store'});
      return res.end(fs.readFileSync(path.join(root, 'button-test.html')));
    }
    if (req.method === 'GET' && route === '/api/themes') return json(res,200,themeCatalog);
    if (req.method === 'GET' && route === '/api/themes/preview') {
      const theme=requestUrl.searchParams.get('theme') || themeCatalog.default;
      const key=Number(requestUrl.searchParams.get('key') || 1);
      if (!themeCatalog.themes.some(item=>item.id===theme) || !Number.isInteger(key) || key<1 || key>14) return json(res,400,{error:'Invalid theme or key'});
      const model=createN4RenderModel({config:{...config,visual:{...config.visual,theme}},lighting:visualLighting,localActions:nativeBridge.n4.localActions});
      res.writeHead(200,{'content-type':'image/svg+xml; charset=utf-8','cache-control':'no-store'});
      return res.end(model.buttons[key-1].svg);
    }
    if (req.method === 'GET' && (route === '/' || route === '/index.html')) {
      const data = fs.readFileSync(path.join(root, 'index.html'));
      res.writeHead(200, {'content-type': 'text/html; charset=utf-8'});
      return res.end(data);
    }
    if (req.method === 'GET' && (route === '/real-n4' || route === '/real-n4.html')) {
      const data = fs.readFileSync(path.join(root, 'real-n4.html'));
      res.writeHead(200, {'content-type': 'text/html; charset=utf-8'});
      return res.end(data);
    }
    if (req.method === 'GET' && (route === '/config' || route === '/config.html')) {
      const data = fs.readFileSync(path.join(root, 'config.html'));
      res.writeHead(200, {'content-type': 'text/html; charset=utf-8'});
      return res.end(data);
    }
    if (req.method === 'GET' && (route === '/visual' || route === '/visual.html')) {
      const data = fs.readFileSync(path.join(root, 'visual.html'));
      res.writeHead(200, {'content-type': 'text/html; charset=utf-8'});
      return res.end(data);
    }

    if (req.method === 'GET' && route === '/api/state') {
      return json(res, 200, {
        server: serverSnapshot(),
        features: capabilitiesSnapshot(),
        identity: IDENTITY,
        slots: endpoint.slots,
        lighting: endpoint.lighting,
        files: endpoint.files.snapshot(),
        config,
        visual: visualSnapshot(),
        transport: transport.snapshot(),
        bridge: bridge.snapshot(),
        sideband: sidebandProbeSnapshot(),
        companion: companionProbeSnapshot(),
        nativeBridge: nativeBridgeSnapshot(),
        nativeControl: nativeControlSnapshot(),
      });
    }
    if (req.method === 'GET' && route === '/api/native/state') {
      return json(res, 200, nativeStatusSnapshot());
    }
    if (req.method === 'POST' && route === '/api/native/state') {
      const value = mergeNativeBridgeStatus(await parseBody(req));
      return json(res, 200, {
        ok: true,
        nativeBridge: value,
        control: nativeControlSnapshot(),
        conflicts: nativeControlSnapshot().conflicts,
      });
    }
    if (req.method === 'GET' && route === '/api/native/status') {
      await collectNativeConflicts();
      return json(res, 200, nativeStatusSnapshot());
    }
    if (req.method === 'POST' && route === '/api/native/start') {
      assertLocalControlOrigin(req);
      return json(res, 200, await startNativeBridge(await parseBody(req)));
    }
    if (req.method === 'POST' && route === '/api/native/stop') {
      assertLocalControlOrigin(req);
      return json(res, 200, await stopNativeBridge());
    }
    if (req.method === 'POST' && (route === '/api/native/lease' || route === '/api/n4/lease')) {
      assertLocalControlOrigin(req);
      return json(res, 200, await handleNativeLease(await parseBody(req)));
    }
    if (req.method === 'GET' && route === '/api/sideband/state') {
      return json(res, 200, sidebandProbeSnapshot());
    }
    if (req.method === 'POST' && route === '/api/sideband/probe') {
      const input = await parseBody(req);
      const value = await runSidebandProbe({
        sidebandPath: input.sidebandPath ?? input.path,
        timeoutMs: input.timeoutMs,
      });
      return json(res, 200, value);
    }
    if (req.method === 'GET' && route === '/api/companion/state') {
      return json(res, 200, companionProbeSnapshot());
    }
    if (req.method === 'POST' && route === '/api/companion/probe') {
      const input = await parseBody(req);
      const value = await runCompanionProbe({
        vendorId: input.vendorId,
        productId: input.productId,
        usagePage: input.usagePage,
        usage: input.usage,
        timeoutMs: input.timeoutMs,
      });
      return json(res, 200, value);
    }
    if (req.method === 'GET' && route === '/api/config') return json(res, 200, config);
    if (req.method === 'PUT' && route === '/api/config') {
      config = savePersistedConfig(configFile, await parseBody(req));
      bridge.setConfig(config);
      refreshVisualModel();
      publish('config', {config, at: new Date().toISOString()});
      publish('visual', {source: 'config', at: new Date().toISOString(), lighting: visualLighting, model: visualModel});
      return json(res, 200, {ok: true, config, bridge: bridge.snapshot()});
    }

    // Browser EventSource endpoint used by both the protocol lab and the
    // real-N4 page. It carries delayed synthetic releases as soon as they
    // fire in the bridge runtime.
    if (req.method === 'GET' && route === '/api/bridge/events') {
      res.writeHead(200, {
        'content-type': 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache, no-store, must-revalidate',
        connection: 'keep-alive',
        'x-accel-buffering': 'no',
      });
      res.write(': connected\n\n');
      sse(res, 'state', {server: serverSnapshot(), features: capabilitiesSnapshot(), config, bridge: bridge.snapshot(), visual: visualSnapshot(), transport: transport.snapshot(), files: endpoint.files.snapshot(), sideband: sidebandProbeSnapshot(), companion: companionProbeSnapshot(), nativeBridge: nativeBridgeSnapshot(), nativeControl: nativeControlSnapshot()});
      subscribers.add(res);
      const keepAlive = setInterval(() => {
        try { res.write(': ping\n\n'); } catch (_) { clearInterval(keepAlive); }
      }, 20000);
      req.on('close', () => { clearInterval(keepAlive); subscribers.delete(res); });
      return;
    }
    if (req.method === 'GET' && route === '/api/bridge/state') {
      return json(res, 200, {
        config,
        bridge: bridge.snapshot(),
        visual: visualSnapshot(),
        transport: transport.snapshot(),
        files: endpoint.files.snapshot(),
        nativeBridge: nativeBridgeSnapshot(),
        nativeControl: nativeControlSnapshot(),
      });
    }

    if (req.method === 'GET' && route === '/api/visual/state') {
      const phaseText = requestUrl.searchParams.get('phase');
      const requestedPhase = phaseText === null ? NaN : Number(phaseText);
      const phase = Number.isFinite(requestedPhase)
        ? Math.max(0, Math.min(1, requestedPhase))
        : visualModel.phase;
      const model = Number.isFinite(requestedPhase)
        ? modelCache().get(phase)
        : currentVisualModel();
      return json(res, 200, {
        lighting: visualLighting,
        model:{...model,screenTest:screenTestSnapshot()},
        sdkPlan: buildSdkImagePlan(model),
      });
    }
    if (req.method === 'GET' && route === '/api/visual/preview') {
      const html = renderPreviewHtml(currentVisualModel());
      res.writeHead(200, {
        'content-type': 'text/html; charset=utf-8',
        'cache-control': 'no-store',
        'content-length': Buffer.byteLength(html),
      });
      return res.end(html);
    }

    if (req.method === 'GET' && route === '/api/transport/state') {
      return json(res, 200, {
        identity: IDENTITY,
        transport: transport.snapshot(),
        bridge: bridge.snapshot(),
        nativeBridge: nativeBridgeSnapshot(),
      });
    }
    if (req.method === 'GET' && route === '/api/transport/input') {
      const parsedLimit = Number(requestUrl.searchParams.get('limit') ?? 64);
      if (!Number.isInteger(parsedLimit) || parsedLimit < 0 || parsedLimit > 512) {
        throw new Error('limit must be an integer between 0 and 512');
      }
      const shouldDrain = requestUrl.searchParams.get('drain') !== '0';
      const reports = shouldDrain
        ? transport.drainInputReports(parsedLimit)
        : transport.peekInputReports(parsedLimit);
      return json(res, 200, {
        ok: true,
        drained: shouldDrain,
        reports: reports.map((report) => ({bytes: [...report], hex: hex(report)})),
        transport: transport.snapshot(),
      });
    }
    if (req.method === 'POST' && route === '/api/transport/output') {
      const input = await parseBody(req);
      const reports = parseReports(input);
      const result = transport.feedHostReports(reports, input.source || 'codex');
      const value = {
        ok: true,
        reports: reports.map(hex),
        messages: result.messages,
        replies: result.replies,
        replyReports: wireReports(result.replies),
        state: {slots: endpoint.slots, lighting: endpoint.lighting, files: endpoint.files.snapshot()},
        visual: visualSnapshot(),
        transport: transport.snapshot(),
      };
      publish('transport-output', {...value, at: new Date().toISOString()});
      return json(res, 200, value);
    }
    if (req.method === 'POST' && route === '/api/transport/reset') {
      const input = await parseBody(req);
      const resetEndpoint = input.endpoint === true;
      transport.reset({endpoint: resetEndpoint});
      if (resetEndpoint) {
        visualLighting = createLightingState();
        refreshVisualModel();
      }
      const result = {ok: true, transport: transport.snapshot(), visual: visualSnapshot()};
      publish('transport-state', {...result, at: new Date().toISOString()});
      return json(res, 200, result);
    }

    if (req.method === 'POST' && route === '/api/rpc') {
      const request = await parseBody(req);
      const reports = encode(request);
      const handled = transport.feedHostReports(reports, 'webui-rpc');
      const result = {
        request,
        reports: reports.length,
        wireReports: reports.map(hex),
        parsed: handled.messages,
        replies: handled.replies,
        replyReports: wireReports(handled.replies),
        state: {slots: endpoint.slots, lighting: endpoint.lighting},
        files: endpoint.files.snapshot(),
        visual: visualSnapshot(),
        transport: transport.snapshot(),
      };
      publish('rpc', {...result, at: new Date().toISOString()});
      return json(res, 200, result);
    }

    if (req.method === 'GET' && route === '/api/compat/state') {
      return json(res, 200, {
        ok: true,
        mode: 'compatibility-state',
        identity: IDENTITY,
        files: endpoint.files.snapshot(),
        endpoint: {slots: endpoint.slots, lighting: endpoint.lighting},
        transport: transport.snapshot(),
      });
    }

    if (req.method === 'POST' && route === '/api/compat/handshake') {
      const input = await parseBody(req);
      if (input && typeof input !== 'object') throw new Error('handshake body must be an object');
      const baseId = Number.isSafeInteger(Number(input?.baseId)) ? Number(input.baseId) : 9000;
      const requests = [
        {method: 'sys.version', id: baseId + 1},
        {method: 'device.status', id: baseId + 2},
        {method: 'fs.list', params: {checksum: true}, id: baseId + 3},
        {method: 'fs.readbin', params: {file: 'keymap.json', offset: 0, len: 384}, id: baseId + 4},
        {method: 'fs.read', params: {file: 'keymap.json'}, id: baseId + 5},
      ];
      const reports = [];
      const replies = [];
      const wire = [];
      for (const request of requests) {
        const encoded = encode(request);
        reports.push(...encoded.map(hex));
        const handled = transport.feedHostReports(encoded, 'webui-handshake');
        replies.push(...handled.replies);
        wire.push(...wireReports(handled.replies));
      }
      const shouldDrain = input?.drain !== false;
      const drained = shouldDrain ? transport.drainInputReports(transport.inputQueue.length) : [];
      const listReply = replies.find((value) => value?.id === baseId + 3);
      const readbinReply = replies.find((value) => value?.id === baseId + 4);
      const readReply = replies.find((value) => value?.id === baseId + 5);
      const result = {
        ok: replies.length === requests.length && replies.every((value) => !value.error),
        mode: 'compatibility-handshake',
        requests,
        requestReports: reports,
        replies,
        replyReports: wire,
        checks: {
          replyCount: replies.length,
          expectedReplyCount: requests.length,
          keymapListed: Boolean(listReply?.result?.some((item) => item?.name === 'keymap.json')),
          readbinTotal: readbinReply?.result?.total_size ?? null,
          keymapReadAsObject: Boolean(readReply?.result && typeof readReply.result === 'object'),
        },
        files: endpoint.files.snapshot(),
        drained,
        transport: transport.snapshot(),
      };
      publish('compatibility', {...result, at: new Date().toISOString()});
      return json(res, 200, result);
    }

    if (req.method === 'POST' && route === '/api/raw') {
      const input = await parseBody(req);
      const bytes = parseBytes(input.hex ?? input.bytes);
      return json(res, 200, {
        bytes: bytes.length,
        hex: hex(bytes),
        sdkEvent: decodeN4Report(bytes),
        configuredEvents: mapN4ReportToWire(bytes, config),
      });
    }

    if (req.method === 'POST' && route === '/api/bridge/report') {
      const input = await parseBody(req);
      const bytes = parseBytes(input.hex ?? input.bytes);
      const events = bridge.handleReport(bytes);
      bridge.drain();
      const result = bridgeResult(events, {
        bytes: bytes.length,
        hex: hex(bytes),
        sdkEvent: decodeN4Report(bytes),
      });
      publish('report', {...result, at: new Date().toISOString()});
      return json(res, 200, result);
    }

    if (req.method === 'POST' && route === '/api/bridge/hardware') {
      const input = await parseBody(req);
      const code = Number(input.hardwareCode);
      const state = input.state === undefined ? 1 : Number(input.state);
      const events = bridge.handleHardwareEvent(code, state);
      bridge.drain();
      const result = bridgeResult(events, {hardwareCode: code, state});
      publish('hardware', {...result, at: new Date().toISOString()});
      return json(res, 200, result);
    }

    if (req.method === 'POST' && route === '/api/bridge/event') {
      const input = await parseBody(req);
      const event = input.event || input;
      const key = event?.params?.k ?? input.key;
      const action = event?.params?.act ?? input.act;
      const wire = inputEvent(String(key), Number(action));
      enqueueDeviceEvent(wire, 'manual');
      const result = bridgeResult([wire], {source: 'manual'});
      publish('micro-event', eventEnvelope(wire, 'manual'));
      return json(res, 200, result);
    }

    if (req.method === 'POST' && route === '/api/bridge/reset') {
      bridge.cancelPending();
      bridge.drain();
      // A bridge reset is a complete input-path reset: delayed synthetic
      // releases and already queued Micro input reports must not leak into the
      // next session.  Preserve endpoint lighting/configuration state.
      transport.reset();
      const result = {ok: true, bridge: bridge.snapshot(), transport: transport.snapshot()};
      publish('state', {config, bridge: result.bridge, visual: visualSnapshot(), transport: result.transport});
      return json(res, 200, result);
    }

    return json(res, 404, {error: 'not found'});
  } catch (error) {
    const status = Number.isInteger(error?.status) ? error.status : 400;
    const body = {
      error: error?.code || error?.message || String(error),
      message: error?.message || String(error),
      ...(error?.details && typeof error.details === 'object' ? error.details : {}),
    };
    return json(res, status, body);
  }
});

server.on('error', (error) => {
  if (error?.code === 'EADDRINUSE') {
    console.error(`Cannot start Mirabox Codex Micro WebUI: http://127.0.0.1:${port} is already in use.`);
    console.error(`An older WebUI process may still be running. In PowerShell, inspect it with: Get-NetTCPConnection -State Listen -LocalPort ${port} | Select-Object LocalPort,OwningProcess`);
    console.error('Stop/restart that process to load the current backend, or choose another port with scripts\\start-webui.ps1 -Port <port>.');
    bridge.close();
    transport.reset();
    process.exitCode = 1;
    return;
  }
  console.error(`Mirabox Codex Micro WebUI server error: ${error?.stack || error}`);
  process.exitCode = 1;
});
server.listen(port, '127.0.0.1', () => {
  console.log(`Mirabox Codex Micro WebUI: http://127.0.0.1:${port} (PID ${process.pid}, build ${SERVER_IDENTITY.buildId}, API v${SERVER_IDENTITY.apiVersion})`);
});

function shutdown() {
  // Best-effort cleanup is limited to the child created by /api/native/start.
  // External N4/StreamDock processes are intentionally left untouched.
  if (nativeBridgeProcess && nativeBridgeProcess.exitCode == null) {
    try { nativeBridgeProcess.kill('SIGTERM'); } catch (_) {}
  }
  bridge.close();
  transport.reset();
  for (const client of subscribers) client.end();
  server.close(() => process.exit(0));
}
process.once('SIGINT', shutdown);
process.once('SIGTERM', shutdown);

module.exports = {
  server,
  bridge,
  transport,
  getConfig: () => config,
  getVisualState: () => ({lighting: visualLighting, model: currentVisualModel()}),
  nativeStatusSnapshot,
};
