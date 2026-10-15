/* ==========================================================================
   js/services/activity-feed.js
   The Recent Activity feed — shared by the dashboard card and the Activity page.

   ── WHY THIS MODULE EXISTS ────────────────────────────────────────────────
   The dashboard's activity card and the full Activity page show the SAME
   three feeds (recently added / recent scans / needs review). Their renderers
   used to live inside `pages/dashboard.js`'s IIFE closure, where the page
   could not reach them, so the page would have needed a second copy.

   ⭐ TWO COPIES OF A RENDERER IS HOW THEY DRIFT. The dashboard already has
   one such pair in this area — the `formatScanPercent` comment in main.js
   says in as many words "Kept identical to the copy in main.js — do not let
   them drift" — and a drift here means the same scan renders differently
   depending on which surface you opened. So the feed is a module, and
   `pages/dashboard.js` delegates to it.

   ⚠️ WHAT IS *NOT* HERE, ON PURPOSE. The pending-approval renderer stays in
   `pages/dashboard.js`, because it does something the page must not: it hides
   and reveals the "Needs Review" TAB when the count is zero. That is a
   property of the dashboard's tab strip, not of the feed. Both surfaces render
   the same ROWS (this module's `approvalRow`), and each decides where they go.

   Load order:  utils/dom.js  →  services/activity-feed.js  →  pages/*.js
   Exposes:     global.ActivityFeed
   ========================================================================== */

(function (global) {
  'use strict';

  /**
   * Scan-type → human label.
   *
   * ⚠️ EVERY key here is a REAL `scan_type` from the backend, and the aliases
   * are load-bearing rather than tidiness: progress rows, scan_history rows and
   * the pipeline's own progress-file names do NOT use one vocabulary. A missing
   * key renders the raw type, which is how "full_scan" appeared in the panel
   * instead of "Full Scan".
   */
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
    // (services/scanning/pipelines/popularity_pipeline.py). Without this key
    // the panel fell through to the raw scan_type.
    full_scan: 'Full Scan',
    library_scan: 'Library Scan',
    missing_releases_scan: 'Missing Releases Scan',
  };

  function scanDisplayName(type) {
    return SCAN_TYPE_DISPLAY_NAMES[type] || String(type || 'Scan');
  }

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  // ── Timestamps ──────────────────────────────────────────────────────────

  /**
   * Parse a scan timestamp, tolerating anything.
   *
   * ⚠️ Returns the EPOCH for unparseable input rather than null, and callers
   * test `getTime() === 0` for "unknown". A null return would need a separate
   * guard at every use, and the one place that forgot it would render
   * "NaN ago".
   */
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

  // ── Recent scans ────────────────────────────────────────────────────────

  /**
   * Collapse a flat scan_history list into one entry per (artist, album).
   *
   * ⭐ A SCAN PASS WRITES ONE ROW PER PHASE, so a single album scan arrives as
   * several rows (metadata, popularity, singles…). Rendering them individually
   * buries the artist under its own phases; grouping them is what makes the
   * feed readable, and the phases become the "types" line.
   *
   * The status is the WORST of the rows: one failed phase means the album did
   * not finish, however many other phases completed. Ordering the comparisons
   * the other way round would report a half-finished scan as a success.
   *
   * In-progress scans are NOT grouped — they are live, they have no completion
   * status, and they pin to the top so a running scan is never scrolled out of
   * sight by history sorting above it.
   */
  function groupRecentScans(scans) {
    const grouped = Object.create(null);
    const inProgress = [];

    (scans || []).forEach((scan) => {
      // Field-name tolerance: progress rows and scan_history rows disagree
      // about what a timestamp is called.
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
      // Worst-status-wins. See the docstring: a completed phase must not
      // overwrite a failed one.
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

  function statusBadge(entry) {
    if (entry._inProgress) return '<span class="badge bg-info">scanning</span>';
    if (global.statusBadge) return global.statusBadge.render(entry.status, { icon: false });
    return `<span class="badge bg-secondary">${esc(entry.status || '')}</span>`;
  }

  /**
   * One grouped scan entry as HTML.
   *
   * ⚠️ THE ROWS ARE CLICKABLE, and that is not cosmetic: an earlier version
   * emitted plain text with no anchors, so recent scans were inert on the
   * rebuilt dashboard while the live one linked through — the reported "the
   * artists and albums on recent scans aren't selectable by clicking them".
   *
   * Session rows (`_SCAN_SESSION_`) are not about one artist, so they get no
   * link. `encodeURIComponent` per segment keeps a slash or a "#" in a name
   * from breaking the path.
   */
  function scanRow(entry) {
    const badge = statusBadge(entry);

    // Distinct phase names only — one album scan repeats "Metadata Scan" across
    // several rows and "Metadata Scan, Metadata Scan" reads as a bug.
    const types = entry.scan_types
      .map((t) => esc(scanDisplayName(t.type)))
      .filter((v, i, arr) => arr.indexOf(v) === i)
      .join(', ');

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
  }

  /**
   * Render the scan feed INTO a container.
   *
   * Takes the container rather than looking up `#recent-scans-body`, so the
   * dashboard's card and the Activity page can each own their own element
   * without this module knowing about either.
   */
  function renderScansInto(container, scans, emptyText) {
    if (!container) return;

    if (!scans || !scans.length) {
      container.innerHTML = `<div class="p-3 text-center text-muted small">${
        esc(emptyText || 'No recent scan history.')
      }</div>`;
      return;
    }

    container.innerHTML = groupRecentScans(scans).map(scanRow).join('');
  }

  // ── Needs review ────────────────────────────────────────────────────────

  /**
   * One album awaiting a review decision, as an `<a>` to the album page.
   *
   * ⭐ THE LIST IS A WORK QUEUE, NOT A LOG. An album leaves it by itself once
   * its review is saved or DISCARDED, because both paths clear the stash the
   * scan wrote (`tracks.pending_mb_updates`) — so there is nothing to re-scan
   * and no "approved" bookkeeping to keep in sync.
   *
   * Exposed as a row rather than a list renderer because the two surfaces
   * need different containers AND different behaviour when the list is empty
   * (the dashboard hides a tab).
   */
  function approvalRow(album) {
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
  }

  function renderApprovalsInto(container, albums, emptyText) {
    if (!container) return;

    if (!albums || !albums.length) {
      container.innerHTML = `<div class="p-3 text-center text-muted small">${
        esc(emptyText || 'Nothing needs review.')
      }</div>`;
      return;
    }

    container.innerHTML = albums.map(approvalRow).join('');
  }

  global.ActivityFeed = {
    SCAN_TYPE_DISPLAY_NAMES,
    scanDisplayName,
    parseScanTimestamp,
    formatScanTimestamp,
    groupRecentScans,
    scanRow,
    renderScansInto,
    approvalRow,
    renderApprovalsInto,
  };
})(window);
