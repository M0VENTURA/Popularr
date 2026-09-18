/* ==========================================================================
   static/js/artist-album-filter.js
   Album status filter for the artist page: All / In Library / Missing.

   -- WHY THIS FILE EXISTS ------------------------------------------------------
   The filter bar in templates/pages/artist_detail_v2.html calls

       setArtistFilter('all' | 'library' | 'missing')

   from inline onclick attributes. That function was defined ONLY in
   static/js/artist_detail.js.

   That file is NOT JavaScript. It is a 5346-line Jinja PAGE TEMPLATE whose
   first line is `{% extends "base.html" %}`, yet the page loads it with
   `<script src>`. The browser parses the Jinja as JS, throws

       SyntaxError: Unexpected token '%'

   on line 1, and discards the ENTIRE file. So setArtistFilter never existed and
   every click threw

       ReferenceError: setArtistFilter is not defined

   which is the reported "the filter between In Library and Missing on the
   artist page still doesn't work; I can't hide the missing releases that are
   populated".

   A SyntaxError in one `<script src>` does not stop LATER scripts from running,
   so this module is loaded independently and the filter works now, while the
   larger file is repaired separately.

   -- MARKUP CONTRACT -----------------------------------------------------------
   Release rows are server-rendered by the release-category macro as

       .category-section                                  (the category card)
         .album-row[data-status="library" | "missing"]

   and the filter bar is

       .artist-filter-btn[data-filter="all" | "library" | "missing"]

   Rows injected later by checkMissingReleases() also carry
   data-status="missing", so they filter with no special-casing.
   ========================================================================== */

(function () {
  'use strict';

  var STORAGE_KEY = 'artistAlbumFilterState';
  var VALID = ['all', 'library', 'missing'];

  // Kept module-level so applyArtistFilter() can RE-ASSERT the current
  // selection after rows are injected or re-sorted: the filter is a state,
  // not a one-off class change.
  var currentFilter = 'all';

  /**
   * Is this row owned for the purposes of the "In Library" filter?
   *
   * NOTE: only an explicit "missing" counts as missing. The two filters are
   * deliberately NOT symmetric - wrongly hiding an owned album is worse than
   * wrongly showing a missing one, and "Missing" is the explicit opt-in. So an
   * unexpected or absent status leans to Library rather than silently
   * disappearing from both views.
   */
  function rowIsLibrary(row) {
    var status = String(row.getAttribute('data-status') || '').toLowerCase();
    return status !== 'missing';
  }

  /**
   * Re-assert the CURRENT filter without changing the selection.
   *
   * Split out from setArtistFilter() so it can also be called after rows are
   * added or removed.
   */
  function applyArtistFilter() {
    var filter = currentFilter;

    // 1. Show/hide each release row according to its status.
    document.querySelectorAll('.category-section .album-row[data-status]').forEach(function (row) {
      var visible;
      if (filter === 'all') {
        visible = true;
      } else if (filter === 'missing') {
        visible = String(row.getAttribute('data-status') || '').toLowerCase() === 'missing';
      } else {
        visible = rowIsLibrary(row);
      }
      // Set '' rather than 'block'/'flex': the rows are flex containers, and
      // clearing the inline style lets the layout keep its own display value.
      row.style.display = visible ? '' : 'none';
    });

    // 2. Hide a category card once it has no visible rows left. Cards with no
    //    status rows at all (e.g. "Covers of ...", populated by JS) are left
    //    alone - otherwise an emptied section keeps its header and a stale
    //    "N / M in Library" badge above an empty body.
    document.querySelectorAll('.category-section').forEach(function (section) {
      var rows = section.querySelectorAll('.album-row[data-status]');
      if (!rows.length) {
        section.style.display = '';
        return;
      }
      var anyVisible = Array.prototype.some.call(rows, function (row) {
        return row.style.display !== 'none';
      });
      section.style.display = anyVisible ? '' : 'none';
    });

    // 3. Reflect the selection on the buttons so the active filter is obvious.
    document.querySelectorAll('.artist-filter-btn').forEach(function (btn) {
      var value = String(btn.getAttribute('data-filter') || 'all').toLowerCase();
      btn.classList.toggle('active', value === filter);
    });
  }

  /**
   * Apply an album status filter. Called from the filter bar's inline onclick.
   *
   * Clicking the ALREADY-ACTIVE filter clears it back to "All", so the buttons
   * behave as on/off switches rather than a sticky 3-way radio.
   */
  function setArtistFilter(filter) {
    var wanted = String(filter || 'all').toLowerCase();
    var next = VALID.indexOf(wanted) !== -1 ? wanted : 'all';

    currentFilter = (next === currentFilter && next !== 'all') ? 'all' : next;

    try {
      localStorage.setItem(STORAGE_KEY, currentFilter);
    } catch (e) {
      // Private mode / storage disabled: the filter still works for this page
      // view, it just will not be remembered. Never let this break filtering.
    }

    applyArtistFilter();
  }

  /** Restore the remembered filter and apply it on page load. */
  function initializeArtistAlbumFilter() {
    var saved = 'all';
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (VALID.indexOf(raw) !== -1) saved = raw;
    } catch (e) {
      saved = 'all';
    }
    currentFilter = saved;
    applyArtistFilter();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeArtistAlbumFilter);
  } else {
    initializeArtistAlbumFilter();
  }

  // Inline onclick attributes resolve by GLOBAL name, so publish explicitly
  // rather than relying on the script's top level.
  window.setArtistFilter = setArtistFilter;
  window.applyArtistFilter = applyArtistFilter;
  window.initializeArtistAlbumFilter = initializeArtistAlbumFilter;
})();
