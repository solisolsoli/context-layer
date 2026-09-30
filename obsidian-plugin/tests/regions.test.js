'use strict';

// Region generalization (src/regions.js): regions come only from the vault's
// own folders or tags, colours are assigned deterministically by rank, and no
// folder or tag names are built into the plugin.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, run } = require('./helpers/harness');
const { file, generateVault, mulberry } = require('./helpers/fixtures');
const Regions = require('../src/regions');

const folderKeys = paths => paths.map(p => Regions.regionKeyForFile(file(p), 'folder'));

test('folder regions are the top-level folder; root notes share one region', () => {
  assert.deepEqual(folderKeys(['a.md', 'x/b.md', 'x/y/c.md', 'Z z/d.md']), ['', 'x', 'x', 'Z z']);
  const table = Regions.buildRegionTable(folderKeys(['a.md', 'x/b.md', 'x/y/c.md']), { source: 'folder' });
  assert.deepEqual(table.regions.map(r => [r.key, r.name, r.count]), [['x', 'x', 2], ['', '(vault root)', 1]]);
});

test('region names are exactly the vault\'s own folder names, whatever they are', () => {
  const random = mulberry(42);
  for (let round = 0; round < 20; round++) {
    const names = Array.from({ length: 1 + Math.floor(random() * 8) }, (_, i) => 'f' + round + '-' + i + '-' + Math.floor(random() * 1e6).toString(36));
    const paths = [];
    names.forEach((n, i) => { for (let k = 0; k <= i; k++) paths.push(n + '/note-' + k + '.md'); });
    const table = Regions.buildRegionTable(folderKeys(paths));
    assert.deepEqual(new Set(table.regions.map(r => r.name)), new Set(names), 'every region is a folder of this vault and nothing else');
    assert.equal(table.other.count, 0);
  }
});

test('colours are assigned by rank (count, then name) and are independent of input order', () => {
  const keys = ['b', 'a', 'b', 'c', 'c', 'c', 'a', 'd'];
  const t1 = Regions.buildRegionTable(keys);
  const t2 = Regions.buildRegionTable(keys.slice().reverse());
  assert.deepEqual(t1.regions.map(r => r.key), ['c', 'a', 'b', 'd'], 'ties broken alphabetically');
  assert.deepEqual(t1.regions.map(r => r.color), Regions.REGION_COLORS.slice(0, 4));
  assert.deepEqual(t1.regions, t2.regions);
  for (const r of t1.regions) assert.deepEqual(r.rgb, Regions.hexToRgb(r.color));
});

test('beyond the palette size, the smallest regions share "Other"', () => {
  const keys = [];
  for (let i = 0; i < 15; i++) for (let k = 0; k < 20 - i; k++) keys.push('folder-' + String(i).padStart(2, '0'));
  const table = Regions.buildRegionTable(keys);
  assert.equal(table.regions.length, Regions.REGION_COLORS.length);
  assert.equal(table.other.groups, 15 - Regions.REGION_COLORS.length);
  assert.equal(table.lookup('folder-14'), table.other);
  assert.equal(table.lookup('folder-00').color, Regions.REGION_COLORS[0]);
  assert.equal(table.regions.length + table.other.groups, new Set(keys).size);
  assert.equal(new Set(Regions.REGION_COLORS).size, Regions.REGION_COLORS.length, 'palette colours are distinct');
  assert.ok(!Regions.REGION_COLORS.includes(Regions.OTHER_COLOR));
});

test('generated vault: one region per generated folder plus the root', () => {
  const vault = generateVault({ notes: 800, seed: 5, folders: 5, attachments: 0 });
  const table = Regions.buildRegionTable(vault.files.map(f => Regions.regionKeyForFile(f, 'folder')));
  assert.deepEqual(new Set(table.regions.map(r => r.key)), new Set([...vault.folders, '']));
});

test('tag regions use the first tag: frontmatter before inline, top-level segment, lower case', () => {
  const caches = {
    'a.md': { frontmatter: { tags: ['Project/Alpha', 'misc'] }, tags: [{ tag: '#inline' }] },
    'b.md': { frontmatter: { tags: 'draft, later' } },
    'c.md': { tags: [{ tag: '#Topic/Sub' }, { tag: '#other' }] },
    'd.md': { frontmatter: { tag: '#single' } },
    'e.md': {},
    'f.md': { frontmatter: { tags: [null, 42, '   '] }, tags: [{ tag: 17 }] },
  };
  const mc = { getFileCache: f => caches[f.path] || null };
  const keys = Object.keys(caches).concat(['missing.md']).map(p => Regions.regionKeyForFile(file(p), 'tag', mc));
  assert.deepEqual(keys, ['#project', '#draft', '#topic', '#single', '', '', '']);
  assert.equal(Regions.regionKeyForFile(file('a.md'), 'tag', { getFileCache() { throw new Error('cache not ready'); } }), '', 'a throwing cache is tolerated');
  assert.equal(Regions.buildRegionTable([''], { source: 'tag' }).regions[0].name, '(untagged)');
});

test('the plugin source contains no built-in folder, tag or region names', () => {
  const src = fs.readFileSync(path.join(__dirname, '../src/regions.js'), 'utf8').replace(/(^|\s)\/\/.*$/gm, '$1');
  const strings = Array.from(src.matchAll(/'((?:[^'\\\n]|\\.)*)'/g), m => m[1]);
  const allowed = new Set(['use strict', 'folder', 'tag', 'tags', '', '(untagged)', '(vault root)', 'Other', '#', '\\u0000other', 'object', 'string', 'function', '/']);
  const unexpected = strings.filter(s => !allowed.has(s) && !/^#[0-9A-F]{6}$/.test(s));
  assert.deepEqual(unexpected, [], 'only colours, source names and generic labels appear as string literals');
});

run('regions');
