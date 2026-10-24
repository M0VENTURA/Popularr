/* ==========================================================================
   static/js/pages/dashboard.js
   Dashboard — scan controls, active/recent scans, upcoming releases table.
   ========================================================================== */

(function (global) {
  'use strict';

  const STATUS_POLL_MS = 5000;
  const UPCOMING_REFRESH_MS = 30 * 60 * 1000;

  let statusPoller = null;
  let upcomingPoller = null;
  let tableFilter = 'all';
  let lastRecentScansPayload = null;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function escapeAttr(value) {
    return String(value == null ? '' : value)
      .split('&').join('&amp;')
      .split('<').join('&lt;')
      .split('>').join('&gt;')
      .split('"').join('&quot;')
      .split("'").join('&#39;');
  }

  const DISMISSED_ALERTS_KEY = 'popularr.dismissedDashboardAlerts';

  function readDismissedAlerts() {
    try {
      const raw = (global.localStorage || {}).getItem(DISMISSED_ALERTS_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_error) {
      return {};
    }
  }

  function writeDismissedAlerts(map) {
    try {
      (global.localStorage || {}).setItem(DISMISSED_ALERTS_KEY, JSON.stringify(map));
    } catch (_error) {
      /* quota / private browsing */
    }
  }

  function abandonedFingerprint(scan) {
    const entries = Object.entries((scan && scan.abandoned_artists) || {});
    const parts = entries.map(([artistName, records]) => {
      const recs = Array.isArray(records) ? records : [];
      const reasons = recs
        .map((r) => `${(r && r.reason) || 'unknown'}|${r && r.abandoned ? 'abandoned' : 'failed'}`)
        .sort();
      return `${artistName}=>${reasons.join(',')}`;
    });
    return `${(scan && scan.scan_type) || 'scan'}::${parts.sort().join(';')}`;
  }

  function isAlertDismissed(id) {
    return Object.prototype.hasOwnProperty.call(readDismissedAlerts(), id);
  }

  function dismissDashboardAlert(id) {
    if (!id) return;
    const map = readDismissedAlerts();
    map[id] = Date.now();
    writeDismissedAlerts(map);
    document.querySelectorAll('[data-alert-id]').forEach((el) => {
      if (el.getAttribute('data-alert-id') === id) el.remove();
    });
  }

  document.addEventListener('click', (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== 'function') return;

    if (target.closest('.js-dismiss-alert')) {
      const alertEl = target.closest('[data-alert-id]');
      if (alertEl) dismissDashboardAlert(alertEl.getAttribute('data-alert-id'));
      return;
    }

    if (target.closest('.js-restart-queue-processor')) {
      restartQueueProcessorFromDashboard();
    }
  });

  function formatElapsed(seconds) {
    if (!seconds) return '';
    return ` — ${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  }

  async function stopAllScans() {
    const confirmFn = (global.ui && global.ui.confirm)
      ? global.ui.confirm
      : (opts) => Promise.resolve(global.confirm(opts.message));

    const accepted = await confirmFn({
      title: 'Stop all scans',
      message: 'Stop every running scan?',
      tone: 'danger',
      confirmLabel: 'Stop all',
    });
    if (!accepted) return;

    try {
      await global.api.postJson('/scan/stop-all', {});
      global.toast.success('Stop signal sent to all scans');
    } catch (error) {
      global.toast.error('Error: ' + error.message);
    }
  }

  const SCAN_TYPE_DISPLAY_NAMES = global.ActivityFeed.SCAN_TYPE_DISPLAY_NAMES;

  function scanDisplayName(type) {
    return global.ActivityFeed.scanDisplayName(type);
  }

  function formatScanPercent(pct) {
    const value = Number(pct) || 0;
    if (value > 0 && value < 1) return '<1%';
    return `${Math.round(value)}%`;
  }

  function updateScanStatusBar(active) {
    const line = document.getElementById('scanStatusLine');
    const icon = document.getElementById('scanStatusIcon');
    if (!line || !icon) return;

    const barApi = global.popularr || {};
    const setActive = typeof barApi.setScanBarActive === 'function'
      ? barApi.setScanBarActive
      : () => {};

    if (typeof barApi.setHeaderScanDot === 'function') {
      barApi.setHeaderScanDot(!!(active && active.length));
    }

    if (!active || !active.length) {
      line.textContent = 'Idle — no scan running';
      icon.className = 'scan-status-idle';
      icon.innerHTML = '<i class="bi bi-circle"></i>';
      setActive(document.getElementById('scanStatusBar'), false, 0);
      return;
    }

    const scan = active[0];
    const pct = Math.min(scan.percent_complete ?? scan.progress ?? 0, 100);
    const stage = scan.current_stage ? ` · ${scan.current_stage}` : '';
    line.textContent = `${scanDisplayName(scan.scan_type)} — ${formatScanPercent(pct)}${stage}` +
      (scan.current_item ? ` · ${scan.current_item}` : '');
    icon.className = 'scan-status-active';
    icon.innerHTML = '<i class="bi bi-activity"></i>';
    setActive(document.getElementById('scanStatusBar'), true, pct);
  }

  function abandonedBanner(scan) {
    const entries = Object.entries(scan.abandoned_artists || {});
    if (!entries.length) return '';

    const alertId = abandonedFingerprint(scan);
    if (isAlertDismissed(alertId)) return '';

    const rows = entries.map(([artistName, records]) => {
      const recs = Array.isArray(records) ? records : [];
      const reasons = recs.map((r) => {
        const reason = r && r.reason ? String(r.reason) : 'unknown';
        const tag = r && r.abandoned
          ? '<span class="badge bg-warning text-dark ms-1">abandoned</span>'
          : '<span class="badge bg-danger ms-1">failed</span>';
        return `<div class="ms-2 small">${tag} <code>${esc(reason)}</code></div>`;
      }).join('');
      return `<div class="mb-1">
        <i class="bi bi-exclamation-triangle-fill text-warning me-1"></i>
        <strong>${esc(artistName)}</strong>${reasons}</div>`;
    }).join('');

    return `<div class="alert alert-warning py-2 px-3 mb-2" style="font-size:.85rem;"
        data-alert-id="${escapeAttr(alertId)}">
      <button type="button" class="btn-close js-dismiss-alert float-end ms-2"
              aria-label="Dismiss this alert" title="Dismiss"></button>
      <div class="fw-semibold mb-1">
        <i class="bi bi-hourglass-split me-1"></i>Artists skipped / abandoned — investigate</div>
      ${rows}
      <div class="text-muted small mt-1">These artists were abandoned after the
      per-artist time budget or raised an error; their tracks were not fully scanned.</div>
    </div>`;
  }

  async function updateActiveScans() {
    let data;
    try {
      data = await global.api.getJson(`/api/scan-progress?_ts=${Date.now()}`);
    } catch (_error) {
      return;
    }

    const active = data.active_scans || [];
    updateScanStatusBar(active);

    const running = active.find((s) => s && s.is_running) || active[0];
    const idleState = document.getElementById('scannerIdleState');
    const activeState = document.getElementById('scannerActiveState');
    const stateLabel = document.getElementById('scannerState');

    if (idleState) idleState.classList.toggle('d-none', !!running);
    if (activeState) activeState.classList.toggle('d-none', !running);
    if (stateLabel) {
      stateLabel.textContent = running ? `Scanning ${scanDisplayName(running.scan_type)}` : 'Idle';
    }

    const badge = document.getElementById('scannerStatusBadge');
    if (badge) {
      badge.innerHTML = '<i class="bi bi-circle-fill me-1 small"></i> ' +
        (running ? 'Active' : 'Idle');
      badge.className = 'badge ' + (running ? 'bg-success' : 'bg-secondary');
      badge.style.fontSize = '0.65rem';
    }

    const panel = document.getElementById('activeScansPanel');
    const body = document.getElementById('activeScansBody');
    if (!panel || !body) return;

    if (!active.length) {
      panel.style.display = 'none';
      return;
    }

    panel.style.display = '';
    body.innerHTML = active.map((scan) => {
      const pct = Math.min(scan.percent_complete ?? scan.progress ?? 0, 100);
      const stage = scan.current_stage
        ? `<span class="badge bg-info ms-2" style="font-size:0.7rem;">${esc(scan.current_stage)}</span>`
        : '';
      const message = scan.message
        ? `<span class="text-muted small ms-2">${esc(scan.message)}</span>`
        : '';
      const currentItem = scan.current_item
        ? `<div class="small text-muted mb-1 text-truncate" style="max-width:600px;">${esc(scan.current_item)}</div>`
        : '';

      const elapsed = formatElapsed(scan.elapsed_seconds);

      return `
        <div class="mb-2">
          ${abandonedBanner(scan)}
          <div class="d-flex justify-content-between align-items-center mb-1">
            <span>
              <i class="bi bi-activity me-1"></i>
              <strong>${esc(scanDisplayName(scan.scan_type))}</strong>${stage}${message}
              <span class="text-muted small">${esc(elapsed)}</span>
            </span>
            <span class="small text-muted">${esc(String(scan.processed_items ?? 0))}/${esc(String(scan.total_items ?? '?'))}</span>
          </div>
          ${currentItem}
          <div class="progress" style="height:8px;" role="progressbar"
               aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100"
               aria-label="${esc(scanDisplayName(scan.scan_type))} progress">
            <div class="progress-bar progress-bar-striped progress-bar-animated bg-success"
                 style="width:${pct}%;"></div>
          </div>
        </div>`;
    }).join('');
  }

  const parseScanTimestamp = (ts) => global.ActivityFeed.parseScanTimestamp(ts);
  const formatScanTimestamp = (ts) => global.ActivityFeed.formatScanTimestamp(ts);
  const groupRecentScans = (scans) => global.ActivityFeed.groupRecentScans(scans);

  function renderRecentScans(scans) {
    global.ActivityFeed.renderScansInto(
      document.getElementById('recent-scans-body'),
      scans,
      'No recent scan history.'
    );
  }

  function renderPendingApprovalInto(container, albums) {
    global.ActivityFeed.renderApprovalsInto(container, albums, 'Nothing needs review.');
  }

  async function playRecentAlbum(button) {
    const artist = button.dataset.artist || '';
    const album = button.dataset.album || '';
    if (!artist || !album) return;

    if (!global.Player || typeof global.Player.playQueue !== 'function') {
      global.toast.error('The player is unavailable on this page.');
      return;
    }

    const restore = global.buttonState
      ? global.buttonState.setBusy(button, '')
      : function () {};

    try {
      const params = new URLSearchParams({ artist, album });
      const data = await global.api.getJson(`/api/album/tracklist?${params}`);
      const tracks = (data && data.tracklist) || [];

      if (!tracks.length) {
        global.toast.warning(`No playable tracks on "${album}".`);
        return;
      }

      global.Player.playQueue(tracks.map((t) => ({
        id: t.track_id,
        title: t.title || '',
        artist: t.artist || artist,
        albumArtUrl: `/api/album/${encodeURIComponent(artist)}/${encodeURIComponent(album)}/art?s=160`,
      })));
    } catch (error) {
      global.toast.error(`Could not play "${album}": ${error.message}`);
    } finally {
      restore();
    }
  }

  function initRecentlyAddedActions() {
    const pane = document.getElementById('recentAddedPane');
    if (!pane) return;
    pane.addEventListener('click', (event) => {
      const button = event.target.closest('.js-play-album');
      if (!button || !pane.contains(button)) return;
      event.preventDefault();
      playRecentAlbum(button);
    });
  }

  let lastPendingApprovalPayload = null;

  function renderPendingApproval(albums) {
    const tabItem = document.getElementById('pendingApprovalTabItem');
    const tabBtn = document.getElementById('pendingApprovalTab');
    const body = document.getElementById('pending-approval-body');
    const countEl = document.getElementById('pending-approval-count');
    if (!body) return;

    const n = (albums || []).length;
    if (countEl) countEl.textContent = String(n);

    if (!n) {
      if (tabItem) tabItem.classList.add('d-none');
      body.innerHTML = '';
      if (tabBtn && tabBtn.classList.contains('active')) {
        const first = document.querySelector('#recentHistoryTabs .nav-link');
        const Tab = global.bootstrap && global.bootstrap.Tab;
        if (first && Tab) Tab.getOrCreateInstance(first).show();
      }
      return;
    }

    if (tabItem) tabItem.classList.remove('d-none');
    renderPendingApprovalInto(body, albums);
  }

  async function updatePendingApproval() {
    try {
      const data = await global.api.getJson('/api/metadata/pending-recommendations?limit=50');
      const payload = JSON.stringify(data.albums || []);
      if (payload === lastPendingApprovalPayload) return;
      lastPendingApprovalPayload = payload;
      renderPendingApproval(data.albums || []);
    } catch (_error) {
      /* transient */
    }
  }

  async function updateRecentScans() {
    try {
      const data = await global.api.getJson('/api/recent-scans?limit=25');
      const payload = JSON.stringify(data.scans || []);
      if (payload === lastRecentScansPayload) return;
      lastRecentScansPayload = payload;
      renderRecentScans(data.scans || []);
    } catch (_error) {
      /* transient */
    }
  }

  function renderFilterButtons() {
    const container = document.getElementById('upcomingTableFilters');
    if (!container) return;
    container.querySelectorAll('[data-table-filter]').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.tableFilter === tableFilter);
    });
  }

  function setUpcomingTableFilter(filter) {
    tableFilter = filter || 'all';
    try {
      sessionStorage.setItem('dashboardTableFilter', tableFilter);
    } catch (_e) {
      /* private browsing */
    }
    renderFilterButtons();
    loadUpcomingReleasesTable();
  }

  async function loadUpcomingReleasesTable() {
    const desktop = document.getElementById('upcomingTableWrap');
    const mobile = document.getElementById('upcoming-releases-carousel');
    if (!desktop && !mobile) return;

    if (desktop) {
      desktop.innerHTML =
        '<div class="text-center py-4 text-muted small">' +
        '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>' +
        'Loading releases…</div>';
    }

    try {
      const data = await global.UpcomingReleasesService.fetchReleases({
        filter: tableFilter,
        include_queue: true,
        limit: 50,
        page: 1,
      });
      const opts = { emptyMessage: 'No upcoming releases found.' };
      if (desktop) {
        global.UpcomingReleasesService.renderTable('upcomingTableWrap', data.releases || [], opts);
      }
      if (mobile) {
        global.UpcomingReleasesService.renderCarousel('upcoming-releases-carousel', data.releases || [], opts);
      }
    } catch (error) {
      const msg =
        `<div class="text-center py-4 text-danger small">Error loading releases: ${esc(error.message)}</div>`;
      if (desktop) desktop.innerHTML = msg;
      if (mobile) mobile.innerHTML = msg;
    }
  }

  const RECENT_TAB_KEY = 'dashboardRecentTab';
  const RECENT_TAB_HINTS = {
    recentAddedPane: 'Last 14 days',
    recentScansPane: 'Latest first',
    pendingApprovalPane: 'Not yet applied',
  };

  function initRecentHistoryTabs() {
    const tabs = document.getElementById('recentHistoryTabs');
    if (!tabs) return;

    const hint = document.getElementById('recentHistoryHint');
    const paneOf = (target) => String(target || '').replace('#', '');
    const setHint = (target) => {
      if (hint) hint.textContent = RECENT_TAB_HINTS[paneOf(target)] || '';
    };

    let stored = null;
    try {
      stored = sessionStorage.getItem(RECENT_TAB_KEY);
    } catch (_e) {
      /* private browsing */
    }
    if (stored) {
      const btn = tabs.querySelector(`[data-bs-target="#${stored}"]`);
      const Tab = global.bootstrap && global.bootstrap.Tab;
      if (btn && Tab) Tab.getOrCreateInstance(btn).show();
    }

    tabs.addEventListener('shown.bs.tab', (event) => {
      const target = event.target && event.target.getAttribute('data-bs-target');
      setHint(target);
      try {
        sessionStorage.setItem(RECENT_TAB_KEY, paneOf(target));
      } catch (_e) {
        /* quota / private browsing */
      }
    });

    const activeBtn = tabs.querySelector('.nav-link.active');
    setHint(activeBtn && activeBtn.getAttribute('data-bs-target'));
  }

  async function updateQueueProcessorAlert() {
    const host = document.getElementById('queueProcessorAlert');
    if (!host) return;

    let data;
    try {
      data = await global.api.getJson('/api/queue-processor/status');
    } catch (_error) {
      return;
    }
    renderQueueProcessorAlert(host, data);
  }

  function renderQueueProcessorAlert(host, data) {
    const status = String((data && data.status) || '').toLowerCase();
    if (status !== 'stopped') {
      host.innerHTML = '';
      host.classList.add('d-none');
      return;
    }

    const auto = (data && data.auto_restart) || {};
    const notes = [];
    if (auto.enabled) {
      const count = Number(auto.restart_count) || 0;
      notes.push(
        count > 0
          ? `Auto-restart is on — restarted ${count} time${count === 1 ? '' : 's'} automatically.`
          : 'Auto-restart is on and will start a cycle automatically.'
      );
      if (auto.last_restart_iso) {
        const when = new Date(auto.last_restart_iso);
        if (!isNaN(when.getTime())) notes.push(`Last automatic restart ${when.toLocaleString()}.`);
      }
    } else {
      notes.push('Auto-restart is turned off — use Restart Processor, or enable it in Config.');
    }

    host.classList.remove('d-none');
    host.innerHTML = `
      <div class="alert alert-danger py-2 px-3 mb-3" style="font-size:.85rem;">
        <div class="d-flex justify-content-between align-items-start gap-2">
          <div class="fw-semibold">
            <i class="bi bi-exclamation-octagon-fill me-1"></i>Download queue processor is not running
          </div>
          <button type="button" class="btn btn-sm btn-danger js-restart-queue-processor flex-shrink-0">
            <i class="bi bi-arrow-clockwise me-1"></i>Restart Processor
          </button>
        </div>
        <div class="small mt-1">${esc((data && data.message) || 'No queue cycle has run recently.')}</div>
        <div class="text-muted small mt-1">${notes.map(esc).join(' ')}</div>
      </div>`;
  }

  async function restartQueueProcessorFromDashboard() {
    const button = document.querySelector('.js-restart-queue-processor');
    if (button) {
      button.disabled = true;
      button.textContent = 'Restarting…';
    }

    try {
      await global.api.postJson('/api/queue-processor/restart', {});
      await updateQueueProcessorAlert();
    } catch (error) {
      const host = document.getElementById('queueProcessorAlert');
      if (host && !host.classList.contains('d-none')) {
        host.innerHTML = `
          <div class="alert alert-danger py-2 px-3 mb-3" style="font-size:.85rem;">
            <div class="fw-semibold">
              <i class="bi bi-exclamation-octagon-fill me-1"></i>Could not restart the queue processor
            </div>
            <div class="small mt-1">${esc(error && error.message ? error.message : String(error))}</div>
            <button type="button" class="btn btn-sm btn-danger js-restart-queue-processor mt-2">
              <i class="bi bi-arrow-clockwise me-1"></i>Try Again
            </button>
          </div>`;
      }
    }
  }

  function updateAll() {
    return Promise.all([
      updateActiveScans(),
      updateRecentScans(),
      updatePendingApproval(),
      updateQueueProcessorAlert(),
    ]);
  }

  document.addEventListener('DOMContentLoaded', function () {
    renderRecentScans((global._pd && global._pd.recentScans) || []);
    initRecentHistoryTabs();
    initRecentlyAddedActions();

    try {
      const stored = sessionStorage.getItem('dashboardTableFilter');
      if (stored) tableFilter = stored;
    } catch (_e) {
      /* private browsing */
    }

    renderFilterButtons();

    if (global.UpcomingReleasesService) {
      global.UpcomingReleasesService.onRefresh = loadUpcomingReleasesTable;
      loadUpcomingReleasesTable();

      upcomingPoller = global.poller.create({
        interval: UPCOMING_REFRESH_MS,
        immediate: false,
        onTick: loadUpcomingReleasesTable,
      });
      upcomingPoller.start();
    }

    updateAll();
    statusPoller = global.poller.create({
      interval: STATUS_POLL_MS,
      immediate: false,
      onTick: updateAll,
    });
    statusPoller.start();
  });

  global.dashboard = {
    updateAll,
    renderRecentScans,
    renderPendingApproval,
    updatePendingApproval,
    loadUpcomingReleasesTable,
    setUpcomingTableFilter,
    formatElapsed,
    scanDisplayName,
    abandonedBanner,
    abandonedFingerprint,
    isAlertDismissed,
    dismissDashboardAlert,
    updateQueueProcessorAlert,
    renderQueueProcessorAlert,
    restartQueueProcessorFromDashboard,
  };

  global.stopAllScans = stopAllScans;
  global.setUpcomingTableFilter = setUpcomingTableFilter;
  global.loadUpcomingReleasesTable = loadUpcomingReleasesTable;
})(window);
