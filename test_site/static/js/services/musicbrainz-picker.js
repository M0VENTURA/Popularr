/* ==========================================================================
   static/js/services/musicbrainz-picker.js
   Driver for the SHARED MusicBrainz search modal.

   Load order:
       utils/dom.js  →  utils/api.js  →  ui/toast.js  →  ui/modal.js
       services/mb-search.js

   ── WHAT THIS IS, AND WHAT IT IS NOT ──────────────────────────────────────
   This file drives the search component that base.html includes on EVERY
   page (components/_musicbrainz_search_component.html): the 4-field form
   (artist / album / track / year), the result list, and the "Select Match"
   → "Apply Match" confirmation flow.

   services/musicbrainz.js is a DIFFERENT thing: the upcoming-release lookup
   that renders per-track accordions and queues tracks. Both talk to
   MusicBrainz; neither replaces the other. Keep the names straight:

       mb-search.js       openGlobalMbSearch / performMbSearch
                          → generic release picker, callback-driven
       musicbrainz.js     musicbrainz.search / render / queueRelease
                          → track-level queueing for a known artist+album

   Extracted from downloads.js because it is page-agnostic — leaving it
   there meant every page that wanted the shared modal had to load the
   entire 2,900-line downloads page script to get it.

   ── CRITICAL: THE COMPONENT MUST BE INCLUDED EXACTLY ONCE ─────────────────
   Every element id below (mbSearchArtist, mbSearchResults, …) is resolved
   with getElementById, which returns the FIRST match in DOM order. A page
   that includes the component a second time will have its results written
   into the hidden global copy while the visible one sits on its
   placeholder. That is the bug album_detail.js's old #albumLookupModal hit,
   and the same one searchMBForOrganize hit before it was renamed to
   org-prefixed ids.
   ========================================================================== */

(function (global) {
  'use strict';

  const SEARCH_ENDPOINT = '/api/musicbrainz/search';
  const MODAL_ID = 'musicBrainzModal';
  const DEFAULT_LIMIT = 25;

  /** Fields the shared form exposes, in payload order. */
  const FORM_FIELDS = ['artist', 'album', 'track', 'year'];

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function fieldValue(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  /**
   * Derive a release's display category from its MusicBrainz types.
   * Secondary types win: a "Live" album is more usefully labelled Live than
   * Album, and the same for compilations, remixes and soundtracks.
   */
  function derivedCategory(release) {
    const secondary = (release.secondary_types || []).map((s) => String(s).toLowerCase());
    const secondaryFirst = [
      'compilation', 'live', 'remix', 'soundtrack', 'dj-mix',
      'mixtape', 'demo', 'spokenword', 'interview', 'audiobook',
    ];
    for (const type of secondaryFirst) {
      if (secondary.indexOf(type) !== -1) return type;
    }
    return String(release.category || release.primary_type || '').toLowerCase() || 'other';
  }

  function releaseArtist(release) {
    return release.artist
      || (release['artist-credit'] && release['artist-credit'][0] && release['artist-credit'][0].name)
      || 'Unknown Artist';
  }

  /**
   * Report an outcome to the user, falling back through the toast layers.
   *
   * This module ships with pages that differ in what they load (the upcoming
   * page has no download-queue.js), so it cannot assume a page-local notify
   * helper exists. A queue action that reports NOTHING is indistinguishable
   * from a broken button, which is the defect this is here to avoid.
   */
  function toastError(message) {
    if (global.toast && typeof global.toast.error === 'function') {
      return global.toast.error(message);
    }
    console.error('[musicbrainz]', message);
  }

  function toastSuccess(message) {
    if (global.toast && typeof global.toast.success === 'function') {
      return global.toast.success(message);
    }
    console.info('[musicbrainz]', message);
  }

  // ── Pending selection ───────────────────────────────────────────────────
  //
  // `handleGlobalMbSelect` stages a pick and reveals the confirmation panel;
  // `confirmReleaseSelection` fires the caller's callback once the user has
  // reviewed it.
  //
  // The original invoked the callback IMMEDIATELY on select, which skipped
  // the panel entirely — #mbSelectedRelease / #mbSelectedTitle /
  // #mbSelectedArtist were never populated, so the panel stayed hidden and
  // confirmReleaseSelection() had nothing to do. Which is also why that
  // function was never written, even though its button existed in the markup.

  let pendingRelease = null;
  let selectionCallback = null;

  /**
   * Whether the CURRENT modal session is a "match this release to my album"
   * flow rather than a "download it" flow.
   *
   * Kept SEPARATE from ``selectionCallback`` on purpose. The callback is
   * consumed (set to null) when the user applies a match, but the intent of
   * the session does not change — so gating the result button on the callback
   * made the button silently revert to "Soulseek" after a single match.
   */
  let matchIntent = false;

  /**
   * Open the shared modal, optionally pre-filling the form.
   *
   * @param {string} artist
   * @param {string} album
   * @param {Function} [callback] receives the chosen release object
   * @param {string} [track]
   * @param {string} [year]
   * @param {Object} [opts]
   * @param {Array} [opts.queueAlbums] albums already in the download queue,
   *        rendered ABOVE the search so a missed release can be picked without
   *        searching for it again
   */
  function openGlobalMbSearch(artist, album, callback, track, year, opts) {
    const modalEl = document.getElementById(MODAL_ID);
    if (!modalEl) {
      console.error('MusicBrainz modal not found in DOM.');
      return;
    }
    const options = opts || {};

    // Every field is CLEARED when no value is supplied. The old guard was
    // `if (el && value)`, so a stale value from a previous open survived:
    // opening for an artist-only search after an album search kept the old
    // album in the form and silently narrowed the query.
    const values = { artist, album, track, year };
    FORM_FIELDS.forEach((name) => {
      const el = document.getElementById('mbSearch' + name.charAt(0).toUpperCase() + name.slice(1));
      if (el) el.value = values[name] || '';
    });

    selectionCallback = typeof callback === 'function' ? callback : null;
    // A callback means the caller wants the chosen release handed back (match);
    // no callback means the modal is a download browser. This is set ONCE per
    // open, so a later search inside the same session keeps the right action.
    matchIntent = selectionCallback !== null;
    pendingRelease = null;

    const panel = document.getElementById('mbSelectedRelease');
    if (panel) panel.classList.add('d-none');

    renderQueueAlbums(options.queueAlbums);

    if (global.modal) global.modal.show(modalEl);
    else if (global.bootstrap) global.bootstrap.Modal.getOrCreateInstance(modalEl).show();

    // Search once the modal is actually shown. A fixed setTimeout raced the
    // Bootstrap transition: on a slow render the search fired before the
    // fields were visible. `once` avoids re-running on every later open.
    modalEl.addEventListener('shown.bs.modal', function runOnce() {
      performSearch();
    }, { once: true });
  }

  /**
   * Render the "albums already in your download queue" block above the search.
   *
   * ⚠️ This is a MATCH-ONLY affordance: callers that pass no ``queueAlbums``
   * (the album page's lookup, bulk downloads) get no block, and a stale list
   * from a previous folder is removed. Selecting a queued album stages it
   * through the SAME confirmation panel as a search result, so "Apply Match"
   * still has to be pressed.
   */
  function renderQueueAlbums(albums) {
    const modalEl = document.getElementById(MODAL_ID);
    const body = modalEl ? modalEl.querySelector('.modal-body') : null;
    if (!body) return;

    let section = document.getElementById('mbQueueAlbumsSection');
    if (!Array.isArray(albums) || !albums.length) {
      if (section) section.remove();
      return;
    }

    if (!section) {
      section = document.createElement('div');
      section.id = 'mbQueueAlbumsSection';
      section.className = 'mb-3';
      body.insertBefore(section, body.firstChild);
    }

    section.innerHTML = `
      <div class="d-flex justify-content-between align-items-center mb-2">
        <h6 class="mb-0 fw-bold">
          <i class="bi bi-download me-1"></i>In your download queue
        </h6>
        <small class="text-muted">${albums.length} album(s)</small>
      </div>
      <div class="list-group mb-2" style="max-height:15rem;overflow-y:auto;">
        ${albums.map((album, index) => `
          <button type="button" class="list-group-item list-group-item-action bg-dark text-light border-secondary mb-queue-album" data-index="${index}">
            <div class="d-flex justify-content-between align-items-center gap-2">
              <div class="min-w-0">
                <div class="fw-semibold text-truncate">${esc(album.album || '')}</div>
                <small class="text-muted d-block text-truncate">
                  ${esc(album.artist || '')}${album.track_count ? ` · ${Number(album.track_count) || 0} track(s)` : ''}
                </small>
              </div>
              <span class="badge bg-info text-dark flex-shrink-0">In queue</span>
            </div>
          </button>`).join('')}
      </div>
      <div class="text-muted extra-small mb-3">
        Pick a queued album above — or search MusicBrainz below for a different release.
      </div>`;

    section.querySelectorAll('.mb-queue-album').forEach((button) => {
      button.addEventListener('click', function () {
        const album = albums[parseInt(this.dataset.index, 10)];
        if (!album || !album.mb_id) return;
        stageSelection({
          id: album.mb_id,
          title: album.album || '',
          artist: album.artist || '',
          category: 'queue',
          cover_art_url: album.cover_art_url || '',
          _from_queue: true,
        });
      });
    });
  }

  /**
   * Run a search from the current form state.
   * @returns {Promise<void>}
   */
  async function performSearch() {
    let artist = fieldValue('mbSearchArtist');
    const album = fieldValue('mbSearchAlbum');
    const track = fieldValue('mbSearchTrack');
    const year = fieldValue('mbSearchYear');

    let query = (artist || album || track || year)
      ? [artist, album, track, year].filter(Boolean).join(' ')
      : fieldValue('mbSearchInput');

    if (!query) return;

    // Artist-only searches need a different server-side strategy: a bare
    // text search returns releases from similarly named artists.
    let artistOnly = false;
    if (global._mbArtistOnlySearch === true) {
      artistOnly = true;
      global._mbArtistOnlySearch = false;
    } else if (artist && !album && !track && !year) {
      artistOnly = true;
    }

    // "artist:Foo" prefix as an explicit opt-in.
    if (query.toLowerCase().startsWith('artist:')) {
      artistOnly = true;
      artist = query.substring(7).trim();
      query = [artist, album, track, year].filter(Boolean).join(' ');
    }

    const resultsEl = document.getElementById('mbSearchResults')
      || document.getElementById('mbResults');
    if (resultsEl) {
      resultsEl.innerHTML =
        '<div class="text-center mt-4"><div class="spinner-border text-info"></div>' +
        '<p class="mt-2 text-muted">Searching MusicBrainz…</p></div>';
    }

    try {
      const payload = { artist, album, track, year };
      if (!artist && !album && !track && !year) payload.query = query;
      if (artistOnly) payload.artist_only = true;

      const typeFilter = fieldValue('mbReleaseType');
      if (typeFilter) payload.type = typeFilter;

      // One-shot flags set by callers that need a wider search.
      if (global._mbSearchIncludeOwned === true) {
        payload.include_owned = true;
        global._mbSearchIncludeOwned = false;
      }
      if (global._mbSearchWithReleases === true) {
        payload.with_releases = true;
        global._mbSearchWithReleases = false;
      }

      const data = await global.api.postJson(SEARCH_ENDPOINT, payload);

      if (data && data.error) {
        if (resultsEl) {
          resultsEl.innerHTML =
            '<div class="alert alert-danger"><i class="bi bi-exclamation-triangle me-1"></i>' +
            `MusicBrainz search failed: ${esc(data.error)}</div>`;
        }
        return;
      }

      let releases = data.releases || [];

      if (typeFilter) {
        const want = typeFilter.toLowerCase();
        releases = releases.filter((r) => derivedCategory(r) === want);
      }

      const limitEl = document.getElementById('mbResultLimit');
      const max = parseInt(limitEl ? limitEl.value : String(DEFAULT_LIMIT), 10) || DEFAULT_LIMIT;
      if (releases.length > max) releases = releases.slice(0, max);

      // The component renders #mbResultCount, which nothing ever populated —
      // the header count stayed blank on every search.
      const countEl = document.getElementById('mbResultCount');
      if (countEl) {
        countEl.textContent = releases.length
          ? `${releases.length} result${releases.length === 1 ? '' : 's'}`
          : '';
      }

      if (!releases.length) {
        if (resultsEl) {
          resultsEl.innerHTML =
            '<div class="alert alert-info"><i class="bi bi-info-circle"></i> ' +
            `No releases found for "${esc(query)}"</div>`;
        }
        return;
      }

      renderResults(resultsEl, releases);
    } catch (error) {
      if (resultsEl) {
        resultsEl.innerHTML =
          `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> Error: ${esc(error.message)}</div>`;
      }
    }
  }

  /** Release objects for the rendered cards, keyed by index. */
  const resultCache = [];

  function renderResults(container, releases) {
    if (!container) return;

    resultCache.length = 0;

    const cards = releases.map((release, index) => {
      resultCache[index] = release;

      const cover = release.cover_art_url || '';
      const date = release.first_release_date || 'Unknown';
      const category = release.category || release.primary_type || 'Release';
      const artist = releaseArtist(release);

      const image = cover
        ? `<img src="${esc(cover)}" class="rounded shadow-sm" style="width:80px;height:80px;object-fit:cover;" alt="">`
        : '<div class="rounded bg-secondary d-flex align-items-center justify-content-center shadow-sm" ' +
          'style="width:80px;height:80px;"><i class="bi bi-music-note-beamed text-white fs-4"></i></div>';

      // The action reflects the caller's INTENT, not a transient flag.
      //
      // ⚠️ BUG FIXED: this used to read `selectionCallback` alone, and
      // confirmReleaseSelection() set that to null after using it. So after a
      // first successful match, EVERY later search in the same modal session
      // rendered the "Soulseek" download button instead of "Match Release" —
      // even though the modal had been opened from the album page's
      // "Lookup MBID" button, whose whole purpose is matching. The action is
      // now driven by `matchIntent`, which only changes when the modal is
      // opened with a different intent (see openGlobalMbSearch).
      //
      // ⭐ "QUEUE", NOT "SOULSEEK", AND THE SAME CALL THE UNIFIED SEARCH MAKES.
      //
      // Reported from the upcoming-releases page: pressing this button "doesn't
      // look like anything is happening". It called ``global.downloadMbRelease``
      // — a function defined ONLY in ``pages/download-queue.js``, which that
      // page does not load — so the click hit the feature-detect guard below
      // and returned without a word. A button that silently does nothing is
      // worse than no button.
      //
      // It now routes through ``queueRelease`` (see the click handler), the
      // helper this file already uses for its own queueing, which posts the
      // same ``/api/musicbrainz/download`` the unified search's Queue button
      // does. The label says "Add to queue" because that is what happens: the
      // release is QUEUED, and Soulseek is only how the queue later fetches it.
      const action = matchIntent
        ? `<button class="btn btn-sm btn-success mb-select-match" data-index="${index}">
             <i class="bi bi-check-circle"></i> Match Release</button>`
        : `<button class="btn btn-sm btn-outline-primary mb-download-release" data-index="${index}"
                   title="Add this release to the download queue">
             <i class="bi bi-download"></i> Add to queue</button>`;

      return `
        <div class="list-group-item bg-dark-subtle border-secondary mb-2 rounded">
          <div class="d-flex gap-3 align-items-start">
            ${image}
            <div class="flex-grow-1">
              <h6 class="mb-1 fw-bold">${esc(release.title)}</h6>
              <p class="mb-1 text-muted small">${esc(artist)}</p>
              <div class="d-flex gap-2 align-items-center mb-2 flex-wrap">
                <span class="badge bg-secondary">${esc(category)}</span>
                <span class="badge bg-info text-dark"><i class="bi bi-cloud"></i> MusicBrainz</span>
                <span class="text-muted small">${esc(date)}</span>
              </div>
            </div>
            <div class="flex-shrink-0 mt-2 mt-sm-0">${action}</div>
          </div>
        </div>`;
    }).join('');

    container.innerHTML = `<div class="list-group">${cards}</div>`;

    container.querySelectorAll('.mb-select-match').forEach((btn) => {
      btn.addEventListener('click', function () {
        stageSelection(resultCache[parseInt(this.dataset.index, 10)]);
      });
    });

    container.querySelectorAll('.mb-download-release').forEach((btn) => {
      btn.addEventListener('click', function () {
        const release = resultCache[parseInt(this.dataset.index, 10)];
        if (!release) return;
        queueReleaseForDownload(this, release);
      });
    });
  }

  /**
   * Add one MusicBrainz release to the download queue.
   *
   * ⭐ THE SAME ENDPOINT THE UNIFIED SEARCH'S QUEUE BUTTON USES.
   *
   * This used to call ``global.downloadMbRelease``, which only exists on pages
   * that load ``pages/download-queue.js``. On the upcoming-releases page — where
   * this modal is the ONLY download surface — that function is undefined, so the
   * click did nothing at all and said nothing.
   *
   * ``/api/musicbrainz/download`` takes a release id, resolves the tracklist
   * server-side and creates the queue rows, so it needs no page-local helper.
   * ``queue_items_only`` keeps it a pure queue add: no search is started here,
   * which is what "Add to queue" promises.
   *
   * Every outcome is REPORTED. A silent no-op is what made the old button look
   * broken, so even the "nothing to queue" answer from the API is surfaced.
   */
  async function queueReleaseForDownload(btn, release) {
    const releaseId = release && release.id ? String(release.id) : '';
    if (!releaseId) {
      toastError('This result has no MusicBrainz release id.');
      return;
    }

    const artist = releaseArtist(release);
    const title = release.title || '';

    // ⚠️ ``buttonState.withBusy(btn, label, fn)`` RUNS the work itself and
    // restores the button in a ``finally``; it is not a "set busy" call. The
    // first draft of this used its return value as a restore function, which
    // would have queued nothing at all.
    const run = async () => {
      try {
        const data = await global.api.postJson('/api/musicbrainz/download', {
          release_id: releaseId,
          release_title: title,
          artist: artist,
          method: 'slskd',
          persistent_search: false,
          queue_items_only: true,
        });

        const queuedCount = Number(data && data.queued_tracks);
        const explicitZero =
          data && data.queued_tracks !== undefined && queuedCount === 0;
        if (
          (data && (explicitZero || data.queued === false || data.success === false))
          && !(queuedCount > 0)
        ) {
          toastError(
            (data && (data.message || data.error)) || 'Nothing to queue for this release.'
          );
          return;
        }

        toastSuccess(
          queuedCount > 0
            ? `Added to queue: ${title} (${queuedCount} track${queuedCount === 1 ? '' : 's'})`
            : `Added to queue: ${title}`
        );
        // Same idea as the unified search's queue hook: let a page refresh
        // whatever queue view it owns instead of showing a stale list.
        document.dispatchEvent(new CustomEvent('mbDownloadQueued', {
          detail: { releaseId, title, artist, queued: queuedCount || null },
        }));
      } catch (error) {
        toastError(
          'Could not queue this release: '
          + (error && error.message ? error.message : error)
        );
      }
    };

    if (global.buttonState && typeof global.buttonState.withBusy === 'function') {
      return global.buttonState.withBusy(btn, 'Queuing…', run);
    }
    return run();
  }

  /** Stage a chosen release and reveal the confirmation panel. */
  function stageSelection(release) {
    if (!release) return;
    pendingRelease = release;

    const titleEl = document.getElementById('mbSelectedTitle');
    const artistEl = document.getElementById('mbSelectedArtist');
    const panel = document.getElementById('mbSelectedRelease');

    if (titleEl) titleEl.textContent = release.title || '';
    if (artistEl) artistEl.textContent = releaseArtist(release);
    if (panel) {
      panel.classList.remove('d-none');
      panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  /**
   * Fire the pending callback with the staged release, then close.
   * Wired to #mbSelectedRelease's "Apply Match" button — previously
   * undefined, so that button existed in the markup with no handler
   * anywhere in the codebase.
   */
  function confirmReleaseSelection() {
    if (!pendingRelease) return;

    // Capture before clearing: the callback is single-use, but `matchIntent`
    // deliberately is not (it describes the session, not this one pick).
    const callback = selectionCallback;
    const release = pendingRelease;

    pendingRelease = null;
    selectionCallback = null;

    const panel = document.getElementById('mbSelectedRelease');
    if (panel) panel.classList.add('d-none');
    if (global.modal) global.modal.hide(MODAL_ID);

    if (callback) {
      try {
        callback(release);
      } catch (error) {
        // A throwing caller must not leave the modal in a half-closed state,
        // and the user needs to know the match did not apply.
        console.error('MusicBrainz selection callback failed', error);
        if (global.toast) global.toast.error('Could not apply the release: ' + error.message);
      }
    }
  }

  /**
   * Clear the search form and results.
   * The component's eraser button calls this via onclick, but it was defined
   * nowhere — clicking it threw ReferenceError and nothing cleared.
   */
  function clearSearch() {
    ['mbSearchArtist', 'mbSearchAlbum', 'mbSearchTrack', 'mbSearchYear', 'mbSearchInput']
      .forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });

    // A pending "Select Match" must not survive a cleared search — the
    // confirmation panel would keep showing a release that no longer relates
    // to whatever the user searches for next.
    pendingRelease = null;

    const resultsEl = document.getElementById('mbSearchResults');
    if (resultsEl) {
      resultsEl.innerHTML =
        '<div class="col-12 text-center text-muted py-5">' +
        '<i class="bi bi-hexagon-fill opacity-50" style="font-size:3rem;"></i>' +
        '<p class="mt-3 fw-semibold">Enter a search term above to find releases on MusicBrainz.</p></div>';
    }

    const countEl = document.getElementById('mbResultCount');
    if (countEl) countEl.textContent = '';

    const panel = document.getElementById('mbSelectedRelease');
    if (panel) panel.classList.add('d-none');
  }

  // ── Lookup form ─────────────────────────────────────────────────────────

  /**
   * Open the shared modal from the lookup form, falling back to reading the
   * form fields when called with no arguments.
   *
   * NOTE: there is exactly ONE definition of doLookup. downloads.js used to
   * open with a guarded `window.doLookup = window.doLookup || function (…)`
   * and then define it again, unguarded, further down. The `||` guard was
   * meaningless — the second assignment always won — and the two bodies were
   * not equivalent: only the later one fell back to the form fields.
   */
  function doLookup(artist, album, track, year, callback) {
    if (!artist && !album && !track && !year) {
      artist = fieldValue('lookupArtist') || fieldValue('mbSearchArtist');
      album = fieldValue('lookupAlbum') || fieldValue('mbSearchAlbum');
      track = fieldValue('lookupTrack') || fieldValue('mbSearchTrack');
      year = fieldValue('lookupYear') || fieldValue('mbSearchYear');
    }
    if (!artist && !album && !track && !year) return;

    const cb = typeof callback === 'function'
      ? callback
      : function (selected) {
          if (typeof global.downloadMbRelease === 'function') {
            global.downloadMbRelease(selected.id, selected.title, selected.artist, 'slskd');
          }
        };

    openGlobalMbSearch(artist, album, cb, track, year);
  }

  function clearLookup() {
    ['lookupArtist', 'lookupAlbum', 'lookupTrack', 'lookupYear',
     'mbSearchArtist', 'mbSearchAlbum', 'mbSearchTrack', 'mbSearchYear']
      .forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
  }

  // Enter submits the single-input variant.
  document.addEventListener('DOMContentLoaded', function () {
    const input = document.getElementById('mbSearchInput');
    if (input) {
      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          performSearch();
        }
      });
    }
  });

  // Some flows dispatch an event instead of passing a callback.
  document.addEventListener('mbReleaseSelected', function (event) {
    const detail = event.detail || {};
    const release = detail.release || global._selectedMusicBrainzRelease;
    if (!release) return;

    const id = detail.releaseId || release.id || '';
    const title = detail.title || release.title || '';
    global._selectedMusicBrainzRelease = null;

    if (typeof global.downloadMbRelease === 'function' && id && title) {
      global.downloadMbRelease(id, title, releaseArtist(release), 'slskd');
    }
  });

  global.mbSearch = {
    open: openGlobalMbSearch,
    search: performSearch,
    clear: clearSearch,
    confirm: confirmReleaseSelection,
    derivedCategory,
    releaseArtist,
  };

  // Legacy names — the component's markup and several pages call these.
  global.openGlobalMbSearch = openGlobalMbSearch;
  global.performMbSearch = performSearch;
  global.performMbDownloadSearch = performSearch;
  global.clearMbSearch = clearSearch;
  global.confirmReleaseSelection = confirmReleaseSelection;
  global.handleGlobalMbSelect = function (indexOrEncoded) {
    // Accepts the new data-index form; the old encoded-object form is gone.
    const index = parseInt(indexOrEncoded, 10);
    if (!Number.isNaN(index) && resultCache[index]) stageSelection(resultCache[index]);
  };
  global.doLookup = doLookup;
  global.clearLookup = clearLookup;
  global.mbDerivedCategory = derivedCategory;
})(window);
