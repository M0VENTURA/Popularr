/**
 * Behavioural probe for the queue button's loading / in-flight behaviour.
 *
 * Usage:  node tests/js/queue-button-probe.js <path-to-search-js> [--expect-bug]
 * Output: a single JSON line on stdout.
 *
 * WHY A STANDALONE FILE: this has to exercise the SHIPPED source, not a
 * reimplementation. Embedding the JS as a Python string made the quoting
 * unmaintainable, so the probe lives here and the pytest wrapper just runs it.
 *
 * WHAT IT PROVES
 * --------------
 * The reported bug: clicking Queue showed nothing, and clicking a few times
 * produced several popups together ~20s later. That is a concurrency bug, so
 * this drives the real `queueRelease` with stub collaborators and counts how
 * many release-picker probes five rapid clicks start.
 *
 * It extracts the functions by brace-matching, so it tests the shipped text —
 * remove the in-flight guard and this fails.
 */
'use strict';

const fs = require('fs');

const SEARCH_PATH = process.argv[2];
const EXPECT_BUG = process.argv.includes('--expect-bug');
if (!SEARCH_PATH) {
  console.error('usage: node queue-button-probe.js <path-to-search-js>');
  process.exit(2);
}

const SRC = fs.readFileSync(SEARCH_PATH, 'utf8');

/** Brace-match `function <name>(...) { ... }` out of a source string. */
function extractFn(source, name) {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) return null;
  let i = source.indexOf('{', start);
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
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced braces extracting ' + name);
}

// ── Stub collaborators ────────────────────────────────────────────────────
const state = { probes: 0, pending: [], toasts: 0, errors: [] };

globalThis.window = globalThis;

globalThis.window.openReleasePicker = function (id, title, artist, onQueued) {
  state.probes++;
  return new Promise((resolve) => state.pending.push({ onQueued, resolve }));
};

globalThis.window.showQueueToast = function () { state.toasts++; };

globalThis.api = {
  postJson: function () {
    return Promise.resolve({ success: true });
  },
};

globalThis.buttonState = {
  setBusy: function (btn) {
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    return function restore() {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
      btn.innerHTML = original;
    };
  },
};

globalThis.toast = { queued: function () { state.toasts++; }, error: function () {} };

// ── Build the harness from the shipped source ─────────────────────────────
const extracted = [
  'setQueueBtnBusy', 'restoreQueueBtn', 'markQueueBtnDone', 'markQueued',
].map((n) => extractFn(SRC, n)).filter(Boolean);

const harness = [
  'var _queuedIds = {};',
  'var _queuedInFlight = {};',
  'const QUEUE_BTN_IDLE_HTML = "<i class=\\"bi bi-download\\"></i> Queue";',
  'function esc(v) { return String(v == null ? "" : v); }',
  'function mbReleaseArtist(rel) { return (rel && rel.artist) || ""; }',
  'function notifyError(msg) { globalThis.__errors.push(msg); }',
  'function fetch() { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ success: true }); } }); }',
  ...extracted,
  extractFn(SRC, 'queueRelease'),
  'globalThis.__queueRelease = queueRelease;',
].join('\n');

globalThis.__errors = state.errors;

let queueRelease;
try {
  queueRelease = new Function(harness + '\nreturn globalThis.__queueRelease;')();
} catch (err) {
  console.log(JSON.stringify({ error: 'harness build failed: ' + err.message }));
  process.exit(0);
}

// ── Fake button ──────────────────────────────────────────────────────────
function makeBtn() {
  const classes = new Set(['btn', 'btn-sm', 'btn-outline-primary']);
  return {
    disabled: false,
    innerHTML: '<i class="bi bi-download"></i> Queue',
    _attrs: {},
    classList: {
      contains: (c) => classes.has(c),
      replace: (a, b) => { classes.delete(a); classes.add(b); },
    },
    setAttribute(k, v) { this._attrs[k] = v; },
    removeAttribute(k) { delete this._attrs[k]; },
    getAttribute(k) { return this._attrs[k]; },
    get isBusy() { return this._attrs['aria-busy'] === 'true'; },
    get isQueued() { return classes.has('btn-success'); },
    get hasSpinner() { return this.innerHTML.indexOf('spinner-border') >= 0; },
  };
}

const rel = { id: 'rg-123', title: 'Album', artist: 'Artist' };
const btn = makeBtn();

// ── The scenario: five rapid clicks ──────────────────────────────────────
for (let i = 0; i < 5; i++) queueRelease(rel, btn);

const duringFlight = {
  probes: state.probes,
  spinner: btn.hasSpinner,
  disabled: btn.disabled,
  ariaBusy: btn.isBusy,
  queued: btn.isQueued,
};

// Settle WITHOUT picking a version: the multi-version flyout is open, so the
// button must return to a usable state.
if (state.pending.length) state.pending[0].resolve();

setImmediate(() => {
  const afterSettle = {
    disabled: btn.disabled,
    ariaBusy: btn.isBusy,
    spinner: btn.hasSpinner,
    queued: btn.isQueued,
    label: btn.innerHTML,
  };

  const result = {
    probesStarted: duringFlight.probes,
    spinnerDuring: duringFlight.spinner,
    disabledDuring: duringFlight.disabled,
    ariaBusyDuring: duringFlight.ariaBusy,
    queuedDuring: duringFlight.queued,
    restoredAfterSettle:
      afterSettle.disabled === false &&
      afterSettle.ariaBusy === false &&
      afterSettle.spinner === false &&
      afterSettle.queued === false,
    errors: state.errors,
  };

  // In --expect-bug mode we are running the OLD source and asserting it is
  // broken, so exit 0 either way and let the caller judge the JSON.
  console.log(JSON.stringify(result));
});
