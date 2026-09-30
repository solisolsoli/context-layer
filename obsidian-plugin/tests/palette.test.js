'use strict';

// Degree palette (src/palette.js): fixed anchors, monotonic size/brightness,
// transitions, interruption, hidden-tab pause and reduced motion.

const assert = require('node:assert/strict');
const { ANCHORS, styleForDegree, createAnimator } = require('../src/palette.js');

const hex = rgb => '#' + rgb.map(x => Math.round(x * 255).toString(16).padStart(2, '0')).join('').toUpperCase();
const luminance = rgb => .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];

for (const anchor of ANCHORS) {
  const style = styleForDegree(anchor.degree);
  assert.equal(hex(style.color), anchor.hex);
  assert.equal(style.size, anchor.size);
}
assert.equal(styleForDegree(0).size, styleForDegree(1).size, 'degree 0 and 1 retain equal dot size');
let previous = styleForDegree(0);
for (const degree of [1, 2, 3, 4, 7, 16, 64, 255, 256, 955, 1024, 4096]) {
  const current = styleForDegree(degree);
  assert(current.size >= previous.size, 'point diameter is monotonic by degree');
  assert(luminance(current.color) >= luminance(previous.color), 'palette luminance is monotonic by degree');
  assert(current.size <= .018 && current.color.every(Number.isFinite));
  previous = current;
}
const stable = styleForDegree(64);
styleForDegree(1000000);
assert.deepEqual(styleForDegree(64), stable, 'an unrelated larger hub cannot renormalize a node');

const animator = createAnimator(), node = { id: 'stable-id', path: 'old.md', degree: 1 };
assert.equal(animator.refresh([node], 0), 0, 'initial palette state snaps');
assert.deepEqual(animator.get(node).color, styleForDegree(1).color);
node.degree = 4;
assert.equal(animator.refresh([node], 100), 1, 'new degree starts one transition');
animator.tick(425);
const halfway = animator.get(node), midway = halfway.color.slice(), midwaySize = halfway.size;
assert(halfway.active && midwaySize > .009 && midwaySize < .0108);
node.path = 'renamed.md';
node.degree = 16;
assert.equal(animator.refresh([node], 425), 1, 'interrupted transition restarts from current display');
assert.deepEqual(animator.get(node).fromColor, midway);
assert.equal(animator.get(node).fromSize, midwaySize);
animator.tick(1100);
assert.equal(animator.activeCount(), 0);
assert.deepEqual(animator.get(node).color, styleForDegree(16).color);
node.degree = 1;
animator.refresh([node], 1200);
animator.tick(1850);
assert.deepEqual(animator.get(node).color, styleForDegree(1).color, 'degree decreases animate to the low anchor');

const hidden = { id: 'same-id', path: 'hidden.md', degree: 1 };
animator.refresh([hidden], 0, { visible: true });
animator.setVisible(false, 100);
hidden.degree = 4;
animator.refresh([hidden], 200, { visible: false });
assert.equal(animator.get(hidden).startedAt, null, 'hidden graph changes defer transition start');
animator.tick(500, { visible: false });
animator.setVisible(true, 1000);
assert.equal(animator.get(hidden).startedAt, 1000, 'hidden transition starts on resume');
animator.tick(1325);
const resumedSize = animator.get(hidden).size;
assert(resumedSize > .009 && resumedSize < .0108, 'hidden duration does not advance transition');
animator.setVisible(false, 1350); animator.setVisible(true, 2350); animator.tick(2550);
assert(animator.get(hidden).active, 'a hidden interval pauses the active transition');

hidden.degree = 64;
animator.refresh([hidden], 2600, { reducedMotion: true });
assert.equal(animator.activeCount(), 0, 'reduced motion snaps degree changes');
assert.deepEqual(animator.get(hidden).color, styleForDegree(64).color);
const recreated = { id: 'same-id', path: 'hidden.md', degree: 4 };
animator.refresh([recreated], 2700, { reducedMotion: false });
assert.equal(animator.activeCount(), 0, 'delete and recreate with same id receives fresh snapped state');
assert.deepEqual(animator.get(recreated).color, styleForDegree(4).color);
assert.equal(animator.get(hidden).color[0], styleForDegree(64).color[0], 'old deleted object state remains independent');

console.log('palette: PASS (anchors, interpolation, monotonic size/luminance, hub invariance, transitions, interruption, rename identity, hidden pause, reduced motion, recreate identity)');
