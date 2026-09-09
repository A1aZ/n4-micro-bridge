'use strict';

/**
 * In-process transport hub for a virtual Codex Micro device.
 *
 * Direction names follow HID terminology:
 *   - device input reports travel from the emulated Micro to Codex Desktop;
 *   - host output reports travel from Codex Desktop to the emulated Micro.
 *
 * The UMDF companion only needs to move opaque 64-byte reports between this
 * hub and the driver's sideband IOCTLs. Protocol framing, JSON decoding and
 * RPC replies stay in user space where they can be tested without a driver.
 */

const {Decoder, MicroEndpoint, encode} = require('./micro-protocol.cjs');

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function reportBuffer(value) {
  const report = Buffer.from(value || []);
  if (report.length !== 64 && report.length !== 63) {
    throw new Error('Micro report must contain 64 bytes (or 63 bytes without report ID)');
  }
  return report;
}

class MicroTransportHub {
  constructor(options = {}) {
    const {
      endpoint = new MicroEndpoint(),
      maxQueuedReports = 512,
      onHostMessage = null,
      onDeviceMessage = null,
    } = options;
    if (!endpoint || typeof endpoint.handle !== 'function') throw new Error('endpoint.handle must be a function');
    if (!Number.isInteger(maxQueuedReports) || maxQueuedReports < 1 || maxQueuedReports > 65536) {
      throw new Error('maxQueuedReports must be an integer between 1 and 65536');
    }
    for (const [name, callback] of [['onHostMessage', onHostMessage], ['onDeviceMessage', onDeviceMessage]]) {
      if (callback !== null && typeof callback !== 'function') throw new Error(`${name} must be a function`);
    }
    this.endpoint = endpoint;
    this.decoder = new Decoder();
    this.maxQueuedReports = maxQueuedReports;
    this.onHostMessage = onHostMessage;
    this.onDeviceMessage = onDeviceMessage;
    this.inputQueue = [];
    this.sequence = 0;
    this.stats = {
      hostReports: 0,
      hostMessages: 0,
      deviceMessages: 0,
      queuedReports: 0,
      drainedReports: 0,
      droppedReports: 0,
      decodeErrors: 0,
      replies: 0,
    };
  }

  setHostMessageHandler(callback) {
    if (callback !== null && typeof callback !== 'function') throw new Error('callback must be a function');
    this.onHostMessage = callback;
  }

  setDeviceMessageHandler(callback) {
    if (callback !== null && typeof callback !== 'function') throw new Error('callback must be a function');
    this.onDeviceMessage = callback;
  }

  /** Queue one JSON message as one or more HID input reports for Codex. */
  enqueueDeviceMessage(message, source = 'bridge') {
    const reports = encode(message);
    const item = {
      sequence: ++this.sequence,
      source,
      message: clone(message),
      reports: reports.map((report) => Buffer.from(report)),
    };
    this.stats.deviceMessages += 1;
    for (const report of item.reports) this._enqueueReport(report);
    if (this.onDeviceMessage) this.onDeviceMessage(item);
    return item;
  }

  _enqueueReport(report) {
    while (this.inputQueue.length >= this.maxQueuedReports) {
      this.inputQueue.shift();
      this.stats.droppedReports += 1;
    }
    this.inputQueue.push(Buffer.from(report));
    this.stats.queuedReports += 1;
  }

  /**
   * Feed one output report written by Codex. Complete JSON objects are sent
   * through MicroEndpoint and any RPC reply is queued back as an input report.
   */
  feedHostReport(value, source = 'codex') {
    const report = reportBuffer(value);
    this.stats.hostReports += 1;
    let messages;
    try {
      messages = this.decoder.feed(report);
    } catch (error) {
      this.stats.decodeErrors += 1;
      throw error;
    }

    const replies = [];
    for (const message of messages) {
      this.stats.hostMessages += 1;
      if (this.onHostMessage) this.onHostMessage({source, message: clone(message)});
      const reply = this.endpoint.handle(message);
      if (!reply) continue;
      replies.push(reply);
      this.stats.replies += 1;
      this.enqueueDeviceMessage(reply, 'rpc-reply');
    }
    return {messages, replies};
  }

  feedHostReports(values, source = 'codex') {
    if (!Array.isArray(values)) throw new Error('reports must be an array');
    const messages = [];
    const replies = [];
    for (const report of values) {
      const result = this.feedHostReport(report, source);
      messages.push(...result.messages);
      replies.push(...result.replies);
    }
    return {messages, replies};
  }

  /** Drain up to `limit` queued device input reports. */
  drainInputReports(limit = this.inputQueue.length) {
    if (!Number.isInteger(limit) || limit < 0) throw new Error('limit must be a non-negative integer');
    const count = Math.min(limit, this.inputQueue.length);
    const reports = this.inputQueue.splice(0, count);
    this.stats.drainedReports += reports.length;
    return reports;
  }

  peekInputReports(limit = this.inputQueue.length) {
    if (!Number.isInteger(limit) || limit < 0) throw new Error('limit must be a non-negative integer');
    return this.inputQueue.slice(0, limit).map((report) => Buffer.from(report));
  }

  reset(options = {}) {
    const {endpoint = false} = options;
    this.decoder.reset();
    this.inputQueue = [];
    if (endpoint && typeof this.endpoint.reset === 'function') this.endpoint.reset();
  }

  snapshot() {
    return {
      queuedInputReports: this.inputQueue.length,
      maxQueuedReports: this.maxQueuedReports,
      stats: {...this.stats},
    };
  }
}

module.exports = {MicroTransportHub, reportBuffer};
