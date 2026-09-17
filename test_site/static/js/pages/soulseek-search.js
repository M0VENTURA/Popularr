/* ==========================================================================
   static/js/pages/soulseek-search.js
   Downloads page — Soulseek search (grouped by user → album → file), the
   per-tab slskd monitor, and the consolidated monitor panel.

   Load order:
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/confirm.js  →  ui/button-state.js
       services/slskd.js
       downloads_page.js

   ── WHY THIS FILE HAD TO CHANGE ───────────────────────────────────────────
   It previously ran on globals it never declared, borrowed from downloads.js
   by shared script scope:

       currentSlskdSearchId   let, top-level in downloads.js
       slskdPollInterval      let, top-level in downloads.js
       escapeHtml / formatBytes / formatDuration / formatETA
       normalizeSoulseekQuery

   Both files carried comments explaining that redeclaring the first two threw
   "Identifier has already been declared" and killed this entire file — so the
   workaround was to leave them undeclared here and rely on load order. That
   made one mutable search handle shared by two Soulseek UIs: whichever
   searched last owned it, and either could clear the other's poll interval
   mid-flight.

   services/slskd.js now owns search state per named context, so this file
   declares everything it uses. It no longer depends on downloads.js at all.

   ── formatETA ────────────────────────────────────────────────────────────
   Defined locally below. It lived in downloads.js and was removed when that
   file was split; this is its only caller. It is NOT formatDuration — the
   sentinel 8640000 means "infinite" (slskd's placeholder for an unknown ETA)
   and renders as ∞, and 0 renders as "–" rather than "0:00".

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. A BUSY SEARCH SLOT SWITCHED TO A DEAD CODE PATH. The slotBusy branch
      called searchSoulseek(), which belongs to the OTHER Soulseek UI in
      downloads.js: it reads #slskdSearchQuery and writes #slskdSearchResults,
      neither of which exists on this page. A busy slot therefore silently
      abandoned the search and this page's results never appeared. The slskd
      service now owns slot-waiting and retries the SAME search.

   2. THE CONSOLIDATED MONITOR NEVER REFRESHED. startConsolidatedMonitorRefresh
      assigned the 5s refresh to `monitorCountdownInterval`, then cleared that
      same handle on the very next line before assigning the 1s countdown to
      it. The refresh timer was killed immediately after creation, so the
      counter ticked 5…4…3…2…1…5… forever while the table never reloaded.
      Two timers now mean two handles — and both are managed pollers.

   3. THE PROGRESS BAR NEVER FILLED (consolidated monitor). Its <div> carried
      TWO style attributes; HTML keeps the first and discards the rest, so
      `style="font-size:0.75rem"` silently replaced `style="width:NN%"`.
      Merged into one attribute.

   4. THE UNBOUNDED SLOT-POLL LOOP. The busy-slot retry used a bare
      setInterval with no timeout and no attempt cap, so a permanently busy
      slskd instance polled forever. poller.until() in the service caps it.

   5. A SINGLE-FILE DOWNLOAD PROMPTED TWICE — downloadSlskdSingle confirmed,
      then called downloadSlskdBatch which confirmed again. One prompt now.

   6. THE SELECTION SURVIVED QUEUEING. Checkboxes stayed ticked after a
      successful "Download selected", so clicking it again re-queued
      everything. Cleared on success.
   ========================================================================== */

(function (global) {
  'use strict';

  const SEARCH_CONTEXT = 'downloads-page';
  const MONITOR_INTERVAL_MS = 5000;
  const COUNTDOWN_INTERVAL_MS = 1000;
  const INFINITE_ETA_SENTINEL = 8640000;

  let searchSession = null;
  let responsesData = Object.create(null);
  let monitorPoller = null;
  let consolidatedPoller = null;
  let countdownPoller = null;
  let refreshCountdown = MONITOR_INTERVAL_MS / 1000;
  let monitorLoaded = false;

  /** Per-page selection set, from services/slskd.js. */
  const selection = global.slskd.createSelection();

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function formatBytes(bytes) {
    return global.formatBytes ? global.formatBytes(bytes) : `${bytes} B`;
  }

  function formatDuration(value) {
    return global.formatDuration ? global.formatDuration(value) : String(value || '');
  }

  /**
   * Format a transfer ETA.
   *
   * NOT interchangeable with formatDuration — see the header note.
   *
   * @param {number} seconds
   * @returns {string}
   */
  function formatETA(seconds) {
    const n = Number(seconds);
    if (!Number.isFinite(n) || n === INFINITE_ETA_SENTINEL || n < 0) return '∞';
    if (n === 0) return '–';

    const days = Math.floor(n / 86400);
    const hours = Math.floor((n % 86400) / 3600);
    const minutes = Math.floor((n % 3600) / 60);
    const secs = Math.floor(n % 60);

    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  }

  function notifyError(message) {
    if (global.toast) global.toast.error(message);
    else global.alert(message);
  }

  function notifySuccess(message) {
    if (global.toast) global.toast.success(message);
    else global.alert(message);
  }

  // ── Selection ───────────────────────────────────────────────────────────

  function updateSelectedButton() {
    const btn = document.getElementById('slskdDownloadSelected');
    if (!btn) return;
    const count = selection.size;
    btn.disabled = count === 0;
    btn.textContent = count > 0 ? `Download selected (${count})` : 'Download selected';
  }

  function clearSelection() {
    selection.clear();
    updateSelectedButton();
    document.querySelectorAll('.slskd-file-select:checked')
      .forEach((cb) => { cb.checked = false; });
  }

  function resetSearchState() {
    selection.clear();
    responsesData = Object.create(null);
    updateSelectedButton();
  }

  // ── Search ──────────────────────────────────────────────────────────────

  function setStatusText(text) {
    const el = document.getElementById('slskdStatusText');
    if (el) el.textContent = text;
  }

  function setCounts(responseCount, fileCount) {
    const responses = document.getElementById('slskdResponseCount');
    const results = document.getElementById('slskdResultCount');
    if (responses) {
      responses.textContent = `${responseCount} response${responseCount === 1 ? '' : 's'}`;
    }
    if (results) {
      results.textContent = `${fileCount} file${fileCount === 1 ? '' : 's'}`;
    }
  }

  function showError(message) {
    const status = document.getElementById('slskdStatus');
    const results = document.getElementById('slskdResults');
    if (status) status.style.display = 'none';
    if (results) {
      // escapeHtml: this can carry a raw server error string, and previously
      // went straight into innerHTML.
      results.innerHTML =
        `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> ${esc(message)}</div>`;
    }
    resetSearchState();
  }

  function performSearch() {
    const input = document.getElementById('slskdSearchInput');
    const query = global.slskd.normalizeQuery(input ? input.value : '');

    if (!query) {
      notifyError('Please enter a search query');
      return;
    }
    if (input) input.value = query;

    resetSearchState();

    const results = document.getElementById('slskdResults');
    const status = document.getElementById('slskdStatus');
    if (results) results.innerHTML = '';
    if (status) status.style.display = 'block';
    setCounts(0, 0);
    setStatusText('Searching…');

    // The service owns the search id, the poll loop, the terminal-state grace
    // window and the busy-slot retry. A busy slot now retries THIS search
    // rather than handing off to downloads.js's unrelated search UI.
    return global.slskd.search(query, {
      context: SEARCH_CONTEXT,
      filterBusy: false,
      onUpdate: (session) => {
        searchSession = session;
        setCounts(session.responseCount, session.results.length);
        setStatusText(
          session.state && session.state.startsWith('Waiting')
            ? session.state
            : `Searching… (${session.responseCount} responses, ${session.results.length} files)`
        );
        renderResponses(session.results);
      },
      onComplete: (session) => {
        searchSession = session;
        setCounts(session.responseCount, session.results.length);
        setStatusText(`Search completed — ${session.state}`);
        if (!session.results.length) {
          if (results) {
            results.innerHTML =
              '<div class="alert alert-info"><i class="bi bi-info-circle"></i> No results found.</div>';
          }
          return;
        }
        renderResponses(session.results);
      },
      onError: (error) => showError(error.message),
    });
  }

  // ── Rendering ───────────────────────────────────────────────────────────

  function buildFileRow(username, file) {
    const selected = selection.has(username, file.filename);
    return `
      <div class="slskd-file-row"
           data-username="${esc(username)}" data-filename="${esc(file.filename)}"
           data-size="${esc(String(file.size || 0))}">
        <div class="form-check me-3">
          <input class="form-check-input slskd-file-select" type="checkbox"
                 data-username="${esc(username)}" data-filename="${esc(file.filename)}"
                 data-size="${esc(String(file.size || 0))}" ${selected ? 'checked' : ''}>
        </div>
        <div class="slskd-file-info">
          <div class="slskd-file-name" title="${esc(file.filename)}">
            ${esc(file.trackName || file.filename)}
          </div>
        </div>
        <div class="slskd-file-stats">
          <span class="slskd-file-stat">
            <span class="slskd-stat-label">Size</span>
            <span class="slskd-stat-value">${esc(formatBytes(file.size))}</span>
          </span>
          <span class="slskd-file-stat">
            <span class="slskd-stat-label">Bitrate</span>
            <span class="slskd-stat-value">${file.bitrate > 0 ? esc(file.bitrate + ' kbps') : '—'}</span>
          </span>
          <span class="slskd-file-stat">
            <span class="slskd-stat-label">Length</span>
            <span class="slskd-stat-value">${file.length > 0 ? esc(formatDuration(file.length)) : '—'}</span>
          </span>
          <span class="slskd-file-stat">
            <span class="slskd-stat-label">Sample Rate</span>
            <span class="slskd-stat-value">${file.sample_rate > 0 ? esc((file.sample_rate / 1000) + ' kHz') : '—'}</span>
          </span>
        </div>
        <button class="slskd-download-btn" type="button" title="Download this file"
                data-username="${esc(username)}" data-filename="${esc(file.filename)}"
                data-size="${esc(String(file.size || 0))}">
          <i class="bi bi-download"></i>
        </button>
      </div>`;
  }

  function buildAlbumBlock(username, album, albumId) {
    const fileRows = album.files.map((file) => buildFileRow(username, file)).join('');
    return `
      <div class="slskd-album" data-username="${esc(username)}"
           data-album-key="${encodeURIComponent(album.key)}">
        <div class="slskd-album-header" data-target="files-${esc(albumId)}">
          <div class="d-flex flex-column">
            <div class="slskd-album-name">${esc(album.displayName)}</div>
            <div class="slskd-user-meta">${album.files.length} track${album.files.length === 1 ? '' : 's'}</div>
          </div>
          <div class="d-flex align-items-center gap-2">
            <button class="btn btn-sm btn-outline-success slskd-album-download" type="button"
                    data-username="${esc(username)}"
                    data-album-key="${encodeURIComponent(album.key)}">Download album</button>
            <i class="bi bi-chevron-down slskd-chevron" id="chevron-${esc(albumId)}"></i>
          </div>
        </div>
        <div id="files-${esc(albumId)}" class="slskd-files-container" style="display:none;">
          <div class="slskd-files-list">${fileRows}</div>
        </div>
      </div>`;
  }

  function renderResponses(results) {
    const container = document.getElementById('slskdResults');
    if (!container) return;

    responsesData = global.slskd.groupByUser(results);
    const users = Object.values(responsesData).sort((a, b) => b.totalFiles - a.totalFiles);

    if (!users.length) {
      container.innerHTML = '';
      return;
    }

    const html = users.map((user, index) => {
      const safeId = `slskd-user-${index}`;
      const albums = user.albumOrder
        .map((key, albumIndex) => buildAlbumBlock(user.username, user.albums[key], `${safeId}-album-${albumIndex}`))
        .join('');

      return `
        <div class="slskd-user-group" data-username="${esc(user.username)}" data-index="${index}">
          <div class="slskd-user-header" style="cursor:pointer;">
            <div class="slskd-user-info">
              <div class="slskd-username">
                <i class="bi bi-person-circle"></i> ${esc(user.username)}
              </div>
              <div class="slskd-user-meta">
                <i class="bi bi-file-earmark-music"></i>
                ${user.totalFiles} file${user.totalFiles === 1 ? '' : 's'}
              </div>
            </div>
            <i class="bi bi-chevron-down slskd-chevron" id="chevron-${safeId}"></i>
          </div>
          <div id="files-${safeId}" class="slskd-user-body" style="display:none;">
            ${albums}
          </div>
        </div>`;
    }).join('');

    container.innerHTML = `<div class="slskd-responses">${html}</div>`;
    attachHandlers(container);
  }

  function toggleSection(bodyEl, chevronEl) {
    if (!bodyEl) return;
    const show = bodyEl.style.display === 'none' || bodyEl.style.display === '';
    bodyEl.style.display = show ? 'block' : 'none';
    if (chevronEl) chevronEl.classList.toggle('rotated', show);
  }

  function attachHandlers(container) {
    container.querySelectorAll('.slskd-user-header').forEach((header) => {
      header.addEventListener('click', function (event) {
        event.stopPropagation();
        const group = this.closest('.slskd-user-group');
        if (!group) return;
        const safeId = `slskd-user-${group.dataset.index}`;
        toggleSection(
          document.getElementById(`files-${safeId}`),
          document.getElementById(`chevron-${safeId}`)
        );
      });
    });

    container.querySelectorAll('.slskd-album-header').forEach((header) => {
      header.addEventListener('click', function (event) {
        if (event.target.closest('.slskd-album-download')) return;
        // The target id is built from indices in buildAlbumBlock, so it is
        // already safe — no sanitising needed, unlike the old path-derived ids.
        toggleSection(
          document.getElementById(this.getAttribute('data-target')),
          this.querySelector('.slskd-chevron')
        );
      });
    });

    container.querySelectorAll('.slskd-album-download').forEach((btn) => {
      btn.addEventListener('click', function (event) {
        event.stopPropagation();
        downloadAlbum(
          this.dataset.username,
          decodeURIComponent(this.dataset.albumKey || ''),
          this
        );
      });
    });

    container.querySelectorAll('.slskd-download-btn').forEach((btn) => {
      btn.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        downloadSingle({
          username: this.dataset.username,
          filename: this.dataset.filename,
          size: parseInt(this.dataset.size, 10) || 0,
        }, this);
      });
    });

    container.querySelectorAll('.slskd-file-select').forEach((input) => {
      input.addEventListener('change', function () {
        selection.toggle(
          this.dataset.username,
          this.dataset.filename,
          parseInt(this.dataset.size, 10) || 0,
          this.checked
        );
        updateSelectedButton();
      });
    });
  }

  // ── Downloads ───────────────────────────────────────────────────────────

  function downloadSingle(file, button) {
    if (!file.username || !file.filename) return;
    // ONE confirmation — the service prompts, so this no longer does.
    return global.buttonState.withBusy(button, '', async () => {
      const ok = await global.slskd.download([file], { label: file.filename });
      if (ok) global.buttonState.flashDone(button, { fromClass: 'slskd-download-btn' });
    });
  }

  function downloadAlbum(username, albumKey, button) {
    const user = responsesData[username];
    if (!user || !user.albums[albumKey]) return;

    const files = user.albums[albumKey].files.map((f) => ({
      username,
      filename: f.filename,
      size: f.size || 0,
    }));
    if (!files.length) return;

    return global.buttonState.withBusy(button, '', () =>
      global.slskd.download(files, {
        label: `${files.length} track${files.length === 1 ? '' : 's'} from ${albumKey}`,
      })
    );
  }

  async function downloadSelected() {
    const files = selection.values();
    if (!files.length) return;

    const btn = document.getElementById('slskdDownloadSelected');
    return global.buttonState.withBusy(btn, '', async () => {
      const ok = await global.slskd.download(files, {
        label: `${files.length} selected file${files.length === 1 ? '' : 's'}`,
      });
      // The checkboxes used to stay ticked, so a second click re-queued the
      // same files.
      if (ok) clearSelection();
    });
  }

  // ── Monitor (per-tab) ───────────────────────────────────────────────────

  function stateBadgeClass(state) {
    const s = String(state || '').toLowerCase();
    if (s.includes('inprogress') || s.includes('downloading')) return 'bg-primary';
    if (s.includes('completed') || s.includes('complete')) return 'bg-success';
    if (s.includes('queued') || s.includes('initializing')) return 'bg-warning';
    if (s.includes('error') || s.includes('failed') || s.includes('cancelled')) return 'bg-danger';
    return 'bg-secondary';
  }

  function buildMonitorRow(download) {
    // Backend-normalised fields — /api/slskd/status already extracts these.
    const filename = download.filename || 'Unknown';
    const fileSize = download.size || 0;
    const transferred = download.bytesTransferred || 0;
    const progress = download.progress || 0;
    const speed = download.averageSpeed || 0;
    const eta = speed > 0 ? Math.floor((fileSize - transferred) / speed) : 0;

    return `
      <tr>
        <td><strong>${esc(download.username || '')}</strong></td>
        <td>
          <div class="text-truncate" style="max-width:420px;" title="${esc(filename)}">
            ${esc(filename)}
          </div>
          <small class="text-muted">
            ${esc(formatBytes(transferred))} / ${esc(formatBytes(fileSize))}${eta > 0 ? ` · ETA: ${esc(formatETA(eta))}` : ''}
          </small>
        </td>
        <td class="text-center">
          <span class="badge ${stateBadgeClass(download.state)}">${esc(download.state || '')}</span>
        </td>
        <td class="text-center">
          <div class="progress" style="height:20px;min-width:100px;">
            <div class="progress-bar ${progress >= 100 ? 'bg-success' : 'bg-primary'}"
                 role="progressbar" style="width:${progress}%"
                 aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100">${progress}%</div>
          </div>
        </td>
        <td class="text-center">${esc(formatBytes(fileSize))}</td>
        <td class="text-center">
          ${speed > 0
            ? `<span class="text-primary"><i class="bi bi-arrow-down"></i> ${esc(formatBytes(speed))}/s</span>`
            : '—'}
        </td>
        <td class="text-center">
          <button class="btn btn-sm btn-danger slskd-cancel-btn"
                  data-username="${esc(download.username || '')}"
                  data-filename="${esc(download.filename || '')}"
                  data-token="${esc(download.remoteToken || '')}"
                  title="Cancel download">
            <i class="bi bi-x-circle"></i>
          </button>
        </td>
      </tr>`;
  }

  async function refreshMonitor(opts = {}) {
    const loading = document.getElementById('slskdMonLoading');
    if (!loading) return;

    const errorBox = document.getElementById('slskdMonError');
    const results = document.getElementById('slskdMonResults');
    const empty = document.getElementById('slskdMonEmpty');
    const table = document.getElementById('slskdMonTable');
    const tbody = document.getElementById('slskdMonTableBody');
    const countBadge = document.getElementById('slskdMonCount');

    if (!monitorLoaded && opts.silent !== true) loading.style.display = 'block';

    let data;
    try {
      data = await global.api.getJson('/api/slskd/status');
    } catch (error) {
      loading.style.display = 'none';
      if (errorBox) {
        errorBox.textContent = 'Network error: ' + error.message;
        errorBox.style.display = 'block';
      }
      return;
    }

    loading.style.display = 'none';
    monitorLoaded = true;

    if (data.error) {
      if (errorBox) {
        errorBox.textContent = 'Error: ' + data.error;
        errorBox.style.display = 'block';
      }
      if (results) results.style.display = 'none';
      return;
    }

    if (errorBox) errorBox.style.display = 'none';
    if (results) results.style.display = 'block';

    const downloads = data.downloads || [];
    if (countBadge) {
      countBadge.style.display = downloads.length ? 'inline-block' : 'none';
      countBadge.textContent = `${downloads.length} active`;
    }

    if (!downloads.length) {
      if (empty) empty.style.display = 'block';
      if (table) table.style.display = 'none';
      return;
    }

    if (empty) empty.style.display = 'none';
    if (table) table.style.display = 'block';
    if (!tbody) return;

    tbody.innerHTML = downloads.map(buildMonitorRow).join('');

    // Delegated rather than inline onclick="cancelSlskdDownload('…')": a
    // filename containing an apostrophe (common in track titles) broke the
    // generated handler, and escapeHtml does not escape for a JS context.
    tbody.querySelectorAll('.slskd-cancel-btn').forEach((btn) => {
      btn.addEventListener('click', function () {
        cancelDownload(this.dataset.username, this.dataset.filename, this.dataset.token, this);
      });
    });
  }

  async function cancelDownload(username, filename, token, button) {
    if (!username || !filename) return;

    const confirmFn = (global.ui && global.ui.confirm)
      ? global.ui.confirm
      : (opts) => Promise.resolve(global.confirm(opts.message));

    const accepted = await confirmFn({
      title: 'Cancel download',
      message: `Cancel "${filename}" from ${username}?`,
      tone: 'danger',
      confirmLabel: 'Cancel download',
      cancelLabel: 'Keep',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, '', async () => {
      try {
        const data = await global.api.postJson('/api/slskd/cancel', { username, filename, token });
        if (!data.success) {
          notifyError(data.error || 'Failed to cancel download');
          return;
        }
        notifySuccess('Download cancelled');
        await refreshMonitor({ silent: true });
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  // ── Consolidated monitor ────────────────────────────────────────────────

  function updateCountdown() {
    const el = document.getElementById('monitorNextRefresh');
    if (el) el.textContent = refreshCountdown + 's';
    refreshCountdown -= 1;
    if (refreshCountdown < 0) refreshCountdown = MONITOR_INTERVAL_MS / 1000;
  }

  function updateTotalCount() {
    const total = document.querySelectorAll('#monitorSlskdTableBody tr').length;

    const badge = document.getElementById('monitorTotalCount');
    if (badge) {
      badge.textContent = String(total);
      badge.style.display = total > 0 ? 'inline-block' : 'none';
    }
    const totalEl = document.getElementById('monitorTotal');
    if (totalEl) totalEl.textContent = String(total);
  }

  function buildConsolidatedRow(download) {
    const filename = download.filename || 'Unknown';
    const fileSize = download.size || 0;
    const progress = download.progress || 0;
    const speed = download.averageSpeed || 0;
    const state = String(download.state || '').toLowerCase();

    // ONE style attribute. The original carried two, and HTML keeps only the
    // first — so the font-size attribute silently discarded the width.
    return `
      <tr>
        <td><small>${esc(download.username || '')}</small></td>
        <td title="${esc(filename)}">
          <small class="text-truncate d-block">${esc(filename)}</small>
        </td>
        <td class="text-center">
          <div class="progress" style="height:18px;min-width:80px;">
            <div class="progress-bar ${progress >= 100 ? 'bg-success' : 'bg-primary'}"
                 role="progressbar" style="width:${progress}%;font-size:0.75rem;"
                 aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100">${progress}%</div>
          </div>
        </td>
        <td class="text-center small">${esc(formatBytes(fileSize))}</td>
        <td class="text-center small">${speed > 0 ? esc(formatBytes(speed)) + '/s' : '—'}</td>
        <td class="text-center">
          <span class="badge ${stateBadgeClass(state)}">${esc(state.substring(0, 8))}</span>
        </td>
      </tr>`;
  }

  async function refreshConsolidatedMonitor() {
    const results = document.getElementById('monitorSlskdResults');
    // `results` was dereferenced without a null check, so this threw on any
    // page rendering the loading element but not the results container.
    if (!results) return;

    const loading = document.getElementById('monitorSlskdLoading');
    const errorBox = document.getElementById('monitorSlskdError');
    const empty = document.getElementById('monitorSlskdEmpty');
    const table = document.getElementById('monitorSlskdTable');
    const tbody = document.getElementById('monitorSlskdTableBody');
    const countBadge = document.getElementById('monitorSlskdCount');

    let data;
    try {
      data = await global.api.getJson('/api/slskd/status');
    } catch (error) {
      if (loading) loading.style.display = 'none';
      if (errorBox) {
        errorBox.textContent = 'Network error: ' + error.message;
        errorBox.style.display = 'block';
      }
      return;
    }

    if (loading) loading.style.display = 'none';

    if (data.error) {
      if (errorBox) {
        errorBox.textContent = 'Error: ' + data.error;
        errorBox.style.display = 'block';
      }
      results.style.display = 'none';
      return;
    }

    if (errorBox) errorBox.style.display = 'none';
    results.style.display = 'block';

    const downloads = data.downloads || [];
    if (countBadge) {
      countBadge.style.display = downloads.length ? 'inline-block' : 'none';
      countBadge.textContent = `${downloads.length} active`;
    }

    if (!downloads.length) {
      if (empty) empty.style.display = 'block';
      if (table) table.style.display = 'none';
      updateTotalCount();
      return;
    }

    if (empty) empty.style.display = 'none';
    if (table) table.style.display = 'block';
    if (tbody) tbody.innerHTML = downloads.map(buildConsolidatedRow).join('');
    updateTotalCount();
  }

  async function refreshAllMonitors() {
    await refreshConsolidatedMonitor();
    refreshCountdown = MONITOR_INTERVAL_MS / 1000;
    updateCountdown();
  }

  /**
   * Start the consolidated monitor's two timers.
   *
   * TWO pollers, two handles — see bug 2 in the header. The refresh used to
   * be assigned to the countdown's handle and cleared on the next line.
   */
  function startConsolidatedRefresh() {
    if (consolidatedPoller) consolidatedPoller.stop();
    if (countdownPoller) countdownPoller.stop();

    consolidatedPoller = global.poller.create({
      interval: MONITOR_INTERVAL_MS,
      immediate: false,
      onTick: refreshAllMonitors,
    });
    countdownPoller = global.poller.create({
      interval: COUNTDOWN_INTERVAL_MS,
      immediate: false,
      onTick: () => updateCountdown(),
    });

    consolidatedPoller.start();
    countdownPoller.start();
  }

  // ── Init ────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    const searchInput = document.getElementById('slskdSearchInput');
    if (searchInput) {
      searchInput.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        performSearch();
      });
    }

    const searchBtn = document.getElementById('slskdSearchBtn');
    if (searchBtn) searchBtn.addEventListener('click', performSearch);

    const selectedBtn = document.getElementById('slskdDownloadSelected');
    if (selectedBtn) selectedBtn.addEventListener('click', downloadSelected);
    updateSelectedButton();

    // The per-tab monitor. The old version tested for #slskdMonLoading twice
    // in a row; one block does.
    if (document.getElementById('slskdMonLoading')) {
      refreshMonitor();
      monitorPoller = global.poller.create({
        interval: MONITOR_INTERVAL_MS,
        immediate: false,
        onTick: () => refreshMonitor({ silent: true }),
      });
      monitorPoller.start();
    }

    if (document.getElementById('monitorSlskdLoading')) {
      refreshAllMonitors();
      startConsolidatedRefresh();
    }
  });

  // NOTE: no `beforeunload` teardown here. utils/poller.js registers its own
  // `pagehide` + `beforeunload` handlers and stops every live poller, so the
  // three timers above are released automatically — including on iOS Safari,
  // where `beforeunload` does not fire and the old hand-rolled cleanup leaked.

  global.downloadsPage = {
    search: performSearch,
    refreshMonitor,
    refreshAllMonitors,
    downloadSelected,
    formatETA,
  };

  // Legacy globals for inline handlers in the downloads templates.
  global.performSlskdSearch = performSearch;
  global.downloadSlskdSelected = downloadSelected;
  global.refreshSlskdMonitor = refreshMonitor;
  global.refreshAllMonitors = refreshAllMonitors;
  global.cancelSlskdDownload = function (username, filename, token) {
    return cancelDownload(username, filename, token, null);
  };

  // `slskdSelectionKey` and `sanitizeId` are gone: the selection key now
  // lives in services/slskd.js, and every element id is built from an index
  // rather than a filesystem path, so nothing needs sanitising.
})(window);
