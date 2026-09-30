'use strict';

// Dependency-free 3D layout. Linked notes are relaxed inside a sphere of
// radius INNER: Barnes-Hut repulsion keeps them apart, a spring along every
// real link pulls linked notes together, and a rank-preserving radial term
// keeps the sphere evenly filled without reordering notes. Unlinked notes get
// stable slots on the unit shell. The result depends only on the graph (note
// keys and links), never on input order, session or revision:
//   - a full solve starts from a deterministic placement that keeps linked
//     notes close (breadth-first order laid along a 3D Hilbert curve);
//   - an incremental solve moves only the free notes and keeps pinned notes
//     exactly where they are.
// Keep apiFactory self-contained: workerSource() serializes it into a Worker
// without CommonJS or access to this module.
function apiFactory() {
  const INNER = 0.94;
  const BINS = 32; // 8 octants x 4 equal-r^3 shells
  const THETA = 1.0; // Barnes-Hut opening angle
  const MAX_DEPTH = 16;
  const REPULSION = 0.6, SPRING = 0.16, REST = 0.55, RADIAL = 0.35, SPEED_CAP = 0.3;
  const MAX_FULL_ITERATIONS = 300, MAX_INCREMENTAL_ITERATIONS = 150;

  // FNV-1a string hash (public domain algorithm).
  function hashString(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  // mulberry32 PRNG by Tommy Ettinger (CC0), used for deterministic seeding.
  function rng(seed) {
    let a = seed >>> 0;
    return function random() { a += 0x6D2B79F5; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }
  function unitVector(random) {
    const z = random() * 2 - 1, a = random() * Math.PI * 2, q = Math.sqrt(Math.max(0, 1 - z * z));
    return [q * Math.cos(a), q * Math.sin(a), z];
  }
  function compareKeys(a, b) { return a < b ? -1 : (a > b ? 1 : 0); }

  // Cell coordinates of position `index` on a 3D Hilbert curve with `bits`
  // bits per axis: J. Skilling's transpose-to-axes transform ("Programming
  // the Hilbert curve", AIP Conf. Proc. 707, 2004). Consecutive indices are
  // face-adjacent cells, so neighbours in a 1D order stay neighbours in 3D.
  function hilbertAxes(index, bits) {
    const X = [0, 0, 0];
    for (let b = 0; b < bits; b++) for (let d = 0; d < 3; d++) X[d] |= ((index >>> (3 * (bits - 1 - b) + 2 - d)) & 1) << (bits - 1 - b);
    const N = 2 << (bits - 1);
    let t = X[2] >> 1;
    for (let i = 2; i > 0; i--) X[i] ^= X[i - 1];
    X[0] ^= t;
    for (let Q = 2; Q !== N; Q <<= 1) {
      const mask = Q - 1;
      for (let i = 2; i >= 0; i--) {
        if (X[i] & Q) X[0] ^= mask;
        else { t = (X[0] ^ X[i]) & mask; X[0] ^= t; X[i] ^= t; }
      }
    }
    return X;
  }
  // Centres of the curve's cells that lie inside the unit ball, in curve order.
  const cellCache = {};
  function ballCells(bits) {
    if (cellCache[bits]) return cellCache[bits];
    const side = 1 << bits, half = side / 2, out = [];
    for (let h = 0; h < side * side * side; h++) {
      const X = hilbertAxes(h, bits);
      const x = (X[0] + 0.5 - half) / half, y = (X[1] + 0.5 - half) / half, z = (X[2] + 0.5 - half) / half;
      if (x * x + y * y + z * z <= 1) out.push(x, y, z);
    }
    return (cellCache[bits] = { centres: Float64Array.from(out), count: out.length / 3, size: 1 / half });
  }

  // `count` evenly spread points on the unit sphere: equal-area latitude
  // rings, each ring's integer share apportioned by sin(theta) with largest
  // remainders so exactly `count` points come out. Accepts a number or a list.
  function surfaceSlots(count) {
    const n = typeof count === 'number' ? count : count.length, out = new Float32Array(3 * n);
    if (!n) return out;
    const rows = Math.min(n, Math.max(1, Math.round(Math.sqrt(n * Math.PI) / 2)));
    const weights = new Float64Array(rows), counts = new Uint32Array(rows), rem = new Float64Array(rows);
    let sum = 0, used = 0;
    for (let r = 0; r < rows; r++) { const theta = Math.PI * (r + 0.5) / rows; weights[r] = Math.sin(theta); sum += weights[r]; }
    for (let r = 0; r < rows; r++) { const exact = n * weights[r] / sum; counts[r] = Math.floor(exact); rem[r] = exact - counts[r]; used += counts[r]; }
    while (used < n) { let best = 0; for (let r = 1; r < rows; r++) if (rem[r] > rem[best]) best = r; counts[best]++; rem[best] = -1; used++; }
    let at = 0;
    for (let r = 0; r < rows; r++) {
      const c = counts[r]; if (!c) continue;
      // Evenly spaced longitudes in each ring; alternating rings are staggered.
      const phase = (r & 1) ? Math.PI / c : 0, theta = Math.PI * (r + 0.5) / rows, z = Math.cos(theta), rr = Math.sin(theta);
      for (let j = 0; j < c; j++, at++) {
        const angle = phase + (Math.PI * 2 * j) / c;
        out[3 * at] = rr * Math.cos(angle); out[3 * at + 1] = rr * Math.sin(angle); out[3 * at + 2] = z;
      }
    }
    return out;
  }
  // Shell capacity grows in steps of 1.5x and is at most 80% full, so adding
  // or removing one unlinked note does not reshuffle the others.
  function shellCapacity(count) { let cap = 64; while (cap * 0.8 < count) cap = Math.ceil(cap * 1.5); return cap; }
  // Stable shell slots: each key probes a regular grid from its own hash, in
  // hash order, so a note keeps its slot while other notes come and go.
  function shellSlots(keys) {
    const n = keys.length, out = new Float32Array(3 * n);
    if (!n) return out;
    const cap = shellCapacity(n), grid = surfaceSlots(cap), taken = new Uint8Array(cap);
    const order = [];
    for (let i = 0; i < n; i++) { const key = String(keys[i]); order.push([hashString(key), key, i]); }
    order.sort((a, b) => a[0] - b[0] || compareKeys(a[1], b[1]) || a[2] - b[2]);
    for (const [h, , i] of order) {
      let s = h % cap;
      while (taken[s]) s = (s + 1) % cap;
      taken[s] = 1;
      out[3 * i] = grid[3 * s]; out[3 * i + 1] = grid[3 * s + 1]; out[3 * i + 2] = grid[3 * s + 2];
    }
    return out;
  }
  function densityStats(positions, degrees) {
    const bins = new Uint32Array(BINS); let connected = 0, maxRadius = 0;
    for (let i = 0; i < degrees.length; i++) if (degrees[i] > 0) {
      const k = 3 * i, x = positions[k], y = positions[k + 1], z = positions[k + 2];
      if (!Number.isFinite(x + y + z)) continue;
      const r = Math.sqrt(x * x + y * y + z * z); maxRadius = Math.max(maxRadius, r); connected++;
      let shell = Math.floor(4 * r * r * r / (INNER * INNER * INNER)); if (shell > 3) shell = 3;
      const oct = (x >= 0 ? 1 : 0) | (y >= 0 ? 2 : 0) | (z >= 0 ? 4 : 0);
      bins[oct * 4 + shell]++;
    }
    if (!connected) return { bins: Array.from(bins), cv: 0, empty: 0, connected: 0, maxRadius: 0 };
    const mean = connected / BINS; let variance = 0, empty = 0;
    for (let b = 0; b < BINS; b++) { const d = bins[b] - mean; variance += d * d; if (!bins[b]) empty++; }
    return { bins: Array.from(bins), cv: Math.sqrt(variance / BINS) / mean, empty, connected, maxRadius };
  }

  // input: { keys (or ids), edges: flat index pairs, positions?: xyz per note
  // (NaN = unknown), pinned?: 1 per note that must not move }.
  function createSolver(input) {
    const keys = Array.from(input.keys || input.ids || [], k => String(k)), n = keys.length;
    // Canonical order: every loop below runs in key order, so the result is
    // bitwise identical for any input order.
    const canon = Array.from({ length: n }, (_, i) => i).sort((a, b) => compareKeys(keys[a], keys[b]) || a - b);
    const at = new Int32Array(n); for (let c = 0; c < n; c++) at[canon[c]] = c;
    const raw = input.edges || [], seen = new Set(), pairs = [];
    for (let e = 0; e + 1 < raw.length; e += 2) {
      const a = raw[e], b = raw[e + 1];
      if (!(a >= 0 && a < n && b >= 0 && b < n) || a === b) continue;
      const lo = Math.min(at[a], at[b]), hi = Math.max(at[a], at[b]), key = lo * n + hi;
      if (!seen.has(key)) { seen.add(key); pairs.push([lo, hi]); }
    }
    pairs.sort((p, q) => p[0] - q[0] || p[1] - q[1]);
    const E = pairs.length, ea = new Int32Array(E), eb = new Int32Array(E), deg = new Uint32Array(n);
    for (let i = 0; i < E; i++) { ea[i] = pairs[i][0]; eb[i] = pairs[i][1]; deg[ea[i]]++; deg[eb[i]]++; }
    const start = new Int32Array(n + 1); for (let c = 0; c < n; c++) start[c + 1] = start[c] + deg[c];
    const cursor = start.slice(0, n), adj = new Int32Array(2 * E);
    for (let i = 0; i < E; i++) { adj[cursor[ea[i]]++] = eb[i]; adj[cursor[eb[i]]++] = ea[i]; }
    const springK = new Float64Array(E);
    for (let i = 0; i < E; i++) springK[i] = SPRING / Math.sqrt(Math.sqrt(deg[ea[i]] * deg[eb[i]]));

    const pos = new Float64Array(3 * n), vel = new Float64Array(3 * n), free = new Uint8Array(n), known = new Uint8Array(n);
    const linked = [], orphanKeys = [], orphans = [];
    for (let c = 0; c < n; c++) { if (deg[c]) linked.push(c); else { orphans.push(c); orphanKeys.push(keys[canon[c]]); } }
    const slots = shellSlots(orphanKeys);
    orphans.forEach((c, o) => { pos[3 * c] = slots[3 * o]; pos[3 * c + 1] = slots[3 * o + 1]; pos[3 * c + 2] = slots[3 * o + 2]; });
    const M = linked.length, spacing = M ? INNER * Math.cbrt(4.18879 / M) : 0.1;
    const rest = REST * spacing, repK = REPULSION * spacing * spacing * spacing * 0.01, soft = 0.02 * spacing * spacing, speedCap = SPEED_CAP * spacing;

    const posIn = input.positions && input.positions.length === 3 * n ? input.positions : null;
    const pinIn = input.pinned && input.pinned.length === n ? input.pinned : null;
    let pinnedCount = 0, knownCount = 0;
    for (const c of linked) {
      const i = canon[c];
      if (posIn && Number.isFinite(posIn[3 * i] + posIn[3 * i + 1] + posIn[3 * i + 2])) {
        pos[3 * c] = posIn[3 * i]; pos[3 * c + 1] = posIn[3 * i + 1]; pos[3 * c + 2] = posIn[3 * i + 2];
        known[c] = 1; knownCount++;
      }
      if (pinIn && pinIn[i] && known[c]) pinnedCount++; else free[c] = 1;
    }
    const fullSolve = !knownCount;
    if (fullSolve) initialPlacement(); else placeUnknown();
    separateCoincident();

    // Breadth-first order per component (largest component first, hubs
    // first), laid along the Hilbert curve through the ball.
    function initialPlacement() {
      if (!M) return;
      const comp = new Int32Array(n).fill(-1), comps = [];
      for (const c0 of linked) {
        if (comp[c0] >= 0) continue;
        const list = [c0]; comp[c0] = comps.length;
        for (let q = 0; q < list.length; q++) for (let k = start[list[q]]; k < start[list[q] + 1]; k++) if (comp[adj[k]] < 0) { comp[adj[k]] = comps.length; list.push(adj[k]); }
        comps.push(list);
      }
      comps.sort((a, b) => b.length - a.length || a[0] - b[0]);
      const order = [];
      for (const list of comps) {
        let root = list[0];
        for (const c of list) if (deg[c] > deg[root] || (deg[c] === deg[root] && c < root)) root = c;
        const visited = new Set([root]); order.push(root);
        for (let q = order.length - 1; q < order.length; q++) {
          const nb = [];
          for (let k = start[order[q]]; k < start[order[q] + 1]; k++) if (!visited.has(adj[k])) { visited.add(adj[k]); nb.push(adj[k]); }
          nb.sort((a, b) => deg[a] - deg[b] || a - b);
          for (const v of nb) order.push(v);
        }
      }
      const cells = ballCells(M > 12000 ? 6 : 5);
      for (let i = 0; i < M; i++) {
        const c = order[i], cell = Math.min(cells.count - 1, Math.floor((i + 0.5) * cells.count / M)), r = rng(hashString(keys[canon[c]]));
        for (let d = 0; d < 3; d++) pos[3 * c + d] = INNER * (cells.centres[3 * cell + d] + (r() - 0.5) * cells.size);
      }
    }
    // Free notes without a position start next to their placed neighbours;
    // notes with no placed neighbour start at a point derived from their key.
    function placeUnknown() {
      let pending = linked.filter(c => !known[c]);
      while (pending.length) {
        const later = [];
        for (const c of pending) {
          let sx = 0, sy = 0, sz = 0, m = 0;
          for (let k = start[c]; k < start[c + 1]; k++) { const v = adj[k]; if (known[v]) { sx += pos[3 * v]; sy += pos[3 * v + 1]; sz += pos[3 * v + 2]; m++; } }
          if (!m) { later.push(c); continue; }
          const u = unitVector(rng(hashString(keys[canon[c]])));
          pos[3 * c] = sx / m + u[0] * rest; pos[3 * c + 1] = sy / m + u[1] * rest; pos[3 * c + 2] = sz / m + u[2] * rest;
          known[c] = 2;
        }
        if (later.length === pending.length) {
          for (const c of later) {
            const r = rng(hashString(keys[canon[c]])), u = unitVector(r), rad = INNER * Math.cbrt(r());
            pos[3 * c] = u[0] * rad; pos[3 * c + 1] = u[1] * rad; pos[3 * c + 2] = u[2] * rad; known[c] = 2;
          }
          break;
        }
        pending = later;
      }
      for (const c of linked) {
        const r = Math.hypot(pos[3 * c], pos[3 * c + 1], pos[3 * c + 2]);
        if (r > INNER) { const q = INNER / r; pos[3 * c] *= q; pos[3 * c + 1] *= q; pos[3 * c + 2] *= q; }
      }
    }
    // Exactly coincident free notes would feel no repulsion; nudge them apart.
    function separateCoincident() {
      const at3 = new Map();
      for (const c of linked) {
        const key = pos[3 * c] + ',' + pos[3 * c + 1] + ',' + pos[3 * c + 2];
        if (!at3.has(key)) { at3.set(key, c); continue; }
        if (!free[c]) continue;
        const u = unitVector(rng(hashString(keys[canon[c]]) ^ 0x9E3779B9));
        for (let d = 0; d < 3; d++) pos[3 * c + d] += u[d] * rest * 0.1;
      }
    }

    let freeCount = 0; for (const c of linked) if (free[c]) freeCount++;
    const maxIterations = fullSolve ? MAX_FULL_ITERATIONS : MAX_INCREMENTAL_ITERATIONS;
    let iterations = 0, stableSteps = 0, settled = freeCount === 0;
    const fx = new Float64Array(n), fy = new Float64Array(n), fz = new Float64Array(n);
    const radius = new Float64Array(M), byRadius = new Int32Array(M), targetRadius = new Float64Array(n);

    // Barnes-Hut octree, reused across iterations; capacity grows by doubling.
    let cap = Math.max(64, 2 * M + 16), treeNodes = 0;
    let cx, cy, cz, half, mass, mx, my, mz, child, mask, point, head;
    const next = new Int32Array(n);
    let stack = new Int32Array(Math.max(64, 8 * MAX_DEPTH + 8));
    function allocate(copy) {
      const old = copy ? { cx, cy, cz, half, mass, mx, my, mz, child, mask, point, head } : null;
      cx = new Float64Array(cap); cy = new Float64Array(cap); cz = new Float64Array(cap); half = new Float64Array(cap);
      mass = new Float64Array(cap); mx = new Float64Array(cap); my = new Float64Array(cap); mz = new Float64Array(cap);
      child = new Int32Array(cap * 8); mask = new Uint8Array(cap); point = new Int32Array(cap); head = new Int32Array(cap);
      if (old) for (const name of Object.keys(old)) ({ cx, cy, cz, half, mass, mx, my, mz, child, mask, point, head })[name].set(old[name]);
    }
    allocate(false);
    function newCell(x, y, z, h) {
      if (treeNodes >= cap) { cap *= 2; allocate(true); }
      const q = treeNodes++;
      cx[q] = x; cy[q] = y; cz[q] = z; half[q] = h; mass[q] = 0; mask[q] = 0; point[q] = -1; head[q] = -1;
      return q;
    }
    function childCell(q, o) {
      const h = half[q] * 0.5;
      const c = newCell(cx[q] + ((o & 1) ? h : -h), cy[q] + ((o & 2) ? h : -h), cz[q] + ((o & 4) ? h : -h), h);
      child[q * 8 + o] = c; mask[q] |= 1 << o;
      return c;
    }
    function octant(q, x, y, z) { return (x >= cx[q] ? 1 : 0) | (y >= cy[q] ? 2 : 0) | (z >= cz[q] ? 4 : 0); }
    function buildTree() {
      treeNodes = 0; newCell(0, 0, 0, 1);
      for (const i of linked) {
        const x = pos[3 * i], y = pos[3 * i + 1], z = pos[3 * i + 2];
        let q = 0, depth = 0;
        for (;;) {
          if (!mask[q]) {
            if (point[q] < 0 && head[q] < 0) { point[q] = i; break; }
            // Coincident points stop subdividing at MAX_DEPTH and share a list.
            if (depth >= MAX_DEPTH) { next[i] = head[q]; head[q] = i; break; }
            const old = point[q]; point[q] = -1;
            point[childCell(q, octant(q, pos[3 * old], pos[3 * old + 1], pos[3 * old + 2]))] = old;
          }
          const o = octant(q, x, y, z);
          if (mask[q] & (1 << o)) { q = child[q * 8 + o]; depth++; continue; }
          point[childCell(q, o)] = i;
          break;
        }
      }
      // Children are allocated after their parent, so a reverse pass aggregates.
      for (let q = treeNodes - 1; q >= 0; q--) {
        let m = 0, sx = 0, sy = 0, sz = 0;
        if (point[q] >= 0) { const i = point[q]; m++; sx += pos[3 * i]; sy += pos[3 * i + 1]; sz += pos[3 * i + 2]; }
        for (let i = head[q]; i >= 0; i = next[i]) { m++; sx += pos[3 * i]; sy += pos[3 * i + 1]; sz += pos[3 * i + 2]; }
        for (let o = 0; o < 8; o++) if (mask[q] & (1 << o)) { const c = child[q * 8 + o], w = mass[c]; m += w; sx += mx[c] * w; sy += my[c] * w; sz += mz[c] * w; }
        mass[q] = m; if (m) { mx[q] = sx / m; my[q] = sy / m; mz[q] = sz / m; }
      }
    }
    function pointForce(i, j, x, y, z) {
      const px = x - pos[3 * j], py = y - pos[3 * j + 1], pz = z - pos[3 * j + 2], d2 = px * px + py * py + pz * pz + soft, s = repK / (d2 * Math.sqrt(d2));
      fx[i] += px * s; fy[i] += py * s; fz[i] += pz * s;
    }
    function repulse(i) {
      const x = pos[3 * i], y = pos[3 * i + 1], z = pos[3 * i + 2];
      let top = 0; stack[0] = 0;
      while (top >= 0) {
        const q = stack[top--], m = mass[q]; if (!m) continue;
        if (mask[q]) {
          const dx = x - mx[q], dy = y - my[q], dz = z - mz[q], d2 = dx * dx + dy * dy + dz * dz + soft, w = 2 * half[q];
          const inside = Math.abs(x - cx[q]) <= half[q] && Math.abs(y - cy[q]) <= half[q] && Math.abs(z - cz[q]) <= half[q];
          if (!inside && w * w < THETA * THETA * d2) { const s = repK * m / (d2 * Math.sqrt(d2)); fx[i] += dx * s; fy[i] += dy * s; fz[i] += dz * s; continue; }
          if (top + 9 >= stack.length) { const grown = new Int32Array(stack.length * 2); grown.set(stack); stack = grown; }
          for (let o = 0; o < 8; o++) if (mask[q] & (1 << o)) stack[++top] = child[q * 8 + o];
          continue;
        }
        if (point[q] >= 0 && point[q] !== i) pointForce(i, point[q], x, y, z);
        for (let j = head[q]; j >= 0; j = next[j]) if (j !== i) pointForce(i, j, x, y, z);
      }
    }
    function step(count) {
      const reps = Math.max(0, count | 0);
      for (let rep = 0; rep < reps && !settled; rep++) {
        buildTree();
        for (const i of linked) { fx[i] = 0; fy[i] = 0; fz[i] = 0; }
        for (const i of linked) if (free[i]) repulse(i);
        // A spring along every real link; hubs' links are softer so a hub's
        // many neighbours spread around it instead of collapsing onto it.
        for (let e = 0; e < E; e++) {
          const a = ea[e], b = eb[e]; if (!free[a] && !free[b]) continue;
          const dx = pos[3 * b] - pos[3 * a], dy = pos[3 * b + 1] - pos[3 * a + 1], dz = pos[3 * b + 2] - pos[3 * a + 2];
          const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1e-9, s = springK[e] * (d - rest) / d;
          fx[a] += dx * s; fy[a] += dy * s; fz[a] += dz * s; fx[b] -= dx * s; fy[b] -= dy * s; fz[b] -= dz * s;
        }
        // Rank-preserving radial fill: the k-th innermost linked note is
        // pulled toward the radius that the k-th note of a uniformly filled
        // ball would have. Radial order, and so locality, is kept.
        for (let k = 0; k < M; k++) { const i = linked[k]; radius[k] = Math.hypot(pos[3 * i], pos[3 * i + 1], pos[3 * i + 2]); byRadius[k] = k; }
        byRadius.sort((a, b) => radius[a] - radius[b] || a - b);
        for (let r = 0; r < M; r++) targetRadius[linked[byRadius[r]]] = INNER * Math.cbrt((r + 0.5) / M);
        for (let k = 0; k < M; k++) {
          const i = linked[k]; if (!free[i]) continue;
          const r = radius[k] || 1e-9, s = RADIAL * (targetRadius[i] - r) / r;
          fx[i] += pos[3 * i] * s; fy[i] += pos[3 * i + 1] * s; fz[i] += pos[3 * i + 2] * s;
        }
        const cooling = Math.max(0.05, 1 - iterations / maxIterations), damping = 0.7;
        let energy = 0;
        for (const i of linked) {
          if (!free[i]) continue;
          const k = 3 * i;
          let vx = (vel[k] + fx[i] * cooling) * damping, vy = (vel[k + 1] + fy[i] * cooling) * damping, vz = (vel[k + 2] + fz[i] * cooling) * damping;
          const speed = Math.sqrt(vx * vx + vy * vy + vz * vz);
          if (speed > speedCap) { const q = speedCap / speed; vx *= q; vy *= q; vz *= q; }
          let x = pos[k] + vx, y = pos[k + 1] + vy, z = pos[k + 2] + vz;
          const r = Math.sqrt(x * x + y * y + z * z);
          if (r > INNER) {
            const q = INNER / r; x *= q; y *= q; z *= q;
            const out = (vx * x + vy * y + vz * z) / (INNER * INNER);
            if (out > 0) { vx -= out * x; vy -= out * y; vz -= out * z; }
          }
          vel[k] = vx; vel[k + 1] = vy; vel[k + 2] = vz; pos[k] = x; pos[k + 1] = y; pos[k + 2] = z;
          energy += Math.min(speed, speedCap);
        }
        iterations++;
        if (iterations >= 40 && energy / freeCount < 0.004 * spacing) stableSteps++; else stableSteps = 0;
        if (stableSteps >= 12 || iterations >= maxIterations) settled = true;
      }
      return positionsOut();
    }
    const output = new Float32Array(3 * n), degrees = new Uint32Array(n);
    for (let c = 0; c < n; c++) degrees[canon[c]] = deg[c];
    function positionsOut() {
      for (let c = 0; c < n; c++) { const i = canon[c]; output[3 * i] = pos[3 * c]; output[3 * i + 1] = pos[3 * c + 1]; output[3 * i + 2] = pos[3 * c + 2]; }
      return output;
    }
    positionsOut();
    return {
      step, degrees,
      get positions() { return positionsOut(); },
      get settled() { return settled; },
      get iterations() { return iterations; },
      get freeCount() { return freeCount; },
      get fullSolve() { return fullSolve; },
      stats() { return densityStats(positionsOut(), degrees); },
    };
  }
  return { createSolver, surfaceSlots, shellSlots, densityStats, hilbertAxes };
}

function workerBootstrap(makeApi) {
  const api = makeApi(); let solver = null, paused = false, timer = null, lastSent = 0, revision = -1, pool = [];
  function replyError(rev, error) { self.postMessage({ type: 'error', revision: rev, error: String(error && error.message || error) }); }
  function schedule() { if (timer === null && !paused) timer = setTimeout(runSlice, 0); }
  function post() {
    let buffer = pool.pop(); if (!buffer || buffer.byteLength !== solver.positions.byteLength) buffer = new ArrayBuffer(solver.positions.byteLength);
    new Float32Array(buffer).set(solver.positions);
    self.postMessage({ type: 'positions', revision, positions: buffer, settled: solver.settled, iterations: solver.iterations, free: solver.freeCount, stats: solver.stats() }, [buffer]);
    lastSent = Date.now();
  }
  function runSlice() {
    timer = null; if (!solver || paused) return;
    try {
      const started = Date.now();
      do { solver.step(1); } while (!solver.settled && Date.now() - started < 7);
      if (Date.now() - lastSent >= 67 || solver.settled) post();
      if (!solver.settled) schedule();
    } catch (error) { const failedRevision = revision; solver = null; replyError(failedRevision, error); }
  }
  self.onmessage = function (event) {
    const m = event.data || {};
    if (m.type === 'pause') { paused = true; if (timer !== null) { clearTimeout(timer); timer = null; } return; }
    if (m.type === 'resume') { paused = false; schedule(); return; }
    if (m.type === 'recycle' && m.buffer instanceof ArrayBuffer) { pool.push(m.buffer); if (pool.length > 3) pool.shift(); return; }
    if (m.type !== 'solve') return;
    const incomingRevision = Number(m.revision);
    if (!Number.isFinite(incomingRevision)) { replyError(m.revision, new Error('revision must be a finite number')); return; }
    if (incomingRevision < revision) return;
    revision = incomingRevision; paused = false;
    try { solver = api.createSolver(m); schedule(); } catch (error) { solver = null; replyError(revision, error); }
  };
}

const api = apiFactory();
module.exports = { createSolver: api.createSolver, surfaceSlots: api.surfaceSlots, shellSlots: api.shellSlots, densityStats: api.densityStats,
  hilbertAxes: api.hilbertAxes, workerSource() { return '(' + workerBootstrap.toString() + ')(' + apiFactory.toString() + ')'; } };
