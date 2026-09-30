'use strict';

// Runs every tests/*.test.js file in its own Node process and exits non-zero
// if any fails. Plain Node, no packages:  node obsidian-plugin/tests/run-all.js
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const files = fs.readdirSync(__dirname).filter(f => f.endsWith('.test.js')).sort();
const results = [];
for (const file of files) {
  const started = Date.now();
  console.log('\n# ' + file);
  const r = spawnSync(process.execPath, [path.join(__dirname, file)], { stdio: 'inherit', timeout: 300000 });
  const ok = r.status === 0 && !r.error;
  results.push({ file, ok, seconds: (Date.now() - started) / 1000, detail: r.error ? String(r.error.message) : 'exit ' + r.status });
}
console.log('\n# summary');
for (const r of results) console.log((r.ok ? 'PASS ' : 'FAIL ') + r.file + ' (' + r.seconds.toFixed(1) + ' s' + (r.ok ? '' : ', ' + r.detail) + ')');
const failed = results.filter(r => !r.ok).length;
console.log(failed ? failed + ' of ' + results.length + ' test files failed' : 'all ' + results.length + ' test files passed');
process.exit(failed ? 1 : 0);
