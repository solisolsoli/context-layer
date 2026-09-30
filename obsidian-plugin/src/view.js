'use strict';

// The 3D view. Notes are points ("neurons"), resolved links are curved
// ribbons ("synapses"). Linked notes fill a sphere; unlinked notes form a
// faint outer shell. An optional overlay replays the last context-layer
// synaptic retrieval: activated notes light up, the traversed links pulse in
// hop order, and everything else dims.

const obsidian = require('obsidian');
const Layout = require('./layout');
const GLProgram = require('./gl-program');
const Metrics = require('./metrics');
const Field = require('./field');
const Shaders = require('./shaders');
const Palette = require('./palette');
const Edges = require('./edges');
const Regions = require('./regions');
const Activation = require('./activation');
const { BrainModel, isMarkdownFile, isFolder } = require('./graph');
const { clamp, lerp, nearestAngle, mat4Perspective, mat4LookAt } = require('./math');

const { ItemView, Notice } = obsidian;
const VIEW_TYPE = 'context-layer-brain-view';
const EDGE_SEGMENTS = 12;                 // ribbon tessellation per link
const { NODE_ALPHA, ORPHAN_ALPHA } = Palette;
const REGION_DIM = 0.25;                  // notes outside a focused region
const { OVERLAY_NODE_DIM } = Activation;   // notes outside an active retrieval
const OVERLAY_EDGE_DIM = 0.3;             // links outside an active retrieval
const OVERLAY_EDGE_ALPHA = 0.22, OVERLAY_EDGE_PULSE = 0.55, OVERLAY_EDGE_STATIC = 0.45, OVERLAY_EDGE_HALF_WIDTH = 1.1;
const MARK_SIZE = 2.4;                    // advisor ring size relative to its note
const DYNAMIC_CAPACITY = 640;             // transient points: fires and pulses
const TONE_EXPOSURE_HDR = 1.25, TONE_EXPOSURE_LDR = 2.0;
const FIT_RADIUS = 1.13;                  // world radius framed at zoom 1
const MIN_ZOOM = 0.3, MAX_ZOOM = 2.4;
const HUD_INTERVAL_MS = 250;
const POLL_INTERVAL_MS = 1000;
// Vault events are coalesced: one rebuild at most REFRESH_DEBOUNCE_MS after
// the last event, and at least every REFRESH_MAX_WAIT_MS during a storm.
const REFRESH_DEBOUNCE_MS = 150, REFRESH_MAX_WAIT_MS = 1000;
const MAX_PENDING_SIGNALS = 64, MAX_SIGNAL_EVENTS = 256;
const KEY_ROTATE = 0.12, KEY_ZOOM = 0.12;

const nextTask = () => new Promise(resolve => setTimeout(resolve, 0));
function errorMessage(err) { return err && err.message ? err.message : String(err); }

class BrainView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this.model = null; this.gl = null; this.rafId = null;
    this.hoverNode = null; this.focusRegionKey = null; this.regionTable = null;
    this.fires = []; this.pulses = []; this.signalEvents = [];
    this.errorLog = [];
    this.activationTrace = null;   // last valid trace read from disk (fresh or not)
    this.overlay = null;           // Activation.mapOverlay(...) while shown
    this.traceMatch = null;        // mapping of the current fresh trace, shown or not
    this.yaw = 0.6; this.pitch = 0.28; this.zoom = 1; this.zoomTarget = null;
    this.center = [0, 0, 0]; this.centerTarget = null; this.focusTarget = null;
    this.drag = { dragging: false, lastX: 0, lastY: 0, velYaw: 0, velPitch: 0, pointerId: null, moved: 0 };
    this.lastInteraction = 0; this.reducedMotion = false;
    this._closed = false; this._renderReady = false; this._lastHud = -Infinity;
    this._refreshDirty = false; this._refreshTimer = null; this._refreshFirstAt = 0;
    this.ready = new Promise(resolve => { this._resolveReady = resolve; });
  }

  getViewType() { return VIEW_TYPE; }
  getDisplayText() { return 'Context Layer Brain View'; }
  getIcon() { return 'brain'; }

  get settings() { return this.plugin.settings; }

  // The window and document the view lives in. In a pop-out window these
  // differ from the main window's globals.
  get win() { return (this.contentEl && this.contentEl.win) || (typeof window !== 'undefined' ? window : null); }
  get doc() { return (this.contentEl && this.contentEl.doc) || (typeof document !== 'undefined' ? document : null); }

  // A trace the user cleared stays cleared across view reloads in a session.
  get dismissedKey() { return this.plugin.dismissedKey || null; }
  set dismissedKey(key) { this.plugin.dismissedKey = key; }

  requestFrame(callback) {
    const win = this.win;
    this._rafWin = win;
    return win && typeof win.requestAnimationFrame === 'function' ? win.requestAnimationFrame(callback) : requestAnimationFrame(callback);
  }

  cancelFrame(id, win = this._rafWin) {
    if (!id) return;
    if (win && typeof win.cancelAnimationFrame === 'function') win.cancelAnimationFrame(id); else cancelAnimationFrame(id);
  }

  async onOpen() {
    this._closed = false; this._renderReady = false;
    try {
      this.setupReducedMotion();
      const root = this.contentEl;
      root.empty(); root.addClass('nb-root');
      this.canvas = root.createEl('canvas', { cls: 'nb-canvas', attr: {
        tabindex: '0', role: 'img',
        'aria-label': '3D link graph of this vault. Arrow keys turn the view, plus and minus zoom, Home resets it. The retrieval summary lists the notes of the last retrieval.',
      } });
      this.hudEl = root.createDiv({ cls: 'nb-hud' });
      this.hudEl.createDiv({ cls: 'nb-title', text: 'CONTEXT LAYER BRAIN VIEW', attr: { lang: 'en' } });
      this.countersEl = this.hudEl.createDiv({ cls: 'nb-counters' });
      this.activationEl = this.hudEl.createDiv({ cls: 'nb-activation nb-hidden' });
      this.advisorEl = this.hudEl.createDiv({ cls: 'nb-advisor nb-hidden' });
      this.overlayKeyEl = this.hudEl.createDiv({ cls: 'nb-overlay-key nb-hidden' });
      this.devEl = this.hudEl.createDiv({ cls: 'nb-dev nb-hidden' });
      this.legendEl = root.createDiv({ cls: 'nb-legend nb-hidden', attr: { role: 'group', 'aria-label': 'Regions' } });
      this.summaryEl = root.createEl('details', { cls: 'nb-summary nb-hidden' });
      this.summaryEl.createEl('summary', { text: 'Retrieval summary' });
      this.summaryStatusEl = this.summaryEl.createDiv({ cls: 'nb-summary-status' });
      this.summaryListEl = this.summaryEl.createEl('ul', { cls: 'nb-summary-list', attr: { 'aria-label': 'Notes in the last retrieval' } });
      // Advisor layer: a read-only panel and its toggle, hidden by default.
      this.advisorLayerEl = root.createDiv({ cls: 'nb-advisor-layer' });
      this.advisorPanelEl = this.advisorLayerEl.createDiv({ cls: 'nb-advisor-panel nb-hidden', attr: { role: 'region', 'aria-label': 'Advisor layer' } });
      this.advisorToggleEl = this.advisorLayerEl.createEl('button', { cls: 'nb-advisor-toggle', text: 'Show advisor layer', attr: { type: 'button', 'aria-pressed': 'false' } });
      this.advisorToggleEl.onclick = () => { this.toggleAdvisorLayer().catch(err => this.logError('advisor layer', err)); };
      this.liveEl = root.createDiv({ cls: 'nb-sr-only', attr: { role: 'status', 'aria-live': 'polite' } });
      this.tooltipEl = root.createDiv({ cls: 'nb-tooltip nb-hidden' });
      this.renderOverlayKey();
      await nextTask(); if (this._closed) return;

      // Opaque canvas: the blend mode writes low alpha, which would wash out
      // against the theme background on a transparent canvas.
      this.gl = this.canvas.getContext('webgl', { alpha: false, antialias: true, premultipliedAlpha: false, powerPreference: 'high-performance' })
        || this.canvas.getContext('experimental-webgl', { alpha: false });
      if (!this.gl) { this.showMessage('WebGL is not available here, so this view cannot render.'); return; }
      await nextTask(); if (this._closed) return;
      const token = this._glInitToken = (this._glInitToken || 0) + 1;
      if (!await this.initGL(token) || this._closed) return;
      await nextTask(); if (this._closed) return;

      this.model = new BrainModel(this.app, { positionCache: this.plugin.positionCache || {}, classify: this.plugin.regionClassifier() });
      await this.model.buildFromVaultAsync();
      if (this._closed) return;
      this.refreshRegions();
      await nextTask(); if (this._closed) return;
      if (!await this.prepareStartupVisuals() || this._closed) return;

      this.metrics = new Metrics.FrameMetrics({ capacity: 600 });
      this.metrics.reset('view', performance.now());
      this._motionTime = 0;
      this.startLayoutWorker();
      this.registerViewEvents();
      this.resizeCanvas();
      this.lastFrameTime = performance.now();
      this._renderBound = t => this.render(t);
      this._renderReady = true;
      this.model.setPaused(!this.isVisible());
      this.rafId = this.requestFrame(this._renderBound);
      this.startActivationWatcher();
      this.applySettings();
    } catch (err) {
      if (this._closed) return;
      this.logError('open', err);
      new Notice('Context Layer Brain View failed to start: ' + errorMessage(err));
    } finally {
      this._resolveReady();
    }
  }

  async onClose() {
    this._closed = true; this._renderReady = false;
    this._glInitToken = (this._glInitToken || 0) + 1;
    if (this.rafId) { this.cancelFrame(this.rafId); this.rafId = null; }
    if (this._hoverRafId) { this.cancelFrame(this._hoverRafId, this._hoverRafWin); this._hoverRafId = null; }
    clearTimeout(this._refreshTimer); this._refreshTimer = null;
    this.unbindWindow();
    this.teardownReducedMotion();
    if (this._resizeObserver) { try { this._resizeObserver.disconnect(); } catch (_) { /* already gone */ } this._resizeObserver = null; }
    if (this.model) {
      this.model.destroy();
      try { await this.plugin.flushPersist(this.model); } catch (_) { /* best effort */ }
    }
    this.destroyGL();
    this._resolveReady();
  }

  showMessage(text) {
    this.contentEl.createDiv({ cls: 'nb-message', text });
  }

  logError(where, err) {
    console.error('[context-layer-brain] ' + where, err);
    this.errorLog.push(where + ': ' + errorMessage(err));
    if (this.errorLog.length > 50) this.errorLog.shift();
  }

  setupReducedMotion() {
    this.teardownReducedMotion();
    const win = this.win;
    const mq = win && typeof win.matchMedia === 'function' ? win.matchMedia('(prefers-reduced-motion: reduce)') : null;
    this._mq = mq;
    this._systemReducedMotion = !!(mq && mq.matches);
    this._mqHandler = e => { this._systemReducedMotion = !!e.matches; this.updateReducedMotion(); };
    if (mq && mq.addEventListener) mq.addEventListener('change', this._mqHandler);
    this.updateReducedMotion();
  }

  teardownReducedMotion() {
    if (this._mq && this._mqHandler && this._mq.removeEventListener) this._mq.removeEventListener('change', this._mqHandler);
    this._mq = null;
  }

  updateReducedMotion() {
    this.reducedMotion = this.settings.respectReducedMotion !== false && !!this._systemReducedMotion;
  }

  // Re-applies every setting; cheap enough to call on any change.
  applySettings() {
    this.updateReducedMotion();
    if (!this.model || !this.gl) return;
    const s = this.settings;
    if (s.bloom && !this.bloomOk) this.initBloom();
    if (this._classifierSource !== s.regionSource) {
      this._classifierSource = s.regionSource;
      this.model.setClassifier(this.plugin.regionClassifier());
    }
    this.refreshRegions(true);
    this._edgePlan = null; this._nodeListKey = null;
    this.model.buffersDirty = true; this._edgeStyleDirty = true;
    this.devEl.toggleClass('nb-hidden', !s.developerDiagnostics);
    if (!s.activationOverlay) { this.setOverlay(null); this.traceMatch = null; this.updateActivationHud(Date.now()); }
    else { this.syncOverlay(true); this.pollActivation(true); }
    this.updateAdvisorLayer();
  }

  // -- setup ------------------------------------------------------------------

  async initGL(token) {
    const gl = this.gl;
    const cancelled = () => this._closed || this.gl !== gl || token !== this._glInitToken;
    const jobs = await Promise.allSettled([
      GLProgram.createProgramAsync(gl, Shaders.POINT_VS, Shaders.POINT_FS, null, cancelled),
      GLProgram.createProgramAsync(gl, Shaders.LINE_VS, Shaders.LINE_FS, { aTS: 0 }, cancelled),
    ]);
    if (cancelled() || jobs.some(j => j.status === 'rejected')) {
      for (const job of jobs) if (job.status === 'fulfilled') gl.deleteProgram(job.value);
      if (cancelled()) return false;
      throw jobs.find(j => j.status === 'rejected').reason;
    }
    this.pointProgram = jobs[0].value; this.lineProgram = jobs[1].value;
    // Link ribbons are instanced; without ANGLE_instanced_arrays only the
    // notes are drawn, and the counters line says so.
    this.instExt = gl.getExtension('ANGLE_instanced_arrays');
    if (!this.instExt) this.logError('initGL', new Error('ANGLE_instanced_arrays unavailable: links are not drawn'));
    const attrib = (p, n) => gl.getAttribLocation(p, n), uniform = (p, n) => gl.getUniformLocation(p, n);
    const pp = this.pointProgram, lp = this.lineProgram;
    this.pointAttribs = { position: attrib(pp, 'aPosition'), color: attrib(pp, 'aColor'), size: attrib(pp, 'aSize'), shell: attrib(pp, 'aShell'), ring: attrib(pp, 'aRing') };
    this.pointUniforms = { projection: uniform(pp, 'uProjection'), view: uniform(pp, 'uView'), alphaMult: uniform(pp, 'uAlphaMult'),
      time: uniform(pp, 'uTime'), eyePos: uniform(pp, 'uEyePos'), viewport: uniform(pp, 'uViewport') };
    this.lineAttribs = { ts: attrib(lp, 'aTS'), p0: attrib(lp, 'aP0'), p1: attrib(lp, 'aP1'), p2: attrib(lp, 'aP2'), p3: attrib(lp, 'aP3'),
      colA: attrib(lp, 'aColA'), colB: attrib(lp, 'aColB') };
    this.lineUniforms = { projection: uniform(lp, 'uProjection'), view: uniform(lp, 'uView'), viewport: uniform(lp, 'uViewport'),
      dpr: uniform(lp, 'uDPR'), widthMult: uniform(lp, 'uWidthMult'), time: uniform(lp, 'uTime') };
    this.buffers = {};
    for (const name of ['nodePos', 'nodeColor', 'nodeSize', 'nodeShell', 'edgeTemplate', 'edgeCtrl', 'edgeStyle',
      'hlCtrl', 'hlStyle', 'ovCtrl', 'ovStyle', 'dynPos', 'dynColor', 'dynSize', 'markPos', 'markColor', 'markSize', 'markRing']) this.buffers[name] = gl.createBuffer();
    this.projMat = new Float32Array(16); this.viewMat = new Float32Array(16);
    const tmpl = new Float32Array((EDGE_SEGMENTS + 1) * 4);
    for (let k = 0; k <= EDGE_SEGMENTS; k++) {
      const t = k / EDGE_SEGMENTS;
      tmpl[k * 4] = t; tmpl[k * 4 + 1] = 1; tmpl[k * 4 + 2] = t; tmpl[k * 4 + 3] = -1;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.edgeTemplate);
    gl.bufferData(gl.ARRAY_BUFFER, tmpl, gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
    this.edgeCount = 0; this.hlCount = 0; this.ovCount = 0; this.dynCount = 0; this.nodeCount = 0; this.markCount = 0;
    this.bloomOk = false;
    if (this.settings.bloom) this.initBloom();
    return true;
  }

  initBloom() {
    const gl = this.gl;
    this.bloomOk = false;
    try {
      this.quadProgram = createProgramSync(gl, Shaders.QUAD_VS, Shaders.QUAD_FS);
      this.quadAttribs = { pos: gl.getAttribLocation(this.quadProgram, 'aQuadPos') };
      this.quadUniforms = { tex: gl.getUniformLocation(this.quadProgram, 'uTex'), blurDir: gl.getUniformLocation(this.quadProgram, 'uBlurDir'),
        intensity: gl.getUniformLocation(this.quadProgram, 'uIntensity') };
      this.compProgram = createProgramSync(gl, Shaders.QUAD_VS, Shaders.COMPOSITE_FS);
      this.compAttribs = { pos: gl.getAttribLocation(this.compProgram, 'aQuadPos') };
      this.compUniforms = { scene: gl.getUniformLocation(this.compProgram, 'uScene'), bloom: gl.getUniformLocation(this.compProgram, 'uBloom'),
        bloomK: gl.getUniformLocation(this.compProgram, 'uBloomK'), exposure: gl.getUniformLocation(this.compProgram, 'uExposure') };
      // Half-float targets give headroom for the tone map when available.
      this.halfFloatExt = gl.getExtension('OES_texture_half_float');
      this.halfFloatLinear = !!gl.getExtension('OES_texture_half_float_linear');
      gl.getExtension('EXT_color_buffer_half_float');
      this.hdr = !!this.halfFloatExt;
      this.quadBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, null);
      this.fboFull = this.fboHalf = this.fboBlurA = this.fboBlurB = null;
      this.bloomOk = true;
    } catch (err) {
      this.logError('bloom init (continuing without bloom)', err);
      this.bloomOk = false;
    }
  }

  createFbo(w, h, hdr) {
    const gl = this.gl;
    w = Math.max(1, w); h = Math.max(1, h);
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    const type = hdr && this.halfFloatExt ? this.halfFloatExt.HALF_FLOAT_OES : gl.UNSIGNED_BYTE;
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, type, null);
    const filter = (hdr && !this.halfFloatLinear) ? gl.NEAREST : gl.LINEAR;
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    const complete = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.bindTexture(gl.TEXTURE_2D, null);
    if (!complete) { gl.deleteFramebuffer(fbo); gl.deleteTexture(tex); return null; }
    return { fbo, tex, w, h };
  }

  deleteFbo(target) {
    if (!target || !this.gl) return;
    try { this.gl.deleteFramebuffer(target.fbo); this.gl.deleteTexture(target.tex); } catch (_) { /* context may be gone */ }
  }

  deleteBloomTargets() {
    this.deleteFbo(this.fboFull); this.deleteFbo(this.fboHalf); this.deleteFbo(this.fboBlurA); this.deleteFbo(this.fboBlurB);
    this.fboFull = this.fboHalf = this.fboBlurA = this.fboBlurB = null;
  }

  ensureBloomTargets() {
    if (!this.bloomOk || !this.gl) return false;
    const fullW = this.canvas.width, fullH = this.canvas.height;
    const halfW = Math.max(1, Math.floor(fullW / 2)), halfH = Math.max(1, Math.floor(fullH / 2));
    if (this.fboFull && this.fboFull.w === fullW && this.fboFull.h === fullH && this.fboHalf && this.fboHalf.w === halfW && this.fboHalf.h === halfH) return true;
    const build = hdr => {
      this.deleteBloomTargets();
      this.fboFull = this.createFbo(fullW, fullH, hdr);
      this.fboHalf = this.createFbo(halfW, halfH, hdr);
      this.fboBlurA = this.createFbo(halfW, halfH, hdr);
      this.fboBlurB = this.createFbo(halfW, halfH, hdr);
      return !!(this.fboFull && this.fboHalf && this.fboBlurA && this.fboBlurB);
    };
    let ok = this.hdr ? build(true) : false;
    if (!ok) { this.hdr = false; ok = build(false); }
    if (!ok) { this.deleteBloomTargets(); this.bloomOk = false; }
    return this.bloomOk;
  }

  drawQuad(target, sourceTex, blurDir, intensity) {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, target ? target.fbo : null);
    gl.viewport(0, 0, target ? target.w : this.canvas.width, target ? target.h : this.canvas.height);
    gl.useProgram(this.quadProgram);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, sourceTex);
    gl.uniform1i(this.quadUniforms.tex, 0);
    gl.uniform2f(this.quadUniforms.blurDir, blurDir[0], blurDir[1]);
    gl.uniform1f(this.quadUniforms.intensity, intensity);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
    gl.enableVertexAttribArray(this.quadAttribs.pos);
    gl.vertexAttribPointer(this.quadAttribs.pos, 2, gl.FLOAT, false, 0, 0);
    gl.disable(gl.DEPTH_TEST);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  destroyGL() {
    const gl = this.gl;
    if (!gl) return;
    try {
      if (this.buffers) for (const key in this.buffers) if (this.buffers[key]) gl.deleteBuffer(this.buffers[key]);
      for (const p of [this.pointProgram, this.lineProgram, this.quadProgram, this.compProgram]) if (p) gl.deleteProgram(p);
      if (this.quadBuffer) gl.deleteBuffer(this.quadBuffer);
      this.deleteBloomTargets();
      const lose = gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();
    } catch (err) { this.logError('GL cleanup', err); }
    this.gl = null;
  }

  startLayoutWorker() {
    const url = URL.createObjectURL(new Blob([Layout.workerSource()], { type: 'text/javascript' }));
    try {
      this.model.attachWorker(new Worker(url), e => this.logError('layout worker', e), () => this.plugin.schedulePersist(this.model));
    } finally { URL.revokeObjectURL(url); }
  }

  registerViewEvents() {
    this.registerEvent(this.app.workspace.on('active-leaf-change', () => this.handleVisibility()));
    this.registerEvent(this.app.workspace.on('layout-change', () => this.handleVisibility()));
    this.registerDomEvent(this.canvas, 'webglcontextlost', e => { e.preventDefault(); this._contextLost = true; this.handleVisibility(); });
    this.registerDomEvent(this.canvas, 'webglcontextrestored', async () => {
      this._contextLost = true;
      const token = this._glInitToken = (this._glInitToken || 0) + 1;
      try {
        if (!await this.initGL(token) || this._closed) return;
        this._contextLost = false; this.model.buffersDirty = true; this._edgePlan = null; this._ovDirty = true; this._marksDirty = true;
        this.handleVisibility();
      } catch (err) { if (!this._closed) this.logError('context restore', err); }
    });
    this.registerVaultEvents();
    this.registerInteractionEvents();
    this.bindWindow();
    // A view dragged into another window keeps working there.
    if (typeof this.contentEl.onWindowMigrated === 'function') {
      const off = this.contentEl.onWindowMigrated(() => this.handleWindowMigrated());
      if (typeof off === 'function') this.register(off);
    }
    if (typeof ResizeObserver !== 'undefined') {
      this._resizeObserver = new ResizeObserver(() => { this.resizeCanvas(); this.handleVisibility(); });
      this._resizeObserver.observe(this.contentEl);
    }
  }

  // Listeners on the view's own window and document (not the main window's
  // globals), removed on close and moved when the view changes window.
  bindWindow() {
    this.unbindWindow();
    const win = this.win, doc = this.doc, off = [];
    const on = (target, type, fn) => {
      if (!target || typeof target.addEventListener !== 'function') return;
      target.addEventListener(type, fn); off.push(() => target.removeEventListener(type, fn));
    };
    on(doc, 'visibilitychange', () => this.handleVisibility());
    on(win, 'resize', () => this.resizeCanvas());
    on(win, 'focus', () => { this.pollActivation(true); });
    this._unbindWindow = () => { for (const fn of off) fn(); };
  }

  unbindWindow() { if (this._unbindWindow) { this._unbindWindow(); this._unbindWindow = null; } }

  handleWindowMigrated() {
    if (this._closed) return;
    if (this.rafId) { this.cancelFrame(this.rafId); this.rafId = null; }
    this.bindWindow();
    this.setupReducedMotion();
    this.resizeCanvas();
    this.handleVisibility();
  }

  // Colours and sizes animate from their current value, so the first
  // palette state and the first link plan are prepared in yielding slices.
  async prepareStartupVisuals() {
    const model = this.model, revision = model.structureVersion;
    const token = this._startupVisualToken = (this._startupVisualToken || 0) + 1;
    const cancelled = () => this._closed || token !== this._startupVisualToken || model.structureVersion !== revision;
    this._degreePalette = Palette.createAnimator();
    if (!await this._degreePalette.seedInitialAsync(model.orderedNodes, { cancelled, sliceMs: 5 }) || cancelled()) return false;
    this._degreePaletteRevision = revision;
    const plan = await Edges.buildEdgePlanAsync(model, node => this.baseNodeColor(node), { cancelled, sliceMs: 5 });
    if (!plan || cancelled()) return false;
    this._preparedEdgePlan = plan; this._preparedEdgePlanKey = this.edgePlanKey();
    return true;
  }

  // -- regions ----------------------------------------------------------------

  refreshRegions(force = false) {
    const m = this.model; if (!m) return;
    const stamp = m.viewRevision + ':' + m.regionVersion + ':' + this.settings.regionSource;
    if (!force && stamp === this._regionStamp) return;
    this._regionStamp = stamp;
    const table = Regions.buildRegionTable(Array.from(m.nodes.values(), n => n.regionKey), { source: this.settings.regionSource });
    for (const n of m.nodes.values()) n.region = table.lookup(n.regionKey);
    this.regionTable = table;
    if (this.focusRegionKey && !table.byKey.has(this.focusRegionKey) && this.focusRegionKey !== Regions.OTHER_KEY) this.focusRegionKey = null;
    if (this.settings.paletteMode === 'folder') { this._edgePlan = null; m.buffersDirty = true; }
    this.renderLegend();
  }

  // Legend entries are buttons (keyboard and screen-reader reachable). Their
  // click handlers are properties of the entries themselves, so a redraw
  // replaces them instead of adding listeners.
  renderLegend() {
    if (!this.legendEl) return;
    this.legendEl.empty();
    const table = this.regionTable, show = this.settings.paletteMode === 'folder' && table && table.regions.length > 0;
    this.legendEl.toggleClass('nb-hidden', !show);
    if (!show) return;
    const entries = table.other.count ? table.regions.concat([table.other]) : table.regions;
    for (const region of entries) {
      const active = region.key === this.focusRegionKey;
      const item = this.legendEl.createEl('button', { cls: 'nb-legend-item' + (active ? ' nb-legend-active' : ''), attr: {
        type: 'button', 'aria-pressed': active ? 'true' : 'false',
        'aria-label': region.name + ', ' + region.count + (region.count === 1 ? ' note' : ' notes') + (active ? ', shown alone' : ''),
      } });
      item.createSpan({ cls: 'nb-legend-dot nb-region-' + (region.index >= 0 ? region.index : 'other') });
      item.createSpan({ cls: 'nb-legend-label', text: region.name });
      item.createSpan({ cls: 'nb-legend-count', text: String(region.count) });
      item.onclick = () => this.focusRegion(region.key === this.focusRegionKey ? null : region.key);
    }
  }

  focusRegion(key) {
    this.focusRegionKey = key;
    if (key && this.model) {
      const dir = this.model.regionDirection(key);
      this.setFocusDirection(dir);
    } else this.focusTarget = null;
    if (this.model) { this.model.buffersDirty = true; this._edgeStyleDirty = true; }
    this.renderLegend();
  }

  setFocusDirection(dir) {
    const len = Math.hypot(dir[0], dir[1], dir[2]);
    if (len < 1e-6) { this.focusTarget = null; return; }
    const yaw = Math.atan2(dir[0] / len, dir[2] / len), pitch = clamp(Math.asin(clamp(dir[1] / len, -1, 1)), -1.2, 1.2);
    this.focusTarget = { yaw: nearestAngle(this.yaw, yaw), pitch };
  }

  // -- node and link buffers ----------------------------------------------------

  baseNodeColor(node) {
    if (this.settings.paletteMode === 'folder') return node.region ? node.region.rgb : Regions.hexToRgb(Regions.OTHER_COLOR);
    return this._degreePalette?.get(node)?.color || Palette.styleForDegree(node.degree).color;
  }

  edgePlanKey() { return (this.model ? this.model.viewRevision : 0) + ':' + this.settings.paletteMode; }

  // Writes colour (rgba) and size of the note at buffer index i.
  writeNodeStyle(i, node) {
    const arr = this._nodeArr;
    const degreeStyle = this._degreePalette?.get(node) || Palette.styleForDegree(node.degree);
    let rgb = this.baseNodeColor(node), size = degreeStyle.size;
    let alpha = node.role === 'orphan' ? ORPHAN_ALPHA : NODE_ALPHA;
    if (this.focusRegionKey && node.region?.key !== this.focusRegionKey) alpha *= REGION_DIM;
    if (this.overlay) {
      const entry = this.overlay.nodes.get(node.path);
      if (entry && entry.node === node) { rgb = entry.color; size *= entry.sizeMult; alpha = entry.alpha; }
      else alpha *= OVERLAY_NODE_DIM;
    }
    if (this.hoverNode === node) { alpha = 0.95; size *= 1.25; }
    arr.color[i * 4] = rgb[0]; arr.color[i * 4 + 1] = rgb[1]; arr.color[i * 4 + 2] = rgb[2]; arr.color[i * 4 + 3] = alpha;
    arr.size[i] = size;
  }

  // Notes to draw. Unlinked notes can be hidden, but notes of the shown
  // retrieval are always drawn so their ribbons never end in empty space.
  renderNodeList() {
    const m = this.model, showOrphans = this.settings.showOrphans !== false;
    const key = m.viewRevision + ':' + showOrphans + ':' + (this.overlay ? this.overlay.key + '#' + this._overlayRevision : '');
    if (this._nodeListKey !== key) {
      const all = m.orderedNodes || [], ov = this.overlay;
      this._renderNodes = showOrphans ? all : all.filter(n => n.role !== 'orphan' || (ov && ov.nodes.get(n.path)?.node === n));
      this._nodeIndexMap = new Map();
      this._renderNodes.forEach((n, i) => this._nodeIndexMap.set(n, i));
      this._nodeListKey = key;
    }
    return this._renderNodes;
  }

  rebuildNodeBuffers() {
    const gl = this.gl, m = this.model, now = performance.now();
    if (!this._degreePalette) this._degreePalette = Palette.createAnimator();
    const revision = m.structureVersion || 0;
    if (this._degreePaletteRevision !== revision) {
      this._degreePalette.refresh(m.orderedNodes || [], now, { visible: this.isVisible(), reducedMotion: this.reducedMotion });
      this._degreePaletteRevision = revision;
    }
    const nodes = this.renderNodeList(), n = nodes.length;
    if (!this._nodeArr || this._nodeArr.cap < n) {
      const cap = n + 64;
      this._nodeArr = { cap, pos: new Float32Array(cap * 3), color: new Float32Array(cap * 4), size: new Float32Array(cap), shell: new Float32Array(cap) };
    }
    const { pos, color, size, shell } = this._nodeArr;
    for (let i = 0; i < n; i++) {
      const node = nodes[i];
      pos[i * 3] = node.pos[0]; pos[i * 3 + 1] = node.pos[1]; pos[i * 3 + 2] = node.pos[2];
      shell[i] = node.role === 'orphan' ? 1 : 0;
      this.writeNodeStyle(i, node);
    }
    this.nodeCount = n;
    for (const [name, arr, count] of [['nodePos', pos, n * 3], ['nodeColor', color, n * 4], ['nodeSize', size, n], ['nodeShell', shell, n]]) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers[name]);
      gl.bufferData(gl.ARRAY_BUFFER, arr.subarray(0, count), gl.DYNAMIC_DRAW);
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
    m.buffersDirty = false;
    this._marksDirty = true;
  }

  // Advisor marks: one ring sprite per judged note of the shown retrieval.
  rebuildMarkBuffers() {
    this._marksDirty = false;
    const ov = this.overlay, gl = this.gl;
    const entries = ov ? Array.from(ov.nodes.values()).filter(e => e.mark && e.node === this.model.nodes.get(e.path)) : [];
    this.markCount = entries.length;
    if (!entries.length) return;
    const n = entries.length, pos = new Float32Array(n * 3), color = new Float32Array(n * 4), size = new Float32Array(n), ring = new Float32Array(n);
    entries.forEach((e, i) => {
      pos.set(e.node.pos, i * 3);
      color[i * 4] = e.mark.rgb[0]; color[i * 4 + 1] = e.mark.rgb[1]; color[i * 4 + 2] = e.mark.rgb[2]; color[i * 4 + 3] = e.mark.alpha;
      size[i] = (this._degreePalette?.get(e.node) || Palette.styleForDegree(e.node.degree)).size * e.sizeMult * MARK_SIZE;
      ring[i] = e.mark.ring;
    });
    for (const [name, arr] of [['markPos', pos], ['markColor', color], ['markSize', size], ['markRing', ring]]) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers[name]);
      gl.bufferData(gl.ARRAY_BUFFER, arr, gl.DYNAMIC_DRAW);
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
  }

  // Degree changes fade over Palette.DURATION_MS. Only transitioning notes
  // and their incident links are touched; each buffer is uploaded once.
  updatePaletteTransitions(now) {
    const m = this.model, anim = this._degreePalette; if (!m || !anim) return;
    const visible = this.isVisible();
    if (this._degreePaletteRevision !== m.structureVersion) {
      anim.refresh(m.orderedNodes || [], now, { visible, reducedMotion: this.reducedMotion });
      this._degreePaletteRevision = m.structureVersion;
    }
    const changed = anim.tick(now, { visible, reducedMotion: this.reducedMotion });
    if (!changed.length || !this._nodeArr) return;
    const gl = this.gl, plan = this._edgePlan, indices = this._nodeIndexMap, styleArray = this._edgeStyleArr;
    for (const node of changed) {
      const index = indices?.get(node);
      if (index !== undefined) this.writeNodeStyle(index, node);
      const rgb = this.baseNodeColor(node);
      for (const i of plan?.incident.get(node.path) || []) {
        const isA = plan.na[i] === node, endpoint = isA ? plan.colA[i] : plan.colB[i], base = i * 8 + (isA ? 0 : 4);
        endpoint[0] = rgb[0]; endpoint[1] = rgb[1]; endpoint[2] = rgb[2];
        if (styleArray) { styleArray[base] = rgb[0]; styleArray[base + 1] = rgb[1]; styleArray[base + 2] = rgb[2]; }
      }
    }
    const count = this.nodeCount || 0;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.nodeColor); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this._nodeArr.color.subarray(0, count * 4));
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.nodeSize); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this._nodeArr.size.subarray(0, count));
    if (plan && styleArray) { this._edgeStyleDirty = true; this._hlDirty = true; }
  }

  updateEdges(now, positionsMoved) {
    const gl = this.gl, m = this.model, key = this.edgePlanKey();
    if (!this._edgePlan || this._edgePlanKey !== key) {
      if (this._preparedEdgePlan && this._preparedEdgePlanKey === key) this._edgePlan = this._preparedEdgePlan;
      else this._edgePlan = Edges.buildEdgePlan(m, node => this.baseNodeColor(node));
      this._preparedEdgePlan = null; this._preparedEdgePlanKey = null;
      this._edgePlanKey = key;
      this.edgeCount = this._edgePlan.n;
      this._edgeCtrl = new Float32Array(this.edgeCount * 12); this._edgeStyleArr = new Float32Array(this.edgeCount * 8);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.edgeCtrl); gl.bufferData(gl.ARRAY_BUFFER, this._edgeCtrl.byteLength, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.edgeStyle); gl.bufferData(gl.ARRAY_BUFFER, this._edgeStyleArr.byteLength, gl.DYNAMIC_DRAW);
      this._edgeStyleDirty = true; positionsMoved = true;
      this.recordNewEdgeAnimations();
    }
    this.updatePaletteTransitions(now);
    const plan = this._edgePlan;
    if (positionsMoved) {
      Edges.updateGuides(plan);
      for (let i = 0; i < plan.n; i++) Edges.edgeControlPoints(plan, i, this._edgeCtrl, i * 12);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.edgeCtrl); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this._edgeCtrl);
      this._hlDirty = true; this._ovDirty = true;
    }
    const focus = this.focusRegionKey, dim = this.overlay ? OVERLAY_EDGE_DIM : 1;
    const styleKey = focus + '|' + dim;
    if (this._edgeStyleKey !== styleKey) { this._edgeStyleKey = styleKey; this._edgeStyleDirty = true; }
    if (this._edgeStyleDirty || this._linesAnimating) {
      let animating = false; const st = this._edgeStyleArr;
      for (let i = 0; i < plan.n; i++) {
        const o = i * 8;
        st[o] = plan.colA[i][0]; st[o + 1] = plan.colA[i][1]; st[o + 2] = plan.colA[i][2];
        st[o + 3] = Edges.edgeAlphaNow(plan, i, focus, this._edgeSpawn, now, dim);
        animating = animating || Edges.edgeAlphaNow.animating;
        st[o + 4] = plan.colB[i][0]; st[o + 5] = plan.colB[i][1]; st[o + 6] = plan.colB[i][2]; st[o + 7] = plan.width[i] * 0.5;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.edgeStyle); gl.bufferSubData(gl.ARRAY_BUFFER, 0, st);
      this._linesAnimating = animating; this._edgeStyleDirty = false; this._hlDirty = true;
      if (!animating) this._edgeSpawn = {};
    }
    if (this._hlDirty) this.updateHoverHighlight();
    this.updateOverlayBuffers(now);
  }

  // Links of the hovered note, redrawn brighter on top.
  updateHoverHighlight() {
    this._hlDirty = false;
    const list = this.hoverNode && this._edgePlan?.incident.get(this.hoverNode.path);
    if (!list?.length) { this.hlCount = 0; return; }
    const n = list.length, gl = this.gl;
    if (!this._hoverArrays || this._hoverArrays.n < n) this._hoverArrays = { n, ctrl: new Float32Array(n * 12), style: new Float32Array(n * 8) };
    const { ctrl, style } = this._hoverArrays;
    const boost = 0.26 / (1 + Math.sqrt(n) * 0.06);
    for (let k = 0; k < n; k++) {
      const i = list[k];
      ctrl.set(this._edgeCtrl.subarray(i * 12, i * 12 + 12), k * 12); style.set(this._edgeStyleArr.subarray(i * 8, i * 8 + 8), k * 8);
      style[k * 8 + 3] = boost; style[k * 8 + 7] *= 1.2;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.hlCtrl); gl.bufferData(gl.ARRAY_BUFFER, ctrl.subarray(0, n * 12), gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.hlStyle); gl.bufferData(gl.ARRAY_BUFFER, style.subarray(0, n * 8), gl.DYNAMIC_DRAW);
    this.hlCount = n;
  }

  // Traversed links of the active retrieval, one ribbon per note pair.
  // Geometry is refreshed when notes move; styles are refreshed every frame
  // while pulses travel.
  updateOverlayBuffers(now) {
    const ov = this.overlay, gl = this.gl;
    if (!ov || !ov.edges.length) { this.ovCount = 0; return; }
    const n = ov.edges.length;
    if (!this._ovArr || this._ovArr.n < n) { this._ovArr = { n, ctrl: new Float32Array(n * 12), style: new Float32Array(n * 8) }; this._ovDirty = true; }
    const { ctrl, style } = this._ovArr;
    if (this._ovDirty) {
      for (let k = 0; k < n; k++) Edges.curveBetween(ov.edges[k].a.pos, ov.edges[k].b.pos, ctrl, k * 12);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.ovCtrl); gl.bufferData(gl.ARRAY_BUFFER, ctrl.subarray(0, n * 12), gl.DYNAMIC_DRAW);
      this._ovDirty = false; this._ovStyleDirty = true;
    }
    if (this._ovStyleDirty || !this.reducedMotion) {
      const elapsed = now - (ov.shownAt || now);
      for (let k = 0; k < n; k++) {
        const e = ov.edges[k], o = k * 8;
        const pulse = this.reducedMotion ? null : Activation.pulseAt(e.hop, ov.maxEdgeHop, elapsed);
        const glow = pulse ? pulse.glow : 0;
        style[o] = e.fromColor[0]; style[o + 1] = e.fromColor[1]; style[o + 2] = e.fromColor[2];
        style[o + 3] = this.reducedMotion ? OVERLAY_EDGE_STATIC : OVERLAY_EDGE_ALPHA + OVERLAY_EDGE_PULSE * glow;
        style[o + 4] = e.toColor[0]; style[o + 5] = e.toColor[1]; style[o + 6] = e.toColor[2];
        style[o + 7] = OVERLAY_EDGE_HALF_WIDTH * (1 + 0.6 * glow);
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.ovStyle); gl.bufferData(gl.ARRAY_BUFFER, style.subarray(0, n * 8), gl.DYNAMIC_DRAW);
      this._ovStyleDirty = false;
    }
    this.ovCount = n;
  }

  // -- vault events -------------------------------------------------------------

  // Every event does constant or per-link work only; the full rebuild is
  // coalesced by scheduleRefresh() and skipped while the view is hidden.
  registerVaultEvents() {
    this._pendingSignals = new Set();
    const vault = this.app.vault, cache = this.app.metadataCache;
    const signal = path => { if (this.isVisible() && this._pendingSignals.size < MAX_PENDING_SIGNALS) this._pendingSignals.add(path); };
    this.registerEvent(vault.on('create', file => { if (isMarkdownFile(file)) { signal(file.path); this.scheduleRefresh(); } }));
    this.registerEvent(vault.on('delete', file => {
      if (!this.model || !file) return;
      try {
        if (isMarkdownFile(file) || isFolder(file)) for (const nb of this.model.removePath(file.path)) signal(nb);
      } catch (err) { this.logError('delete', err); }
      this.scheduleRefresh();
    }));
    this.registerEvent(vault.on('rename', (file, oldPath) => {
      if (!this.model) return;
      try { if (this.model.rename(file, oldPath) && isMarkdownFile(file)) signal(file.path); } catch (err) { this.logError('rename', err); }
      this.scheduleRefresh();
    }));
    this.registerEvent(vault.on('modify', file => {
      if (!this.isVisible()) return;
      const node = this.model?.nodes.get(file.path); if (node) this.triggerFire(node, performance.now());
    }));
    this.registerEvent(cache.on('changed', () => this.scheduleRefresh()));
    this.registerEvent(cache.on('resolved', () => this.scheduleRefresh()));
  }

  scheduleRefresh() {
    this._refreshDirty = true;
    if (this._closed || !this.isVisible()) return;
    const now = performance.now();
    if (!this._refreshFirstAt) this._refreshFirstAt = now;
    clearTimeout(this._refreshTimer);
    const wait = Math.max(0, Math.min(REFRESH_DEBOUNCE_MS, this._refreshFirstAt + REFRESH_MAX_WAIT_MS - now));
    this._refreshTimer = setTimeout(() => this.runRefresh(), wait);
  }

  runRefresh() {
    clearTimeout(this._refreshTimer); this._refreshTimer = null; this._refreshFirstAt = 0;
    if (this._closed || !this.model || !this._refreshDirty || !this.isVisible()) return;
    this._refreshDirty = false;
    try {
      this.model.buildFromVault();
      this.recordNewEdgeAnimations();
      const now = performance.now();
      for (const path of this._pendingSignals) { const n = this.model.nodes.get(path); if (n) this.triggerFire(n, now); }
      this._pendingSignals.clear();
      if (this.hoverNode && this.model.nodes.get(this.hoverNode.path) !== this.hoverNode) { this.hoverNode = null; this._hlDirty = true; this.hideTooltip(); }
      this.syncOverlay(false);
    } catch (err) { this.logError('graph refresh', err); }
  }

  recordNewEdgeAnimations() {
    if (!this.model?.newEdgeKeys?.length) return;
    this._edgeSpawn = this._edgeSpawn || {}; const now = performance.now();
    for (const key of this.model.newEdgeKeys) {
      this._edgeSpawn[key] = now;
      const e = this.model.edgeMap.get(key); if (e) this.triggerFire(this.model.nodes.get(e.a), now);
    }
    this.model.newEdgeKeys = []; this._edgeStyleDirty = true;
  }

  // A real vault change makes a note flash and send short pulses to up to
  // three neighbours.
  triggerFire(node, now) {
    if (!node) return;
    this._lastSignal = this._lastSignal || new Map();
    if (now - (this._lastSignal.get(node.id) || -Infinity) < 500) return;
    this._lastSignal.set(node.id, now); this.signalEvents.push(now);
    if (this.signalEvents.length > MAX_SIGNAL_EVENTS) this.signalEvents.splice(0, this.signalEvents.length - MAX_SIGNAL_EVENTS);
    if (this.reducedMotion) return;
    this.fires.push({ node, start: now, duration: 800 }); if (this.fires.length > 48) this.fires.shift();
    let count = 0;
    for (const path of this.model.adjacency.get(node.path) || []) {
      const to = this.model.nodes.get(path); if (to) this.pulses.push({ from: node, to, start: now, duration: 1200 });
      if (++count === 3) break;
    }
    if (this.pulses.length > 96) this.pulses.splice(0, this.pulses.length - 96);
  }

  updateAmbientAndSignals(now) {
    this.fires = this.fires.filter(f => now - f.start < f.duration && this.model.nodes.get(f.node.path) === f.node);
    this.pulses = this.pulses.filter(p => now - p.start < p.duration && this.model.edgeMap.has(this.model.edgeKey(p.from.path, p.to.path)));
    while (this.signalEvents.length && now - this.signalEvents[0] > 1000) this.signalEvents.shift();
    if (this.reducedMotion) { this.fires.length = 0; this.pulses.length = 0; }
  }

  // Transient points: vault-change fires/pulses and retrieval pulses.
  rebuildDynamicBuffer(now) {
    const gl = this.gl, plan = this._edgePlan, ov = this.overlay;
    const ovPulses = ov && !this.reducedMotion && this._ovArr ? ov.edges.length : 0;
    if (!this.fires.length && !this.pulses.length && !ovPulses) { this.dynCount = 0; return; }
    if (!this._dynamic) this._dynamic = { pos: new Float32Array(DYNAMIC_CAPACITY * 3), color: new Float32Array(DYNAMIC_CAPACITY * 4), size: new Float32Array(DYNAMIC_CAPACITY) };
    const { pos, color, size } = this._dynamic; let n = 0;
    const push = (x, y, z, rgb, alpha, s) => {
      if (n >= DYNAMIC_CAPACITY) return;
      pos[n * 3] = x; pos[n * 3 + 1] = y; pos[n * 3 + 2] = z;
      color[n * 4] = rgb[0]; color[n * 4 + 1] = rgb[1]; color[n * 4 + 2] = rgb[2]; color[n * 4 + 3] = alpha; size[n] = s; n++;
    };
    const point = [0, 0, 0], rgb = [0, 0, 0];
    if (plan && this._edgeCtrl) for (const p of this.pulses) {
      const key = this.model.edgeKey(p.from.path, p.to.path);
      const i = (plan.incident.get(p.from.path) || []).find(j => plan.key[j] === key); if (i === undefined) continue;
      let t = clamp((now - p.start) / p.duration, 0, 1); if (plan.na[i] !== p.from) t = 1 - t;
      Edges.cubicPoint(this._edgeCtrl, i * 12, t, point, 0);
      for (let c = 0; c < 3; c++) { const v = lerp(plan.colA[i][c], plan.colB[i][c], t); rgb[c] = v + (1 - v) * 0.12; }
      push(point[0], point[1], point[2], rgb, 0.55 * Math.sin(Math.PI * clamp((now - p.start) / p.duration, 0, 1)), 0.009);
    }
    for (const f of this.fires) {
      const c = this.baseNodeColor(f.node);
      push(f.node.pos[0], f.node.pos[1], f.node.pos[2], c, 0.4 * (1 - (now - f.start) / f.duration), 0.014);
    }
    if (ovPulses) {
      const elapsed = now - (ov.shownAt || now);
      for (let k = 0; k < ov.edges.length; k++) {
        const e = ov.edges[k], pulse = Activation.pulseAt(e.hop, ov.maxEdgeHop, elapsed);
        if (!pulse) continue;
        Edges.cubicPoint(this._ovArr.ctrl, k * 12, pulse.t, point, 0);
        for (let c = 0; c < 3; c++) rgb[c] = lerp(e.fromColor[c], e.toColor[c], pulse.t) * 0.6 + 0.4;
        push(point[0], point[1], point[2], rgb, 0.85 * pulse.glow, 0.016);
      }
    }
    for (const [name, arr, count] of [['dynPos', pos, n * 3], ['dynColor', color, n * 4], ['dynSize', size, n]]) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers[name]); gl.bufferData(gl.ARRAY_BUFFER, arr.subarray(0, count), gl.DYNAMIC_DRAW);
    }
    this.dynCount = n;
  }

  // -- activation overlay ------------------------------------------------------

  startActivationWatcher() {
    this.activationWatcher = Activation.createWatcher({
      adapter: this.app.vault.adapter,
      getPath: () => this.settings.activationPath,
      onChange: trace => { this.activationTrace = trace; this.syncOverlay(true); },
    });
    this.registerInterval(window.setInterval(() => { this.pollActivation(false); }, POLL_INTERVAL_MS));
    this.pollActivation(true);
  }

  // Dotfolders are not indexed by Obsidian, so the trace file is polled:
  // once a second while the view is visible, and whenever the window gains
  // focus. Each poll is a stat; the file is only read when it changed.
  async pollActivation(force) {
    if (this._closed || !this.model || !this.activationWatcher || this._polling) return;
    if (!this.settings.activationOverlay) { this.setOverlay(null); this.traceMatch = null; return; }
    if (!force && !this.isVisible()) return;
    this._polling = true;
    try { await this.activationWatcher.poll(); } catch (err) { this.logError('activation poll', err); }
    finally { this._polling = false; }
    this.syncOverlay(false);
  }

  traceWanted(now = Date.now()) {
    const t = this.activationTrace;
    return !!t && this.settings.activationOverlay && t.key !== this.dismissedKey &&
      Activation.isFresh(t, now, this.settings.activationWindowMinutes * 60000);
  }

  // Maps the current trace onto the graph and shows it, or hides the
  // overlay. A trace none of whose notes is in this vault, or that lists no
  // notes at all, is not shown: nothing is dimmed and the HUD says why.
  syncOverlay(traceChanged) {
    if (!this.model) return;
    if (!this.traceWanted()) { this.traceMatch = null; if (this.overlay) this.setOverlay(null); return; }
    const t = this.activationTrace, shadow = !!this.settings.showAdvisorShadow;
    const stale = !this.traceMatch || this.traceMatch.key !== t.key || this._overlayRevision !== this.model.viewRevision || this._overlayShadow !== shadow;
    if (!traceChanged && !stale) return;
    const mapped = Activation.mapOverlay(t, path => this.model.lookup(path), { showAdvisorShadow: shadow });
    this.traceMatch = mapped; this._overlayShadow = shadow;
    this._overlayRevision = this.model.viewRevision;
    this.setOverlay(mapped.nodes.size ? mapped : null);
  }

  setOverlay(overlay) {
    const previous = this.overlay;
    if (!overlay && !previous) { this._summaryKey = null; return; }
    if (overlay) {
      overlay.shownAt = previous && previous.key === overlay.key ? previous.shownAt : performance.now();
      this._overlayRevision = this.model ? this.model.viewRevision : 0;
    }
    this.overlay = overlay;
    this._ovDirty = true; this._ovStyleDirty = true; this._edgeStyleDirty = true; this._marksDirty = true; this._nodeListKey = null;
    if (this.model) this.model.buffersDirty = true;
    this._lastHud = -Infinity; this._summaryKey = null;
    this.renderOverlayKey();
  }

  async focusLastRetrieval() {
    await this.pollActivation(true);
    const ov = this.overlay;
    if (!ov || !ov.nodes.size) { new Notice(this.focusNotice()); return false; }
    const framed = Activation.frameNodes(Array.from(ov.nodes.values(), e => ({ pos: e.node.pos, weight: e.activation })));
    if (!framed) return false;
    this.centerTarget = framed.center;
    this.zoomTarget = clamp((framed.radius + 0.15) / FIT_RADIUS, MIN_ZOOM, 1);
    this.setFocusDirection(framed.center);
    if (!this.focusTarget) this.focusTarget = { yaw: this.yaw, pitch: this.pitch };
    this.lastInteraction = performance.now();
    return true;
  }

  // Why "Focus last retrieval" has nothing to focus.
  focusNotice() {
    const status = this.activationStatus();
    if (!this.settings.activationOverlay) return 'The activation overlay is turned off in the plugin settings.';
    if (!status) return 'No retrieval trace found yet.';
    if (status.state === 'cleared') return 'The last retrieval was cleared. A newer retrieval shows again.';
    if (status.state === 'stale') return 'The last retrieval is older than the freshness window (' + this.settings.activationWindowMinutes + ' min).';
    if (status.state === 'unmatched') return 'None of the ' + status.total + ' notes in the last retrieval is in this vault. Check that context-layer indexed this vault folder.';
    if (status.state === 'empty') return 'The last retrieval activated no notes' + (this.activationTrace.packet.status === 'NOT_FOUND' ? ' (no evidence found).' : '.');
    return 'Nothing to focus.';
  }

  clearActivation() {
    if (this.activationTrace) this.dismissedKey = this.activationTrace.key;
    this.traceMatch = null;
    this.setOverlay(null);
    this.centerTarget = [0, 0, 0]; this.zoomTarget = 1; this.focusTarget = null;
    this._lastHud = -Infinity; this.updateActivationHud(Date.now());
  }

  // The HUD always says when the last trace was generated and whether it is
  // shown, has no notes in this vault, lists no notes, is stale or was
  // cleared, so an old or foreign retrieval is never mistaken for the
  // current one.
  activationStatus(now = Date.now()) {
    const t = this.activationTrace;
    if (!t || !this.settings.activationOverlay) return null;
    const at = 'Last retrieval at ' + t.generatedAtText;
    if (t.key === this.dismissedKey) return { state: 'cleared', text: at + ' \u00b7 cleared' };
    if (!Activation.isFresh(t, now, this.settings.activationWindowMinutes * 60000)) {
      return { state: 'stale', text: at + ' \u00b7 stale (older than ' + this.settings.activationWindowMinutes + ' min), not shown' };
    }
    const m = this.traceMatch && this.traceMatch.key === t.key ? this.traceMatch : null;
    if (this.overlay && this.overlay.key === t.key) {
      return { state: 'shown', text: 'Last retrieval ' + Activation.formatHud(t, now, this.overlay), matched: this.overlay.matched, total: this.overlay.total };
    }
    if (m && m.total > 0) {
      return { state: 'unmatched', text: at + ' \u00b7 0 of ' + m.total + ' notes are in this vault (check that context-layer indexed this vault folder)', matched: 0, total: m.total };
    }
    const empty = t.packet.status === 'NOT_FOUND' ? '' : ' \u00b7 no notes to show';
    return { state: 'empty', text: 'Last retrieval ' + Activation.formatHud(t, now) + empty, matched: 0, total: 0 };
  }

  updateActivationHud(now) {
    if (!this.activationEl) return;
    this.syncOverlay(false);
    const status = this.activationStatus(now);
    this.activationEl.toggleClass('nb-hidden', !status);
    this.activationEl.toggleClass('nb-activation-stale', !!status && status.state !== 'shown');
    if (status) this.activationEl.setText(status.text);
    const shown = !!status && status.state === 'shown';
    this.overlayKeyEl.toggleClass('nb-hidden', !shown);
    const advisor = shown ? Activation.formatAdvisor(this.overlay.advisor) : '';
    this.advisorEl.toggleClass('nb-hidden', !advisor);
    this.advisorEl.setText(advisor);
    this.updateSummary(status);
    this.updateAdvisorLayer();
  }

  // The advisor layer: what the optional advisor did in the last retrieval,
  // from the trace only. Hidden until the toggle (or the setting) turns it on;
  // with no advisor data it says so instead of staying silent.
  async toggleAdvisorLayer() {
    this.settings.showAdvisorLayer = !this.settings.showAdvisorLayer;
    if (typeof this.plugin.saveSettings === 'function') await this.plugin.saveSettings();
    this.updateAdvisorLayer();
  }

  advisorLayerSource(now = Date.now()) {
    const t = this.activationTrace;
    if (!this.settings.activationOverlay) return { trace: null, absent: 'disabled' };
    if (!t) return { trace: null, absent: 'none' };
    if (t.key === this.dismissedKey) return { trace: null, absent: 'cleared' };
    if (!Activation.isFresh(t, now, this.settings.activationWindowMinutes * 60000)) return { trace: null, absent: 'stale' };
    return { trace: t, absent: null };
  }

  updateAdvisorLayer() {
    if (!this.advisorPanelEl) return;
    const open = !!this.settings.showAdvisorLayer;
    this.advisorToggleEl.setText(open ? 'Hide advisor layer' : 'Show advisor layer');
    this.advisorToggleEl.setAttr('aria-pressed', open ? 'true' : 'false');
    this.advisorPanelEl.toggleClass('nb-hidden', !open);
    if (!open) { this._advisorKey = null; return; }
    const { trace, absent } = this.advisorLayerSource();
    const key = (trace ? trace.key : 'none:' + absent) + '|' + (this.model ? this.model.viewRevision : 0);
    if (key === this._advisorKey) return;
    this._advisorKey = key;
    const m = Activation.advisorLayerModel(trace, absent), el = this.advisorPanelEl;
    el.empty();
    el.createDiv({ cls: 'nb-advisor-heading', text: 'Advisor layer (read only)' });
    if (m.message) el.createDiv({ cls: 'nb-advisor-message', text: m.message });
    if (m.facts.length) {
      const list = el.createEl('ul', { cls: 'nb-advisor-facts' });
      for (const fact of m.facts) list.createEl('li', { text: fact });
    }
    const section = (title, entries, empty) => {
      el.createDiv({ cls: 'nb-advisor-subheading', text: title });
      if (!entries.length) { el.createDiv({ cls: 'nb-advisor-message', text: empty }); return; }
      const list = el.createEl('ul', { cls: 'nb-advisor-notes' });
      for (const e of entries) {
        const node = this.model ? this.model.lookup(e.path) : null;
        const label = e.path + ' \u00b7 hop ' + e.hop;
        const item = list.createEl('li');
        if (!node) { item.createSpan({ cls: 'nb-advisor-note-missing', text: label + ' \u00b7 not in this vault' }); continue; }
        const button = item.createEl('button', { cls: 'nb-advisor-note', text: label, attr: { type: 'button' } });
        button.onclick = evt => this.openNote(node, evt);
      }
    };
    if (m.state === 'data') {
      if (m.rescued.length || !m.candidates.length) section('Notes the advisor rescued', m.rescued, 'None: the advisor added no note to this packet.');
      if (m.candidates.length) section('Judged on topic, not in the packet', m.candidates, 'None.');
    }
    el.createDiv({ cls: 'nb-advisor-note-text', text: m.note });
  }

  // The key under the HUD line: what the colours and outlines mean.
  renderOverlayKey() {
    const el = this.overlayKeyEl; if (!el) return;
    el.empty();
    const items = [['nb-key-seed', 'seed'], ['nb-key-hop', 'reached by link, in packet'], ['nb-key-reached', 'reached only']];
    const adv = this.overlay && this.overlay.advisor;
    if (adv && adv.marks) {
      const would = adv.applied ? '' : ' nb-key-would';
      items.push(['nb-key-rescued' + would, adv.applied ? 'rescued by advisor' : 'advisor would rescue (shadow)']);
      items.push(['nb-key-flagged' + would, adv.applied ? 'flagged off-topic by advisor' : 'advisor would flag (shadow)']);
    }
    for (const [cls, label] of items) {
      const item = el.createSpan({ cls: 'nb-key-item' });
      item.createSpan({ cls: 'nb-key-dot ' + cls });
      item.createSpan({ text: label });
    }
    if (adv) el.createSpan({ cls: 'nb-key-item nb-key-note', text: Activation.ADVISOR_NOTE });
    const mode = this.activationTrace && this.overlay ? Activation.MODE_NOTES[this.activationTrace.mode] : null;
    if (mode) el.createSpan({ cls: 'nb-key-item nb-key-note', text: mode });
  }

  // Text summary of the last retrieval for keyboard and screen-reader use:
  // the status, then every activated note as a button that opens it. The
  // live region announces changes of state, not the ticking age.
  updateSummary(status) {
    if (!this.summaryEl) return;
    const ov = this.overlay;
    const key = status ? status.state + '|' + this.activationTrace.key + '|' + (ov ? ov.matched + '#' + this._overlayRevision + '#' + !!this._overlayShadow : '') : '';
    if (key === this._summaryKey) return;
    this._summaryKey = key;
    this.summaryEl.toggleClass('nb-hidden', !status);
    this.summaryListEl.empty();
    if (!status) { this.liveEl.setText(''); return; }
    const t = this.activationTrace;
    const lines = [status.text.replace(/ \u00b7 (just now|\d+ (s|min|h) ago)/, '')];
    const advisor = ov ? Activation.formatAdvisor(ov.advisor) : '';
    if (advisor) lines.push(advisor + ' (' + Activation.ADVISOR_NOTE + ')');
    if (ov && Activation.MODE_NOTES[t.mode]) lines.push(Activation.MODE_NOTES[t.mode]);
    this.summaryStatusEl.setText(lines.join('\n'));
    this.liveEl.setText(lines.join('. '));
    if (!ov) return;
    const entries = Array.from(ov.nodes.values()).sort((a, b) => a.hop - b.hop || b.activation - a.activation || (a.path < b.path ? -1 : 1));
    for (const e of entries) {
      const parts = ['hop ' + e.hop, e.role === 'seed' ? 'seed' : 'reached by link', e.selected ? 'in packet' : 'reached only'];
      if (e.verdict) parts.push((ov.advisor.applied ? 'advisor: ' : 'advisor would: ') + e.verdict.replace('_', ' '));
      const button = this.summaryListEl.createEl('li').createEl('button', { cls: 'nb-summary-note', text: parts.join(' \u00b7 ') + ' \u00b7 ' + e.path, attr: { type: 'button' } });
      button.onclick = evt => this.openNote(e.node, evt);
    }
  }

  openNote(node, evt) {
    if (!node || node.removed || !node.file) return;
    const Keymap = obsidian.Keymap;
    const type = evt && Keymap && typeof Keymap.isModEvent === 'function' ? Keymap.isModEvent(evt) : false;
    // Plain clicks open a new tab (so the view stays); modifier clicks follow
    // Obsidian's split and window conventions.
    this.app.workspace.getLeaf(type || 'tab').openFile(node.file);
  }

  // -- interaction --------------------------------------------------------------

  registerInteractionEvents() {
    const canvas = this.canvas;
    this.registerDomEvent(canvas, 'pointerdown', e => {
      if (e.button !== 0) return;
      try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* not supported */ }
      Object.assign(this.drag, { dragging: true, pointerId: e.pointerId, lastX: e.clientX, lastY: e.clientY, velYaw: 0, velPitch: 0, moved: 0 });
      this.lastInteraction = performance.now();
    });
    this.registerDomEvent(canvas, 'pointermove', e => {
      if (this.drag.dragging) {
        const dx = e.clientX - this.drag.lastX, dy = e.clientY - this.drag.lastY, k = 0.006;
        this.drag.moved += Math.abs(dx) + Math.abs(dy);
        this.yaw += dx * k; this.pitch = clamp(this.pitch - dy * k, -1.3, 1.3);
        this.drag.velYaw = dx * k; this.drag.velPitch = -dy * k;
        this.drag.lastX = e.clientX; this.drag.lastY = e.clientY;
        this.lastInteraction = performance.now(); this.focusTarget = null;
      } else {
        // Pick at most once per animation frame.
        this._pendingHoverClient = { x: e.clientX, y: e.clientY };
        if (!this._hoverRafId) {
          this._hoverRafWin = this.win;
          this._hoverRafId = this.requestFrame(() => {
            this._hoverRafId = null;
            if (this._pendingHoverClient) this.handleHover(this._pendingHoverClient);
          });
        }
      }
    });
    const endDrag = e => {
      if (!this.drag.dragging) return;
      this.drag.dragging = false;
      try { canvas.releasePointerCapture(e.pointerId); } catch (_) { /* not supported */ }
      this.lastInteraction = performance.now();
    };
    this.registerDomEvent(canvas, 'pointerup', endDrag);
    this.registerDomEvent(canvas, 'pointercancel', endDrag);
    this.registerDomEvent(canvas, 'pointerleave', () => {
      this._pendingHoverClient = null;
      if (this.hoverNode) { this.hoverNode = null; this._hlDirty = true; if (this.model) this.model.buffersDirty = true; }
      this.hideTooltip();
    });
    this.registerDomEvent(canvas, 'wheel', e => {
      e.preventDefault();
      this.zoom = clamp(this.zoom + e.deltaY * 0.001, MIN_ZOOM, MAX_ZOOM); this.zoomTarget = null;
      this.lastInteraction = performance.now();
    }, { passive: false });
    this.registerDomEvent(canvas, 'click', e => {
      this.handleHover({ x: e.clientX, y: e.clientY });
      if (this.drag.moved > 6) return;
      if (this.hoverNode) this.openNote(this.hoverNode, e);
      else if (this.focusRegionKey) this.focusRegion(null);
    });
    this.registerDomEvent(canvas, 'keydown', e => { if (this.handleKey(e.key)) e.preventDefault(); });
  }

  // Keyboard control of the focused canvas. Returns true if the key was used.
  handleKey(key) {
    const turn = { ArrowLeft: [-KEY_ROTATE, 0], ArrowRight: [KEY_ROTATE, 0], ArrowUp: [0, KEY_ROTATE], ArrowDown: [0, -KEY_ROTATE] }[key];
    if (turn) {
      this.yaw += turn[0]; this.pitch = clamp(this.pitch + turn[1], -1.3, 1.3);
      this.focusTarget = null; this.drag.velYaw = 0; this.drag.velPitch = 0;
    } else if (key === '+' || key === '=') { this.zoom = clamp(this.zoom - KEY_ZOOM, MIN_ZOOM, MAX_ZOOM); this.zoomTarget = null; }
    else if (key === '-' || key === '_') { this.zoom = clamp(this.zoom + KEY_ZOOM, MIN_ZOOM, MAX_ZOOM); this.zoomTarget = null; }
    else if (key === 'Home') { this.centerTarget = [0, 0, 0]; this.zoomTarget = 1; this.focusTarget = null; }
    else if (key === 'Escape' && this.focusRegionKey) this.focusRegion(null);
    else return false;
    this.lastInteraction = performance.now();
    return true;
  }

  // Finds the drawn note nearest to the pointer (within 12 px).
  handleHover(client) {
    if (!this.model || !this._renderNodes || !this.viewMat || !this.canvas) return;
    const rect = this.canvas.getBoundingClientRect(); if (!rect.width || !rect.height) return;
    const mx = client.x - rect.left, my = client.y - rect.top, v = this.viewMat, p = this.projMat;
    const world = this._pickWorld || (this._pickWorld = [0, 0, 0]);
    let nearest = null, best = 12, sx = 0, sy = 0;
    for (const node of this._renderNodes) {
      if (node.removed || this.model.nodes.get(node.path) !== node) continue;
      Field.displace(node.pos[0], node.pos[1], node.pos[2], this._motionTime || 0, world);
      const x = world[0], y = world[1], z = world[2];
      const vx = v[0] * x + v[4] * y + v[8] * z + v[12], vy = v[1] * x + v[5] * y + v[9] * z + v[13], vz = v[2] * x + v[6] * y + v[10] * z + v[14];
      if (vz > -0.05) continue;
      const w = p[3] * vx + p[7] * vy + p[11] * vz + p[15]; if (w <= 0) continue;
      const px = ((p[0] * vx + p[4] * vy + p[8] * vz + p[12]) / w * 0.5 + 0.5) * rect.width;
      const py = (0.5 - (p[1] * vx + p[5] * vy + p[9] * vz + p[13]) / w * 0.5) * rect.height;
      const d = Math.hypot(px - mx, py - my);
      if (d < best) { best = d; nearest = node; sx = px; sy = py; }
    }
    const prev = this.hoverNode; this.hoverNode = nearest;
    if (prev !== nearest) { this.model.buffersDirty = true; this._hlDirty = true; }
    if (!nearest) { this.hideTooltip(); return; }
    if (prev !== nearest || this._tooltipRevision !== this.model.viewRevision + ':' + (this.overlay ? this.overlay.key : '')) {
      this.tooltipEl.setText(this.tooltipText(nearest));
      this._tooltipRevision = this.model.viewRevision + ':' + (this.overlay ? this.overlay.key : '');
    }
    this.tooltipEl.removeClass('nb-hidden');
    setCssVars(this.tooltipEl, {
      '--nb-tooltip-x': Math.min(sx + 14, Math.max(10, rect.width - 430)) + 'px',
      '--nb-tooltip-y': Math.min(sy + 10, Math.max(10, rect.height - 220)) + 'px',
    });
  }

  tooltipText(node) {
    const neighbours = Array.from(this.model.adjacency.get(node.path) || []);
    const lines = [node.title, node.path, neighbours.length + (neighbours.length === 1 ? ' link' : ' links')];
    if (neighbours.length) lines.push(neighbours.slice(0, 8).map(p => this.model.nodes.get(p)?.title || p).join(' \u00b7 ') + (neighbours.length > 8 ? ' \u2026' : ''));
    const entry = this.overlay?.nodes.get(node.path);
    if (entry) {
      lines.push('last retrieval: ' + entry.role + ', hop ' + entry.hop + ', activation score ' + entry.activation.toFixed(2) + (entry.selected ? ', in packet' : ', reached only'));
      if (entry.verdict) lines.push((this.overlay.advisor.applied ? 'advisor: ' : 'advisor would (shadow): ') + entry.verdict.replace('_', ' ') + ' (' + Activation.ADVISOR_NOTE + ')');
    }
    return lines.join('\n');
  }

  hideTooltip() { if (this.tooltipEl) this.tooltipEl.addClass('nb-hidden'); }

  // The canvas fills its container through CSS; only its pixel buffer is
  // sized here, at the device pixel ratio of the view's own window.
  resizeCanvas() {
    if (!this.canvas) return;
    const rect = this.contentEl.getBoundingClientRect();
    const win = this.win;
    const dpr = Math.min((win && win.devicePixelRatio) || 1, 3);
    const w = Math.max(1, Math.floor(rect.width * dpr)), h = Math.max(1, Math.floor(rect.height * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; }
    this.dpr = dpr;
  }

  // -- camera and visibility ------------------------------------------------------

  updateCamera(dt) {
    const now = performance.now(), rm = this.reducedMotion, ease = (a, b) => rm ? b : lerp(a, b, 0.08);
    if (this.focusTarget) { this.yaw = ease(this.yaw, this.focusTarget.yaw); this.pitch = ease(this.pitch, this.focusTarget.pitch); }
    else if (!this.drag.dragging) {
      if (rm) { this.drag.velYaw = 0; this.drag.velPitch = 0; }
      this.yaw += this.drag.velYaw; this.pitch = clamp(this.pitch + this.drag.velPitch, -1.3, 1.3);
      this.drag.velYaw *= 0.88; this.drag.velPitch *= 0.88;
      if (!rm && this.settings.autoRotate !== false && now - this.lastInteraction > 3500) this.yaw += dt * Math.PI / 55;
    }
    if (this.centerTarget) {
      for (let c = 0; c < 3; c++) this.center[c] = ease(this.center[c], this.centerTarget[c]);
      if (Math.hypot(this.center[0] - this.centerTarget[0], this.center[1] - this.centerTarget[1], this.center[2] - this.centerTarget[2]) < 1e-4) this.centerTarget = null;
    }
    if (this.zoomTarget !== null) {
      this.zoom = ease(this.zoom, this.zoomTarget);
      if (Math.abs(this.zoom - this.zoomTarget) < 1e-4) this.zoomTarget = null;
    }
    const aspect = this.canvas.width / Math.max(1, this.canvas.height);
    const tanShort = Math.tan(25 * Math.PI / 180) * Math.min(1, aspect);
    this.fitDistance = FIT_RADIUS / Math.sin(Math.atan(tanShort * 0.88));
    this.distance = this.fitDistance * this.zoom;
    const c = this.center, d = this.distance;
    const eye = [c[0] + d * Math.sin(this.yaw) * Math.cos(this.pitch), c[1] + d * Math.sin(this.pitch), c[2] + d * Math.cos(this.yaw) * Math.cos(this.pitch)];
    this.eyePos = eye;
    mat4LookAt(this.viewMat, eye, c, [0, 1, 0]);
    mat4Perspective(this.projMat, 50 * Math.PI / 180, aspect, 0.05, 20);
  }

  // Rendering (and the layout worker) run only while the canvas is actually
  // on screen: not in a background tab, not in a hidden window.
  isVisible() {
    const doc = this.doc;
    return this._renderReady === true && !this._closed && !this._contextLost && !(doc && doc.hidden) &&
      !!this.canvas && this.canvas.clientWidth > 0 && this.canvas.clientHeight > 0;
  }

  handleVisibility() {
    const visible = this.isVisible();
    this.model?.setPaused(!visible);
    this._degreePalette?.setVisible(visible, performance.now());
    if (!visible) {
      if (this.rafId) this.cancelFrame(this.rafId);
      this.rafId = null;
      this.metrics?.frame(performance.now(), 0, { visible: false });
    } else if (!this.rafId && this.gl) {
      this.lastFrameTime = performance.now();
      // Vault changes that arrived while hidden are applied once, now.
      if (this._refreshDirty) this.runRefresh();
      this.rafId = this.requestFrame(this._renderBound);
      this.pollActivation(true);
    }
  }

  // -- frame ----------------------------------------------------------------------

  render(now) {
    this.rafId = null;
    if (!this.gl || !this.isVisible()) { this.handleVisibility(); return; }
    const begin = performance.now(), dt = Math.max(0, Math.min((now - this.lastFrameTime) / 1000, 0.05));
    this.lastFrameTime = now;
    try {
      if (!this.reducedMotion) this._motionTime = ((this._motionTime || 0) + dt) % 600;
      this.refreshRegions();
      if (this.traceMatch && this._overlayRevision !== this.model.viewRevision) this.syncOverlay(false);
      this.updateCamera(dt);
      this.updateAmbientAndSignals(now);
      const moved = this.model.advance(dt, this.reducedMotion);
      if (moved) { this._ovDirty = true; this._marksDirty = true; }
      if (this.model.buffersDirty) this.rebuildNodeBuffers();
      if (this._marksDirty) this.rebuildMarkBuffers();
      this.updateEdges(now, moved);
      // Re-pick a stationary pointer: the field and camera move underneath it.
      if (this._pendingHoverClient && !this.drag.dragging && now - (this._lastPick || 0) > 32) { this._lastPick = now; this.handleHover(this._pendingHoverClient); }
      this.rebuildDynamicBuffer(now);
      this.drawScene();
      this.updateHud(now);
    } catch (err) { this.logError('render', err); }
    this.metrics?.frame(now, performance.now() - begin, { visible: true });
    if (!this.rafId && this.isVisible()) this.rafId = this.requestFrame(this._renderBound);
  }

  drawScene() {
    const gl = this.gl;
    const bloomK = this.settings.bloom ? clamp(this.settings.bloomStrength, 0, 1.5) : 0;
    const useFbo = bloomK > 0.001 && this.bloomOk && this.ensureBloomTargets();
    gl.bindFramebuffer(gl.FRAMEBUFFER, useFbo ? this.fboFull.fbo : null);
    gl.viewport(0, 0, useFbo ? this.fboFull.w : this.canvas.width, useFbo ? this.fboFull.h : this.canvas.height);
    this.renderCoreScene();
    if (!useFbo) return;
    // Downsample, blur horizontally and vertically, then composite + tone map.
    gl.disable(gl.BLEND);
    this.drawQuad(this.fboHalf, this.fboFull.tex, [0, 0], 1.0);
    this.drawQuad(this.fboBlurA, this.fboHalf.tex, [1 / this.fboHalf.w, 0], 1.0);
    this.drawQuad(this.fboBlurB, this.fboBlurA.tex, [0, 1 / this.fboHalf.h], 1.0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.disable(gl.BLEND); gl.disable(gl.DEPTH_TEST);
    gl.useProgram(this.compProgram);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.fboFull.tex); gl.uniform1i(this.compUniforms.scene, 0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, this.fboBlurB.tex); gl.uniform1i(this.compUniforms.bloom, 1);
    gl.uniform1f(this.compUniforms.bloomK, bloomK);
    gl.uniform1f(this.compUniforms.exposure, this.hdr ? TONE_EXPOSURE_HDR : TONE_EXPOSURE_LDR);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
    gl.enableVertexAttribArray(this.compAttribs.pos);
    gl.vertexAttribPointer(this.compAttribs.pos, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.activeTexture(gl.TEXTURE0);
  }

  // Draws notes, links, overlay links, advisor marks and transient points
  // into the bound framebuffer (screen or bloom source).
  renderCoreScene() {
    const gl = this.gl, t = this._motionTime || 0, eye = this.eyePos || [0, 0, 2.5];
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.depthMask(false);

    gl.useProgram(this.pointProgram);
    gl.uniformMatrix4fv(this.pointUniforms.projection, false, this.projMat);
    gl.uniformMatrix4fv(this.pointUniforms.view, false, this.viewMat);
    gl.uniform1f(this.pointUniforms.time, t);
    gl.uniform3f(this.pointUniforms.eyePos, eye[0], eye[1], eye[2]);
    gl.uniform2f(this.pointUniforms.viewport, this.canvas.width, this.canvas.height);
    gl.uniform1f(this.pointUniforms.alphaMult, 1.0);
    if (this.nodeCount) this.drawPoints(this.buffers.nodePos, this.buffers.nodeColor, this.buffers.nodeSize, this.buffers.nodeShell, this.nodeCount);

    if (this.instExt) {
      gl.useProgram(this.lineProgram);
      gl.uniformMatrix4fv(this.lineUniforms.projection, false, this.projMat);
      gl.uniformMatrix4fv(this.lineUniforms.view, false, this.viewMat);
      gl.uniform2f(this.lineUniforms.viewport, this.canvas.width, this.canvas.height);
      gl.uniform1f(this.lineUniforms.dpr, this.dpr || 1);
      gl.uniform1f(this.lineUniforms.time, t);
      const width = this.settings.edgeWidth || 1;
      if (this.edgeCount) this.drawEdgeInstances(this.buffers.edgeCtrl, this.buffers.edgeStyle, this.edgeCount, width);
      if (this.hlCount) this.drawEdgeInstances(this.buffers.hlCtrl, this.buffers.hlStyle, this.hlCount, width);
      if (this.ovCount) this.drawEdgeInstances(this.buffers.ovCtrl, this.buffers.ovStyle, this.ovCount, Math.max(1, width));
    }

    if (this.markCount || this.dynCount) gl.useProgram(this.pointProgram);
    if (this.markCount) this.drawPoints(this.buffers.markPos, this.buffers.markColor, this.buffers.markSize, null, this.markCount, this.buffers.markRing);
    if (this.dynCount) this.drawPoints(this.buffers.dynPos, this.buffers.dynColor, this.buffers.dynSize, null, this.dynCount);
    gl.depthMask(true);
    gl.disable(gl.BLEND);
  }

  drawPoints(posBuf, colorBuf, sizeBuf, shellBuf, count, ringBuf = null) {
    const gl = this.gl, a = this.pointAttribs;
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf); gl.enableVertexAttribArray(a.position); gl.vertexAttribPointer(a.position, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, colorBuf); gl.enableVertexAttribArray(a.color); gl.vertexAttribPointer(a.color, 4, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, sizeBuf); gl.enableVertexAttribArray(a.size); gl.vertexAttribPointer(a.size, 1, gl.FLOAT, false, 0, 0);
    for (const [loc, buf] of [[a.shell, shellBuf], [a.ring, ringBuf]]) {
      if (loc < 0) continue;
      if (buf) { gl.bindBuffer(gl.ARRAY_BUFFER, buf); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 1, gl.FLOAT, false, 0, 0); }
      else { gl.disableVertexAttribArray(loc); gl.vertexAttrib1f(loc, 0); }
    }
    gl.drawArrays(gl.POINTS, 0, count);
  }

  // One instanced draw of cubic ribbons. Instance divisors are global state in
  // WebGL1, so they are reset before returning (the point program reuses the
  // same attribute locations).
  drawEdgeInstances(ctrlBuf, styleBuf, count, widthMult) {
    const gl = this.gl, ext = this.instExt, la = this.lineAttribs;
    gl.uniform1f(this.lineUniforms.widthMult, widthMult);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.edgeTemplate);
    gl.enableVertexAttribArray(la.ts);
    gl.vertexAttribPointer(la.ts, 2, gl.FLOAT, false, 0, 0);
    ext.vertexAttribDivisorANGLE(la.ts, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, ctrlBuf);
    const controls = [la.p0, la.p1, la.p2, la.p3];
    for (let k = 0; k < 4; k++) {
      gl.enableVertexAttribArray(controls[k]);
      gl.vertexAttribPointer(controls[k], 3, gl.FLOAT, false, 48, k * 12);
      ext.vertexAttribDivisorANGLE(controls[k], 1);
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, styleBuf);
    gl.enableVertexAttribArray(la.colA); gl.vertexAttribPointer(la.colA, 4, gl.FLOAT, false, 32, 0); ext.vertexAttribDivisorANGLE(la.colA, 1);
    gl.enableVertexAttribArray(la.colB); gl.vertexAttribPointer(la.colB, 4, gl.FLOAT, false, 32, 16); ext.vertexAttribDivisorANGLE(la.colB, 1);
    ext.drawArraysInstancedANGLE(gl.TRIANGLE_STRIP, 0, 2 * (EDGE_SEGMENTS + 1), count);
    for (const loc of [la.p0, la.p1, la.p2, la.p3, la.colA, la.colB]) { ext.vertexAttribDivisorANGLE(loc, 0); gl.disableVertexAttribArray(loc); }
  }

  updateHud(now) {
    if (now - this._lastHud < HUD_INTERVAL_MS || !this.model) return;
    this._lastHud = now;
    const m = this.model;
    this.countersEl.setText(m.nodes.size + ' notes \u00b7 ' + m.edgeMap.size + ' links \u00b7 ' + (m.orphanCount || 0) + ' unlinked' +
      (this.instExt ? '' : ' \u00b7 links not drawn (no WebGL instancing)'));
    this.syncOverlay(false);
    this.updateActivationHud(Date.now());
    if (this.settings.developerDiagnostics) {
      const s = this.metrics ? this.metrics.summary() : null;
      const fps = s && s.fps ? s.fps.toFixed(0) : '-', p95 = s && s.frameMs.p95 !== null ? s.frameMs.p95.toFixed(1) : '-';
      this.devEl.setText('fps ' + fps + ' \u00b7 p95 ' + p95 + ' ms \u00b7 layout ' + m.workerState + (this.errorLog.length ? ' \u00b7 errors ' + this.errorLog.length : ''));
    }
  }
}

// Sets CSS custom properties (Obsidian's setCssProps when present).
function setCssVars(el, props) {
  if (typeof el.setCssProps === 'function') { el.setCssProps(props); return; }
  if (el.style && typeof el.style.setProperty === 'function') for (const [k, v] of Object.entries(props)) el.style.setProperty(k, v);
}

// Synchronous program creation for the small, optional bloom shaders.
function createProgramSync(gl, vsSource, fsSource) {
  const compile = (type, src) => {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src); gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) { const log = gl.getShaderInfoLog(sh); gl.deleteShader(sh); throw new Error('shader compile error: ' + log); }
    return sh;
  };
  const vs = compile(gl.VERTEX_SHADER, vsSource), fs = compile(gl.FRAGMENT_SHADER, fsSource);
  const program = gl.createProgram();
  gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program);
  gl.deleteShader(vs); gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { const log = gl.getProgramInfoLog(program); gl.deleteProgram(program); throw new Error('program link error: ' + log); }
  return program;
}

module.exports = { BrainView, VIEW_TYPE };
