'use strict';

// GLSL sources. Points (notes) and ribbons (links) share the slow radial
// "breathing" field from field.js so CPU picking matches what is drawn.
const Field = require('./field');

// aRing selects the sprite: 0 = a note (core and halo), 1 = a solid ring,
// 2 = a dashed ring (the advisor marks drawn around a note).
const POINT_VS = `
attribute vec3 aPosition;
attribute vec4 aColor;
attribute float aSize;
attribute float aShell;
attribute float aRing;
uniform mat4 uProjection, uView;
uniform vec2 uViewport;
uniform vec3 uEyePos;
uniform float uTime;
varying vec4 vColor;
varying float vSize;
varying float vRing;
${Field.glsl}
void main() {
  vec3 p = neuralField(aPosition, uTime);
  vec4 v = uView * vec4(p, 1.0);
  gl_Position = uProjection * v;
  float dist = max(-v.z, 0.001);
  gl_PointSize = clamp(aSize * uProjection[1][1] * uViewport.y * 0.5 / dist / 0.7, 2.5, 96.0);
  vSize = gl_PointSize;
  vRing = aRing;
  float front = dot(normalize(p + vec3(0.000001)), normalize(uEyePos));
  float shellDepth = mix(0.22, 0.92, smoothstep(-0.45, 0.7, front));
  float innerDepth = clamp(1.0 - (dist - length(uEyePos) + 1.0) * 0.3, 0.35, 1.0);
  vColor = vec4(aColor.rgb, aColor.a * mix(innerDepth, shellDepth, aShell));
}`;

const POINT_FS = `
precision mediump float;
varying vec4 vColor;
varying float vSize;
varying float vRing;
uniform float uAlphaMult;
void main() {
  vec2 q = gl_PointCoord - vec2(0.5);
  float d = length(q) * 2.0;
  float aa = min(1.4 / vSize, 0.3);
  float a;
  if (vRing > 0.5) {
    float band = 1.0 - smoothstep(0.09, 0.09 + aa, abs(d - 0.8));
    if (vRing > 1.5) band *= step(0.0, sin(atan(q.y, q.x) * 8.0));
    a = band * vColor.a * uAlphaMult;
  } else {
    float core = 1.0 - smoothstep(0.70 - aa, 0.70 + aa, d);
    float halo = 0.13 * exp(-7.0 * d * d) * (1.0 - smoothstep(0.85, 1.0, d));
    a = (core + halo) * vColor.a * uAlphaMult;
  }
  gl_FragColor = vec4(vColor.rgb * a, a);
}`;

// One instance per link: a cubic Bezier (aP0..aP3) evaluated on the GPU and
// expanded into a screen-space ribbon. aTS = (t along the curve, side +-1).
// aColA = endpoint A rgb + alpha; aColB = endpoint B rgb + half width (px).
const LINE_VS = `
attribute vec2 aTS;
attribute vec3 aP0, aP1, aP2, aP3;
attribute vec4 aColA, aColB;
uniform mat4 uProjection, uView;
uniform vec2 uViewport;
uniform float uDPR, uWidthMult, uTime;
varying vec4 vColor;
varying float vFog, vAcross, vCore;
${Field.glsl}
void main() {
  float t = aTS.x, it = 1.0 - t;
  vec3 raw = it * it * it * aP0 + 3.0 * it * it * t * aP1 + 3.0 * it * t * t * aP2 + t * t * t * aP3;
  vec3 tangent = 3.0 * it * it * (aP1 - aP0) + 6.0 * it * t * (aP2 - aP1) + 3.0 * t * t * (aP3 - aP2);
  tangent = length(tangent) > 0.000001 ? normalize(tangent) : vec3(1.0, 0.0, 0.0);
  vec3 p = neuralField(raw, uTime);
  vec4 v = uView * vec4(p, 1.0);
  vec4 c0 = uProjection * v;
  vec4 c1 = uProjection * uView * vec4(neuralField(raw + tangent * 0.005, uTime), 1.0);
  vec2 halfVp = uViewport * 0.5;
  vec2 d = (c1.xy / c1.w - c0.xy / c0.w) * halfVp;
  vec2 normal = length(d) > 0.000001 ? normalize(vec2(-d.y, d.x)) : vec2(0.0, 1.0);
  float eyeDistance = length(uView[3].xyz);
  float depthScale = clamp(eyeDistance / max(-v.z, 0.01), 0.7, 1.35);
  float coreHalf = max(abs(aColB.w) * uDPR * uWidthMult * depthScale, 0.45);
  float halfW = coreHalf + 0.85 * uDPR;
  c0.xy += normal * aTS.y * halfW / halfVp * c0.w;
  gl_Position = c0;
  vAcross = aTS.y; vCore = coreHalf / halfW;
  // Ribbons taper slightly at both ends so dense hubs do not form bright knots.
  float taper = 0.55 + 0.45 * smoothstep(0.0, 0.09, min(t, 1.0 - t));
  vColor = vec4(mix(aColA.rgb, aColB.rgb, t), aColA.a * taper);
  vFog = clamp(0.50 - (-v.z - eyeDistance) * 0.62, 0.09, 1.0);
}`;

const LINE_FS = `
precision mediump float;
varying vec4 vColor;
varying float vFog, vAcross, vCore;
void main() {
  float d = abs(vAcross);
  float core = 1.0 - smoothstep(vCore * 0.55, vCore + 0.12, d);
  float halo = 0.045 * exp(-5.0 * d * d) * (1.0 - smoothstep(0.8, 1.0, d));
  float a = (core + halo) * vColor.a * vFog;
  gl_FragColor = vec4(vColor.rgb * a, a);
}`;

// Optional bloom: fullscreen-quad blit/blur. uBlurDir = (0,0) is a plain
// copy; otherwise a 5-tap linear-sampled Gaussian along that direction. The
// weights and offsets are the well-known values from D. Rakos, "Efficient
// Gaussian blur with linear sampling" (2010). See THIRD_PARTY.md.
const QUAD_VS = `
attribute vec2 aQuadPos;
varying vec2 vUv;
void main() {
  vUv = aQuadPos * 0.5 + 0.5;
  gl_Position = vec4(aQuadPos, 0.0, 1.0);
}`;

const QUAD_FS = `
precision mediump float;
uniform sampler2D uTex;
uniform vec2 uBlurDir;
uniform float uIntensity;
varying vec2 vUv;
void main() {
  vec4 sum;
  if (uBlurDir.x == 0.0 && uBlurDir.y == 0.0) {
    sum = texture2D(uTex, vUv);
  } else {
    sum  = texture2D(uTex, vUv) * 0.227027;
    sum += texture2D(uTex, vUv + uBlurDir * 1.384615) * 0.316216;
    sum += texture2D(uTex, vUv - uBlurDir * 1.384615) * 0.316216;
    sum += texture2D(uTex, vUv + uBlurDir * 3.230769) * 0.070270;
    sum += texture2D(uTex, vUv - uBlurDir * 3.230769) * 0.070270;
  }
  gl_FragColor = sum * uIntensity;
}`;

// Final composite for the bloom path: scene + blurred glow, then a
// hue-preserving exponential tone map on the brightest channel so dense
// regions do not clip to white.
const COMPOSITE_FS = `
precision mediump float;
uniform sampler2D uScene;
uniform sampler2D uBloom;
uniform float uBloomK;
uniform float uExposure;
varying vec2 vUv;
void main() {
  vec3 c = texture2D(uScene, vUv).rgb + texture2D(uBloom, vUv).rgb * uBloomK;
  float m = max(max(c.r, c.g), c.b);
  vec3 o = m > 1e-5 ? c * ((1.0 - exp(-uExposure * m)) / m) : vec3(0.0);
  gl_FragColor = vec4(o, 1.0);
}`;

module.exports = { POINT_VS, POINT_FS, LINE_VS, LINE_FS, QUAD_VS, QUAD_FS, COMPOSITE_FS };
