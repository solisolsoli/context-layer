'use strict';

// Reads the activation trace that `context-layer` writes after a synaptic
// retrieval (<vault>/.context/activation.json, format version 1) and turns it
// into display state for the overlay. Everything here is defensive: a
// missing, partial, oversized, stale or malformed file is ignored without
// throwing. Unknown fields added to version 1 later are ignored. This
// module never writes files and never touches the network.

const DEFAULT_PATH = '.context/activation.json';
const MAX_BYTES = 512 * 1024;
const MAX_NODES = 200;
const MAX_EDGES = 400;
const MAX_HOP = 64;
const CLOCK_SKEW_MS = 60 * 1000;
const EDGE_KINDS = Object.freeze(['wikilink', 'embed', 'mdlink', 'frontmatter', 'backlink']);
const ISO_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})$/;
// Trace file names the plugin agrees to read: `activation*.json` directly
// inside a `.context` folder. Nothing else under `.context/` is ever opened.
const TRACE_FILE = /^(?:[^/]+\/)*\.context\/activation[A-Za-z0-9._-]*\.json$/;

// Packet modes of synaptic retrieval (docs/synapse.md): `superset` is the fts
// packet plus graph extras; `compact` can leave out fts evidence.
const MODES = Object.freeze(['superset', 'compact']);
const MODE_NOTES = Object.freeze({ compact: 'compact packet: it can leave out evidence the fts packet would carry' });
// Packet status names (docs/cli.md). PARTIAL means some evidence was found,
// which the passage count already says; OK is written by older traces.
const STATUS_TEXT = Object.freeze({ PARTIAL: '', OK: '', NOT_FOUND: 'no evidence found', ERROR: 'retrieval error' });

// Overlay look. Seeds (full-text matches) are warm amber; notes reached by
// following links are cool cyan that whitens with the activation score.
// Notes that were reached but did not contribute a passage to the packet
// ("reached only") are drawn in a muted grey-blue so they never read as
// evidence.
const SEED_RGB = Object.freeze([1.0, 0.76, 0.34]);
const HOP_RGB_LOW = Object.freeze([0.30, 0.78, 1.0]);
const HOP_RGB_HIGH = Object.freeze([0.90, 0.98, 1.0]);
const REACHED_RGB = Object.freeze([0.52, 0.58, 0.68]);
const REACHED_MIX = 0.6;
// Opacity factor for every note outside an active retrieval, so the notes of
// the retrieval stand out against the degree palette.
const OVERLAY_NODE_DIM = 0.22;
// Pulse timing: each hop level starts PULSE_STEP_MS after the previous one;
// a pulse needs PULSE_TRAVEL_MS to cross its link; the cycle then rests.
const PULSE_STEP_MS = 700;
const PULSE_TRAVEL_MS = 900;
const PULSE_REST_MS = 1400;

function isPlainObject(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }
function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
function compareStrings(a, b) { return a < b ? -1 : (a > b ? 1 : 0); }
function finiteNumber(v) { return typeof v === 'number' && Number.isFinite(v) ? v : null; }
function nonNegativeInt(v) { return Number.isInteger(v) && v >= 0 ? v : null; }
function nfc(s) { return typeof s.normalize === 'function' ? s.normalize('NFC') : s; }

// A vault-relative POSIX path without '.', '..' or empty segments, in
// Unicode form NFC so composed and decomposed spellings compare equal.
function sanitizeVaultPath(p) {
  if (typeof p !== 'string') return null;
  const t = p.trim().replace(/^\.\//, '');
  if (!t || t.length > 1024 || t.startsWith('/') || t.includes('\\') || t.includes('\u0000') || /^[a-zA-Z]:/.test(t)) return null;
  const segments = t.split('/');
  if (segments.some(s => s === '' || s === '.' || s === '..')) return null;
  return nfc(segments.join('/'));
}

// The Activation file setting: a sanitized path to `.context/activation*.json`.
function sanitizeTracePath(p) {
  const clean = sanitizeVaultPath(p);
  return clean && TRACE_FILE.test(clean) ? clean : null;
}

function parseTimestamp(value) {
  if (typeof value !== 'string' || !ISO_UTC.test(value)) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

// Validates an already-parsed JSON value. Returns a normalized trace or null.
// The optional `query` and `query_sha256` fields are deliberately not copied:
// the view never displays or compares query text, even when the user opted
// in to recording it. A trace is identified by `generated_at` plus the
// optional random `run_id`.
function normalizeTrace(raw) {
  if (!isPlainObject(raw) || raw.version !== 1) return null;
  const generatedAt = parseTimestamp(raw.generated_at);
  if (generatedAt === null || !Array.isArray(raw.nodes)) return null;

  const nodes = [], seen = new Set();
  for (const n of raw.nodes) {
    if (!isPlainObject(n)) continue;
    const path = sanitizeVaultPath(n.path);
    const activation = finiteNumber(n.activation);
    const hop = Number.isInteger(n.hop) && n.hop >= 0 && n.hop <= MAX_HOP ? n.hop : null;
    if (!path || seen.has(path) || activation === null || hop === null) continue;
    const role = n.role === 'seed' || n.role === 'hop' ? n.role : (hop === 0 ? 'seed' : 'hop');
    nodes.push({ path, activation: clamp01(activation), hop, role, selected: n.selected === true });
    seen.add(path);
  }
  nodes.sort((a, b) => b.activation - a.activation || a.hop - b.hop || compareStrings(a.path, b.path));
  if (nodes.length > MAX_NODES) nodes.length = MAX_NODES;

  const edges = [], edgeSeen = new Set();
  for (const e of Array.isArray(raw.edges) ? raw.edges : []) {
    if (!isPlainObject(e)) continue;
    const from = sanitizeVaultPath(e.from), to = sanitizeVaultPath(e.to);
    if (!from || !to || from === to) continue;
    // Only explicit vault links are drawn; anything else is dropped.
    if (!EDGE_KINDS.includes(e.kind)) continue;
    const kind = e.kind;
    const key = from + '\u0000' + to + '\u0000' + kind;
    if (edgeSeen.has(key)) continue;
    edgeSeen.add(key);
    const weight = finiteNumber(e.weight);
    // The traversal step that used the link: `depth`, or `hop`, the name the
    // writer gives it (docs/synapse.md). Optional; without it the depth is
    // derived from the `from` note's hop in mapOverlay.
    const step = Number.isInteger(e.depth) ? e.depth : e.hop;
    const depth = Number.isInteger(step) && step >= 1 && step <= MAX_HOP ? step : null;
    edges.push({ from, to, kind, weight: weight === null ? 0 : clamp01(weight), depth });
  }
  edges.sort((a, b) => b.weight - a.weight || compareStrings(a.from, b.from) || compareStrings(a.to, b.to));
  if (edges.length > MAX_EDGES) edges.length = MAX_EDGES;

  const packet = isPlainObject(raw.packet) ? raw.packet : {};
  const runId = typeof raw.run_id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(raw.run_id) ? raw.run_id : null;
  const status = typeof packet.status === 'string' && /^[A-Za-z_]{1,32}$/.test(packet.status) ? packet.status : null;
  return {
    version: 1,
    generatedAt,
    generatedAtText: raw.generated_at,
    runId,
    key: raw.generated_at + '|' + (runId || ''),
    method: typeof raw.method === 'string' && /^[A-Za-z0-9_-]{1,32}$/.test(raw.method) ? raw.method : 'unknown',
    mode: MODES.includes(raw.mode) ? raw.mode : null,
    budgetTokens: nonNegativeInt(raw.budget_tokens),
    nodes,
    edges,
    packet: { passages: nonNegativeInt(packet.passages), estTokens: nonNegativeInt(packet.est_tokens), status },
  };
}

// Parses the file text. Oversized, truncated or invalid input returns null.
function parseActivation(text, options = {}) {
  const maxBytes = options.maxBytes || MAX_BYTES;
  if (typeof text !== 'string' || !text.length || text.length > maxBytes) return null;
  let raw;
  try { raw = JSON.parse(text); } catch (_) { return null; }
  return normalizeTrace(raw);
}

// Fresh = generated within the window (with a little tolerance for clocks
// that run slightly ahead of this machine).
function isFresh(trace, nowMs, windowMs) {
  if (!trace || !Number.isFinite(trace.generatedAt)) return false;
  const age = nowMs - trace.generatedAt;
  return age >= -CLOCK_SKEW_MS && age <= windowMs;
}

function formatAge(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return s + ' s ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + ' min ago';
  return Math.floor(m / 60) + ' h ago';
}

// Status as the HUD shows it: '' when evidence was found, a short phrase for
// known statuses, the raw name for anything else.
function statusText(status) {
  if (!status) return '';
  return Object.prototype.hasOwnProperty.call(STATUS_TEXT, status) ? STATUS_TEXT[status] : status;
}

function methodLabel(trace) { return trace.mode ? trace.method + ' ' + trace.mode : trace.method; }

// One-line HUD summary, e.g.
// "at 2026-09-24T12:00:00Z . synaptic . 5 passages . ~910 tokens . 12 s ago".
// `match` ({matched, total}) adds "m of n notes in this vault" when some
// trace notes are not in this vault.
function formatHud(trace, nowMs, match) {
  if (!trace) return '';
  const parts = ['at ' + trace.generatedAtText, methodLabel(trace)];
  const p = trace.packet || {};
  if (p.passages !== null && p.passages !== undefined) parts.push(p.passages + (p.passages === 1 ? ' passage' : ' passages'));
  if (p.estTokens !== null && p.estTokens !== undefined) parts.push('~' + p.estTokens + ' tokens');
  parts.push(formatAge(nowMs - trace.generatedAt));
  const status = statusText(p.status);
  if (status) parts.push(status);
  if (match && match.total > 0 && match.matched < match.total) parts.push(match.matched + ' of ' + match.total + ' notes in this vault');
  return parts.join(' \u00b7 ');
}

function mixRgb(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }

// Colour, size multiplier and alpha of one activated note. Three things are
// visible at a glance: seed vs hop (hue), activation score (size and
// brightness), and in the packet vs reached only (saturation and alpha).
function overlayNodeStyle(entry) {
  const a = clamp01(entry.activation);
  const seed = entry.role === 'seed';
  let color = seed ? SEED_RGB.slice() : mixRgb(HOP_RGB_LOW, HOP_RGB_HIGH, a);
  if (!entry.selected) color = mixRgb(color, REACHED_RGB, REACHED_MIX);
  let alpha = entry.selected ? 0.98 : 0.45 + 0.25 * a;
  return { color, sizeMult: 1.35 + 1.4 * a + (seed ? 0.3 : 0), alpha };
}

// Maps a trace onto the notes currently in the graph. `lookupNode(path)`
// returns the model node or undefined; unknown paths are dropped. The
// result is keyed by the model's own paths. Links are merged per note pair:
// one ribbon per pair, at the earliest traversal step that used it, pulsing
// in that direction. Returns null for no trace.
function mapOverlay(trace, lookupNode) {
  if (!trace) return null;
  const nodes = new Map(), byTracePath = new Map(); let maxHop = 0;
  for (const entry of trace.nodes) {
    const node = lookupNode(entry.path);
    if (!node || nodes.has(node.path)) continue;
    const style = overlayNodeStyle(entry);
    const mapped = { node, path: node.path, tracePath: entry.path, activation: entry.activation, hop: entry.hop, role: entry.role,
      selected: entry.selected, ...style };
    nodes.set(node.path, mapped); byTracePath.set(entry.path, mapped);
    if (entry.hop > maxHop) maxHop = entry.hop;
  }
  const pairs = new Map(); let droppedEdges = 0;
  for (const e of trace.edges) {
    const a = lookupNode(e.from), b = lookupNode(e.to);
    if (!a || !b || a === b) { droppedEdges++; continue; }
    const fromEntry = byTracePath.get(e.from), toEntry = byTracePath.get(e.to);
    // The writer expands notes of hop d-1 at step d, so a link used at step d
    // leaves a note of hop d-1: depth = hop(from) + 1.
    const hop = e.depth || (fromEntry ? fromEntry.hop + 1 : Math.max(1, toEntry ? toEntry.hop : 1));
    const key = a.path < b.path ? a.path + '\u0000' + b.path : b.path + '\u0000' + a.path;
    const prev = pairs.get(key);
    if (!prev) { pairs.set(key, { a, b, fromEntry, toEntry, kinds: [e.kind], weight: e.weight, hop, links: 1 }); continue; }
    prev.links++;
    if (!prev.kinds.includes(e.kind)) prev.kinds.push(e.kind);
    prev.weight = Math.max(prev.weight, e.weight);
    if (hop < prev.hop) Object.assign(prev, { a, b, fromEntry, toEntry, hop });
  }
  const edges = []; let maxEdgeHop = 1;
  for (const p of pairs.values()) {
    p.kinds.sort(compareStrings);
    if (p.hop > maxEdgeHop) maxEdgeHop = p.hop;
    edges.push({ from: p.a.path, to: p.b.path, a: p.a, b: p.b, kinds: p.kinds, weight: p.weight, hop: p.hop, links: p.links,
      fromColor: p.fromEntry ? p.fromEntry.color : HOP_RGB_LOW.slice(), toColor: p.toEntry ? p.toEntry.color : HOP_RGB_LOW.slice() });
  }
  edges.sort((x, y) => x.hop - y.hop || y.weight - x.weight || compareStrings(x.from, y.from) || compareStrings(x.to, y.to));
  const total = trace.nodes.length;
  return { key: trace.key, generatedAt: trace.generatedAt, nodes, edges, maxHop, maxEdgeHop,
    total, matched: nodes.size, droppedNodes: total - nodes.size, droppedEdges };
}

function pulseCycleMs(maxEdgeHop) { return Math.max(1, maxEdgeHop) * PULSE_STEP_MS + PULSE_TRAVEL_MS + PULSE_REST_MS; }

// Position (t in [0, 1)) and glow of the pulse on a link of the given hop at
// `elapsedMs` since the overlay appeared, or null while that link is idle.
function pulseAt(edgeHop, maxEdgeHop, elapsedMs) {
  if (!(elapsedMs >= 0)) return null;
  const local = (elapsedMs % pulseCycleMs(maxEdgeHop)) - (Math.max(1, edgeHop) - 1) * PULSE_STEP_MS;
  if (local < 0 || local >= PULSE_TRAVEL_MS) return null;
  const t = local / PULSE_TRAVEL_MS;
  return { t, glow: Math.sin(Math.PI * t) };
}

// Weighted centre and radius of a set of positions ({pos, weight}).
function frameNodes(points) {
  let wsum = 0; const c = [0, 0, 0];
  for (const p of points) {
    if (!p || (!Array.isArray(p.pos) && !ArrayBuffer.isView(p.pos))) continue;
    const w = Number.isFinite(p.weight) && p.weight > 0 ? p.weight : 0.05;
    for (let k = 0; k < 3; k++) c[k] += p.pos[k] * w;
    wsum += w;
  }
  if (!wsum) return null;
  for (let k = 0; k < 3; k++) c[k] /= wsum;
  let radius = 0;
  for (const p of points) {
    if (!p || !p.pos) continue;
    radius = Math.max(radius, Math.hypot(p.pos[0] - c[0], p.pos[1] - c[1], p.pos[2] - c[2]));
  }
  return { center: c, radius };
}

// Polls the trace file cheaply: stat first, read only when mtime or size
// changed. `onChange(trace | null)` fires when the visible trace changes.
// A missing file clears the trace; an oversized or malformed file is ignored
// and the previous trace (if any) is kept until it goes stale.
function createWatcher(options) {
  const adapter = options.adapter;
  const getPath = typeof options.getPath === 'function' ? options.getPath : () => DEFAULT_PATH;
  const maxBytes = options.maxBytes || MAX_BYTES;
  const onChange = typeof options.onChange === 'function' ? options.onChange : () => {};
  let signature = null, current = null, reads = 0;

  function clear() {
    signature = null;
    if (current) { current = null; onChange(null); }
    return null;
  }

  async function poll() {
    const path = sanitizeTracePath(getPath());
    if (!path || !adapter) return clear();
    let stat = null;
    if (typeof adapter.stat === 'function') {
      try { stat = await adapter.stat(path); } catch (_) { stat = null; }
      if (!stat || stat.type !== 'file') return clear();
      const sig = path + '|' + stat.mtime + '|' + stat.size;
      if (sig === signature) return current;
      signature = sig;
      if (!(stat.size >= 0) || stat.size > maxBytes) return current;
    } else {
      let exists = false;
      try { exists = await adapter.exists(path); } catch (_) { exists = false; }
      if (!exists) return clear();
    }
    let text;
    try { text = await adapter.read(path); reads++; } catch (_) { return current; }
    if (typeof adapter.stat !== 'function') {
      if (text === signature) return current;
      signature = text;
    }
    const trace = parseActivation(text, { maxBytes });
    if (!trace) return current;
    current = trace; onChange(trace);
    return current;
  }

  return { poll, reset() { signature = null; current = null; }, get current() { return current; }, get reads() { return reads; } };
}

module.exports = {
  DEFAULT_PATH, MAX_BYTES, MAX_NODES, MAX_EDGES, EDGE_KINDS, MODES, MODE_NOTES, STATUS_TEXT,
  SEED_RGB, HOP_RGB_LOW, HOP_RGB_HIGH, REACHED_RGB, OVERLAY_NODE_DIM,
  PULSE_STEP_MS, PULSE_TRAVEL_MS, PULSE_REST_MS,
  sanitizeVaultPath, sanitizeTracePath, parseTimestamp, normalizeTrace, parseActivation, isFresh, formatAge,
  statusText, methodLabel, formatHud, overlayNodeStyle, mapOverlay,
  pulseCycleMs, pulseAt, frameNodes, createWatcher,
};
