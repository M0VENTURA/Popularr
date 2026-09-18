/* ==========================================================================
   static/js/pages/missing-releases.js
   Missing tracks & albums — two accordions:

     Section 1  albums whose local track count is below the MusicBrainz total
                (gap-detected), expandable to a merged library+missing tracklist
     Section 2  artists with cached missing releases, expandable on demand

   Load order: utils/dom.js → utils/api.js → ui/toast.js → ui/modal.js →
               ui/confirm.js → ui/button-state.js, then this file.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/missing_releases.html carried a 472-line inline <script> with
   14 functions and its own escHtml. The script is this file; the local escHtml
   is gone in favour of utils/dom.js's (equivalent in practice — the local one
   escaped &, <, > and ", which is sufficient for double-quoted attributes).

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. `loadOverview()` HAD NO ERROR HANDLING FOR A NON-JSON BODY. It called
      `.json()` on the response unconditionally, so a session expiry or an HTML
      error page threw "Unexpected token '<'" and was reported as a *parse*
      failure. It also did not check `resp.ok`. Now api.getJson, which names the
      real problem.

   2. `alert()` → toast throughout (five call sites).

   3. INLINE onclick IN GENERATED MARKUP → delegated `data-action`. The original
      built rows with `onclick="toggleGapAlbum(this)"`, `downloadAllMissing(this)`,
      `queueMissingTrackFromMissing(this)`, `onMissingArtistExpand(this)` and
      `importMissingRelease(this)`, then relied on those globals. Delegation means
      re-rendering cannot leave a row unbound, and it removes five window globals.

   4. `downloadAllMissing` SWALLOWED EVERY QUEUE FAILURE in a bare `catch (_) {}`,
      then reported "Queued N/M" — so a partial failure looked like a smaller
      album rather than an error. Failures are now counted and reported.

   5. Download-all now reports the reason when there is nothing to queue
      (`no_mbid` vs complete) instead of one generic message for both.

   ── WHAT DELIBERATELY STAYS ───────────────────────────────────────────────
   * `defaultQueueSource = 'soulseek'` — the queue source is not configurable on
     this page; it matches what the other queue adders send.
   * Sequential queueing in downloadAllMissing: /api/queue/add is a write per
     track and the responses are counted, so parallelising would make the count
     unreliable for no benefit at this size.
   * `importMissingRelease` still routes through the shared MusicBrainz picker
     rather than /api/artist/import-release — the source comment explains that
     the import endpoint created placeholder rows with no audio, while the
     picker → Soulseek path produces playable files.
   ========================================================================== */

(function (global) {
  'use strict';

  const OVERVIEW_ENDPOINT = '/api/missing/overview';
  const MISSING_TRACKS_ENDPOINT = '/api/album/missing-tracks';
  const LIBRARY_TRACKS_ENDPOINT = '/api/album/library-tracks';
  const CACHED_MISSING_ENDPOINT = '/api/artist/cached-missing-releases';
  const QUEUE_ADD_ENDPOINT = '/api/queue/add';

  // base.html loads js/utils/artist-names.js before page modules. "The Offspring"
  // is shown as "Offspring, The" and sorted among the O's, matching what the
  // server does for /api/missing/overview and the /artists sections.
  //
  // The fallback degrades to the RAW name rather than throwing: a display
  // nicety must never be able to take this page down.
  const names = global.artistNames || {
    sortName: (v) => (v === null || v === undefined ? '' : String(v)),
    compare: (a, b) => (a < b ? -1 : a > b ? 1 : 0),
  };
  const DEFAULT_QUEUE_SOURCE = 'soulseek';

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function fmtDuration(secs) {
    if (!secs) return '—';
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  /** Encode a path segment the way the links on this page always have. */
  function seg(value) {
    return encodeURIComponent(String(value == null ? '' : value));
  }

  // ── Main load ───────────────────────────────────────────────────────────

  async function loadOverview() {
    const spinner = document.getElementById('loadingSpinner');
    const errorAlert = document.getElementById('errorAlert');

    try {
      const data = await global.api.getJson(OVERVIEW_ENDPOINT);
      if (data.error) throw new Error(data.error);

      if (spinner) spinner.classList.add('d-none');
      const s1 = document.getElementById('section1');
      const s2 = document.getElementById('section2');
      if (s1) s1.classList.remove('d-none');
      if (s2) s2.classList.remove('d-none');

      renderGapAlbums(data.gap_albums || []);
      renderMissingArtists(data.artists_with_missing_releases || []);
    } catch (error) {
      if (spinner) spinner.classList.add('d-none');
      if (errorAlert) {
        errorAlert.textContent = 'Failed to load missing content: ' + error.message;
        errorAlert.classList.remove('d-none');
      }
    }
  }

  function reloadPage() {
    global.location.reload();
  }

  // ── Section 1: albums with gap-detected missing tracks ──────────────────

  function renderGapAlbums(gapAlbums) {
    const accordion = document.getElementById('gapArtistAccordion');
    const noItems = document.getElementById('noGapAlbums');
    const countBadge = document.getElementById('gapAlbumCount');
    if (!accordion) return;

    accordion.innerHTML = '';

    if (!gapAlbums.length) {
      if (noItems) noItems.classList.remove('d-none');
      if (countBadge) countBadge.textContent = '0';
      return;
    }

    const byArtist = {};
    gapAlbums.forEach((entry) => {
      if (!byArtist[entry.artist]) byArtist[entry.artist] = [];
      byArtist[entry.artist].push(entry);
    });

    if (countBadge) {
      countBadge.textContent =
        `${gapAlbums.length} album${gapAlbums.length !== 1 ? 's' : ''}`;
    }

    // Sort by the FILED key, not localeCompare, so this section's order matches
    // the server-rendered lists ("The Cure" before "The Offspring").
    Object.keys(byArtist).sort(names.compare).forEach((artist, artistIdx) => {
      const albums = byArtist[artist].slice().sort((a, b) => a.album.localeCompare(b.album));
      const safeId = `gap_artist_${artistIdx}`;

      const item = document.createElement('div');
      item.className = 'accordion-item';
      item.innerHTML = `
        <h2 class="accordion-header" id="hdr_${esc(safeId)}">
          <button class="accordion-button" type="button" data-bs-toggle="collapse"
                  data-bs-target="#col_${esc(safeId)}" aria-expanded="true" aria-controls="col_${esc(safeId)}">
            <a href="/artist/${seg(artist)}" class="fw-bold me-2 text-decoration-none"
               data-stop-propagation>${esc(names.sortName(artist))}</a>
            <span class="badge bg-warning text-dark ms-1">${albums.length} album${albums.length !== 1 ? 's' : ''}</span>
          </button>
        </h2>
        <div id="col_${esc(safeId)}" class="accordion-collapse collapse show" aria-labelledby="hdr_${esc(safeId)}">
          <div class="accordion-body p-2" id="body_${esc(safeId)}">
            ${albums.map((alb, albIdx) => renderGapAlbumCard(alb, `${safeId}_${albIdx}`)).join('')}
          </div>
        </div>`;
      accordion.appendChild(item);
    });
  }

  function renderGapAlbumCard(album, cardId) {
    const albumUrl = `/album/${seg(album.artist)}/${seg(album.album)}`;
    return `
      <div class="card mb-2">
        <div class="card-header d-flex align-items-center justify-content-between py-2 px-3">
          <div class="d-flex align-items-center gap-2 flex-wrap">
            <button type="button" class="btn btn-sm btn-link p-0 text-start fw-semibold gap_expand_btn"
                    data-action="mr-toggle-gap"
                    data-card-id="${esc(cardId)}"
                    data-artist="${esc(album.artist)}"
                    data-album="${esc(album.album)}"
                    data-mb-mbid="${esc(album.mb_mbid || '')}"
                    style="text-decoration:none;">
              <i class="bi bi-chevron-right gap_chevron_${esc(cardId)}" style="transition:transform 0.2s;"></i>
              <a href="${esc(albumUrl)}" class="ms-1 text-decoration-none" data-stop-propagation>${esc(album.album)}</a>
            </button>
            <span class="badge bg-secondary">${esc(String(album.library_count))} / ${esc(String(album.max_track))} tracks</span>
            <span class="badge bg-warning text-dark">~${esc(String(album.missing_count_estimate))} missing</span>
          </div>
          <button type="button" class="btn btn-sm btn-outline-warning queue-album-missing-btn"
                  data-action="mr-download-all"
                  data-card-id="${esc(cardId)}"
                  data-artist="${esc(album.artist)}"
                  data-album="${esc(album.album)}"
                  data-mb-mbid="${esc(album.mb_mbid || '')}"
                  title="Download all missing tracks for this album">
            <i class="bi bi-download"></i> Download Missing
          </button>
        </div>
        <div class="card-body p-0 d-none" id="tracklist_${esc(cardId)}">
          <div class="text-center py-3 text-muted gap_tracklist_loading_${esc(cardId)}">
            <span class="spinner-border spinner-border-sm"></span> Loading tracklist…
          </div>
          <table class="table table-dark table-sm mb-0 d-none gap_tracklist_table_${esc(cardId)}">
            <thead>
              <tr>
                <th style="width:3rem">#</th>
                <th>Title</th>
                <th style="width:5rem">Duration</th>
                <th style="width:7rem" class="text-end">Action</th>
              </tr>
            </thead>
            <tbody id="tracklist_tbody_${esc(cardId)}"></tbody>
          </table>
        </div>
      </div>`;
  }

  async function toggleGapAlbum(button) {
    const cardId = button.dataset.cardId;
    const body = document.getElementById('tracklist_' + cardId);
    const chevron = document.querySelector('.gap_chevron_' + cardId);
    if (!body) return;

    if (!body.classList.contains('d-none')) {
      body.classList.add('d-none');
      if (chevron) chevron.style.transform = '';
      return;
    }

    body.classList.remove('d-none');
    if (chevron) chevron.style.transform = 'rotate(90deg)';

    // The tracklist is fetched once per card; a second expand reuses it.
    if (body.dataset.loaded) return;
    body.dataset.loaded = '1';

    await loadGapTracklist(button.dataset.artist, button.dataset.album, cardId);
  }

  async function loadGapTracklist(artist, album, cardId) {
    const loading = document.querySelector('.gap_tracklist_loading_' + cardId);
    const table = document.querySelector('.gap_tracklist_table_' + cardId);
    const tbody = document.getElementById('tracklist_tbody_' + cardId);
    if (!tbody) return;

    try {
      const qs = `artist=${seg(artist)}&album=${seg(album)}`;
      const [data, library] = await Promise.all([
        global.api.getJson(`${MISSING_TRACKS_ENDPOINT}?${qs}`),
        global.api.getJson(`${LIBRARY_TRACKS_ENDPOINT}?${qs}`),
      ]);

      if (loading) loading.classList.add('d-none');
      if (table) table.classList.remove('d-none');

      const allTracks = [
        ...(library.tracks || []).map((t) => ({ ...t, is_missing: false })),
        ...(data.missing_tracks || []).map((t) => ({ ...t, is_missing: true })),
      ];

      // Ordered by disc then track, so the missing rows appear in album order
      // alongside the ones you own rather than bunched at the end.
      allTracks.sort((a, b) => {
        const da = parseInt(a.disc_number || 1, 10);
        const db = parseInt(b.disc_number || 1, 10);
        if (da !== db) return da - db;
        const ta = parseInt(a.track_number || 999, 10);
        const tb = parseInt(b.track_number || 999, 10);
        return ta - tb;
      });

      if (!allTracks.length) {
        tbody.innerHTML =
          '<tr><td colspan="4" class="text-muted text-center py-2">No track data available. Album may not have a MusicBrainz ID.</td></tr>';
        return;
      }

      tbody.innerHTML = allTracks.map((t) => {
        const number = esc(String(t.track_number || '?'));
        const isQueued = String(t.file_path || '').includes('__queued_for_download__');

        if (t.is_missing) {
          return `<tr class="text-muted" style="opacity:0.65;">
            <td class="text-muted">${number}</td>
            <td><em>${esc(t.title)}</em> <span class="badge bg-warning text-dark" style="font-size:0.65rem;">Missing</span></td>
            <td>${fmtDuration(t.duration)}</td>
            <td class="text-end">
              <button type="button" class="btn btn-sm btn-outline-success queue-missing-track-btn"
                      data-action="mr-queue-track"
                      data-artist="${esc(t.artist || artist)}"
                      data-album-artist="${esc(artist)}"
                      data-title="${esc(t.title)}"
                      data-album="${esc(album)}"
                      data-track-number="${esc(String(t.track_number || ''))}"
                      data-disc-number="${esc(String(t.disc_number || ''))}"
                      data-year="${esc(String(t.year || ''))}"
                      data-release-id="${esc(t.release_id || '')}"
                      data-recording-mbid="${esc(t.recording_mbid || '')}"
                      data-duration="${esc(String(t.duration || ''))}"
                      title="Add to download queue">
                <i class="bi bi-download"></i>
              </button>
            </td>
          </tr>`;
        }

        if (isQueued) {
          return `<tr class="table-secondary opacity-75">
            <td class="text-muted">${number}</td>
            <td><span class="text-muted">${esc(t.title)}</span> <span class="badge bg-warning text-dark ms-1" style="font-size:0.65rem;">In Queue</span></td>
            <td>${fmtDuration(t.duration)}</td>
            <td></td>
          </tr>`;
        }

        const title = t.id
          ? `<a href="/track/${esc(String(t.id))}">${esc(t.title)}</a>`
          : esc(t.title);
        return `<tr>
          <td class="text-muted">${number}</td>
          <td>${title}</td>
          <td>${fmtDuration(t.duration)}</td>
          <td></td>
        </tr>`;
      }).join('');
    } catch (error) {
      if (loading) {
        loading.innerHTML =
          `<span class="text-danger">Failed to load tracklist: ${esc(error.message)}</span>`;
      }
    }
  }

  /** Queue one missing track from the expanded tracklist. */
  async function queueMissingTrack(button) {
    const d = button.dataset;
    return global.buttonState.withBusy(button, '', async () => {
      try {
        const data = await global.api.postJson(QUEUE_ADD_ENDPOINT, {
          artist: d.artist || d.albumArtist,
          title: d.title,
          album: d.album,
          album_artist: d.albumArtist || null,
          track_number: d.trackNumber || null,
          disc_number: d.discNumber || null,
          year: d.year || null,
          release_id: d.releaseId || null,
          release_mbid: d.releaseId || null,
          release_source: d.releaseId ? 'musicbrainz' : null,
          recording_mbid: d.recordingMbid || null,
          duration: d.duration ? parseInt(d.duration, 10) : null,
          source: DEFAULT_QUEUE_SOURCE,
        });

        if (!data.success) {
          global.toast.error('Failed to queue track: ' + (data.error || 'unknown error'));
          return;
        }
        global.buttonState.flashDone(button);
        button.title = 'Added to queue';
      } catch (error) {
        global.toast.error('Failed to queue track: ' + error.message);
      }
    });
  }

  // ── Download every missing track on an album ────────────────────────────

  async function downloadAllMissing(button) {
    const { artist, album } = button.dataset;
    const original = button.innerHTML;

    return global.buttonState.withBusy(button, 'Queuing…', async () => {
      let missing = [];
      try {
        const data = await global.api.getJson(
          `${MISSING_TRACKS_ENDPOINT}?artist=${seg(artist)}&album=${seg(album)}`
        );
        missing = data.missing_tracks || [];

        if (!missing.length) {
          const reason = data.reason || '';
          if (reason === 'no_mbid') {
            global.toast.info('No MusicBrainz ID for this album — open the album page and link it to a release first.');
          } else {
            global.toast.info('No missing tracks for this album — it may already be complete, or MusicBrainz data is unavailable.');
          }
          return;
        }
      } catch (error) {
        global.toast.error('Could not load the missing tracks: ' + error.message);
        return;
      }

      // Sequential: one write per track, and the reported count must be exact.
      let queued = 0;
      const failed = [];

      for (const track of missing) {
        try {
          const result = await global.api.postJson(QUEUE_ADD_ENDPOINT, {
            artist: track.artist || artist,
            title: track.title,
            album,
            album_artist: artist,
            track_number: track.track_number || null,
            disc_number: track.disc_number || null,
            year: track.year || null,
            release_id: track.release_id || null,
            release_mbid: track.release_id || null,
            release_source: track.release_id ? 'musicbrainz' : null,
            recording_mbid: track.recording_mbid || null,
            duration: track.duration || null,
            source: DEFAULT_QUEUE_SOURCE,
          });
          if (result.success) queued += 1;
          else failed.push(track.title || 'unknown');
        } catch (_error) {
          failed.push(track.title || 'unknown');
        }
      }

      button.classList.replace('btn-outline-warning', 'btn-warning');
      button.innerHTML = `<i class="bi bi-check-lg"></i> Queued ${queued}/${missing.length}`;

      // The original reported the count and swallowed every failure, so a
      // partial failure looked like a smaller album. Say so explicitly.
      if (failed.length) {
        global.toast.warning(
          `${failed.length} of ${missing.length} track(s) could not be queued.`,
          'Some tracks failed'
        );
        console.warn('[missing-releases] queue failures:', failed);
        button.innerHTML = original;
        button.disabled = false;
      } else {
        global.toast.success(`Queued ${queued} track(s) from "${album}".`);
      }
    });
  }

  // ── Section 2: artists with cached missing releases ─────────────────────

  function renderMissingArtists(artists) {
    const accordion = document.getElementById('missingArtistAccordion');
    const noItems = document.getElementById('noMissingAlbums');
    const countBadge = document.getElementById('missingArtistCount');
    if (!accordion) return;

    accordion.innerHTML = '';

    if (!artists.length) {
      if (noItems) noItems.classList.remove('d-none');
      if (countBadge) countBadge.textContent = '0';
      return;
    }

    if (countBadge) {
      countBadge.textContent = `${artists.length} artist${artists.length !== 1 ? 's' : ''}`;
    }

    artists.forEach((entry, idx) => {
      const safeId = `missing_artist_${idx}`;

      const item = document.createElement('div');
      item.className = 'accordion-item';
      item.innerHTML = `
        <h2 class="accordion-header" id="hdr_${esc(safeId)}">
          <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse"
                  data-bs-target="#col_${esc(safeId)}" aria-expanded="false" aria-controls="col_${esc(safeId)}"
                  data-action="mr-expand-artist"
                  data-artist="${esc(entry.artist)}"
                  data-safe-id="${esc(safeId)}">
            <a href="/artist/${seg(entry.artist)}" class="fw-bold me-2 text-decoration-none"
               data-stop-propagation>${esc(names.sortName(entry.artist))}</a>
            <span class="badge bg-info ms-1">${esc(String(entry.missing_count ?? 0))} missing</span>
          </button>
        </h2>
        <div id="col_${esc(safeId)}" class="accordion-collapse collapse" aria-labelledby="hdr_${esc(safeId)}">
          <div class="accordion-body p-2" id="missing_body_${esc(safeId)}">
            <div class="text-center py-3 text-muted missing_loading_${esc(safeId)}">
              <span class="spinner-border spinner-border-sm"></span> Loading…
            </div>
            <div id="missing_releases_${esc(safeId)}"></div>
          </div>
        </div>`;
      accordion.appendChild(item);
    });
  }

  async function onMissingArtistExpand(button) {
    const { safeId, artist } = button.dataset;
    if (!safeId || button.dataset.loaded) return;
    button.dataset.loaded = '1';
    await loadMissingReleases(artist, safeId);
  }

  async function loadMissingReleases(artist, safeId) {
    const loading = document.querySelector('.missing_loading_' + safeId);
    const container = document.getElementById('missing_releases_' + safeId);
    if (!container) return;

    try {
      const data = await global.api.getJson(
        `${CACHED_MISSING_ENDPOINT}?artist=${seg(artist)}`
      );
      if (loading) loading.classList.add('d-none');

      const missing = data.missing || [];
      if (!missing.length) {
        container.innerHTML =
          '<p class="text-muted p-2">No cached missing releases. Run the Missing Releases scan from the dashboard.</p>';
        return;
      }

      container.innerHTML = missing.map((release) => {
        const typeLabel = release.primary_type || release.category || 'Album';
        const year = release.first_release_date ? release.first_release_date.substring(0, 4) : '';
        return `
          <div class="d-flex align-items-center justify-content-between py-2 px-2 border-bottom flex-wrap gap-2">
            <div>
              <span class="fw-semibold">${esc(release.title)}</span>
              <span class="badge bg-secondary ms-1" style="font-size:0.7rem;">${esc(typeLabel)}</span>
              ${year ? `<span class="text-muted ms-1" style="font-size:0.85rem;">${esc(year)}</span>` : ''}
            </div>
            <button type="button" class="btn btn-sm btn-outline-success import-missing-release-btn"
                    data-action="mr-import-release"
                    data-artist="${esc(artist)}"
                    data-release-id="${esc(release.id || '')}"
                    data-title="${esc(release.title)}"
                    title="Download all tracks for this release">
              <i class="bi bi-download"></i> Download
            </button>
          </div>`;
      }).join('');
    } catch (error) {
      if (loading) {
        loading.innerHTML = `<span class="text-danger">Failed to load: ${esc(error.message)}</span>`;
      }
    }
  }

  /**
   * Download one cached missing release.
   *
   * Routes through the shared MusicBrainz picker rather than
   * /api/artist/import-release: that endpoint created placeholder DB rows with
   * no audio, whereas picker → Soulseek download produces playable files. Kept
   * as-is from the original, with the guarded fallbacks intact.
   */
  function importMissingRelease(button) {
    const { artist, title } = button.dataset;

    if (typeof global.openGlobalMbSearch !== 'function') {
      global.toast.error('MusicBrainz search is not available on this page.');
      return;
    }

    global.openGlobalMbSearch(artist, title, function (selectedRelease) {
      if (!selectedRelease) return;

      if (typeof global.downloadMbRelease === 'function') {
        global.downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
      } else if (typeof global.downloadReleaseViaSoulseek === 'function') {
        global.downloadReleaseViaSoulseek(selectedRelease.id, selectedRelease.title, selectedRelease.artist);
      } else {
        global.toast.error('Soulseek download is not available on this page.');
      }
    });
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  const ACTIONS = {
    'mr-reload': () => reloadPage(),
    'mr-toggle-gap': (el) => toggleGapAlbum(el),
    'mr-download-all': (el) => downloadAllMissing(el),
    'mr-queue-track': (el) => queueMissingTrack(el),
    'mr-expand-artist': (el) => onMissingArtistExpand(el),
    'mr-import-release': (el) => importMissingRelease(el),
  };

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;

    // Anchors inside an accordion header must not toggle the accordion.
    if (event.target.closest('[data-stop-propagation]')) {
      event.stopPropagation();
      return;
    }

    const el = event.target.closest('[data-action]');
    if (!el) return;
    const handler = ACTIONS[el.getAttribute('data-action')];
    if (!handler) return;
    event.preventDefault();
    handler(el);
  });

  document.addEventListener('DOMContentLoaded', loadOverview);

  global.missingReleases = {
    loadOverview,
    renderGapAlbums,
    renderGapAlbumCard,
    toggleGapAlbum,
    loadGapTracklist,
    queueMissingTrack,
    downloadAllMissing,
    renderMissingArtists,
    loadMissingReleases,
    importMissingRelease,
  };
})(window);
