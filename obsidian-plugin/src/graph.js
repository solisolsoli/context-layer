'use strict';

// Presentation model of the vault's link graph. It reads only file paths,
// names and Obsidian's resolved-link table; note contents are never read or
// written here.
const Layout = require('./layout');

// Walks the public TFolder tree iteratively. Folders and non-Markdown files
// are yielded too, so the async builder can yield on every visited item.
function* walkVaultEntries(vault, root = typeof vault.getRoot === 'function' ? vault.getRoot() : null) {
  if (!root || !Array.isArray(root.children)) {
    for (const file of vault.getMarkdownFiles()) yield file;
    return;
  }
  const stack = [{ children: root.children, index: 0 }];
  while (stack.length) {
    const frame = stack[stack.length - 1];
    if (frame.index >= frame.children.length) { stack.pop(); continue; }
    const entry = frame.children[frame.index++]; yield entry;
    if (Array.isArray(entry?.children)) stack.push({ children: entry.children, index: 0 });
  }
}

function isMarkdownFile(file) {
  return !!file && !Array.isArray(file.children) &&
    (file.extension === 'md' || (typeof file.path === 'string' && file.path.toLowerCase().endsWith('.md')));
}
function isFolder(file) { return !!file && Array.isArray(file.children); }

// Unicode normalization form C, so decomposed (NFD) and composed (NFC)
// spellings of the same path compare equal.
function nfc(path) { return typeof path === 'string' && typeof path.normalize === 'function' ? path.normalize('NFC') : path; }

// FNV-1a over a string (public domain algorithm); used for the layout cache.
function hashString(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

const INNER_RADIUS = 0.94;

class BrainModel {
  // options.positionCache: {path: {x, y, z, h}} from a previous session, where
  // h is the hash of the note's neighbour set when the position was saved.
  // options.classify: file -> region key (string). Defaults to ''.
  constructor(app, options = {}) {
    this.app = app;
    this.positionCache = options.positionCache || {};
    this.classify = typeof options.classify === 'function' ? options.classify : () => '';
    this.nodes = new Map(); this.edgeMap = new Map(); this.adjacency = new Map();
    this.orderedNodes = []; this.connectedNodes = []; this.newEdgeKeys = [];
    // structureVersion: the link structure (restarts the layout);
    // viewRevision: structure or paths (render caches keyed by path).
    this.structureVersion = 0; this.regionVersion = 0; this.viewRevision = 0; this.orphanCount = 0;
    this.buffersDirty = true; this._serial = 0;
    this.workerState = 'initializing'; this.staleResults = 0; this.layoutRequests = 0;
    this._pendingStructure = false; this._pathsChanged = false; this._linkChanged = new Set(); this._nfcIndex = null;
  }

  edgeKey(a, b) { return a < b ? a + '\u0000' + b : b + '\u0000' + a; }

  regionKeyFor(file) {
    try { const key = this.classify(file); return typeof key === 'string' ? key : ''; } catch (_) { return ''; }
  }

  makeNode(file) {
    const old = this.positionCache[file.path];
    const ok = !!old && [old.x, old.y, old.z].every(Number.isFinite);
    return { id: file.path + ':' + this._serial++, path: file.path, file, title: file.basename,
      regionKey: this.regionKeyFor(file), region: null, degree: 0, role: null,
      pos: ok ? [old.x, old.y, old.z] : [0, 0, 0], target: null, seeded: ok,
      cachedHash: ok && Number.isInteger(old.h) ? old.h : null, placed: false };
  }

  // Looks a note up by path, falling back to Unicode-normalized comparison.
  lookup(path) {
    if (typeof path !== 'string') return undefined;
    const direct = this.nodes.get(path);
    if (direct) return direct;
    if (!this._nfcIndex) {
      this._nfcIndex = new Map();
      for (const [p, node] of this.nodes) { const key = nfc(p); if (!this._nfcIndex.has(key)) this._nfcIndex.set(key, node); }
    }
    return this._nfcIndex.get(nfc(path));
  }

  // Refreshes an existing node from its file; returns true if its region changed.
  refreshNode(node, file) {
    node.file = file; node.title = file.basename;
    const key = this.regionKeyFor(file);
    if (key === node.regionKey) return false;
    node.regionKey = key; return true;
  }

  setClassifier(classify) {
    this.classify = typeof classify === 'function' ? classify : () => '';
    this.reclassify();
  }

  reclassify() {
    let changed = false;
    for (const node of this.nodes.values()) if (this.refreshNode(node, node.file)) changed = true;
    if (changed) this.regionVersion++;
    return changed;
  }

  // Full reconciliation with the vault: adds, removes and refreshes notes,
  // then recomputes links. Returns true if the link structure changed.
  buildFromVault() {
    const seen = new Set(); let changed = false, regionChanged = false;
    const count = { entries: 0, markdown: 0, folders: 0, otherFiles: 0 };
    for (const f of walkVaultEntries(this.app.vault)) {
      count.entries++;
      if (Array.isArray(f?.children)) { count.folders++; continue; }
      if (!isMarkdownFile(f)) { count.otherFiles++; continue; }
      count.markdown++;
      seen.add(f.path); let node = this.nodes.get(f.path);
      if (!node) { node = this.makeNode(f); this.nodes.set(f.path, node); changed = true; }
      else if (this.refreshNode(node, f)) regionChanged = true;
    }
    for (const p of this.nodes.keys()) if (!seen.has(p)) { this.nodes.delete(p); changed = true; }
    return this.finishBuild(count, changed, regionChanged);
  }

  // Same result as buildFromVault, but yields to the event loop every
  // `sliceMs` of work so opening the view on a large vault stays responsive.
  async buildFromVaultAsync(options = {}) {
    const sliceMs = Math.max(1, options.sliceMs || 6);
    let chunk = performance.now();
    const checkpoint = async () => { await new Promise(resolve => setTimeout(resolve, 0)); chunk = performance.now(); };
    const seen = new Set(); let changed = false, regionChanged = false;
    const count = { entries: 0, markdown: 0, folders: 0, otherFiles: 0 };
    for (const f of walkVaultEntries(this.app.vault)) {
      if (this.closed) return false;
      count.entries++;
      if (Array.isArray(f?.children)) count.folders++;
      else if (!isMarkdownFile(f)) count.otherFiles++;
      else {
        count.markdown++;
        seen.add(f.path); let node = this.nodes.get(f.path);
        if (!node) { node = this.makeNode(f); this.nodes.set(f.path, node); changed = true; }
        else if (this.refreshNode(node, f)) regionChanged = true;
      }
      if (performance.now() - chunk > sliceMs) await checkpoint();
    }
    if (this.closed) return false;
    for (const p of this.nodes.keys()) if (!seen.has(p)) { this.nodes.delete(p); changed = true; }
    await checkpoint(); if (this.closed) return false;
    const map = new Map(), resolved = this.app.metadataCache.resolvedLinks || {};
    for (const a of Object.keys(resolved)) {
      if (!this.nodes.has(a)) continue;
      for (const b of Object.keys(resolved[a])) {
        if (a !== b && this.nodes.has(b) && resolved[a][b] > 0) map.set(this.edgeKey(a, b), { a, b });
        if (performance.now() - chunk > sliceMs) { await checkpoint(); if (this.closed) return false; }
      }
    }
    await checkpoint(); if (this.closed) return false;
    this.noteBuild(count, regionChanged);
    return this.applyEdgeMap(map, changed);
  }

  noteBuild(count, regionChanged) {
    if (regionChanged || this._pathsChanged) this.regionVersion++;
    if (this._pathsChanged) { this._pathsChanged = false; this.viewRevision++; }
    this._nfcIndex = null;
    this.graphEnumeration = count;
  }

  finishBuild(count, changed, regionChanged) {
    this.noteBuild(count, regionChanged);
    return this.recomputeEdges(changed);
  }

  // Re-keys a renamed note (or every note below a renamed folder) in place:
  // node identity, position and links are kept, so a rename never moves a
  // note. Cost is the number of links of the renamed notes, not the vault
  // size; the next buildFromVault() reconciles anything else.
  rename(file, oldPath) {
    if (!file || typeof oldPath !== 'string') return false;
    if (isFolder(file)) {
      const prefix = oldPath + '/', moved = [];
      for (const [p, node] of this.nodes) if (p.startsWith(prefix)) moved.push(node);
      for (const node of moved) this.rekey(node, file.path + '/' + node.path.slice(prefix.length), null);
      return moved.length > 0;
    }
    const node = this.nodes.get(oldPath);
    if (!node) return false;
    if (!isMarkdownFile(file)) return this.forget(node);
    this.rekey(node, file.path, file);
    return true;
  }

  rekey(node, newPath, file) {
    const oldPath = node.path;
    if (oldPath === newPath) return;
    this.nodes.delete(oldPath);
    node.path = newPath;
    if (file) { node.file = file; node.title = file.basename; }
    node.regionKey = this.regionKeyFor(node.file);
    this.nodes.set(newPath, node);
    const neighbours = this.adjacency.get(oldPath);
    if (neighbours) {
      this.adjacency.delete(oldPath); this.adjacency.set(newPath, neighbours);
      for (const nb of neighbours) {
        const set = this.adjacency.get(nb); if (set) { set.delete(oldPath); set.add(newPath); }
        const edge = this.edgeMap.get(this.edgeKey(oldPath, nb));
        if (edge) {
          this.edgeMap.delete(this.edgeKey(oldPath, nb));
          if (edge.a === oldPath) edge.a = newPath; else edge.b = newPath;
          this.edgeMap.set(this.edgeKey(edge.a, edge.b), edge);
        }
      }
    }
    this._pathsChanged = true; this._nfcIndex = null;
  }

  // Removes one deleted note at once (constant time), or every note below a
  // deleted folder. Returns the paths of former neighbours so the view can
  // signal them. Links are reconciled by the next buildFromVault().
  removePath(path) {
    const neighbours = new Set();
    const node = this.nodes.get(path);
    const doomed = node ? [node] : Array.from(this.nodes.values()).filter(n => n.path.startsWith(path + '/'));
    for (const n of doomed) {
      for (const nb of this.adjacency.get(n.path) || []) neighbours.add(nb);
      this.forget(n);
    }
    for (const p of neighbours) if (!this.nodes.has(p)) neighbours.delete(p);
    return Array.from(neighbours);
  }

  forget(node) {
    this.nodes.delete(node.path); node.removed = true;
    this._pendingStructure = true; this._nfcIndex = null;
    return true;
  }

  recomputeEdges(nodeChange = false) {
    const map = new Map(), resolved = this.app.metadataCache.resolvedLinks || {};
    for (const a of Object.keys(resolved)) if (this.nodes.has(a)) {
      for (const b of Object.keys(resolved[a])) {
        if (a !== b && this.nodes.has(b) && resolved[a][b] > 0) map.set(this.edgeKey(a, b), { a, b });
      }
    }
    return this.applyEdgeMap(map, nodeChange);
  }

  applyEdgeMap(map, nodeChange = false) {
    const added = []; let changed = nodeChange || this._pendingStructure || map.size !== this.edgeMap.size;
    for (const key of map.keys()) if (!this.edgeMap.has(key)) { added.push(key); changed = true; }
    if (!changed && this.structureVersion) return false;
    this._pendingStructure = false;
    this.newEdgeKeys = this.structureVersion ? added : [];
    // Endpoints of new links are re-solved; removed links leave notes in place.
    if (this.structureVersion) for (const key of added) { const e = map.get(key); this._linkChanged.add(e.a); this._linkChanged.add(e.b); }
    this.edgeMap = map; this.adjacency = new Map();
    for (const n of this.nodes.values()) n.degree = 0;
    for (const { a, b } of map.values()) {
      this.nodes.get(a).degree++; this.nodes.get(b).degree++;
      if (!this.adjacency.has(a)) this.adjacency.set(a, new Set());
      if (!this.adjacency.has(b)) this.adjacency.set(b, new Set());
      this.adjacency.get(a).add(b); this.adjacency.get(b).add(a);
    }
    this.structureVersion++; this.viewRevision++;
    this.assignRolesAndSeed();
    return true;
  }

  // Hash of a note's current neighbour paths; a cached position is reused
  // only while this still matches.
  neighbourHash(path) {
    const list = Array.from(this.adjacency.get(path) || []).sort();
    return hashString(list.join('\n'));
  }

  // Orphans (no links) sit on stable slots of an outer shell; linked notes
  // fill the inner volume and are relaxed by the layout worker.
  assignRolesAndSeed() {
    this.connectedNodes = []; this.orderedNodes = Array.from(this.nodes.values());
    const orphans = [];
    for (const n of this.orderedNodes) {
      const role = n.degree ? 'linked' : 'orphan';
      // A note that just gained its first link is placed next to its neighbours.
      if (role === 'linked' && n.role === 'orphan' && n.seeded) { n.placed = false; n.cachedHash = null; n.seeded = false; }
      n.role = role;
      if (n.degree) this.connectedNodes.push(n); else orphans.push(n);
    }
    const shell = Layout.shellSlots(orphans.map(n => n.path));
    for (let i = 0; i < orphans.length; i++) {
      const n = orphans[i]; n.target = Array.from(shell.subarray(i * 3, i * 3 + 3));
      if (!n.seeded) { n.pos = n.target.slice(); n.seeded = true; }
    }
    // Drawable positions until the worker answers: next to placed
    // neighbours, else a point derived from the path.
    for (const n of this.connectedNodes) {
      if (n.seeded) continue;
      let m = 0; const c = [0, 0, 0];
      for (const p of this.adjacency.get(n.path) || []) { const nb = this.nodes.get(p); if (nb && nb.seeded && nb.role === 'linked') { for (let k = 0; k < 3; k++) c[k] += nb.pos[k]; m++; } }
      const h = hashString(n.path), u = [((h & 1023) / 1023) * 2 - 1, (((h >>> 10) & 1023) / 1023) * 2 - 1, (((h >>> 20) & 1023) / 1023) * 2 - 1];
      const r = INNER_RADIUS * (m ? 0.04 : 0.45);
      const p = m ? [c[0] / m + u[0] * r, c[1] / m + u[1] * r, c[2] / m + u[2] * r] : [u[0] * r, u[1] * r, u[2] * r];
      const len = Math.hypot(p[0], p[1], p[2]);
      n.pos = len > INNER_RADIUS ? p.map(v => v * INNER_RADIUS / len) : p;
      n.seeded = true;
    }
    this.orphanCount = orphans.length; this.buffersDirty = true;
    if (this.worker) this.requestLayout();
  }

  attachWorker(worker, onError, onSettled) {
    this.worker = worker; this.onError = onError; this.onSettled = onSettled;
    worker.onmessage = ({ data: m }) => {
      if (this.closed) return;
      if (m.revision !== this.structureVersion) { this.staleResults++; this.recycle(m.positions); return; }
      if (m.type === 'error') { this.workerState = 'error'; onError(new Error(m.error)); return; }
      if (m.type !== 'positions') return;
      const p = new Float32Array(m.positions);
      if (p.length !== this.orderedNodes.length * 3 || !p.every(Number.isFinite)) {
        this.recycle(m.positions); onError(new Error('Invalid worker positions')); return;
      }
      if (this.pendingPositions) this.recycle(this.pendingPositions.buffer);
      this.pendingPositions = p; this.layoutStats = m.stats; this.layoutIterations = m.iterations;
      this.workerState = m.settled ? 'settled' : 'solving';
      this._workerSettled = m.settled;
      if (m.settled) { for (const n of this.connectedNodes) n.placed = true; this._linkChanged.clear(); }
    };
    worker.onerror = e => { this.workerState = 'error'; onError(new Error(e.message)); };
    this.requestLayout();
  }

  recycle(buffer) { if (buffer && buffer.byteLength && this.worker && !this.closed) this.worker.postMessage({ type: 'recycle', buffer }, [buffer]); }

  // Sends the graph to the worker. Linked notes that already have a settled
  // (or valid cached) position and no new link are pinned, so the worker
  // moves only new and re-linked notes. If every linked note is pinned,
  // nothing is solved and the saved layout is used as it is.
  requestLayout() {
    this._persistedRevision = 0;
    if (this.pendingPositions) { this.recycle(this.pendingPositions.buffer); this.pendingPositions = null; }
    const count = this.orderedNodes.length, index = new Map(), keys = [];
    const positions = new Float32Array(count * 3), pinned = new Uint8Array(count);
    let free = 0;
    this.orderedNodes.forEach((n, i) => {
      index.set(n.path, i); keys.push(n.path);
      const at = n.target && n.role === 'linked' && n.placed ? n.target : n.pos;
      const cacheValid = !n.placed && n.cachedHash !== null && n.cachedHash === this.neighbourHash(n.path);
      if (cacheValid) n.placed = true;
      const known = n.placed || (n.cachedHash !== null && n.seeded);
      positions[3 * i] = known ? at[0] : NaN; positions[3 * i + 1] = known ? at[1] : NaN; positions[3 * i + 2] = known ? at[2] : NaN;
      if (n.role !== 'linked') return;
      if (n.placed && !this._linkChanged.has(n.path)) pinned[i] = 1; else free++;
    });
    if (!free) {
      // Nothing to solve: the saved or settled layout stands.
      for (const n of this.orderedNodes) if (n.role === 'linked') { n.target = n.pos.slice(); n.placed = true; }
      this._linkChanged.clear();
      this._workerSettled = true; this.workerState = 'settled'; this._persistedRevision = this.structureVersion;
      return;
    }
    const edges = new Uint32Array(this.edgeMap.size * 2); let k = 0;
    for (const e of this.edgeMap.values()) { edges[k++] = index.get(e.a); edges[k++] = index.get(e.b); }
    this._workerSettled = false; this.workerState = 'solving'; this.layoutRequests++;
    this.worker.postMessage({ type: 'solve', revision: this.structureVersion, keys, edges, positions, pinned },
      [edges.buffer, positions.buffer, pinned.buffer]);
    if (this.paused) this.worker.postMessage({ type: 'pause' });
  }

  // Eases drawn positions toward the latest worker result. Returns true if
  // anything moved this frame.
  advance(dt, reducedMotion) {
    if (this.pendingPositions) {
      const p = this.pendingPositions;
      this.orderedNodes.forEach((n, i) => { const t = n.target || (n.target = [0, 0, 0]); t[0] = p[3 * i]; t[1] = p[3 * i + 1]; t[2] = p[3 * i + 2]; });
      this.pendingPositions = null; this.recycle(p.buffer);
    }
    let moved = false; const a = reducedMotion ? 1 : 1 - Math.exp(-Math.min(dt, 0.05) * 9);
    for (const n of this.orderedNodes) if (n.target) {
      for (let c = 0; c < 3; c++) { const d = n.target[c] - n.pos[c]; if (Math.abs(d) > 0.00001) { n.pos[c] += d * a; moved = true; } else n.pos[c] = n.target[c]; }
    }
    if (moved) this.buffersDirty = true;
    if (!moved && this._workerSettled && !this._persistedRevision) { this._persistedRevision = this.structureVersion; this.onSettled?.(); }
    return moved;
  }

  setPaused(paused) { this.paused = paused; if (this.worker) this.worker.postMessage({ type: paused ? 'pause' : 'resume' }); }
  destroy() { this.closed = true; if (this.worker) this.worker.terminate(); this.worker = null; }

  // Unit vector from the sphere centre toward the mean position of a region.
  regionDirection(key) {
    const p = [0, 0, 0];
    for (const n of this.nodes.values()) if (n.region?.key === key) for (let c = 0; c < 3; c++) p[c] += n.pos[c];
    const l = Math.hypot(...p) || 1; return p.map(x => x / l);
  }
}

module.exports = { BrainModel, walkVaultEntries, isMarkdownFile, isFolder, nfc, hashString };
