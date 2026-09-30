'use strict';

// Contract test against traces written by the real context-layer writer:
// tests/fixtures/writer-trace-*.json were produced by
// tests/fixtures/make_writer_traces.py (fictional dev vault, real CLI).
// Run that script with --check to see whether the writer's format drifted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, run } = require('./helpers/harness');
const A = require('../src/activation');

const DOT = ' ' + String.fromCharCode(0xB7) + ' ';
const read = name => fs.readFileSync(path.join(__dirname, 'fixtures', name), 'utf8');
const depth2Text = read('writer-trace-depth2.json'), notFoundText = read('writer-trace-not-found.json');
const depth2 = JSON.parse(depth2Text), notFound = JSON.parse(notFoundText);

test('writer traces: version 1, random run id, no query, and every field the plugin reads', () => {
  for (const raw of [depth2, notFound]) {
    assert.equal(raw.version, 1);
    assert.match(raw.run_id, /^[0-9a-f]{32}$/);
    assert.equal(raw.query, null, 'the query is not recorded unless the user opted in');
    assert.equal(raw.method, 'synaptic');
    assert.ok(Number.isInteger(raw.budget_tokens));
    assert.ok(['PARTIAL', 'NOT_FOUND'].includes(raw.packet.status), 'the writer emits PARTIAL or NOT_FOUND');
    if ('mode' in raw) assert.ok(A.MODES.includes(raw.mode), 'a mode field, once written, is superset or compact');
    const t = A.parseActivation(depth2 === raw ? depth2Text : notFoundText);
    assert.ok(t, 'the plugin accepts the writer output');
    assert.equal(t.nodes.length, raw.nodes.length, 'no node is dropped');
    assert.equal(t.edges.length, raw.edges.length, 'every edge kind is one the plugin draws');
  }
  assert.equal(depth2.packet.status, 'PARTIAL');
  assert.equal(notFound.packet.status, 'NOT_FOUND');
  assert.deepEqual(notFound.nodes, []);
});

test('writer statuses render truthfully in the HUD', () => {
  const t = A.parseActivation(depth2Text), n = A.parseActivation(notFoundText);
  const mode = depth2.mode ? ' ' + depth2.mode : '';
  assert.equal(A.formatHud(t, t.generatedAt + 1000),
    'at ' + depth2.generated_at + DOT + 'synaptic' + mode + DOT + depth2.packet.passages + ' passages' + DOT + '~' + depth2.packet.est_tokens + ' tokens' + DOT + 'just now',
    'PARTIAL adds nothing: the passage count already says evidence was found');
  assert.equal(A.formatHud(n, n.generatedAt + 1000),
    'at ' + notFound.generated_at + DOT + 'synaptic' + (notFound.mode ? ' ' + notFound.mode : '') + DOT + '0 passages' + DOT + '~0 tokens' + DOT + 'just now' + DOT + 'no evidence found');
});

test('writer edge semantics: an edge leaves a note one hop closer to the seeds', () => {
  const hop = new Map(depth2.nodes.map(n => [n.path, n.hop]));
  for (const e of depth2.edges) {
    assert.ok(hop.has(e.from) && hop.has(e.to), 'both ends of a written edge are listed nodes');
    assert.ok(hop.get(e.to) <= hop.get(e.from) + 1, 'the target is at most one step further out');
  }
  // The fixture keeps the case the old plugin drew wrong: a depth-2 edge into
  // a seed (the target hop says 1, the traversal depth is 2).
  assert.ok(depth2.edges.some(e => hop.get(e.from) === 1 && hop.get(e.to) === 0), 'fixture has a depth-2 backlink into a seed');
  const nodes = new Map(depth2.nodes.map((n, i) => [n.path, { path: n.path, pos: [i * 0.01, 0, 0] }]));
  const ov = A.mapOverlay(A.parseActivation(depth2Text), p => nodes.get(p));
  for (const e of ov.edges) {
    const depths = depth2.edges.filter(x => [x.from, x.to].sort().join('|') === [e.from, e.to].sort().join('|')).map(x => hop.get(x.from) + 1);
    assert.equal(e.hop, Math.min(...depths), e.from + ' - ' + e.to + ' pulses at its earliest traversal depth');
  }
});

test('writer trace: a link and its reverse become one ribbon', () => {
  const pairs = depth2.edges.map(e => [e.from, e.to].sort().join('|'));
  assert.ok(pairs.length > new Set(pairs).size, 'fixture has a link written in both directions');
  const nodes = new Map(depth2.nodes.map(n => [n.path, { path: n.path, pos: [0, 0, 0] }]));
  const ov = A.mapOverlay(A.parseActivation(depth2Text), p => nodes.get(p));
  assert.equal(ov.edges.length, new Set(pairs).size);
  assert.equal(ov.matched, depth2.nodes.length);
  assert.equal(ov.droppedNodes, 0);
});

test('writer trace mapped onto a vault with other paths: nothing matches, nothing is shown', () => {
  const ov = A.mapOverlay(A.parseActivation(depth2Text), p => undefined);
  assert.equal(ov.nodes.size, 0);
  assert.equal(ov.total, depth2.nodes.length, 'the view reports 0 of N notes instead of showing an empty overlay');
});

test('writer edges carry `hop`, the traversal step; the plugin uses it as the ribbon depth', () => {
  assert.ok(depth2.edges.every(e => Number.isInteger(e.hop) && e.hop >= 1), 'the writer emits an integer hop on every edge');
  const t = A.parseActivation(depth2Text);
  assert.deepEqual(t.edges.map(e => e.depth).sort(), depth2.edges.map(e => e.hop).sort());
  const hop = new Map(depth2.nodes.map(n => [n.path, n.hop]));
  for (const e of depth2.edges) assert.equal(e.hop, hop.get(e.from) + 1, 'hop equals the depth the plugin derived before');
  assert.equal(A.parseActivation(JSON.stringify({ version: 1, generated_at: '2026-09-24T12:00:00Z', nodes: [],
    edges: [{ from: 'a.md', to: 'b.md', kind: 'wikilink', weight: 1, depth: 2, hop: 1 }] })).edges[0].depth, 2, 'depth wins when both are present');
});

run('contract');
