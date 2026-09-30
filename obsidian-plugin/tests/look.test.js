'use strict';

// The resting look of the graph: a fixed blue-violet to cyan-white scale by
// real degree, point diameter that grows with degree, unlinked notes drawn
// dim, and the overlay colours staying distinct from that base. These tests pin
// the scale so a
// change to it is deliberate.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, run } = require('./helpers/harness');
const { MACHADO, ciede2000, shown, simulate, lab } = require('./helpers/color');
const Palette = require('../src/palette');
const Activation = require('../src/activation');
const Edges = require('../src/edges');

const luminance = rgb => 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
// Every degree from 0 to 64, then every 8th up to past the last anchor.
const DEGREES = [];
for (let d = 0; d <= 1100; d += d < 64 ? 1 : 8) DEGREES.push(d);

test('colour and diameter follow the real degree on one fixed log scale, never the rest of the vault', () => {
  let previous = Palette.styleForDegree(0);
  for (const degree of DEGREES) {
    const now = Palette.styleForDegree(degree);
    assert.ok(now.size >= previous.size - 1e-12, 'diameter never shrinks as degree grows (degree ' + degree + ')');
    assert.ok(luminance(now.color) >= luminance(previous.color) - 1e-9, 'brightness never drops as degree grows (degree ' + degree + ')');
    assert.ok(now.color.every(c => c >= 0 && c <= 1), 'channels stay in range');
    previous = now;
  }
  const low = Palette.styleForDegree(1), mid = Palette.styleForDegree(16), hub = Palette.styleForDegree(1024);
  assert.ok(hub.size >= 1.9 * low.size && hub.size <= 2.1 * low.size, 'a 1024-link hub is drawn about twice as wide as a one-link note');
  assert.ok(mid.size > low.size && mid.size < hub.size, 'a mid-degree note sits between them');
  assert.deepEqual(Palette.styleForDegree(4096), hub, 'the scale is capped at the top anchor');
  // What the scale looks like: blue dominates all the way up; few links read
  // blue-violet, many links cyan, the busiest notes almost white.
  for (const degree of DEGREES) {
    const c = Palette.styleForDegree(degree).color;
    assert.ok(c[2] >= Math.max(c[0], c[1]) - 1e-9, 'blue is the strongest channel at degree ' + degree);
  }
  const l1 = Palette.styleForDegree(1).color;
  assert.ok(l1[0] > l1[1] && l1[2] > l1[0], 'one link: violet-leaning blue (red above green, blue on top)');
  const l64 = Palette.styleForDegree(64).color;
  assert.ok(l64[1] > l64[0] + 0.3, 'sixty-four links: cyan (green well above red)');
  assert.ok(hub.color.every(c => c > 0.85), 'a hub is cyan-white');
});

test('unlinked notes in the outer shell are much dimmer than any linked note', () => {
  assert.equal(Palette.ORPHAN_ALPHA, 0.12);
  assert.equal(Palette.NODE_ALPHA, 0.55);
  assert.ok(Palette.ORPHAN_ALPHA * 4 < Palette.NODE_ALPHA, 'the shell is at least four times fainter than the interior');
  const shell = luminance(shown(Palette.styleForDegree(0).color, Palette.ORPHAN_ALPHA));
  for (const degree of DEGREES.filter(d => d > 0)) {
    const interior = luminance(shown(Palette.styleForDegree(degree).color, Palette.NODE_ALPHA));
    assert.ok(interior > 2 * shell, 'a note with ' + degree + ' links is drawn over twice as bright as a shell note');
  }
  const view = read('src/view.js');
  assert.match(view, /const \{ NODE_ALPHA, ORPHAN_ALPHA \} = Palette;/, 'the view takes both opacities from the palette module');
  assert.match(view, /node\.role === 'orphan' \? ORPHAN_ALPHA : NODE_ALPHA/, 'and uses them for the rest state');
});

test('links are real, 1.5 px wide and coloured from their two notes', () => {
  const nodes = new Map([
    ['a.md', { path: 'a.md', degree: 1, pos: [0, 0, 0] }],
    ['b.md', { path: 'b.md', degree: 1024, pos: [0.5, 0.2, 0.1] }],
  ]);
  const model = { nodes, edgeMap: new Map([['a|b', { a: 'a.md', b: 'b.md' }]]), edgeKey: (x, y) => (x < y ? x + '|' + y : y + '|' + x) };
  const plan = Edges.buildEdgePlan(model);
  assert.equal(plan.n, 1);
  assert.equal(plan.width[0], 1.5);
  assert.deepEqual(plan.colA[0], Palette.styleForDegree(1).color);
  assert.deepEqual(plan.colB[0], Palette.styleForDegree(1024).color);
  assert.ok(plan.baseAlpha[0] > 0 && plan.baseAlpha[0] < 0.155, 'the ribbon of a busy hub is fainter than the base alpha');
});

test('the amber seed stays distinct from the base scale, under simulated colour-vision deficiency too', () => {
  const seed = Activation.overlayNodeStyle({ activation: 1, role: 'seed', selected: true });
  const drawn = shown(seed.color, seed.alpha);
  for (const model of Object.keys(MACHADO)) {
    let worst = Infinity, at = 0;
    for (const degree of DEGREES) {
      const base = shown(Palette.styleForDegree(degree).color, Palette.NODE_ALPHA);
      const d = ciede2000(lab(simulate(drawn, model)), lab(simulate(base, model)));
      if (d < worst) { worst = d; at = degree; }
    }
    assert.ok(worst >= 30, model + ': the seed is only ' + worst.toFixed(1) + ' CIEDE2000 from the base colour of a note with ' + at + ' links');
  }
  const [r, g, b] = seed.color;
  assert.ok(r > g && g > b, 'the seed is warm (red above green above blue), which no base colour is');
  for (const degree of DEGREES) { const c = Palette.styleForDegree(degree).color; assert.ok(c[2] >= c[0], 'no base colour is warm'); }
});

test('while a retrieval is shown, every overlay style is distinct from the dimmed base notes', () => {
  const styles = {
    'seed, in packet': { activation: 1, role: 'seed', selected: true },
    'seed, reached only': { activation: 1, role: 'seed', selected: false },
    'hop 1.0, in packet': { activation: 1, role: 'hop', selected: true },
    'hop 0.6, in packet': { activation: 0.6, role: 'hop', selected: true },
    'hop 0.0, in packet': { activation: 0, role: 'hop', selected: true },
    'hop 0.3, reached only': { activation: 0.3, role: 'hop', selected: false },
    'hop 0.0, reached only': { activation: 0, role: 'hop', selected: false },
  };
  for (const [name, entry] of Object.entries(styles)) {
    const s = Activation.overlayNodeStyle(entry);
    assert.ok(s.sizeMult >= 1.35, name + ' is drawn larger than its degree size (size is a second cue besides colour)');
    const drawn = shown(s.color, s.alpha);
    for (const model of Object.keys(MACHADO)) {
      let worst = Infinity;
      for (const degree of DEGREES) {
        for (const alpha of [Palette.NODE_ALPHA, Palette.ORPHAN_ALPHA]) {
          const base = shown(Palette.styleForDegree(degree).color, alpha * Activation.OVERLAY_NODE_DIM);
          worst = Math.min(worst, ciede2000(lab(simulate(drawn, model)), lab(simulate(base, model))));
        }
      }
      assert.ok(worst >= 15, model + ': ' + name + ' is only ' + worst.toFixed(1) + ' CIEDE2000 from the dimmed base');
    }
  }
  assert.ok(Activation.OVERLAY_NODE_DIM <= 0.25, 'notes outside the retrieval are dimmed to a quarter or less');
});

test('the HUD keeps the quiet uppercase letter-spaced title and the monospace counts line', () => {
  const css = read('styles.css');
  const rule = selector => { const m = css.match(new RegExp('(?:^|\\n)' + selector.replace(/[.]/g, '\\.') + ' \\{([^}]*)\\}')); assert.ok(m, selector + ' rule'); return m[1]; };
  const title = rule('.nb-title');
  assert.match(title, /font-weight: 300;/);
  assert.match(title, /font-size: 13px;/);
  assert.match(title, /letter-spacing: 0\.28em;/);
  assert.match(title, /font-family: var\(--nb-hud-font\);/);
  const counters = css.match(/\.nb-counters,\n\.nb-dev \{([^}]*)\}/);
  assert.ok(counters, 'counts line rule');
  assert.match(counters[1], /font-family: var\(--nb-hud-mono\);/);
  assert.match(counters[1], /font-size: 10\.5px;/);
  assert.match(css, /--nb-hud-mono: "SF Mono", Menlo, var\(--font-monospace, monospace\), monospace;/);
  assert.ok(!/https?:\/\//.test(css), 'local font stacks only');
  const view = read('src/view.js');
  assert.match(view, /text: 'CONTEXT LAYER BRAIN VIEW'/, 'the title is the plugin name, written in capitals');
});

run('look');
