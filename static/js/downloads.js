// ===== DOWNLOAD PAGES JAVASCRIPT =====
// Central logic for downloading, queue management, and external API searches.

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

async function fetchJsonOrThrow(url, options = {}, timeoutMs = 30000) {
  const controller = new AbortController();
  const externalSignal = options?.signal || null;
  const mergedOptions = { ...options };
  const onExternalAbort = () => controller.abort();

  if (externalSignal && typeof AbortSignal.any === 'function') {
    mergedOptions.signal = AbortSignal.any([controller.signal, externalSignal]);
  } else if (externalSignal) {
    externalSignal.addEventListener('abort', onExternalAbort, { once: true });
    mergedOptions.signal = controller.signal;
  } else {
    mergedOptions.signal = controller.signal;
  }

  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  let raw;
  try {
    response = await fetch(url, mergedOptions);
    raw = await response.text();
  } catch (error) {
    if (error?.name === 'AbortError') {
      if (externalSignal?.aborted) throw error;
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
    if (externalSignal && typeof AbortSignal.any !== 'function') {
      externalSignal.removeEventListener('abort', onExternalAbort);
    }
  }

  if ([524, 504, 502].includes(response.status)) {
    throw new Error(`Connection timed out (HTTP ${response.status}). The server took too long to respond.`);
  }

  let data = null;
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (_) {
    const contentType = (response.headers.get('content-type') || '').toLowerCase();
    if (contentType.includes('text/html') || raw?.trim().startsWith('<!DOCTYPE') || raw?.trim().startsWith('<html')) {
      throw new Error(`Server returned HTML instead of JSON (HTTP ${response.status}).`);
    }
    throw new Error(`Server returned non-JSON response (HTTP ${response.status}).`);
  }

  if (!response.ok) {
    const serverMsg = data && data.error ? data.error : response.statusText;
    throw new Error(`HTTP ${response.status}: ${serverMsg || 'Request failed'}`);
  }
  return data;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Escapes a string for interpolation into an inline onclick="" attribute.
//
// This was previously defined ONLY in album_detail.js, while being CALLED from
// searchMBForOrganize / searchDiscogsForOrganize here — so those two threw
// ReferenceError on every page that loaded downloads.js without
// album_detail.js (the dashboard and the downloads pages).
//
// It also now escapes backslashes. The old copy handled only ' and ", so an
// album title containing a backslash still broke the generated handler.
function escapeJsString(str) {
  return String(str || '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/"/g, '\\"');
}

// confirmOrganizeGroup() calls this three times. It was defined NOWHERE in
// either file, so a successful organize threw instead of reporting its result.
// Delegates to the global toast helpers in main.js, with an alert() fallback.
function showToastMsg(message, isError) {
  if (typeof window.showTopToast === 'function') {
    window.showTopToast(message, isError ? 'danger' : 'success');
  } else if (typeof window.showToast === 'function') {
    window.showToast(isError ? 'Error' : 'Success', message, isError ? 'error' : 'success');
  } else {
    alert(message);
  }
}

window.mbDerivedCategory = function (release) {
  const secondary = (release.secondary_types || []).map(s => String(s).toLowerCase());
  const secondaryFirst = ['compilation', 'live', 'remix', 'soundtrack', 'dj-mix', 'mixtape', 'demo', 'spokenword', 'interview', 'audiobook'];
  for (let i = 0; i < secondaryFirst.length; i++) {
    if (secondary.indexOf(secondaryFirst[i]) !== -1) return secondaryFirst[i];
  }
  const pt = String(release.category || release.primary_type || '').toLowerCase();
  return pt || 'other';
};

function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

function formatDuration(rawValue) {
  if (rawValue == null || rawValue === '') return 'Unknown';
  const n = Number(rawValue);
  if (!Number.isFinite(n) || n <= 0) return 'Unknown';
  let seconds = n >= 100000000 ? n / 1000000 : (n > 10000 ? n / 1000 : n);
  seconds = Math.max(0, Math.floor(seconds));

  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  if (hours > 0) return `${hours}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function formatETA(seconds) {
  if (seconds === 8640000 || seconds < 0) return '∞';
  if (seconds === 0) return '–';

  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;

  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function encodeInlineArg(value) {
  return encodeURIComponent(JSON.stringify(value)).replace(/'/g, '%27');
}

function decodeInlineArg(value, fallback = null) {
  try {
    return JSON.parse(decodeURIComponent(value));
  } catch (error) {
    console.warn('Failed to decode inline argument:', error);
    return fallback;
  }
}

function normalizeSoulseekQuery(value) {
  return String(value || '')
    .replace(/\\u0026/gi, ' ')
    .replace(/&amp;/gi, ' ')
    .replace(/&/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// ============================================================================
// LOOKUP FORM HELPERS
// ============================================================================

// NOTE: there is exactly ONE definition of window.doLookup.
//
// The file used to open with a guarded
//     window.doLookup = window.doLookup || function (...)
// and then define it again, unguarded, right here. The `||` guard was
// meaningless — the second assignment always won — and the two bodies were
// not equivalent: only this one falls back to reading the lookup form fields.
window.doLookup = function (artist, album, track, year, callback) {
  if (!artist && !album && !track && !year) {
    artist = document.getElementById('lookupArtist')?.value?.trim() || document.getElementById('mbSearchArtist')?.value?.trim() || '';
    album = document.getElementById('lookupAlbum')?.value?.trim() || document.getElementById('mbSearchAlbum')?.value?.trim() || '';
    track = document.getElementById('lookupTrack')?.value?.trim() || document.getElementById('mbSearchTrack')?.value?.trim() || '';
    year = document.getElementById('lookupYear')?.value?.trim() || document.getElementById('mbSearchYear')?.value?.trim() || '';
  }

  if (!artist && !album && !track && !year) return;

  if (typeof callback === 'function') {
    window._mbSearchCallback = callback;
  } else {
    window._mbSearchCallback = function (selected) {
      if (typeof window.downloadMbRelease === 'function') {
        window.downloadMbRelease(selected.id, selected.title, selected.artist, 'slskd');
      }
    };
  }

  if (typeof window.openGlobalMbSearch === 'function') {
    window.openGlobalMbSearch(artist, album, window._mbSearchCallback, track, year);
  } else {
    console.warn('Global MB Search modal not yet initialized.');
  }
};

window.clearLookup = function () {
  ['lookupArtist', 'lookupAlbum', 'lookupTrack', 'lookupYear', 'mbSearchArtist', 'mbSearchAlbum', 'mbSearchTrack', 'mbSearchYear'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.value = '';
  });
};

// ============================================================================
// MUSICBRAINZ SEARCH & RESULTS CORE
// ============================================================================

window.performMbSearch = async function () {
  let artist = document.getElementById('mbSearchArtist')?.value.trim() || '';
  const album = document.getElementById('mbSearchAlbum')?.value.trim() || '';
  const track = document.getElementById('mbSearchTrack')?.value.trim() || '';
  const year = document.getElementById('mbSearchYear')?.value.trim() || '';

  let query = '';
  if (artist || album || track || year) {
    query = [artist, album, track, year].filter(Boolean).join(' ');
  } else {
    const singleInput = document.getElementById('mbSearchInput');
    query = singleInput?.value.trim() || '';
  }
  if (!query) return;

  let artistOnly = false;
  if (window._mbArtistOnlySearch === true) {
    artistOnly = true;
    window._mbArtistOnlySearch = false;
  } else if (artist && !album && !track && !year) {
    artistOnly = true;
  }
  if (query.toLowerCase().startsWith('artist:')) {
    artistOnly = true;
    artist = query.substring(7).trim();
    query = [artist, album, track, year].filter(Boolean).join(' ');
  }
  window._mbArtistOnlySearch = artistOnly;

  const resultsEl = document.getElementById('mbSearchResults') || document.getElementById('mbResults');
  if (resultsEl) {
    resultsEl.innerHTML = '<div class="text-center mt-4"><div class="spinner-border text-info"></div><p class="mt-2 text-muted">Searching MusicBrainz...</p></div>';
  }

  try {
    const payload = { artist, album, track, year };
    if (!artist && !album && !track && !year) {
      payload.query = query;
    }
    if (artistOnly) payload.artist_only = true;

    const releaseTypeServer = document.getElementById('mbReleaseType')?.value || '';
    if (releaseTypeServer) payload.type = releaseTypeServer;

    if (window._mbSearchIncludeOwned === true) {
      payload.include_owned = true;
      window._mbSearchIncludeOwned = false;
    }
    if (window._mbSearchWithReleases === true) {
      payload.with_releases = true;
      window._mbSearchWithReleases = false;
    }

    const data = await fetchJsonOrThrow('/api/musicbrainz/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (data && data.error) {
      if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle me-1"></i>MusicBrainz search failed: ${escapeHtml(data.error)}</div>`;
      return;
    }

    let releases = data.releases || [];
    const releaseType = document.getElementById('mbReleaseType')?.value || '';
    if (releaseType) {
      const want = releaseType.toLowerCase();
      releases = releases.filter(r => window.mbDerivedCategory(r) === want);
    }

    const limitEl = document.getElementById('mbResultLimit');
    const max = parseInt(limitEl ? limitEl.value : '25', 10) || 25;
    if (releases.length > max) releases = releases.slice(0, max);

    // The component renders a #mbResultCount element that nothing ever
    // populated, so the header count stayed blank on every search.
    const countEl = document.getElementById('mbResultCount');
    if (countEl) countEl.textContent = releases.length ? `${releases.length} result${releases.length === 1 ? '' : 's'}` : '';

    if (releases.length === 0) {
      if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-info"><i class="bi bi-info-circle"></i> No releases found for "${escapeHtml(query)}"</div>`;
      return;
    }

    let html = '<div class="list-group">';
    releases.forEach(release => {
      const coverArt = release.cover_art_url || '';
      const releaseDate = release.first_release_date || 'Unknown';
      const category = release.category || release.primary_type || 'Release';
      const resultArtist = release.artist || (release['artist-credit']?.[0]?.name) || 'Unknown Artist';

      const imgHtml = coverArt
        ? `<img src="${escapeHtml(coverArt)}" class="rounded shadow-sm" style="width:80px;height:80px;object-fit:cover;" alt="">`
        : '<div class="rounded bg-secondary d-flex align-items-center justify-content-center shadow-sm" style="width:80px;height:80px;"><i class="bi bi-music-note-beamed text-white fs-4"></i></div>';

      // escapeJsString for the inline-onclick arguments: escapeHtml escapes
      // for an HTML text context, not a JS string literal, so a title
      // containing an apostrophe (very common) broke the handler.
      //
      // ⚠️ Gated on _mbMatchIntent, NOT _mbSearchCallback. The callback is
      // nulled once a match is applied, so gating on it made every subsequent
      // search in the same session render the Soulseek button — even when the
      // modal had been opened to MATCH a release. Falls back to the callback
      // so a caller that sets only that keeps the old behaviour.
      const wantsMatch = (typeof window._mbMatchIntent === 'boolean')
        ? window._mbMatchIntent
        : !!window._mbSearchCallback;
      const actionButtons = wantsMatch
        ? `<button class="btn btn-sm btn-success" onclick="handleGlobalMbSelect('${encodeInlineArg(release)}')"><i class="bi bi-check-circle"></i> Select Match</button>`
        : `<button class="btn btn-sm btn-success" onclick="downloadMbRelease('${escapeJsString(release.id)}', '${escapeJsString(release.title)}', '${escapeJsString(resultArtist)}', 'slskd')" title="Download via Soulseek"><i class="bi bi-music-note-list"></i> Soulseek</button>`;

      html += `
        <div class="list-group-item bg-dark-subtle border-secondary mb-2 rounded">
          <div class="d-flex gap-3 align-items-start">
            ${imgHtml}
            <div class="flex-grow-1">
              <h6 class="mb-1 fw-bold">${escapeHtml(release.title)}</h6>
              <p class="mb-1 text-muted small">${escapeHtml(resultArtist)}</p>
              <div class="d-flex gap-2 align-items-center mb-2 flex-wrap">
                <span class="badge bg-secondary">${escapeHtml(category)}</span>
                <span class="badge bg-info text-dark"><i class="bi bi-cloud"></i> MusicBrainz</span>
                <span class="text-muted small">${escapeHtml(releaseDate)}</span>
              </div>
            </div>
            <div class="flex-shrink-0 mt-2 mt-sm-0">
              ${actionButtons}
            </div>
          </div>
        </div>`;
    });
    html += '</div>';

    if (resultsEl) resultsEl.innerHTML = html;
  } catch (error) {
    if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> Error: ${escapeHtml(error.message)}</div>`;
  }
};

// Clears the shared MusicBrainz search form. The component's eraser button
// calls this via onclick, but it was defined nowhere — clicking it threw
// ReferenceError and the form never cleared.
window.clearMbSearch = function () {
  ['mbSearchArtist', 'mbSearchAlbum', 'mbSearchTrack', 'mbSearchYear', 'mbSearchInput'].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });

  // A pending "Select Match" choice must not survive a cleared search — the
  // confirmation panel below would otherwise keep showing a release that no
  // longer matches whatever the user searches for next.
  window._mbPendingRelease = null;

  const resultsEl = document.getElementById('mbSearchResults');
  if (resultsEl) {
    resultsEl.innerHTML = '<div class="col-12 text-center text-muted py-5">' +
      '<i class="bi bi-hexagon-fill opacity-50" style="font-size: 3rem;"></i>' +
      '<p class="mt-3 fw-semibold">Enter a search term above to find releases on MusicBrainz.</p></div>';
  }

  const countEl = document.getElementById('mbResultCount');
  if (countEl) countEl.textContent = '';

  const selectedEl = document.getElementById('mbSelectedRelease');
  if (selectedEl) selectedEl.classList.add('d-none');
};

// Holds the release the user picked via "Select Match", awaiting confirmation
// via the "Apply Match" button (#mbSelectedRelease / confirmReleaseSelection).
window._mbPendingRelease = null;

// Called when a result card's "Select Match" button is clicked (only rendered
// when window._mbSearchCallback is set — i.e. a caller like
// openAlbumLookupModal() wants a release chosen here, not downloaded).
//
// This used to invoke window._mbSearchCallback immediately, which skipped the
// "Apply Match" confirmation panel entirely (#mbSelectedRelease, #mbSelectedTitle,
// #mbSelectedArtist in _musicbrainz_search_component.html never got populated,
// so the panel stayed hidden and confirmReleaseSelection() had nothing to do —
// which is also why that function was never written). Now it stages the pick
// and shows the summary panel; confirmReleaseSelection() below fires the
// callback once the user reviews and clicks "Apply Match".
window.handleGlobalMbSelect = function (releaseEnc) {
  const release = decodeInlineArg(releaseEnc);
  if (!release) return;

  window._mbPendingRelease = release;

  const resultArtist = release.artist || (release['artist-credit']?.[0]?.name) || 'Unknown Artist';
  const titleEl = document.getElementById('mbSelectedTitle');
  const artistEl = document.getElementById('mbSelectedArtist');
  const panel = document.getElementById('mbSelectedRelease');

  if (titleEl) titleEl.textContent = release.title || '';
  if (artistEl) artistEl.textContent = resultArtist;
  if (panel) {
    panel.classList.remove('d-none');
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
};

// Wired to #mbSelectedRelease's "Apply Match" button in
// _musicbrainz_search_component.html. Fires the pending window._mbSearchCallback
// (e.g. album_detail.js's openAlbumLookupModal() callback, which calls
// applyAlbumMbid()) with the release staged by handleGlobalMbSelect(), then
// closes the modal. Previously undefined — the button existed in the markup
// but had no handler anywhere in the codebase.
window.confirmReleaseSelection = function () {
  const release = window._mbPendingRelease;
  if (!release) return;

  if (window._mbSearchCallback) {
    window._mbSearchCallback(release);
    window._mbSearchCallback = null;
  }
  window._mbPendingRelease = null;

  const panel = document.getElementById('mbSelectedRelease');
  if (panel) panel.classList.add('d-none');

  const modalEl = document.getElementById('musicBrainzModal');
  if (modalEl) {
    const modal = bootstrap.Modal.getInstance(modalEl);
    if (modal) modal.hide();
  }
};

window.performMbDownloadSearch = window.performMbSearch;

// ============================================================================
// UPCOMING RELEASES
// ============================================================================

async function clearUpcomingReleases() {
  if (!confirm('Are you sure you want to clear all upcoming releases from the database? This cannot be undone.')) return;
  const statusEl = document.getElementById('upcomingStatus');
  const statusText = document.getElementById('upcomingStatusText');
  const errorEl = document.getElementById('upcomingError');
  if (!statusEl || !statusText || !errorEl) return;

  statusEl.style.display = 'block';
  errorEl.style.display = 'none';
  statusText.textContent = 'Clearing database...';

  try {
    const data = await fetchJsonOrThrow('/api/upcoming-releases/clear', { method: 'POST' });
    statusText.textContent = `✓ ${data.message}`;
    setTimeout(() => {
      statusEl.style.display = 'none';
      const container = document.getElementById('upcomingReleases');
      if (container) {
        container.innerHTML = '<div class="text-center py-5"><p class="text-muted">Database cleared. Click <strong>Check for Updates</strong> to search MusicBrainz for new release data.</p></div>';
      }
    }, 2000);
  } catch (error) {
    statusEl.style.display = 'none';
    errorEl.style.display = 'block';
    errorEl.innerHTML = `<i class="bi bi-exclamation-triangle"></i> <strong>Error clearing database:</strong> ${escapeHtml(error.message)}`;
  }
}

async function scrapeUpcomingReleases() {
  const statusEl = document.getElementById('upcomingStatus');
  const statusText = document.getElementById('upcomingStatusText');
  const errorEl = document.getElementById('upcomingError');
  if (!statusEl || !statusText || !errorEl) return;

  statusEl.style.display = 'block';
  errorEl.style.display = 'none';
  statusText.textContent = 'Scraping MusicBrainz for upcoming releases...';

  try {
    const data = await fetchJsonOrThrow('/api/upcoming-releases/scrape', { method: 'POST' });
    statusText.textContent = `✓ ${data.message}`;
    setTimeout(() => {
      statusEl.style.display = 'none';
      refreshUpcomingReleases();
    }, 2000);
  } catch (error) {
    statusEl.style.display = 'none';
    errorEl.style.display = 'block';
    errorEl.innerHTML = `<i class="bi bi-exclamation-triangle"></i> <strong>Error updating releases:</strong> ${escapeHtml(error.message)}`;
  }
}

async function checkForUpdates() {
  localStorage.setItem('upcomingReleasesLastChecked', Date.now().toString());
  await refreshUpcomingReleases();
}

async function refreshUpcomingReleases() {
  const container = document.getElementById('upcomingReleases');
  if (!container) return;
  const filterCollection = document.getElementById('upcomingFilterCollection')?.checked || false;

  container.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary spinner-border-sm"></div><p class="mt-2 small">Loading upcoming releases...</p></div>';

  try {
    const data = await fetchJsonOrThrow(`/api/upcoming-releases?collection=${filterCollection}&include_queue=true`);

    if (!data.releases || data.releases.length === 0) {
      container.innerHTML = '<div class="alert alert-info"><i class="bi bi-info-circle"></i> No upcoming releases found. Click <strong>Check for Updates</strong> to scan MusicBrainz.</div>';
      return;
    }

    let releases = data.releases;
    if (filterCollection) {
      releases = releases.filter(r => !r.album_in_collection);
      if (releases.length === 0) {
        container.innerHTML = '<div class="alert alert-info"><i class="bi bi-check-circle"></i> You have all upcoming releases from artists in your collection!</div>';
        return;
      }
    }

    const grouped = {};
    releases.forEach(release => {
      const month = (release.release_date || 'Unknown Date').substring(0, 7);
      if (!grouped[month]) grouped[month] = [];
      grouped[month].push(release);
    });

    const sortedMonths = Object.keys(grouped).sort();
    let html = '<div class="accordion" id="releaseAccordion">';

    sortedMonths.forEach((month, idx) => {
      const monthReleases = grouped[month];
      // A month key of "Unknown" produces an Invalid Date, which rendered as
      // the literal string "Invalid Date" in the accordion header.
      const parsed = new Date(month + '-01');
      const monthLabel = isNaN(parsed.getTime())
        ? 'Unknown Date'
        : parsed.toLocaleDateString('en-US', { year: 'numeric', month: 'long' });

      html += `
        <div class="accordion-item">
          <h2 class="accordion-header" id="heading${idx}">
            <button class="accordion-button ${idx > 0 ? 'collapsed' : ''}" type="button" data-bs-toggle="collapse" data-bs-target="#collapse${idx}">
              <strong>${escapeHtml(monthLabel)}</strong> <span class="badge bg-primary ms-2">${monthReleases.length}</span>
            </button>
          </h2>
          <div id="collapse${idx}" class="accordion-collapse collapse ${idx === 0 ? 'show' : ''}" data-bs-parent="#releaseAccordion">
            <div class="accordion-body p-0">
              <div class="table-responsive">
                <table class="table table-dark table-hover table-sm mb-0" data-mobile-cards>
                  <thead><tr><th>Artist</th><th>Album</th><th>Date</th><th style="width: 120px;">Action</th></tr></thead>
                  <tbody>
      `;

      monthReleases.forEach(release => {
        const albumStatus = release.album_in_collection ? ' <span class="badge bg-success ms-1">In Collection</span>' : (release.in_queue ? ' <span class="badge bg-warning text-dark ms-1">Downloading</span>' : '');
        const artistStatus = release.artist_in_collection ? ' <span class="badge bg-success ms-1">Artist in Collection</span>' : '';
        const artistArg = JSON.stringify(String(release.artist_name || ''));
        const albumArg = JSON.stringify(String(release.album_name || ''));

        // data-label on every cell: popularr.css turns [data-mobile-cards]
        // tables into stacked cards on mobile using `content: attr(data-label)`.
        // Without it the mobile rows render unlabelled.
        html += `
          <tr>
            <td data-label="Artist">${escapeHtml(release.artist_name)}${artistStatus}</td>
            <td data-label="Album">${escapeHtml(release.album_name)}${albumStatus}</td>
            <td data-label="Date"><small>${escapeHtml(release.release_date || 'TBA')}</small></td>
            <td data-label="Action">
              <button type="button" class="btn btn-sm btn-outline-primary" title="Search on MusicBrainz" onclick='searchMusicBrainzRelease(event, ${artistArg}, ${albumArg})'>
                <i class="bi bi-search"></i> Search
              </button>
            </td>
          </tr>`;
      });

      html += '</tbody></table></div></div></div></div>';
    });

    html += '</div>';
    container.innerHTML = html;
  } catch (error) {
    container.innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> <strong>Error:</strong> ${escapeHtml(error.message)}</div>`;
  }
}

async function searchMusicBrainzRelease(event, artist, album, upcomingReleaseId = null) {
  if (event) { event.preventDefault(); event.stopPropagation(); }
  window.currentUpcomingReleaseContext = upcomingReleaseId ? { releaseId: upcomingReleaseId, artist, album } : null;

  const modalEl = document.getElementById('musicBrainzModal');
  if (!modalEl) {
    alert('Search UI not available on this page.');
    return;
  }

  const resultsEl = document.getElementById('mbSearchResults');
  if (resultsEl) {
    resultsEl.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary"></div><p class="mt-2 text-muted">Searching MusicBrainz...</p></div>';
  }

  // Both fields are set unconditionally. The old `if (field && value)` guard
  // left a stale value from a previous search in place, so an artist-only
  // lookup after an album lookup silently kept the old album and narrowed
  // the query.
  const artistField = document.getElementById('mbSearchArtist');
  const albumField = document.getElementById('mbSearchAlbum');
  if (artistField) artistField.value = artist || '';
  if (albumField) albumField.value = album || '';

  const hasBootstrapModal = !!(window.bootstrap && window.bootstrap.Modal);
  if (hasBootstrapModal) {
    const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
    modal.show();
  } else {
    modalEl.style.display = 'block';
    modalEl.classList.add('show');
    modalEl.removeAttribute('aria-hidden');
    modalEl.setAttribute('aria-modal', 'true');
    document.body.classList.add('modal-open');
  }

  const parseJsonResponse = async (resp, sourceName) => {
    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    if (!contentType.includes('application/json')) {
      const raw = await resp.text();
      const hint = raw && raw.trim().startsWith('<')
        ? 'Received HTML instead of JSON (possible auth/session redirect).'
        : `Unexpected response format from ${sourceName}.`;
      throw new Error(`${hint} HTTP ${resp.status}`);
    }
    const parsed = await resp.json();
    if (!resp.ok) {
      throw new Error(parsed.error || parsed.message || `${sourceName} request failed (HTTP ${resp.status})`);
    }
    return parsed;
  };

  try {
    const response = await fetch('/api/upcoming-releases/search-musicbrainz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    const data = await parseJsonResponse(response, 'MusicBrainz');
    const isArtistOnlySearch = !album || !String(album).trim();

    if (data.success && data.results && data.results.length >= 1) {
      if (resultsEl) resultsEl.innerHTML = '';
      displayMusicBrainzResults(data.results);
      return;
    }

    if (isArtistOnlySearch) {
      if (resultsEl) {
        resultsEl.innerHTML = '<div class="alert alert-info"><i class="bi bi-info-circle"></i> No releases found on MusicBrainz for this artist.</div>';
      }
      return;
    }

    if (resultsEl) {
      resultsEl.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary"></div><p class="mt-2 text-muted">Searching Discogs as fallback...</p></div>';
    }

    const discogsResponse = await fetch('/api/upcoming-releases/search-discogs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    const discogsData = await parseJsonResponse(discogsResponse, 'Discogs');

    const allResults = [];
    if (Array.isArray(data.results)) allResults.push(...data.results);
    if (discogsData.success && Array.isArray(discogsData.results)) allResults.push(...discogsData.results);

    if (allResults.length === 0) {
      if (resultsEl) {
        resultsEl.innerHTML = '<div class="alert alert-info"><i class="bi bi-info-circle"></i> No releases found on MusicBrainz or Discogs.</div>';
      }
      return;
    }

    if (resultsEl) resultsEl.innerHTML = '';
    displayMusicBrainzResults(allResults);
  } catch (error) {
    if (resultsEl) {
      resultsEl.innerHTML = '<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> Error searching: ' + escapeHtml(error.message) + '</div>';
    }
  }
}

document.addEventListener('mbReleaseSelected', function (evt) {
  var detail = evt.detail || {};
  var release = detail.release || window._selectedMusicBrainzRelease;
  if (!release) return;
  var id = detail.releaseId || release.id || '';
  var title = detail.title || release.title || '';
  var artistName = release.artist
    || (release['artist-credit'] && release['artist-credit'][0] && release['artist-credit'][0].name)
    || 'Unknown Artist';
  window._selectedMusicBrainzRelease = null;
  if (typeof window.downloadMbRelease === 'function' && id && title) {
    window.downloadMbRelease(id, title, artistName, 'slskd');
  }
});

function displayMusicBrainzResults(results) {
  const container = document.getElementById('mbSearchResults');
  if (!container) return;

  if (!window.mbReleaseData) {
    window.mbReleaseData = {};
  }

  let html = '<div class="accordion" id="mbResultsAccordion">';

  results.forEach((release, index) => {
    const releaseId = `mbRelease${index}`;
    const dataKey = `release_${Date.now()}_${index}`;
    window.mbReleaseData[dataKey] = {
      artist: release.artist,
      album: release.title,
      tracks: release.tracks,
      year: release.date || release.first_release_date || release.year || null,
      release_id: release.release_id || null,
      release_group_id: release.release_group_id || null,
      source: release.source || 'musicbrainz'
    };

    const source = release.source || 'musicbrainz';
    const sourceBadge = source === 'discogs'
      ? '<span class="badge bg-info ms-2">Discogs</span>'
      : '<span class="badge bg-primary ms-2">MusicBrainz</span>';

    const tracksHtml = (release.tracks || []).map((track, trackIndex) => {
      let duration = 'N/A';
      if (track.length != null && track.length !== '') {
        duration = formatDuration(track.length);
      } else if (track.duration != null && track.duration !== '') {
        duration = track.duration;
      }
      return `
        <tr class="table-dark">
          <td style="width: 44px;" class="text-center">
            <input type="checkbox" class="form-check-input mb-track-select" data-release-key="${dataKey}" data-track-index="${trackIndex}">
          </td>
          <td>${escapeHtml(track.position || '')}</td>
          <td>${escapeHtml(track.title || '')}</td>
          <td>${escapeHtml(String(duration))}</td>
          <td style="width: 120px;" class="text-center">
            <button class="btn btn-sm btn-outline-success mb-download-track" data-release-key="${dataKey}" data-track-index="${trackIndex}">
              <i class="bi bi-download"></i> Download
            </button>
          </td>
        </tr>
      `;
    }).join('');

    const releaseInfo = [];
    if (release.type) {
      releaseInfo.push(release.type);
    } else if (Array.isArray(release.formats) && release.formats.length > 0) {
      releaseInfo.push(release.formats.join(', '));
    } else {
      releaseInfo.push('Album');
    }
    if (release.date) releaseInfo.push(release.date);
    else if (release.year) releaseInfo.push(release.year);
    else releaseInfo.push('Unknown date');
    releaseInfo.push(`${release.track_count || (release.tracks || []).length} tracks`);

    html += `
      <div class="accordion-item">
        <h2 class="accordion-header" id="heading${releaseId}">
          <button class="accordion-button ${index === 0 ? '' : 'collapsed'}" type="button"
            data-bs-toggle="collapse" data-bs-target="#${releaseId}"
            aria-expanded="${index === 0 ? 'true' : 'false'}" aria-controls="${releaseId}">
            <div class="w-100">
              <strong>${escapeHtml(release.title || '')}</strong>${sourceBadge}
              <small class="text-muted ms-2">${escapeHtml(releaseInfo.join(' · '))}</small>
            </div>
          </button>
        </h2>
        <div id="${releaseId}" class="accordion-collapse collapse ${index === 0 ? 'show' : ''}"
          aria-labelledby="heading${releaseId}" data-bs-parent="#mbResultsAccordion">
          <div class="accordion-body">
            <div class="mb-3 d-flex flex-wrap align-items-center gap-2">
              <button class="btn btn-success mb-download-release" data-release-key="${dataKey}">
                <i class="bi bi-download"></i> Download All Tracks (${release.track_count || (release.tracks || []).length})
              </button>
              <button class="btn btn-outline-success mb-download-selected" data-release-key="${dataKey}" disabled>
                <i class="bi bi-check2-square"></i> Download Selected (0)
              </button>
              ${window.currentUpcomingReleaseContext && source !== 'discogs' && release.release_group_id ? `
              <button class="btn btn-outline-primary mb-save-upcoming-match" data-release-key="${dataKey}">
                <i class="bi bi-link-45deg"></i> Use MBID
              </button>
              ` : ''}
              <div class="form-check mb-0 ms-md-2">
                <input class="form-check-input mb-select-all" type="checkbox" id="mbSelectAll_${releaseId}" data-release-key="${dataKey}">
                <label class="form-check-label" for="mbSelectAll_${releaseId}">Select All</label>
              </div>
            </div>
            <table class="table table-sm table-hover table-striped table-dark mb-track-table">
              <thead>
                <tr>
                  <th style="width: 44px;" class="text-center"></th>
                  <th style="width: 60px;">#</th>
                  <th>Title</th>
                  <th style="width: 100px;">Duration</th>
                  <th style="width: 120px;" class="text-center">Action</th>
                </tr>
              </thead>
              <tbody>${tracksHtml}</tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  });

  html += '</div>';
  container.innerHTML = html;

  container.querySelectorAll('.mb-download-release').forEach(button => {
    button.addEventListener('click', function () {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];
      if (!releaseData) {
        alert('Error: Release data not found');
        return;
      }
      downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        releaseData.tracks,
        releaseData.year,
        releaseData.release_id,
        releaseData.source
      );
    });
  });

  container.querySelectorAll('.mb-download-track').forEach(button => {
    button.addEventListener('click', async function () {
      const dataKey = this.dataset.releaseKey;
      const trackIndex = Number(this.dataset.trackIndex);
      const releaseData = window.mbReleaseData[dataKey];
      if (!releaseData || !Array.isArray(releaseData.tracks) || !releaseData.tracks[trackIndex]) {
        alert('Error: Track data not found');
        return;
      }
      const singleTrack = [releaseData.tracks[trackIndex]];
      const queued = await downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        singleTrack,
        releaseData.year,
        releaseData.release_id,
        releaseData.source,
        { closeModal: false, selectionLabel: 'track' }
      );
      if (queued) {
        markMBTrackQueued(container, dataKey, trackIndex);
      }
    });
  });

  container.querySelectorAll('.mb-download-selected').forEach(button => {
    button.addEventListener('click', async function () {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];
      const selectedTracks = getSelectedTracksForRelease(dataKey);
      const selectedIndices = Array.from(container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`))
        .map(input => Number(input.dataset.trackIndex));
      if (!releaseData || selectedTracks.length === 0) {
        alert('Select at least one track first');
        return;
      }
      const queued = await downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        selectedTracks,
        releaseData.year,
        releaseData.release_id,
        releaseData.source,
        { closeModal: false, selectionLabel: 'selected tracks' }
      );
      if (queued) {
        selectedIndices.forEach(index => markMBTrackQueued(container, dataKey, index));
      }
    });
  });

  container.querySelectorAll('.mb-save-upcoming-match').forEach(button => {
    button.addEventListener('click', async function () {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];
      const context = window.currentUpcomingReleaseContext;
      if (!releaseData || !releaseData.release_group_id) {
        alert('This result does not include a MusicBrainz release-group MBID');
        return;
      }
      if (!context || !context.releaseId) {
        alert('No upcoming release is selected for matching');
        return;
      }
      try {
        const response = await fetch(`/api/upcoming-releases/${context.releaseId}/match`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            release_group_mbid: releaseData.release_group_id,
            source: 'manual_selection'
          })
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
          throw new Error(data.error || 'Failed to save upcoming release match');
        }
        const modalEl = document.getElementById('musicBrainzModal');
        const modal = modalEl ? bootstrap.Modal.getInstance(modalEl) : null;
        if (modal) {
          modal.hide();
        }
        if (typeof refreshUpcomingReleases === 'function') {
          await refreshUpcomingReleases();
        }
        if (typeof refreshUpcomingReleasesMonitor === 'function') {
          await refreshUpcomingReleasesMonitor();
        }
      } catch (error) {
        console.error('Error saving upcoming release match:', error);
        alert('Error saving upcoming release match: ' + error.message);
      }
    });
  });

  container.querySelectorAll('.mb-select-all').forEach(checkbox => {
    checkbox.addEventListener('change', function () {
      const dataKey = this.dataset.releaseKey;
      const checked = this.checked;
      container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]`).forEach(trackBox => {
        trackBox.checked = checked;
      });
      updateMBSelectionUI(container, dataKey);
    });
  });

  container.querySelectorAll('.mb-track-select').forEach(checkbox => {
    checkbox.addEventListener('change', function () {
      const dataKey = this.dataset.releaseKey;
      updateMBSelectionUI(container, dataKey);
    });
  });
}

function getSelectedTracksForRelease(dataKey) {
  const releaseData = window.mbReleaseData && window.mbReleaseData[dataKey];
  if (!releaseData || !Array.isArray(releaseData.tracks)) {
    return [];
  }
  const selectedIndices = Array.from(document.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`))
    .map(input => Number(input.dataset.trackIndex))
    .filter(index => Number.isInteger(index) && index >= 0 && index < releaseData.tracks.length);
  return selectedIndices.map(index => releaseData.tracks[index]);
}

function updateMBSelectionUI(container, dataKey) {
  const allTrackBoxes = container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]`);
  const selectedTrackBoxes = container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`);
  const selectedCount = selectedTrackBoxes.length;
  const totalCount = allTrackBoxes.length;

  const selectedBtn = container.querySelector(`.mb-download-selected[data-release-key="${dataKey}"]`);
  if (selectedBtn) {
    selectedBtn.disabled = selectedCount === 0;
    selectedBtn.innerHTML = `<i class="bi bi-check2-square"></i> Download Selected (${selectedCount})`;
  }

  const selectAll = container.querySelector(`.mb-select-all[data-release-key="${dataKey}"]`);
  if (selectAll) {
    selectAll.checked = totalCount > 0 && selectedCount === totalCount;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < totalCount;
  }
}

function markMBTrackQueued(container, dataKey, trackIndex) {
  const rowCheckbox = container.querySelector(`.mb-track-select[data-release-key="${dataKey}"][data-track-index="${trackIndex}"]`);
  if (rowCheckbox) {
    rowCheckbox.checked = false;
    rowCheckbox.disabled = true;
  }
  const rowButton = container.querySelector(`.mb-download-track[data-release-key="${dataKey}"][data-track-index="${trackIndex}"]`);
  if (rowButton) {
    rowButton.disabled = true;
    rowButton.classList.remove('btn-outline-success');
    rowButton.classList.add('btn-success');
    rowButton.innerHTML = '<i class="bi bi-check2"></i> Queued';
  }
  updateMBSelectionUI(container, dataKey);
}

async function downloadMusicBrainzRelease(artist, album, tracks, year, release_id, source, options = {}) {
  if (!tracks || tracks.length === 0) {
    alert('No tracks to download');
    return false;
  }

  const closeModal = options.closeModal !== false;
  const selectionLabel = options.selectionLabel || null;

  try {
    let releaseYear = null;
    if (year) {
      releaseYear = String(year).substring(0, 4);
    }

    const trackItems = tracks.map(track => ({
      artist: track.artist || artist,
      title: track.title,
      album: album,
      source: 'soulseek',
      priority: 5,
      track_number: track.position || null,
      disc_number: track.disc_number || null,
      album_artist: artist,
      year: releaseYear,
      release_id: release_id,
      release_mbid: release_id || null,
      release_source: source || 'musicbrainz',
      recording_mbid: track.recording_mbid || null,
      duration: track.length || null
    }));

    const import_group = `${artist}_${album}`.replace(/\s+/g, '_').substring(0, 100);

    const response = await fetch('/api/queue/add-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: trackItems,
        import_group: import_group,
        import_type: 'album'
      })
    });
    const data = await response.json();

    if (!data.success) {
      alert('❌ Error: ' + (data.error || 'Failed to add tracks to queue'));
      return false;
    }

    if (closeModal) {
      const modalEl = document.getElementById('musicBrainzModal');
      const modal = modalEl ? bootstrap.Modal.getInstance(modalEl) : null;
      if (modal) {
        modal.hide();
      }
    }

    const label = selectionLabel ? ` ${selectionLabel}` : ' tracks';
    let message = `✅ Added ${data.added || tracks.length}${label} from "${album}" to download queue`;
    if (data.failed > 0) {
      message += `\n⚠️ Failed to add ${data.failed} tracks`;
    }
    if ((data.added || 0) > 0 && import_group) {
      message += `\n\n📦 All ${data.import_type || 'album'} tracks are grouped as: "${album}"`;
      message += '\nOnce downloads complete, use "Organize All" in the Completed section to move them to /music';
    }
    alert(message);

    if (typeof loadQueueStatus === 'function') {
      await loadQueueStatus();
    }
    try {
      localStorage.setItem('popularr_queue_updated', Date.now().toString());
    } catch (e) {
      console.warn('Could not update localStorage:', e);
    }
    return true;
  } catch (error) {
    console.error('Error downloading release:', error);
    alert('❌ Error: ' + error.message);
    return false;
  }
}

window.searchMusicBrainzReleaseCanonical = searchMusicBrainzRelease;

// ============================================================================
// MANAGED DOWNLOADS (MusicBrainz/Queuing)
// ============================================================================

const MB_DEFAULT_MAX_RETRIES = 3;

function getMbStatusBadge(status) {
  const s = (status || '').toLowerCase();
  if (s === 'completed') return '<span class="badge bg-success"><i class="bi bi-check-circle"></i> Completed</span>';
  if (['downloading', 'in_progress', 'initiating_download'].includes(s)) return '<span class="badge bg-info"><i class="bi bi-download"></i> Downloading</span>';
  if (['queued', 'pending'].includes(s)) return '<span class="badge bg-secondary"><i class="bi bi-clock"></i> Queued</span>';
  if (['failed', 'error'].includes(s)) return '<span class="badge bg-danger"><i class="bi bi-x-circle"></i> Failed</span>';
  if (s === 'searching') return '<span class="badge bg-warning"><i class="bi bi-search"></i> Searching</span>';
  if (s === 'awaiting_selection') return '<span class="badge bg-primary"><i class="bi bi-hand-index"></i> Select File</span>';
  return `<span class="badge bg-secondary">${escapeHtml(status)}</span>`;
}

async function downloadMbRelease(releaseId, releaseTitle, artist, method) {
  const persistentEl = document.getElementById('persistentSearchCheck');
  const persistentSearch = persistentEl ? persistentEl.checked : false;
  const sessionSelector = document.getElementById('mbSessionSelector');
  const selectedSession = sessionSelector ? sessionSelector.value : '';

  if (selectedSession === 'create') {
    const sessionName = prompt('Enter name for new playlist session:');
    if (!sessionName) return;
    try {
      const data = await fetchJsonOrThrow('/api/playlist-downloads/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_name: sessionName, total_tracks: null, priority_queue: false })
      });
      _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, data.session_id);
    } catch (e) {
      alert('Error creating session: ' + e.message);
    }
    return;
  }

  if (!confirm(`Download "${releaseTitle}" by ${artist} via ${method}?${persistentSearch ? '\n\nPersistent search enabled - will auto-retry if failed.' : ''}`)) return;
  _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, selectedSession || null);
}

async function _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, sessionId) {
  try {
    const data = await fetchJsonOrThrow('/api/musicbrainz/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        release_id: releaseId,
        release_title: releaseTitle,
        artist: artist,
        method: method,
        persistent_search: persistentSearch,
        max_retries: MB_DEFAULT_MAX_RETRIES,
        session_id: sessionId,
        queue_items_only: true
      })
    });

    let msg = `Download queued: ${releaseTitle}\nTracking ID: ${data.tracking_id || 'N/A'}`;
    if (data.persistent_search) msg += '\n\n✓ Persistent search enabled - will retry automatically on failure';
    if (data.session_id) msg += `\n✓ Added to session ID: ${data.session_id}`;
    alert(msg);

    setTimeout(refreshMbDownloads, 1000);
    if (typeof window.loadFolderGroups === 'function') {
      setTimeout(function () { window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true }); }, 1500);
    }
  } catch (e) {
    alert('Error initiating download: ' + e.message);
  }
}

async function loadMbSessionSelector() {
  try {
    const data = await fetchJsonOrThrow('/api/playlist-downloads');
    if (!data.sessions) return;
    const selector = document.getElementById('mbSessionSelector');
    if (!selector) return;
    const createOption = selector.querySelector('option[value="create"]');
    Array.from(selector.options).forEach((opt, i) => { if (i > 1) opt.remove(); });
    data.sessions.filter(s => s.status !== 'completed' && s.status !== 'cancelled').forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.session_name} (${s.completed_tracks}/${s.total_tracks})`;
      selector.insertBefore(opt, createOption);
    });
  } catch (e) {
    console.error('Error loading sessions:', e);
  }
}

async function refreshMbDownloads() {
  const loadingEl = document.getElementById('mbDownloadsLoading');
  const errorEl = document.getElementById('mbDownloadsError');
  const resultsEl = document.getElementById('mbDownloadsResults');
  const emptyEl = document.getElementById('mbDownloadsEmpty');
  const tableEl = document.getElementById('mbDownloadsTable');
  const tableBody = document.getElementById('mbDownloadsTableBody');
  const countBadge = document.getElementById('mbDownloadCount');
  if (!loadingEl) return;

  loadingEl.style.display = 'block';
  if (errorEl) errorEl.style.display = 'none';
  if (resultsEl) resultsEl.style.display = 'none';

  try {
    const data = await fetchJsonOrThrow('/api/musicbrainz/downloads');
    loadingEl.style.display = 'none';
    if (resultsEl) resultsEl.style.display = 'block';
    const downloads = data.downloads || [];

    if (downloads.length === 0) {
      if (emptyEl) emptyEl.style.display = 'block';
      if (tableEl) tableEl.style.display = 'none';
      if (countBadge) countBadge.style.display = 'none';
      return;
    }

    if (emptyEl) emptyEl.style.display = 'none';
    if (tableEl) tableEl.style.display = 'block';
    if (countBadge) { countBadge.textContent = downloads.length; countBadge.style.display = 'inline-block'; }

    if (tableBody) {
      tableBody.innerHTML = downloads.map(dl => {
        const statusBadge = getMbStatusBadge(dl.status);
        const canRetry = ['failed', 'error', 'timeout'].includes((dl.status || '').toLowerCase());
        const canRemove = ['completed', 'failed', 'error', 'cancelled'].includes((dl.status || '').toLowerCase());
        const isAwaitingSelection = (dl.status || '').toLowerCase() === 'awaiting_selection' && dl.method === 'slskd';
        const persistentBadge = dl.persistent_search ? ' <span class="badge bg-secondary ms-1" title="Auto-retry enabled"><i class="bi bi-arrow-repeat"></i> Auto-retry</span>' : '';
        const retryInfo = (dl.persistent_search && dl.retry_count) ? `<div><small class="text-muted">(Retry ${dl.retry_count}/${dl.max_retries || MB_DEFAULT_MAX_RETRIES})</small></div>` : '';
        // Ids are interpolated into inline onclick handlers, so they go through
        // escapeJsString rather than escapeHtml.
        const safeId = escapeJsString(String(dl.id));

        return `<tr data-dl-id="${escapeHtml(String(dl.id))}">
          <td>
            <div><strong>${escapeHtml(dl.release_title)}</strong>${persistentBadge}</div>
            ${retryInfo}
            ${dl.total_tracks ? `<small class="text-muted">Tracks: ${dl.completed_tracks}/${dl.total_tracks} completed</small>` : ''}
          </td>
          <td>${escapeHtml(dl.artist)}</td>
          <td class="text-center"><span class="badge bg-success">Soulseek</span></td>
          <td class="text-center">${statusBadge}</td>
          <td class="text-center text-muted small">${escapeHtml(new Date(dl.created_at).toLocaleString())}</td>
          <td class="text-center">
            <div class="btn-group btn-group-sm">
              ${isAwaitingSelection ? `<button class="btn btn-primary" onclick="showSlskdResults('${safeId}')"><i class="bi bi-hand-index"></i> Select</button>` : ''}
              ${canRetry ? `<button class="btn btn-outline-warning" onclick="retryMbDownload('${safeId}')"><i class="bi bi-arrow-clockwise"></i></button>` : ''}
              ${canRemove ? `<button class="btn btn-outline-danger" onclick="removeMbDownload('${safeId}')"><i class="bi bi-trash"></i></button>` : ''}
            </div>
          </td>
        </tr>`;
      }).join('');
    }
  } catch (error) {
    if (loadingEl) loadingEl.style.display = 'none';
    if (errorEl) { errorEl.textContent = 'Error loading downloads: ' + error.message; errorEl.style.display = 'block'; }
  }
}

async function retryMbDownload(downloadId) {
  if (!confirm('Retry this download?')) return;
  try {
    const data = await fetchJsonOrThrow(`/api/musicbrainz/download/${downloadId}/retry`, { method: 'POST' });
    if (data.success) { alert('Download retry initiated'); refreshMbDownloads(); }
    else alert('Error: ' + (data.error || 'Unknown error'));
  } catch (e) { alert('Error retrying download: ' + e.message); }
}

async function removeMbDownload(downloadId) {
  if (!confirm('Remove this download from the list?')) return;
  try {
    const data = await fetchJsonOrThrow(`/api/musicbrainz/download/${downloadId}`, { method: 'DELETE' });
    if (data.success) refreshMbDownloads();
    else alert('Error: ' + (data.error || 'Unknown error'));
  } catch (e) { alert('Error removing download: ' + e.message); }
}

// ============================================================================
// SOULSEEK SEARCH FUNCTIONS
// ============================================================================
// NOTE: `currentSlskdSearchId` and `slskdPollInterval` are declared HERE and
// deliberately reused (not redeclared) by downloads_page.js, which shares
// script scope. Redeclaring them there throws
// "Identifier has already been declared" and kills that whole file.

let currentSlskdSearchId = null;
let slskdPollInterval = null;
let _slskdCompleteGraceCount = 0;
const _SLSKD_COMPLETE_GRACE_POLLS = 8;

async function searchSoulseek(event) {
  if (event && event.preventDefault) event.preventDefault();
  const query = document.getElementById('slskdSearchQuery')?.value.trim();
  if (!query) return;

  const emptyEl = document.getElementById('slskdSearchEmpty');
  const resultsEl = document.getElementById('slskdSearchResults');
  if (emptyEl) emptyEl.style.display = 'none';
  if (resultsEl) {
    resultsEl.style.display = 'block';
    resultsEl.innerHTML = '<div class="text-center p-3"><span class="spinner-border spinner-border-sm me-2"></span>Searching Soulseek…</div>';
  }
  if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }

  // Reset the grace counter per search. It is module-level, so a previous
  // exhausted search left it at its cap and the next search declared
  // "no results" on its very first terminal poll.
  _slskdCompleteGraceCount = 0;

  try {
    const data = await fetchJsonOrThrow('/api/slskd/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query })
    });

    if (data.slotBusy) {
      if (resultsEl) resultsEl.innerHTML = '<div class="alert alert-warning"><i class="bi bi-clock"></i> <strong>Soulseek search slot is busy.</strong> Retrying automatically…</div>';
      const slotPoll = setInterval(async () => {
        try {
          const slotData = await fetchJsonOrThrow('/api/slskd/search-slot');
          if (slotData.slotFree) {
            clearInterval(slotPoll);
            searchSoulseek({ preventDefault: () => {} });
          }
        } catch (_) {}
      }, 2000);
      return;
    }

    currentSlskdSearchId = data.searchId;
    slskdPollInterval = setInterval(pollSlskdSearchResults, 1500);
    pollSlskdSearchResults();
  } catch (error) {
    if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger">Network error: ${escapeHtml(error.message)}</div>`;
  }
}

async function pollSlskdSearchResults() {
  if (!currentSlskdSearchId) return;
  try {
    const data = await fetchJsonOrThrow(`/api/slskd/search/${encodeURIComponent(currentSlskdSearchId)}`);
    const resultsEl = document.getElementById('slskdSearchResults');
    if (!resultsEl) return;

    const results = data.results || [];
    const isComplete = data.isComplete || false;
    const state = data.state || 'Searching';

    _slskdCompleteGraceCount = _slskdCompleteGraceCount || 0;
    if (isComplete && results.length === 0) {
      if (_slskdCompleteGraceCount < _SLSKD_COMPLETE_GRACE_POLLS) {
        _slskdCompleteGraceCount++;
        return;
      }
      resultsEl.innerHTML = `<div class="alert alert-info"><i class="bi bi-info-circle"></i> Search complete (${escapeHtml(state)}). No results found.</div>`;
      if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }
      _slskdCompleteGraceCount = 0;
      return;
    }
    _slskdCompleteGraceCount = 0;

    const validResults = results.filter(r => {
      const slots = r.freeUploadSlots !== undefined ? r.freeUploadSlots : 1;
      return slots > 0;
    });

    let html = '<div class="table-responsive"><table class="table table-hover" data-mobile-cards><thead><tr><th>File</th><th class="text-center">User</th><th class="text-center">Size</th><th class="text-center">Bitrate</th><th class="text-center">Action</th></tr></thead><tbody>';
    validResults.forEach(r => {
      const sizeMB = r.size_mb || (r.size ? (r.size / (1024 * 1024)).toFixed(2) : 'N/A');
      html += `<tr>
        <td data-label="File"><div class="small" style="max-width:500px;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(r.filename || 'Unknown')}</div></td>
        <td class="text-center" data-label="User"><small class="text-muted">${escapeHtml(r.username || 'N/A')}</small></td>
        <td class="text-center" data-label="Size">${escapeHtml(String(sizeMB))} MB</td>
        <td class="text-center" data-label="Bitrate"><small class="text-muted">${escapeHtml(String(r.bitrate || 'unknown'))}</small></td>
        <td class="text-center" data-label="Action"><button class="btn btn-sm btn-success" onclick="downloadSlskdFile('${escapeJsString(r.username)}', '${escapeJsString(r.filename)}', ${parseInt(r.size) || 0})"><i class="bi bi-download"></i> Download</button></td>
      </tr>`;
    });
    html += `</tbody></table></div><div class="text-muted small mt-2">Found ${validResults.length} available result(s) (filtered zero slots) — State: ${escapeHtml(state)}${isComplete ? ' (complete)' : ''}</div>`;
    resultsEl.innerHTML = html;

    if (isComplete && slskdPollInterval) {
      clearInterval(slskdPollInterval);
      slskdPollInterval = null;
    }
  } catch (error) {
    const resultsEl = document.getElementById('slskdSearchResults');
    if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger">Network error: ${escapeHtml(error.message)}</div>`;
    if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }
  }
}

// ============================================================================
// MANUAL SOULSEEK SEARCH MODAL
// ============================================================================

window.soulseekManualSearchState = window.soulseekManualSearchState || {
  activeSearchId: null,
  pollTimer: null,
  pollingStopped: false,
  waitingForSlot: false,
  queueId: null,
};

function searchOtherSources(artist, titleOrAlbum, isQueueItem = false, queueId = null) {
  const query = normalizeSoulseekQuery(isQueueItem ? `${artist} - ${titleOrAlbum}` : `${artist} ${titleOrAlbum}`);
  if (isQueueItem) {
    openSoulseekManualSearchModal(query, queueId);
    return;
  }
  window.open(`/downloads/search/musicbrainz?q=${encodeURIComponent(query)}`, '_blank');
}

function searchOtherSourcesFromEncoded(artistEnc, titleOrAlbumEnc, isQueueItem = false, queueIdRaw = null) {
  const artist = decodeInlineArg(artistEnc, '');
  const titleOrAlbum = decodeInlineArg(titleOrAlbumEnc, '');
  const queueId = queueIdRaw ? parseInt(queueIdRaw, 10) || null : null;
  return searchOtherSources(artist, titleOrAlbum, isQueueItem === true || isQueueItem === 'true', queueId);
}

function ensureSoulseekManualSearchModal() {
  const existing = document.getElementById('soulseekManualSearchModal');
  if (existing) return existing;

  const modalHtml = `
    <div class="modal fade" id="soulseekManualSearchModal" tabindex="-1" aria-hidden="true">
      <div class="modal-dialog modal-xl">
        <div class="modal-content bg-dark text-light">
          <div class="modal-header border-secondary">
            <h5 class="modal-title"><i class="bi bi-search"></i> Manual Soulseek Search</h5>
            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="input-group mb-3">
              <input type="text" id="soulseekManualQuery" class="form-control" placeholder="Artist - Title">
              <button class="btn btn-success" id="soulseekManualSearchBtn" type="button" onclick="runSoulseekManualSearch()">
                <i class="bi bi-search"></i> Search
              </button>
            </div>
            <div id="soulseekManualStatus" class="small text-muted mb-2">Enter a query and search.</div>
            <div id="soulseekManualResults"></div>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.insertAdjacentHTML('beforeend', modalHtml);

  const modalEl = document.getElementById('soulseekManualSearchModal');

  // Enter submits the query. Without this the modal's only affordance was the
  // button, which is inconsistent with every other search box in the app.
  const queryInput = modalEl.querySelector('#soulseekManualQuery');
  if (queryInput) {
    queryInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        runSoulseekManualSearch();
      }
    });
  }

  modalEl.addEventListener('hidden.bs.modal', () => {
    const state = window.soulseekManualSearchState;
    if (state?.pollTimer) clearTimeout(state.pollTimer);
    window.soulseekManualSearchState = {
      activeSearchId: null,
      pollTimer: null,
      pollingStopped: true,
      waitingForSlot: false,
      queueId: null,
    };
  });
  return modalEl;
}

function openSoulseekManualSearchModal(defaultQuery = '', queueId = null) {
  const modalEl = ensureSoulseekManualSearchModal();
  const queryInput = document.getElementById('soulseekManualQuery');
  const statusEl = document.getElementById('soulseekManualStatus');
  const resultsEl = document.getElementById('soulseekManualResults');

  if (queryInput) queryInput.value = normalizeSoulseekQuery(defaultQuery || '');
  if (statusEl) statusEl.textContent = 'Edit query if needed, then click Search.';
  if (resultsEl) resultsEl.innerHTML = '';

  window.soulseekManualSearchState = window.soulseekManualSearchState || {};
  window.soulseekManualSearchState.queueId = queueId ? parseInt(queueId, 10) || null : null;
  window.soulseekManualSearchState.pollingStopped = false;

  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  modal.show();
}

async function runSoulseekManualSearch() {
  const queryInput = document.getElementById('soulseekManualQuery');
  const statusEl = document.getElementById('soulseekManualStatus');
  const resultsEl = document.getElementById('soulseekManualResults');
  const searchBtn = document.getElementById('soulseekManualSearchBtn');
  const query = normalizeSoulseekQuery(queryInput?.value || '');

  if (!query) {
    alert('Enter search terms first.');
    return;
  }
  if (queryInput) queryInput.value = query;

  if (window.soulseekManualSearchState.pollTimer) {
    clearTimeout(window.soulseekManualSearchState.pollTimer);
    window.soulseekManualSearchState.pollTimer = null;
  }
  window.soulseekManualSearchState.activeSearchId = null;
  window.soulseekManualSearchState.pollingStopped = false;
  window.soulseekManualSearchState.waitingForSlot = false;

  if (searchBtn) {
    searchBtn.disabled = true;
    searchBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Searching...';
  }
  if (statusEl) statusEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Starting search...';
  if (resultsEl) resultsEl.innerHTML = '';

  try {
    const startData = await fetchJsonOrThrow('/api/slskd/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    }, 60000);

    if (startData.slotBusy) {
      _showSlotBusyBanner(query, startData);
      _pollForSlotFree(query);
      return;
    }

    const searchId = startData.searchId;
    if (!searchId) throw new Error('Search did not return a search ID.');
    window.soulseekManualSearchState.activeSearchId = searchId;
    await pollSoulseekManualSearchResults(searchId, 0);
  } catch (error) {
    console.error('Manual Soulseek search failed:', error);
    if (statusEl) statusEl.innerHTML = `<span class="text-danger">${escapeHtml(error.message)}</span>`;
  } finally {
    if (!window.soulseekManualSearchState?.waitingForSlot && searchBtn) {
      searchBtn.disabled = false;
      searchBtn.innerHTML = '<i class="bi bi-search"></i> Search';
    }
  }
}

function _showSlotBusyBanner(pendingQuery, busyData) {
  const statusEl = document.getElementById('soulseekManualStatus');
  if (!statusEl) return;
  window.soulseekManualSearchState.waitingForSlot = true;
  const activeQuery = busyData.activeSearchQuery
    ? `<em>${escapeHtml(busyData.activeSearchQuery)}</em>`
    : 'another query';
  statusEl.innerHTML = `
    <div class="alert alert-warning d-flex align-items-start gap-2 mb-0 py-2" id="slskdSlotBusyBanner">
      <span class="spinner-border spinner-border-sm mt-1 flex-shrink-0"></span>
      <div class="flex-grow-1">
        <strong>slskd search slot is busy.</strong>
        An active search for ${activeQuery} is in progress.
        <br>Your search for <em>${escapeHtml(pendingQuery)}</em> will start automatically when it finishes.
      </div>
      <button class="btn btn-sm btn-outline-secondary flex-shrink-0" onclick="_cancelWaitForSlot()">Cancel</button>
    </div>`;
}

async function _pollForSlotFree(pendingQuery) {
  const POLL_INTERVAL_MS = 2000;
  const state = window.soulseekManualSearchState || {};
  if (state.pollingStopped || !state.waitingForSlot) return;

  try {
    const data = await fetchJsonOrThrow('/api/slskd/search-slot');
    if (data.slotFree) {
      const statusEl = document.getElementById('soulseekManualStatus');
      if (statusEl) statusEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Slot free — starting search…';
      window.soulseekManualSearchState.waitingForSlot = false;
      const searchBtn = document.getElementById('soulseekManualSearchBtn');
      if (searchBtn) {
        searchBtn.disabled = false;
        searchBtn.innerHTML = '<i class="bi bi-search"></i> Search';
      }
      const queryInput = document.getElementById('soulseekManualQuery');
      if (queryInput) queryInput.value = pendingQuery;
      await runSoulseekManualSearch();
      return;
    }

    const banner = document.getElementById('slskdSlotBusyBanner');
    if (banner && data.activeSearchQuery) {
      const activeQuery = `<em>${escapeHtml(data.activeSearchQuery)}</em>`;
      const div = banner.querySelector('div.flex-grow-1');
      if (div) {
        div.innerHTML = `
          <strong>slskd search slot is busy.</strong>
          An active search for ${activeQuery} is in progress.
          <br>Your search for <em>${escapeHtml(pendingQuery)}</em> will start automatically when it finishes.`;
      }
    }
  } catch (err) {
    console.warn('_pollForSlotFree error:', err);
  }

  state.pollTimer = setTimeout(() => _pollForSlotFree(pendingQuery), POLL_INTERVAL_MS);
  window.soulseekManualSearchState = state;
}

function _cancelWaitForSlot() {
  const state = window.soulseekManualSearchState || {};
  if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
  state.pollingStopped = true;
  state.waitingForSlot = false;
  window.soulseekManualSearchState = state;

  const statusEl = document.getElementById('soulseekManualStatus');
  if (statusEl) statusEl.textContent = 'Search cancelled. Edit query if needed, then click Search.';
  const searchBtn = document.getElementById('soulseekManualSearchBtn');
  if (searchBtn) {
    searchBtn.disabled = false;
    searchBtn.innerHTML = '<i class="bi bi-search"></i> Search';
  }
}

async function pollSoulseekManualSearchResults(searchId, attempt, transientErrors = 0) {
  const statusEl = document.getElementById('soulseekManualStatus');
  const resultsEl = document.getElementById('soulseekManualResults');
  const state = window.soulseekManualSearchState || {};
  if (state.pollingStopped || state.activeSearchId !== searchId) return;

  try {
    const data = await fetchJsonOrThrow(`/api/slskd/search/${encodeURIComponent(searchId)}`, {}, 45000);
    const results = Array.isArray(data.results) ? data.results : [];
    const responseCount = Number(data.responseCount || 0);
    const isComplete = !!data.isComplete;

    if (statusEl) {
      statusEl.textContent = `Responses: ${responseCount} | Files: ${results.length} | State: ${data.state || 'searching'}${isComplete ? ' (complete)' : ''}`;
    }
    renderSoulseekManualSearchResults(results);

    const _graceExhausted = isComplete && results.length === 0 && attempt >= _SLSKD_COMPLETE_GRACE_POLLS;
    if ((!isComplete || (isComplete && results.length === 0 && !_graceExhausted)) && attempt < 60) {
      state.pollTimer = setTimeout(() => {
        pollSoulseekManualSearchResults(searchId, attempt + 1, 0);
      }, 1500);
      window.soulseekManualSearchState = state;
      return;
    }

    if (statusEl && isComplete && results.length === 0) {
      statusEl.textContent = 'Search complete. No files found. Try adjusting the query.';
    }
  } catch (error) {
    console.error('Error polling Soulseek manual search results:', error);
    const msg = (error.message || '').toLowerCase();
    const isTransient = (
      msg.includes('timed out') || msg.includes('timeout') || msg.includes('network') ||
      msg.includes('524') || msg.includes('504') || msg.includes('502') ||
      msg.includes('503') || msg.includes('500') || msg.includes('abort') || msg.includes('fetch')
    );
    if (isTransient && transientErrors < 5) {
      if (statusEl) statusEl.textContent = `Poll failed (${transientErrors + 1}/5) — retrying…`;
      state.pollTimer = setTimeout(() => {
        pollSoulseekManualSearchResults(searchId, attempt + 1, transientErrors + 1);
      }, 2000);
      window.soulseekManualSearchState = state;
      return;
    }
    if (statusEl) statusEl.innerHTML = `<span class="text-danger">${escapeHtml(error.message)}</span>`;
    if (resultsEl && !resultsEl.innerHTML) {
      resultsEl.innerHTML = '<div class="alert alert-danger mb-0">Could not load search results.</div>';
    }
  }
}

function renderSoulseekManualSearchResults(results) {
  const container = document.getElementById('soulseekManualResults');
  if (!container) return;

  const validResults = (results || []).filter(row => {
    const slots = row.freeUploadSlots !== undefined ? row.freeUploadSlots : 1;
    return slots > 0;
  });

  if (!validResults || validResults.length === 0) {
    container.innerHTML = '<div class="text-muted small">No available results with free upload slots yet.</div>';
    return;
  }

  const rows = validResults.map((row) => {
    const username = row.username || '';
    const filename = row.filename || '';
    const sizeMb = row.size_mb || '-';
    const bitrate = row.bitrate || '-';
    const duration = row.duration || row.length || '-';

    // Ensure absolute safe integer parsing for bytes
    const rawSize = row.size !== undefined ? row.size : (row.size_mb ? Number(row.size_mb) * 1024 * 1024 : 0);
    const size = Number.isFinite(Number(rawSize)) ? Math.round(Number(rawSize)) : 0;

    const rawLength = row.length_seconds !== undefined ? row.length_seconds : (row.length !== undefined ? row.length : 0);
    const length = Number.isFinite(Number(rawLength)) ? Math.round(Number(rawLength)) : 0;

    // Encode parameters safely for inline JavaScript execution
    const encUser = encodeInlineArg(username);
    const encFile = encodeInlineArg(filename);

    return `
      <tr>
        <td data-label="User">${escapeHtml(username)}</td>
        <td style="word-break: break-word;" data-label="Filename">${escapeHtml(filename)}</td>
        <td data-label="Size">${escapeHtml(String(sizeMb))} MB</td>
        <td data-label="Bitrate">${escapeHtml(String(bitrate))}</td>
        <td data-label="Duration">${escapeHtml(String(duration))}</td>
        <td class="text-center" data-label="Action">
          <button class="btn btn-sm btn-success fw-bold px-3" onclick="downloadSoulseekManualResult('${encUser}', '${encFile}', ${size}, ${length}, this)">
            Select
          </button>
        </td>
      </tr>
    `;
  }).join('');

  container.innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm table-dark table-striped align-middle mb-0" data-mobile-cards>
        <thead>
          <tr>
            <th>User</th>
            <th>Filename</th>
            <th>Size MB</th>
            <th>Bitrate</th>
            <th>Duration</th>
            <th style="width: 100px;" class="text-center">Action</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

async function downloadSoulseekManualResult(usernameEnc, filenameEnc, size, length, buttonEl = null) {
  const username = decodeInlineArg(usernameEnc, '');
  const filename = decodeInlineArg(filenameEnc, '');
  const statusEl = document.getElementById('soulseekManualStatus');

  if (!username || !filename) {
    alert('Missing Soulseek result details.');
    return;
  }

  const btn = buttonEl && buttonEl.tagName ? buttonEl : null;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  }

  const queueId = window.soulseekManualSearchState?.queueId || null;
  const endpoint = queueId ? '/api/slskd/queue-download' : '/api/slskd/download';

  // Ensure size is a valid integer, fallback to 0 if missing
  const parsedSize = Number.isFinite(Number(size)) ? Math.round(Number(size)) : 0;
  const parsedLength = Number.isFinite(Number(length)) ? Math.round(Number(length)) : null;

  const body = {
    username,
    filename,
    size: parsedSize,
    length: parsedLength
  };
  if (queueId) {
    body.queue_id = queueId;
  }

  try {
    const data = await fetchJsonOrThrow(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!data.success) throw new Error(data.error || 'Failed to enqueue Soulseek download');

    if (statusEl) statusEl.innerHTML = `<span class="text-success">Queued: ${escapeHtml(filename)}</span>`;
    alert('✅ Soulseek download queued');

    if (typeof loadQueueStatus === 'function') await loadQueueStatus();
    if (typeof window.loadFolderGroups === 'function') {
      await window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
    }
  } catch (error) {
    console.error('Error queueing Soulseek manual download:', error);
    if (statusEl) statusEl.innerHTML = `<span class="text-danger">${escapeHtml(error.message)}</span>`;
    alert('❌ Could not queue download: ' + error.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Select';
    }
  }
}

document.addEventListener('DOMContentLoaded', function () {
  try {
    const params = new URLSearchParams(window.location.search);
    const searchParam = normalizeSoulseekQuery(params.get('search') || '');
    if (searchParam) openSoulseekManualSearchModal(searchParam);
  } catch (_) {}
});

// ============================================================================
// QUEUE MANAGEMENT FUNCTIONS
// ============================================================================

let queuePageOffset = 0;
const QUEUE_PAGE_LIMIT = 500;

function getQueuePageUrl(limit, offset) {
  const params = new URLSearchParams({
    limit: String(limit || QUEUE_PAGE_LIMIT),
    offset: String(Math.max(0, offset || queuePageOffset))
  });
  return `/api/downloads/queue?${params.toString()}`;
}

function updateQueuePageControls(totalCount, loadedCount) {
  const summary = document.getElementById('queuePageSummary');
  const prevBtn = document.getElementById('queuePrevPageBtn');
  const nextBtn = document.getElementById('queueNextPageBtn');
  const safeTotal = Number(totalCount || 0);
  const safeLoaded = Number(loadedCount || 0);
  const start = safeTotal === 0 ? 0 : queuePageOffset + 1;
  const end = safeTotal === 0 ? 0 : Math.min(queuePageOffset + safeLoaded, safeTotal);

  if (summary) summary.textContent = safeTotal === 0 ? 'Showing 0 of 0' : `Showing ${start}-${end} of ${safeTotal}`;
  if (prevBtn) prevBtn.disabled = queuePageOffset <= 0;
  if (nextBtn) nextBtn.disabled = (queuePageOffset + safeLoaded) >= safeTotal;
}

function changeQueuePage(direction) {
  const nextOffset = Math.max(0, queuePageOffset + (direction * QUEUE_PAGE_LIMIT));
  if (nextOffset === queuePageOffset) return;
  queuePageOffset = nextOffset;
  if (typeof window.loadFolderGroups === 'function') {
    window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
  }
}

async function addToQueue(event) {
  event.preventDefault();
  const artist = document.getElementById('queueArtist').value.trim();
  const title = document.getElementById('queueTitle').value.trim();
  const album = document.getElementById('queueAlbum').value.trim();
  const source = document.getElementById('queueSource')?.value || 'soulseek';
  const priority = parseInt(document.getElementById('queuePriority')?.value || 5);

  if (!artist) { alert('Please enter an artist.'); return; }
  if (!title && !album) { alert('Please enter either a song title or an album name.'); return; }

  if (!title && album) {
    if (confirm('No song title entered. Search MusicBrainz releases for this artist/album instead?')) {
      searchMusicBrainzForQueue();
    }
    return;
  }

  try {
    await fetchJsonOrThrow('/api/queue/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, title, album, source, priority })
    });

    alert(`✅ Added to queue: ${artist} - ${title}`);
    const form = document.getElementById('addToQueueForm');
    if (form) form.reset();

    queuePageOffset = 0;
    await loadQueueStatus();
    if (typeof window.loadFolderGroups === 'function') {
      await window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
    }
  } catch (error) {
    alert('❌ Network error: ' + error.message);
  }
}

function searchMusicBrainzForQueue() {
  const artist = document.getElementById('queueArtist')?.value.trim() || '';
  const album = document.getElementById('queueAlbum')?.value.trim() || '';
  const track = document.getElementById('queueTitle')?.value.trim() || '';

  if (!artist && !album && !track) {
    alert('Please enter at least one field before searching MusicBrainz.');
    return;
  }

  if (typeof window.openGlobalMbSearch !== 'function') {
    console.error('openGlobalMbSearch is unavailable — is main.js loaded?');
    alert('MusicBrainz search is unavailable on this page.');
    return;
  }

  window.openGlobalMbSearch(artist, album, (selectedRelease) => {
    downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
  }, track);
}

async function loadQueueStatus() {
  try {
    const data = await fetchJsonOrThrow('/api/downloads/queue?limit=1&offset=0');
    if (!data || !data.queue) {
      ['queueTotalCount', 'queueActiveCount', 'queueQueuedCount', 'queueCompletedCount', 'queueMovingCount', 'queueFailedCount'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '0';
      });
      return;
    }

    const statusCounts = data.status_counts || {};
    const countFor = (...s) => s.reduce((sum, st) => sum + Number(statusCounts[st] || 0), 0);
    const setNum = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = String(val); };

    setNum('queueTotalCount', countFor('queued', 'searching', 'processing', 'unmatched', 'pending_match', 'discovered', 'queried', 'matched', 'downloading', 'completed', 'moving', 'importing', 'failed', 'possible_duplicate', 'duplicate'));
    setNum('queueQueuedCount', countFor('queued', 'searching', 'processing', 'unmatched', 'pending_match', 'discovered', 'queried', 'matched'));
    setNum('queueActiveCount', countFor('downloading'));
    setNum('queueCompletedCount', countFor('completed'));
    setNum('queueMovingCount', countFor('moving', 'importing'));
    setNum('queueFailedCount', countFor('failed'));

    document.querySelectorAll('.stat-pill[data-pill-for]').forEach(pill => {
      const el = document.getElementById(pill.dataset.pillFor);
      const count = Number(el ? el.textContent : 0) || 0;
      pill.classList.toggle('d-none', count === 0);
    });
  } catch (error) {
    console.error('Error loading queue status:', error);
  }

  await renderQueueSection();
  await renderQueueLog();
  await renderSearchLog();
  await renderQueuePage();
}

async function clearEntireQueue() {
  if (!confirm('Clear the entire queue? This removes queued, failed and completed items but keeps imported records.')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/clear', { method: 'DELETE' });
    alert(`✅ Cleared ${data.deleted || 0} item(s) from queue`);
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function clearQueue() {
  return clearEntireQueue();
}

async function purgeAllQueueAndDownloads() {
  if (!confirm('PURGE ALL? This deletes all queue rows and permanently deletes all files/folders in your configured downloads folder. This cannot be undone.')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/purge-all', { method: 'DELETE' });
    alert(`✅ Purge complete\n\nDownloads folder: ${data.downloads_dir || 'unknown'}\nQueue items removed: ${data.queue_items_deleted || 0}\nFiles deleted: ${data.deleted_files || 0}`);
    queuePageOffset = 0;
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function retryAllFailed() {
  if (!confirm('Re-queue all failed downloads?')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/retry-all-failed', { method: 'POST' });
    alert(`✅ Re-queued ${data.retried || 0} failed item(s)`);
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function cleanupCopiedSources() {
  if (!confirm('Delete copied source files from /downloads while keeping queue history?')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/cleanup-copied', { method: 'POST' });
    alert(`✅ ${data.message} (scanned ${data.scanned})`);
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function loadQueueEvents() {
  try {
    const data = await fetchJsonOrThrow('/api/queue/events?limit=50');
    const events = data.events || [];
    const emptyDiv = document.getElementById('queueEventsEmpty');
    const table = document.getElementById('queueEventsTable');
    const tbody = document.getElementById('queueEventsBody');
    if (!emptyDiv || !table || !tbody) return;

    if (events.length === 0) {
      emptyDiv.style.display = 'block';
      table.style.display = 'none';
      return;
    }
    emptyDiv.style.display = 'none';
    table.style.display = 'table';

    tbody.innerHTML = events.map(e => {
      const ts = new Date(e.created_at);
      const badgeClass = e.event_type === 'file_found' ? 'bg-info' : e.event_type === 'status_change' ? 'bg-primary' : e.event_type === 'error' ? 'bg-danger' : 'bg-success';
      return `<tr><td class="small text-muted">${escapeHtml(ts.toLocaleString())}</td><td><span class="badge ${badgeClass}">${escapeHtml((e.event_type || '').replace(/_/g, ' ').toUpperCase())}</span></td><td>${escapeHtml(e.message || '')}</td></tr>`;
    }).join('');
  } catch (error) { console.error('Error loading queue events:', error); }
}

function clearQueueEventsLog() {
  const tbody = document.getElementById('queueEventsBody');
  const emptyDiv = document.getElementById('queueEventsEmpty');
  const table = document.getElementById('queueEventsTable');
  if (tbody) tbody.innerHTML = '';
  if (table) table.style.display = 'none';
  if (emptyDiv) emptyDiv.style.display = 'block';
}

async function restartQueueProcessor() {
  const btn = document.getElementById('restartProcessorBtn');
  const originalText = btn ? btn.innerHTML : '';
  try {
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Restarting...'; }
    await fetchJsonOrThrow('/api/queue-processor/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    if (btn) btn.innerHTML = '<i class="bi bi-check-circle"></i> Restarted!';
    setTimeout(() => {
      if (typeof window.loadQueueStatus === 'function') window.loadQueueStatus();
      if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
    }, 2000);
  } catch (e) {
    if (btn) btn.innerHTML = '<i class="bi bi-exclamation-circle"></i> Error!';
    setTimeout(() => { if (btn) { btn.innerHTML = originalText; btn.disabled = false; } }, 3000);
  }
}

async function deleteQueueItem(queueId, deleteDownloadsFile) {
  const promptText = deleteDownloadsFile ? 'Delete this item from queue AND remove its file from /downloads?' : 'Remove from queue?';
  if (!confirm(promptText)) return;
  try {
    const query = deleteDownloadsFile ? '?delete_download_file=1' : '';
    await fetchJsonOrThrow('/api/queue/' + queueId + '/delete' + query, { method: 'DELETE' });
    alert('✅ Removed from queue');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function retryQueueItem(queueId) {
  try {
    await fetchJsonOrThrow('/api/queue/' + queueId + '/requeue', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    alert('✅ Retrying download...');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function cancelQueueItem(queueId) {
  if (!confirm('Cancel this download? The queue item will be marked failed and can be retried.')) return;
  try {
    await fetchJsonOrThrow('/api/queue/' + queueId + '/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    alert('✅ Download cancelled');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function organizeFile(queueId) {
  if (!confirm('Copy file to music library?')) return;
  try {
    await fetchJsonOrThrow('/api/queue/' + queueId + '/organize', { method: 'POST' }, 120000);
    alert('✅ File organized successfully!');
    await loadQueueStatus();
    if (typeof window.loadFolderGroups === 'function') {
      await window.loadFolderGroups({ forceRender: true });
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function runQueueCleanup() {
  if (!confirm('Run queue cleanup now?')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/cleanup', { method: 'POST' });
    const stats = data.stats || {};
    alert('✅ Cleanup complete\n\nDuplicates removed: ' + (stats.deleted_duplicates || 0) + '\nCompleted albums: ' + (stats.completed_albums || 0));
    await loadQueueStatus();
    if (typeof window.loadFolderGroups === 'function') {
      await window.loadFolderGroups({ forceRender: true });
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function organizeSelected() { alert('organizeSelected not yet implemented'); }
async function batchOrganizeSelected() { alert('batchOrganizeSelected not yet implemented'); }

// ============================================================================
// QUEUE RENDERING
// ============================================================================

async function renderQueueSection() {
  const section = document.getElementById('folderGroupsSection');
  const list = document.getElementById('folderGroupsList');
  const badge = document.getElementById('folderGroupsBadge');
  if (!section || !list) return;
  section.style.display = 'block';

  let qItems = [];
  try {
    const qd = await fetchJsonOrThrow('/api/downloads/queue?limit=200');
    qItems = (qd && qd.queue) || [];
  } catch (error) {
    console.error('Error loading queue items:', error);
  }

  let html = '';
  if (qItems.length > 0) {
    html += '<h6 class="px-3 pt-3 mb-0 small text-muted text-uppercase">Queue Items</h6>';
    const queueGroups = buildQueueGroups(qItems);
    window.__queueGroupsArr = queueGroups;
    html += '<div class="list-group list-group-flush">' + queueGroups.map(function (group, index) {
      if (group.items.length === 1) {
        return renderQueueItemRow(group.items[0], 'active');
      }
      return renderQueueGroupRow(group, 'active', index);
    }).join('') + '</div>';
  }

  const total = qItems.length;
  if (total === 0) {
    if (badge) badge.textContent = '0 items';
    list.innerHTML = '<div class="alert alert-info m-3"><i class="bi bi-info-circle"></i> No items in queue right now.</div>';
    return;
  }

  if (badge) badge.textContent = total + ' items';
  list.innerHTML = html;
  attachQueueGroupToggles(list);
  restoreQueueGroupExpansion(list);
  if (typeof updateQueuePageControls === 'function') updateQueuePageControls(total, total);
}

async function renderQueueLog() {
  const logEl = document.getElementById('queueActivityLog');
  if (!logEl) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/events?limit=100');
    const events = (data && data.events) || [];
    const lines = events.slice().reverse().map(function (event) {
      const ts = event.created_at || event.timestamp || null;
      const timeLabel = ts ? new Date(ts).toLocaleTimeString([], { hour12: false }) : '--:--:--';
      return '[' + timeLabel + '] ' + (event.event_type || 'info').toUpperCase() + ' ' + (event.message || '');
    });
    logEl.textContent = lines.length ? lines.join('\n') : 'No queue events yet.';
    logEl.scrollTop = logEl.scrollHeight;
  } catch (error) {
    console.error('Error loading queue log:', error);
  }
}

async function renderSearchLog() {
  const logEl = document.getElementById('soulseekSearchLog');
  if (!logEl) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/search-events?limit=100');
    const events = (data && data.events) || [];
    const chunks = [];
    events.slice().reverse().forEach(function (event) {
      const ts = event.timestamp ? new Date(event.timestamp) : null;
      const timeLabel = ts ? ts.toLocaleTimeString([], { hour12: false }) : '--:--:--';
      const type = (event.search_type || 'unknown').toUpperCase();
      chunks.push('[' + type + '] [' + timeLabel + '] Query: "' + (event.query || '') + '"  |  ' + (event.artist || '') + ' - ' + (event.title || ''));
      chunks.push('    Results: ' + (event.result_count ?? 0) + '  Duration: ' + (event.duration_seconds != null ? event.duration_seconds + 's' : 'n/a'));
    });
    logEl.textContent = chunks.length ? chunks.join('\n') : 'No Soulseek search events yet.';
    logEl.scrollTop = logEl.scrollHeight;
  } catch (error) {
    console.error('Error loading search log:', error);
  }
}

async function renderQueuePage() {
  const activeList = document.getElementById('activeQueueList');
  if (!activeList) return;
  try {
    const data = await fetchJsonOrThrow('/api/downloads/queue?limit=500');
    const statusCounts = (data && data.status_counts) || {};
    const countFor = (...s) => s.reduce((sum, st) => sum + Number(statusCounts[st] || 0), 0);
    const setNum = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = String(val); };

    setNum('statQueuedNum', countFor('queued', 'searching', 'unmatched', 'pending_match', 'discovered', 'queried', 'matched'));
    setNum('statDownloadingNum', countFor('downloading'));
    setNum('statCompletedNum', countFor('completed'));
    setNum('statFailedNum', countFor('failed'));
    setNum('statImportedNum', countFor('imported', 'moving'));
    setNum('queueActiveCount', countFor('queued', 'searching', 'downloading', 'failed'));
    setNum('queueCompletedCount', countFor('completed'));
    setNum('queueFailedCount', countFor('failed'));

    const lastRefreshed = document.getElementById('queueLastRefreshed');
    if (lastRefreshed) lastRefreshed.textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour12: false });

    const items = (data && data.queue) || [];
    const completed = (data && data.completed) || [];
    renderQueueList('active', items.filter(i => i.status !== 'failed' && i.status !== 'completed'));
    renderQueueList('completed', completed.filter(i => (i.status || 'completed') !== 'failed'));
    renderQueueList('failed', items.filter(i => i.status === 'failed'));

    const retryAllBtn = document.getElementById('retryAllBtn');
    if (retryAllBtn) retryAllBtn.style.display = Number(statusCounts.failed || 0) > 0 ? 'inline-block' : 'none';
  } catch (error) {
    console.error('Error rendering queue page:', error);
  }
}

function renderQueueList(kind, items) {
  const idPrefix = kind === 'active' ? 'active' : kind;
  const listEl = document.getElementById(idPrefix + 'QueueList');
  const emptyEl = document.getElementById(idPrefix + 'QueueEmpty');
  const badgeEl = document.getElementById(kind === 'active' ? 'queueActiveCount' : kind + 'Badge');
  if (!listEl || !emptyEl) return;

  if (!items.length) {
    listEl.style.display = 'none';
    emptyEl.style.display = 'block';
    if (badgeEl) badgeEl.style.display = 'none';
    return;
  }

  emptyEl.style.display = 'none';
  listEl.style.display = 'block';
  if (badgeEl) { badgeEl.textContent = items.length + ' item' + (items.length !== 1 ? 's' : ''); badgeEl.style.display = 'inline-block'; }

  const groups = buildQueueGroups(items);
  window.__queueGroupsArr = groups;
  const rows = groups.map(function (group, index) {
    if (group.items.length === 1) {
      return renderQueueItemRow(group.items[0], kind);
    }
    return renderQueueGroupRow(group, kind, index);
  });

  listEl.innerHTML = '<div class="list-group list-group-flush">' + rows.join('') + '</div>';
  attachQueueGroupToggles(listEl);
  restoreQueueGroupExpansion(listEl);
}

function buildQueueGroups(items) {
  var groups = [];
  var map = {};
  (items || []).forEach(function (item) {
    var album = (item.album || item.queue_folder || '').trim();
    var artist = (item.album_artist || item.artist || '').trim();
    var title = (item.title || '').trim();
    var key, label, sublabel;

    // Explicitly ignore "default" so legacy rows don't merge
    if (item.import_group && item.import_group !== 'default' && item.import_group !== 'manual') {
      key = 'grp_' + String(item.import_group);
      label = album || String(item.import_group);
      sublabel = artist;
    } else if (album && album !== title) {
      key = 'alb_' + artist.toLowerCase() + '|' + album.toLowerCase();
      label = album;
      sublabel = artist;
    } else {
      key = 'solo_' + (item.id || item.filename);
      label = album || 'Unmatched Files';
      sublabel = artist || 'Various';
    }

    if (!map[key]) {
      map[key] = { key: key, label: label, sublabel: sublabel, items: [] };
      groups.push(map[key]);
    }
    map[key].items.push(item);
  });
  return groups;
}

function manualQueueSlskdSearch(encodedQuery, queueIdRaw) {
  const query = decodeURIComponent(encodedQuery || '');
  if (!query) return;

  if (typeof window.openSoulseekManualSearchModal === 'function') {
    const queueId = queueIdRaw ? parseInt(queueIdRaw, 10) || null : null;
    window.openSoulseekManualSearchModal(query, queueId);
    return;
  }

  const input = document.getElementById('slskdSearchQuery');
  if (input) input.value = query;
  const tabBtn = document.getElementById('soulseek-tab');
  if (tabBtn && typeof bootstrap !== 'undefined' && bootstrap.Tab) {
    bootstrap.Tab.getOrCreateInstance(tabBtn).show();
  }
  const form = document.getElementById('slskdSearchForm');
  if (form) {
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setTimeout(function () { form.dispatchEvent(new Event('submit')); }, 150);
  }
}

function renderQueueItemRow(item, kind) {
  const st = item.status || 'queued';
  const pillCls = st === 'failed' ? 'failed'
    : (st === 'downloading' || st === 'searching' || st === 'processing') ? 'downloading'
      : (st === 'completed' || st === 'moving' || st === 'imported' || st === 'in_collection') ? 'complete'
        : (st === 'pending_release' || st === 'unmatched') ? 'pending'
          : 'queued';

  let pillLabel = st.charAt(0).toUpperCase() + st.slice(1).replace(/_/g, ' ');
  if (st === 'downloading' && item.progress != null && Number(item.progress) > 0) {
    pillLabel = 'Downloading ' + Math.round(Math.max(0, Math.min(100, Number(item.progress)))) + '%';
  }
  const statusPill = '<span class="badge status-pill status-' + pillCls + '">' + escapeHtml(pillLabel) + '</span>';

  const chips = [];
  if (item.album && item.album !== item.title) {
    chips.push('<span class="meta-pill"><i class="bi bi-disc"></i>' + escapeHtml(item.album) + '</span>');
  }
  const mbid = item.release_mbid || item.release_id || item.recording_mbid || '';
  if (mbid) {
    const shortMbid = String(mbid).slice(0, 8);
    chips.push('<span class="meta-pill" title="' + escapeHtml(mbid) + '"><i class="bi bi-fingerprint"></i>' + escapeHtml(shortMbid) + '</span>');
  }
  if (item.duration) {
    chips.push('<span class="meta-pill"><i class="bi bi-clock"></i>' + escapeHtml(formatDuration(item.duration)) + '</span>');
  }
  if (item.track_number) {
    chips.push('<span class="meta-pill"><i class="bi bi-music-note"></i>Track ' + escapeHtml(String(item.track_number)) + '</span>');
  }
  const metaLine = '<div class="d-flex align-items-center gap-1 flex-wrap mt-1">' + statusPill + chips.join('') + '</div>';

  let progressHtml = '';
  if (st === 'downloading' && item.progress != null && Number(item.progress) > 0) {
    const pct = Math.min(100, Math.max(0, Number(item.progress)));
    progressHtml = '<div class="progress mt-2" style="height: 6px; max-width: 260px;">' +
      '<div class="progress-bar bg-primary" role="progressbar" style="width: ' + pct + '%" aria-valuenow="' + pct + '" aria-valuemin="0" aria-valuemax="100"></div>' +
      '</div>';
  }

  const itemId = parseInt(item.id, 10) || 0;
  let actions = '';

  if (kind !== 'completed') {
    const searchQuery = [item.artist, item.title, (item.album && item.album !== item.title) ? item.album : '']
      .filter(Boolean).join(' ');
    if (searchQuery) {
      actions += '<button class="btn btn-sm btn-outline-info py-0 px-2 ms-1" title="Search Soulseek manually" onclick="manualQueueSlskdSearch(\'' + encodeURIComponent(searchQuery) + '\',' + itemId + ')"><i class="bi bi-search"></i></button>';
    }
  }
  if (kind === 'active') {
    if (st === 'downloading' || st === 'searching' || st === 'processing') {
      actions += '<button class="btn btn-sm btn-outline-danger py-0 px-2 ms-1" title="Cancel download" onclick="cancelQueueItem(' + itemId + ')"><i class="bi bi-x-circle"></i></button>';
    }
  }
  if (kind === 'failed' || st === 'failed') {
    actions += '<button class="btn btn-sm btn-outline-warning py-0 px-2 ms-1" title="Retry" onclick="retryQueueItem(' + itemId + ')"><i class="bi bi-arrow-clockwise"></i></button>';
  }
  if (kind === 'completed') {
    actions += '<button class="btn btn-sm btn-outline-success py-0 px-2 ms-1" title="Copy to library" onclick="organizeFile(' + itemId + ')"><i class="bi bi-folder-plus"></i></button>';
  }
  actions += '<button class="btn btn-sm btn-outline-danger py-0 px-2 ms-1" title="Remove" onclick="deleteQueueItem(' + itemId + ', false)"><i class="bi bi-trash"></i></button>';

  return '<div class="list-group-item"><div class="d-flex justify-content-between align-items-center gap-2">' +
    '<div style="min-width:0;">' +
    '<div class="text-truncate"><strong>' + escapeHtml(item.title || item.album || 'Unknown') + '</strong>' +
    (item.artist ? '<br><small class="text-muted">' + escapeHtml(item.artist) + (item.album && item.album !== item.title ? ' - ' + escapeHtml(item.album) : '') + '</small>' : '') +
    '</div>' +
    metaLine + progressHtml +
    (kind === 'failed' && item.failure_reason ? '<div class="small text-danger mt-1"><i class="bi bi-exclamation-triangle"></i> ' + escapeHtml(item.failure_reason) + '</div>' : '') +
    '</div>' +
    '<div class="d-flex align-items-center gap-1 flex-shrink-0">' + actions + '</div>' +
    '</div></div>';
}

function renderQueueGroupRow(group, kind, index) {
  const bodyId = 'queueGroupBody_' + kind + '_' + sanitizeQueueGroupKey(group.key);
  const items = group.items;
  const total = items.length;

  const counts = {};
  items.forEach(function (item) {
    const st = item.status || 'queued';
    counts[st] = (counts[st] || 0) + 1;
  });
  const summary = Object.keys(counts).map(function (st) {
    return counts[st] + ' ' + st;
  }).join(' · ');

  const subline = group.sublabel
    ? ' <small class="text-muted">' + escapeHtml(group.sublabel) + '</small>'
    : '';

  let actions = '';
  if (kind === 'active') {
    const hasActive = items.some(function (i) {
      return i.status === 'downloading' || i.status === 'searching' || i.status === 'processing';
    });
    if (hasActive) {
      actions += '<button class="btn btn-sm btn-outline-danger py-0 px-2 ms-1" title="Cancel all active downloads" onclick="cancelGroup(' + index + ')"><i class="bi bi-x-circle"></i></button>';
    }
  }
  if (kind === 'completed') {
    actions += '<button class="btn btn-sm btn-outline-success py-0 px-2 ms-1" title="Copy all tracks to music library" onclick="organizeGroup(' + index + ')"><i class="bi bi-folder-check"></i></button>';
  }
  if (kind === 'failed') {
    actions += '<button class="btn btn-sm btn-outline-warning py-0 px-2 ms-1" title="Retry all failed tracks" onclick="retryGroup(' + index + ')"><i class="bi bi-arrow-clockwise"></i></button>';
  }

  // Organize Album action for album groups.
  // escapeJsString, not escapeHtml: these two values are interpolated into a
  // JS string literal inside an onclick attribute, and an album label
  // containing an apostrophe ("Livin' Thing") terminated the string early and
  // broke the handler.
  if (group.key.startsWith('alb_') || group.key.startsWith('grp_')) {
    actions += '<button class="btn btn-sm btn-outline-success py-0 px-2 ms-1" title="Organize and move album group" onclick="openOrganizeGroupModal(\'' + escapeJsString(group.key) + '\', \'' + escapeJsString(group.label) + '\', ' + total + ')"><i class="bi bi-folder-check"></i></button>';
  }
  actions += '<button class="btn btn-sm btn-outline-danger py-0 px-2 ms-1" title="Remove all tracks in this album" onclick="deleteGroup(' + index + ')"><i class="bi bi-trash"></i></button>';

  const children = items.map(function (item) {
    return renderQueueItemRow(item, kind);
  }).join('');

  return '<div class="list-group-item">' +
    '<div class="d-flex justify-content-between align-items-center gap-2">' +
    '<button type="button" class="btn btn-sm btn-link p-0 text-decoration-none queue-group-toggle flex-shrink-0" data-target="' + bodyId + '" title="Expand album" style="color:var(--text-secondary);">' +
    '<i class="bi bi-chevron-down queue-group-chevron"></i>' +
    '</button>' +
    '<div class="text-truncate flex-grow-1" style="min-width:0;">' +
    '<strong><i class="bi bi-folder2-open me-1"></i>' + escapeHtml(group.label) + '</strong>' + subline +
    '<br><small class="text-muted">' + total + ' track' + (total !== 1 ? 's' : '') + ' · ' + escapeHtml(summary) + '</small>' +
    '</div>' +
    '<div class="d-flex align-items-center gap-1 flex-shrink-0">' + actions + '</div>' +
    '</div>' +
    '<div id="' + bodyId + '" class="queue-group-body ps-3 border-start ms-2 mt-2" style="display:none;">' + children + '</div>' +
    '</div>';
}

window.__expandedQueueGroups = window.__expandedQueueGroups || new Set();

function sanitizeQueueGroupKey(key) {
  return String(key || 'x').replace(/[^a-zA-Z0-9_-]/g, '_');
}
window.__sanitizeQueueGroupKey = window.__sanitizeQueueGroupKey || sanitizeQueueGroupKey;

function restoreQueueGroupExpansion(listEl) {
  if (!listEl) return;
  const expanded = window.__expandedQueueGroups;
  const found = new Set();
  listEl.querySelectorAll('.queue-group-body').forEach(function (body) {
    found.add(body.id);
    if (!expanded.has(body.id)) return;
    body.style.display = 'block';
    const item = body.closest('.list-group-item');
    const btn = item && item.querySelector('.queue-group-toggle');
    const chevron = btn && btn.querySelector('.queue-group-chevron');
    if (chevron) chevron.classList.add('rotated');
  });
  expanded.forEach(function (id) { if (!found.has(id)) expanded.delete(id); });
}
window.__restoreQueueGroupExpansion = window.__restoreQueueGroupExpansion || restoreQueueGroupExpansion;

function attachQueueGroupToggles(listEl) {
  if (!listEl) return;
  listEl.querySelectorAll('.queue-group-toggle').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const body = document.getElementById(btn.getAttribute('data-target'));
      const chevron = btn.querySelector('.queue-group-chevron');
      if (!body) return;
      const show = body.style.display === 'none' || body.style.display === '';
      body.style.display = show ? 'block' : 'none';
      if (chevron) chevron.classList.toggle('rotated', show);
      if (show) {
        window.__expandedQueueGroups.add(body.id);
      } else {
        window.__expandedQueueGroups.delete(body.id);
      }
    });
  });
}

function queueGroupByIndex(index) {
  const groups = window.__queueGroupsArr || [];
  return groups[index] || null;
}

async function organizeGroup(index) {
  const group = queueGroupByIndex(index);
  if (!group || !group.items.length) return;
  const copyable = group.items.filter(function (i) {
    return i.status === 'completed' || i.status === 'moving';
  });
  if (!copyable.length) { alert('No completed tracks in this album to copy.'); return; }
  if (!confirm('Copy ' + copyable.length + ' completed track(s) in "' + (group.label || '') + '" to the music library?')) return;

  try {
    const ig = group.items.find(function (i) { return i.import_group; });
    if (ig) {
      await fetchJsonOrThrow('/api/queue/organize-group', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_id: ig.import_group })
      }, 300000);
    } else {
      for (const item of copyable) {
        await fetchJsonOrThrow('/api/queue/' + item.id + '/organize', { method: 'POST' }, 120000);
      }
    }
    alert('✅ Album organized successfully!');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function retryGroup(index) {
  const group = queueGroupByIndex(index);
  if (!group || !group.items.length) return;
  const failed = group.items.filter(function (i) { return i.status === 'failed'; });
  if (!failed.length) { alert('No failed tracks in this album to retry.'); return; }
  if (!confirm('Re-queue ' + failed.length + ' failed track(s) in "' + (group.label || '') + '"?')) return;

  try {
    for (const item of failed) {
      await fetchJsonOrThrow('/api/queue/' + item.id + '/requeue', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    }
    alert('✅ Retrying ' + failed.length + ' track(s)...');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function cancelGroup(index) {
  const group = queueGroupByIndex(index);
  if (!group || !group.items.length) return;
  const active = group.items.filter(function (i) {
    return i.status === 'downloading' || i.status === 'searching' || i.status === 'processing';
  });
  if (!active.length) { alert('No active downloads in this album.'); return; }
  if (!confirm('Cancel ' + active.length + ' active download(s) in "' + (group.label || '') + '"?')) return;

  try {
    for (const item of active) {
      await fetchJsonOrThrow('/api/queue/' + item.id + '/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    }
    alert('✅ Downloads cancelled');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function deleteGroup(index) {
  const group = queueGroupByIndex(index);
  if (!group || !group.items.length) return;
  if (!confirm('Remove all ' + group.items.length + ' track(s) in "' + (group.label || '') + '" from the queue?')) return;

  try {
    for (const item of group.items) {
      await fetchJsonOrThrow('/api/queue/' + item.id + '/delete', { method: 'DELETE' });
    }
    alert('✅ Album removed from queue');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

// ============================================================================
// ORGANIZE GROUP MODAL CONTROLLERS
// ============================================================================

let currentOrganizeGroupKey = null;

function openOrganizeGroupModal(groupKey, label, count) {
  currentOrganizeGroupKey = groupKey;

  const infoText = document.getElementById('groupInfoText');
  const itemCount = document.getElementById('groupItemCount');
  if (infoText) infoText.textContent = label || 'Album Group';
  if (itemCount) itemCount.textContent = count || '0';

  const orgAlbum = document.getElementById('orgAlbum');
  if (orgAlbum && label) orgAlbum.value = label;

  // The group's artist was never prefilled, so the Artist field opened blank
  // and confirmOrganizeGroup() rejected the submit with "Artist and Album
  // fields are required" until the user retyped a value the app already had.
  const group = (window.__queueGroupsArr || []).find(function (g) { return g.key === groupKey; });
  if (group) {
    const orgArtist = document.getElementById('orgArtist');
    const orgAlbumArtist = document.getElementById('orgAlbumArtist');
    if (orgArtist && group.sublabel) orgArtist.value = group.sublabel;
    if (orgAlbumArtist && group.sublabel) orgAlbumArtist.value = group.sublabel;

    const firstYear = (group.items || []).map(function (i) { return i.year; }).find(Boolean);
    const orgYear = document.getElementById('orgYear');
    if (orgYear && firstYear) orgYear.value = String(firstYear).substring(0, 4);
  }

  updateFolderPreview();

  const modalEl = document.getElementById('organizeGroupModal');
  if (modalEl && window.bootstrap) {
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}

function updateFolderPreview() {
  const artist = document.getElementById('orgArtist')?.value.trim() || 'Artist Name';
  const albumArtist = document.getElementById('orgAlbumArtist')?.value.trim() || artist;
  const album = document.getElementById('orgAlbum')?.value.trim() || 'Album Name';
  const year = document.getElementById('orgYear')?.value.trim() || String(new Date().getFullYear());

  let format = document.getElementById('orgFolderFormat')?.value || '{album_artist}/{year} - {album}';
  if (format === 'custom') {
    format = document.getElementById('orgCustomFormat')?.value.trim() || '{album_artist}/{year} - {album}';
  }

  const previewPath = format
    .replace(/{album_artist}/g, albumArtist)
    .replace(/{artist}/g, artist)
    .replace(/{album}/g, album)
    .replace(/{year}/g, year);

  const previewEl = document.getElementById('folderPreview');
  if (previewEl) {
    previewEl.textContent = `Music / ${previewPath} / 01. Track Title.mp3`;
  }
}

document.addEventListener('input', function (e) {
  if (['orgArtist', 'orgAlbumArtist', 'orgAlbum', 'orgYear', 'orgCustomFormat', 'orgFolderFormat'].includes(e.target.id)) {
    if (e.target.id === 'orgFolderFormat') {
      const customDiv = document.getElementById('customFormatDiv');
      if (customDiv) {
        customDiv.style.display = e.target.value === 'custom' ? 'block' : 'none';
      }
    }
    updateFolderPreview();
  }
});

// `orgFolderFormat` is a <select>, which fires `change`, not `input`. Without
// this the folder preview did not update when the directory format changed.
document.addEventListener('change', function (e) {
  if (e.target && e.target.id === 'orgFolderFormat') {
    const customDiv = document.getElementById('customFormatDiv');
    if (customDiv) {
      customDiv.style.display = e.target.value === 'custom' ? 'block' : 'none';
    }
    updateFolderPreview();
  }
});

// Show / hide the organize-lookup spinner.
//
// The template used to carry `style="display:none !important"` on this
// element; an inline !important cannot be overridden by
// `loadingDiv.style.display = 'flex'`, so the spinner never appeared. It is
// now a `d-none` class toggle (see the template change in the notes).
function _setOrganizeLookupLoading(loadingDiv, visible) {
  if (!loadingDiv) return;
  loadingDiv.classList.toggle('d-none', !visible);
  loadingDiv.classList.toggle('d-flex', visible);
}

async function searchMBForOrganize() {
  const artist = document.getElementById('orgArtist')?.value.trim() || '';
  const album = document.getElementById('orgAlbum')?.value.trim() || '';

  // `org`-prefixed ids: the Organize Group modal deliberately namespaces its
  // containers so they cannot collide with the global MusicBrainz modal that
  // base.html renders on EVERY page.
  //
  // This used to target #mbSearchResults / #mbSearchLoading — the global
  // modal's ids. getElementById returns the FIRST match in DOM order, which is
  // base.html's hidden modal, so results were injected there while this modal
  // showed nothing, and loadingDiv was null so the spinner never cleared.
  const resultsDiv = document.getElementById('orgMbSearchResults');
  const loadingDiv = document.getElementById('orgMbSearchLoading');

  if (!artist && !album) {
    alert('Please enter at least an artist or album name.');
    return;
  }
  if (resultsDiv) resultsDiv.innerHTML = '';
  _setOrganizeLookupLoading(loadingDiv, true);

  try {
    const data = await fetchJsonOrThrow('/api/musicbrainz/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    _setOrganizeLookupLoading(loadingDiv, false);

    const releases = data.releases || [];
    if (releases.length === 0) {
      if (resultsDiv) resultsDiv.innerHTML = '<div class="alert alert-warning py-1 extra-small">No matches found on MusicBrainz.</div>';
      return;
    }

    let html = '<div class="list-group list-group-flush" style="max-height: 200px; overflow-y: auto;">';
    releases.slice(0, 10).forEach(r => {
      const safeTitle = escapeHtml(r.title);
      const safeArtist = escapeHtml(r.artist || artist);
      const safeYear = escapeHtml((r.first_release_date || '').substring(0, 4));

      html += `
        <button type="button" class="list-group-item list-group-item-action bg-dark text-light border-secondary py-1 px-2 extra-small"
          onclick="applyMBMatchForOrganize('${escapeJsString(r.artist || artist)}', '${escapeJsString(r.title)}', '${escapeJsString((r.first_release_date || '').substring(0, 4))}')">
          <strong>${safeTitle}</strong> — <span class="text-muted">${safeArtist} (${safeYear || 'TBA'})</span>
        </button>`;
    });
    html += '</div>';
    if (resultsDiv) resultsDiv.innerHTML = html;
  } catch (err) {
    _setOrganizeLookupLoading(loadingDiv, false);
    if (resultsDiv) resultsDiv.innerHTML = `<div class="alert alert-danger py-1 extra-small">Error: ${escapeHtml(err.message)}</div>`;
  }
}

async function searchDiscogsForOrganize() {
  const artist = document.getElementById('orgArtist')?.value.trim() || '';
  const album = document.getElementById('orgAlbum')?.value.trim() || '';
  // Same id correction as searchMBForOrganize above.
  const resultsDiv = document.getElementById('orgMbSearchResults');
  const loadingDiv = document.getElementById('orgMbSearchLoading');

  if (!artist && !album) {
    alert('Please enter at least an artist or album name.');
    return;
  }
  if (resultsDiv) resultsDiv.innerHTML = '';
  _setOrganizeLookupLoading(loadingDiv, true);

  try {
    const data = await fetchJsonOrThrow('/api/album/discogs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    _setOrganizeLookupLoading(loadingDiv, false);

    const results = data.results || [];
    if (results.length === 0) {
      if (resultsDiv) resultsDiv.innerHTML = '<div class="alert alert-warning py-1 extra-small">No matches found on Discogs.</div>';
      return;
    }

    let html = '<div class="list-group list-group-flush" style="max-height: 200px; overflow-y: auto;">';
    results.slice(0, 10).forEach(r => {
      const safeTitle = escapeHtml(r.title);
      const safeYear = escapeHtml(String(r.year || ''));

      html += `
        <button type="button" class="list-group-item list-group-item-action bg-dark text-light border-secondary py-1 px-2 extra-small"
          onclick="applyMBMatchForOrganize('${escapeJsString(artist)}', '${escapeJsString(r.title)}', '${escapeJsString(String(r.year || ''))}')">
          <strong>${safeTitle}</strong> — <span class="text-muted">${safeYear || 'TBA'}</span>
        </button>`;
    });
    html += '</div>';
    if (resultsDiv) resultsDiv.innerHTML = html;
  } catch (err) {
    _setOrganizeLookupLoading(loadingDiv, false);
    if (resultsDiv) resultsDiv.innerHTML = `<div class="alert alert-danger py-1 extra-small">Error: ${escapeHtml(err.message)}</div>`;
  }
}

function applyMBMatchForOrganize(artist, album, year) {
  const orgArtist = document.getElementById('orgArtist');
  const orgAlbum = document.getElementById('orgAlbum');
  const orgYear = document.getElementById('orgYear');
  if (orgArtist) orgArtist.value = artist;
  if (orgAlbum) orgAlbum.value = album;
  if (orgYear && year && year !== 'TBA') orgYear.value = year;

  updateFolderPreview();

  const matchInfo = document.getElementById('mbSelectedInfo');
  const matchAlert = document.getElementById('mbSelectedMatch');
  if (matchInfo) matchInfo.textContent = `${artist} - ${album} (${year || 'N/A'})`;
  if (matchAlert) matchAlert.classList.remove('d-none');

  const metadataTabBtn = document.getElementById('metadataTab');
  if (metadataTabBtn && window.bootstrap) {
    bootstrap.Tab.getOrCreateInstance(metadataTabBtn).show();
  }
}

function clearMBSelection() {
  const matchAlert = document.getElementById('mbSelectedMatch');
  if (matchAlert) matchAlert.classList.add('d-none');
}

async function confirmOrganizeGroup() {
  if (!currentOrganizeGroupKey) {
    alert('No active album group selected.');
    return;
  }

  const artist = document.getElementById('orgArtist')?.value.trim();
  const album = document.getElementById('orgAlbum')?.value.trim();

  if (!artist || !album) {
    alert('Artist and Album fields are required.');
    return;
  }

  const payload = {
    group_id: currentOrganizeGroupKey,
    artist: artist,
    album_artist: document.getElementById('orgAlbumArtist')?.value.trim() || null,
    album: album,
    year: document.getElementById('orgYear')?.value.trim() || null,
    directory_format: document.getElementById('orgFolderFormat')?.value || null,
    custom_format: document.getElementById('orgCustomFormat')?.value.trim() || null
  };

  try {
    const modalEl = document.getElementById('organizeGroupModal');
    const modal = modalEl && window.bootstrap ? bootstrap.Modal.getInstance(modalEl) : null;
    if (modal) modal.hide();

    const data = await fetchJsonOrThrow('/api/queue/organize-group', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }, 120000);

    if (data.success) {
      showToastMsg(`Successfully organized and moved ${data.moved_count || 'album'} tracks into library.`, false);
      if (typeof window.loadFolderGroups === 'function') {
        window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
      }
      if (typeof window.loadQueueStatus === 'function') {
        window.loadQueueStatus();
      }
    } else {
      showToastMsg(data.error || 'Failed to organize group.', true);
    }
  } catch (err) {
    showToastMsg('Network error during organization: ' + err.message, true);
  }
}

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', function () {
  // `refreshUpcomingReleases()` was called TWICE in this handler — once at the
  // top and again at the bottom — so every page load fired two identical
  // requests and rendered the accordion twice.
  if (document.getElementById('upcomingReleases')) {
    refreshUpcomingReleases();
  }

  const mbSearchInput = document.getElementById('mbSearchInput');
  if (mbSearchInput) {
    mbSearchInput.addEventListener('keypress', function (e) {
      if (e.key === 'Enter') {
        if (typeof performMbSearch === 'function') performMbSearch();
      }
    });
    loadMbSessionSelector();
    refreshMbDownloads();
  }

  var mbTabEl = document.getElementById('mbTab');
  if (mbTabEl) {
    mbTabEl.addEventListener('click', function () {
      setTimeout(loadMbSessionSelector, 200);
    });
  }

  if (document.getElementById('folderGroupsSection') || document.getElementById('statQueuedNum')) {
    loadQueueStatus();
    let _queuePollInFlight = false;
    setInterval(async () => {
      if (_queuePollInFlight) return;
      _queuePollInFlight = true;
      try { await loadQueueStatus(); } finally { _queuePollInFlight = false; }
    }, 10000);
  }

  if (document.getElementById('queueEventsBody')) {
    loadQueueEvents();
  }
});
