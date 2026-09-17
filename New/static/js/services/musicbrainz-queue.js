/* ==========================================================================
   static/js/services/musicbrainz-queue.js
   MusicBrainz / Discogs release lookup, result rendering, and queueing.

   Load order (all four are required):

       <script src="{{ url_for('static', filename='js/utils/dom.js') }}"></script>
       <script src="{{ url_for('static', filename='js/utils/api.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/toast.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/button-state.js') }}"></script>
       <script src="{{ url_for('static', filename='js/services/musicbrainz.js') }}"></script>

   Replaces these, which are near-duplicates of each other:

       artist_detail.html   searchMusicBrainzRelease, displayMusicBrainzResults,
                            updateMBSelectionUI, markMBTrackQueued,
                            downloadMusicBrainzRelease, mbRetrySearch,
                            searchMusicBrainzReleaseFromEncoded
       downloads.js         the same five, plus the upcoming-release match flow

   ── DIVERGENCES RECONCILED ────────────────────────────────────────────────
   The two copies had drifted apart in ways that change behaviour. Each
   decision below is deliberate; do not "simplify" them back.

   1. release_id vs release_group_id  — FIXED BUG
      artist_detail collapsed the two into one field:
          release_id: release.release_id || release.release_group_id
      They are different MBIDs for different entities. A release-group MBID
      stored in release_id is then POSTed as `release_mbid` to
      /api/queue/add-batch, where it is used for MB-specific duplicate
      detection — so a release group silently poisons dedupe for every
      release inside it. downloads.js kept them separate; that is correct
      and is what this module does.

   2. closeModal default — OPPOSITE in the two copies
      artist_detail:  options.closeModal === true    (default: stay open)
      downloads.js:   options.closeModal !== false   (default: close)
      Both are intentional for their page: the artist page queues several
      albums in a row from one search, the downloads page queues one and
      returns. Neither default is right for both, so it is now an explicit
      required-ish option defaulting to STAYING OPEN (the less destructive
      of the two), and the downloads call sites pass closeModal: true.

   3. Artist-only search  — DEAD CODE in artist_detail
      artist_detail returns early for a blank album, delegating to
      searchMusicBrainzForAllReleases. Every line after that computes
          const isArtistOnlySearch = !album || !String(album).trim();
      which is therefore ALWAYS false, making its `if (isArtistOnlySearch)`
      branch unreachable and pinning the result threshold at 3. Preserved as
      the real behaviour (delegate), with the dead branch removed.

   4. Result threshold before the Discogs fallback
      artist_detail: >= 3    downloads.js: >= 1
      The artist page asks for Discogs whenever MB returns 1-2 results, then
      MERGES both lists, so nothing is lost — it just gets more candidates.
      That is the better behaviour and is used here for both.

   5. Discogs failure handling
      artist_detail wraps Discogs in try/catch and treats a failure as
      "no Discogs results", because Discogs is optional and often
      unconfigured. downloads.js let a Discogs error abort the whole
      search, discarding MusicBrainz results that had already arrived.
      The tolerant version wins.

   6. escapeHtml(track.position) — LATENT CRASH
      artist_detail passed track.position and duration to escapeHtml with no
      guard. Combined with the throwing escapeHtml copy documented in
      utils/dom.js, any numeric position crashed the render. The new
      escapeHtml coerces, and this module also keeps downloads.js's
      `|| ''` guards.

   7. releaseInfo was NOT escaped in artist_detail
      `${releaseInfo.join(' · ')}` went in raw; it contains API-supplied
      format/label/catalog strings. downloads.js escaped it. Escaped here.
   ========================================================================== */

(function (global) {
  'use strict';

  const SEARCH_ENDPOINT = '/api/upcoming-releases/search-musicbrainz';
  const DISCOGS_ENDPOINT = '/api/upcoming-releases/search-discogs';
  const QUEUE_BATCH_ENDPOINT = '/api/queue/add-batch';
  const MODAL_ID = 'musicBrainzModal';

  /** Cross-tab signal that the queue changed. */
  const QUEUE_UPDATED_KEY = 'popularr_queue_updated';

  /**
   * Release payloads are keyed here rather than serialised into HTML
   * attributes — they contain quotes, apostrophes and unicode that broke
   * attribute escaping. Buttons carry only `data-release-key`.
   */
  const releaseCache = Object.create(null);

  /** Set when the search was launched to match an upcoming release. */
  let upcomingContext = null;

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

  // ── Track duration ────────────────────────────────────────────────────

  /**
   * MusicBrainz returns `length` in ms; Discogs returns `duration` as
   * a pre-formatted "m:ss" string. Handle both.
   *
   * @param {Object} track
   * @returns {string}
   */
  function trackDuration(track) {
    if (!track) return '';
    if (track.duration) return String(track.duration);
    if (track.length) {
      if (global.formatDuration) return global.formatDuration(track.length);
      const total = Math.floor(Number(track.length) / 1000);
      const mins = Math.floor(total / 60);
      const secs = total % 60;
      return `${mins}:${String(secs).padStart(2, '0')}`;
    }
    return '';
  }

  // ── Search ────────────────────────────────────────────────────────────

  /**
   * Search MusicBrainz for a release, falling back to Discogs.
   *
   * @param {Event|null} event
   * @param {string} artist
   * @param {string} album  blank delegates to the artist-releases flow
   * @param {Object} [opts]
   * @param {string|number} [opts.upcomingReleaseId] enables the "Use MBID" button
   * @returns {Promise<void>}
   */
  async function search(event, artist, album, opts = {}) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }

    upcomingContext = opts.upcomingReleaseId
      ? { releaseId: opts.upcomingReleaseId, artist, album }
      : null;

    // Artist-only lookups must use the artist-releases flow: a bare text
    // search returns releases from similarly named artists.
    if (!album || !String(album).trim()) {
      if (typeof global.searchMusicBrainzForAllReleases === 'function') {
        global.searchMusicBrainzForAllReleases(artist);
      } else {
        notifyError('Artist-only lookup is not available on this page.');
      }
      return;
    }

    const modalEl = document.getElementById(MODAL_ID);
    const statusEl = document.getElementById('mbSearchStatus');
    const errorEl = document.getElementById('mbSearchError');
    const resultsEl = document.getElementById('mbSearchResults');

    if (!modalEl || !resultsEl) {
      notifyError('Search UI not available on this page.');
      return;
    }

    if (global.modal) {
      global.modal.show(modalEl);
    } else if (global.bootstrap && global.bootstrap.Modal) {
      global.bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }

    // Set both fields unconditionally. The old `if (el && value)` guard left
    // a stale value in place, so an artist-only lookup run after an album
    // lookup silently kept the previous album and narrowed the query.
    const infoArtistEl = document.getElementById('mbSearchArtist');
    const infoAlbumEl = document.getElementById('mbSearchAlbum');
    if (infoArtistEl) infoArtistEl.textContent = artist || '';
    if (infoAlbumEl) infoAlbumEl.textContent = album || '';
    const infoEl = document.getElementById('mbSearchInfo');
    if (infoEl) infoEl.style.display = 'block';

    const setStatus = (html) => {
      if (!statusEl) return;
      statusEl.style.display = html ? 'block' : 'none';
      if (html) statusEl.innerHTML = html;
    };
    const showError = (message) => {
      setStatus('');
      if (errorEl) {
        errorEl.textContent = message;
        errorEl.style.display = 'block';
      } else {
        notifyError(message);
      }
      showRetry(artist, album);
    };

    setStatus('<div class="spinner-border spinner-border-sm me-2"></div>Searching MusicBrainz...');
    if (errorEl) errorEl.style.display = 'none';
    hideRetry();
    resultsEl.innerHTML = '';

    try {
      const data = await global.api.postJson(SEARCH_ENDPOINT, { artist, album });
      const mbResults = Array.isArray(data.results) ? data.results : [];

      if (data.success && mbResults.length >= 3) {
        setStatus('');
        render(mbResults);
        return;
      }

      setStatus('<div class="spinner-border spinner-border-sm me-2"></div>Searching Discogs fallback...');

      // Discogs is optional and frequently unconfigured. A failure here must
      // NOT discard the MusicBrainz results we already hold.
      const discogs = await global.api.fetchJsonOrDefault(
        DISCOGS_ENDPOINT,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ artist, album }),
        },
        { success: false, results: [] }
      );

      setStatus('');

      const all = mbResults.slice();
      if (discogs.success && Array.isArray(discogs.results)) {
        all.push(...discogs.results);
      }

      if (all.length === 0) {
        showError('No releases found on MusicBrainz or Discogs');
        return;
      }
      render(all);
    } catch (error) {
      showError('Error searching: ' + error.message);
    }
  }

  function showRetry(artist, album) {
    const retryEl = document.getElementById('mbSearchRetry');
    if (!retryEl) return;
    const a = document.getElementById('mbRetryArtist');
    const b = document.getElementById('mbRetryAlbum');
    if (a) a.value = artist || '';
    if (b) b.value = album || '';
    retryEl.style.display = 'block';
  }

  function hideRetry() {
    const retryEl = document.getElementById('mbSearchRetry');
    if (retryEl) retryEl.style.display = 'none';
  }

  /** Re-run from the retry inputs. */
  function retrySearch() {
    const artist = (document.getElementById('mbRetryArtist')?.value || '').trim();
    const album = (document.getElementById('mbRetryAlbum')?.value || '').trim();
    return search(null, artist, album);
  }

  // ── Rendering ─────────────────────────────────────────────────────────

  function buildTrackRow(track, trackIndex, dataKey) {
    return `
      <tr class="table-dark">
        <td>
          <input type="checkbox" class="form-check-input mb-track-check"
                 data-release-key="${esc(dataKey)}" data-track-index="${trackIndex}">
        </td>
        <td>${esc(track.position || '')}</td>
        <td>${esc(track.title || '')}</td>
        <td>${esc(trackDuration(track))}</td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-success mb-download-track"
                  data-release-key="${esc(dataKey)}" data-track-index="${trackIndex}">
            <i class="bi bi-download"></i>
          </button>
        </td>
      </tr>`;
  }

  function buildReleaseInfo(release) {
    const info = [];
    if (release.format) info.push(release.format);
    else if (Array.isArray(release.formats) && release.formats.length) {
      info.push(release.formats.join(', '));
    }
    if (release.country) info.push(release.country);
    if (release.date) info.push(release.date);
    else if (release.year) info.push(String(release.year));
    if (release.label) info.push(release.label);
    if (release.catalog_number) info.push(release.catalog_number);
    info.push(`${release.track_count || (release.tracks || []).length} tracks`);
    return info;
  }

  /**
   * Render search results into #mbSearchResults.
   * @param {Array<Object>} results
   */
  function render(results) {
    const container = document.getElementById('mbSearchResults');
    if (!container) return;

    const html = (results || []).map((release, index) => {
      const releaseId = `mb-release-${index}`;
      const dataKey = `release_${index}`;
      const tracks = release.tracks || [];

      releaseCache[dataKey] = {
        artist: release.artist,
        album: release.title,
        tracks: tracks,
        year: release.year || release.date,
        // Kept DISTINCT — see divergence 1 in the header.
        release_id: release.release_id || null,
        release_group_id: release.release_group_id || null,
        source: release.source || 'musicbrainz',
      };

      const source = release.source || 'musicbrainz';
      const sourceBadge = source === 'discogs'
        ? '<span class="badge bg-info ms-2">Discogs</span>'
        : '<span class="badge bg-primary ms-2">MusicBrainz</span>';

      const tracksHtml = tracks
        .map((track, i) => buildTrackRow(track, i, dataKey))
        .join('');

      const trackCount = release.track_count || tracks.length;

      // Only offered when the search came from an upcoming release AND the
      // result actually carries a release-group MBID (Discogs never does).
      const matchButton =
        upcomingContext && source !== 'discogs' && release.release_group_id
          ? `<button class="btn btn-outline-primary mb-save-upcoming-match" data-release-key="${esc(dataKey)}">
               <i class="bi bi-link-45deg"></i> Use MBID
             </button>`
          : '';

      return `
        <div class="accordion-item">
          <h2 class="accordion-header">
            <button class="accordion-button ${index === 0 ? '' : 'collapsed'}" type="button"
              data-bs-toggle="collapse" data-bs-target="#${releaseId}">
              <div class="d-flex justify-content-between align-items-center w-100 me-3">
                <div>
                  <strong>${esc(release.title || '')}</strong>${sourceBadge}
                  <small class="text-muted ms-2">${esc(buildReleaseInfo(release).join(' · '))}</small>
                </div>
              </div>
            </button>
          </h2>
          <div id="${releaseId}" class="accordion-collapse collapse ${index === 0 ? 'show' : ''}"
               data-bs-parent="#mbSearchResults">
            <div class="accordion-body">
              <div class="d-flex gap-2 mb-3 flex-wrap">
                <button class="btn btn-success mb-download-release" data-release-key="${esc(dataKey)}">
                  <i class="bi bi-download"></i> Download All Tracks (${trackCount})
                </button>
                <button class="btn btn-outline-success mb-download-selected" data-release-key="${esc(dataKey)}" disabled>
                  <i class="bi bi-check2-square"></i> Download Selected (<span class="mb-selected-count">0</span>)
                </button>
                ${matchButton}
              </div>
              <table class="table table-sm table-hover table-striped table-dark mb-track-table">
                <thead>
                  <tr>
                    <th style="width:2.5rem;">
                      <input type="checkbox" class="form-check-input mb-track-check-all"
                             data-release-key="${esc(dataKey)}">
                    </th>
                    <th style="width:3rem;">#</th>
                    <th>Title</th>
                    <th style="width:5rem;">Length</th>
                    <th style="width:4rem;"></th>
                  </tr>
                </thead>
                <tbody>${tracksHtml}</tbody>
              </table>
            </div>
          </div>
        </div>`;
    }).join('');

    container.innerHTML = `<div class="accordion" id="mbResultsAccordion">${html}</div>`;
    attachHandlers(container);
  }

  // ── Selection state ───────────────────────────────────────────────────

  function selectedIndexes(container, dataKey) {
    return Array.from(
      container.querySelectorAll(`.mb-track-check[data-release-key="${dataKey}"]`)
    )
      .filter((cb) => cb.checked && !cb.disabled)
      .map((cb) => parseInt(cb.dataset.trackIndex, 10));
  }

  function selectedTracks(container, dataKey) {
    const data = releaseCache[dataKey];
    if (!data) return [];
    return selectedIndexes(container, dataKey)
      .map((i) => data.tracks[i])
      .filter(Boolean);
  }

  /** Sync the "Download Selected (n)" button with the checkboxes. */
  function updateSelectionUI(container, dataKey) {
    const count = selectedIndexes(container, dataKey).length;
    const button = container.querySelector(`.mb-download-selected[data-release-key="${dataKey}"]`);
    if (button) {
      button.disabled = count === 0;
      const label = button.querySelector('.mb-selected-count');
      if (label) label.textContent = String(count);
    }

    const all = container.querySelector(`.mb-track-check-all[data-release-key="${dataKey}"]`);
    if (all) {
      const boxes = Array.from(
        container.querySelectorAll(`.mb-track-check[data-release-key="${dataKey}"]`)
      ).filter((cb) => !cb.disabled);
      all.checked = boxes.length > 0 && boxes.every((cb) => cb.checked);
      all.indeterminate = !all.checked && boxes.some((cb) => cb.checked);
    }
  }

  /** Grey out a track row once it has been queued. */
  function markQueued(container, dataKey, trackIndex) {
    const checkbox = container.querySelector(
      `.mb-track-check[data-release-key="${dataKey}"][data-track-index="${trackIndex}"]`
    );
    if (checkbox) {
      checkbox.checked = false;
      checkbox.disabled = true;
    }
    const button = container.querySelector(
      `.mb-download-track[data-release-key="${dataKey}"][data-track-index="${trackIndex}"]`
    );
    if (button && global.buttonState) {
      global.buttonState.setDone(button, {
        title: 'Already queued',
        fromClass: 'btn-outline-success',
      });
    }
    updateSelectionUI(container, dataKey);
  }

  // ── Event wiring ──────────────────────────────────────────────────────

  function attachHandlers(container) {
    container.querySelectorAll('.mb-track-check').forEach((cb) => {
      cb.addEventListener('change', function () {
        updateSelectionUI(container, this.dataset.releaseKey);
      });
    });

    container.querySelectorAll('.mb-track-check-all').forEach((cb) => {
      cb.addEventListener('change', function () {
        const key = this.dataset.releaseKey;
        const checked = this.checked;
        container
          .querySelectorAll(`.mb-track-check[data-release-key="${key}"]`)
          .forEach((box) => {
            if (!box.disabled) box.checked = checked;
          });
        updateSelectionUI(container, key);
      });
    });

    container.querySelectorAll('.mb-download-release').forEach((button) => {
      button.addEventListener('click', function () {
        const data = releaseCache[this.dataset.releaseKey];
        if (!data) return notifyError('Release data not found');
        return global.buttonState.withBusy(this, 'Queuing…', () =>
          queueRelease(data.artist, data.album, data.tracks, data.year,
            data.release_id, data.source)
        );
      });
    });

    container.querySelectorAll('.mb-download-selected').forEach((button) => {
      button.addEventListener('click', function () {
        const key = this.dataset.releaseKey;
        const data = releaseCache[key];
        if (!data) return notifyError('Release data not found');
        const tracks = selectedTracks(container, key);
        if (!tracks.length) return notifyError('No tracks selected');

        const indexes = selectedIndexes(container, key);
        return global.buttonState.withBusy(this, 'Queuing…', async () => {
          const ok = await queueRelease(
            data.artist, data.album, tracks, data.year,
            data.release_id, data.source,
            { selectionLabel: `${tracks.length} selected track${tracks.length === 1 ? '' : 's'}` }
          );
          if (ok) indexes.forEach((i) => markQueued(container, key, i));
        });
      });
    });

    container.querySelectorAll('.mb-download-track').forEach((button) => {
      button.addEventListener('click', function () {
        const key = this.dataset.releaseKey;
        const index = parseInt(this.dataset.trackIndex, 10);
        const data = releaseCache[key];
        if (!data || !data.tracks[index]) return notifyError('Track data not found');

        return global.buttonState.withBusy(this, '', async () => {
          const ok = await queueRelease(
            data.artist, data.album, [data.tracks[index]], data.year,
            data.release_id, data.source,
            { selectionLabel: '1 track' }
          );
          if (ok) markQueued(container, key, index);
        });
      });
    });

    container.querySelectorAll('.mb-save-upcoming-match').forEach((button) => {
      button.addEventListener('click', function () {
        const data = releaseCache[this.dataset.releaseKey];
        if (!data || !data.release_group_id) {
          return notifyError('This result does not include a MusicBrainz release-group MBID');
        }
        if (!upcomingContext || !upcomingContext.releaseId) {
          return notifyError('No upcoming release is selected for matching');
        }
        return global.buttonState.withBusy(this, 'Saving…', () =>
          saveUpcomingMatch(upcomingContext.releaseId, data.release_group_id)
        );
      });
    });
  }

  // ── Queueing ──────────────────────────────────────────────────────────

  /**
   * Add tracks to the download queue as one batch.
   *
   * @param {string} artist        release-level artist
   * @param {string} album
   * @param {Array<Object>} tracks
   * @param {string|number} year
   * @param {string} releaseId     release MBID — NOT a release-group MBID
   * @param {string} source        'musicbrainz' | 'discogs'
   * @param {Object} [options]
   * @param {boolean} [options.closeModal=false]
   * @param {string}  [options.selectionLabel]
   * @returns {Promise<boolean>} true when the batch was accepted
   */
  async function queueRelease(artist, album, tracks, year, releaseId, source, options = {}) {
    if (!tracks || tracks.length === 0) {
      notifyError('No tracks to download');
      return false;
    }

    // Defaults to STAYING OPEN so multiple albums can be queued from one
    // search. downloads.js call sites pass closeModal: true.
    const closeModal = options.closeModal === true;
    const selectionLabel = options.selectionLabel || null;

    let releaseYear = null;
    if (year) releaseYear = String(year).substring(0, 4);

    // Compilations carry a per-track artist. Use it as the queue item's
    // artist so files are tagged and named correctly, while album_artist
    // keeps the release-level artist (e.g. "Various Artists") so library
    // folder organisation stays correct.
    const items = tracks.map((track) => ({
      artist: track.artist || artist,
      album_artist: artist,
      album: album,
      title: track.title,
      year: releaseYear,
      track_number: track.position || null,
      source: source,
      // Separate column used for MB-specific duplicate detection and
      // cross-source overwrite merge in add_to_queue.
      release_mbid: source === 'musicbrainz' ? releaseId : null,
      duration: track.length || null,
    }));

    const importGroup = `${artist} - ${album}`;

    try {
      const data = await global.api.postJson(QUEUE_BATCH_ENDPOINT, {
        items: items,
        import_group: importGroup,
        import_type: 'album',
      });

      if (!data.success) {
        notifyError('Error: ' + (data.error || 'Failed to add tracks to queue'));
        return false;
      }

      if (closeModal && global.modal) global.modal.hide(MODAL_ID);

      notifySuccess(buildQueueMessage(data, album, selectionLabel));

      if (typeof global.loadQueueStatus === 'function') {
        await global.loadQueueStatus();
      }

      // Tells other open tabs to refresh their queue view.
      try {
        localStorage.setItem(QUEUE_UPDATED_KEY, Date.now().toString());
      } catch (e) {
        console.warn('Could not update localStorage:', e);
      }
      return true;
    } catch (error) {
      console.error('Error queueing release:', error);
      notifyError('Error: ' + error.message);
      return false;
    }
  }

  /**
   * Build the post-queue summary.
   * Keeps artist_detail's richer reporting (skipped + failed track names);
   * downloads.js reported neither, so silently-skipped duplicates looked
   * like successes.
   */
  function buildQueueMessage(data, album, selectionLabel) {
    const added = data.added || 0;
    const skipped = data.skipped || 0;
    const failed = data.failed || 0;
    const label = selectionLabel ? ` ${selectionLabel}` : ' tracks';

    let message = `Added ${added}${label} from "${album}" to download queue`;

    const names = (list) => {
      const shown = (list || []).slice(0, 5).join(', ');
      return shown ? `: ${shown}${list.length > 5 ? '…' : ''}` : '';
    };

    if (skipped > 0) {
      message += `\nSkipped ${skipped} already queued${names(data.skipped_tracks)}`;
    }
    if (failed > 0) {
      message += `\nFailed to add ${failed}${names(data.failed_tracks)}`;
    }
    if (added > 0 && data.import_group) {
      message +=
        `\n\nAll ${data.import_type || 'album'} tracks are grouped as "${album}". ` +
        'Once downloads complete, use "Organize All" in the Completed section.';
    }
    return message;
  }

  /**
   * Link an upcoming release to a MusicBrainz release group.
   *
   * @param {string|number} upcomingReleaseId
   * @param {string} releaseGroupMbid
   * @returns {Promise<boolean>}
   */
  async function saveUpcomingMatch(upcomingReleaseId, releaseGroupMbid) {
    try {
      const data = await global.api.postJson(
        `/api/upcoming-releases/${upcomingReleaseId}/match`,
        { release_group_mbid: releaseGroupMbid, source: 'manual_selection' }
      );
      if (!data.success) {
        notifyError(data.error || 'Failed to save match');
        return false;
      }
      notifySuccess('MusicBrainz release linked');
      if (typeof global.loadUpcomingReleases === 'function') {
        global.loadUpcomingReleases();
      }
      return true;
    } catch (error) {
      notifyError('Error saving match: ' + error.message);
      return false;
    }
  }

  // ── Public API ────────────────────────────────────────────────────────

  global.musicbrainz = {
    search,
    retrySearch,
    render,
    queueRelease,
    saveUpcomingMatch,
    updateSelectionUI,
    markQueued,
    selectedTracks,
    trackDuration,
    getRelease: (key) => releaseCache[key],
    setUpcomingContext: (ctx) => { upcomingContext = ctx; },
  };

  // ── Legacy aliases ────────────────────────────────────────────────────
  // NOTE: downloads.js sets `window.searchMusicBrainzReleaseCanonical` to
  // work around artist_detail's copy overwriting its own. Once both pages
  // load this module that alias is redundant and should be deleted.
  global.searchMusicBrainzRelease = function (event, artist, album, upcomingReleaseId) {
    return search(event, artist, album, { upcomingReleaseId });
  };
  global.searchMusicBrainzReleaseCanonical = global.searchMusicBrainzRelease;
  global.displayMusicBrainzResults = render;
  global.downloadMusicBrainzRelease = queueRelease;
  global.updateMBSelectionUI = updateSelectionUI;
  global.markMBTrackQueued = markQueued;
  global.mbRetrySearch = retrySearch;

  // artist_detail passes base64/URI-encoded args through inline onclick="".
  global.searchMusicBrainzReleaseFromEncoded = function (event, artistEnc, albumEnc) {
    const decode = global.decodeInlineArtistArg || ((v) => v || '');
    return search(event, decode(artistEnc, ''), decode(albumEnc, ''));
  };
})(window);
