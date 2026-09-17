/* ==========================================================================
   static/js/pages/search.js
   Unified search page initialisation — delegated row actions, Soulseek
   auto-search from the URL.

   Requires: services/slskd.js, ui/toast.js
   Load AFTER playlist_import.js (which populates window._trackPayloads).

   ── CHANGES FROM THE PREVIOUS VERSION ─────────────────────────────────────
   1. MISSING HANDLERS — these three were called but defined NOWHERE in the
      codebase, so every click threw ReferenceError:
          addSelectedTrack(title, artist, album)
          searchTrackInSoulseek(query, button)
          openReplacementTrackModal(artist, title, album)
      `searchTrackInSoulseek` is implemented below against services/slskd.js.
      The other two are guarded: if a page defines them they are used, and if
      not the user gets a clear message instead of a silent console error.
      Wire them up (or delete the buttons) when the replacement-track flow is
      built.

   2. `_trackPayloads` is now read through a guarded accessor. It is a
      top-level `const` in playlist_import.js; if that file failed to parse —
      which it always did, via the `currentImportData` redeclaration clash —
      this file threw on the first click instead of degrading.

   3. The Soulseek auto-search fired a synthetic `submit` event on
      #slskdSearchForm after a fixed 100ms timeout. That raced the listener
      registration in downloads.js. It now calls the search directly.
   ========================================================================== */

(function (global) {
  'use strict';

  function payloadFor(element) {
    const store = global._trackPayloads;
    if (!store) {
      console.warn('[search_init] _trackPayloads is unavailable — did playlist_import.js fail to parse?');
      return null;
    }
    return store[element.dataset.pid] || null;
  }

  /**
   * Search Soulseek for one track and show the results in the manual modal.
   * @param {string} query
   * @param {HTMLElement} button
   */
  function searchTrackInSoulseek(query, button) {
    if (!query) return;

    // downloads.js owns the manual search modal; prefer it so the user gets
    // the full per-file result list and the queue-linked download path.
    if (typeof global.openSoulseekManualSearchModal === 'function') {
      global.openSoulseekManualSearchModal(query, null);
      return;
    }

    // Standalone fallback: search and auto-queue the best match.
    return global.buttonState.withBusy(button, '', async () => {
      const session = await global.slskd.search(query, { context: 'search-page' });
      if (!session) return;
      const best = global.slskd.bestMatch(session.results);
      if (!best) {
        global.toast.warning('No Soulseek results with free upload slots');
        return;
      }
      await global.slskd.download(
        [{ username: best.username, filename: best.filename, size: best.size || 0 }],
        { label: best.filename }
      );
    });
  }

  document.addEventListener('click', function (event) {
    const addBtn = event.target.closest('.js-add-track-btn');
    if (addBtn) {
      const payload = payloadFor(addBtn);
      if (!payload) return;
      if (typeof global.addSelectedTrack === 'function') {
        global.addSelectedTrack(payload.title, payload.artist, payload.album);
      } else {
        global.toast.warning('Adding tracks is not available on this page');
      }
      return;
    }

    const slskdBtn = event.target.closest('.js-slskd-search-btn');
    if (slskdBtn) {
      const payload = payloadFor(slskdBtn);
      if (payload) {
        searchTrackInSoulseek(
          payload.query || `${payload.artist} ${payload.title}`,
          slskdBtn
        );
      }
      return;
    }

    const replaceBtn = event.target.closest('.js-replace-btn');
    if (replaceBtn) {
      const payload = payloadFor(replaceBtn);
      if (!payload) return;
      if (typeof global.openReplacementTrackModal === 'function') {
        global.openReplacementTrackModal(payload.artist, payload.title, payload.album);
      } else {
        global.toast.warning('Track replacement is not available yet');
      }
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    const params = new URLSearchParams(global.location.search);
    const queryParam = params.get('q');
    if (!queryParam) return;

    const input = document.getElementById('slskdSearchQuery');
    if (!input) return;

    const normalized = global.slskd
      ? global.slskd.normalizeQuery(queryParam)
      : queryParam;
    input.value = normalized;

    // Call the search directly rather than dispatching a synthetic submit —
    // the old 100ms timeout raced downloads.js's listener registration.
    if (typeof global.searchSoulseek === 'function') {
      global.searchSoulseek({ preventDefault: function () {} });
    }
  });

  global.searchTrackInSoulseek = searchTrackInSoulseek;
})(window);
