/* ==========================================================================
   static/js/pages/track.js
   Track detail page — rename file, cover detection, MusicBrainz recording
   lookup, LRCLIB lyrics fetch, Soulseek search, similar artists.

   Load order (everything else comes from base.html):
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/modal.js  →  ui/confirm.js  →  ui/button-state.js
       ui/add-to-playlist.js          (global.openAddToPlaylistModal)
       services/musicbrainz-picker.js (global.openGlobalMbSearch)
       services/musicbrainz-queue.js  (global.searchMusicBrainzRelease)
       pages/track.js

   Page data is NOT scraped from the DOM. templates/Pages/track_detail.html
   emits one <script id="page-data" type="application/json"> block; this file
   parses it once. That is the existing contract and it is a good one — it
   keeps Jinja out of the JavaScript and removes every
   `{{ track.artist|tojson }}` interpolation that used to sit inside the
   inline script.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/track_detail.html carried a 26-line inline <style> block and
   a 233-line inline <script>. The CSS is now static/CSS/track.css; the script
   is this file. The markup now carries ZERO JavaScript.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. `openMbReleaseModal()` DID NOT EXIST — every click threw ReferenceError.
      The template's External-IDs accordion called it
      (`<button onclick="openMbReleaseModal('{{ track.id }}')">`). The only
      definition in the tree is in old_system/templates/track.html, where it
      consumed `GET /api/track/<id>/mb-releases` expecting
      `{releases, recording_mbid}`. The CURRENT route returns
      `{success, recordings}` and passes them through `search_recordings()` —
      so the old renderer would have read `undefined` and shown "No releases
      found for this recording" even if the function had been defined. The
      button is now wired to the release picker that actually works
      (`global.openGlobalMbSearch`) and writes the chosen release into the
      MusicBrainz Album ID field.

   2. `selectCoverMatch()` DID NOT EXIST EITHER. The cover-search results table
      built `<button onclick="selectCoverMatch('<artist>')">` from a string
      assembled with `JSON.stringify(r.artist)` — which is not HTML-escaped,
      so an artist name containing a quote broke the attribute, and no
      function with that name is defined anywhere. Selecting a result now
      fills the "Writer or original artist" field via a delegated listener.

   3. `escapeHtml()` DID NOT ESCAPE QUOTES.
          div.appendChild(document.createTextNode(String(text || '')));
          return div.innerHTML;
      That escapes `&`, `<` and `>` and leaves `"` and `'` untouched — and the
      result was interpolated into attributes (`title="${escapeHtml(a.name)}"`,
      `onclick="${action}"`), so an artist name with a double quote closed the
      attribute early. utils/dom.js's escapeHtml escapes quotes; this file no
      longer defines its own.

   4. THE SIMILAR-ARTISTS BLOCK MADE UP TO 13 REQUESTS PER PAGE VIEW.
      /api/artist/<name>/similar already annotates each entry with
      `in_collection` (see services/metadata/artist_metadata_service
      `_annotate_similar_artist`), and the code even read
      `data.similar_artists.in_collection` — but only to build the candidate
      list; it then classified every candidate AGAIN with a parallel
      `/api/search/unified` request and ignored the server's answer. The
      server's split is used first now, and the unified-search fallback only
      runs when the payload has no `in_collection`/`missing` arrays at all.

   5. `alert()` / `confirm()` / raw `fetch()` → ui/toast.js, ui/confirm.js and
      utils/api.js (so an HTML error page no longer surfaces as
      "Unexpected token '<'").

   6. INLINE onclick HANDLERS. Ten of them, each requiring a window global —
      including two (`openMbReleaseModal`, `selectCoverMatch`) that never
      existed. They are `data-action` buttons handled by one delegated
      listener; nothing is exported onto window except the small `global.track`
      namespace for programmatic use.

   ── DELIBERATE REMOVAL: THE TRACK FAVOURITE HEART ─────────────────────────
   The hero card had a heart calling `toggleTrackFavourite('{{ track.id }}')`.
   That global is gone from the refactored tree on purpose: pages/main.js
   records "toggleTrackFavourite, toggleAlbumFavourite → deleted (favourites
   removed)" and pages/album.js says "favourites are being removed. Delete the
   heart button from the template" — the album page's heart was already
   dropped for the same reason (its duplicate of the function was backed by a
   different endpoint than main.js's, so the two disagreed). Artist favourites
   are a different feature (auto-queue new albums, /api/artist/favourite) and
   are untouched.

   If per-user track favourites come back, the correct implementation is
   /api/favourites/toggle + /api/favourites/state (routes/favourites.py,
   services/favourites_service.py) — NOT the old /api/track/favourite route
   the deleted function used. Add the button back with a data-action for it.
   ========================================================================== */

(function (global) {
  'use strict';

  const SEARCH_TIMEOUT_MS = 30000;
  const SIMILAR_LIMIT = 12;

  /** Parsed once from <script id="page-data" type="application/json">. */
  const pageData = (function readPageData() {
    const el = document.getElementById('page-data');
    if (!el) {
      console.error('[track] #page-data is missing — the template must emit it.');
      return {};
    }
    try {
      return JSON.parse(el.textContent) || {};
    } catch (error) {
      console.error('[track] #page-data is not valid JSON:', error.message);
      return {};
    }
  })();

  const trackId = pageData.trackId || '';
  const trackTitle = pageData.title || '';
  const trackArtist = pageData.artistName || '';
  const trackAlbum = pageData.album || '';

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function notifyError(message) {
    if (global.toast) global.toast.error(message);
    else console.error('[track]', message);
  }

  function notifyInfo(message) {
    if (global.toast) global.toast.info(message);
    else console.log('[track]', message);
  }

  /** Show a Bootstrap modal, going through ui/modal.js when it is loaded. */
  function showModal(el) {
    if (!el) return null;
    if (global.modal && typeof global.modal.show === 'function') {
      global.modal.show(el);
      return el;
    }
    if (global.bootstrap && global.bootstrap.Modal) {
      global.bootstrap.Modal.getOrCreateInstance(el).show();
    }
    return el;
  }

  function hideModal(el) {
    if (!el) return;
    if (global.modal && typeof global.modal.hide === 'function') {
      global.modal.hide(el);
      return;
    }
    if (global.bootstrap && global.bootstrap.Modal) {
      const instance = global.bootstrap.Modal.getInstance(el);
      if (instance) instance.hide();
    }
  }

  // ── Rename file ─────────────────────────────────────────────────────────

  async function renameTrackFile() {
    if (!trackId) return;
    try {
      const data = await global.api.postJson(
        `/api/track/${encodeURIComponent(trackId)}/rename-file`, {}
      );
      if (data.success && data.renamed) {
        notifyInfo('File renamed to: ' + (data.new_path || ''));
        setTimeout(() => global.location.reload(), 1200);
      } else if (data.success && !data.renamed) {
        notifyInfo('File is already at the correct path — no rename needed.');
      } else {
        notifyError('Rename failed: ' + (data.error || 'Unknown error'));
      }
    } catch (error) {
      notifyError('Rename failed: ' + error.message);
    }
  }

  // ── Cover-song detection ────────────────────────────────────────────────

  async function searchCoverSong() {
    const titleEl = document.getElementById('coverSearchTitle');
    const writerEl = document.getElementById('coverSearchWriter');
    const results = document.getElementById('coverSearchResults');
    if (!titleEl || !results) return;

    const title = titleEl.value.trim();
    const writer = writerEl ? writerEl.value.trim() : '';
    if (!title) {
      notifyError('Please enter a song title');
      return;
    }

    results.innerHTML =
      '<div class="text-center py-3"><span class="spinner-border spinner-border-sm"></span> ' +
      'Searching MusicBrainz…</div>';

    try {
      const data = await global.api.postJson(
        '/api/track/musicbrainz', { title, artist: writer }, { timeoutMs: SEARCH_TIMEOUT_MS }
      );
      const rows = data.results || [];
      if (!rows.length) {
        results.innerHTML = '<p class="text-muted mb-0">No results found.</p>';
        return;
      }

      // data-artist instead of an inline onclick: the previous version built
      // `onclick="selectCoverMatch(' + JSON.stringify(r.artist) + ')"`, so an
      // artist name containing a double quote produced broken markup — and
      // selectCoverMatch() was never defined anyway (see file header).
      results.innerHTML =
        '<table class="table table-sm table-hover" style="font-size:0.8rem;">' +
        '<thead><tr><th>Title</th><th>Artist</th><th>Year</th><th></th></tr></thead><tbody>' +
        rows.map((row) => `
          <tr>
            <td>${esc(row.title)}</td>
            <td>${esc(row.artist)}</td>
            <td>${esc(row.year || '—')}</td>
            <td>
              <button type="button" class="btn btn-sm btn-outline-success py-0 px-1 cover-select-btn"
                      data-artist="${esc(row.artist || '')}">Select</button>
            </td>
          </tr>`).join('') +
        '</tbody></table>';
    } catch (error) {
      results.innerHTML =
        `<div class="alert alert-danger mb-0">Search failed: ${esc(error.message)}</div>`;
    }
  }

  /** Copy a cover-search result into the "original artist" field. */
  function selectCoverMatch(button) {
    const writerEl = document.getElementById('coverSearchWriter');
    if (!writerEl) return;
    writerEl.value = button.dataset.artist || '';
    notifyInfo('Original artist filled in.');
  }

  // ── MusicBrainz recording lookup ────────────────────────────────────────

  async function lookupTrackMusicBrainz() {
    const titleEl = document.getElementById('title');
    const artistEl = document.getElementById('artist');
    const title = titleEl ? titleEl.value.trim() : '';
    const artist = artistEl ? artistEl.value.trim() : '';
    if (!title || !artist) {
      notifyError('Please enter both title and artist');
      return;
    }

    // The modal is created on first use rather than living in the template,
    // so it is not shipped on every page load for a button most visits ignore.
    let modalEl = document.getElementById('trackMBLookupModal');
    if (!modalEl) {
      document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="trackMBLookupModal" tabindex="-1" aria-hidden="true">
          <div class="modal-dialog modal-lg modal-dialog-scrollable">
            <div class="modal-content">
              <div class="modal-header">
                <h5 class="modal-title">MusicBrainz Track Lookup</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
              </div>
              <div class="modal-body" id="trackMBLookupResults">
                <div class="text-center py-4"><span class="spinner-border text-primary"></span></div>
              </div>
            </div>
          </div>
        </div>`);
      modalEl = document.getElementById('trackMBLookupModal');
    }

    const resultsEl = document.getElementById('trackMBLookupResults');
    resultsEl.innerHTML =
      '<div class="text-center py-4"><span class="spinner-border text-primary me-2"></span> ' +
      'Searching MusicBrainz…</div>';
    showModal(modalEl);

    try {
      const data = await global.api.postJson(
        '/api/track/musicbrainz', { title, artist }, { timeoutMs: SEARCH_TIMEOUT_MS }
      );
      const results = data.results || [];
      if (!results.length) {
        resultsEl.innerHTML = '<div class="alert alert-info mb-0">No results found</div>';
        return;
      }

      resultsEl.innerHTML = '<div class="list-group">' + results.map((result) => {
        const confidence = Number(result.confidence) || 0;
        const tone = confidence > 0.8 ? 'success' : (confidence > 0.5 ? 'warning' : 'secondary');
        return `
          <div class="list-group-item p-3">
            <div class="d-flex justify-content-between align-items-start mb-2">
              <div>
                <h6 class="mb-1 fw-bold">${esc(result.title)}</h6>
                <small class="text-muted">by ${esc(result.artist)}</small>
              </div>
              <span class="badge bg-${tone}">${(confidence * 100).toFixed(1)}%</span>
            </div>
            <div class="small text-muted mb-2"><strong>MBID:</strong> <code>${esc(result.mbid)}</code></div>
            <button type="button" class="btn btn-sm btn-primary mb-recording-select-btn"
                    data-mbid="${esc(result.mbid || '')}">Select</button>
          </div>`;
      }).join('') + '</div>';
    } catch (error) {
      resultsEl.innerHTML =
        `<div class="alert alert-danger mb-0">Network error: ${esc(error.message)}</div>`;
    }
  }

  /** Copy a chosen recording MBID into the form's MBID field. */
  function selectRecordingMbid(button) {
    const field = document.getElementById('mbid');
    if (!field) return;
    field.value = button.dataset.mbid || '';
    hideModal(document.getElementById('trackMBLookupModal'));
    notifyInfo('MusicBrainz recording ID filled in — press Save Changes to keep it.');
  }

  /**
   * "Find releases for this recording" (External Linked IDs card).
   *
   * Replaces the deleted openMbReleaseModal() — see the file header for why
   * that function could never have worked against the current endpoint. This
   * opens the shared release picker and writes the selected release into the
   * MusicBrainz Album ID field, which is what the button sits next to.
   */
  function findAlbumReleases() {
    if (typeof global.openGlobalMbSearch !== 'function') {
      notifyError('The MusicBrainz search component is not loaded on this page.');
      return;
    }
    global.openGlobalMbSearch(trackArtist, trackAlbum, function onSelected(selected) {
      const field = document.getElementById('musicbrainz_albumid');
      if (field && selected && selected.id) field.value = selected.id;
      notifyInfo('Release selected — press Save Changes to store the album ID.');
    });
  }

  // ── Lyrics ──────────────────────────────────────────────────────────────

  async function fetchLyricsFromLrclib() {
    const status = document.getElementById('lyricsFetchStatus');
    if (!trackId) return;

    const setStatus = (text, className) => {
      if (!status) return;
      status.textContent = text;
      status.className = className;
    };

    setStatus('Searching LRCLIB…', 'small text-muted');
    try {
      const data = await global.api.postJson(
        `/api/track/${encodeURIComponent(trackId)}/lyrics/fetch`, {}
      );
      if (!data.found) {
        setStatus(data.error || 'No lyrics found on LRCLIB.', 'small text-warning');
        return;
      }
      const textarea = document.getElementById('lyrics');
      if (textarea) textarea.value = data.lyrics || '';
      setStatus(data.synced ? '✓ Saved (synced LRC)' : '✓ Saved (plain)', 'small text-success');
    } catch (error) {
      setStatus('Network error: ' + error.message, 'small text-danger');
    }
  }

  // ── Soulseek ────────────────────────────────────────────────────────────

  function openSlskdSearchTrack() {
    const query = `${trackArtist} ${trackTitle}`.replace(/&/g, ' ').replace(/\s+/g, ' ').trim();
    global.location.href = `/downloads/search?q=${encodeURIComponent(query)}`;
  }

  // ── Similar artists ─────────────────────────────────────────────────────

  function renderSimilarGrid(artists, inCollection) {
    if (!artists.length) return '';
    const cards = artists.map((artist) => {
      const name = artist.name || '';
      const img = `/api/artist/${encodeURIComponent(name)}/image`;
      const placeholder =
        "this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 " +
        'width=%22200%22 height=%22200%22%3E%3Crect fill=%22%232a2a2a%22 width=%22200%22 ' +
        'height=%22200%22/%3E%3C/svg%3E\'';
      return `
        <div class="col-4 col-sm-3 col-md-4 col-lg-3 col-xl-2">
          <div class="card h-100 bg-dark border-secondary overflow-hidden shadow-sm similar-artist-card"
               role="button" tabindex="0"
               data-artist="${esc(name)}" data-in-collection="${inCollection ? '1' : '0'}">
            <img src="${esc(img)}" alt="${esc(name)}" class="card-img-top similar-artist-img" onerror="${placeholder}">
            <div class="card-body p-2 text-center d-flex align-items-center justify-content-center"
                 style="background: rgba(0,0,0,0.8); min-height: 40px;">
              <div class="extra-small fw-bold text-light similar-artist-name" title="${esc(name)}">${esc(name)}</div>
            </div>
          </div>
        </div>`;
    }).join('');
    return `<div class="row g-2">${cards}</div>`;
  }

  function activateSimilarArtist(card) {
    const name = card.dataset.artist || '';
    if (!name) return;
    if (card.dataset.inCollection === '1') {
      global.location.href = `/artist/${encodeURIComponent(name)}`;
      return;
    }
    if (typeof global.openGlobalMbSearch === 'function') {
      global.openGlobalMbSearch(name, '');
      return;
    }
    notifyError('MusicBrainz search is unavailable on this page.');
  }

  /**
   * Fill #similarArtistsContainer.
   *
   * Order of preference for the in-collection split:
   *   1. the server's `in_collection` / `missing` arrays (already annotated by
   *      artist_metadata_service) — one request, authoritative;
   *   2. a per-candidate /api/search/unified lookup, ONLY when the payload
   *      carries neither array (older/partial API responses).
   */
  async function loadSimilarArtists() {
    const container = document.getElementById('similarArtistsContainer');
    if (!container || !trackArtist) return;

    try {
      const data = await global.api.getJson(
        `/api/artist/${encodeURIComponent(trackArtist)}/similar`
      );
      const payload = data.similar_artists || {};
      const serverSplit = Array.isArray(payload.in_collection) || Array.isArray(payload.missing);

      const dedupe = (list) => {
        const seen = new Set();
        const out = [];
        (list || []).forEach((entry) => {
          const name = (typeof entry === 'string' ? entry : (entry && entry.name) || '').trim();
          if (!name || seen.has(name)) return;
          seen.add(name);
          out.push({ name });
        });
        return out;
      };

      let inCollection = [];
      let missing = [];

      if (serverSplit) {
        inCollection = dedupe(payload.in_collection);
        missing = dedupe(payload.missing);
      } else {
        const candidates = dedupe([
          ...(payload.lastfm || []),
          ...(payload.listenbrainz || []),
        ]).slice(0, SIMILAR_LIMIT);

        await Promise.all(candidates.map(async (candidate) => {
          try {
            const search = await global.api.getJson(
              `/api/search/unified?q=${encodeURIComponent(candidate.name)}`
            );
            const match = (search.artists || []).some(
              (a) => String(a.name || '').toLowerCase() === candidate.name.toLowerCase()
            );
            (match ? inCollection : missing).push(candidate);
          } catch (_error) {
            missing.push(candidate);
          }
        }));
      }

      inCollection = inCollection.slice(0, SIMILAR_LIMIT);
      missing = missing.slice(0, SIMILAR_LIMIT);

      if (!inCollection.length && !missing.length) {
        container.innerHTML =
          '<div class="text-center py-3 text-muted small">No similar artists data available.</div>';
        return;
      }

      let html = '';
      if (inCollection.length) {
        html += '<h6 class="text-success small fw-bold text-uppercase mb-3">' +
          '<i class="bi bi-collection-play me-1"></i> In Collection</h6>';
        html += renderSimilarGrid(inCollection, true);
        if (missing.length) html += '<hr class="border-secondary my-4">';
      }
      if (missing.length) {
        html += '<h6 class="text-info small fw-bold text-uppercase mb-3">' +
          '<i class="bi bi-cloud-download me-1"></i> Not In Collection</h6>';
        html += renderSimilarGrid(missing, false);
      }
      container.innerHTML = html;
    } catch (_error) {
      container.innerHTML =
        '<div class="text-danger small py-3 text-center">Failed to load similar artists.</div>';
    }
  }

  // ── Artist navigation (breadcrumb + hero credit links) ──────────────────

  /**
   * Go to the artist page, or open a MusicBrainz lookup when the artist is not
   * in the library at all.
   *
   * This is the old inline `navigateToArtist(name, event)` — it was defined in
   * the artist page's script, not this one, which is why the inline onclick
   * here depended on whatever page happened to have loaded before it. The
   * failure mode is deliberately "navigate anyway": if the existence endpoint
   * errors we send the user to the normal artist route rather than showing an
   * error, because that route is what they asked for.
   */
  async function navigateToArtist(name) {
    if (!name) return;

    let exists = true;
    try {
      const data = await global.api.getJson(
        '/api/artist/exists?artist=' + encodeURIComponent(name)
      );
      exists = !!data.exists;
    } catch (_error) {
      exists = true;
    }

    if (exists) {
      global.location.href = `/artist/${encodeURIComponent(name)}`;
      return;
    }
    if (typeof global.openGlobalMbSearch === 'function') {
      global.openGlobalMbSearch(name, '');
      return;
    }
    notifyError('MusicBrainz search is unavailable on this page.');
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  /** `data-action` in the markup → handler here. No window globals needed. */
  const ACTIONS = {
    'track-artist-nav': (button) => navigateToArtist(button.dataset.artist),
    'track-rename': () => renameTrackFile(),
    'track-slskd-search': () => openSlskdSearchTrack(),
    'track-add-playlist': (button) => {
      if (typeof global.openAddToPlaylistModal === 'function') {
        global.openAddToPlaylistModal(trackId, button.dataset.title || trackTitle);
      } else {
        notifyError('The playlist picker is not loaded on this page.');
      }
    },
    'track-lookup-recording': () => lookupTrackMusicBrainz(),
    'track-find-releases': () => findAlbumReleases(),
    'track-fetch-lyrics': () => fetchLyricsFromLrclib(),
    'track-cover-search': () => searchCoverSong(),
    'track-play': () => {
      if (!global.Player || typeof global.Player.playTrack !== 'function') return;
      global.Player.playTrack({
        id: trackId,
        title: trackTitle,
        artist: trackArtist,
        albumArtUrl: document.getElementById('trackAlbumArtImage')
          ? document.getElementById('trackAlbumArtImage').src
          : '',
      });
    },
  };

  function bindActions() {
    document.addEventListener('click', (event) => {
      const target = event.target;
      if (!target.closest) return;

      const selectors = [
        ['[data-action]', (el) => {
          const handler = ACTIONS[el.getAttribute('data-action')];
          if (!handler) return;
          event.preventDefault();
          handler(el);
        }],
        ['.cover-select-btn', (el) => selectCoverMatch(el)],
        ['.mb-recording-select-btn', (el) => selectRecordingMbid(el)],
        ['.similar-artist-card', (el) => activateSimilarArtist(el)],
      ];

      for (const [selector, run] of selectors) {
        const el = target.closest(selector);
        if (el) {
          event.preventDefault();
          run(el);
          return;
        }
      }
    });

    // The similar-artist "cards" are clickable divs, so they need keyboard
    // activation too — an onclick on a div is not reachable by keyboard.
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const card = event.target.closest && event.target.closest('.similar-artist-card');
      if (!card) return;
      event.preventDefault();
      activateSimilarArtist(card);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    bindActions();

    // "Run Scan" in the Audio Analysis card submits the hidden Essentia form.
    // It was an inline onclick reaching into the DOM by id; keeping it as a
    // listener means the button still works if the form id ever moves.
    const essentiaBtn = document.getElementById('runEssentiaScanBtn');
    const essentiaForm = document.getElementById('essentiaScanForm');
    if (essentiaBtn && essentiaForm) {
      essentiaBtn.addEventListener('click', () => essentiaForm.submit());
    }

    loadSimilarArtists();
  });

  // Programmatic surface. The markup uses data-action; these exist for other
  // modules (and for the console) rather than for inline attributes.
  global.track = {
    data: pageData,
    renameTrackFile,
    searchCoverSong,
    lookupTrackMusicBrainz,
    findAlbumReleases,
    fetchLyricsFromLrclib,
    openSlskdSearchTrack,
    navigateToArtist,
    loadSimilarArtists,
  };
})(window);
