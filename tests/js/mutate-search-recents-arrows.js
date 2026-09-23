/**
 * Mutation harness for tests/js/search-recents-arrows-probe.js
 *
 * Rewrites the SHIPPED implementation in scratch copies and asserts the probe
 * FAILS on each. A probe that still passes against a broken variant proves
 * nothing — and the last mutation below is the bug the probe actually caught
 * during development, so it must stay caught.
 *
 * Usage: node tests/js/mutate-search-recents-arrows.js <path-to-search-flyout.js>
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const SRC_PATH = process.argv[2];
if (!SRC_PATH) {
  console.error('usage: node mutate-search-recents-arrows.js <path-to-search-flyout.js>');
  process.exit(2);
}

// Normalise line endings so the LF anchors below match on Windows checkouts.
const src = fs.readFileSync(SRC_PATH, 'utf8').replace(/\r\n/g, '\n');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sra-mut-'));

const mutations = [
  {
    name: 'M1 rememberSearch appends without de-duplicating',
    apply: function (s) {
      return s.replace(
        'const rest = readRecentSearches().filter((r) => r.toLowerCase() !== key);',
        'const rest = readRecentSearches();',
      );
    },
  },
  {
    name: 'M2 rememberSearch loses the cap on the stored list',
    apply: function (s) {
      return s.replace(
        'store.setItem(RECENTS_KEY, JSON.stringify(list.slice(0, RECENTS_MAX)));',
        'store.setItem(RECENTS_KEY, JSON.stringify(list));',
      );
    },
  },
  {
    name: 'M3 de-duplication compares case-sensitively',
    apply: function (s) {
      return s.replace(
        'const rest = readRecentSearches().filter((r) => r.toLowerCase() !== key);',
        'const rest = readRecentSearches().filter((r) => r !== q);',
      );
    },
  },
  {
    name: 'M4 moveActiveRow drops the closed-flyout guard',
    apply: function (s) {
      return s.replace(
        'function moveActiveRow(delta) {\n    if (!isSearchOpen()) return false;\n',
        'function moveActiveRow(delta) {\n',
      );
    },
  },
  {
    name: 'M5 the index clamps instead of leaving the list',
    apply: function (s) {
      return s.replace(
        'if (index < 0 || index >= rows.length) {\n      _activeRowIndex = -1;\n      return;\n    }',
        'if (index < 0) index = 0;\n    if (index >= rows.length) index = rows.length - 1;\n    if (!rows.length) {\n      _activeRowIndex = -1;\n      return;\n    }',
      );
    },
  },
  {
    name: 'M6 resetActiveRow only zeroes the index (the bug the probe caught)',
    apply: function (s) {
      return s.replace(
        '    setActiveRow(-1);\n  }\n\n  /**\n   * Highlight `index`',
        '    _activeRowIndex = -1;\n  }\n\n  /**\n   * Highlight `index`',
      );
    },
  },
  {
    name: 'M7 the recents filter no longer rejects short entries',
    apply: function (s) {
      return s.replace(
        ".filter((q) => typeof q === 'string' && q.trim().length >= MIN_QUERY_LENGTH)",
        ".filter((q) => typeof q === 'string')",
      );
    },
  },
];

// NOT MUTATED HERE: the "clearing the box refreshes the recents view" line and
// the other DOMContentLoaded wiring. This probe extracts FUNCTIONS; it cannot
// reach the init block, so a mutation there would escape and the harness would
// be reporting a false all-clear. Those assertions live in
// tests/test_search_recents_and_arrow_keys.py as source-contract checks.

function runProbe(target) {
  const out = execFileSync(
    process.execPath,
    [path.join(__dirname, 'search-recents-arrows-probe.js'), target],
    { encoding: 'utf8' },
  );
  return JSON.parse(out.trim());
}

let failures = 0;
for (const m of mutations) {
  const mutated = m.apply(src);
  if (mutated === src) {
    // FAIL LOUDLY: an anchor that does not match would otherwise be counted
    // as a pass, which is how a mutation harness lies.
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
    // A crash is still a detection, but say so explicitly.
    console.log('DETECTED (crash)  ' + m.name + '  -> ' + String(err.message).slice(0, 120));
    continue;
  }
  if (result.error) {
    console.log('DETECTED (build)  ' + m.name + '  -> ' + result.error);
    continue;
  }
  if (result.failed > 0) {
    console.log(
      'DETECTED  ' + m.name + '  -> ' + result.failed + '/' + result.total +
      ' failed: ' + result.checks.filter((c) => !c.pass).map((c) => c.name).join('; '),
    );
  } else {
    console.log('ESCAPED   ' + m.name + '  (probe still passed!)');
    failures++;
  }
}

fs.rmSync(tmpDir, { recursive: true, force: true });
console.log(failures === 0 ? 'ALL MUTATIONS DETECTED' : failures + ' MUTATION(S) ESCAPED');
process.exit(failures === 0 ? 0 : 1);
