'use strict';
// Shared radial Gaussian field. CPU picking and both GPU programs use these
// exact constants. Sum of amplitudes is 0.114R, below the 0.12R contract.
const centers = [[0.8, 0.5, 0.3], [-0.5, 0.6, -0.6], [0.2, -0.8, 0.5]];
for (const v of centers) { const l = Math.hypot(...v); for (let i = 0; i < 3; i++) v[i] /= l; }
const frequencies = [Math.PI / 100, Math.PI / 75, Math.PI / 150];
const phases = [0, 2, 4];
function displace(x, y, z, time, out = [0, 0, 0]) {
  const r = Math.hypot(x, y, z);
  if (r < 1e-7) { out[0] = x; out[1] = y; out[2] = z; return out; }
  let field = 0;
  for (let i = 0; i < 3; i++) {
    const c = centers[i], dx = x / r - c[0], dy = y / r - c[1], dz = z / r - c[2];
    field += .038 * Math.exp(-2.2 * (dx * dx + dy * dy + dz * dz)) * Math.sin(time * frequencies[i] + phases[i]);
  }
  const scale = 1 + field * (.35 + .65 * Math.min(r * r, 1));
  out[0] = x * scale; out[1] = y * scale; out[2] = z * scale; return out;
}
const f = x => x.toFixed(12);
const glsl = `
vec3 neuralField(vec3 p, float time) {
  float r = length(p);
  if (r < 0.0000001) return p;
  vec3 n = p / r;
  float field = 0.0;
  ${centers.map((c, i) => `vec3 d${i} = n - vec3(${c.map(f).join(',')});
  field += 0.038 * exp(-2.2 * dot(d${i}, d${i})) * sin(time * ${f(frequencies[i])} + ${f(phases[i])});`).join('\n')}
  return p * (1.0 + field * (0.35 + 0.65 * min(r * r, 1.0)));
}`;
module.exports = { displace, glsl, centers, frequencies, phases };
