/* ==========================================================================
   static/js/pages/artist.js
   Artist page — bio, country, IDs, image, favourite, similar artists,
   covers, missing-release discovery, tracklists, and the track edit modal.

   Load order:
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/modal.js  →  ui/confirm.js  →  ui/button-state.js
       services/musicbrainz-queue.js
       services/slskd.js
       services/genres.js
       pages/artist.js

   ── EXTRACTED FROM artist_detail.html ─────────────────────────────────────
   This page's JavaScript lived in TWO inline <script> blocks totalling
   ~2,000 lines, separated by a <style> block and a <script src> tag. That
   separation is why several functions ended up defined twice: the two
   blocks share one global scope but were edited independently.

   ── TEMPLATE CHANGES REQUIRED ─────────────────────────────────────────────
   1. CLOSE THE GENRES CARD. The "Genres and Moods" section is missing TWO
      closing </div> tags — the card-body closes, but #artist-genres-section
      and its .card never do:

          <div id="recommendedArtistGenresSection" …>
            …
            <button … id="applyArtistGenresBtn" …>…</button>
          </div>      ← closes recommendedArtistGenresSection
        </div>        ← closes card-body
        <!-- Top Tracks Section -->

      Add two more </div> before the Top Tracks comment.

      This is not cosmetic. Every section after it — Top Tracks, Albums,
      Compilations, Live, Remix, EPs, Singles, Covers, Appears On — is
      currently a DESCENDANT of the genres card. Two visible consequences:

        a) They inherit the card's background and padding, and
           `#artist-genres-section .card-title { font-size: 1.2rem }` in
           artist.css applies to every card title below it.

        b) The section-reorder handler at the bottom of the page does
           `parent.insertBefore(topTracksSection, …)` where `parent` is
           #artist-similar-section's parent. Because Top Tracks is nested
           inside the genres card, that call MOVES it out into the main
           container at runtime — so the rendered order is partly repaired
           by accident, while Albums and everything below stay trapped
           inside the genres card.

   2. DELETE both <style> blocks — already extracted to CSS/artist.css.

   3. DELETE <script src="…/genre-utils.js"> — services/genres.js replaces
      it. See the note on the fallback stubs below.

   4. Replace every `{{ artist_name|tojson }}` in JS with the data
      attribute. The template ALREADY has it on the <h1>:
          <h1 class="h2 mb-2" data-artist-name="{{ artist_name|e }}">
      so `artistName()` below just reads that.

   5. The recommendation chips previously used the class
      `badge-outline-primary`, WHICH DOES NOT EXIST in Bootstrap or in
      popularr.css — that is why the old code also set inline
      `border:2px solid #0d6efd; color:#0d6efd`. services/genres.js emits
      `.genre-chip` instead; make sure the `.genre-chip` rule is in
      popularr.css.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. escapeHtml WAS DEFINED TWICE, and the broken one won.
        block 1:  div.textContent = text; return div.innerHTML
                  → coerces any type, does NOT escape quotes
        block 2:  return text.replace(/[&<>"']/g, m => map[m])
                  → escapes quotes, THROWS on a non-string
      Block 2 parses later, so its definition overwrote block 1's for the
      whole page. Every call passing a number or null threw
      "text.replace is not a function" — including
      escapeHtml(track.position) in displayMusicBrainzResults, where
      position is routinely a number. utils/dom.js coerces AND escapes
      quotes.

   2. downloadSlskdFile WAS ALSO DEFINED TWICE, both in block 1:
        line ~3075:  downloadSlskdFile(username, filename, size)
        line ~4843:  downloadSlskdFile(username, filename, size, btn, originalHTML)
      The 5-arg version wins. pollSlskdResults renders buttons that call it
      with THREE arguments, so `btn` is undefined and `btn.disabled = false`
      throws inside the .then() — meaning the manual Soulseek search results
      on this page were completely un-downloadable. services/slskd.js
      reconciles both signatures.

   3. FIVE HANDLERS READ THE IMPLICIT GLOBAL `event`:
        importMissingRelease    event.target.closest('button')
        createEssentialPlaylist event.target.closest('button')
        downloadTrack           event.target.closest('button')
        checkMissingReleases    window.event?.target?.closest('button')
        importRelease           window.event ? … : document.activeElement…
      `window.event` is legacy and non-standard: undefined in Firefox, and
      undefined in any async continuation. checkMissingReleases is also
      called on page load with no event at all, where its optional chaining
      silently yields undefined and the progress spinner never appears.
      All five now take the button as an argument.

   4. applySelectedArtistGenres RESTORED THE WRONG LABEL. Its error paths
      hard-coded "Apply Selected to All Artist Tracks (MP3 Files)" but the
      button in the template reads "(MP3/FLAC Files)". One failed request
      permanently rewrote the label. buttonState.withBusy restores the
      captured markup instead.

   5. toggleArtistGenreSelection BUILT AN ATTRIBUTE SELECTOR WITH escapeHtml:
          document.querySelector(`[data-artist-genre="${escapeHtml(genre)}"]`)
      HTML escaping is not CSS escaping. A genre containing a quote or a
      bracket produced an invalid selector and threw. services/genres.js
      uses CSS.escape.

   6. THE genre-utils.js FALLBACK STUBS WERE ALWAYS DEAD. Block 1 defines
      toggleGenreCheckbox / getSelectedGenres / handleGenreRemoval behind
      `if (typeof X === 'undefined')` guards — but the <script src> for
      genre-utils.js comes AFTER block 1, so the stubs always installed
      first and were then overwritten. The handleGenreRemoval stub merely
      alert()s the genre list instead of removing anything, so if that file
      ever failed to load, removal silently no-opped.
   ========================================================================== */

(function (global) {
  'use strict';

  const SIMILAR_TIMEOUT_MS = 15000;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  /** The artist name, from data-artist-name on the page heading. */
  function artistName() {
    const el = document.querySelector('[data-artist-name]');
    return el ? (el.dataset.artistName || '') : '';
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

  function withBusy(btn, label, fn) {
    if (btn && global.buttonState) return global.buttonState.withBusy(btn, label, fn);
    return fn();
  }

  /** Slugify a value for use in an element id. */
  function safeForDomId(value) {
    return String(value || '').replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  }

  // ── Artist IDs ──────────────────────────────────────────────────────────

  function openEditArtistIdsModal() {
    const artist = artistName();
    const mbId = document.getElementById('musicbrainzArtistId')?.value || '';
    const dcEl = document.getElementById('discogsArtistId');
    const dcId = dcEl ? dcEl.value : '';

    const bodyHtml = `
      <div class="mb-3">
        <label for="editMusicbrainzArtistId" class="form-label">MusicBrainz Artist ID</label>
        <input type="text" class="form-control" id="editMusicbrainzArtistId"
               value="${esc(mbId)}" placeholder="e.g., a74b1b7f-71a5-4011-9441-d0b5e4122711">
        <div class="form-text">
          <a href="https://musicbrainz.org/search?query=${encodeURIComponent(artist)}&type=artist"
             target="_blank" rel="noopener">Search MusicBrainz</a> to find the artist ID
        </div>
      </div>
      <div class="mb-3">
        <label for="editDiscogsArtistId" class="form-label">Discogs Artist ID</label>
        <input type="text" class="form-control" id="editDiscogsArtistId"
               value="${esc(dcId)}" placeholder="e.g., 123456">
        <div class="form-text">
          <a href="https://www.discogs.com/search/?q=${encodeURIComponent(artist)}&type=artist"
             target="_blank" rel="noopener">Search Discogs</a> to find the artist ID
        </div>
      </div>`;

    const footerHtml =
      '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
      '<button type="button" class="btn btn-outline-info" data-lookup-ids>' +
      '<i class="bi bi-cloud-download"></i> Lookup and Save</button>' +
      '<button type="button" class="btn btn-primary" data-save-ids>' +
      '<i class="bi bi-save"></i> Save Changes</button>';

    const el = global.modal.open('editArtistIdsModal', global.modal.template({
      id: 'editArtistIdsModal',
      title: 'Edit Artist IDs',
      icon: 'bi-pencil',
      bodyHtml,
      footerHtml,
    }));
    if (!el) return;

    el.querySelector('[data-save-ids]')
      .addEventListener('click', function () { saveArtistIds(this); });
    el.querySelector('[data-lookup-ids]')
      .addEventListener('click', function () { lookupAndSaveArtistIds(this); });
  }

  function saveArtistIds(btn) {
    const artist = artistName();
    const mbId = document.getElementById('editMusicbrainzArtistId').value.trim();
    const dcId = document.getElementById('editDiscogsArtistId').value.trim();

    return withBusy(btn, 'Saving…', async () => {
      try {
        const data = await global.api.postJson('/api/artist/update-ids', {
          artist,
          lastfm_artist_mbid: mbId,
          musicbrainz_artist_id: mbId,
          discogs_artist_id: dcId,
        });
        if (!data.success) {
          notifyError(data.error || 'Failed to update IDs');
          return;
        }

        const mbField = document.getElementById('musicbrainzArtistId');
        const dcField = document.getElementById('discogsArtistId');
        if (mbField) mbField.value = mbId;
        if (dcField) dcField.value = dcId;

        global.modal.hide('editArtistIdsModal');
        notifySuccess('Artist IDs updated');
        setTimeout(() => global.location.reload(), 1000);
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  function lookupAndSaveArtistIds(btn) {
    return withBusy(btn, 'Looking up…', async () => {
      try {
        const data = await global.api.postJson('/api/artist/lookup-ids', { artist: artistName() });
        if (!data.success) {
          notifyError(data.error || 'Lookup failed');
          return;
        }

        const mbid = data.musicbrainz_artist_id || '';
        const discogs = data.discogs_artist_id || '';

        [['editMusicbrainzArtistId', mbid], ['musicbrainzArtistId', mbid],
         ['editDiscogsArtistId', discogs], ['discogsArtistId', discogs]]
          .forEach(([id, value]) => {
            const el = document.getElementById(id);
            if (el && value) el.value = value;
          });

        notifySuccess('Lookup complete — IDs saved to artist tracks');
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  // ── Download search modal (qBittorrent + Soulseek) ─────────────────────

  let currentDownloadAlbum = { artist: null, album: null };

  function openDownloadSearch(artist, album) {
    currentDownloadAlbum = { artist, album };

    const nameEl = document.getElementById('downloadArtistName');
    if (nameEl) nameEl.textContent = artist + (album ? ' - ' + album : '');

    const query = album ? artist + ' ' + album : artist;

    const qbitInput = document.getElementById('qbitSearchInput');
    if (qbitInput) {
      qbitInput.value = query;
      document.getElementById('qbitResults').innerHTML = '';
    }

    const slskdInput = document.getElementById('slskdSearchInput');
    if (slskdInput) {
      slskdInput.value = query;
      document.getElementById('slskdResults').innerHTML = '';
      document.getElementById('slskdTracksContainer').innerHTML = '';
    }

    const modalEl = document.getElementById('downloadModal');
    if (modalEl && global.modal) global.modal.show(modalEl);

    // Search once the modal is actually shown. The old fixed 300ms timeout
    // raced the Bootstrap transition on a slow render.
    modalEl.addEventListener('shown.bs.modal', function runOnce() {
      const activeTab = document.querySelector('#downloadTabs .nav-link.active');
      if (!activeTab) return;
      if (activeTab.id === 'qbit-tab') {
        performQbitSearch();
      } else if (activeTab.id === 'slskd-tab') {
        if (album) showSlskdTrackQueueOption();
        runSlskdSearch();
      }
    }, { once: true });
  }

  function showSlskdTrackQueueOption() {
    const container = document.getElementById('slskdTracksContainer');
    if (!container) return;

    container.innerHTML = `
      <div class="alert alert-info d-flex justify-content-between align-items-center mb-3">
        <div>
          <i class="bi bi-info-circle"></i>
          Queue individual tracks from <strong>${esc(currentDownloadAlbum.album)}</strong>
          to the Soulseek download queue
        </div>
        <button class="btn btn-sm btn-success" id="slskdQueueTracksBtn">
          <i class="bi bi-plus-circle"></i> Queue Tracks
        </button>
      </div>
      <div id="slskdTrackQueueStatus"></div>`;

    container.querySelector('#slskdQueueTracksBtn')
      .addEventListener('click', function () { queueAlbumTracksToSlskd(this); });
  }

  /**
   * Queue every track of the current album.
   *
   * Tracks are queued SEQUENTIALLY, not in parallel — the original did the
   * same via a recursive queueNextTrack(). Kept deliberately: the queue
   * endpoint performs a duplicate check per item, and firing 20 concurrent
   * requests races that check.
   */
  async function queueAlbumTracksToSlskd(btn) {
    if (!currentDownloadAlbum.album) {
      notifyError('No album selected');
      return;
    }

    const statusDiv = document.getElementById('slskdTrackQueueStatus');
    statusDiv.innerHTML =
      '<div class="text-center py-3"><div class="spinner-border spinner-border-sm text-primary" role="status"></div>' +
      '<p class="mt-2">Fetching album tracklist…</p></div>';

    return withBusy(btn, 'Queuing…', async () => {
      let tracks;
      try {
        const params = new URLSearchParams({
          artist: currentDownloadAlbum.artist,
          album: currentDownloadAlbum.album,
        });
        const data = await global.api.getJson(`/api/album/tracklist?${params}`);
        tracks = data.tracks || [];
        if (!tracks.length) throw new Error(data.error || 'No tracks found');
      } catch (error) {
        statusDiv.innerHTML =
          '<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> ' +
          `Could not fetch tracklist: ${esc(error.message)}</div>`;
        return;
      }

      statusDiv.innerHTML = `
        <div class="alert alert-info" id="trackQueueSummary">
          <strong>Adding ${tracks.length} track${tracks.length === 1 ? '' : 's'} to the download queue…</strong>
        </div>
        <div class="table-responsive">
          <table class="table table-sm">
            <thead><tr><th style="width:40px;"></th><th>Track</th><th class="text-center">Status</th></tr></thead>
            <tbody>
              ${tracks.map((track, i) => `
                <tr>
                  <td class="text-center"><small class="text-muted">${i + 1}</small></td>
                  <td><small>${esc(track.title || `Track ${i + 1}`)}</small></td>
                  <td class="text-center"><small id="queue-status-${i}" class="text-muted">pending…</small></td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>`;

      let queued = 0;
      for (let i = 0; i < tracks.length; i += 1) {
        const statusEl = document.getElementById(`queue-status-${i}`);
        const title = tracks[i].title || `Track ${i + 1}`;

        statusEl.textContent = 'queuing…';
        statusEl.className = 'text-warning';

        try {
          const result = await global.api.postJson('/api/queue/add', {
            artist: currentDownloadAlbum.artist,
            title,
            album: currentDownloadAlbum.album,
            source: 'soulseek',
            priority: 5,
          });
          if (!result.success && !result.queue_id) {
            throw new Error(result.error || 'Unknown error');
          }
          queued += 1;
          statusEl.textContent = 'queued';
          statusEl.className = 'text-success';
        } catch (_error) {
          statusEl.textContent = 'failed';
          statusEl.className = 'text-danger';
        }
      }

      // Targeted by id. The original selected `.alert-info strong`, which
      // matches the FIRST such element on the page — not necessarily this one.
      const summary = document.querySelector('#trackQueueSummary strong');
      if (summary) {
        summary.textContent =
          `Added ${queued} of ${tracks.length} track${tracks.length === 1 ? '' : 's'} to the download queue`;
      }
    });
  }

  /** Soulseek search inside the download modal, via services/slskd.js. */
  function runSlskdSearch() {
    const input = document.getElementById('slskdSearchInput');
    const query = input ? input.value : '';
    if (!query) return;

    const loading = document.getElementById('slskdLoading');
    const results = document.getElementById('slskdResults');
    if (loading) loading.style.display = 'block';
    if (results) results.innerHTML = '';

    return global.slskd.search(query, {
      context: 'artist-page',
      filterBusy: false,
      onComplete: (session) => {
        if (loading) loading.style.display = 'none';
        global.slskd.renderTable(results, session);
      },
      onError: (error) => {
        if (loading) loading.style.display = 'none';
        if (results) {
          results.innerHTML =
            `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> ${esc(error.message)}</div>`;
        }
      },
    });
  }

  // ── qBittorrent ─────────────────────────────────────────────────────────

  async function performQbitSearch() {
    const query = document.getElementById('qbitSearchInput')?.value;
    if (!query) return;

    const loading = document.getElementById('qbitLoading');
    const resultsEl = document.getElementById('qbitResults');
    if (loading) loading.style.display = 'block';
    if (resultsEl) resultsEl.innerHTML = '';

    try {
      const data = await global.api.postJson('/api/qbittorrent/search', { query });
      if (loading) loading.style.display = 'none';

      if (data.error) {
        resultsEl.innerHTML =
          `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> ${esc(data.error)}</div>`;
        return;
      }

      const results = data.results || [];
      if (!results.length) {
        resultsEl.innerHTML =
          '<div class="alert alert-info"><i class="bi bi-info-circle"></i> No results found. Try a different search query.</div>';
        return;
      }

      const rows = results.map((r, index) => {
        const size = global.formatBytes ? global.formatBytes(r.fileSize || 0) : `${r.fileSize || 0} B`;
        const seeds = Number(r.nbSeeders) || 0;
        const seedClass = seeds > 10 ? 'text-success' : (seeds > 0 ? 'text-warning' : 'text-danger');
        return `
          <tr>
            <td>
              <div class="small text-truncate" style="max-width:500px;">${esc(r.fileName || 'Unknown')}</div>
              <div class="text-muted" style="font-size:0.75rem;">${esc(r.siteUrl || '')}</div>
            </td>
            <td class="text-center">${esc(size)}</td>
            <td class="text-center ${seedClass}"><i class="bi bi-arrow-up-circle"></i> ${seeds}</td>
            <td class="text-center"><i class="bi bi-arrow-down-circle"></i> ${Number(r.nbLeechers) || 0}</td>
            <td class="text-center">
              <button class="btn btn-sm btn-success qbit-add-btn" data-index="${index}"
                      ${!r.fileUrl ? 'disabled' : ''}>
                <i class="bi bi-plus-circle"></i> Add
              </button>
            </td>
          </tr>`;
      }).join('');

      resultsEl.innerHTML = `
        <div class="table-responsive">
          <table class="table table-hover">
            <thead><tr><th>Name</th><th class="text-center">Size</th>
              <th class="text-center">Seeds</th><th class="text-center">Peers</th>
              <th class="text-center">Action</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <div class="text-muted small mt-2">Found ${results.length} results</div>`;

      // data-index rather than an inline onclick carrying escapeJsString
      // output — a torrent URL can contain quotes and backslashes.
      resultsEl.querySelectorAll('.qbit-add-btn').forEach((btn) => {
        btn.addEventListener('click', function () {
          addTorrent(results[parseInt(this.dataset.index, 10)]?.fileUrl, this);
        });
      });
    } catch (error) {
      if (loading) loading.style.display = 'none';
      if (resultsEl) {
        resultsEl.innerHTML =
          `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> ${esc(error.message)}</div>`;
      }
    }
  }

  async function addTorrent(url, btn) {
    if (!url) return;

    const accepted = await confirmFn({
      title: 'Add torrent',
      message: 'Add this torrent to qBittorrent?',
      tone: 'primary',
      confirmLabel: 'Add',
    });
    if (!accepted) return;

    return withBusy(btn, '', async () => {
      try {
        const data = await global.api.postJson('/api/qbittorrent/add', { url });
        if (!data.success) {
          notifyError(data.error || 'Failed to add torrent');
          return;
        }
        notifySuccess('Torrent added');
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  // ── Bio and singles ─────────────────────────────────────────────────────

  async function loadArtistBio(artist) {
    const container = document.getElementById('artistBio');
    if (!container) return;

    try {
      const data = await global.api.getJson(`/api/artist/bio?name=${encodeURIComponent(artist)}`);
      if (data.bio && data.bio.length > 0) {
        const sanitize = global.sanitizeBio || esc;
        let html = `<p>${sanitize(data.bio)}</p>`;
        if (data.source) {
          html += `<p class="small text-muted mt-2"><em>Source: ${esc(data.source)}</em></p>`;
        }
        container.innerHTML = html;
      } else {
        container.innerHTML =
          '<p class="text-muted"><em>No biography available for this artist.</em></p>';
      }
    } catch (_error) {
      container.innerHTML =
        '<p class="text-muted"><em>Unable to load artist biography.</em></p>';
    }
  }

  /**
   * Populate the singles count badge.
   *
   * NOTE: #singlesCount does not exist anywhere in artist_detail.html, so
   * this request is currently made and its result discarded. Either add the
   * badge to the template or delete this function and its call.
   */
  async function loadSinglesCount(artist) {
    const badge = document.getElementById('singlesCount');
    if (!badge) return;
    try {
      const data = await global.api.getJson(
        `/api/artist/singles-count?name=${encodeURIComponent(artist)}`
      );
      if (data.count !== undefined) badge.textContent = data.count;
    } catch (error) {
      console.error('Failed to load singles count:', error);
    }
  }

  function showArtistSingles(artist) {
    global.location.href = `/artist/${encodeURIComponent(artist)}/singles`;
  }

  async function createEssentialPlaylist(artist, btn) {
    const accepted = await confirmFn({
      title: 'Create Essential Playlist',
      message: `Create an Essential Playlist for ${artist}?`,
      detail: 'Single detection will be used to pick the best tracks.',
      tone: 'primary',
      confirmLabel: 'Create',
    });
    if (!accepted) return;

    return withBusy(btn, 'Creating…', async () => {
      try {
        const data = await global.api.postJson('/api/artist/create-essential-playlist',
          { artist });
        if (!data.success) {
          notifyError(data.error || 'Failed to create playlist');
          return;
        }
        notifySuccess(`${data.message || 'Playlist created'} — ${data.playlist_name || ''}`);

        if (data.navidrome_url) {
          const open = await confirmFn({
            title: 'Open playlist',
            message: 'Open the playlist in Navidrome?',
            tone: 'primary',
            confirmLabel: 'Open',
          });
          if (open) global.open(data.navidrome_url, '_blank', 'noopener');
        }
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  // ── Artist image ────────────────────────────────────────────────────────

  function openArtistImageModal(artist) {
    const bodyHtml = `
      <div class="mb-3">
        <label for="manualImageUrl" class="form-label">Manual Image URL</label>
        <input type="text" class="form-control" id="manualImageUrl"
               placeholder="https://example.com/image.jpg">
      </div>
      <div class="d-flex gap-2 mb-4 flex-wrap">
        <button class="btn btn-primary" data-apply-url><i class="bi bi-check"></i> Apply URL</button>
        <button class="btn btn-secondary" data-img-source="musicbrainz">
          <i class="bi bi-search"></i> MusicBrainz</button>
        <button class="btn btn-secondary" data-img-source="discogs">
          <i class="bi bi-disc"></i> Discogs</button>
        <button class="btn btn-secondary" data-img-source="applemusic">
          <i class="bi bi-apple"></i> Apple Music</button>
      </div>
      <div id="artistImageResults"></div>`;

    const el = global.modal.open('artistImageModal', global.modal.template({
      id: 'artistImageModal',
      title: 'Change Artist Image',
      icon: 'bi-image',
      bodyHtml,
      size: 'modal-lg',
    }));
    if (!el) return;

    el.querySelector('[data-apply-url]').addEventListener('click', function () {
      const url = document.getElementById('manualImageUrl').value.trim();
      if (!url) {
        notifyError('Please enter an image URL');
        return;
      }
      applyArtistImage(artist, url, this);
    });

    el.querySelectorAll('[data-img-source]').forEach((btn) => {
      btn.addEventListener('click', function () {
        searchArtistImages(artist, this.dataset.imgSource, this);
      });
    });
  }

  function searchArtistImages(artist, source, btn) {
    const resultsDiv = document.getElementById('artistImageResults');
    resultsDiv.innerHTML =
      '<div class="text-center"><span class="spinner-border"></span> Searching…</div>';

    return withBusy(btn, '', async () => {
      try {
        const data = await global.api.getJson(
          `/api/artist/search-images?name=${encodeURIComponent(artist)}&source=${encodeURIComponent(source)}`
        );
        const images = data.images || [];
        if (!images.length) {
          resultsDiv.innerHTML = '<div class="alert alert-info">No images found</div>';
          return;
        }

        resultsDiv.innerHTML = '<div class="row g-3">' + images.map((img, index) => `
          <div class="col-6 col-md-4">
            <div class="card">
              <img src="${esc(img.url)}" class="card-img-top"
                   style="height:200px;object-fit:cover;" alt="">
              <div class="card-body p-2">
                <button class="btn btn-sm btn-primary w-100 artist-img-pick" data-index="${index}">
                  <i class="bi bi-check"></i> Use This
                </button>
              </div>
            </div>
          </div>`).join('') + '</div>';

        resultsDiv.querySelectorAll('.artist-img-pick').forEach((pick) => {
          pick.addEventListener('click', function () {
            applyArtistImage(artist, images[parseInt(this.dataset.index, 10)].url, this);
          });
        });
      } catch (error) {
        resultsDiv.innerHTML = `<div class="alert alert-danger">Error: ${esc(error.message)}</div>`;
      }
    });
  }

  function applyArtistImage(artist, imageUrl, btn) {
    return withBusy(btn, '', async () => {
      try {
        const data = await global.api.postJson('/api/artist/set-image',
          { artist, image_url: imageUrl });
        if (!data.success) {
          notifyError(data.error || 'Failed to update image');
          return;
        }
        // Cache-bust so the new image is fetched rather than the cached one.
        const img = document.getElementById('artistImage');
        if (img) img.src = imageUrl + '?t=' + Date.now();
        global.modal.hide('artistImageModal');
        notifySuccess('Artist image updated');
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  // ── Country / origin ────────────────────────────────────────────────────

  /** Read the country from the badge, skipping the <i> icon element. */
  function currentCountryText() {
    const badge = document.querySelector('#artistCountryDisplay .badge');
    if (!badge) return '';
    return Array.from(badge.childNodes)
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent.trim())
      .join('');
  }

  function fetchArtistCountry() {
    const btn = document.getElementById('fetchArtistCountryBtn');
    const container = document.getElementById('artistCountryDisplay');
    container.innerHTML =
      '<span class="text-muted small">Loading country information from MusicBrainz…</span>';

    return withBusy(btn, 'Fetching…', async () => {
      try {
        const data = await global.api.postJson('/api/artist/country',
          { artist_name: artistName() });

        if (data.success && data.country) {
          container.innerHTML = `
            <span class="badge bg-info" style="font-size:1rem;padding:0.5rem 1rem;">
              <i class="bi bi-geo-alt"></i> ${esc(data.country)}
            </span>
            <p class="text-muted small mt-2 mb-0">This can be used as a genre tag for classification</p>`;
          notifySuccess(data.message || 'Country updated');
        } else {
          container.innerHTML =
            '<div class="alert alert-warning mb-0"><i class="bi bi-exclamation-triangle"></i> ' +
            `${esc(data.error || 'No country information found')}</div>`;
        }
      } catch (error) {
        container.innerHTML =
          `<div class="alert alert-danger mb-0"><i class="bi bi-x-circle"></i> Error: ${esc(error.message)}</div>`;
      }
    });
  }

  function editArtistCountry() {
    const bodyHtml = `
      <div class="mb-3">
        <label for="countryInput" class="form-label">Country/Origin</label>
        <input type="text" class="form-control" id="countryInput"
               value="${esc(currentCountryText())}"
               placeholder="e.g., United States, United Kingdom, Japan">
        <div class="form-text">Enter the artist's country or region of origin</div>
      </div>`;

    const el = global.modal.open('editCountryModal', global.modal.template({
      id: 'editCountryModal',
      title: 'Edit Artist Country/Origin',
      icon: 'bi-pencil',
      bodyHtml,
      footerHtml: global.modal.footer({ confirmLabel: 'Save', confirmId: 'saveCountryBtn' }),
    }));
    if (!el) return;

    el.querySelector('#saveCountryBtn')
      .addEventListener('click', function () { saveArtistCountry(this); });
  }

  function saveArtistCountry(btn) {
    const country = document.getElementById('countryInput').value.trim();
    if (!country) {
      notifyError('Please enter a country');
      return;
    }

    return withBusy(btn, 'Saving…', async () => {
      try {
        const data = await global.api.postJson('/api/artist/country/update',
          { artist_name: artistName(), country });
        if (!data.success) {
          notifyError(data.error || 'Failed to update country');
          return;
        }

        global.modal.hide('editCountryModal');
        document.getElementById('artistCountryDisplay').innerHTML = `
          <div class="d-flex align-items-center gap-2 mb-2">
            <span class="badge bg-info" style="font-size:0.95rem;padding:0.4rem 0.8rem;">
              ${esc(country)}
            </span>
          </div>`;
        notifySuccess('Country updated');
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  /**
   * Apply the artist's country as a genre tag on every track.
   *
   * NOTE: no button in artist_detail.html calls this. Either wire it up or
   * delete it — it has been unreachable dead code.
   */
  async function applyCountryAsGenre() {
    const country = currentCountryText();
    if (!country) {
      notifyError('No country information available');
      return;
    }

    const accepted = await confirmFn({
      title: 'Apply country as genre',
      message: `Apply "${country}" as a genre tag to all tracks by ${artistName()}?`,
      tone: 'primary',
      confirmLabel: 'Apply',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson('/api/artist/country/apply-as-genre',
        { artist_name: artistName() });
      if (!data.success) {
        notifyError(data.error || 'Failed to apply genre');
        return;
      }
      notifySuccess(`${data.message} — updated ${data.tracks_updated} track(s)`);
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  // ── Favourite ───────────────────────────────────────────────────────────

  async function loadArtistFavouriteState(artist) {
    try {
      const data = await global.api.getJson(
        '/api/artist/favourite?artist=' + encodeURIComponent(artist)
      );
      updateFavouriteButtonState(data.is_favourite);
    } catch (error) {
      console.warn('Could not load favourite state:', error);
    }
  }

  function updateFavouriteButtonState(isFavourite) {
    const btn = document.getElementById('artistFavouriteBtn');
    const icon = document.getElementById('artistFavouriteIcon');
    const label = document.getElementById('artistFavouriteLabel');
    if (!btn) return;

    btn.classList.toggle('btn-danger', !!isFavourite);
    btn.classList.toggle('btn-outline-danger', !isFavourite);
    if (icon) {
      icon.classList.toggle('bi-heart-fill', !!isFavourite);
      icon.classList.toggle('bi-heart', !isFavourite);
    }
    if (label) label.textContent = isFavourite ? 'Favourited' : 'Favourite';
  }

  /**
   * Toggle the artist favourite.
   *
   * This is the ARTIST favourite (auto-queue new albums), backed by
   * /api/artist/favourite. It is unrelated to the album/track favourites
   * that were removed from album.js and main.js.
   */
  async function toggleArtistFavourite(artist) {
    const btn = document.getElementById('artistFavouriteBtn');
    const isFavourite = btn && btn.classList.contains('btn-danger');

    // Optimistic flip, reverted on failure — the original never reverted,
    // so a failed request left the heart showing the wrong state.
    updateFavouriteButtonState(!isFavourite);

    try {
      if (isFavourite) {
        await global.api.deleteJson(
          '/api/artist/favourite?artist=' + encodeURIComponent(artist)
        );
      } else {
        await global.api.postJson('/api/artist/favourite', { artist });
      }
    } catch (error) {
      updateFavouriteButtonState(isFavourite);
      notifyError('Error updating favourite status: ' + error.message);
    }
  }

  // ── Covered by ──────────────────────────────────────────────────────────

  async function loadArtistCoveredBy(artist) {
    const container = document.getElementById('artistCoveredByContainer');
    const countEl = document.getElementById('artistCoveredByCount');
    if (!container) return;

    try {
      const data = await global.api.getJson(
        '/api/artist/covered-by?artist=' + encodeURIComponent(artist)
      );
      const covers = data.covers || [];

      if (!covers.length) {
        container.innerHTML = `
          <div class="p-3 text-muted text-center">
            <i class="bi bi-info-circle"></i> No covers of ${esc(artist)}'s songs found in your library yet.
            <br><small>Cover songs are detected automatically during popularity scans when
            lyricist/writer metadata is available.</small>
          </div>`;
        if (countEl) countEl.textContent = 'No covers found in library';
        return;
      }

      if (countEl) {
        countEl.textContent = `${covers.length} cover${covers.length === 1 ? '' : 's'} found in library`;
      }

      container.innerHTML = `
        <div class="table-responsive">
          <table class="table table-hover mb-0">
            <thead><tr><th>Covering Artist</th><th>Song Title</th><th>Album</th>
              <th class="text-center">Year</th></tr></thead>
            <tbody>
              ${covers.map((cover) => {
                const artistUrl = `/artist/${encodeURIComponent(cover.artist)}`;
                const albumCell = cover.album
                  ? `<a href="/album/${encodeURIComponent(cover.artist)}/${encodeURIComponent(cover.album)}" ` +
                    `class="text-decoration-none text-muted">${esc(cover.album)}</a>`
                  : '—';
                return `
                  <tr>
                    <td><a href="${esc(artistUrl)}" class="text-decoration-none">${esc(cover.artist)}</a></td>
                    <td>${esc(cover.title)}</td>
                    <td>${albumCell}</td>
                    <td class="text-center">${esc(cover.year || '—')}</td>
                  </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>`;
    } catch (error) {
      console.error('Error loading covered-by data:', error);
      container.innerHTML =
        '<div class="p-3 text-danger text-center"><i class="bi bi-exclamation-triangle"></i> ' +
        `Error loading covers: ${esc(error.message)}</div>`;
    }
  }

  // ── Missing releases ────────────────────────────────────────────────────

  const CATEGORY_TO_SECTION = {
    album: 'albums',
    live_album: 'live-albums',
    remix_album: 'remix-albums',
    ep: 'eps',
    single: 'singles',
    compilation: 'compilations',
  };

  /**
   * Classify a MusicBrainz release into one of the page's album sections.
   * Falls back to inspecting the TITLE when the stored category is the
   * generic "album" — MusicBrainz often omits the secondary type.
   */
  function missingCategory(item) {
    const raw = String(item.category || item.primary_type || 'album').toLowerCase();
    if (raw.includes('compilation')) return 'compilation';
    if (raw.includes('live')) return 'live_album';
    if (raw.includes('remix')) return 'remix_album';
    if (raw.includes('single')) return 'single';
    if (raw.includes('ep')) return 'ep';

    const title = String(item.title || '').toLowerCase();
    if (title.includes('live') || title.includes('unplugged')) return 'live_album';
    if (title.includes('remix')) return 'remix_album';
    return 'album';
  }

  function rowExists(tbody, albumTitle) {
    const wanted = String(albumTitle || '').trim().toLowerCase();
    return Array.from(tbody.querySelectorAll('tr.album-row')).some((row) =>
      String(row.getAttribute('data-album') || '').trim().toLowerCase() === wanted
    );
  }

  /** Re-sort a tbody newest-first, keeping each album row with its tracklist row. */
  function sortTbodyByYear(tbody) {
    const pairs = Array.from(tbody.querySelectorAll('tr.album-row')).map((row) => {
      const next = row.nextElementSibling;
      return {
        row,
        tracklistRow: (next && next.classList.contains('tracklist-row')) ? next : null,
      };
    });

    pairs.sort((a, b) =>
      (parseInt(b.row.getAttribute('data-year') || '0', 10)) -
      (parseInt(a.row.getAttribute('data-year') || '0', 10))
    );

    tbody.innerHTML = '';
    pairs.forEach(({ row, tracklistRow }) => {
      tbody.appendChild(row);
      if (tracklistRow) tbody.appendChild(tracklistRow);
    });
  }

  function buildMissingRow(artist, item) {
    const year = (item.first_release_date || '').slice(0, 4) || '????';
    const fallbackArt = '/api/album-art-placeholder';
    const artUrl = item.cover_art_url || fallbackArt;

    const row = document.createElement('tr');
    row.className = 'album-row';
    row.setAttribute('data-year', year === '????' ? '0' : year);
    row.setAttribute('data-status', 'missing');
    row.setAttribute('data-source', 'live-missing');
    row.setAttribute('data-album', item.title || '');

    row.innerHTML = `
      <td class="text-center" style="width:100px;padding:0.5rem;">
        <img src="${esc(artUrl)}" alt="${esc(item.title)}" class="img-thumbnail album-art-missing"
             style="width:80px;height:80px;object-fit:cover;border:1px solid var(--border-color);background-color:#2a2a2a;">
      </td>
      <td>${esc(item.title)}</td>
      <td class="text-center">${esc(year)}</td>
      <td class="text-center"><span class="badge bg-warning text-dark">Missing</span></td>
      <td class="text-center">—</td>
      <td class="text-center"><span class="text-muted">—</span></td>
      <td class="text-center">
        <div class="btn-group btn-group-sm" role="group">
          <button class="btn btn-outline-info missing-toggle-tracklist" title="Show tracklist">
            <i class="bi bi-list-ul"></i>
          </button>
          <button class="btn btn-outline-success missing-import" title="Import this release">
            <i class="bi bi-download"></i>
          </button>
          <button class="btn btn-outline-secondary missing-mb-search" title="Search MusicBrainz">
            <i class="bi bi-search"></i>
          </button>
        </div>
      </td>`;

    // Broken-artwork fallback without an inline onerror attribute.
    const img = row.querySelector('.album-art-missing');
    img.addEventListener('error', function handle() {
      this.removeEventListener('error', handle);
      this.src = fallbackArt;
    });

    const safeArtist = safeForDomId(artist);
    const safeAlbum = safeForDomId(item.title);
    const contentId = `tracklist-content-${safeArtist}-${safeAlbum}`;

    const tracklistRow = document.createElement('tr');
    tracklistRow.className = 'tracklist-row';
    tracklistRow.id = `tracklist-${safeArtist}-${safeAlbum}`;
    tracklistRow.setAttribute('data-source', 'live-missing');
    tracklistRow.style.display = 'none';
    tracklistRow.innerHTML = `
      <td colspan="7" style="padding:1rem;">
        <div class="tracklist-container">
          <button class="btn btn-sm btn-outline-info me-2 btn-tracklist-load">
            <span class="spinner-border spinner-border-sm me-2" style="display:none;"></span>
            <i class="bi bi-music-note-list"></i> Load Tracklist from MusicBrainz
          </button>
          <div id="${esc(contentId)}" class="mt-3"></div>
        </div>
      </td>`;

    // All four handlers hold the artist/title/id in a CLOSURE. The original
    // built onclick attributes from encodeURIComponent(JSON.stringify(x))
    // with a manual .replace(/'/g, '%27') on each — three layers of escaping
    // to smuggle a string through an HTML attribute.
    row.querySelector('.missing-toggle-tracklist')
      .addEventListener('click', () => toggleTracklist(artist, item.title));
    row.querySelector('.missing-import')
      .addEventListener('click', function () { importRelease(artist, item.id, item.title, this); });
    row.querySelector('.missing-mb-search')
      .addEventListener('click', () => global.musicbrainz.search(null, artist, item.title));
    tracklistRow.querySelector('.btn-tracklist-load')
      .addEventListener('click', function () { loadTracklist(artist, item.title, this, item.id); });

    return { row, tracklistRow };
  }

  /**
   * Check MusicBrainz for releases missing from the library and inject them
   * inline into the matching album sections.
   *
   * @param {string} artist
   * @param {Object} [opts]
   * @param {boolean} [opts.silent]     suppress the completion toast
   * @param {boolean} [opts.background] ask the server for a cached result
   * @param {HTMLElement} [opts.button]
   */
  async function checkMissingReleases(artist, opts = {}) {
    // The button is now PASSED IN. The original read
    // `window.event?.target?.closest('button')`, which is undefined in
    // Firefox, undefined in async continuations, and undefined on the
    // page-load call — where the optional chaining hid the failure.
    const run = async () => {
      try {
        let url = '/api/artist/missing-releases?artist=' + encodeURIComponent(artist);
        if (opts.background) url += '&background=1';

        const data = await global.api.getJson(url);
        if (data.error) throw new Error(data.error);

        // Clear previously injected rows before re-inserting.
        document.querySelectorAll('tr[data-source="live-missing"]')
          .forEach((row) => row.remove());

        let added = 0;
        (data.missing || []).forEach((item) => {
          const sectionKey = CATEGORY_TO_SECTION[missingCategory(item)];
          const tbody = document.querySelector(`#${sectionKey}-section .${sectionKey}-tbody`);
          if (!tbody || rowExists(tbody, item.title)) return;

          const { row, tracklistRow } = buildMissingRow(artist, item);
          tbody.appendChild(row);
          tbody.appendChild(tracklistRow);
          added += 1;
        });

        Object.values(CATEGORY_TO_SECTION).forEach((sectionKey) => {
          const tbody = document.querySelector(`#${sectionKey}-section .${sectionKey}-tbody`);
          if (tbody) sortTbodyByYear(tbody);
        });

        initializeMissingToggle();

        if (!opts.silent) {
          notifySuccess(added > 0
            ? `Added ${added} missing release(s) inline from MusicBrainz`
            : 'No new missing releases found.');
        }
      } catch (error) {
        if (!opts.silent) notifyError('Error checking missing releases: ' + error.message);
      }
    };

    return opts.button ? withBusy(opts.button, 'Checking…', run) : run();
  }

  /** Import a missing release's full tracklist into the library. */
  async function importRelease(artist, releaseId, title, btn) {
    const accepted = await confirmFn({
      title: 'Import release',
      message: `Import "${title}" by ${artist}?`,
      detail: 'The full tracklist will be fetched from MusicBrainz and added to your library.',
      tone: 'primary',
      confirmLabel: 'Import',
    });
    if (!accepted) return;

    return withBusy(btn, '', async () => {
      try {
        const data = await global.api.postJson('/api/artist/import-release', {
          artist, release_id: releaseId, title,
        });
        if (data.error) {
          notifyError('Error importing release: ' + data.error);
          return;
        }
        notifySuccess(
          data.message || `Imported ${data.tracks_imported || 0} tracks from "${title}"`
        );
        setTimeout(() => global.location.reload(), 1000);
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  // ── Missing-row visibility toggle ───────────────────────────────────────

  const MISSING_CATEGORIES = [
    'albums', 'compilations', 'live-albums', 'remix-albums', 'eps', 'singles',
  ];

  function setMissingVisibility(category, show) {
    const section = document.getElementById(category + '-section');
    if (!section) return;

    const tbody = section.querySelector('.' + category + '-tbody');
    const button = section.querySelector('.toggle-missing-btn');
    if (!tbody || !button) return;

    tbody.querySelectorAll('tr[data-status="missing"]').forEach((row) => {
      row.style.display = show ? '' : 'none';
    });
    button.setAttribute('data-show', show ? 'true' : 'false');
    button.innerHTML = show
      ? '<i class="bi bi-eye"></i> <span class="d-none d-sm-inline">Hide Missing</span>'
      : '<i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>';
  }

  function toggleMissing(category) {
    const section = document.getElementById(category + '-section');
    const button = section && section.querySelector('.toggle-missing-btn');
    if (!button) return;

    const show = button.getAttribute('data-show') !== 'true';
    setMissingVisibility(category, show);
    try {
      localStorage.setItem('showMissing-' + category, String(show));
    } catch (_e) {
      /* private browsing */
    }
  }

  function initializeMissingToggle() {
    MISSING_CATEGORIES.forEach((category) => {
      let show = false;
      try {
        show = localStorage.getItem('showMissing-' + category) === 'true';
      } catch (_e) {
        /* private browsing */
      }
      setMissingVisibility(category, show);
    });
  }

  // ── Tracklists ──────────────────────────────────────────────────────────

  function tracklistIds(artist, album) {
    const safeArtist = safeForDomId(artist);
    const safeAlbum = safeForDomId(album);
    return {
      rowId: `tracklist-${safeArtist}-${safeAlbum}`,
      btnId: `toggle-btn-${safeArtist}-${safeAlbum}`,
      contentId: `tracklist-content-${safeArtist}-${safeAlbum}`,
    };
  }

  function toggleTracklist(artist, album) {
    const { rowId, btnId, contentId } = tracklistIds(artist, album);
    const row = document.getElementById(rowId);
    const btn = document.getElementById(btnId);
    if (!row) return;

    const isHidden = row.style.display === 'none';
    row.style.display = isHidden ? '' : 'none';
    if (!btn) return;

    if (!isHidden) {
      btn.classList.remove('active');
      btn.innerHTML = '<i class="bi bi-list-ul"></i>';
      return;
    }

    btn.classList.add('active');
    btn.innerHTML = '<i class="bi bi-chevron-up"></i>';

    // Auto-load on first expand.
    const contentDiv = document.getElementById(contentId);
    if (!contentDiv || contentDiv.innerHTML.trim() !== '') return;

    const loadBtn = row.querySelector('button.btn-tracklist-load');
    if (loadBtn) loadBtn.click();
    else loadTracklist(artist, album, null, null);
  }

  function buildTracklistRowHtml(artist, track, matched, queued) {
    const normalized = (track.title || '').toLowerCase();
    const isQueued = queued.has(normalized);
    const isMatched = matched.has(normalized) && !isQueued;

    const matchBadge = isMatched
      ? '<span class="badge bg-success ms-2"><i class="bi bi-check-circle"></i> In Library</span>'
      : (isQueued
        ? '<span class="badge bg-warning text-dark ms-2"><i class="bi bi-hourglass-split"></i> In Queue</span>'
        : '');

    const trackId = track.track_id || '';
    const actions = isMatched ? `
      <div class="d-flex gap-2" style="flex-shrink:0;">
        <button class="btn btn-sm btn-outline-primary tracklist-edit-btn"
                data-track-id="${esc(trackId)}" title="Edit track">
          <i class="bi bi-pencil"></i>
        </button>
        <a href="/track/${encodeURIComponent(trackId)}" class="btn btn-sm btn-outline-info"
           title="View track page"><i class="bi bi-arrow-right"></i></a>
      </div>` : '';

    return `
      <div class="list-group-item ${isQueued ? 'list-group-item-warning' : ''}">
        <div class="d-flex justify-content-between align-items-center">
          <div class="flex-grow-1">
            <span class="text-muted small" style="min-width:2em;text-align:right;display:inline-block;">
              ${esc(String(track.position || '').padStart(2, '0'))}
            </span>
            <span class="ms-2">${esc(track.title || '')}</span>
            ${matchBadge}
          </div>
          <div class="d-flex align-items-center gap-2">
            <small class="text-muted me-2 text-truncate" style="max-width:120px;">${esc(track.artist || '')}</small>
            ${actions}
            <button class="btn btn-sm btn-outline-success tracklist-download-btn"
                    data-title="${esc(track.title || '')}" title="Download this track"
                    style="flex-shrink:0;" ${isQueued ? 'disabled' : ''}>
              <i class="bi bi-download"></i>
            </button>
          </div>
        </div>
      </div>`;
  }

  async function loadTracklist(artist, album, button, mbid) {
    const { contentId } = tracklistIds(artist, album);
    const contentDiv = document.getElementById(contentId);
    if (!contentDiv) return;

    const spinner = button ? button.querySelector('.spinner-border') : null;
    if (spinner) spinner.style.display = 'inline-block';
    if (button) button.disabled = true;

    try {
      let url = `/api/album/tracklist?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`;
      if (mbid) url += `&mbid=${encodeURIComponent(mbid)}`;

      const [tracklistResponse, matchResponse] = await Promise.all([
        global.api.getJson(url),
        global.api.getJson(
          `/api/album/tracklist/match?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`
        ),
      ]);

      if (tracklistResponse.error) {
        contentDiv.innerHTML =
          `<div class="alert alert-warning"><i class="bi bi-exclamation-triangle"></i> ${esc(tracklistResponse.error)}</div>`;
        return;
      }

      const tracklist = tracklistResponse.tracklist || [];
      if (!tracklist.length) {
        contentDiv.innerHTML = '<div class="alert alert-info">No tracks found</div>';
        return;
      }

      const matched = new Set((matchResponse.matched || []).map((t) => (t.title || '').toLowerCase()));
      const queued = new Set((matchResponse.queued || []).map((t) => (t.title || '').toLowerCase()));

      contentDiv.innerHTML = `
        <div class="card border-info">
          <div class="card-header bg-info bg-opacity-10">
            <h6 class="mb-0"><i class="bi bi-list-ul"></i> Tracklist (${tracklist.length} tracks)</h6>
          </div>
          <div class="list-group list-group-flush">
            ${tracklist.map((t) => buildTracklistRowHtml(artist, t, matched, queued)).join('')}
          </div>
          <div class="card-footer text-muted small">
            <i class="bi bi-info-circle"></i> Green = already in library. Yellow = already in queue.
            Click download to add missing tracks from Soulseek.
          </div>
        </div>`;

      contentDiv.querySelectorAll('.tracklist-edit-btn').forEach((btn) => {
        btn.addEventListener('click', function () {
          openEditTrackFromArtistModal(this.dataset.trackId);
        });
      });

      contentDiv.querySelectorAll('.tracklist-download-btn').forEach((btn) => {
        btn.addEventListener('click', function () {
          downloadTrack(artist, this.dataset.title, this);
        });
      });
    } catch (error) {
      contentDiv.innerHTML =
        `<div class="alert alert-danger"><i class="bi bi-x-circle"></i> Error: ${esc(error.message)}</div>`;
    } finally {
      if (spinner) spinner.style.display = 'none';
      // The original restored `button.disabled` in the catch WITHOUT a null
      // check, so a failed auto-load (button === null) threw a second error.
      if (button) button.disabled = false;
    }
  }

  /** Search Soulseek for one track and queue the best match. */
  async function downloadTrack(artist, title, btn) {
    const accepted = await confirmFn({
      title: 'Download track',
      message: `Download "${title}" by ${artist}?`,
      tone: 'primary',
      confirmLabel: 'Download',
    });
    if (!accepted) return;

    return global.slskd.searchAndDownloadBest(artist, title, { button: btn });
  }

  function openSlskdSearch(query, artist) {
    const searchQuery = global.slskd.normalizeQuery(
      artist ? `${artist} ${query}` : query
    );
    global.location.href = `/downloads?search=${encodeURIComponent(searchQuery)}`;
  }

  // ── All releases on MusicBrainz ─────────────────────────────────────────

  function searchMusicBrainzForAllReleases(artist) {
    const modalEl = document.getElementById('musicBrainzModal');
    if (!modalEl) {
      notifyError('MusicBrainz search modal not found on this page.');
      return;
    }

    ['mbSearchInfo', 'mbSearchError'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });
    const results = document.getElementById('mbSearchResults');
    if (results) results.innerHTML = '';
    const status = document.getElementById('mbSearchStatus');
    if (status) status.style.display = 'none';

    if (global.modal) global.modal.show(modalEl);

    // Search once shown, rather than on a fixed 500ms timer.
    modalEl.addEventListener('shown.bs.modal', function runOnce() {
      performMusicBrainzSearchForArtist(artist);
    }, { once: true });
  }

  async function performMusicBrainzSearchForArtist(artist) {
    const info = document.getElementById('mbSearchInfo');
    const status = document.getElementById('mbSearchStatus');
    const errorEl = document.getElementById('mbSearchError');
    const results = document.getElementById('mbSearchResults');
    const artistEl = document.getElementById('mbSearchArtist');

    if (artistEl) artistEl.textContent = artist;
    if (info) info.style.display = 'block';
    if (status) status.style.display = 'block';

    try {
      const data = await global.api.getJson(
        '/api/artist/missing-releases?artist=' + encodeURIComponent(artist)
      );
      if (status) status.style.display = 'none';
      if (data.error) throw new Error(data.error);

      const missing = data.missing || [];
      if (!missing.length) {
        if (results) {
          results.innerHTML =
            '<div class="alert alert-info"><i class="bi bi-info-circle"></i> ' +
            'No releases found for this artist on MusicBrainz, or all releases are already in your library.</div>';
        }
        return;
      }

      const byType = Object.create(null);
      missing.forEach((release) => {
        const type = release.category || release.primary_type || 'Album';
        if (!byType[type]) byType[type] = [];
        byType[type].push(release);
      });

      const sections = Object.entries(byType).map(([type, releases]) => {
        const itemId = 'accordion-' + type.replace(/\s+/g, '-').toLowerCase();
        const open = type === 'Album';

        const rows = releases.map((release, index) => {
          const year = release.first_release_date
            ? release.first_release_date.substring(0, 4)
            : 'Unknown';
          const cover = release.cover_art_url
            ? `<img src="${esc(release.cover_art_url)}" alt="" style="width:40px;height:40px;object-fit:cover;border-radius:3px;margin-right:0.5rem;">`
            : '<i class="bi bi-disc" style="font-size:1.5rem;margin-right:0.5rem;opacity:0.5;"></i>';

          return `
            <div class="list-group-item d-flex align-items-center justify-content-between">
              <div class="d-flex align-items-center" style="flex:1;min-width:0;">
                ${cover}
                <div style="min-width:0;flex:1;">
                  <div class="text-truncate"><strong>${esc(release.title)}</strong></div>
                  <small class="text-muted">${esc(year)}</small>
                </div>
              </div>
              <button class="btn btn-sm btn-outline-success ms-2 mb-all-download"
                      data-type="${esc(type)}" data-index="${index}"
                      title="Search for this release">
                <i class="bi bi-download"></i> Download
              </button>
            </div>`;
        }).join('');

        return `
          <div class="accordion-item">
            <h2 class="accordion-header">
              <button class="accordion-button${open ? '' : ' collapsed'}" type="button"
                      data-bs-toggle="collapse" data-bs-target="#${esc(itemId)}">
                <i class="bi bi-disc"></i>&nbsp;${esc(type)} (${releases.length})
              </button>
            </h2>
            <div id="${esc(itemId)}" class="accordion-collapse collapse${open ? ' show' : ''}"
                 data-bs-parent="#mbResultsAccordion">
              <div class="accordion-body p-0">
                <div class="list-group list-group-flush">${rows}</div>
              </div>
            </div>
          </div>`;
      }).join('');

      if (results) {
        results.innerHTML = `<div class="accordion" id="mbResultsAccordion">${sections}</div>`;
        results.querySelectorAll('.mb-all-download').forEach((btn) => {
          btn.addEventListener('click', function () {
            const release = byType[this.dataset.type][parseInt(this.dataset.index, 10)];
            if (release) global.musicbrainz.search(null, artist, release.title);
          });
        });
      }
    } catch (error) {
      if (status) status.style.display = 'none';
      if (errorEl) {
        errorEl.textContent = error.message;
        errorEl.style.display = 'block';
      }
    }
  }

  // ── Similar artists ─────────────────────────────────────────────────────

  function normalizeSimilarArtist(entry, source) {
    if (typeof entry === 'string') {
      const name = entry.trim();
      return name ? { name, match: 0, source, in_collection: false } : null;
    }
    if (!entry || typeof entry !== 'object') return null;

    const name = String(entry.name || entry.artist || '').trim();
    if (!name) return null;

    const matchValue = Number(entry.match ?? entry.score ?? 0);
    return Object.assign({}, entry, {
      name,
      match: Number.isFinite(matchValue) ? matchValue : 0,
      source,
      in_collection: !!entry.in_collection,
    });
  }

  async function loadSimilarArtists(artist) {
    const container = document.getElementById('artistSimilarArtistsContainer');
    if (!container) return;

    try {
      const data = await global.api.getJson(
        `/api/artist/${encodeURIComponent(artist)}/similar`,
        { timeoutMs: SIMILAR_TIMEOUT_MS }
      );
      displayArtistSimilarArtists(container, data);
    } catch (error) {
      container.innerHTML =
        `<div class="alert alert-danger mb-0"><i class="bi bi-exclamation-circle"></i> ${esc(error.message)}</div>`;
    }
  }

  function displayArtistSimilarArtists(container, data) {
    if (!data || !data.similar_artists) {
      container.innerHTML =
        '<div class="alert alert-info mb-0"><i class="bi bi-info-circle"></i> No similar artists data available</div>';
      return;
    }

    const all = [
      ...(data.similar_artists.lastfm || []).map((a) => normalizeSimilarArtist(a, 'lastfm')),
      ...(data.similar_artists.listenbrainz || []).map((a) => normalizeSimilarArtist(a, 'listenbrainz')),
    ].filter(Boolean);

    // Deduplicate by name, preferring the in-collection entry.
    const seen = new Map();
    all.forEach((a) => {
      const key = a.name.toLowerCase();
      if (!seen.has(key) || a.in_collection) seen.set(key, a);
    });
    const unique = Array.from(seen.values());

    if (!unique.length) {
      container.innerHTML =
        '<div class="alert alert-info mb-0"><i class="bi bi-info-circle"></i> No similar artists available yet.' +
        '<br><small>Similar artists are collected during popularity scans from Last.fm and ListenBrainz.</small></div>';
      return;
    }

    const inCollection = unique.filter((a) => a.in_collection);
    const notInCollection = unique.filter((a) => !a.in_collection);
    let html = '';

    if (inCollection.length) {
      html += `
        <div class="mb-4">
          <h6 class="mb-2">
            <i class="bi bi-collection-fill text-success"></i> Similar Artists (In Collection)
            <span class="badge bg-success">${inCollection.length}</span>
          </h6>
          <div class="d-flex flex-wrap gap-2">
            ${inCollection.map((a) => `
              <a href="/artist/${encodeURIComponent(a.name)}"
                 class="badge bg-success text-decoration-none similar-artist-pill">${esc(a.name)}</a>
            `).join('')}
          </div>
        </div>`;
    }

    if (notInCollection.length) {
      html += `
        <div>
          <h6 class="mb-3">
            <i class="bi bi-person-plus text-secondary"></i> Similar Artists (Not in Collection)
            <span class="badge bg-secondary">${notInCollection.length}</span>
          </h6>
          <div class="row g-3">
            ${notInCollection.map((a, index) => {
              const pct = a.match ? Math.round(a.match * 100) : 0;
              const badge = pct > 0
                ? `<span class="badge bg-success position-absolute" style="top:8px;right:8px;">${pct}%</span>`
                : '';
              return `
                <div class="col-6 col-md-4 col-lg-3">
                  <div class="card h-100 overflow-hidden" style="position:relative;">
                    ${badge}
                    <div class="similar-artist-art" style="aspect-ratio:1;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);display:flex;align-items:center;justify-content:center;position:relative;">
                      <img src="/api/artist/image?name=${encodeURIComponent(a.name)}"
                           alt="${esc(a.name)}" style="width:100%;height:100%;object-fit:cover;">
                      <i class="bi bi-person-circle similar-artist-fallback"
                         style="font-size:3rem;color:white;opacity:0.8;display:none;"></i>
                    </div>
                    <div class="card-body d-flex flex-column">
                      <h6 class="card-title mb-2 fw-bold text-truncate" title="${esc(a.name)}">${esc(a.name)}</h6>
                      <div class="btn-group-vertical btn-group-sm mt-auto" role="group">
                        <button class="btn btn-outline-secondary btn-sm similar-find-releases"
                                data-index="${index}" title="Search MusicBrainz for releases">
                          <i class="bi bi-download"></i> Find Releases
                        </button>
                        <a href="https://www.last.fm/music/${encodeURIComponent(a.name)}"
                           target="_blank" rel="noopener" class="btn btn-outline-info btn-sm">
                          <i class="bi bi-box-arrow-up-right"></i> Last.fm
                        </a>
                      </div>
                    </div>
                  </div>
                </div>`;
            }).join('')}
          </div>
        </div>`;
    }

    container.innerHTML = html;

    // Delegated image fallback — the original used an inline onerror that
    // reached for `this.nextElementSibling`, which broke if the markup order
    // ever changed. Capture is required: error events do not bubble.
    container.addEventListener('error', function (e) {
      const img = e.target;
      if (!img || img.tagName !== 'IMG') return;
      const wrap = img.closest('.similar-artist-art');
      if (!wrap) return;
      img.style.display = 'none';
      const fallback = wrap.querySelector('.similar-artist-fallback');
      if (fallback) fallback.style.display = 'block';
    }, true);

    container.querySelectorAll('.similar-find-releases').forEach((btn) => {
      btn.addEventListener('click', function () {
        const target = notInCollection[parseInt(this.dataset.index, 10)];
        if (target) global.musicbrainz.search(null, target.name, '');
      });
    });
  }

  // ── Corrections banner + per-album missing-track counts ────────────────

  async function initCorrectionsAndMissingTracks() {
    const artist = artistName();
    if (!artist) return;

    const banner = document.getElementById('artist-corrections-banner');
    if (banner) banner.style.display = 'block';

    const mbRows = document.querySelectorAll('tr[data-mb-mbid]');
    if (!mbRows.length) return;

    const counts = await Promise.all(Array.from(mbRows).map(async (row) => {
      const albumName = row.dataset.albumName;
      const mbMbid = row.dataset.mbMbid;
      if (!albumName || !mbMbid) return 0;

      try {
        const data = await global.api.getJson(
          `/api/album/missing-tracks?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(albumName)}`
        );
        const count = data.missing_count || 0;
        if (count > 0) {
          const badge = row.querySelector('.album-missing-tracks-badge');
          if (badge) {
            badge.textContent = `${count} track${count === 1 ? '' : 's'} missing`;
            badge.style.display = 'inline-block';
          }
          const searchBtn = row.querySelector('.album-search-missing-btn');
          if (searchBtn) searchBtn.style.display = 'inline-block';
        }
        return count;
      } catch (_error) {
        return 0;
      }
    }));

    const total = counts.reduce((a, b) => a + b, 0);
    if (total > 0) {
      const badge = document.getElementById('artist-missing-tracks-badge');
      if (badge) {
        badge.textContent = `${total} missing track${total === 1 ? '' : 's'} in library`;
        badge.style.display = 'inline-block';
      }
    }
  }

  // ── Album tag conflicts ─────────────────────────────────────────────────
  //
  // GET /api/correcting/albums is the JSON twin of the /correcting page. Until
  // it existed, album-level tag conflicts (the thing that makes Navidrome show
  // one album as several entries) could only be seen by leaving this page for
  // Tag Corrections — the per-album "N tracks missing" check right above has a
  // badge, this had nothing.
  //
  // ONE request for the whole artist page, not one per album row: the endpoint
  // returns every conflicting album for the artist at once. The missing-tracks
  // check next door still does a request per row because its API is per-album,
  // which is worth revisiting if this page ever gets slow.
  //
  // The match key is "album_artist::album", lowercased, matching how the API
  // keys its map. The rows already carry data-artist and data-album-name from
  // components/_album_category_section.html's render_album_row.

  /** The API's map key for one album. */
  function albumConflictKey(albumArtist, album) {
    return `${String(albumArtist || '').trim().toLowerCase()}::` +
      `${String(album || '').trim().toLowerCase()}`;
  }

  /**
   * Badge the album rows whose album-level tags disagree.
   * @param {string} artist  the page's artist, used to scope the request
   * @returns {Promise<number>} how many albums were flagged
   */
  async function annotateAlbumTagConflicts(artist) {
    if (!artist) return 0;

    const rows = Array.from(document.querySelectorAll('tr.album-row'))
      .filter((row) => row.dataset.albumName);
    if (!rows.length) return 0;

    let byAlbum = {};
    try {
      const data = await global.api.getJson(
        `/api/correcting/albums?artist=${encodeURIComponent(artist)}`
      );
      byAlbum = data.albums || {};
    } catch (error) {
      // An indicator only — never let it affect the rest of the page.
      console.warn('[artist] album tag conflicts unavailable:', error.message);
      return 0;
    }

    let flagged = 0;
    rows.forEach((row) => {
      const badge = row.querySelector('.album-tag-conflicts-badge');
      if (!badge) return;

      const key = albumConflictKey(row.dataset.artist || artist, row.dataset.albumName);
      const entry = byAlbum[key];
      const labels = entry && Array.isArray(entry.field_labels) ? entry.field_labels : [];

      if (!labels.length) {
        // Hidden rather than removed, so this is safe to re-run.
        badge.style.display = 'none';
        badge.textContent = '';
        return;
      }

      flagged += 1;
      badge.textContent = labels.length === 1 ? labels[0] : `${labels.length} conflicts`;
      badge.title =
        "Album-level tags disagree across this album's tracks: " +
        `${labels.join(', ')}. Fix on the Tag Corrections page.`;
      badge.style.display = 'inline-block';
    });

    return flagged;
  }

  // ── Simple track title edit ─────────────────────────────────────────────

  function editTrackTitle(trackId, currentTitle) {
    document.getElementById('editTrackId').value = trackId;
    document.getElementById('editTrackCurrentField').value = 'title';
    document.getElementById('editTrackLabel').textContent = 'Track Title';
    const field = document.getElementById('editTrackValue');
    field.value = currentTitle || '';

    const modalEl = document.getElementById('editTrackModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
    field.focus();
  }

  async function saveEditedTrackFromArtistPage(btn) {
    const trackId = document.getElementById('editTrackId').value;
    const field = document.getElementById('editTrackCurrentField').value;
    const value = document.getElementById('editTrackValue').value.trim();
    if (!trackId || !field) return;

    const payload = { track_id: trackId, sync_to_file: true };
    payload[field] = value;

    return withBusy(btn, 'Saving…', async () => {
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

        global.modal.hide('editTrackModal');
        setTimeout(() => global.location.reload(), 1000);
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  // ── Comprehensive track edit modal ──────────────────────────────────────
  //
  // ARTIST_MODAL_ADVANCED_FIELDS is PRESERVED VERBATIM. It is a
  // transcription of the Navidrome/file tag schema the backend writes — a
  // renamed or dropped key silently stops that tag being saved.

  const ARTIST_MODAL_ADVANCED_FIELDS = [
    { name: 'writer', label: 'Writer/Lyricist' },
    { name: 'arranger', label: 'Arranger' },
    { name: 'mixer', label: 'Mixer' },
    { name: 'producer', label: 'Producer' },
    { name: 'work', label: 'Work/Composition' },
    { name: 'isrc', label: 'ISRC' },
    { name: 'bpm', label: 'BPM' },
    { name: 'bitrate', label: 'Bitrate (kbps)' },
    { name: 'sample_rate', label: 'Sample Rate (Hz)' },
    { name: 'titlesort', label: 'Title Sort' },
    { name: 'albumsort', label: 'Album Sort' },
    { name: 'artistsort', label: 'Artist Sort' },
    { name: 'composersort', label: 'Composer Sort' },
    { name: 'albumartistsort', label: 'Album Artist Sort' },
    { name: 'lyricistsort', label: 'Lyricist Sort' },
    { name: 'artistssort', label: 'Artists Sort' },
    { name: 'albumartistssort', label: 'Album Artists Sort' },
    { name: 'artists', label: 'Artists (multi)' },
    { name: 'albumartists', label: 'Album Artists (multi)' },
    { name: 'conductor', label: 'Conductor' },
    { name: 'performer', label: 'Performer' },
    { name: 'director', label: 'Director' },
    { name: 'djmixer', label: 'DJ Mixer' },
    { name: 'engineer', label: 'Engineer' },
    { name: 'remixer', label: 'Remixer' },
    { name: 'lyricist', label: 'Lyricist' },
    { name: 'albumversion', label: 'Album Version' },
    { name: 'recordlabel', label: 'Record Label' },
    { name: 'copyright', label: 'Copyright' },
    { name: 'releasedate', label: 'Release Date' },
    { name: 'releasetype', label: 'Release Type' },
    { name: 'releasestatus', label: 'Release Status' },
    { name: 'releasecountry', label: 'Release Country' },
    { name: 'media', label: 'Media Format' },
    { name: 'barcode', label: 'Barcode' },
    { name: 'catalognumber', label: 'Catalog Number' },
    { name: 'asin', label: 'ASIN' },
    { name: 'originalyear', label: 'Original Year' },
    { name: 'originaldate', label: 'Original Date' },
    { name: 'tracktotal', label: 'Track Total' },
    { name: 'disctotal', label: 'Disc Total' },
    { name: 'script', label: 'Script' },
    { name: 'discsubtitle', label: 'Disc Subtitle' },
    { name: 'subtitle', label: 'Subtitle' },
    { name: 'grouping', label: 'Grouping' },
    { name: 'movement', label: 'Movement' },
    { name: 'movementname', label: 'Movement Name' },
    { name: 'movementtotal', label: 'Movement Total' },
    { name: 'key', label: 'Musical Key' },
    { name: 'language', label: 'Language' },
    { name: 'license', label: 'License' },
    { name: 'website', label: 'Website' },
    { name: 'encodedby', label: 'Encoded By' },
    { name: 'encodersettings', label: 'Encoder Settings' },
    { name: 'explicitstatus', label: 'Explicit Status' },
    { name: 'musicbrainz_albumid', label: 'MusicBrainz Album ID' },
    { name: 'musicbrainz_artistid', label: 'MusicBrainz Artist ID' },
    { name: 'musicbrainz_albumartistid', label: 'MusicBrainz Album Artist ID' },
    { name: 'musicbrainz_releasegroupid', label: 'MusicBrainz Release Group ID' },
    { name: 'musicbrainz_releasetrackid', label: 'MusicBrainz Release Track ID' },
    { name: 'musicbrainz_workid', label: 'MusicBrainz Work ID' },
    { name: 'replaygain_track_gain', label: 'ReplayGain Track Gain' },
    { name: 'replaygain_track_peak', label: 'ReplayGain Track Peak' },
    { name: 'replaygain_album_gain', label: 'ReplayGain Album Gain' },
    { name: 'replaygain_album_peak', label: 'ReplayGain Album Peak' },
    { name: 'r128_track_gain', label: 'R128 Track Gain' },
    { name: 'r128_album_gain', label: 'R128 Album Gain' },
    { name: 'lyrics', label: 'Lyrics', type: 'textarea' },
  ];

  /**
   * Separator used when joining/splitting the genre list for this modal.
   *
   * VERIFY THIS AGAINST THE BACKEND. The original split on a character class
   * and joined with a different single character; if the join separator is
   * not one of the split characters, genres saved here will not round-trip
   * on the next open. Whatever the server expects, both halves must agree.
   */
  const GENRE_JOIN = ';';
  const GENRE_SPLIT = /[;,/\\]/;

  let editArtistTrackCurrentGenres = [];

  function renderArtistAdvancedTrackFields(trackData) {
    const container = document.getElementById('editArtistTrackAdvancedFields');
    if (!container) return;

    container.innerHTML = ARTIST_MODAL_ADVANCED_FIELDS.map((def) => {
      const fieldId = `editArtistTrackAdv_${def.name}`;
      if (def.type === 'textarea') {
        return `
          <div class="col-12">
            <label for="${fieldId}" class="form-label">${esc(def.label)}</label>
            <textarea class="form-control" id="${fieldId}" rows="4"></textarea>
          </div>`;
      }
      return `
        <div class="col-md-6">
          <label for="${fieldId}" class="form-label">${esc(def.label)}</label>
          <input type="text" class="form-control" id="${fieldId}">
        </div>`;
    }).join('');

    ARTIST_MODAL_ADVANCED_FIELDS.forEach((def) => {
      const el = document.getElementById(`editArtistTrackAdv_${def.name}`);
      if (el) el.value = (trackData && trackData[def.name]) || '';
    });
  }

  async function openEditTrackFromArtistModal(trackId) {
    try {
      const trackData = await global.api.getJson(`/api/track/${encodeURIComponent(trackId)}`);
      openComprehensiveEditArtistTrackModal(trackId, trackData);
    } catch (error) {
      notifyError('Error loading track: ' + error.message);
    }
  }

  function openComprehensiveEditArtistTrackModal(trackId, trackData) {
    document.getElementById('editArtistTrackId').value = trackId;
    document.getElementById('editArtistTrackTitle').textContent = trackData.title || 'Unknown';

    const simpleFields = [
      ['editArtistTrackTitleField', 'title'],
      ['editArtistTrackArtistField', 'artist'],
      ['editArtistTrackAlbumField', 'album'],
      ['editArtistTrackYearField', 'year'],
      ['editArtistTrackAlbumArtistField', 'album_artist'],
      ['editArtistTrackComposerField', 'composer'],
      ['editArtistTrackTrackNumberField', 'track_number'],
      ['editArtistTrackDiscNumberField', 'disc_number'],
      ['editArtistTrackMBIDField', 'mbid'],
      ['editArtistTrackCommentField', 'comment'],
    ];
    simpleFields.forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (el) el.value = trackData[key] || '';
    });

    document.getElementById('editArtistTrackStarsField').value = trackData.stars || 0;
    document.getElementById('editArtistTrackSingleField').value = trackData.is_single || 0;
    document.getElementById('editArtistTrackConfidenceField').value =
      trackData.single_confidence || 'low';

    renderArtistAdvancedTrackFields(trackData || {});

    editArtistTrackCurrentGenres = trackData.genres
      ? String(trackData.genres).split(GENRE_SPLIT).map((g) => g.trim()).filter(Boolean)
      : [];
    updateEditArtistTrackGenresDisplay();

    loadRecommendedGenresForTrack(trackId);

    const modalEl = document.getElementById('editTrackFromArtistModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  async function loadRecommendedGenresForTrack(trackId) {
    const section = document.getElementById('artistRecommendedGenresSection');
    const display = document.getElementById('artistRecommendedGenresDisplay');
    if (!section || !display) return;

    try {
      const data = await global.api.getJson(`/api/genres/track/${encodeURIComponent(trackId)}`);
      if (!data.genres) {
        section.style.display = 'none';
        return;
      }

      const recommended = new Map();
      ['lastfm_tags', 'discogs_genres', 'spotify_genres'].forEach((key) => {
        (data.genres[key] || []).forEach((genre) => {
          const name = typeof genre === 'object' ? genre.name : genre;
          if (name) recommended.set(name, (recommended.get(name) || 0) + 1);
        });
      });

      if (!recommended.size) {
        section.style.display = 'none';
        return;
      }

      section.style.display = 'block';
      const entries = Array.from(recommended.entries());
      display.innerHTML = entries.map(([genre, count], index) => `
        <button type="button" class="btn btn-sm btn-outline-info rec-genre-btn"
                data-index="${index}" title="Add to track genres">
          ${esc(genre)} <small class="text-muted ms-1">(${count})</small>
        </button>`).join('');

      display.querySelectorAll('.rec-genre-btn').forEach((btn) => {
        btn.addEventListener('click', function () {
          addEditArtistGenreFromRecommended(entries[parseInt(this.dataset.index, 10)][0]);
        });
      });
    } catch (error) {
      section.style.display = 'none';
      console.warn('Could not load recommended genres:', error);
    }
  }

  function addEditArtistGenreFromRecommended(genre) {
    if (editArtistTrackCurrentGenres.includes(genre)) return;
    editArtistTrackCurrentGenres.push(genre);
    updateEditArtistTrackGenresDisplay();
  }

  function updateEditArtistTrackGenresDisplay() {
    const container = document.getElementById('editArtistTrackGenresDisplay');
    if (!container) return;

    container.innerHTML = '';

    if (!editArtistTrackCurrentGenres.length) {
      container.innerHTML = '<span class="text-muted small">No genres set</span>';
    } else {
      editArtistTrackCurrentGenres.forEach((genre) => {
        const badge = document.createElement('span');
        badge.className = 'badge bg-primary me-1 mb-1';
        badge.style.fontSize = '0.9rem';
        // textContent, never innerHTML — a genre name is user input, and the
        // original interpolated it into an onclick via escapeJsString.
        badge.textContent = genre;

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close btn-close-white ms-1';
        close.style.fontSize = '0.6rem';
        close.setAttribute('aria-label', `Remove ${genre}`);
        close.addEventListener('click', () => removeEditArtistTrackGenre(genre));

        badge.appendChild(close);
        container.appendChild(badge);
      });
    }

    const hidden = document.getElementById('editArtistTrackGenresField');
    if (hidden) hidden.value = editArtistTrackCurrentGenres.join(GENRE_JOIN);
  }

  function addEditArtistTrackGenre() {
    const input = document.getElementById('editArtistTrackGenreInput');
    const genre = input.value.trim();
    if (!genre) return;

    if (!editArtistTrackCurrentGenres.includes(genre)) {
      editArtistTrackCurrentGenres.push(genre);
      updateEditArtistTrackGenresDisplay();
    }
    input.value = '';
    input.focus();
  }

  function removeEditArtistTrackGenre(genre) {
    editArtistTrackCurrentGenres = editArtistTrackCurrentGenres.filter((g) => g !== genre);
    updateEditArtistTrackGenresDisplay();
  }

  async function saveArtistEditedTrack(btn) {
    const trackId = document.getElementById('editArtistTrackId').value;
    if (!trackId) {
      notifyError('No track ID');
      return;
    }

    const value = (id) => document.getElementById(id).value.trim();

    const payload = {
      track_id: trackId,
      title: value('editArtistTrackTitleField'),
      artist: value('editArtistTrackArtistField'),
      album: value('editArtistTrackAlbumField'),
      year: value('editArtistTrackYearField') || null,
      stars: parseInt(document.getElementById('editArtistTrackStarsField').value, 10) || 0,
      is_single: parseInt(document.getElementById('editArtistTrackSingleField').value, 10) || 0,
      single_confidence: document.getElementById('editArtistTrackConfidenceField').value,
      genres: editArtistTrackCurrentGenres.join(GENRE_JOIN),
      album_artist: value('editArtistTrackAlbumArtistField') || null,
      composer: value('editArtistTrackComposerField') || null,
      track_number: value('editArtistTrackTrackNumberField') || null,
      disc_number: value('editArtistTrackDiscNumberField') || null,
      mbid: value('editArtistTrackMBIDField') || null,
      comment: value('editArtistTrackCommentField') || null,
      sync_to_file: true,
    };

    ARTIST_MODAL_ADVANCED_FIELDS.forEach((def) => {
      const el = document.getElementById(`editArtistTrackAdv_${def.name}`);
      if (el) payload[def.name] = (el.value || '').trim() || null;
    });

    if (!payload.title) {
      notifyError('Title is required');
      return;
    }

    return withBusy(btn, 'Saving…', async () => {
      try {
        const data = await global.api.postJson('/api/track/update-metadata', payload);
        if (!data.success) {
          notifyError(data.error || 'Failed to update');
          return;
        }

        global.modal.hide('editTrackFromArtistModal');

        if (data.file_synced === false) {
          global.toast.warning(
            'Saved to database, but file tags were not updated. Check file permissions and the logs.'
          );
        } else {
          notifySuccess('Track metadata updated (database + file tags)');
        }
        setTimeout(() => global.location.reload(), 500);
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  // ── Smart artist routing (shared with track.html) ───────────────────────

  /**
   * Navigate to an artist page, or open a MusicBrainz lookup when the
   * artist is not in the library.
   */
  async function navigateToArtist(artist, event) {
    if (event && event.preventDefault) event.preventDefault();
    if (!artist) return;

    try {
      const data = await global.api.getJson(
        `/api/artist/exists?artist=${encodeURIComponent(artist)}`
      );
      if (data.exists) {
        global.location.href = `/artist/${encodeURIComponent(artist)}`;
      } else {
        openArtistMusicBrainzLookup(artist);
      }
    } catch (error) {
      console.error('Error checking artist existence:', error);
      global.location.href = `/artist/${encodeURIComponent(artist)}`;
    }
  }

  function openArtistMusicBrainzLookup(artist) {
    const bodyHtml = `
      <div id="artistMBLookupInfo" class="alert alert-info mb-3">
        Searching for <strong>${esc(artist)}</strong>…
      </div>
      <div id="artistMBLookupLoading" class="text-center py-5">
        <div class="spinner-border text-primary" role="status">
          <span class="visually-hidden">Loading…</span>
        </div>
        <p class="mt-3">Searching MusicBrainz and Discogs…</p>
      </div>
      <div id="artistMBLookupError" class="alert alert-danger" style="display:none;"></div>
      <div id="artistMBLookupResults"></div>`;

    global.modal.open('artistMBLookupModal', global.modal.template({
      id: 'artistMBLookupModal',
      title: 'Add Artist to Library',
      icon: 'bi-person-heart',
      bodyHtml,
      size: 'modal-xl',
      scrollable: true,
    }));

    performArtistMusicBrainzSearch(artist);
  }

  async function performArtistMusicBrainzSearch(artist) {
    const loading = document.getElementById('artistMBLookupLoading');
    const errorDiv = document.getElementById('artistMBLookupError');
    const resultsDiv = document.getElementById('artistMBLookupResults');

    try {
      const data = await global.api.postJson('/api/musicbrainz/search',
        { query: artist, artist_only: true });
      if (loading) loading.style.display = 'none';

      if (data.error) {
        errorDiv.textContent = 'Search error: ' + data.error;
        errorDiv.style.display = 'block';
        return;
      }

      const releases = data.releases || [];
      if (!releases.length) {
        resultsDiv.innerHTML =
          `<div class="alert alert-info"><i class="bi bi-info-circle"></i> No releases found for "${esc(artist)}".</div>`;
        return;
      }

      resultsDiv.innerHTML = '<div class="row g-3">' + releases.map((release, index) => {
        const date = release.first_release_date || 'Unknown';
        const type = release.category || release.primary_type || 'Release';
        const cover = release.cover_art_url
          ? `<img src="${esc(release.cover_art_url)}" class="card-img-top artist-lookup-art"
                  alt="" style="height:150px;object-fit:cover;">`
          : '<div class="card-img-top d-flex align-items-center justify-content-center" ' +
            'style="height:150px;background:#333;"><i class="bi bi-disc" style="font-size:2rem;opacity:0.5;"></i></div>';

        return `
          <div class="col-12 col-sm-6 col-md-4">
            <div class="card h-100">
              ${cover}
              <div class="card-body d-flex flex-column">
                <h6 class="card-title text-truncate">${esc(release.title)}</h6>
                <p class="card-text small text-muted mb-2">
                  <span class="badge bg-secondary">${esc(type)}</span>
                  <span class="badge bg-info ms-1">${esc(date)}</span>
                </p>
                <div class="mt-auto">
                  <button class="btn btn-sm btn-primary w-100 artist-lookup-queue" data-index="${index}">
                    <i class="bi bi-download"></i> Add to Queue
                  </button>
                </div>
              </div>
            </div>
          </div>`;
      }).join('') + '</div>';

      resultsDiv.querySelectorAll('.artist-lookup-queue').forEach((btn) => {
        btn.addEventListener('click', function () {
          const release = releases[parseInt(this.dataset.index, 10)];
          if (!release) return;
          const releaseArtist = release.artist
            || (release['artist-credit'] && release['artist-credit'][0]
              && release['artist-credit'][0].name)
            || artist;
          openSlskdSearch(release.title, releaseArtist);
          global.modal.hide('artistMBLookupModal');
        });
      });
    } catch (error) {
      if (loading) loading.style.display = 'none';
      if (errorDiv) {
        errorDiv.textContent = 'Network error: ' + error.message;
        errorDiv.style.display = 'block';
      }
    }
  }

  // ── Section reorder ─────────────────────────────────────────────────────

  /**
   * Put the sections in the intended reading order:
   * Overview → Stats → Similar → Top Tracks → Genres → Albums …
   *
   * NOTE: this only works once the unclosed <div> in the genres card is
   * fixed. Until then Top Tracks is a DESCENDANT of the genres card, so the
   * first insertBefore silently pulls it out of the card — masking the
   * markup bug while leaving every album section still nested inside.
   */
  function reorderSections() {
    const similar = document.getElementById('artist-similar-section');
    const topTracks = document.getElementById('artist-top-tracks-section');
    const genres = document.getElementById('artist-genres-section');
    const albums = document.getElementById('albums-section');
    if (!similar || !topTracks || !genres || !albums) return;

    const parent = similar.parentNode;
    if (!parent) return;

    parent.insertBefore(topTracks, similar.nextSibling);
    parent.insertBefore(genres, topTracks.nextSibling);
  }

  // ── Init ────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    const artist = artistName();

    reorderSections();
    initializeMissingToggle();
    initCorrectionsAndMissingTracks();

    if (!artist) return;

    const bio = document.getElementById('artistBio');
    if (bio && bio.dataset.hasInitialBio !== '1') loadArtistBio(artist);

    loadSinglesCount(artist);
    loadSimilarArtists(artist);
    loadArtistFavouriteState(artist);
    loadArtistCoveredBy(artist);

    // Album rows whose album-level tags disagree — see annotateAlbumTagConflicts.
    annotateAlbumTagConflicts(artist);

    // Background refresh so the album sections are current without a click.
    checkMissingReleases(artist, { silent: true, background: true });

    // Enter submits the two in-modal search inputs.
    const qbitInput = document.getElementById('qbitSearchInput');
    if (qbitInput) {
      qbitInput.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        performQbitSearch();
      });
    }
    const slskdInput = document.getElementById('slskdSearchInput');
    if (slskdInput) {
      slskdInput.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        runSlskdSearch();
      });
    }

    // The Soulseek tab reveals the per-track queue panel when an album is
    // in context.
    const slskdTab = document.getElementById('slskd-tab');
    if (slskdTab) {
      slskdTab.addEventListener('shown.bs.tab', function () {
        if (currentDownloadAlbum.album) showSlskdTrackQueueOption();
      });
    }
  });

  global.artistPage = {
    artistName,
    checkMissingReleases,
    loadTracklist,
    toggleTracklist,
    toggleMissing,
    loadSimilarArtists,
    annotateAlbumTagConflicts,
    openEditTrackFromArtistModal,
  };

  // ── Globals for the inline onclick handlers in artist_detail.html ───────
  //
  // Each wrapper passes the button explicitly rather than relying on the
  // implicit `event` global — see bug 3 in the header. Update the template
  // to pass `this` where indicated.

  global.openEditArtistIdsModal = openEditArtistIdsModal;
  global.openDownloadSearch = openDownloadSearch;
  global.performQbitSearch = performQbitSearch;
  global.performSlskdSearch = runSlskdSearch;
  global.openArtistImageModal = openArtistImageModal;
  global.fetchArtistCountry = fetchArtistCountry;
  global.editArtistCountry = editArtistCountry;
  global.applyCountryAsGenre = applyCountryAsGenre;
  global.toggleArtistFavourite = toggleArtistFavourite;
  global.toggleMissing = toggleMissing;
  global.toggleTracklist = toggleTracklist;
  global.searchMusicBrainzForAllReleases = searchMusicBrainzForAllReleases;
  global.showArtistSingles = showArtistSingles;
  global.openSlskdSearch = openSlskdSearch;
  global.navigateToArtist = navigateToArtist;
  global.openArtistMusicBrainzLookup = openArtistMusicBrainzLookup;
  global.editTrackTitle = editTrackTitle;
  global.openEditTrackFromArtistModal = openEditTrackFromArtistModal;
  global.addEditArtistTrackGenre = addEditArtistTrackGenre;
  global.removeEditArtistTrackGenre = removeEditArtistTrackGenre;

  // `this` should be passed from the template: onclick="checkMissingReleases(this)"
  global.checkMissingReleases = function (button) {
    return checkMissingReleases(artistName(), {
      button: button instanceof HTMLElement ? button : null,
    });
  };
  // onclick="createEssentialPlaylist(this)"
  global.createEssentialPlaylist = function (button) {
    return createEssentialPlaylist(artistName(),
      button instanceof HTMLElement ? button : null);
  };
  // onclick="saveEditedTrackFromArtistPage(this)"
  global.saveEditedTrackFromArtistPage = function (button) {
    return saveEditedTrackFromArtistPage(button instanceof HTMLElement ? button : null);
  };
  // onclick="saveArtistEditedTrack(this)"
  global.saveArtistEditedTrack = function (button) {
    return saveArtistEditedTrack(button instanceof HTMLElement ? button : null);
  };

  // NOTE: importMissingRelease is GONE — it was a near-duplicate of
  // importRelease hitting the same /api/artist/import-release endpoint with
  // the same payload. Point any remaining call site at importRelease.
  global.importRelease = function (artist, releaseId, title, button) {
    return importRelease(artist, releaseId, title,
      button instanceof HTMLElement ? button : null);
  };
})(window);
