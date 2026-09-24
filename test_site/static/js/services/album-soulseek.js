/* ==========================================================================
   static/js/services/album-soulseek.js
   The album page's "Search Soulseek" action.

   ── WHY THIS EXISTS ──────────────────────────────────────────────────────
   `templates/pages/album_detail.html` (and its rebuilt twin) render

       onclick="openSlskdSearchAlbum('{{ artist }}', '{{ album }}')"

   inside the Actions dropdown, guarded by `slskd_config.enabled`. The function
   was defined ONLY in `old_system/templates/album.html` and was never carried
   across, so `git grep` found the call sites but no definition anywhere in
   either tree: the menu item threw `ReferenceError` and did nothing.

   The original navigated to `/downloads?search=…`. That target no longer
   reads a `search` parameter — the Soulseek UI moved to
   `/downloads/search`, which is the page every other "Search Soulseek" link in
   the app already navigates to, with `?q=`:

       pages/track.js:374   /downloads/search?q=…

   so this uses `?q=` too, and `pages/search.js` prefills the box from it and
   fires the search. Keeping the page's own query normalisation rather than
   inventing a second one means the ampersand stripping (slskd returns zero
   results for `&`) behaves identically no matter which link was clicked.

   Loaded by base.html so that EVERY page rendering the album Actions dropdown
   gets it — album_detail exists in both the live and rebuilt trees, and the
   rebuilt page loads only a couple of page-specific scripts.
   ========================================================================== */
(function (global) {
  'use strict';

  /**
   * Strip characters that break slskd's query parser.
   *
   * Deliberately mirrors `services/slskd.js::normalizeQuery`: an ampersand
   * makes slskd return zero results instead of an error, so it is replaced
   * with a space rather than left in. The legacy implementation also unescaped
   * `\u0026`/`&amp;` first, because the artist/album arrive from
   * `|escapejs` inside an inline handler.
   */
  function normalizeQuery(value) {
    return String(value == null ? '' : value)
      .replace(/\\u0026/gi, ' ')
      .replace(/&amp;/gi, ' ')
      .replace(/&/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  /**
   * Open the Soulseek search page with this album prefilled.
   * @param {string} artist
   * @param {string} [album]
   */
  function openSlskdSearchAlbum(artist, album) {
    const query = normalizeQuery([artist, album].filter(Boolean).join(' '));
    if (!query) return;

    global.location.href = '/downloads/search?q=' + encodeURIComponent(query);
  }

  global.openSlskdSearchAlbum = openSlskdSearchAlbum;
  global.albumSoulseek = { openSlskdSearchAlbum, normalizeQuery };
})(window);
