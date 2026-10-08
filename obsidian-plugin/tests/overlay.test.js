'use strict';

// Overlay state mapping: which notes and links light up for a trace, how
// brightly, in which hop order, and how the view writes that into its point
// buffers (activated notes stand out, everything else dims).
const assert = require('node:assert/strict');
const { test, run } = require('./helpers/harness');
const fakes = require('./helpers/fakes');
fakes.hookObsidianRequire();
fakes.installGlobals();
const A = require('../src/activation');
const Palette = require('../src/palette');
const { BrainView } = require('../src/view');

const luminance = c => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];

function sampleTrace() {
  return A.parseActivation(JSON.stringify({
    version: 1, generated_at: '2026-09-24T12:00:00Z', method: 'synaptic', budget_tokens: 800,
    nodes: [
      { path: 'seed.md', activation: 1, hop: 0, role: 'seed', selected: true },
      { path: 'near.md', activation: 0.6, hop: 1, role: 'hop', selected: true },
      { path: 'far.md', activation: 0.2, hop: 2, role: 'hop', selected: false },
      { path: 'deleted-since.md', activation: 0.5, hop: 1, role: 'hop', selected: false },
    ],
    edges: [
      { from: 'near.md', to: 'far.md', kind: 'wikilink', weight: 0.2 },
      { from: 'seed.md', to: 'near.md', kind: 'embed', weight: 0.6 },
      { from: 'seed.md', to: 'deleted-since.md', kind: 'wikilink', weight: 0.5 },
      { from: 'seed.md', to: 'bystander.md', kind: 'backlink', weight: 0.1 },
    ],
    packet: { passages: 3, est_tokens: 400, status: 'OK' },
  }));
}

function sampleNodes() {
  const mk = (path, degree, pos, role = 'linked') => ({ id: path + ':0', path, title: path.replace('.md', ''), degree, role, pos, region: { key: '' } });
  return new Map([
    ['seed.md', mk('seed.md', 3, [0.1, 0.1, 0.1])],
    ['near.md', mk('near.md', 2, [0.3, 0.1, 0])],
    ['far.md', mk('far.md', 1, [0.5, -0.2, 0.1])],
    ['bystander.md', mk('bystander.md', 1, [-0.4, 0.2, 0.3])],
    ['lonely.md', mk('lonely.md', 0, [0, 0, 1], 'orphan')],
  ]);
}

test('unknown paths are dropped; nodes keep their trace values', () => {
  const nodes = sampleNodes();
  const ov = A.mapOverlay(sampleTrace(), p => nodes.get(p));
  assert.deepEqual(Array.from(ov.nodes.keys()), ['seed.md', 'near.md', 'far.md']);
  assert.equal(ov.droppedNodes, 1);
  assert.equal(ov.droppedEdges, 1, 'an edge to a note that no longer exists is dropped');
  assert.equal(ov.nodes.get('near.md').node, nodes.get('near.md'));
  assert.equal(ov.maxHop, 2);
});

test('seeds are visually distinct from hop notes; brightness and size grow with activation', () => {
  const seed = A.overlayNodeStyle({ activation: 1, role: 'seed', selected: true });
  const hopHigh = A.overlayNodeStyle({ activation: 1, role: 'hop', selected: true });
  const hopLow = A.overlayNodeStyle({ activation: 0.1, role: 'hop', selected: false });
  assert.deepEqual(seed.color, Array.from(A.SEED_RGB));
  assert.ok(Math.abs(seed.color[0] - seed.color[2]) > 0.4 && Math.abs(hopHigh.color[0] - hopHigh.color[2]) < 0.2, 'seed is warm, hop is cool/white');
  assert.ok(seed.sizeMult > hopHigh.sizeMult, 'seed is larger at equal activation');
  assert.ok(hopHigh.sizeMult > hopLow.sizeMult && luminance(hopHigh.color) > luminance(hopLow.color));
  assert.ok(hopHigh.alpha > hopLow.alpha);
  const reached = A.overlayNodeStyle({ activation: 1, role: 'hop', selected: false });
  const sat = c => Math.max(...c) - Math.min(...c);
  assert.ok(sat(reached.color) < sat(hopHigh.color) - 0.2 || sat(hopHigh.color) < 0.2 && luminance(reached.color) < luminance(hopHigh.color) - 0.15, 'reached-only notes are muted compared with notes in the packet');
  assert.ok(reached.alpha < hopHigh.alpha - 0.2, 'reached-only notes are fainter');
  const seedReached = A.overlayNodeStyle({ activation: 1, role: 'seed', selected: false });
  assert.ok(sat(seedReached.color) < sat(seed.color), 'a seed without a passage is muted too');
  for (let a = 0; a <= 1; a += 0.1) {
    const s = A.overlayNodeStyle({ activation: a, role: 'hop', selected: false }), t = A.overlayNodeStyle({ activation: Math.min(1, a + 0.1), role: 'hop', selected: false });
    assert.ok(t.sizeMult >= s.sizeMult && luminance(t.color) >= luminance(s.color) - 1e-12, 'monotonic in activation');
  }
});

test('edges are ordered by hop and colored from their endpoints', () => {
  const nodes = sampleNodes();
  const ov = A.mapOverlay(sampleTrace(), p => nodes.get(p));
  assert.deepEqual(ov.edges.map(e => [e.from, e.to, e.hop]), [
    ['seed.md', 'near.md', 1], ['seed.md', 'bystander.md', 1], ['near.md', 'far.md', 2]]);
  assert.equal(ov.maxEdgeHop, 2);
  const first = ov.edges[0];
  assert.deepEqual(first.fromColor, ov.nodes.get('seed.md').color);
  assert.deepEqual(first.toColor, ov.nodes.get('near.md').color);
  assert.equal(ov.edges[1].b, nodes.get('bystander.md'), 'an endpoint in the vault but outside the node list is still drawn');
});

test('pulses run in hop order, one travel per cycle, and stay within the cycle', () => {
  const cycle = A.pulseCycleMs(2);
  assert.equal(cycle, 2 * A.PULSE_STEP_MS + A.PULSE_TRAVEL_MS + A.PULSE_REST_MS);
  assert.ok(A.pulseAt(1, 2, 0) && A.pulseAt(1, 2, 0).t === 0, 'hop 1 starts immediately');
  assert.equal(A.pulseAt(2, 2, 0), null, 'hop 2 waits');
  assert.ok(A.pulseAt(2, 2, A.PULSE_STEP_MS + 1), 'hop 2 starts one step later');
  const firstStart = t => { for (let ms = 0; ms < cycle; ms += 10) if (A.pulseAt(t, 2, ms)) return ms; return -1; };
  assert.ok(firstStart(1) < firstStart(2));
  assert.equal(A.pulseAt(1, 2, A.PULSE_TRAVEL_MS + 5), null, 'a pulse finishes after its travel time');
  assert.ok(A.pulseAt(1, 2, cycle + 10), 'the cycle repeats');
  assert.equal(A.pulseAt(1, 2, -5), null);
  const mid = A.pulseAt(1, 1, A.PULSE_TRAVEL_MS / 2);
  assert.ok(Math.abs(mid.t - 0.5) < 1e-9 && Math.abs(mid.glow - 1) < 1e-9);
});

test('framing finds the weighted centre and the spread of activated notes', () => {
  const f = A.frameNodes([{ pos: [1, 0, 0], weight: 1 }, { pos: [-1, 0, 0], weight: 1 }, { pos: [0, 1, 0], weight: 0 }]);
  assert.ok(Math.abs(f.center[0]) < 1e-9 && f.center[1] > 0 && f.center[1] < 0.1, 'zero weight still counts a little');
  assert.ok(f.radius >= 1);
  assert.equal(A.frameNodes([]), null);
  assert.equal(A.frameNodes([{ pos: null, weight: 1 }]), null);
});

function viewDouble(nodes, overlay, settings = {}) {
  const uploads = new Map(); let bound = null;
  const ordered = Array.from(nodes.values());
  const view = Object.assign(Object.create(BrainView.prototype), {
    plugin: { settings: Object.assign({ paletteMode: 'degree', showOrphans: true }, settings) },
    gl: { ARRAY_BUFFER: 1, DYNAMIC_DRAW: 2, bindBuffer(_t, b) { bound = b; }, bufferData(_t, data) { if (bound) uploads.set(bound, Array.from(data)); } },
    buffers: { nodePos: 'pos', nodeColor: 'color', nodeSize: 'size', nodeShell: 'shell' },
    model: { orderedNodes: ordered, nodes, structureVersion: 1, orphanCount: 1 },
    hoverNode: null, focusRegionKey: null, overlay,
    isVisible() { return true; },
  });
  view.rebuildNodeBuffers();
  const drawn = view.renderNodeList();
  const at = path => { const i = drawn.findIndex(n => n.path === path); const c = uploads.get('color'); return { rgba: c.slice(i * 4, i * 4 + 4), size: uploads.get('size')[i] }; };
  return { view, at, uploads };
}

test('view buffers: activated notes brighter and larger, others dimmed', () => {
  const nodes = sampleNodes();
  const base = viewDouble(nodes, null);
  const ov = A.mapOverlay(sampleTrace(), p => nodes.get(p));
  const lit = viewDouble(nodes, ov);
  const seedBefore = base.at('seed.md'), seedAfter = lit.at('seed.md');
  assert.ok(seedAfter.size > seedBefore.size * 2, 'seed grows');
  assert.ok(seedAfter.rgba[3] > seedBefore.rgba[3], 'seed brighter');
  assert.deepEqual(seedAfter.rgba.slice(0, 3).map(v => +v.toFixed(5)), Array.from(A.SEED_RGB).map(v => +v.toFixed(5)));
  const nearAfter = lit.at('near.md');
  assert.ok(Math.abs(nearAfter.rgba[0] - seedAfter.rgba[0]) > 0.2 || Math.abs(nearAfter.rgba[2] - seedAfter.rgba[2]) > 0.2, 'hop colour differs from seed colour');
  for (const p of ['bystander.md', 'lonely.md']) {
    const before = base.at(p), after = lit.at(p);
    assert.ok(Math.abs(after.rgba[3] - before.rgba[3] * 0.22) < 1e-6, p + ' is dimmed');
    assert.equal(after.size, before.size, p + ' keeps its size');
    assert.deepEqual(after.rgba.slice(0, 3), before.rgba.slice(0, 3), p + ' keeps its colour');
  }
  assert.deepEqual(base.at('bystander.md').rgba.slice(0, 3).map(v => +v.toFixed(5)), Palette.styleForDegree(1).color.map(v => +v.toFixed(5)));
});

test('view buffers: an overlay entry for a replaced node object is not applied', () => {
  const nodes = sampleNodes();
  const ov = A.mapOverlay(sampleTrace(), p => nodes.get(p));
  const replaced = Object.assign({}, nodes.get('seed.md'));
  nodes.set('seed.md', replaced);
  const lit = viewDouble(nodes, ov);
  const base = viewDouble(nodes, null);
  assert.ok(Math.abs(lit.at('seed.md').rgba[3] - base.at('seed.md').rgba[3] * 0.22) < 1e-6);
});

test('view buffers: hidden orphans are not uploaded', () => {
  const nodes = sampleNodes();
  const { uploads } = viewDouble(nodes, null, { showOrphans: false });
  assert.equal(uploads.get('size').length, 4);
  assert.deepEqual(uploads.get('shell'), [0, 0, 0, 0]);
});

// A trace shaped like the writer's two-hop output: A and C are seeds; B was
// reached from A (depth 1); at depth 2 the writer followed B's links back to A
// and across to C (backlinks), and on to D.
function depthTrace() {
  return A.parseActivation(JSON.stringify({
    version: 1, generated_at: '2026-09-24T12:00:00Z', method: 'synaptic', budget_tokens: 600,
    nodes: [
      { path: 'a.md', activation: 1, hop: 0, role: 'seed', selected: true },
      { path: 'c.md', activation: 0.7, hop: 0, role: 'seed', selected: true },
      { path: 'b.md', activation: 0.4, hop: 1, role: 'hop', selected: true },
      { path: 'd.md', activation: 0.2, hop: 2, role: 'hop', selected: false },
    ],
    edges: [
      { from: 'a.md', to: 'b.md', kind: 'wikilink', weight: 0.4 },
      { from: 'b.md', to: 'a.md', kind: 'backlink', weight: 0.1 },
      { from: 'b.md', to: 'c.md', kind: 'backlink', weight: 0.05 },
      { from: 'b.md', to: 'd.md', kind: 'wikilink', weight: 0.2 },
    ],
    packet: { passages: 3, est_tokens: 300, status: 'PARTIAL' },
  }));
}
function depthNodes() {
  const mk = (path, degree, pos, role = 'linked') => ({ id: path + ':0', path, title: path, degree, role, pos, region: { key: '' } });
  return new Map([['a.md', mk('a.md', 2, [0, 0, 0])], ['b.md', mk('b.md', 3, [0.1, 0, 0])], ['c.md', mk('c.md', 1, [0.2, 0, 0])],
    ['d.md', mk('d.md', 1, [0.3, 0, 0])], ['e.md', mk('e.md', 0, [0, 0, 1], 'orphan')]]);
}

test('links pulse at the step that used them: depth = hop(from) + 1, not the hop of the target', () => {
  const nodes = depthNodes();
  const ov = A.mapOverlay(depthTrace(), p => nodes.get(p));
  const hopOf = new Map(ov.edges.map(e => [[e.from, e.to].sort().join(' '), e.hop]));
  assert.equal(hopOf.get('b.md c.md'), 2, 'a depth-2 backlink into a seed pulses at depth 2 (the old rule said 1)');
  assert.equal(hopOf.get('b.md d.md'), 2);
  assert.equal(hopOf.get('a.md b.md'), 1);
  assert.equal(ov.maxEdgeHop, 2);
});

test('a link and its reverse are one ribbon, at the earliest depth, with both kinds listed', () => {
  const nodes = depthNodes();
  const ov = A.mapOverlay(depthTrace(), p => nodes.get(p));
  const pairs = ov.edges.map(e => [e.from, e.to].sort().join(' '));
  assert.equal(new Set(pairs).size, pairs.length, 'one ribbon per note pair');
  assert.equal(ov.edges.length, 3);
  const ab = ov.edges.find(e => e.from === 'a.md' && e.to === 'b.md');
  assert.deepEqual(ab.kinds, ['backlink', 'wikilink']);
  assert.equal(ab.hop, 1); assert.equal(ab.links, 2);
  assert.equal(ab.a, nodes.get('a.md'), 'the pulse runs in the direction first traversed');
});

test('legacy advisor annotations cannot alter the retrieval overlay', () => {
  const nodes = depthNodes();
  const plain = A.mapOverlay(depthTrace(), p => nodes.get(p));
  const raw = JSON.parse(JSON.stringify({
    version: 1, generated_at: '2026-09-24T12:00:00Z', method: 'synaptic',
    nodes: [{ path: 'b.md', activation: 0.4, hop: 1, role: 'hop', selected: true, jev: 'rescued' }],
    edges: [], jev: { mode: 'on', applied: true, rescued: 1 },
  }));
  const parsed = A.parseActivation(JSON.stringify(raw));
  assert.equal('advisor' in parsed, false);
  assert.equal('jev' in parsed.nodes[0], false);
  assert.equal('mark' in A.mapOverlay(parsed, p => nodes.get(p)).nodes.get('b.md'), false);
  assert.equal('advisor' in plain, false);
});

test('with unlinked notes hidden, a note of the shown retrieval is still drawn', () => {
  const nodes = depthNodes();
  const trace = A.parseActivation(JSON.stringify({ version: 1, generated_at: '2026-09-24T12:00:00Z', method: 'synaptic',
    nodes: [{ path: 'a.md', activation: 1, hop: 0, role: 'seed', selected: true }, { path: 'e.md', activation: 0.5, hop: 1, role: 'hop', selected: true }],
    edges: [{ from: 'a.md', to: 'e.md', kind: 'frontmatter', weight: 0.5 }] }));
  const ov = A.mapOverlay(trace, p => nodes.get(p));
  const { view } = viewDouble(nodes, ov, { showOrphans: false });
  const drawn = view.renderNodeList().map(n => n.path);
  assert.ok(drawn.includes('e.md'), 'the ribbon to e.md ends at a drawn note');
  assert.ok(!viewDouble(nodes, null, { showOrphans: false }).view.renderNodeList().some(n => n.path === 'e.md'), 'without the overlay it stays hidden');
});

run('overlay');
