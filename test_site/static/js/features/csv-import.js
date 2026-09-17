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

  function storePayload(payload) {
    const id = payloadIndex++;
    trackPayloads[id] = payload;
    return id;
  }
  global.storePayload = storePayload;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
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
    const statusEl = document.getElementById('csvImportStatus');

    if (!fileInput.files.length || !playlistName) {
      global.toast.warning('Please select a CSV file and enter a playlist name');
      return;
    }

    statusEl.textContent = 'Importing…';
    statusEl.className = 'ms-2 text-secondary';

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('playlist_name', playlistName);
    formData.append('playlist_description', playlistDescription);
    formData.append('target_user', targetUser);

    try {
      // FormData must NOT get a Content-Type header — the browser sets the
      // multipart boundary. api.postJson would override it, so this uses
      // fetch + parseJsonResponse for the same error handling.
      const response = await fetch('/api/playlist/import/csv', {
        method: 'POST',
        body: formData,
      });
      const data = await global.api.parseJsonResponse(response, 'CSV import');

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
    }
  }

  function buildTrackRow(track, options = {}) {
    const actions = options.actionsHtml || '';
    return `
      <div class="track-row"${options.pid !== undefined ? ` data-pid="${esc(options.pid)}"` : ''}>
        <div class="track-info">
          <div class="track-title">${esc(track.title)}</div>
          <div class="track-artist">${esc(track.artist)}</div>
          <div class="track-album">${esc(track.album || 'Unknown Album')}</div>
        </div>
        <div class="track-actions">${options.badgeHtml || ''}${actions}</div>
      </div>`;
  }

  /**
   * Render the matched / missing summary for an import response.
   * @param {Object} data
   */
  function displayResults(data) {
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
})(window);
