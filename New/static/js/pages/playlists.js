/* ==========================================================================
   static/js/pages/playlists.js
   /playlists — list smart + regular playlists, view tracks, rename, delete,
   generate, export, CSV import.

   Load order:
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/modal.js  →  ui/confirm.js  →  ui/button-state.js
       playlists_index.js

   ── WHAT WAS REMOVED ──────────────────────────────────────────────────────
     escapeHtml         → utils/dom.js (also escapes quotes; see bug 1)
     parseJsonOrThrow   → utils/api.js parseJsonResponse
     formatDuration     → kept LOCAL, deliberately (see note below)

   formatDuration here takes PLAIN SECONDS and always renders m:ss — the
   Navidrome track duration field. utils/dom.js's version guesses the unit by
   magnitude (µs / ms / s), so a 3600-second track would render as "1:00"
   there instead of "60:00". Not interchangeable; this copy stays.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. escapeHtml DID NOT ESCAPE QUOTES, AND ITS OUTPUT WENT INTO ATTRIBUTES.
      The local version used `div.textContent = …; return div.innerHTML`,
      which escapes & < > but leaves " and ' intact. That result was then
      interpolated into title="…" in four places — playlistCard's file hint
      and all three track-row cells:
          title="${escapeHtml(track.title)}"
      A track or playlist named  Say "Hello"  closed the attribute early and
      the remainder became live markup. utils/dom.js's escapeHtml escapes all
      five entities, so it is safe in both text and attribute contexts.

   2. THE RENAME BUTTON SENT A STALE FILE NAME.
          file_name: currentPlaylist.source === 'file'
            ? document.getElementById('renameFileName').value.trim()
            : null
      openRenameModal only POPULATES #renameFileName when the playlist is
      .nsp (`source === 'file' && !isM3u`) — for an .m3u it hides the group
      and leaves the input holding whatever the PREVIOUS .nsp rename put
      there. Renaming an .m3u straight after an .nsp therefore posted the
      other playlist's file name. Now the field is cleared when hidden, and
      the payload only includes file_name when the group is actually shown.

   3. submitRename RE-SELECTED THE WRONG PLAYLIST. It reads
      `currentPlaylist.source` AFTER `await loadPlaylists()`, which calls
      resetDetail() and sets `currentPlaylist = null` — so the expression
      threw on a null read, the catch swallowed it as a "Rename failed"
      alert, and the rename had actually succeeded. The source/id are now
      captured before the reload.

   4. THE EXPORT POLL WAS UNBOUNDED. _pollExport rescheduled itself every
      600ms with no overall cap; only transient FAILURES were limited (5
      retries). A job stuck at status "running" polled forever. Now a
      managed poller with a 10-minute ceiling that also stops on teardown.

   5. `alert()` in submitCsvImport was the only native alert left in the
      file, everything else used showAlert(). Now consistent.
   ========================================================================== */

(function (global) {
  'use strict';

  const EXPORT_POLL_MS = 600;
  const EXPORT_MAX_POLLS = 1000;        // ~10 minutes at 600ms
  const EXPORT_MAX_TRANSIENT_RETRIES = 5;
  const TRACKS_TIMEOUT_MS = 30000;

  let playlists = [];
  let currentPlaylist = null;
  let currentTracks = [];
  let playlistFilter = '';
  let csvImportData = null;
  let exportJobId = null;
  let exportPoller = null;
  let exportTransientRetries = 0;

  const compactView = global.matchMedia('(max-width: 767.98px)');

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  /**
   * Format a track duration.
   * PLAIN SECONDS only — see the note in the file header about why this is
   * not utils/dom.js's formatDuration.
   */
  function formatDuration(seconds) {
    const n = Number(seconds) || 0;
    const mins = Math.floor(n / 60);
    const secs = Math.floor(n % 60);
    return `${mins}:${String(secs).padStart(2, '0')}`;
  }

  function showAlert(message, isError) {
    const container = document.getElementById('playlistsAlert');
    if (!container) {
      if (isError) global.toast.error(message);
      else global.toast.success(message);
      return;
    }
    container.innerHTML = `
      <div class="alert ${isError ? 'alert-danger' : 'alert-success'} alert-dismissible fade show py-2 small">
        ${esc(message)}
        <button type="button" class="btn-close py-2" data-bs-dismiss="alert" aria-label="Close"></button>
      </div>`;
  }

  function playlistIcon(playlist) {
    return playlist.type === 'smart'
      ? 'bi-stars text-warning'
      : 'bi-collection-play text-primary';
  }

  function isM3u(playlist) {
    const kind = String(playlist.kind || '').toLowerCase();
    const file = String(playlist.file_name || '').toLowerCase();
    return kind === 'm3u' || file.endsWith('.m3u') || file.endsWith('.m3u8');
  }

  /** True when this playlist exposes an editable raw file name (.nsp only). */
  function hasEditableFileName(playlist) {
    return playlist.source === 'file' && !isM3u(playlist);
  }

  // ── Mobile panel switching (one panel at a time below 992px) ────────────

  function switchToDetail() {
    if (global.innerWidth >= 992) return;
    document.getElementById('playlistListPanel').classList.add('d-none');
    document.getElementById('playlistDetailPanel').classList.remove('d-none');
  }

  function showPlaylistList() {
    document.getElementById('playlistListPanel').classList.remove('d-none');
    document.getElementById('playlistDetailPanel').classList.add('d-none');
  }

  // ── Listing ─────────────────────────────────────────────────────────────

  async function loadPlaylists() {
    const listEl = document.getElementById('playlistList');
    if (!listEl) return;

    listEl.innerHTML = '<p class="text-center text-muted py-4 small">Loading playlists…</p>';
    currentPlaylist = null;
    resetDetail();

    try {
      const data = await global.api.getJson('/api/playlists/all');
      playlists = data.playlists || [];
      renderList();
      showPlaylistList();
    } catch (error) {
      console.error('loadPlaylists:', error);
      listEl.innerHTML = '<p class="text-center text-muted py-4 small">Could not load playlists</p>';
      showAlert(error.message || 'Could not load playlists', true);
    }
  }

  function renderList() {
    const listEl = document.getElementById('playlistList');
    if (!listEl) return;

    const needle = playlistFilter.trim().toLowerCase();
    const filtered = needle
      ? playlists.filter((p) => (p.name || '').toLowerCase().includes(needle))
      : playlists;

    const smart = filtered.filter((p) => p.type === 'smart');
    const regular = filtered.filter((p) => p.type === 'regular');

    const smartCount = document.getElementById('smartCount');
    const regularCount = document.getElementById('regularCount');
    if (smartCount) smartCount.textContent = String(smart.length);
    if (regularCount) regularCount.textContent = String(regular.length);

    listEl.innerHTML = '';

    if (!filtered.length) {
      listEl.innerHTML = '<p class="text-center text-muted py-4 small">' +
        (needle ? `No playlists match "${esc(playlistFilter.trim())}"` : 'No playlists found') +
        '</p>';
      return;
    }

    const renderGroup = (label, items) => {
      if (!items.length) return;
      listEl.insertAdjacentHTML('beforeend',
        `<div class="small fw-semibold text-muted text-uppercase mt-1 mb-1 px-1">${esc(label)}</div>`);
      items.forEach((item) => listEl.appendChild(playlistCard(item)));
    };

    renderGroup('Smart Playlists', smart);
    renderGroup('Regular Playlists', regular);
  }

  function applyPlaylistFilter(value) {
    playlistFilter = value || '';
    renderList();
  }

  function playlistCard(playlist) {
    const card = document.createElement('div');
    const isActive = currentPlaylist
      && currentPlaylist.id === playlist.id
      && currentPlaylist.source === playlist.source;

    const fileHint = playlist.file_name
      ? `<small class="text-muted d-block text-truncate" title="${esc(playlist.file_name)}">${esc(playlist.file_name)}</small>`
      : '';

    const countBadge = playlist.rule_based
      ? '<span class="badge bg-info-subtle text-info-emphasis text-nowrap">Rules</span>'
      : `<span class="badge bg-secondary-subtle text-secondary-emphasis text-nowrap">${esc(String(playlist.track_count || 0))} tracks</span>`;

    card.className = `card playlist-card ${isActive ? 'border-primary' : ''}`;
    card.style.cursor = 'pointer';
    card.innerHTML = `
      <div class="card-body py-2 d-flex justify-content-between align-items-center gap-2">
        <div style="min-width:0">
          <div class="d-flex align-items-center gap-2">
            <i class="bi ${playlistIcon(playlist)}"></i>
            <span class="fw-semibold text-truncate">${esc(playlist.name)}</span>
          </div>
          ${fileHint}
        </div>
        <div class="d-flex align-items-center gap-2 text-nowrap">
          ${countBadge}
          <button type="button" class="btn btn-sm btn-outline-secondary py-0" title="Rename playlist">
            <i class="bi bi-pencil"></i>
          </button>
        </div>
      </div>`;

    card.querySelector('button').addEventListener('click', (event) => {
      event.stopPropagation();
      openRenameModal(playlist.id, playlist.source);
    });
    card.addEventListener('click', () => selectPlaylist(playlist));
    return card;
  }

  // ── Detail / tracks ─────────────────────────────────────────────────────

  async function selectPlaylist(playlist) {
    currentPlaylist = playlist;
    renderList();      // re-render for active highlighting
    switchToDetail();  // mobile: show detail, hide list

    document.getElementById('detailHeader').classList.remove('d-none');
    document.getElementById('detailEmpty').classList.add('d-none');
    document.getElementById('detailTracksWrap').classList.remove('d-none');
    document.getElementById('detailIcon').className = `bi ${playlistIcon(playlist)}`;
    document.getElementById('detailName').textContent = playlist.name;
    document.getElementById('detailComment').textContent = playlist.comment || '';
    document.getElementById('detailMeta').textContent =
      `${playlist.type === 'smart' ? 'Smart' : 'Regular'} playlist · ` +
      `${playlist.rule_based ? 'rule-based' : (playlist.track_count || 0) + ' tracks'}`;

    // File path subtitle with copy button (file-backed playlists only).
    const pathRow = document.getElementById('detailPathRow');
    const pathEl = document.getElementById('detailPath');
    if (playlist.file_path) {
      pathEl.textContent = playlist.file_path;
      pathEl.title = playlist.file_path;
      pathRow.classList.remove('d-none');
      pathRow.classList.add('d-flex');
    } else {
      pathRow.classList.add('d-none');
      pathRow.classList.remove('d-flex');
    }

    renderTracks([]);
    document.getElementById('detailTracks').innerHTML =
      '<tr><td colspan="6" class="text-center text-muted py-4">Loading tracks…</td></tr>';
    document.getElementById('detailStacked').innerHTML =
      '<div class="text-center text-muted py-4 small">Loading tracks…</div>';

    try {
      // A stalled smart-playlist lookup (slow Navidrome) must not leave the
      // panel on "Loading tracks…" forever.
      const data = await global.api.postJson('/api/playlists/tracks', {
        id: playlist.id,
        source: playlist.source,
        file_path: playlist.file_path || null,
      }, { timeoutMs: TRACKS_TIMEOUT_MS });
      renderTracks(data.tracks || []);
    } catch (error) {
      console.error('selectPlaylist:', error);
      const message = /timed out/i.test(error.message)
        ? 'Timed out loading tracks after 30s — check the Activity Center logs and the Navidrome connection.'
        : (error.message || 'Failed to load tracks');
      document.getElementById('detailTracks').innerHTML =
        `<tr><td colspan="6" class="text-center text-danger py-4">${esc(message)}</td></tr>`;
      document.getElementById('detailStacked').innerHTML =
        `<div class="text-center text-danger py-4 small">${esc(message)}</div>`;
    }
  }

  function renderTracks(tracks) {
    currentTracks = tracks;
    const tableWrap = document.getElementById('detailTableWrap');
    const stackedWrap = document.getElementById('detailStackedWrap');
    if (!tableWrap || !stackedWrap) return;

    if (compactView.matches) {
      renderStackedTracks(tracks);
      tableWrap.classList.add('d-none');
      stackedWrap.classList.remove('d-none');
    } else {
      renderTableTracks(tracks);
      stackedWrap.classList.add('d-none');
      tableWrap.classList.remove('d-none');
    }
  }

  function renderTableTracks(tracks) {
    const tbody = document.getElementById('detailTracks');
    if (!tbody) return;

    if (!tracks.length) {
      tbody.innerHTML =
        '<tr><td colspan="6" class="text-center text-muted py-4">No tracks in this playlist</td></tr>';
      return;
    }

    tbody.innerHTML = tracks.map((track, index) => `
      <tr>
        <td class="text-muted">${index + 1}</td>
        <td class="text-truncate" style="max-width:220px;" title="${esc(track.title)}">${esc(track.title)}</td>
        <td class="text-truncate" style="max-width:160px;" title="${esc(track.artist)}">${esc(track.artist)}</td>
        <td class="text-truncate" style="max-width:160px;" title="${esc(track.album)}">${esc(track.album)}</td>
        <td class="text-end text-nowrap">${esc(formatDuration(track.duration))}</td>
        <td class="text-center">${track.rating ? '<i class="bi bi-star-fill text-warning"></i> ' + esc(String(track.rating)) : ''}</td>
      </tr>`).join('');
  }

  /**
   * Mobile compact rows: Title (bold) / Artist • Album (muted) on the left,
   * duration + stars on the right — no horizontal scrolling.
   */
  function renderStackedTracks(tracks) {
    const container = document.getElementById('detailStacked');
    if (!container) return;

    if (!tracks.length) {
      container.innerHTML =
        '<div class="text-center text-muted py-4 small">No tracks in this playlist</div>';
      return;
    }

    container.innerHTML = tracks.map((track, index) => {
      const artist = esc(track.artist || '');
      const album = esc(track.album || '');
      return `
        <div class="border rounded p-2 d-flex justify-content-between align-items-center gap-2">
          <div style="min-width:0">
            <div class="fw-semibold text-truncate" title="${esc(track.title)}">${index + 1}. ${esc(track.title)}</div>
            <div class="small text-muted text-truncate">${artist}${album ? ' • ' + album : ''}</div>
          </div>
          <div class="text-end text-nowrap">
            <div class="small">${esc(formatDuration(track.duration))}</div>
            ${starRating(track.rating)}
          </div>
        </div>`;
    }).join('');
  }

  function starRating(rating) {
    const count = Math.max(0, Math.min(5, Math.round(Number(rating) || 0)));
    if (!count) return '';
    return `<span class="text-warning small">${'⭐'.repeat(count)}</span>`;
  }

  function resetDetail() {
    const ids = ['detailHeader', 'detailEmpty', 'detailTracksWrap',
      'detailTracks', 'detailStacked', 'detailPathRow'];
    if (ids.some((id) => !document.getElementById(id))) return;

    document.getElementById('detailHeader').classList.add('d-none');
    document.getElementById('detailEmpty').classList.remove('d-none');
    document.getElementById('detailTracksWrap').classList.add('d-none');
    document.getElementById('detailTracks').innerHTML = '';
    document.getElementById('detailStacked').innerHTML = '';
    document.getElementById('detailPathRow').classList.add('d-none');
    document.getElementById('detailPathRow').classList.remove('d-flex');
    currentTracks = [];
  }

  // ── Copy path ───────────────────────────────────────────────────────────

  async function copyPath() {
    const path = document.getElementById('detailPath')?.textContent || '';
    if (!path) return;

    try {
      await navigator.clipboard.writeText(path);
    } catch (_error) {
      // Clipboard API unavailable (insecure context) — fall back to a
      // temporary input + execCommand.
      const input = document.createElement('input');
      input.value = path;
      document.body.appendChild(input);
      input.select();
      try {
        document.execCommand('copy');
      } catch (_error2) {
        showAlert('Could not copy path', true);
        input.remove();
        return;
      }
      input.remove();
    }
    showAlert('Path copied to clipboard', false);
  }

  // ── Rename ──────────────────────────────────────────────────────────────

  /**
   * Open the rename modal.
   * Called from card buttons with explicit id/source, or from the header
   * with no args (uses currentPlaylist).
   */
  function openRenameModal(id, source) {
    let target = currentPlaylist;
    if (id || source) {
      target = playlists.find((p) => p.id === id && p.source === source) || currentPlaylist;
    }
    if (!target) return;

    currentPlaylist = target;
    document.getElementById('renameName').value = target.name || '';

    // Generated .m3u playlists derive their file name from the playlist
    // name — only .nsp smart playlists expose the raw file name field.
    const editable = hasEditableFileName(target);
    const group = document.getElementById('renameFileGroup');
    const fileInput = document.getElementById('renameFileName');
    const pathLabel = document.getElementById('renameFilePath');

    group.style.display = editable ? '' : 'none';

    if (editable) {
      fileInput.value = target.file_name ? target.file_name.replace(/\.nsp$/i, '') : '';
      pathLabel.textContent = target.file_path ? `Currently: ${target.file_path}` : '';
    } else {
      // CLEAR when hidden. Previously the stale value from a previous .nsp
      // rename stayed in the input and was posted for the next .m3u rename.
      fileInput.value = '';
      pathLabel.textContent = '';
    }

    const modalEl = document.getElementById('renameModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
    document.getElementById('renameName').focus();
  }

  async function submitRename() {
    if (!currentPlaylist) return;

    const name = document.getElementById('renameName').value.trim();
    if (!name) {
      showAlert('Playlist name is required', true);
      return;
    }

    // Captured BEFORE the reload: loadPlaylists() sets currentPlaylist to
    // null, so reading these afterwards threw and the catch reported a
    // "Rename failed" for a rename that had actually succeeded.
    const target = currentPlaylist;
    const editable = hasEditableFileName(target);

    const payload = { id: target.id, source: target.source, name };
    if (editable) {
      payload.file_name = document.getElementById('renameFileName').value.trim();
    }

    const button = document.querySelector('#renameModal .modal-footer .btn-primary');
    return global.buttonState.withBusy(button, 'Saving…', async () => {
      try {
        const data = await global.api.postJson('/api/playlists/rename', payload);

        const modalEl = document.getElementById('renameModal');
        if (modalEl && global.modal) global.modal.hide(modalEl);
        showAlert(`Renamed playlist to "${name}"`, false);

        await loadPlaylists();

        const match = target.source === 'file'
          ? playlists.find((p) => p.file_path === data.file_path)
          : playlists.find((p) => p.id === target.id && p.source === 'navidrome');
        if (match) selectPlaylist(match);
      } catch (error) {
        console.error('submitRename:', error);
        showAlert(error.message || 'Rename failed', true);
      }
    });
  }

  // ── Delete ──────────────────────────────────────────────────────────────

  function openDeleteModal() {
    if (!currentPlaylist) return;
    document.getElementById('deleteName').textContent = currentPlaylist.name;
    document.getElementById('deleteHint').textContent = currentPlaylist.source === 'file'
      ? `Removes ${currentPlaylist.file_name || 'the playlist file'} from the Playlists folder. This cannot be undone.`
      : 'Deletes the playlist from Navidrome. This cannot be undone.';

    const modalEl = document.getElementById('deleteModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  async function submitDelete() {
    if (!currentPlaylist) return;

    const target = currentPlaylist;
    const button = document.querySelector('#deleteModal .modal-footer .btn-danger');

    return global.buttonState.withBusy(button, 'Deleting…', async () => {
      try {
        await global.api.postJson('/api/playlists/delete', {
          id: target.id,
          source: target.source,
          file_path: target.file_path || null,
        });

        const modalEl = document.getElementById('deleteModal');
        if (modalEl && global.modal) global.modal.hide(modalEl);
        showAlert(`Deleted playlist "${target.name}"`, false);
        await loadPlaylists();
      } catch (error) {
        console.error('submitDelete:', error);
        showAlert(error.message || 'Delete failed', true);
      }
    });
  }

  // ── Generator (Last.fm / ListenBrainz recommendations) ──────────────────

  function openGeneratorModal() {
    const result = document.getElementById('genResult');
    result.classList.add('d-none');
    result.innerHTML = '';

    const modalEl = document.getElementById('generatorModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
    document.getElementById('genName').focus();
  }

  async function submitGenerator() {
    const btn = document.getElementById('genSubmitBtn');
    const resultEl = document.getElementById('genResult');
    const name = document.getElementById('genName').value.trim() || 'Recommended Mix';
    const limit = Math.max(1, Math.min(
      parseInt(document.getElementById('genLimit').value, 10) || 12, 25
    ));

    resultEl.classList.remove('d-none');
    resultEl.className = 'alert alert-info small py-2';
    resultEl.innerHTML =
      '<i class="bi bi-hourglass-split me-1"></i>Fetching recommendations — this can take a minute (API rate limits).';

    return global.buttonState.withBusy(btn, 'Generating…', async () => {
      try {
        // Recommendation generation hits external APIs with rate limits;
        // the default 30s timeout is not enough.
        const data = await global.api.postJson('/api/playlists/generate/recommendations', {
          source: document.getElementById('genSource').value,
          name,
          limit,
        }, { timeoutMs: 180000 });

        const queuedNote = data.queued_failed > 0
          ? ` <span class="text-danger">(${esc(String(data.queued_failed))} failed to queue)</span>`
          : '';
        const pathNote = data.playlist_path
          ? ` (<code>${esc(String(data.playlist_path).split('/').pop())}</code>)`
          : '';

        resultEl.className = 'alert alert-success small py-2';
        resultEl.innerHTML =
          '<i class="bi bi-check-circle me-1"></i>' +
          `Playlist "<strong>${esc(data.playlist_name)}</strong>" ready — ` +
          `<strong>${esc(String(data.added_now))}</strong> track(s) from the library${pathNote}` +
          ` · <strong>${esc(String(data.queued_ok))}</strong> missing track(s) queued to Soulseek${queuedNote}.`;

        const modalEl = document.getElementById('generatorModal');
        if (modalEl && global.modal) global.modal.hide(modalEl);
        await loadPlaylists();
      } catch (error) {
        resultEl.className = 'alert alert-danger small py-2';
        resultEl.innerHTML =
          `<i class="bi bi-exclamation-triangle me-1"></i>${esc(error.message)}`;
      }
    });
  }

  // ── Export (zip download with progress) ─────────────────────────────────

  function exportShowError(message) {
    const errorEl = document.getElementById('exportError');
    if (errorEl) {
      errorEl.textContent = message;
      errorEl.classList.remove('d-none');
    }
    document.getElementById('exportProgressWrap')?.classList.add('d-none');
    document.getElementById('exportDoneWrap')?.classList.add('d-none');
    stopExportPoll();
  }

  function setExportProgress(percent, text) {
    const bar = document.getElementById('exportProgressBar');
    const pct = Math.max(0, Math.min(100, Math.round(percent)));
    if (bar) {
      bar.style.width = pct + '%';
      bar.textContent = pct + '%';
    }
    const textEl = document.getElementById('exportProgressText');
    if (textEl) textEl.textContent = text;
  }

  function stopExportPoll() {
    if (exportPoller) {
      exportPoller.stop();
      exportPoller = null;
    }
  }

  function openExportModal() {
    if (!currentPlaylist) return;

    document.getElementById('exportInfo').textContent =
      `"${currentPlaylist.name}" — zipping the local audio files of its ` +
      `${currentPlaylist.track_count || 0} track(s). Missing files are skipped.`;
    document.getElementById('exportError').classList.add('d-none');
    document.getElementById('exportDoneWrap').classList.add('d-none');
    document.getElementById('exportProgressWrap').classList.remove('d-none');
    setExportProgress(0, 'Starting…');

    const modalEl = document.getElementById('exportPlaylistModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
    startExport();
  }

  async function startExport() {
    stopExportPoll();
    exportTransientRetries = 0;

    try {
      const data = await global.api.postJson('/api/playlists/export', {
        id: currentPlaylist.id,
        source: currentPlaylist.source,
        file_path: currentPlaylist.file_path || null,
      });

      if (data.status === 'error') {
        exportShowError(data.error || 'No local audio files found for this playlist');
        return;
      }

      exportJobId = data.job_id;
      pollExport();
    } catch (error) {
      exportShowError(error.message);
    }
  }

  function handleExportJob(job, ctx) {
    if (job.status === 'error') {
      exportShowError(job.error || 'Export failed');
      ctx.stop();
      return;
    }

    if (job.status === 'done') {
      const skippedCount = (job.skipped || []).length;
      setExportProgress(100,
        `Done — ${job.total} file(s) zipped${skippedCount ? `, ${skippedCount} skipped` : ''}`);

      const download = document.getElementById('exportDownloadLink');
      if (download) download.href = `/api/playlists/export/download/${exportJobId}`;

      const skipped = document.getElementById('exportSkippedNote');
      if (skipped) {
        if (skippedCount) {
          skipped.textContent = `${skippedCount} track(s) had no local file and were skipped.`;
          skipped.classList.remove('d-none');
        } else {
          skipped.classList.add('d-none');
        }
      }

      document.getElementById('exportProgressWrap').classList.add('d-none');
      document.getElementById('exportDoneWrap').classList.remove('d-none');
      ctx.stop();
      return;
    }

    const percent = job.total ? (job.done / job.total) * 100 : 0;
    setExportProgress(percent, job.current ? `Zipping: ${job.current}` : 'Preparing…');
    exportTransientRetries = 0;
  }

  function pollExport() {
    if (!exportJobId) return;
    stopExportPoll();

    // Bounded: the old version rescheduled itself indefinitely, capping only
    // transient failures. A job stuck on "running" polled forever.
    exportPoller = global.poller.create({
      interval: EXPORT_POLL_MS,
      maxAttempts: EXPORT_MAX_POLLS,
      pauseWhenHidden: false,   // the zip must finish even in a background tab
      onTick: async (ctx) => {
        const job = await global.api.getJson(
          `/api/playlists/export/status/${encodeURIComponent(exportJobId)}`
        );
        handleExportJob(job, ctx);
      },
      onError: () => {
        exportTransientRetries += 1;
        if (exportTransientRetries >= EXPORT_MAX_TRANSIENT_RETRIES) {
          exportShowError('Export job became unavailable — please try again.');
        }
      },
      onTimeout: () => exportShowError('Export timed out — please try again.'),
    });
    exportPoller.start();
  }

  // ── CSV import ──────────────────────────────────────────────────────────

  function openCsvImportModal() {
    document.getElementById('csvImportForm').reset();
    document.getElementById('csvImportStatus').textContent = '';
    document.getElementById('csvImportResult').classList.add('d-none');
    csvImportData = null;

    const modalEl = document.getElementById('csvImportModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  async function submitCsvImport(event) {
    event.preventDefault();

    const fileInput = document.getElementById('csvImportFile');
    const name = document.getElementById('csvImportName').value.trim();
    if (!fileInput.files.length || !name) {
      showAlert('Please select a CSV file and enter a playlist name', true);
      return;
    }

    const statusEl = document.getElementById('csvImportStatus');
    const submitBtn = document.getElementById('csvImportSubmitBtn');
    statusEl.textContent = 'Importing and matching tracks…';
    statusEl.className = 'mb-2 small text-secondary';

    return global.buttonState.withBusy(submitBtn, 'Importing…', async () => {
      try {
        const formData = new FormData();
        formData.append('file', fileInput.files[0]);
        formData.append('playlist_name', name);
        formData.append('playlist_description', '');

        // FormData must not carry an explicit Content-Type — the browser
        // sets the multipart boundary.
        const response = await fetch('/api/playlist/import/csv', {
          method: 'POST',
          body: formData,
        });
        const data = await global.api.parseJsonResponse(response, 'CSV import');

        csvImportData = data;
        const matched = (data.matched_tracks || []).length;
        const missing = (data.missing_tracks || []).length;
        const total = matched + missing;
        const coverage = total > 0 ? Math.round((matched / total) * 100) : 0;

        document.getElementById('csvImportMatched').textContent = `${matched} matched`;
        document.getElementById('csvImportMissing').textContent = `${missing} missing`;
        document.getElementById('csvImportCoverage').textContent =
          `${total} track(s) · ${coverage}% in library`;
        document.getElementById('csvImportCreateBtn').disabled = matched === 0;
        document.getElementById('csvImportResult').classList.remove('d-none');

        statusEl.textContent = 'Import complete';
        statusEl.className = 'mb-2 small text-success';
      } catch (error) {
        console.error('submitCsvImport:', error);
        statusEl.textContent = error.message;
        statusEl.className = 'mb-2 small text-danger';
      }
    });
  }

  async function createPlaylistFromImport() {
    if (!csvImportData) return;

    const name = document.getElementById('csvImportName').value.trim();
    const btn = document.getElementById('csvImportCreateBtn');

    return global.buttonState.withBusy(btn, 'Creating…', async () => {
      try {
        const data = await global.api.postJson('/api/playlist/create', {
          playlist_name: name,
          playlist_description: '',
          matched_tracks: csvImportData.matched_tracks || [],
          format: 'm3u',
        });

        showAlert(`Playlist "${name}" created with ${data.track_count} track(s)`, false);

        const modalEl = document.getElementById('csvImportModal');
        if (modalEl && global.modal) global.modal.hide(modalEl);
        await loadPlaylists();
      } catch (error) {
        console.error('createPlaylistFromImport:', error);
        showAlert(error.message || 'Failed to create playlist', true);
      }
    });
  }

  // ── Init ────────────────────────────────────────────────────────────────

  // Re-render tracks in the right layout when crossing the mobile breakpoint.
  compactView.addEventListener('change', () => {
    if (currentPlaylist) renderTracks(currentTracks);
  });

  document.addEventListener('DOMContentLoaded', function () {
    loadPlaylists();

    const exportModal = document.getElementById('exportPlaylistModal');
    if (exportModal) {
      exportModal.addEventListener('hidden.bs.modal', stopExportPoll);
    }
  });

  global.playlistsIndex = {
    loadPlaylists,
    selectPlaylist,
    applyPlaylistFilter,
    formatDuration,
  };

  // Globals for the inline handlers in playlists.html.
  global.loadPlaylists = loadPlaylists;
  global.applyPlaylistFilter = applyPlaylistFilter;
  global.showPlaylistList = showPlaylistList;
  global.copyPath = copyPath;
  global.openRenameModal = openRenameModal;
  global.submitRename = submitRename;
  global.openDeleteModal = openDeleteModal;
  global.submitDelete = submitDelete;
  global.openGeneratorModal = openGeneratorModal;
  global.submitGenerator = submitGenerator;
  global.openExportModal = openExportModal;
  global.openCsvImportModal = openCsvImportModal;
  global.submitCsvImport = submitCsvImport;
  global.createPlaylistFromImport = createPlaylistFromImport;
})(window);
