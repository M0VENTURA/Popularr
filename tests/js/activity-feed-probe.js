/**
 * Behavioural probe for the shared activity feed module.
 *
 * Usage:  node tests/js/activity-feed-probe.js <scenario> <path-to-activity-feed.js>
 * Output: a single JSON line on stdout.
 *
 * WHY A PROBE. The module's whole job is to decide WHICH scan rows belong
 * together and WHAT status the collapsed entry has. A source-text assertion
 * cannot tell a correct grouping from one that drops a phase, and it cannot
 * tell "worst status wins" from "last status wins" — both read as plausible
 * code. So the REAL module is loaded and driven, the same technique the other
 * probes in this directory use.
 *
 * The module is an IIFE assigned onto `window`, so the harness supplies a
 * `window` and reads the published `ActivityFeed` back out of it.
 */
'use strict';

const fs = require('fs');

const scenario = process.argv[2];
const modulePath = process.argv[3];
if (!scenario || !modulePath) {
  console.error('usage: node activity-feed-probe.js <scenario> <path-to-activity-feed.js>');
  process.exit(2);
}

const SRC = fs.readFileSync(modulePath, 'utf8');

// ── Load the REAL module into a stand-in global ───────────────────────────
// It only needs `window.escapeHtml` (optional) and, at call time, a
// `statusBadge` for the badge — supplied here so a render never throws on a
// missing helper, which would look like a feed bug rather than a harness gap.
const window_ = {
  escapeHtml: function (v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  },
  statusBadge: {
    render: function (status) {
      return '<span class="badge">' + String(status == null ? '' : status) + '</span>';
    },
  },
};

try {
  new Function('window', SRC)(window_);
} catch (err) {
  console.log(JSON.stringify({ error: 'module load failed: ' + err.message }));
  process.exit(0);
}

const feed = window_.ActivityFeed;
if (!feed) {
  console.log(JSON.stringify({ error: 'ActivityFeed was not published' }));
  process.exit(0);
}

// ── Scenarios ─────────────────────────────────────────────────────────────
const out = {};

if (scenario === 'grouping') {
  // One album scanned by two phases: scan_history writes a row per phase.
  const grouped = feed.groupRecentScans([
    { artist: 'Nirvana', album: 'Nevermind', scan_type: 'metadata',
      status: 'completed', scan_timestamp: '2026-10-16T10:00:00' },
    { artist: 'Nirvana', album: 'Nevermind', scan_type: 'popularity',
      status: 'completed', scan_timestamp: '2026-10-16T10:05:00' },
  ]);
  out.entries = grouped.length;
  out.types = grouped[0].scan_types.map(function (t) {
    return feed.scanDisplayName(t.type);
  });
} else if (scenario === 'status') {
  // A LATER completed phase must not hide an EARLIER failure.
  const grouped = feed.groupRecentScans([
    { artist: 'A', album: 'B', scan_type: 'metadata', status: 'failed',
      scan_timestamp: '2026-10-16T10:00:00' },
    { artist: 'A', album: 'B', scan_type: 'popularity', status: 'completed',
      scan_timestamp: '2026-10-16T10:05:00' },
  ]);
  out.status = grouped[0].status;
} else if (scenario === 'in_progress') {
  const grouped = feed.groupRecentScans([
    { artist: 'A', album: 'B', scan_type: 'metadata', status: 'completed',
      scan_timestamp: '2026-10-16T12:00:00' },
    { artist: 'C', album: 'D', scan_type: 'popularity', _inProgress: true,
      scan_timestamp: '2026-10-16T09:00:00' },
  ]);
  out.entries = grouped.length;
  // The live scan is OLDER, so recency sorting alone would bury it.
  out.first_is_in_progress = grouped[0]._inProgress === true;
} else if (scenario === 'labels') {
  out.known = feed.scanDisplayName('full_scan');
  out.unknown = feed.scanDisplayName('some_future_scan');
} else if (scenario === 'timestamps') {
  out.bad = feed.formatScanTimestamp('not-a-date');
  out.missing = feed.formatScanTimestamp(null);
} else if (scenario === 'escaping') {
  const html = feed.scanRow({
    artist: '<img src=x onerror=alert(1)>',
    album: '" onmouseover="alert(2)',
    status: 'completed',
    scan_types: [{ type: 'metadata' }],
    latest_timestamp: '2026-10-16T10:00:00',
  });
  out.escapes_html = html.indexOf('<img src=x') === -1;
  out.encodes_path = html.indexOf('/artist/%3Cimg%20src%3Dx') !== -1;
  out.html = html;
} else {
  out.error = 'unknown scenario: ' + scenario;
}

console.log(JSON.stringify(out));
