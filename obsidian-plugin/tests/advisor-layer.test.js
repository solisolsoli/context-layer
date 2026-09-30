'use strict';

// The advisor layer: a read-only, default-hidden panel that reports what the
// optional advisor did in the last retrieval, from the activation trace only.
// The model tests use traces written by the real writer
// (tests/fixtures/make_jev_writer_traces.py); the view tests drive the panel
// through the fakes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, run } = require('./helpers/harness');
const fakes = require('./helpers/fakes');
fakes.hookObsidianRequire();
const env = fakes.installGlobals();
const A = require('../src/activation');
const Plugin = require('../src/main');
const { sanitizeSettings, DEFAULT_SETTINGS } = require('../src/settings');
const { generateVault } = require('./helpers/fixtures');

const readFixture = name => fs.readFileSync(path.join(__dirname, 'fixtures', name), 'utf8');
const onText = readFixture('writer-trace-jev-on.json');
const shadowText = readFixture('writer-trace-jev-shadow.json');
const plainText = readFixture('writer-trace-depth2.json');
const TRACE = '.context/activation.json';
const DOT = String.fromCharCode(0xB7);

// Loads a trace, keeping its fresh-timestamp-independent model.
const model = (text, absent) => A.advisorLayerModel(text ? A.parseActivation(text) : null, absent);

test('writer trace, mode on: rescued note, counts, mode and the advisory wording', () => {
  const m = model(onText);
  assert.equal(m.state, 'data');
  assert.deepEqual(m.facts.slice(0, 5), [
    'Mode: on, applied to the packet', 'Provider kind: fake', 'Rescued: 1', 'Flagged off topic: 2', 'Kept: 0',
  ]);
  assert.ok(m.facts.includes('Notes listed in the trace, by verdict: rescued 1, on topic 0, off topic 3, local only 0, not judged 0'));
  assert.deepEqual(m.rescued.map(n => n.path), ['people/Mira Holt.md']);
  assert.deepEqual(m.candidates, []);
  assert.match(m.note, /Advisory only/);
  assert.match(m.note, /not a check of correctness/);
});

test('writer trace, mode shadow: nothing is called rescued; on-topic notes outside the packet are listed as such', () => {
  const m = model(shadowText);
  assert.equal(m.state, 'data');
  assert.equal(m.facts[0], 'Mode: shadow, not applied (the packet was not changed)');
  assert.ok(!m.facts.some(f => /^Rescued:/.test(f)), 'a shadow run rescued nothing');
  assert.deepEqual(m.rescued, []);
  assert.deepEqual(m.candidates.map(n => n.path), ['people/Mira Holt.md'], 'judged on topic, not in the packet');
  assert.ok(m.facts.some(f => /^Would flag off topic: \d+$/.test(f)));
  assert.ok(m.facts.includes('Would rescue: 1'), 'the writer records how many notes mode on would have added');
});

test('a trace without advisor data gives an honest empty state, not a blank panel', () => {
  for (const text of [plainText, readFixture('writer-trace-not-found.json')]) {
    const m = model(text);
    assert.equal(m.state, 'no-advisor');
    assert.equal(m.message, A.ADVISOR_LAYER_NO_DATA);
    assert.match(m.message, /carries no advisor data/);
    assert.deepEqual([m.facts, m.rescued, m.candidates], [[], [], []]);
  }
});

test('no trace to show: the reason is named (none, disabled, cleared, stale); mode off says nothing was judged', () => {
  for (const reason of ['none', 'disabled', 'cleared', 'stale']) {
    const m = model(null, reason);
    assert.equal(m.state, 'no-trace');
    assert.equal(m.message, A.ADVISOR_LAYER_EMPTY[reason]);
  }
  assert.equal(model(null, 'unknown reason').message, A.ADVISOR_LAYER_EMPTY.none);
  const raw = JSON.parse(onText); raw.jev.mode = 'off'; raw.jev.applied = false;
  const off = A.advisorLayerModel(A.parseActivation(JSON.stringify(raw)));
  assert.equal(off.state, 'off');
  assert.equal(off.message, A.ADVISOR_LAYER_OFF);
});

test('only enums, booleans and counters reach the panel: free text in the block is ignored', () => {
  const raw = JSON.parse(onText);
  raw.jev.reason = 'SECRET-REASON'; raw.jev.model_reported = 'SECRET-MODEL'; raw.jev.provider_kind = 'SECRET-KIND';
  raw.jev.notice = { text: 'SECRET-NOTICE' };
  raw.nodes[0].jev = 'SECRET-VERDICT'; raw.nodes[1].advice = 'SECRET-ADVICE';
  const m = A.advisorLayerModel(A.parseActivation(JSON.stringify(raw)));
  assert.doesNotMatch(JSON.stringify(m), /SECRET/);
  assert.ok(!m.facts.some(f => /^Provider kind/.test(f)), 'an unknown provider kind is not shown');
  assert.equal(m.counts.rescued, 1);
});

test('mode on that was not applied lists no rescued note', () => {
  const raw = JSON.parse(onText); raw.jev.applied = false;
  const m = A.advisorLayerModel(A.parseActivation(JSON.stringify(raw)));
  assert.equal(m.facts[0], 'Mode: on, but not applied to this packet');
  assert.deepEqual(m.rescued, []);
});

test('the setting exists, is off by default and is sanitized to a boolean', () => {
  assert.equal(DEFAULT_SETTINGS.showAdvisorLayer, false);
  assert.equal(sanitizeSettings({}).showAdvisorLayer, false);
  assert.equal(sanitizeSettings({ showAdvisorLayer: 'yes' }).showAdvisorLayer, false);
  assert.equal(sanitizeSettings({ showAdvisorLayer: true }).showAdvisorLayer, true);
});

async function openView() {
  const vault = generateVault({ notes: 200, seed: 21, attachments: 5 });
  const app = fakes.createApp({ files: vault.files, links: vault.links });
  fakes.FakeCanvas.contextFactory = () => fakes.createFakeGL();
  const plugin = new Plugin(app, { id: 'context-layer-brain', version: '0.4.0' });
  await plugin.onload();
  const leaf = await plugin.activateView();
  await leaf.view.ready;
  return { app, plugin, view: leaf.view };
}
async function closeView({ view, plugin }) { await view.onClose(); view.unloadComponent(); plugin.onunload(); }

// A writer trace re-pointed at notes of the generated vault (verdicts, counts
// and modes stay exactly as the writer wrote them) and made fresh.
function remap(text, view, extra = {}) {
  const raw = JSON.parse(text);
  const linked = view.model.orderedNodes.filter(n => n.degree > 0).map(n => n.path);
  const map = new Map(raw.nodes.map((n, i) => [n.path, linked[i]]));
  raw.generated_at = new Date(Date.now() - 2000).toISOString();
  for (const n of raw.nodes) n.path = map.get(n.path);
  for (const e of raw.edges) { e.from = map.get(e.from); e.to = map.get(e.to); e.anchor.path = map.get(e.anchor.path); }
  return { text: JSON.stringify(Object.assign(raw, extra)), map };
}
const flushView = view => { view._lastHud = -Infinity; env.frames.flush(2); };
const panelText = view => {
  const out = [];
  const walk = el => { if (el.text) out.push(el.text); el.children.forEach(walk); };
  walk(view.advisorPanelEl);
  return out.join('\n');
};

test('the layer is hidden by default, even when the trace carries advisor data', async () => {
  const ctx = await openView(); const { app, view } = ctx;
  assert.equal(view.advisorPanelEl.hasClass('nb-hidden'), true);
  assert.equal(view.advisorToggleEl.tagName, 'BUTTON');
  assert.equal(view.advisorToggleEl.text, 'Show advisor layer');
  assert.equal(view.advisorToggleEl.attrs['aria-pressed'], 'false');
  app.vault.adapter.setFile(TRACE, remap(onText, view).text);
  await view.pollActivation(true); flushView(view);
  assert.equal(view.advisorPanelEl.hasClass('nb-hidden'), true, 'data alone does not open the panel');
  assert.equal(view.advisorPanelEl.children.length, 0, 'nothing is even built while hidden');
  await closeView(ctx);
});

test('the toggle opens the panel: mode, counts, the rescued note as a button, the advisory wording', async () => {
  const ctx = await openView(); const { app, view, plugin } = ctx;
  const { text, map } = remap(onText, view);
  app.vault.adapter.setFile(TRACE, text);
  await view.pollActivation(true);
  view.advisorToggleEl.dispatch('click');
  await new Promise(resolve => setTimeout(resolve, 0)); flushView(view);
  assert.equal(plugin.settings.showAdvisorLayer, true);
  assert.equal(app._pluginData.settings.showAdvisorLayer, true, 'the choice is saved in the plugin data file');
  assert.equal(view.advisorPanelEl.hasClass('nb-hidden'), false);
  assert.equal(view.advisorToggleEl.text, 'Hide advisor layer');
  assert.equal(view.advisorToggleEl.attrs['aria-pressed'], 'true');
  const shown = panelText(view);
  assert.match(shown, /Advisor layer \(read only\)/);
  assert.match(shown, /Mode: on, applied to the packet/);
  assert.match(shown, /Rescued: 1/);
  assert.match(shown, /Notes the advisor rescued/);
  assert.match(shown, /not a check of correctness/);
  const buttons = view.advisorPanelEl.findAll('nb-advisor-note');
  assert.equal(buttons.length, 1);
  const rescuedPath = map.get('people/Mira Holt.md');
  assert.equal(buttons[0].text, rescuedPath + ' ' + DOT + ' hop 1');
  buttons[0].dispatch('click');
  assert.deepEqual(app.workspace.leaves.at(-1).opened, [rescuedPath], 'a rescued note opens like any other');
  // Off again.
  view.advisorToggleEl.dispatch('click');
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(view.advisorPanelEl.hasClass('nb-hidden'), true);
  assert.equal(plugin.settings.showAdvisorLayer, false);
  await closeView(ctx);
});

test('shadow trace in the panel: labelled as not applied, nothing called rescued', async () => {
  const ctx = await openView(); const { app, view, plugin } = ctx;
  app.vault.adapter.setFile(TRACE, remap(shadowText, view).text);
  await view.pollActivation(true);
  plugin.settings.showAdvisorLayer = true; await plugin.saveSettings(); flushView(view);
  const shown = panelText(view);
  assert.match(shown, /Mode: shadow, not applied/);
  assert.match(shown, /Judged on topic, not in the packet/);
  assert.doesNotMatch(shown, /Rescued: /);
  assert.doesNotMatch(shown, /Notes the advisor rescued/);
  assert.equal(view.advisorPanelEl.findAll('nb-advisor-note').length, 1);
  await closeView(ctx);
});

test('empty states in the panel: no trace, no advisor data, stale', async () => {
  const ctx = await openView(); const { app, view, plugin } = ctx;
  plugin.settings.showAdvisorLayer = true; await plugin.saveSettings();
  await new Promise(resolve => setTimeout(resolve, 0)); flushView(view); // let the poll that saving started finish
  assert.match(panelText(view), /No retrieval trace found yet/);
  assert.equal(view.advisorPanelEl.findAll('nb-advisor-note').length, 0);
  const plainRaw = JSON.parse(plainText); plainRaw.generated_at = new Date(Date.now() - 2000).toISOString();
  app.vault.adapter.setFile(TRACE, JSON.stringify(plainRaw));
  await view.pollActivation(true); flushView(view);
  const plain = panelText(view);
  assert.match(plain, /carries no advisor data/);
  assert.match(plain, /Nothing was recorded/);
  assert.doesNotMatch(plain, /Rescued|Mode:/);
  const old = JSON.parse(remap(onText, view).text); old.generated_at = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
  app.vault.adapter.setFile(TRACE, JSON.stringify(old));
  await view.pollActivation(true); flushView(view);
  assert.match(panelText(view), /older than the freshness window/);
  await closeView(ctx);
});

test('notes that are not in this vault are listed without a button', async () => {
  const ctx = await openView(); const { app, view, plugin } = ctx;
  const raw = JSON.parse(onText);
  raw.generated_at = new Date(Date.now() - 2000).toISOString();
  app.vault.adapter.setFile(TRACE, JSON.stringify(raw));
  await view.pollActivation(true);
  plugin.settings.showAdvisorLayer = true; await plugin.saveSettings(); flushView(view);
  assert.equal(view.advisorPanelEl.findAll('nb-advisor-note').length, 0);
  assert.match(panelText(view), /people\/Mira Holt\.md \u00b7 hop 1 \u00b7 not in this vault/);
  await closeView(ctx);
});

test('the existing overlay and HUD are unchanged by the layer', async () => {
  const ctx = await openView(); const { app, view, plugin } = ctx;
  app.vault.adapter.setFile(TRACE, remap(onText, view).text);
  await view.pollActivation(true); flushView(view);
  const before = { hud: view.activationEl.text, advisor: view.advisorEl.text, marks: view.markCount, nodes: view.overlay.nodes.size };
  plugin.settings.showAdvisorLayer = true; await plugin.saveSettings(); flushView(view);
  assert.deepEqual({ hud: view.activationEl.text, advisor: view.advisorEl.text, marks: view.markCount, nodes: view.overlay.nodes.size }, before);
  assert.equal(view.errorLog.length, 0, view.errorLog.join('; '));
  await closeView(ctx);
});

test('the layer adds no command and no network access; the bundle carries it', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'src', 'main.js'), 'utf8');
  assert.equal((main.match(/addCommand\(/g) || []).length, 3, 'the three existing commands only');
  const dist = fs.readFileSync(path.join(__dirname, '..', 'dist', 'main.js'), 'utf8');
  assert.ok(dist.includes('Advisor layer (read only)'), 'dist/main.js is rebuilt with the layer');
  assert.ok(dist.includes('not a check of correctness'));
});

run('advisor-layer');
