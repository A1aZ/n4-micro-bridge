'use strict';

/**
 * Runtime-neutral N4 -> Codex Micro event bridge.
 *
 * This module deliberately has no USB/HID dependencies.  A transport adapter
 * can feed `handleReport()` with the bytes from an N4 input report and write
 * the returned wire events to whichever Micro transport it owns.  Synthetic
 * encoder releases are scheduled here so a one-shot N4 press behaves like the
 * press/release pair expected by Micro.
 */

const {defaultConfig, normalizeConfig, mapN4HardwareEvent, mapN4Report, wireEvent} = require('./micro-config.cjs');
const {n4HardwareEvent} = require('./n4-input.cjs');

function defaultSchedule(fn, delayMs) {
  return setTimeout(fn, delayMs);
}

function defaultCancel(handle) {
  clearTimeout(handle);
}

class MicroBridge {
  constructor(options = {}) {
    const {config, schedule = defaultSchedule, cancel = defaultCancel, onEvent = null} = options;
    if (typeof schedule !== 'function' || typeof cancel !== 'function') {
      throw new Error('schedule and cancel must be functions');
    }
    if (onEvent !== null && typeof onEvent !== 'function') throw new Error('onEvent must be a function');
    this.config = normalizeConfig(config || defaultConfig());
    this.schedule = schedule;
    this.cancel = cancel;
    this.onEvent = onEvent;
    this.queue = [];
    this.localActions = [];
    this.pending = new Map();
    this.stats = {reports: 0, hardwareEvents: 0, emitted: 0, scheduled: 0, cancelled: 0, ignored: 0};
  }

  setConfig(config) {
    this.cancelPending();
    this.localActions=[];
    this.config = normalizeConfig(config);
    return this.config;
  }

  setEventHandler(onEvent) {
    if (onEvent !== null && typeof onEvent !== 'function') throw new Error('onEvent must be a function');
    this.onEvent = onEvent;
  }

  /**
   * Handle one complete N4 report.  Immediate wire events are returned; a
   * synthetic release is delivered later through the queue/callback.
   */
  handleReport(report) {
    this.stats.reports += 1;
    const bytes = Buffer.from(report || []);
    const extracted = n4HardwareEvent(bytes);
    if (!extracted) {
      this.stats.ignored += 1;
      return [];
    }
    // Use the normalized hardware-code offset for pending synthetic releases;
    // a 513-byte hidapi report has the code at data[10], not data[9].
    return this._handleEvents(mapN4Report(bytes, this.config), extracted.hardwareCode);
  }

  /** Handle the already extracted data[9]/data[10] pair. */
  handleHardwareEvent(hardwareCode, state = 0) {
    this.stats.hardwareEvents += 1;
    return this._handleEvents(mapN4HardwareEvent(hardwareCode, state, this.config), hardwareCode);
  }

  _handleEvents(events, sourceCode = null) {
    const immediate = [];
    for (const event of events) {
      if (event?.localAction) {
        this.localActions.push({...event.localAction});
        if(this.localActions.length>64)this.localActions.shift();
        continue;
      }
      if (!event || !event.method || !event.params) {
        this.stats.ignored += 1;
        continue;
      }
      const wire = wireEvent(event);
      const key = wire.params?.k;
      const action = wire.params?.act;
      // Key names are shared by all four N4 knobs (ENC). Include the source
      // hardware code in the pending key so two knobs pressed close together
      // each get their own synthetic release timer.
      const pendingKey = sourceCode === null ? key : `${sourceCode}:${key}`;
      if (event.meta?.synthetic) {
        // A real release report arriving before the timer cancels this entry;
        // this avoids duplicate act=0 events on firmware variants that do emit
        // releases after all.
        const delayMs = event.meta.delayMs;
        const old = this.pending.get(pendingKey);
        if (old) {
          this.cancel(old.handle);
          this.pending.delete(pendingKey);
          this.stats.cancelled += 1;
        }
        const pending = {handle: null, event: wire};
        pending.handle = this.schedule(() => {
          // A scheduler may race cancellation.  Only the current timer for
          // this key is allowed to deliver the synthetic release.
          if (this.pending.get(pendingKey) !== pending) return;
          this.pending.delete(pendingKey);
          this._emit(wire);
        }, delayMs);
        this.pending.set(pendingKey, pending);
        this.stats.scheduled += 1;
        continue;
      }

      if (action === 0 && this.pending.has(pendingKey)) {
        this.cancel(this.pending.get(pendingKey).handle);
        this.pending.delete(pendingKey);
        this.stats.cancelled += 1;
      }
      immediate.push(wire);
      this._emit(wire);
    }
    return immediate;
  }

  _emit(event) {
    this.queue.push(event);
    this.stats.emitted += 1;
    if (this.onEvent) this.onEvent(event);
  }

  /** Return and clear events emitted since the previous drain. */
  drain() {
    const events = this.queue;
    this.queue = [];
    return events;
  }

  drainLocalActions() { const actions=this.localActions;this.localActions=[];return actions; }

  /** Cancel all delayed releases without emitting them. */
  cancelPending() {
    for (const pending of this.pending.values()) {
      this.cancel(pending.handle);
      this.stats.cancelled += 1;
    }
    this.pending.clear();
  }

  /** Flush delayed releases immediately (useful when shutting down/tests). */
  flushPending() {
    const handles = [...this.pending.entries()];
    this.pending.clear();
    for (const [key, pending] of handles) {
      this.cancel(pending.handle);
      this.stats.cancelled += 1;
      this._emit({method: 'v.oai.hid', params: {...pending.event.params, act: 0}});
    }
    return this.drain();
  }

  close() {
    this.cancelPending();
    this.localActions=[];
    this.onEvent = null;
  }

  snapshot() {
    return {
      config: this.config,
      pendingReleases: this.pending.size,
      queuedEvents: this.queue.length,
      stats: {...this.stats},
    };
  }
}

function createBridge(options) {
  return new MicroBridge(options);
}

module.exports = {MicroBridge, createBridge};
