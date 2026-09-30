'use strict';

// Link render plan (src/edges.js) and the shared breathing field
// (src/field.js): the plan contains exactly the real links, curve endpoints
// sit on the notes, the async plan matches the sync plan.
const assert = require('node:assert/strict');
const { test, run } = require('./helpers/harness');
const { file } = require('./helpers/fixtures');
const { displace, frequencies } = require('../src/field');
const { BrainModel } = require('../src/graph');
const Edges = require('../src/edges');
const Palette = require('../src/palette');

function app(paths, resolvedLinks = {}) {
  const files = paths.map(p => file(p));
  return { files, vault: { getMarkdownFiles() { return files; } }, metadataCache: { resolvedLinks } };
}

test('field displacement stays within 0.12R and repeats every 600 s', () => {
  for (const [i, expected] of [200, 150, 300].entries()) assert.ok(Math.abs(2 * Math.PI / frequencies[i] - expected) < 1e-12);
  for (let i = 0; i < 192; i++) {
    const z = 1 - 2 * (i + 0.5) / 192, a = i * 2.399963229728653, r = [0.15, 0.62, 0.94, 1][i % 4];
    const xyz = [r * Math.sqrt(1 - z * z) * Math.cos(a), r * Math.sqrt(1 - z * z) * Math.sin(a), r * z];
    for (let time = 0; time <= 600; time += 37.5) {
      const p = displace(...xyz, time), q = displace(...xyz, time + 600);
      assert.ok(p.every(Number.isFinite));
      assert.ok(Math.abs(Math.hypot(...p) - r) <= r * 0.114 + 1e-12);
      assert.ok(Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]) < 1e-10);
    }
  }
});

test('edge plan has exactly the real links and cubic endpoints at the notes', () => {
  const model = new BrainModel(app(['a.md', 'b.md', 'c.md', 'orphan.md'], {
    'a.md': { 'b.md': 1, 'c.md': 1, 'missing.md': 1, 'a.md': 1 },
    'b.md': { 'a.md': 1, 'c.md': 1 },
    'c.md': { 'b.md': 0 },
  }));
  model.buildFromVault();
  for (const [i, n] of model.orderedNodes.entries()) n.pos = [i * 0.17 - 0.2, i * -0.11 + 0.1, 0.3 - i * 0.09];
  const plan = Edges.buildEdgePlan(model);
  Edges.updateGuides(plan);
  assert.equal(plan.n, 3);
  assert.deepEqual(new Set(plan.key), new Set(Array.from(model.edgeMap.values(), e => model.edgeKey(e.a, e.b))));
  const cubic = new Float64Array(12), point = [0, 0, 0];
  for (let i = 0; i < plan.n; i++) {
    Edges.edgeControlPoints(plan, i, cubic, 0);
    for (let c = 0; c < 3; c++) { assert.equal(cubic[c], plan.na[i].pos[c]); assert.equal(cubic[9 + c], plan.nb[i].pos[c]); }
    Edges.cubicPoint(cubic, 0, 0, point, 0); assert.deepEqual(point, Array.from(plan.na[i].pos));
    Edges.cubicPoint(cubic, 0, 1, point, 0); assert.deepEqual(point.map(v => +v.toFixed(12)), plan.nb[i].pos.map(v => +v.toFixed(12)));
  }
  assert.deepEqual(plan.colA[0], Palette.styleForDegree(plan.na[0].degree).color, 'default colours follow the degree palette');
});

test('adding and removing links is reversible', () => {
  const source = app(['a.md', 'b.md', 'c.md'], { 'a.md': { 'b.md': 1 } });
  const model = new BrainModel(source); model.buildFromVault();
  assert.equal(Edges.buildEdgePlan(model).n, 1);
  source.metadataCache.resolvedLinks = { 'a.md': { 'b.md': 1, 'c.md': 1 } }; model.recomputeEdges();
  assert.equal(Edges.buildEdgePlan(model).n, 2);
  source.metadataCache.resolvedLinks = {}; model.recomputeEdges();
  assert.equal(Edges.buildEdgePlan(model).n, 0);
});

test('async edge plan matches the sync plan, yields, and can be cancelled', async () => {
  const model = { nodes: new Map(), edgeMap: new Map(), edgeKey: (a, b) => [a, b].sort().join('|') };
  for (let i = 0; i < 12000; i++) model.nodes.set(`n${i}.md`, { path: `n${i}.md`, degree: i % 956, pos: [Math.sin(i) * 0.3, Math.cos(i * 0.3) * 0.3, Math.sin(i * 0.1) * 0.3], region: { key: 'all' } });
  for (let i = 0; i < 11999; i++) model.edgeMap.set(`e${i}`, { a: `n${i}.md`, b: `n${i + 1}.md` });
  const syncPlan = Edges.buildEdgePlan(model);
  const asyncPlan = await Edges.buildEdgePlanAsync(model, undefined, { sliceMs: 1 });
  assert.ok(asyncPlan.slices >= 1, 'yielded at least once');
  for (const key of ['n', 'key', 'width', 'colA', 'colB', 'baseAlpha']) assert.deepEqual(asyncPlan[key], syncPlan[key]);
  assert.deepEqual(asyncPlan.groups.map(g => g.edges), syncPlan.groups.map(g => g.edges));
  let cancel = false; setTimeout(() => { cancel = true; }, 0);
  const cancelled = await Edges.buildEdgePlanAsync(model, undefined, { sliceMs: 1, cancelled: () => cancel });
  assert.equal(cancelled, null, 'a cancelled plan is never returned');
});

test('edge alpha dims outside a focused region and under an overlay', () => {
  const region = k => ({ key: k });
  const plan = { baseAlpha: [0.1, 0.1], key: ['x', 'y'], na: [{ region: region('r1') }, { region: region('r2') }], nb: [{ region: region('r1') }, { region: region('r2') }] };
  assert.equal(Edges.edgeAlphaNow(plan, 0, null, null, 0), 0.1);
  assert.equal(Edges.edgeAlphaNow(plan, 0, 'r1', null, 0), 0.1);
  assert.ok(Math.abs(Edges.edgeAlphaNow(plan, 1, 'r1', null, 0) - 0.02) < 1e-12);
  assert.ok(Math.abs(Edges.edgeAlphaNow(plan, 0, null, null, 0, 0.3) - 0.03) < 1e-12);
  assert.ok(Edges.edgeAlphaNow(plan, 0, null, { x: 100 }, 200) > 0.1, 'a new link flashes');
  assert.equal(Edges.edgeAlphaNow.animating, true);
});

run('edges');
