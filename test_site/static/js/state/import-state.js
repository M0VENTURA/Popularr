/* ==========================================================================
   static/js/state/import-state.js
   Shared state for the playlist / CSV import flow.

   Load BEFORE both playlist.js and playlist_import.js:

       <script src="{{ url_for('static', filename='js/state/import-state.js') }}"></script>
       <script src="{{ url_for('static', filename='js/playlist.js') }}"></script>
       <script src="{{ url_for('static', filename='js/playlist_import.js') }}"></script>

   ── THE BUG THIS FIXES ────────────────────────────────────────────────────
   Both files declare the SAME TWO top-level bindings:

       playlist.js         line ~5133   let currentImportData = null;
                                        let missingTracksForSearch = [];
       playlist_import.js  line ~7280   let currentImportData = null;
                                        let missingTracksForSearch = [];

   Classic <script> tags share ONE top-level scope, and `let` cannot be
   redeclared in it. On any page that loads both files, the second one
   throws

       SyntaxError: Identifier 'currentImportData' has already been declared

   at PARSE time — which means the entire second file never executes. Not
   one function in it is defined. This is the same failure mode that
   downloads_page.js already documents for `currentSlskdSearchId`, where
   the fix was to deliberately NOT redeclare.

   The two files are not independent — they are two halves of one flow:

       playlist_import.js  WRITES the state
                           importPlaylistFromCSV() sets currentImportData
                           and missingTracksForSearch from the API response

       playlist.js         READS the state
                           createPlaylist() reads currentImportData.
                           playlist_name / .playlist_description /
                           .matched_tracks / .target_user

   So they genuinely need shared state. Declaring it in a third file that
   both depend on is the fix: neither declares it, so neither can collide,
   and the dependency becomes explicit instead of relying on load order.

   ── HOW TO APPLY ──────────────────────────────────────────────────────────
   1. DELETE both `let` declarations from playlist.js.
   2. DELETE both `let` declarations from playlist_import.js.
   3. Add this file's <script> tag before both.
   4. No other edits: the accessor properties below keep the bare names
      `currentImportData` and `missingTracksForSearch` working exactly as
      before at every existing call site.
   ========================================================================== */

(function (global) {
  'use strict';

  /** The most recent import response. Null until an import runs. */
  let currentImportData = null;

  /** Tracks from that import which were not found in the library. */
  let missingTracksForSearch = [];

  /**
   * Bare global accessors.
   *
   * Defined as properties rather than plain assignments so that reads and
   * writes both route through this module. Existing code such as
   *     currentImportData = data;
   *     if (!currentImportData) return;
   * keeps working untouched — but there is now exactly one storage location
   * behind the name, no matter which file does the writing.
   */
  Object.defineProperty(global, 'currentImportData', {
    get() { return currentImportData; },
    set(value) { currentImportData = value; },
    configurable: true,
  });

  Object.defineProperty(global, 'missingTracksForSearch', {
    get() { return missingTracksForSearch; },
    set(value) { missingTracksForSearch = value || []; },
    configurable: true,
  });

  /**
   * Store an import response.
   * @param {Object} data  the /api/playlist/import/* response
   */
  function setImport(data) {
    currentImportData = data || null;
    missingTracksForSearch = (data && data.missing_tracks) || [];
    return currentImportData;
  }

  /** Clear the import between runs, so a failed import cannot be re-used. */
  function clear() {
    currentImportData = null;
    missingTracksForSearch = [];
  }

  /** True when there is an import result available to act on. */
  function hasImport() {
    return currentImportData !== null;
  }

  /** Matched tracks from the current import, or an empty array. */
  function matchedTracks() {
    return (currentImportData && currentImportData.matched_tracks) || [];
  }

  /** Missing tracks from the current import. */
  function missingTracks() {
    return missingTracksForSearch;
  }

  global.importState = {
    set: setImport,
    clear,
    hasImport,
    matchedTracks,
    missingTracks,
    get data() { return currentImportData; },
  };
})(window);
