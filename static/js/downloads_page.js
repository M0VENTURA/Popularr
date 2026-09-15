// Downloads page logic — extracted from templates/pages/downloads/queue.html
// (was previously an untagged inline script block that browsers treated as
// inert text; loaded via versioned_static after downloads.js).

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------
// Everything below was USED throughout this file but DECLARED nowhere — not
// here, not in downloads.js. Each one threw a ReferenceError on the first
// Soulseek search, which is why the search UI failed silently.
//
// `currentSlskdSearchId` and `slskdPollInterval` are deliberately NOT
// redeclared: they are `let` at the top level of downloads.js, which shares
// script scope with this file. Declaring them again throws
// "Identifier has already been declared" and kills the whole file.

const slskdSelected = new Map();   // key -> { username, filename, size }
let slskdResponsesData = {};       // username -> { albums, albumOrder, totalFiles }
let slskdMonInFlight = false;
let slskdMonLoaded = false;
let monitorInterval = null;

// A stable composite key for the selection Map. A NUL separator cannot appear
// in either a Soulseek username or a filename, so "a\0b" and "a" + "\0b"
// cannot collide the way a "-" or "/" separator could.
function slskdSelectionKey(username, filename) {
  return String(username || '') + '\u0000' + String(filename || '');
}

function resetSlskdState() {
  slskdSelected.clear();
  slskdResponsesData = {};
  window._slskdTerminalGrace = 0;
  updateSlskdSelectedButton();
}

// Album keys are raw filesystem paths, so they can contain characters that are
// invalid in an id and in a CSS selector. The markup builds ids from indices
// (already safe), so this is the guard for any id derived from a path.
// If the id scheme changes, change this with it.
function sanitizeId(value) {
  return String(value || '').replace(/[^a-zA-Z0-9_-]/g, '_');
}

function updateSlskdSelectedButton() {
  const btn = document.getElementById('slskdDownloadSelected');
  if (!btn) return;
  const count = slskdSelected.size;
  btn.disabled = count === 0;
  btn.textContent = count > 0 ? `Download selected (${count})` : 'Download selected';
}

function toggleSlskdSelection(username, filename, size, checked) {
  const key = slskdSelectionKey(username, filename);
  if (checked) {
    slskdSelected.set(key, { username, filename, size: size || 0 });
  } else {
    slskdSelected.delete(key);
  }
  updateSlskdSelectedButton();
}

function performSlskdSearch() {
  const input = document.getElementById('slskdSearchInput');
  const query = normalizeSoulseekQuery(input?.value || '');
  if (!query) {
    alert('Please enter a search query');
    return;
  }
  if (input) {
    input.value = query;
  }

  // Clear previous results and selections
  resetSlskdState();
  currentSlskdSearchId = null;
  if (slskdPollInterval) clearInterval(slskdPollInterval);

  document.getElementById('slskdResults').innerHTML = '';
  document.getElementById('slskdStatus').style.display = 'block';
  document.getElementById('slskdResponseCount').textContent = '0 responses';
  document.getElementById('slskdResultCount').textContent = '0 results';
  document.getElementById('slskdStatusText').textContent = 'Searching...';

  fetch('/api/slskd/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: query })
  })
    .then(response => response.json())
    .then(data => {
      if (data.error) {
        showSlskdError(data.error);
        return;
      }
      if (data.slotBusy) {
        document.getElementById('slskdStatusText').textContent = 'Soulseek search slot is busy — retrying automatically…';
        // Poll /api/slskd/search-slot until free, then retry.
        //
        // FIXED: this used to call searchSoulseek(), which belongs to the
        // OTHER Soulseek UI in downloads.js — it reads #slskdSearchQuery and
        // writes #slskdSearchResults, neither of which exists on this page.
        // So a busy slot silently switched to a dead search path and this
        // page's results never appeared. It now retries its own search.
        const slotPoll = setInterval(() => {
          fetch('/api/slskd/search-slot')
            .then(r => r.json())
            .then(slotData => {
              if (slotData.slotFree) {
                clearInterval(slotPoll);
                performSlskdSearch();
              }
            })
            .catch(() => {});
        }, 2000);
        return;
      }

      currentSlskdSearchId = data.searchId;
      document.getElementById('slskdStatusText').textContent = 'Searching the Soulseek network...';

      // Start polling for results
      setTimeout(() => {
        slskdPollInterval = setInterval(pollSlskdResults, 1000);
        pollSlskdResults(); // Poll immediately
      }, 500);
    })
    .catch(error => {
      showSlskdError('Network error: ' + error.message);
    });
}

function pollSlskdResults() {
  if (!currentSlskdSearchId) return;

  fetch(`/api/slskd/search/${currentSlskdSearchId}`)
    .then(response => response.json())
    .then(data => {
      if (data.error) {
        showSlskdError(data.error);
        return;
      }

      const results = data.results || [];
      const responseCount = data.responseCount || 0;
      const isComplete = data.isComplete || false;
      const state = data.state || 'Searching';

      // Group by username and album path
      slskdResponsesData = {};
      results.forEach(result => {
        const username = result.username;
        const pathParts = (result.filename || '').split(/[\\/]/);
        const trackName = pathParts.pop() || result.filename;
        const albumPath = pathParts.length ? pathParts.join('/') : '(Unknown folder)';
        const albumKey = albumPath || '(Unknown folder)';
        const normalized = { ...result, trackName, albumPath, albumKey };

        if (!slskdResponsesData[username]) {
          slskdResponsesData[username] = {
            username,
            albums: {},
            albumOrder: [],
            totalFiles: 0
          };
        }
        const user = slskdResponsesData[username];
        if (!user.albums[albumKey]) {
          user.albums[albumKey] = { key: albumKey, displayName: albumPath, files: [] };
          user.albumOrder.push(albumKey);
        }
        user.albums[albumKey].files.push(normalized);
        user.totalFiles += 1;
      });

      // Update status
      const totalFiles = results.length;
      document.getElementById('slskdResponseCount').textContent = responseCount + ' response' + (responseCount !== 1 ? 's' : '');
      document.getElementById('slskdResultCount').textContent = totalFiles + ' file' + (totalFiles !== 1 ? 's' : '');

      // Grace window: slskd flips a search to a terminal state ("Completed,
      // TimedOut") while responses are STILL streaming in.  Stopping at the
      // first terminal poll with 0 files showed "No results found" even
      // though real files were on the way (the reported manual searches that
      // showed 0 but had downloadable results).  Keep polling for a short
      // grace after a terminal 0-result poll.
      if (isComplete) {
        if (totalFiles > 0) {
          document.getElementById('slskdStatusText').textContent = 'Search completed - ' + state;
          if (slskdPollInterval) {
            clearInterval(slskdPollInterval);
            slskdPollInterval = null;
          }
        } else {
          // Terminal but still 0 files — grace-poll up to 8 more times (8s)
          // before declaring "no results".
          const grace = Number(window._slskdTerminalGrace || 0) + 1;
          window._slskdTerminalGrace = grace;
          if (grace >= 8) {
            document.getElementById('slskdStatusText').textContent = 'Search completed - ' + state;
            if (slskdPollInterval) {
              clearInterval(slskdPollInterval);
              slskdPollInterval = null;
            }
          } else {
            document.getElementById('slskdStatusText').textContent = 'Searching... (' + responseCount + ' responses, ' + totalFiles + ' files) — waiting for late responses';
          }
        }
      } else {
        window._slskdTerminalGrace = 0;
        document.getElementById('slskdStatusText').textContent = `Searching... (${responseCount} responses, ${totalFiles} files)`;
      }

      // Display user responses
      if (Object.keys(slskdResponsesData).length > 0) {
        displaySlskdResponses();
      } else if (isComplete && Number(window._slskdTerminalGrace || 0) >= 8) {
        document.getElementById('slskdResults').innerHTML = `
          <div class="alert alert-info">
            <i class="bi bi-info-circle"></i> No results found.
          </div>
        `;
      }
    })
    .catch(error => {
      console.error('Poll error:', error);
    });
}

function displaySlskdResponses() {
  const container = document.getElementById('slskdResults');

  let html = '<div class="slskd-responses">';

  // Sort by number of files (descending)
  const sortedUsers = Object.values(slskdResponsesData).sort((a, b) => b.totalFiles - a.totalFiles);

  sortedUsers.forEach((userData, index) => {
    const fileCount = userData.totalFiles;
    const safeId = `slskd-user-${index}`;

    let albumsHtml = '';
    userData.albumOrder.forEach((albumKey, albumIndex) => {
      const album = userData.albums[albumKey];
      const albumId = `${safeId}-album-${albumIndex}`;
      const fileRows = album.files.map(file => {
        const selected = slskdSelected.has(slskdSelectionKey(userData.username, file.filename));
        return `
          <div class="slskd-file-row" data-username="${escapeHtml(userData.username)}" data-filename="${escapeHtml(file.filename)}" data-size="${file.size}">
            <div class="form-check me-3">
              <input class="form-check-input slskd-file-select" type="checkbox" data-username="${escapeHtml(userData.username)}" data-filename="${escapeHtml(file.filename)}" data-size="${file.size}" ${selected ? 'checked' : ''}>
            </div>
            <div class="slskd-file-info">
              <div class="slskd-file-name" title="${escapeHtml(file.filename)}">
                ${escapeHtml(file.trackName || file.filename)}
              </div>
            </div>
            <div class="slskd-file-stats">
              <span class="slskd-file-stat">
                <span class="slskd-stat-label">Size</span>
                <span class="slskd-stat-value">${formatBytes(file.size)}</span>
              </span>
              <span class="slskd-file-stat">
                <span class="slskd-stat-label">Bitrate</span>
                <span class="slskd-stat-value">${file.bitrate > 0 ? file.bitrate + ' kbps' : '—'}</span>
              </span>
              <span class="slskd-file-stat">
                <span class="slskd-stat-label">Length</span>
                <span class="slskd-stat-value">${file.length > 0 ? formatDuration(file.length) : '—'}</span>
              </span>
              <span class="slskd-file-stat">
                <span class="slskd-stat-label">Sample Rate</span>
                <span class="slskd-stat-value">${file.sample_rate > 0 ? (file.sample_rate / 1000) + ' kHz' : '—'}</span>
              </span>
            </div>
            <button class="slskd-download-btn" type="button" title="Download this file" data-username="${escapeHtml(userData.username)}" data-filename="${escapeHtml(file.filename)}" data-size="${file.size}">
              <i class="bi bi-download"></i>
            </button>
          </div>
        `;
      }).join('');

      albumsHtml += `
        <div class="slskd-album" data-username="${escapeHtml(userData.username)}" data-album-key="${encodeURIComponent(album.key)}">
          <div class="slskd-album-header" data-target="files-${albumId}">
            <div class="d-flex flex-column">
              <div class="slskd-album-name">${escapeHtml(album.displayName)}</div>
              <div class="slskd-user-meta">${album.files.length} track${album.files.length !== 1 ? 's' : ''}</div>
            </div>
            <div class="d-flex align-items-center gap-2">
              <button class="btn btn-sm btn-outline-success slskd-album-download" type="button" data-username="${escapeHtml(userData.username)}" data-album-key="${encodeURIComponent(album.key)}">Download album</button>
              <i class="bi bi-chevron-down slskd-chevron" id="chevron-${albumId}"></i>
            </div>
          </div>
          <div id="files-${albumId}" class="slskd-files-container" style="display: none;">
            <div class="slskd-files-list">${fileRows}</div>
          </div>
        </div>
      `;
    });

    html += `
      <div class="slskd-user-group" data-username="${escapeHtml(userData.username)}" data-index="${index}">
        <div class="slskd-user-header" style="cursor: pointer;">
          <div class="slskd-user-info">
            <div class="slskd-username">
              <i class="bi bi-person-circle"></i> ${escapeHtml(userData.username)}
            </div>
            <div class="slskd-user-meta">
              <i class="bi bi-file-earmark-music"></i> ${fileCount} file${fileCount !== 1 ? 's' : ''}
            </div>
          </div>
          <i class="bi bi-chevron-down slskd-chevron" id="chevron-${safeId}"></i>
        </div>
        <div id="files-${safeId}" class="slskd-user-body" style="display: none;">
          ${albumsHtml}
        </div>
      </div>
    `;
  });

  html += '</div>';
  container.innerHTML = html;
  attachSlskdEventHandlers();
}

function attachSlskdEventHandlers() {
  document.querySelectorAll('.slskd-user-header').forEach((header) => {
    header.addEventListener('click', function (e) {
      e.stopPropagation();
      const userGroup = this.closest('.slskd-user-group');
      const index = userGroup.getAttribute('data-index');
      const safeId = `slskd-user-${index}`;
      const filesDiv = document.getElementById(`files-${safeId}`);
      const chevron = document.getElementById(`chevron-${safeId}`);
      if (!filesDiv || !chevron) return;
      const shouldShow = filesDiv.style.display === 'none' || filesDiv.style.display === '';
      filesDiv.style.display = shouldShow ? 'block' : 'none';
      chevron.classList.toggle('rotated', shouldShow);
    });
  });

  document.querySelectorAll('.slskd-album-header').forEach(header => {
    header.addEventListener('click', function (e) {
      if (e.target.closest('.slskd-album-download')) return;
      const targetId = this.getAttribute('data-target');
      // Sanitize targetId for selector
      const safeTargetId = sanitizeId(targetId);
      const filesDiv = document.getElementById(safeTargetId);
      const chevron = this.querySelector('.slskd-chevron');
      if (!filesDiv || !chevron) return;
      const shouldShow = filesDiv.style.display === 'none' || filesDiv.style.display === '';
      filesDiv.style.display = shouldShow ? 'block' : 'none';
      chevron.classList.toggle('rotated', shouldShow);
    });
  });

  document.querySelectorAll('.slskd-album-download').forEach(btn => {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      const username = this.getAttribute('data-username');
      const albumKey = decodeURIComponent(this.getAttribute('data-album-key') || '');
      downloadSlskdAlbum(username, albumKey);
    });
  });

  document.querySelectorAll('.slskd-download-btn').forEach(btn => {
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      const username = this.getAttribute('data-username');
      const filename = this.getAttribute('data-filename');
      const size = parseInt(this.getAttribute('data-size'), 10) || 0;
      downloadSlskdSingle({ username, filename, size });
    });
  });

  document.querySelectorAll('.slskd-file-select').forEach(input => {
    input.addEventListener('change', function () {
      const username = this.getAttribute('data-username');
      const filename = this.getAttribute('data-filename');
      const size = parseInt(this.getAttribute('data-size'), 10) || 0;
      toggleSlskdSelection(username, filename, size, this.checked);
    });
  });
}

function showSlskdError(message) {
  document.getElementById('slskdStatus').style.display = 'none';
  // escapeHtml: `message` can carry a server error string, and previously went
  // straight into innerHTML.
  document.getElementById('slskdResults').innerHTML = `
    <div class="alert alert-danger">
      <i class="bi bi-exclamation-triangle"></i> ${escapeHtml(message)}
    </div>
  `;
  if (slskdPollInterval) {
    clearInterval(slskdPollInterval);
    slskdPollInterval = null;
  }
  resetSlskdState();
}

function downloadSlskdSingle(file) {
  if (!file.username || !file.filename) return;
  // The confirm() was inside this guard, so cancelling ALSO short-circuited
  // the return value. Split so the intent is readable.
  if (!confirm('Download this file from Soulseek?')) return;
  return downloadSlskdBatch([file], 'this file', { confirmed: true });
}

function downloadSlskdAlbum(username, albumKey) {
  const user = slskdResponsesData[username];
  if (!user || !user.albums[albumKey]) return;
  const files = user.albums[albumKey].files.map(f => ({ username, filename: f.filename, size: f.size }));
  if (!files.length) return;
  downloadSlskdBatch(files, `${files.length} track(s) from ${albumKey}`);
}

function downloadSlskdSelected() {
  const files = Array.from(slskdSelected.values());
  if (!files.length) return;
  downloadSlskdBatch(files, `${files.length} selected file(s)`);
}

function downloadSlskdBatch(files, label, options = {}) {
  if (!files || !files.length) return;
  const confirmLabel = label || `${files.length} file(s)`;
  // downloadSlskdSingle already asked; without this the user was prompted
  // twice for a single-file download.
  if (!options.confirmed && !confirm(`Download ${confirmLabel}?`)) return;

  fetch('/api/slskd/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ files })
  })
    .then(response => response.json())
    .then(data => {
      if (data.success) {
        alert('✅ Download(s) enqueued!');
        // Clear the selection once queued — the checkboxes were left ticked,
        // so the next "Download selected" re-queued everything again.
        if (slskdSelected.size) {
          slskdSelected.clear();
          updateSlskdSelectedButton();
          document.querySelectorAll('.slskd-file-select:checked').forEach(cb => { cb.checked = false; });
        }
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to enqueue downloads'));
      }
    })
    .catch(error => {
      alert('❌ Network error: ' + error.message);
    });
}

function refreshSlskdMonitor(options = {}) {
  const silent = options.silent === true;
  const loading = document.getElementById('slskdMonLoading');
  if (!loading) return; // monitor not rendered
  const errorBox = document.getElementById('slskdMonError');
  const results = document.getElementById('slskdMonResults');
  const empty = document.getElementById('slskdMonEmpty');
  const table = document.getElementById('slskdMonTable');
  const tbody = document.getElementById('slskdMonTableBody');
  const countBadge = document.getElementById('slskdMonCount');

  if (slskdMonInFlight) return;
  slskdMonInFlight = true;

  if (!slskdMonLoaded && !silent) {
    loading.style.display = 'block';
  }

  fetch('/api/slskd/status')
    .then(resp => resp.json())
    .then(data => {
      if (loading) loading.style.display = 'none';
      slskdMonLoaded = true;
      if (data.error) {
        if (errorBox) {
          errorBox.textContent = 'Error: ' + data.error;
          errorBox.style.display = 'block';
        }
        if (results) results.style.display = 'none';
        slskdMonInFlight = false;
        return;
      }
      if (errorBox) errorBox.style.display = 'none';
      if (results) results.style.display = 'block';
      const downloads = data.downloads || [];
      if (countBadge) {
        countBadge.style.display = downloads.length ? 'inline-block' : 'none';
        countBadge.textContent = `${downloads.length} active`;
      }
      if (downloads.length === 0) {
        if (empty) empty.style.display = 'block';
        if (table) table.style.display = 'none';
        slskdMonInFlight = false;
        return;
      }
      if (empty) empty.style.display = 'none';
      if (table) table.style.display = 'block';
      if (tbody) tbody.innerHTML = '';

      downloads.forEach(download => {
        const row = document.createElement('tr');
        const state = (download.state || '').toLowerCase();
        let stateClass = 'bg-secondary';
        if (state.includes('inprogress') || state.includes('downloading')) stateClass = 'bg-primary';
        else if (state.includes('completed') || state.includes('complete')) stateClass = 'bg-success';
        else if (state.includes('queued') || state.includes('initializing')) stateClass = 'bg-warning';
        else if (state.includes('error') || state.includes('failed') || state.includes('cancelled')) stateClass = 'bg-danger';

        // Use backend-normalized fields (already extracted in /api/slskd/status)
        const filename = download.filename || 'Unknown';
        const fileSize = download.size || 0;
        const bytesTransferred = download.bytesTransferred || 0;
        const progress = download.progress || 0; // Backend already calculates this
        const speed = download.averageSpeed || 0;
        const remainingBytes = fileSize - bytesTransferred;
        const eta = speed > 0 ? Math.floor(remainingBytes / speed) : 0;

        row.innerHTML = `
          <td><strong>${escapeHtml(download.username || '')}</strong></td>
          <td>
            <div class="text-truncate" style="max-width: 420px;" title="${escapeHtml(filename)}">
              ${escapeHtml(filename)}
            </div>
            <small class="text-muted">${formatBytes(bytesTransferred)} / ${formatBytes(fileSize)}${eta > 0 ? ` · ETA: ${formatETA(eta)}` : ''}</small>
          </td>
          <td class="text-center"><span class="badge ${stateClass}">${escapeHtml(download.state || '')}</span></td>
          <td class="text-center">
            <div class="progress" style="height: 20px; min-width: 100px;">
              <div class="progress-bar ${progress >= 100 ? 'bg-success' : 'bg-primary'}" role="progressbar" style="width: ${progress}%" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100">${progress}%</div>
            </div>
          </td>
          <td class="text-center">${formatBytes(fileSize)}</td>
          <td class="text-center">${speed > 0 ? `<span class="text-primary"><i class="bi bi-arrow-down"></i> ${formatBytes(speed)}/s</span>` : '—'}</td>
          <td class="text-center">
            <button class="btn btn-sm btn-danger slskd-cancel-btn"
                    data-username="${escapeHtml(download.username || '')}"
                    data-filename="${escapeHtml(download.filename || '')}"
                    data-token="${escapeHtml(download.remoteToken || '')}"
                    title="Cancel download">
              <i class="bi bi-x-circle"></i>
            </button>
          </td>
        `;
        if (tbody) tbody.appendChild(row);
      });

      // Delegated instead of inline onclick="cancelSlskdDownload('...')":
      // a filename containing an apostrophe (common in track titles) broke
      // the generated handler, and escapeHtml does not escape for a JS
      // string context.
      if (tbody) {
        tbody.querySelectorAll('.slskd-cancel-btn').forEach(btn => {
          btn.addEventListener('click', function () {
            cancelSlskdDownload(
              this.getAttribute('data-username'),
              this.getAttribute('data-filename'),
              this.getAttribute('data-token')
            );
          });
        });
      }

      slskdMonInFlight = false;
    })
    .catch(err => {
      if (!silent && loading) {
        loading.style.display = 'none';
      }
      if (errorBox) {
        errorBox.textContent = 'Network error: ' + err.message;
        errorBox.style.display = 'block';
      }
      slskdMonInFlight = false;
    });
}

function cancelSlskdDownload(username, filename, token) {
  if (!username || !filename) return;
  if (!confirm(`Cancel download of "${filename}" from ${username}?`)) return;
  fetch('/api/slskd/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, filename, token })
  })
    .then(resp => resp.json())
    .then(data => {
      if (data.success) {
        alert('✅ Download cancelled');
        refreshSlskdMonitor();
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to cancel download'));
      }
    })
    .catch(err => alert('❌ Network error: ' + err.message));
}

function startMonitorAutoRefresh() {
  if (monitorInterval) clearInterval(monitorInterval);
  monitorInterval = setInterval(() => {
    refreshSlskdMonitor({ silent: true });
  }, 5000);
}

// Allow Enter key to trigger search
document.addEventListener('DOMContentLoaded', function () {
  const slskdInput = document.getElementById('slskdSearchInput');
  if (slskdInput) {
    slskdInput.addEventListener('keypress', function (e) {
      if (e.key === 'Enter') performSlskdSearch();
    });
  }

  // Kick off monitors if present (for individual monitor tabs).
  // The same `if (document.getElementById('slskdMonLoading'))` test ran twice
  // in a row; collapsed into one block.
  if (document.getElementById('slskdMonLoading')) {
    refreshSlskdMonitor();
    startMonitorAutoRefresh();
  }

  // Initialize consolidated monitor
  if (document.getElementById('monitorSlskdLoading')) {
    refreshAllMonitors();
    startConsolidatedMonitorRefresh();
  }
});

// ===== Consolidated Download Monitor Functions =====
let monitorRefreshCountdown = 5;
let monitorCountdownInterval = null;
// Two timers need two handles — see startConsolidatedMonitorRefresh below.
let monitorRefreshInterval = null;

function refreshAllMonitors() {
  // Refresh the Soulseek monitor
  refreshConsolidatedSlskdMonitor();

  // Reset countdown
  monitorRefreshCountdown = 5;
  updateMonitorCountdown();
}

function updateMonitorCountdown() {
  const countdownEl = document.getElementById('monitorNextRefresh');
  if (countdownEl) {
    countdownEl.textContent = monitorRefreshCountdown + 's';
    monitorRefreshCountdown--;
    if (monitorRefreshCountdown < 0) {
      monitorRefreshCountdown = 5;
    }
  }
}

function startConsolidatedMonitorRefresh() {
  // FIXED: the 5-second refresh used to be assigned to
  // `monitorCountdownInterval`, then cleared on the VERY NEXT LINE before the
  // 1-second countdown was assigned to the same handle. The refresh was
  // therefore killed immediately after being created: the counter ticked down
  // forever and the monitor never actually reloaded. Two timers, two handles.
  if (monitorRefreshInterval) clearInterval(monitorRefreshInterval);
  if (monitorCountdownInterval) clearInterval(monitorCountdownInterval);

  monitorRefreshInterval = setInterval(refreshAllMonitors, 5000);
  monitorCountdownInterval = setInterval(updateMonitorCountdown, 1000);
}

function refreshConsolidatedSlskdMonitor() {
  const loading = document.getElementById('monitorSlskdLoading');
  const errorBox = document.getElementById('monitorSlskdError');
  const results = document.getElementById('monitorSlskdResults');
  const empty = document.getElementById('monitorSlskdEmpty');
  const table = document.getElementById('monitorSlskdTable');
  const tbody = document.getElementById('monitorSlskdTableBody');
  const countBadge = document.getElementById('monitorSlskdCount');

  // `results` was dereferenced without a null check, so this threw on any
  // page that renders the loading element but not the results container.
  if (!results) return;

  // Only show loading on first load
  if (!results.style.display || results.style.display === 'block') {
    if (loading) loading.style.display = 'none';
  }

  fetch('/api/slskd/status')
    .then(resp => resp.json())
    .then(data => {
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
      if (downloads.length === 0) {
        if (empty) empty.style.display = 'block';
        if (table) table.style.display = 'none';
        updateTotalMonitorCount();
        return;
      }
      if (empty) empty.style.display = 'none';
      if (table) table.style.display = 'block';
      if (tbody) tbody.innerHTML = '';

      downloads.forEach(download => {
        const row = document.createElement('tr');
        const state = (download.state || '').toLowerCase();
        let stateClass = 'bg-secondary';
        if (state.includes('inprogress') || state.includes('downloading')) stateClass = 'bg-primary';
        else if (state.includes('completed') || state.includes('complete')) stateClass = 'bg-success';
        else if (state.includes('queued') || state.includes('initializing')) stateClass = 'bg-warning';
        else if (state.includes('error') || state.includes('failed') || state.includes('cancelled')) stateClass = 'bg-danger';

        const filename = download.filename || 'Unknown';
        const fileSize = download.size || 0;
        const progress = download.progress || 0;
        const speed = download.averageSpeed || 0;

        // The progress bar carried TWO style attributes; the second (font-size)
        // silently overwrote the first (width), so the bar never filled.
        // Merged into one.
        row.innerHTML = `
          <td><small>${escapeHtml(download.username || '')}</small></td>
          <td title="${escapeHtml(filename)}">
            <small class="text-truncate d-block">${escapeHtml(filename)}</small>
          </td>
          <td class="text-center">
            <div class="progress" style="height: 18px; min-width: 80px;">
              <div class="progress-bar ${progress >= 100 ? 'bg-success' : 'bg-primary'}" role="progressbar" style="width: ${progress}%; font-size: 0.75rem;" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100">${progress}%</div>
            </div>
          </td>
          <td class="text-center small">${formatBytes(fileSize)}</td>
          <td class="text-center small">${speed > 0 ? formatBytes(speed) + '/s' : '—'}</td>
          <td class="text-center"><span class="badge ${stateClass}">${escapeHtml(state.substring(0, 8))}</span></td>
        `;
        if (tbody) tbody.appendChild(row);
      });

      updateTotalMonitorCount();
    })
    .catch(err => {
      if (errorBox) {
        errorBox.textContent = 'Network error: ' + err.message;
        errorBox.style.display = 'block';
      }
    });
}

function updateTotalMonitorCount() {
  const slskdCount = document.querySelectorAll('#monitorSlskdTableBody tr').length;
  const total = slskdCount;

  const totalBadge = document.getElementById('monitorTotalCount');
  if (totalBadge) {
    totalBadge.textContent = total;
    totalBadge.style.display = total > 0 ? 'inline-block' : 'none';
  }

  const totalEl = document.getElementById('monitorTotal');
  if (totalEl) {
    totalEl.textContent = total;
  }
}
// ===== END Consolidated Download Monitor Functions =====

// Clean up polling when leaving page.
// The two consolidated-monitor timers were never cleared here, so they kept
// firing against a torn-down DOM during bfcache navigations.
window.addEventListener('beforeunload', () => {
  if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }
  if (monitorInterval) { clearInterval(monitorInterval); monitorInterval = null; }
  if (monitorRefreshInterval) { clearInterval(monitorRefreshInterval); monitorRefreshInterval = null; }
  if (monitorCountdownInterval) { clearInterval(monitorCountdownInterval); monitorCountdownInterval = null; }
});
