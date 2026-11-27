/* ==========================================================================
   static/js/features/csv-import.js
   Playlist / CSV import for the unified search page.

   (The original header said `static/js//features/csv_import.js` — a doubled
   slash and an underscore instead of the kebab-case the rest of the tree
   uses. The file has always been csv-import.js.)

   Requires: utils/dom.js, utils/api.js, state/import-state.js
   Load AFTER state/import-state.js and AFTER components/search/_playlists.html

   ── CHANGES FROM THE PREVIOUS VERSION ─────────────────────────────────────
   1. THE `let` COLLISION — FIXED BUG. This file opened with
          let currentImportData = null;
          let missingTracksForSearch = [];
      and playlist.js declares BOTH of the same names at its own top level.
      Classic <script> tags share one scope, so on any page loading both,
      the second file throws
          SyntaxError: Identifier 'currentImportData' has already been declared
      at PARSE time — nothing in it is defined. Both declarations are gone;
      state/import-state.js owns them and exposes the same bare names via
      accessor properties, so the assignments below still work unchanged.

   2. `escapeHtml` is no longer borrowed from downloads.js. The old header
      said "Depends on: downloads.js (for escapeHtml, fetchJsonOrThrow)" —
      but this page does not load downloads.js, so both were undefined here
      and every displayResults() call threw on the first escapeHtml.

   3. DEAD PAYLOAD IDS. displayResults() called
          const pid = storePayload({...});
      for every matched track and then never emitted `pid` into the markup.
      search_init.js reads `_trackPayloads[btn.dataset.pid]` from
      `.js-add-track-btn` elements — which this file never rendered, so the
      Add / Soulseek / Replace buttons did not exist on matched rows at all.
      The rows now carry `data-pid` and the action buttons, so the existing
      delegation in search_init.js has something to bind to.
   ========================================================================== */

(function (global) {
  'use strict';

  /**
   * Track payloads for the delegated handlers in search_init.js.
   * Kept as a window global because that file resolves it by bare name.
   */
  const trackPayloads = global._trackPayloads || {};
  global._trackPayloads = trackPayloads;

  let payloadIndex = 0;
  // The last import response, so "Download Missing" can act without a re-upload.
  let lastImportData = null;

  function storePayload(payload) {
    const id = payloadIndex++;
    trackPayloads[id] = payload;
    return id;
  }
  global.storePayload = storePayload;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function selectedMode() {
    const album = document.getElementById('csvModeAlbum');
    return album && album.checked ? 'album' : 'playlist';
  }

  /** Show the album-name field only in album mode, and relabel the submit button. */
  function syncModeUi() {
    const mode = selectedMode();
    const albumWrap = document.getElementById('csvAlbumNameWrap');
    const userWrap = document.getElementById('csvTargetUserWrap');
    const playlistHint = document.getElementById('csvModeHint');
    const albumHint = document.getElementById('csvAlbumHint');
    const label = document.getElementById('csvSubmitLabel');
    if (albumWrap) albumWrap.style.display = mode === 'album' ? '' : 'none';
    // A playlist is per-user; an album queue is not.
    if (userWrap) userWrap.style.display = mode === 'album' ? 'none' : '';
    if (playlistHint) playlistHint.style.display = mode === 'album' ? 'none' : '';
    if (albumHint) albumHint.style.display = mode === 'album' ? '' : 'none';
    if (label) label.textContent = mode === 'album' ? 'Import Album' : 'Import Playlist';
  }

  /**
   * Import a CSV of tracks and match them against the library.
   * @param {Event} event
   */
  async function importPlaylistFromCSV(event) {
    event.preventDefault();

    const fileInput = document.getElementById('csvFile');
    const playlistName = document.getElementById('csvPlaylistName').value.trim();
    const playlistDescription = document.getElementById('csvPlaylistDescription').value.trim();
    const targetUser = document.getElementById('csvTargetUser').value.trim();
    const albumNameEl = document.getElementById('csvAlbumName');
    const albumName = albumNameEl ? albumNameEl.value.trim() : '';
    const mode = selectedMode();
    const statusEl = document.getElementById('csvImportStatus');

    if (!fileInput.files.length || !playlistName) {
      global.toast.warning('Please select a CSV file and enter a name');
      return;
    }

    statusEl.textContent = mode === 'album' ? 'Queueing album…' : 'Importing…';
    statusEl.className = 'ms-2 text-secondary';

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('playlist_name', playlistName);
    formData.append('playlist_description', playlistDescription);
    formData.append('target_user', targetUser);
    formData.append('mode', mode);
    if (albumName) formData.append('album_name', albumName);

    try {
      // FormData must NOT get a Content-Type header — the browser sets the
      // multipart boundary. api.postJson would override it, so this uses
      // fetch + parseJsonResponse for the same error handling.
      const response = await fetch('/api/playlist/import/csv', {
        method: 'POST',
        body: formData,
      });
      const data = await global.api.parseJsonResponse(response, 'CSV import');

      if (data && data.mode === 'album') {
        displayAlbumResult(data);
        statusEl.textContent = 'Album queued';
        statusEl.className = 'ms-2 text-success';
        return;
      }

      // One call replaces the two separate assignments; import-state.js
      // derives missingTracksForSearch from the response.
      global.importState.set(data);

      displayResults(data);
      statusEl.textContent = 'Import complete';
      statusEl.className = 'ms-2 text-success';
    } catch (error) {
      console.error('CSV import error:', error);
      global.importState.clear();
      statusEl.textContent = error.message;
      statusEl.className = 'ms-2 text-danger';
      document.getElementById('errorSection').style.display = 'block';
      document.getElementById('errorMessage').textContent = error.message;
      document.getElementById('resultsSection').style.display = 'none';
      const albumSection = document.getElementById('albumSummarySection');
      if (albumSection) albumSection.style.display = 'none';
    }
  }

  function displayAlbumResult(data) {
    const text = document.getElementById('albumSummaryText');
    if (text) {
      const comp = data.is_compilation ? ' (Various Artists)' : '';
      text.textContent =
        `"${data.album}" by ${data.album_artist}${comp} — ` +
        `${data.queued} queued, ${data.skipped} already queued, ${data.failed} failed ` +
        `of ${data.total}.`;
    }
    const albumSection = document.getElementById('albumSummarySection');
    if (albumSection) albumSection.style.display = 'block';
    const results = document.getElementById('resultsSection');
    if (results) results.style.display = 'none';
    const errSection = document.getElementById('errorSection');
    if (errSection) errSection.style.display = 'none';
  }

  function fmtDuration(seconds) {
    const total = Math.round(Number(seconds));
    if (!Number.isFinite(total) || total <= 0) return '';
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function buildTrackRow(track, options = {}) {
    const actions = options.actionsHtml || '';
    const dur = fmtDuration(track.duration_s);
    return `
      <div class="track-row"${options.pid !== undefined ? ` data-pid="${esc(options.pid)}"` : ''}>
        <div class="track-info">
          <div class="track-title">${esc(track.title)}${dur ? ` <span class="text-muted small">${esc(dur)}</span>` : ''}</div>
          <div class="track-artist">${esc(track.artist)}</div>
          <div class="track-album">${esc(track.album || 'Unknown Album')}</div>
        </div>
        <div class="track-actions">${options.badgeHtml || ''}${actions}</div>
      </div>`;
  }

  /**
   * Queue every missing track under its OWN artist and album.
   *
   * RECOMMENDATION (asked which is cleaner): for a PLAYLIST export, a missing
   * track belongs on its real album, not forced into one artificial album — a
   * playlist spans many artists and releases. Each track therefore queues as a
   * normal single with its own album/artist from the CSV. (Album mode is the
   * opposite by design: everything becomes one album.)
   *
   * Each queued row carries its CSV duration, so a track you already own is
   * COPIED from your library rather than downloaded (the copy gate needs a known
   * length to verify).
   */
  async function downloadMissingPlaylistTracks() {
    const missing = (lastImportData && lastImportData.missing_tracks) || [];
    if (!missing.length) {
      global.toast.info && global.toast.info('Nothing is missing — every track is in your library.');
      return;
    }
    const btn = document.getElementById('downloadMissingBtn');
    const run = async () => {
      const group = ((lastImportData.playlist_name || 'playlist')
        .replace(/\s+/g, '_').replace(/[^A-Za-z0-9_]/g, '').slice(0, 80)) || 'playlist';
      const items = missing.map((t) => ({
        artist: t.artist || '',
        title: t.title || '',
        album: t.album || '',
        album_artist: t.album_artist || t.artist || '',
        duration: t.duration_s || null,
        year: t.year || null,
        source: 'soulseek',
        import_type: 'song',
      })).filter((t) => t.title);
      if (!items.length) return;
      const data = await global.api.postJson('/api/queue/add-batch', {
        items,
        import_group: group,
        import_type: 'song',
        source: 'soulseek',
      });
      const added = (data && data.added) || 0;
      const skipped = (data && data.skipped) || 0;
      if (global.toast.queued) global.toast.queued(`${added} missing track(s)`);
      else global.toast.success && global.toast.success(`Queued ${added} track(s)`);
      if (skipped) {
        global.toast.info && global.toast.info(`${skipped} were already in the queue.`);
      }
    };
    if (btn && global.buttonState && global.buttonState.withBusy) {
      return global.buttonState.withBusy(btn, 'Queueing…', run);
    }
    return run();
  }

  /**
   * Render the matched / missing summary for an import response.
   * @param {Object} data
   */
  function displayResults(data) {
    lastImportData = data || null;
    const matched = data.matched_tracks || [];
    const missing = data.missing_tracks || [];
    const total = matched.length + missing.length;
    const coverage = total > 0 ? Math.round((matched.length / total) * 100) : 0;

    document.getElementById('matchedCount').textContent = matched.length;
    document.getElementById('missingCount').textContent = missing.length;
    document.getElementById('totalCount').textContent = total;
    document.getElementById('coverage').textContent = coverage + '%';

    const coverageCard = document.getElementById('coverageCard');
    coverageCard.classList.remove('bg-success', 'bg-warning', 'bg-danger');
    if (coverage >= 90) coverageCard.classList.add('bg-success');
    else if (coverage >= 70) coverageCard.classList.add('bg-warning');
    else coverageCard.classList.add('bg-danger');

    const matchedContainer = document.getElementById('matchedTracksContainer');
    matchedContainer.innerHTML = matched.length
      ? matched.map((track) => {
          // data-pid is what search_init.js's delegated handlers read. The
          // previous version computed this id and discarded it.
          const pid = storePayload({
            artist: track.artist,
            title: track.title,
            album: track.album || '',
            query: `${track.artist} ${track.title}`,
          });
          return buildTrackRow(track, {
            pid,
            badgeHtml: '<span class="badge bg-success">Found</span>',
            actionsHtml:
              `<button type="button" class="btn btn-sm btn-outline-secondary js-add-track-btn ms-1" data-pid="${pid}" title="Add to playlist">` +
              '<i class="bi bi-plus-lg"></i></button>',
          });
        }).join('')
      : '<p class="text-secondary text-center py-5">No matched tracks found</p>';

    const missingContainer = document.getElementById('missingTracksContainer');
    missingContainer.innerHTML = missing.length
      ? missing.map((track) => {
          const pid = storePayload({
            artist: track.artist,
            title: track.title,
            album: track.album || '',
            query: `${track.artist} ${track.title}`,
          });
          return buildTrackRow(track, {
            pid,
            badgeHtml: '<span class="missing-track-badge">Missing</span>',
            actionsHtml:
              `<button type="button" class="btn btn-sm btn-outline-info js-slskd-search-btn ms-1" data-pid="${pid}" title="Search Soulseek">` +
              '<i class="bi bi-search"></i></button>',
          });
        }).join('')
      : '<p class="text-success text-center py-5">All tracks found in library!</p>';

    document.getElementById('resultsSection').style.display = 'block';
    document.getElementById('errorSection').style.display = 'none';
  }

  global.importPlaylistFromCSV = importPlaylistFromCSV;
  global.displayResults = displayResults;
  global.downloadMissingPlaylistTracks = downloadMissingPlaylistTracks;

  // Wire the mode toggle (the partial is rendered once; the listeners are
  // idempotent and cheap).
  const _albumRadio = document.getElementById('csvModeAlbum');
  const _playlistRadio = document.getElementById('csvModePlaylist');
  if (_albumRadio) _albumRadio.addEventListener('change', syncModeUi);
  if (_playlistRadio) _playlistRadio.addEventListener('change', syncModeUi);
  syncModeUi();
})(window);
