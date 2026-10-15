/**
 * Behavioural probe: WHICH box does `openUnifiedSearch()` put the caret in?
 *
 * Usage:  node tests/js/search-focus-probe.js <path-to-ui/search-flyout.js>
 * Output: a single JSON object on stdout.
 *
 * WHY A PROBE. The reported bug was "the Search button brings up the modal, but
 * the search entry isn't there". That is a question about which element ends up
 * FOCUSED at a given viewport width, and no source-text assertion can answer it:
 * `input.focus()` is present in both the broken and the fixed version — what
 * changes is whether the element it names is on screen at the time.
 *
 * `openUnifiedSearch` is brace-matched out of the SHIPPED source and driven
 * against a DOM stub whose `offsetWidth` models the real responsive classes:
 *
 *   at lg+   `.nav-search-wrapper` is `d-none d-lg-block` -> navbar box VISIBLE
 *            the flyout's own wrapper is `d-lg-none`     -> flyout box HIDDEN
 *   below lg the reverse.
 *
 * So the probe asserts the CARE TARGET per width, which is the actual contract.
 */
'use strict';

const fs = require('fs');

const SRC_PATH = process.argv[2];
if (!SRC_PATH) {
  console.error('usage: node search-focus-probe.js <path-to-search-flyout.js>');
  process.exit(2);
}

const SRC = fs.readFileSync(SRC_PATH, 'utf8');

const { extractFn } = require('./_extract.js');

// ── the DOM stub ──────────────────────────────────────────────────────────
//
// `offsetWidth` is what `isVisible()` measures, so each element reports a width
// of 100 when it is "on screen" at the current breakpoint and 0 when it is not.
// `getClientRects()` is stubbed to agree, because `isVisible` ORs the three.
function makeInput(id) {
  const el = {
    id: id,
    value: '',
    _visible: false,
    _focused: false,
    className: '',
    classList: { add() {}, remove() {}, contains() { return false; } },
    style: {},
    offsetWidth: 0,
    offsetHeight: 0,
    getClientRects() { return el._visible ? [{}] : []; },
    focus() { el._focused = true; globalThis.__focusLog.push(id); },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
  };
  return el;
}

const navInput = makeInput('navSearchInput');
const flyoutInput = makeInput('unifiedSearchInput');
const modalEl = {
  id: 'unifiedSearchModal',
  className: '',
  classList: { add() {}, remove() {}, contains() { return false; } },
  addEventListener() {},
};

const els = {
  navSearchInput: navInput,
  unifiedSearchInput: flyoutInput,
  unifiedSearchModal: modalEl,
  searchBackdrop: {
    classList: { add() {}, remove() {}, contains() { return false; } },
  },
};

globalThis.__focusLog = [];

const document_ = {
  getElementById: (id) => els[id] || null,
  activeElement: null,
  querySelector: () => null,
  addEventListener() {},
};

// ── harness ───────────────────────────────────────────────────────────────
//
// Only `function` DECLARATIONS are extractable; `getInputEl` / `getModalEl` are
// one-line arrow consts that just look an element up, so they are stubbed
// against the same DOM stub. The subject here is `openUnifiedSearch`'s
// branching, not the element lookup.
const needed = ['openUnifiedSearch', 'isVisible'];
const missing = needed.filter((n) => !extractFn(SRC, n));
if (missing.length) {
  console.log(JSON.stringify({ error: 'missing functions: ' + missing.join(', ') }));
  process.exit(0);
}

const stubs = [
  'var document = globalThis.__doc;',
  'var global = globalThis.__g;',
  'const getModalEl = () => document.getElementById("unifiedSearchModal");',
  'const getInputEl = () => document.getElementById("unifiedSearchInput");',
  'function selectScope() {}',
  'function setAdvancedFiltersVisible() {}',
  'function getErrorEl() { return null; }',
  'function resetActiveRow() {}',
  'function runSearch() { globalThis.__ranSearch = true; }',
  'var _scope = "all";',
  'var _lastQuery = null;',
  'var _lastScope = null;',
];
const harness = [
  ...stubs,
  ...needed.map((n) => extractFn(SRC, n)),
  'globalThis.__open = openUnifiedSearch;',
].join('\n');

globalThis.__doc = document_;
globalThis.__g = {};

let open;
try {
  open = new Function(harness + '\nreturn globalThis.__open;')();
} catch (err) {
  console.log(JSON.stringify({ error: 'harness build failed: ' + err.message }));
  process.exit(0);
}

// ── scenarios ─────────────────────────────────────────────────────────────
function viewport(kind) {
  const mobile = kind === 'mobile';
  navInput._visible = !mobile;       // `d-none d-lg-block`
  flyoutInput._visible = mobile;     // `d-lg-none`
  navInput._focused = false;
  flyoutInput._focused = false;
  navInput.value = '';
  flyoutInput.value = '';
  globalThis.__focusLog = [];
  globalThis.__ranSearch = false;
  // ⚠️ NOT the element we expect to be focused. `openUnifiedSearch` guards each
  // call with `input !== document.activeElement`, so pointing activeElement at
  // the expected target makes the guard short-circuit and the probe pass while
  // focusing NOTHING — the vacuous version of this probe reported
  // `mobile_focuses_flyout: false` for that reason. `null` models the real case
  // anyway: the user tapped the bottom-nav button, so focus is on the button.
  document_.activeElement = null;
}

const results = {};

// 1. MOBILE — the reported bug. The caret must land in the flyout's own box,
//    because the navbar's is display:none and cannot receive focus at all.
viewport('mobile');
open();
results.mobile_focuses_flyout = flyoutInput._focused;
results.mobile_does_not_focus_navbar = !navInput._focused;

// 2. DESKTOP — unchanged behaviour: the navbar field is the hero input and the
//    flyout's own box is hidden, so focus must go to the navbar.
viewport('desktop');
open();
results.desktop_focuses_navbar = navInput._focused;
results.desktop_does_not_focus_flyout = !flyoutInput._focused;

// 3. MOBILE reopen must not WIPE a query typed the previous time. The banner
//    boxes are `d-none` and permanently empty below lg, so a prefill that reads
//    only them assigns '' over what the user typed.
viewport('mobile');
flyoutInput.value = 'audioslave';
document_.activeElement = null;
open();
results.mobile_reopen_preserves_query = flyoutInput.value === 'audioslave';

// 4. DESKTOP: the navbar field still wins the prefill when it has text.
viewport('desktop');
navInput.value = 'cochise';
flyoutInput.value = 'stale';
open();
results.desktop_navbar_wins_the_prefill = flyoutInput.value === 'cochise';

// 5. An explicit prefill argument still overrides everything.
viewport('desktop');
navInput.value = 'ignored';
document_.activeElement = null;
open('all', 'explicit');
results.explicit_prefill_wins = flyoutInput.value === 'explicit';

console.log(JSON.stringify(results));
