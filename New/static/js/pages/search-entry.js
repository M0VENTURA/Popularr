/* ==========================================================================
   static/js/pages/search-entry.js
   Thin entry point for the /search URL.

   ── WHY THIS IS NOT A SEARCH IMPLEMENTATION ───────────────────────────────
   This page used to be a full library search UI (its own /api/search call,
   its own result cards, its own release-bucket rendering). All of that now
   lives in ui/search-flyout.js, which is loaded globally by base.html and backs
   the navbar search: same endpoint, same bucket shape (albums / compilations /
   live_albums / eps / singles), plus release-type filters, MusicBrainz
   merging and queue buttons that the old page never had.

   Nothing live links to /search any more — the only references left are in
   templates/pages/downloads/search_playlists.html, which is itself dead (its
   route redirects to /downloads/search).

   So this file does NOT search. It hands off to the one implementation:

       #page-data carries {initialQuery} from ?q=, and we open the flyout with
       it pre-filled. Without ?q= we just open the flyout.

   A previous pass at this page produced a full second implementation
   (pages/library-search.js + css/search.css). Those are SUPERSEDED and should
   be deleted — keeping them would mean three search implementations, and they
   would drift the same way the two smart-playlist builders did.
   ========================================================================== */

(function (global) {
  'use strict';

  function initialQuery() {
    const el = document.getElementById('page-data');
    if (!el) return '';
    try {
      return (JSON.parse(el.textContent) || {}).initialQuery || '';
    } catch (error) {
      console.error('[search-entry] #page-data is not valid JSON:', error.message);
      return '';
    }
  }

  /** Open the shared flyout, pre-filled when ?q= was supplied. */
  function openSearch() {
    if (typeof global.openUnifiedSearch !== 'function') {
      global.toast && global.toast.error('Search is unavailable — the search flyout did not load.');
      return;
    }
    global.openUnifiedSearch('all', initialQuery());
  }

  document.addEventListener('DOMContentLoaded', function () {
    // A button, so the page is usable without the auto-open.
    const btn = document.getElementById('openSearchBtn');
    if (btn) btn.addEventListener('click', openSearch);

    // ?q= means the user came from a link — go straight to the results.
    if (initialQuery()) openSearch();
  });

  global.searchEntry = { openSearch };
})(window);
