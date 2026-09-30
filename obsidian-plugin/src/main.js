'use strict';

// Plugin entry: registers the view, commands and settings, and keeps the
// optional layout cache. The plugin reads the vault's link graph through
// Obsidian's public API and, for the overlay, one trace file. It never writes
// to notes or to the trace file and makes no network requests. Its only write
// is Obsidian's own plugin data file (settings and, if enabled, cached
// positions of linked notes).

const { Plugin, Notice } = require('obsidian');
const { BrainView, VIEW_TYPE } = require('./view');
const { sanitizeSettings, BrainSettingTab } = require('./settings');
const Regions = require('./regions');

// Bump when layout constants or the cache format change so cached positions
// are not reused. Version 2: link-aware layout; entries carry a neighbour hash.
const LAYOUT_VERSION = 2;
const PERSIST_DELAY_MS = 3000;

class ContextLayerBrainPlugin extends Plugin {
  async onload() {
    let data = null;
    try { data = await this.loadData(); } catch (_) { data = null; }
    data = data && typeof data === 'object' ? data : {};
    this.settings = sanitizeSettings(data.settings);
    this.positionCache = this.settings.rememberLayout && data.layoutVersion === LAYOUT_VERSION && data.positions && typeof data.positions === 'object' ? data.positions : {};
    this._persistTimer = null;
    this.dismissedKey = null;

    this.registerView(VIEW_TYPE, leaf => new BrainView(leaf, this));
    this.addRibbonIcon('brain', 'Open Context Layer Brain View', () => { this.activateView(); });
    this.addCommand({ id: 'open-view', name: 'Open view', callback: () => { this.activateView(); } });
    this.addCommand({ id: 'focus-last-retrieval', name: 'Focus last retrieval', callback: () => { this.withView(view => view.focusLastRetrieval(), { open: true, reveal: true }); } });
    this.addCommand({ id: 'clear-activation', name: 'Clear activation', callback: () => { this.withView(view => view.clearActivation(), { open: false, reveal: false }); } });
    this.addSettingTab(new BrainSettingTab(this.app, this));

    if (this.settings.openOnStartup) {
      this.app.workspace.onLayoutReady(() => { this.activateView().catch(err => console.error('[context-layer-brain] open on startup', err)); });
    }
  }

  onunload() {
    if (this._persistTimer) { clearTimeout(this._persistTimer); this._persistTimer = null; }
  }

  regionClassifier() {
    const source = this.settings.regionSource, cache = this.app.metadataCache;
    return file => Regions.regionKeyForFile(file, source, cache);
  }

  async activateView() {
    const { workspace } = this.app;
    let leaf = workspace.getLeavesOfType(VIEW_TYPE)[0];
    if (!leaf) {
      leaf = workspace.getLeaf('tab');
      await leaf.setViewState({ type: VIEW_TYPE, active: true });
    }
    await workspace.revealLeaf(leaf);
    return leaf;
  }

  // Runs fn on the view. Since Obsidian 1.7.2 a restored leaf holds a
  // deferred placeholder until it is shown, so the leaf is revealed first
  // (reveal) or loaded in place without switching tabs (loadIfDeferred).
  // With `open` the view is opened when there is none.
  async withView(fn, { open = true, reveal = open } = {}) {
    try {
      const { workspace } = this.app;
      let leaf = workspace.getLeavesOfType(VIEW_TYPE)[0];
      if (!leaf && open) leaf = await this.activateView();
      if (leaf && reveal) await workspace.revealLeaf(leaf);
      else if (leaf && !(leaf.view instanceof BrainView) && typeof leaf.loadIfDeferred === 'function') await leaf.loadIfDeferred();
      const view = leaf && leaf.view;
      if (!(view instanceof BrainView)) { if (!open) new Notice('Context Layer Brain View is not open.'); return; }
      await view.ready;
      await fn(view);
    } catch (err) { console.error('[context-layer-brain] command failed', err); }
  }

  async saveSettings() {
    this.settings = sanitizeSettings(this.settings);
    if (!this.settings.rememberLayout) this.positionCache = {};
    await this.savePluginData();
    for (const leaf of this.app.workspace.getLeavesOfType(VIEW_TYPE)) {
      if (leaf.view instanceof BrainView) leaf.view.applySettings();
    }
  }

  async savePluginData() {
    const data = { settings: this.settings };
    if (this.settings.rememberLayout) { data.layoutVersion = LAYOUT_VERSION; data.positions = this.positionCache; }
    await this.saveData(data);
  }

  schedulePersist(model) {
    if (!this.settings.rememberLayout) return;
    if (this._persistTimer) clearTimeout(this._persistTimer);
    this._persistTimer = setTimeout(() => { this._persistTimer = null; this.flushPersist(model).catch(() => { /* best effort */ }); }, PERSIST_DELAY_MS);
  }

  // Saves the position of every linked note. A settled position carries a
  // hash of the note's neighbours, and the next session keeps the note
  // exactly there while the hash still matches. A position that never
  // settled is saved without a hash: it only warm-starts the next solve.
  async flushPersist(model) {
    if (!model || !this.settings.rememberLayout) return;
    const round = v => Math.round(v * 1e4) / 1e4;
    const positions = {};
    for (const node of model.nodes.values()) {
      if (node.role !== 'linked') continue;
      const p = node.target || node.pos;
      if (!p || !p.every(Number.isFinite)) continue;
      if (node.placed) positions[node.path] = { x: round(p[0]), y: round(p[1]), z: round(p[2]), h: model.neighbourHash(node.path) };
      else if (this.positionCache[node.path]) positions[node.path] = this.positionCache[node.path];
      else positions[node.path] = { x: round(p[0]), y: round(p[1]), z: round(p[2]) };
    }
    this.positionCache = positions;
    await this.savePluginData();
  }
}

module.exports = ContextLayerBrainPlugin;
module.exports.default = ContextLayerBrainPlugin;
