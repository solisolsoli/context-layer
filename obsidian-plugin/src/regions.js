'use strict';

// Regions group notes for the folder palette and the legend. They are derived
// only from the vault itself: either a note's top-level folder or its first
// tag. No folder or tag names are built in.

// Eight colours that stay readable on the black stage and stay apart for
// the common colour-vision deficiencies: every pair, including OTHER_COLOR,
// is at least 10 CIEDE2000 units apart under simulated protanopia,
// deuteranopia and tritanopia (Machado et al. 2009, severity 1), both at
// full strength (legend) and at the notes' 0.55 opacity over black
// (tests/palette-cvd.test.js). The largest regions receive them in rank
// order; everything else shares OTHER_COLOR. Colour is never the only cue:
// the legend names every region and clicking it isolates that region.
const REGION_COLORS = Object.freeze([
  '#E19600', '#A596FF', '#FFFFB4', '#C3D2FF', '#C396B4', '#87D278', '#FFE12D', '#A5FFF0',
]);
const OTHER_COLOR = '#C3C3B4';
const OTHER_KEY = '\u0000other';
const SOURCES = Object.freeze(['folder', 'tag']);

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  return m ? [parseInt(m[1], 16) / 255, parseInt(m[2], 16) / 255, parseInt(m[3], 16) / 255] : [1, 1, 1];
}

// '' for notes in the vault root.
function folderKey(path) {
  if (typeof path !== 'string') return '';
  const i = path.indexOf('/');
  return i > 0 ? path.slice(0, i) : '';
}

// '#Area/Sub-topic' -> 'area'. Returns '' for anything unusable.
function normalizeTag(tag) {
  if (typeof tag !== 'string') return '';
  const s = tag.trim().replace(/^#+/, '').split('/')[0].trim().toLowerCase();
  return /\s/.test(s) ? '' : s;
}

// Tags in the order Obsidian users expect: frontmatter first, then inline.
function tagsFromCache(cache) {
  const out = [];
  if (!cache || typeof cache !== 'object') return out;
  const fm = cache.frontmatter;
  if (fm && typeof fm === 'object') {
    for (const field of ['tags', 'tag']) {
      const value = fm[field];
      const list = Array.isArray(value) ? value : (typeof value === 'string' ? value.split(/[,\s]+/) : []);
      for (const t of list) { const n = normalizeTag(t); if (n) out.push(n); }
    }
  }
  if (Array.isArray(cache.tags)) for (const t of cache.tags) { const n = normalizeTag(t && t.tag); if (n) out.push(n); }
  return out;
}

function regionKeyForFile(file, source, metadataCache) {
  if (!file) return '';
  if (source === 'tag') {
    let cache = null;
    try { cache = metadataCache && typeof metadataCache.getFileCache === 'function' ? metadataCache.getFileCache(file) : null; } catch (_) { cache = null; }
    const tags = tagsFromCache(cache);
    return tags.length ? '#' + tags[0] : '';
  }
  return folderKey(file.path);
}

function displayName(key, source) {
  if (key === '') return source === 'tag' ? '(untagged)' : '(vault root)';
  return key;
}

function compareKeys(a, b) { return a < b ? -1 : (a > b ? 1 : 0); }

// Builds the region table for a list of region keys (one per note). The
// result is fully determined by the multiset of keys: regions are ranked by
// note count, ties broken by key, and colours are assigned by rank.
function buildRegionTable(keys, options = {}) {
  const source = SOURCES.includes(options.source) ? options.source : 'folder';
  const limit = Math.max(0, Math.min(REGION_COLORS.length, options.maxRegions ?? REGION_COLORS.length));
  const counts = new Map();
  for (const key of keys) { const k = typeof key === 'string' ? key : ''; counts.set(k, (counts.get(k) || 0) + 1); }
  const ranked = Array.from(counts).sort((a, b) => b[1] - a[1] || compareKeys(a[0], b[0]));
  const regions = [], byKey = new Map();
  const other = { key: OTHER_KEY, name: 'Other', color: OTHER_COLOR, rgb: hexToRgb(OTHER_COLOR), count: 0, groups: 0, index: -1 };
  ranked.forEach(([key, count], i) => {
    if (i < limit) {
      const color = REGION_COLORS[i];
      const region = { key, name: displayName(key, source), color, rgb: hexToRgb(color), count, index: i };
      regions.push(region); byKey.set(key, region);
    } else { other.count += count; other.groups++; }
  });
  return { source, regions, other, byKey, lookup(key) { return byKey.get(key) || other; } };
}

module.exports = { REGION_COLORS, OTHER_COLOR, OTHER_KEY, SOURCES, hexToRgb, folderKey, normalizeTag, tagsFromCache, regionKeyForFile, buildRegionTable };
