/**
 * Behavioural probe: recent searches + arrow-key navigation in the unified
 * search flyout.
 *
 * Usage:  node tests/js/search-recents-arrows-probe.js <path-to-search-flyout.js>
 * Output: a single JSON line on stdout.
 *
 * WHY THIS LIVES IN A FILE
 * ------------------------
 * It drives the SHIPPED functions, brace-matched out of the source and spliced
 * into an IIFE, so the probe cannot silently drift from the code it claims to
 * test. A source-contract test cannot tell a working de-duplication rule from
 * one that appends forever, nor a clamped arrow index from an unclamped one.
 *
 * WHY NO JSDOM
 * ------------
 * The repo has no jsdom and adding a runtime dependency for a test is out of
 * scope. `navigableRows()` touches exactly three things — `querySelectorAll`,
 * per-row `classList`/`getAttribute`/`setAttribute`/`scrollIntoView`/`click` —
 * so a ~30-line stub covers it honestly. Anything the stub does NOT implement
 * would throw, which is the desired failure mode.
 */
'use strict';

const fs = require('fs');

const SRC_PATH = process.argv[2];
if (!SRC_PATH) {
  console.error('usage: node search-recents-arrows-probe.js <path-to-search-flyout.js>');
  process.exit(2);
}

const SRC = fs.readFileSync(SRC_PATH, 'utf8');

/**
 * Blank out comments, preserving newlines so line-based debugging still works.
 *
 * WHY THIS IS NECESSARY, NOT COSMETIC: the brace matcher below tracks string
 * literals so that a `{` inside a string cannot be counted. But an APOSTROPHE
 * inside a COMMENT ("the markup's own semantics") is not a string opener, and
 * without this pass it puts the tracker into string state, where it stays until
 * the next apostrophe — desynchronising the brace count and swallowing every
 * function that follows. That silently extracted 4656 characters for a
 * 15-line function.
 *
 * Known limitation: a regex literal containing `//` or `/*` would be seen as a
 * comment. None exist in this file, and a mis-extraction fails loudly (the
 * harness will not build) rather than passing vacuously.
 */
function stripComments(source) {
  const out = source.split('');
  let i = 0;
  const n = source.length;
  function blank(from, to) {
    for (let k = from; k < to && k < n; k++) {
      if (out[k] !== '\n') out[k] = ' ';
    }
  }
  while (i < n) {
    const ch = source[i];
    const next = source[i + 1];
    if (ch === '/' && next === '/') {
      let j = i;
      while (j < n && source[j] !== '\n') j++;
      blank(i, j);
      i = j;
    } else if (ch === '/' && next === '*') {
      let j = i + 2;
      while (j < n && !(source[j] === '*' && source[j + 1] === '/')) j++;
      j = Math.min(j + 2, n);
      blank(i, j);
      i = j;
    } else if (ch === '"' || ch === "'" || ch === '`') {
      // Skip over the literal untouched so a comment marker inside it survives.
      let j = i + 1;
      while (j < n) {
        if (source[j] === '\\') { j += 2; continue; }
        if (source[j] === ch) { j++; break; }
        j++;
      }
      i = j;
    } else {
      i++;
    }
  }
  return out.join('');
}

/** The source used for extraction — comments removed, everything else intact. */
const EXTRACT_SRC = stripComments(SRC);

/**
 * Brace-match `function <name>(...) { ... }` out of a source string.
 *
 * Includes a leading `async`/`export` modifier when present — slicing from the
 * bare `function` keyword would drop `async` from an async function and the
 * spliced copy would then throw "await is only valid in async functions".
 */
function extractFn(source, name) {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) return null;
  let begin = start;
  for (const modifier of ['export default ', 'export ', 'async ']) {
    if (source.startsWith(modifier, start - modifier.length)) {
      begin = start - modifier.length;
      break;
    }
  }
  return sliceBraces(source, source.indexOf('{', start), begin, name);
}

/** Extract `const <name> = (...) => { ... }` (block-bodied arrow only). */
function extractArrow(source, name) {
  const decl = 'const ' + name + ' = ';
  const start = source.indexOf(decl);
  if (start < 0) return null;
  const body = source.indexOf('{', start + decl.length);
  if (body < 0) return null;
  const text = sliceBraces(source, body, start, name);
  if (text.indexOf('=>') < 0) {
    throw new Error('extractArrow: ' + name + ' is not an arrow function');
  }
  return text + ';';
}

/** From an opening brace, return the balanced `{...}` slice starting at `begin`. */
function sliceBraces(source, openIndex, begin, name) {
  let i = openIndex;
  let depth = 0;
  let inStr = null;
  for (; i < source.length; i++) {
    const ch = source[i];
    const prev = source[i - 1];
    if (inStr) {
      if (ch === inStr && prev !== '\\') inStr = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') { inStr = ch; continue; }
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return source.slice(begin, i + 1);
    }
  }
  throw new Error('unbalanced braces extracting ' + name);
}

// ── Minimal DOM stub ──────────────────────────────────────────────────────
const state = {
  searchOpen: true,
  searchesRun: 0,
  lastQuery: null,
  storageThrows: false,
  store: Object.create(null),
};

globalThis.window = globalThis;

globalThis.localStorage = {
  getItem: function (k) {
    if (state.storageThrows) throw new Error('SecurityError: storage disabled');
    return k in state.store ? state.store[k] : null;
  },
  setItem: function (k, v) {
    if (state.storageThrows) throw new Error('QuotaExceededError');
    state.store[k] = String(v);
  },
};

function makeRow(attrs, tagName) {
  const attributes = Object.assign({}, attrs || {});
  const classes = Object.create(null);
  return {
    tagName: tagName || 'A',
    _clicked: 0,
    _scrolled: 0,
    classList: {
      add: function (c) { classes[c] = true; },
      remove: function (c) { delete classes[c]; },
      contains: function (c) { return !!classes[c]; },
      all: function () { return Object.keys(classes); },
    },
    getAttribute: function (k) { return k in attributes ? attributes[k] : null; },
    setAttribute: function (k, v) { attributes[k] = String(v); },
    removeAttribute: function (k) { delete attributes[k]; },
    hasAttribute: function (k) { return k in attributes; },
    scrollIntoView: function () { this._scrolled += 1; },
    click: function () { this._clicked += 1; },
  };
}

const dom = {
  modal: { classList: { contains: function (c) { return c === 'd-none' && !state.searchOpen; } } },
  input: { value: '' },
  results: {
    _rows: [],
    innerHTML: '',
    querySelectorAll: function (sel) { return sel === '.us-row' ? this._rows.slice() : []; },
  },
  error: { classList: { add: function () {}, remove: function () {} } },
  byId: Object.create(null),
};

globalThis.document = {
  getElementById: function (id) {
    if (id === 'unifiedSearchModal') return dom.modal;
    if (id === 'unifiedSearchInput') return dom.input;
    if (id === 'unifiedSearchResults') return dom.results;
    if (id === 'unifiedSearchError') return dom.error;
    return dom.byId[id] || null;
  },
};

// ── Build the harness from the shipped source ─────────────────────────────
const FN_NAMES = [
  'readRecentSearches', 'writeRecentSearches', 'rememberSearch', 'forgetSearch',
  'navigableRows', 'resetActiveRow', 'setActiveRow', 'moveActiveRow',
  'activeRow', 'activateRow', 'handleEnterKey', 'applyRecentSearch',
];

const ARROW_NAMES = ['isSearchOpen'];

const missing = FN_NAMES.filter(function (n) { return !extractFn(EXTRACT_SRC, n); })
  .concat(ARROW_NAMES.filter(function (n) { return !extractArrow(EXTRACT_SRC, n); }));
if (missing.length) {
  console.log(JSON.stringify({ error: 'missing functions: ' + missing.join(', ') }));
  process.exit(0);
}

const harness = [
  "var MIN_QUERY_LENGTH = 2;",
  "var RECENTS_KEY = 'popularr.unifiedSearch.recent';",
  "var RECENTS_MAX = 8;",
  'var _activeRowIndex = -1;',
  'function esc(v) { return String(v == null ? "" : v); }',
  'function getModalEl() { return globalThis.document.getElementById("unifiedSearchModal"); }',
  'function getInputEl() { return globalThis.document.getElementById("unifiedSearchInput"); }',
  'function getResultsEl() { return globalThis.document.getElementById("unifiedSearchResults"); }',
  'function getErrorEl() { return globalThis.document.getElementById("unifiedSearchError"); }',
  'function setAdvancedFiltersVisible() {}',
  'function runSearch() { globalThis.__probeState.searchesRun++; }',
  'function hasAnyAdvancedFilter() { return false; }',
  'function getAdvancedFilters() { return {}; }',
  'var __state = globalThis.__probeState;',
  extractArrow(EXTRACT_SRC, 'isSearchOpen'),
  ...FN_NAMES.map(function (n) { return extractFn(EXTRACT_SRC, n); }),
  'globalThis.__api = {',
  '  readRecentSearches: readRecentSearches,',
  '  writeRecentSearches: writeRecentSearches,',
  '  rememberSearch: rememberSearch,',
  '  forgetSearch: forgetSearch,',
  '  navigableRows: navigableRows,',
  '  resetActiveRow: resetActiveRow,',
  '  setActiveRow: setActiveRow,',
  '  moveActiveRow: moveActiveRow,',
  '  activeRow: activeRow,',
  '  handleEnterKey: handleEnterKey,',
  '  applyRecentSearch: applyRecentSearch,',
  '  isSearchOpen: isSearchOpen,',
  '  getActiveIndex: function () { return _activeRowIndex; },',
  '  rawStored: function () {',
  '    var raw = globalThis.localStorage.getItem(RECENTS_KEY);',
  '    return raw === null ? null : JSON.parse(raw);',
  '  },',
  '};',
].join('\n');

globalThis.__probeState = state;

let api;
try {
  api = new Function(harness + '\nreturn globalThis.__api;')();
} catch (err) {
  console.log(JSON.stringify({ error: 'harness build failed: ' + err.message }));
  process.exit(0);
}

// ── Checks ────────────────────────────────────────────────────────────────
const checks = [];
function check(name, cond, detail) {
  checks.push({ name: name, pass: !!cond, detail: detail === undefined ? null : detail });
}
function reset() {
  state.searchOpen = true;
  state.searchesRun = 0;
  state.lastQuery = null;
  state.storageThrows = false;
  state.store = Object.create(null);
  dom.results._rows = [];
  dom.results.innerHTML = '';
  dom.input.value = '';
  api.resetActiveRow();
}
function seedRows(n, attrs) {
  const rows = [];
  for (let i = 0; i < n; i++) rows.push(makeRow(attrs || { 'data-row': String(i) }));
  dom.results._rows = rows;
  return rows;
}

// ── 1. The recents store ──────────────────────────────────────────────────
reset();
api.rememberSearch('weezer');
api.rememberSearch('pixies');
check('recents: newest first', api.readRecentSearches()[0] === 'pixies', api.readRecentSearches());

reset();
api.rememberSearch('weezer');
api.rememberSearch('pixies');
api.rememberSearch('weezer');
check(
  'recents: re-searching moves to front without duplicating',
  JSON.stringify(api.readRecentSearches()) === JSON.stringify(['weezer', 'pixies']),
  api.readRecentSearches(),
);

reset();
api.rememberSearch('Weezer');
api.rememberSearch('weezer');
check(
  'recents: de-duplication is case-insensitive',
  api.readRecentSearches().length === 1,
  api.readRecentSearches(),
);
check(
  'recents: the casing the user typed is what is stored',
  api.readRecentSearches()[0] === 'weezer',
  api.readRecentSearches(),
);

reset();
for (let i = 0; i < 14; i++) api.rememberSearch('query' + i);
check('recents: capped at RECENTS_MAX', api.readRecentSearches().length === 8, api.readRecentSearches().length);
check('recents: cap keeps the NEWEST', api.readRecentSearches()[0] === 'query13', api.readRecentSearches()[0]);
// Assert on the RAW stored value, not the read-back list. readRecentSearches
// slices to RECENTS_MAX on READ, so a missing cap on the WRITE side is
// invisible through it — the read-side slice masks the write-side one and the
// stored JSON grows without bound instead.
check(
  'recents: the cap is enforced on WRITE, so storage cannot grow unbounded',
  Array.isArray(api.rawStored()) && api.rawStored().length === 8,
  api.rawStored() && api.rawStored().length,
);

reset();
api.rememberSearch('a');
check('recents: rejects a query below MIN_QUERY_LENGTH', api.readRecentSearches().length === 0, api.readRecentSearches());

reset();
api.rememberSearch('  weird  ');
check('recents: stores the trimmed query', api.readRecentSearches()[0] === 'weird', api.readRecentSearches());

reset();
api.rememberSearch('keep');
api.rememberSearch('drop');
api.forgetSearch('drop');
check('recents: forget removes one entry', JSON.stringify(api.readRecentSearches()) === JSON.stringify(['keep']), api.readRecentSearches());

reset();
api.rememberSearch('keep');
api.forgetSearch('KEEP');
check('recents: forget is case-insensitive', api.readRecentSearches().length === 0, api.readRecentSearches());

reset();
api.writeRecentSearches([]);
check('recents: clearing leaves an empty list', api.readRecentSearches().length === 0, api.readRecentSearches());

reset();
state.store['popularr.unifiedSearch.recent'] = 'not json at all {{{';
check('recents: corrupt JSON degrades to empty', api.readRecentSearches().length === 0, api.readRecentSearches());

reset();
state.store['popularr.unifiedSearch.recent'] = JSON.stringify({ not: 'an array' });
check('recents: non-array JSON degrades to empty', api.readRecentSearches().length === 0, api.readRecentSearches());

reset();
state.store['popularr.unifiedSearch.recent'] = JSON.stringify(['ok', 42, null, 'x', 'fine']);
check(
  'recents: non-string and too-short entries are filtered out',
  JSON.stringify(api.readRecentSearches()) === JSON.stringify(['ok', 'fine']),
  api.readRecentSearches(),
);

reset();
state.storageThrows = true;
let threw = false;
try { api.rememberSearch('boom'); api.readRecentSearches(); } catch (_e) { threw = true; }
check('recents: a throwing localStorage never propagates', !threw);

// ── 2. Arrow-key navigation ───────────────────────────────────────────────
reset();
seedRows(3);
api.moveActiveRow(1);
check('arrows: ArrowDown from nothing selects the FIRST row', api.getActiveIndex() === 0, api.getActiveIndex());
api.moveActiveRow(1);
check('arrows: ArrowDown steps forward', api.getActiveIndex() === 1, api.getActiveIndex());

reset();
seedRows(3);
api.moveActiveRow(-1);
check('arrows: ArrowUp from nothing selects the LAST row', api.getActiveIndex() === 2, api.getActiveIndex());

reset();
seedRows(3);
api.moveActiveRow(1);
api.moveActiveRow(-1);
check('arrows: ArrowUp at the top leaves the list', api.getActiveIndex() === -1, api.getActiveIndex());

reset();
seedRows(3);
api.moveActiveRow(1);
api.moveActiveRow(1);
api.moveActiveRow(1);
check('arrows: the third ArrowDown reaches the LAST row', api.getActiveIndex() === 2, api.getActiveIndex());
api.moveActiveRow(1);
check('arrows: one more ArrowDown leaves the list', api.getActiveIndex() === -1, api.getActiveIndex());

reset();
const rowSet = seedRows(3);
api.moveActiveRow(1);
check('arrows: the active row is styled', rowSet[0].classList.contains('us-row-active'), rowSet[0].classList.all());
check('arrows: the active row is announced', rowSet[0].getAttribute('aria-current') === 'true', rowSet[0].getAttribute('aria-current'));
check('arrows: aria-selected is NOT used on an anchor', !rowSet[0].hasAttribute('aria-selected'));
check('arrows: the active row is scrolled into view', rowSet[0]._scrolled === 1, rowSet[0]._scrolled);

reset();
const two = seedRows(3);
api.moveActiveRow(1);
api.moveActiveRow(1);
check('arrows: only ONE row is active at a time', !two[0].classList.contains('us-row-active') && two[1].classList.contains('us-row-active'), [two[0].classList.all(), two[1].classList.all()]);
check('arrows: the old row stops being announced', !two[0].hasAttribute('aria-current'));

reset();
seedRows(2);
api.moveActiveRow(1);
api.resetActiveRow();
const cleared = dom.results._rows[0];
check('arrows: reset clears the highlight', !cleared.classList.contains('us-row-active') && api.getActiveIndex() === -1, api.getActiveIndex());
check('arrows: reset clears the announcement', !cleared.hasAttribute('aria-current'));

reset();
seedRows(3);
state.searchOpen = false;
const moved = api.moveActiveRow(1);
check('arrows: a CLOSED flyout ignores navigation', moved === false && api.getActiveIndex() === -1, [moved, api.getActiveIndex()]);
check('arrows: activeRow is null while closed', api.activeRow() === null);

reset();
seedRows(0);
check('arrows: an empty list is not navigable', api.moveActiveRow(1) === false);

// ── 3. Enter acts on the highlighted row ──────────────────────────────────
reset();
seedRows(2);
check('enter: with nothing highlighted it falls through to a search', api.handleEnterKey() === false);

reset();
const enterRows = seedRows(2);
api.moveActiveRow(1);
check('enter: with a row highlighted it is consumed', api.handleEnterKey() === true);
check('enter: the highlighted row is activated', enterRows[0]._clicked === 1, enterRows[0]._clicked);

reset();
const second = seedRows(2);
api.moveActiveRow(1);
api.moveActiveRow(1);
api.handleEnterKey();
check('enter: activates the SECOND row when that is the highlighted one', second[1]._clicked === 1 && second[0]._clicked === 0, [second[0]._clicked, second[1]._clicked]);

reset();
dom.results._rows = [makeRow({ 'data-us-recent': 'pixies' }, 'DIV')];
api.moveActiveRow(1);
api.handleEnterKey();
check('enter: activating a recent runs a search', state.searchesRun === 1, state.searchesRun);
check('enter: activating a recent mirrors the text into the flyout', dom.input.value === 'pixies', dom.input.value);

reset();
dom.byId.navSearchInput = { value: 'stale' };
dom.results._rows = [makeRow({ 'data-us-recent': 'pixies' }, 'DIV')];
api.moveActiveRow(1);
api.handleEnterKey();
check('enter: activating a recent mirrors into the banner box too', dom.byId.navSearchInput.value === 'pixies', dom.byId.navSearchInput.value);

reset();
seedRows(2);
state.searchOpen = false;
check('enter: a closed flyout never activates a row', api.handleEnterKey() === false);

// ── Report ────────────────────────────────────────────────────────────────
const failed = checks.filter(function (c) { return !c.pass; });
console.log(JSON.stringify({ total: checks.length, failed: failed.length, checks: checks }));
