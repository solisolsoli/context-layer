'use strict';

// Static checks over the plugin directory: the source is plain ASCII, uses
// no network or vault-write APIs, and the docs avoid claims the project has
// decided not to make.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, run } = require('./helpers/harness');

const root = path.join(__dirname, '..');

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue;
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(p, out); else out.push(p);
  }
  return out;
}
const textFiles = walk(root).filter(p => /\.(js|py|css|json|md)$/.test(p) || path.basename(p) === 'LICENSE');
const srcFiles = walk(path.join(root, 'src'));
const read = p => fs.readFileSync(p, 'utf8');
const rel = p => path.relative(root, p);

test('every text file in the plugin is ASCII', () => {
  for (const p of textFiles) {
    const bad = read(p).split('\n').findIndex(line => /[^\x00-\x7F]/.test(line));
    assert.equal(bad, -1, rel(p) + ':' + (bad + 1) + ' contains a non-ASCII character');
  }
});

test('no CSS uppercases text by locale (Turkish locales turn i into a dotted I)', () => {
  const css = read(path.join(root, 'styles.css'));
  assert.ok(!/text-transform:\s*uppercase/i.test(css), 'styles.css must not use text-transform: uppercase');
});

test('the source makes no network requests', () => {
  const network = /\b(fetch|XMLHttpRequest|WebSocket|EventSource|requestUrl|request|sendBeacon|navigator\.sendBeacon|importScripts)\s*\(|new\s+(XMLHttpRequest|WebSocket|EventSource)\b|https?:\/\//;
  for (const p of srcFiles) assert.equal(network.test(read(p)), false, rel(p) + ' must not use network APIs or URLs');
});

test('the source never writes to the vault', () => {
  const writes = /\.(write|writeBinary|append|process|modify|create|createFolder|mkdir|remove|rmdir|rename|trash|copy|delete)\s*\(/;
  for (const p of srcFiles) {
    const lines = read(p).split('\n').filter(line => writes.test(line) && /vault|adapter|fileManager/.test(line));
    assert.deepEqual(lines, [], rel(p) + ' must not call vault or adapter write methods');
  }
});

test('docs and code avoid overclaiming phrases', () => {
  const banned = [['watch', 'your', 'AI', 'think'], ['neural', 'network'], ['first', 'plugin', 'to'], ['state', 'of', 'the', 'art']].map(words => new RegExp(words.join('[\\s-]+'), 'i'));
  for (const p of textFiles) {
    const text = read(p);
    for (const re of banned) assert.equal(re.test(text), false, rel(p) + ' contains a banned phrase: ' + re.source);
  }
});

test('the source contains no absolute user paths or hard-coded note names', () => {
  const home = new RegExp('/' + 'Users/|/home/[a-z]|[A-Z]:\\\\Users');
  for (const p of textFiles) assert.equal(home.test(read(p)), false, rel(p) + ' contains an absolute home path');
  for (const p of srcFiles) assert.equal(/getAbstractFileByPath\s*\(\s*['"]/.test(read(p)), false, rel(p) + ' looks up a hard-coded note');
});

test('no inline styles: colours and positions go through CSS classes and variables', () => {
  for (const p of srcFiles) {
    const text = read(p);
    assert.equal(/\.style\.[A-Za-z]+\s*=[^=]|\.style\[|\.cssText|setAttribute\(\s*['"]style/.test(text), false, rel(p) + ' assigns an inline style');
  }
});

test('the view uses its own window and document (pop-out windows), with documented fallbacks only', () => {
  const view = read(path.join(root, 'src', 'view.js'));
  const lines = view.split('\n').filter(line => /\b(window|document)\b\.|\b(requestAnimationFrame|cancelAnimationFrame)\(/.test(line) && !/^\s*\/\//.test(line));
  const allowed = [/typeof window !== 'undefined' \? window : null/, /typeof document !== 'undefined' \? document : null/,
    /window\.setInterval\(/, /win\.requestAnimationFrame\(callback\) : requestAnimationFrame\(callback\)/, /win\.cancelAnimationFrame\(id\); else cancelAnimationFrame\(id\)/];
  for (const line of lines) assert.ok(allowed.some(re => re.test(line)), 'unexpected global use: ' + line.trim());
});

test('the only file the plugin reads is the activation trace, through the restricted watcher', () => {
  for (const p of srcFiles) {
    const text = read(p);
    if (!p.endsWith(path.join('src', 'activation.js'))) assert.equal(/adapter\.(read|stat|exists|list)\(/.test(text), false, rel(p) + ' reads through the adapter');
    const code = text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"\\])\/\/.*$/gm, '$1');
    const literals = Array.from(code.matchAll(/(['"`])((?:\\.|(?!\1)[^\\\n])*)\1/g), m => m[2]);
    for (const lit of literals) assert.equal(/\.context\/(?!activation)/.test(lit), false, rel(p) + ' names a .context file other than the trace: ' + lit);
    assert.equal(/\.context\/(?!activation)[a-z-]+\.jsonl?/i.test(text), false, rel(p) + ' refers to non-trace state files');
  }
  const activation = read(path.join(root, 'src', 'activation.js'));
  assert.match(activation, /const path = sanitizeTracePath\(getPath\(\)\);/, 'the watcher restricts the path before any read');
});

run('static');
