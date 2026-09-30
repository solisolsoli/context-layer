'use strict';

// Contract tests for src/layout.js: shell slots, determinism, density of the
// volumetric layout, Barnes-Hut permutation equivariance and the worker
// protocol. All fixtures are synthetic.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const path = require('node:path');
const { performance } = require('node:perf_hooks');
const { test, run } = require('./helpers/harness');

const { createSolver, surfaceSlots, shellSlots, densityStats, hilbertAxes, workerSource } = require(path.join(__dirname, '../src/layout.js'));

function inputs(n, pairs, positions, ids) {
  return {
    revision: 1,
    ids: ids || Array.from({ length: n }, (_, i) => `note-${i}`),
    edges: Uint32Array.from(pairs.flat()),
    ...(positions ? { positions: Float32Array.from(positions) } : {}),
  };
}

function assertFiniteAndRadius(positions, limit, label) {
  assert.equal(positions.length % 3, 0, `${label}: xyz triples`);
  for (let i = 0; i < positions.length; i += 3) {
    const x = positions[i], y = positions[i + 1], z = positions[i + 2];
    assert.ok(Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(z), `${label}: finite point ${i / 3}`);
    assert.ok(Math.hypot(x, y, z) <= limit + 2e-5, `${label}: point ${i / 3} radius <= ${limit}`);
  }
}

function radius(p, i) { return Math.hypot(p[3 * i], p[3 * i + 1], p[3 * i + 2]); }

function groupSurfaceRows(positions) {
  const rows = new Map();
  for (let i = 0; i < positions.length / 3; i++) {
    const z = positions[3 * i + 2];
    // All points assigned to one latitude band share z; quantization avoids
    // depending on tiny Float32 serialization differences.
    const key = Math.round(z * 1e6);
    if (!rows.has(key)) rows.set(key, []);
    rows.get(key).push(Math.atan2(positions[3 * i + 1], positions[3 * i]));
  }
  return rows;
}

function testUniformSurfaceRows() {
  const ids = Array.from({ length: 1024 }, (_, i) => `surface-${i}`);
  const rows = groupSurfaceRows(surfaceSlots(ids));
  assert.ok(rows.size > 10, 'surface has multiple latitude rows');
  for (const [row, angles] of rows) {
    if (angles.length < 4) continue;
    const sorted = angles.map(angle => (angle + 2 * Math.PI) % (2 * Math.PI)).sort((a, b) => a - b);
    const gaps = sorted.map((angle, i) => (i + 1 < sorted.length ? sorted[i + 1] : sorted[0] + 2 * Math.PI) - angle);
    const expected = 2 * Math.PI / angles.length;
    const maxError = Math.max(...gaps.map(gap => Math.abs(gap - expected)));
    assert.ok(maxError <= 2e-4, `row ${row} has uniform longitude spacing (max gap error ${maxError})`);
  }
}

function testEmptyAndSingle() {
  const empty = createSolver(inputs(0, []));
  empty.step(4);
  assert.equal(empty.positions.length, 0);
  assert.equal(empty.degrees.length, 0);
  assertFiniteAndRadius(empty.positions, 0.94, 'empty');

  const one = createSolver(inputs(1, []));
  one.step(4);
  assert.equal(one.positions.length, 3);
  assert.equal(one.degrees[0], 0);
  assert.ok(Math.abs(radius(one.positions, 0) - 1) < 2e-5, 'single orphan is on unit sphere');
}

function testCoincidentAndShell() {
  const coincident = inputs(8, [[0, 1], [1, 2], [2, 3], [3, 4]], Array(24).fill(0));
  const solver = createSolver(coincident);
  solver.step(80);
  assertFiniteAndRadius(solver.positions, 1, 'coincident fixture');
  for (let i = 0; i < 5; i++) assert.ok(radius(solver.positions, i) <= 0.94 + 2e-5);
  for (let i = 5; i < 8; i++) assert.ok(Math.abs(radius(solver.positions, i) - 1) < 2e-5, 'orphan remains on shell');

  const shellA = surfaceSlots(['a', 'b', 'c', 'd']);
  const shellB = surfaceSlots(['a', 'b', 'c', 'd']);
  assert.equal(shellA.length, 4 * 3, 'one xyz position per ID');
  assert.deepEqual(Array.from(shellA), Array.from(shellB), 'surface assignment deterministic');
  assertFiniteAndRadius(shellA, 1 + 2e-5, 'surface slots');
  const unique = new Set(Array.from({ length: 4 }, (_, i) => Array.from(shellA.slice(i * 3, i * 3 + 3)).join(',')));
  assert.equal(unique.size, 4, 'surface slots have no duplicate coordinates');
}

function testOrphansUseTheirOwnSurfaceSlots() {
  const n = 300;
  const ids = Array.from({ length: n }, (_, i) => `identity-${i}`);
  const pairs = Array.from({ length: 100 }, (_, i) => [i * 2, i * 2 + 1]);
  const solver = createSolver(inputs(n, pairs, undefined, ids));
  const orphanIndices = Array.from({ length: 100 }, (_, i) => 200 + i);
  const expected = shellSlots(orphanIndices.map(index => ids[index]));
  const actual = new Float32Array(orphanIndices.length * 3);
  orphanIndices.forEach((index, i) => actual.set(solver.positions.slice(index * 3, index * 3 + 3), i * 3));
  assert.deepEqual(Array.from(actual), Array.from(expected), 'orphan positions exactly equal shellSlots(orphan keys)');
  solver.step(2);
  orphanIndices.forEach((index, i) => assert.deepEqual(
    Array.from(solver.positions.slice(index * 3, index * 3 + 3)),
    Array.from(expected.slice(i * 3, i * 3 + 3)),
    `orphan ${index} remains in its own stable shell slot`,
  ));
}

function makeLargeFixture() {
  const n = 7000;
  const pairs = [];
  // Four disconnected components of 1500 nodes, each with a real high-degree
  // anchor and 1499 leaves. This gives 5996 edges before four cross-leaf
  // chords; the last 1000 nodes are genuine orphans.
  for (let component = 0; component < 4; component++) {
    const start = component * 1500;
    for (let i = 1; i < 1500; i++) pairs.push([start, start + i]);
  }
  pairs.push([1, 2], [1501, 1502], [3001, 3002], [4501, 4502]);
  assert.equal(pairs.length, 6000);
  return { n, pairs };
}

function testLargeFixture() {
  const { n, pairs } = makeLargeFixture();
  const input = inputs(n, pairs);
  const solver = createSolver(input);
  const start = performance.now();
  const maxIterations = 300;
  solver.step(maxIterations);
  const iterations = solver.iterations;
  const elapsedMs = performance.now() - start;
  assert.equal(solver.positions.length, n * 3, 'large fixture exact position count');
  assert.equal(solver.degrees.length, n, 'large fixture exact degree count');
  assert.equal(solver.degrees.reduce((sum, degree) => sum + degree, 0), pairs.length * 2, 'degree sum matches undirected real edges');
  assertFiniteAndRadius(solver.positions, 1, 'large fixture');
  for (let i = 0; i < 6000; i++) assert.ok(radius(solver.positions, i) <= 0.94 + 2e-5, `connected node ${i} inside 0.94R`);
  for (let i = 6000; i < n; i++) assert.ok(Math.abs(radius(solver.positions, i) - 1) < 2e-5, `orphan ${i} on unit shell`);

  // A density metric describes each of 8 octants x 4 equal-volume radial
  // bands. Four hubs with 1,499 leaves each form four clusters (that is the
  // point of a link-aware layout), so the bins are not uniform; the sphere
  // must still be filled: every bin occupied and every radial band holding
  // a fair share, while the four stars stay apart.
  const density = densityStats(solver.positions, solver.degrees);
  const shells = [0, 0, 0, 0];
  density.bins.forEach((count, b) => { shells[b & 3] += count; });
  const p = solver.positions, dist = (a, b) => Math.hypot(p[3 * a] - p[3 * b], p[3 * a + 1] - p[3 * b + 1], p[3 * a + 2] - p[3 * b + 2]);
  let sameStar = 0, otherStar = 0;
  for (let i = 0; i < 400; i++) { const a = (i * 37) % 6000, b = (a + 1 + (i % 1400)) % 6000, c = (a + 1500 + i) % 6000; sameStar += Math.floor(a / 1500) === Math.floor(b / 1500) ? dist(a, b) : 0; otherStar += dist(a, c); }
  console.log('  large fixture: ' + JSON.stringify({ elapsedMs: Math.round(elapsedMs), iterations, settled: solver.settled, cv: +density.cv.toFixed(4), empty: density.empty, shells }));
  assert.ok(solver.settled, `large fixture settles within ${maxIterations} iterations (actual ${iterations})`);
  assert.ok(iterations <= maxIterations);
  assert.equal(density.bins.length, 32, '32 octant/radial bins');
  assert.ok(density.bins.every(Number.isFinite), 'density bins are finite counts');
  assert.equal(density.empty, 0, 'all 32 connected density bins occupied');
  for (const share of shells.map(c => c / 6000)) assert.ok(share >= 0.15 && share <= 0.35, `each equal-volume radial band holds 15-35% of linked notes (got ${share})`);
  assert.equal(density.connected, 6000);
  assert.equal(density.maxRadius <= 0.94 + 2e-5, true);
  return { elapsedMs, iterations, settled: solver.settled, stats: solver.stats(), density };
}

function testDeterminism() {
  const input = inputs(96, Array.from({ length: 95 }, (_, i) => [i, i + 1]));
  const a = createSolver(input), b = createSolver(input);
  a.step(48); b.step(48);
  assert.deepEqual(Array.from(a.positions), Array.from(b.positions), 'same input and steps yield same positions');
  assert.deepEqual(Array.from(a.degrees), Array.from(b.degrees), 'same edge list yields same degrees');
}

function testBarnesHutPermutationEquivariance() {
  const n = 80;
  const ids = Array.from({ length: n }, (_, i) => `bh-${i}`);
  const positions = Array.from({ length: n }, (_, i) => [
    0.28 * Math.sin(i * 1.71),
    0.28 * Math.sin(i * 2.39 + 0.4),
    0.28 * Math.sin(i * 2.93 + 1.2),
  ]).flat();
  // Force exact split-plane and coincident cases into the tree.
  for (const [i, xyz] of [[0, [-0.2, 0, 0]], [1, [0.2, 0, 0]], [2, [0, -0.2, 0]], [3, [0, 0.2, 0]], [4, [0, 0, -0.2]], [5, [0, 0, 0.2]], [6, [0.1, 0.1, 0.1]], [7, [0.1, 0.1, 0.1]]]) positions.splice(i * 3, 3, ...xyz);
  const pairs = Array.from({ length: n }, (_, i) => [i, (i + 1) % n]);
  const original = { revision: 1, ids, edges: Uint32Array.from(pairs.flat()), positions: Float32Array.from(positions) };
  const permutation = Array.from({ length: n }, (_, i) => (i * 37) % n); // 37 is coprime to 80.
  const permutedIndex = new Uint32Array(n);
  permutation.forEach((oldIndex, newIndex) => { permutedIndex[oldIndex] = newIndex; });
  const permuted = {
    revision: 1,
    ids: permutation.map(i => ids[i]),
    edges: Uint32Array.from(pairs.flatMap(([a, b]) => [permutedIndex[a], permutedIndex[b]])),
    positions: Float32Array.from(permutation.flatMap(i => positions.slice(3 * i, 3 * i + 3))),
  };
  const a = createSolver(original), b = createSolver(permuted);
  for (let oldIndex = 0; oldIndex < n; oldIndex++) {
    const newIndex = permutedIndex[oldIndex];
    for (let axis = 0; axis < 3; axis++) {
      assert.equal(a.positions[3 * oldIndex + axis], b.positions[3 * newIndex + axis], `fixture has identical initial point for node ${oldIndex}, axis ${axis}`);
    }
  }
  a.step(1); b.step(1);
  for (let oldIndex = 0; oldIndex < n; oldIndex++) {
    const newIndex = permutedIndex[oldIndex];
    for (let axis = 0; axis < 3; axis++) {
      const delta = Math.abs(a.positions[3 * oldIndex + axis] - b.positions[3 * newIndex + axis]);
      assert.ok(delta <= 1e-4, `Barnes-Hut insertion is permutation-equivariant for node ${oldIndex}, axis ${axis} (delta ${delta})`);
    }
  }
}

function workerHarness() {
  const messages = [];
  const listeners = new Set();
  const self = {
    onmessage: null,
    postMessage(message) {
      messages.push(message);
      for (const listener of listeners) listener(message);
    },
  };
  const context = {
    self, postMessage: self.postMessage.bind(self),
    setTimeout, clearTimeout, performance,
    console, Math, Date, Array, Object, Number, String, Boolean,
    Map, Set, Uint32Array, Float32Array, ArrayBuffer, DataView,
  };
  vm.runInNewContext(workerSource(), context, { filename: 'layout.worker.js' });
  assert.equal(typeof self.onmessage, 'function', 'worker source installs self.onmessage');
  return {
    messages,
    send(data) { self.onmessage({ data }); },
    waitFor(predicate, timeoutMs = 10000) {
      const prior = messages.find(predicate);
      if (prior) return Promise.resolve(prior);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { listeners.delete(onMessage); reject(new Error(`worker timeout after ${timeoutMs}ms`)); }, timeoutMs);
        const onMessage = message => {
          if (!predicate(message)) return;
          clearTimeout(timer); listeners.delete(onMessage); resolve(message);
        };
        listeners.add(onMessage);
      });
    },
    settle(ms) { return new Promise(resolve => setTimeout(resolve, ms)); },
  };
}

async function testWorkerRevisionAndPause() {
  const worker = workerHarness();
  worker.send({ type: 'solve', revision: 41, ids: ['p', 'q'], edges: [0, 1] });
  worker.send({ type: 'pause' });
  await worker.settle(30);
  assert.equal(worker.messages.some(message => message.type === 'positions'), false, 'paused worker holds its pending solve');
  worker.send({ type: 'resume' });
  const result = await worker.waitFor(message => message.type === 'positions');
  assert.equal(result.revision, 41, 'resumed solve retains revision');
  assert.ok(result.positions instanceof ArrayBuffer || ArrayBuffer.isView(result.positions), 'worker transfers/returns packed positions');

  worker.send({ type: 'solve', revision: 42, ids: ['p', 'q'], edges: [0, 1] });
  worker.send({ type: 'solve', revision: 43, ids: ['p', 'q'], edges: [0, 1] });
  const latest = await worker.waitFor(message => message.type === 'positions' && message.revision === 43);
  assert.equal(latest.revision, 43, 'latest solve revision completes');
  await worker.settle(30);
  assert.equal(worker.messages.some(message => message.type === 'positions' && message.revision === 42), false, 'superseded solve does not post stale positions');
}

// Planted partition as in the plugin audit: 20 groups of 50 notes, 12% of
// in-group pairs linked, plus 0.5 random cross links per note.
function plantedPartition() {
  let a = 1;
  const r = () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const groups = 20, size = 50, n = groups * size, pairs = [], seen = new Set();
  const add = (x, y) => { if (x === y) return; const k = x < y ? x + ',' + y : y + ',' + x; if (!seen.has(k)) { seen.add(k); pairs.push([x, y]); } };
  for (let g = 0; g < groups; g++) for (let i = 0; i < size; i++) for (let j = i + 1; j < size; j++) if (r() < 0.12) add(g * size + i, g * size + j);
  for (let k = 0; k < n / 2; k++) add(Math.floor(r() * n), Math.floor(r() * n));
  return { n, pairs, group: i => Math.floor(i / size), keys: Array.from({ length: n }, (_, i) => 'area/note-' + i + '.md') };
}
function solveFully(input) { const s = createSolver(input); let guard = 0; while (!s.settled && guard++ < 1000) s.step(10); return s; }
let plantedCache = null;
function plantedLayout() {
  if (!plantedCache) { const g = plantedPartition(); const s = solveFully({ keys: g.keys, edges: Uint32Array.from(g.pairs.flat()) }); plantedCache = { g, solver: s, pos: Float32Array.from(s.positions) }; }
  return plantedCache;
}

test('3D Hilbert curve: consecutive cells are face neighbours and every cell is visited once', () => {
  for (const bits of [1, 2, 3, 4]) {
    const side = 1 << bits, seen = new Set(); let prev = hilbertAxes(0, bits); seen.add(prev.join());
    for (let h = 1; h < side ** 3; h++) {
      const q = hilbertAxes(h, bits); seen.add(q.join());
      assert.equal(Math.abs(q[0] - prev[0]) + Math.abs(q[1] - prev[1]) + Math.abs(q[2] - prev[2]), 1, `bits ${bits} step ${h}`);
      prev = q;
    }
    assert.equal(seen.size, side ** 3);
  }
});

test('linked notes sit together: neighbour purity far above chance on a planted partition', () => {
  const { g, solver, pos } = plantedLayout();
  const d = (a, b) => Math.hypot(pos[3 * a] - pos[3 * b], pos[3 * a + 1] - pos[3 * b + 1], pos[3 * a + 2] - pos[3 * b + 2]);
  let linked = 0; for (const [a, b] of g.pairs) linked += d(a, b); linked /= g.pairs.length;
  let random = 0, count = 0; for (let i = 0; i < g.n; i += 7) for (let j = 3; j < g.n; j += 11) if (i !== j) { random += d(i, j); count++; } random /= count;
  let purity = 0, sample = 0;
  for (let i = 0; i < g.n; i += 5, sample++) {
    const near = [];
    for (let j = 0; j < g.n; j++) if (j !== i) near.push([d(i, j), j]);
    near.sort((x, y) => x[0] - y[0]);
    purity += near.slice(0, 10).filter(([, j]) => g.group(j) === g.group(i)).length / 10;
  }
  purity /= sample;
  const chance = 49 / 999, stats = solver.stats();
  console.log('  planted partition: ' + JSON.stringify({ purity: +purity.toFixed(3), chance: +chance.toFixed(3), linkedOverRandom: +(linked / random).toFixed(3), iterations: solver.iterations, cv: +stats.cv.toFixed(3), empty: stats.empty }));
  assert.ok(purity >= 0.6, `10-nearest-neighbour purity ${purity} >= 0.6 (chance ${chance})`);
  assert.ok(linked / random <= 0.45, `linked pairs much closer than random pairs (ratio ${linked / random})`);
  assert.equal(stats.empty, 0, 'the sphere is still filled');
});

test('stability: with the rest pinned, one new link moves only its two notes', () => {
  const { g, pos } = plantedLayout();
  const pinned = new Uint8Array(g.n).fill(1); pinned[3] = 0; pinned[777] = 0;
  const s = solveFully({ keys: g.keys, edges: Uint32Array.from([...g.pairs.flat(), 3, 777]), positions: pos, pinned });
  const after = s.positions; let moved = 0;
  for (let i = 0; i < g.n; i++) if (Math.hypot(after[3 * i] - pos[3 * i], after[3 * i + 1] - pos[3 * i + 1], after[3 * i + 2] - pos[3 * i + 2]) > 1e-6) moved++;
  assert.equal(moved, 2, 'median and 90th percentile displacement are exactly zero');
  assert.ok(s.iterations <= 150, 'incremental solves are capped at 150 iterations');
  const cached = solveFully({ keys: g.keys, edges: Uint32Array.from(g.pairs.flat()), positions: pos, pinned: new Uint8Array(g.n).fill(1) });
  assert.equal(cached.freeCount, 0); assert.equal(cached.iterations, 0); assert.equal(cached.settled, true);
  assert.deepEqual(Array.from(cached.positions), Array.from(pos), 'a fully pinned solve returns the saved layout unchanged');
});

test('a new note is placed next to its neighbours without moving the others', () => {
  const { g, pos } = plantedLayout();
  const keys = g.keys.concat(['area/new-note.md']), n = keys.length;
  const positions = new Float32Array(3 * n); positions.set(pos); positions.fill(NaN, 3 * g.n);
  const pinned = new Uint8Array(n).fill(1); pinned[n - 1] = 0;
  const s = solveFully({ keys, edges: Uint32Array.from([...g.pairs.flat(), n - 1, 5, n - 1, 9]), positions, pinned });
  const p = s.positions, d = (a, b) => Math.hypot(p[3 * a] - p[3 * b], p[3 * a + 1] - p[3 * b + 1], p[3 * a + 2] - p[3 * b + 2]);
  for (let i = 0; i < 3 * g.n; i++) assert.equal(p[i], pos[i]);
  assert.ok(Math.min(d(n - 1, 5), d(n - 1, 9)) < 0.5, 'the new note lands near one of its neighbours');
});

test('determinism: the same graph gives the same layout whatever the input order or revision', () => {
  const g = plantedPartition(), n = g.n;
  const perm = Array.from({ length: n }, (_, i) => (i * 37 + 11) % n), inv = new Int32Array(n);
  perm.forEach((old, k) => { inv[old] = k; });
  const a = createSolver({ revision: 1, keys: g.keys, edges: Uint32Array.from(g.pairs.flat()) });
  const b = createSolver({ revision: 99, keys: perm.map(i => g.keys[i]), edges: Uint32Array.from(g.pairs.flatMap(([x, y]) => [inv[x], inv[y]])) });
  a.step(40); b.step(40);
  const pa = a.positions, pb = b.positions;
  for (let i = 0; i < n; i++) for (let c = 0; c < 3; c++) assert.equal(pa[3 * i + c], pb[3 * inv[i] + c], `note ${i} axis ${c}`);
});

test('shell slots are stable: one more unlinked note moves almost no other', () => {
  const keys = Array.from({ length: 900 }, (_, i) => 'orphan-' + i + '.md');
  const before = shellSlots(keys), after = shellSlots(keys.concat(['orphan-new.md']));
  let moved = 0;
  for (let i = 0; i < keys.length; i++) if (before[3 * i] !== after[3 * i] || before[3 * i + 1] !== after[3 * i + 1] || before[3 * i + 2] !== after[3 * i + 2]) moved++;
  assert.ok(moved <= keys.length * 0.02, `moved ${moved} of ${keys.length}`);
  for (let i = 0; i < before.length; i += 3) assert.ok(Math.abs(Math.hypot(before[i], before[i + 1], before[i + 2]) - 1) < 2e-5, 'on the unit shell');
  const unique = new Set(Array.from({ length: keys.length }, (_, i) => Array.from(before.slice(3 * i, 3 * i + 3)).join(',')));
  assert.equal(unique.size, keys.length, 'no two unlinked notes share a slot');
});

test('exports', () => {
  assert.equal(typeof createSolver, 'function');
  assert.equal(typeof surfaceSlots, 'function');
  assert.equal(typeof densityStats, 'function');
  assert.equal(typeof workerSource, 'function');
});
test('empty and single-note graphs', testEmptyAndSingle);
test('coincident input stays finite; orphans stay on the shell', testCoincidentAndShell);
test('shell rows have uniform longitude spacing', testUniformSurfaceRows);
test('orphans keep their own shell slots', testOrphansUseTheirOwnSurfaceSlots);
test('same input gives the same layout', testDeterminism);
test('Barnes-Hut insertion is permutation-equivariant', testBarnesHutPermutationEquivariance);
test('7,000-note star fixture settles within 300 iterations and still fills the sphere', testLargeFixture);
test('worker honours pause/resume and drops superseded solves', testWorkerRevisionAndPause);

run('layout');
