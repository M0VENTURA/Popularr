/* ==========================================================================
   static/js/pages/monitor.js
   Monitor page — CSV inline import, unmatched folders, disk discovery,
   upcoming releases.

   Requires: utils/dom.js, utils/api.js, ui/toast.js, ui/confirm.js,
             ui/button-state.js, ui/status-badge.js
   Still relies on downloads.js for queue rendering (`loadQueueStatus`) and
   on the shared MusicBrainz search component (`openGlobalMbSearch`).

   ── CHANGES FROM THE PREVIOUS VERSION ─────────────────────────────────────
   1. PROGRESS PERCENTAGE WAS NEVER ROUNDED — FIXED BUG.
          csvSetProgress((processedCount / items.length) * 100, …)
      With, say, 3 of 7 batches done that produced
          style="width: 42.857142857142854%"
          textContent "42.857142857142854%"
      so the label showed a 17-digit number mid-import. Rounded once, inside
      csvSetProgress, so every caller benefits.

   2. `deleteAllEmptyFolders` SWALLOWED EVERY FAILURE.
          try { await fetchJsonOrThrow(...) } catch (e) {}
      An empty catch on a delete loop meant a folder that failed to delete
      was reported as pruned. It now counts failures and reports them.

   3. The delete loop also left `btn.disabled = true` if anything threw
      before the final assignment. Now ui/button-state.js, which restores in
      a `finally`.

   4. `escapeHtml` and `fetchJsonOrThrow` were borrowed from downloads.js.
      Now from utils/dom.js and utils/api.js, so this file no longer depends
      on downloads.js having parsed first.

   5. The upcoming-releases table built
          onclick="searchUpcomingReleaseFromEncoded('${encodeURIComponent(...)}')"
      URI-encoding does not escape a single quote, so an artist name
      containing one still terminated the attribute. Replaced with
      addEventListener + data attributes.

   6. Status badges now come from ui/status-badge.js rather than two
      hand-built `status-pill` strings.

   7. `alert()` → toast, `confirm()` → `await ui.confirm()`.
   ========================================================================== */

(function (global) {
  'use strict';

  const CSV_BATCH_SIZE = 10;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  // ── CSV inline import ───────────────────────────────────────────────────

  function csvSetProgress(pct, label, sub) {
    // Rounded here so no caller can emit a fractional width or label.
    const rounded = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
    const bar = document.getElementById('csvInlineProgressBar');
    if (bar) {
      bar.style.width = rounded + '%';
      bar.setAttribute('aria-valuenow', String(rounded));
    }
    const pctEl = document.getElementById('csvInlineProgressPct');
    if (pctEl) pctEl.textContent = rounded + '%';
    if (label !== undefined) {
      const el = document.getElementById('csvInlineProgressLabel');
      if (el) el.textContent = label;
    }
    if (sub !== undefined) {
      const el = document.getElementById('csvInlineProgressSub');
      if (el) el.textContent = sub;
    }
  }

  function csvInlineReset() {
    document.getElementById('csvInlineForm').style.display = '';
    document.getElementById('csvInlineProgress').style.display = 'none';
    document.getElementById('csvInlineResult').style.display = 'none';
    document.getElementById('csvInlineFormEl').reset();
  }

  function buildCsvResultStats(total, added, skipped) {
    const tile = (value, label, variant) => `
      <div class="col-6 col-md-4">
        <div class="card text-center bg-${variant} bg-opacity-10 border-${variant} h-100">
          <div class="card-body py-3">
            <div class="h3 text-${variant} mb-1">${esc(value)}</div>
            <div class="small text-secondary">${esc(label)}</div>
          </div>
        </div>
      </div>`;
    return tile(total, 'Total Tracks', 'info')
      + tile(added, 'Added to Queue', 'success')
      + tile(skipped, 'Already Queued', 'secondary');
  }

  async function csvInlineImport(event) {
    event.preventDefault();

    const fileInput = document.getElementById('csvInlineFile');
    const importName = document.getElementById('csvInlineName').value.trim();
    if (!fileInput.files.length || !importName) return;

    document.getElementById('csvInlineForm').style.display = 'none';
    document.getElementById('csvInlineProgress').style.display = '';
    document.getElementById('csvInlineResult').style.display = 'none';
    csvSetProgress(5, 'Parsing CSV…', 'Reading track metadata from the file.');

    try {
      const formData = new FormData();
      formData.append('file', fileInput.files[0]);
      formData.append('playlist_name', importName);
      formData.append('skip_matching', 'true');

      // FormData must not carry an explicit Content-Type — the browser sets
      // the multipart boundary — so this uses fetch + parseJsonResponse
      // rather than api.postJson.
      const response = await fetch('/api/playlist/import/csv', {
        method: 'POST',
        body: formData,
      });
      const parsed = await global.api.parseJsonResponse(response, 'CSV import');

      const allTracks = parsed.all_tracks || [];
      if (!allTracks.length) throw new Error('No tracks found in CSV');

      const nameSlug = importName.replace(/\s+/g, '_').substring(0, 80);
      const currentYear = String(new Date().getFullYear());

      const items = allTracks.map((track, index) => ({
        artist: track.artist,
        title: track.title,
        album: importName,
        album_artist: 'Various Artists',
        year: currentYear,
        track_number: index + 1,
        duration: track.duration_s || null,
        genres: track.genres || null,
        isrc: track.isrc || null,
      }));

      let totalAdded = 0;
      let totalSkipped = 0;
      let totalFailed = 0;
      let processed = 0;

      csvSetProgress(2, 'Adding to queue…', `Sending ${items.length} track(s) to the queue.`);

      for (let i = 0; i < items.length; i += CSV_BATCH_SIZE) {
        const batch = items.slice(i, i + CSV_BATCH_SIZE);
        try {
          const data = await global.api.postJson('/api/queue/add-batch', {
            items: batch,
            import_group: nameSlug,
            import_type: 'playlist',
            source: 'soulseek',
          });
          totalAdded += data.added || 0;
          totalSkipped += data.skipped || 0;
          totalFailed += data.failed || 0;
        } catch (batchError) {
          console.error('Batch failed:', batchError);
          totalFailed += batch.length;
        }

        processed += batch.length;
        csvSetProgress(
          (processed / items.length) * 100,
          'Adding to queue…',
          `Processed ${processed} of ${items.length}`
        );
      }

      csvSetProgress(100, 'Done', `${totalAdded} track(s) added to the queue.`);
      document.getElementById('csvInlineProgress').style.display = 'none';
      document.getElementById('csvInlineResult').style.display = '';
      document.getElementById('csvInlineResultStats').innerHTML =
        buildCsvResultStats(allTracks.length, totalAdded, totalSkipped);

      // The old version computed `queueOk` and never used it, so a partial
      // failure looked identical to a clean import.
      if (totalFailed > 0) {
        global.toast.warning(
          `${totalFailed} track(s) could not be queued.`,
          'Import finished with errors'
        );
      }
    } catch (error) {
      csvSetProgress(0, 'Error', error.message);
      document.getElementById('csvInlineProgress').style.display = 'none';
      document.getElementById('csvInlineForm').style.display = '';
      global.toast.error('Import failed: ' + error.message);
    }
  }

  // ── Unmatched folders ───────────────────────────────────────────────────

  /** Empty folders from the last render, for the "Prune All" action. */
  let emptyFolders = [];

  // ── Folder rows ────────────────────────────────────────────────────────
  //
  // Built through services/item-groups.js. That module was extracted FROM
  // this section and pages/download-queue.js's group rows, which had drifted
  // into two implementations of the same pattern — the completed queue list
  // had even ended up repeating its album actions on every track row. The
  // markup produced here is unchanged; only the source moved, so the
  // .unmatched-*-btn selectors that attachFolderActions binds still match.

  function buildFolderActions(folder) {
    const pathData = {
      path: folder.name,
      artist: folder.artist || '',
      album: folder.album || '',
    };
    const isAssociated = !!(folder.match || folder.release_mbid);
    const buttons = [];

    if (isAssociated) {
      buttons.push(global.itemGroups.actionButton({
        className: 'btn-outline-warning unmatched-change-match-btn',
        icon: 'bi-arrow-repeat', label: 'Change Match', data: pathData,
      }));
      buttons.push(global.itemGroups.actionButton({
        className: 'btn-success unmatched-confirm-btn',
        icon: 'bi-check-lg', label: 'Confirm Match',
        data: { path: folder.name, mbid: folder.release_mbid || '' },
      }));
    } else {
      buttons.push(global.itemGroups.actionButton({
        className: 'btn-outline-primary unmatched-match-btn',
        icon: 'bi-search', label: 'Match', data: pathData,
      }));
    }

    buttons.push(global.itemGroups.actionButton({
      className: 'btn-outline-danger unmatched-delete-btn',
      icon: 'bi-trash3', label: 'Delete', data: { path: folder.name },
    }));

    return buttons.join('');
  }

  function buildFolderRow(folder) {
    const badge = folder.status === 'matched'
      ? global.itemGroups.statusBadge('complete', { label: 'Matched' })
      : global.itemGroups.statusBadge('queued', { label: `${folder.audio_count || 0} audio` });

    const subtitle = folder.artist && folder.album
      ? `<div class="text-muted small mt-1">${esc(folder.artist)} — ${esc(folder.album)}</div>`
      : '';

    return global.itemGroups.rowShell({
      align: 'start',
      // The action cluster wraps on this page because the labels are long
      // ("Change Match" / "Confirm Match") — hence the explicit class.
      actionsClass: 'd-flex flex-shrink-0 gap-1 flex-wrap justify-content-end',
      titleHtml:
        '<div class="text-truncate">' +
        '<i class="bi bi-folder2 me-1 text-muted"></i>' +
        `<strong>${esc(folder.display_name || folder.name)}</strong>` +
        `<span class="ms-2">${badge}</span>` +
        '</div>',
      subtitleHtml: subtitle,
      actionsHtml: buildFolderActions(folder),
    });
  }

  async function renderUnmatchedFolders() {
    const section = document.getElementById('unmatchedFoldersSection');
    const list = document.getElementById('unmatchedFoldersList');
    const badge = document.getElementById('unmatchedFoldersBadge');
    if (!section || !list) return;

    try {
      const data = await global.api.getJson('/api/downloads/unmatched-folders');
      const folders = data.folders || [];

      if (!folders.length) {
        section.style.display = 'none';
        list.innerHTML = '';
        emptyFolders = [];
        if (badge) badge.textContent = '0 items';
        return;
      }

      section.style.display = 'block';
      if (badge) badge.textContent = `${folders.length} item(s)`;

      emptyFolders = folders.filter((f) => f.status !== 'matched' && !(f.audio_count > 0));
      const withContent = folders.filter((f) => f.status === 'matched' || (f.audio_count || 0) > 0);

      let html = '';
      if (withContent.length) {
        html += '<div class="list-group list-group-flush">'
          + withContent.map(buildFolderRow).join('')
          + '</div>';
      }
      if (emptyFolders.length) {
        html += `
          <div class="list-group list-group-flush">
            <div class="list-group-item d-flex justify-content-between align-items-center">
              <span class="text-muted">
                <i class="bi bi-folder-x me-1"></i> Empty Folders (${emptyFolders.length})
              </span>
              <button class="btn btn-sm btn-outline-danger py-0" id="pruneEmptyFoldersBtn">
                <i class="bi bi-trash3"></i> Prune All
              </button>
            </div>
          </div>`;
      }

      list.innerHTML = html;
      attachFolderActions(list);
    } catch (error) {
      console.error('Error loading unmatched folders:', error);
    }
  }

  async function deleteFolder(path) {
    return global.api.postJson('/api/downloads/folder/delete', { folder_path: path });
  }

  /**
   * Delete every empty folder.
   * Failures are counted and reported — the previous version used an empty
   * catch, so a folder that failed to delete was silently treated as pruned.
   */
  async function pruneEmptyFolders(button) {
    const folders = emptyFolders.slice();
    if (!folders.length) return;

    const accepted = await global.ui.confirm({
      title: 'Prune empty folders',
      message: `Delete ${folders.length} empty folder${folders.length === 1 ? '' : 's'}?`,
      items: folders.map((f) => f.display_name || f.name),
      tone: 'danger',
      confirmLabel: 'Delete',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, 'Pruning…', async () => {
      const failed = [];
      for (const folder of folders) {
        try {
          await deleteFolder(folder.name);
        } catch (error) {
          failed.push(folder.display_name || folder.name);
        }
      }

      if (failed.length) {
        global.toast.error(
          `${failed.length} of ${folders.length} could not be deleted.`,
          'Prune incomplete'
        );
      } else {
        global.toast.success(`Deleted ${folders.length} empty folder(s)`);
      }
      await renderUnmatchedFolders();
    });
  }

  function attachFolderActions(listEl) {
    if (!listEl) return;

    // Same selector -> handler table shape as download-queue.js, bound through
    // services/item-groups.js. The confirm and delete handlers keep their
    // button-busy wrapper, which restores the button in a `finally` — the
    // hand-rolled version used to leave it disabled when a request threw.
    global.itemGroups.bindActions(listEl, {
      '.unmatched-match-btn, .unmatched-change-match-btn': function () {
        openFolderMbSearch(
          this.dataset.path,
          this.classList.contains('unmatched-change-match-btn'),
          this.dataset.artist,
          this.dataset.album
        );
      },
      '.unmatched-confirm-btn': function () {
        const btn = this;
        return global.buttonState.withBusy(btn, '', async () => {
          try {
            await global.api.postJson('/api/downloads/confirm-match', {
              folder_path: btn.dataset.path,
              release_mbid: btn.dataset.mbid,
            });
            await renderUnmatchedFolders();
            if (typeof global.loadQueueStatus === 'function') global.loadQueueStatus();
          } catch (error) {
            global.toast.error(error.message);
          }
        });
      },
      '.unmatched-delete-btn': function () {
        const btn = this;
        return (async () => {
          const accepted = await global.ui.confirm({
            title: 'Delete folder',
            message: 'Delete this folder?',
            detail: btn.dataset.path,
            tone: 'danger',
            confirmLabel: 'Delete',
          });
          if (!accepted) return;

          return global.buttonState.withBusy(btn, '', async () => {
            try {
              await deleteFolder(btn.dataset.path);
              await renderUnmatchedFolders();
            } catch (error) {
              global.toast.error(error.message);
            }
          });
        })();
      },
    });

    const pruneBtn = listEl.querySelector('#pruneEmptyFoldersBtn');
    if (pruneBtn) {
      pruneBtn.addEventListener('click', () => pruneEmptyFolders(pruneBtn));
    }
  }

  /**
   * Open the shared MusicBrainz search to associate a folder with a release.
   */
  function openFolderMbSearch(folderPath, isChange, detectedArtist, detectedAlbum) {
    if (typeof global.openGlobalMbSearch !== 'function') {
      global.toast.error('The MusicBrainz search component is not loaded on this page.');
      return;
    }

    // Captured in the closure rather than parked on window._folderMatchTarget,
    // which a second search could overwrite mid-flight.
    const target = folderPath;

    global._mbSearchIncludeOwned = true;
    global.openGlobalMbSearch(detectedArtist, detectedAlbum, async function (selected) {
      try {
        await global.api.postJson('/api/downloads/folder/associate', {
          folder_path: target,
          mb_id: selected.id,
        });
        await renderUnmatchedFolders();
      } catch (error) {
        global.toast.error('Could not associate folder: ' + error.message);
      }
    });
  }

  // ── Disk actions ────────────────────────────────────────────────────────

  function discoverFiles(event) {
    const button = event && event.currentTarget;
    return global.buttonState.withBusy(button, 'Scanning…', async () => {
      try {
        await global.api.postJson('/api/downloads/discover', {});
        if (typeof global.loadQueueStatus === 'function') await global.loadQueueStatus();
        await renderUnmatchedFolders();
      } catch (error) {
        global.toast.error(error.message);
      }
    });
  }

  function processAlbums(event) {
    const button = event && event.currentTarget;
    return global.buttonState.withBusy(button, 'Processing…', async () => {
      try {
        await global.api.postJson('/api/downloads/process-albums', {});
        if (typeof global.loadQueueStatus === 'function') await global.loadQueueStatus();
        await renderUnmatchedFolders();
      } catch (error) {
        global.toast.error(error.message);
      }
    });
  }

  // ── Upcoming releases ───────────────────────────────────────────────────

  async function refreshUpcomingReleases() {
    const container = document.getElementById('upcomingReleasesMonitor');
    if (!container) return;

    const filterCollection =
      document.getElementById('upcomingFilterCollectionMonitor')?.checked || false;

    container.innerHTML =
      '<div class="text-center py-4"><div class="spinner-border text-primary spinner-border-sm" role="status"></div>' +
      '<p class="mt-2 small mb-0">Loading upcoming releases…</p></div>';

    try {
      const data = await global.api.getJson(
        `/api/upcoming-releases?include_queue=true${filterCollection ? '&collection=true' : ''}`
      );
      const releases = data.releases || [];

      if (!releases.length) {
        container.innerHTML =
          '<div class="text-center py-4"><p class="text-muted mb-0">No upcoming releases found.</p></div>';
        return;
      }

      // data-* rather than an onclick carrying encodeURIComponent output —
      // URI encoding does not escape a single quote, so an artist name
      // containing one broke the generated attribute.
      container.innerHTML = `
        <div class="table-responsive">
          <table class="table table-sm table-dark table-hover mb-0">
            <thead>
              <tr><th>Artist</th><th>Album</th><th>Date</th><th style="width:120px;">Action</th></tr>
            </thead>
            <tbody>
              ${releases.map((r) => `
                <tr>
                  <td>${esc(r.artist_name)}</td>
                  <td>${esc(r.album_name)}</td>
                  <td><small>${esc(r.release_date || 'TBA')}</small></td>
                  <td>
                    <button class="btn btn-sm btn-outline-primary upcoming-search-btn"
                            data-artist="${esc(r.artist_name)}"
                            data-album="${esc(r.album_name)}">
                      <i class="bi bi-search"></i> Search
                    </button>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>`;

      container.querySelectorAll('.upcoming-search-btn').forEach((btn) => {
        btn.addEventListener('click', function () {
          if (typeof global.searchMusicBrainzRelease === 'function') {
            global.searchMusicBrainzRelease(null, this.dataset.artist, this.dataset.album);
          }
        });
      });
    } catch (error) {
      container.innerHTML =
        '<div class="text-center py-4"><p class="text-danger mb-2">' +
        `<i class="bi bi-exclamation-triangle"></i> Error loading upcoming releases: ${esc(error.message)}` +
        '</p></div>';
    }
  }

  async function checkForUpdates() {
    try {
      localStorage.setItem('upcomingReleasesLastChecked', Date.now().toString());
    } catch (e) {
      /* private browsing */
    }
    await refreshUpcomingReleases();
  }

  // ── Page chrome ─────────────────────────────────────────────────────────

  /**
   * "Upcoming Releases" button in the page header.
   *
   * This was `onclick="jumpToUpcomingReleases()"` in the template, with the
   * function defined in that template's own inline <script>. Moving it here
   * means templates/Pages/downloads/monitor.html ships with no JavaScript at
   * all, and the scroll-then-navigate fallback has one definition instead of
   * being duplicated in the queue template (which referenced the function
   * without defining it — a ReferenceError on every click until it was
   * reduced to a plain link).
   */
  function jumpToUpcomingReleases() {
    const section = document.getElementById('upcomingReleasesSection');
    if (section) {
      section.scrollIntoView({ behavior: 'smooth' });
      return;
    }
    global.location.href = '/downloads/discover/upcoming';
  }

  const ACTIONS = {
    'jump-to-upcoming': jumpToUpcomingReleases,
  };

  function bindActions() {
    document.addEventListener('click', function (event) {
      const el = event.target.closest ? event.target.closest('[data-action]') : null;
      if (!el) return;
      const handler = ACTIONS[el.getAttribute('data-action')];
      if (!handler) return;
      event.preventDefault();
      handler(el);
    });
  }

  // ── Init ────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    bindActions();
    renderUnmatchedFolders();
    if (document.getElementById('upcomingReleasesMonitor')) {
      refreshUpcomingReleases();
    }
  });

  global.csvInlineReset = csvInlineReset;
  global.csvInlineImport = csvInlineImport;
  global.renderUnmatchedFolders = renderUnmatchedFolders;
  global.discoverFiles = discoverFiles;
  global.processAlbums = processAlbums;
  global.refreshUpcomingReleasesMonitor = refreshUpcomingReleases;
  global.checkForUpdatesMonitor = checkForUpdates;
})(window);
