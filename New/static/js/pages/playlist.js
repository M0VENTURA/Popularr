/* ==========================================================================
   static/js/pages/playlist.js
   Playlist manager — browse Navidrome playlists, the playlist downloader,
   custom playlist creation, the smart-playlist builder, and the Last.fm /
   ListenBrainz recommendation tabs.

   Load order:
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/modal.js  →  ui/confirm.js  →  ui/button-state.js
       ui/status-badge.js
       state/import-state.js          ← REQUIRED, see below
       playlist.js

   ── THE CRITICAL FIX ──────────────────────────────────────────────────────
   This file used to open with:

       let currentImportData = null;
       let missingTracksForSearch = [];

   playlist_import.js declares the SAME two names at ITS top level. Classic
   <script> tags share one top-level scope and `let` cannot be redeclared in
   it, so on any page loading both files the second one threw

       SyntaxError: Identifier 'currentImportData' has already been declared

   at PARSE time — meaning not a single function in it was ever defined.

   Both declarations are GONE. state/import-state.js owns them and re-exposes
   the same bare names through accessor properties, so `currentImportData`
   still reads and writes exactly as before at every call site below.
   That file MUST load before this one.

   ── DELIBERATELY PRESERVED VERBATIM ───────────────────────────────────────
   The smart-playlist builder (~700 lines: SPB_FIELDS, SPB_SORT_FIELDS,
   SPB_OPERATORS, SPB_PRESETS, the drag-and-drop helpers, and the
   rule/group/sort collectors) is copied UNCHANGED.

   SPB_FIELDS and SPB_OPERATORS are a transcription of the .nsp rule schema
   your backend parses, and spbBuildJson() emits the exact JSON shape it
   expects. Rewriting any of it risks changing that shape — a renamed
   operator key or a dropped field would produce smart playlists that
   silently match nothing. The Navidrome bucket-assignment logic in
   loadNavidromePlaylists() and the render functions are preserved for the
   same reason.

   ── formatDuration STAYS LOCAL ────────────────────────────────────────────
   This page's version takes PLAIN SECONDS. utils/dom.js's guesses the unit
   by magnitude (µs / ms / s), so a 3600-second track would render as "1:00"
   there instead of "60:00". Not interchangeable — same call as
   playlists_index.js.

   ── OTHER FIXES ───────────────────────────────────────────────────────────
   1. _lbQueueStatusBadge INTERPOLATED AN UNESCAPED FAILURE REASON:
          const failHint = track.queue_failure_reason
            ? ` title="${track.queue_failure_reason}"` : '';
      That value is a server-side error string. One containing a double
      quote closed the attribute early and the rest became live markup.
      Escaped now.

   2. escapeHtml here used createTextNode + innerHTML, which escapes & < >
      but NOT quotes — and its output went into title="…" in several render
      functions. utils/dom.js's version escapes all five entities.

   3. `alert()` → toast; hand-rolled button spinners → ui/button-state.js,
      which restores the button in a `finally` (createPlaylist and
      createCustomPlaylist both had paths that left the spinner stuck).
   ========================================================================== */

(function (global) {
  'use strict';

  // ── State ─────────────────────────────────────────────────────────────
  //
  // NOTE: `currentImportData` and `missingTracksForSearch` are deliberately
  // NOT declared here — state/import-state.js owns them. See the header.

  let currentPlaylistId = null;
  let currentPlaylistData = null;
  let currentPlaylistType = null;
  let currentSmartPlaylistId = null;
  let currentSmartPlaylistData = null;
  let lfmRecommendationsData = null;
  let lbRecommendationsData = null;
  let replacingTrackInfo = null;
  let selectedSongs = [];
  let selectedReplacements = {};

  function escapeHtml(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  /**
   * Format a track duration.
   * PLAIN SECONDS — see the header note on why this is not dom.js's version.
   */
  function formatDuration(seconds) {
    const n = Number(seconds) || 0;
    const mins = Math.floor(n / 60);
    const secs = Math.floor(n % 60);
    return `${mins}:${String(secs).padStart(2, '0')}`;
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

  // Shim so the preserved blocks below keep working unchanged.
  const alert = notifyError;


  // ── Page setup ────────────────────────────────────────────────────────

  function setupBrowsePageListeners() {
    // Shared handler for all three bucket selects and the legacy combined select
    async function onPlaylistSelectChange() {
      if (!this.value) return;
      const selectedOption = this.options[this.selectedIndex];
      const playlistType = selectedOption?.dataset?.playlistType || 'regular';
      const playlistName = (selectedOption?.textContent || '').trim();
      // Clear the other bucket selects so only one shows as selected
      ['essentialPlaylistSelect', 'topPlaylistSelect', 'browsePlaylistSelect'].forEach(id => {
        if (id !== this.id) {
          const el = document.getElementById(id);
          if (el) el.value = '';
        }
      });
      loadNavidromePlaylistDetail(this.value, playlistType);
      loadPlaylistForDownload(this.value);
      // Try to load a matching .nsp file into the smart playlist editor first.
      // This handles NSP-backed playlists regardless of how Navidrome classifies them.
      // loadBrowseSmartPlaylistIntoEditor returns true if an NSP file was loaded.
      let loadedAsNsp = false;
      if (typeof loadBrowseSmartPlaylistIntoEditor === 'function') {
        loadedAsNsp = await loadBrowseSmartPlaylistIntoEditor(playlistName);
      }
      // Only fall back to the regular editor if no .nsp file matched
      if (!loadedAsNsp && playlistType === 'regular' && typeof loadBrowseRegularPlaylistIntoEditor === 'function') {
        loadBrowseRegularPlaylistIntoEditor(this.value, playlistName);
      }
    }

    try {
      const essentialPlaylistSelect = document.getElementById('essentialPlaylistSelect');
      if (essentialPlaylistSelect) {
        essentialPlaylistSelect.addEventListener('change', onPlaylistSelectChange);
      }
    } catch (e) {
      console.warn('[Playlist Manager] Could not set up essentialPlaylistSelect listener:', e);
    }

    try {
      const topPlaylistSelect = document.getElementById('topPlaylistSelect');
      if (topPlaylistSelect) {
        topPlaylistSelect.addEventListener('change', onPlaylistSelectChange);
      }
    } catch (e) {
      console.warn('[Playlist Manager] Could not set up topPlaylistSelect listener:', e);
    }

    try {
      const browsePlaylistSelect = document.getElementById('browsePlaylistSelect');
      if (browsePlaylistSelect) {
        browsePlaylistSelect.addEventListener('change', onPlaylistSelectChange);
      }
    } catch (e) {
      console.warn('[Playlist Manager] Could not set up browsePlaylistSelect listener:', e);
    }

    try {
      const smartPlaylistSelect = document.getElementById('smartPlaylistSelect');
      if (smartPlaylistSelect) {
        smartPlaylistSelect.addEventListener('change', function() {
          if (this.value) {
            const regularPlaylistSelect = document.getElementById('regularPlaylistSelect');
            if (regularPlaylistSelect) regularPlaylistSelect.value = '';
            const downloaderSelect = document.getElementById('playlistFileSelect');
            if (downloaderSelect) downloaderSelect.value = this.value;
            loadNavidromePlaylistDetail(this.value, 'smart');
          }
        });
      }
    } catch (e) {
      console.warn('[Playlist Manager] Could not set up smartPlaylistSelect listener:', e);
    }

    try {
      const regularPlaylistSelect = document.getElementById('regularPlaylistSelect');
      if (regularPlaylistSelect) {
        regularPlaylistSelect.addEventListener('change', function() {
          if (this.value) {
            const smartPlaylistSelect = document.getElementById('smartPlaylistSelect');
            if (smartPlaylistSelect) smartPlaylistSelect.value = '';
            const downloaderSelect = document.getElementById('playlistFileSelect');
            if (downloaderSelect) downloaderSelect.value = this.value;
            loadNavidromePlaylistDetail(this.value, 'regular');
          }
        });
      }
    } catch (e) {
      console.warn('[Playlist Manager] Could not set up regularPlaylistSelect listener:', e);
    }

    // Tab switching events
    const smartTab = document.getElementById('smartTab');
    if (smartTab) {
      smartTab.addEventListener('show.bs.tab', function(e) {
        console.log('[Playlist Manager] Smart tab shown');
        loadSmartPlaylists().catch(err => {
          console.error('[Playlist Manager] Error loading smart playlists on tab switch:', err);
        });
      });
    }

    const browseTab = document.getElementById('browseTab');
    if (browseTab) {
      browseTab.addEventListener('show.bs.tab', function(e) {
        console.log('[Playlist Manager] Browse tab shown');
      });
    }
  }

  // ===============================
  // CREATE PAGE SETUP
  // ===============================

  function setupCreatePageListeners() {
    if (document.getElementById("lbRssTables")) { loadListenBrainzRssTables(); loadListenBrainzSyncStatus(); }
    // Search and create playlist form handling
    const customPlaylistForm = document.getElementById('customPlaylistForm');
    if (customPlaylistForm) {
      customPlaylistForm.addEventListener('submit', createCustomPlaylist);
    }

    if (document.getElementById('smartPlaylistBuilderForm')) {
      initSmartPlaylistBuilder();
    }
  }

  // ===============================
  // NAVIDROME PLAYLISTS
  // ===============================

  // ── Navidrome playlists ───────────────────────────────────────────────

  async function loadNavidromePlaylists() {
    try {
      console.log('[Playlist Manager] Starting loadNavidromePlaylists');
      const response = await fetch('/api/navidrome/playlists');
      let data;
      try {
        data = await response.json();
      } catch (parseErr) {
        console.error('[Playlist Manager] API returned non-JSON:', parseErr);
        data = { error: 'Server returned invalid response (HTTP ' + response.status + ')' };
      }
      console.log('[Playlist Manager] API response:', data);

      // Three-bucket selects (browse page)
      const essentialSelect = document.getElementById('essentialPlaylistSelect');
      const topSelect       = document.getElementById('topPlaylistSelect');
      const otherSelect     = document.getElementById('browsePlaylistSelect');
      // Legacy separate selects (other pages)
      const smartSelect   = document.getElementById('smartPlaylistSelect');
      const regularSelect = document.getElementById('regularPlaylistSelect');

      const hasThreeBuckets = essentialSelect && topSelect && otherSelect;

      if (!hasThreeBuckets && !smartSelect && !regularSelect && !otherSelect) {
        console.error('[Playlist Manager] Could not find playlist select elements');
        return;
      }

      if (essentialSelect) essentialSelect.innerHTML = '<option value="">Select an Essential playlist...</option>';
      if (topSelect)       topSelect.innerHTML       = '<option value="">Select a Top playlist...</option>';
      if (otherSelect)     otherSelect.innerHTML     = '<option value="">Select a playlist...</option>';
      if (smartSelect)     smartSelect.innerHTML     = '<option value="">Select a smart playlist...</option>';
      if (regularSelect)   regularSelect.innerHTML   = '<option value="">Select a regular playlist...</option>';

      if (data.error) {
        console.error('[Playlist Manager] API Error:', data.error);
        const errMsg = 'Error: ' + data.error;
        if (essentialSelect) essentialSelect.innerHTML = `<option value="">${errMsg}</option>`;
        if (topSelect)       topSelect.innerHTML       = `<option value="">${errMsg}</option>`;
        if (otherSelect)     otherSelect.innerHTML     = `<option value="">${errMsg}</option>`;
        if (smartSelect)     smartSelect.innerHTML     = `<option value="">${errMsg}</option>`;
        if (regularSelect)   regularSelect.innerHTML   = `<option value="">${errMsg}</option>`;
        return;
      }

      // Merge smart + regular into a flat list for bucket assignment
      const allPlaylists = [];
      if (Array.isArray(data.smart)) {
        data.smart.forEach(pl => allPlaylists.push({ ...pl, playlistType: 'smart' }));
      }
      if (Array.isArray(data.regular)) {
        data.regular.forEach(pl => allPlaylists.push({ ...pl, playlistType: 'regular' }));
      }

      if (hasThreeBuckets) {
        // Populate three-bucket dropdowns
        const essentialGroup = { smart: document.createElement('optgroup'), regular: document.createElement('optgroup') };
        essentialGroup.smart.label   = 'Smart Playlists';
        essentialGroup.regular.label = 'Regular Playlists';
        const topGroup     = { smart: document.createElement('optgroup'), regular: document.createElement('optgroup') };
        topGroup.smart.label   = 'Smart Playlists';
        topGroup.regular.label = 'Regular Playlists';
        const otherGroup   = { smart: document.createElement('optgroup'), regular: document.createElement('optgroup') };
        otherGroup.smart.label   = 'Smart Playlists';
        otherGroup.regular.label = 'Regular Playlists';

        allPlaylists.forEach(pl => {
          const opt = document.createElement('option');
          opt.value = pl.id;
          opt.textContent = pl.name;
          opt.dataset.playlistType = pl.playlistType;

          const lname = (pl.name || '').toLowerCase();
          const grp = pl.playlistType === 'smart' ? 'smart' : 'regular';
          if (lname.startsWith('essential')) {
            essentialGroup[grp].appendChild(opt);
          } else if (lname.startsWith('top ') || lname === 'top') {
            topGroup[grp].appendChild(opt);
          } else {
            otherGroup[grp].appendChild(opt);
          }
        });

        [
          [essentialSelect, essentialGroup, 'No Essential playlists found'],
          [topSelect,       topGroup,       'No Top playlists found'],
          [otherSelect,     otherGroup,     'No other playlists found'],
        ].forEach(([sel, groups, emptyMsg]) => {
          if (!sel) return;
          let added = false;
          if (groups.smart.children.length > 0)   { sel.appendChild(groups.smart);   added = true; }
          if (groups.regular.children.length > 0) { sel.appendChild(groups.regular); added = true; }
          if (!added) sel.innerHTML = `<option value="">${emptyMsg}</option>`;
        });
      } else if (otherSelect) {
        // Fallback: single combined dropdown (non-browse pages)
        const smartGroup   = document.createElement('optgroup');
        smartGroup.label   = 'Smart Playlists';
        const regularGroup = document.createElement('optgroup');
        regularGroup.label = 'Regular Playlists';
        allPlaylists.forEach(pl => {
          const opt = document.createElement('option');
          opt.value = pl.id;
          opt.textContent = pl.name;
          opt.dataset.playlistType = pl.playlistType;
          (pl.playlistType === 'smart' ? smartGroup : regularGroup).appendChild(opt);
        });
        if (smartGroup.children.length > 0)   otherSelect.appendChild(smartGroup);
        if (regularGroup.children.length > 0) otherSelect.appendChild(regularGroup);
        if (allPlaylists.length === 0) otherSelect.innerHTML = '<option value="">No playlists found</option>';
      }

      // Also populate legacy smart/regular selects if present
      if (data.smart && data.smart.length > 0 && smartSelect) {
        data.smart.forEach(pl => {
          const opt = document.createElement('option');
          opt.value = pl.id;
          opt.textContent = pl.name;
          smartSelect.appendChild(opt);
        });
      }
      if (data.regular && data.regular.length > 0 && regularSelect) {
        data.regular.forEach(pl => {
          const opt = document.createElement('option');
          opt.value = pl.id;
          opt.textContent = pl.name;
          regularSelect.appendChild(opt);
        });
      }
      if (allPlaylists.length === 0) {
        console.warn('[Playlist Manager] No playlists found in Navidrome');
        if (smartSelect)   smartSelect.innerHTML   = '<option value="">No smart playlists found</option>';
        if (regularSelect) regularSelect.innerHTML = '<option value="">No regular playlists found</option>';
      }
    } catch (e) {
      console.error('[Playlist Manager] Failed to load Navidrome playlists:', e);
    }
  }

  // ── Playlist detail, downloader, search, create, smart lists ──────────

  async function loadNavidromePlaylistDetail(playlistId, type) {
    currentPlaylistId = playlistId;
    currentPlaylistType = type || 'regular';
  
    try {
      const response = await fetch(`/api/navidrome/playlist/${playlistId}`);
      const data = await response.json();
    
      if (!response.ok) {
        throw new Error(data.error || 'Failed to load playlist');
      }
    
      currentPlaylistData = data;
      displayPlaylistDetails(data, type);
    } catch (e) {
      console.error('Failed to load playlist details:', e);
      alert('Failed to load playlist: ' + e.message);
    }
  }

  function displayPlaylistDetails(data, type) {
    const detailsSection = document.getElementById('playlistDetailsSection');
    const nameEl = document.getElementById('playlistDetailName');
    const metadataEl = document.getElementById('playlistMetadata');
    const tracksEl = document.getElementById('playlistTracks');
  
    if (!detailsSection) return;
  
    nameEl.textContent = data.name || 'Unknown Playlist';
  
    let metadataHtml = '<div class="row g-3">';
    metadataHtml += `<div class="col-md-3"><strong>Type:</strong> ${type === 'smart' ? 'Smart Playlist' : 'Regular Playlist'}</div>`;
    metadataHtml += `<div class="col-md-3"><strong>Tracks:</strong> ${data.songCount || 0}</div>`;
    if (data.duration) {
      const hours = Math.floor(data.duration / 3600);
      const minutes = Math.floor((data.duration % 3600) / 60);
      metadataHtml += `<div class="col-md-3"><strong>Duration:</strong> ${hours}h ${minutes}m</div>`;
    }
    if (data.owner) {
      metadataHtml += `<div class="col-md-3"><strong>Owner:</strong> ${escapeHtml(data.owner)}</div>`;
    }
    if (data.comment) {
      metadataHtml += `<div class="col-12"><strong>Description:</strong> ${escapeHtml(data.comment)}</div>`;
    }
    metadataHtml += '</div>';
    metadataEl.innerHTML = metadataHtml;
  
    if (data.tracks && data.tracks.length > 0) {
      let tracksHtml = '<h6 class="mt-3 mb-2">Tracks</h6>';
      tracksHtml += '<div style="max-height: 400px; overflow-y: auto;">';
      data.tracks.forEach((track, idx) => {
        tracksHtml += `
          <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
            <div>
              <span class="text-muted me-2">${idx + 1}.</span>
              <strong>${escapeHtml(track.title || 'Unknown Track')}</strong><br>
              <small class="text-muted ms-4">${escapeHtml(track.artist || 'Unknown Artist')} ${track.album ? '– ' + escapeHtml(track.album) : ''}</small>
            </div>
            ${track.duration ? `<small class="text-muted">${formatDuration(track.duration)}</small>` : ''}
          </div>
        `;
      });
      tracksHtml += '</div>';
      tracksEl.innerHTML = tracksHtml;
    } else {
      tracksEl.innerHTML = '<p class="text-muted text-center py-3">No tracks available</p>';
    }
  
    detailsSection.style.display = 'block';
    detailsSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function editPlaylist() {
    if (!currentPlaylistId || !currentPlaylistData) {
      alert('No playlist selected');
      return;
    }
    const navidromeUrl = currentPlaylistData.navidromeUrl || '/navidrome';
    window.open(`${navidromeUrl}/#/playlist/${currentPlaylistId}`, '_blank');
  }

  function refreshPlaylistDetails() {
    if (currentPlaylistId) {
      const type = currentPlaylistType || 'regular';
      loadNavidromePlaylistDetail(currentPlaylistId, type);
      loadPlaylistForDownload(currentPlaylistId);
    }
  }

  // ===============================
  // PLAYLIST DOWNLOADER
  // ===============================

  async function loadPlaylistList() {
    try {
      console.log('[Playlist Manager] Starting loadPlaylistList');
      const response = await fetch('/api/playlist/list');
      const data = await response.json();
    
      const select = document.getElementById('playlistFileSelect');
      if (!select) return;
    
      select.innerHTML = '<option value="">Select a playlist...</option>';
    
      if (!data || !data.playlists) {
        console.error('[Playlist Manager] Invalid playlist list response:', data);
        select.innerHTML = '<option value="">Error: Invalid API response</option>';
        return;
      }
    
        if (data.playlists && data.playlists.length > 0) {
        data.playlists.forEach(playlist => {
          const option = document.createElement('option');
            option.value = playlist.id || playlist.path;
          let typeLabel = '';
          if (playlist.type === 'smart' || playlist.type === 'smart-local') typeLabel = ' (Smart)';
            option.textContent = `${playlist.name || playlist.path}${typeLabel} [${playlist.songCount || 0}]`;
          select.appendChild(option);
        });
      } else {
        select.innerHTML = '<option value="">No playlists found</option>';
      }
    } catch (error) {
      console.error('Error loading playlists:', error);
      const select = document.getElementById('playlistFileSelect');
      if (select) {
        select.innerHTML = '<option value="">Error loading playlists</option>';
      }
    }
  }

  async function loadPlaylistForDownload(playlistId = null) {
    const browseSelect = document.getElementById('browsePlaylistSelect');
    const playlistPath = String(playlistId || (browseSelect ? browseSelect.value : '') || '').trim();
    if (!playlistPath) {
      alert('Please select a playlist');
      return;
    }

    try {
      const response = await fetch('/api/playlist/load', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ playlist_id: playlistPath })
      });

      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Failed to load playlist');

      const songs = Array.isArray(data.songs) ? data.songs : [];
      const matchedFiles = Array.isArray(data.matched_files) ? data.matched_files : [];
      const normalized = {
        playlist_path: playlistPath,
        songs,
        matched_files: matchedFiles
      };

      currentPlaylistData = normalized;
      displayPlaylistDownloader(normalized);
    } catch (error) {
      console.error('Error:', error);
      alert('Error: ' + error.message);
    }
  }

  function displayPlaylistDownloader(data) {
    const songs = data.songs || [];
    const allMatches = data.matched_files || [];
    const matched = allMatches.filter(match => !!(match.file_path || match.filename));
  
    const downloaderEmpty = document.getElementById('downloaderEmpty');
    const downloaderResults = document.getElementById('downloaderResults');
  
    if (!downloaderEmpty || !downloaderResults) return;
  
    downloaderEmpty.style.display = 'none';
    downloaderResults.style.display = 'block';

    document.getElementById('dlTotalCount').textContent = songs.length;
    document.getElementById('dlMatchedCount').textContent = matched.length;
    const coverage = songs.length > 0 ? Math.round((matched.length / songs.length) * 100) : 0;
    document.getElementById('dlCoverage').textContent = coverage + '%';

    const originalHtml = songs.map((song, idx) => `
      <div class="card mb-2">
        <div class="card-body py-2">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <strong class="d-block">${escapeHtml(song.title || 'Unknown')}</strong>
              <small class="text-muted">${escapeHtml(song.artist || 'Unknown Artist')}</small>
              ${song.album ? `<br><small class="text-secondary text-opacity-75">${escapeHtml(song.album)}</small>` : ''}
            </div>
            <span class="badge ${song.detected ? 'bg-success' : 'bg-warning'}">
              ${song.detected ? '✓' : '✗'}
            </span>
          </div>
        </div>
      </div>
    `).join('');
    document.getElementById('dlOriginalSongs').innerHTML = originalHtml || '<p class="text-muted text-center py-5">No songs in playlist</p>';

    const matchedHtml = allMatches.map((match, idx) => {
      const dbPath = match.file_path || '';
      const navPath = match.filename || '';
      const statusBadge = dbPath
        ? '<span class="badge bg-success">DB Path</span>'
        : '<span class="badge bg-warning text-dark">Not Found In DB</span>';

      return `
      <div class="card mb-2 border-success">
        <div class="card-body py-2">
          <div class="d-flex justify-content-between align-items-start gap-2">
            <div style="flex: 1;">
              <strong class="d-block">${escapeHtml(match.title || 'Unknown')}</strong>
              <small class="text-muted">${escapeHtml(match.artist || 'Unknown')}</small>
              <div class="mt-1">${statusBadge}</div>
              ${dbPath ? `<br><small class="text-secondary text-opacity-75">${escapeHtml(dbPath)}</small>` : ''}
              ${!dbPath && navPath ? `<br><small class="text-muted">Navidrome: ${escapeHtml(navPath)}</small>` : ''}
            </div>
          </div>
        </div>
      </div>
    `;
    }).join('');
    document.getElementById('dlDetectedMatches').innerHTML = matchedHtml || '<p class="text-muted text-center py-5">No tracks available</p>';

    selectedReplacements = {};
  }

  function replacePlaylistMatch(matchIndex) {
    alert('Replace functionality coming soon');
  }

  async function downloadPlaylistMatches() {
    if (!currentPlaylistData) return;
    const btn = document.getElementById('dlDownloadBtn');
    if (!btn) return;
  
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Downloading...';

    try {
      const response = await fetch('/api/playlist/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          playlist_path: currentPlaylistData.playlist_path,
          replacements: selectedReplacements
        })
      });

      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Download failed');

      alert('✓ Download queued! ' + (data.queued || 0) + ' files enqueued');
    } catch (error) {
      console.error('Error:', error);
      alert('✗ Error: ' + error.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = originalText;
    }
  }

  // ===============================
  // CUSTOM PLAYLIST CREATOR
  // ===============================

  async function searchSongs() {
    const query = document.getElementById('songSearchInput')?.value.trim() || '';
    const title = document.getElementById('searchTitleInput')?.value.trim() || '';
    const artist = document.getElementById('searchArtistInput')?.value.trim() || '';
    const album = document.getElementById('searchAlbumInput')?.value.trim() || '';

    if (!query && !title && !artist && !album) {
      alert('Please enter at least one search field');
      return;
    }

    try {
      const response = await fetch('/api/playlist/search-songs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, title, artist, album })
      });

      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Search failed');

      displaySearchResults(data.songs || []);
    } catch (error) {
      console.error('Error:', error);
      alert('Error: ' + error.message);
    }
  }

  function displaySearchResults(songs) {
    const resultsDiv = document.getElementById('songSearchResults');
    if (!resultsDiv) return;
  
    if (songs.length === 0) {
      resultsDiv.innerHTML = '<p class="text-muted text-center">No songs found</p>';
      resultsDiv.style.display = 'block';
      return;
    }

    resultsDiv.innerHTML = songs.map((song, idx) => `
      <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
        <div>
          <strong>${escapeHtml(song.title || 'Unknown')}</strong><br>
          <small class="text-muted">${escapeHtml(song.artist || 'Unknown')} ${song.album ? '– ' + escapeHtml(song.album) : ''}</small>
        </div>
        <button class="btn btn-sm btn-outline-success" data-song-idx="${idx}" type="button">
          <i class="bi bi-plus"></i> Add
        </button>
      </div>
    `).join('');
  
    songs.forEach((song, idx) => {
      const btn = resultsDiv.querySelector(`button[data-song-idx="${idx}"]`);
      if (btn) {
        btn.addEventListener('click', function() {
          addSongToPlaylistObj(song);
        });
      }
    });
    resultsDiv.style.display = 'block';
  }

  function getSongKey(song) {
    return song.id || `${song.title || ''}|${song.artist || ''}|${song.album || ''}`;
  }

  function addSongToPlaylistObj(song) {
    const key = getSongKey(song);
    if (selectedSongs.find(s => getSongKey(s) === key)) {
      alert('This song is already in the playlist');
      return;
    }
    selectedSongs.push(song);
    updateSelectedSongsList();
  }

  function updateSelectedSongsList() {
    const listDiv = document.getElementById('selectedSongsList');
    const countBadge = document.getElementById('selectedSongsCount');
  
    if (!listDiv || !countBadge) return;
  
    countBadge.textContent = selectedSongs.length;

    if (selectedSongs.length === 0) {
      listDiv.innerHTML = '<p class="text-muted text-center py-5">No songs added yet</p>';
      return;
    }

    listDiv.innerHTML = selectedSongs.map((song, idx) => `
      <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
        <div>
          <strong>${escapeHtml(song.title || 'Unknown')}</strong><br>
          <small class="text-muted">${escapeHtml(song.artist || 'Unknown')}</small>
        </div>
        <button class="btn btn-sm btn-outline-danger" onclick="removeSongFromPlaylist(${idx})">
          <i class="bi bi-trash"></i>
        </button>
      </div>
    `).join('');
  }

  function removeSongFromPlaylist(index) {
    selectedSongs.splice(index, 1);
    updateSelectedSongsList();
  }

  async function createCustomPlaylist(event) {
    event.preventDefault();

    if (selectedSongs.length === 0) {
      alert('Please add at least one song to the playlist');
      return;
    }

    const btn = event.target.querySelector('button[type="submit"]');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Creating...';

    try {
      const response = await fetch('/api/playlist/create-custom', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: document.getElementById('customPlaylistName').value.trim(),
          description: document.getElementById('customPlaylistDesc').value.trim(),
          user: document.getElementById('customPlaylistUser').value,
          is_public: document.getElementById('customPlaylistPublic').checked,
          songs: selectedSongs
        })
      });

      let data;
      try {
        data = await response.json();
      } catch (jsonErr) {
        throw new Error('Invalid JSON response from server.');
      }
      if (!response.ok) throw new Error(data.error || 'Failed to create playlist');

      alert('✓ Playlist created successfully!');
      event.target.reset();
      selectedSongs = [];
      updateSelectedSongsList();
    } catch (error) {
      console.error('Error:', error);
      alert('✗ Error: ' + error.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = originalText;
    }
  }

  // ===============================
  // SMART PLAYLISTS
  // ===============================

  async function loadSmartPlaylists() {
    try {
      console.log('[Playlist Manager] Loading smart playlists dropdown');
      const response = await fetch('/api/navidrome/playlists');
      const data = await response.json();
    
      const select = document.getElementById('smartPlaylistDropdown');
      if (!select) {
        console.warn('[Playlist Manager] smartPlaylistDropdown element not found');
        return;
      }
    
      select.innerHTML = '<option value="">Select a smart playlist...</option>';
    
      if (data.error) {
        console.error('[Playlist Manager] API Error:', data.error);
        select.innerHTML = '<option value="">Error: ' + data.error + '</option>';
        return;
      }
    
      if (data.smart && data.smart.length > 0) {
        data.smart.forEach(playlist => {
          const option = document.createElement('option');
          option.value = playlist.id;
          option.textContent = playlist.name;
          select.appendChild(option);
        });
      } else {
        select.innerHTML = '<option value="">No smart playlists found</option>';
      }
    } catch (error) {
      console.error('[Playlist Manager] Error loading smart playlists:', error);
      const select = document.getElementById('smartPlaylistDropdown');
      if (select) {
        select.innerHTML = '<option value="">Error: Network failure</option>';
      }
    }
  }

  async function loadSmartPlaylistDetail() {
    const playlistId = document.getElementById('smartPlaylistDropdown').value;
    if (!playlistId) {
      alert('Please select a smart playlist');
      return;
    }

    try {
      const response = await fetch(`/api/navidrome/playlist/${playlistId}`);
      const data = await response.json();
    
      if (!response.ok) {
        throw new Error(data.error || 'Failed to load playlist');
      }
    
      currentSmartPlaylistId = playlistId;
      currentSmartPlaylistData = data;
      displaySmartPlaylistDetails(data);
    } catch (error) {
      console.error('Error loading smart playlist details:', error);
      alert('Failed to load playlist: ' + error.message);
    }
  }

  function displaySmartPlaylistDetails(data) {
    const detailsSection = document.getElementById('smartPlaylistDetails');
    if (!detailsSection) return;
  
    const nameEl = document.getElementById('smartPlaylistName');
    const metadataEl = document.getElementById('smartPlaylistMetadata');
    const tracksEl = document.getElementById('smartPlaylistTracks');
  
    nameEl.textContent = data.name || 'Unknown Smart Playlist';
  
    let metadataHtml = '<div class="row g-3">';
    metadataHtml += `<div class="col-md-3"><strong>Tracks:</strong> ${data.songCount || 0}</div>`;
    if (data.duration) {
      const hours = Math.floor(data.duration / 3600);
      const minutes = Math.floor((data.duration % 3600) / 60);
      metadataHtml += `<div class="col-md-3"><strong>Duration:</strong> ${hours}h ${minutes}m</div>`;
    }
    if (data.owner) {
      metadataHtml += `<div class="col-md-3"><strong>Owner:</strong> ${escapeHtml(data.owner)}</div>`;
    }
    if (data.comment) {
      metadataHtml += `<div class="col-12"><strong>Description:</strong> ${escapeHtml(data.comment)}</div>`;
    }
    metadataHtml += '</div>';
    metadataEl.innerHTML = metadataHtml;
  
    if (data.tracks && data.tracks.length > 0) {
      let tracksHtml = '<h6 class="mb-2">Tracks</h6>';
      data.tracks.forEach((track, idx) => {
        tracksHtml += `
          <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
            <div style="flex: 1;">
              <strong>${escapeHtml(track.title || 'Unknown Track')}</strong><br>
              <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')} ${track.album ? '– ' + escapeHtml(track.album) : ''}</small>
            </div>
            ${track.duration ? `<small class="text-muted">${formatDuration(track.duration)}</small>` : ''}
          </div>
        `;
      });
      tracksEl.innerHTML = tracksHtml;
    } else {
      tracksEl.innerHTML = '<p class="text-muted text-center py-3">No tracks in this playlist</p>';
    }
  
    document.getElementById('smartPlaylistEmpty').style.display = 'none';
    detailsSection.style.display = 'block';
    detailsSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function refreshSmartPlaylistDetail() {
    if (currentSmartPlaylistId) {
      loadSmartPlaylistDetail();
    }
  }

  // ===============================
  // SMART PLAYLIST BUILDER (CREATE PAGE)
  // ===============================

  // ── Smart playlist builder (PRESERVED VERBATIM — see header) ──────────

  const SPB_FIELDS = [
    { key: 'title', label: 'Title', type: 'string' },
    { key: 'album', label: 'Album', type: 'string' },
    { key: 'artist', label: 'Artist', type: 'string' },
    { key: 'albumartist', label: 'Album Artist', type: 'string' },
    { key: 'genre', label: 'Genre', type: 'string' },
    { key: 'mood', label: 'Mood', type: 'string' },
    { key: 'rating', label: 'Rating', type: 'number' },
    { key: 'playcount', label: 'Play Count', type: 'number' },
    { key: 'year', label: 'Year', type: 'number' },
    { key: 'duration', label: 'Duration (seconds)', type: 'number' },
    { key: 'bitrate', label: 'Bitrate', type: 'number' },
    { key: 'track', label: 'Track Number', type: 'number' },
    { key: 'discnumber', label: 'Disc Number', type: 'number' },
    { key: 'loved', label: 'Loved', type: 'boolean' },
    { key: 'compilation', label: 'Compilation', type: 'boolean' },
    { key: 'hascoverart', label: 'Has Cover Art', type: 'boolean' },
    { key: 'lastplayed', label: 'Last Played', type: 'date' },
    { key: 'dateadded', label: 'Date Added', type: 'date' },
    { key: 'datemodified', label: 'Date Modified', type: 'date' },
    { key: 'dateloved', label: 'Date Loved', type: 'date' },
    { key: 'daterated', label: 'Date Rated', type: 'date' },
    { key: 'filepath', label: 'File Path', type: 'string' },
    { key: 'filetype', label: 'File Type', type: 'string' },
    { key: 'library_id', label: 'Library ID', type: 'string' },
    { key: 'id', label: 'Playlist ID', type: 'playlist' }
  ];

  const SPB_SORT_FIELDS = [
    'random', 'title', 'album', 'artist', 'albumartist',
    'year', 'rating', 'playcount', 'lastplayed', 'dateadded',
    'datemodified', 'dateloved', 'daterated',
    'duration', 'bitrate', 'genre', 'mood'
  ];

  const SPB_OPERATORS = {
    string: [
      { key: 'is', label: 'Is exactly' },
      { key: 'isNot', label: 'Is not' },
      { key: 'contains', label: 'Contains' },
      { key: 'notContains', label: 'Does not contain' },
      { key: 'startsWith', label: 'Starts with' },
      { key: 'endsWith', label: 'Ends with' }
    ],
    number: [
      { key: 'is', label: 'Is exactly' },
      { key: 'isNot', label: 'Is not' },
      { key: 'gt', label: 'Is greater than' },
      { key: 'lt', label: 'Is less than' },
      { key: 'inTheRange', label: 'Is in range' }
    ],
    boolean: [
      { key: 'is', label: 'Is' },
      { key: 'isNot', label: 'Is not' }
    ],
    date: [
      { key: 'before', label: 'Before date' },
      { key: 'after', label: 'After date' },
      { key: 'inTheLast', label: 'Within last N days' },
      { key: 'notInTheLast', label: 'Not within last N days' },
      { key: 'inTheRange', label: 'In date range' }
    ],
    playlist: [
      { key: 'inPlaylist', label: 'In playlist' },
      { key: 'notInPlaylist', label: 'Not in playlist' }
    ]
  };

  let spbDraggedItem = null;

  function spbEnableDropContainer(container) {
    if (!container || container.dataset.dndEnabled === '1') return;
    container.dataset.dndEnabled = '1';

    container.addEventListener('dragover', event => {
      event.preventDefault();
      if (!spbDraggedItem) return;

      const afterElement = spbGetDragAfterElement(container, event.clientY);
      if (!afterElement) {
        container.appendChild(spbDraggedItem);
        return;
      }
      container.insertBefore(spbDraggedItem, afterElement);
    });

    container.addEventListener('drop', event => {
      event.preventDefault();
      spbUpdatePreview();
    });
  }

  function spbGetDragAfterElement(container, mouseY) {
    const draggableElements = [...container.querySelectorAll(':scope > .spb-item:not(.spb-dragging)')];

    let closest = { offset: Number.NEGATIVE_INFINITY, element: null };
    draggableElements.forEach(element => {
      const box = element.getBoundingClientRect();
      const offset = mouseY - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) {
        closest = { offset, element };
      }
    });

    return closest.element;
  }

  function spbEnableDragItem(item) {
    if (!item || item.dataset.dragEnabled === '1') return;
    item.dataset.dragEnabled = '1';
    item.setAttribute('draggable', 'true');

    item.addEventListener('dragstart', event => {
      spbDraggedItem = item;
      item.classList.add('spb-dragging');
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', 'spb-item');
      }
    });

    item.addEventListener('dragend', () => {
      item.classList.remove('spb-dragging');
      spbDraggedItem = null;
      spbUpdatePreview();
    });
  }

  const SPB_PRESETS = {
    recently_played: {
      fileName: 'recently-played',
      playlist: {
        name: 'Recently Played',
        comment: 'Tracks played in the last 30 days',
        all: [{ inTheLast: { lastplayed: 30 } }],
        sort: 'lastplayed',
        order: 'desc',
        limit: 100
      }
    },
    never_played: {
      fileName: 'never-played',
      playlist: {
        name: 'Never Played',
        comment: 'Tracks with zero play count',
        all: [{ is: { playcount: 0 } }],
        sort: 'random',
        limit: 200
      }
    },
    loved_tracks: {
      fileName: 'loved-tracks',
      playlist: {
        name: 'Loved Tracks',
        comment: 'Tracks marked as loved',
        all: [{ is: { loved: true } }],
        sort: 'dateloved',
        order: 'desc',
        limit: 500
      }
    },
    high_quality_flac: {
      fileName: 'high-quality-flac',
      playlist: {
        name: 'High Quality FLAC',
        comment: 'Lossless tracks with high bitrate',
        all: [{ is: { filetype: 'flac' } }, { gt: { bitrate: 900 } }],
        sort: 'random',
        limit: 200
      }
    },
    mood_energetic: {
      fileName: 'mood-energetic',
      playlist: {
        name: 'Energetic Mood',
        comment: 'Tracks with energetic mood tags',
        any: [
          { contains: { mood: 'energetic' } },
          { contains: { mood: 'upbeat' } },
          { contains: { mood: 'driving' } }
        ],
        sort: 'random',
        limit: 200
      }
    }
  };

  function spbAddRule(prefill = null, container = null) {
    const targetContainer = container || document.getElementById('spbRulesContainer');
    if (!targetContainer) return;

  	spbEnableDropContainer(targetContainer);

    const row = document.createElement('div');
    row.className = 'border rounded p-2 spb-rule-row spb-item';
    row.innerHTML = `
      <div class="row g-2 align-items-end">
        <div class="col-md-4">
          <label class="form-label mb-1 small">Field</label>
          <select class="form-select form-select-sm spb-field">${spbFieldSelectHtml(prefill?.field || '')}</select>
        </div>
        <div class="col-md-3">
          <label class="form-label mb-1 small">Operator</label>
          <select class="form-select form-select-sm spb-operator"><option value="">Operator</option></select>
        </div>
        <div class="col-md-4">
          <label class="form-label mb-1 small">Value</label>
          <div class="spb-value-wrap"></div>
        </div>
        <div class="col-md-1 d-grid">
          <button type="button" class="btn btn-sm btn-outline-danger spb-remove-rule">X</button>
        </div>
      </div>
    `;
    targetContainer.appendChild(row);

    const fieldEl = row.querySelector('.spb-field');
    const opEl = row.querySelector('.spb-operator');

    fieldEl.addEventListener('change', () => {
      spbUpdateOperatorOptions(row);
      spbUpdateValueInput(row);
      spbUpdatePreview();
    });

    opEl.addEventListener('change', () => {
      spbUpdateValueInput(row);
      spbUpdatePreview();
    });

    row.querySelector('.spb-remove-rule').addEventListener('click', () => {
      row.remove();
      spbUpdatePreview();
    });

    row.addEventListener('input', spbUpdatePreview);
    row.addEventListener('change', spbUpdatePreview);

  	spbEnableDragItem(row);
    spbUpdateOperatorOptions(row, prefill?.operator);
    spbUpdateValueInput(row, prefill?.value);
  }

  function spbAddGroup(prefill = null, container = null) {
    const targetContainer = container || document.getElementById('spbRulesContainer');
    if (!targetContainer) return;

  	spbEnableDropContainer(targetContainer);

    const group = document.createElement('div');
    group.className = 'border border-secondary rounded p-2 spb-group-item spb-item';
    group.innerHTML = `
      <div class="d-flex justify-content-between align-items-center mb-2">
        <div class="d-flex align-items-center gap-2">
          <span class="badge bg-secondary">Group</span>
          <select class="form-select form-select-sm spb-group-logic" style="width: 180px;">
            <option value="all">ALL rules must match</option>
            <option value="any">ANY rule can match</option>
          </select>
        </div>
        <button type="button" class="btn btn-sm btn-outline-danger spb-remove-group">Remove Group</button>
      </div>
      <div class="spb-group-rules d-flex flex-column gap-2 mb-2"></div>
      <div class="d-flex gap-2">
        <button type="button" class="btn btn-sm btn-outline-primary spb-group-add-rule">Add Rule</button>
        <button type="button" class="btn btn-sm btn-outline-dark spb-group-add-group">Add Subgroup</button>
      </div>
    `;
    targetContainer.appendChild(group);

    if (prefill?.logic === 'any') {
      group.querySelector('.spb-group-logic').value = 'any';
    }

    const groupRules = group.querySelector('.spb-group-rules');
    group.querySelector('.spb-group-add-rule').addEventListener('click', () => {
      spbAddRule(null, groupRules);
      spbUpdatePreview();
    });
    group.querySelector('.spb-group-add-group').addEventListener('click', () => {
      spbAddGroup(null, groupRules);
      spbUpdatePreview();
    });
    group.querySelector('.spb-remove-group').addEventListener('click', () => {
      group.remove();
      spbUpdatePreview();
    });
    group.querySelector('.spb-group-logic').addEventListener('change', spbUpdatePreview);

  	spbEnableDragItem(group);

    if (Array.isArray(prefill?.conditions) && prefill.conditions.length > 0) {
      spbRenderConditions(groupRules, prefill.conditions);
    } else {
      spbAddRule(null, groupRules);
    }
  }

  function spbUpdateOperatorOptions(row, preselected = '') {
    const fieldEl = row.querySelector('.spb-field');
    const opEl = row.querySelector('.spb-operator');
    const field = spbGetField(fieldEl.value);
    const operators = field ? SPB_OPERATORS[field.type] || SPB_OPERATORS.string : [];

    opEl.innerHTML = spbCreateSelectOptions(operators, 'Operator');
    if (preselected) {
      opEl.value = preselected;
    }
  }

  function spbUpdateValueInput(row, prefillValue = null) {
    const fieldEl = row.querySelector('.spb-field');
    const opEl = row.querySelector('.spb-operator');
    const wrap = row.querySelector('.spb-value-wrap');
    const field = spbGetField(fieldEl.value);
    const operator = opEl.value;

    if (!field || !operator) {
      wrap.innerHTML = '<input type="text" class="form-control form-control-sm spb-value" placeholder="Value" disabled>';
      return;
    }

    if (field.type === 'boolean') {
      wrap.innerHTML = `
        <select class="form-select form-select-sm spb-value-bool">
          <option value="true">True</option>
          <option value="false">False</option>
        </select>
      `;
      if (typeof prefillValue === 'boolean') {
        wrap.querySelector('.spb-value-bool').value = String(prefillValue);
      }
      return;
    }

    if (operator === 'inTheRange') {
      const type = field.type === 'date' ? 'date' : 'number';
      const minVal = Array.isArray(prefillValue) ? (prefillValue[0] ?? '') : '';
      const maxVal = Array.isArray(prefillValue) ? (prefillValue[1] ?? '') : '';
      wrap.innerHTML = `
        <div class="input-group input-group-sm">
          <input type="${type}" class="form-control spb-value-min" placeholder="Min" value="${minVal}">
          <span class="input-group-text">to</span>
          <input type="${type}" class="form-control spb-value-max" placeholder="Max" value="${maxVal}">
        </div>
      `;
      return;
    }

    if (operator === 'inTheLast' || operator === 'notInTheLast') {
      const daysVal = typeof prefillValue === 'number' ? prefillValue : '';
      wrap.innerHTML = `<input type="number" min="0" class="form-control form-control-sm spb-value-days" placeholder="Days" value="${daysVal}">`;
      return;
    }

    if (field.type === 'number') {
      const val = typeof prefillValue === 'number' ? prefillValue : '';
      wrap.innerHTML = `<input type="number" class="form-control form-control-sm spb-value" placeholder="Number" value="${val}">`;
      return;
    }

    if (field.type === 'date') {
      const val = typeof prefillValue === 'string' ? prefillValue : '';
      wrap.innerHTML = `<input type="date" class="form-control form-control-sm spb-value" value="${val}">`;
      return;
    }

    const val = typeof prefillValue === 'string' ? prefillValue : '';
    const placeholder = field.type === 'playlist' ? 'Playlist ID' : 'Text value';
    wrap.innerHTML = `<input type="text" class="form-control form-control-sm spb-value" placeholder="${placeholder}" value="${escapeHtml(val)}">`;
  }

  function spbAddSort(prefill = null) {
    const container = document.getElementById('spbSortContainer');
    if (!container) return;

    const row = document.createElement('div');
    row.className = 'border rounded p-2 spb-sort-row';
    row.innerHTML = `
      <div class="row g-2 align-items-end">
        <div class="col-7">
          <label class="form-label mb-1 small">Field</label>
          <select class="form-select form-select-sm spb-sort-field">${spbSortFieldSelectHtml(prefill?.field || '')}</select>
        </div>
        <div class="col-4">
          <label class="form-label mb-1 small">Direction</label>
          <select class="form-select form-select-sm spb-sort-direction">
            <option value="asc">Ascending</option>
            <option value="desc">Descending</option>
          </select>
        </div>
        <div class="col-1 d-grid">
          <button type="button" class="btn btn-sm btn-outline-danger spb-remove-sort">X</button>
        </div>
      </div>
    `;

    container.appendChild(row);
    if (prefill?.direction) {
      row.querySelector('.spb-sort-direction').value = prefill.direction;
    }

    row.querySelector('.spb-remove-sort').addEventListener('click', () => {
      row.remove();
      spbUpdatePreview();
    });
    row.addEventListener('change', spbUpdatePreview);
  }

  function spbParseValue(field, operator, wrap) {
    if (field.type === 'boolean') {
      const boolEl = wrap.querySelector('.spb-value-bool');
      return boolEl ? boolEl.value === 'true' : null;
    }

    if (operator === 'inTheRange') {
      const minEl = wrap.querySelector('.spb-value-min');
      const maxEl = wrap.querySelector('.spb-value-max');
      if (!minEl || !maxEl || minEl.value === '' || maxEl.value === '') return null;
      if (field.type === 'number') {
        return [Number(minEl.value), Number(maxEl.value)];
      }
      return [minEl.value, maxEl.value];
    }

    if (operator === 'inTheLast' || operator === 'notInTheLast') {
      const daysEl = wrap.querySelector('.spb-value-days');
      if (!daysEl || daysEl.value === '') return null;
      return Number(daysEl.value);
    }

    const valueEl = wrap.querySelector('.spb-value');
    if (!valueEl || valueEl.value === '') return null;
    if (field.type === 'number') return Number(valueEl.value);
    return valueEl.value;
  }

  function spbCollectRules() {
    return spbCollectRulesFromContainer(document.getElementById('spbRulesContainer'));
  }

  function spbCollectRulesFromContainer(container) {
    const rules = [];
    if (!container) return rules;

    Array.from(container.children).forEach(item => {
      if (item.classList.contains('spb-rule-row')) {
        const fieldKey = item.querySelector('.spb-field')?.value;
        const operator = item.querySelector('.spb-operator')?.value;
        const field = spbGetField(fieldKey);
        const wrap = item.querySelector('.spb-value-wrap');
        if (!field || !operator || !wrap) return;

        const value = spbParseValue(field, operator, wrap);
        if (value === null || Number.isNaN(value)) return;

        const cond = {};
        cond[operator] = {};
        cond[operator][field.key] = value;
        rules.push(cond);
        return;
      }

      if (item.classList.contains('spb-group-item')) {
        const logic = item.querySelector('.spb-group-logic')?.value || 'all';
        const groupRules = item.querySelector('.spb-group-rules');
        const conditions = spbCollectRulesFromContainer(groupRules);
        if (conditions.length > 0) {
          const nested = {};
          nested[logic] = conditions;
          rules.push(nested);
        }
      }
    });

    return rules;
  }

  function spbRenderConditions(container, conditions) {
    if (!container || !Array.isArray(conditions)) return;
    conditions.forEach(condition => {
      const key = Object.keys(condition || {})[0];
      if (!key) return;

      if ((key === 'all' || key === 'any') && Array.isArray(condition[key])) {
        spbAddGroup({ logic: key, conditions: condition[key] }, container);
        return;
      }

      const payload = condition[key] || {};
      const field = Object.keys(payload)[0];
      if (!field) return;
      spbAddRule({ field, operator: key, value: payload[field] }, container);
    });
  }

  function spbCollectSorts() {
    const sorts = [];
    document.querySelectorAll('#spbSortContainer .spb-sort-row').forEach(row => {
      const field = row.querySelector('.spb-sort-field')?.value;
      const direction = row.querySelector('.spb-sort-direction')?.value || 'asc';
      if (!field) return;
      sorts.push({ field, direction });
    });
    return sorts;
  }

  function spbBuildJson() {
    const name = (document.getElementById('spbPlaylistName')?.value || '').trim();
    const comment = (document.getElementById('spbComment')?.value || '').trim();
    const logic = document.getElementById('spbLogic')?.value || 'all';
    const limitRaw = (document.getElementById('spbLimit')?.value || '').trim();
    const rules = spbCollectRules();
    const sorts = spbCollectSorts();

    const json = {};
    if (name) json.name = name;
    if (comment) json.comment = comment;
    if (rules.length > 0) json[logic] = rules;

    if (sorts.length === 1) {
      json.sort = sorts[0].field;
      if (sorts[0].field !== 'random') {
        json.order = sorts[0].direction;
      }
    } else if (sorts.length > 1) {
      json.sort = sorts.map(s => `${s.direction === 'desc' ? '-' : '+'}${s.field}`).join(',');
    }

    if (limitRaw) {
      const limit = Number(limitRaw);
      if (!Number.isNaN(limit) && limit > 0) {
        json.limit = limit;
      }
    }

    return json;
  }

  function spbUpdatePreview() {
    const preview = document.getElementById('spbJsonPreview');
    if (!preview) return;
    preview.textContent = JSON.stringify(spbBuildJson(), null, 2);
  }

  function spbResetBuilder() {
    const form = document.getElementById('smartPlaylistBuilderForm');
    if (!form) return;
    form.reset();
    document.getElementById('spbRulesContainer').innerHTML = '';
    document.getElementById('spbSortContainer').innerHTML = '';
    spbAddRule();
    spbAddSort({ field: 'random', direction: 'asc' });
    spbUpdatePreview();
  }

  function spbApplyPreset(key) {
    const preset = SPB_PRESETS[key];
    if (!preset) return;

    document.getElementById('spbPlaylistName').value = preset.playlist.name || '';
    document.getElementById('spbFileName').value = preset.fileName || '';
    document.getElementById('spbComment').value = preset.playlist.comment || '';

    const logic = preset.playlist.any ? 'any' : 'all';
    document.getElementById('spbLogic').value = logic;

    const rules = preset.playlist[logic] || [];
    const rulesContainer = document.getElementById('spbRulesContainer');
    rulesContainer.innerHTML = '';
    spbRenderConditions(rulesContainer, rules);
    if (rules.length === 0) spbAddRule();

    const sortsContainer = document.getElementById('spbSortContainer');
    sortsContainer.innerHTML = '';
    const sort = preset.playlist.sort;
    if (sort) {
      if (typeof sort === 'string' && sort.includes(',')) {
        sort.split(',').forEach(part => {
          const trimmed = part.trim();
          if (!trimmed) return;
          const direction = trimmed.startsWith('-') ? 'desc' : 'asc';
          const field = trimmed.replace(/^[-+]/, '');
          spbAddSort({ field, direction });
        });
      } else {
        spbAddSort({ field: sort, direction: preset.playlist.order || 'asc' });
      }
    } else {
      spbAddSort({ field: 'random', direction: 'asc' });
    }

    document.getElementById('spbLimit').value = preset.playlist.limit || '';
    spbUpdatePreview();
  }

  function spbCreateGenreMoodPlaylist(field, value) {
    const safe = String(value || '').trim();
    if (!safe) return;
    const slug = safe.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

    document.getElementById('spbPlaylistName').value = `${safe} ${field === 'genre' ? 'Genre' : 'Mood'} Mix`;
    document.getElementById('spbFileName').value = `${field}-${slug}`;
    document.getElementById('spbComment').value = `Auto-generated ${field} playlist for ${safe}`;
    document.getElementById('spbLogic').value = 'all';

    const rulesContainer = document.getElementById('spbRulesContainer');
    rulesContainer.innerHTML = '';
    spbAddRule({ field, operator: 'contains', value: safe });

    const sortContainer = document.getElementById('spbSortContainer');
    sortContainer.innerHTML = '';
    spbAddSort({ field: 'random', direction: 'asc' });
    document.getElementById('spbLimit').value = 200;

    spbUpdatePreview();
  }

  async function spbSubmitBuilder(event) {
    event.preventDefault();

    const fileName = (document.getElementById('spbFileName')?.value || '').trim();
    const playlist = spbBuildJson();
    const logic = document.getElementById('spbLogic')?.value || 'all';
    const conditions = Array.isArray(playlist[logic]) ? playlist[logic] : [];
    if (!fileName) {
      alert('File name is required');
      return;
    }
    if (!playlist.name) {
      alert('Playlist name is required');
      return;
    }
    if (conditions.length === 0) {
      alert('Add at least one valid rule before creating a smart playlist');
      return;
    }

    try {
      const response = await fetch('/api/smartplaylist/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fileName, playlist })
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || 'Failed to create smart playlist');
      }
      alert(`Smart playlist created: ${data.file_name || fileName + '.nsp'}`);
      spbResetBuilder();
      loadSmartPlaylists();
    } catch (error) {
      alert(`Error creating smart playlist: ${error.message}`);
    }
  }

  function spbCopyJson() {
    const preview = document.getElementById('spbJsonPreview');
    if (!preview) return;
    navigator.clipboard.writeText(preview.textContent || '{}')
      .then(() => alert('JSON copied to clipboard'))
      .catch(() => alert('Could not copy JSON'));
  }

  function initSmartPlaylistBuilder() {
    const form = document.getElementById('smartPlaylistBuilderForm');
    if (!form || form.dataset.initialized === '1') return;
    form.dataset.initialized = '1';

    document.getElementById('spbAddRuleBtn')?.addEventListener('click', () => {
      spbAddRule();
      spbUpdatePreview();
    });

    document.getElementById('spbAddGroupBtn')?.addEventListener('click', () => {
      spbAddGroup();
      spbUpdatePreview();
    });

    document.getElementById('spbAddSortBtn')?.addEventListener('click', () => {
      spbAddSort();
      spbUpdatePreview();
    });

    document.getElementById('spbPreset')?.addEventListener('change', e => {
      if (e.target.value) {
        spbApplyPreset(e.target.value);
        e.target.value = '';
      }
    });

    document.getElementById('spbCopyJsonBtn')?.addEventListener('click', spbCopyJson);
    document.getElementById('spbResetBtn')?.addEventListener('click', spbResetBuilder);
    form.addEventListener('submit', spbSubmitBuilder);
    form.addEventListener('input', spbUpdatePreview);
    form.addEventListener('change', spbUpdatePreview);

    spbResetBuilder();
  }


  // ── Import results ────────────────────────────────────────────────────


  function displayImportResults(data) {
    const matchedCount = data.matched_tracks ? data.matched_tracks.length : 0;
    const missingCount = data.missing_tracks ? data.missing_tracks.length : 0;
    const totalCount = matchedCount + missingCount;
    const coverage = totalCount > 0 ? Math.round((matchedCount / totalCount) * 100) : 0;
  
    const matchedCountEl = document.getElementById('matchedCount');
    const missingCountEl = document.getElementById('missingCount');
    const totalCountEl = document.getElementById('totalCount');
    const coverageEl = document.getElementById('coverage');
  
    if (matchedCountEl) matchedCountEl.textContent = matchedCount;
    if (missingCountEl) missingCountEl.textContent = missingCount;
    if (totalCountEl) totalCountEl.textContent = totalCount;
    if (coverageEl) coverageEl.textContent = coverage + '%';
  
    const matchedContainer = document.getElementById('matchedTracksContainer');
    if (matchedContainer) {
      if (matchedCount > 0) {
        matchedContainer.innerHTML = data.matched_tracks.map((track, idx) => `
          <div class="track-row">
            <div class="track-info">
              <div class="track-title">${escapeHtml(track.title)}</div>
              <div class="track-artist">${escapeHtml(track.artist)}</div>
              <div class="track-album">${escapeHtml(track.album)}</div>
            </div>
            <div class="track-actions">
              <span class="badge bg-success">✓ Found</span>
            </div>
          </div>
        `).join('');
      } else {
        matchedContainer.innerHTML = '<p class="text-secondary text-center py-5">No matched tracks found</p>';
      }
    }
  
    const missingContainer = document.getElementById('missingTracksContainer');
    if (missingContainer) {
      if (missingCount > 0) {
        missingContainer.innerHTML = data.missing_tracks.map((track, idx) => `
          <div class="track-row">
            <div class="track-info">
              <div class="track-title">${escapeHtml(track.title)}</div>
              <div class="track-artist">${escapeHtml(track.artist)}</div>
              <div class="track-album">${escapeHtml(track.album || 'Unknown Album')}</div>
            </div>
            <div class="track-actions">
              <span class="badge bg-warning">✗ Missing</span>
            </div>
          </div>
        `).join('');
      } else {
        missingContainer.innerHTML = '<p class="text-success text-center py-5">All tracks found in library!</p>';
      }
    }
  
    const resultsSection = document.getElementById('resultsSection');
    if (resultsSection) resultsSection.style.display = 'block';
  
    const errorSection = document.getElementById('errorSection');
    if (errorSection) errorSection.style.display = 'none';
  }

  /**
   * Create a Navidrome playlist from the current CSV import.
   *
   * Reads `currentImportData`, which state/import-state.js owns and
   * playlist_import.js writes — see the header note. Before that split, both
   * files declared the binding and the second to load died at parse time.
   */
  async function createPlaylist() {
    if (!currentImportData) return;

    const createBtn = document.getElementById('createPlaylistBtn');
    if (!createBtn) return;

    const targetUser = (
      currentImportData.target_user
      || document.getElementById('playlistTargetUser')?.value
      || ''
    ).trim();

    // withBusy restores the button in a finally. The original restored it only
    // in the catch, so a SUCCESSFUL create left it disabled showing
    // "Playlist Created!" with no way back without a reload — and a thrown
    // error before the fetch left it spinning forever.
    return window.buttonState.withBusy(createBtn, 'Creating…', async () => {
      try {
        const data = await window.api.postJson('/api/playlist/create-custom', {
          name: currentImportData.playlist_name,
          description: currentImportData.playlist_description,
          user: targetUser,
          is_public: true,
          songs: currentImportData.matched_tracks,
        });

        const ownerText = data.target_user ? ` for ${data.target_user}` : '';
        notifySuccess(`Playlist "${currentImportData.playlist_name}" created${ownerText}`);

        window.buttonState.setDone(createBtn, {
          label: 'Playlist Created',
          fromClass: 'btn-primary',
        });
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }


  // ── Last.fm recommendations ───────────────────────────────────────────


  async function loadLastfmRecommendations() {
    const recType = document.getElementById('lfmRecType')?.value || 'tracks';
  
    console.log(`[Last.fm Playlist] Loading ${recType} recommendations...`);
  
    try {
      const lfmRecommendationsEmpty = document.getElementById('lfmRecommendationsEmpty');
      const lfmRecommendationsResults = document.getElementById('lfmRecommendationsResults');
    
      if (lfmRecommendationsEmpty) lfmRecommendationsEmpty.style.display = 'none';
      if (lfmRecommendationsResults) lfmRecommendationsResults.style.display = 'block';
    
      document.getElementById('lfmTotalCount').textContent = '...';
      document.getElementById('lfmMatchedCount').textContent = '...';
      document.getElementById('lfmMissingCount').textContent = '...';
    
      const response = await fetch(`/api/lastfm/create-playlist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: recType })
      });
    
      console.log(`[Last.fm Playlist] API Response status: ${response.status} ${response.statusText}`);
    
      if (!response.ok) {
        const error = await response.json();
        console.error('[Last.fm Playlist] API Error:', error);
        throw new Error(error.error || `API error: ${response.status} ${response.statusText}`);
      }
    
      const data = await response.json();
      console.log('[Last.fm Playlist] API Data:', data);
    
      // Check if recommendations are actually empty
      if (!data.matched_tracks && !data.missing_tracks) {
        console.warn('[Last.fm Playlist] API returned no recommendations at all. This indicates:');
        console.warn('  1. Last.fm account has no scrobbling history, or');
        console.warn('  2. API key is invalid, or');
        console.warn('  3. Last.fm username is not configured in your profile');
      }
    
      lfmRecommendationsData = data;
    
      document.getElementById('lfmTotalCount').textContent = data.total_recommendations || 0;
      document.getElementById('lfmMatchedCount').textContent = data.matched || 0;
      document.getElementById('lfmMissingCount').textContent = data.missing || 0;
    
      const matchedHtml = (data.matched_tracks || []).map(track => `
        <div class="card mb-2 border-success">
          <div class="card-body py-2">
            <div class="d-flex justify-content-between align-items-start">
              <div>
                <strong class="d-block">${escapeHtml(track.title || 'Unknown')}</strong>
                <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')}</small>
              </div>
              <span class="badge bg-success">✓</span>
            </div>
          </div>
        </div>
      `).join('');
      document.getElementById('lfmMatchedTracks').innerHTML = matchedHtml || '<p class="text-muted text-center py-5">No matched tracks found</p>';
    
      const missingHtml = (data.missing_tracks || []).map(track => `
        <div class="card mb-2 border-warning">
          <div class="card-body py-2">
            <div class="d-flex justify-content-between align-items-start">
              <div>
                <strong class="d-block">${escapeHtml(track.title || 'Unknown')}</strong>
                <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')}</small>
                ${track.playcount ? `<br><small class="text-secondary">Playcount: ${track.playcount.toLocaleString()}</small>` : ''}
              </div>
              <span class="badge bg-warning">Missing</span>
            </div>
          </div>
        </div>
      `).join('');
      document.getElementById('lfmMissingTracks').innerHTML = missingHtml || '<p class="text-muted text-center py-5">All tracks are in your library!</p>';
    
    } catch (error) {
      console.error('Error loading Last.fm recommendations:', error);
      alert('Error: ' + error.message);
      const lfmRecommendationsResults = document.getElementById('lfmRecommendationsResults');
      const lfmRecommendationsEmpty = document.getElementById('lfmRecommendationsEmpty');
      if (lfmRecommendationsResults) lfmRecommendationsResults.style.display = 'none';
      if (lfmRecommendationsEmpty) lfmRecommendationsEmpty.style.display = 'block';
    }
  }

  async function createPlaylistFromLastfm(event) {
    if (event) event.preventDefault();
  
    if (!lfmRecommendationsData || !lfmRecommendationsData.matched_tracks || lfmRecommendationsData.matched_tracks.length === 0) {
      alert('No matched tracks to create playlist from');
      return;
    }
  
    const playlistName = document.getElementById('lfmPlaylistName')?.value?.trim();
    const playlistDesc = document.getElementById('lfmPlaylistDesc')?.value?.trim() || '';
    const playlistUser = document.getElementById('lfmPlaylistUser')?.value?.trim();
    const isPublic = document.getElementById('lfmPlaylistPublic')?.checked || false;
  
    if (!playlistName) {
      alert('Please enter a playlist name');
      return;
    }
  
    if (!playlistUser) {
      alert('Please select a user');
      return;
    }
  
    try {
      const response = await fetch('/api/lastfm/create-playlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: playlistName,
          description: playlistDesc || `Last.fm recommendations`,
          user: playlistUser,
          is_public: isPublic,
          songs: lfmRecommendationsData.matched_tracks
        })
      });
    
      const data = await response.json();
    
      if (!response.ok) {
        throw new Error(data.error || 'Failed to create playlist');
      }
    
      alert('✓ Playlist created successfully!');
      resetLastfmForm();
    } catch (error) {
      console.error('Error creating playlist:', error);
      alert('✗ Error: ' + error.message);
    }
  }

  // ===============================


  // =============================================================================
  // LISTENBRAINZ RSS PLAYLISTS
  // =============================================================================

  // ── ListenBrainz queue badge ──────────────────────────────────────────

  /**
   * Badge for a ListenBrainz recommendation row.
   *
   * Not routed through ui/status-badge.js: this combines TWO server fields
   * (match_status and queue_status) plus a queue_id presence check into one
   * label, and "matched" here means "already in your library" rather than a
   * queue state. The vocabularies only partly overlap.
   */
  function lbQueueStatusBadge(track) {
    const matchStatus = track.match_status;
    const queueStatus = track.queue_status;
    const queueId = track.queue_id;

    if (matchStatus === 'matched') {
      return '<span class="badge bg-success ms-1" title="In your library">' +
        '<i class="bi bi-check-circle-fill"></i> Matched</span>';
    }

    if (matchStatus === 'queued' || queueId) {
      const label = queueStatus || 'queued';
      let cls = 'bg-secondary';
      if (label === 'downloading') cls = 'bg-info text-dark';
      else if (label === 'completed' || label === 'imported') cls = 'bg-success';
      else if (label === 'failed') cls = 'bg-danger';
      else if (label === 'searching') cls = 'bg-warning text-dark';

      // ESCAPED: queue_failure_reason is a server-side error string and went
      // into this attribute raw, so one containing a double quote closed the
      // attribute early.
      const failHint = track.queue_failure_reason
        ? ` title="${escapeHtml(track.queue_failure_reason)}"`
        : '';
      return `<span class="badge ${cls} ms-1"${failHint}>` +
        `<i class="bi bi-download"></i> ${escapeHtml(label)}</span>`;
    }

    return '<span class="badge bg-secondary ms-1">missing</span>';
  }

  // Preserved blocks below call it by its original name.
  const _lbQueueStatusBadge = lbQueueStatusBadge;



  // ── ListenBrainz ──────────────────────────────────────────────────────


  function renderListenBrainzPlaylistTables(playlists) {
    const container = document.getElementById('lbRssTables');
    if (!container) return;
    if (!playlists || Object.keys(playlists).length === 0) {
      container.innerHTML = '<div class="p-3 text-muted">No playlist data yet. Click Sync Playlists to fetch.</div>';
      return;
    }
    const specOrder = ['weekly_jams','weekly_exploration','last_week_jams','last_week_exploration','rolling_jams','rolling_exploration'];
    let html = '<div class="accordion accordion-flush" id="lbPlaylistAccordion">';
    specOrder.forEach((key, idx) => {
      const pl = playlists[key];
      if (!pl) return;
      const tracks = pl.tracks || [];
      const matched = tracks.filter(t => t.match_status === 'matched').length;
      const queued = tracks.filter(t => t.match_status === 'queued' || (t.queue_id && t.match_status !== 'matched')).length;
      const missing = tracks.filter(t => t.match_status === 'missing').length;
      const collapseId = `lb-collapse-${key}`;
      html += `
        <div class="accordion-item">
          <h2 class="accordion-header">
            <button class="accordion-button${idx > 0 ? ' collapsed' : ''}" type="button"
              data-bs-toggle="collapse" data-bs-target="#${collapseId}">
              <strong>${pl.name || key}</strong>
              <span class="ms-2 badge bg-success">${matched} matched</span>
              <span class="ms-1 badge bg-warning text-dark">${queued} queued</span>
              <span class="ms-1 badge bg-secondary">${missing} missing</span>
            </button>
          </h2>
          <div id="${collapseId}" class="accordion-collapse collapse${idx === 0 ? ' show' : ''}"
            data-bs-parent="#lbPlaylistAccordion">
            <div class="accordion-body p-0">`;
      if (tracks.length === 0) {
        html += '<p class="text-muted p-3 mb-0">No tracks yet.</p>';
      } else {
        html += `<div class="table-responsive"><table class="table table-sm table-hover mb-0">
          <thead><tr>
            <th>Artist</th><th>Title</th><th>Album</th><th>Status</th>
          </tr></thead><tbody>`;
        tracks.forEach(t => {
          html += `<tr>
            <td>${t.artist || ''}</td>
            <td>${t.title || ''}</td>
            <td class="text-muted small">${t.album || ''}</td>
            <td>${_lbQueueStatusBadge(t)}</td>
          </tr>`;
        });
        html += '</tbody></table></div>';
      }
      html += '</div></div></div>';
    });
    html += '</div>';
    container.innerHTML = html;
  }

  async function loadListenBrainzSyncStatus() {
    const badge = document.getElementById('lbSyncBadge');
    if (!badge) return;
    try {
      const resp = await fetch('/api/listenbrainz/rss/sync-status');
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.last_synced_at) {
        const d = new Date(data.last_synced_at);
        badge.textContent = `Last synced: ${d.toLocaleDateString()} ${d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
      } else {
        badge.textContent = 'Never synced';
      }
      if (data.last_rematch_at) {
        const d2 = new Date(data.last_rematch_at);
        badge.title = `Last re-match check: ${d2.toLocaleDateString()} ${d2.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
      }
    } catch (e) {
      // ignore
    }
  }

  async function loadListenBrainzRssTables() {
    const container = document.getElementById('lbRssTables');
    try {
      const resp = await fetch('/api/listenbrainz/rss/playlists');
      if (!resp.ok) {
        let errorMessage = 'Could not load playlists.';
        try {
          const err = await resp.json();
          if (err && err.error) errorMessage = err.error;
        } catch (_) {
          // ignore json parse errors
        }
        if (container) container.innerHTML = `<div class="p-3 text-muted">${errorMessage}</div>`;
        return;
      }
      const data = await resp.json();
      if (data.playlists) renderListenBrainzPlaylistTables(data.playlists);
      else if (container) container.innerHTML = '<div class="p-3 text-muted">No playlist data returned.</div>';
    } catch (e) {
      if (container) container.innerHTML = '<div class="p-3 text-muted">Could not load playlists.</div>';
    }
  }

  async function syncListenBrainzRssPlaylists(button) {
    // The button used to be located BY ITS OWN INLINE ATTRIBUTE:
    //     document.querySelector('button[onclick="syncListenBrainzRssPlaylists()"]')
    // That only works while the markup keeps that exact attribute string, so
    // it breaks the moment the template moves to data-action — which is the
    // point of this refactor. The caller passes the element when it has one;
    // the [data-action] lookup is the new contract and the onclick lookup is
    // kept last so the not-yet-ported create.html keeps working.
    const btn = button
      || document.querySelector('[data-action="playlist-lb-rss-sync"]')
      || document.querySelector('button[onclick="syncListenBrainzRssPlaylists()"]');
    const usernameInput = document.getElementById('lbRssUsername');
    const lbUsername = usernameInput ? usernameInput.value.trim() : '';
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Syncing...'; }
    try {
      const body = { enqueue_missing: true, write_m3u: true };
      if (lbUsername) body.listenbrainz_username = lbUsername;
      const resp = await fetch('/api/listenbrainz/rss/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (data.playlists) {
        renderListenBrainzPlaylistTables(data.playlists);
        await loadListenBrainzSyncStatus();
      } else if (data.error) {
        alert('Sync failed: ' + data.error);
      }
    } catch (e) {
      alert('Sync error: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-arrow-clockwise"></i> Sync Playlists'; }
    }
  }

  // LISTENBRAINZ RECOMMENDATIONS
  // ===============================

  async function loadListenBrainzRecommendations() {
    const recType = document.getElementById('lbRecType')?.value || 'weekly_jams';
  
    try {
      const lbRecommendationsEmpty = document.getElementById('lbRecommendationsEmpty');
      const lbRecommendationsResults = document.getElementById('lbRecommendationsResults');
    
      if (lbRecommendationsEmpty) lbRecommendationsEmpty.style.display = 'none';
      if (lbRecommendationsResults) lbRecommendationsResults.style.display = 'block';
    
      document.getElementById('lbTotalCount').textContent = '...';
      document.getElementById('lbMatchedCount').textContent = '...';
      document.getElementById('lbMissingCount').textContent = '...';
    
      const response = await fetch(`/api/listenbrainz/create-playlist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: recType })
      });
    
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || 'Failed to load recommendations');
      }
    
      const data = await response.json();
      lbRecommendationsData = data;
    
      document.getElementById('lbTotalCount').textContent = data.total_recommendations || 0;
      document.getElementById('lbMatchedCount').textContent = data.matched || 0;
      document.getElementById('lbMissingCount').textContent = data.missing || 0;
    
      const matchedHtml = (data.matched_tracks || []).map(track => `
        <div class="card mb-2 border-success">
          <div class="card-body py-2">
            <div class="d-flex justify-content-between align-items-start">
              <div>
                <strong class="d-block">${escapeHtml(track.title || 'Unknown')}</strong>
                <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')}</small>
              </div>
              <span class="badge bg-success">✓</span>
            </div>
          </div>
        </div>
      `).join('');
      document.getElementById('lbMatchedTracks').innerHTML = matchedHtml || '<p class="text-muted text-center py-5">No matched tracks found</p>';
    
      const missingHtml = (data.missing_tracks || []).map(track => `
        <div class="card mb-2 border-warning">
          <div class="card-body py-2">
            <div class="d-flex justify-content-between align-items-start">
              <div>
                <strong class="d-block">${escapeHtml(track.title || 'Unknown')}</strong>
                <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')}</small>
                ${track.mbid ? `<br><small class="text-secondary">MBID: ${track.mbid}</small>` : ''}
              </div>
              <span class="badge bg-warning">Missing</span>
            </div>
          </div>
        </div>
      `).join('');
      document.getElementById('lbMissingTracks').innerHTML = missingHtml || '<p class="text-muted text-center py-5">All tracks are in your library!</p>';
    
    } catch (error) {
      console.error('Error loading ListenBrainz recommendations:', error);
      alert('Error: ' + error.message);
      const lbRecommendationsResults = document.getElementById('lbRecommendationsResults');
      const lbRecommendationsEmpty = document.getElementById('lbRecommendationsEmpty');
      if (lbRecommendationsResults) lbRecommendationsResults.style.display = 'none';
      if (lbRecommendationsEmpty) lbRecommendationsEmpty.style.display = 'block';
    }
  }

  async function createPlaylistFromListenBrainz(event) {
    if (event) event.preventDefault();
  
    if (!lbRecommendationsData || !lbRecommendationsData.matched_tracks || lbRecommendationsData.matched_tracks.length === 0) {
      alert('No matched tracks to create playlist from');
      return;
    }
  
    const playlistName = document.getElementById('lbPlaylistName')?.value?.trim();
    const playlistDesc = document.getElementById('lbPlaylistDesc')?.value?.trim() || '';
    const playlistUser = document.getElementById('lbPlaylistUser')?.value?.trim();
    const isPublic = document.getElementById('lbPlaylistPublic')?.checked || false;
  
    if (!playlistName) {
      alert('Please enter a playlist name');
      return;
    }
  
    if (!playlistUser) {
      alert('Please select a user');
      return;
    }
  
    try {
      const response = await fetch('/api/listenbrainz/create-playlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: playlistName,
          description: playlistDesc || `ListenBrainz recommendations`,
          user: playlistUser,
          is_public: isPublic,
          songs: lbRecommendationsData.matched_tracks
        })
      });
    
      const data = await response.json();
    
      if (!response.ok) {
        throw new Error(data.error || 'Failed to create playlist');
      }
    
      alert('✓ Playlist created successfully!');
      resetListenBrainzForm();
    } catch (error) {
      console.error('Error creating playlist:', error);
      alert('✗ Error: ' + error.message);
    }
  }

  // ===============================
  // FORM RESET FUNCTIONS
  // ===============================

  function resetLastfmForm() {
    document.getElementById('lfmPlaylistForm')?.reset?.();
    document.getElementById('lfmPlaylistName').value = '';
    document.getElementById('lfmPlaylistDesc').value = '';
    document.getElementById('lfmPlaylistUser').value = '';
    document.getElementById('lfmPlaylistPublic').checked = true;
  }

  function resetListenBrainzForm() {
    document.getElementById('listenbrainzPlaylistForm')?.reset?.();
    document.getElementById('lbPlaylistName').value = '';
    document.getElementById('lbPlaylistDesc').value = '';
    document.getElementById('lbPlaylistUser').value = '';
    document.getElementById('lbPlaylistPublic').checked = true;
  }



  // ── Init ──────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    // Bootstrap tabs
    document.querySelectorAll('button[data-bs-toggle="tab"]').forEach((el) => {
      if (global.bootstrap && global.bootstrap.Tab) {
        global.bootstrap.Tab.getOrCreateInstance(el);
      }
    });

    if (document.getElementById('essentialPlaylistSelect')
      || document.getElementById('browsePlaylistSelect')
      || document.getElementById('smartPlaylistSelect')) {
      setupBrowsePageListeners();
      loadNavidromePlaylists();
    }

    if (document.getElementById('playlistFileSelect')) {
      loadPlaylistList();
    }

    setupCreatePageListeners();

    if (document.getElementById('smartPlaylistsList')) {
      loadSmartPlaylists();
    }
  });

  global.playlistManager = {
    loadNavidromePlaylists,
    loadNavidromePlaylistDetail,
    loadPlaylistList,
    loadPlaylistForDownload,
    loadSmartPlaylists,
    formatDuration,
    lbQueueStatusBadge,
  };

  // Globals for the inline onclick handlers in the playlist templates.
  global.loadNavidromePlaylists = loadNavidromePlaylists;
  global.loadNavidromePlaylistDetail = loadNavidromePlaylistDetail;
  global.editPlaylist = editPlaylist;
  global.refreshPlaylistDetails = refreshPlaylistDetails;
  global.loadPlaylistList = loadPlaylistList;
  global.loadPlaylistForDownload = loadPlaylistForDownload;
  global.replacePlaylistMatch = replacePlaylistMatch;
  global.downloadPlaylistMatches = downloadPlaylistMatches;
  global.searchSongs = searchSongs;
  global.addSongToPlaylistObj = addSongToPlaylistObj;
  global.removeSongFromPlaylist = removeSongFromPlaylist;
  global.createCustomPlaylist = createCustomPlaylist;
  global.loadSmartPlaylists = loadSmartPlaylists;
  global.loadSmartPlaylistDetail = loadSmartPlaylistDetail;
  global.refreshSmartPlaylistDetail = refreshSmartPlaylistDetail;
  global.createPlaylist = createPlaylist;
  global.displayImportResults = displayImportResults;
  global.loadLastfmRecommendations = loadLastfmRecommendations;
  global.createPlaylistFromLastfm = createPlaylistFromLastfm;
  global.loadListenBrainzRecommendations = loadListenBrainzRecommendations;
  global.createPlaylistFromListenBrainz = createPlaylistFromListenBrainz;
  global.loadListenBrainzRssTables = loadListenBrainzRssTables;
  global.syncListenBrainzRssPlaylists = syncListenBrainzRssPlaylists;
  global.loadListenBrainzSyncStatus = loadListenBrainzSyncStatus;
  global.resetLastfmForm = resetLastfmForm;
  global.resetListenBrainzForm = resetListenBrainzForm;

  // Smart-playlist builder entry points (preserved block).
  global.spbAddRule = spbAddRule;
  global.spbAddGroup = spbAddGroup;
  global.spbAddSort = spbAddSort;
  global.spbResetBuilder = spbResetBuilder;
  global.spbApplyPreset = spbApplyPreset;
  global.spbCopyJson = spbCopyJson;
  global.spbCreateGenreMoodPlaylist = spbCreateGenreMoodPlaylist;
  global.initSmartPlaylistBuilder = initSmartPlaylistBuilder;
})(window);
