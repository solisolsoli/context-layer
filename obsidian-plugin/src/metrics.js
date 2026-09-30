'use strict';

const DEFAULT_CAPACITY = 40000;
const DEFAULT_LONG_FRAME_MS = 50;
const MIN_VALID_ELAPSED_MS = 60000;
const MIN_VALID_INTERVALS = 60;

/**
 * Fixed-size, allocation-free-per-frame recorder for real requestAnimationFrame
 * samples. Percentiles use the most recent `capacity` paired samples; elapsed
 * time, interval count, and FPS cover the full recording since reset().
 */
class FrameMetrics {
  constructor({ capacity = DEFAULT_CAPACITY, longFrameMs = DEFAULT_LONG_FRAME_MS } = {}) {
    if (!Number.isInteger(capacity) || capacity < 1) {
      throw new RangeError('capacity must be a positive integer');
    }
    if (!Number.isFinite(longFrameMs) || longFrameMs < 0) {
      throw new RangeError('longFrameMs must be a non-negative finite number');
    }
    this.capacity = capacity;
    this.longFrameMs = longFrameMs;
    this._frameRing = new Float64Array(capacity);
    this._cpuRing = new Float64Array(capacity);
    this.reset(null, 0);
  }

  reset(label = null, now = 0) {
    this.label = label;
    this._resetTimestamp = Number.isFinite(now) ? now : null;
    // Only a real RAF timestamp can establish an interval baseline.
    this._lastTimestamp = null;
    this._hasBaseline = false;
    this._writeIndex = 0;
    this._sampleCount = 0;
    this._intervalCount = 0;
    this._elapsedMs = 0;
    this._longFrames = 0;
    this._visibilityInterruptions = 0;
    return this;
  }

  /**
   * Record one real rAF timestamp and its synchronous CPU work duration.
   * Invisible samples break the interval segment. Synthetic samples are
   * ignored entirely and do not alter the baseline.
   */
  frame(timestamp, cpuMs, { visible = true, synthetic = false } = {}) {
    if (synthetic) return false;
    if (!visible) {
      if (this._hasBaseline) this._visibilityInterruptions++;
      this._hasBaseline = false;
      this._lastTimestamp = null;
      return false;
    }
    if (!Number.isFinite(timestamp)) return false;

    const cost = Number.isFinite(cpuMs) && cpuMs >= 0 ? cpuMs : 0;
    if (!this._hasBaseline) {
      this._lastTimestamp = timestamp;
      this._hasBaseline = true;
      return false;
    }

    const interval = timestamp - this._lastTimestamp;
    if (!(interval > 0)) {
      // A backwards or duplicate clock value starts a fresh segment.
      this._lastTimestamp = timestamp;
      return false;
    }

    this._lastTimestamp = timestamp;
    this._frameRing[this._writeIndex] = interval;
    this._cpuRing[this._writeIndex] = cost;
    this._writeIndex = (this._writeIndex + 1) % this.capacity;
    if (this._sampleCount < this.capacity) this._sampleCount++;
    this._intervalCount++;
    this._elapsedMs += interval;
    if (interval >= this.longFrameMs) this._longFrames++;
    return true;
  }

  summary() {
    const frameSorted = new Float64Array(this._sampleCount);
    const cpuSorted = new Float64Array(this._sampleCount);
    const start = (this._writeIndex - this._sampleCount + this.capacity) % this.capacity;
    for (let i = 0; i < this._sampleCount; i++) {
      const at = (start + i) % this.capacity;
      frameSorted[i] = this._frameRing[at];
      cpuSorted[i] = this._cpuRing[at];
    }
    frameSorted.sort();
    cpuSorted.sort();

    const elapsedMs = this._elapsedMs;
    const frames = this._intervalCount;
    return {
      label: this.label,
      elapsedMs,
      frames,
      fps: elapsedMs > 0 ? (frames * 1000) / elapsedMs : null,
      frameMs: distribution(frameSorted),
      cpuMs: distribution(cpuSorted),
      longFrames: this._longFrames,
      visibilityInterruptions: this._visibilityInterruptions,
      contiguous: this._visibilityInterruptions === 0,
      valid: elapsedMs >= MIN_VALID_ELAPSED_MS &&
        frames >= MIN_VALID_INTERVALS &&
        this._visibilityInterruptions === 0
    };
  }
}

function distribution(sorted) {
  const n = sorted.length;
  if (!n) return { p50: null, p95: null, p99: null, max: null };
  return {
    p50: percentile(sorted, 0.50),
    p95: percentile(sorted, 0.95),
    p99: percentile(sorted, 0.99),
    max: sorted[n - 1]
  };
}

// Nearest-rank percentile, with a one-based rank rounded upward.
function percentile(sorted, fraction) {
  return sorted[Math.max(0, Math.ceil(fraction * sorted.length) - 1)];
}

module.exports = { FrameMetrics };
