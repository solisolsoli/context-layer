'use strict';

// View and plugin lifecycle from the source modules, with fake Obsidian,
// DOM, WebGL and Worker objects. This checks behaviour and wiring; it does
// not render pixels or prove anything about a real GPU.
const assert = require('node:assert/strict');
const { test, run } = require('./helpers/harness');
const fakes = require('./helpers/fakes');
fakes.hookObsidianRequire();
const env = fakes.installGlobals();
const Palette = require('../src/palette');
const { BrainView } = require('../src/view');
const Plugin = require('../src/main');
const { runScenario } = require('./helpers/scenario');
const { sanitizeSettings, DEFAULT_SETTINGS } = require('../src/settings');
const { nearestAngle } = require('../src/math');
const fs = require('node:fs');
const path = require('node:path');
const { generateVault, file } = require('./helpers/fixtures');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const TRACE = '.context/activation.json';
const fresh = (ms = 2000) => new Date(Date.now() - ms).toISOString();

async function openView({ notes = 300, seed = 21, vault } = {}) {
  const v = vault || generateVault({ notes, seed, attachments: 5 });
  const app = fakes.createApp({ files: v.files, links: v.links });
  fakes.FakeCanvas.contextFactory = () => fakes.createFakeGL();
  const plugin = new Plugin(app, { id: 'context-layer-brain', version: '0.4.0' });
  await plugin.onload();
  const leaf = await plugin.activateView();
  await leaf.view.ready;
  return { app, plugin, leaf, view: leaf.view, vault: v };
}
async function closeView({ view, plugin }) { await view.onClose(); view.unloadComponent(); plugin.onunload(); }
function traceText(nodes, extra = {}) {
  return JSON.stringify(Object.assign({ version: 1, generated_at: fresh(), run_id: 'c0ffee00c0ffee00c0ffee00c0ffee00', query: null, method: 'synaptic', budget_tokens: 600,
    nodes: nodes.map((p, i) => ({ path: p, activation: 1 - i * 0.1, hop: i ? 1 : 0, role: i ? 'hop' : 'seed', selected: true })),
    edges: nodes.slice(1).map(p => ({ from: nodes[0], to: p, kind: 'wikilink', weight: 0.5, anchor: { path: nodes[0], line: 1 } })),
    packet: { passages: nodes.length, est_tokens: 300, status: 'PARTIAL' } }, extra));
}
const alphas = view => Array.from(view._nodeArr.color.slice(0, view.nodeCount * 4)).filter((_, i) => i % 4 === 3);
class RecordingWorker {
  constructor() { this.posted = []; this.terminated = false; RecordingWorker.last = this; }
  postMessage(m) { this.posted.push(m.type); }
  terminate() { this.terminated = true; }
}

function bareView(extra = {}) {
  const view = Object.create(BrainView.prototype);
  return Object.assign(view, {
    plugin: { settings: Object.assign({}, DEFAULT_SETTINGS) },
    _closed: false, _contextLost: false, _renderReady: true, rafId: null, gl: {},
    canvas: { clientWidth: 800, clientHeight: 600, width: 800, height: 600 },
    drag: { dragging: false, velYaw: 0, velPitch: 0 }, center: [0, 0, 0], centerTarget: null, zoomTarget: null, focusTarget: null,
    fires: [], pulses: [], signalEvents: [], errorLog: [], _renderBound() {},
  }, extra);
}

test('full lifecycle from source modules', async () => {
  await runScenario({ PluginClass: Plugin, env, label: 'src' });
});

test('hidden window pauses the worker and cancels frames; showing resumes', () => {
  const pauses = [], metrics = [];
  const view = bareView({ rafId: 17, model: { setPaused(v) { pauses.push(v); } }, metrics: { frame(...a) { metrics.push(a); } } });
  env.document.hidden = true;
  view.handleVisibility();
  assert.deepEqual(pauses, [true]);
  assert.equal(view.rafId, null);
  assert.equal(metrics[0][2].visible, false, 'hidden time is excluded from frame statistics');
  env.document.hidden = false;
  view.handleVisibility();
  assert.deepEqual(pauses, [true, false]);
  assert.ok(view.rafId, 'a new frame is scheduled');
});

test('a background tab (zero-size canvas) counts as hidden; an unfocused visible split does not', () => {
  const view = bareView();
  assert.equal(view.isVisible(), true, 'visibility does not depend on which leaf is active');
  view.canvas.clientWidth = 0;
  assert.equal(view.isVisible(), false);
});

test('frames are not scheduled before the view finished opening', () => {
  const pauses = [];
  const view = bareView({ _renderReady: false, model: { setPaused(v) { pauses.push(v); } } });
  assert.equal(view.isVisible(), false);
  view.handleVisibility();
  assert.equal(view.rafId, null);
  assert.equal(pauses.at(-1), true);
  view._renderReady = true;
  view.handleVisibility();
  assert.ok(view.rafId);
  assert.equal(pauses.at(-1), false);
});

test('reduced motion freezes the field clock, camera inertia and signals', () => {
  const model = { workerState: 'settled', buffersDirty: false, structureVersion: 1, regionVersion: 0, nodes: new Map(), edgeMap: new Map(),
    edgeKey: (a, b) => a + ':' + b, advance() { return false; }, setPaused() {} };
  const view = bareView({ reducedMotion: true, _motionTime: 12, lastFrameTime: 0, lastInteraction: 0, yaw: 0.4, pitch: 0.2, zoom: 1,
    drag: { dragging: false, velYaw: 0.4, velPitch: -0.2 }, model, viewMat: new Float32Array(16), projMat: new Float32Array(16),
    _regionStamp: '1:0:folder', refreshRegions() {}, updateEdges() {}, rebuildDynamicBuffer() {}, drawScene() {}, updateHud() {} });
  view.render(16);
  assert.equal(view._motionTime, 12);
  assert.equal(view.yaw, 0.4); assert.equal(view.pitch, 0.2);
  assert.equal(view.drag.velYaw, 0); assert.equal(view.drag.velPitch, 0);
  const node = { path: 'a.md', id: 'a' };
  model.nodes.set('a.md', node); model.edgeMap.set('a.md:b.md', {});
  view.fires = [{ node, start: 16, duration: 800 }];
  view.pulses = [{ from: node, to: { path: 'b.md' }, start: 16, duration: 1200 }];
  view.updateAmbientAndSignals(17);
  assert.equal(view.fires.length, 0); assert.equal(view.pulses.length, 0);
});

test('the reduced-motion setting can be turned off', () => {
  const view = bareView({ _systemReducedMotion: true });
  view.updateReducedMotion();
  assert.equal(view.reducedMotion, true);
  view.plugin.settings.respectReducedMotion = false;
  view.updateReducedMotion();
  assert.equal(view.reducedMotion, false);
});

test('degree 0 and 1 share the small size; hubs grow but stay bounded; the shell is dimmer', () => {
  const inner = { id: 'i', path: 'inner.md', title: 'Inner', degree: 1, role: 'linked', region: { key: '' }, pos: [0, 0, 0] };
  const shell = { id: 's', path: 'shell.md', title: 'Shell', degree: 0, role: 'orphan', region: { key: '' }, pos: [0, 0, 1] };
  const hub = { id: 'h', path: 'hub.md', title: 'Hub', degree: 955, role: 'linked', region: { key: '' }, pos: [0, 0, -1] };
  const uploads = new Map(); let bound = null;
  const view = bareView({
    gl: { ARRAY_BUFFER: 1, DYNAMIC_DRAW: 2, bindBuffer(_t, b) { bound = b; }, bufferData(_t, d) { if (bound) uploads.set(bound, Array.from(d)); } },
    buffers: { nodePos: 'pos', nodeColor: 'color', nodeSize: 'size', nodeShell: 'shell' },
    model: { orderedNodes: [inner, shell, hub], structureVersion: 1, orphanCount: 1, nodes: new Map([[inner.path, inner], [shell.path, shell], [hub.path, hub]]) },
    hoverNode: null, focusRegionKey: null, overlay: null,
  });
  view.rebuildNodeBuffers();
  const size = uploads.get('size'), color = uploads.get('color');
  assert.equal(size[0], size[1]);
  assert.ok(Math.abs(size[0] - 0.009) < 1e-8);
  assert.ok(size[2] > size[1] && size[2] <= 0.018);
  assert.deepEqual(uploads.get('shell'), [0, 1, 0]);
  assert.ok(color[3] > color[7], 'inner notes are brighter than the shell');
  assert.ok(Math.abs(color[8] - Palette.styleForDegree(955).color[0]) < 1e-6);
});

test('camera: focus directions never spin through extra turns', () => {
  assert.ok(Math.abs(nearestAngle(20 * Math.PI + 0.1, -0.1) - (20 * Math.PI - 0.1)) < 1e-9);
  assert.ok(Math.abs(nearestAngle(0, 3 * Math.PI / 2) - (-Math.PI / 2)) < 1e-9);
  const view = bareView({ yaw: 13.0, pitch: 0 });
  view.setFocusDirection([1, 0, 0]);
  assert.ok(Math.abs(view.focusTarget.yaw - view.yaw) <= Math.PI + 1e-9);
  view.setFocusDirection([0, 0, 0]);
  assert.equal(view.focusTarget, null, 'a zero direction clears the focus');
});

test('settings are sanitized: types, ranges, enums and a safe trace path', () => {
  const s = sanitizeSettings({ openOnStartup: 'yes', paletteMode: 'rainbow', edgeWidth: 99, activationWindowMinutes: -5,
    activationPath: '../../etc/passwd', unknownKey: 1, bloom: true, regionSource: 'tag' });
  assert.equal(s.openOnStartup, false);
  assert.equal(s.paletteMode, 'degree');
  assert.equal(s.edgeWidth, 3);
  assert.equal(s.activationWindowMinutes, 1);
  assert.equal(sanitizeSettings({ activationWindowMinutes: 500 }).activationWindowMinutes, 120,
    'a stored window above the slider maximum is clamped, not shown as 120 while staying in force');
  assert.equal(s.activationPath, '.context/activation.json');
  assert.equal(s.bloom, true);
  assert.equal(s.regionSource, 'tag');
  assert.equal('unknownKey' in s, false);
  assert.deepEqual(sanitizeSettings(null), Object.assign({}, DEFAULT_SETTINGS));
  assert.equal(DEFAULT_SETTINGS.openOnStartup, false, 'new users are not surprised by a view on startup');
});

test('a fresh trace whose notes are not in this vault is reported, not shown, and dims nothing (D-01)', async () => {
  const ctx = await openView({ notes: 200 });
  const { app, view } = ctx;
  env.frames.flush(3);
  const before = alphas(view);
  const linked = view.model.orderedNodes.filter(n => n.degree > 0).slice(0, 3).map(n => n.path);
  app.vault.adapter.setFile(TRACE, traceText(linked.map(p => 'Sub/' + p)));
  await view.pollActivation(true);
  view._lastHud = -Infinity; env.frames.flush(2);
  assert.equal(view.overlay, null, 'no overlay for 0 matched notes');
  assert.deepEqual(alphas(view), before, 'no note is dimmed');
  assert.equal(view.activationStatus().state, 'unmatched');
  assert.match(view.activationEl.text, /0 of 3 notes are in this vault \(check that context-layer indexed this vault folder\)$/);
  assert.equal(view.overlayKeyEl.hasClass('nb-hidden'), true);
  fakes.notices.length = 0;
  assert.equal(await view.focusLastRetrieval(), false);
  assert.match(fakes.notices.at(-1), /^None of the 3 notes in the last retrieval is in this vault/);
  app.vault.adapter.setFile(TRACE, traceText([linked[0], linked[1], 'Sub/' + linked[2]]));
  await view.pollActivation(true);
  view._lastHud = -Infinity; env.frames.flush(1);
  assert.ok(view.overlay, 'a partial match is shown');
  assert.match(view.activationEl.text, / 2 of 3 notes in this vault$/);
  await closeView(ctx);
});

test('a NOT_FOUND trace written by context-layer shows "no evidence found" and dims nothing', async () => {
  const ctx = await openView({ notes: 150 });
  const { app, view } = ctx;
  env.frames.flush(2);
  const before = alphas(view);
  const raw = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'writer-trace-not-found.json'), 'utf8'));
  raw.generated_at = fresh().replace(/\.\d+Z$/, 'Z');
  app.vault.adapter.setFile(TRACE, JSON.stringify(raw));
  await view.pollActivation(true);
  view._lastHud = -Infinity; env.frames.flush(2);
  assert.equal(view.overlay, null);
  assert.deepEqual(alphas(view), before);
  assert.equal(view.activationStatus().state, 'empty');
  assert.match(view.activationEl.text, /0 passages \u00b7 ~0 tokens \u00b7 (just now|\d+ s ago) \u00b7 no evidence found$/);
  fakes.notices.length = 0; await view.focusLastRetrieval();
  assert.equal(fakes.notices.at(-1), 'The last retrieval activated no notes (no evidence found).');
  await closeView(ctx);
});

test('the real depth-2 writer trace on a vault that has its notes: all shown, one ribbon per pair', async () => {
  const raw = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'writer-trace-depth2.json'), 'utf8'));
  raw.generated_at = fresh().replace(/\.\d+Z$/, 'Z');
  const paths = raw.nodes.map(n => n.path), links = {};
  for (const e of raw.edges) (links[e.anchor.path] ||= {})[e.anchor.path === e.from ? e.to : e.from] = 1;
  const ctx = await openView({ vault: { files: paths.concat(['misc/elsewhere.md']).map(p => file(p)), links, folders: [] } });
  ctx.app.vault.adapter.setFile(TRACE, JSON.stringify(raw));
  await ctx.view.pollActivation(true);
  ctx.view._lastHud = -Infinity; env.frames.flush(2);
  const ov = ctx.view.overlay;
  assert.equal(ov.matched, paths.length);
  assert.equal(ov.edges.length, new Set(raw.edges.map(e => [e.from, e.to].sort().join('|'))).size);
  assert.equal(ctx.view.ovCount, ov.edges.length, 'ribbons uploaded once per pair');
  assert.doesNotMatch(ctx.view.activationEl.text, /PARTIAL/, 'the writer status is not printed raw');
  await closeView(ctx);
});

test('bulk vault events are coalesced: 1,000 deletes on 10,000 notes, one rebuild, one layout request (D-04)', async () => {
  const RealWorker = globalThis.Worker; globalThis.Worker = RecordingWorker;
  try {
    const vault = generateVault({ notes: 10000, seed: 21, attachments: 5 });
    const ctx = await openView({ vault });
    const { app, view } = ctx, worker = RecordingWorker.last;
    const solves = () => worker.posted.filter(t => t === 'solve').length;
    const solvesBefore = solves(), structure = view.model.structureVersion;
    const gone = app.vault.files.filter(f => f.path.startsWith(vault.folders[0] + '/')).slice(0, 1000);
    const goneSet = new Set(gone.map(f => f.path)), links = app.metadataCache.resolvedLinks;
    app.vault.files = app.vault.files.filter(f => !goneSet.has(f.path));
    for (const src of Object.keys(links)) { if (goneSet.has(src)) { delete links[src]; continue; } for (const t of Object.keys(links[src])) if (goneSet.has(t)) delete links[src][t]; }
    const t0 = performance.now();
    for (const f of gone) app.vault.trigger('delete', f);
    const handlerMs = performance.now() - t0;
    assert.equal(view.model.structureVersion, structure, 'no rebuild inside the event handlers');
    await sleep(260);
    console.log('  1,000 delete events on 10,000 notes: ' + JSON.stringify({ handlerMs: +handlerMs.toFixed(1), layoutRequests: solves() - solvesBefore, rebuilds: view.model.structureVersion - structure }));
    assert.ok(handlerMs < 250, 'event handlers stay cheap (' + handlerMs.toFixed(1) + ' ms for 1,000 events)');
    assert.equal(view.model.structureVersion - structure, 1, 'one coalesced rebuild');
    assert.ok(solves() - solvesBefore <= 1, 'at most one layout request');
    assert.equal(view.model.nodes.size, 9000);
    // A folder renamed file by file: nodes keep identity and the layout is not restarted.
    const moving = app.vault.files.filter(f => f.path.startsWith(vault.folders[1] + '/')).slice(0, 1000);
    const nodesBefore = moving.map(f => view.model.nodes.get(f.path)), versionBefore = view.model.structureVersion, requestsBefore = solves();
    const renamed = moving.map(f => file(f.path.replace(vault.folders[1] + '/', vault.folders[1] + '-moved/')));
    const byOld = new Map(moving.map((f, i) => [f.path, renamed[i].path]));
    app.vault.files = app.vault.files.map(f => byOld.has(f.path) ? renamed[moving.indexOf(f)] : f);
    const next = {};
    for (const src of Object.keys(links)) { const row = {}; for (const t of Object.keys(links[src])) row[byOld.get(t) || t] = links[src][t]; next[byOld.get(src) || src] = row; }
    app.metadataCache.resolvedLinks = next;
    const r0 = performance.now();
    moving.forEach((f, i) => app.vault.trigger('rename', renamed[i], f.path));
    const renameMs = performance.now() - r0;
    await sleep(260);
    console.log('  1,000 rename events on 9,000 notes: ' + JSON.stringify({ handlerMs: +renameMs.toFixed(1), layoutRequests: solves() - requestsBefore }));
    assert.ok(renameMs < 250);
    assert.ok(renamed.every((f, i) => view.model.nodes.get(f.path) === nodesBefore[i]), 'renamed notes keep their node');
    assert.equal(view.model.structureVersion, versionBefore, 'renames do not restart the layout');
    assert.equal(solves(), requestsBefore);
    await closeView(ctx);
  } finally { globalThis.Worker = RealWorker; }
});

test('while hidden, vault events only mark the view dirty; it rebuilds once when shown (D-04, D-11)', async () => {
  const RealWorker = globalThis.Worker; globalThis.Worker = RecordingWorker;
  try {
    const ctx = await openView({ notes: 2000 });
    const { app, view } = ctx, worker = RecordingWorker.last;
    env.document.hidden = true; view.handleVisibility();
    const structure = view.model.structureVersion, signals = view.signalEvents.length;
    const gone = app.vault.files.filter(f => f.extension === 'md').slice(0, 300), goneSet = new Set(gone.map(f => f.path));
    app.vault.files = app.vault.files.filter(f => !goneSet.has(f.path));
    for (const f of gone) app.vault.trigger('delete', f);
    for (const n of view.model.orderedNodes.slice(0, 500)) app.vault.trigger('modify', n.file);
    await sleep(260);
    assert.equal(view.model.structureVersion, structure, 'no rebuild while hidden');
    assert.equal(view._refreshTimer, null);
    assert.equal(view.signalEvents.length, signals, 'modify events are not recorded while hidden');
    const solves = worker.posted.filter(t => t === 'solve').length;
    env.document.hidden = false; view.handleVisibility();
    assert.equal(view.model.structureVersion, structure + 1, 'one rebuild on reveal');
    assert.equal(worker.posted.filter(t => t === 'solve').length, solves + 1);
    await closeView(ctx);
  } finally { globalThis.Worker = RealWorker; env.document.hidden = false; }
});

test('legend entries are buttons; redraws do not add listeners (D-10, D-12)', async () => {
  const ctx = await openView({ notes: 400 });
  const { plugin, view } = ctx;
  plugin.settings.paletteMode = 'folder'; await plugin.saveSettings(); env.frames.flush(1);
  const registered = view._domEvents.length;
  for (let i = 0; i < 100; i++) view.refreshRegions(true);
  assert.equal(view._domEvents.length, registered, 'no registration per redraw');
  const items = view.legendEl.children;
  assert.ok(items.length >= 2 && items.every(el => el.tagName === 'BUTTON' && el.attrs.type === 'button' && el.attrs['aria-pressed'] === 'false'));
  assert.ok(items[0].find('nb-region-0'), 'colours come from CSS classes, not inline styles');
  assert.match(items[0].attrs['aria-label'], /\d+ notes?$/);
  items[0].dispatch('click');
  assert.ok(view.focusRegionKey !== null);
  assert.equal(view.legendEl.children[0].attrs['aria-pressed'], 'true');
  await closeView(ctx);
});

test('deferred views: Focus reveals and loads the view; Clear loads it in place (D-08)', async () => {
  const vault = generateVault({ notes: 120, seed: 4, attachments: 0 });
  const app = fakes.createApp({ files: vault.files, links: vault.links });
  fakes.FakeCanvas.contextFactory = () => fakes.createFakeGL();
  const plugin = new Plugin(app, { id: 'context-layer-brain' });
  await plugin.onload();
  const leaf = app.workspace.getLeaf('tab'); leaf.defer('context-layer-brain-view');
  assert.equal(leaf.view instanceof BrainView, false, 'restored leaf holds a placeholder');
  let cleared = null;
  await plugin.withView(view => { cleared = view; view.clearActivation(); }, { open: false, reveal: false });
  assert.ok(cleared instanceof BrainView, 'clear reaches the real view');
  assert.equal(app.workspace.revealed || 0, 0, 'clearing does not switch tabs');
  leaf.defer('context-layer-brain-view');
  let focused = null;
  await plugin.withView(view => { focused = view; }, { open: true, reveal: true });
  assert.ok(focused instanceof BrainView);
  assert.equal(app.workspace.revealed, 1, 'focus reveals the leaf');
  for (const v of [cleared, focused]) { await v.onClose(); v.unloadComponent(); }
  plugin.onunload();
});

test('pop-out windows: frames, visibility and listeners follow the view to its window (D-09)', async () => {
  const ctx = await openView({ notes: 150 });
  const { view } = ctx, main = env.window, popout = fakes.createFakeWindow({ devicePixelRatio: 1 });
  const mainListeners = main.listenerCount(), mainDoc = env.document.listenerCount();
  view.contentEl.migrateTo(popout, popout.document);
  assert.equal(main.listenerCount(), mainListeners - 2, 'resize and focus moved off the main window');
  assert.equal(env.document.listenerCount(), mainDoc - 1, 'visibilitychange moved off the main document');
  assert.equal(popout.listenerCount(), 2); assert.equal(popout.document.listenerCount(), 1);
  assert.ok(popout.frames.size >= 1, 'frames are requested from the pop-out window');
  popout.frames.flush(2);
  assert.equal(view.dpr, 1, 'device pixel ratio of the pop-out window');
  popout.document.hidden = true; view.handleVisibility();
  assert.equal(view.isVisible(), false, 'visibility follows the document the view is in');
  popout.document.hidden = false;
  await closeView(ctx);
  assert.equal(popout.listenerCount(), 0); assert.equal(popout.document.listenerCount(), 0);
  assert.equal(popout.frames.size, 0);
});

test('keyboard: the canvas is focusable and arrow, plus, minus and Home keys move the camera (D-12)', async () => {
  const ctx = await openView({ notes: 120 });
  const { view } = ctx, canvas = view.canvas;
  assert.equal(canvas.attrs.tabindex, '0');
  assert.match(canvas.attrs['aria-label'], /Arrow keys/);
  const yaw = view.yaw, zoom = view.zoom;
  assert.equal(canvas.dispatch('keydown', { key: 'ArrowLeft' }).defaultPrevented, true);
  assert.ok(view.yaw < yaw);
  canvas.dispatch('keydown', { key: '+' }); assert.ok(view.zoom < zoom);
  canvas.dispatch('keydown', { key: 'Home' }); assert.equal(view.zoomTarget, 1);
  assert.equal(canvas.dispatch('keydown', { key: 'q' }).defaultPrevented, undefined, 'other keys pass through');
  await closeView(ctx);
});

test('accessible summary: the last retrieval as text, notes as buttons, a live status without the ticking age (D-12)', async () => {
  const ctx = await openView({ notes: 200 });
  const { app, view } = ctx;
  const linked = view.model.orderedNodes.filter(n => n.degree > 0).slice(0, 3).map(n => n.path);
  app.vault.adapter.setFile(TRACE, traceText(linked));
  await view.pollActivation(true);
  view._lastHud = -Infinity; env.frames.flush(1);
  assert.equal(view.summaryEl.hasClass('nb-hidden'), false);
  assert.equal(view.summaryEl.tagName, 'DETAILS');
  const buttons = view.summaryListEl.findAll('nb-summary-note');
  assert.equal(buttons.length, 3);
  assert.match(buttons[0].text, new RegExp('^hop 0 \u00b7 seed \u00b7 in packet \u00b7 ' + linked[0].replace(/[.]/g, '[.]') + '$'));
  buttons[1].dispatch('click');
  assert.deepEqual(app.workspace.leaves.at(-1).opened, [linked[1]], 'a summary entry opens its note');
  assert.equal(view.liveEl.attrs['aria-live'], 'polite');
  assert.match(view.liveEl.text, /^Last retrieval at .* 3 passages/);
  assert.doesNotMatch(view.liveEl.text, /just now|s ago/);
  const text = view.liveEl.text;
  view._lastHud = -Infinity; env.frames.flush(1);
  assert.equal(view.liveEl.text, text, 'no re-announcement without a change');
  await closeView(ctx);
});

test('unload leaves nothing behind: listeners, frames, timers and the worker (hygiene)', async () => {
  const winBefore = env.window.listenerCount(), docBefore = env.document.listenerCount();
  const ctx = await openView({ notes: 200 });
  const { view, plugin, app } = ctx;
  app.vault.trigger('create', file('new-note.md'));
  plugin.schedulePersist(view.model);
  assert.ok(env.window.listenerCount() > winBefore && env.document.listenerCount() > docBefore);
  const worker = fakes.FakeWorker.instances.at(-1);
  await closeView(ctx);
  assert.equal(env.window.listenerCount(), winBefore, 'window listeners removed');
  assert.equal(env.document.listenerCount(), docBefore, 'document listeners removed');
  assert.equal(env.frames.size, 0, 'no animation frame left');
  assert.equal(view._refreshTimer, null, 'no pending rebuild');
  assert.equal(plugin._persistTimer, null, 'no pending save');
  assert.equal(worker.terminated, true);
  assert.equal(view.canvas.listenerCount(), 0, 'canvas listeners removed');
});

test('no network request at runtime, with or without an advisor block', async () => {
  const calls = [];
  const trap = name => function () { calls.push(name); throw new Error('network is not allowed: ' + name); };
  const saved = {};
  for (const name of ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource']) { saved[name] = globalThis[name]; globalThis[name] = trap(name); }
  try {
    const ctx = await openView({ notes: 200 });
    const { app, view, plugin } = ctx;
    const linked = view.model.orderedNodes.filter(n => n.degree > 0).slice(0, 4).map(n => n.path);
    const withAdvisor = JSON.parse(traceText(linked));
    withAdvisor.jev = { mode: 'on', applied: true, provider_kind: 'host_cli', rescued: 1, flagged: 1, kept: 2 };
    withAdvisor.nodes[1].jev = 'rescued'; withAdvisor.nodes[2].jev = 'off_topic';
    app.vault.adapter.setFile(TRACE, JSON.stringify(withAdvisor));
    await view.pollActivation(true);
    plugin.settings.showAdvisorShadow = true; await plugin.saveSettings();
    view._lastHud = -Infinity; env.frames.flush(3);
    assert.equal(view.markCount, 2);
    assert.match(view.advisorEl.text, /^advisor on \u00b7 rescued 1 \u00b7 flagged 1 \u00b7 kept 2$/);
    assert.ok(view.overlayKeyEl.children.some(el => el.text === 'advisor judgement, not evidence'));
    await closeView(ctx);
  } finally { for (const name of Object.keys(saved)) globalThis[name] = saved[name]; }
  assert.deepEqual(calls, []);
});

run('view');
