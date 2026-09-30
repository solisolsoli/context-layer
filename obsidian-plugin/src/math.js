'use strict';

// Small numeric helpers and column-major 4x4 matrices (WebGL convention).

function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
function lerp(a, b, t) { return a + (b - a) * t; }

// Returns the angle equivalent to `to` that is closest to `from`, so that a
// camera easing from `from` never spins through extra full turns.
function nearestAngle(from, to) {
  const turn = Math.PI * 2;
  let d = (to - from) % turn;
  if (d > Math.PI) d -= turn;
  if (d < -Math.PI) d += turn;
  return from + d;
}

// Projection and view matrices follow the standard gluPerspective and
// gluLookAt definitions (OpenGL clip space, column-major storage), the same
// conventions used by the gl-matrix library (MIT, Brandon Jones and Colin
// MacKenzie IV). See THIRD_PARTY.md.

function mat4Perspective(out, fovY, aspect, near, far) {
  const cot = 1 / Math.tan(fovY / 2);
  const depth = near - far;
  out.fill(0);
  out[0] = cot / aspect;
  out[5] = cot;
  out[10] = (near + far) / depth;
  out[11] = -1;
  out[14] = (2 * near * far) / depth;
  return out;
}

function normalize3(v, fallback) {
  const len = Math.hypot(v[0], v[1], v[2]);
  return len < 1e-6 ? fallback.slice() : [v[0] / len, v[1] / len, v[2] / len];
}
function cross3(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
function dot3(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

// View matrix for a camera at `eye` looking at `target`. The rows of the
// rotation are the camera's right, up and backward axes.
function mat4LookAt(out, eye, target, up) {
  const back = normalize3([eye[0] - target[0], eye[1] - target[1], eye[2] - target[2]], [0, 0, 1]);
  const right = normalize3(cross3(up, back), [1, 0, 0]);
  const camUp = cross3(back, right);
  for (let col = 0; col < 3; col++) {
    out[col * 4] = right[col];
    out[col * 4 + 1] = camUp[col];
    out[col * 4 + 2] = back[col];
    out[col * 4 + 3] = 0;
  }
  out[12] = -dot3(right, eye);
  out[13] = -dot3(camUp, eye);
  out[14] = -dot3(back, eye);
  out[15] = 1;
  return out;
}

module.exports = { clamp, lerp, nearestAngle, mat4Perspective, mat4LookAt };
