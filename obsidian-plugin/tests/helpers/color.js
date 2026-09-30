'use strict';

// Colour maths shared by the palette tests. Simulation: Machado, Oliveira and
// Fernandes (2009), severity 1, applied to linear RGB. Difference: CIEDE2000
// (Sharma, Wu and Dalal 2005) on CIELAB with the D65 white point. See
// THIRD_PARTY.md.

const MACHADO = {
  normal: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
  protan: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  deutan: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
  tritan: [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.303900]],
};
const clamp01 = v => Math.min(1, Math.max(0, v));
const toLinear = c => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
const toSrgb = c => (c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055);

function simulate(rgb, model) {
  const lin = rgb.map(toLinear), m = MACHADO[model];
  return m.map(row => clamp01(toSrgb(clamp01(row[0] * lin[0] + row[1] * lin[1] + row[2] * lin[2]))));
}
function lab(rgb) {
  const [r, g, b] = rgb.map(toLinear);
  const x = (0.412453 * r + 0.357580 * g + 0.180423 * b) / 0.95047;
  const y = 0.212671 * r + 0.715160 * g + 0.072169 * b;
  const z = (0.019334 * r + 0.119193 * g + 0.950227 * b) / 1.08883;
  const f = t => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const fx = f(x), fy = f(y), fz = f(z);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}
function ciede2000([L1, a1, b1], [L2, a2, b2]) {
  const rad = Math.PI / 180, cos = d => Math.cos(d * rad), sin = d => Math.sin(d * rad);
  const cbar = (Math.hypot(a1, b1) + Math.hypot(a2, b2)) / 2;
  const g = 0.5 * (1 - Math.sqrt(cbar ** 7 / (cbar ** 7 + 25 ** 7)));
  const a1p = (1 + g) * a1, a2p = (1 + g) * a2, c1 = Math.hypot(a1p, b1), c2 = Math.hypot(a2p, b2);
  const hue = (b, a) => { if (a === 0 && b === 0) return 0; const h = Math.atan2(b, a) / rad; return h < 0 ? h + 360 : h; };
  const h1 = hue(b1, a1p), h2 = hue(b2, a2p);
  let dh = 0;
  if (c1 * c2 !== 0) { dh = h2 - h1; if (dh > 180) dh -= 360; else if (dh < -180) dh += 360; }
  const dL = L2 - L1, dC = c2 - c1, dH = 2 * Math.sqrt(c1 * c2) * sin(dh / 2);
  const lbar = (L1 + L2) / 2, cbarp = (c1 + c2) / 2;
  let hbar = h1 + h2;
  if (c1 * c2 !== 0) hbar = Math.abs(h1 - h2) > 180 ? (h1 + h2 < 360 ? (h1 + h2 + 360) / 2 : (h1 + h2 - 360) / 2) : (h1 + h2) / 2;
  const t = 1 - 0.17 * cos(hbar - 30) + 0.24 * cos(2 * hbar) + 0.32 * cos(3 * hbar + 6) - 0.20 * cos(4 * hbar - 63);
  const dTheta = 30 * Math.exp(-(((hbar - 275) / 25) ** 2));
  const rc = 2 * Math.sqrt(cbarp ** 7 / (cbarp ** 7 + 25 ** 7));
  const sl = 1 + 0.015 * (lbar - 50) ** 2 / Math.sqrt(20 + (lbar - 50) ** 2), sc = 1 + 0.045 * cbarp, sh = 1 + 0.015 * cbarp * t;
  const rt = -sin(2 * dTheta) * rc;
  return Math.sqrt((dL / sl) ** 2 + (dC / sc) ** 2 + (dH / sh) ** 2 + rt * (dC / sc) * (dH / sh));
}
const shown = (rgb, alpha) => rgb.map(c => clamp01(c * alpha));
function minPair(colours, model) {
  let best = { d: Infinity };
  for (let i = 0; i < colours.length; i++) for (let j = i + 1; j < colours.length; j++) {
    const d = ciede2000(lab(simulate(colours[i].rgb, model)), lab(simulate(colours[j].rgb, model)));
    if (d < best.d) best = { d, a: colours[i].name, b: colours[j].name };
  }
  return best;
}

module.exports = { MACHADO, clamp01, simulate, lab, ciede2000, shown, minPair };
