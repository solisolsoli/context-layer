'use strict';

// Activation trace handling (src/activation.js): parsing and validation of
// .context/activation.json, freshness, HUD text and the polling watcher.
// Traces here are fictional and follow the version 1 contract.
const assert = require('node:assert/strict');
const { test, run } = require('./helpers/harness');
const A = require('../src/activation');

const NOW = Date.parse('2026-09-24T12:05:00Z');

function trace(overrides = {}) {
  return Object.assign({
    version: 1,
    generated_at: '2026-09-24T12:00:00Z',
    run_id: '5f3a9c0e7b21d4aa',
    query: null,
    method: 'synaptic',
    budget_tokens: 1200,
    nodes: [
      { path: 'projects/apollo.md', activation: 1.0, hop: 0, role: 'seed', selected: true },
      { path: 'people/ada.md', activation: 0.42, hop: 1, role: 'hop', selected: true },
      { path: 'topics/orbits.md', activation: 0.2, hop: 2, role: 'hop', selected: false },
    ],
    edges: [
      { from: 'projects/apollo.md', to: 'people/ada.md', kind: 'wikilink', weight: 0.42, anchor: { path: 'projects/apollo.md', line: 12 } },
      { from: 'people/ada.md', to: 'topics/orbits.md', kind: 'backlink', weight: 0.2, anchor: { path: 'topics/orbits.md', line: 3 } },
    ],
    packet: { passages: 5, est_tokens: 910, status: 'PARTIAL' },
  }, overrides);
}
const text = obj => JSON.stringify(obj);

test('a valid trace is parsed and normalized', () => {
  const t = A.parseActivation(text(trace()));
  assert.ok(t);
  assert.equal(t.generatedAt, Date.parse('2026-09-24T12:00:00Z'));
  assert.equal(t.method, 'synaptic');
  assert.equal(t.budgetTokens, 1200);
  assert.deepEqual(t.nodes.map(n => [n.path, n.role, n.hop]), [['projects/apollo.md', 'seed', 0], ['people/ada.md', 'hop', 1], ['topics/orbits.md', 'hop', 2]]);
  assert.deepEqual(t.edges.map(e => e.kind), ['wikilink', 'backlink']);
  assert.deepEqual(t.packet, { passages: 5, estTokens: 910, status: 'PARTIAL' });
  assert.equal('query' in t, false, 'query text is never carried into display state');
  assert.equal(t.runId, '5f3a9c0e7b21d4aa');
  assert.equal(t.key, '2026-09-24T12:00:00Z|5f3a9c0e7b21d4aa');
  assert.equal(t.generatedAtText, '2026-09-24T12:00:00Z');
});

test('query text recorded by opt-in is not exposed', () => {
  const t = A.parseActivation(text(trace({ query: 'a private question', query_sha256: 'cd'.repeat(32) })));
  assert.ok(t);
  assert.equal(JSON.stringify(t).includes('private question'), false);
  assert.equal(JSON.stringify(t).includes('cdcd'), false, 'query hash is not carried either');
  const noRun = A.parseActivation(text(trace({ run_id: undefined })));
  assert.equal(noRun.runId, null, 'run_id is optional');
  assert.equal(A.parseActivation(text(trace({ run_id: '../x' }))).runId, null, 'odd run_id values are ignored');
});

test('malformed, partial and wrong-version files are rejected without throwing', () => {
  const full = text(trace());
  for (const bad of ['', '{', full.slice(0, full.length / 2), 'null', '[]', '"x"', '42',
    text(trace({ version: 2 })), text(trace({ version: '1' })), text(trace({ generated_at: 'yesterday' })),
    text(trace({ generated_at: '2026-09-24 12:00:00' })), text(trace({ nodes: 'nope' })), text({ version: 1 })]) {
    assert.equal(A.parseActivation(bad), null, 'rejected: ' + bad.slice(0, 40));
  }
  assert.equal(A.parseActivation(undefined), null);
  assert.equal(A.parseActivation(Buffer.from(full)), null, 'non-string input');
});

test('oversized files are rejected before parsing', () => {
  const big = text(trace({ padding: 'x'.repeat(A.MAX_BYTES) }));
  assert.equal(A.parseActivation(big), null);
  assert.ok(A.parseActivation(text(trace()), { maxBytes: 100 }) === null);
});

test('invalid entries are dropped individually; limits and order are enforced', () => {
  const nodes = [
    { path: '/abs/path.md', activation: 1, hop: 0 },
    { path: '../escape.md', activation: 1, hop: 0 },
    { path: 'a//b.md', activation: 1, hop: 0 },
    { path: 'ok.md', activation: 'high', hop: 0 },
    { path: 'ok.md', activation: 0.5, hop: -1 },
    { path: 'ok.md', activation: 0.5, hop: 1.5 },
    { path: 'clamped.md', activation: 7, hop: 0 },
    { path: 'clamped.md', activation: 0.1, hop: 1 },
    { path: 'derived.md', activation: -3, hop: 2, role: 'weird', selected: 'yes' },
    'not an object', null,
  ];
  for (let i = 0; i < 300; i++) nodes.push({ path: `bulk/n-${i}.md`, activation: i / 1000, hop: 1 });
  const edges = [{ from: 'a.md', to: 'a.md' }, { from: 'a.md', to: 'b.md', kind: 'teleport', weight: 'x' }, { from: 'a.md', to: 'b.md', kind: 'teleport' }];
  for (let i = 0; i < 500; i++) edges.push({ from: `bulk/n-${i}.md`, to: `bulk/n-${i + 1}.md`, kind: 'mdlink', weight: i / 500 });
  const t = A.parseActivation(text(trace({ nodes, edges })));
  assert.equal(t.nodes.length, A.MAX_NODES);
  assert.equal(t.nodes[0].path, 'clamped.md');
  assert.equal(t.nodes[0].activation, 1, 'activation clamped to [0, 1]');
  assert.equal(t.nodes.filter(n => n.path === 'clamped.md').length, 1, 'duplicate paths keep the first entry');
  const derived = A.parseActivation(text(trace({ nodes: [nodes[8]] }))).nodes[0];
  assert.deepEqual(derived, { path: 'derived.md', activation: 0, hop: 2, role: 'hop', selected: false });
  assert.ok(!t.nodes.some(n => n.path.includes('..') || n.path.startsWith('/')));
  for (let i = 1; i < t.nodes.length; i++) assert.ok(t.nodes[i - 1].activation >= t.nodes[i].activation, 'highest activation first');
  assert.equal(t.edges.length, A.MAX_EDGES);
  assert.ok(t.edges.every(e => e.from !== e.to && A.EDGE_KINDS.includes(e.kind)));
  const unknownKind = A.parseActivation(text(trace({ edges: edges.slice(1, 3).concat([{ from: 'a.md', to: 'b.md', kind: 'embed' }, { from: 'a.md', to: 'b.md', kind: 'embed' }]) }))).edges;
  assert.deepEqual(unknownKind, [{ from: 'a.md', to: 'b.md', kind: 'embed', weight: 0, depth: null }], 'only explicit link kinds are kept; duplicates collapse');
});

test('missing optional sections degrade gracefully', () => {
  const t = A.parseActivation(text({ version: 1, generated_at: '2026-09-24T12:00:00.123+02:00', nodes: [] }));
  assert.ok(t);
  assert.deepEqual(t.edges, []);
  assert.deepEqual(t.packet, { passages: null, estTokens: null, status: null });
  assert.equal(t.method, 'unknown');
  assert.equal(A.formatHud(t, t.generatedAt + 90000), 'at 2026-09-24T12:00:00.123+02:00 \u00b7 unknown \u00b7 1 min ago');
});

test('freshness window and clock tolerance', () => {
  const t = A.parseActivation(text(trace()));
  const window = 10 * 60000;
  assert.equal(A.isFresh(t, NOW, window), true, '5 minutes old');
  assert.equal(A.isFresh(t, t.generatedAt + window + 1, window), false, 'stale just after the window');
  assert.equal(A.isFresh(t, t.generatedAt - 30000, window), true, 'slightly in the future is tolerated');
  assert.equal(A.isFresh(t, t.generatedAt - 5 * 60000, window), false, 'far in the future is not');
  assert.equal(A.isFresh(null, NOW, window), false);
});

test('the HUD never shows activation as a percentage or confidence', () => {
  const t = A.parseActivation(text(trace()));
  const line = A.formatHud(t, t.generatedAt + 1000);
  assert.equal(/%|percent|confiden|probab/i.test(line), false);
});

test('HUD line shows method, passages, tokens, age and non-OK status only', () => {
  const t = A.parseActivation(text(trace()));
  assert.equal(A.formatHud(t, t.generatedAt + 42000), 'at 2026-09-24T12:00:00Z \u00b7 synaptic \u00b7 5 passages \u00b7 ~910 tokens \u00b7 42 s ago');
  assert.equal(A.formatHud(t, t.generatedAt + 1000), 'at 2026-09-24T12:00:00Z \u00b7 synaptic \u00b7 5 passages \u00b7 ~910 tokens \u00b7 just now');
  const one = A.parseActivation(text(trace({ packet: { passages: 1, est_tokens: 80, status: 'BUDGET_EXCEEDED' } })));
  assert.equal(A.formatHud(one, one.generatedAt + 2 * 3600000), 'at 2026-09-24T12:00:00Z \u00b7 synaptic \u00b7 1 passage \u00b7 ~80 tokens \u00b7 2 h ago \u00b7 BUDGET_EXCEEDED', 'an unknown status is shown as written');
  const odd = A.parseActivation(text(trace({ method: '<b>x</b>', packet: { status: 'not <ok>' } })));
  assert.equal(odd.method, 'unknown', 'unexpected method strings are not displayed');
  assert.equal(odd.packet.status, null);
});

test('vault path sanitizer', () => {
  assert.equal(A.sanitizeVaultPath('.context/activation.json'), '.context/activation.json');
  assert.equal(A.sanitizeVaultPath('./.context/x.json'), '.context/x.json');
  for (const bad of ['', '/etc/passwd', '../x.json', 'a/../b', 'C:/x.json', 'a\\b.json', 'a//b', 42, null]) assert.equal(A.sanitizeVaultPath(bad), null);
});

function fakeAdapter(files = {}) {
  const disk = new Map(Object.entries(files).map(([p, t]) => [p, { text: t, mtime: 1 }]));
  return {
    stats: 0, reads: 0,
    async stat(p) { this.stats++; const f = disk.get(p); return f ? { type: 'file', mtime: f.mtime, size: Buffer.byteLength(f.text) } : null; },
    async read(p) { this.reads++; return disk.get(p).text; },
    set(p, t) { const old = disk.get(p); disk.set(p, { text: t, mtime: (old ? old.mtime : 0) + 1 }); },
    remove(p) { disk.delete(p); },
  };
}

test('watcher: stat first, read only on change, clear when the file disappears', async () => {
  const adapter = fakeAdapter();
  const changes = [];
  const w = A.createWatcher({ adapter, getPath: () => A.DEFAULT_PATH, onChange: t => changes.push(t) });
  assert.equal(await w.poll(), null, 'missing file: nothing');
  assert.equal(adapter.reads, 0);
  adapter.set(A.DEFAULT_PATH, text(trace()));
  assert.ok(await w.poll());
  assert.equal(changes.length, 1);
  await w.poll(); await w.poll();
  assert.equal(adapter.reads, 1, 'unchanged mtime/size is not re-read');
  assert.equal(adapter.stats, 4);
  adapter.set(A.DEFAULT_PATH, text(trace({ generated_at: '2026-09-24T12:01:00Z' })));
  assert.equal((await w.poll()).generatedAt, Date.parse('2026-09-24T12:01:00Z'));
  assert.equal(changes.length, 2);
  adapter.remove(A.DEFAULT_PATH);
  assert.equal(await w.poll(), null);
  assert.equal(changes.at(-1), null, 'removal clears the trace');
});

test('watcher: malformed or oversized updates are ignored and keep the last good trace', async () => {
  const adapter = fakeAdapter({ [A.DEFAULT_PATH]: text(trace()) });
  const changes = [];
  const w = A.createWatcher({ adapter, getPath: () => A.DEFAULT_PATH, onChange: t => changes.push(t), maxBytes: 4096 });
  const good = await w.poll();
  adapter.set(A.DEFAULT_PATH, '{"version": 1, "generated_at": "2026-09-24T12:0');
  assert.equal(await w.poll(), good, 'partial write ignored');
  adapter.set(A.DEFAULT_PATH, text(trace({ padding: 'x'.repeat(5000) })));
  const readsBefore = adapter.reads;
  assert.equal(await w.poll(), good, 'oversized file ignored');
  assert.equal(adapter.reads, readsBefore, 'oversized file is never read');
  assert.equal(changes.length, 1);
});

test('watcher: unsafe path settings and adapter failures are tolerated', async () => {
  const adapter = fakeAdapter({ 'x.json': text(trace()) });
  const w = A.createWatcher({ adapter, getPath: () => '../outside.json', onChange: () => { throw new Error('must not fire'); } });
  assert.equal(await w.poll(), null);
  assert.equal(adapter.stats, 0, 'an unsafe path is never touched');
  const failing = A.createWatcher({ adapter: { async stat() { throw new Error('io'); } }, getPath: () => A.DEFAULT_PATH });
  assert.equal(await failing.poll(), null);
  const noStat = { reads: 0, async exists() { return true; }, async read() { this.reads++; return text(trace()); } };
  const legacy = A.createWatcher({ adapter: noStat, getPath: () => A.DEFAULT_PATH });
  assert.ok(await legacy.poll());
  assert.ok(await legacy.poll());
  assert.equal(noStat.reads, 2, 'without stat the file is read, but unchanged text is not re-parsed into a new trace');
});

const DOT = ' ' + String.fromCharCode(0xB7) + ' ';

test('status vocabulary: PARTIAL and legacy OK add nothing, NOT_FOUND says so', () => {
  const at = (status) => A.formatHud(A.parseActivation(text(trace({ packet: { passages: 5, est_tokens: 910, status } }))), Date.parse('2026-09-24T12:00:01Z'));
  const base = 'at 2026-09-24T12:00:00Z' + DOT + 'synaptic' + DOT + '5 passages' + DOT + '~910 tokens' + DOT + 'just now';
  assert.equal(at('PARTIAL'), base, 'PARTIAL: evidence found; the passage count already says so');
  assert.equal(at('OK'), base, 'OK is read as a legacy spelling of found evidence');
  assert.equal(at('NOT_FOUND'), base + DOT + 'no evidence found');
  assert.equal(A.statusText('ERROR'), 'retrieval error');
  assert.equal(A.statusText(null), '');
});

test('packet mode: superset and compact are labelled; anything else is ignored', () => {
  const now = Date.parse('2026-09-24T12:00:01Z');
  const superset = A.parseActivation(text(trace({ mode: 'superset' })));
  assert.equal(superset.mode, 'superset');
  assert.equal(A.formatHud(superset, now), 'at 2026-09-24T12:00:00Z' + DOT + 'synaptic superset' + DOT + '5 passages' + DOT + '~910 tokens' + DOT + 'just now');
  const compact = A.parseActivation(text(trace({ mode: 'compact' })));
  assert.match(A.formatHud(compact, now), /synaptic compact/);
  assert.match(A.MODE_NOTES.compact, /can leave out evidence the fts packet would carry/);
  assert.equal(A.parseActivation(text(trace({ mode: '<i>fast</i>' }))).mode, null);
  assert.equal(A.parseActivation(text(trace())).mode, null, 'a writer without the field shows no mode');
});

test('paths are compared in Unicode form NFC; NFD and NFC spellings collapse', () => {
  const nfd = 'places/Cafe' + String.fromCharCode(0x301) + '.md', nfc = 'places/Caf' + String.fromCharCode(0xE9) + '.md';
  const t = A.parseActivation(text(trace({ nodes: [
    { path: nfd, activation: 0.9, hop: 0, role: 'seed', selected: true },
    { path: nfc, activation: 0.5, hop: 1, role: 'hop', selected: false },
  ], edges: [{ from: nfd, to: 'people/ada.md', kind: 'wikilink', weight: 0.5 }] })));
  assert.deepEqual(t.nodes.map(n => n.path), [nfc], 'both spellings are one note, kept once');
  assert.equal(t.edges[0].from, nfc);
  assert.equal(A.sanitizeVaultPath(nfd), nfc);
});

test('the trace file setting only accepts .context/activation*.json', () => {
  for (const ok of ['.context/activation.json', '.context/activation-2.json', 'sub/.context/activation.json', './.context/activation.json']) {
    assert.ok(A.sanitizeTracePath(ok), ok);
  }
  for (const bad of ['.context/routes.json', '.context/not-activation.json', 'notes/activation.json', 'activation.json',
    '.context/x/activation.json', '../.context/activation.json', '.context/activation.json/x', '']) {
    assert.equal(A.sanitizeTracePath(bad), null, bad);
  }
});

test('an optional per-edge depth is read; invalid depths are ignored', () => {
  const t = A.parseActivation(text(trace({ edges: [
    { from: 'a.md', to: 'b.md', kind: 'wikilink', weight: 0.5, depth: 2 },
    { from: 'b.md', to: 'c.md', kind: 'wikilink', weight: 0.4, depth: 0 },
    { from: 'c.md', to: 'd.md', kind: 'wikilink', weight: 0.3, depth: 1.5 },
  ] })));
  assert.deepEqual(t.edges.map(e => e.depth), [2, null, null]);
});

test('legacy advisor fields are ignored as untrusted trace data', () => {
  const t = A.parseActivation(text(trace({
    jev: { mode: 'on', rationale: 'SECRET' },
    nodes: [{ path: 'projects/apollo.md', activation: 1, hop: 0, role: 'seed', jev: 'rescued', jev_reason: 'SECRET' }],
  })));
  assert.ok(t);
  assert.equal('advisor' in t, false);
  assert.equal('jev' in t.nodes[0], false);
  assert.equal(JSON.stringify(t).includes('SECRET'), false);
});

run('activation');
