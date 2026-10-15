/**
 * Behavioural probe: the dashboard's "Needs Metadata Approval" list.
 *
 * Usage:  node tests/js/pending-approval-probe.js <path-to-pages/dashboard.js>
 * Output: a single JSON line on stdout.
 *
 * WHY A PROBE. The list is a SUPPRESSION surface as much as a display one:
 *   * it must be HIDDEN when there is nothing to approve (an up-to-date library
 *     must not show an empty panel), and
 *   * every row must LINK to the album page, because that is where the review
 *     and Save live — a row with no href is a dead end, and the row is an <a>.
 * A source-text assertion cannot tell a correct hide from an over-eager one, so
 * `renderPendingApproval` is brace-matched out of the SHIPPED source and driven
 * here against a DOM stub. It cannot silently drift from the code it tests.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const SRC_PATH = process.argv[2];
if (!SRC_PATH) {
  console.error('usage: node pending-approval-probe.js <path-to-dashboard.js>');
  process.exit(2);
}

const SRC = fs.readFileSync(SRC_PATH, 'utf8');

// ⭐ THE ROW MARKUP LIVES IN A SHARED MODULE NOW, AND THE PROBE FOLLOWS IT.
//
// `renderPendingApproval` kept its name and its tab behaviour, but the rows it
// emits come from `services/activity-feed.js` — shared with the Activity page
// so the two surfaces cannot render the same album differently.
//
// ⚠️ The feed functions are EXTRACTED, not stubbed. Two of this probe's
// assertions are about HTML escaping and URL encoding INSIDE the row markup, so
// stubbing the row builder would leave the probe asserting against its own stub:
// it would pass whatever the real code did. Resolved relative to the dashboard
// so it follows the tree under test rather than assuming one.
const FEED_PATH = path.resolve(path.dirname(SRC_PATH), '..', 'services', 'activity-feed.js');
if (!fs.existsSync(FEED_PATH)) {
  console.log(JSON.stringify({ error: 'shared feed module not found at ' + FEED_PATH }));
  process.exit(0);
}
const FEED_SRC = fs.readFileSync(FEED_PATH, 'utf8');

// Shared with the search-focus probe. It skips comments before tracking quotes,
// because an apostrophe in prose otherwise opens a phantom string literal.
const { extractFn } = require('./_extract.js');

// ── DOM stub ──────────────────────────────────────────────────────────────
function makeEl(id) {
  return {
    id: id,
    innerHTML: '',
    textContent: '',
    _classes: new Set(),
    classList: {
      add: function (c) { this._owner._classes.add(c); },
      remove: function (c) { this._owner._classes.delete(c); },
      contains: function (c) { return this._owner._classes.has(c); },
    },
    _owner: null,
  };
}

const els = {
  pendingApprovalTabItem: makeEl('pendingApprovalTabItem'),
  pendingApprovalTab: makeEl('pendingApprovalTab'),
  'pending-approval-body': makeEl('pending-approval-body'),
  'pending-approval-count': makeEl('pending-approval-count'),
};
Object.values(els).forEach(function (el) { el.classList._owner = el; });

const document_ = {
  getElementById: function (id) { return els[id] || null; },
  // renderPendingApproval calls this only when an emptied tab was the ACTIVE
  // one (to move the user off a pane that is about to be hidden). The probe
  // never marks the tab active, so it correctly never fires — but the lookup
  // has to exist or the harness throws on the reference.
  querySelector: function () { return null; },
};

// ── Build the harness from the shipped source ─────────────────────────────
const NEEDED_DASH = ['renderPendingApproval', 'renderPendingApprovalInto'];
const NEEDED_FEED = ['approvalRow', 'renderApprovalsInto'];

const missing = NEEDED_DASH.filter(function (n) { return !extractFn(SRC, n); })
  .map(function (n) { return 'dashboard:' + n; })
  .concat(NEEDED_FEED.filter(function (n) { return !extractFn(FEED_SRC, n); })
    .map(function (n) { return 'feed:' + n; }));
if (missing.length) {
  console.log(JSON.stringify({ error: 'missing functions: ' + missing.join(', ') }));
  process.exit(0);
}

const harness = [
  'var document = globalThis.__doc;',
  // dashboard.js reaches the shared module through `global.ActivityFeed`, so the
  // harness has to provide that same object rather than let it dangle.
  'var global = globalThis.__global;',
  'var esc = function (v) { return globalThis.__esc(v); };',
  'var formatScanTimestamp = function (v) { return globalThis.__fmt(v); };',
  ...NEEDED_FEED.map(function (n) { return extractFn(FEED_SRC, n); }),
  'global.ActivityFeed = { approvalRow: approvalRow, renderApprovalsInto: renderApprovalsInto };',
  ...NEEDED_DASH.map(function (n) { return extractFn(SRC, n); }),
  'globalThis.__render = renderPendingApproval;',
].join('\n');

globalThis.__doc = document_;
globalThis.__global = { ActivityFeed: {} };
globalThis.__esc = function (v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
};
globalThis.__fmt = function (v) { return v ? 'REFORMATTED(' + v + ')' : ''; };

let render;
try {
  render = new Function(harness + '\nreturn globalThis.__render;')();
} catch (err) {
  console.log(JSON.stringify({ error: 'harness build failed: ' + err.message }));
  process.exit(0);
}

// ── Scenarios ─────────────────────────────────────────────────────────────
function snapshot() {
  return {
    // The TAB item carries the d-none now — Needs Review is the activity
    // feed's third tab, so "is this list available" is the tab's visibility.
    hidden: els.pendingApprovalTabItem._classes.has('d-none'),
    count: els['pending-approval-count'].textContent,
    html: els['pending-approval-body'].innerHTML,
  };
}

function reset() {
  els.pendingApprovalTabItem._classes = new Set(['d-none']);
  els['pending-approval-body'].innerHTML = '';
  els['pending-approval-count'].textContent = '';
}

const results = {};

// 1. Empty list -> tab HIDDEN, count zero.
reset();
render([]);
results.empty_hidden = snapshot().hidden;
results.empty_count = snapshot().count;
results.empty_body = snapshot().html;

// 2. One album -> visible, count 1, links to the album page.
reset();
render([{
  artist: 'Ricky Martin',
  album: '17: Greatest Hits',
  album_year: '1999',
  change_count: 4,
  album_changes: 1,
  tracks_changed: 3,
  stashed_at: '2026-09-27T10:00:00',
}]);
let snap = snapshot();
results.one_visible = !snap.hidden;
results.one_count = snap.count;
results.one_links_album = /href="\/album\/Ricky%20Martin\/17%3A%20Greatest%20Hits\/1999"/.test(snap.html);
results.one_encodes_segments = snap.html.indexOf('/album/Ricky%20Martin/') !== -1;
results.one_shows_change_count = snap.html.indexOf('4 change') !== -1;
results.one_shows_timestamp = snap.html.indexOf('REFORMATTED(2026-09-27T10:00:00)') !== -1;
results.one_body = snap.html;

// 3. No year -> link omits the year segment (must not emit a trailing slash).
reset();
render([{ artist: 'A', album: 'B', change_count: 1, album_changes: 0, tracks_changed: 1 }]);
results.no_year_link = /href="\/album\/A\/B"/.test(snapshot().html);
results.no_year_no_trailing = /href="\/album\/A\/B"/.test(snapshot().html)
  && !/href="\/album\/A\/B\/"/.test(snapshot().html);

// 4. Singular/plural wording.
reset();
render([{ artist: 'A', album: 'B', change_count: 1, album_changes: 1, tracks_changed: 0 }]);
results.singular = snapshot().html.indexOf('1 change<') !== -1
  || snapshot().html.indexOf('1 change"') !== -1
  || /1 change\b(?!s)/.test(snapshot().html);
results.singular_body = snapshot().html;

// 5. Album with ONLY album-level changes still shows a row.
reset();
render([{ artist: 'A', album: 'B', change_count: 2, album_changes: 2, tracks_changed: 0 }]);
results.album_only_visible = !snapshot().hidden;

// 6. XSS: a title containing markup must be ESCAPED, not injected.
reset();
render([{
  artist: '<img src=x onerror=alert(1)>',
  album: '" onmouseover="alert(2)',
  change_count: 1, album_changes: 1, tracks_changed: 0,
}]);
results.escapes_html = snapshot().html.indexOf('<img src=x') === -1;
results.escapes_quotes = snapshot().html.indexOf('" onmouseover="alert(2)') === -1;
results.xss_body = snapshot().html;

// 7. Higher count pluralises.
reset();
render([{ artist: 'A', album: 'B', change_count: 12, album_changes: 2, tracks_changed: 10 }]);
results.plural_present = /12 changes/.test(snapshot().html);

console.log(JSON.stringify(results));
