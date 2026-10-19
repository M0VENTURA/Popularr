/**
 * Behavioural probe: the dashboard's two new alert surfaces.
 *
 * Usage:  node tests/js/dashboard-alerts-probe.js <path-to-dashboard.js>
 * Output: a single JSON line on stdout.
 *
 * WHY A PROBE. Both surfaces are SUPPRESSION surfaces as much as display ones,
 * and a source-text assertion cannot tell a correct hide from an over-eager one:
 *
 *   1. the "artists skipped / abandoned" banner must come back for a NEW
 *      abandonment but stay hidden for one the operator has already dismissed.
 *      A probe that only checks "a dismiss button exists" passes on an
 *      implementation that hides the banner for ever;
 *   2. the queue processor alert must be silent while the processor is healthy
 *      and present when it is stopped.
 *
 * Both files are driven through their SHIPPED functions — brace-matched out of
 * the source that is actually served — so the probe cannot drift from the code
 * under test. `abandonedBanner` / `renderQueueProcessorAlert` are pure
 * string-builders, which is what makes this possible without a real DOM.
 *
 * ONE probe, BOTH trees: the live tree's `abandonedBanner` calls `escapeHtml`
 * and the rebuilt one's calls `esc`, so whichever the source defines is
 * extracted and the other is supplied. Everything else is identical, and the
 * Python suite diffs the two trees' JSON — so a fix applied to only one of them
 * fails.
 */
'use strict';

const fs = require('fs');

const SRC_PATH = process.argv[2];
if (!SRC_PATH) {
  console.error('usage: node dashboard-alerts-probe.js <path-to-dashboard.js>');
  process.exit(2);
}

const SRC = fs.readFileSync(SRC_PATH, 'utf8');
const { extractFn } = require('./_extract.js');

// ── Extraction ────────────────────────────────────────────────────────────
//
// `extractFn` only understands `function name(`; the dismissed-alerts storage
// key is a `const`, so it is lifted separately. Both are reported as MISSING
// rather than defaulted — a probe that invents the key it is testing would
// happily pass while the shipped code used a different one.
function extractConst(source, name) {
  const match = source.match(new RegExp('const\\s+' + name + '\\s*=\\s*([^;]+);'));
  return match ? 'const ' + name + ' = ' + match[1] + ';' : null;
}

const REQUIRED = [
  'escapeAttr',
  'readDismissedAlerts',
  'writeDismissedAlerts',
  'abandonedFingerprint',
  'isAlertDismissed',
  'dismissDashboardAlert',
  'abandonedBanner',
  'renderQueueProcessorAlert',
];

const missing = [];
const parts = [];
try {
  for (const name of REQUIRED) {
    const fn = extractFn(SRC, name);
    if (!fn) missing.push(name);
    else parts.push(fn);
  }
} catch (err) {
  // `extractFn` THROWS on unbalanced braces rather than returning null. It does
  // not understand regex literals, so a function carrying a quote-bearing
  // character class lands here — reported as a clean probe failure rather than
  // an uncaught crash, which is a far better message to read.
  console.log(JSON.stringify({ error: 'extraction failed: ' + err.message }));
  process.exit(0);
}

const keySource = extractConst(SRC, 'DISMISSED_ALERTS_KEY');
if (!keySource) missing.push('DISMISSED_ALERTS_KEY');

// Exactly one of these exists per tree: live has `escapeHtml`, rebuilt has `esc`.
const hasEscapeHtml = !!extractFn(SRC, 'escapeHtml');
const hasEsc = !!extractFn(SRC, 'esc');
if (!hasEscapeHtml && !hasEsc) missing.push('escapeHtml|esc');

if (missing.length) {
  console.log(JSON.stringify({ error: 'missing from source: ' + missing.join(', ') }));
  process.exit(0);
}

// ── DOM stub ──────────────────────────────────────────────────────────────
//
// ⚠️ `__esc3` mirrors what the LIVE `escapeHtml` really does: it round-trips
// through createTextNode/innerHTML, which escapes `&`, `<` and `>` but NOT
// quotes. An all-five-entities stub here would make the attribute-escaping
// assertion below pass for a helper that never escapes quotes — the exact trap
// `tests/js/upcoming-art-probe.js` documents.
function esc3(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function esc5(value) {
  return esc3(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function makeEl(id) {
  const el = { id: id, innerHTML: '', textContent: '', _classes: new Set() };
  el.classList = {
    add: (c) => el._classes.add(c),
    remove: (c) => el._classes.delete(c),
    contains: (c) => el._classes.has(c),
    toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
  };
  return el;
}

function makeAlertEl(id) {
  return {
    _removed: false,
    getAttribute: (name) => (name === 'data-alert-id' ? id : null),
    remove() { this._removed = true; },
  };
}

const doc = {
  getElementById: () => null,
  querySelectorAll: () => globalThis.__alertEls,
  addEventListener: () => {},
  createElement: () => ({
    _text: '',
    appendChild(node) { this._text += String(node && node._text != null ? node._text : ''); },
    get innerHTML() { return globalThis.__esc3(this._text); },
  }),
  createTextNode: (text) => ({ _text: String(text) }),
};

globalThis.__esc3 = esc3;
globalThis.__esc5 = esc5;
globalThis.__alertEls = [];

const store = {};
globalThis.__localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};

// ── Harness ───────────────────────────────────────────────────────────────
const harness = [
  'var document = globalThis.__doc;',
  'var localStorage = globalThis.__localStorage;',
  'var global = globalThis.__global;',
  keySource,
  hasEscapeHtml
    ? extractFn(SRC, 'escapeHtml')
    : 'function escapeHtml(v) { return globalThis.__esc5(v); }',
  hasEsc
    ? extractFn(SRC, 'esc')
    // The rebuilt `esc` delegates to utils/dom.js, which is not in this file.
    // That module escapes all five entities, so the stub matches it.
    : 'function esc(v) { return globalThis.__esc5(v); }',
  ...parts,
  'globalThis.__probe = { abandonedBanner: abandonedBanner,'
    + ' abandonedFingerprint: abandonedFingerprint,'
    + ' isAlertDismissed: isAlertDismissed,'
    + ' dismissDashboardAlert: dismissDashboardAlert,'
    + ' renderQueueProcessorAlert: renderQueueProcessorAlert };',
].join('\n');

globalThis.__doc = doc;
// ⚠️ The rebuilt copy reaches storage through `global.localStorage` (it is an
// IIFE over `window`), the live copy through the bare global. BOTH have to be
// supplied or the rebuilt tree's dismissal silently no-ops here while working in
// a browser — the probe would then be asserting against its own harness.
globalThis.__global = { escapeHtml: esc5, localStorage: globalThis.__localStorage };

let probe;
try {
  probe = new Function(harness + '\nreturn globalThis.__probe;')();
} catch (err) {
  console.log(JSON.stringify({ error: 'harness build failed: ' + err.message }));
  process.exit(0);
}

// ── Scenarios ─────────────────────────────────────────────────────────────
const out = {};

function resetStore() {
  for (const k of Object.keys(store)) delete store[k];
  globalThis.__alertEls = [];
}

function scanFor(artists) {
  return {
    scan_type: 'full_scan',
    is_running: true,
    abandoned_artists: artists,
  };
}

const ONE = scanFor({ 'Visions Of Atlantis': [{ reason: 'exceeded 2700.0s budget', abandoned: true }] });
const OTHER = scanFor({ Candlemass: [{ reason: 'exceeded 900.0s budget', abandoned: true }] });

// 1. A fresh abandonment is shown, with a way out.
resetStore();
const first = probe.abandonedBanner(ONE);
out.banner_shown = first.indexOf('Artists skipped / abandoned') >= 0;
out.banner_is_alert = first.indexOf('alert-warning') >= 0;
out.banner_has_dismiss_button = first.indexOf('js-dismiss-alert') >= 0;
out.banner_has_alert_id = first.indexOf('data-alert-id=') >= 0;

// 2. Dismissing hides it, and it STAYS hidden for the same content.
const id = probe.abandonedFingerprint(ONE);
probe.dismissDashboardAlert(id);
out.dismissed_state_recorded = probe.isAlertDismissed(id) === true;
out.banner_hidden_after_dismiss = probe.abandonedBanner(ONE) === '';

// 3. ...but a DIFFERENT abandonment is not hidden by that dismissal. This is the
//    assertion that fails if the acknowledgement is keyed on the scan type or
//    on a plain "seen it" boolean.
out.other_fingerprint_differs = probe.abandonedFingerprint(OTHER) !== id;
out.other_abandonment_still_shown = probe.abandonedBanner(OTHER).indexOf('Candlemass') >= 0;

// 4. The dismissal actually removes the element on screen, rather than waiting
//    five seconds for the next poll to notice.
resetStore();
const alertId = probe.abandonedFingerprint(ONE);
const fakeEl = makeAlertEl(alertId);
globalThis.__alertEls = [fakeEl];
probe.dismissDashboardAlert(alertId);
out.dismiss_removes_element = fakeEl._removed === true;

// 5. An artist name containing a double quote must not break out of the
//    data-alert-id attribute. `escapeHtml`/`esc` alone would NOT save this —
//    only `escapeAttr` does, which is why the probe asserts on the escaped
//    value rather than on the attribute merely existing.
resetStore();
const QUOTED = scanFor({ 'Al "Test" Band': [{ reason: 'boom', abandoned: false }] });
const quotedHtml = probe.abandonedBanner(QUOTED);
const attrMatch = quotedHtml.match(/data-alert-id="([^"]*)"/);
out.quoted_attr_is_wellformed = !!attrMatch;
out.quoted_attr_is_escaped = !!attrMatch && attrMatch[1].indexOf('&quot;') >= 0;

// 6. The queue processor alert is silent while the processor is healthy...
const runningHost = makeEl('queueProcessorAlert');
probe.renderQueueProcessorAlert(runningHost, { status: 'running', message: 'all good' });
out.queue_running_hidden = runningHost._classes.has('d-none') && runningHost.innerHTML === '';

const restartingHost = makeEl('queueProcessorAlert');
probe.renderQueueProcessorAlert(restartingHost, { status: 'restarting' });
out.queue_restarting_hidden = restartingHost._classes.has('d-none');

// 7. ...and present, with a restart control, when it is stopped.
const stoppedHost = makeEl('queueProcessorAlert');
probe.renderQueueProcessorAlert(stoppedHost, {
  status: 'stopped',
  message: 'Queue processor is NOT running — no queue cycle has completed in the last 180s.',
  auto_restart: {
    enabled: true,
    restart_count: 2,
    last_restart_iso: '2026-10-19T02:30:00+00:00',
  },
});
out.queue_stopped_shown = !stoppedHost._classes.has('d-none') && stoppedHost.innerHTML.length > 0;
out.queue_stopped_says_not_running = stoppedHost.innerHTML.indexOf('not running') >= 0;
out.queue_stopped_has_restart = stoppedHost.innerHTML.indexOf('js-restart-queue-processor') >= 0;
out.queue_stopped_reports_auto_restart = stoppedHost.innerHTML.indexOf('restarted 2 times') >= 0;
out.queue_stopped_escapes_message = stoppedHost.innerHTML.indexOf('—') >= 0;

// 8. With auto-restart switched off the alert says so, rather than implying help
//    is coming.
const offHost = makeEl('queueProcessorAlert');
probe.renderQueueProcessorAlert(offHost, {
  status: 'stopped',
  message: 'not running',
  auto_restart: { enabled: false, restart_count: 0 },
});
out.queue_auto_off_explained = offHost.innerHTML.indexOf('Auto-restart is turned off') >= 0;

console.log(JSON.stringify(out));
