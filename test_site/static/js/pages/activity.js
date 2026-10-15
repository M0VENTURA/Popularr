/* ==========================================================================
   js/pages/activity.js
   The full Activity view.

   WHAT THIS PAGE OWNS, AND WHAT IT DOES NOT
   -----------------------------------------
   The dashboard card summarises three feeds inside a single tab strip, capped
   to fit (12 albums, 25 scans, 50 reviews). This page shows the same three
   feeds STACKED with higher caps, because "View all" means the user wants
   everything at once rather than three tab clicks.

   ⭐ THE RENDERING IS NOT HERE. `services/activity-feed.js` owns the grouping,
   the labels, the timestamps and the row markup, and the dashboard card uses
   the same module — so a scan cannot render one way on this page and another
   way on the card. What lives here is only what is specific to THIS page:
   which containers the feeds go into, how many items to ask for, and the
   per-section counts.

   ⚠️ ONE POLL, NOT THREE. The three feeds are fetched in a single
   `Promise.all`, and each one is skipped when its payload is unchanged. The
   dashboard learned this the hard way: re-rendering a static list every 5
   seconds throws away the scroll position and any hover state, and on a long
   list it is visible.

   Load order:  utils/dom.js → utils/api.js → utils/poller.js → ui/toast.js →
                ui/status-badge.js → services/activity-feed.js → activity.js
   Exposes:     global.activityPage
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
  let poller = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function setCount(id, n) {
    const el = byId(id);
    if (el) el.textContent = String(n);
  }

  // ── Recent Scans ────────────────────────────────────────────────────────

  async function loadScans() {
    const body = byId('activity-scans-body');
    if (!body) return;

    try {
      // ⚠️ `/api/recent-scans` — the order is reversed from the obvious guess.
      // See the note in pages/dashboard.js: this path was wrong in the rebuilt
      // tree from the start and 404'd silently behind a bare catch.
      const data = await global.api.getJson(`/api/recent-scans?limit=${SCAN_LIMIT}`);
      const scans = data.scans || [];
      const payload = JSON.stringify(scans);
      if (payload === scansPayload) return;   // see the header note on re-rendering
      scansPayload = payload;

      setCount('activityScansCount', scans.length);
      global.ActivityFeed.renderScansInto(body, scans, 'No scan history yet.');
    } catch (error) {
      // Reported rather than left as a spinner: a permanent spinner reads as
      // "still loading" forever, and the user has no way to tell the difference.
      body.innerHTML =
        `<div class="p-3 text-center text-danger small">Could not load scan history: ${
          global.escapeHtml ? global.escapeHtml(error.message) : ''
        }</div>`;
    }
  }

  // ── Needs Review ────────────────────────────────────────────────────────

  async function loadApprovals() {
    const body = byId('activity-review-body');
    if (!body) return;

    try {
      const data = await global.api.getJson(
        `/api/metadata/pending-recommendations?limit=${REVIEW_LIMIT}`
      );
      const albums = data.albums || [];
      const payload = JSON.stringify(albums);
      if (payload === reviewPayload) return;
      reviewPayload = payload;

      setCount('activityReviewCount', albums.length);
      global.ActivityFeed.renderApprovalsInto(
        body, albums, 'Nothing needs review — the last scan left no changes to confirm.'
      );
    } catch (error) {
      body.innerHTML =
        `<div class="p-3 text-center text-danger small">Could not load recommendations: ${
          global.escapeHtml ? global.escapeHtml(error.message) : ''
        }</div>`;
    }
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function refreshAll() {
    return Promise.all([loadScans(), loadApprovals()]);
  }

  document.addEventListener('DOMContentLoaded', function () {
    refreshAll();

    // ⚠️ A managed poller, not a bare setInterval: this module has no other
    // reason to hold a timer, and the poller stops on a hidden tab. The
    // dashboard's three uncleared setIntervals kept running in background tabs
    // for the life of the page — that is what ui/poller.js exists to prevent.
    if (!global.poller) return;
    poller = global.poller.create({
      interval: REFRESH_MS,
      immediate: false,
      onTick: refreshAll,
    });
    poller.start();
  });

  global.activityPage = {
    refreshAll,
    loadScans,
    loadApprovals,
  };
})(window);
