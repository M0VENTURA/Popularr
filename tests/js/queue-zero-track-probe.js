/**
 * Behavioural probe: a queue request that enqueues nothing must NOT be
 * reported as success.
 *
 * Usage:  node tests/js/queue-zero-track-probe.js <path-to-search-flyout.js>
 * Output: a single JSON line on stdout.
 *
 * WHY THIS LIVES IN A FILE
 * ------------------------
 * It has to drive the SHIPPED `queueRelease`, not a reimplementation. The
 * functions are brace-matched out of the source and spliced into an IIFE, so
 * this probe cannot silently drift from the code it claims to test.
 *
 * THE BUG
 * -------
 * `queueRelease` awaited `api.postJson` and then called `settle(true)` and
 * `toast.queued(...)` without inspecting the response. The endpoint now
 * answers 200 with `queued: false` + `queued_tracks: 0` when the release is
 * already owned or already queued (it cannot use a 4xx because postJson
 * throws on non-2xx, which would hide the specific reason). A version of this
 * function that ignores that field marks the button "Queued" and shows a
 * green pill for a download that will never happen.
 */
'use strict';

const fs = require('fs');

const SRC_PATH = process.argv[2];
if (!SRC_PATH) {
  console.error('usage: node queue-zero-track-probe.js <path-to-search-flyout.js>');
  process.exit(2);
}

const SRC = fs.readFileSync(SRC_PATH, 'utf8');

/** Brace-match `function <name>(...) { ... }` out of a source string.
 *
 * Includes a leading `async`/`export` modifier when present — slicing from the
 * bare `function` keyword would drop `async` from `async function queueRelease`
 * and the spliced copy would then throw "await is only valid in async
 * functions" at build time.
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
      if (depth === 0) return source.slice(begin, i + 1);
    }
  }
  throw new Error('unbalanced braces extracting ' + name);
}

// ── Stub collaborators ────────────────────────────────────────────────────
const state = { queuedToasts: 0, warnings: 0, errors: [], lastWarning: null, posted: 0 };

globalThis.window = globalThis;

globalThis.toast = {
  queued: function () { state.queuedToasts++; },
  warning: function (m) { state.warnings++; state.lastWarning = m; },
  error: function (m) { state.errors.push(m); },
};

// The response this probe feeds back. Swapped per scenario.
let nextResponse = { success: true, queued: true, queued_tracks: 3, tracking_id: 9 };
let nextThrows = false;

globalThis.api = {
  postJson: function () {
    state.posted++;
    if (nextThrows) return Promise.reject(new Error('HTTP 500: boom'));
    return Promise.resolve(nextResponse);
  },
};

// ── Build the harness from the shipped source ─────────────────────────────
const needed = ['notifyError', 'markQueued', 'queueRelease'];
const missing = needed.filter(function (n) { return !extractFn(SRC, n); });
if (missing.length) {
  console.log(JSON.stringify({ error: 'missing functions: ' + missing.join(', ') }));
  process.exit(0);
}

const harness = [
  'var _queuedIds = Object.create(null);',
  'var _queuedInFlight = Object.create(null);',
  'function mbReleaseArtist(rel) { return (rel && rel.artist) || ""; }',
  'function getErrorEl() { return null; }',
  'var __state = globalThis.__probeState;',
  ...needed.map(function (n) { return extractFn(SRC, n); }),
  'globalThis.__queueRelease = queueRelease;',
  'globalThis.__queuedIds = _queuedIds;',
].join('\n');

globalThis.__probeState = state;

let queueRelease;
try {
  queueRelease = new Function(harness + '\nreturn globalThis.__queueRelease;')();
} catch (err) {
  console.log(JSON.stringify({ error: 'harness build failed: ' + err.message }));
  process.exit(0);
}

// ── Fake button ───────────────────────────────────────────────────────────
function makeButton() {
  return {
    innerHTML: '<i class="bi bi-download"></i> Queue',
    disabled: false,
    classList: {
      _c: ['btn-outline-primary'],
      replace: function (a, b) {
        const i = this._c.indexOf(a);
        if (i >= 0) this._c[i] = b; else this._c.push(b);
      },
      contains: function (c) { return this._c.indexOf(c) >= 0; },
    },
  };
}

const checks = [];
function check(name, cond, detail) {
  checks.push({ name: name, pass: !!cond, detail: detail === undefined ? null : detail });
}

// `openReleasePicker` is deliberately absent so queueRelease takes its direct
// POST branch — that is the branch that ignored the response.
delete globalThis.openReleasePicker;

(async function run() {
  // 1. Zero tracks → warning, no queued toast, button NOT marked Queued.
  state.queuedToasts = 0;
  state.warnings = 0;
  nextResponse = {
    success: false, queued: false, queued_tracks: 0, tracking_id: null,
    reason: 'all_in_library',
    message: 'Every track in this release is already in your library.',
  };
  const b1 = makeButton();
  await queueRelease({ id: 'rel-1', title: 'Abyss', artist: 'Ad Infinitum' }, b1);
  check('zero tracks: no queued toast', state.queuedToasts === 0, state.queuedToasts);
  check('zero tracks: warning shown', state.warnings === 1, state.warnings);
  check(
    'zero tracks: warning carries the API message',
    /already in your library/i.test(state.lastWarning || ''),
    state.lastWarning,
  );
  check('zero tracks: button not marked Queued', !b1.disabled, b1.disabled);
  check(
    'zero tracks: button class untouched',
    b1.classList.contains('btn-outline-primary') && !b1.classList.contains('btn-success'),
    b1.classList._c.join(','),
  );

  // 1b. A retry after a no-op must be allowed (the in-flight guard was cleared).
  state.warnings = 0;
  const b1b = makeButton();
  await queueRelease({ id: 'rel-1', title: 'Abyss', artist: 'Ad Infinitum' }, b1b);
  check('zero tracks: retryable', state.posted === 2, state.posted);

  // 1c. THE ORIGINAL SERVER SHAPE: success:true but queued_tracks:0.
  //     This is literally what the buggy endpoint returned, so the guard must
  //     trust the track count over the `success` flag. Without this case a
  //     regression that only reads `success` would slip through.
  state.queuedToasts = 0;
  state.warnings = 0;
  nextResponse = {
    success: true, queued_tracks: 0, tracking_id: null,
    message: 'Download queued for Abyss (0 tracks)',
  };
  const b1c = makeButton();
  await queueRelease({ id: 'rel-1c', title: 'Abyss', artist: 'Ad Infinitum' }, b1c);
  check('success:true + 0 tracks: no queued toast', state.queuedToasts === 0, state.queuedToasts);
  check('success:true + 0 tracks: warning shown', state.warnings === 1, state.warnings);
  check('success:true + 0 tracks: button not marked Queued', !b1c.disabled, b1c.disabled);

  // 1d. The explicit `queued:false` contract on its own, with no track count.
  //     The current server always pairs queued:false with success:false, but
  //     the flag is documented as authoritative, so the guard must honour it
  //     independently of `success` and of the count.
  state.queuedToasts = 0;
  state.warnings = 0;
  nextResponse = {
    success: true, queued: false, tracking_id: null,
    message: 'Nothing to queue for Abyss.',
  };
  const b1d = makeButton();
  await queueRelease({ id: 'rel-1d', title: 'Abyss', artist: 'Ad Infinitum' }, b1d);
  check('queued:false alone: no queued toast', state.queuedToasts === 0, state.queuedToasts);
  check('queued:false alone: warning shown', state.warnings === 1, state.warnings);
  check('queued:false alone: button not marked Queued', !b1d.disabled, b1d.disabled);

  // 2. Real success still behaves exactly as before.
  state.queuedToasts = 0;
  state.warnings = 0;
  nextResponse = { success: true, queued: true, queued_tracks: 3, tracking_id: 9 };
  const b2 = makeButton();
  await queueRelease({ id: 'rel-2', title: 'Abyss', artist: 'Ad Infinitum' }, b2);
  check('success: queued toast fires', state.queuedToasts === 1, state.queuedToasts);
  check('success: no warning', state.warnings === 0, state.warnings);
  check('success: button marked Queued', b2.disabled === true, b2.disabled);

  // 2b. Already-marked release is not re-queued.
  const before = state.posted;
  await queueRelease({ id: 'rel-2', title: 'Abyss', artist: 'Ad Infinitum' }, makeButton());
  check('success: second click ignored', state.posted === before, state.posted - before);

  // 3. Transport failure still routes to the error path (not the warning one).
  state.queuedToasts = 0;
  state.warnings = 0;
  state.errors = [];
  nextThrows = true;
  const b3 = makeButton();
  await queueRelease({ id: 'rel-3', title: 'Abyss', artist: 'Ad Infinitum' }, b3);
  nextThrows = false;
  check('transport error: no queued toast', state.queuedToasts === 0, state.queuedToasts);
  check('transport error: no warning', state.warnings === 0, state.warnings);
  check('transport error: error surfaced', state.errors.length === 1, state.errors);

  // 4. Success with queued_tracks but no explicit queued flag still counts as
  //    success (back-compat with an older server).
  state.queuedToasts = 0;
  state.warnings = 0;
  nextResponse = { success: true, tracking_id: 5, queued_tracks: 2 };
  const b4 = makeButton();
  await queueRelease({ id: 'rel-4', title: 'Abyss', artist: 'Ad Infinitum' }, b4);
  check('legacy success shape: queued toast fires', state.queuedToasts === 1, state.queuedToasts);
  check('legacy success shape: button marked Queued', b4.disabled === true, b4.disabled);

  const failed = checks.filter(function (c) { return !c.pass; });
  console.log(JSON.stringify({ total: checks.length, failed: failed.length, checks: checks }));
})();
