'use strict';

// Region and overlay colours under simulated colour-vision deficiency.
// Simulation: Machado, Oliveira and Fernandes (2009), severity 1, applied to
// linear RGB. Difference: CIEDE2000 (Sharma, Wu and Dalal 2005) on CIELAB
// with the D65 white point. Colours are compared as displayed: the notes'
// premultiplied colour over the black stage (alpha 0.55) and the legend dot
// at full strength.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, run } = require('./helpers/harness');
const Regions = require('../src/regions');
const A = require('../src/activation');

const { MACHADO, ciede2000, shown, simulate, lab, minPair } = require('./helpers/color');

test('CIEDE2000 implementation matches the published test pairs', () => {
  // Sharma, Wu and Dalal (2005), Table 1: pairs 1, 7, 17, 25 and 34.
  const pairs = [
    [[50.0, 2.6772, -79.7751], [50.0, 0.0, -82.7485], 2.0425],
    [[50.0, 0.0, 0.0], [50.0, -1.0, 2.0], 2.3669],
    [[50.0, 2.5, 0.0], [73.0, 25.0, -18.0], 27.1492],
    [[60.2574, -34.0099, 36.2677], [60.4626, -34.1751, 39.4387], 1.2644],
    [[90.8027, -2.0831, 1.4410], [91.1528, -1.6435, 0.0447], 1.4441],
  ];
  for (const [x, y, expected] of pairs) assert.ok(Math.abs(ciede2000(x, y) - expected) < 1e-4, expected);
});

test('region colours stay at least 10 CIEDE2000 apart for every vision model, on notes and in the legend', () => {
  const colours = Regions.REGION_COLORS.map(hex => ({ name: hex, rgb: Regions.hexToRgb(hex) }))
    .concat([{ name: 'Other ' + Regions.OTHER_COLOR, rgb: Regions.hexToRgb(Regions.OTHER_COLOR) }]);
  const report = {};
  for (const alpha of [0.55, 1]) for (const model of Object.keys(MACHADO)) {
    const best = minPair(colours.map(c => ({ name: c.name, rgb: shown(c.rgb, alpha) })), model);
    report[model + '@' + alpha] = +best.d.toFixed(1) + ' (' + best.a + ' / ' + best.b + ')';
    assert.ok(best.d >= 10, `${model} at alpha ${alpha}: ${best.a} vs ${best.b} only ${best.d.toFixed(2)}`);
  }
  console.log('  smallest region pair distance: ' + JSON.stringify(report));
});

test('the overlay styles stay apart under simulated colour-vision deficiency', () => {
  const style = e => { const s = A.overlayNodeStyle(e); return shown(s.color, s.alpha); };
  const colours = [
    { name: 'seed, in packet', rgb: style({ activation: 1, role: 'seed', selected: true }) },
    { name: 'hop 0.6, in packet', rgb: style({ activation: 0.6, role: 'hop', selected: true }) },
    { name: 'hop 0.3, reached only', rgb: style({ activation: 0.3, role: 'hop', selected: false }) },
    { name: 'seed, reached only', rgb: style({ activation: 1, role: 'seed', selected: false }) },
  ];
  for (const model of Object.keys(MACHADO)) {
    const best = minPair(colours, model);
    assert.ok(best.d >= 15, `${model}: ${best.a} vs ${best.b} only ${best.d.toFixed(2)}`);
  }
});

test('CSS region classes use exactly the palette the renderer uses', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'styles.css'), 'utf8');
  Regions.REGION_COLORS.forEach((hex, i) => assert.match(css, new RegExp('--nb-region-' + i + ': ' + hex + ';'), 'region ' + i));
  assert.match(css, new RegExp('--nb-region-other: ' + Regions.OTHER_COLOR + ';'));
  assert.equal((css.match(/--nb-region-\d+:/g) || []).length, Regions.REGION_COLORS.length, 'no stale colour in the stylesheet');
  for (let i = 0; i < Regions.REGION_COLORS.length; i++) assert.match(css, new RegExp('\\.nb-region-' + i + ' \\{ background-color: var\\(--nb-region-' + i + '\\); \\}'));
});

run('palette-cvd');
