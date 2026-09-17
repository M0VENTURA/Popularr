/* ==========================================================================
   static/js/pages/download-queue.js
   Download queue: status, rendering, grouping, per-item and per-group
   actions, the Organize Group modal, and the managed MusicBrainz download
   list.

   Load order:
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/modal.js  →  ui/confirm.js
       ui/button-state.js  →  ui/status-badge.js
       services/mb-search.js  →  services/slskd.js  →  services/musicbrainz.js
       downloads.js

   ── WHAT WAS REMOVED ──────────────────────────────────────────────────────
   This file was ~2,890 lines. These sections now live elsewhere:

     fetchJsonOrThrow, escapeHtml, escapeJsString,
     formatBytes, formatDuration                    → utils/*.js
     showToastMsg                                   → ui/toast.js
     getMbStatusBadge                               → ui/status-badge.js
     performMbSearch, clearMbSearch, doLookup,
     handleGlobalMbSelect, confirmReleaseSelection  → services/mb-search.js
     searchSoulseek, pollSlskdSearchResults, and
     the entire manual-search modal                 → services/slskd.js
     searchMusicBrainzRelease, displayMusicBrainzResults,
     getSelectedTracksForRelease, updateMBSelectionUI,
     markMBTrackQueued, downloadMusicBrainzRelease  → services/musicbrainz.js

   The upcoming-releases block (clearUpcomingReleases, scrapeUpcomingReleases,
   refreshUpcomingReleases) is NOT included here. There is a separate
   static/js/upcoming_releases.js in the tree which I could not read, so I
   cannot tell whether these are duplicates of it or the only copy. CHECK
   THAT FILE before deleting the originals — if upcoming_releases.js already
   owns them, the downloads.js copies are dead weight; if not, they need a
   home.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. GROUP ACTIONS COULD ACT ON THE WRONG ALBUM.
      `window.__queueGroupsArr` was a single global overwritten by EVERY
      render pass:
          renderQueueSection()          → sets it to the queue-section groups
          renderQueuePage()
            → renderQueueList('active')    → overwrites
            → renderQueueList('completed') → overwrites
            → renderQueueList('failed')    → overwrites (wins)
      All four run inside one loadQueueStatus() cycle, so after a poll the
      array held only the FAILED groups. Every action button passed a bare
      array INDEX — organizeGroup(2), deleteGroup(0) — so clicking "remove
      all tracks" on a completed album indexed into the failed list and
      deleted a different album's tracks. Groups are now keyed by a stable
      id and looked up per-list.

   2. PAGINATION DID NOTHING. `getQueuePageUrl(limit, offset)` was defined
      and never called. changeQueuePage() advanced `queuePageOffset`, and
      updateQueuePageControls() reported ranges from it, but every fetch used
      a hard-coded `?limit=200` / `?limit=500` with no offset — so the Next
      button relabelled the summary and showed the same 200 rows.

   3. APOSTROPHES BROKE THE MANUAL-SEARCH BUTTON.
          onclick="manualQueueSlskdSearch('${encodeURIComponent(query)}', id)"
      encodeURIComponent does NOT escape the apostrophe (it is in the
      unreserved set), so a query like "Livin' Thing" terminated the JS
      string literal. The file's own encodeInlineArg() adds
      `.replace(/'/g, '%27')` for exactly this reason — this call site just
      never used it. Now addEventListener + dataset.

   4. FOUR REQUESTS PER POLL TO ONE ENDPOINT. loadQueueStatus() hit
      /api/downloads/queue once for counts, then renderQueueSection() fetched
      it again (limit=200) and renderQueuePage() a third time (limit=500),
      plus two event-log calls — every 10 seconds. One fetch now feeds all
      the renderers.
   ========================================================================== */

(function (global) {
  'use strict';

  const QUEUE_ENDPOINT = '/api/downloads/queue';
  const QUEUE_PAGE_LIMIT = 500;
  const POLL_INTERVAL_MS = 10000;
  const MB_DEFAULT_MAX_RETRIES = 3;
  const ORGANIZE_TIMEOUT_MS = 300000;

  let queuePageOffset = 0;
  let queuePoller = null;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
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

  function formatDuration(value) {
    return global.formatDuration ? global.formatDuration(value) : String(value || '');
  }

  // ── Grouping ────────────────────────────────────────────────────────────

  /**
   * Every group built this cycle, keyed by its stable group key.
   *
   * Replaces `window.__queueGroupsArr` — see bug 1 in the header. Action
   * buttons carry `data-group-key`, not an array index, so a stale render
   * can no longer point an action at the wrong album.
   */
  const groupRegistry = Object.create(null);

  /**
   * Bucket queue items into album groups.
   *
   * @param {Array<Object>} items
   * @returns {Array<Object>} [{ key, label, sublabel, items }]
   */
  function buildQueueGroups(items) {
    const groups = [];
    const byKey = Object.create(null);

    (items || []).forEach((item) => {
      const album = (item.album || item.queue_folder || '').trim();
      const artist = (item.album_artist || item.artist || '').trim();
      const title = (item.title || '').trim();

      let key;
      let label;
      let sublabel;

      // "default" and "manual" are sentinel import groups — treating them as
      // real groups would merge every unrelated legacy row into one album.
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

      if (!byKey[key]) {
        byKey[key] = { key, label, sublabel, items: [] };
        groups.push(byKey[key]);
      }
      byKey[key].items.push(item);
    });

    return groups;
  }

  function registerGroups(kind, groups) {
    groups.forEach((group) => {
      // Scope by kind: the same album can legitimately appear in both the
      // completed and failed lists with different item subsets.
      groupRegistry[kind + '::' + group.key] = group;
    });
  }

  function lookupGroup(kind, key) {
    return groupRegistry[kind + '::' + key] || null;
  }

  function sanitizeGroupKey(key) {
    return global.itemGroups.sanitizeId(key);
  }

  // ── Expansion state ─────────────────────────────────────────────────────
  //
  // The set, the toggle wiring and the restore-after-render logic now live in
  // services/item-groups.js, so the folder list on the monitor page can share
  // them instead of growing a second copy. The local wrappers below are kept
  // so the call sites in this file read unchanged.
  //
  // The contextKey matters: without it a queue album and a download folder
  // with the same id would collapse each other.

  const GROUP_CONTEXT = 'queue';
  const TOGGLE_CLASS = 'queue-group-toggle';
  const CHEVRON_CLASS = 'queue-group-chevron';
  const BODY_CLASS = 'queue-group-body';

  function restoreGroupExpansion(listEl) {
    global.itemGroups.restoreExpansion(listEl, {
      contextKey: GROUP_CONTEXT,
      bodyClass: BODY_CLASS,
      chevronClass: CHEVRON_CLASS,
    });
  }

  function attachGroupToggles(listEl) {
    global.itemGroups.attachToggles(listEl, {
      contextKey: GROUP_CONTEXT,
      toggleClass: TOGGLE_CLASS,
      bodyClass: BODY_CLASS,
      chevronClass: CHEVRON_CLASS,
    });
  }

  // ── Row rendering ───────────────────────────────────────────────────────

  function statusPill(item) {
    const status = item.status || 'queued';
    let label = global.statusBadge ? global.statusBadge.label(status) : status;

    if (status === 'downloading' && item.progress != null && Number(item.progress) > 0) {
      const pct = Math.round(Math.max(0, Math.min(100, Number(item.progress))));
      label = `Downloading ${pct}%`;
    }

    return global.statusBadge
      ? global.statusBadge.pill(status, { label, icon: false })
      : `<span class="badge status-pill">${esc(label)}</span>`;
  }

  function metaChips(item) {
    const chips = [];

    if (item.album && item.album !== item.title) {
      chips.push(`<span class="meta-pill"><i class="bi bi-disc"></i>${esc(item.album)}</span>`);
    }

    const mbid = item.release_mbid || item.release_id || item.recording_mbid || '';
    if (mbid) {
      chips.push(
        `<span class="meta-pill" title="${esc(mbid)}">` +
        `<i class="bi bi-fingerprint"></i>${esc(String(mbid).slice(0, 8))}</span>`
      );
    }

    if (item.duration) {
      chips.push(`<span class="meta-pill"><i class="bi bi-clock"></i>${esc(formatDuration(item.duration))}</span>`);
    }

    if (item.track_number) {
      chips.push(
        `<span class="meta-pill"><i class="bi bi-music-note"></i>Track ${esc(String(item.track_number))}</span>`
      );
    }

    return chips.join('');
  }

  function progressBar(item) {
    const status = item.status || 'queued';
    if (status !== 'downloading' || item.progress == null || Number(item.progress) <= 0) return '';
    const pct = Math.min(100, Math.max(0, Number(item.progress)));
    return `
      <div class="progress mt-2" style="height:6px;max-width:260px;">
        <div class="progress-bar bg-primary" role="progressbar" style="width:${pct}%"
             aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"></div>
      </div>`;
  }

  function itemActions(item, kind, standalone) {
    const status = item.status || 'queued';
    const id = parseInt(item.id, 10) || 0;
    let html = '';

    if (kind !== 'completed') {
      const query = [item.artist, item.title, (item.album && item.album !== item.title) ? item.album : '']
        .filter(Boolean).join(' ');
      if (query) {
        // data-* rather than encodeURIComponent-in-an-onclick — see bug 3.
        html += `<button class="btn btn-sm btn-outline-info py-0 px-2 ms-1 queue-manual-search"
                         data-query="${esc(query)}" data-queue-id="${id}"
                         title="Search Soulseek manually"><i class="bi bi-search"></i></button>`;
      }
    }

    if (kind === 'active' && ['downloading', 'searching', 'processing'].includes(status)) {
      html += `<button class="btn btn-sm btn-outline-danger py-0 px-2 ms-1 queue-cancel"
                       data-queue-id="${id}" title="Cancel download"><i class="bi bi-x-circle"></i></button>`;
    }

    if (kind === 'failed' || status === 'failed') {
      html += `<button class="btn btn-sm btn-outline-warning py-0 px-2 ms-1 queue-retry"
                       data-queue-id="${id}" title="Retry"><i class="bi bi-arrow-clockwise"></i></button>`;
    }

    // COMPLETED items get their actions from the ALBUM ROW, not from each
    // track row — see the note on renderGroupedList. `standalone` is set only
    // for an album that rendered as a single ungrouped row, which has no
    // album row above it to carry the actions.
    if (kind === 'completed' && standalone) {
      html += `<button class="btn btn-sm btn-outline-success py-0 px-2 ms-1 queue-organize"
                       data-queue-id="${id}" title="Copy to library"><i class="bi bi-folder-plus"></i></button>`;
    }

    // A completed track has no individual Delete either: removing one track of
    // an organised album is what the album row's Delete is for, and having it
    // on every row made it easy to delete one file while meaning to clear the
    // album. Every other kind keeps its per-item Delete.
    if (kind !== 'completed' || standalone) {
      html += `<button class="btn btn-sm btn-outline-danger py-0 px-2 ms-1 queue-delete"
                       data-queue-id="${id}" title="Remove"><i class="bi bi-trash"></i></button>`;
    }

    return html;
  }

  function renderItemRow(item, kind, standalone) {
    const status = item.status || 'queued';
    const subtitle = item.artist
      ? `<br><small class="text-muted">${esc(item.artist)}` +
        `${item.album && item.album !== item.title ? ' - ' + esc(item.album) : ''}</small>`
      : '';
    const failure = (kind === 'failed' && item.failure_reason)
      ? `<div class="small text-danger mt-1"><i class="bi bi-exclamation-triangle"></i> ${esc(item.failure_reason)}</div>`
      : '';

    return `
      <div class="list-group-item">
        <div class="d-flex justify-content-between align-items-center gap-2">
          <div style="min-width:0;">
            <div class="text-truncate">
              <strong>${esc(item.title || item.album || 'Unknown')}</strong>${subtitle}
            </div>
            <div class="d-flex align-items-center gap-1 flex-wrap mt-1">
              ${statusPill(item)}${metaChips(item)}
            </div>
            ${progressBar(item)}
            ${failure}
          </div>
          <div class="d-flex align-items-center gap-1 flex-shrink-0">${itemActions(item, kind, standalone)}</div>
        </div>
      </div>`;
  }

  function renderGroupRow(group, kind) {
    const bodyId = `queueGroupBody_${kind}_${sanitizeGroupKey(group.key)}`;
    const items = group.items;
    const total = items.length;

    const counts = {};
    items.forEach((item) => {
      const status = item.status || 'queued';
      counts[status] = (counts[status] || 0) + 1;
    });
    const summary = Object.keys(counts).map((s) => `${counts[s]} ${s}`).join(' · ');

    const subline = group.sublabel
      ? ` <small class="text-muted">${esc(group.sublabel)}</small>`
      : '';

    // Every group action carries the group KEY, not an index — see bug 1.
    const keyData = { 'group-key': group.key, 'group-kind': kind };
    const buttons = [];

    if (kind === 'active') {
      const hasActive = items.some((i) =>
        ['downloading', 'searching', 'processing'].includes(i.status));
      if (hasActive) {
        buttons.push(global.itemGroups.actionButton({
          className: 'btn-outline-danger group-cancel', icon: 'bi-x-circle', data: keyData,
          title: 'Cancel all active downloads',
        }));
      }
    }
    if (kind === 'completed') {
      // The album row owns the completed actions — see renderGroupedList.
      buttons.push(global.itemGroups.actionButton({
        className: 'btn-outline-success group-organize', icon: 'bi-folder-plus', data: keyData,
        title: 'Copy all tracks to the music library',
      }));
    }
    if (kind === 'failed') {
      buttons.push(global.itemGroups.actionButton({
        className: 'btn-outline-warning group-retry', icon: 'bi-arrow-clockwise', data: keyData,
        title: 'Retry all failed tracks',
      }));
    }
    if (group.key.startsWith('alb_') || group.key.startsWith('grp_')) {
      buttons.push(global.itemGroups.actionButton({
        className: 'btn-outline-success group-organize-modal', icon: 'bi-folder-check', data: keyData,
        title: 'Organize and move this album (folder format and metadata)',
      }));
    }
    buttons.push(global.itemGroups.actionButton({
      className: 'btn-outline-danger group-delete', icon: 'bi-trash', data: keyData,
      title: 'Remove all tracks in this album',
    }));

    // The action classes above (group-cancel, group-organize, …) are the FIRST
    // token so attachRowHandlers' selectors keep matching after the shared
    // builder prepends the btn/btn-sm/btn-outline-* utilities.
    const children = items.map((item) => renderItemRow(item, kind)).join('');

    return global.itemGroups.rowShell({
      align: 'center',
      collapsible: {
        toggleClass: TOGGLE_CLASS,
        chevronClass: CHEVRON_CLASS,
        bodyClass: BODY_CLASS,
        bodyId,
        toggleTitle: 'Expand album',
        expanded: false,
      },
      titleHtml:
        `<strong><i class="bi bi-folder2-open me-1"></i>${esc(group.label)}</strong>${subline}`,
      metaHtml: `<br><small class="text-muted">${total} track${total === 1 ? '' : 's'} · ${esc(summary)}</small>`,
      actionsHtml: buttons.join(''),
      bodyHtml: children,
    });
  }

  // ── Event wiring for rendered rows ──────────────────────────────────────

  function attachRowHandlers(listEl) {
    if (!listEl) return;

    // One selector -> handler table instead of four copy-pasted
    // querySelectorAll/addEventListener loops. The binding itself lives in
    // services/item-groups.js so the monitor page's folder rows use the same
    // path.
    const handlers = {
      '.queue-manual-search': function () {
        manualQueueSearch(this.dataset.query, parseInt(this.dataset.queueId, 10) || null);
      },
      '.queue-cancel': function () {
        cancelQueueItem(parseInt(this.dataset.queueId, 10), this);
      },
      '.queue-retry': function () {
        retryQueueItem(parseInt(this.dataset.queueId, 10), this);
      },
      '.queue-organize': function () {
        organizeFile(parseInt(this.dataset.queueId, 10), this);
      },
      '.queue-delete': function () {
        deleteQueueItem(parseInt(this.dataset.queueId, 10), false, this);
      },
      '.group-cancel': function () {
        cancelGroup(this.dataset.groupKind, this.dataset.groupKey, this);
      },
      '.group-retry': function () {
        retryGroup(this.dataset.groupKind, this.dataset.groupKey, this);
      },
      '.group-organize': function () {
        organizeGroup(this.dataset.groupKind, this.dataset.groupKey, this);
      },
      '.group-delete': function () {
        deleteGroup(this.dataset.groupKind, this.dataset.groupKey, this);
      },
      '.group-organize-modal': function () {
        const group = lookupGroup(this.dataset.groupKind, this.dataset.groupKey);
        if (group) openOrganizeGroupModal(group);
      },
    };

    global.itemGroups.bindActions(listEl, handlers);

    attachGroupToggles(listEl);
    restoreGroupExpansion(listEl);
  }

  function renderGroupedList(listEl, kind, items) {
    const groups = buildQueueGroups(items);
    registerGroups(kind, groups);

    // DE-DUPLICATED 2026-09-18 — the completed list used to repeat the album's
    // actions on every track row underneath it: the album row carried
    // Organize / Organize & Move / Delete and then each track carried Organize
    // / Delete again. Two ways to do the same thing, with the row the user is
    // most likely to click not being the one that acts on the album.
    //
    // This now follows the Matched & Unmatched Folders pattern in
    // pages/monitor.js: one action cluster per FOLDER (album), and the tracks
    // inside are a plain list. `standalone` is the one exception — an album
    // that is a single track renders as a bare row with no album header above
    // it, so that row has to carry the actions itself.
    const rows = groups.map((group) =>
      group.items.length === 1
        ? renderItemRow(group.items[0], kind, true)
        : renderGroupRow(group, kind)
    );

    listEl.innerHTML = '<div class="list-group list-group-flush">' + rows.join('') + '</div>';
    attachRowHandlers(listEl);
  }

  // ── Queue status ────────────────────────────────────────────────────────

  function setCount(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = String(value);
  }

  function countStatuses(statusCounts, ...statuses) {
    return statuses.reduce((sum, status) => sum + Number(statusCounts[status] || 0), 0);
  }

  function updatePageControls(total, loaded) {
    const summary = document.getElementById('queuePageSummary');
    const prevBtn = document.getElementById('queuePrevPageBtn');
    const nextBtn = document.getElementById('queueNextPageBtn');

    const safeTotal = Number(total || 0);
    const safeLoaded = Number(loaded || 0);
    const start = safeTotal === 0 ? 0 : queuePageOffset + 1;
    const end = safeTotal === 0 ? 0 : Math.min(queuePageOffset + safeLoaded, safeTotal);

    if (summary) {
      summary.textContent = safeTotal === 0 ? 'Showing 0 of 0' : `Showing ${start}-${end} of ${safeTotal}`;
    }
    if (prevBtn) prevBtn.disabled = queuePageOffset <= 0;
    if (nextBtn) nextBtn.disabled = (queuePageOffset + safeLoaded) >= safeTotal;
  }

  function changeQueuePage(direction) {
    const next = Math.max(0, queuePageOffset + (direction * QUEUE_PAGE_LIMIT));
    if (next === queuePageOffset) return;
    queuePageOffset = next;
    loadQueueStatus();
  }

  /**
   * Fetch the queue once and drive every renderer from that one response.
   * The previous version issued four separate requests to this endpoint per
   * poll — see bug 4.
   */
  async function loadQueueStatus() {
    let data;
    try {
      // The offset is now actually sent. getQueuePageUrl() existed for this
      // and was never called — see bug 2.
      const params = new URLSearchParams({
        limit: String(QUEUE_PAGE_LIMIT),
        offset: String(Math.max(0, queuePageOffset)),
      });
      data = await global.api.getJson(`${QUEUE_ENDPOINT}?${params.toString()}`);
    } catch (error) {
      console.error('Error loading queue status:', error);
      return;
    }

    const statusCounts = (data && data.status_counts) || {};
    const items = (data && data.queue) || [];
    const completed = (data && data.completed) || [];

    setCount('queueTotalCount', countStatuses(statusCounts,
      'queued', 'searching', 'processing', 'unmatched', 'pending_match', 'discovered',
      'queried', 'matched', 'downloading', 'completed', 'moving', 'importing', 'failed',
      'possible_duplicate', 'duplicate'));
    setCount('queueQueuedCount', countStatuses(statusCounts,
      'queued', 'searching', 'processing', 'unmatched', 'pending_match', 'discovered',
      'queried', 'matched'));
    setCount('queueActiveCount', countStatuses(statusCounts, 'downloading'));
    setCount('queueCompletedCount', countStatuses(statusCounts, 'completed'));
    setCount('queueMovingCount', countStatuses(statusCounts, 'moving', 'importing'));
    setCount('queueFailedCount', countStatuses(statusCounts, 'failed'));

    setCount('statQueuedNum', countStatuses(statusCounts,
      'queued', 'searching', 'unmatched', 'pending_match', 'discovered', 'queried', 'matched'));
    setCount('statDownloadingNum', countStatuses(statusCounts, 'downloading'));
    setCount('statCompletedNum', countStatuses(statusCounts, 'completed'));
    setCount('statFailedNum', countStatuses(statusCounts, 'failed'));
    setCount('statImportedNum', countStatuses(statusCounts, 'imported', 'moving'));

    // Hide zero-value stat pills.
    document.querySelectorAll('.stat-pill[data-pill-for]').forEach((pill) => {
      const el = document.getElementById(pill.dataset.pillFor);
      const count = Number(el ? el.textContent : 0) || 0;
      pill.classList.toggle('d-none', count === 0);
    });

    const lastRefreshed = document.getElementById('queueLastRefreshed');
    if (lastRefreshed) {
      lastRefreshed.textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour12: false });
    }

    renderQueueSection(items);
    renderQueueLists(items, completed);

    const retryAllBtn = document.getElementById('retryAllBtn');
    if (retryAllBtn) {
      retryAllBtn.style.display = Number(statusCounts.failed || 0) > 0 ? 'inline-block' : 'none';
    }

    updatePageControls(data.total != null ? data.total : items.length, items.length);

    await renderQueueLog();
    await renderSearchLog();
  }

  function renderQueueSection(items) {
    const section = document.getElementById('folderGroupsSection');
    const list = document.getElementById('folderGroupsList');
    const badge = document.getElementById('folderGroupsBadge');
    if (!section || !list) return;

    section.style.display = 'block';

    if (!items.length) {
      if (badge) badge.textContent = '0 items';
      list.innerHTML =
        '<div class="alert alert-info m-3"><i class="bi bi-info-circle"></i> No items in queue right now.</div>';
      return;
    }

    if (badge) badge.textContent = items.length + ' items';
    list.innerHTML = '<h6 class="px-3 pt-3 mb-0 small text-muted text-uppercase">Queue Items</h6>';

    const holder = document.createElement('div');
    renderGroupedList(holder, 'active', items);
    list.appendChild(holder.firstElementChild);
    attachRowHandlers(list);
  }

  function renderQueueLists(items, completed) {
    const lists = [
      ['active', items.filter((i) => i.status !== 'failed' && i.status !== 'completed')],
      ['completed', completed.filter((i) => (i.status || 'completed') !== 'failed')],
      ['failed', items.filter((i) => i.status === 'failed')],
    ];

    lists.forEach(([kind, kindItems]) => {
      const listEl = document.getElementById(kind + 'QueueList');
      const emptyEl = document.getElementById(kind + 'QueueEmpty');
      const badgeEl = document.getElementById(
        kind === 'active' ? 'queueActiveCount' : kind + 'Badge'
      );
      if (!listEl || !emptyEl) return;

      if (!kindItems.length) {
        listEl.style.display = 'none';
        emptyEl.style.display = 'block';
        if (badgeEl && kind !== 'active') badgeEl.style.display = 'none';
        return;
      }

      emptyEl.style.display = 'none';
      listEl.style.display = 'block';
      if (badgeEl && kind !== 'active') {
        badgeEl.textContent = `${kindItems.length} item${kindItems.length === 1 ? '' : 's'}`;
        badgeEl.style.display = 'inline-block';
      }

      renderGroupedList(listEl, kind, kindItems);
    });
  }

  // ── Activity logs ───────────────────────────────────────────────────────

  async function renderQueueLog() {
    const logEl = document.getElementById('queueActivityLog');
    if (!logEl) return;

    try {
      const data = await global.api.getJson('/api/queue/events?limit=100');
      const events = (data && data.events) || [];
      const lines = events.slice().reverse().map((event) => {
        const ts = event.created_at || event.timestamp || null;
        const time = ts ? new Date(ts).toLocaleTimeString([], { hour12: false }) : '--:--:--';
        return `[${time}] ${(event.event_type || 'info').toUpperCase()} ${event.message || ''}`;
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
      const data = await global.api.getJson('/api/queue/search-events?limit=100');
      const events = (data && data.events) || [];
      const chunks = [];

      events.slice().reverse().forEach((event) => {
        const ts = event.timestamp ? new Date(event.timestamp) : null;
        const time = ts ? ts.toLocaleTimeString([], { hour12: false }) : '--:--:--';
        const type = (event.search_type || 'unknown').toUpperCase();
        chunks.push(
          `[${type}] [${time}] Query: "${event.query || ''}"  |  ` +
          `${event.artist || ''} - ${event.title || ''}`
        );
        chunks.push(
          `    Results: ${event.result_count ?? 0}  ` +
          `Duration: ${event.duration_seconds != null ? event.duration_seconds + 's' : 'n/a'}`
        );
      });

      logEl.textContent = chunks.length ? chunks.join('\n') : 'No Soulseek search events yet.';
      logEl.scrollTop = logEl.scrollHeight;
    } catch (error) {
      console.error('Error loading search log:', error);
    }
  }

  async function loadQueueEvents() {
    const emptyDiv = document.getElementById('queueEventsEmpty');
    const table = document.getElementById('queueEventsTable');
    const tbody = document.getElementById('queueEventsBody');
    if (!emptyDiv || !table || !tbody) return;

    try {
      const data = await global.api.getJson('/api/queue/events?limit=50');
      const events = data.events || [];

      if (!events.length) {
        emptyDiv.style.display = 'block';
        table.style.display = 'none';
        return;
      }

      emptyDiv.style.display = 'none';
      table.style.display = 'table';
      tbody.innerHTML = events.map((event) => {
        const ts = new Date(event.created_at);
        const badgeClass = event.event_type === 'file_found' ? 'bg-info'
          : event.event_type === 'status_change' ? 'bg-primary'
          : event.event_type === 'error' ? 'bg-danger' : 'bg-success';
        return `<tr>
          <td class="small text-muted">${esc(ts.toLocaleString())}</td>
          <td><span class="badge ${badgeClass}">${esc((event.event_type || '').replace(/_/g, ' ').toUpperCase())}</span></td>
          <td>${esc(event.message || '')}</td>
        </tr>`;
      }).join('');
    } catch (error) {
      console.error('Error loading queue events:', error);
    }
  }

  function clearQueueEventsLog() {
    const tbody = document.getElementById('queueEventsBody');
    const emptyDiv = document.getElementById('queueEventsEmpty');
    const table = document.getElementById('queueEventsTable');
    if (tbody) tbody.innerHTML = '';
    if (table) table.style.display = 'none';
    if (emptyDiv) emptyDiv.style.display = 'block';
  }

  // ── Single-item actions ─────────────────────────────────────────────────

  function manualQueueSearch(query, queueId) {
    if (!query) return;
    if (typeof global.openSoulseekManualSearchModal === 'function') {
      global.openSoulseekManualSearchModal(query, queueId);
      return;
    }
    if (global.slskd) {
      global.slskd.search(query, { context: 'queue' });
    }
  }

  async function addToQueue(event) {
    event.preventDefault();

    const artist = document.getElementById('queueArtist').value.trim();
    const title = document.getElementById('queueTitle').value.trim();
    const album = document.getElementById('queueAlbum').value.trim();
    const source = document.getElementById('queueSource')?.value || 'soulseek';
    const priority = parseInt(document.getElementById('queuePriority')?.value || '5', 10);

    if (!artist) {
      notifyError('Please enter an artist.');
      return;
    }
    if (!title && !album) {
      notifyError('Please enter either a song title or an album name.');
      return;
    }

    if (!title && album) {
      const accepted = await confirmFn({
        title: 'No track title',
        message: 'Search MusicBrainz releases for this artist/album instead?',
        tone: 'primary',
        confirmLabel: 'Search',
      });
      if (accepted) searchMusicBrainzForQueue();
      return;
    }

    try {
      await global.api.postJson('/api/queue/add', { artist, title, album, source, priority });
      notifySuccess(`Added to queue: ${artist} - ${title}`);
      const form = document.getElementById('addToQueueForm');
      if (form) form.reset();
      queuePageOffset = 0;
      await loadQueueStatus();
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  function searchMusicBrainzForQueue() {
    const artist = document.getElementById('queueArtist')?.value.trim() || '';
    const album = document.getElementById('queueAlbum')?.value.trim() || '';
    const track = document.getElementById('queueTitle')?.value.trim() || '';

    if (!artist && !album && !track) {
      notifyError('Please enter at least one field before searching MusicBrainz.');
      return;
    }
    if (typeof global.openGlobalMbSearch !== 'function') {
      notifyError('MusicBrainz search is unavailable on this page.');
      return;
    }

    global.openGlobalMbSearch(artist, album, (selected) => {
      downloadMbRelease(selected.id, selected.title, selected.artist, 'slskd');
    }, track);
  }

  async function deleteQueueItem(queueId, deleteFile, btn) {
    const accepted = await confirmFn({
      title: 'Remove from queue',
      message: deleteFile
        ? 'Delete this item from the queue AND remove its file from /downloads?'
        : 'Remove this item from the queue?',
      tone: 'danger',
      confirmLabel: 'Remove',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        const query = deleteFile ? '?delete_download_file=1' : '';
        await global.api.deleteJson(`/api/queue/${queueId}/delete${query}`);
        notifySuccess('Removed from queue');
        await loadQueueStatus();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };

    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  function retryQueueItem(queueId, btn) {
    const run = async () => {
      try {
        await global.api.postJson(`/api/queue/${queueId}/requeue`, {});
        notifySuccess('Retrying download…');
        await loadQueueStatus();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  async function cancelQueueItem(queueId, btn) {
    const accepted = await confirmFn({
      title: 'Cancel download',
      message: 'Cancel this download?',
      detail: 'The queue item will be marked failed and can be retried.',
      tone: 'danger',
      confirmLabel: 'Cancel download',
      cancelLabel: 'Keep',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        await global.api.postJson(`/api/queue/${queueId}/cancel`, {});
        notifySuccess('Download cancelled');
        await loadQueueStatus();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  async function organizeFile(queueId, btn) {
    const accepted = await confirmFn({
      title: 'Organize file',
      message: 'Copy this file to the music library?',
      tone: 'primary',
      confirmLabel: 'Copy',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        await global.api.postJson(`/api/queue/${queueId}/organize`, {}, { timeoutMs: 120000 });
        notifySuccess('File organized');
        await loadQueueStatus();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  // ── Group actions ───────────────────────────────────────────────────────

  async function organizeGroup(kind, key, btn) {
    const group = lookupGroup(kind, key);
    if (!group || !group.items.length) return;

    const copyable = group.items.filter((i) => i.status === 'completed' || i.status === 'moving');
    if (!copyable.length) {
      notifyError('No completed tracks in this album to copy.');
      return;
    }

    const accepted = await confirmFn({
      title: 'Organize album',
      message: `Copy ${copyable.length} completed track${copyable.length === 1 ? '' : 's'} ` +
        `in "${group.label || ''}" to the music library?`,
      tone: 'primary',
      confirmLabel: 'Copy',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        const withGroup = group.items.find((i) => i.import_group);
        if (withGroup) {
          await global.api.postJson('/api/queue/organize-group',
            { group_id: withGroup.import_group }, { timeoutMs: ORGANIZE_TIMEOUT_MS });
        } else {
          for (const item of copyable) {
            await global.api.postJson(`/api/queue/${item.id}/organize`, {}, { timeoutMs: 120000 });
          }
        }
        notifySuccess('Album organized');
        await loadQueueStatus();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  async function retryGroup(kind, key, btn) {
    const group = lookupGroup(kind, key);
    if (!group || !group.items.length) return;

    const failed = group.items.filter((i) => i.status === 'failed');
    if (!failed.length) {
      notifyError('No failed tracks in this album to retry.');
      return;
    }

    const accepted = await confirmFn({
      title: 'Retry album',
      message: `Re-queue ${failed.length} failed track${failed.length === 1 ? '' : 's'} in "${group.label || ''}"?`,
      tone: 'primary',
      confirmLabel: 'Retry',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        for (const item of failed) {
          await global.api.postJson(`/api/queue/${item.id}/requeue`, {});
        }
        notifySuccess(`Retrying ${failed.length} track(s)…`);
        await loadQueueStatus();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  async function cancelGroup(kind, key, btn) {
    const group = lookupGroup(kind, key);
    if (!group || !group.items.length) return;

    const active = group.items.filter((i) =>
      ['downloading', 'searching', 'processing'].includes(i.status));
    if (!active.length) {
      notifyError('No active downloads in this album.');
      return;
    }

    const accepted = await confirmFn({
      title: 'Cancel downloads',
      message: `Cancel ${active.length} active download${active.length === 1 ? '' : 's'} in "${group.label || ''}"?`,
      tone: 'danger',
      confirmLabel: 'Cancel downloads',
      cancelLabel: 'Keep',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        for (const item of active) {
          await global.api.postJson(`/api/queue/${item.id}/cancel`, {});
        }
        notifySuccess('Downloads cancelled');
        await loadQueueStatus();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  async function deleteGroup(kind, key, btn) {
    const group = lookupGroup(kind, key);
    if (!group || !group.items.length) return;

    const accepted = await confirmFn({
      title: 'Remove album from queue',
      message: `Remove all ${group.items.length} track(s) in "${group.label || ''}" from the queue?`,
      items: group.items.map((i) => i.title || i.filename || String(i.id)),
      tone: 'danger',
      confirmLabel: 'Remove',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        for (const item of group.items) {
          await global.api.deleteJson(`/api/queue/${item.id}/delete`);
        }
        notifySuccess('Album removed from queue');
        await loadQueueStatus();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  // ── Bulk queue actions ──────────────────────────────────────────────────

  /**
   * Clear the queue.
   *
   * RENAMED from `clearQueue`, which collided with player.js's global of the
   * same name for the PLAYBACK queue. Two unrelated destructive actions
   * shared one global; whichever script parsed last owned it. Rename
   * player.js's to clearPlaybackQueue (already done in the rebuilt file).
   */
  async function clearDownloadQueue() {
    const accepted = await confirmFn({
      title: 'Clear queue',
      message: 'Clear the entire download queue?',
      detail: 'Removes queued, failed and completed items but keeps imported records.',
      tone: 'danger',
      confirmLabel: 'Clear',
    });
    if (!accepted) return;

    try {
      const data = await global.api.deleteJson('/api/queue/clear');
      notifySuccess(`Cleared ${data.deleted || 0} item(s) from queue`);
      await loadQueueStatus();
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  async function purgeAllQueueAndDownloads() {
    // Typed confirmation: this permanently deletes every file in the
    // downloads folder. A one-click confirm is not proportionate.
    const accepted = (global.ui && global.ui.confirmTyped)
      ? await global.ui.confirmTyped({
          title: 'Purge everything',
          message: 'This deletes ALL queue rows and permanently deletes every file and folder ' +
            'in your configured downloads folder.',
          phrase: 'PURGE',
          confirmLabel: 'Purge all',
        })
      : global.confirm('PURGE ALL? This cannot be undone.');
    if (!accepted) return;

    try {
      const data = await global.api.deleteJson('/api/queue/purge-all');
      notifySuccess(
        `Purge complete — ${data.queue_items_deleted || 0} queue item(s) removed, ` +
        `${data.deleted_files || 0} file(s) deleted from ${data.downloads_dir || 'the downloads folder'}`
      );
      queuePageOffset = 0;
      await loadQueueStatus();
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  async function retryAllFailed() {
    const accepted = await confirmFn({
      title: 'Retry all failed',
      message: 'Re-queue all failed downloads?',
      tone: 'primary',
      confirmLabel: 'Retry all',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson('/api/queue/retry-all-failed', {});
      notifySuccess(`Re-queued ${data.retried || 0} failed item(s)`);
      await loadQueueStatus();
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  async function cleanupCopiedSources() {
    const accepted = await confirmFn({
      title: 'Clean up copied sources',
      message: 'Delete copied source files from /downloads?',
      detail: 'Queue history is kept.',
      tone: 'danger',
      confirmLabel: 'Delete',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson('/api/queue/cleanup-copied', {});
      notifySuccess(`${data.message} (scanned ${data.scanned})`);
      await loadQueueStatus();
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  async function runQueueCleanup() {
    const accepted = await confirmFn({
      title: 'Run cleanup',
      message: 'Run queue cleanup now?',
      tone: 'primary',
      confirmLabel: 'Run',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson('/api/queue/cleanup', {});
      const stats = data.stats || {};
      notifySuccess(
        `Cleanup complete — ${stats.deleted_duplicates || 0} duplicate(s) removed, ` +
        `${stats.completed_albums || 0} completed album(s)`
      );
      await loadQueueStatus();
    } catch (error) {
      notifyError('Error: ' + error.message);
    }
  }

  function restartQueueProcessor() {
    const btn = document.getElementById('restartProcessorBtn');
    const run = async () => {
      try {
        await global.api.postJson('/api/queue-processor/restart', {});
        if (btn && global.buttonState) global.buttonState.flashDone(btn, { label: 'Restarted' });
        setTimeout(loadQueueStatus, 1500);
      } catch (error) {
        notifyError('Could not restart the queue processor: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, 'Restarting…', run) : run();
  }

  // ── Organize Group modal ────────────────────────────────────────────────

  let organizeGroupKey = null;

  function organizeFieldValue(id, fallback) {
    const el = document.getElementById(id);
    const value = el ? el.value.trim() : '';
    return value || fallback || '';
  }

  function updateFolderPreview() {
    const artist = organizeFieldValue('orgArtist', 'Artist Name');
    const albumArtist = organizeFieldValue('orgAlbumArtist', artist);
    const album = organizeFieldValue('orgAlbum', 'Album Name');
    const year = organizeFieldValue('orgYear', String(new Date().getFullYear()));

    let format = document.getElementById('orgFolderFormat')?.value || '{album_artist}/{year} - {album}';
    if (format === 'custom') {
      format = organizeFieldValue('orgCustomFormat', '{album_artist}/{year} - {album}');
    }

    const path = format
      .replace(/{album_artist}/g, albumArtist)
      .replace(/{artist}/g, artist)
      .replace(/{album}/g, album)
      .replace(/{year}/g, year);

    const previewEl = document.getElementById('folderPreview');
    if (previewEl) previewEl.textContent = `Music / ${path} / 01. Track Title.mp3`;
  }

  function openOrganizeGroupModal(group) {
    organizeGroupKey = group.key;

    const infoText = document.getElementById('groupInfoText');
    const itemCount = document.getElementById('groupItemCount');
    if (infoText) infoText.textContent = group.label || 'Album Group';
    if (itemCount) itemCount.textContent = String(group.items.length);

    const orgAlbum = document.getElementById('orgAlbum');
    if (orgAlbum && group.label) orgAlbum.value = group.label;

    // The group's artist was never prefilled, so the Artist field opened
    // blank and confirmOrganizeGroup() rejected the submit with "Artist and
    // Album fields are required" until the user retyped a value the app
    // already had.
    const orgArtist = document.getElementById('orgArtist');
    const orgAlbumArtist = document.getElementById('orgAlbumArtist');
    if (orgArtist && group.sublabel) orgArtist.value = group.sublabel;
    if (orgAlbumArtist && group.sublabel) orgAlbumArtist.value = group.sublabel;

    const firstYear = (group.items || []).map((i) => i.year).find(Boolean);
    const orgYear = document.getElementById('orgYear');
    if (orgYear && firstYear) orgYear.value = String(firstYear).substring(0, 4);

    updateFolderPreview();

    const modalEl = document.getElementById('organizeGroupModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  /**
   * Toggle the organize-lookup spinner.
   *
   * The template used to carry `style="display:none !important"` on this
   * element; an inline !important cannot be overridden by
   * `el.style.display = 'flex'`, so the spinner never appeared. It is now a
   * d-none class toggle — make sure the template no longer sets that inline
   * style.
   */
  function setOrganizeLookupLoading(el, visible) {
    if (!el) return;
    el.classList.toggle('d-none', !visible);
    el.classList.toggle('d-flex', visible);
  }

  function renderOrganizeResults(results, mapRow) {
    const resultsDiv = document.getElementById('orgMbSearchResults');
    if (!resultsDiv) return;

    if (!results.length) {
      resultsDiv.innerHTML =
        '<div class="alert alert-warning py-1 extra-small">No matches found.</div>';
      return;
    }

    resultsDiv.innerHTML =
      '<div class="list-group list-group-flush" style="max-height:200px;overflow-y:auto;">' +
      results.slice(0, 10).map((r, index) => {
        const row = mapRow(r);
        return `<button type="button" data-index="${index}"
                  class="list-group-item list-group-item-action bg-dark text-light border-secondary py-1 px-2 extra-small org-match-btn">
                  <strong>${esc(row.title)}</strong> — <span class="text-muted">${esc(row.subtitle)}</span>
                </button>`;
      }).join('') + '</div>';

    resultsDiv.querySelectorAll('.org-match-btn').forEach((btn) => {
      btn.addEventListener('click', function () {
        const row = mapRow(results[parseInt(this.dataset.index, 10)]);
        applyOrganizeMatch(row.artist, row.album, row.year);
      });
    });
  }

  async function searchMBForOrganize() {
    const artist = organizeFieldValue('orgArtist');
    const album = organizeFieldValue('orgAlbum');
    const resultsDiv = document.getElementById('orgMbSearchResults');
    const loadingDiv = document.getElementById('orgMbSearchLoading');

    if (!artist && !album) {
      notifyError('Please enter at least an artist or album name.');
      return;
    }

    if (resultsDiv) resultsDiv.innerHTML = '';
    setOrganizeLookupLoading(loadingDiv, true);

    try {
      // `org`-prefixed ids deliberately namespace this modal's containers so
      // they cannot collide with the global MusicBrainz modal that base.html
      // renders on EVERY page. This used to target #mbSearchResults /
      // #mbSearchLoading — the global modal's ids — so getElementById
      // resolved to base.html's hidden copy and results were injected there
      // while this modal showed nothing.
      const data = await global.api.postJson('/api/musicbrainz/search', { artist, album });
      setOrganizeLookupLoading(loadingDiv, false);

      renderOrganizeResults(data.releases || [], (r) => ({
        title: r.title,
        subtitle: `${r.artist || artist} (${(r.first_release_date || '').substring(0, 4) || 'TBA'})`,
        artist: r.artist || artist,
        album: r.title,
        year: (r.first_release_date || '').substring(0, 4),
      }));
    } catch (error) {
      setOrganizeLookupLoading(loadingDiv, false);
      if (resultsDiv) {
        resultsDiv.innerHTML =
          `<div class="alert alert-danger py-1 extra-small">Error: ${esc(error.message)}</div>`;
      }
    }
  }

  async function searchDiscogsForOrganize() {
    const artist = organizeFieldValue('orgArtist');
    const album = organizeFieldValue('orgAlbum');
    const resultsDiv = document.getElementById('orgMbSearchResults');
    const loadingDiv = document.getElementById('orgMbSearchLoading');

    if (!artist && !album) {
      notifyError('Please enter at least an artist or album name.');
      return;
    }

    if (resultsDiv) resultsDiv.innerHTML = '';
    setOrganizeLookupLoading(loadingDiv, true);

    try {
      const data = await global.api.postJson('/api/album/discogs', { artist, album });
      setOrganizeLookupLoading(loadingDiv, false);

      renderOrganizeResults(data.results || [], (r) => ({
        title: r.title,
        subtitle: String(r.year || 'TBA'),
        artist: artist,
        album: r.title,
        year: String(r.year || ''),
      }));
    } catch (error) {
      setOrganizeLookupLoading(loadingDiv, false);
      if (resultsDiv) {
        resultsDiv.innerHTML =
          `<div class="alert alert-danger py-1 extra-small">Error: ${esc(error.message)}</div>`;
      }
    }
  }

  function applyOrganizeMatch(artist, album, year) {
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

    const metadataTab = document.getElementById('metadataTab');
    if (metadataTab && global.bootstrap) {
      global.bootstrap.Tab.getOrCreateInstance(metadataTab).show();
    }
  }

  function clearOrganizeMatch() {
    const matchAlert = document.getElementById('mbSelectedMatch');
    if (matchAlert) matchAlert.classList.add('d-none');
  }

  async function confirmOrganizeGroup() {
    if (!organizeGroupKey) {
      notifyError('No active album group selected.');
      return;
    }

    const artist = organizeFieldValue('orgArtist');
    const album = organizeFieldValue('orgAlbum');
    if (!artist || !album) {
      notifyError('Artist and Album fields are required.');
      return;
    }

    const payload = {
      group_id: organizeGroupKey,
      artist,
      album,
      album_artist: organizeFieldValue('orgAlbumArtist') || null,
      year: organizeFieldValue('orgYear') || null,
      directory_format: document.getElementById('orgFolderFormat')?.value || null,
      custom_format: organizeFieldValue('orgCustomFormat') || null,
    };

    const modalEl = document.getElementById('organizeGroupModal');
    if (modalEl && global.modal) global.modal.hide(modalEl);

    try {
      const data = await global.api.postJson('/api/queue/organize-group', payload,
        { timeoutMs: 120000 });
      if (!data.success) {
        notifyError(data.error || 'Failed to organize group.');
        return;
      }
      notifySuccess(
        `Organized and moved ${data.moved_count || 'the'} track(s) into the library.`
      );
      await loadQueueStatus();
    } catch (error) {
      notifyError('Error during organization: ' + error.message);
    }
  }

  // Live folder preview. `orgFolderFormat` is a <select>, which fires
  // `change`, not `input` — without the second listener the preview did not
  // update when the directory format changed.
  const ORGANIZE_FIELD_IDS = [
    'orgArtist', 'orgAlbumArtist', 'orgAlbum', 'orgYear', 'orgCustomFormat', 'orgFolderFormat',
  ];

  function handleOrganizeFieldChange(event) {
    if (!ORGANIZE_FIELD_IDS.includes(event.target.id)) return;
    if (event.target.id === 'orgFolderFormat') {
      const customDiv = document.getElementById('customFormatDiv');
      if (customDiv) customDiv.style.display = event.target.value === 'custom' ? 'block' : 'none';
    }
    updateFolderPreview();
  }

  document.addEventListener('input', handleOrganizeFieldChange);
  document.addEventListener('change', handleOrganizeFieldChange);

  // ── Managed MusicBrainz downloads ───────────────────────────────────────

  async function downloadMbRelease(releaseId, releaseTitle, artist, method) {
    const persistentEl = document.getElementById('persistentSearchCheck');
    const persistentSearch = persistentEl ? persistentEl.checked : false;
    const sessionSelector = document.getElementById('mbSessionSelector');
    const selectedSession = sessionSelector ? sessionSelector.value : '';

    if (selectedSession === 'create') {
      const sessionName = global.prompt('Enter name for new playlist session:');
      if (!sessionName) return;
      try {
        const data = await global.api.postJson('/api/playlist-downloads/create', {
          session_name: sessionName, total_tracks: null, priority_queue: false,
        });
        return addMbDownloadToSession(releaseId, releaseTitle, artist, method,
          persistentSearch, data.session_id);
      } catch (error) {
        notifyError('Error creating session: ' + error.message);
        return;
      }
    }

    const accepted = await confirmFn({
      title: 'Download release',
      message: `Download "${releaseTitle}" by ${artist} via ${method}?`,
      detail: persistentSearch ? 'Persistent search is enabled — it will auto-retry if it fails.' : undefined,
      tone: 'primary',
      confirmLabel: 'Download',
    });
    if (!accepted) return;

    return addMbDownloadToSession(releaseId, releaseTitle, artist, method,
      persistentSearch, selectedSession || null);
  }

  async function addMbDownloadToSession(releaseId, releaseTitle, artist, method,
                                        persistentSearch, sessionId) {
    try {
      const data = await global.api.postJson('/api/musicbrainz/download', {
        release_id: releaseId,
        release_title: releaseTitle,
        artist,
        method,
        persistent_search: persistentSearch,
        max_retries: MB_DEFAULT_MAX_RETRIES,
        session_id: sessionId,
        queue_items_only: true,
      });

      let message = `Download queued: ${releaseTitle}`;
      if (data.tracking_id) message += ` (tracking ${data.tracking_id})`;
      if (data.persistent_search) message += ' — will retry automatically on failure';
      notifySuccess(message);

      setTimeout(refreshMbDownloads, 1000);
      setTimeout(loadQueueStatus, 1500);
    } catch (error) {
      notifyError('Error initiating download: ' + error.message);
    }
  }

  async function loadMbSessionSelector() {
    const selector = document.getElementById('mbSessionSelector');
    if (!selector) return;

    try {
      const data = await global.api.getJson('/api/playlist-downloads');
      if (!data.sessions) return;

      const createOption = selector.querySelector('option[value="create"]');
      Array.from(selector.options).forEach((opt, i) => { if (i > 1) opt.remove(); });

      data.sessions
        .filter((s) => s.status !== 'completed' && s.status !== 'cancelled')
        .forEach((session) => {
          const option = document.createElement('option');
          option.value = session.id;
          option.textContent = `${session.session_name} (${session.completed_tracks}/${session.total_tracks})`;
          selector.insertBefore(option, createOption);
        });
    } catch (error) {
      console.error('Error loading sessions:', error);
    }
  }

  async function refreshMbDownloads() {
    const loadingEl = document.getElementById('mbDownloadsLoading');
    if (!loadingEl) return;

    const errorEl = document.getElementById('mbDownloadsError');
    const resultsEl = document.getElementById('mbDownloadsResults');
    const emptyEl = document.getElementById('mbDownloadsEmpty');
    const tableEl = document.getElementById('mbDownloadsTable');
    const tableBody = document.getElementById('mbDownloadsTableBody');
    const countBadge = document.getElementById('mbDownloadCount');

    loadingEl.style.display = 'block';
    if (errorEl) errorEl.style.display = 'none';
    if (resultsEl) resultsEl.style.display = 'none';

    try {
      const data = await global.api.getJson('/api/musicbrainz/downloads');
      loadingEl.style.display = 'none';
      if (resultsEl) resultsEl.style.display = 'block';

      const downloads = data.downloads || [];
      if (!downloads.length) {
        if (emptyEl) emptyEl.style.display = 'block';
        if (tableEl) tableEl.style.display = 'none';
        if (countBadge) countBadge.style.display = 'none';
        return;
      }

      if (emptyEl) emptyEl.style.display = 'none';
      if (tableEl) tableEl.style.display = 'block';
      if (countBadge) {
        countBadge.textContent = downloads.length;
        countBadge.style.display = 'inline-block';
      }
      if (!tableBody) return;

      tableBody.innerHTML = downloads.map((dl) => {
        const status = (dl.status || '').toLowerCase();
        const canRetry = ['failed', 'error', 'timeout'].includes(status);
        const canRemove = ['completed', 'failed', 'error', 'cancelled'].includes(status);
        const awaiting = status === 'awaiting_selection' && dl.method === 'slskd';

        const persistent = dl.persistent_search
          ? ' <span class="badge bg-secondary ms-1" title="Auto-retry enabled"><i class="bi bi-arrow-repeat"></i> Auto-retry</span>'
          : '';
        const retryInfo = (dl.persistent_search && dl.retry_count)
          ? `<div><small class="text-muted">(Retry ${dl.retry_count}/${dl.max_retries || MB_DEFAULT_MAX_RETRIES})</small></div>`
          : '';

        const id = esc(String(dl.id));
        return `<tr data-dl-id="${id}">
          <td>
            <div><strong>${esc(dl.release_title)}</strong>${persistent}</div>
            ${retryInfo}
            ${dl.total_tracks ? `<small class="text-muted">Tracks: ${esc(String(dl.completed_tracks))}/${esc(String(dl.total_tracks))} completed</small>` : ''}
          </td>
          <td>${esc(dl.artist)}</td>
          <td class="text-center"><span class="badge bg-success">Soulseek</span></td>
          <td class="text-center">${global.statusBadge ? global.statusBadge.render(dl.status) : esc(dl.status)}</td>
          <td class="text-center text-muted small">${esc(new Date(dl.created_at).toLocaleString())}</td>
          <td class="text-center">
            <div class="btn-group btn-group-sm">
              ${awaiting ? `<button class="btn btn-primary mb-dl-select" data-id="${id}"><i class="bi bi-hand-index"></i> Select</button>` : ''}
              ${canRetry ? `<button class="btn btn-outline-warning mb-dl-retry" data-id="${id}"><i class="bi bi-arrow-clockwise"></i></button>` : ''}
              ${canRemove ? `<button class="btn btn-outline-danger mb-dl-remove" data-id="${id}"><i class="bi bi-trash"></i></button>` : ''}
            </div>
          </td>
        </tr>`;
      }).join('');

      // data-* + listeners rather than inline onclick with escapeJsString'd
      // ids, consistent with the rest of this file.
      tableBody.querySelectorAll('.mb-dl-retry').forEach((btn) => {
        btn.addEventListener('click', function () { retryMbDownload(this.dataset.id, this); });
      });
      tableBody.querySelectorAll('.mb-dl-remove').forEach((btn) => {
        btn.addEventListener('click', function () { removeMbDownload(this.dataset.id, this); });
      });
      tableBody.querySelectorAll('.mb-dl-select').forEach((btn) => {
        btn.addEventListener('click', function () {
          if (typeof global.showSlskdResults === 'function') global.showSlskdResults(this.dataset.id);
        });
      });
    } catch (error) {
      loadingEl.style.display = 'none';
      if (errorEl) {
        errorEl.textContent = 'Error loading downloads: ' + error.message;
        errorEl.style.display = 'block';
      }
    }
  }

  async function retryMbDownload(downloadId, btn) {
    const accepted = await confirmFn({
      title: 'Retry download',
      message: 'Retry this download?',
      tone: 'primary',
      confirmLabel: 'Retry',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        const data = await global.api.postJson(
          `/api/musicbrainz/download/${encodeURIComponent(downloadId)}/retry`, {}
        );
        if (!data.success) {
          notifyError(data.error || 'Retry failed');
          return;
        }
        notifySuccess('Download retry initiated');
        refreshMbDownloads();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  async function removeMbDownload(downloadId, btn) {
    const accepted = await confirmFn({
      title: 'Remove download',
      message: 'Remove this download from the list?',
      tone: 'danger',
      confirmLabel: 'Remove',
    });
    if (!accepted) return;

    const run = async () => {
      try {
        const data = await global.api.deleteJson(
          `/api/musicbrainz/download/${encodeURIComponent(downloadId)}`
        );
        if (!data.success) {
          notifyError(data.error || 'Removal failed');
          return;
        }
        refreshMbDownloads();
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    };
    return btn && global.buttonState ? global.buttonState.withBusy(btn, '', run) : run();
  }

  // ── Init ────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('mbSearchInput')) {
      loadMbSessionSelector();
      refreshMbDownloads();
    }

    const mbTab = document.getElementById('mbTab');
    if (mbTab) {
      mbTab.addEventListener('click', function () {
        setTimeout(loadMbSessionSelector, 200);
      });
    }

    if (document.getElementById('folderGroupsSection') || document.getElementById('statQueuedNum')) {
      // Managed poller: never overlaps requests, pauses in a background tab,
      // and stops on teardown. The old version hand-rolled the overlap guard
      // with a `_queuePollInFlight` flag and never cleared the interval.
      queuePoller = global.poller.create({
        interval: POLL_INTERVAL_MS,
        onTick: loadQueueStatus,
      });
      queuePoller.start();
    }

    if (document.getElementById('queueEventsBody')) {
      loadQueueEvents();
    }
  });

  global.downloads = {
    loadQueueStatus,
    changeQueuePage,
    addToQueue,
    clearDownloadQueue,
    purgeAllQueueAndDownloads,
    retryAllFailed,
    cleanupCopiedSources,
    runQueueCleanup,
    restartQueueProcessor,
    openOrganizeGroupModal,
    confirmOrganizeGroup,
    downloadMbRelease,
    refreshMbDownloads,
    buildQueueGroups,
  };

  // Legacy globals for inline handlers in the download templates.
  global.loadQueueStatus = loadQueueStatus;
  global.changeQueuePage = changeQueuePage;
  global.addToQueue = addToQueue;
  global.searchMusicBrainzForQueue = searchMusicBrainzForQueue;
  global.deleteQueueItem = deleteQueueItem;
  global.retryQueueItem = retryQueueItem;
  global.cancelQueueItem = cancelQueueItem;
  global.organizeFile = organizeFile;
  global.clearEntireQueue = clearDownloadQueue;
  global.clearDownloadQueue = clearDownloadQueue;
  global.purgeAllQueueAndDownloads = purgeAllQueueAndDownloads;
  global.retryAllFailed = retryAllFailed;
  global.cleanupCopiedSources = cleanupCopiedSources;
  global.runQueueCleanup = runQueueCleanup;
  global.restartQueueProcessor = restartQueueProcessor;
  global.loadQueueEvents = loadQueueEvents;
  global.clearQueueEventsLog = clearQueueEventsLog;
  global.updateFolderPreview = updateFolderPreview;
  global.searchMBForOrganize = searchMBForOrganize;
  global.searchDiscogsForOrganize = searchDiscogsForOrganize;
  global.clearMBSelection = clearOrganizeMatch;
  global.confirmOrganizeGroup = confirmOrganizeGroup;
  global.downloadMbRelease = downloadMbRelease;
  global.refreshMbDownloads = refreshMbDownloads;
  global.retryMbDownload = retryMbDownload;
  global.removeMbDownload = removeMbDownload;

  // NOTE: `window.clearQueue` is deliberately NOT defined — see the comment
  // on clearDownloadQueue. player.js owns the playback queue; neither should
  // claim the bare name.
  //
  // `organizeSelected` / `batchOrganizeSelected` are also gone: both were
  // stubs whose entire body was alert('… not yet implemented'). Remove their
  // buttons from the template, or implement them.
})(window);
