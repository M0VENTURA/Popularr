/* ==========================================================================
   static/js/pages/dashboard.js
   Dashboard — scan controls, active/recent scans, upcoming releases table.

   Load order:
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/button-state.js  →  ui/status-badge.js
       upcoming_releases.js
       dashboard.js

   ── THE BIG ONE: postJSON SWALLOWED EVERY FAILURE ─────────────────────────
       async function postJSON(u, b) {
         const r = await fetch(u, {...});
         return r.json().catch(() => ({}));       // <-- everything becomes {}
       }
   No `r.ok` check, and a failed parse resolved to an empty object. A 500, an
   HTML login page, a dropped connection and a real success were
   indistinguishable to every caller. Concretely:

     startPopularityScan    awaited it and ignored the result entirely, so a
                            rejected scan looked identical to a started one
     startNavidromeImport   read `d.success` — always undefined on failure —
                            and printed `d.error || "Failed"`, i.e. always
                            the generic "Failed" with the real reason lost
     startNavidromeServerScan  same
     addUpcomingReleaseToQueueDashboard  same, on a queue write

   Replaced with api.postJson, which throws. Every call site below now has a
   try/catch that surfaces the actual error.

   ── OTHER FIXES ───────────────────────────────────────────────────────────
   1. `window.onerror` was overridden to console.error. It returned undefined
      (so the default handler still ran) but it clobbered any other global
      error handler and reported nothing to the server. Removed — it was
      described in its own comment as "for development debugging".

   2. THREE bare setIntervals, none cleared: updateAll @5s, the upcoming
      table @30min, and a player-bar visibility check @2s. All still ran in
      a backgrounded tab. Now managed pollers.

   3. The player-bar poll was a 2-second DOM poll for a class change. It is
      now driven by the same `player-visible` body class player.js already
      sets, so the timer is gone entirely.

   4. `escapeHtml` used createTextNode + innerHTML, which does NOT escape
      quotes — and its output went into `title="…"` attributes in the
      upcoming table. utils/dom.js's version escapes all five entities.

   5. `fE()` (elapsed formatter) renamed to formatElapsed and kept local: it
      produces the " — 3m 20s" SUFFIX including the em dash, so it is not
      interchangeable with formatDuration from utils/dom.js.

   NOTE: `.source-key-badge` is rendered by _buildUpcomingReleaseRow here and
   by upcoming_releases.js. It has NO definition in popularr.css — the rule
   used to be injected from JS by upcoming_releases.js and that injection was
   removed. Add the CSS rule, or both tables render an unstyled span.
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

  /**
   * Elapsed-time SUFFIX, e.g. " — 3m 20s".
   * Not interchangeable with formatDuration: it includes the separator and
   * returns '' for falsy input rather than 'N/A'.
   */
  function formatElapsed(seconds) {
    if (!seconds) return '';
    return ` — ${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  }

  function setStatus(id, text, className) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = className;
  }

  // ── Scan controls ───────────────────────────────────────────────────────

  /**
   * Run the gate before every scan start.
   *
   * A start while another scan is running is rejected server-side as a
   * duplicate, so ask first: name the running scan, offer to cancel it, and
   * wait for it to actually stop before letting the new scan through.
   */
  async function scanGate(scanName) {
    if (!global.ScanPreflight) return true;
    return global.ScanPreflight.confirmIfRunning({ scanName: scanName || '' });
  }

  function startPopularityScan(mode, force, restart, resumeFrom) {
    const body = {
      mode: mode || 'popularity',
      force: !!force,
      restart: !!restart,
    };
    // Explicit resume point from the picker. The server treats a missing value
    // as "use the stored checkpoint", so it is only sent when chosen.
    if (resumeFrom) body.resume_from = resumeFrom;
    return global.api.postJson('/api/popularity/run', body);
  }

  function stopPopularityScan() {
    return global.api.postJson('/scan/stop-popularity', {});
  }

  /**
   * Run the selected popularity scan with the current Force / Restart state.
   * Restart clears the resume checkpoint so the scan begins from the top.
   *
   * With Restart UNCHECKED the user is continuing a previous run, so they are
   * asked which artist to resume from first. Restart already means "from the
   * top", so prompting there would be contradictory.
   */
  function runDashboardPopularityScan() {
    const mode = document.getElementById('popScanSelector')?.value || 'popularity';
    const force = !!document.getElementById('popScanForce')?.checked;
    const restart = !!document.getElementById('popScanRestart')?.checked;
    const btn = document.getElementById('popScanRunBtn');

    return global.buttonState.withBusy(btn, 'Starting…', async () => {
      try {
        if (!await scanGate(global.ScanPreflight
          ? global.ScanPreflight.label(mode) : mode)) return;

        // Resume prompt — only when continuing (Restart unchecked). `resume_from`
        // is honoured by every mode (the load stage skips to that artist), so a
        // metadata/popularity/singles run gets the same choice as a full scan.
        // If nothing was ever recorded there is no prompt at all.
        let resumeFrom = null;
        if (!restart && global.ScanResumePicker) {
          const choice = await global.ScanResumePicker.chooseResumeArtist();
          if (!choice) return;   // cancelled — do not start
          resumeFrom = choice.resume_from || null;
        }

        await startPopularityScan(mode, force, restart, resumeFrom);
        global.toast.success('Popularity scan started');
      } catch (error) {
        // Previously this was `catch (e) { console.error(...) }` — the user
        // saw the spinner stop and nothing else.
        global.toast.error('Could not start scan: ' + error.message);
      }
    });
  }

  async function pollPopularityStatus() {
    const el = document.getElementById('pop-status');
    if (!el) return;
    try {
      const data = await global.api.getJson('/api/popularity/status');
      if (!data.success) return;
      if (data.running) {
        setStatus('pop-status',
          `${data.message || 'Running…'}${formatElapsed(data.elapsed_seconds)}`,
          'text-primary small');
      } else {
        setStatus('pop-status', data.message || 'Idle', 'text-muted small');
      }
    } catch (_error) {
      /* transient — next tick retries */
    }
  }

  async function startNavidromeImport() {
    // "Forced" = mode:force → serial full re-import of every artist, ignoring
    // the new-items/changed-album skips. Unchecked = mode:all → normal import
    // that skips unchanged albums.
    const force = !!document.getElementById('navImportForce')?.checked;
    const el = document.getElementById('nav-status');

    if (!await scanGate('Navidrome Import')) return;

    try {
      const data = await global.api.postJson('/api/navidrome/import', {
        mode: force ? 'force' : 'all',
      });
      if (!data.success) {
        setStatus('nav-status', data.error || 'Failed', 'text-danger small');
        return;
      }
      setStatus('nav-status', data.message || 'Started', 'text-success small');
      if (el) el.dataset.started = String(Date.now());
    } catch (error) {
      // The real reason now reaches the user instead of a bare "Failed".
      setStatus('nav-status', error.message, 'text-danger small');
    }
  }

  async function startNavidromeServerScan() {
    if (!await scanGate('Navidrome Server Scan')) return;
    try {
      const data = await global.api.postJson('/api/navidrome/scan/start', {});
      setStatus('nav-status',
        data.success ? 'Server scan triggered' : (data.error || 'Failed'),
        data.success ? 'text-success small' : 'text-danger small');
    } catch (error) {
      setStatus('nav-status', error.message, 'text-danger small');
    }
  }

  function stopNavidromeSync() {
    return global.api.postJson('/scan/stop-navidrome', {});
  }

  async function pollNavidromeStatus() {
    const el = document.getElementById('nav-status');
    if (!el) return;

    try {
      const progress = await global.api.getJson('/api/scan-progress');
      const scan = (progress.active_scans || [])
        .find((s) => s.scan_type === 'navidrome_scan');

      if (scan && scan.is_running) {
        setStatus('nav-status',
          `${scan.message || 'Importing…'}${formatElapsed(scan.elapsed_seconds)}`,
          'text-primary small');
        delete el.dataset.started;
        return;
      }

      const serverStatus = await global.api.getJson('/api/navidrome/scan/status');
      if (serverStatus.scanning) {
        setStatus('nav-status', 'Server scanning…', 'text-warning small');
        delete el.dataset.started;
        return;
      }

      // Grace window: the import takes a moment to appear in scan-progress,
      // so a recent start keeps showing "Importing…" for up to two minutes.
      if (el.dataset.started) {
        const elapsed = Date.now() - parseInt(el.dataset.started, 10);
        if (elapsed < 120000) {
          setStatus('nav-status', 'Importing…', 'text-primary small');
          return;
        }
        delete el.dataset.started;
      }
      setStatus('nav-status', 'Idle', 'text-muted small');
    } catch (_error) {
      /* transient */
    }
  }

  async function startEssentiaScan() {
    if (!await scanGate('Essentia Mood Scan')) return;
    return global.api.postJson('/api/essentia/run', {});
  }

  function stopEssentiaScan() {
    return global.api.postJson('/scan/stop-essentia-mood', {});
  }

  async function pollEssentiaStatus() {
    const el = document.getElementById('ess-status');
    if (!el) return;
    try {
      const progress = await global.api.getJson('/api/scan-progress');
      const scan = (progress.active_scans || [])
        .find((s) => s.scan_type === 'essentia_mood_scan');
      if (scan && scan.is_running) {
        setStatus('ess-status',
          `${scan.message || 'Running…'}${formatElapsed(scan.elapsed_seconds)}`,
          'text-primary small');
      } else {
        setStatus('ess-status', 'Idle', 'text-muted small');
      }
    } catch (_error) {
      /* transient */
    }
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

  async function checkNavidromeConnectivity() {
    const el = document.getElementById('nav-connectivity');
    if (!el) return;
    try {
      const data = await global.api.getJson('/api/navidrome/scan/status');
      const ok = data.success !== false;
      el.className = 'badge ms-1 ' + (ok ? 'bg-success' : 'bg-danger');
      el.title = ok ? 'OK' : 'Unreachable';
      el.textContent = ok ? '\u2713' : '\u2717';
    } catch (_error) {
      el.className = 'badge bg-danger ms-1';
      el.title = 'Unreachable';
      el.textContent = '\u2717';
    }
  }

  // ── Scan display names ──────────────────────────────────────────────────

  const SCAN_TYPE_DISPLAY_NAMES = {
    navidrome: 'Navidrome Import',
    metadata: 'Metadata Scan',
    popularity: 'Popularity Scan',
    singles: 'Singles Detection',
    singles_detection: 'Singles Detection',
    essentia: 'Essentia Mood Scan',
    mood: 'Mood Scan',
    combined: 'Combined Scan',
    all: 'Full Scan (All)',
    navidrome_scan: 'Navidrome Import',
    metadata_lookup_scan: 'Metadata Scan',
    popularity_scan: 'Popularity Scan',
    singles_scan: 'Singles Detection',
    essentia_mood_scan: 'Essentia Mood Scan',
    // The dashboard "All" scan writes its progress row as `full_scan`
    // (services/scanning/pipelines/popularity_pipeline.py).  Without this key
    // the panel fell through to the raw scan_type and rendered "full_scan"
    // instead of a human label.
    full_scan: 'Full Scan',
    library_scan: 'Library Scan',
    missing_releases_scan: 'Missing Releases Scan',
  };

  function scanDisplayName(type) {
    return SCAN_TYPE_DISPLAY_NAMES[type] || String(type || 'Scan');
  }

  // ── Active scans ────────────────────────────────────────────────────────

  function updateScanStatusBar(active) {
    const line = document.getElementById('scanStatusLine');
    const icon = document.getElementById('scanStatusIcon');
    if (!line || !icon) return;

    if (!active || !active.length) {
      line.textContent = 'Idle — no scan running';
      icon.className = 'scan-status-idle';
      icon.innerHTML = '<i class="bi bi-circle"></i>';
      return;
    }

    const scan = active[0];
    const pct = Math.min(scan.percent_complete ?? scan.progress ?? 0, 100);
    const stage = scan.current_stage ? ` · ${scan.current_stage}` : '';
    line.textContent = `${scanDisplayName(scan.scan_type)} — ${pct}%${stage}` +
      (scan.current_item ? ` · ${scan.current_item}` : '');
    icon.className = 'scan-status-active';
    icon.innerHTML = '<i class="bi bi-activity"></i>';
  }

  /**
   * Build the "artists skipped / abandoned" banner.
   *
   * The full-scan orchestrator records per-artist failures and budget
   * abandons on the progress row (scan.abandoned_artists). Surfacing them
   * lets the operator investigate instead of the scan silently moving past
   * a hung artist.
   */
  function abandonedBanner(scan) {
    const entries = Object.entries(scan.abandoned_artists || {});
    if (!entries.length) return '';

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

    return `<div class="alert alert-warning py-2 px-3 mb-2" style="font-size:.85rem;">
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

    const badge = document.getElementById('scannerStatusBadge');
    if (badge) {
      badge.innerHTML = '<i class="bi bi-circle-fill me-1 small"></i> ' +
        (active.length ? 'Active' : 'Idle');
      badge.className = 'badge ' + (active.length ? 'bg-success' : 'bg-secondary');
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

      return `
        <div class="mb-2">
          ${abandonedBanner(scan)}
          <div class="d-flex justify-content-between align-items-center mb-1">
            <span>
              <i class="bi bi-activity me-1"></i>
              <strong>${esc(scanDisplayName(scan.scan_type))}</strong>${stage}${message}
            </span>
            <span class="small text-muted">${esc(String(scan.processed_items ?? 0))}/${esc(String(scan.total_items ?? '?'))}</span>
          </div>
          ${currentItem}
          <div class="progress" style="height:8px;">
            <div class="progress-bar progress-bar-striped progress-bar-animated bg-success"
                 style="width:${pct}%;"></div>
          </div>
        </div>`;
    }).join('');
  }

  // ── Recent scans ────────────────────────────────────────────────────────

  function parseScanTimestamp(ts) {
    if (!ts) return new Date(0);
    const parsed = new Date(ts);
    return Number.isNaN(parsed.getTime()) ? new Date(0) : parsed;
  }

  function formatScanTimestamp(ts) {
    const date = parseScanTimestamp(ts);
    if (date.getTime() === 0) return 'unknown';

    const diffMs = Date.now() - date.getTime();
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return date.toLocaleDateString();
  }

  function groupRecentScans(scans) {
    const grouped = Object.create(null);
    const inProgress = [];

    (scans || []).forEach((scan) => {
      const ts = scan.scan_timestamp || scan.started_at || scan.timestamp || null;

      if (scan._inProgress) {
        inProgress.push({
          artist: scan.artist,
          album: scan.album || '…',
          scan_types: [{ type: scan.scan_type, timestamp: ts, _inProgress: true }],
          latest_timestamp: ts,
          latest_timestamp_obj: new Date(),
          _inProgress: true,
        });
        return;
      }

      const key = `${scan.artist}|${scan.album}`;
      if (!grouped[key]) {
        grouped[key] = {
          artist: scan.artist,
          album: scan.album,
          status: scan.status,
          scan_types: [],
          latest_timestamp: ts,
          latest_timestamp_obj: parseScanTimestamp(ts),
        };
      }

      const entry = grouped[key];
      if (scan.status === 'failed' || scan.status === 'error') {
        entry.status = 'failed';
      } else if (scan.status === 'completed' || scan.status === 'complete') {
        if (entry.status !== 'failed') entry.status = 'completed';
      } else if (scan.status === 'started' && !entry.status) {
        entry.status = 'started';
      } else if (scan.status === 'stopped'
        && entry.status !== 'failed' && entry.status !== 'completed') {
        entry.status = 'stopped';
      }

      entry.scan_types.push({ type: scan.scan_type, timestamp: ts });

      const tsObj = parseScanTimestamp(ts);
      if (tsObj > entry.latest_timestamp_obj) {
        entry.latest_timestamp = ts;
        entry.latest_timestamp_obj = tsObj;
      }
    });

    return inProgress.concat(
      Object.values(grouped).sort((a, b) => b.latest_timestamp_obj - a.latest_timestamp_obj)
    );
  }

  function renderRecentScans(scans) {
    const body = document.getElementById('recent-scans-body');
    if (!body) return;

    if (!scans || !scans.length) {
      body.innerHTML = '<div class="p-3 text-center text-muted small">No recent scan history.</div>';
      return;
    }

    body.innerHTML = groupRecentScans(scans).map((entry) => {
      const badge = entry._inProgress
        ? '<span class="badge bg-info">scanning</span>'
        : (global.statusBadge
          ? global.statusBadge.render(entry.status, { icon: false })
          : `<span class="badge bg-secondary">${esc(entry.status || '')}</span>`);

      const types = entry.scan_types
        .map((t) => esc(scanDisplayName(t.type)))
        .filter((v, i, arr) => arr.indexOf(v) === i)
        .join(', ');

      // Rows must be CLICKABLE. This renderer previously emitted plain <div>
      // text with no anchors, so recent scans were inert on the rebuilt
      // dashboard while the live one linked through — the reported "the
      // artists and albums on recent scans aren't selectable by clicking
      // them".
      //
      // Session rows (`_SCAN_SESSION_`) are not about one artist, so they get
      // no link; `encodeURIComponent` on each segment keeps slashes and "#" in
      // names from breaking the path.
      const isSession = entry.artist === '_SCAN_SESSION_' || !entry.artist;
      const artistUrl = `/artist/${encodeURIComponent(entry.artist || '')}`;
      const albumUrl = (entry.album && entry.album !== '…')
        ? `/album/${encodeURIComponent(entry.artist || '')}/${encodeURIComponent(entry.album)}`
        : artistUrl;

      const artistHtml = isSession
        ? `<strong>${esc(entry.artist || '')}</strong>`
        : `<a href="${artistUrl}" class="text-success text-decoration-none fw-semibold">${esc(entry.artist || '')}</a>`;

      const albumHtml = (isSession || !entry.album || entry.album === '…')
        ? (entry.album ? `<div class="small text-muted text-truncate">${esc(entry.album)}</div>` : '')
        : `<div class="small text-muted text-truncate"><a href="${albumUrl}" class="text-muted text-decoration-none">${esc(entry.album)}</a></div>`;

      return `
        <div class="border-bottom px-3 py-2">
          <div class="d-flex justify-content-between align-items-start gap-2">
            <div class="text-truncate" style="min-width:0;">
              <div class="text-truncate">${artistHtml}</div>
              ${albumHtml}
              <div class="small text-muted">${types}</div>
            </div>
            <div class="text-end flex-shrink-0">
              ${badge}
              <div class="small text-muted mt-1">${esc(formatScanTimestamp(entry.latest_timestamp))}</div>
            </div>
          </div>
        </div>`;
    }).join('');
  }

  async function updateRecentScans() {
    try {
      const data = await global.api.getJson('/api/scans/recent?limit=25');
      const payload = JSON.stringify(data.scans || []);
      // Skip the re-render when nothing changed — this runs every 5s and the
      // list is usually static.
      if (payload === lastRecentScansPayload) return;
      lastRecentScansPayload = payload;
      renderRecentScans(data.scans || []);
    } catch (_error) {
      /* transient */
    }
  }

  // ── Upcoming releases ───────────────────────────────────────────────────

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
    const container = document.getElementById('upcomingTableWrap');
    if (!container) return;

    try {
      // Delegated to UpcomingReleasesService so the dashboard table and the
      // dedicated page share one fetch + render path.
      const data = await global.UpcomingReleasesService.fetchReleases({
        filter: tableFilter,
        include_queue: true,
        limit: 50,
        page: 1,
      });
      global.UpcomingReleasesService.renderTable('upcomingTableWrap', data.releases || [], {
        emptyMessage: 'No upcoming releases found.',
      });
    } catch (error) {
      container.innerHTML =
        `<div class="text-center py-4 text-danger small">Error loading releases: ${esc(error.message)}</div>`;
    }
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function updateAll() {
    return Promise.all([
      pollPopularityStatus(),
      pollNavidromeStatus(),
      pollEssentiaStatus(),
      updateActiveScans(),
      updateRecentScans(),
    ]);
  }

  document.addEventListener('DOMContentLoaded', function () {
    checkNavidromeConnectivity();
    renderRecentScans((global._pd && global._pd.recentScans) || []);

    try {
      const stored = sessionStorage.getItem('dashboardTableFilter');
      if (stored) tableFilter = stored;
    } catch (_e) {
      /* private browsing */
    }

    renderFilterButtons();

    if (global.UpcomingReleasesService) {
      // Re-render after a match or queue mutation.
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

  // The sticky scan bar must clear the global player bar when it appears.
  // This used to be a 2-second DOM poll; player.js already toggles
  // `player-visible` on <body>, so the timer is unnecessary.

  global.dashboard = {
    updateAll,
    renderRecentScans,
    loadUpcomingReleasesTable,
    setUpcomingTableFilter,
    formatElapsed,
    scanDisplayName,
  };

  // Globals for the inline onclick handlers in dashboard.html.
  global.startPopularityScan = startPopularityScan;
  global.stopPopularityScan = stopPopularityScan;
  global.runDashboardPopularityScan = runDashboardPopularityScan;
  global.startNavidromeImport = startNavidromeImport;
  global.startNavidromeServerScan = startNavidromeServerScan;
  global.stopNavidromeSync = stopNavidromeSync;
  global.startEssentiaScan = startEssentiaScan;
  global.stopEssentiaScan = stopEssentiaScan;
  global.stopAllScans = stopAllScans;
  global.setUpcomingTableFilter = setUpcomingTableFilter;
  global.loadUpcomingReleasesTable = loadUpcomingReleasesTable;
})(window);
