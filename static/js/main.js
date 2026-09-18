/**
 * Popularr main entry point.
 *
 * This file is the entry point for esbuild bundling.
 * All JS modules are imported here and bundled into static/dist/main.js.
 *
 * To add a new JS module:
 *   1. Create the module in static/js/
 *   2. Import it below
 *   3. Rebuild: `npm run build`
 */

// ==========================================================================
// Navbar height — single source of truth for --navbar-height
// --------------------------------------------------------------------------
// popularr.css ships per-breakpoint fallbacks, but the navbar is flex-wrap:
// its search row wraps below the lg breakpoint, and the row count also varies
// with how many nav links are rendered (custom bookmark links, the logged-in
// user menu). Measuring is therefore exact where a static value cannot be.
//
// Everything that consumes the token follows automatically: main's
// padding-top, .search-flyout's top and max-height, .navbar-collapse.show,
// and #dashboardRow.
//
// This MUST remain the only writer. base.html briefly carried a second copy
// of this logic; two writers for one variable is how the 68px / 96px / 112px
// disagreement started in the first place.
// ==========================================================================
function syncNavbarHeight() {
  const nav = document.querySelector("nav.navbar.fixed-top");
  if (!nav || !nav.offsetHeight) return;
  document.documentElement.style.setProperty("--navbar-height", nav.offsetHeight + "px");
}

document.addEventListener("DOMContentLoaded", () => {
  syncNavbarHeight();

  // A resize listener alone misses height changes that are not window
  // resizes — the mobile menu expanding, or web fonts landing after first
  // paint and reflowing the brand row.
  if (typeof ResizeObserver !== "undefined") {
    const nav = document.querySelector("nav.navbar.fixed-top");
    if (nav) new ResizeObserver(syncNavbarHeight).observe(nav);
  } else {
    window.addEventListener("resize", syncNavbarHeight);
  }

  // Bootstrap's collapse animates, so measure once it has settled.
  const navCollapse = document.getElementById("navbarNav");
  if (navCollapse) {
    navCollapse.addEventListener("shown.bs.collapse", syncNavbarHeight);
    navCollapse.addEventListener("hidden.bs.collapse", syncNavbarHeight);
  }

  window.addEventListener("load", syncNavbarHeight);

  // Initialize Bootstrap tooltips if Bootstrap loaded
  if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
    const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    [...tooltipTriggerList].map((el) => new bootstrap.Tooltip(el));
  }
});

// ==========================================================================
// Generic mobile tab engine (artist / album / track pages)
// --------------------------------------------------------------------------
// A tab bar with ``data-mobile-tabs`` gets:
//   - data-group-attr    : section attribute grouping content (default
//                          ``data-mobile-group``)
//   - data-active-class  : class toggled on the active section (default
//                          ``mobile-tab-active``)
// Sections are shown/hidden via the active class, so each page's desktop
// media query (all sections visible >= 992px) keeps working untouched.
// Active tab persists to the URL hash on user clicks only.
// ==========================================================================
function initMobileTabs(bar, options) {
  options = options || {};
  const groupAttr = options.groupAttr || 'data-mobile-group';
  const activeClass = options.activeClass || 'mobile-tab-active';
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
    if (persistHash && window.history && history.replaceState) {
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
  // the first tab.  The hash is never written at load time so desktop
  // scrollspy anchors keep working.
  const hash = window.location.hash ? window.location.hash.replace('#', '') : '';
  const initial =
    hash && buttons.some((b) => b.getAttribute('data-tab') === hash)
      ? hash
      : (bar.querySelector('.active') || buttons[0]).getAttribute('data-tab');
  apply(initial, false);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll('[data-mobile-tabs]').forEach((bar) => {
    initMobileTabs(bar, {
      groupAttr: bar.getAttribute('data-group-attr') || 'data-mobile-group',
      activeClass: bar.getAttribute('data-active-class') || 'mobile-tab-active',
    });
  });
});

// ==========================================================================
// Global log viewer + sticky scan bar (every page)
// --------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
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
});

// The sticky bottom bar and the fullscreen log modal live in base.html.
// The dashboard's own polling (dashboard.js) drives the bar there; on every
// other page the SSE stream updates it.  The modal reads per-source log
// files and can export the last hour with one tap.
// ==========================================================================
const LOG_SOURCE_FILES = {
  scanner: 'unified_scan.log',
  queue: 'queue.log',
  soulseek: 'search.log',
  navidrome: 'info.log',
  system: 'error.log',
};

let activeLogSource = 'scanner';
let logPaused = false;
let logModalVisible = false;
let logShowTimestamps = false;
let _logRawLines = [];

function openUnifiedLogModal() {
  const modalEl = document.getElementById('unifiedLogModal');
  if (!modalEl) return;
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

// ===== Log line rendering: optional timestamps + color-coded badges =====
const _LOG_TAG_CLASSES = {
  INFO: 'log-tag-info', DEBUG: 'log-tag-info',
  'TRACK_RESULT': 'log-tag-track-result', 'ALBUM_RESULT': 'log-tag-track-result',
  'FINALISE_STAGE': 'log-tag-finalise', 'POPULARITY_STAGE': 'log-tag-finalise',
  'SINGLE_DETECTION': 'log-tag-finalise', 'LOAD_STAGE': 'log-tag-finalise',
  'ALBUM_STAGE': 'log-tag-finalise', 'TRACK_STAGE': 'log-tag-finalise',
  'WARNING': 'log-tag-warning', 'WARN': 'log-tag-warning',
  'ERROR': 'log-tag-error', 'CRITICAL': 'log-tag-error',
  'QUEUE': 'log-tag-queue', 'QUEUE_PROCESSOR': 'log-tag-queue',
  'SOULSEEK': 'log-tag-soulseek', 'SLSKD': 'log-tag-soulseek',
};

function _logTagClass(tag) {
  return _LOG_TAG_CLASSES[tag] || _LOG_TAG_CLASSES[String(tag).toUpperCase()] || '';
}

// Escape first, then wrap [TAG] tokens and ★ glyphs in styled spans (XSS-safe).
function _formatLogLine(line) {
  let s = window.escapeHtml(line);
  if (!logShowTimestamps) {
    s = s.replace(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*/, '');
  }
  s = s.replace(/\[([A-Za-z0-9_]+)\]/g, (m, tag) => {
    const cls = _logTagClass(tag);
    return cls ? `<span class="log-tag ${cls}">${m}</span>` : m;
  });
  return s.replace(/(★+)/g, '<span class="log-stars">$1</span>');
}

function renderLogLines() {
  const logEl = document.getElementById('unifiedLog');
  if (!logEl) return;
  let html = '';
  for (const line of _logRawLines) html += _formatLogLine(line) + '\n';
  logEl.innerHTML = html || '<span class="text-muted">— no output —</span>';
  if (logModalVisible && !logPaused) logEl.scrollTop = logEl.scrollHeight;
}

function toggleLogPause() {
  logPaused = !logPaused;
  const pauseBtn = document.getElementById('pauseLogBtn');
  if (pauseBtn) {
    pauseBtn.innerHTML = logPaused ? '<i class="bi bi-play"></i> <span class="d-none d-sm-inline">Resume</span>' : '<i class="bi bi-pause"></i> <span class="d-none d-sm-inline">Pause</span>';
    pauseBtn.classList.toggle('log-pause-active', logPaused);
  }
}

function toggleLogTimestamps(show) {
  logShowTimestamps = !!show;
  renderLogLines();
}

function clearUnifiedLog() {
  _logRawLines = [];
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

function updateUnifiedLog() {
  if (logPaused) return;
  const logEl = document.getElementById('unifiedLog');
  if (!logEl) return;

  // Self-healing poll: a hung request must not freeze the log panel forever.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);

  fetch('/api/log-file?name=' + encodeURIComponent(LOG_SOURCE_FILES[activeLogSource]) + '&lines=300', { signal: controller.signal })
    .then((r) => r.json())
    .then((data) => {
      // FIXED: this was `Array.isArray(data && data.lines)`, which passes the
      // whole `data && data.lines` EXPRESSION to isArray — that is just
      // `data.lines`, so it happened to work, but the parenthesis placement
      // read as a null-guard that it was not. If `data` were null the guard
      // short-circuited to `Array.isArray(null)` → false, silently showing a
      // stale log instead of surfacing the bad response.
      if (data && Array.isArray(data.lines)) {
        _logRawLines = data.lines;
        renderLogLines();
      }
    })
    .catch(() => {})
    .finally(() => clearTimeout(timer));
}

// ===== Live activity widget (per-source summary above the stream) =====
function setLogTabDot(source, active) {
  const dot = document.querySelector('.log-source-tab[data-source="' + source + '"] .log-tab-dot');
  if (!dot) return;
  dot.classList.toggle('d-none', !active);
  dot.classList.toggle('log-tab-dot-active', active);
}

function updateQueueStatusBar() {
  const el = document.getElementById('scanQueueSummary');
  if (!el) return;
  fetch('/api/queue/status?limit=1', { cache: 'no-store' })
    .then((r) => r.json())
    .then((data) => {
      const counts = data.counts || {};
      const downloading = Number(counts.downloading) || 0;
      const queued = Number(counts.queued) || 0;
      const activeTotal = Number(data.total_active) || 0;

      setLogTabDot('queue', activeTotal > 0);
      setLogTabDot('soulseek', downloading > 0);

      if ((downloading + queued) > 0) {
        el.innerHTML = '<i class="bi bi-cloud-arrow-down me-1"></i>' + downloading + ' Downloading · ' + queued + ' Queued';
        el.classList.remove('d-none');
      } else {
        el.classList.add('d-none');
      }
    })
    .catch(() => {});
}

// Context-aware default tab when the modal opens: a running scan wins, then
// active Soulseek downloads, otherwise keep the last-viewed source.
function smartSelectLogSource() {
  fetch('/api/scan-progress?_ts=' + Date.now(), { cache: 'no-store' })
    .then((r) => r.json())
    .then((data) => {
      const running = (data.active_scans || []).some((s) => s.is_running);
      if (running) { switchLogSource('scanner'); return; }
      fetch('/api/queue/status?limit=1', { cache: 'no-store' })
        .then((r) => r.json())
        .then((q) => {
          const counts = q.counts || {};
          if ((Number(counts.downloading) || 0) > 0) switchLogSource('soulseek');
        })
        .catch(() => {});
    })
    .catch(() => {});
}

// The dashboard's own polling (dashboard.js) owns the bar there.
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
  line.textContent = `${name} — ${pct}%` + stage + (scan.current_item ? ` · ${scan.current_item}` : '');
  icon.className = 'scan-status-active';
  icon.innerHTML = '<i class="bi bi-activity"></i>';
}

document.addEventListener("DOMContentLoaded", () => {
  const logModalEl = document.getElementById('unifiedLogModal');
  if (logModalEl) {
    logModalEl.addEventListener('shown.bs.modal', () => {
      logModalVisible = true;
      smartSelectLogSource();
      updateUnifiedLog();
    });
    logModalEl.addEventListener('hidden.bs.modal', () => { logModalVisible = false; });
  }
  setInterval(() => {
    if (logModalVisible) updateUnifiedLog();
    updateQueueStatusBar();
  }, 5000);
  updateQueueStatusBar();
});

// ==========================================================================
// Sticky save bar (dirty-state tracking)
// --------------------------------------------------------------------------
// Forms opt in with ``data-sticky-save``.  Any input/change marks the form
// dirty and slides up a fixed bottom bar (Discard / Save Metadata).  JS-driven
// edits (chip inputs, quick-fill buttons) call ``markFormDirty(formId)``.
// ==========================================================================
window.markFormDirty = function (formId) {
  const form = document.getElementById(formId);
  if (form && form._setDirty) form._setDirty(true);
};

function initStickySaveBar(form) {
  let bar = document.getElementById('stickySaveBar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'stickySaveBar';
    bar.className = 'sticky-save-bar d-none';
    bar.innerHTML =
      '<div class="sticky-save-bar-inner">' +
        '<span class="sticky-save-bar-msg text-warning small"><i class="bi bi-exclamation-circle me-1"></i>You have unsaved metadata changes</span>' +
        '<div class="d-flex gap-2">' +
          '<button type="button" class="btn btn-outline-secondary btn-sm" data-discard><i class="bi bi-x-lg me-1"></i>Discard</button>' +
          '<button type="button" class="btn btn-success btn-sm" data-save><i class="bi bi-check-lg me-1"></i>Save Metadata</button>' +
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

  // The bar is shared by every opted-in form on the page, but its buttons are
  // wired once per form — so with two such forms the second wiring would also
  // reset/submit the first. `_boundForm` keeps the buttons pointed at whichever
  // form most recently went dirty.
  bar._boundForm = bar._boundForm || form;
  form.addEventListener('input', () => { bar._boundForm = form; });
  form.addEventListener('change', () => { bar._boundForm = form; });

  if (!bar._actionsWired) {
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
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll('form[data-sticky-save]').forEach(initStickySaveBar);
});

// ==========================================================================
// Global functions (moved from inline base.html <script> block)
// These must be on window because they are called by inline onclick/onsubmit
// handlers in templates (e.g. navSearch, openSlideOver).
// ==========================================================================
window.openSlideOver = function (url, title) {
  var el = document.getElementById('detailSlideOver');
  var contentEl = document.getElementById('slideOverContent');
  var titleEl = document.getElementById('slideOverTitle');
  titleEl.textContent = title || 'Loading...';
  contentEl.innerHTML = '<div class="d-flex justify-content-center py-5"><div class="spinner-border text-primary"></div></div>';

  var bsOffcanvas = new bootstrap.Offcanvas(el);
  bsOffcanvas.show();

  fetch(url)
    .then(function (r) { return r.text(); })
    .then(function (html) { contentEl.innerHTML = html; })
    .catch(function (err) { contentEl.innerHTML = '<div class="alert alert-danger m-3">Error loading details: ' + window.escapeHtml(err.message) + '</div>'; });
};

// ==========================================================================
// MusicBrainz Release Picker (slide-over)
// --------------------------------------------------------------------------
// Opens the /api/musicbrainz/release-picker flyout for a release GROUP so
// the user can choose the exact physical release (15-track CD, 18-track
// deluxe, 5-track promo, ...) before anything is queued.  Queueing a chosen
// version posts its concrete release MBID, which resolve_release_id accepts
// as-is (no re-resolution to the biggest official release).
// ==========================================================================
window._releasePickerOnQueued = null;

window.openReleasePicker = function (releaseGroupId, title, artist, onQueued) {
  if (!releaseGroupId) {
    if (typeof showToast === 'function') showToast('Error', 'Missing Release Group ID', 'error');
    else alert('❌ Error: Missing Release Group ID');
    return;
  }

  window._releasePickerOnQueued = typeof onQueued === 'function' ? onQueued : null;

  var url = '/api/musicbrainz/release-picker?rg_id=' + encodeURIComponent(releaseGroupId) +
    '&artist=' + encodeURIComponent(artist || '') +
    '&album=' + encodeURIComponent(title || '');

  // Probe the release group first: a group with exactly ONE release is
  // queued directly (no flyout); multi-version groups open the picker so
  // the user can choose the exact edition (CD / deluxe / promo).
  fetch(url + '&format=json', { headers: { 'Accept': 'application/json' } })
    .then(function (r) { return r.json().catch(function () { return null; }); })
    .then(function (data) {
      var releases = (data && Array.isArray(data.releases)) ? data.releases : null;
      if (releases && releases.length === 1) {
        var rel = releases[0];
        // queueSpecificRelease fires the onQueued callback (marked via
        // _releasePickerOnQueued) and shows the success toast.
        return window.queueSpecificRelease(rel.id, rel.title || title || '', artist || '');
      }
      // Zero or multiple releases → show the flyout as before (the flyout
      // itself renders an error card when nothing was found).
      window.openSlideOver(url, 'Select Version: ' + (title || ''));
    })
    .catch(function () {
      // Network failure → fall back to the flyout, which surfaces the error.
      window.openSlideOver(url, 'Select Version: ' + (title || ''));
    });
};

window.queueSpecificRelease = async function (releaseId, releaseTitle, artist) {
  if (!releaseId) return;
  try {
    var resp = await fetch('/api/musicbrainz/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        release_id: releaseId,
        release_title: releaseTitle,
        artist: artist,
        method: 'slskd',
        queue_items_only: true
      })
    });
    var data = await resp.json().catch(function () { return {}; });

    if (resp.ok && (data.success || data.tracking_id)) {
      if (typeof showQueueToast === 'function') {
        // Top-center floating pill with inline multi-item counting.
        showQueueToast(releaseTitle);
      } else if (typeof showToast === 'function') {
        showToast('Success', '📥 Queued ' + releaseTitle + ' (' + artist + ')', 'success');
      } else {
        alert('✅ Queued version "' + releaseTitle + '" to download queue!');
      }

      var cb = window._releasePickerOnQueued;
      window._releasePickerOnQueued = null;
      if (typeof cb === 'function') { try { cb(releaseId); } catch (e) { /* ignore */ } }

      var slideOverEl = document.getElementById('detailSlideOver');
      if (slideOverEl && window.bootstrap) {
        var inst = bootstrap.Offcanvas.getInstance(slideOverEl);
        if (inst) inst.hide();
      }
    } else {
      alert('❌ Error queuing release: ' + (data.error || 'Unknown error'));
    }
  } catch (e) {
    alert('❌ Network error while queuing release: ' + e.message);
  }
};

window.toggleReleaseTracklistPreview = function (releaseId) {
  var el = document.getElementById('preview-rel-' + releaseId);
  if (!el) return;
  if (el.classList.contains('d-none')) {
    el.classList.remove('d-none');
    el.innerHTML = '<div class="text-center py-2"><span class="spinner-border spinner-border-sm" role="status"></span></div>';
    fetch('/api/musicbrainz/release-picker?release_id=' + encodeURIComponent(releaseId))
      .then(function (r) { return r.text(); })
      .then(function (html) { el.innerHTML = html; })
      .catch(function () { el.innerHTML = '<div class="text-danger small">Failed to load tracklist.</div>'; });
  } else {
    el.classList.add('d-none');
  }
};

window.escapeHtml = function (text) {
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
};

// Global toast helper — works on every page without page-specific markup.
// Pages that define their own showToast (config, bookmarks, discover, ...)
// keep it — their scripts run later and shadow this one; every other page
// finally gets working toasts instead of silent no-ops.
window.showToast = function (title, message, type) {
  var wrap = document.getElementById('popularrToastWrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'popularrToastWrap';
    wrap.style.cssText = 'position:fixed;bottom:1rem;left:1rem;z-index:2200;' +
      'display:flex;flex-direction:column;gap:0.5rem;max-width:min(90vw,420px);';
    document.body.appendChild(wrap);
  }

  var el = document.createElement('div');
  var kind = type === 'success' ? 'bg-success'
    : (type === 'error' || type === 'danger') ? 'bg-danger'
    : type === 'warning' ? 'bg-warning text-dark' : 'bg-dark';
  var icon = type === 'success' ? 'bi-check-circle-fill'
    : (type === 'error' || type === 'danger') ? 'bi-x-circle-fill'
    : type === 'warning' ? 'bi-exclamation-triangle-fill' : 'bi-info-circle-fill';

  el.className = 'shadow-sm ' + kind;
  el.style.cssText = 'padding:0.65rem 1rem;border-radius:0.5rem;font-size:0.85rem;' +
    'display:flex;align-items:center;gap:0.5rem;opacity:0;transition:opacity 0.2s ease;';
  el.innerHTML = '<i class="bi ' + icon + '"></i><span></span>';
  // textContent — user-supplied titles/messages must never be injected as HTML.
  el.querySelector('span').textContent = title && message
    ? title + ': ' + message
    : (message || title || '');

  wrap.appendChild(el);
  requestAnimationFrame(function () { el.style.opacity = '1'; });
  setTimeout(function () {
    el.style.opacity = '0';
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 250);
  }, 4000);
};

// ==========================================================================
// Queue toast — top-center floating pill (~25% down the viewport, in the
// user's focal path under the search flyout and clear of the bottom log
// bar).  Rapid multi-queueing updates the pill IN PLACE instead of stacking
// toasts: "✓ "Album" added" -> "✓ Queued 3 items".
// ==========================================================================
window._queueToastCount = 0;
window._queueToastHideTimer = null;

window.showQueueToast = function (title) {
  var el = document.getElementById('queueToastPill');
  if (!el) {
    el = document.createElement('div');
    el.id = 'queueToastPill';
    el.className = 'd-none';
    el.style.cssText = 'position:fixed;top:25%;left:50%;transform:translateX(-50%);z-index:2400;' +
      'background:#198754;color:#fff;border-radius:999px;padding:0.55rem 1.1rem;' +
      'font-size:0.85rem;font-weight:600;box-shadow:0 4px 14px rgba(0,0,0,0.35);' +
      'display:flex;align-items:center;gap:0.5rem;max-width:min(90vw,480px);' +
      'white-space:nowrap;overflow:hidden;transition:opacity 0.2s ease;';
    el.innerHTML = '<i class="bi bi-check-circle-fill flex-shrink-0"></i><span class="text-truncate"></span>';
    document.body.appendChild(el);
  }

  window._queueToastCount += 1;
  // textContent — titles come from external APIs and must never hit innerHTML.
  el.querySelector('span').textContent = window._queueToastCount === 1
    ? 'Queued "' + title + '"'
    : 'Queued ' + window._queueToastCount + ' items';

  el.classList.remove('d-none');
  el.style.opacity = '1';

  clearTimeout(window._queueToastHideTimer);
  window._queueToastHideTimer = setTimeout(function () {
    el.style.opacity = '0';
    setTimeout(function () { el.classList.add('d-none'); }, 250);
    window._queueToastCount = 0;
  }, 2600);
};

// Generic top toast (25% from the top, centered pill) for queue/match
// feedback — success (green), warning (amber) or error (red).  Never uses
// innerHTML for the message; auto-hides after 2.6s.
window.showTopToast = function (message, type) {
  var kind = type === 'warning' ? '#b45309'
    : type === 'danger' || type === 'error' ? '#b91c1c'
    : '#198754';
  var icon = type === 'warning' ? 'bi-exclamation-triangle-fill'
    : type === 'danger' || type === 'error' ? 'bi-x-circle-fill'
    : 'bi-check-circle-fill';

  var el = document.createElement('div');
  el.style.cssText = 'position:fixed;top:25%;left:50%;transform:translateX(-50%);z-index:2400;' +
    'background:' + kind + ';color:#fff;border-radius:999px;padding:0.55rem 1.1rem;' +
    'font-size:0.85rem;font-weight:600;box-shadow:0 4px 14px rgba(0,0,0,0.35);' +
    'display:flex;align-items:center;gap:0.5rem;max-width:min(90vw,480px);' +
    'white-space:nowrap;overflow:hidden;transition:opacity 0.2s ease;';
  el.innerHTML = '<i class="bi ' + icon + ' flex-shrink-0"></i><span class="text-truncate"></span>';
  el.querySelector('span').textContent = message;

  document.body.appendChild(el);
  requestAnimationFrame(function () { el.style.opacity = '1'; });
  setTimeout(function () {
    el.style.opacity = '0';
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 250);
  }, 2600);
};

// ==========================================================================
// Alert → toast conversion (global)
// --------------------------------------------------------------------------
// Every ``alert()`` in the app now renders a top toast instead of a blocking
// browser dialog, and the message is shipped to ``client.log`` so UI
// feedback is greppable in the log files.  ``confirm()`` stays native —
// destructive flows keep their explicit confirmation.
// ==========================================================================
function _toastTypeFromMessage(message) {
  var m = String(message || '');
  var looksGood = /✅|✓|success|completed|updated|added|queued|deleted|saved|matched|started|lookup complete/i.test(m);
  var looksBad = /❌|✗|error|failed|invalid|missing|network|could not|unable|please enter|please select/i.test(m);
  if (looksGood && !looksBad) return 'success';
  if (looksBad) return 'danger';
  return 'warning';
}

function _logClientMessage(message) {
  try {
    fetch('/api/logs/client', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: String(message || '').slice(0, 500) }),
    }).catch(function () {});
  } catch (_e) { /* never block the UI on logging */ }
}

window.alert = function (message) {
  _logClientMessage(message);
  if (typeof window.showTopToast === 'function') {
    window.showTopToast(message, _toastTypeFromMessage(message));
  } else {
    window.console.warn('[alert→toast]', message);
  }
};

window.addBookmark = function (type, name, artist, album, trackId) {
  fetch('/api/bookmarks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: type, name: name, artist: artist, album: album, track_id: trackId })
  })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.success) {
        if (typeof showToast === 'function') showToast('Success', 'Bookmark added', 'success');
        else alert('Bookmark added successfully');
      } else {
        if (typeof showToast === 'function') showToast('Info', data.error || 'Bookmark may already exist', 'warning');
        else alert(data.error || 'Bookmark may already exist');
      }
    })
    .catch(function (error) {
      if (typeof showToast === 'function') showToast('Error', 'Failed to add bookmark: ' + error.message, 'error');
      else alert('Failed to add bookmark: ' + error.message);
    });
};

// Toggle the track favourite heart (per-user Navidrome star sync, no reload).
window.toggleTrackFavourite = function (trackId) {
  var icon = document.getElementById('trackFavouriteIcon');
  if (!icon || !trackId) return;

  function setState(fav) {
    icon.classList.remove(fav ? 'bi-heart' : 'bi-heart-fill');
    icon.classList.add(fav ? 'bi-heart-fill' : 'bi-heart');
  }

  // Read the current per-user state, then toggle it.
  fetch('/api/favourites/state?entity_type=track&entity_id=' + encodeURIComponent(trackId))
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var isFav = !!(data && data.is_favourite);
      return fetch('/api/favourites/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          entity_type: 'track',
          entity_id: trackId,
          navidrome_id: trackId,
          is_favourite: !isFav
        })
      });
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data && data.success) {
        setState(!!data.is_favourite);
        if (data.navidrome_synced === false) {
          console.warn('Track heart saved locally but Navidrome sync failed:', trackId);
        }
      } else {
        console.error('Track favourite toggle failed:', data && data.error);
      }
    })
    .catch(function (error) {
      console.error('Error toggling track favourite:', error);
    });
};

// Toggle the album favourite heart (bookmarks-backed, no page reload).
window.toggleAlbumFavourite = function (artistName, albumName) {
  var icon = document.getElementById('albumFavouriteIcon');
  if (!icon) return;

  function setState(fav) {
    icon.classList.remove(fav ? 'bi-heart' : 'bi-heart-fill');
    icon.classList.add(fav ? 'bi-heart-fill' : 'bi-heart');
  }

  fetch('/api/bookmarks')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var existing = (data.bookmarks || []).filter(function (b) {
        return b.type === 'album' && b.name === albumName
          && (!b.artist_name || b.artist_name === artistName);
      })[0];

      if (existing) {
        return fetch('/api/bookmarks/' + existing.id, { method: 'DELETE' })
          .then(function () { setState(false); });
      }
      return fetch('/api/bookmarks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: 'album', name: albumName, artist: artistName, album: albumName })
      }).then(function () { setState(true); });
    })
    .catch(function (error) {
      console.error('Error toggling album favourite:', error);
    });
};

window.navSearch = function (event) {
  event.preventDefault();
  var form = event && event.target ? event.target : null;
  var activeInput = form ? form.querySelector('input[type="text"], input[type="search"]') : null;
  var fallbackInput = document.getElementById('navSearchInput');
  var fallbackSearchInput = document.getElementById('dashboardTopSearchInput');
  var query = ((activeInput && activeInput.value) || (fallbackInput && fallbackInput.value) || (fallbackSearchInput && fallbackSearchInput.value) || '').trim();
  if (query) window.location.href = '/search?q=' + encodeURIComponent(query);
};

// ==========================================================================
// Global MusicBrainz search modal (canonical implementation)
// ==========================================================================
// Opens the MusicBrainz search modal included from
// components/_musicbrainz_search_modal.html (loaded via base.html). Fills
// the 4-field form (artist/album/track/year), wires the selection callback,
// and auto-runs the search. This is the single source of truth — pages that
// once defined their own copy (base.html inline block, downloads.js) are
// consolidated here.
//
// IMPORTANT: the component is included ONCE, by base.html. Every element id
// it renders (mbSearchArtist, mbSearchResults, ...) is resolved below with
// getElementById, which returns the FIRST match in DOM order. A page that
// includes the component a second time will have its search results written
// into the hidden global copy instead of the visible one.
window.openGlobalMbSearch = function (artist, album, callback, track, year) {
  const modalEl = document.getElementById('musicBrainzModal');
  if (!modalEl) {
    console.error("MusicBrainz modal not found in DOM.");
    return;
  }

  // Auto-fill the 4 search fields.
  //
  // Each field is CLEARED when no value is supplied. Previously the guard was
  // `if (el && value)`, so a stale value from a previous open survived: opening
  // the modal for an artist-only search after an album search kept the old
  // album in the form and silently narrowed the query.
  const fields = [
    ['mbSearchArtist', artist],
    ['mbSearchAlbum', album],
    ['mbSearchTrack', track],
    ['mbSearchYear', year],
  ];
  fields.forEach(function (pair) {
    const el = document.getElementById(pair[0]);
    if (el) el.value = pair[1] || '';
  });

  // Assign callback to global window object so the component can trigger it
  window._mbSearchCallback = callback;

  // ⚠️ Whether this session is a MATCH or a DOWNLOAD must be recorded
  // separately from the callback itself.
  //
  // The result renderer picks its button from `_mbSearchCallback`, but that is
  // set to null as soon as a match is applied (so it cannot fire twice). After
  // one match, every later search in the same modal session therefore fell
  // through to the "Soulseek" download button — even when the user had opened
  // the modal from the album page's "Lookup MBID" action purely to match.
  //
  // This flag is set once per open and is NOT cleared by applying a match, so
  // the button keeps reflecting the caller's intent for the whole session.
  window._mbMatchIntent = typeof callback === 'function';

  // Show Modal
  const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
  modal.show();

  // Auto-search once the modal is actually shown. A fixed setTimeout raced the
  // Bootstrap transition: on a slow render the search fired before the fields
  // were visible, and `once: true` avoids re-running on every subsequent open.
  const runSearch = function () {
    if (typeof window.performMbSearch === 'function') window.performMbSearch();
  };
  modalEl.addEventListener('shown.bs.modal', runSearch, { once: true });
};

// ==========================================================================
// Encoded-argument MusicBrainz search (shared by artist/album detail pages)
// ==========================================================================
// Decodes URL-encoded inline arguments and opens the canonical shared modal.
// Some pages (album_detail.js) render buttons calling this with encoded args.

// Self-contained Soulseek download helper for pages that don't load
// downloads.js (artist/album pages) — mirrors _addMbDownloadToSession.
window.downloadReleaseViaSoulseek = function (releaseId, title, artist) {
  if (!confirm(`Download "${title}" by ${artist} via Soulseek?`)) return;
  fetch('/api/musicbrainz/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      release_id: releaseId,
      release_title: title,
      artist: artist,
      method: 'slskd',
      persistent_search: false,
      max_retries: 3,
      session_id: null,
      queue_items_only: true
    })
  })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.error) { alert('Error: ' + data.error); return; }
      alert(`Download queued: ${title}\nTracking ID: ${data.tracking_id || 'N/A'}`);
    })
    .catch(function (err) { alert('Network error: ' + err.message); });
};

window.searchMusicBrainzReleaseFromEncoded = function (event, artistEnc, albumEnc) {
  const decode = function (value, fallback) {
    try {
      const decoded = decodeURIComponent(value || '');
      try { return JSON.parse(decoded); } catch (_e) { return decoded || fallback; }
    } catch (_e) { return fallback; }
  };

  const artist = decode(artistEnc, '');
  const album = decode(albumEnc, '');

  if (event && event.preventDefault) event.preventDefault();
  if (event && event.stopPropagation) event.stopPropagation();

  window.openGlobalMbSearch(artist, album, function (selectedRelease) {
    if (!selectedRelease) return;
    if (typeof window.downloadMbRelease === 'function') {
      window.downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
    } else if (typeof window.downloadReleaseViaSoulseek === 'function') {
      window.downloadReleaseViaSoulseek(selectedRelease.id, selectedRelease.title, selectedRelease.artist);
    }
  });
};
