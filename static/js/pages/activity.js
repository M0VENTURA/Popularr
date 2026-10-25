/* ==========================================================================
   static/js/pages/activity.js
   The full Activity view (templates/pages/activity.html).

   WHAT THIS PAGE OWNS, AND WHAT IT DOES NOT
   -----------------------------------------
   The dashboard card summarises three feeds inside a single tab strip, capped
   to fit (12 albums, 25 scans, 50 reviews). This page shows the same three
   feeds STACKED with higher caps, because "View all" means the user wants
   everything at once rather than three tab clicks.

   ⭐ THE RENDERING IS NOT HERE. `js/activity-feed.js` owns the grouping, the
   labels, the timestamps and the row markup, and the dashboard card uses the
   same module — so a scan cannot render one way on this page and another way
   on the card. What lives here is only what is specific to THIS page: which
   containers the feeds go into, how many items to ask for, and the per-section
   counts.

   ⚠️ MIGRATED FROM THE REBUILT TREE, WITH ITS TWO DEPENDENCIES REMOVED.
   The rebuilt version called `global.api.getJson(...)` and `global.poller`,
   which are part of that tree's utils/ui layer. The live tree has neither, so
   this version uses `fetch` directly and a visibility-guarded `setInterval`
   that reproduces what `ui/poller.js` did: no ticks on a hidden tab, no
   overlapping requests, and the timer cleared on unload. Porting `api.js` and
   `poller.js` wholesale would mean replacing every other live script's fetch
   calls too, which is the cutover — not this migration.

   Load order:  js/activity-feed.js → js/pages/activity.js
   Exposes:     window.activityPage
   ========================================================================== */

(function (global) {
  'use strict';

  const REFRESH_MS = 15000;

  // Higher than the card's caps on purpose — the page exists to show what the
  // card cut off. The server clamps each endpoint to its own maximum.
  const SCAN_LIMIT = 100;
  const REVIEW_LIMIT = 200;

  let scansPayload = null;
  let reviewPayload = null;
  let timer = null;
  let inFlight = false;

  function byId(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return global.escapeHtml ? global.escapeHtml(value) : String(value == null ? '' : value);
  }

  function setCount(id, n) {
    const el = byId(id);
    if (el) el.textContent = String(n);
  }

  /**
   * GET JSON, or throw an Error carrying the status text.
   *
   * ⚠️ `response.ok` is checked BEFORE parsing. The old live scripts called
   * `.json()` unconditionally, so a 401/500 HTML body surfaced as
   * "Unexpected token '<'" — a parse error pointing at the wrong layer.
   */
  async function getJson(url) {
    const response = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}`);
    }
    return response.json();
  }

  function renderError(body, prefix, error) {
    body.innerHTML =
      `<div class="p-3 text-center text-danger small">${esc(prefix)}: ${esc(error && error.message)}</div>`;
  }

  // ── Recent Scans ────────────────────────────────────────────────────────

  async function loadScans() {
    const body = byId('activity-scans-body');
    if (!body) return;

    try {
      const data = await getJson(`/api/recent-scans?limit=${SCAN_LIMIT}`);
      const scans = data.scans || [];
      const payload = JSON.stringify(scans);
      if (payload === scansPayload) return;   // skip an unchanged re-render
      scansPayload = payload;

      setCount('activityScansCount', scans.length);
      global.ActivityFeed.renderScansInto(body, scans, 'No scan history yet.');
    } catch (error) {
      // Reported rather than left as a spinner: a permanent spinner reads as
      // "still loading" forever, and the user has no way to tell the difference.
      renderError(body, 'Could not load scan history', error);
    }
  }

  // ── Needs Review ────────────────────────────────────────────────────────

  async function loadApprovals() {
    const body = byId('activity-review-body');
    if (!body) return;

    try {
      const data = await getJson(`/api/metadata/pending-recommendations?limit=${REVIEW_LIMIT}`);
      const albums = data.albums || [];
      const payload = JSON.stringify(albums);
      if (payload === reviewPayload) return;
      reviewPayload = payload;

      setCount('activityReviewCount', albums.length);
      global.ActivityFeed.renderApprovalsInto(
        body, albums, 'Nothing needs review — the last scan left no changes to confirm.'
      );
    } catch (error) {
      renderError(body, 'Could not load recommendations', error);
    }
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function refreshAll() {
    return Promise.all([loadScans(), loadApprovals()]);
  }

  /** One tick: never overlap requests, never run on a hidden tab. */
  function tick() {
    if (inFlight) return;
    if (document.visibilityState === 'hidden') return;
    inFlight = true;
    refreshAll().finally(function () {
      inFlight = false;
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    refreshAll();

    timer = global.setInterval(tick, REFRESH_MS);

    // A background tab must not poll forever — the dashboard's three uncleared
    // setIntervals did exactly that. Refresh once on return so the page is
    // never stale after the tab was hidden.
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') tick();
    });

    global.addEventListener('beforeunload', function () {
      if (timer) global.clearInterval(timer);
    });
  });

  global.activityPage = {
    refreshAll,
    loadScans,
    loadApprovals,
  };
})(window);
