'use strict';

function minuteToken(clock) {
  if (clock && typeof clock === 'object' && typeof clock.minute === 'string') {
    return clock.minute;
  }
  const value = clock instanceof Date ? clock : new Date(clock);
  if (Number.isNaN(value.getTime())) throw new Error('visual cache clock must identify a valid minute');
  const pad = number => String(number).padStart(2, '0');
  return [
    value.getFullYear(),
    pad(value.getMonth() + 1),
    pad(value.getDate()),
    pad(value.getHours()),
    pad(value.getMinutes()),
  ].join('-');
}

class MinuteVisualModelCache {
  constructor({clock, build, maxEntries = 8, phaseSteps = 8} = {}) {
    if (typeof clock !== 'function') throw new Error('visual cache clock must be a function');
    if (typeof build !== 'function') throw new Error('visual cache build must be a function');
    if (!Number.isInteger(maxEntries) || maxEntries < 1) {
      throw new Error('visual cache maxEntries must be a positive integer');
    }
    if (!Number.isInteger(phaseSteps) || phaseSteps < 1) {
      throw new Error('visual cache phaseSteps must be a positive integer');
    }
    this.clock = clock;
    this.build = build;
    this.maxEntries = maxEntries;
    this.phaseSteps = phaseSteps;
    this.minute = null;
    this.models = new Map();
  }

  get(phase = 0.5) {
    const currentClock = this.clock();
    const currentMinute = minuteToken(currentClock);
    if (currentMinute !== this.minute) {
      this.minute = currentMinute;
      this.models.clear();
    }
    const requestedPhase = Number(phase);
    if (!Number.isFinite(requestedPhase)) throw new Error('visual cache phase must be finite');
    const boundedPhase = Math.min(1, Math.max(0, requestedPhase));
    // Animation phase is circular: 1 and 0 represent the same frame. Keeping
    // the sample index modulo phaseSteps yields exactly phaseSteps cache keys.
    const sampleIndex = Math.round(boundedPhase * this.phaseSteps) % this.phaseSteps;
    const sampledPhase = sampleIndex / this.phaseSteps;
    if (this.models.has(sampledPhase)) {
      const cached = this.models.get(sampledPhase);
      // Refresh insertion order so eviction keeps the most recently requested
      // animation frames, not merely the numerically lowest phases.
      this.models.delete(sampledPhase);
      this.models.set(sampledPhase, cached);
      return cached;
    }
    const model = this.build(sampledPhase, currentClock);
    this.models.set(sampledPhase, model);
    while (this.models.size > this.maxEntries) {
      this.models.delete(this.models.keys().next().value);
    }
    return model;
  }

  invalidate() {
    this.models.clear();
  }

  refresh(phase = 0.5) {
    this.invalidate();
    return this.get(phase);
  }

  get size() {
    return this.models.size;
  }
}

module.exports = {MinuteVisualModelCache, minuteToken};
