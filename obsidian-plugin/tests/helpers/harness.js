'use strict';

// Minimal test runner: register with test(name, fn), then call run(label).
// Sets a non-zero exit code if any test fails.
const tests = [];

function test(name, fn) { tests.push({ name, fn }); }

async function run(label) {
  let failed = 0;
  for (const t of tests) {
    try {
      await t.fn();
      console.log('ok - ' + t.name);
    } catch (error) {
      failed++;
      console.log('not ok - ' + t.name);
      console.log(String(error && error.stack || error).split('\n').map(line => '  ' + line).join('\n'));
    }
  }
  console.log(label + ': ' + (tests.length - failed) + '/' + tests.length + ' passed');
  // Exit explicitly so a leaked timer in a failing test cannot hang the run.
  process.exit(failed ? 1 : 0);
}

module.exports = { test, run };
