'use strict';

// Frame statistics used by the developer diagnostics readout.

const assert = require('node:assert/strict');
const { FrameMetrics } = require('../src/metrics');

const { test, run } = require('./helpers/harness');

test('uses timestamp ratio for stable 60 FPS and excludes the initial sample', () => {
  const metrics = new FrameMetrics();
  metrics.reset('idle', 0);
  for (let i = 0; i <= 3600; i++) metrics.frame(i * (1000 / 60), 2);
  const s = metrics.summary();
  assert.equal(s.label, 'idle');
  assert.equal(s.frames, 3600);
  assert.ok(Math.abs(s.fps - 60) < 1e-9);
  assert.ok(Math.abs(s.elapsedMs - 60000) < 1e-8);
  assert.equal(s.cpuMs.p50, 2);
  assert.equal(s.valid, true);
});

test('reports spike percentiles and does not clamp long intervals', () => {
  const metrics = new FrameMetrics({ capacity: 40000 });
  metrics.reset('spike', 0);
  let t = 0;
  for (let i = 0; i <= 100; i++) {
    const spike = i === 98 ? 120 : (i === 99 ? 250 : 1000 / 60);
    t += spike;
    metrics.frame(t, i === 98 ? 43 : (i === 99 ? 73 : 1));
  }
  const s = metrics.summary();
  assert.equal(s.frames, 100);
  assert.ok(s.frameMs.p99 >= 120);
  assert.equal(s.frameMs.max, 250);
  assert.equal(s.cpuMs.max, 73);
  assert.equal(s.longFrames, 2);
});

test('empty recorder returns null FPS and null distributions', () => {
  const s = new FrameMetrics().summary();
  assert.equal(s.fps, null);
  assert.equal(s.frames, 0);
  assert.deepEqual(s.frameMs, { p50: null, p95: null, p99: null, max: null });
  assert.deepEqual(s.cpuMs, { p50: null, p95: null, p99: null, max: null });
  assert.equal(s.valid, false);
});

test('visibility gaps and synthetic frames cannot create fake elapsed FPS', () => {
  const metrics = new FrameMetrics();
  metrics.reset('visibility', 0);
  metrics.frame(0, 1);
  metrics.frame(1000 / 60, 1);
  metrics.frame(120000, 0, { visible: false });
  metrics.frame(0, 0, { visible: false });
  metrics.frame(300000, 0, { synthetic: true });
  metrics.frame(500000, 1);
  metrics.frame(500000 + 1000 / 60, 1);
  const s = metrics.summary();
  assert.equal(s.frames, 2);
  assert.ok(Math.abs(s.fps - 60) < 1e-9);
  assert.ok(Math.abs(s.elapsedMs - 2000 / 60) < 1e-9);
  assert.equal(s.valid, false);
  assert.equal(s.contiguous, false);
  assert.equal(s.visibilityInterruptions, 1);
});

test('60 seconds accumulated across a hidden segment is diagnostic but invalid', () => {
  const metrics = new FrameMetrics();
  metrics.reset('interrupted', 0);
  for (let i = 0; i <= 1800; i++) metrics.frame(i * (1000 / 60), 1);
  metrics.frame(30000, 0, { visible: false });
  metrics.frame(100000, 1); // First frame after visibility returns is a baseline.
  for (let i = 1; i <= 1800; i++) metrics.frame(100000 + i * (1000 / 60), 1);
  const s = metrics.summary();
  assert.equal(s.visibilityInterruptions, 1);
  assert.equal(s.contiguous, false);
  assert.ok(s.elapsedMs >= 60000);
  assert.equal(s.valid, false);
  assert.ok(Math.abs(s.fps - 60) < 1e-9);
});

test('ring capacity bounds percentile storage while lifetime totals continue', () => {
  const metrics = new FrameMetrics({ capacity: 3 });
  metrics.reset('ring', 0);
  metrics.frame(0, 10);
  metrics.frame(10, 1);
  metrics.frame(30, 2);
  metrics.frame(60, 3);
  metrics.frame(100, 4);
  const s = metrics.summary();
  assert.equal(s.frames, 4);
  assert.equal(s.elapsedMs, 100);
  assert.deepEqual(s.frameMs, { p50: 30, p95: 40, p99: 40, max: 40 });
  assert.deepEqual(s.cpuMs, { p50: 3, p95: 4, p99: 4, max: 4 });
});

run('metrics');
