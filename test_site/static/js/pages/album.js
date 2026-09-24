/* ==========================================================================
   static/js/pages/album.js
   Album detail page — playback, genres, MusicBrainz compare/apply, bulk delete.

   Load order:
       utils/dom.js  →  utils/api.js
       ui/toast.js  →  ui/modal.js  →  ui/confirm.js
       ui/button-state.js
       album_detail.js

   Still depends on downloads.js for the shared MusicBrainz modal
   (openGlobalMbSearch / performMbSearch / handleGlobalMbSelect /
   confirmReleaseSelection) and on main.js for markFormDirty.

   ── ENDPOINT PREFIX ───────────────────────────────────────────────────────
   The compare / apply-field / ignore-field / bulk-delete calls hit the
   api_v1 blueprint (api_v1/albums.py, api_v1/tracks.py), assumed registered
   under /api/v1. VERIFY against __init__.py's
   register_blueprint(..., url_prefix=...) and update API_V1 below if it
   differs. Every other endpoint on this page (recommend-genres, queue/add,
   artist/similar) uses unversioned "/api/..." paths from an older
   blueprint, so this page intentionally mixes both.

   ── CHANGES FROM THE PREVIOUS VERSION ─────────────────────────────────────
   1. FAVOURITES REMOVED. `window.toggleAlbumFavourite` is deleted. It was
      backed by /api/album/favourite while main.js's same-named function was
      backed by /api/bookmarks — two different tables behind one global,
      resolved only by load order. Remove the heart button and
      #albumFavouriteIcon from album_detail.html too.

   2. `_API_V1_PREFIX` WAS A TOP-LEVEL `const`. Classic <script> tags share
      one top-level scope and `const` cannot be redeclared in it, so a second
      file declaring that name would throw
      "Identifier has already been declared" at PARSE time and die entirely —
      the same failure that `currentImportData` caused between playlist.js
      and playlist_import.js. It is now private to this module's IIFE.

   3. DUPLICATED BANNER BUILDER — `_displayMBComparison` re-implemented the
      "create #mb-compare-banner if absent" block inline instead of calling
      `_showMBCompareBanner`, which exists directly above it and does exactly
      that. One builder now, used by both paths.

   4. `escapeJsString` in an inline onclick — the match-candidate rows built
          onclick="doAlbumMatchTrack('${escapeJsString(id)}')"
      using a helper defined in downloads.js. Replaced with addEventListener
      + data attributes, so this page no longer needs downloads.js to have
      parsed first just to render a table.

   5. `confirm()` → `await ui.confirm()`, `alert()` → toast, hand-rolled
      spinners → ui/button-state.js, hand-rolled green ticks → setDone().
   ========================================================================== */

(function (global) {
  'use strict';

  const API_V1 = '/api/v1';

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function pageArtist() {
    return global._pageData ? global._pageData.artistName : '';
  }

  function pageAlbum() {
    return global._pageData ? global._pageData.albumName : '';
  }

  function notifyError(message) {
    if (global.toast) global.toast.error(message);
    else global.alert(message);
  }

  function notifySuccess(message) {
    if (global.toast) global.toast.success(message);
    else global.alert(message);
  }

  function confirmFn(opts) {
    if (global.ui && global.ui.confirm) return global.ui.confirm(opts);
    const parts = [opts.message];
    if (opts.detail) parts.push(opts.detail);
    return Promise.resolve(global.confirm(parts.join('\n\n')));
  }

  function markDirty() {
    if (typeof global.markFormDirty === 'function') {
      global.markFormDirty('albumMetadataForm');
    }
  }

  // ── Playback ────────────────────────────────────────────────────────────

  function playAlbum() {
    const playBtns = document.querySelectorAll('.player-play-btn');
    if (!playBtns.length) {
      notifyError('No playable tracks found for this album.');
      return;
    }
    playBtns[0].click();
  }

  /**
   * Play one track from a row button.
   * Player.playTrack takes a single object — see the note in player.js about
   * the old positional signature.
   */
  function playTrackFromAlbum(btn) {
    if (typeof Player === 'undefined' || typeof Player.playTrack !== 'function') {
      notifyError('The player is not available on this page.');
      return;
    }
    Player.playTrack({
      id: btn.getAttribute('data-track-id'),
      title: btn.getAttribute('data-title'),
      artist: btn.getAttribute('data-artist'),
      albumArtUrl: btn.getAttribute('data-art'),
    });
  }

  // ── Track row actions ───────────────────────────────────────────────────

  /**
   * Open the comprehensive track edit modal (#editTrackModal), populated
   * with this track's current metadata.
   *
   * FIXED: this previously looked for an id — #trackEditModal — that
   * exists NOWHERE in the template. components/modals/_track_edit.html
   * (included by both album_detail.html and artist_detail.html) actually
   * defines two different modals:
   *   #simpleEditTrackModal   single-field quick edit, saved via
   *                           saveEditedTrack()
   *   #editTrackModal         the full form (~30 fields: title/artist/
   *                           album/year/rating/genres/writer/composer/
   *                           ISRC/MB ids/flags/...), saved via
   *                           saveComprehensiveEditedTrack()
   * The "Edit track metadata" pencil icon on this page's track rows always
   * fell through to the `/track/{id}/edit` redirect, since the modal it
   * asked for could never be found.
   *
   * This opens the comprehensive form — the pencil icon's title text
   * ("Edit track metadata") matches that modal's scope, not the
   * single-field one.
   */

  // ── Simple single-field track edit ──────────────────────────────────────
  //
  // IMPLEMENTED 2026-09-18 — this was the missing half of the track edit.
  //
  // components/modals/_track_edit.html renders #simpleEditTrackModal with a
  // Save button calling saveEditedTrack(), and this file's own header
  // documented the modal as live — but saveEditedTrack() was defined NOWHERE
  // in any tree (New/static/js, static/js or old_system/static). The quick
  // edit had therefore never worked since the inline JS was split into
  // modules: the button threw ReferenceError and the modal sat there doing
  // nothing.
  //
  // services/genres.js::editTrackArtist() is the caller that surfaces this
  // most visibly (it opens this modal to rename a track's artist).
  //
  // The payload mirrors pages/artist.js::saveEditedTrackFromArtistPage, which
  // performs the identical operation and already worked — the artist page has
  // its own inline copy of this modal, so it never hit the missing function.
  //
  // NOTE the id split: the SIMPLE modal's hidden track id is
  // #simpleEditTrackId, while the COMPREHENSIVE modal's is #editTrackId. They
  // used to share the name `editTrackId`, which meant the quick edit read the
  // wrong track when both modals were on the page. Both are read here (simple
  // first) so this also serves the artist page, whose inline modal still uses
  // the old #editTrackId + #editTrackCurrentField pairing.

  async function saveEditedTrack(btn) {
    const idEl = document.getElementById('simpleEditTrackId')
      || document.getElementById('editTrackId');
    const fieldEl = document.getElementById('editTrackCurrentField');
    const valueEl = document.getElementById('editTrackValue');

    const trackId = idEl ? String(idEl.value || '').trim() : '';
    const field = fieldEl ? String(fieldEl.value || '').trim() : '';
    const value = valueEl ? String(valueEl.value || '').trim() : '';

    if (!trackId || !field) {
      notifyError('No track or field selected for quick edit.');
      return;
    }

    // The field name is used directly as the payload key, so it must be one
    // the API accepts. Callers are genres.js ('artist') and the modal's own
    // title edit ('title').
    const payload = { track_id: trackId, sync_to_file: true };
    payload[field] = value;

    return global.buttonState.withBusy(btn, 'Saving…', async () => {
      try {
        const data = await global.api.postJson('/api/track/update-metadata', payload);
        if (!data.success) {
          notifyError(data.error || 'Failed to update');
          return;
        }

        if (data.file_synced === false) {
          global.toast.warning(
            'Saved to database, but file tags were not updated. Check file permissions and the logs.'
          );
        } else {
          notifySuccess('Track metadata updated (database + file tags)');
        }

        // Hide whichever modal was actually open: genres.js shows
        // #simpleEditTrackModal, the artist page shows #editTrackModal.
        ['simpleEditTrackModal', 'editTrackModal'].forEach((id) => {
          const el = document.getElementById(id);
          if (el && el.classList.contains('show') && global.modal) global.modal.hide(id);
        });
        setTimeout(() => global.location.reload(), 1000);
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  async function openEditTrackFromAlbum(trackId) {
    const modalEl = document.getElementById('editTrackModal');
    if (!modalEl) {
      // Component not included on this page — fall back rather than show
      // nothing.
      // The route is /track/<id> — NOT /track/<id>/edit. The latter has never
      // been registered (same class of bug as the delete button below, which
      // navigated to /track/<id>/delete), so this fallback 404'd whenever the
      // edit modal was absent from the page.
      global.location.href = `/track/${encodeURIComponent(trackId)}`;
      return;
    }

    try {
      const track = await global.api.getJson(`/api/track/${encodeURIComponent(trackId)}`);
      populateComprehensiveTrackModal(trackId, track || {});
      if (global.modal) global.modal.show(modalEl);
    } catch (error) {
      notifyError('Error loading track: ' + error.message);
    }
  }

  /**
   * Simple string/number fields on #editTrackModal, mapped id -> track key.
   * Matches components/modals/_track_edit.html exactly — do not rename one
   * side without the other.
   */
  const COMPREHENSIVE_TRACK_FIELDS = {
    editTrackId: 'id',
    editTrackTitleField: 'title',
    editTrackArtistField: 'artist',
    editTrackAlbumField: 'album',
    editTrackYearField: 'year',
    editTrackStarsField: 'stars',
    editTrackSingleField: 'is_single',
    editTrackConfidenceField: 'single_confidence',
    editTrackAlbumArtistField: 'album_artist',
    editTrackWriterField: 'writer',
    editTrackComposerField: 'composer',
    editTrackWorkField: 'work',
    editTrackCommentField: 'comment',
    editTrackTrackNumberField: 'track_number',
    editTrackDiscNumberField: 'disc_number',
    editTrackISRCField: 'isrc',
    editTrackMBIDField: 'mbid',
    editTrackMBAlbumIdField: 'musicbrainz_albumid',
    editTrackMBArtistIdField: 'musicbrainz_artistid',
    editTrackMBAlbumArtistIdField: 'musicbrainz_albumartistid',
    editTrackMBReleaseGroupIdField: 'musicbrainz_releasegroupid',
    editTrackMBReleaseTrackIdField: 'musicbrainz_releasetrackid',
    editTrackMBWorkIdField: 'musicbrainz_workid',
  };

  /** Checkbox flags on #editTrackModal, id -> track key. */
  const COMPREHENSIVE_TRACK_FLAGS = {
    editTrackIsCoverField: 'is_cover',
    editTrackAlternateTakeField: 'alternate_take',
    editTrackIsCompilationField: 'is_compilation',
    editTrackIsLiveField: 'is_live',
    editTrackIsAcousticField: 'is_acoustic',
    editTrackIsRemixField: 'is_remix',
  };

  /** Staged genre chips for #editTrackModal — mirrors the album-genre
   *  staging pattern above, scoped to whichever track is currently open. */
  let comprehensiveTrackGenres = [];

  function populateComprehensiveTrackModal(trackId, track) {
    const titleEl = document.getElementById('editTrackTitle');
    if (titleEl) titleEl.textContent = track.title || 'Unknown';

    Object.entries(COMPREHENSIVE_TRACK_FIELDS).forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (id === 'editTrackId') {
        el.value = trackId;
      } else {
        el.value = track[key] != null ? track[key] : '';
      }
    });

    Object.entries(COMPREHENSIVE_TRACK_FLAGS).forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (el) el.checked = !!track[key];
    });

    comprehensiveTrackGenres = track.genres
      ? String(track.genres).split(/[;,/\\]/).map((g) => g.trim()).filter(Boolean)
      : [];
    renderComprehensiveTrackGenres();
    loadRecommendedGenresForComprehensiveTrack(trackId);
  }

  function renderComprehensiveTrackGenres() {
    const container = document.getElementById('editTrackGenresDisplay');
    if (!container) return;
    container.innerHTML = '';

    if (!comprehensiveTrackGenres.length) {
      container.innerHTML = '<span class="text-muted small">No genres set</span>';
    } else {
      comprehensiveTrackGenres.forEach((genre) => {
        const badge = document.createElement('span');
        badge.className = 'badge bg-primary me-1 mb-1';
        // textContent, never innerHTML: a genre name is user input.
        badge.textContent = genre;

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close btn-close-white ms-1';
        close.style.fontSize = '0.6rem';
        close.setAttribute('aria-label', `Remove ${genre}`);
        close.addEventListener('click', () => {
          comprehensiveTrackGenres = comprehensiveTrackGenres.filter((g) => g !== genre);
          renderComprehensiveTrackGenres();
        });

        badge.appendChild(close);
        container.appendChild(badge);
      });
    }

    const hidden = document.getElementById('editTrackGenresField');
    if (hidden) hidden.value = comprehensiveTrackGenres.join(';');
  }

  /** Wired to the modal's own "Add" button (onclick="addEditTrackGenre()"). */
  function addEditTrackGenre() {
    const input = document.getElementById('editTrackGenreInput');
    if (!input) return;
    const genre = input.value.trim();
    if (genre && !comprehensiveTrackGenres.includes(genre)) {
      comprehensiveTrackGenres.push(genre);
      renderComprehensiveTrackGenres();
    }
    input.value = '';
    input.focus();
  }

  async function loadRecommendedGenresForComprehensiveTrack(trackId) {
    const section = document.getElementById('recommendedGenresSection');
    const display = document.getElementById('recommendedGenresDisplay');
    if (!section || !display) return;

    try {
      const data = await global.api.getJson(`/api/genres/track/${encodeURIComponent(trackId)}`);
      const recommended = new Map();
      ['lastfm_tags', 'discogs_genres', 'spotify_genres'].forEach((key) => {
        (data.genres && data.genres[key] || []).forEach((genre) => {
          const name = typeof genre === 'object' ? genre.name : genre;
          if (name) recommended.set(name, (recommended.get(name) || 0) + 1);
        });
      });

      if (!recommended.size) {
        section.style.display = 'none';
        return;
      }

      section.style.display = 'block';
      display.innerHTML = Array.from(recommended.entries()).map(([genre, count], index) => `
        <button type="button" class="btn btn-sm btn-outline-info rec-genre-btn"
                data-index="${index}" title="Add to track genres">
          ${esc(genre)} <small class="text-muted ms-1">(${count})</small>
        </button>`).join('');

      const entries = Array.from(recommended.keys());
      display.querySelectorAll('.rec-genre-btn').forEach((btn) => {
        btn.addEventListener('click', function () {
          const genre = entries[parseInt(this.dataset.index, 10)];
          if (genre && !comprehensiveTrackGenres.includes(genre)) {
            comprehensiveTrackGenres.push(genre);
            renderComprehensiveTrackGenres();
          }
        });
      });
    } catch (_error) {
      section.style.display = 'none';
    }
  }

  /** Save handler for #editTrackModal's "Save Changes" button. */
  function saveComprehensiveEditedTrack(btn) {
    const trackId = document.getElementById('editTrackId').value;
    if (!trackId) {
      notifyError('No track ID');
      return;
    }

    const payload = { track_id: trackId, sync_to_file: true, genres: comprehensiveTrackGenres.join(';') };

    Object.entries(COMPREHENSIVE_TRACK_FIELDS).forEach(([id, key]) => {
      if (id === 'editTrackId') return;
      const el = document.getElementById(id);
      if (el) payload[key] = el.value.trim() || null;
    });
    Object.entries(COMPREHENSIVE_TRACK_FLAGS).forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (el) payload[key] = el.checked;
    });

    if (!payload.title) {
      notifyError('Title is required');
      return;
    }

    return global.buttonState.withBusy(btn, 'Saving…', async () => {
      try {
        const data = await global.api.postJson('/api/track/update-metadata', payload);
        if (!data.success) {
          notifyError(data.error || 'Failed to update');
          return;
        }
        if (data.file_synced === false) {
          global.toast.warning('Saved to database, but file tags were not updated. Check file permissions and the logs.');
        } else {
          notifySuccess('Track metadata updated (database + file tags)');
        }
        global.modal.hide('editTrackModal');
        setTimeout(() => global.location.reload(), 500);
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  /**
   * Delete one track.
   *
   * FIXED: this navigated to `/track/${id}/delete`, a URL no route in this
   * app has ever served — every click 404'd. It also made a destructive
   * change reachable by a GET (prefetch, crawler, stray link). Now a POST to
   * the api_v1 track-delete route, which reuses the same service the artist
   * page's corrections page calls, so both pages delete identically.
   */
  async function deleteTrack(trackId) {
    const accepted = await confirmFn({
      title: 'Delete track',
      message: 'Delete this track?',
      detail: 'It is removed from the database, and its file is deleted from disk too, if it exists. This cannot be undone.',
      tone: 'danger',
      confirmLabel: 'Delete',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson(
        `${API_V1}/tracks/${encodeURIComponent(trackId)}/delete`,
        { delete_file: true },
      );
      if (!data.success) {
        notifyError(data.error || 'Failed to delete track');
        return;
      }
      notifySuccess(`Deleted track${data.deleted_file ? ' and its file' : ' (no file on disk)'}.`);
      setTimeout(() => global.location.reload(), 500);
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  /** Fill the Track Artist field with the most common artist on this album. */
  function populateMajorityArtist() {
    const rows = document.querySelectorAll('#albumTracksTbody tr');
    const artists = [];
    rows.forEach((row) => {
      const artist = row.getAttribute('data-track-artist');
      if (artist) artists.push(artist);
    });
    if (!artists.length) return;

    const counts = artists.reduce((acc, name) => {
      acc[name] = (acc[name] || 0) + 1;
      return acc;
    }, {});
    const majority = Object.keys(counts).reduce((a, b) => (counts[a] > counts[b] ? a : b));

    const input = document.getElementById('track_artist');
    if (input) input.value = majority;
  }

  // ── Album genres (staged, written to all tracks on save) ────────────────

  function genreBadges() {
    const container = document.getElementById('albumGenresContainer');
    return container ? Array.from(container.querySelectorAll('.badge')) : [];
  }

  function badgeText(badge) {
    return badge.textContent.replace('×', '').trim();
  }

  /**
   * Sync the hidden input the form actually submits.
   * Also marks the form dirty: the sticky save bar only appears on
   * input/change events, and genre chips are added by JS, which fires
   * neither — without this the staged genres look saved when they are not.
   */
  function updateHiddenGenres() {
    const hidden = document.getElementById('album_genres');
    if (!hidden) return;
    hidden.value = genreBadges().map(badgeText).filter(Boolean).join(', ');
    markDirty();
  }

  function addAlbumGenre() {
    const input = document.getElementById('newAlbumGenreInput');
    const container = document.getElementById('albumGenresContainer');
    if (!input || !container) return;

    const genre = input.value.trim();
    if (!genre) return;

    // Do not stage the same genre twice.
    const existing = genreBadges().map((b) => badgeText(b).toLowerCase());
    if (existing.includes(genre.toLowerCase())) {
      input.value = '';
      return;
    }

    // "No genres set" placeholder is not a badge — clear it on first add.
    const placeholder = container.querySelector('.text-muted');
    if (placeholder && !placeholder.classList.contains('badge')) placeholder.remove();

    const badge = document.createElement('span');
    badge.className = 'badge bg-primary me-1 mb-1';
    // textContent, never innerHTML — a genre name is user input.
    badge.textContent = genre;

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-close-white ms-1';
    close.style.fontSize = '0.6rem';
    close.setAttribute('aria-label', `Remove ${genre}`);
    close.addEventListener('click', function () {
      stageRemoveAlbumGenre(this);
    });

    badge.appendChild(close);
    container.appendChild(badge);
    input.value = '';
    updateHiddenGenres();
  }

  /**
   * Remove a staged genre.
   * Accepts either the close button element, or the genre name as a string
   * (the Jinja-rendered chips pass the name).
   */
  function stageRemoveAlbumGenre(target) {
    if (typeof target === 'string') {
      const wanted = target.trim().toLowerCase();
      genreBadges().forEach((badge) => {
        if (badgeText(badge).toLowerCase() === wanted) badge.remove();
      });
    } else if (target && target.closest) {
      const badge = target.closest('.badge');
      if (badge) badge.remove();
    }
    updateHiddenGenres();
  }

  function goToAlbumGenres() {
    const btn = document.querySelector('#albumPageTabs [data-bs-target="#tab-genres"]');
    if (btn && global.bootstrap) global.bootstrap.Tab.getOrCreateInstance(btn).show();
  }

  // ── Online genre recommendations ────────────────────────────────────────

  function fetchGenreRecommendations() {
    const btn = document.getElementById('fetchGenresBtn');
    const section = document.getElementById('recommendedGenresSection');
    const container = document.getElementById('recommendedGenres');
    if (!btn || !section || !container) return;

    const run = async () => {
      try {
        const data = await global.api.getJson(
          `/api/album/recommend-genres?artist=${encodeURIComponent(pageArtist())}` +
          `&album=${encodeURIComponent(pageAlbum())}`
        );

        if (!data.success || !data.genres || !data.genres.length) {
          notifyError('No recommendations found.');
          return;
        }

        section.style.display = 'block';
        container.innerHTML = '';

        // createElement rather than an innerHTML template: the original
        // interpolated the genre straight into
        // onclick="addRecommendedGenre('...')", so a genre containing an
        // apostrophe ("rock 'n' roll") broke the handler and a hostile tag
        // name was executable.
        data.genres.forEach((genre) => {
          const chip = document.createElement('span');
          chip.className = 'badge bg-secondary';
          chip.style.cursor = 'pointer';
          chip.setAttribute('role', 'button');
          chip.setAttribute('tabindex', '0');
          chip.textContent = genre + ' +';
          const activate = () => addRecommendedGenre(genre);
          chip.addEventListener('click', activate);
          chip.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              activate();
            }
          });
          container.appendChild(chip);
          container.appendChild(document.createTextNode(' '));
        });
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };

    return global.buttonState
      ? global.buttonState.withBusy(btn, 'Fetching…', run)
      : run();
  }

  function addRecommendedGenre(genre) {
    const input = document.getElementById('newAlbumGenreInput');
    if (!input) return;
    input.value = genre;
    addAlbumGenre();
  }

  /** Stage every checked source tag as an album genre. */
  function applySelectedAlbumSourceTags() {
    const checks = document.querySelectorAll('.album-source-tag-check:checked');
    const input = document.getElementById('newAlbumGenreInput');
    if (!input) return;

    checks.forEach((check) => {
      input.value = check.value;
      addAlbumGenre();
      check.checked = false;
    });

    // Re-hide every "Add Selected" button now nothing is checked — the
    // per-pane change handler below only fires on user interaction.
    document.querySelectorAll('.album-apply-source-tags-btn').forEach((btn) => {
      btn.style.display = 'none';
    });
  }

  document.addEventListener('change', function (event) {
    const target = event.target;
    if (!target.classList || !target.classList.contains('album-source-tag-check')) return;
    const pane = target.closest('.tab-pane');
    if (!pane) return;
    const anyChecked = pane.querySelectorAll('.album-source-tag-check:checked').length > 0;
    const applyBtn = pane.querySelector('.album-apply-source-tags-btn');
    if (applyBtn) applyBtn.style.display = anyChecked ? 'inline-block' : 'none';
  });

  // ── MusicBrainz lookup ──────────────────────────────────────────────────

  /**
   * Open the SHARED modal from base.html.
   *
   * This used to open a page-local #albumLookupModal that included the
   * search component a second time — producing duplicate #mbSearchArtist /
   * #mbSearchResults ids, so performMbSearch() (which resolves them with
   * getElementById, i.e. the FIRST match) wrote its results into base.html's
   * hidden global modal while the visible one sat on its placeholder.
   */
  function openAlbumLookupModal() {
    if (typeof global.openGlobalMbSearch !== 'function') {
      console.error('openGlobalMbSearch is unavailable — is services/musicbrainz-picker.js loaded?');
      notifyError('MusicBrainz search is unavailable on this page.');
      return;
    }
    // The lookup is a network round trip plus (once a release is picked) a
    // best-release probe and a metadata preview — several seconds of silence
    // on a dropdown-item click that has no button to spin. The popup is opened
    // here and closed in applyAlbumMatch (or on dismissal) rather than wrapped,
    // because the window spans the user's choice in the modal.
    lookupBusy = global.busyPopup
      ? global.busyPopup.show('Looking up MusicBrainz match…')
      : null;

    global.openGlobalMbSearch(pageArtist(), pageAlbum(), function (selected) {
      if (selected) {
        // `.finally` guarantees the popup is released even if the match
        // resolution throws — applyAlbumMatch is async and is not awaited here,
        // so an escape would otherwise leave the popup up permanently.
        Promise.resolve()
          .then(() => applyAlbumMatch(selected))
          .catch((error) => {
            console.error('Applying the MusicBrainz match failed', error);
            notifyError('Could not apply the MusicBrainz match.');
          })
          .finally(endLookupBusy);
      } else {
        // Dismissed without a pick — release the popup so it cannot strand.
        endLookupBusy();
      }
    });
  }

  /**
   * The in-flight lookup popup, if any.
   *
   * Module-scoped because the popup is opened when the picker opens and must be
   * released from applyAlbumMatch, which is a separate callback.
   */
  let lookupBusy = null;

  /** Release the lookup popup. Safe to call repeatedly / with no popup. */
  function endLookupBusy() {
    if (global.busyPopup) global.busyPopup.hide(lookupBusy);
    lookupBusy = null;
  }

  /**
   * Refresh the album page after a mutation.
   *
   * ⚠️ THIS FUNCTION WAS REFERENCED BUT NEVER DEFINED, in either tree. Callers
   * guarded with `typeof global.refreshAlbumPage === 'function'`, which turned
   * the missing definition into a SILENT no-op: after changing the album art or
   * auto-linking MBIDs the page kept showing the old state and reported no error.
   *
   * The full reload is deliberate. Applying a new cover rewrites the art file
   * and `/art` is streamed from a BytesIO with no ETag/Cache-Control, so
   * reloading reliably re-fetches it rather than needing a `?t=` cache-buster.
   * And auto-linking can change several things at once (per-track MB status,
   * the comparison banner), so patching one element's `src` would leave the
   * rest stale.
   */
  function refreshAlbumPage() {
    window.location.reload();
  }

  global.refreshAlbumPage = refreshAlbumPage;

  /**
   * Auto-link Recording MBIDs for this album's unlinked tracks.
   *
   * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY. Both the Actions dropdown item
   * ("Auto-Link MBIDs") and the inline "Link" button already called
   * `autoLinkAllMbids()` — four call sites across the two trees — but nothing
   * defined it, so every click threw `ReferenceError: autoLinkAllMbids is not
   * defined`. A template has no compiler, so the broken button shipped.
   *
   * The endpoint it should have called already existed and was reachable:
   * POST /api/musicbrainz/link-album-mbids, which matches the local (unlinked)
   * tracklist against an MB release's recordings and writes
   * musicbrainz_trackid + recording_mbid onto each matched row.
   *
   * Needs a release MBID to fetch a tracklist from. `linkedReleaseMbid()`
   * returns the release-group id when the concrete release id is empty, and
   * the endpoint only acts on the latter (it validates a UUID and fetches the
   * release) — so a group-only album gets the endpoint's own explanatory
   * message rather than a silent no-op.
   */
  function autoLinkAllMbids() {
    const releaseId = linkedReleaseMbid();
    const run = async () => {
      const data = await global.api.postJson('/api/musicbrainz/link-album-mbids', {
        artist: pageArtist(),
        album: pageAlbum(),
        release_id: releaseId,
      });
      if (!data || data.success !== true) {
        notifyError((data && data.error) || 'Auto-linking MBIDs failed.');
        return;
      }
      // `linked: 0` with a message is the normal "nothing left to do" case,
      // so the server's own sentence is the best thing to show either way.
      notifySuccess(data.message || `Linked ${data.linked || 0} track(s).`);
      if (typeof global.refreshAlbumPage === 'function') global.refreshAlbumPage();
    };

    if (global.busyPopup) {
      return global.busyPopup.showAndRun('Auto-linking MusicBrainz IDs…', run)
        .catch((error) => notifyError('Error: ' + error.message));
    }
    return run().catch((error) => notifyError('Error: ' + error.message));
  }

  global.autoLinkAllMbids = autoLinkAllMbids;

  /**
   * Download EVERY track this album is missing from the library.
   *
   * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY — `onclick="downloadMissingTracks()"`
   * on the Actions dropdown threw ReferenceError.
   *
   * The missing tracks are already known: `comparisonData` is the last
   * Compare-with-MusicBrainz result, and the template renders each missing
   * track as a `.mb-queue-missing` row with a payload. So this reuses exactly
   * that path — one queue POST per missing track, each with the same payload
   * the per-row button builds — rather than inventing a bulk endpoint that
   * would have to re-derive the same set.
   *
   * Reuses `queueMissingTrack` so the release/recording MBIDs, duration and
   * source are filled identically.
   *
   * ⚠️ SETTLEMENT MARKERS — do NOT re-derive these. `buttonState.setBusy`
   * sets `disabled = true` for the whole call and marks the button
   * `_popularrBusy`; `setDone` then leaves `disabled = true` and adds the
   * non-reverting `btn-success` class. Only a FAILURE restores `disabled` to
   * false. Consequences that are easy to get wrong:
   *   - `disabled` is true while IN FLIGHT *and* on SUCCESS, so it means
   *     "not actionable", never "finished successfully".
   *   - There is no `data-queued` attribute; nothing sets one.
   * A poll that waits for `disabled` therefore matches on its FIRST tick and
   * reports every row as queued — a false success on total failure.
   */
  async function downloadMissingTracks() {
    const rows = Array.from(document.querySelectorAll('.mb-queue-missing'));
    const isDone = (btn) => btn.classList.contains('btn-success');
    const isBusy = (btn) => Boolean(btn._popularrBusy) || btn.disabled;
    const pending = rows.filter((btn) => !isDone(btn) && !isBusy(btn));

    if (!pending.length) {
      notifyError(
        rows.length
          ? 'Every missing track has already been queued.'
          : 'No missing tracks to queue — run Compare with MusicBrainz first.'
      );
      return;
    }

    const confirmed = global.ui && global.ui.confirm
      ? await global.ui.confirm({
        title: 'Download missing tracks',
        message: `Add ${pending.length} missing track(s) to the download queue?`,
        tone: 'primary',
        confirmLabel: 'Add to queue',
      })
      : window.confirm(`Add ${pending.length} missing track(s) to the download queue?`);
    if (!confirmed) return;

    const run = async () => {
      // ⚠️ The per-row payload lives in a CLOSURE (`queueMissingTrack(payload,
      // this)` in buildMissingRow), not in a data-* attribute — so it cannot be
      // read back out of the DOM. Clicking each button is therefore the correct
      // way to reuse that path: it is the same code the user's own click runs,
      // so release/recording MBIDs, duration and source are filled identically
      // and nothing is duplicated here.
      const ordered = pending.slice();
      // `queueMissingTrack` is async and returns a promise, but the listener
      // ignores it; so poll the button's own markers instead of awaiting.
      for (const btn of ordered) {
        btn.click();
      }

      // The per-row handler holds `_popularrBusy` for the whole request, so
      // waiting for THAT to clear is a true completion signal. Waiting on
      // `disabled` would match immediately (busy = disabled) and misreport.
      const deadline = Date.now() + 60000;
      let stillPending = 0;
      for (;;) {
        stillPending = ordered.filter((b) => Boolean(b._popularrBusy)).length;
        if (!stillPending || Date.now() >= deadline) break;
        await new Promise((r) => setTimeout(r, 150));
      }

      // Success is the non-reverting `btn-success` class setDone applies.
      const queued = ordered.filter(isDone).length;
      if (!queued) {
        notifyError('No track could be queued — see the error shown on the row.');
      } else if (stillPending) {
        notifySuccess(
          `Queued ${queued} of ${ordered.length} track(s). ${stillPending} still ` +
          'processing — watch the Downloads page.'
        );
      } else {
        notifySuccess(`Queued ${queued} of ${ordered.length} track(s).`);
      }
    };

    if (global.busyPopup) {
      return global.busyPopup.showAndRun('Adding missing tracks to queue…', run);
    }
    return run();
  }

  global.downloadMissingTracks = downloadMissingTracks;

  /**
   * Rename this album's files to match the configured naming format.
   *
   * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY — `renameAlbumFiles(...)` on the
   * Actions dropdown threw ReferenceError. The endpoint already existed and
   * was reachable: POST /api/album/{artist}/{album}/rename-files, which renames
   * every file in the album from its current metadata and updates the DB rows.
   *
   * The artist/album are taken from the page rather than the handler arguments
   * (the template passes them, but the page already knows them — and the page's
   * values are what the URL path must quote, so one source avoids a mismatch).
   * The arguments are still accepted so the existing `onclick` keeps working.
   */
  async function renameAlbumFiles(_artistArg, _albumArg) {
    const artist = _artistArg || pageArtist();
    const album = _albumArg || pageAlbum();
    if (!artist || !album) {
      notifyError('Cannot rename files without an artist and album.');
      return;
    }

    const confirmed = global.ui && global.ui.confirm
      ? await global.ui.confirm({
        title: 'Rename files',
        message: `Rename every file in "${album}" to the configured naming format?`,
        detail: 'Tags are re-read from the files, and the file paths in the database are updated.',
        tone: 'warning',
        confirmLabel: 'Rename',
      })
      : window.confirm(`Rename every file in "${album}"?`);
    if (!confirmed) return;

    const run = async () => {
      const data = await global.api.postJson(
        `/api/album/${encodeURIComponent(artist)}/${encodeURIComponent(album)}/rename-files`,
        {}
      );
      if (!data || data.success !== true) {
        notifyError((data && data.error) || 'Rename failed.');
        return;
      }
      notifySuccess(data.message || `Renamed ${data.renamed_count || 0} file(s).`);
      if (Array.isArray(data.errors) && data.errors.length) {
        notifyError(`${data.errors.length} file(s) could not be renamed — see the logs.`);
      }
    };

    if (global.busyPopup) {
      return global.busyPopup.showAndRun('Renaming album files…', run)
        .catch((error) => notifyError('Error: ' + error.message));
    }
    return run().catch((error) => notifyError('Error: ' + error.message));
  }

  global.renameAlbumFiles = renameAlbumFiles;

  /**
   * Open the "Change Album Art" dialog: search external sources, paste a URL,
   * or upload a file.
   *
   * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY — three call sites (the Actions
   * dropdown item AND the pencil over the album art, in both trees) threw
   * ReferenceError. All three backend endpoints already existed and were
   * reachable:
   *
   *   GET  /api/album/search-art?artist=&album=&source=   → candidates
   *   POST /api/album/set-art        {artist, album, image_url}
   *   POST /api/album/upload-art     multipart: artist, album, image
   *
   * The dialog is built on demand and torn down on close, so it needs no
   * template markup and cannot collide with ids on pages that never open it.
   */
  function openAlbumArtModal() {
    const artist = pageArtist();
    const album = pageAlbum();
    if (!artist || !album) {
      notifyError('Cannot change album art without an artist and album.');
      return;
    }

    const MODAL_ID = 'albumArtChangeModal';
    if (document.getElementById(MODAL_ID)) {
      // Already open — reuse rather than stacking a second copy.
      if (global.bootstrap) global.bootstrap.Modal.getOrCreateInstance(
        document.getElementById(MODAL_ID)
      ).show();
      return;
    }

    const wrap = document.createElement('div');
    wrap.className = 'modal fade';
    wrap.id = MODAL_ID;
    wrap.tabIndex = -1;
    wrap.innerHTML = `
      <div class="modal-dialog modal-lg modal-dialog-centered">
        <div class="modal-content bg-dark text-light border-secondary">
          <div class="modal-header border-secondary">
            <h5 class="modal-title"><i class="bi bi-image me-2"></i>Change Album Art</h5>
            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <div class="modal-body">
            <div class="d-flex gap-2 mb-3">
              <button type="button" class="btn btn-sm btn-outline-info" id="albumArtSearchBtn">
                <i class="bi bi-search me-1"></i>Search external sources
              </button>
              <select class="form-select form-select-sm bg-dark text-light border-secondary" id="albumArtSourceSel" style="max-width:12rem">
                <option value="musicbrainz" selected>MusicBrainz</option>
                <option value="discogs">Discogs</option>
                <option value="itunes">iTunes</option>
              </select>
            </div>
            <div id="albumArtStatus" class="small text-muted mb-2"></div>
            <div id="albumArtResults" class="row g-2 mb-3"></div>
            <hr class="border-secondary">
            <label for="albumArtUrlInput" class="form-label small">…or paste an image URL</label>
            <div class="input-group input-group-sm mb-3">
              <input type="url" class="form-control bg-dark text-light border-secondary" id="albumArtUrlInput" placeholder="https://…/cover.jpg">
              <button class="btn btn-outline-success" type="button" id="albumArtUrlApplyBtn">Apply</button>
            </div>
            <label for="albumArtFileInput" class="form-label small">…or upload a file</label>
            <div class="input-group input-group-sm">
              <input type="file" class="form-control bg-dark text-light border-secondary" id="albumArtFileInput" accept="image/*">
              <button class="btn btn-outline-success" type="button" id="albumArtUploadBtn">Upload</button>
            </div>
          </div>
          <div class="modal-footer border-secondary">
            <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Close</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(wrap);

    const modal = global.bootstrap ? new global.bootstrap.Modal(wrap) : null;
    const statusEl = wrap.querySelector('#albumArtStatus');
    const resultsEl = wrap.querySelector('#albumArtResults');

    const setStatus = (msg, isError) => {
      statusEl.textContent = msg || '';
      statusEl.className = 'small mb-2 ' + (isError ? 'text-danger' : 'text-muted');
    };

    /** Apply an image URL (from a search hit or the paste box). */
    const applyUrl = async (url, btn) => {
      if (!url) return;
      const run = async () => {
        const data = await global.api.postJson('/api/album/set-art', {
          artist, album, image_url: url,
        });
        if (!data || data.success !== true) {
          setStatus((data && data.error) || 'Could not set album art.', true);
          return;
        }
        notifySuccess('Album art updated.');
        if (modal) modal.hide();
      };
      if (btn && global.buttonState) {
        return global.buttonState.withBusy(btn, '', run)
          .catch((e) => setStatus('Error: ' + e.message, true));
      }
      return run().catch((e) => setStatus('Error: ' + e.message, true));
    };

    wrap.querySelector('#albumArtSearchBtn').addEventListener('click', async function () {
      const source = wrap.querySelector('#albumArtSourceSel').value;
      const btn = this;
      const run = async () => {
        setStatus('Searching…');
        resultsEl.innerHTML = '';
        const params = new URLSearchParams({ artist, album, source });
        const data = await global.api.getJson(`/api/album/search-art?${params.toString()}`);
        const images = (data && (data.images || data.results)) || [];
        if (!images.length) {
          setStatus(data && data.error ? data.error : 'No images found on that source.', !data);
          return;
        }
        setStatus(`${images.length} image(s) found — click one to apply.`);
        resultsEl.innerHTML = images.map((img, i) => {
          const url = typeof img === 'string' ? img : (img.url || img.image_url || '');
          const safe = esc(String(url));
          return `<div class="col-4 col-md-3">
            <button type="button" class="btn p-0 border-0 w-100 album-art-pick" data-url="${safe}" title="Use this image">
              <img src="${safe}" class="img-fluid rounded" style="aspect-ratio:1;object-fit:cover" alt="Album art candidate ${i + 1}">
            </button>
          </div>`;
        }).join('');
        resultsEl.querySelectorAll('.album-art-pick').forEach((b) => {
          b.addEventListener('click', () => applyUrl(b.dataset.url, b));
        });
      };
      if (global.buttonState) {
        return global.buttonState.withBusy(btn, 'Searching…', run)
          .catch((e) => setStatus('Error: ' + e.message, true));
      }
      return run().catch((e) => setStatus('Error: ' + e.message, true));
    });

    wrap.querySelector('#albumArtUrlApplyBtn').addEventListener('click', function () {
      applyUrl(wrap.querySelector('#albumArtUrlInput').value.trim(), this);
    });

    wrap.querySelector('#albumArtUploadBtn').addEventListener('click', async function () {
      const input = wrap.querySelector('#albumArtFileInput');
      const file = input.files && input.files[0];
      if (!file) {
        setStatus('Choose an image file first.', true);
        return;
      }
      const btn = this;
      const run = async () => {
        setStatus('Uploading…');
        const form = new FormData();
        form.append('artist', artist);
        form.append('album', album);
        form.append('image', file);
        // Multipart: must NOT go through postJson (which sets a JSON body).
        const response = await fetch('/api/album/upload-art', { method: 'POST', body: form });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || (data && data.success === false)) {
          setStatus((data && data.error) || `Upload failed (HTTP ${response.status}).`, true);
          return;
        }
        notifySuccess('Album art updated.');
        if (modal) modal.hide();
      };
      if (global.buttonState) {
        return global.buttonState.withBusy(btn, 'Uploading…', run)
          .catch((e) => setStatus('Error: ' + e.message, true));
      }
      return run().catch((e) => setStatus('Error: ' + e.message, true));
    });

    // Tear the markup down rather than leaving a hidden copy on the page, so a
    // later open starts clean and ids cannot go stale.
    wrap.addEventListener('hidden.bs.modal', () => wrap.remove());

    if (modal) modal.show();
  }

  global.openAlbumArtModal = openAlbumArtModal;

  /**
   * Align the tracklist: renumber the album's tracks from the current
   * MusicBrainz comparison so on-disk numbering matches the release.
   *
   * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY — the "Align" button threw
   * ReferenceError.
   *
   * Unlike the other three there is NO dedicated endpoint for this, and it
   * would be wrong to invent one that rewrites files: the album page ALREADY
   * stages per-track changes and applies them in ONE atomic save ("Save
   * Metadata"). So Align fills in the already-staged track numbers from the
   * comparison, then tells the user to save — the same contract as a lookup,
   * where nothing is written until the form is submitted.
   *
   * Requires a prior Compare (or Lookup) so there is a tracklist to align to.
   */
  async function alignTracklist() {
    const comparison = (comparisonData && comparisonData.comparison) || [];
    if (!comparison.length) {
      notifyError(
        'Nothing to align to — run "Lookup MBID" or "Compare with MusicBrainz" first.'
      );
      return;
    }

    // Only a track whose number ACTUALLY differs is worth writing; the rest
    // would be a no-op POST each.
    const needsNumber = comparison.filter(
      (c) => c
        && c.matched
        && c.library_track_id
        && c.mb_track_number != null
        && String(c.library_track_number ?? '') !== String(c.mb_track_number)
    );
    if (!needsNumber.length) {
      notifySuccess('Track numbers already match the MusicBrainz order.');
      return;
    }

    const confirmed = global.ui && global.ui.confirm
      ? await global.ui.confirm({
        title: 'Align tracklist',
        message: `Renumber ${needsNumber.length} track(s) to the MusicBrainz order?`,
        detail:
          'This WRITES the track number to the database and the audio file tags for ' +
          'each track, exactly like clicking Apply on each track-number suggestion.',
        tone: 'warning',
        confirmLabel: 'Align',
      })
      : window.confirm(`Renumber ${needsNumber.length} track(s) to the MusicBrainz order?`);
    if (!confirmed) return;

    const run = async () => {
      let applied = 0;
      let failed = 0;
      // Sequential: each call writes to the DB and rewrites the file's tags, so
      // firing them concurrently would hammer the disk and the tag writer.
      for (const comp of needsNumber) {
        try {
          const result = await global.api.postJson(
            `${API_V1}/tracks/${encodeURIComponent(String(comp.library_track_id))}/apply-mb-field`,
            { field: 'track_number', value: String(comp.mb_track_number) }
          );
          if (result && result.success) {
            applied += 1;
            // Keep the visible cell in step, the same way the per-row Apply does.
            document.querySelectorAll(
              `.track-number-display-${CSS.escape(String(comp.library_track_id))}`
            ).forEach((el) => { el.textContent = comp.mb_track_number; });
            // The stale number suggestion is now applied, so drop its row.
            document.querySelectorAll('.mb-update-row').forEach((row) => {
              if (row.dataset.mbField !== 'track_number') return;
              const rowComp = JSON.parse(row.dataset.mbComp || '{}');
              if (String(rowComp.library_track_id) === String(comp.library_track_id)) {
                row.remove();
              }
            });
          } else {
            failed += 1;
          }
        } catch (_error) {
          failed += 1;
        }
      }

      if (applied) {
        notifySuccess(
          `Aligned ${applied} track number(s)` + (failed ? `; ${failed} failed.` : '.')
        );
      } else {
        notifyError(`Could not align any tracks (${failed} failed).`);
      }
    };

    if (global.busyPopup) {
      return global.busyPopup.showAndRun('Aligning tracklist…', run);
    }
    return run();
  }

  global.alignTracklist = alignTracklist;

  /**
   * Apply a chosen MusicBrainz release to the Edit Album form.
   *
   * ⚠️ TWO BUGS FIXED HERE.
   *
   * 1. WRONG FIELD. The search modal returns RELEASE-GROUP results, so
   *    `selected.id` is a release-GROUP MBID. The old code passed it to
   *    applyAlbumMbid(), which wrote it into `#album_mbid` — the CONCRETE
   *    RELEASE field. Saving then stored a group id where a release id
   *    belongs, which is why a lookup could appear to "work" and still leave
   *    the album unmatched. The group id now goes to
   *    `#album_release_group_mbid` (its correct field) and the concrete
   *    release id is resolved separately.
   *
   * 2. METADATA WAS NOT IMPORTED. Only the id field was filled, so the user
   *    had to run compare/update separately to get anything else. The
   *    descriptive fields the release actually provides (edition title, years,
   *    country, cover, type) are now populated too.
   *
   * Nothing is written to the database or to any file here — this only fills
   * the form, so the user reviews the values and presses Save Metadata. That
   * matches how the rest of this page works and keeps a bad match harmless.
   */
  async function applyAlbumMatch(release) {
    if (!release || !release.id) {
      notifyError('No release selected.');
      endLookupBusy();
      return;
    }

    // Re-label for the second phase. Everything below (best-release probe +
    // metadata preview) is more network work the user should see acknowledged.
    if (global.busyPopup) global.busyPopup.update(lookupBusy, 'Resolving release details…');

    // The release-GROUP id belongs in the release-group field.
    setFieldValue('album_release_group_mbid', release.id);

    // The group's first release date is the ORIGINAL year (what `year`
    // stores), so a reissue still groups with its original pressing.
    const originalYear = yearOf(release.first_release_date);
    if (originalYear) setFieldValue('album_originalyear', originalYear);

    // The edition's own title, when MusicBrainz exposes one distinct from the
    // album name. Kept OUT of #album_title: that holds the album's main
    // identity, and silently renaming an album from a search hit is not
    // something a "match" should do behind the user's back.
    const editionTitle = String(release.release_title || release.title || '').trim();
    if (editionTitle && editionTitle.toLowerCase() !== pageAlbum().toLowerCase()) {
      setFieldValue('album_release_title', editionTitle);
    }

    // Category -> the form's type select, when the option exists.
    const category = String(release.category || release.primary_type || '').toLowerCase();
    if (category) setAlbumTypeIfPresent(category);

    if (release.cover_art_url) {
      const coverField = document.getElementById('cover_art_url');
      if (coverField) coverField.value = release.cover_art_url;
    }

    const tabBtn = document.querySelector('#albumPageTabs [data-bs-target="#tab-details"]');
    if (tabBtn && global.bootstrap) global.bootstrap.Tab.getOrCreateInstance(tabBtn).show();

    markDirty();
    notifySuccess('Release matched. Resolving the exact edition…');

    // Resolve the CONCRETE release inside the group so the release-id field,
    // the edition year and the country can be filled from real release data.
    // A failure here must not undo the match — the group id is already set and
    // the user can still save or pick a version manually.
    let resolvedReleaseId = '';
    try {
      const data = await global.api.postJson('/api/album/musicbrainz/best-release', {
        release_group_mbid: release.id,
        artist: pageArtist(),
        album: pageAlbum(),
      });

      const best = (data && data.best_release) || null;
      if (best && best.id) {
        resolvedReleaseId = best.id;
        setFieldValue('album_mbid', best.id);

        const releaseYear = yearOf(best.date);
        if (releaseYear) setFieldValue('release_year', releaseYear);

        if (best.country) setFieldValue('album_releasecountry', best.country);
        if (best.cover_art_url && !(release.cover_art_url)) {
          const coverField = document.getElementById('cover_art_url');
          if (coverField) coverField.value = best.cover_art_url;
        }
      }
    } catch (error) {
      // Non-fatal by design — see above.
      console.warn('Could not resolve the concrete release for this group', error);
    }

    // ── Full metadata preview ─────────────────────────────────────────────
    // Show EVERYTHING a metadata import would write — every album-level field
    // plus each per-track change — as an orange bar under the field it
    // affects. This is the whole point of the lookup: the user reviews one
    // complete proposal and saves it once.
    //
    // ⚠️ STILL NOTHING IS WRITTEN. The album values go into the form and the
    // per-track changes are staged into #staged_track_updates, which only the
    // form's own submit posts. A wrong release is undone by reloading.
    //
    // The concrete release id is preferred (it carries the edition's own
    // label/catalog/barcode/media), falling back to the release-group id the
    // server can resolve itself.
    let staged = null;
    if (global.albumMetadataReview) {
      try {
        staged = await global.albumMetadataReview.applyProposal(
          resolvedReleaseId || release.id
        );
      } catch (error) {
        console.warn('Could not build the metadata preview', error);
      }
    }

    if (staged) {
      const counts = staged.counts || {};
      notifySuccess(
        `Release matched. ${counts.album_changes || 0} album field(s) and ` +
        `${counts.tracks_changed || 0} track(s) have MusicBrainz updates to review — ` +
        'check the Edit Album tab and the orange bars, then click "Save Metadata".'
      );
    } else {
      notifySuccess(
        'Release matched and metadata filled in. Review the Edit Album tab, then click "Save Metadata".'
      );
    }
    endLookupBusy();
  }
  /** First four digits of a MusicBrainz date ("2014-11-24" → 2014). */
  function yearOf(value) {
    const text = String(value || '').trim();
    return text.length >= 4 && /^\d{4}/.test(text) ? text.slice(0, 4) : '';
  }

  /** Set a value on a form field by id, if it exists. */
  function setFieldValue(id, value) {
    const field = document.getElementById(id);
    if (!field) return false;
    field.value = value;
    field.style.transition = 'background-color 0.3s';
    field.style.backgroundColor = 'var(--accent-color)';
    setTimeout(() => { field.style.backgroundColor = ''; }, 500);
    return true;
  }

  /**
   * Select the matching option in #album_type when one exists.
   *
   * Only selects an EXISTING option: adding one for an arbitrary MusicBrainz
   * category would silently change what the form submits. "album+live" and
   * friends are matched on their parts so they still land on the Live option.
   */
  function setAlbumTypeIfPresent(category) {
    const select = document.getElementById('album_type');
    if (!select) return false;

    const wanted = String(category).toLowerCase();
    const options = Array.from(select.options);
    const exact = options.find((o) => String(o.value).toLowerCase() === wanted);
    const partial = exact || options.find((o) => {
      const value = String(o.value).toLowerCase();
      return value && (wanted.includes(value) || value.includes(wanted));
    });
    if (!partial) return false;

    select.value = partial.value;
    return true;
  }

  /**
   * Apply a chosen release MBID to the Edit Album form.
   *
   * Retained for callers that already have a concrete release MBID (the
   * release picker). The shared search modal returns release GROUPS, so its
   * callback goes through applyAlbumMatch() instead.
   */
  function applyAlbumMbid(mbid) {
    if (!mbid) {
      notifyError('No release selected.');
      return;
    }
    if (!setFieldValue('album_mbid', mbid)) return;

    markDirty();

    // Surface the Edit Album tab so the change is visible before saving.
    const tabBtn = document.querySelector('#albumPageTabs [data-bs-target="#tab-details"]');
    if (tabBtn && global.bootstrap) global.bootstrap.Tab.getOrCreateInstance(tabBtn).show();

    // Full metadata preview — staged only, written by the form's own submit.
    if (global.albumMetadataReview) {
      global.albumMetadataReview.applyProposal(mbid)
        .then((staged) => {
          if (staged) {
            const counts = staged.counts || {};
            notifySuccess(
              `MusicBrainz ID applied. ${counts.album_changes || 0} album field(s) and ` +
              `${counts.tracks_changed || 0} track(s) have updates to review — ` +
              'check the orange bars, then click "Save Metadata".'
            );
          } else {
            notifySuccess('MusicBrainz ID applied. Click "Save Metadata" to persist it.');
          }
        })
        .catch(() => {
          notifySuccess('MusicBrainz ID applied. Click "Save Metadata" to persist it.');
        });
      return;
    }

    notifySuccess('MusicBrainz ID applied. Click "Save Metadata" to persist it.');
  }

  // ── Compare with MusicBrainz ────────────────────────────────────────────
  //
  // Backed by the server's compare_musicbrainz_release(), which matches MB
  // tracklist entries to library rows (by disc+track number, falling back to
  // fuzzy title match) and reports per-track diff_fields (title,
  // track_number, disc_number, mbid — duration is reported but informational
  // only, since a file's real duration is not an editable metadata field).
  //
  // Field names below (mb_recording_mbid, mb_title, …) match the server's
  // response shape exactly — see musicbrainz_service.py.

  let comparisonData = null;

  function linkedReleaseMbid() {
    const group = document.getElementById('album_release_group_mbid');
    const release = document.getElementById('album_mbid');
    return (group && group.value.trim()) || (release && release.value.trim()) || '';
  }

  function bannerContainer() {
    const tableResponsive = document.querySelector('#album-tracks-section .table-responsive');
    return tableResponsive ? tableResponsive.parentElement : null;
  }

  /**
   * Create or update the comparison banner.
   * Single builder — `_displayMBComparison` previously re-implemented this
   * same "create if absent" block inline.
   */
  function showBanner(innerHtml, type, showDismiss) {
    let banner = document.getElementById('mb-compare-banner');
    if (!banner) {
      const container = bannerContainer();
      if (!container) return null;
      banner = document.createElement('div');
      banner.id = 'mb-compare-banner';
      container.insertBefore(
        banner,
        document.querySelector('#album-tracks-section .table-responsive')
      );
    }

    const dismiss = showDismiss
      ? '<button type="button" class="btn-close ms-auto" data-mb-dismiss aria-label="Dismiss"></button>'
      : '';
    banner.innerHTML =
      `<div class="alert alert-${esc(type)} d-flex align-items-center gap-2 mb-2 py-2 flex-wrap">` +
      `${innerHtml}${dismiss}</div>`;

    const dismissBtn = banner.querySelector('[data-mb-dismiss]');
    if (dismissBtn) dismissBtn.addEventListener('click', clearComparison);

    const updateAllBtn = banner.querySelector('[data-mb-update-all]');
    if (updateAllBtn) updateAllBtn.addEventListener('click', () => updateAllTracksFromMB());

    return banner;
  }

  function clearComparison() {
    ['.mb-update-row', '.mb-missing-row', '.mb-extra-row', '.mb-extra-badge-dynamic']
      .forEach((sel) => document.querySelectorAll(sel).forEach((el) => el.remove()));
    const banner = document.getElementById('mb-compare-banner');
    if (banner) banner.remove();
    comparisonData = null;
  }

  const FIELD_LABELS = {
    title: (c) =>
      `Title: <em>${esc(c.library_title || '')}</em> → <strong>${esc(c.mb_title || '')}</strong>`,
    track_number: (c) =>
      `Track #: ${esc(String(c.library_track_number ?? '—'))} → ${esc(String(c.mb_track_number))}`,
    disc_number: (c) =>
      `Disc: ${esc(String(c.library_disc_number ?? 1))} → ${esc(String(c.mb_disc_number))}`,
    mbid: (c) =>
      `MusicBrainz Recording ID: ${c.library_mbid
        ? '<em>' + esc(c.library_mbid) + '</em>'
        : '<em>missing</em>'} → <strong>added</strong>`,
    duration: (c) =>
      `Length: ${esc(String(c.library_duration ?? '—'))} → ${esc(String(c.mb_duration ?? '—'))} ` +
      '<span class="text-muted">(informational — not editable)</span>',
  };

  async function compareWithMusicBrainz() {
    const releaseMbid = linkedReleaseMbid();
    if (!releaseMbid) {
      notifyError('No MusicBrainz release linked yet. Use "Lookup on MusicBrainz" first, then Save Metadata.');
      return;
    }

    const btn = document.getElementById('albumCompareMbBtn');
    showBanner(
      '<span class="spinner-border spinner-border-sm me-2" role="status"></span>' +
      'Comparing tracks with MusicBrainz…',
      'info',
      false
    );

    const run = async () => {
      try {
        // Path-based, matching api_v1's
        // /albums/<path:artist>/<path:album>/… convention.
        const data = await global.api.postJson(
          `${API_V1}/albums/${encodeURIComponent(pageArtist())}` +
          `/${encodeURIComponent(pageAlbum())}/musicbrainz-compare`,
          { release_mbid: releaseMbid }
        );

        if (!data.success) {
          showBanner(
            '<i class="bi bi-exclamation-triangle-fill"></i>' +
            `<div>Could not compare tracks: ${esc(data.error || 'Unknown error')}</div>`,
            'warning',
            true
          );
          return;
        }

        comparisonData = data;
        displayComparison(data);
      } catch (error) {
        showBanner(
          '<i class="bi bi-x-circle-fill"></i>' +
          `<div>Network error while comparing tracks: ${esc(error.message)}</div>`,
          'danger',
          true
        );
      }
    };

    return btn && global.buttonState
      ? global.buttonState.withBusy(btn, 'Comparing…', run)
      : run();
  }

  function displayComparison(data) {
    ['.mb-update-row', '.mb-missing-row', '.mb-extra-row', '.mb-extra-badge-dynamic']
      .forEach((sel) => document.querySelectorAll(sel).forEach((el) => el.remove()));

    const needsUpdate = data.comparison.filter((c) => c.needs_update && c.library_track_id);
    const missing = data.comparison.filter((c) => !c.matched);
    const extra = data.extra_tracks || [];

    if (!needsUpdate.length && !missing.length && !extra.length) {
      showBanner(
        '<i class="bi bi-check-circle-fill text-success"></i>' +
        `<div>All ${esc(String(data.total_tracks))} tracks match MusicBrainz metadata — no updates needed.</div>`,
        'success',
        true
      );
      return;
    }

    if (missing.length) injectMissingRows(missing, data);
    if (extra.length) markExtraTracks(extra);

    const plural = (n, one, many) => (n === 1 ? one : many);

    if (!needsUpdate.length) {
      let message;
      if (missing.length && extra.length) {
        message = `<strong>${missing.length}</strong> ${plural(missing.length, 'track is', 'tracks are')} ` +
          `missing from the library and <strong>${extra.length}</strong> ` +
          `${plural(extra.length, 'track is', 'tracks are')} not in the MusicBrainz tracklist.`;
      } else if (missing.length) {
        message = `<strong>${missing.length} of ${esc(String(data.total_tracks))} tracks</strong> ` +
          'are missing from the library.';
      } else {
        message = `<strong>${extra.length}</strong> ` +
          `${plural(extra.length, 'track in the library is', 'tracks in the library are')} ` +
          'not found in the MusicBrainz tracklist for this release.';
      }
      showBanner(`<i class="bi bi-exclamation-triangle-fill"></i><div>${message}</div>`, 'warning', true);
      return;
    }

    let message = `<strong>${needsUpdate.length} of ${esc(String(data.total_tracks))} tracks</strong> ` +
      'have metadata that can be updated from MusicBrainz.';
    if (missing.length) {
      message += ` <strong>${missing.length}</strong> ${plural(missing.length, 'track is', 'tracks are')} ` +
        'missing from the library.';
    }
    if (extra.length) {
      message += ` <strong>${extra.length}</strong> ${plural(extra.length, 'track is', 'tracks are')} ` +
        'not in the MusicBrainz tracklist.';
    }

    showBanner(
      '<i class="bi bi-exclamation-triangle-fill"></i>' +
      `<div>${message}</div>` +
      '<button class="btn btn-warning btn-sm ms-auto" data-mb-update-all>' +
      '<i class="bi bi-arrow-repeat"></i> Update All</button>',
      'warning',
      true
    );

    injectUpdateRows(data);
  }

  function injectUpdateRows(data) {
    const tbody = document.getElementById('albumTracksTbody');
    if (!tbody) return;

    data.comparison.forEach((trackComp) => {
      if (!trackComp.needs_update || !trackComp.library_track_id) return;
      const trackId = String(trackComp.library_track_id);
      const trackRow = tbody.querySelector(`tr[data-track-id="${CSS.escape(trackId)}"]`);
      if (!trackRow) return;

      let insertAfter = trackRow;
      let sibling = insertAfter.nextElementSibling;
      while (sibling && (sibling.classList.contains('mb-update-row')
        || sibling.classList.contains('mb-missing-row'))) {
        insertAfter = sibling;
        sibling = sibling.nextElementSibling;
      }

      trackComp.diff_fields.forEach((field) => {
        if (!FIELD_LABELS[field]) return;

        const row = document.createElement('tr');
        row.className = 'mb-update-row';
        row.dataset.trackId = trackId;
        row.dataset.mbComp = JSON.stringify(trackComp);
        row.dataset.mbField = field;
        row.innerHTML = `
          <td colspan="6" style="padding:0.3rem 0.75rem;border-top:none;">
            <div class="d-flex align-items-center gap-2 flex-wrap rounded px-2 py-1 mb-suggest-row">
              <small class="text-warning-emphasis">
                <i class="bi bi-lightning-fill me-1"></i><strong>MusicBrainz:</strong>
              </small>
              <small class="text-muted">${FIELD_LABELS[field](trackComp)}</small>
              <button class="btn btn-warning btn-sm ms-auto py-0 px-2 mb-apply-field"
                      style="font-size:0.75rem;white-space:nowrap;">
                <i class="bi bi-check-lg"></i> Apply
              </button>
              <button class="btn btn-outline-secondary btn-sm py-0 px-2 mb-ignore-field"
                      style="font-size:0.75rem;white-space:nowrap;">
                <i class="bi bi-x-lg"></i> Ignore
              </button>
            </div>
          </td>`;

        row.querySelector('.mb-apply-field')
          .addEventListener('click', function () { applyMBField(this); });
        row.querySelector('.mb-ignore-field')
          .addEventListener('click', function () { ignoreMBField(this); });

        insertAfter.insertAdjacentElement('afterend', row);
        insertAfter = row;
      });
    });
  }

  function buildMissingRow(trackComp, data) {
    const row = document.createElement('tr');
    row.className = 'text-muted missing-track-row mb-missing-row';
    row.style.opacity = '0.6';

    const trackNum = esc(String(trackComp.mb_track_number || ''));
    row.innerHTML = `
      <td></td>
      <td class="fst-italic">${trackNum || '?'}</td>
      <td colspan="3" class="fst-italic">
        ${esc(trackComp.mb_title || '')}
        <span class="badge bg-warning text-dark ms-2" style="font-size:0.65rem;">Missing</span>
      </td>
      <td class="text-end">
        <div class="btn-group btn-group-sm">
          <button class="btn btn-outline-success py-0 px-2 mb-queue-missing" title="Add to download queue">
            <i class="bi bi-download"></i>
          </button>
          <button class="btn btn-outline-primary py-0 px-2 mb-match-existing"
                  title="Match to an existing track in the library">
            <i class="bi bi-link-45deg"></i>
          </button>
          <button class="btn btn-outline-secondary py-0 px-2 mb-hide-missing"
                  title="Hide this track from the missing list">
            <i class="bi bi-x-lg"></i>
          </button>
        </div>
      </td>`;

    // Payload held in a closure rather than serialised into ten data-*
    // attributes — no escaping needed and no attribute-size limits.
    const payload = {
      artist: pageArtist(),
      album_artist: pageArtist(),
      album: pageAlbum(),
      title: trackComp.mb_title || '',
      track_number: trackComp.mb_track_number || null,
      disc_number: trackComp.mb_disc_number || 1,
      year: data.mb_year || null,
      release_id: data.release_mbid || null,
      recording_mbid: trackComp.mb_recording_mbid || null,
      duration: trackComp.mb_duration != null ? trackComp.mb_duration : null,
    };

    row.querySelector('.mb-queue-missing').addEventListener('click', function () {
      queueMissingTrack(payload, this);
    });
    row.querySelector('.mb-match-existing').addEventListener('click', function () {
      openMatchModal({
        title: payload.title,
        track_number: payload.track_number,
        disc_number: payload.disc_number,
        recording_mbid: payload.recording_mbid,
      });
    });
    row.querySelector('.mb-hide-missing').addEventListener('click', function () {
      const tr = this.closest('tr');
      if (tr) tr.remove();
    });

    return row;
  }

  function injectMissingRows(missingTracks, data) {
    const tbody = document.getElementById('albumTracksTbody');
    if (!tbody) return;

    missingTracks.forEach((trackComp) => {
      const index = data.comparison.indexOf(trackComp);
      let insertAfterRow = null;

      for (let i = index - 1; i >= 0; i--) {
        const prev = data.comparison[i];
        if (prev.matched && prev.library_track_id) {
          const candidate = tbody.querySelector(
            `tr[data-track-id="${CSS.escape(String(prev.library_track_id))}"]`
          );
          if (candidate) { insertAfterRow = candidate; break; }
        }
      }

      const row = buildMissingRow(trackComp, data);
      if (insertAfterRow) {
        let next = insertAfterRow.nextElementSibling;
        while (next && (next.classList.contains('mb-update-row')
          || next.classList.contains('mb-missing-row'))) {
          insertAfterRow = next;
          next = next.nextElementSibling;
        }
        insertAfterRow.insertAdjacentElement('afterend', row);
      } else {
        const firstRow = tbody.querySelector('tr[data-track-id]');
        if (firstRow) firstRow.insertAdjacentElement('beforebegin', row);
        else tbody.appendChild(row);
      }
    });
  }

  function markExtraTracks(extraTracks) {
    const tbody = document.getElementById('albumTracksTbody');
    if (!tbody) return;

    extraTracks.forEach((extra) => {
      const trackId = String(extra.library_track_id);
      const trackRow = tbody.querySelector(`tr[data-track-id="${CSS.escape(trackId)}"]`);
      if (!trackRow) return;

      if (!trackRow.querySelector('.mb-extra-badge')) {
        const titleLink = trackRow.querySelector('.track-title-display-' + CSS.escape(trackId));
        if (titleLink) {
          const badge = document.createElement('span');
          badge.className = 'badge bg-secondary ms-1 mb-extra-badge mb-extra-badge-dynamic';
          badge.title = 'This track was not found in the MusicBrainz tracklist for this release';
          badge.textContent = 'Extra';
          titleLink.insertAdjacentElement('afterend', badge);
        }
      }

      const subRow = document.createElement('tr');
      subRow.className = 'mb-extra-row';
      subRow.innerHTML = `
        <td colspan="6" style="padding:0.3rem 0.75rem;border-top:none;">
          <div class="d-flex align-items-center gap-2 flex-wrap rounded px-2 py-1 mb-extra-note">
            <small class="text-secondary">
              <i class="bi bi-question-circle-fill me-1"></i><strong>MusicBrainz:</strong>
            </small>
            <small class="text-muted">Not found in the MusicBrainz tracklist for this release</small>
          </div>
        </td>`;

      let insertAfter = trackRow;
      let sibling = insertAfter.nextElementSibling;
      while (sibling && (sibling.classList.contains('mb-update-row')
        || sibling.classList.contains('mb-missing-row')
        || sibling.classList.contains('mb-extra-row'))) {
        insertAfter = sibling;
        sibling = sibling.nextElementSibling;
      }
      insertAfter.insertAdjacentElement('afterend', subRow);
    });
  }

  /**
   * Apply (or, for the informational "duration" field, dismiss) a single
   * diff field for one track. Shared by the per-row Apply button and by
   * Update All.
   *
   * @returns {Promise<boolean>} true when the row was handled
   */
  async function applyOneField(updateRow) {
    const trackComp = JSON.parse(updateRow.dataset.mbComp || '{}');
    const trackId = String(trackComp.library_track_id || updateRow.dataset.trackId || '');
    const field = updateRow.dataset.mbField || '';

    // Duration is intrinsic to the audio file, not an editable metadata
    // field — there is nothing to write, so just dismiss the row.
    if (field === 'duration') {
      updateRow.remove();
      return true;
    }

    let value = null;
    if (field === 'title') value = trackComp.mb_title;
    else if (field === 'track_number') value = String(trackComp.mb_track_number);
    else if (field === 'disc_number') value = String(trackComp.mb_disc_number);
    else if (field === 'mbid') value = trackComp.mb_recording_mbid;

    if (value === null) {
      updateRow.remove();
      return true;
    }

    try {
      const result = await global.api.postJson(
        `${API_V1}/tracks/${encodeURIComponent(trackId)}/apply-mb-field`,
        { field, value }
      );
      if (!result.success) return false;

      updateRow.remove();

      if (field === 'title') {
        document.querySelectorAll(`.track-title-display-${CSS.escape(trackId)}`)
          .forEach((el) => { el.textContent = trackComp.mb_title; });
      } else if (field === 'track_number') {
        document.querySelectorAll(`.track-number-display-${CSS.escape(trackId)}`)
          .forEach((el) => { el.textContent = trackComp.mb_track_number; });
      }
      return true;
    } catch (_error) {
      return false;
    }
  }

  function applyMBField(btn) {
    const updateRow = btn.closest('.mb-update-row');
    if (!updateRow) return;

    return global.buttonState.withBusy(btn, '', async () => {
      const ok = await applyOneField(updateRow);
      if (!ok) {
        notifyError('Failed to apply MusicBrainz update.');
        return;
      }
      if (!document.querySelector('.mb-update-row')) {
        showBanner(
          '<i class="bi bi-check-circle-fill text-success"></i>' +
          '<div>All MusicBrainz suggestions have been applied.</div>',
          'success',
          true
        );
      }
    });
  }

  function ignoreMBField(btn) {
    const updateRow = btn.closest('.mb-update-row');
    if (!updateRow) return;

    const trackComp = JSON.parse(updateRow.dataset.mbComp || '{}');
    const trackId = String(trackComp.library_track_id || updateRow.dataset.trackId || '');
    const field = updateRow.dataset.mbField || '';

    return global.buttonState.withBusy(btn, '', async () => {
      try {
        // Persists into tracks.mb_ignored_fields — the SAME column
        // compare_musicbrainz_release() reads to suppress diff_fields, so an
        // ignored field stays ignored on future comparisons too.
        const result = await global.api.postJson(
          `${API_V1}/tracks/${encodeURIComponent(trackId)}/ignore-mb-field`,
          { field }
        );
        if (!result.success) {
          notifyError('Failed to ignore field: ' + (result.error || 'Unknown error'));
          return;
        }

        updateRow.remove();
        if (!document.querySelector('.mb-update-row')) {
          showBanner(
            '<i class="bi bi-check-circle-fill text-success"></i>' +
            '<div>All MusicBrainz suggestions have been handled.</div>',
            'success',
            true
          );
        }
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  async function updateAllTracksFromMB() {
    const rows = Array.from(document.querySelectorAll('.mb-update-row'));
    if (!rows.length) return;

    const accepted = await confirmFn({
      title: 'Apply all suggestions',
      message: `Apply ${rows.length} MusicBrainz suggestion${rows.length === 1 ? '' : 's'}?`,
      tone: 'primary',
      confirmLabel: 'Apply all',
    });
    if (!accepted) return;

    let applied = 0;
    let failed = 0;
    for (const row of rows) {
      // The row may already have been removed by a preceding same-track
      // apply (Update All can process several fields for one track).
      if (!row.isConnected) continue;
      if (await applyOneField(row)) applied += 1;
      else failed += 1;
    }

    const type = failed > 0 ? 'warning' : 'success';
    const icon = failed > 0 ? 'exclamation-triangle-fill' : 'check-circle-fill';
    const message = `Applied ${applied} update${applied === 1 ? '' : 's'}` +
      (failed > 0 ? `, ${failed} failed.` : ' successfully.');
    showBanner(`<i class="bi bi-${icon}"></i><div>${esc(message)}</div>`, type, true);
  }

  // ── Missing tracks: queue, match, hide ──────────────────────────────────

  function queueMissingTrack(payload, btn) {
    const body = Object.assign({}, payload, {
      release_id: payload.release_id || linkedReleaseMbid() || null,
      release_mbid: payload.release_id || linkedReleaseMbid() || null,
      release_source: (payload.release_id || linkedReleaseMbid()) ? 'musicbrainz' : null,
      duration: payload.duration != null ? parseInt(payload.duration, 10) : null,
      source: 'soulseek',
    });

    return global.buttonState.withBusy(btn, '', async () => {
      try {
        const data = await global.api.postJson('/api/queue/add', body);
        if (!data.success) {
          notifyError('Failed to queue track: ' + (data.error || 'Unknown error'));
          return;
        }
      // ⚠️ A dedupe is NOT an insert. ``success: true`` with
      // ``already_queued: true`` means the request was handled but NO row was
      // added — marking the button done for it is the same false success that
      // made newly added tracks look like they had vanished.
      if (data.already_queued) {
        // The row is real, just pre-existing — the ordinary case. Say so.
        global.buttonState.setDone(btn, {
          title: data.message || 'Already in the download queue',
          fromClass: 'btn-outline-success',
          icon: 'bi-info-circle',
        });
        // ⚠️ But when the blocker is a status the queue page cannot render, the
        // user has NO way to see or clear it from here — so that one must be
        // surfaced loudly rather than shown as a quiet tick.
        if (data.displayable === false) {
          notifyError(data.message || 'Already in the queue, but that row is not listed on the queue page.');
        }
        return;
      }
      global.buttonState.setDone(btn, {
        title: 'Added to download queue',
        fromClass: 'btn-outline-success',
      });
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  });
  }

  // ── Match a missing MB track to an existing library track ───────────────
  //
  // Candidates are the CURRENT comparison's extra_tracks — library rows the
  // comparison could not match to anything in the MB tracklist. That avoids
  // needing a separate "list all tracks" endpoint: the exact set of orphaned
  // local tracks is already in `comparisonData` from the last Compare run.

  let matchTarget = null;

  function openMatchModal(mbTrack) {
    matchTarget = mbTrack;

    const titleEl = document.getElementById('albumMatchMbTitle');
    const numEl = document.getElementById('albumMatchMbTrackNum');
    if (titleEl) titleEl.textContent = mbTrack.title;
    if (numEl) numEl.textContent = mbTrack.track_number || '?';

    const tbody = document.getElementById('albumMatchCandidatesTbody');
    if (!tbody) return;

    const candidates = (comparisonData && comparisonData.extra_tracks) || [];
    if (!candidates.length) {
      tbody.innerHTML =
        '<tr><td colspan="3" class="text-center text-muted">' +
        'No unmatched library tracks available to match against. ' +
        'Run Compare with MusicBrainz again if you expect one here.</td></tr>';
    } else {
      // data-* rather than an inline onclick carrying escapeJsString output.
      tbody.innerHTML = candidates.map((track) => `
        <tr>
          <td class="text-muted">${esc(String(track.library_track_number || '—'))}</td>
          <td>${esc(track.library_title || '—')}</td>
          <td class="text-center">
            <button class="btn btn-sm btn-primary album-match-btn"
                    data-track-id="${esc(String(track.library_track_id))}">
              <i class="bi bi-check-lg"></i> Match
            </button>
          </td>
        </tr>`).join('');

      tbody.querySelectorAll('.album-match-btn').forEach((btn) => {
        btn.addEventListener('click', function () {
          doMatchTrack(this.dataset.trackId, this);
        });
      });
    }

    const modalEl = document.getElementById('albumMatchTrackModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  async function doMatchTrack(trackId, btn) {
    if (!matchTarget) return;

    const accepted = await confirmFn({
      title: 'Apply MusicBrainz track',
      message: `Apply "${matchTarget.title}" (track ${matchTarget.track_number || '?'}) to this track?`,
      detail: 'This updates its title, track number and MusicBrainz recording ID.',
      tone: 'primary',
      confirmLabel: 'Apply',
    });
    if (!accepted) return;

    const fields = [
      ['title', matchTarget.title],
      ['track_number', matchTarget.track_number],
      ['disc_number', matchTarget.disc_number],
      ['mbid', matchTarget.recording_mbid],
    ].filter(([, value]) => value !== undefined && value !== null && value !== '');

    const run = async () => {
      let failed = false;
      for (const [field, value] of fields) {
        try {
          const result = await global.api.postJson(
            `${API_V1}/tracks/${encodeURIComponent(trackId)}/apply-mb-field`,
            { field, value: String(value) }
          );
          if (!result.success) failed = true;
        } catch (_error) {
          failed = true;
        }
      }

      const modalEl = document.getElementById('albumMatchTrackModal');
      if (modalEl && global.modal) global.modal.hide(modalEl);

      if (failed) {
        notifyError('Some fields could not be applied. Reloading to show the current state.');
      }
      setTimeout(() => global.location.reload(), failed ? 1500 : 400);
    };

    return btn && global.buttonState
      ? global.buttonState.withBusy(btn, '', run)
      : run();
  }

  // ── Bulk track selection + delete ───────────────────────────────────────

  function toggleSelectAll(checkbox) {
    document.querySelectorAll('.track-checkbox').forEach((cb) => { cb.checked = checkbox.checked; });
    updateBulkActionsUI();
  }

  function selectAllTracks() {
    document.querySelectorAll('.track-checkbox').forEach((cb) => { cb.checked = true; });
    const selectAll = document.getElementById('selectAllTracksCheckbox');
    if (selectAll) selectAll.checked = true;
    updateBulkActionsUI();
  }

  function clearAllTracks() {
    document.querySelectorAll('.track-checkbox').forEach((cb) => { cb.checked = false; });
    const selectAll = document.getElementById('selectAllTracksCheckbox');
    if (selectAll) selectAll.checked = false;
    updateBulkActionsUI();
  }

  function updateBulkActionsUI() {
    const checked = document.querySelectorAll('.track-checkbox:checked');
    const toolbar = document.getElementById('bulkActionsToolbar');
    const countEl = document.getElementById('selectedCount');
    if (!toolbar) return;

    if (checked.length > 0) {
      toolbar.classList.remove('d-none');
      toolbar.classList.add('d-flex');
      if (countEl) countEl.textContent = String(checked.length);
    } else {
      toolbar.classList.add('d-none');
      toolbar.classList.remove('d-flex');
    }

    // Keep the header checkbox in step with the rows.
    const selectAll = document.getElementById('selectAllTracksCheckbox');
    if (selectAll) {
      const all = document.querySelectorAll('.track-checkbox');
      selectAll.checked = all.length > 0 && checked.length === all.length;
      selectAll.indeterminate = checked.length > 0 && checked.length < all.length;
    }
  }

  // Delegated so per-row checkboxes update the toolbar without each needing
  // an inline onchange — and so rows added after load are covered too.
  document.addEventListener('change', function (event) {
    if (event.target.classList && event.target.classList.contains('track-checkbox')) {
      updateBulkActionsUI();
    }
  });

  function selectedTrackIds() {
    return Array.from(document.querySelectorAll('.track-checkbox:checked'))
      .map((cb) => cb.dataset.trackId);
  }

  function confirmBulkDeleteTracks() {
    const ids = selectedTrackIds();
    if (!ids.length) {
      notifyError('Please select at least one track.');
      return;
    }
    const countEl = document.getElementById('deleteTrackCount');
    if (countEl) countEl.textContent = String(ids.length);
    const modalEl = document.getElementById('bulkDeleteModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  function deleteDatabaseOnly() {
    const modalEl = document.getElementById('bulkDeleteModal');
    if (modalEl && global.modal) global.modal.hide(modalEl);
    performBulkDelete(false);
  }

  async function deleteWithFiles() {
    const modalEl = document.getElementById('bulkDeleteModal');
    if (modalEl && global.modal) global.modal.hide(modalEl);

    const ids = selectedTrackIds();
    if (!ids.length) return;

    // Typed confirmation: this permanently deletes audio from disk, and a
    // one-click confirm is not proportionate to that.
    const accepted = (global.ui && global.ui.confirmTyped)
      ? await global.ui.confirmTyped({
          title: 'Permanently delete files',
          message: `This will PERMANENTLY delete ${ids.length} file${ids.length === 1 ? '' : 's'} from disk.`,
          phrase: 'DELETE',
          confirmLabel: 'Delete files',
        })
      : global.confirm(`Permanently delete ${ids.length} file(s) from disk? This cannot be undone.`);

    if (!accepted) return;
    performBulkDelete(true);
  }

  async function performBulkDelete(deleteFiles) {
    const ids = selectedTrackIds();
    if (!ids.length) return;

    try {
      const data = await global.api.postJson(
        `${API_V1}/albums/${encodeURIComponent(pageArtist())}` +
        `/${encodeURIComponent(pageAlbum())}/bulk-delete`,
        { track_ids: ids, delete_files: deleteFiles }
      );

      if (!data.success) {
        notifyError('Error: ' + (data.error || 'Failed to delete tracks'));
        return;
      }
      notifySuccess(
        `Deleted ${data.deleted_count} track(s) ` +
        (deleteFiles ? 'from files and database' : 'from database')
      );
      setTimeout(() => global.location.reload(), 800);
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  // ── Similar artists ─────────────────────────────────────────────────────
  //
  // PORTED from album_detail.html's own inline <script> — this page ALREADY
  // had a working, richer implementation (in-collection vs not, thumbnail
  // grid, per-artist collection check) directly in the template. An earlier
  // pass here added a second, simpler implementation (a flat badge list)
  // without realising the template had its own — both attached a
  // DOMContentLoaded listener and both wrote into the SAME container, so the
  // one whose fetch resolved last silently won on every page load. That
  // duplicate has been removed from album_detail.html; this replaces it
  // with the template's original (better) version, fixed:
  //   - its own local `escapeHtml` (createTextNode + innerHTML, which does
  //     NOT escape quotes) is gone; the shared utils/dom.js escapeHtml is
  //     used via the module's `esc()` helper instead
  //   - onclick="..." strings built with manual `.replace(/'/g, "\\\\'")`
  //     escaping are replaced with addEventListener + closures
  //   - the artwork onerror fallback is a delegated listener instead of an
  //     inline attribute with its own separate hand-escaped data URI

  const SIMILAR_IMG_PLACEHOLDER =
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E" +
    "%3Crect fill='%232a2a2a' width='200' height='200'/%3E%3Ctext x='50%25' y='50%25' " +
    "text-anchor='middle' dy='.3em' fill='%23666' font-size='24'%3E%3Ctspan x='50%25' dy='-10'" +
    "%3E%26%23128100%3B%3C/tspan%3E%3C/text%3E%3C/svg%3E";

  function buildSimilarArtistCard(artist, inCollection) {
    const col = document.createElement('div');
    col.className = 'col-4 col-sm-3 col-md-4 col-lg-3 col-xl-2';

    const card = document.createElement('div');
    card.className = 'card h-100 bg-dark border-secondary overflow-hidden shadow-sm';
    card.style.cssText = 'cursor:pointer;transition:transform 0.2s;';
    card.addEventListener('mouseover', () => { card.style.transform = 'scale(1.05)'; });
    card.addEventListener('mouseout', () => { card.style.transform = 'scale(1)'; });
    card.addEventListener('click', () => {
      if (inCollection) {
        global.location.href = '/artist/' + encodeURIComponent(artist.name);
      } else if (typeof global.openGlobalMbSearch === 'function') {
        global.openGlobalMbSearch(artist.name, '');
      } else {
        notifyError('MusicBrainz search is unavailable on this page.');
      }
    });

    const img = document.createElement('img');
    img.className = 'card-img-top';
    img.style.cssText = 'aspect-ratio:1;object-fit:cover;';
    img.src = `/api/artist/${encodeURIComponent(artist.name)}/image`;
    img.addEventListener('error', function handle() {
      this.removeEventListener('error', handle);
      this.src = SIMILAR_IMG_PLACEHOLDER;
    });

    const body = document.createElement('div');
    body.className = 'card-body p-2 text-center d-flex align-items-center justify-content-center';
    body.style.cssText = 'background:rgba(0,0,0,0.8);min-height:40px;';
    const label = document.createElement('div');
    label.className = 'extra-small fw-bold text-light';
    label.style.cssText =
      'display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;' +
      'overflow:hidden;text-overflow:ellipsis;line-height:1.2;';
    label.title = artist.name;
    label.textContent = artist.name;
    body.appendChild(label);

    card.appendChild(img);
    card.appendChild(body);
    col.appendChild(card);
    return col;
  }

  function renderSimilarArtistGrid(artists, inCollection) {
    const row = document.createElement('div');
    row.className = 'row g-2';
    artists.forEach((a) => row.appendChild(buildSimilarArtistCard(a, inCollection)));
    return row;
  }

  async function loadSimilarArtists() {
    const container = document.getElementById('albumSimilarArtistsContainer');
    if (!container) return;

    const artist = pageArtist();
    if (!artist) {
      container.innerHTML = '<div class="text-center py-3 text-muted small">No artist context.</div>';
      return;
    }

    let data;
    try {
      data = await global.api.getJson(`/api/artist/${encodeURIComponent(artist)}/similar`);
    } catch (_error) {
      container.innerHTML =
        '<div class="text-danger small py-3 text-center">Failed to load similar artists.</div>';
      return;
    }

    if (!data.similar_artists) {
      container.innerHTML =
        '<div class="text-center py-3 text-muted small">No similar artists data available.</div>';
      return;
    }

    // Two API response shapes are handled: a pre-split in_collection/missing
    // pair (preferred — no extra requests needed), or the raw per-source
    // lastfm/listenbrainz arrays, which require a collection check per
    // artist below. Each entry is tagged with its origin bucket AT MERGE
    // TIME, before normalising/deduping — checking membership by re-scanning
    // the original arrays afterwards does not work reliably, since string
    // entries get wrapped into new {name} objects that no longer match by
    // reference or by value against the source array.
    const hasPreSplitShape = !!(data.similar_artists.in_collection || data.similar_artists.missing);
    let rawList;
    if (hasPreSplitShape) {
      rawList = [
        ...(data.similar_artists.in_collection || []).map((a) => ({ entry: a, tagged: true })),
        ...(data.similar_artists.missing || []).map((a) => ({ entry: a, tagged: false })),
      ];
    } else {
      rawList = [
        ...(data.similar_artists.lastfm || []),
        ...(data.similar_artists.listenbrainz || []),
      ].map((a) => ({ entry: a, tagged: null })); // null = not yet known
    }

    const normalized = rawList.map(({ entry, tagged }) => ({
      artist: typeof entry === 'string' ? { name: entry } : entry,
      tagged,
    }));

    const uniqueArtists = normalized
      .filter((v, i, arr) => arr.findIndex((t) => t.artist.name === v.artist.name) === i)
      .slice(0, 12);

    if (!uniqueArtists.length) {
      container.innerHTML =
        '<div class="text-center py-3 text-muted small">No similar artists data available.</div>';
      return;
    }

    const inCollection = [];
    const missing = [];

    if (hasPreSplitShape) {
      uniqueArtists.forEach(({ artist, tagged }) => (tagged ? inCollection : missing).push(artist));
    } else {
      // Each check runs in parallel and defaults to "missing" on failure
      // rather than blocking the whole render on one bad request.
      await Promise.all(uniqueArtists.map(async ({ artist }) => {
        try {
          const searchData = await global.api.getJson(
            `/api/search/unified?q=${encodeURIComponent(artist.name)}`
          );
          const match = (searchData.artists || [])
            .find((x) => x.name.toLowerCase() === artist.name.toLowerCase());
          (match ? inCollection : missing).push(artist);
        } catch (_e) {
          missing.push(artist);
        }
      }));
    }

    container.innerHTML = '';

    if (inCollection.length) {
      const heading = document.createElement('h6');
      heading.className = 'text-success small fw-bold text-uppercase mb-3';
      heading.innerHTML = '<i class="bi bi-collection-play me-1"></i> In Collection';
      container.appendChild(heading);
      container.appendChild(renderSimilarArtistGrid(inCollection, true));
      if (missing.length) {
        const hr = document.createElement('hr');
        hr.className = 'border-secondary my-4';
        container.appendChild(hr);
      }
    }

    if (missing.length) {
      const heading = document.createElement('h6');
      heading.className = 'text-info small fw-bold text-uppercase mb-3';
      heading.innerHTML = '<i class="bi bi-cloud-download me-1"></i> Not In Collection';
      container.appendChild(heading);
      container.appendChild(renderSimilarArtistGrid(missing, false));
    }
  }

  document.addEventListener('DOMContentLoaded', loadSimilarArtists);

  // ── Public API ──────────────────────────────────────────────────────────

  global.albumDetail = {
    playAlbum,
    playTrackFromAlbum,
    openEditTrackFromAlbum,
    deleteTrack,
    populateMajorityArtist,
    addAlbumGenre,
    stageRemoveAlbumGenre,
    goToAlbumGenres,
    fetchGenreRecommendations,
    addRecommendedGenre,
    applySelectedAlbumSourceTags,
    openAlbumLookupModal,
    applyAlbumMbid,
    compareWithMusicBrainz,
    clearMBComparison: clearComparison,
    updateAllTracksFromMB,
    toggleSelectAll,
    selectAllTracks,
    clearAllTracks,
    updateBulkActionsUI,
    confirmBulkDeleteTracks,
    deleteDatabaseOnly,
    deleteWithFiles,
  };

  // Legacy globals for the inline onclick="" handlers in album_detail.html.
  global.playAlbum = playAlbum;
  global.playTrackFromAlbum = playTrackFromAlbum;
  global.openEditTrackFromAlbum = openEditTrackFromAlbum;
  global.deleteTrack = deleteTrack;
  global.populateMajorityArtist = populateMajorityArtist;
  global.addAlbumGenre = addAlbumGenre;
  global.stageRemoveAlbumGenre = stageRemoveAlbumGenre;
  global.goToAlbumGenres = goToAlbumGenres;
  global.fetchGenreRecommendations = fetchGenreRecommendations;
  global.addRecommendedGenre = addRecommendedGenre;
  global.applySelectedAlbumSourceTags = applySelectedAlbumSourceTags;
  global.openAlbumLookupModal = openAlbumLookupModal;
  global.applyAlbumMbid = applyAlbumMbid;
  global.compareWithMusicBrainz = compareWithMusicBrainz;
  global.clearMBComparison = clearComparison;
  global.applyMBField = applyMBField;
  global.ignoreMBField = ignoreMBField;
  global.updateAllTracksFromMB = updateAllTracksFromMB;
  global.toggleSelectAll = toggleSelectAll;
  global.selectAllTracks = selectAllTracks;
  global.clearAllTracks = clearAllTracks;
  global.updateBulkActionsUI = updateBulkActionsUI;
  global.confirmBulkDeleteTracks = confirmBulkDeleteTracks;
  global.deleteDatabaseOnly = deleteDatabaseOnly;
  global.deleteWithFiles = deleteWithFiles;
  global.saveComprehensiveEditedTrack = saveComprehensiveEditedTrack;
  global.addEditTrackGenre = addEditTrackGenre;
  // Wired to #simpleEditTrackModal's Save button in
  // components/modals/_track_edit.html. Was undefined until 2026-09-18.
  global.saveEditedTrack = saveEditedTrack;

  // NOTE: window.toggleAlbumFavourite is deliberately NOT defined here —
  // favourites are being removed. Delete the heart button from the template.
})(window);
