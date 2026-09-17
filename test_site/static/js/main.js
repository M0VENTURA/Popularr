/* ==========================================================================
   static/js/main.js
   App shell — runs on every page.

   Navbar height sync, mobile tab engine, unified log viewer, sticky scan
   bar, sticky save bar, slide-over, MusicBrainz release picker, bookmarks.

   Load order:
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/modal.js  →  ui/confirm.js
       main.js

   ── WHAT WAS REMOVED ──────────────────────────────────────────────────────
     escapeHtml                              → utils/dom.js
     showToast, showQueueToast, showTopToast,
     _toastTypeFromMessage, _logClientMessage,
     the window.alert override                → ui/toast.js
     toggleTrackFavourite, toggleAlbumFavourite
                                              → deleted (favourites removed)
     openGlobalMbSearch                       → services/mb-search.js
     searchMusicBrainzReleaseFromEncoded      → services/musicbrainz.js

   ── IMPORTANT: THE MB SEARCH CALLBACK CONTRACT CHANGED ────────────────────
   main.js's openGlobalMbSearch stashed the caller's callback on
   `window._mbSearchCallback` for the search component to invoke.
   services/mb-search.js keeps it in module scope instead, and fires it from
   confirmReleaseSelection() — i.e. after the user confirms, not on first
   click.

   If _musicbrainz_search_component.html still reads
   `window._mbSearchCallback` anywhere, that read is now dead: it will be
   undefined and the selection will silently do nothing. Grep the component
   for `_mbSearchCallback` and route it through
   `window.confirmReleaseSelection()` instead.

   Two files also defined openGlobalMbSearch (here and downloads.js). They
   were NOT equivalent — this one never populated the confirmation panel,
   the other never cleared stale form fields. mb-search.js is the reconciled
   version; both originals should go.

   ── BUG FIXED ─────────────────────────────────────────────────────────────
   updateUnifiedLog / updateQueueStatusBar ran on a bare 5s setInterval that
   was never cleared and never paused. On a backgrounded tab it kept issuing
   two requests every five seconds indefinitely. Now a managed poller:
   pauses when hidden, never overlaps, stops on teardown.
   ========================================================================== */

(function (global) {
  'use strict';

  const LOG_POLL_INTERVAL_MS = 5000;
  const LOG_FETCH_TIMEOUT_MS = 8000;
  const LOG_LINES = 300;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  // ── Navbar height ───────────────────────────────────────────────────────
  //
  // popularr.css ships per-breakpoint fallbacks, but the navbar is
  // flex-wrap: its search row wraps below the lg breakpoint, and the row
  // count also varies with how many nav links are rendered. Measuring is
  // exact where a static value cannot be.
  //
  // Everything consuming the token follows automatically: main's
  // padding-top, .search-flyout's top and max-height, .navbar-collapse.show,
  // and #dashboardRow.
  //
  // THIS MUST REMAIN THE ONLY WRITER. base.html briefly carried a second
  // copy; two writers for one variable is how the 68px / 96px / 112px
  // disagreement started.

  function syncNavbarHeight() {
    const nav = document.querySelector('nav.navbar.fixed-top');
    if (!nav || !nav.offsetHeight) return;
    document.documentElement.style.setProperty('--navbar-height', nav.offsetHeight + 'px');
  }

  function initNavbarHeight() {
    syncNavbarHeight();

    // A resize listener alone misses height changes that are not window
    // resizes — the mobile menu expanding, or web fonts landing after first
    // paint and reflowing the brand row.
    if (typeof ResizeObserver !== 'undefined') {
      const nav = document.querySelector('nav.navbar.fixed-top');
      if (nav) new ResizeObserver(syncNavbarHeight).observe(nav);
    } else {
      global.addEventListener('resize', syncNavbarHeight);
    }

    // Bootstrap's collapse animates, so measure once it has settled.
    const navCollapse = document.getElementById('navbarNav');
    if (navCollapse) {
      navCollapse.addEventListener('shown.bs.collapse', syncNavbarHeight);
      navCollapse.addEventListener('hidden.bs.collapse', syncNavbarHeight);
    }

    global.addEventListener('load', syncNavbarHeight);
  }

  // ── Mobile tab engine ───────────────────────────────────────────────────
  //
  // A tab bar with `data-mobile-tabs` gets:
  //   data-group-attr   section attribute grouping content
  //                     (default `data-mobile-group`)
  //   data-active-class class toggled on the active section
  //                     (default `mobile-tab-active`)
  // Sections show/hide via the active class, so each page's desktop media
  // query (all sections visible >= 992px) keeps working untouched.

  function initMobileTabs(bar, options) {
    const opts = options || {};
    const groupAttr = opts.groupAttr || 'data-mobile-group';
    const activeClass = opts.activeClass || 'mobile-tab-active';
    const buttons = Array.prototype.slice.call(bar.querySelectorAll('[data-tab]'));
    if (!buttons.length) return;

    function apply(tab, persistHash) {
      buttons.forEach((b) => {
        const isActive = b.getAttribute('data-tab') === tab;
        b.classList.toggle('active', isActive);
        b.setAttribute('aria-selected', isActive ? 'true' : 'false');
      });
      document.querySelectorAll('[' + groupAttr + ']').forEach((section) => {
        section.classList.toggle(activeClass, section.getAttribute(groupAttr) === tab);
      });
      if (persistHash && global.history && history.replaceState) {
        history.replaceState(null, '', '#' + tab);
      }
    }

    bar.addEventListener('click', (e) => {
      const btn = e.target.closest ? e.target.closest('[data-tab]') : null;
      if (!btn) return;
      e.preventDefault();
      apply(btn.getAttribute('data-tab'), true);
    });

    // Initial state: URL hash wins, else the pre-marked active button, else
    // the first tab. The hash is never written at load so desktop scrollspy
    // anchors keep working.
    const hash = global.location.hash ? global.location.hash.replace('#', '') : '';
    const initial = hash && buttons.some((b) => b.getAttribute('data-tab') === hash)
      ? hash
      : (bar.querySelector('.active') || buttons[0]).getAttribute('data-tab');
    apply(initial, false);
  }

  // ── Unified log viewer ──────────────────────────────────────────────────

  const LOG_SOURCE_FILES = {
    scanner: 'unified_scan.log',
    queue: 'queue.log',
    soulseek: 'search.log',
    navidrome: 'info.log',
    system: 'error.log',
  };

  const LOG_TAG_CLASSES = {
    INFO: 'log-tag-info', DEBUG: 'log-tag-info',
    TRACK_RESULT: 'log-tag-track-result', ALBUM_RESULT: 'log-tag-track-result',
    FINALISE_STAGE: 'log-tag-finalise', POPULARITY_STAGE: 'log-tag-finalise',
    SINGLE_DETECTION: 'log-tag-finalise', LOAD_STAGE: 'log-tag-finalise',
    ALBUM_STAGE: 'log-tag-finalise', TRACK_STAGE: 'log-tag-finalise',
    WARNING: 'log-tag-warning', WARN: 'log-tag-warning',
    ERROR: 'log-tag-error', CRITICAL: 'log-tag-error',
    QUEUE: 'log-tag-queue', QUEUE_PROCESSOR: 'log-tag-queue',
    SOULSEEK: 'log-tag-soulseek', SLSKD: 'log-tag-soulseek',
  };

  let activeLogSource = 'scanner';
  let logPaused = false;
  let logModalVisible = false;
  let logShowTimestamps = false;
  let logRawLines = [];
  let logPoller = null;

  function logTagClass(tag) {
    return LOG_TAG_CLASSES[tag] || LOG_TAG_CLASSES[String(tag).toUpperCase()] || '';
  }

  /** Escape FIRST, then wrap [TAG] tokens and ★ glyphs in styled spans. */
  function formatLogLine(line) {
    let s = esc(line);
    if (!logShowTimestamps) {
      s = s.replace(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*/, '');
    }
    s = s.replace(/\[([A-Za-z0-9_]+)\]/g, (m, tag) => {
      const cls = logTagClass(tag);
      return cls ? `<span class="log-tag ${cls}">${m}</span>` : m;
    });
    return s.replace(/(★+)/g, '<span class="log-stars">$1</span>');
  }

  function renderLogLines() {
    const logEl = document.getElementById('unifiedLog');
    if (!logEl) return;
    const html = logRawLines.map(formatLogLine).join('\n');
    logEl.innerHTML = html || '<span class="text-muted">— no output —</span>';
    if (logModalVisible && !logPaused) logEl.scrollTop = logEl.scrollHeight;
  }

  function openUnifiedLogModal() {
    const modalEl = document.getElementById('unifiedLogModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  function toggleLogPause() {
    logPaused = !logPaused;
    const pauseBtn = document.getElementById('pauseLogBtn');
    if (!pauseBtn) return;
    pauseBtn.innerHTML = logPaused
      ? '<i class="bi bi-play"></i> <span class="d-none d-sm-inline">Resume</span>'
      : '<i class="bi bi-pause"></i> <span class="d-none d-sm-inline">Pause</span>';
    pauseBtn.classList.toggle('log-pause-active', logPaused);
  }

  function toggleLogTimestamps(show) {
    logShowTimestamps = !!show;
    renderLogLines();
  }

  function clearUnifiedLog() {
    logRawLines = [];
    renderLogLines();
  }

  function switchLogSource(source) {
    activeLogSource = LOG_SOURCE_FILES[source] ? source : 'scanner';
    document.querySelectorAll('.log-source-tab').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.source === activeLogSource);
    });
    updateUnifiedLog();
  }

  function downloadActiveLogLastHour() {
    const link = document.createElement('a');
    link.href = '/api/logs/export?source=' + encodeURIComponent(activeLogSource) + '&hours=1';
    link.download = activeLogSource + '_log_last_1hr.log';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  async function updateUnifiedLog() {
    if (logPaused) return;
    const logEl = document.getElementById('unifiedLog');
    if (!logEl) return;

    try {
      const data = await global.api.getJson(
        '/api/log-file?name=' + encodeURIComponent(LOG_SOURCE_FILES[activeLogSource]) +
        '&lines=' + LOG_LINES,
        { timeoutMs: LOG_FETCH_TIMEOUT_MS }
      );
      if (data && Array.isArray(data.lines)) {
        logRawLines = data.lines;
        renderLogLines();
      }
    } catch (_error) {
      // A hung or failed poll must not freeze the panel; the next tick retries.
    }
  }

  function setLogTabDot(source, active) {
    const dot = document.querySelector(
      '.log-source-tab[data-source="' + source + '"] .log-tab-dot'
    );
    if (!dot) return;
    dot.classList.toggle('d-none', !active);
    dot.classList.toggle('log-tab-dot-active', active);
  }

  async function updateQueueStatusBar() {
    const el = document.getElementById('scanQueueSummary');
    if (!el) return;

    try {
      const data = await global.api.getJson('/api/queue/status?limit=1');
      const counts = data.counts || {};
      const downloading = Number(counts.downloading) || 0;
      const queued = Number(counts.queued) || 0;
      const activeTotal = Number(data.total_active) || 0;

      setLogTabDot('queue', activeTotal > 0);
      setLogTabDot('soulseek', downloading > 0);

      if ((downloading + queued) > 0) {
        el.innerHTML = '<i class="bi bi-cloud-arrow-down me-1"></i>' +
          `${downloading} Downloading · ${queued} Queued`;
        el.classList.remove('d-none');
      } else {
        el.classList.add('d-none');
      }
    } catch (_error) {
      /* transient — next tick retries */
    }
  }

  /**
   * Context-aware default tab when the modal opens: a running scan wins,
   * then active Soulseek downloads, otherwise keep the last-viewed source.
   */
  async function smartSelectLogSource() {
    try {
      const data = await global.api.getJson('/api/scan-progress?_ts=' + Date.now());
      if ((data.active_scans || []).some((s) => s.is_running)) {
        switchLogSource('scanner');
        return;
      }
      const queue = await global.api.getJson('/api/queue/status?limit=1');
      if ((Number((queue.counts || {}).downloading) || 0) > 0) {
        switchLogSource('soulseek');
      }
    } catch (_error) {
      /* keep the current source */
    }
  }

  // ── Sticky scan bar ─────────────────────────────────────────────────────

  /** The dashboard's own polling (dashboard.js) owns the bar there. */
  function updateGlobalScanBar(data) {
    if (document.getElementById('dashboardFlags')) return;

    const bar = document.getElementById('scanStatusBar');
    const line = document.getElementById('scanStatusLine');
    const icon = document.getElementById('scanStatusIcon');
    if (!bar || !line || !icon) return;

    const active = (data && (data.active_scans || [])) || [];
    const scan = active.find((s) => s && s.is_running);
    setLogTabDot('scanner', !!scan);

    if (!scan) {
      line.textContent = 'Idle';
      icon.className = 'scan-status-idle';
      icon.innerHTML = '<i class="bi bi-circle"></i>';
      return;
    }

    const pct = Math.min(scan.percent_complete ?? scan.progress ?? 0, 100);
    const name = String(scan.scan_type || 'scan').replace(/_/g, ' ');
    const stage = scan.current_stage ? ` · ${scan.current_stage}` : '';
    line.textContent = `${name} — ${pct}%${stage}` +
      (scan.current_item ? ` · ${scan.current_item}` : '');
    icon.className = 'scan-status-active';
    icon.innerHTML = '<i class="bi bi-activity"></i>';
  }

  function initScanStream() {
    if (typeof EventSource === 'undefined') return;
    try {
      const source = new EventSource('/api/scan-progress/stream');
      source.addEventListener('message', (e) => {
        if (!e.data) return;
        try {
          updateGlobalScanBar(JSON.parse(e.data));
        } catch (_) {
          /* malformed frame — ignore */
        }
      });
    } catch (_) {
      /* SSE unavailable — dashboard polling still covers progress */
    }
  }

  // ── Sticky save bar ─────────────────────────────────────────────────────
  //
  // Forms opt in with `data-sticky-save`. Any input/change marks the form
  // dirty and slides up a fixed bottom bar. JS-driven edits (chip inputs,
  // quick-fill buttons) call markFormDirty(formId).

  function markFormDirty(formId) {
    const form = document.getElementById(formId);
    if (form && form._setDirty) form._setDirty(true);
  }

  function initStickySaveBar(form) {
    let bar = document.getElementById('stickySaveBar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'stickySaveBar';
      bar.className = 'sticky-save-bar d-none';
      bar.innerHTML =
        '<div class="sticky-save-bar-inner">' +
          '<span class="sticky-save-bar-msg text-warning small">' +
            '<i class="bi bi-exclamation-circle me-1"></i>You have unsaved metadata changes</span>' +
          '<div class="d-flex gap-2">' +
            '<button type="button" class="btn btn-outline-secondary btn-sm" data-discard>' +
              '<i class="bi bi-x-lg me-1"></i>Discard</button>' +
            '<button type="button" class="btn btn-success btn-sm" data-save>' +
              '<i class="bi bi-check-lg me-1"></i>Save Metadata</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(bar);
    }

    const setDirty = (dirty) => {
      if (dirty === form._dirty) return;
      form._dirty = dirty;
      bar.classList.toggle('d-none', !dirty);
    };

    form._setDirty = setDirty;
    form.addEventListener('input', () => setDirty(true));
    form.addEventListener('change', () => setDirty(true));
    form.addEventListener('submit', () => setDirty(false));

    // The bar is shared by every opted-in form on the page, but its buttons
    // are wired once — so with two such forms the second wiring would also
    // reset/submit the first. `_boundForm` keeps the buttons pointed at
    // whichever form most recently went dirty.
    bar._boundForm = bar._boundForm || form;
    form.addEventListener('input', () => { bar._boundForm = form; });
    form.addEventListener('change', () => { bar._boundForm = form; });

    if (bar._actionsWired) return;
    bar._actionsWired = true;

    bar.querySelector('[data-discard]').addEventListener('click', () => {
      const target = bar._boundForm;
      if (!target) return;
      target.reset();
      if (target._setDirty) target._setDirty(false);
    });
    bar.querySelector('[data-save]').addEventListener('click', () => {
      const target = bar._boundForm;
      if (!target) return;
      if (typeof target.requestSubmit === 'function') target.requestSubmit();
      else target.submit();
    });
  }

  // ── Slide-over ──────────────────────────────────────────────────────────

  function openSlideOver(url, title) {
    const el = document.getElementById('detailSlideOver');
    const contentEl = document.getElementById('slideOverContent');
    const titleEl = document.getElementById('slideOverTitle');
    if (!el || !contentEl) return;

    if (titleEl) titleEl.textContent = title || 'Loading…';
    contentEl.innerHTML =
      '<div class="d-flex justify-content-center py-5"><div class="spinner-border text-primary"></div></div>';

    if (global.bootstrap && global.bootstrap.Offcanvas) {
      new global.bootstrap.Offcanvas(el).show();
    }

    fetch(url)
      .then((r) => r.text())
      .then((html) => { contentEl.innerHTML = html; })
      .catch((err) => {
        contentEl.innerHTML =
          `<div class="alert alert-danger m-3">Error loading details: ${esc(err.message)}</div>`;
      });
  }

  // ── MusicBrainz release picker ──────────────────────────────────────────
  //
  // Opens the /api/musicbrainz/release-picker flyout for a release GROUP so
  // the user can choose the exact physical release (15-track CD, 18-track
  // deluxe, 5-track promo, …) before anything is queued. Queueing a chosen
  // version posts its concrete release MBID, which resolve_release_id
  // accepts as-is (no re-resolution to the biggest official release).

  let releasePickerOnQueued = null;

  async function openReleasePicker(releaseGroupId, title, artist, onQueued) {
    if (!releaseGroupId) {
      global.toast.error('Missing release group ID');
      return;
    }

    releasePickerOnQueued = typeof onQueued === 'function' ? onQueued : null;

    const url = '/api/musicbrainz/release-picker?rg_id=' + encodeURIComponent(releaseGroupId) +
      '&artist=' + encodeURIComponent(artist || '') +
      '&album=' + encodeURIComponent(title || '');

    // Probe the group first: exactly ONE release is queued directly (no
    // flyout); multi-version groups open the picker so the user can choose
    // the exact edition.
    try {
      const data = await global.api.getJson(url + '&format=json');
      const releases = (data && Array.isArray(data.releases)) ? data.releases : null;
      if (releases && releases.length === 1) {
        const rel = releases[0];
        return queueSpecificRelease(rel.id, rel.title || title || '', artist || '');
      }
    } catch (_error) {
      // Network failure → fall through to the flyout, which surfaces the error.
    }

    openSlideOver(url, 'Select Version: ' + (title || ''));
  }

  async function queueSpecificRelease(releaseId, releaseTitle, artist) {
    if (!releaseId) return;

    try {
      const data = await global.api.postJson('/api/musicbrainz/download', {
        release_id: releaseId,
        release_title: releaseTitle,
        artist,
        method: 'slskd',
        queue_items_only: true,
      });

      if (!data.success && !data.tracking_id) {
        global.toast.error('Error queueing release: ' + (data.error || 'Unknown error'));
        return;
      }

      global.toast.queued(releaseTitle);

      const cb = releasePickerOnQueued;
      releasePickerOnQueued = null;
      if (typeof cb === 'function') {
        try { cb(releaseId); } catch (_e) { /* caller's problem */ }
      }

      const slideOverEl = document.getElementById('detailSlideOver');
      if (slideOverEl && global.bootstrap) {
        const inst = global.bootstrap.Offcanvas.getInstance(slideOverEl);
        if (inst) inst.hide();
      }
    } catch (error) {
      global.toast.error('Error queueing release: ' + error.message);
    }
  }

  function toggleReleaseTracklistPreview(releaseId) {
    const el = document.getElementById('preview-rel-' + releaseId);
    if (!el) return;

    if (!el.classList.contains('d-none')) {
      el.classList.add('d-none');
      return;
    }

    el.classList.remove('d-none');
    el.innerHTML =
      '<div class="text-center py-2"><span class="spinner-border spinner-border-sm" role="status"></span></div>';

    fetch('/api/musicbrainz/release-picker?release_id=' + encodeURIComponent(releaseId))
      .then((r) => r.text())
      .then((html) => { el.innerHTML = html; })
      .catch(() => {
        el.innerHTML = '<div class="text-danger small">Failed to load tracklist.</div>';
      });
  }

  /**
   * Queue a release via Soulseek, for pages that do not load downloads.js
   * (artist / album pages). Mirrors downloads.js's addMbDownloadToSession.
   */
  async function downloadReleaseViaSoulseek(releaseId, title, artist) {
    const confirmFn = (global.ui && global.ui.confirm)
      ? global.ui.confirm
      : (opts) => Promise.resolve(global.confirm(opts.message));

    const accepted = await confirmFn({
      title: 'Download release',
      message: `Download "${title}" by ${artist} via Soulseek?`,
      tone: 'primary',
      confirmLabel: 'Download',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson('/api/musicbrainz/download', {
        release_id: releaseId,
        release_title: title,
        artist,
        method: 'slskd',
        persistent_search: false,
        max_retries: 3,
        session_id: null,
        queue_items_only: true,
      });
      if (data.error) {
        global.toast.error('Error: ' + data.error);
        return;
      }
      global.toast.success(
        `Download queued: ${title}` + (data.tracking_id ? ` (tracking ${data.tracking_id})` : '')
      );
    } catch (error) {
      global.toast.error('Error: ' + error.message);
    }
  }

  // ── Bookmarks ───────────────────────────────────────────────────────────

  async function addBookmark(type, name, artist, album, trackId) {
    try {
      const data = await global.api.postJson('/api/bookmarks', {
        type, name, artist, album, track_id: trackId,
      });
      if (!data.success) {
        global.toast.error(data.error || 'Could not add bookmark');
        return;
      }
      global.toast.success(`Bookmarked "${name}"`);
    } catch (error) {
      global.toast.error('Error: ' + error.message);
    }
  }

  // ── Nav search ──────────────────────────────────────────────────────────

  function navSearch(event) {
    if (event && event.preventDefault) event.preventDefault();

    const form = (event && event.target) ? event.target : null;
    const activeInput = form
      ? form.querySelector('input[type="text"], input[type="search"]')
      : null;
    const fallbackInput = document.getElementById('navSearchInput');
    const dashInput = document.getElementById('dashboardTopSearchInput');

    const query = (
      (activeInput && activeInput.value) ||
      (fallbackInput && fallbackInput.value) ||
      (dashInput && dashInput.value) || ''
    ).trim();

    if (!query) return;

    if (typeof global.openUnifiedSearch === 'function') {
      global.openUnifiedSearch('all', query);
      return;
    }
    global.location.href = '/search?q=' + encodeURIComponent(query);
  }

  // ── Init ────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    initNavbarHeight();
    initScanStream();

    if (global.bootstrap && global.bootstrap.Tooltip) {
      document.querySelectorAll('[data-bs-toggle="tooltip"]')
        .forEach((el) => new global.bootstrap.Tooltip(el));
    }

    document.querySelectorAll('[data-mobile-tabs]').forEach((bar) => {
      initMobileTabs(bar, {
        groupAttr: bar.getAttribute('data-group-attr') || 'data-mobile-group',
        activeClass: bar.getAttribute('data-active-class') || 'mobile-tab-active',
      });
    });

    document.querySelectorAll('form[data-sticky-save]').forEach(initStickySaveBar);

    const logModalEl = document.getElementById('unifiedLogModal');
    if (logModalEl) {
      logModalEl.addEventListener('shown.bs.modal', () => {
        logModalVisible = true;
        smartSelectLogSource();
        updateUnifiedLog();
      });
      logModalEl.addEventListener('hidden.bs.modal', () => { logModalVisible = false; });
    }

    // Managed poller: pauses when the tab is hidden, never overlaps, and is
    // released on teardown. The previous bare setInterval kept firing two
    // requests every five seconds in a background tab, forever.
    logPoller = global.poller.create({
      interval: LOG_POLL_INTERVAL_MS,
      onTick: async () => {
        if (logModalVisible) await updateUnifiedLog();
        await updateQueueStatusBar();
      },
    });
    logPoller.start();

    // Routes the ~120 remaining alert() call sites through a themed toast.
    if (global.toast && global.toast.installAlertBridge) {
      global.toast.installAlertBridge();
    }
  });

  global.popularr = {
    syncNavbarHeight,
    initMobileTabs,
    openSlideOver,
    openReleasePicker,
    queueSpecificRelease,
    markFormDirty,
    updateGlobalScanBar,
  };

  // Globals for the inline onclick / onsubmit handlers in base.html.
  global.markFormDirty = markFormDirty;
  global.openSlideOver = openSlideOver;
  global.openReleasePicker = openReleasePicker;
  global.queueSpecificRelease = queueSpecificRelease;
  global.toggleReleaseTracklistPreview = toggleReleaseTracklistPreview;
  global.downloadReleaseViaSoulseek = downloadReleaseViaSoulseek;
  global.addBookmark = addBookmark;
  global.navSearch = navSearch;
  global.openUnifiedLogModal = openUnifiedLogModal;
  global.toggleLogPause = toggleLogPause;
  global.toggleLogTimestamps = toggleLogTimestamps;
  global.clearUnifiedLog = clearUnifiedLog;
  global.switchLogSource = switchLogSource;
  global.downloadActiveLogLastHour = downloadActiveLogLastHour;
  global.updateUnifiedLog = updateUnifiedLog;
})(window);
