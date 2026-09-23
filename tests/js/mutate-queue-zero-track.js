/**
 * Mutation harness for tests/js/queue-zero-track-probe.js
 *
 * Rewrites the SHIPPED fix in a scratch copy and asserts the probe FAILS.
 * A probe that still passes against a broken variant proves nothing.
 *
 * Usage: node tests/js/mutate-queue-zero-track.js <path-to-search-flyout.js>
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const SRC_PATH = process.argv[2];
if (!SRC_PATH) {
  console.error('usage: node mutate-queue-zero-track.js <path-to-search-flyout.js>');
  process.exit(2);
}

const src = fs.readFileSync(SRC_PATH, 'utf8');
// Normalise line endings so the anchors below (written with \n) match on
// Windows checkouts, which are CRLF.
const srcNorm = src.replace(/\r\n/g, '\n');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'qzt-mut-'));

// The guard block the fix introduced inside queueRelease's direct-POST branch.
const GUARD_ANCHOR = [
  '      const queuedCount = Number(data && data.queued_tracks) || 0;',
  '      const succeeded = !(',
  '        (data && (data.queued === false || data.success === false))',
  '        || (data && data.queued_tracks !== undefined && queuedCount === 0)',
  '      );',
  '      if (!succeeded) {',
].join('\n');

const mutations = [
  {
    name: 'M1 ignore queued:false and always settle as success',
    apply: function (s) {
      // Mirrors the original blind call: no field of the response is read.
      return s.replace(
        GUARD_ANCHOR,
        ['      const succeeded = true;', '      if (!succeeded) {'].join('\n'),
      );
    },
  },
  {
    name: 'M2 believe success:true and trust the flag over the count',
    apply: function (s) {
      // The precedence bug: `success` short-circuits the count check, so the
      // endpoint's old success:true + queued_tracks:0 shape slips through.
      return s.replace(
        GUARD_ANCHOR,
        [
          '      const queuedCount = Number(data && data.queued_tracks) || 0;',
          "      const succeeded = !(data && (data.queued === false || data.success === false)) || queuedCount > 0;",
          '      if (!succeeded) {',
        ].join('\n'),
      );
    },
  },
  {
    name: 'M3 drop the queued:false check but keep success:false',
    apply: function (s) {
      // Semantically valid: only the NEW queued:false contract is ignored.
      return s.replace(
        '        (data && (data.queued === false || data.success === false))\n',
        '        (data && data.success === false)\n',
      );
    },
  },
];

function runProbe(target) {
  const out = execFileSync(
    process.execPath,
    [path.join(__dirname, 'queue-zero-track-probe.js'), target],
    { encoding: 'utf8' },
  );
  return JSON.parse(out.trim());
}

let failures = 0;
for (const m of mutations) {
  const mutated = m.apply(srcNorm);
  if (mutated === srcNorm) {
    console.log('ANCHOR-NOT-FOUND  ' + m.name);
    failures++;
    continue;
  }
  const target = path.join(tmpDir, 'mutated.js');
  fs.writeFileSync(target, mutated, 'utf8');

  let result;
  try {
    result = runProbe(target);
  } catch (err) {
    // A harness build failure is also a detected mutation.
    console.log('DETECTED (crash)  ' + m.name + '  -> ' + String(err.message).slice(0, 120));
    continue;
  }
  if (result.error) {
    console.log('DETECTED (build)  ' + m.name + '  -> ' + result.error);
    continue;
  }
  if (result.failed > 0) {
    console.log(
      'DETECTED  ' + m.name + '  -> ' + result.failed + '/' + result.total + ' checks failed: ' +
      result.checks.filter((c) => !c.pass).map((c) => c.name).join('; '),
    );
  } else {
    console.log('ESCAPED   ' + m.name + '  (probe still passed!)');
    failures++;
  }
}

fs.rmSync(tmpDir, { recursive: true, force: true });
console.log(failures === 0 ? 'ALL MUTATIONS DETECTED' : failures + ' MUTATION(S) ESCAPED');
process.exit(failures === 0 ? 0 : 1);
