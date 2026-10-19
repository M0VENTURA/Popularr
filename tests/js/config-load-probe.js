/**
 * Behavioural probe: does the Config page's config collector ACTUALLY RUN?
 *
 * Usage:  node tests/js/config-load-probe.js <path-to-config.js>
 * Output: a single JSON line on stdout.
 *
 * WHY THIS IS NOT A SOURCE ASSERTION
 * ----------------------------------
 * The Config page died with
 *
 *     Could not collect config: getValue is not defined
 *
 * on EVERY save. The cause was pure SCOPING: ``collectGenreSelectionConfig()``
 * — a top-level/IIFE-scope function — called ``getValue``/``getChecked``, which
 * were declared *inside* ``buildConfigObject()``. A function's free variables
 * resolve where the function is DEFINED, not where it is CALLED, so being
 * invoked from inside ``buildConfigObject`` did not help.
 *
 * ⚠️ ``tests/test_genre_selection_engine.py`` already asserted the file
 * CONTAINS ``selection: collectGenreSelectionConfig()`` — and passed the whole
 * time the page was broken. ``extractFn`` is name-based too: it happily pulls a
 * NESTED ``getValue`` out of ``buildConfigObject``, so an extraction-based
 * harness would define the helper and never reproduce the bug.
 *
 * The only harness that sees real scope is the whole file, so this loads the
 * SHIPPED script into a vm context with a DOM stub and CALLS the collector.
 * Pre-fix it throws the reported ReferenceError; post-fix it returns config.
 */
'use strict';

const fs = require('fs');
const vm = require('vm');

const SRC_PATH = process.argv[2];
if (!SRC_PATH) {
  console.error('usage: node config-load-probe.js <path-to-config.js>');
  process.exit(2);
}

const SRC = fs.readFileSync(SRC_PATH, 'utf8');

// ── DOM stub ──────────────────────────────────────────────────────────────
//
// Every element is absent: `getValue`/`getChecked` then return their defaults,
// which is exactly what a page with no inputs would produce. That keeps the
// probe from having to model hundreds of fields while still executing the real
// collection code.
function makeElement(id) {
  return {
    id,
    value: '',
    checked: false,
    textContent: '',
    innerHTML: '',
    dataset: {},
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    appendChild() {},
    remove() {},
    querySelectorAll: () => [],
    querySelector: () => null,
  };
}

const documentStub = {
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => makeElement(tag),
  createTextNode: (text) => ({ textContent: String(text) }),
  // Recorded, never invoked: the page's INIT block must not run here (it would
  // need the whole page). It is registered at load time and that is all this
  // probe cares about.
  addEventListener() {},
  body: makeElement('body'),
  head: makeElement('head'),
};

const sandbox = {
  console: { log() {}, warn() {}, error() {}, debug() {}, info() {} },
  document: documentStub,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: () => Promise.reject(new Error('no network in probe')),
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  pageConfig: {
    playlists: {
      excluded_artists: ['Artist A', 'Artist B'],
    },
  },
};
// `pageConfig` is read as `window.pageConfig` in the live copy and
// `global.pageConfig` in the rebuilt one; both resolve to this sandbox once
// `window` points at it.
sandbox.window = sandbox;

const out = {};

let context;
try {
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: SRC_PATH, timeout: 10000 });
  out.loaded = true;
} catch (error) {
  out.loaded = false;
  out.load_error = error && error.message ? error.message : String(error);
}

// The page normally initialises the excluded-artists list from pageConfig in
// its DOMContentLoaded handler; that event never fires in this stub, so run the
// loader explicitly when the script exposes it.
if (out.loaded && typeof sandbox.loadExcludedArtists === 'function') {
  try {
    sandbox.loadExcludedArtists();
  } catch (error) {
    out.excluded_load_error = error && error.message ? error.message : String(error);
  }
}

// Both trees publish the collector: the live copy as a bare global function,
// the rebuilt one on `global.config`.
function resolveBuild() {
  if (typeof sandbox.buildConfigObject === 'function') return sandbox.buildConfigObject;
  if (sandbox.config && typeof sandbox.config.buildConfigObject === 'function') {
    return sandbox.config.buildConfigObject;
  }
  return null;
}

const build = resolveBuild();
out.build_resolved = typeof build === 'function';

if (out.build_resolved) {
  try {
    const config = build();
    out.collect_ok = true;
    // CONTROL: prove the call did real work rather than returning undefined.
    const genres = config && config.genres;
    const selection = genres && genres.selection;
    out.collect_shape = {
      is_object: !!config && typeof config === 'object',
      has_queue: !!(config && config.queue),
      has_playlists: !!(config && config.playlists),
      // ⚠️ `genres.selection`, not `genre_selection` — the collector is carried
      // under the `genres` section, alongside the vocabulary weights.
      has_genres_selection: !!selection,
    };
    out.selection_is_object = !!selection && typeof selection === 'object';
    // Concrete values, so "it returned an object" cannot pass for "it ran".
    // With no DOM inputs every reader falls back to its default.
    out.selection_enabled = selection ? selection.enabled : null;
    out.selection_max_genres = selection ? selection.max_genres : null;
    out.selection_family_slot_limit = selection ? selection.family_slot_limit : null;
    // Defaults from collectGenreSelectionConfig itself.
    out.selection_seems_populated =
      !!selection &&
      selection.enabled === true &&
      selection.max_genres === 3 &&
      selection.family_slot_limit === 3;
    // Excluded artists must round-trip from pageConfig through the collector.
    const excluded = config && config.playlists && config.playlists.excluded_artists;
    out.excluded_artists = excluded || null;
  } catch (error) {
    out.collect_ok = false;
    // The reported symptom, verbatim, when this regresses.
    out.collect_error = error && error.message ? error.message : String(error);
  }
}

console.log(JSON.stringify(out));
