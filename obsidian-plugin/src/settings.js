'use strict';

const obsidian = require('obsidian');
const Activation = require('./activation');

const { PluginSettingTab, Setting } = obsidian;
// Obsidian's normalizePath() cleans slashes, spaces and Unicode form of a
// user-typed path; the trace path is then restricted further.
const normalizePath = typeof obsidian.normalizePath === 'function' ? obsidian.normalizePath : p => p;

const DEFAULT_SETTINGS = Object.freeze({
  openOnStartup: false,
  paletteMode: 'degree',          // 'degree' | 'folder'
  regionSource: 'folder',         // 'folder' | 'tag'
  showOrphans: true,
  edgeWidth: 1.0,
  bloom: false,
  bloomStrength: 0.6,
  autoRotate: true,
  respectReducedMotion: true,
  activationOverlay: true,
  activationPath: Activation.DEFAULT_PATH,
  activationWindowMinutes: 10,
  showAdvisorShadow: false,
  showAdvisorLayer: false,
  rememberLayout: true,
  developerDiagnostics: false,
});

const ENUMS = { paletteMode: ['degree', 'folder'], regionSource: ['folder', 'tag'] };
const RANGES = { edgeWidth: [0.25, 3], bloomStrength: [0.05, 1.5], activationWindowMinutes: [1, 120] };

// A user-typed trace path, or null: normalizePath(), then only
// `.context/activation*.json` inside the vault is accepted.
function cleanTracePath(value) {
  if (typeof value !== 'string') return null;
  let normalized;
  try { normalized = normalizePath(value); } catch (_) { return null; }
  return Activation.sanitizeTracePath(normalized);
}

// Returns a complete, type-checked settings object. Unknown keys are dropped;
// invalid values fall back to their defaults.
function sanitizeSettings(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const out = {};
  for (const [key, def] of Object.entries(DEFAULT_SETTINGS)) {
    const v = src[key];
    if (ENUMS[key]) out[key] = ENUMS[key].includes(v) ? v : def;
    else if (typeof def === 'boolean') out[key] = typeof v === 'boolean' ? v : def;
    else if (typeof def === 'number') {
      const [lo, hi] = RANGES[key];
      out[key] = typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : def;
    } else if (key === 'activationPath') out[key] = cleanTracePath(v) || def;
    else out[key] = def;
  }
  return out;
}

class BrainSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    const s = this.plugin.settings;
    const save = async (key, value) => { this.plugin.settings[key] = value; await this.plugin.saveSettings(); };
    containerEl.empty();

    new Setting(containerEl).setName('Open on startup')
      .setDesc('Open the view automatically when the vault loads.')
      .addToggle(t => t.setValue(s.openOnStartup).onChange(v => save('openOnStartup', v)));

    new Setting(containerEl).setName('Colour notes by')
      .setDesc('Link count uses a fixed scale, so a note keeps its colour when unrelated notes change. Region uses one colour per top-level folder or first tag.')
      .addDropdown(d => d.addOption('degree', 'Link count').addOption('folder', 'Region').setValue(s.paletteMode)
        .onChange(async v => { await save('paletteMode', v); this.display(); }));

    new Setting(containerEl).setName('Regions come from')
      .setDesc('Used by the region colours and the legend.')
      .addDropdown(d => d.addOption('folder', 'Top-level folder').addOption('tag', 'First tag').setValue(s.regionSource)
        .onChange(v => save('regionSource', v)));

    new Setting(containerEl).setName('Show unlinked notes')
      .setDesc('Notes without links are drawn as a faint outer shell. Notes in the last retrieval are always drawn.')
      .addToggle(t => t.setValue(s.showOrphans).onChange(v => save('showOrphans', v)));

    new Setting(containerEl).setName('Link thickness')
      .addSlider(sl => sl.setLimits(0.25, 3, 0.25).setValue(s.edgeWidth).setDynamicTooltip().onChange(v => save('edgeWidth', v)));

    new Setting(containerEl).setName('Bloom')
      .setDesc('Soft glow around bright areas. Costs extra GPU time; off by default.')
      .addToggle(t => t.setValue(s.bloom).onChange(async v => { await save('bloom', v); this.display(); }));
    if (s.bloom) {
      new Setting(containerEl).setName('Bloom strength')
        .addSlider(sl => sl.setLimits(0.05, 1.5, 0.05).setValue(s.bloomStrength).setDynamicTooltip().onChange(v => save('bloomStrength', v)));
    }

    new Setting(containerEl).setName('Slow auto-rotation')
      .setDesc('Rotate slowly after a few seconds without interaction.')
      .addToggle(t => t.setValue(s.autoRotate).onChange(v => save('autoRotate', v)));

    new Setting(containerEl).setName('Respect reduced motion')
      .setDesc('When the system asks for reduced motion, stop rotation, breathing and pulses.')
      .addToggle(t => t.setValue(s.respectReducedMotion).onChange(v => save('respectReducedMotion', v)));

    new Setting(containerEl).setName('Retrieval activation').setHeading();

    new Setting(containerEl).setName('Show activation overlay')
      .setDesc('Highlight the notes and links used by the last context-layer synaptic retrieval.')
      .addToggle(t => t.setValue(s.activationOverlay).onChange(v => save('activationOverlay', v)));

    new Setting(containerEl).setName('Activation file')
      .setDesc('Vault-relative path of the trace file: an activation*.json file inside a .context folder. Read only; the plugin never writes it.')
      .addText(t => t.setPlaceholder(Activation.DEFAULT_PATH).setValue(s.activationPath)
        .onChange(v => { const clean = cleanTracePath(v); if (clean) save('activationPath', clean); }));

    new Setting(containerEl).setName('Freshness window (minutes)')
      .setDesc('Traces older than this are not shown.')
      .addSlider(sl => sl.setLimits(1, 120, 1).setValue(Math.min(120, s.activationWindowMinutes)).setDynamicTooltip()
        .onChange(v => save('activationWindowMinutes', v)));

    new Setting(containerEl).setName('Show advisor (shadow)')
      .setDesc('When the trace carries advisor verdicts that were not applied (shadow mode), draw what the advisor would have done, labelled "would". Advisor judgement, not evidence.')
      .addToggle(t => t.setValue(s.showAdvisorShadow).onChange(v => save('showAdvisorShadow', v)));

    new Setting(containerEl).setName('Show advisor layer')
      .setDesc('A read-only panel in the view: what the optional advisor did in the last retrieval (mode, counts, the notes it rescued). Hidden by default. It reads only what the trace records; if there is no advisor data it says so. Advisory only, not a check of correctness.')
      .addToggle(t => t.setValue(s.showAdvisorLayer).onChange(v => save('showAdvisorLayer', v)));

    new Setting(containerEl).setName('Advanced').setHeading();

    new Setting(containerEl).setName('Remember layout')
      .setDesc('Store settled positions of linked notes in this plugin\'s data file. The next time the view opens, notes whose links did not change keep their places and only new or re-linked notes are placed.')
      .addToggle(t => t.setValue(s.rememberLayout).onChange(v => save('rememberLayout', v)));

    new Setting(containerEl).setName('Developer diagnostics')
      .setDesc('Show frame rate, frame time and layout state in the corner of the view. Local only.')
      .addToggle(t => t.setValue(s.developerDiagnostics).onChange(v => save('developerDiagnostics', v)));
  }
}

module.exports = { DEFAULT_SETTINGS, sanitizeSettings, cleanTracePath, BrainSettingTab };
