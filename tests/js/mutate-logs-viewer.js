/**
 * Mutation-test the log-viewer probe: does it actually catch broken logic?
 *
 * The probe asserts against the real logs.js, so it must FAIL when the code is
 * broken. A probe that passes either way proves nothing.
 */
'use strict';
const fs = require('fs');
const { execFileSync } = require('child_process');
const path = require('path');

const TARGET = path.join(__dirname, '..', '..', 'test_site', 'static', 'js', 'pages', 'logs.js');
const PROBE = path.join(__dirname, 'logs-viewer-probe.js');
const original = fs.readFileSync(TARGET, 'utf8');

const MUTATIONS = [
  ['lower-case the regex pattern (narrows [A-Z] to [a-z])',
   'compiledRegex = new RegExp(filterText, flags);',
   'compiledRegex = new RegExp(filterText.toLowerCase(), flags);'],

  ['drop the case-sensitive branch (case toggle does nothing)',
   '} else if (filterCaseSensitive) {',
   '} else if (false) {'],

  ['invalid pattern matches EVERYTHING instead of nothing',
   'if (!compiledRegex) return false;',
   'if (!compiledRegex) return true;'],

  ['remove the pattern length cap (ReDoS guard)',
   'if (filterText.length > MAX_PATTERN_LENGTH) {',
   'if (false) {'],

  // The probe asserts "7 / 7 lines". Using the TOTAL in place of `visible`
  // makes a filtered result indistinguishable from an unfiltered one.
  ['counter ignores the filter (reports the total as visible)',
   'node.textContent = `${visible.toLocaleString()} / ${rawLines.length.toLocaleString()} lines`;',
   'node.textContent = `${rawLines.length.toLocaleString()} / ${rawLines.length.toLocaleString()} lines`;'],
];

function run() {
  try {
    execFileSync(process.execPath, [PROBE], { stdio: 'pipe' });
    return 0;
  } catch (err) {
    return 1;
  }
}

const baseline = run();
console.log(`BASELINE (unmutated): exit=${baseline} ${baseline === 0 ? '(pass)' : '(FAIL — probe is broken)'}`);
if (baseline !== 0) {
  console.log('refusing to mutation-test a probe that does not pass on the real code');
  process.exit(1);
}
console.log('');

let missed = 0;
try {
  for (const [label, from, to] of MUTATIONS) {
    if (!original.includes(from)) {
      console.log(`  !! anchor not found: ${label}`);
      missed += 1;
      continue;
    }
    fs.writeFileSync(TARGET, original.replace(from, to), 'utf8');
    const code = run();
    const caught = code !== 0;
    if (!caught) missed += 1;
    console.log(`  ${caught ? 'CAUGHT ' : 'MISSED '} ${label}`);
  }
} finally {
  fs.writeFileSync(TARGET, original, 'utf8');
}

console.log('');
console.log(missed ? `${missed} mutation(s) NOT caught — probe has gaps` : 'ALL mutations caught — probe has teeth');
process.exit(missed ? 1 : 0);
