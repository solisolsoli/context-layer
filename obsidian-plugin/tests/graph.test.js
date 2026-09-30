'use strict';

// Lifecycle tests for the presentation graph (src/graph.js) against fake
// vault/cache objects. No note content is read or written.
const assert = require('node:assert/strict');
const { test, run } = require('./helpers/harness');
const { file, folderTree, generateVault } = require('./helpers/fixtures');
const { BrainModel } = require('../src/graph');
const Layout = require('../src/layout');

const byFolder = f => { const i = f.path.indexOf('/'); return i > 0 ? f.path.slice(0, i) : ''; };

function app(files, resolvedLinks = {}) {
  return { vault: { files, getMarkdownFiles() { return this.files.filter(f => f.extension === 'md'); } }, metadataCache: { resolvedLinks } };
}
function edgePairs(model) {
  return Array.from(model.edgeMap.values(), ({ a, b }) => [a, b].sort().join(' <-> ')).sort();
}
function fakeWorker(posted) {
  return { onmessage: null, onerror: null, postMessage(message) { posted.push(message); }, terminate() { this.terminated = true; } };
}

test('only positive resolved links between distinct Markdown notes become edges', () => {
  const files = [file('a.md'), file('b.md'), file('c.md'), file('x.txt')];
  const model = new BrainModel(app(files, {
    'a.md': { 'b.md': 1, 'c.md': 0, 'missing.md': 2, 'a.md': 9, 'x.txt': 1 },
    'b.md': { 'a.md': 2, 'c.md': -1 },
    'x.txt': { 'a.md': 7 },
  }));
  model.buildFromVault();
  assert.deepEqual(edgePairs(model), ['a.md <-> b.md'], 'reciprocal link is one undirected edge');
  assert.equal(model.nodes.has('x.txt'), false, 'non-Markdown file never becomes a node');
  assert.equal(model.nodes.get('c.md').role, 'orphan');
  assert.equal(model.nodes.get('a.md').role, 'linked');
});

test('rename keeps node identity and moves edges to the new path', () => {
  const original = file('topic/old.md');
  const files = [original, file('topic/neighbor.md')];
  const source = app(files, { 'topic/old.md': { 'topic/neighbor.md': 1 } });
  const model = new BrainModel(source, { classify: byFolder }); model.buildFromVault();
  const node = model.nodes.get(original.path);
  const renamed = file('other/new.md');
  source.vault.files = [renamed, files[1]];
  source.metadataCache.resolvedLinks = { 'other/new.md': { 'topic/neighbor.md': 1 } };
  model.rename(renamed, original.path);
  assert.equal(model.nodes.has(original.path), false);
  assert.equal(model.nodes.get(renamed.path), node, 'rename preserves the node object');
  assert.equal(node.regionKey, 'other', 'region follows the new folder');
  assert.deepEqual(edgePairs(model), ['other/new.md <-> topic/neighbor.md']);

  const textFile = file('other/new.txt');
  source.vault.files = [textFile, files[1]];
  model.rename(textFile, renamed.path);
  assert.equal(model.nodes.has(renamed.path), false, 'renaming to a non-Markdown file removes the note');
  assert.equal(model.nodes.has(textFile.path), false);
});

test('delete then recreate yields a fresh node object', () => {
  const oldFile = file('same.md'); const source = app([oldFile]);
  const model = new BrainModel(source); model.buildFromVault();
  const oldNode = model.nodes.get(oldFile.path);
  source.vault.files = []; model.buildFromVault();
  assert.equal(model.nodes.has(oldFile.path), false);
  const again = file('same.md'); again.basename = 'same again';
  source.vault.files = [again]; model.buildFromVault();
  const replacement = model.nodes.get(oldFile.path);
  assert.ok(replacement && replacement !== oldNode);
  assert.equal(replacement.title, 'same again');
});

test('removePath removes a folder subtree and reports surviving neighbours', () => {
  const files = [file('keep.md'), file('gone/a.md'), file('gone/deep/b.md'), file('gone-not/c.md')];
  const source = app(files, { 'keep.md': { 'gone/a.md': 1, 'gone-not/c.md': 1 }, 'gone/a.md': { 'gone/deep/b.md': 1 } });
  const model = new BrainModel(source); model.buildFromVault();
  source.vault.files = [files[0], files[3]];
  const neighbours = model.removePath('gone');
  assert.deepEqual(Array.from(model.nodes.keys()).sort(), ['gone-not/c.md', 'keep.md'], 'prefix match respects folder boundaries');
  assert.deepEqual(neighbours, ['keep.md']);
  assert.equal(model.buildFromVault(), true, 'links are reconciled by the next build');
  assert.deepEqual(edgePairs(model), ['gone-not/c.md <-> keep.md']);
});

test('removing one deleted note is constant work and never scans the vault', () => {
  const files = [file('a.md'), file('b.md'), file('c.md')];
  const source = app(files, { 'a.md': { 'b.md': 1 }, 'b.md': { 'c.md': 1 } });
  const model = new BrainModel(source); model.buildFromVault();
  let scanned = 0;
  const realValues = model.nodes.values.bind(model.nodes);
  model.nodes.values = () => { scanned++; return realValues(); };
  assert.deepEqual(model.removePath('b.md').sort(), ['a.md', 'c.md']);
  assert.equal(scanned, 0, 'an exact note path is removed without walking all notes');
  delete model.nodes.values;
  assert.equal(model.nodes.has('b.md'), false);
  source.vault.files = [files[0], files[2]]; source.metadataCache.resolvedLinks = {};
  model.buildFromVault();
  assert.ok(model.orderedNodes.every(n => n.role === 'orphan'));
});

test('a renamed note keeps its node, position and links, and does not restart the layout', () => {
  const files = [file('x/a.md'), file('x/b.md'), file('c.md')];
  const source = app(files, { 'x/a.md': { 'x/b.md': 1 }, 'c.md': { 'x/a.md': 1 } });
  const model = new BrainModel(source, { classify: byFolder }); model.buildFromVault();
  const node = model.nodes.get('x/a.md'); node.pos = [0.1, 0.2, 0.3];
  const structure = model.structureVersion, view = model.viewRevision;
  const renamed = file('y/a.md');
  assert.equal(model.rename(renamed, 'x/a.md'), true);
  assert.equal(model.nodes.get('y/a.md'), node);
  assert.deepEqual(edgePairs(model), ['c.md <-> y/a.md', 'x/b.md <-> y/a.md'], 'links move with the note at once');
  source.vault.files = [renamed, files[1], files[2]];
  source.metadataCache.resolvedLinks = { 'y/a.md': { 'x/b.md': 1 }, 'c.md': { 'y/a.md': 1 } };
  assert.equal(model.buildFromVault(), false, 'same links under new paths: no structural change');
  assert.equal(model.structureVersion, structure, 'the layout is not restarted');
  assert.ok(model.viewRevision > view, 'render caches keyed by path are refreshed');
  assert.deepEqual(node.pos, [0.1, 0.2, 0.3]);
  assert.equal(node.regionKey, 'y');
  // A folder rename re-keys every note below it.
  const folder = { path: 'z', name: 'z', children: [] };
  assert.equal(model.rename(folder, 'x'), true);
  assert.ok(model.nodes.has('z/b.md') && !model.nodes.has('x/b.md'));
});

test('lookup falls back to Unicode-normalized paths', () => {
  const nfd = 'places/Cafe' + String.fromCharCode(0x301) + '.md', nfc = 'places/Caf' + String.fromCharCode(0xE9) + '.md';
  const model = new BrainModel(app([file(nfd), file('b.md')], { 'b.md': { [nfd]: 1 } })); model.buildFromVault();
  assert.equal(model.lookup(nfc), model.nodes.get(nfd), 'an NFC trace path finds an NFD vault note');
  assert.equal(model.lookup(nfd), model.nodes.get(nfd));
  assert.equal(model.lookup('missing.md'), undefined);
});

function linkedModel(cache) {
  const files = ['a.md', 'b.md', 'c.md', 'd.md', 'lonely.md'].map(p => file(p));
  const source = app(files, { 'a.md': { 'b.md': 1, 'c.md': 1 }, 'c.md': { 'd.md': 1 } });
  const model = new BrainModel(source, { positionCache: cache || {} }); model.buildFromVault();
  return { model, source };
}

test('Remember layout: a cache that still matches every note posts no solve at all', () => {
  const first = linkedModel();
  const posted = [];
  first.model.attachWorker(fakeWorker(posted), e => { throw e; }, () => {});
  const solve = posted.find(m => m.type === 'solve');
  assert.ok(solve && solve.pinned.every(v => v === 0), 'no cache: a full solve with nothing pinned');
  assert.ok(Array.from(solve.positions).filter(Number.isNaN).length >= 12, 'unknown positions are left to the worker');
  const settledAt = new Float32Array(first.model.orderedNodes.length * 3).map((_, i) => (i % 7) * 0.01);
  first.model.worker.onmessage({ data: { type: 'positions', revision: solve.revision, positions: settledAt.buffer, settled: true, iterations: 9, stats: {} } });
  first.model.advance(1, true);
  const cache = {};
  for (const n of first.model.connectedNodes) cache[n.path] = { x: n.target[0], y: n.target[1], z: n.target[2], h: first.model.neighbourHash(n.path) };
  const next = linkedModel(cache), posted2 = [];
  next.model.attachWorker(fakeWorker(posted2), e => { throw e; }, () => {});
  assert.equal(posted2.filter(m => m.type === 'solve').length, 0, 'unchanged vault: the saved layout is used as it is');
  assert.equal(next.model.workerState, 'settled');
  for (const n of next.model.connectedNodes) assert.deepEqual(n.pos, [cache[n.path].x, cache[n.path].y, cache[n.path].z]);
});

test('Remember layout: only notes whose links changed are re-solved', () => {
  const first = linkedModel();
  const cache = {};
  first.model.connectedNodes.forEach((n, i) => { cache[n.path] = { x: i * 0.1, y: 0, z: 0, h: first.model.neighbourHash(n.path) }; });
  cache['b.md'].h = 12345; // b.md gained or lost a link since the cache was written
  const next = linkedModel(cache), posted = [];
  next.model.attachWorker(fakeWorker(posted), e => { throw e; }, () => {});
  const solve = posted.find(m => m.type === 'solve');
  const pinnedPaths = solve.keys.filter((k, i) => solve.pinned[i]);
  assert.deepEqual(pinnedPaths.sort(), ['a.md', 'c.md', 'd.md']);
  assert.equal(solve.pinned[solve.keys.indexOf('b.md')], 0);
  // Within the session, one new link frees only its two endpoints.
  next.model.worker.onmessage({ data: { type: 'positions', revision: solve.revision, positions: new Float32Array(solve.keys.length * 3).buffer, settled: true, iterations: 3, stats: {} } });
  next.source.metadataCache.resolvedLinks = { 'a.md': { 'b.md': 1, 'c.md': 1 }, 'c.md': { 'd.md': 1 }, 'b.md': { 'd.md': 1 } };
  next.model.recomputeEdges();
  const again = posted.filter(m => m.type === 'solve').at(-1);
  assert.deepEqual(again.keys.filter((k, i) => again.pinned[i]).sort(), ['a.md', 'c.md']);
  assert.equal(next.model.layoutRequests, 2);
});

test('removing the last link turns both notes into orphans', () => {
  const source = app([file('a.md'), file('b.md')], { 'a.md': { 'b.md': 1 } });
  const model = new BrainModel(source); model.buildFromVault();
  assert.equal(model.connectedNodes.length, 2);
  source.metadataCache.resolvedLinks = {};
  assert.equal(model.recomputeEdges(), true);
  assert.deepEqual(edgePairs(model), []);
  assert.ok(Array.from(model.nodes.values()).every(n => n.role === 'orphan' && n.degree === 0));
});

test('a stale worker revision is rejected and its buffer recycled', () => {
  const source = app([file('a.md'), file('b.md')], { 'a.md': { 'b.md': 1 } });
  const model = new BrainModel(source); model.buildFromVault();
  const posted = [], errors = [];
  const worker = fakeWorker(posted);
  model.attachWorker(worker, e => errors.push(e), () => {});
  const staleRevision = posted.find(m => m.type === 'solve').revision;
  source.vault.files.push(file('c.md'));
  source.metadataCache.resolvedLinks = { 'a.md': { 'b.md': 1, 'c.md': 1 } };
  model.buildFromVault();
  const current = posted.filter(m => m.type === 'solve').at(-1).revision;
  assert.ok(current > staleRevision);
  worker.onmessage({ data: { type: 'positions', revision: staleRevision, positions: new ArrayBuffer(model.orderedNodes.length * 12), settled: true, iterations: 1, stats: {} } });
  assert.equal(model.staleResults, 1);
  assert.equal(model.pendingPositions, undefined);
  assert.equal(errors.length, 0);
  assert.ok(posted.some(m => m.type === 'recycle'));
});

test('current worker positions are applied and eased in', () => {
  const source = app([file('a.md'), file('b.md')], { 'a.md': { 'b.md': 1 } });
  const model = new BrainModel(source); model.buildFromVault();
  const posted = [];
  const worker = fakeWorker(posted);
  let settled = 0;
  model.attachWorker(worker, e => { throw e; }, () => settled++);
  const revision = posted.find(m => m.type === 'solve').revision;
  const target = new Float32Array([0.1, 0.2, 0.3, -0.1, -0.2, -0.3]);
  worker.onmessage({ data: { type: 'positions', revision, positions: target.buffer, settled: true, iterations: 5, stats: {} } });
  for (let i = 0; i < 200; i++) model.advance(0.05, false);
  assert.ok(Math.abs(model.orderedNodes[0].pos[0] - 0.1) < 1e-4);
  assert.equal(settled, 1, 'settled callback fires once after motion stops');
  model.advance(0.05, false);
  assert.equal(settled, 1);
});

test('the public folder tree matches getMarkdownFiles without calling it', () => {
  const files = [file('root.md'), file('nested/a.md'), file('nested/deeper/b.md'), file('lonely.md'), file('cover.png'), file('nested/data.json')];
  const links = { 'root.md': { 'nested/a.md': 1, 'lonely.md': 1 }, 'nested/deeper/b.md': { 'nested/a.md': 1 } };
  const flat = new BrainModel(app(files, links)); flat.buildFromVault();
  let legacyCalls = 0;
  const root = folderTree(files); root.children.push({ path: 'empty', name: 'empty', children: [] });
  const treeApp = { vault: { getRoot() { return root; }, getMarkdownFiles() { legacyCalls++; throw new Error('must not be called'); } }, metadataCache: { resolvedLinks: links } };
  const tree = new BrainModel(treeApp); tree.buildFromVault();
  assert.equal(legacyCalls, 0);
  assert.deepEqual(new Set(tree.nodes.keys()), new Set(Array.from(flat.nodes.keys())));
  assert.deepEqual(edgePairs(tree), edgePairs(flat));
  assert.deepEqual(tree.graphEnumeration, { entries: 9, markdown: 4, folders: 3, otherFiles: 2 });
});

test('async build of a generated 5,000-note vault matches the sync build and yields', async () => {
  const vault = generateVault({ notes: 5000, seed: 11, attachments: 300 });
  const root = folderTree(vault.files);
  const treeApp = () => ({ vault: { getRoot: () => root, getMarkdownFiles() { throw new Error('unused'); } }, metadataCache: { resolvedLinks: vault.links } });
  const sync = new BrainModel(treeApp()); sync.buildFromVault();
  const asyncModel = new BrainModel(treeApp());
  let ticks = 0; const timer = setInterval(() => ticks++, 0);
  await asyncModel.buildFromVaultAsync({ sliceMs: 1 });
  clearInterval(timer);
  // Compare as joined strings: a failing deepEqual on 5,000 entries would
  // spend a lot of memory rendering its diff.
  assert.equal([...asyncModel.edgeMap.keys()].join('\n'), [...sync.edgeMap.keys()].join('\n'));
  assert.equal(JSON.stringify(asyncModel.orderedNodes.map(n => [n.id, n.path, n.degree, n.pos])), JSON.stringify(sync.orderedNodes.map(n => [n.id, n.path, n.degree, n.pos])));
  assert.ok(ticks >= 2, 'returned to the event loop between slices');
  assert.equal(asyncModel.graphEnumeration.markdown, 5000);
  assert.equal(asyncModel.graphEnumeration.otherFiles, 300);
  assert.ok(sync.orphanCount > 1000 && sync.edgeMap.size > 3000, 'fixture has both orphans and links');
  const closed = new BrainModel(app(vault.files, vault.links)); closed.destroy();
  assert.equal(await closed.buildFromVaultAsync(), false);
  assert.equal(closed.nodes.size, 0, 'a closed model stops building');
});

test('generated graph solves with the real layout inside the unit sphere', () => {
  const vault = generateVault({ notes: 1200, seed: 3 });
  const model = new BrainModel(app(vault.files, vault.links)); model.buildFromVault();
  const index = new Map(model.orderedNodes.map((n, i) => [n.path, i]));
  const edges = new Uint32Array(model.edgeMap.size * 2); let k = 0;
  for (const e of model.edgeMap.values()) { edges[k++] = index.get(e.a); edges[k++] = index.get(e.b); }
  const solver = Layout.createSolver({ revision: 1, ids: model.orderedNodes.map(n => n.id), edges });
  solver.step(300);
  assert.ok(solver.settled, 'settles within 300 iterations');
  const stats = solver.stats();
  assert.equal(stats.empty, 0);
  assert.ok(stats.maxRadius <= 0.94 + 2e-5);
  model.orderedNodes.forEach((n, i) => { if (n.role === 'orphan') assert.ok(Math.abs(Math.hypot(solver.positions[3 * i], solver.positions[3 * i + 1], solver.positions[3 * i + 2]) - 1) < 2e-5); });
});

test('region keys come from the classifier; changes bump regionVersion only', () => {
  const files = [file('alpha/a.md'), file('beta/b.md'), file('c.md')];
  const model = new BrainModel(app(files, { 'alpha/a.md': { 'beta/b.md': 1 } }), { classify: byFolder });
  model.buildFromVault();
  assert.deepEqual(model.orderedNodes.map(n => n.regionKey), ['alpha', 'beta', '']);
  const structure = model.structureVersion, regions = model.regionVersion;
  model.setClassifier(() => 'same');
  assert.equal(model.structureVersion, structure, 'reclassifying does not restart the layout');
  assert.equal(model.regionVersion, regions + 1);
  model.setClassifier(() => { throw new Error('broken classifier'); });
  assert.ok(model.orderedNodes.every(n => n.regionKey === ''), 'a throwing classifier falls back to the root region');
});

run('graph');
