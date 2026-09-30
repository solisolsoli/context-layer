'use strict';

// Test doubles for the parts of Obsidian, the DOM, WebGL and Web Workers that
// the plugin touches. They record calls instead of rendering; no Obsidian
// installation, browser or GPU is involved.

const path = require('node:path');
const vm = require('node:vm');
const Module = require('node:module');
const { folderTree } = require('./fixtures');

// ---------------------------------------------------------------- DOM ----

class FakeEl {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.classes = new Set(); this.style = {}; this.dataset = {}; this.attrs = {}; this.cssProps = {};
    this.text = ''; this.listeners = {}; this.clientWidth = 800; this.clientHeight = 600;
    this._migrated = [];
  }
  // Obsidian adds win/doc to every node: the window and document the node
  // lives in. Tests can move an element to another fake window.
  get win() { return this._win || (this.parent ? this.parent.win : FakeEl.defaultWindow); }
  get doc() { return this._doc || (this.parent ? this.parent.doc : FakeEl.defaultDocument); }
  onWindowMigrated(fn) { this._migrated.push(fn); return () => { this._migrated = this._migrated.filter(f => f !== fn); }; }
  migrateTo(win, doc) { this._win = win; this._doc = doc; for (const fn of this._migrated.slice()) fn(win); }
  createEl(tag, options = {}) {
    const el = tag === 'canvas' ? new FakeCanvas() : new FakeEl(tag);
    if (options.cls) for (const c of String(options.cls).split(/\s+/)) if (c) el.classes.add(c);
    if (options.text !== undefined) el.text = String(options.text);
    if (options.attr) for (const [k, v] of Object.entries(options.attr)) el.attrs[k] = String(v);
    el.parent = this; this.children.push(el);
    return el;
  }
  setCssProps(props) { Object.assign(this.cssProps, props); }
  setAttr(k, v) { this.attrs[k] = String(v); }
  getAttr(k) { return this.attrs[k]; }
  createDiv(options) { return this.createEl('div', options); }
  createSpan(options) { return this.createEl('span', options); }
  empty() { this.children = []; this.text = ''; }
  addClass(c) { this.classes.add(c); }
  removeClass(c) { this.classes.delete(c); }
  toggleClass(c, on) { if (on) this.classes.add(c); else this.classes.delete(c); }
  hasClass(c) { return this.classes.has(c); }
  setText(t) { this.text = String(t); }
  getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn); }
  dispatch(type, event = {}) {
    const e = Object.assign({ preventDefault() { e.defaultPrevented = true; }, button: 0, pointerId: 1 }, event);
    for (const fn of this.listeners[type] || []) fn(e);
    if (typeof this['on' + type] === 'function') this['on' + type](e);
    return e;
  }
  listenerCount() { return Object.values(this.listeners).reduce((n, list) => n + list.length, 0); }
  setPointerCapture() {}
  releasePointerCapture() {}
  // Depth-first search by class name, for assertions.
  find(cls) {
    if (this.classes.has(cls)) return this;
    for (const c of this.children) { const hit = c.find(cls); if (hit) return hit; }
    return null;
  }
  findAll(cls, out = []) {
    if (this.classes.has(cls)) out.push(this);
    for (const c of this.children) c.findAll(cls, out);
    return out;
  }
}
FakeEl.defaultWindow = null;
FakeEl.defaultDocument = null;

class FakeCanvas extends FakeEl {
  constructor() { super('canvas'); this.width = 300; this.height = 150; }
  getContext(kind) { return FakeCanvas.contextFactory ? FakeCanvas.contextFactory(kind, this) : null; }
}
FakeCanvas.contextFactory = null;

// -------------------------------------------------------------- WebGL ----

// A WebGL1 stand-in: every method exists and succeeds; draw calls are counted.
function createFakeGL(options = {}) {
  const stats = { drawArrays: 0, instancedDraws: 0, instances: 0, bufferData: 0, bufferSubData: 0, programs: 0, deletedPrograms: 0, lost: false, points: [] };
  let nextLocation = 0, nextId = 0;
  const constantCache = new Map();
  const instanced = options.instancing === false ? null : {
    vertexAttribDivisorANGLE() {},
    drawArraysInstancedANGLE(_mode, _first, _count, instances) { stats.instancedDraws++; stats.instances += instances; },
  };
  const overrides = {
    stats,
    canvas: null,
    getExtension(name) {
      if (name === 'ANGLE_instanced_arrays') return instanced;
      if (name === 'WEBGL_lose_context') return { loseContext() { stats.lost = true; } };
      if (name === 'OES_texture_half_float') return { HALF_FLOAT_OES: 0x8D61 };
      return null;
    },
    createShader() { return { id: ++nextId }; },
    createProgram() { stats.programs++; return { id: ++nextId }; },
    createBuffer() { return { id: ++nextId }; },
    createTexture() { return { id: ++nextId }; },
    createFramebuffer() { return { id: ++nextId }; },
    deleteProgram() { stats.deletedPrograms++; },
    getShaderParameter() { return true; },
    getProgramParameter() { return true; },
    getAttribLocation() { return nextLocation++ % 16; },
    getUniformLocation(_p, name) { return { name }; },
    checkFramebufferStatus() { return 0x8CD5; },
    isContextLost() { return false; },
    drawArrays(mode, first, count) { stats.drawArrays++; stats.points.push(count); },
    bufferData() { stats.bufferData++; },
    bufferSubData() { stats.bufferSubData++; },
    getShaderInfoLog() { return ''; },
    getProgramInfoLog() { return ''; },
  };
  return new Proxy(overrides, {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (typeof prop === 'string' && /^[A-Z0-9_]+$/.test(prop)) {
        if (prop === 'FRAMEBUFFER_COMPLETE') return 0x8CD5;
        if (!constantCache.has(prop)) constantCache.set(prop, 0x1000 + constantCache.size);
        return constantCache.get(prop);
      }
      return () => undefined;
    },
  });
}

// ------------------------------------------------------ Worker and Blob ----

// Blob URLs map to their source text so FakeWorker can run the real layout
// worker inside a vm context, delivering messages asynchronously.
const blobSources = new Map();
let blobCounter = 0;
class FakeBlob { constructor(parts) { this.text = parts.join(''); } }
const FakeURL = {
  createObjectURL(blob) { const url = 'blob:test/' + (++blobCounter); blobSources.set(url, blob.text); return url; },
  revokeObjectURL(url) { blobSources.delete(url); },
};

class FakeWorker {
  constructor(url) {
    const source = blobSources.get(url);
    if (typeof source !== 'string') throw new Error('unknown blob url');
    this.onmessage = null; this.onerror = null; this.terminated = false; this.posted = [];
    const self = { onmessage: null, postMessage: message => { setTimeout(() => { if (!this.terminated && this.onmessage) this.onmessage({ data: message }); }, 0); } };
    this._self = self;
    vm.runInNewContext(source, { self, setTimeout, clearTimeout, Date, Math, Array, Object, Number, String, Boolean, Map, Set, Uint8Array, Uint32Array, Int32Array, Float32Array, Float64Array, ArrayBuffer, Error });
    FakeWorker.instances.push(this);
  }
  postMessage(message) { this.posted.push(message.type); if (!this.terminated) this._self.onmessage({ data: message }); }
  terminate() { this.terminated = true; }
}
FakeWorker.instances = [];

// ------------------------------------------------ animation frames, etc ----

function createFrameQueue() {
  let nextId = 0; const pending = new Map();
  return {
    requestAnimationFrame(cb) { const id = ++nextId; pending.set(id, cb); return id; },
    cancelAnimationFrame(id) { pending.delete(id); },
    get size() { return pending.size; },
    // Runs queued callbacks `count` times at `stepMs` intervals.
    flush(count = 1, stepMs = 16.7) {
      for (let i = 0; i < count; i++) {
        const batch = Array.from(pending.values()); pending.clear();
        const now = performance.now() + stepMs;
        for (const cb of batch) cb(now);
      }
    },
  };
}

// Installs window/document/rAF/Worker/Blob/URL stand-ins on `target`
// (globalThis by default) and returns handles for tests.
// A window with its own animation-frame queue, listeners and document, like
// the main window or an Obsidian pop-out window.
function createFakeWindow(options = {}) {
  const frames = createFrameQueue();
  const listeners = {};
  const mediaListeners = [];
  const windowObj = {
    frames,
    devicePixelRatio: options.devicePixelRatio || 2,
    matchMedia: () => ({ matches: !!options.reducedMotion, addEventListener(t, fn) { mediaListeners.push(fn); }, removeEventListener(t, fn) { const i = mediaListeners.indexOf(fn); if (i >= 0) mediaListeners.splice(i, 1); } }),
    mediaListeners,
    requestAnimationFrame: frames.requestAnimationFrame, cancelAnimationFrame: frames.cancelAnimationFrame,
    setInterval: (fn, ms) => setInterval(fn, ms),
    clearInterval: id => clearInterval(id),
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter(f => f !== fn); },
    dispatch(type) { for (const fn of listeners[type] || []) fn({}); },
    listenerCount() { return Object.values(listeners).reduce((n, list) => n + list.length, 0); },
  };
  const documentObj = new FakeEl('document');
  documentObj.hidden = false;
  windowObj.document = documentObj;
  return windowObj;
}

function installGlobals(target = globalThis, options = {}) {
  const windowObj = createFakeWindow(options), frames = windowObj.frames, documentObj = windowObj.document;
  FakeEl.defaultWindow = windowObj; FakeEl.defaultDocument = documentObj;
  Object.assign(target, {
    window: windowObj, document: documentObj,
    requestAnimationFrame: frames.requestAnimationFrame, cancelAnimationFrame: frames.cancelAnimationFrame,
    Worker: FakeWorker, Blob: FakeBlob, URL: FakeURL,
  });
  return { frames, window: windowObj, document: documentObj };
}

// ----------------------------------------------------------- obsidian ----

const notices = [];

class Component {
  constructor() { this._intervals = []; this._domEvents = []; this._events = []; }
  registerEvent(ref) { this._events.push(ref); }
  registerDomEvent(el, type, fn, opts) { el.addEventListener(type, fn, opts); this._domEvents.push([el, type, fn]); }
  registerInterval(id) { this._intervals.push(id); return id; }
  register(fn) { (this._cleanups ||= []).push(fn); }
  // Obsidian runs this when a component unloads; tests call it explicitly.
  unloadComponent() {
    for (const id of this._intervals) clearInterval(id);
    for (const [el, type, fn] of this._domEvents) el.removeEventListener(type, fn);
    for (const fn of this._cleanups || []) fn();
    this._intervals = []; this._domEvents = [];
  }
}

class ItemView extends Component {
  constructor(leaf) {
    super();
    this.leaf = leaf; this.app = leaf.app;
    this.containerEl = new FakeEl();
    this.contentEl = this.containerEl.createDiv({ cls: 'view-content' });
  }
}

class Plugin extends Component {
  constructor(app, manifest) {
    super();
    this.app = app; this.manifest = manifest || { id: 'context-layer-brain' };
    this.commands = []; this.ribbons = []; this.settingTabs = []; this.saved = [];
  }
  async loadData() { return this.app._pluginData ? JSON.parse(JSON.stringify(this.app._pluginData)) : null; }
  async saveData(data) { this.app._pluginData = JSON.parse(JSON.stringify(data)); this.saved.push(this.app._pluginData); }
  registerView(type, factory) { this.app.workspace._factories[type] = factory; }
  addRibbonIcon(icon, title, cb) { const el = new FakeEl(); this.ribbons.push({ icon, title, cb }); return el; }
  addCommand(command) { this.commands.push(command); return command; }
  addSettingTab(tab) { this.settingTabs.push(tab); }
}

class PluginSettingTab {
  constructor(app, plugin) { this.app = app; this.plugin = plugin; this.containerEl = new FakeEl(); }
}

// Chainable control double; the last onChange handler is kept for tests.
function control(kind, sink) {
  const c = { kind, value: undefined, options: [], handler: null };
  const proxy = new Proxy(c, {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (prop === 'setValue') return v => { target.value = v; return proxy; };
      if (prop === 'addOption') return (v, label) => { target.options.push([v, label]); return proxy; };
      if (prop === 'onChange') return fn => { target.handler = fn; return proxy; };
      return () => proxy;
    },
  });
  sink.push(c);
  return proxy;
}

class Setting {
  constructor(containerEl) {
    this.containerEl = containerEl; this.controls = [];
    (containerEl.settings ||= []).push(this);
  }
  setName(name) { this.name = name; return this; }
  setDesc(desc) { this.desc = desc; return this; }
  setHeading() { this.heading = true; return this; }
  addToggle(cb) { cb(control('toggle', this.controls)); return this; }
  addDropdown(cb) { cb(control('dropdown', this.controls)); return this; }
  addSlider(cb) { cb(control('slider', this.controls)); return this; }
  addText(cb) { cb(control('text', this.controls)); return this; }
}

class Notice { constructor(message) { this.message = message; notices.push(message); } }

// Mirrors Obsidian's documented behaviour: one forward slash between
// segments, no leading or trailing slashes, non-breaking spaces become
// spaces, and the result is Unicode-normalized.
function normalizePath(path) {
  let p = String(path).replace(/[\\/]+/g, '/').replace(/\u00A0|\u202F/g, ' ');
  p = p.replace(/^\/+|\/+$/g, '');
  return (p === '' ? '/' : p).normalize();
}

// Mod-click opens a new tab; Mod+Alt a split; Mod+Alt+Shift a window.
const Keymap = {
  isModEvent(evt) {
    if (!evt || !(evt.ctrlKey || evt.metaKey)) return false;
    if (evt.altKey && evt.shiftKey) return 'window';
    if (evt.altKey) return 'split';
    return 'tab';
  },
};

const obsidian = { Plugin, ItemView, PluginSettingTab, Setting, Notice, notices, normalizePath, Keymap };

// Makes require('obsidian') resolve to the fake module in this process.
function hookObsidianRequire() {
  if (hookObsidianRequire.done) return;
  const fakePath = path.join(__dirname, 'fake-obsidian.js');
  const original = Module._resolveFilename;
  Module._resolveFilename = function resolve(request, ...rest) {
    if (request === 'obsidian') return fakePath;
    return original.call(this, request, ...rest);
  };
  hookObsidianRequire.done = true;
}

// ------------------------------------------------------ app and vault ----

function eventSource() {
  const handlers = {};
  return {
    on(name, fn) { (handlers[name] ||= []).push(fn); return { name, fn }; },
    trigger(name, ...args) { for (const fn of handlers[name] || []) fn(...args); },
  };
}

// A fake app over a list of TFile-like objects and a resolved-link table.
// `disk` holds non-indexed files (such as .context/activation.json) that are
// only reachable through vault.adapter.
function createApp({ files = [], links = {}, fileCaches = {}, disk = {} } = {}) {
  const vaultEvents = eventSource(), cacheEvents = eventSource(), workspaceEvents = eventSource();
  const diskFiles = new Map(Object.entries(disk).map(([p, text]) => [p, { text, mtime: 1 }]));
  const adapter = {
    reads: 0, stats: 0,
    async stat(p) { this.stats++; const f = diskFiles.get(p); return f ? { type: 'file', mtime: f.mtime, ctime: f.mtime, size: Buffer.byteLength(f.text) } : null; },
    async read(p) { this.reads++; const f = diskFiles.get(p); if (!f) throw new Error('ENOENT'); return f.text; },
    async exists(p) { return diskFiles.has(p); },
    writes: 0,
    async write() { this.writes++; throw new Error('the plugin must not write through the adapter'); },
    setFile(p, text) { const prev = diskFiles.get(p); diskFiles.set(p, { text, mtime: (prev ? prev.mtime : 0) + 1000 }); },
    removeFile(p) { diskFiles.delete(p); },
  };
  const vault = {
    files, adapter,
    getRoot() { return folderTree(this.files); },
    getMarkdownFiles() { return this.files.filter(f => f.extension === 'md'); },
    on: vaultEvents.on, trigger: vaultEvents.trigger,
  };
  const metadataCache = {
    resolvedLinks: links,
    getFileCache(file) { return fileCaches[file.path] || null; },
    on: cacheEvents.on, trigger: cacheEvents.trigger,
  };
  const app = { vault, metadataCache, _pluginData: null };
  app.workspace = {
    _factories: {}, leaves: [], activeLeaf: null,
    on: workspaceEvents.on, trigger: workspaceEvents.trigger,
    onLayoutReady(cb) { cb(); },
    getLeavesOfType(type) { return this.leaves.filter(l => l.type === type); },
    getLeaf(type) { const leaf = createLeaf(app); leaf.openedAs = type; this.leaves.push(leaf); return leaf; },
    // Revealing a deferred leaf loads its real view, as Obsidian 1.7.2+ does.
    async revealLeaf(leaf) { this.activeLeaf = leaf; this.revealed = (this.revealed || 0) + 1; await leaf.loadIfDeferred(); },
  };
  return app;
}

function createLeaf(app) {
  return {
    app, type: null, view: null, opened: [], deferred: false,
    async setViewState({ type }) {
      this.type = type;
      this.view = app.workspace._factories[type](this);
      await this.view.onOpen();
    },
    // A restored leaf whose view is still a placeholder (DeferredView).
    defer(type) { this.type = type; this.deferred = true; this.view = { getViewType: () => type }; },
    async loadIfDeferred() {
      if (!this.deferred) return;
      this.deferred = false;
      this.view = app.workspace._factories[this.type](this);
      await this.view.onOpen();
    },
    async openFile(file) { this.opened.push(file.path); },
  };
}

module.exports = {
  FakeEl, FakeCanvas, createFakeGL, FakeWorker, FakeBlob, FakeURL, createFrameQueue, createFakeWindow, installGlobals,
  obsidian, notices, normalizePath, hookObsidianRequire, createApp, createLeaf,
};
