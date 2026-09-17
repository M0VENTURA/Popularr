/* ==========================================================================
   static/js/services/genres.js
   Genre management: recommendations, apply, remove, and Navidrome rescan.

   Load order:

       <script src="{{ url_for('static', filename='js/utils/dom.js') }}"></script>
       <script src="{{ url_for('static', filename='js/utils/api.js') }}"></script>
       <script src="{{ url_for('static', filename='js/utils/poller.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/toast.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/modal.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/confirm.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/button-state.js') }}"></script>
       <script src="{{ url_for('static', filename='js/services/genres.js') }}"></script>

   REPLACES static/js/genre_utils.js ENTIRELY — delete that file.
   Also delete these from artist_detail.html's inline <script>:
       fetchArtistGenreRecommendations, toggleArtistGenreSelection,
       applySelectedArtistGenres, removeSelectedArtistGenres,
       applySelectedArtistSourceTags, and the `selectedArtistGenres` Set.

   ── THE BUG THIS FIXES ────────────────────────────────────────────────────
   genre_utils.js and artist_detail.html both define six functions with the
   SAME NAMES. genre_utils.js is loaded AFTER the inline block, so its
   versions win — but they target different element IDs and different
   endpoints, none of which exist on the artist page:

     function                        artist_detail (works)        genre_utils (wins, broken)
     ──────────────────────────────  ───────────────────────────  ──────────────────────────
     fetchArtistGenreRecommendations #recommendedArtistGenres     #recommendedGenres        (absent)
                                     /api/artist/genre-           /api/genres/
                                       recommendations              recommendations
                                     reads data.recommendations   reads data.genres
     applySelectedArtistGenres       #recommendedArtistGenres     #recommendedGenres        (absent)
                                     /api/artist/apply-genres     /api/genres/apply
                                     selection Set                checkbox values
     removeSelectedArtistGenres      thin, correct                sets a spinner then calls
                                                                  handleGenreRemoval, which
                                                                  never restores it

   Net effect on every artist page today:
     - "Get Online Suggestions" throws on `recommendedContainer.innerHTML`
       because #recommendedGenres does not exist.
     - "Apply Selected to All Artist Tracks" always reports
       "Please select at least one genre", because it reads checkboxes from
       a container that is not there while the real UI stores selections in
       a Set.
     - The remove button keeps its spinner forever after use.

   ── HOW THE MERGE WAS DECIDED ─────────────────────────────────────────────
   Kept from artist_detail.html (it matches the real markup and API):
       recommendation fetch + endpoint + response shape
       badge-based selection model
       apply endpoint /api/artist/apply-genres
   Kept from genre_utils.js (genuinely shared, album + artist + track):
       handleGenreRemoval / callGenreRemovalAPI
       Navidrome scan monitoring + progress modal
       toggleGenreCheckbox / getSelectedGenres
       removeAlbumGenre / removeTrackGenre / editTrackArtist

   ── TWO TEMPLATE CHANGES REQUIRED ─────────────────────────────────────────
   1. artist_detail.html reads the artist name from Jinja directly
      (`const artistName = {{ artist_name|tojson }}`), which cannot work in
      an external .js file. Add the attribute genre_utils.js already
      expected, on the page wrapper:

          <div class="artist-page" data-artist-name="{{ artist_name }}">

      album pages additionally need data-album-name="{{ album_name }}".

   2. The recommendation badges used `class="badge badge-outline-primary"`.
      `badge-outline-primary` DOES NOT EXIST in Bootstrap or in popularr.css
      — which is why the old code also set inline
      `border: 2px solid #0d6efd; color: #0d6efd`, hardcoding Bootstrap blue
      into a green theme. This module emits `.genre-chip` / `.genre-chip.selected`
      instead; add those two rules to popularr.css (supplied separately).
   ========================================================================== */

(function (global) {
  'use strict';

  const RECOMMEND_ENDPOINT = '/api/artist/genre-recommendations';
  const APPLY_ENDPOINT = '/api/artist/apply-genres';
  const REMOVE_ENDPOINT = '/api/genres/remove';
  const SCAN_STATUS_ENDPOINT = '/api/navidrome/scan/status';
  const TRACK_ENDPOINT = '/api/track/';
  const TRACK_TAGS_ENDPOINT = '/api/tags/track/';
  const TRACK_METADATA_ENDPOINT = '/api/track/update-metadata';

  const SCAN_MODAL_ID = 'scanProgressModal';
  const SCAN_POLL_INTERVAL_MS = 1000;
  const SCAN_MAX_ATTEMPTS = 120;
  /** Consecutive scan-status failures tolerated before giving up. */
  const SCAN_ERROR_GRACE = 5;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
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

  /**
   * Read the artist name from the page.
   * Requires data-artist-name on a wrapper element — see the header note.
   */
  function currentArtist() {
    const el = document.querySelector('[data-artist-name]');
    return el ? el.dataset.artistName || '' : '';
  }

  /** Read the album name from the page, for album-scoped operations. */
  function currentAlbum() {
    const el = document.querySelector('[data-album-name]');
    return el ? el.dataset.albumName || '' : '';
  }

  // ── Selection state ───────────────────────────────────────────────────
  // Badge-based, from artist_detail.html. genre_utils.js assumed checkboxes
  // in a container that does not exist on the artist page.

  const selected = new Set();

  /** Toggle a recommendation chip on or off. */
  function toggleSelection(genre) {
    const badge = document.querySelector(`[data-artist-genre="${CSS.escape(genre)}"]`);
    if (selected.has(genre)) {
      selected.delete(genre);
      if (badge) badge.classList.remove('selected');
    } else {
      selected.add(genre);
      if (badge) badge.classList.add('selected');
    }
    updateApplyButton();
  }

  function updateApplyButton() {
    const btn = document.getElementById('applyArtistGenresBtn');
    if (!btn) return;
    btn.style.display = selected.size > 0 ? 'inline-block' : 'none';
  }

  function clearSelection() {
    selected.clear();
    document.querySelectorAll('.genre-chip.selected')
      .forEach((el) => el.classList.remove('selected'));
    updateApplyButton();
  }

  // ── Recommendations ───────────────────────────────────────────────────

  /**
   * Fetch online genre suggestions for the current artist.
   * Endpoint and response shape taken from artist_detail.html; the request
   * goes through the backend so MusicBrainz headers are set server-side.
   */
  async function fetchRecommendations() {
    const artist = currentArtist();
    if (!artist) {
      notifyError('Could not determine the artist name');
      return;
    }

    const btn = document.getElementById('fetchArtistGenresBtn');
    const container = document.getElementById('recommendedArtistGenres');
    const section = document.getElementById('recommendedArtistGenresSection');
    if (!container) {
      notifyError('Genre recommendations are not available on this page');
      return;
    }

    if (section) section.style.display = 'block';
    container.innerHTML = '<span class="text-muted small">Loading recommendations…</span>';

    const run = async () => {
      try {
        const data = await global.api.getJson(
          `${RECOMMEND_ENDPOINT}?artist=${encodeURIComponent(artist)}`
        );

        // De-duplicated and sorted; the API can return the same genre from
        // several sources.
        const genres = Array.from(new Set(
          (data.recommendations || [])
            .map((g) => String(g || '').trim())
            .filter(Boolean)
        )).sort();

        if (!genres.length) {
          container.innerHTML =
            '<span class="text-muted small">No genre recommendations found</span>';
          return;
        }

        container.innerHTML = genres.map((genre) => `
          <span class="badge genre-chip" role="button" tabindex="0"
                data-artist-genre="${esc(genre)}">${esc(genre)}</span>
        `).join('');

        // Listeners rather than inline onclick: the old markup built
        // onclick="toggleArtistGenreSelection('${escapeHtml(genre)}')",
        // which broke on any genre containing an apostrophe
        // (e.g. "drum 'n' bass") because HTML escaping does not escape
        // for a JavaScript string context.
        container.querySelectorAll('.genre-chip').forEach((chip) => {
          const activate = () => toggleSelection(chip.dataset.artistGenre);
          chip.addEventListener('click', activate);
          chip.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              activate();
            }
          });
        });

        clearSelection();
      } catch (error) {
        container.innerHTML =
          `<span class="text-danger small">Error fetching recommendations: ${esc(error.message)}</span>`;
      }
    };

    return btn && global.buttonState
      ? global.buttonState.withBusy(btn, 'Fetching…', run)
      : run();
  }

  // ── Apply ─────────────────────────────────────────────────────────────

  /**
   * Apply the selected recommendation chips to every track by this artist.
   * Uses /api/artist/apply-genres — genre_utils.js posted to
   * /api/genres/apply, which takes a different payload shape.
   */
  async function applySelected() {
    if (selected.size === 0) {
      notifyError('Please select at least one genre to apply');
      return false;
    }

    const artist = currentArtist();
    const genres = Array.from(selected);

    const accepted = await confirmFn({
      title: 'Apply genres',
      message: `Apply ${genres.length} genre${genres.length === 1 ? '' : 's'} to ALL tracks by "${artist}"?`,
      detail: 'This updates the MP3/FLAC files for every album by this artist.',
      items: genres,
      tone: 'primary',
      confirmLabel: 'Apply',
    });
    if (!accepted) return false;

    const btn = document.getElementById('applyArtistGenresBtn');
    const run = async () => {
      try {
        const data = await global.api.postJson(APPLY_ENDPOINT, { artist, genres });
        if (!data.success) {
          notifyError(data.error || 'Failed to apply genres');
          return false;
        }
        notifySuccess(data.message || 'Genres applied to all artist tracks');
        setTimeout(() => location.reload(), 1200);
        return true;
      } catch (error) {
        notifyError('Error: ' + error.message);
        return false;
      }
    };

    return btn && global.buttonState
      ? global.buttonState.withBusy(btn, 'Applying…', run)
      : run();
  }

  /**
   * Apply checked source tags (Last.fm / Discogs / MusicBrainz / Essentia
   * tabs) to all artist tracks. Same endpoint as applySelected, different
   * source of truth for the selection.
   */
  async function applySelectedSourceTags() {
    const checked = Array.from(
      document.querySelectorAll('.artist-source-tag-check:checked')
    ).map((cb) => cb.value).filter(Boolean);

    if (!checked.length) {
      notifyError('No tags selected');
      return false;
    }

    const artist = currentArtist();
    const accepted = await confirmFn({
      title: 'Save tags',
      message: `Save ${checked.length} tag${checked.length === 1 ? '' : 's'} to ALL tracks by "${artist}"?`,
      items: checked,
      tone: 'primary',
      confirmLabel: 'Save',
    });
    if (!accepted) return false;

    // There is one apply button per source tab; disable them all so a
    // second tab cannot fire the same request concurrently.
    const buttons = Array.from(document.querySelectorAll('.artist-apply-source-tags-btn'));
    const restores = global.buttonState
      ? buttons.map((b) => global.buttonState.setBusy(b, 'Saving…'))
      : [];

    try {
      const data = await global.api.postJson(APPLY_ENDPOINT, { artist, genres: checked });
      if (!data.success) {
        notifyError(data.error || 'Failed to save tags');
        return false;
      }
      notifySuccess(data.message || 'Tags saved to all artist tracks');
      setTimeout(() => location.reload(), 1200);
      return true;
    } catch (error) {
      notifyError('Error: ' + error.message);
      return false;
    } finally {
      restores.forEach((restore) => restore());
    }
  }

  // ── Removal ───────────────────────────────────────────────────────────

  /**
   * Remove genres from an artist's tracks or from one album.
   * Kept from genre_utils.js — this half was correct and is shared by the
   * artist page, album page and track rows.
   *
   * @param {string} artist
   * @param {string|null} album  null for artist-wide removal
   * @param {Array<string>} genres
   * @returns {Promise<boolean>}
   */
  async function remove(artist, album, genres) {
    if (!genres || !genres.length) {
      notifyError('No genres selected');
      return false;
    }

    const scope = album ? `the album "${album}"` : `all tracks by "${artist}"`;
    const accepted = await confirmFn({
      title: 'Remove genres',
      message: `Remove ${genres.length} genre${genres.length === 1 ? '' : 's'} from ${scope}?`,
      detail: 'This updates the MP3/FLAC files and triggers a Navidrome scan.',
      items: genres,
      tone: 'danger',
      confirmLabel: 'Remove',
    });
    if (!accepted) return false;

    showScanModal();
    updateScanProgress('Removing genres…');

    const payload = { artist_name: artist, genres };
    if (album) payload.album_name = album;

    try {
      const data = await global.api.postJson(REMOVE_ENDPOINT, payload);
      if (!data.success) {
        updateScanProgress('Error: ' + (data.error || 'Removal failed'));
        setTimeout(closeScanModal, 3000);
        return false;
      }

      const count = data.affected_tracks || 0;
      updateScanProgress(`Removed from ${count} track${count === 1 ? '' : 's'}.`);

      if (data.scan_triggered) {
        await monitorNavidromeScan();
      } else {
        setTimeout(() => {
          closeScanModal();
          location.reload();
        }, 2000);
      }
      return true;
    } catch (error) {
      updateScanProgress('Error: ' + error.message);
      setTimeout(closeScanModal, 3000);
      return false;
    }
  }

  /** Remove the checked genres in #currentArtistGenres from this artist. */
  function removeSelectedArtistGenres() {
    return remove(currentArtist(), null, getSelectedGenres('currentArtistGenres'));
  }

  /** Remove one genre from one album. */
  function removeAlbumGenre(genre, artist, album) {
    const a = artist || currentArtist();
    const b = album || currentAlbum();
    if (!a || !b) {
      notifyError('Could not determine the artist or album');
      return Promise.resolve(false);
    }
    return remove(a, b, [genre]);
  }

  /**
   * Remove one genre from a single track, rewriting the track's genre tag.
   * Kept from genre_utils.js; not duplicated anywhere.
   */
  async function removeTrackGenre(trackId, genre, element) {
    const accepted = await confirmFn({
      title: 'Remove genre',
      message: `Remove "${genre}" from this track?`,
      tone: 'danger',
      confirmLabel: 'Remove',
    });
    if (!accepted) return false;

    try {
      const track = await global.api.getJson(TRACK_ENDPOINT + encodeURIComponent(trackId));
      const remaining = String(track.genre || '')
        .split(/[;,/]/g)
        .map((g) => g.trim())
        .filter((g) => g && g !== genre);

      const data = await global.api.postJson(TRACK_TAGS_ENDPOINT + encodeURIComponent(trackId), {
        tags: { genre: remaining.join(';') },
        sync_to_file: true,
      });

      if (!data.success && !data.message) {
        notifyError(data.error || 'Failed to remove genre');
        return false;
      }

      if (element) {
        element.style.opacity = '0.5';
        setTimeout(() => element.remove(), 300);
      }
      notifySuccess(`Genre removed${data.file_synced ? ' and file updated' : ''}`);
      return true;
    } catch (error) {
      notifyError('Error: ' + error.message);
      return false;
    }
  }

  // ── Checkbox helpers ──────────────────────────────────────────────────
  // Kept from genre_utils.js; used by the current-genres checkbox lists on
  // both the artist and album pages.

  /** Show/hide a remove button based on how many boxes are ticked. */
  function toggleGenreCheckbox(containerId, buttonId) {
    const container = document.getElementById(containerId);
    const button = document.getElementById(buttonId);
    if (!container || !button) return;

    const count = container.querySelectorAll('input[type="checkbox"]:checked').length;
    button.style.display = count > 0 ? 'inline-block' : 'none';
    if (count > 0) {
      button.textContent = `Remove ${count} Selected Genre${count === 1 ? '' : 's'}`;
    }
  }

  /** Values of the ticked checkboxes in a container. */
  function getSelectedGenres(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return [];
    return Array.from(container.querySelectorAll('input[type="checkbox"]:checked'))
      .map((cb) => cb.value);
  }

  // ── Navidrome scan progress ───────────────────────────────────────────

  function showScanModal() {
    const bodyHtml = `
      <div class="text-center py-3">
        <div class="spinner-border text-info mb-3" role="status">
          <span class="visually-hidden">Working…</span>
        </div>
        <p id="scanProgressText" class="text-secondary mb-0">Updating tracks…</p>
      </div>`;

    if (global.modal) {
      // Themed via modal.template — the old markup hardcoded
      // style="background-color:#1a1a1a; border-color:#333", which is not
      // a theme token and is one shade off --secondary-bg (#1e1e1e).
      global.modal.open(SCAN_MODAL_ID, global.modal.template({
        id: SCAN_MODAL_ID,
        title: 'Updating Genres',
        icon: 'bi-hourglass-split',
        bodyHtml,
        centered: true,
        staticBackdrop: true,
      }), { autoDestroy: false });
    }
  }

  function updateScanProgress(message) {
    const el = document.getElementById('scanProgressText');
    if (el) el.textContent = message;
  }

  function closeScanModal() {
    if (global.modal) {
      global.modal.hide(SCAN_MODAL_ID);
      global.modal.destroy(SCAN_MODAL_ID);
    }
  }

  /**
   * Watch a Navidrome rescan to completion.
   *
   * Uses the managed poller, so it stops on teardown and cannot overlap
   * requests. The old version held a raw setInterval that kept firing if
   * the user navigated away mid-scan.
   *
   * @returns {Promise<void>}
   */
  function monitorNavidromeScan() {
    updateScanProgress('Starting Navidrome scan…');
    let consecutiveErrors = 0;

    return new Promise((resolve) => {
      const finish = (message, reload) => {
        updateScanProgress(message);
        setTimeout(() => {
          closeScanModal();
          if (reload) location.reload();
          resolve();
        }, 3000);
      };

      const p = global.poller.create({
        interval: SCAN_POLL_INTERVAL_MS,
        maxAttempts: SCAN_MAX_ATTEMPTS,
        // A rescan must keep being watched even in a background tab.
        pauseWhenHidden: false,
        onTick: async (ctx) => {
          let data;
          try {
            data = await global.api.getJson(SCAN_STATUS_ENDPOINT);
            consecutiveErrors = 0;
          } catch (error) {
            // Navidrome may not be configured or reachable. Tolerate a few
            // failures before concluding anything — the genre write itself
            // already succeeded.
            consecutiveErrors += 1;
            if (consecutiveErrors > SCAN_ERROR_GRACE) {
              ctx.stop();
              finish('Could not verify scan status. Genres have been updated.', true);
            }
            return;
          }

          if (!data.success) {
            ctx.stop();
            finish('Could not connect to Navidrome.', false);
            return;
          }

          if (data.scanning) {
            updateScanProgress(`Scanning Navidrome library… ${data.count || 0} items processed`);
            return;
          }

          ctx.stop();
          finish('Navidrome scan completed. Genres are now updated.', true);
        },
        onTimeout: () => finish('Scan timed out. Check Navidrome for progress.', false),
      });
      p.start();
    });
  }

  // ── Track artist edit ─────────────────────────────────────────────────
  // Kept from genre_utils.js; not duplicated elsewhere.

  /**
   * Edit a track's artist — via the album page's modal when present,
   * otherwise a prompt.
   */
  async function editTrackArtist(trackId, currentValue) {
    const modalEl = document.getElementById('editTrackModal');

    if (modalEl) {
      document.getElementById('editTrackId').value = trackId;
      document.getElementById('editTrackCurrentField').value = 'artist';
      document.getElementById('editTrackLabel').textContent = 'Track Artist';
      const field = document.getElementById('editTrackValue');
      field.value = currentValue && currentValue !== '—' ? currentValue : '';
      if (global.modal) global.modal.show(modalEl);
      field.focus();
      return true;
    }

    const next = global.prompt('Enter new artist name:', currentValue || '');
    if (!next) return false;

    try {
      const data = await global.api.postJson(TRACK_METADATA_ENDPOINT, {
        track_id: trackId,
        artist: next,
      });
      if (!data.success) {
        notifyError(data.error || 'Failed to update');
        return false;
      }
      notifySuccess('Track artist updated');
      setTimeout(() => location.reload(), 1000);
      return true;
    } catch (error) {
      notifyError('Error: ' + error.message);
      return false;
    }
  }

  // ── Source-tag checkbox visibility ────────────────────────────────────
  // Moved out of artist_detail.html's inline block. Delegated, so it works
  // for tab panes rendered after load.

  document.addEventListener('change', function (event) {
    const target = event.target;
    if (!target || !target.classList.contains('artist-source-tag-check')) return;
    const pane = target.closest('.tab-pane');
    if (!pane) return;
    const applyBtn = pane.querySelector('.artist-apply-source-tags-btn');
    if (!applyBtn) return;
    const anyChecked = pane.querySelectorAll('.artist-source-tag-check:checked').length > 0;
    applyBtn.style.display = anyChecked ? '' : 'none';
  });

  global.genres = {
    fetchRecommendations,
    toggleSelection,
    applySelected,
    applySelectedSourceTags,
    remove,
    removeSelectedArtistGenres,
    removeAlbumGenre,
    removeTrackGenre,
    toggleGenreCheckbox,
    getSelectedGenres,
    monitorNavidromeScan,
    editTrackArtist,
    clearSelection,
    currentArtist,
    currentAlbum,
  };

  // ── Legacy aliases ────────────────────────────────────────────────────
  // For the inline onclick="" handlers still in the templates.
  // NOTE: applySelectedArtistGenres and removeSelectedArtistGenres are now
  // async. The existing onclick handlers ignore the return value, so they
  // keep working — but any NEW caller that needs the result must await it.
  global.fetchArtistGenreRecommendations = fetchRecommendations;
  global.toggleArtistGenreSelection = toggleSelection;
  global.applySelectedArtistGenres = applySelected;
  global.applySelectedArtistSourceTags = applySelectedSourceTags;
  global.removeSelectedArtistGenres = removeSelectedArtistGenres;
  global.removeAlbumGenre = removeAlbumGenre;
  global.removeTrackGenre = removeTrackGenre;
  global.toggleGenreCheckbox = toggleGenreCheckbox;
  global.getSelectedGenres = getSelectedGenres;
  global.editTrackArtist = editTrackArtist;
  global.handleGenreRemoval = function (artist, album, genreList) {
    return remove(artist, album, genreList);
  };
})(window);
