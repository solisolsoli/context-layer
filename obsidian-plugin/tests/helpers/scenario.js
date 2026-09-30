'use strict';

// End-to-end scenario against the fakes: load the plugin, open the view on a
// generated vault, render frames, show and clear a retrieval overlay, react
// to vault events, then close and unload. Used for both the source modules
// and the built dist/main.js (loaded in a vm context).
const assert = require('node:assert/strict');
const fakes = require('./fakes');
const { generateVault } = require('./fixtures');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const TRACE_PATH = '.context/activation.json';

function traceFor(model, generatedAtMs, extra = {}) {
  const linked = model.orderedNodes.filter(n => n.degree > 0).sort((a, b) => b.degree - a.degree || (a.path < b.path ? -1 : 1));
  const seed = linked[0];
  const hops = Array.from(model.adjacency.get(seed.path)).sort().slice(0, 3);
  return JSON.stringify(Object.assign({
    version: 1,
    generated_at: new Date(generatedAtMs).toISOString(),
    run_id: 'a1b2c3d4e5f60718',
    query: null,
    method: 'synaptic',
    budget_tokens: 1200,
    nodes: [{ path: seed.path, activation: 1, hop: 0, role: 'seed', selected: true }]
      .concat(hops.map((p, i) => ({ path: p, activation: 0.6 - i * 0.1, hop: 1, role: 'hop', selected: i === 0 })))
      .concat([{ path: 'not/in/this/vault.md', activation: 0.5, hop: 1, role: 'hop', selected: false }]),
    edges: hops.map(p => ({ from: seed.path, to: p, kind: 'wikilink', weight: 0.5, anchor: { path: seed.path, line: 1 } })),
    packet: { passages: 4, est_tokens: 777, status: 'PARTIAL' },
  }, extra));
}

async function runScenario({ PluginClass, env, label }) {
  const step = name => { if (process.env.NB_TRACE) console.log('  step: ' + name); };
  const { FakeCanvas, createFakeGL, createApp, obsidian, FakeWorker } = fakes;
  const vault = generateVault({ notes: 300, seed: 21, attachments: 5 });
  const app = createApp({ files: vault.files, links: vault.links });
  let gl = null;
  FakeCanvas.contextFactory = () => (gl = createFakeGL());
  const workersBefore = FakeWorker.instances.length;

  // Load: defaults, commands, no view opened on startup.
  step('Load: defaults, commands, no view opened on startup.');
  const plugin = new PluginClass(app, { id: 'context-layer-brain', version: '0.4.0' });
  await plugin.onload();
  assert.deepEqual(plugin.commands.map(c => c.id), ['open-view', 'focus-last-retrieval', 'clear-activation'], label + ': commands');
  assert.equal(plugin.settings.openOnStartup, false);
  assert.equal(app.workspace.leaves.length, 0, label + ': nothing opens on startup by default');

  // Settings tab renders every control and saves changes.
  step('Settings tab renders every control and saves changes.');
  const tab = plugin.settingTabs[0];
  tab.display();
  const names = tab.containerEl.settings.map(s => s.name);
  for (const expected of ['Open on startup', 'Colour notes by', 'Show unlinked notes', 'Link thickness', 'Bloom', 'Respect reduced motion', 'Show activation overlay', 'Developer diagnostics']) {
    assert.ok(names.includes(expected), label + ': setting ' + expected);
  }

  // Open the view.
  step('Open the view.');
  const leaf = await plugin.activateView();
  const view = leaf.view;
  await view.ready;
  assert.equal(view.errorLog.length, 0, label + ': no errors while opening: ' + view.errorLog.join('; '));
  assert.equal(view.model.nodes.size, 300);
  assert.ok(view.isVisible());
  assert.equal(FakeWorker.instances.length, workersBefore + 1, label + ': layout worker started');
  env.frames.flush(4);
  assert.ok(gl.stats.drawArrays > 0, label + ': points drawn');
  assert.ok(gl.stats.instancedDraws > 0, label + ': link ribbons drawn');
  assert.match(view.countersEl.text, /^300 notes \u00b7 \d+ links \u00b7 \d+ unlinked$/);
  assert.equal(view.activationEl.hasClass('nb-hidden'), true, label + ': no overlay without a trace');

  // A fresh trace appears on the next poll.
  step('A fresh trace appears on the next poll.');
  app.vault.adapter.setFile(TRACE_PATH, traceFor(view.model, Date.now() - 5000));
  await view.pollActivation(true);
  assert.ok(view.overlay, label + ': overlay shown');
  assert.equal(view.overlay.nodes.size, 4, label + ': unknown path dropped');
  assert.equal(view.overlay.edges.length, 3);
  view._lastHud = -Infinity;
  env.frames.flush(3);
  assert.equal(view.ovCount, 3, label + ': overlay links uploaded');
  assert.ok(view.dynCount > 0, label + ': pulses drawn');
  assert.match(view.activationEl.text, /^Last retrieval at \d{4}-\d\d-\d\dT[\d:.]+Z \u00b7 synaptic \u00b7 4 passages \u00b7 ~777 tokens \u00b7 (just now|\d+ s ago) \u00b7 4 of 5 notes in this vault$/,
    label + ': PARTIAL adds no suffix; the note outside this vault is counted');
  assert.equal(view.overlayKeyEl.hasClass('nb-hidden'), false, label + ': seed/hop/reached key shown');
  assert.equal(view.activationEl.hasClass('nb-hidden'), false);

  // Commands.
  step('Commands.');
  await plugin.commands.find(c => c.id === 'focus-last-retrieval').callback();
  await sleep(0); await sleep(0);
  assert.ok(view.centerTarget && view.zoomTarget !== null && view.focusTarget, label + ': focus frames the activated notes');
  env.frames.flush(3);
  await plugin.commands.find(c => c.id === 'clear-activation').callback();
  await sleep(0);
  assert.equal(view.overlay, null, label + ': cleared');
  await view.pollActivation(true);
  assert.equal(view.overlay, null, label + ': a cleared trace stays hidden');
  view.updateActivationHud(Date.now());
  assert.match(view.activationEl.text, /cleared$/);
  app.vault.adapter.setFile(TRACE_PATH, traceFor(view.model, Date.now() - 1000));
  await view.pollActivation(true);
  assert.ok(view.overlay, label + ': a newer trace shows again');

  // Stale, malformed and missing traces hide or are ignored without errors.
  step('Stale, malformed and missing traces hide or are ignored without errors.');
  app.vault.adapter.setFile(TRACE_PATH, traceFor(view.model, Date.now() - 11 * 60000));
  await view.pollActivation(true);
  assert.equal(view.overlay, null, label + ': stale trace hidden');
  view.updateActivationHud(Date.now());
  assert.match(view.activationEl.text, /^Last retrieval at \S+ \u00b7 stale \(older than 10 min\), not shown$/, label + ': stale state is visible');
  assert.equal(view.overlayKeyEl.hasClass('nb-hidden'), true);
  app.vault.adapter.setFile(TRACE_PATH, traceFor(view.model, Date.now(), { version: 2 }));
  await view.pollActivation(true);
  assert.equal(view.overlay, null, label + ': version 2 is not shown');
  app.vault.adapter.setFile(TRACE_PATH, '{"version":1,"generated_at":');
  await view.pollActivation(true);
  app.vault.adapter.removeFile(TRACE_PATH);
  await view.pollActivation(true);
  env.frames.flush(2);
  assert.equal(view.overlay, null);
  assert.equal(view.errorLog.length, 0, label + ': ' + view.errorLog.join('; '));

  // Settings changes apply to the open view.
  step('Settings changes apply to the open view.');
  plugin.settings.paletteMode = 'folder';
  plugin.settings.showOrphans = false;
  await plugin.saveSettings();
  env.frames.flush(2);
  assert.equal(view.legendEl.hasClass('nb-hidden'), false, label + ': legend visible in region mode');
  assert.ok(view.legendEl.children.length >= vault.folders.length);
  assert.equal(view.nodeCount, view.model.orderedNodes.filter(n => n.role !== 'orphan').length, label + ': orphans hidden');
  view.legendEl.children[0].dispatch('click');
  assert.ok(view.focusRegionKey !== null && view.focusTarget, label + ': legend click focuses a region');
  plugin.settings.bloom = true; plugin.settings.developerDiagnostics = true;
  await plugin.saveSettings();
  view._lastHud = -Infinity;
  env.frames.flush(2);
  assert.ok(view.bloomOk, label + ': bloom path initialized');
  assert.match(view.devEl.text, /^fps /);

  // Pointer interaction does not throw; a click on a note opens it.
  step('Pointer interaction does not throw; a click on a note opens it.');
  const canvas = view.canvas;
  canvas.dispatch('pointermove', { clientX: 400, clientY: 300 });
  env.frames.flush(2);
  canvas.dispatch('pointerdown', { clientX: 400, clientY: 300 });
  canvas.dispatch('pointermove', { clientX: 460, clientY: 320 });
  canvas.dispatch('pointerup', { clientX: 460, clientY: 320 });
  canvas.dispatch('wheel', { deltaY: 120 });
  env.frames.flush(2);
  view.hoverNode = view.model.orderedNodes[0];
  view.drag.moved = 0;
  view.handleHover = () => {};
  canvas.dispatch('click', { clientX: 1, clientY: 1 });
  assert.deepEqual(app.workspace.leaves.at(-1).opened, [view.model.orderedNodes[0].path], label + ': click opens the hovered note in a new tab');

  // Layout worker delivers real positions over time.
  step('Layout worker delivers real positions over time.');
  await sleep(150);
  env.frames.flush(3);
  assert.ok(['solving', 'settled'].includes(view.model.workerState), label + ': worker state ' + view.model.workerState);

  // Vault events: modify fires a signal; delete removes the note after refresh.
  step('Vault events: modify fires a signal; delete removes the note after refresh.');
  const victim = view.model.orderedNodes.find(n => n.degree > 0);
  app.vault.trigger('modify', victim.file);
  assert.ok(view.signalEvents.length > 0);
  app.vault.files = app.vault.files.filter(f => f.path !== victim.path);
  app.vault.trigger('delete', victim.file);
  await sleep(150);
  env.frames.flush(2);
  assert.equal(view.model.nodes.size, 299, label + ': deleted note removed');

  // Hidden window pauses rendering and the worker.
  step('Hidden window pauses rendering and the worker.');
  env.document.hidden = true;
  view.handleVisibility();
  assert.equal(view.rafId, null);
  const worker = FakeWorker.instances.at(-1);
  assert.equal(worker.posted.at(-1), 'pause');
  env.document.hidden = false;
  view.handleVisibility();
  assert.ok(view.rafId);
  assert.equal(worker.posted.at(-1), 'resume');

  // Close and unload.
  step('Close and unload.');
  await view.onClose();
  view.unloadComponent();
  assert.equal(worker.terminated, true);
  assert.equal(gl.stats.lost, true, label + ': GL context released');
  const linked = view.model.orderedNodes.filter(n => n.role === 'linked').length;
  assert.ok(app._pluginData.positions && Object.keys(app._pluginData.positions).length === linked, label + ': positions of linked notes cached in plugin data');
  assert.equal(app._pluginData.layoutVersion, 2);
  plugin.onunload();
  assert.equal(app.vault.adapter.writes, 0, label + ': never writes through the vault adapter');
  assert.equal(view.errorLog.length, 0, label + ': ' + view.errorLog.join('; '));

  // Without WebGL the view shows a message instead of failing.
  step('Without WebGL the view shows a message instead of failing.');
  FakeCanvas.contextFactory = () => null;
  const leaf2 = app.workspace.getLeaf('tab');
  await leaf2.setViewState({ type: 'context-layer-brain-view' });
  await leaf2.view.ready;
  assert.ok(leaf2.view.contentEl.find('nb-message'), label + ': WebGL message');
  await leaf2.view.onClose();
  leaf2.view.unloadComponent();
  assert.ok(obsidian.notices.every(n => typeof n === 'string'));
  return { plugin, view };
}

module.exports = { runScenario, traceFor };
