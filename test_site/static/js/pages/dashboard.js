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

   Replaced with api.postJson, which throws, so every call site carries a
   try/catch that surfaces the actual error.

   ⚠️ THE THREE SCAN STARTERS NAMED ABOVE NO LONGER EXIST. They moved into the
   shared scan sheet and were deleted from this file — see "Scan controls"
   below for why, and for the one function of that family that survived.
   `addUpcomingReleaseToQueueDashboard` is gone too. The note is kept because
   it is the reason api.postJson exists at all, and because "it reported
   success on failure" is the bug class to watch for in anything added here.

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

  // ── Scan controls ───────────────────────────────────────────────────────
  //
  // The scan STARTERS are not in this file. Every start goes through the shared
  // scan sheet (`components/_scan_modal.html` + `js/ui/scan-modal.js`), which
  // owns the mode list, the Force / Restart options, the preflight gate and the
  // dispatch, and calls the same endpoints this file used to.
  //
  // A parallel copy of that whole flow used to live here — startPopularityScan,
  // runDashboardPopularityScan, pollPopularityStatus, startNavidromeImport,
  // startNavidromeServerScan, stopNavidromeSync, pollNavidromeStatus,
  // startEssentiaScan, stopEssentiaScan, pollEssentiaStatus and
  // checkNavidromeConnectivity — driving the tabbed engine panel that
  // `Pages/dashboard.html` no longer renders. Every `getElementById` in them
  // resolved to null, so not one of them could run.
  //
  // They were DELETED rather than left in place precisely BECAUSE they did not
  // throw. A reader would see `#popScanSelector` being read and conclude the
  // dashboard still owned that control — a comment-shaped lie with a body. The
  // survivors are below: `stopAllScans` (the scanner card's Stop button) and
  // everything from `scanDisplayName` down.
  //
  // Both of the helpers those functions shared are gone with them: `setStatus`
  // (whose only callers were the six pollers) and `scanGate` (whose only
  // callers were the starters — the sheet runs its own preflight).

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

  // ── Scan display names ──────────────────────────────────────────────────

  const SCAN_TYPE_DISPLAY_NAMES = {
    navidrome: 'Navidrome Import',
    metadata: 'Metadata Scan',
    popularity: 'Popularity Scan',
    singles: 'Singles Detection',
    singles_detection: 'Singles Detection',
    finalise: 'Finalise Scan',
    finalise_scan: 'Finalise Scan',
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

  /**
   * Render a scan percentage for a text label.
   *
   * progress_service returns real decimals: one artist's share of a full scan
   * is 100/total_artists, so on a large library the first artists are a fraction
   * of a percent. Truncating to an integer is what made the status bar sit on
   * "0%" while the scan was working, so anything inside (0, 1) shows as "<1%".
   * Progress BARS must keep using the raw number for their width.
   * Kept identical to the copy in main.js — do not let them drift.
   */
  function formatScanPercent(pct) {
    const value = Number(pct) || 0;
    if (value > 0 && value < 1) return '<1%';
    return `${Math.round(value)}%`;
  }

  function updateScanStatusBar(active) {
    const line = document.getElementById('scanStatusLine');
    const icon = document.getElementById('scanStatusIcon');
    if (!line || !icon) return;

    // `setScanBarActive` is owned by main.js because the same bar is written
    // from two places (this poll on the dashboard, the global SSE stream
    // everywhere else). Both must agree on what "active" looks like, or the
    // pill and the hairline would depend on which writer ran last.
    const barApi = global.popularr || {};
    const setActive = typeof barApi.setScanBarActive === 'function'
      ? barApi.setScanBarActive
      : () => {};

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

    // ── The scanner card's two states ──────────────────────────────────
    //
    // Idle: a line of reassurance and one action. Scanning: the progress and
    // the two controls that apply to a running scan. ONE card toggled, not two
    // — two would need this function to keep them in sync and would double the
    // empty-state surface.
    const running = active.find((s) => s && s.is_running) || active[0];
    const idleState = document.getElementById('scannerIdleState');
    const activeState = document.getElementById('scannerActiveState');
    const stateLabel = document.getElementById('scannerState');

    if (idleState) idleState.classList.toggle('d-none', !!running);
    if (activeState) activeState.classList.toggle('d-none', !running);
    if (stateLabel) {
      // "Scanning Library", not just "Active" — the heading names WHAT is
      // running, which is the fact the old badge could not carry.
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

      // Elapsed time is what the old per-engine pollers showed and this panel
      // dropped when it replaced them. formatElapsed is the suffix formatter
      // written for those pollers (" — 3m 20s"), so it is wired back in here
      // rather than left behind as an export with no caller.
      //
      // ⚠️ Keep comments OUT of the template literal below: it contains
      // backticks of its own, so a stray one in prose half-closes the string.
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

  // ── Recently Added: play an album ───────────────────────────────────────

  /**
   * Play an album from the Recently Added feed.
   *
   * The tracklist comes from GET /api/album/tracklist, which is the endpoint
   * the album page already uses — so this needs no new API and cannot drift
   * from what the album page shows. Its rows are
   * ``{track_id, position, title, artist}``; Player.playQueue wants
   * ``{id, title, artist, albumArtUrl}``, so the id is renamed here.
   *
   * ⭐ DELEGATED from the list container rather than bound per button. The
   * Recently Added pane is SERVER-RENDERED, so its buttons exist before this
   * module runs — but the tab can be re-rendered by a navigation and a
   * per-button listener would then be lost. One listener on the ancestor
   * survives any innerHTML replacement.
   *
   * The album is passed through ``data-`` attributes, not an inline onclick:
   * album names routinely contain apostrophes, which would terminate a JS
   * string literal in the attribute and produce a syntax error on the page.
   */
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
        // An empty album is a real state (every track failed to import), and
        // silently playing nothing reads as a broken button.
        global.toast.warning(`No playable tracks on "${album}".`);
        return;
      }

      global.Player.playQueue(tracks.map((t) => ({
        id: t.track_id,
        title: t.title || '',
        artist: t.artist || artist,
        // The same art URL the row already loaded, so the player reuses the
        // browser cache instead of requesting it a second time.
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
      // The button sits BESIDE the album link, not inside it (a <button> in an
      // <a> is invalid HTML), but stop the event anyway so a future markup
      // change that nests them cannot start navigating as well as playing.
      event.preventDefault();
      playRecentAlbum(button);
    });
  }

  // ── Needs Metadata Approval ─────────────────────────────────────────────

  let lastPendingApprovalPayload = null;

  /**
   * Render the albums whose scan recommendations are still awaiting a decision.
   *
   * ⭐ THE LIST IS A WORK QUEUE, NOT A LOG. An album leaves it by itself once
   * its review is saved or DISCARDED, because both paths clear the stash the
   * scan wrote (`tracks.pending_mb_updates`) — so there is nothing to re-scan
   * and no "approved" bookkeeping to keep in sync here.
   */
  function renderPendingApproval(albums) {
    const tabItem = document.getElementById('pendingApprovalTabItem');
    const tabBtn = document.getElementById('pendingApprovalTab');
    const body = document.getElementById('pending-approval-body');
    const countEl = document.getElementById('pending-approval-count');
    if (!body) return;

    const n = (albums || []).length;
    if (countEl) countEl.textContent = String(n);

    if (!n) {
      // Hide the TAB, not a card: an always-visible "Needs Review 0" is a
      // permanent nag on a library that has nothing to review.
      if (tabItem) tabItem.classList.add('d-none');
      body.innerHTML = '';
      // If the user was sitting on this tab when the last item was cleared
      // (saving an album does exactly that), leaving them on a now-hidden pane
      // shows a blank card with no way back. Move them to the first tab.
      if (tabBtn && tabBtn.classList.contains('active')) {
        const first = document.querySelector('#recentHistoryTabs .nav-link');
        const Tab = global.bootstrap && global.bootstrap.Tab;
        if (first && Tab) Tab.getOrCreateInstance(first).show();
      }
      return;
    }

    if (tabItem) tabItem.classList.remove('d-none');

    body.innerHTML = albums.map((album) => {
      // Each row links to the album page, where the existing review UI shows
      // the proposed changes and "Save Metadata" applies them (which also
      // clears the stash, removing the row on the next poll).
      const artist = encodeURIComponent(album.artist || '');
      const title = encodeURIComponent(album.album || '');
      const year = album.album_year ? `/${encodeURIComponent(album.album_year)}` : '';
      const url = `/album/${artist}/${title}${year}`;

      const changes = Number(album.change_count) || 0;
      const tracks = Number(album.tracks_changed) || 0;
      const albumChanges = Number(album.album_changes) || 0;

      // Spell out WHERE the changes are: a two-field album-level edit and a
      // twenty-track retag are very different amounts of work.
      const detail = [
        albumChanges ? `${albumChanges} album field${albumChanges === 1 ? '' : 's'}` : '',
        tracks ? `${tracks} track${tracks === 1 ? '' : 's'}` : '',
      ].filter(Boolean).join(' · ');

      return `
        <a href="${url}"
           class="list-group-item list-group-item-action bg-transparent border-secondary d-flex justify-content-between align-items-start gap-2 p-2">
          <div class="min-w-0">
            <div class="fw-bold text-truncate text-light" style="font-size: 0.9rem;">${esc(album.album || '')}</div>
            <div class="text-muted text-truncate" style="font-size: 0.8rem;">${esc(album.artist || '')}</div>
            ${detail ? `<div class="text-muted extra-small text-truncate">${esc(detail)}</div>` : ''}
          </div>
          <div class="flex-shrink-0 text-end">
            <span class="badge bg-info text-dark" title="${esc(String(changes))} recommended change${changes === 1 ? '' : 's'}">
              ${esc(String(changes))} change${changes === 1 ? '' : 's'}
            </span>
            <div class="text-muted extra-small mt-1">${esc(formatScanTimestamp(album.stashed_at))}</div>
          </div>
        </a>`;
    }).join('');
  }

  async function updatePendingApproval() {
    try {
      const data = await global.api.getJson('/api/metadata/pending-recommendations?limit=50');
      const payload = JSON.stringify(data.albums || []);
      // Skip the re-render when nothing changed — this runs on the same 5s
      // poll as the rest of the dashboard and the list is usually static.
      if (payload === lastPendingApprovalPayload) return;
      lastPendingApprovalPayload = payload;
      renderPendingApproval(data.albums || []);
    } catch (_error) {
      /* transient: leave the last good list in place */
    }
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
    const desktop = document.getElementById('upcomingTableWrap');
    const mobile = document.getElementById('upcoming-releases-carousel');
    if (!desktop && !mobile) return;

    // Loading state. Without it the card is blank until the fetch resolves,
    // which reads as "no releases" rather than "still loading".
    if (desktop) {
      desktop.innerHTML =
        '<div class="text-center py-4 text-muted small">' +
        '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>' +
        'Loading releases…</div>';
    }

    try {
      // Delegated to UpcomingReleasesService so the dashboard table and the
      // dedicated page share one fetch + render path.
      const data = await global.UpcomingReleasesService.fetchReleases({
        filter: tableFilter,
        include_queue: true,
        limit: 50,
        page: 1,
      });
      const opts = { emptyMessage: 'No upcoming releases found.' };
      // TWO presentations of ONE fetch: a sortable table at lg+, and a
      // horizontal ribbon below it. They used to share the animated
      // accordion renderer, which is why the phone got an accordion inside a
      // vertically-scrolling page — the swipe moved the page, not the list.
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

  // ── Recently Added / Recent Scans tabs ──────────────────────────────────
  //
  // The two historical lists share ONE card (see dashboard.html), so the tab
  // choice is remembered — same sessionStorage pattern as dashboardTableFilter
  // below — or every reload would bounce the user back to "Recently Added".
  //
  // Switching goes through Bootstrap's own Tab API rather than flipping
  // classes: hand-toggling `.active` would desync the panes from the buttons'
  // aria-selected state and from the fade transition.
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

    // The hint must match whichever tab is active AFTER the restore above.
    const activeBtn = tabs.querySelector('.nav-link.active');
    setHint(activeBtn && activeBtn.getAttribute('data-bs-target'));
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function updateAll() {
    return Promise.all([
      updateActiveScans(),
      updateRecentScans(),
      updatePendingApproval(),
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
    renderPendingApproval,
    updatePendingApproval,
    loadUpcomingReleasesTable,
    setUpcomingTableFilter,
    formatElapsed,
    scanDisplayName,
  };

  // Globals for the inline onclick handlers in dashboard.html.
  //
  // Only the two survivor families are published now. The eight scan-starer /
  // poller exports that used to sit here were removed with the functions they
  // pointed at — exporting a name for a deleted function is worse than not
  // exporting it, because `dashboard.html`'s `onclick` would then fail at
  // CLICK time with a bare ReferenceError instead of at load time.
  global.stopAllScans = stopAllScans;
  global.setUpcomingTableFilter = setUpcomingTableFilter;
  global.loadUpcomingReleasesTable = loadUpcomingReleasesTable;
})(window);
