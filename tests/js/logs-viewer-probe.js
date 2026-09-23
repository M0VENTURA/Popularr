/**
 * Behaviour probe for the log viewer's new filter + jump-button logic.
 *
 * Extracts the REAL functions from test_site/static/js/pages/logs.js rather
 * than reimplementing them, so this cannot drift from the shipped code.
 * Run with: node tests/js/logs-viewer-probe.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', 'test_site', 'static', 'js', 'pages', 'logs.js');
const src = fs.readFileSync(SRC, 'utf8');

let failures = 0;
function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}`);
  if (!ok) console.log(`         expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}
function ok(label, cond) {
  if (!cond) failures += 1;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}`);
}

// ── Extract the logic under test by evaluating the module against a stub DOM.
function loadModule(opts) {
  const state = Object.assign({ filterText: '', filterTextLower: '', filterRegex: false,
    filterCaseSensitive: false, levelFilter: 'all', unreadCount: 0 }, opts.state || {});

  const els = Object.assign({
    logOutput: { scrollTop: 0, clientHeight: 100, scrollHeight: 100,
                 addEventListener() {}, style: {} },
    scrollToBottomBtn: { classList: { toggle() {}, contains() { return false; } },
                         setAttribute() {} },
    unreadLogBadge: { textContent: '', classList: { toggle() {}, contains() { return false; } } },
    // The match counter writes textContent AND className, so the stub needs both.
    logMatchCount: { textContent: '', className: '' },
  }, opts.els || {});

  const windowStub = {
    document: {
      getElementById: (id) => els[id] || null,
      addEventListener() {},
      createElement: () => ({ style: {}, select() {}, value: '' }),
      body: { appendChild() {}, removeChild() {} },
    },
    api: { getJson: () => Promise.resolve({ lines: [] }) },
    toast: { warning() {} },
    location: { href: '' },
    navigator: {},
    EventSource: function () {},
    addEventListener() {},
  };

  // The source is an IIFE — `(function (global) { … })(window)`. Injecting the
  // probe API OUTSIDE it cannot see the closure variables, so it is spliced in
  // just before the closing invocation instead. This keeps the probe reading
  // the REAL functions rather than a reimplementation that could drift.
  const closing = '})(window);';
  const at = src.lastIndexOf(closing);
  if (at === -1) {
    throw new Error('could not find the IIFE closing marker — did logs.js change shape?');
  }
  const spliced =
    src.slice(0, at) +
    `
  global.__probe = {
    setFilter(text, regex, caseSensitive) {
      filterText = text; filterTextLower = text.toLowerCase();
      filterRegex = regex; filterCaseSensitive = caseSensitive;
    },
    matches(line) { compileFilter(); return matchesFilters(line, lineLevel(line) || 'info'); },
    setLevel(l) { levelFilter = l; },
    unread() { return unreadCount; },
    setUnread(n) { unreadCount = n; },
    atBottom() { return atBottom(); },
    compileInvalid() { compileFilter(); return regexInvalid; },
    compiledIsNull() { compileFilter(); return compiledRegex === null; },
    setLines(l) { rawLines = l; },
    // Set the buffer to N lines but report M as matching. NO BACKTICKS here:
    // this block is injected inside a JS template literal, so one backtick
    // would terminate the string early.
    countFor(visible, total) {
      rawLines = new Array(total === undefined ? visible : total).fill('x');
      updateMatchCount(visible);
    },
    matchText() { var e = document.getElementById('logMatchCount'); return e ? e.textContent : null; },
    resetUnread() { resetUnread(); },
    jump() { updateJumpButton(); },
  };
  ` +
    src.slice(at);

  const factory = new Function('window', 'document', 'globalThis', spliced + '\nreturn window.__probe;');
  return factory(windowStub, windowStub.document, windowStub);
}

console.log('filter matching');
{
  const m = loadModule({});
  m.setFilter('error', false, false);
  ok('case-insensitive substring matches uppercase', m.matches('an ERROR happened') === true);
  m.setFilter('ERROR', false, false);
  ok('pattern case does not matter when insensitive', m.matches('an error happened') === true);
  m.setFilter('error', false, true);
  ok('case-sensitive rejects lowercase mismatch', m.matches('an ERROR happened') === false);
  ok('case-sensitive accepts exact', m.matches('an error happened') === true);
}

console.log('regex mode');
{
  const m = loadModule({});
  m.setFilter('ERR\\d+', true, false);
  ok('regex matches digits', m.matches('ERR500 boom') === true);
  ok('regex rejects non-match', m.matches('ERR boom') === false);

  // ⚠️ The bug this guards: lower-casing the pattern would narrow [A-Z] to [a-z].
  m.setFilter('[A-Z]{3}', true, true);
  ok('case-sensitive regex keeps [A-Z] working', m.matches('ABC here') === true);
  ok('case-sensitive regex rejects lowercase', m.matches('abc here') === false);

  m.setFilter('[a-z]{3}', true, false);
  ok('case-insensitive regex reaches uppercase via the i flag', m.matches('ABC here') === true);
}

console.log('invalid + oversized patterns');
{
  const m = loadModule({});
  m.setFilter('(', true, false);
  ok('unbalanced group is reported invalid', m.compileInvalid() === true);
  ok('invalid pattern matches NOTHING (not everything)', m.matches('anything') === false);
  ok('invalid pattern leaves no compiled regex', m.compiledIsNull() === true);

  const m2 = loadModule({});
  m2.setFilter('a'.repeat(500), true, false);
  ok('over-long pattern is rejected', m2.compileInvalid() === true);

  const m3 = loadModule({});
  m3.setFilter('ok', true, false);
  ok('valid pattern is not flagged invalid', m3.compileInvalid() === false);
}

console.log('empty filter passes everything');
{
  const m = loadModule({});
  m.setFilter('', false, false);
  ok('no filter -> all lines match', m.matches('whatever') === true);
}

console.log('level filter still applies');
{
  const m = loadModule({});
  m.setFilter('', false, false);
  m.setLevel('error');
  ok('ERROR passes the error filter', m.matches('2024-01-01 00:00:00 ERROR boom') === true);
  ok('INFO is excluded by the error filter', m.matches('2024-01-01 00:00:00 INFO fine') === false);
}

console.log('match counter');
{
  const m = loadModule({});
  m.setFilter('', false, false);
  m.setLevel('all');
  m.countFor(0);
  check('no filter -> no counter text', m.matchText(), '');

  // ⚠️ Visible and total MUST differ, or a counter that reports the total as
  // "visible" is indistinguishable from a correct one. Mutation testing caught
  // exactly that: an earlier version of this probe passed 7/7 and could not
  // detect the bug.
  m.setFilter('x', false, false);
  m.countFor(7, 40);
  check('filtered counter shows visible / total', m.matchText(), '7 / 40 lines');
}

console.log('jump button + unread counter');
{
  const m = loadModule({ els: { logOutput: { scrollTop: 0, clientHeight: 100, scrollHeight: 100 } } });
  ok('at bottom when scrollTop+clientHeight >= scrollHeight', m.atBottom() === true);

  m.setUnread(5);
  m.resetUnread();
  check('reset clears the count', m.unread(), 0);

  const far = loadModule({ els: { logOutput: { scrollTop: 0, clientHeight: 100, scrollHeight: 5000 } } });
  ok('not at bottom when scrolled away', far.atBottom() === false);
}

console.log('');
if (failures) {
  console.log(`${failures} check(s) FAILED`);
  process.exit(1);
}
console.log('all log-viewer probes passed');
