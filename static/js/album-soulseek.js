/* ==========================================================================
   static/js/album-soulseek.js  (LIVE tree mirror)

   The album page's "Search Soulseek" action, for the live template tree.

   `templates/pages/album_detail.html` renders
   `onclick="openSlskdSearchAlbum(artist, album)"`, but the function was
   defined only in `old_system/templates/album.html` and never carried across
   — the call site existed with no definition anywhere, so the Actions-menu
   item threw ReferenceError and did nothing.

   Kept byte-for-byte equivalent (apart from this header) to the rebuilt tree's
   `test_site/static/js/services/album-soulseek.js` so the two UIs behave
   identically. The path uses `?q=`, which is what every other
   "Search Soulseek" link in the app already uses and what
   `pages/search.js` prefills from.
   ========================================================================== */
(function (global) {
  'use strict';

  /**
   * Strip characters that break slskd's query parser.
   * An ampersand makes slskd return zero results rather than an error.
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
