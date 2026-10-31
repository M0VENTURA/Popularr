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
   updateQueueStatusBar (and updateUnifiedLog, since moved to js/pages/
   logs.js with the rest of the log engine) ran on a bare 5s setInterval that
   was never cleared and never paused. On a backgrounded tab it kept issuing
   two requests every five seconds indefinitely. Now a managed poller:
   pauses when hidden, never overlaps, stops on teardown.
   ========================================================================== */

(function (global) {
  'use strict';

  const LOG_POLL_INTERVAL_MS = 5000;

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

  // ⚠️ THE EXPANDED MOBILE MENU MUST NOT COUNT TOWARDS THE TOKEN.
  //
  // `.navbar-collapse.show` is itself SIZED FROM the token
  // (`max-height: calc(100dvh - var(--navbar-height) - 0.5rem)`), so measuring
  // the navbar while the menu was open closed a loop:
  //
  //     menu grows → navbar taller → token grows → max-height shrinks
  //     → menu clamps → navbar height changes → ResizeObserver fires → …
  //
  // It settled on TWO values and swept between them, forever, and every
  // consumer of the token moved with each sweep (main's padding-top, the
  // dashboard's min-height, the sticky footer) — reported as the bottom of the
  // page flickering when the hamburger was pressed on mobile.
  //
  // The token means "how tall the COLLAPSED bar is", which is exactly what
  // `main` has to clear; the open menu overlays the page instead of moving it.
  const NAVBAR_MOBILE = global.matchMedia
    ? global.matchMedia('(max-width: 991.98px)')
    : null;

  function navbarCollapseIsOpen() {
    const collapse = document.getElementById('navbarNav');
    if (!collapse) return false;
    // At lg and up the toggler is hidden and the collapse sits INLINE in the
    // row, so it is part of the bar's real height and must not be subtracted.
    if (NAVBAR_MOBILE && !NAVBAR_MOBILE.matches) return false;
    return collapse.classList.contains('show') ||
      collapse.classList.contains('collapsing');
  }

  function syncNavbarHeight() {
    const nav = document.querySelector('nav.navbar.fixed-top');
    if (!nav || !nav.offsetHeight) return;

    let height = nav.offsetHeight;
    if (navbarCollapseIsOpen()) {
      const collapse = document.getElementById('navbarNav');
      if (collapse && nav.contains(collapse)) height -= collapse.offsetHeight;
    }
    if (height <= 0) return;

    document.documentElement.style.setProperty('--navbar-height', height + 'px');
  }

  // ── Scrollbar width ─────────────────────────────────────────────────────
  //
  // The navbar is `position: fixed; left: 0; right: 0`, so it spans the whole
  // viewport and lies OVER the top of main's scrollbar — which made the
  // scrollbar impossible to grab for the first ~navbar-height pixels. CSS pulls
  // the bar's right edge in by this token so the scrollbar stays reachable.
  //
  // Measured from `main` rather than the document: page scrolling lives on
  // `main` (body is overflow:hidden), so the document's own client width tells
  // us nothing. `scrollbar-gutter: stable` on main means the value does NOT
  // change as content grows or shrinks, so measuring on resize/load is enough.
  //
  // ⚠️ OVERLAY SCROLLBARS CONSUME NO LAYOUT WIDTH, so `offsetWidth - clientWidth`
  // is 0 even on a scrollable page (macOS, Chrome's overlay mode, mobile). The
  // browser then paints the bar OVER main's right edge, UNDER the fixed
  // navbar — the reported "side scroll bar gets covered by the banner". Reserve
  // an explicit strip in that case so the bar is not painted beneath the
  // navbar. Only for a fine pointer: a touch scrollbar is transient, and a
  // permanent gap would just narrow the header.
  const OVERLAY_SCROLLBAR_WIDTH = 15;

  function syncScrollbarWidth() {
    const main = document.querySelector('main.container-fluid');
    if (!main) return;

    let width = Math.max(0, main.offsetWidth - main.clientWidth);
    if (
      !width &&
      main.scrollHeight > main.clientHeight + 1 &&
      global.matchMedia &&
      global.matchMedia('(hover: hover) and (pointer: fine)').matches
    ) {
      width = OVERLAY_SCROLLBAR_WIDTH;
    }
    document.documentElement.style.setProperty('--scrollbar-width', width + 'px');
  }

  function initNavbarHeight() {
    syncNavbarHeight();
    syncScrollbarWidth();

    // A resize listener alone misses height changes that are not window
    // resizes — the mobile menu expanding, or web fonts landing after first
    // paint and reflowing the brand row.
    if (typeof ResizeObserver !== 'undefined') {
      const nav = document.querySelector('nav.navbar.fixed-top');
      if (nav) new ResizeObserver(syncNavbarHeight).observe(nav);

      // A classic scrollbar appearing/disappearing shrinks/grows main's
      // content box, so this also re-measures the width when the page first
      // becomes scrollable (e.g. async content landing after DOMContentLoaded).
      const main = document.querySelector('main.container-fluid');
      if (main) new ResizeObserver(syncScrollbarWidth).observe(main);
    } else {
      global.addEventListener('resize', syncNavbarHeight);
    }

    // Bootstrap's collapse animates, so measure once it has settled.
    const navCollapse = document.getElementById('navbarNav');
    if (navCollapse) {
      navCollapse.addEventListener('shown.bs.collapse', syncNavbarHeight);
      navCollapse.addEventListener('hidden.bs.collapse', syncNavbarHeight);
    }

    // The scrollbar can appear/disappear with the window width (overlay vs
    // classic), and a zoom change can resize it too.
    global.addEventListener('resize', syncScrollbarWidth);
    global.addEventListener('load', syncScrollbarWidth);

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
  //
  // ⚠️ main.js no longer OWNS the log viewer — js/pages/logs.js does, because
  // this app had two competing log views (the modal and the /logs page) and
  // the consolidation made the page's engine the only one. Duplicating the
  // fetch/render loop here is exactly what produced the two-view problem in
  // the first place, so this file now does two things only:
  //
  //   1. opens the modal (openUnifiedLogModal, called by the status bar and
  //      the System ▸ Logs menu item);
  //   2. drives the modal's LIFETIME — `shown`/`hidden` hand off to
  //      logsPage.onOpen()/onClose() (the engine fills the panel and starts
  //      the stream only when the panel is actually visible), and the managed
  //      poller below keeps the STATUS BAR's queue summary fresh on every
  //      page, modal open or not.
  //
  // The old constants here (LOG_SOURCE_FILES, LOG_TAG_CLASSES), the five
  // source tabs, the pause/timestamp/clear/export buttons and the 5s log poll
  // all moved to logs.js or died with the tabs.
  let logPoller = null;

  function openUnifiedLogModal() {
    const modalEl = document.getElementById('unifiedLogModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  async function updateQueueStatusBar() {
    const el = document.getElementById('scanQueueSummary');
    if (!el) return;

    try {
      const data = await global.api.getJson('/api/queue/status?limit=1');
      const counts = data.counts || {};
      const downloading = Number(counts.downloading) || 0;
      const queued = Number(counts.queued) || 0;


      // The Queue/Soulseek activity DOTS died with the modal's source tabs,
      // which were folded into the file picker. The signal itself was never
      // lost: it is printed here, on the status bar, which is visible whether
      // or not the log modal is open.
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

  // ── Sticky scan bar ─────────────────────────────────────────────────────

  /**
   * Render a scan percentage for a text label.
   *
   * progress_service returns real decimals: one artist's share of a full scan
   * is 100/total_artists, so on a large library the first artists are a fraction
   * of a percent. Truncating to an integer is what made the status bar sit on
   * "0%" while the scan was working, so anything inside (0, 1) shows as "<1%".
   * Progress BARS must keep using the raw number for their width.
   * Kept identical to the copy in pages/dashboard.js — do not let them drift.
   */
  function formatScanPercent(pct) {
    const value = Number(pct) || 0;
    if (value > 0 && value < 1) return '<1%';
    return `${Math.round(value)}%`;
  }

  /**
   * The header terminal button's activity dot.
   *
   * ⚠️ Written from BOTH scan-state writers, because neither covers every page:
   * this file's SSE stream returns early on the dashboard (`#dashboardFlags`),
   * and dashboard.js polls for itself there. Each writer calls this, so the dot
   * means the same thing on every page instead of depending on which poll
   * happened to run last.
   *
   * Exposed through `global.popularr` for the same reason `setScanBarActive` is:
   * dashboard.js has its own copy of the status-bar logic and must not fork a
   * second notion of "a scan is running".
   */
  function setHeaderScanDot(isActive) {
    const dot = document.getElementById('appHeaderScanDot');
    if (!dot) return;
    dot.hidden = !isActive;
    dot.classList.toggle('is-active', !!isActive);

    // The button's accessible name carries the state too: the dot is decorative
    // (`aria-hidden`), so a screen reader would otherwise hear "Scanning" for
    // both a running and an idle scan.
    const button = document.getElementById('appHeaderScan');
    if (button) {
      button.title = isActive ? 'Scanning — open log' : 'Scanning';
    }
  }

  /** The dashboard's own polling (dashboard.js) owns the bar there. */
  function updateGlobalScanBar(data) {
    if (document.getElementById('dashboardFlags')) return;

    const bar = document.getElementById('scanStatusBar');
    const line = document.getElementById('scanStatusLine');
    const icon = document.getElementById('scanStatusIcon');
    if (!bar || !line || !icon) return;

    const active = (data && (data.active_scans || [])) || [];
    const scan = active.find((s) => s && s.is_running);
    setHeaderScanDot(!!scan);
    // NOTE: this used to call setLogTabDot('scanner', …), which lit a dot on
    // the modal's Scanner tab. The tabs are folded into the file picker now,
    // so there is nothing left to light — and the scan state the dot
    // communicated is printed right here, on the status bar line.

    if (!scan) {
      line.textContent = 'Idle';
      icon.className = 'scan-status-idle';
      icon.innerHTML = '<i class="bi bi-circle"></i>';
      setScanBarActive(bar, false, 0);
      return;
    }

    const pct = Math.min(scan.percent_complete ?? scan.progress ?? 0, 100);
    const name = String(scan.scan_type || 'scan').replace(/_/g, ' ');
    const stage = scan.current_stage ? ` · ${scan.current_stage}` : '';
    line.textContent = `${name} — ${formatScanPercent(pct)}${stage}` +
      (scan.current_item ? ` · ${scan.current_item}` : '');
    icon.className = 'scan-status-active';
    icon.innerHTML = '<i class="bi bi-activity"></i>';
    setScanBarActive(bar, true, pct);
  }

  /**
   * Switch the status bar between its idle hairline and the active pill.
   *
   * The idle and active presentations are the same element in two states, so
   * the class and the progress width must be written TOGETHER — setting the
   * class without the width would show the previous scan's progress.
   *
   * ⚠️ The accessibility switch is MOBILE-ONLY, because the element is two
   * different things at the two breakpoints. Above lg it is a normal footer
   * bar with text on it, and it stays a button while idle — it is the only way
   * to open the log there, so stripping `role`/`tabindex` on idle would have
   * silently removed the desktop keyboard path to the log viewer. Below lg the
   * idle state is a 2px line with `pointer-events: none`, so leaving it
   * announced as a button would offer a control that cannot be activated.
   */
  function setScanBarActive(bar, isActive, percent) {
    if (!bar) return;
    bar.classList.toggle('is-active', !!isActive);
    const fill = document.getElementById('scanBarFill');
    if (fill) fill.style.width = isActive ? `${Math.max(0, Math.min(percent || 0, 100))}%` : '0';

    const isMobile = !!(global.matchMedia && global.matchMedia('(max-width: 991.98px)').matches);
    if (isActive || !isMobile) {
      bar.setAttribute('role', 'button');
      bar.setAttribute('tabindex', '0');
      bar.removeAttribute('aria-hidden');
    } else {
      bar.removeAttribute('role');
      bar.removeAttribute('tabindex');
      bar.setAttribute('aria-hidden', 'true');
    }
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

  /**
   * True when /api/musicbrainz/download reported that it enqueued nothing.
   *
   * The endpoint returns HTTP 200 with ``queued: false`` (plus a ``reason``
   * and human-readable ``message``) rather than an error status, because
   * ``api.postJson`` throws on non-2xx and that would hide the specific
   * "already in your library" message behind a generic HTTP error.
   *
   * The track COUNT outranks ``success``: the endpoint used to answer
   * ``success: true`` alongside ``queued_tracks: 0``, so a check that only
   * looked at ``success`` would let the original bug straight back in. A
   * legacy payload that omits the count entirely still counts as queued.
   */
  function nothingQueued(data) {
    if (!data) return false;
    const count = Number(data.queued_tracks);
    if (count > 0) return false;
    if (data.queued === false) return true;
    if (data.queued === true) return false;
    if (data.queued_tracks !== undefined && count === 0) return true;
    return data.success === false && !data.tracking_id;
  }

  /** Show the non-success outcome of a queue request as a warning. */
  function warnNothingQueued(data) {
    const message = (data && (data.message || data.error)) || 'Nothing to queue for this release.';
    if (global.toast && typeof global.toast.warning === 'function') {
      global.toast.warning(message);
    } else if (global.toast) {
      global.toast.error(message);
    }
    return message;
  }

  async function openReleasePicker(releaseGroupId, title, artist, onQueued) {
    if (!releaseGroupId) {
      global.toast.error('Missing release group ID');
      return;
    }

    releasePickerOnQueued = typeof onQueued === 'function' ? onQueued : null;

    const url = '/api/musicbrainz/release-picker?mode=queue&rg_id=' + encodeURIComponent(releaseGroupId) +
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

      // Every edition in this group carries the SAME number of tracks, so the
      // flyout would offer rows differing only by PRESSING — country, date,
      // format — which is not a difference a Soulseek download acts on.
      // Queue the best one instead of asking. The endpoint sorts Official
      // first and then by track count, so ``releases[0]`` is it.
      //
      // A count of 0 means the media did not load: that is UNIDENTIFIED, not
      // "identical", and must still be chosen by hand — hence the explicit
      // ``has(0)`` guard.
      if (releases.length > 1) {
        const counts = new Set(releases.map((rel) => Number(rel.track_count) || 0));
        if (counts.size === 1 && !counts.has(0)) {
          const best = releases[0];
          const note = `All ${releases.length} versions have ${best.track_count} tracks — queued the best match`;
          return queueSpecificRelease(best.id, best.title || title || '', artist || '', note);
        }
      }
    } catch (_error) {
      // Network failure → fall through to the flyout, which surfaces the error.
    }

    openSlideOver(url, 'Select Version: ' + (title || ''));
  }

  async function queueSpecificRelease(releaseId, releaseTitle, artist, note) {
    if (!releaseId) return;

    try {
      const data = await global.api.postJson('/api/musicbrainz/download', {
        release_id: releaseId,
        release_title: releaseTitle,
        artist,
        method: 'slskd',
        queue_items_only: true,
      });

      // The API answers with queued:false when it created no queue rows — the
      // release is already owned or already queued. That is not success, so
      // the green pill and the onQueued callback must not fire.
      if (nothingQueued(data)) {
        warnNothingQueued(data);
        return;
      }

      if (!data.success && !data.tracking_id) {
        global.toast.error('Error queueing release: ' + (data.error || 'Unknown error'));
        return;
      }

      global.toast.queued(releaseTitle);
      // Only after the queue actually succeeded: announcing it up front would
      // claim a queue that the API then refused.
      if (note && global.toast && typeof global.toast.info === 'function') {
        global.toast.info(note);
      }

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

  // ── Queue a chosen release from the "Select Version" flyout ─────────────
  //
  // The flyout is SERVER-RENDERED, so its buttons cannot be bound when the page
  // loads — delegate from the document, the same way album.js handles
  // .js-pick-release.
  //
  // ⚠️ WHY THIS EXISTS. ``openReleasePicker`` auto-queues only a group whose
  // editions all share a track count (or a group with exactly one release).
  // Every group with genuinely different editions opened the flyout with the
  // text "Select the exact version you want to queue:" and no button at all, so
  // the version could not be selected — the reported bug. The button is rendered
  // by ``mode=queue``; this is what makes it do something.
  //
  // Delegated from ``document`` rather than the slide-over so it works whichever
  // page opened the flyout (unified search, downloads monitor, upcoming
  // releases, artist or album pages) — those do not all load the same scripts.
  document.addEventListener('click', (event) => {
    const btn = event.target && event.target.closest
      ? event.target.closest('.js-queue-release')
      : null;
    if (!btn) return;

    const releaseId = btn.getAttribute('data-release-id') || '';
    if (!releaseId) return;

    // Disable while the POST is in flight: the queue round trip is seconds long
    // and a second click would queue the release twice.
    btn.disabled = true;
    Promise.resolve(
      queueSpecificRelease(
        releaseId,
        btn.getAttribute('data-release-title') || '',
        btn.getAttribute('data-release-artist') || ''
      )
    ).finally(() => {
      // ``queueSpecificRelease`` hides the slide-over on success, so re-enabling
      // a detached button is harmless; on FAILURE the flyout stays open and the
      // button must come back or the user cannot retry.
      btn.disabled = false;
    });
  });

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
      if (nothingQueued(data)) {
        warnNothingQueued(data);
        return;
      }
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

  // ── App header (mobile) ─────────────────────────────────────────────────

  /**
   * The header's back button.
   *
   * Browser history FIRST, so a deep link or a search result returns to where
   * the user actually came from — "back" that jumps to a structural parent
   * would silently discard the page they were browsing. `history.length > 1`
   * is the only signal available, and it is not reliable in every engine, so
   * it is used as a *preference* rather than a gate on correctness: the
   * fallback below is a real navigation, not an error path.
   */
  function navigateBack() {
    if (global.history && global.history.length > 1) {
      global.history.back();
      return;
    }
    // Landed directly on this URL (a bookmark, a shared link, a fresh tab):
    // there is nothing to go back to, so fall back to the structural parent.
    const fallback = document.getElementById('appHeaderBack');
    const href = fallback && fallback.getAttribute('data-fallback-href');
    global.location.href = href || '/dashboard';
  }

  /**
   * The header's scan button.
   *
   * A page that can scan something listens for `popularr:scan-request` and
   * calls `preventDefault()`, which is how the header stays a shell concern:
   * it does not know what is on the page, and the page does not own a button
   * in the shell.
   *
   * With no listener the event returns undefaulted and this opens the scanning
   * log instead — the same modal the status bar opens. That fallback is the
   * point: a scan button that does nothing on 25 of 30 pages would train
   * people to ignore it, and the log is the one scanning view that exists
   * everywhere.
   */
  function requestContextScan() {
    const event = new CustomEvent('popularr:scan-request', {
      cancelable: true,
      bubbles: true,
      detail: { path: global.location.pathname },
    });
    const handled = !document.dispatchEvent(event);
    if (!handled && typeof openUnifiedLogModal === 'function') openUnifiedLogModal();
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
      // The ENGINE owns the panel's content: on open it fills the file picker,
      // loads the tail and starts the stream; on close it stops the stream.
      // main.js only signals lifetime — fetching a log nobody can see (or
      // leaving an SSE connection open behind a dismissed modal) is the whole
      // reason this hand-off exists.
      logModalEl.addEventListener('shown.bs.modal', () => {
        if (global.logsPage) global.logsPage.onOpen();
      });
      logModalEl.addEventListener('hidden.bs.modal', () => {
        if (global.logsPage) global.logsPage.onClose();
      });
    }

    // Old /logs bookmarks: ui_routes.logs redirects test_site mode here with
    // ?logs=1, so a link from a doc or a browser history entry still opens the
    // viewer instead of a page that no longer exists. The param is stripped
    // afterwards so a refresh does not re-open a modal the user just closed.
    if (/[?&]logs=1\b/.test(global.location.search)) {
      const strip = new URLSearchParams(global.location.search);
      strip.delete('logs');
      const query = strip.toString();
      if (global.history && history.replaceState) {
        history.replaceState(null, '', global.location.pathname + (query ? `?${query}` : ''));
      }
      setTimeout(() => openUnifiedLogModal(), 0);
    }

    // Managed poller: pauses when the tab is hidden, never overlaps, and is
    // released on teardown. The previous bare setInterval kept firing two
    // requests every five seconds in a background tab, forever.
    //
    // NOTE: the log stream is NOT in this loop any more — logs.js streams
    // over SSE while the modal is open, so polling it here would fetch the
    // same 300 lines twice. This tick keeps the STATUS BAR's queue summary
    // fresh, which is needed on every page regardless of the modal.
    logPoller = global.poller.create({
      interval: LOG_POLL_INTERVAL_MS,
      onTick: async () => {
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
    syncScrollbarWidth,
    initMobileTabs,
    openSlideOver,
    openReleasePicker,
    queueSpecificRelease,
    markFormDirty,
    updateGlobalScanBar,
    setScanBarActive,
    setHeaderScanDot,
    navigateBack,
    requestContextScan,
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
  global.navigateBack = navigateBack;
  global.requestContextScan = requestContextScan;
  // ⚠️ The other log handlers this file used to export (toggleLogPause,
  // toggleLogTimestamps, clearUnifiedLog, switchLogSource,
  // downloadActiveLogLastHour) were bound by inline onclick in the old modal
  // markup. That markup is gone: the buttons now live in components/
  // _log_modal.html and logs.js binds them by id, so exporting a second
  // implementation of each would be the duplication this consolidation
  // removed. (Same for updateUnifiedLog — its whole body moved with them.)
})(window);
