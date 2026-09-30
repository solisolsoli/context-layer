'use strict';

// Fixed degree anchors avoid vault-wide normalization: a note keeps the same
// visual meaning when unrelated notes are added to the graph.
const ANCHORS = Object.freeze([
  [0, '#46516D', .009], [1, '#8278ED', .009], [4, '#678FFE', .0108],
  [16, '#4EBBFA', .0126], [64, '#68DBF5', .0144], [256, '#ACEEFF', .0162],
  [1024, '#E3F9FF', .018],
].map(([degree, hex, size]) => Object.freeze({
  degree, x: Math.log2(1 + degree), hex, size,
  rgb: Object.freeze([1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)),
})));
const DURATION_MS = 650;

function styleForDegree(degree) {
  const d = Math.max(0, Number.isFinite(degree) ? degree : 0);
  const x = Math.log2(1 + Math.min(d, 1024));
  let hi = 1;
  while (hi < ANCHORS.length - 1 && x > ANCHORS[hi].x) hi++;
  const a = ANCHORS[hi - 1], b = ANCHORS[hi];
  const t = Math.max(0, Math.min(1, (x - a.x) / (b.x - a.x)));
  return {
    color: [a.rgb[0] + (b.rgb[0] - a.rgb[0]) * t,
      a.rgb[1] + (b.rgb[1] - a.rgb[1]) * t,
      a.rgb[2] + (b.rgb[2] - a.rgb[2]) * t],
    size: a.size + (b.size - a.size) * t,
  };
}

function createAnimator() {
  const stateByNode = new WeakMap();
  const active = [];
  const changedBuffer = [];
  let liveNodes = new Set();
  let visible = true, hiddenAt = 0;

  function seedNode(node) {
    if (stateByNode.has(node)) return;
    const style = styleForDegree(node.degree);
    stateByNode.set(node, { degree: node.degree, color: style.color.slice(), size: style.size,
      fromColor: style.color.slice(), toColor: style.color.slice(), fromSize: style.size,
      toSize: style.size, startedAt: null, active: false });
  }

  function finish(state, style) {
    state.color[0] = style.color[0]; state.color[1] = style.color[1]; state.color[2] = style.color[2];
    state.size = style.size; state.active = false; state.startedAt = null;
  }
  function advanceState(state, now) {
    if (!state.active || state.startedAt === null) return false;
    const raw = Math.max(0, Math.min(1, (now - state.startedAt) / DURATION_MS));
    const t = raw * raw * (3 - 2 * raw); // smoothstep, including interrupted changes
    state.color[0] = state.fromColor[0] + (state.toColor[0] - state.fromColor[0]) * t;
    state.color[1] = state.fromColor[1] + (state.toColor[1] - state.fromColor[1]) * t;
    state.color[2] = state.fromColor[2] + (state.toColor[2] - state.fromColor[2]) * t;
    state.size = state.fromSize + (state.toSize - state.fromSize) * t;
    if (raw >= 1) state.active = false;
    return true;
  }
  function setVisible(next, now) {
    next = !!next;
    if (next === visible) return;
    if (!next) { visible = false; hiddenAt = now; return; }
    const paused = Math.max(0, now - hiddenAt);
    for (let i = 0; i < active.length; i++) {
      const s = stateByNode.get(active[i]);
      if (s?.active) {
        if (s.startedAt !== null) s.startedAt += paused;
        else s.startedAt = now; // A transition requested while hidden starts on resume.
      }
    }
    visible = true;
  }
  function refresh(nodes, now, options = {}) {
    setVisible(options.visible !== false, now);
    const reducedMotion = !!options.reducedMotion;
    liveNodes = new Set(nodes);
    let out = 0;
    for (let i = 0; i < active.length; i++) {
      const node = active[i], s = stateByNode.get(node);
      if (liveNodes.has(node) && s?.active) active[out++] = node;
    }
    active.length = out;
    for (const node of nodes) {
      let s = stateByNode.get(node);
      if (!s) {
        seedNode(node);
        continue; // New nodes snap into their initial degree state.
      }
      if (s.degree === node.degree) continue;
      const target = styleForDegree(node.degree);
      const wasActive = s.active;
      if (s.active && visible) advanceState(s, now);
      s.degree = node.degree;
      s.fromColor = s.color.slice(); s.fromSize = s.size;
      s.toColor = target.color; s.toSize = target.size;
      if (reducedMotion) { finish(s, target); continue; }
      s.active = true; s.startedAt = visible ? now : null;
      if (!wasActive) active.push(node);
    }
    out = 0;
    for (let i = 0; i < active.length; i++) {
      const node = active[i], s = stateByNode.get(node);
      if (liveNodes.has(node) && s?.active) active[out++] = node;
    }
    active.length = out;
    return active.length;
  }
  async function seedInitialAsync(nodes, options = {}) {
    const cancelled = options.cancelled || (() => false);
    const onChunk = options.onChunk || (() => {});
    const sliceMs = Math.max(1, options.sliceMs || 5);
    let chunk = performance.now();
    for (let i = 0; i < nodes.length; i++) {
      if (cancelled()) return false;
      seedNode(nodes[i]);
      if (performance.now() - chunk >= sliceMs) {
        onChunk(chunk, 'paletteSeed');
        await new Promise(resolve => setTimeout(resolve, 0));
        if (cancelled()) return false;
        chunk = performance.now();
      }
    }
    if (performance.now() - chunk > 0) onChunk(chunk, 'paletteSeed');
    return !cancelled();
  }
  function tick(now, options = {}) {
    setVisible(options.visible !== false, now);
    const changed = changedBuffer; changed.length = 0;
    if (!visible) return changed;
    let out = 0;
    for (let i = 0; i < active.length; i++) {
      const node = active[i];
      if (!liveNodes.has(node)) continue;
      const s = stateByNode.get(node);
      if (!s?.active) continue;
      if (options.reducedMotion) finish(s, { color: s.toColor, size: s.toSize });
      else advanceState(s, now);
      changed.push(node);
      if (s.active) active[out++] = node;
    }
    active.length = out;
    return changed;
  }
  function get(node) { return stateByNode.get(node); }
  function summary(nodes) {
    const distribution = { '0': 0, '1': 0, '2-3': 0, '4-15': 0, '16-63': 0, '64-255': 0, '256-1023': 0, '1024+': 0 };
    for (const n of nodes) {
      const d = n.degree;
      if (d === 0) distribution['0']++;
      else if (d === 1) distribution['1']++;
      else if (d < 4) distribution['2-3']++;
      else if (d < 16) distribution['4-15']++;
      else if (d < 64) distribution['16-63']++;
      else if (d < 256) distribution['64-255']++;
      else if (d < 1024) distribution['256-1023']++;
      else distribution['1024+']++;
    }
    return { anchors: ANCHORS.map(({ degree, hex, size }) => ({ degree, hex, size })), distribution, activeTransitions: active.length };
  }
  return { refresh, seedInitialAsync, tick, get, setVisible, summary, activeCount: () => active.length, durationMs: DURATION_MS };
}

module.exports = { ANCHORS, DURATION_MS, styleForDegree, createAnimator };
