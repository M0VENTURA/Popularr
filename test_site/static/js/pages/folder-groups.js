/* ==========================================================================
   static/js/pages/folder-groups.js
   Legacy folder-groups view — shows MusicBrainz releases as "green" folders
   with discovery progress.

   Requires: utils/dom.js, utils/api.js, utils/poller.js,
             ui/modal.js, ui/toast.js, ui/confirm.js

   ── CHANGES FROM THE PREVIOUS VERSION ─────────────────────────────────────
   1. UNESCAPED INTERPOLATION — SECURITY FIX. Every rendered field went
      straight into innerHTML: `${group.display_name}`, `${f.name}`,
      `${data.name}`, `${f.extension}`. These are filesystem paths and
      MusicBrainz titles, i.e. external input. All escaped now.

   2. INLINE onclick WITH RAW PATHS — FIXED BUG.
          onclick="viewFolderContents('${group.name}')"
          onclick="cancelFolderDownloads('${group.name}')"
          onclick="retryMatchingForRelease('${group.release_id}')"
      `group.name` is a raw folder path. Any apostrophe in a folder name
      ("Livin' Thing", "Guns N' Roses") terminated the JS string literal and
      broke the handler; a backslash did the same. Replaced with
      addEventListener + data attributes.

   3. `modal.tabindex = '-1'` — FIXED BUG. That assigns an ordinary JS
      property, not the HTML attribute (which is `tabIndex`, capital I). The
      folder modal therefore never received `tabindex="-1"` and Bootstrap's
      focus trap did not engage, so Escape did not close it. ui/modal.js's
      template sets the attribute properly.

   4. `formatFileSize` deleted — a third copy of formatBytes (after the ones
      in artist_detail and downloads.js). Now from utils/dom.js.

   5. The 5-second `setInterval` is now a managed poller: it pauses when the
      tab is hidden (the old one hand-rolled that with a `document.hidden`
      check inside the tick, but still fired the timer), never overlaps
      requests, and stops on page teardown.

   6. `alert()` → toast, `confirm()` → `await ui.confirm()`.

   NOTE: the stand-down guard is preserved exactly. This view must NOT run
   when monitor.js's `loadFolderGroups` or downloads.js's `loadQueueStatus`
   exist — it renders into the same #folderGroupsList and its refresh used to
   overwrite the real queue items.
   ========================================================================== */

(function (global) {
  'use strict';

  const REFRESH_INTERVAL_MS = 5000;
  const MODAL_ID = 'folderModalView';
  const MAX_FILES_SHOWN = 3;

  let refreshPoller = null;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function formatSize(bytes) {
    return global.formatBytes ? global.formatBytes(bytes) : `${bytes} B`;
  }

  /**
   * True when a newer renderer owns the folder-groups section.
   * See the NOTE in the header — this is not an optimisation.
   */
  function supersededByQueueRenderers() {
    return typeof global.loadFolderGroups === 'function'
      || typeof global.loadQueueStatus === 'function';
  }

  // ── Rendering ───────────────────────────────────────────────────────────

  function buildFilesDisplay(group) {
    const files = group.files || [];
    if (!files.length) {
      return `<small class="text-muted"><i class="bi bi-hourglass-split"></i> ` +
        `Waiting for ${esc(group.total_tracks)} tracks…</small>`;
    }
    let html = files.slice(0, MAX_FILES_SHOWN).map((f) =>
      `<small class="text-success d-block"><i class="bi bi-file-earmark-music"></i> ${esc(f.name)}</small>`
    ).join('');
    if (files.length > MAX_FILES_SHOWN) {
      html += `<small class="text-muted d-block"><em>… and ${files.length - MAX_FILES_SHOWN} more files</em></small>`;
    }
    return html;
  }

  function buildStatusLabel(group) {
    if (group.discovered_count >= group.total_tracks) {
      return '<span class="badge bg-success ms-2">Ready to Finalize</span>';
    }
    if (group.discovered_count > 0) {
      return '<span class="badge bg-info ms-2">In Progress</span>';
    }
    return '<span class="badge bg-warning ms-2">Waiting</span>';
  }

  function buildGroupHtml(group) {
    const isMusicBrainz = group.type === 'musicbrainz';
    const badgeColor = isMusicBrainz ? 'bg-success' : 'bg-secondary';
    const icon = isMusicBrainz ? 'bi-disc' : 'bi-folder';
    const progressColor = isMusicBrainz ? 'bg-success' : 'bg-info';
    const accent = isMusicBrainz ? 'var(--accent-color)' : 'var(--text-tertiary)';
    const pct = Math.max(0, Math.min(100, Number(group.progress_percent) || 0));

    // data-* rather than inline onclick — see header note 2.
    const retryBtn = isMusicBrainz
      ? `<button class="btn btn-outline-warning fg-retry-btn" data-release-id="${esc(group.release_id)}"
                 title="Retry file matching"><i class="bi bi-arrow-repeat"></i></button>`
      : '';

    return `
      <div class="list-group-item" style="border-left:4px solid ${accent};">
        <div class="d-flex justify-content-between align-items-start">
          <div style="flex:1;">
            <div class="d-flex align-items-center gap-2 mb-2">
              <i class="bi ${icon}" style="font-size:1.2rem;"></i>
              <h6 class="mb-0">${esc(group.display_name)}</h6>
              <span class="badge ${badgeColor}" style="font-size:0.75rem;">
                ${isMusicBrainz ? 'Release' : 'Folder'}
              </span>
              ${buildStatusLabel(group)}
            </div>

            <div class="progress mb-2" style="height:20px;">
              <div class="progress-bar ${progressColor}" style="width:${pct}%;">
                <small style="font-weight:bold;">${pct}%</small>
              </div>
            </div>

            <div style="font-size:0.9rem;margin:0.5rem 0;">${buildFilesDisplay(group)}</div>

            <small class="text-muted d-block mt-2">
              <i class="bi bi-file-earmark-music"></i>
              ${esc(group.discovered_count)} of ${esc(group.total_tracks)} tracks discovered
              ${group.metadata ? `<span class="ms-2">·</span> <i class="bi bi-calendar"></i> ${esc(group.metadata.year)}` : ''}
            </small>
          </div>

          <div class="btn-group btn-group-sm ms-2" role="group">
            <button class="btn btn-outline-info fg-view-btn" data-folder="${esc(group.name)}"
                    title="View folder contents"><i class="bi bi-folder-open"></i></button>
            ${retryBtn}
            <button class="btn btn-outline-danger fg-cancel-btn" data-folder="${esc(group.name)}"
                    title="Cancel this folder"><i class="bi bi-x"></i></button>
          </div>
        </div>
      </div>`;
  }

  function attachHandlers(listEl) {
    listEl.querySelectorAll('.fg-view-btn').forEach((btn) => {
      btn.addEventListener('click', () => viewFolderContents(btn.dataset.folder));
    });
    listEl.querySelectorAll('.fg-retry-btn').forEach((btn) => {
      btn.addEventListener('click', () => retryMatchingForRelease(btn.dataset.releaseId, btn));
    });
    listEl.querySelectorAll('.fg-cancel-btn').forEach((btn) => {
      btn.addEventListener('click', () => cancelFolderDownloads(btn.dataset.folder, btn));
    });
  }

  // ── Load ────────────────────────────────────────────────────────────────

  async function load() {
    const section = document.getElementById('folderGroupsSection');
    if (!section) return;
    if (supersededByQueueRenderers()) return;

    try {
      const data = await global.api.getJson('/api/downloads/folder-groups');

      // Do NOT hide the section when empty — the queue renderers own its
      // empty state on pages where they run.
      if (!data.success || data.count === 0) return;

      section.style.display = 'block';
      const badge = document.getElementById('folderGroupsBadge');
      if (badge) badge.textContent = data.count;

      const list = document.getElementById('folderGroupsList');
      if (!list) return;

      list.innerHTML = '<div class="list-group list-group-flush">' +
        (data.folder_groups || []).map(buildGroupHtml).join('') +
        '</div>';
      attachHandlers(list);

      if (global.bootstrap && global.bootstrap.Tooltip) {
        list.querySelectorAll('[title]').forEach((el) => new global.bootstrap.Tooltip(el));
      }
    } catch (error) {
      console.error('Error loading folder groups:', error);
    }
  }

  // ── Folder contents modal ───────────────────────────────────────────────

  async function viewFolderContents(folderPath) {
    try {
      const folderName = String(folderPath || '').split('/').pop();
      const data = await global.api.getJson(
        '/api/downloads/folder/' + encodeURIComponent(folderName)
      );

      if (!data.success) {
        global.toast.error(data.error || 'Could not load folder contents');
        return;
      }

      const rows = (data.files || []).map((f) => `
        <div class="list-group-item ${f.is_audio ? 'list-group-item-success' : ''}">
          <div class="d-flex justify-content-between">
            <div>
              <h6 class="mb-1">
                <i class="bi ${f.is_audio ? 'bi-file-earmark-music' : 'bi-file-earmark'}"></i>
                ${esc(f.name)}
              </h6>
              <small class="text-muted">
                ${esc(formatSize(f.size))} · ${esc(new Date(f.modified).toLocaleString())}
              </small>
            </div>
            ${f.is_audio
              ? '<span class="badge bg-success">Audio</span>'
              : `<span class="badge bg-secondary">${esc(f.extension)}</span>`}
          </div>
        </div>`).join('');

      const bodyHtml = `
        <p class="text-muted small">
          <i class="bi bi-file-earmark-music"></i> ${esc(data.audio_files)} audio files
          <span class="ms-2">·</span>
          <i class="bi bi-file"></i> ${esc(data.file_count)} total files
        </p>
        <div style="max-height:400px;overflow-y:auto;">
          <div class="list-group">${rows}</div>
        </div>`;

      global.modal.open(MODAL_ID, global.modal.template({
        id: MODAL_ID,
        title: data.name,
        icon: 'bi-folder-open',
        bodyHtml,
        footerHtml: '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>',
        size: 'modal-lg',
      }));
    } catch (error) {
      console.error('Error viewing folder:', error);
      global.toast.error('Error viewing folder contents: ' + error.message);
    }
  }

  // ── Actions ─────────────────────────────────────────────────────────────

  async function retryMatchingForRelease(releaseId, button) {
    const accepted = await global.ui.confirm({
      title: 'Retry matching',
      message: 'Retry file matching for this release?',
      tone: 'primary',
      confirmLabel: 'Retry',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        const data = await global.api.postJson(
          `/api/musicbrainz/release/${encodeURIComponent(releaseId)}/retry-match`, {}
        );
        if (!data.success) {
          global.toast.error(data.error || 'Retry failed');
          return;
        }
        const count = (data.unmatched_tracks || []).length;
        global.toast.success(`Retry initiated for ${count} unmatched track${count === 1 ? '' : 's'}`);
        setTimeout(load, 1000);
      } catch (error) {
        console.error('Error retrying match:', error);
        global.toast.error('Error: ' + error.message);
      }
    };

    return button && global.buttonState
      ? global.buttonState.withBusy(button, '', run)
      : run();
  }

  async function cancelFolderDownloads(folderPath, button) {
    const accepted = await global.ui.confirm({
      title: 'Cancel folder',
      message: 'Cancel this folder?',
      detail: 'It will be removed from the download queue.',
      tone: 'danger',
      confirmLabel: 'Cancel folder',
      cancelLabel: 'Keep',
    });
    if (!accepted) return;

    const folderName = String(folderPath || '').split('/').pop();
    const run = async () => {
      try {
        const data = await global.api.postJson(
          `/api/downloads/folder/${encodeURIComponent(folderName)}/cancel`, {}
        );
        if (!data.success) {
          global.toast.error(data.error || 'Cancellation failed');
          return;
        }
        global.toast.success('Folder cancelled and removed from the queue');
        setTimeout(load, 500);
      } catch (error) {
        console.error('Error cancelling folder:', error);
        global.toast.error('Error: ' + error.message);
      }
    };

    return button && global.buttonState
      ? global.buttonState.withBusy(button, '', run)
      : run();
  }

  /**
   * Filter the rendered groups by type.
   * Was comparing `item.style.borderLeftColor` against hard-coded
   * 'rgb(40, 167, 69)' — which stopped matching the moment the accent colour
   * moved to a CSS variable. Now driven by a data attribute.
   */
  function setFolderGroupFilter(filterType) {
    ['All', 'Release', 'Folder'].forEach((suffix) => {
      const btn = document.getElementById('folderFilter' + suffix);
      if (btn) btn.classList.toggle('active', filterType === suffix.toLowerCase());
    });

    document.querySelectorAll('#folderGroupsList .list-group-item').forEach((item) => {
      const isRelease = !!item.querySelector('.fg-retry-btn');
      const show = filterType === 'all'
        || (filterType === 'release' && isRelease)
        || (filterType === 'folder' && !isRelease);
      item.style.display = show ? '' : 'none';
    });
  }

  // ── Refresh lifecycle ───────────────────────────────────────────────────

  function startRefresh() {
    if (refreshPoller) return;
    if (supersededByQueueRenderers()) return;

    refreshPoller = global.poller.create({
      interval: REFRESH_INTERVAL_MS,
      immediate: false,
      pauseWhenHidden: true,
      onTick: load,
    });
    refreshPoller.start();
  }

  function stopRefresh() {
    if (refreshPoller) {
      refreshPoller.stop();
      refreshPoller = null;
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    load();
    startRefresh();
  });

  global.loadFolderGroupsWithMusicBrainz = load;
  global.viewFolderContents = viewFolderContents;
  global.retryMatchingForRelease = retryMatchingForRelease;
  global.cancelFolderDownloads = cancelFolderDownloads;
  global.setFolderGroupFilter = setFolderGroupFilter;
})(window);
