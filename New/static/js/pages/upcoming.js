/* ==========================================================================
   static/js/pages/upcoming.js
   Controller for the dedicated Upcoming Releases page
   (templates/Pages/downloads/upcoming.html).

   This file owns ONLY the page chrome — filter tabs, the source dropdown,
   the status strip, the "Sync & Tools" actions and the scrape-progress poll.
   Everything that fetches, matches, queues or renders releases lives in
   static/js/services/upcoming-releases.js (UpcomingReleasesService), which is
   shared by this page, the download monitor card and the dashboard table.

   Load order:
       utils/dom.js  →  utils/api.js  →  utils/poller.js
       ui/toast.js  →  ui/modal.js  →  ui/confirm.js
       services/upcoming-releases.js
       pages/upcoming.js

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/downloads/upcoming.html carried a 246-line inline <script>
   plus a 12-rule <style> block. The script is this file. The <style> block was
   DELETED rather than moved: every rule in it was a #musicBrainzModal
   override written with raw #1a1a1a / #2a2a2a / #333 literals, duplicating
   rules popularr.css already owns through the theme tokens. Two competing
   definitions of the same modal is how the dark-theme palette drifted.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. `S.escapeHtml` DID NOT EXIST.
      UpcomingReleasesService deliberately does not export it (utils/dom.js
      owns escapeHtml and the service's own header comment says so). Both
      showError() and loadSourceFilter() called S.escapeHtml(...) anyway, so a
      failed scrape — the exact case showError exists for — threw
      "S.escapeHtml is not a function", and any source name containing an
      ampersand or quote broke the dropdown markup.
      Both now use global.escapeHtml.

   2. THE PROGRESS POLL WAS AN UNMANAGED setInterval.
      `progressTimer = setInterval(...)` was cleared only by its own callback.
      Navigate away mid-scrape and the 3s loop kept issuing requests for the
      life of the document; backgrounded, it kept running too. It is now a
      managed poller (utils/poller.js) — pauses when the tab is hidden, never
      overlaps a slow request, and is torn down on pagehide.

   3. NATIVE DIALOGS AND RAW fetch().
      Clear Database and Auto-Match All used blocking confirm() and called
      fetch() directly. A non-2xx response with an HTML body (session
      redirect) produced "Unexpected token '<'" with no useful context.
      Now ui/confirm.js + utils/api.js.

   4. FIVE window.* GLOBALS EXISTED ONLY TO SERVE INLINE onclick.
      The Sync & Tools dropdown called
      scrapeWikipedia() / checkForUpdates() / autoMatchAll() / clearDatabase()
      from inline attributes, and the init block assigned all of them (plus
      refreshUpcomingList) onto window. They are now `data-action` buttons
      handled by one delegated listener, and NOTHING is exported onto window.

   ── CROSS-PAGE NOTE ───────────────────────────────────────────────────────
   `checkForUpdates` exists here AND in pages/monitor.js (as
   checkForUpdatesMonitor) AND as a dashboard action. They are deliberately
   separate: each refreshes a different container. Do not "deduplicate" them
   into one global — the monitor card and this page render into different DOM
   ids and one of the two would silently stop updating.
   ========================================================================== */

(function (global) {
  'use strict';

  const PAGE_LIMIT = 1000;
  const SOURCES_TIMEOUT_MS = 10000;
  const SCRAPE_TIMEOUT_MS = 120000;
  const PROGRESS_POLL_MS = 3000;
  const AUTO_CHECK_AFTER_MS = 1000;
  const AUTO_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;
  const LAST_CHECKED_KEY = 'upcomingReleasesLastChecked';

  const state = {
    filter: 'all',
    source: 'all',
    progressPoller: null,
  };

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  /** The shared service, or null with a console error naming the missing file. */
  function service() {
    const svc = global.UpcomingReleasesService;
    if (!svc) {
      console.error(
        '[upcoming] UpcomingReleasesService is unavailable — ' +
        'is services/upcoming-releases.js loaded before pages/upcoming.js?'
      );
    }
    return svc;
  }

  // ── Status strip ────────────────────────────────────────────────────────
  //
  // #upcomingStatus is the spinning "working on it" bar; #upcomingError is
  // the failure banner. They are mutually exclusive — showing one always
  // hides the other, which the three helpers below keep invariant.

  function showStatus(message) {
    const errorEl = document.getElementById('upcomingError');
    const statusEl = document.getElementById('upcomingStatus');
    const textEl = document.getElementById('upcomingStatusText');
    if (!statusEl || !textEl) return;
    if (errorEl) errorEl.style.display = 'none';
    statusEl.style.display = 'block';
    textEl.textContent = message;
  }

  function hideStatus() {
    const statusEl = document.getElementById('upcomingStatus');
    if (statusEl) statusEl.style.display = 'none';
  }

  function showError(message) {
    const errorEl = document.getElementById('upcomingError');
    if (errorEl) {
      errorEl.innerHTML =
        '<i class="bi bi-exclamation-triangle"></i> <strong>Error:</strong> ' +
        esc(message);
      errorEl.style.display = 'block';
    }
    hideStatus();
  }

  // ── Progress poll ───────────────────────────────────────────────────────

  async function updateProgressBadge() {
    const svc = service();
    if (!svc) return false;
    try {
      const status = await svc.fetchScrapeStatus();
      const badgeEl = document.getElementById('upcomingProgressBadge');
      if (badgeEl) badgeEl.innerHTML = svc.renderProgressBadge(status);
      return !!(status && status.status === 'running');
    } catch (_error) {
      return false;
    }
  }

  function stopProgressPolling() {
    if (state.progressPoller) {
      state.progressPoller.stop();
      state.progressPoller = null;
    }
  }

  /**
   * Poll /scrape/status until the scraper reports it is no longer running,
   * then refresh the table once.
   *
   * The previous implementation was a bare setInterval with no visibility
   * guard and no teardown — see the file header.
   */
  function startProgressPolling() {
    stopProgressPolling();
    state.progressPoller = global.poller.create({
      interval: PROGRESS_POLL_MS,
      immediate: false,
      onTick: async (ctx) => {
        const running = await updateProgressBadge();
        if (!running) {
          ctx.stop();
          state.progressPoller = null;
          refreshPage();
        }
      },
      onError: () => {
        // Transient status-endpoint failures must not kill the poll; the
        // scraper itself is server-side and keeps running either way.
      },
    });
    state.progressPoller.start();
  }

  // ── Data ────────────────────────────────────────────────────────────────

  async function refreshPage() {
    const svc = service();
    const container = document.getElementById('upcomingReleases');
    if (!svc || !container) return;

    container.innerHTML =
      '<div class="text-center py-4">' +
      '<div class="spinner-border text-primary spinner-border-sm" role="status"></div>' +
      '<p class="mt-2 small mb-0">Loading upcoming releases…</p></div>';

    try {
      const data = await svc.fetchReleases({
        filter: state.filter === 'all' ? undefined : state.filter,
        source: state.source === 'all' ? undefined : state.source,
        include_queue: true,
        page: 1,
        limit: PAGE_LIMIT,
      });

      const totalBadge = document.getElementById('upcomingTotalBadge');
      if (totalBadge) {
        totalBadge.textContent = String(svc.state.total || 0);
        totalBadge.classList.remove('d-none');
      }

      svc.renderTable('upcomingReleases', data.releases || [], {
        emptyMessage: state.filter === 'discovered'
          ? 'No Wikipedia-discovered releases yet. Use Update from Wikipedia to scrape.'
          : 'No upcoming releases found. Use Check for Updates to search MusicBrainz.',
      });
      hideStatus();
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      container.innerHTML =
        '<div class="text-center py-4"><p class="text-muted mb-0">Nothing to show yet.</p></div>';
      showError(error.message || 'Failed to load upcoming releases');
    }
  }

  async function loadSourceFilter() {
    const svc = service();
    const select = document.getElementById('upcomingSourceFilter');
    if (!svc || !select) return;

    try {
      const sources = await svc.fetchSources();
      let html = `<option value="all">All Sources (${sources.length})</option>`;
      sources.forEach((source) => {
        html += `<option value="${esc(source.key)}">` +
          `${esc(source.label || source.key)} (${source.count || 0})</option>`;
      });
      select.innerHTML = html;
      select.value = state.source;
    } catch (_error) {
      // The dropdown degrades to "All Sources" — the table still loads.
    }
  }

  function applyFilter(filter) {
    state.filter = filter || 'all';
    document.querySelectorAll('#upcomingTabs .nav-link').forEach((tab) => {
      const isActive = tab.getAttribute('data-filter') === state.filter;
      tab.classList.toggle('active', isActive);
      tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    return refreshPage();
  }

  // ── Sync & Tools actions ────────────────────────────────────────────────

  async function checkForUpdates() {
    try {
      localStorage.setItem(LAST_CHECKED_KEY, Date.now().toString());
    } catch (_error) {
      /* private browsing — the timestamp is a nicety, not a requirement */
    }
    await refreshPage();
  }

  async function scrapeWikipedia() {
    const svc = service();
    if (!svc) return;

    showStatus('Scraping Wikipedia for upcoming releases…');
    try {
      const data = await svc.triggerScrape();
      showStatus('✓ ' + (data.message || 'Scrape complete'));
      startProgressPolling();
      await updateProgressBadge();
    } catch (error) {
      showError(error.message || 'Scrape failed');
    }
  }

  async function clearDatabase(button) {
    const accepted = await global.ui.confirm({
      title: 'Clear upcoming releases',
      message: 'Remove every upcoming release from the database? This cannot be undone.',
      tone: 'danger',
      confirmLabel: 'Clear',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, 'Clearing…', async () => {
      showStatus('Clearing database…');
      try {
        await global.api.postJson('/api/upcoming-releases/clear', {});
        showStatus('✓ Database cleared');
        await refreshPage();
        hideStatus();
      } catch (error) {
        showError(error.message || 'Failed to clear database');
      }
    });
  }

  async function autoMatchAll(button) {
    const accepted = await global.ui.confirm({
      title: 'Auto-match releases',
      message: 'Match every unmatched release to MusicBrainz? This can take a while.',
      confirmLabel: 'Match',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, 'Matching…', async () => {
      showStatus('Matching releases to MusicBrainz…');
      try {
        const data = await global.api.postJson(
          '/api/upcoming-releases/refresh-musicbrainz', {}, { timeoutMs: SCRAPE_TIMEOUT_MS }
        );
        if (data && data.success) {
          showStatus(
            '✓ Matched: ' + (data.matched || 0) +
            ' · Candidates: ' + (data.candidates || 0) +
            ' · Unmatched: ' + (data.unmatched || 0)
          );
        } else {
          showError((data && data.error) || 'Auto-match failed');
        }
      } catch (error) {
        showError(error.message || 'Auto-match failed');
      } finally {
        await refreshPage();
        hideStatus();
      }
    });
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  /** `data-action` in the markup → handler here. No globals are exported. */
  const ACTIONS = {
    'upcoming-refresh': (_button) => refreshPage(),
    'upcoming-check': (_button) => checkForUpdates(),
    'upcoming-scrape': (_button) => scrapeWikipedia(),
    'upcoming-auto-match': (button) => autoMatchAll(button),
    'upcoming-clear': (button) => clearDatabase(button),
  };

  function bindActions() {
    // One delegated listener for the toolbar, the tabs and the source filter.
    // Replaces five inline onclick attributes and one per-tab listener.
    document.addEventListener('click', (event) => {
      const actionEl = event.target.closest ? event.target.closest('[data-action]') : null;
      if (actionEl && ACTIONS[actionEl.getAttribute('data-action')]) {
        event.preventDefault();
        ACTIONS[actionEl.getAttribute('data-action')](actionEl);
        return;
      }

      const tab = event.target.closest ? event.target.closest('#upcomingTabs [data-filter]') : null;
      if (tab) {
        event.preventDefault();
        applyFilter(tab.getAttribute('data-filter'));
      }
    });

    const sourceSelect = document.getElementById('upcomingSourceFilter');
    if (sourceSelect) {
      sourceSelect.addEventListener('change', function () {
        state.source = this.value || 'all';
        refreshPage();
      });
    }
  }

  function shouldAutoCheck() {
    let lastChecked = null;
    try {
      lastChecked = localStorage.getItem(LAST_CHECKED_KEY);
    } catch (_error) {
      lastChecked = null;
    }
    if (!lastChecked) return true;
    const parsed = parseInt(lastChecked, 10);
    if (Number.isNaN(parsed)) return true;
    return (Date.now() - parsed) > AUTO_CHECK_INTERVAL_MS;
  }

  document.addEventListener('DOMContentLoaded', function () {
    bindActions();

    const svc = service();
    if (!svc) return;
    svc.onRefresh = refreshPage;

    loadSourceFilter();

    // Resume the badge if a scrape is already running (e.g. page reload
    // mid-scrape), otherwise do the once-a-day automatic refresh.
    updateProgressBadge().then((running) => {
      if (running) startProgressPolling();
    });

    if (shouldAutoCheck()) {
      setTimeout(checkForUpdates, AUTO_CHECK_AFTER_MS);
    } else {
      setTimeout(refreshPage, AUTO_CHECK_AFTER_MS);
    }
  });

  // Exposed for programmatic use (e.g. a future notification click-through).
  // These are NOT required by any inline handler — the markup uses data-action.
  global.upcomingPage = {
    refresh: refreshPage,
    checkForUpdates,
    scrapeWikipedia,
    autoMatchAll,
    clearDatabase,
    stopProgressPolling,
  };
})(window);
