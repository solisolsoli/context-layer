'use strict';

// Render plan for links. Every drawn ribbon is one real, resolved link from
// the model's edge map; nothing here invents or drops edges.
const Palette = require('./palette');

function degreeColor(node) { return Palette.styleForDegree(node.degree).color; }

function createEdgePlanBuilder(model, colorForNode) {
  const colorOf = colorForNode || degreeColor;
  const plan = { n: 0, na: [], nb: [], key: [], baseAlpha: [], width: [], colA: [], colB: [], incident: new Map(), groups: [], groupOf: [] };
  const groups = new Map();
  function addEdge(e) {
    const a = model.nodes.get(e.a), b = model.nodes.get(e.b); if (!a || !b) return;
    const i = plan.n++, maxDegree = Math.max(a.degree, b.degree);
    plan.na.push(a); plan.nb.push(b); plan.key.push(model.edgeKey(a.path, b.path));
    // Busy hubs get fainter individual ribbons so they do not saturate.
    plan.width.push(1.5); plan.baseAlpha.push(0.155 / (1 + Math.sqrt(maxDegree) * 0.09));
    plan.colA.push(colorOf(a).slice(0, 3));
    plan.colB.push(colorOf(b).slice(0, 3));
    for (const n of [a, b]) { if (!plan.incident.has(n.path)) plan.incident.set(n.path, []); plan.incident.get(n.path).push(i); }
    // Links with similar position and direction share a group; the group only
    // nudges their curvature toward a common path, never their endpoints.
    const p = a.pos, q = b.pos, d = q.map((v, j) => v - p[j]);
    const len = Math.hypot(...d) || 1;
    const key = p.map((v, j) => Math.floor((v + q[j]) * 1.6)).join(',') + ':' + d.map(v => Math.round(v / len * 2)).join(',');
    let g = groups.get(key);
    if (!g) { g = { edges: [], cp: new Float32Array(6) }; groups.set(key, g); }
    g.edges.push(i); plan.groupOf.push(g);
  }
  function finish() { plan.groups = Array.from(groups.values()); return plan; }
  return { plan, addEdge, finish };
}

function buildEdgePlan(model, colorForNode) {
  const builder = createEdgePlanBuilder(model, colorForNode);
  for (const edge of model.edgeMap.values()) builder.addEdge(edge);
  return builder.finish();
}

// Same result as buildEdgePlan, but yields to the event loop every `sliceMs`
// so a large vault does not freeze the UI while the view opens.
async function buildEdgePlanAsync(model, colorForNode, options = {}) {
  const cancelled = options.cancelled || (() => false);
  const sliceMs = Math.max(1, options.sliceMs || 5), builder = createEdgePlanBuilder(model, colorForNode);
  let chunk = performance.now(), slices = 0;
  for (const edge of model.edgeMap.values()) {
    if (cancelled()) return null;
    builder.addEdge(edge);
    if (performance.now() - chunk >= sliceMs) {
      slices++;
      await new Promise(resolve => setTimeout(resolve, 0));
      if (cancelled()) return null;
      chunk = performance.now();
    }
  }
  if (cancelled()) return null;
  const plan = builder.finish();
  plan.slices = slices;
  return plan;
}

// Writes the two inner control points of a gently bowed curve from a to b
// (outward from the sphere centre) at out[offset..offset+5].
function bowedControlPoints(a, b, out, offset) {
  let x = a[0] + b[0], y = a[1] + b[1], z = a[2] + b[2], l = Math.hypot(x, y, z);
  if (l < 0.05) { x = b[2] - a[2]; y = 0.3; z = a[0] - b[0]; l = Math.hypot(x, y, z) || 1; }
  const bow = 0.055 * Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]) / l;
  for (let c = 0; c < 3; c++) {
    const bend = (c === 0 ? x : c === 1 ? y : z) * bow;
    out[offset + c] = a[c] + (b[c] - a[c]) / 3 + bend;
    out[offset + 3 + c] = a[c] + 2 * (b[c] - a[c]) / 3 + bend;
  }
}

// Full cubic (12 floats: P0, P1, P2, P3) between two positions.
function curveBetween(a, b, out, o) {
  bowedControlPoints(a, b, out, o + 3);
  for (let c = 0; c < 3; c++) { out[o + c] = a[c]; out[o + 9 + c] = b[c]; }
}

function updateGuides(plan) {
  const cp = new Float32Array(6);
  for (const g of plan.groups) {
    g.cp.fill(0);
    for (const i of g.edges) { bowedControlPoints(plan.na[i].pos, plan.nb[i].pos, cp, 0); for (let c = 0; c < 6; c++) g.cp[c] += cp[c] / g.edges.length; }
  }
}

function edgeControlPoints(plan, i, out, o) {
  const g = plan.groupOf[i];
  curveBetween(plan.na[i].pos, plan.nb[i].pos, out, o);
  if (g.edges.length > 1) for (let c = 0; c < 6; c++) out[o + 3 + c] += Math.max(-0.025, Math.min(0.025, (g.cp[c] - out[o + 3 + c]) * 0.18));
}

// Point on a cubic stored as 12 floats at ctrl[o..o+11].
function cubicPoint(ctrl, o, t, out, oo) {
  const u = 1 - t;
  for (let c = 0; c < 3; c++) {
    out[oo + c] = u * u * u * ctrl[o + c] + 3 * u * u * t * ctrl[o + 3 + c] + 3 * u * t * t * ctrl[o + 6 + c] + t * t * t * ctrl[o + 9 + c];
  }
  return out;
}

const SPAWN_MS = 900;

// Current alpha of link i. New links flash briefly; links outside a focused
// region and links outside an active retrieval overlay are dimmed.
function edgeAlphaNow(plan, i, focus, spawn, now, dim = 1) {
  let a = plan.baseAlpha[i] * dim; const born = spawn && spawn[plan.key[i]];
  edgeAlphaNow.animating = !!born && now - born < SPAWN_MS;
  if (edgeAlphaNow.animating) a *= 1 + 0.65 * (1 - (now - born) / SPAWN_MS);
  if (focus && plan.na[i].region?.key !== focus && plan.nb[i].region?.key !== focus) a *= 0.2;
  return a;
}

module.exports = { buildEdgePlan, buildEdgePlanAsync, edgeControlPoints, curveBetween, cubicPoint, updateGuides, edgeAlphaNow };
