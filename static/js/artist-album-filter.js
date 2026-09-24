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

   -- MARKUP CONTRACT (UPDATED) -------------------------------------------------
   The release rows are now rendered by the SHARED component
   templates/components/_release_section.html, and the status filter moved INTO
   each section (see static/js/artist-releases.js). The old markup was

       .category-section                                  (the category card)
         .album-row[data-status="library" | "missing" | "upcoming"]

   and the new markup is

       .release-section                                   (the category card)
         .release-item[data-status="library" | "missing" | "upcoming"]

   Both shapes are matched below. The previous version only knew the OLD one, so
   after the release-section migration every selector matched nothing and this
   module became inert while looking entirely correct — the exact failure mode
   its own header warns about.

   `setArtistFilter()` is kept for compatibility with any remaining inline
   handler and now also drives the new per-section radios, so a page-wide
   selection still reaches the rows.
   ========================================================================== */

(function () {
  'use strict';

  var STORAGE_KEY = 'artistAlbumFilterState';
  var VALID = ['all', 'library', 'missing'];

  // Current markup (release-section component) plus the legacy shape, so the
  // module is correct during and after the migration.
  var ROW_SELECTOR = '.release-section .release-item[data-status], .category-section .album-row[data-status]';
  var SECTION_SELECTOR = '.release-section, .category-section';

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
   *
   * ⚠️ `upcoming` is the exception, because it is a KNOWN status and the album
   * is demonstrably NOT owned. Leaning it to Library (as the default rule above
   * would) claimed the collection contained an album that has not been released
   * yet. The lean-to-Library rule is for UNKNOWN statuses only.
   */
  function rowIsLibrary(row) {
    var status = String(row.getAttribute('data-status') || '').toLowerCase();
    return status !== 'missing' && status !== 'upcoming';
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
    document.querySelectorAll(ROW_SELECTOR).forEach(function (row) {
      var visible;
      if (filter === 'all') {
        visible = true;
      } else if (filter === 'missing') {
        visible = String(row.getAttribute('data-status') || '').toLowerCase() === 'missing';
      } else if (filter === 'upcoming') {
        visible = String(row.getAttribute('data-status') || '').toLowerCase() === 'upcoming';
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
    document.querySelectorAll(SECTION_SELECTOR).forEach(function (section) {
      var rows = section.querySelectorAll('.release-item[data-status], .album-row[data-status]');
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

    // 4. Re-assert the selection on the per-section radios that replaced the
    //    page-wide bar, so the two controls cannot disagree. Guarded because
    //    artist-releases.js owns those radios and may not be loaded.
    document.querySelectorAll('.release-section').forEach(function (section) {
      var radio = section.querySelector('.release-filter-radio[value="' + filter + '"]');
      if (radio && !radio.checked) {
        radio.checked = true;
        radio.dispatchEvent(new Event('change', { bubbles: true }));
      }
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
