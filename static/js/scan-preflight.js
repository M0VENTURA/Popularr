/* ==========================================================================
   static/js/scan-preflight.js
   "A scan is already running" pre-flight gate.
   (Live tree copy of test_site/static/js/services/scan-preflight.js.)

   ── WHY THIS EXISTS ───────────────────────────────────────────────────────
   Every scan-start path is guarded server-side: a second scan is rejected as a
   duplicate (`409 "A popularity scan is already running"`). But the user only
   discovered that AFTER the fact, as a failure toast — and the guard is
   deliberately conservative (it also treats a scan in another hypercorn worker
   as running), so a start that "should" work could simply refuse.

   This module asks FIRST. When a scan is already running it names it, offers to
   cancel it and start the new one, and only then lets the caller proceed.

   ── THE HARD PART: STOPPING IS ASYNCHRONOUS ───────────────────────────────
   `/scan/stop-all` only sets a flag. The running scan checks `is_stop_requested`
   BETWEEN artists/albums, so it keeps running for a moment after the stop is
   accepted. Firing the new start immediately would hit the server's duplicate
   guard and fail — which is exactly the confusing outcome this module exists to
   prevent. So after cancelling we POLL `/api/scan-progress` until nothing is
   running, and only then return "proceed".

   If it does not stop inside the budget we ABORT and say so, rather than
   starting a scan that the server is about to reject.

   ── FAILING OPEN ──────────────────────────────────────────────────────────
   If the status probe itself fails, we PROCEED. Blocking a legitimate scan
   start because a status endpoint hiccuped would be worse than the original
   problem. The server-side guard still protects correctness.

   Load AFTER ui/confirm.js where available. The live tree has no ui/confirm.js,
   so the native `confirm()` is used there — both paths are supported.
   ========================================================================== */

(function (global) {
  'use strict';

  const PROGRESS_URL = '/api/scan-progress';
  const STOP_ALL_URL = '/scan/stop-all';

  /** How long to wait for a cancelled scan to actually stop. */
  const STOP_WAIT_MS = 20000;
  const POLL_INTERVAL_MS = 700;

  /**
   * Human labels for every progress scan_type.
   *
   * Deliberately OWNED here rather than read from the dashboard: this module
   * must label a scan on the ARTIST page too, which does not load dashboard.js.
   * The keys match `services/scanning/pipelines/progress_service.py::SCAN_TYPES`
   * plus the secondary rows the orchestrators write.
   */
  const SCAN_LABELS = {
    full_scan: 'Full Scan',
    library_scan: 'Library Scan',
    navidrome_scan: 'Navidrome Import',
    popularity_scan: 'Popularity Scan',
    singles_scan: 'Singles Detection',
    metadata_lookup_scan: 'Metadata Scan',
    essentia_mood_scan: 'Essentia Mood Scan',
    combined_scan: 'Combined Scan',
    missing_releases_scan: 'Missing Releases Scan',
    mp3_import: 'MP3 Import',
  };

  function label(type) {
    const key = String(type || '').trim();
    if (!key) return 'Scan';
    if (SCAN_LABELS[key]) return SCAN_LABELS[key];
    // Unknown type: "some_new_scan" -> "Some New Scan" rather than a raw key.
    return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  /** "Full Scan — Madball (42%)" — the type, the artist, the progress. */
  function describe(scan) {
    if (!scan || typeof scan !== 'object') return 'Scan';
    const parts = [label(scan.scan_type)];

    const artist = String(scan.current_artist || '').trim();
    const album = String(scan.current_album || '').trim();
    if (artist && album) parts.push(`— ${artist} › ${album}`);
    else if (artist) parts.push(`— ${artist}`);

    const pct = Number(scan.percent_complete);
    if (Number.isFinite(pct) && pct > 0) parts.push(`(${Math.round(pct)}%)`);

    return parts.join(' ');
  }

  function esc(value) {
    if (global.escapeHtml) return global.escapeHtml(value);
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(message, kind) {
    if (kind === 'error') {
      if (global.toast && typeof global.toast.error === 'function') {
        return global.toast.error(message);
      }
      if (typeof global.showToast === 'function') {
        return global.showToast('Scan', message, 'error');
      }
      if (typeof global.alert === 'function') return global.alert(message);
      return;
    }
    if (global.toast && typeof global.toast.success === 'function') {
      return global.toast.success(message);
    }
    if (typeof global.showToast === 'function') {
      return global.showToast('Scan', message, 'success');
    }
  }

  /** Read the unified progress payload. Throws only on a transport failure. */
  async function fetchProgress() {
    const response = await fetch(PROGRESS_URL, {
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin',
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  }

  /** The scans currently running, newest-first order as the server sends them. */
  async function fetchRunningScans() {
    let data;
    try {
      data = await fetchProgress();
    } catch (_error) {
      // Fail open: see the module header. `null` tells the caller we could not
      // find out, which is different from "nothing is running".
      return null;
    }
    return (data && Array.isArray(data.active_scans))
      ? data.active_scans.filter((s) => s && s.is_running)
      : [];
  }

  /** Ask the user to confirm cancelling. Resolves true when they accept. */
  function askToCancel(items) {
    const message =
      'A scan is currently running. Would you like to cancel this scan and ' +
      'start the new scan?';

    // test_site tree: themed modal, supports a bulleted list.
    if (global.ui && typeof global.ui.confirm === 'function') {
      return global.ui.confirm({
        title: 'A scan is already running',
        message,
        items,
        detail: 'The running scan is stopped before the new one starts.',
        tone: 'warning',
        confirmLabel: 'Cancel scan & start new',
        cancelLabel: 'Leave it running',
      });
    }

    // Live tree (no ui/confirm.js): native dialog.
    const text = [message, '', 'Running:', ...items.map((i) => `  • ${i}`)].join('\n');
    return Promise.resolve(global.confirm(text));
  }

  /** Ask the server to stop every scan family. */
  async function requestStopAll() {
    const response = await fetch(STOP_ALL_URL, {
      method: 'POST',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: '{}',
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json().catch(() => ({}));
  }

  /** Poll until no scan is running. Resolves true when it went idle. */
  async function waitForIdle(timeoutMs) {
    const budget = Number.isFinite(timeoutMs) ? timeoutMs : STOP_WAIT_MS;
    const deadline = Date.now() + budget;

    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      const running = await fetchRunningScans();
      // A failed probe while waiting must not be read as "idle" — keep waiting.
      if (running === null) continue;
      if (running.length === 0) return true;
    }
    return false;
  }

  /**
   * The gate every scan-start path calls.
   *
   * @param {Object} [opts]
   * @param {string} [opts.scanName]  what the CALLER is about to start, for
   *                                  messages (e.g. "Popularity Scan")
   * @param {number} [opts.timeoutMs] override the stop-wait budget
   * @returns {Promise<boolean>} true = go ahead and start; false = do nothing
   */
  async function confirmIfRunning(opts = {}) {
    const running = await fetchRunningScans();

    // Could not tell, or nothing running -> proceed (fail open).
    if (running === null || running.length === 0) return true;

    const items = running.map(describe);
    const accepted = await askToCancel(items);
    if (!accepted) return false;

    try {
      await requestStopAll();
    } catch (error) {
      toast('Could not stop the running scan: ' + error.message, 'error');
      return false;
    }

    const idle = await waitForIdle(opts.timeoutMs);
    if (!idle) {
      toast(
        'The running scan is still stopping. Wait a moment, then start the new scan.',
        'error'
      );
      return false;
    }
    return true;
  }

  /**
   * Intercept a scan <form> so its submit goes through the gate.
   *
   * Needed because the artist page starts scans with a plain POST form, which
   * has no JavaScript path to hook.
   *
   * ⚠️ `form.submit()` does NOT dispatch a submit event (that is
   * `form.requestSubmit()`), so re-submitting here cannot loop and needs no
   * "already cleared" flag. An earlier draft set one anyway; because nothing
   * consumed it, it stayed set and silently SKIPPED the gate on the user's next
   * click. Do not reintroduce that.
   *
   * `_inFlight` is a re-entrancy guard for the real hazard: a user
   * double-clicking Run while the dialog is open would otherwise open two.
   */
  const _inFlight = new WeakSet();

  function attachFormGuard(formOrId, opts = {}) {
    const form = typeof formOrId === 'string'
      ? document.getElementById(formOrId)
      : formOrId;
    if (!form || form.dataset.scanPreflightAttached === '1') return false;
    form.dataset.scanPreflightAttached = '1';

    form.addEventListener('submit', function (event) {
      event.preventDefault();

      if (_inFlight.has(form)) return;
      _inFlight.add(form);

      const gate = global.ScanPreflight
        ? global.ScanPreflight.confirmIfRunning(opts)
        : Promise.resolve(true);

      gate.then((proceed) => {
        _inFlight.delete(form);
        if (!proceed) return;
        // Not `requestSubmit()` — that would re-enter this listener.
        form.submit();
      }).catch(() => {
        // A failure inside the gate must not strand the form; the server-side
        // duplicate guard is still there to reject a genuine double start.
        _inFlight.delete(form);
        form.submit();
      });
    });

    return true;
  }

  /**
   * Auto-attach to declaratively marked forms.
   *
   * Scan forms are server-rendered and submitted by the browser, so there is no
   * page JS to edit. A form opts in with `data-scan-preflight` (and may set
   * `data-scan-preflight-name` for nicer wording). This keeps the wiring in one
   * place instead of adding a DOMContentLoaded block to every page.
   */
  function attachDeclaredForms() {
    document.querySelectorAll('form[data-scan-preflight]').forEach((form) => {
      attachFormGuard(form, {
        scanName: form.getAttribute('data-scan-preflight-name') || '',
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attachDeclaredForms);
  } else {
    // Script loaded after DOM parsed (deferred / end of body).
    attachDeclaredForms();
  }

  global.ScanPreflight = {
    confirmIfRunning,
    attachFormGuard,
    attachDeclaredForms,
    fetchRunningScans,
    describe,
    label,
    SCAN_LABELS,
    esc,
  };
})(window);
