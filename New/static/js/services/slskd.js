/* ==========================================================================
   static/js/services/slskd.js
   Soulseek (slskd) search, polling and download.

   Load order:

       <script src="{{ url_for('static', filename='js/utils/dom.js') }}"></script>
       <script src="{{ url_for('static', filename='js/utils/api.js') }}"></script>
       <script src="{{ url_for('static', filename='js/utils/poller.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/toast.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/confirm.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/button-state.js') }}"></script>
       <script src="{{ url_for('static', filename='js/services/slskd.js') }}"></script>

   Replaces THREE independent Soulseek search implementations:

       artist_detail.html  performSlskdSearch / pollSlskdResults /
                           downloadSlskdFile (x2) / pollSlskdResultsForDownload
       downloads_page.js   performSlskdSearch / pollSlskdResults /
                           displaySlskdResponses / downloadSlskdBatch
       downloads.js        searchSoulseek / pollSlskdSearchResults

   ── THE SHARED-STATE HAZARD THIS REMOVES ──────────────────────────────────
   `currentSlskdSearchId` and `slskdPollInterval` are declared with `let` at
   the top level of downloads.js and deliberately NOT redeclared in
   downloads_page.js — both files carry comments explaining that redeclaring
   them throws "Identifier has already been declared" and kills the whole
   file. Two files therefore share one mutable search handle by convention.
   On any page loading both, whichever searches last owns the id, and either
   can clear the other's poll interval mid-flight.

   Here that state is private to the module and scoped per search session.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. downloadSlskdFile IS DEFINED TWICE IN artist_detail.html.
        line ~3075  downloadSlskdFile(username, filename, size)
        line ~4843  downloadSlskdFile(username, filename, size, btn, originalHTML)
      The second overwrites the first for the whole page. pollSlskdResults
      renders buttons calling it with THREE arguments, so `btn` is undefined
      and `btn.disabled = false` throws inside the .then(). The manual
      Soulseek search results are therefore un-downloadable on the artist
      page. Reconciled into one function with an optional button.

   2. THREE DIFFERENT ENDPOINTS for the same action:
        artist_detail    /api/slskd/download-single   { username, filename, size }
        downloads_page   /api/slskd/download          { files: [...] }
        downloads.js     /api/slskd/queue-download    when a queueId is present
      A single download is just a batch of one, so this module always uses
      the batch endpoint and keeps queue-download for the queue-linked case.
      If /api/slskd/download-single has behaviour the batch route lacks,
      check that before deleting the old call sites.

   3. GRACE COUNTER LEAKED BETWEEN SEARCHES. `_slskdCompleteGraceCount` is
      module-level in downloads.js; its own comment records that an
      exhausted search left it at the cap so the NEXT search declared
      "no results" on its first terminal poll. It is now per-session state.

   4. THE GRACE WINDOW ITSELF was only implemented in two of the three
      copies. slskd flips a search to a terminal state ("Completed,
      TimedOut") while responses are still streaming in; stopping at the
      first terminal poll shows "No results" when real files are arriving.
      artist_detail had no grace window at all.

   5. downloads.js filtered out results with zero free upload slots; the
      other two did not, offering files that cannot start. Filtering is now
      opt-in per call via `filterBusy`, defaulting ON.
   ========================================================================== */

(function (global) {
  'use strict';

  const SEARCH_ENDPOINT = '/api/slskd/search';
  const SLOT_ENDPOINT = '/api/slskd/search-slot';
  const RESULT_ENDPOINT = '/api/slskd/search/';
  const DOWNLOAD_ENDPOINT = '/api/slskd/download';
  const QUEUE_DOWNLOAD_ENDPOINT = '/api/slskd/queue-download';

  const POLL_INTERVAL_MS = 1000;
  const MAX_POLL_ATTEMPTS = 120;
  /** Terminal-but-empty polls to tolerate before declaring "no results". */
  const TERMINAL_GRACE_POLLS = 8;
  const SLOT_RETRY_INTERVAL_MS = 2000;
  const SLOT_MAX_RETRIES = 30;
  /**
   * How many times a single search() call may re-enter itself after a busy
   * slot. Without this the retry is unbounded: all three original copies
   * called themselves again with no depth limit, so a slot that reports
   * "free" and then immediately busy again loops forever, issuing requests
   * until the tab is closed.
   */
  const SLOT_MAX_REENTRIES = 3;
  const BYTES_TO_MB = 1024 * 1024;

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

  function formatSize(bytes) {
    if (global.formatBytes) return global.formatBytes(bytes);
    const n = Number(bytes);
    return Number.isFinite(n) && n > 0 ? `${(n / BYTES_TO_MB).toFixed(2)} MB` : '—';
  }

  /**
   * Strip characters that break slskd's query parser.
   * Ampersands in particular return zero results rather than an error.
   */
  function normalizeQuery(value) {
    return String(value || '')
      .replace(/&amp;/gi, ' ')
      .replace(/&/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  /**
   * A stable composite key for the selection map. A NUL separator cannot
   * appear in a Soulseek username or filename, so "a\0b" and "a" + "\0b"
   * cannot collide the way "-" or "/" could.
   */
  function selectionKey(username, filename) {
    return `${String(username || '')}\u0000${String(filename || '')}`;
  }

  // ── Search sessions ───────────────────────────────────────────────────

  /**
   * Each search gets its own session object. Nothing is shared between
   * searches or between pages — this is what removes the cross-file
   * `currentSlskdSearchId` hazard.
   */
  function createSession() {
    return {
      searchId: null,
      poll: null,
      graceCount: 0,
      results: [],
      responseCount: 0,
      isComplete: false,
      state: 'Searching',
      cancelled: false,
    };
  }

  /** The session for each named search context (e.g. 'artist', 'downloads'). */
  const sessions = Object.create(null);

  function sessionFor(context) {
    return sessions[context || 'default'] || null;
  }

  /**
   * Cancel any in-flight search for a context and start a fresh session.
   * @param {string} context
   * @returns {Object} the new session
   */
  function resetSession(context) {
    const key = context || 'default';
    const previous = sessions[key];
    if (previous) {
      previous.cancelled = true;
      if (previous.poll) previous.poll.stop();
    }
    sessions[key] = createSession();
    return sessions[key];
  }

  // ── Search ────────────────────────────────────────────────────────────

  /**
   * Run a Soulseek search and poll until it completes.
   *
   * @param {string} rawQuery
   * @param {Object} [opts]
   * @param {string}   [opts.context='default']  isolates concurrent searches
   * @param {Function} [opts.onUpdate]  (session) => void, every poll
   * @param {Function} [opts.onComplete](session) => void
   * @param {Function} [opts.onError]   (Error) => void
   * @param {boolean}  [opts.filterBusy=true] drop results with no free slots
   * @param {boolean}  [opts.waitForSlot=true] retry when the slot is busy
   * @param {number}   [opts._retryDepth=0]  internal; busy-slot re-entry count
   * @returns {Promise<Object>} the finished session
   */
  async function search(rawQuery, opts = {}) {
    const query = normalizeQuery(rawQuery);
    if (!query) {
      notifyError('Please enter a search query');
      return null;
    }

    const context = opts.context || 'default';
    const session = resetSession(context);

    const fail = (error) => {
      if (typeof opts.onError === 'function') opts.onError(error);
      else notifyError(error.message);
    };

    let data;
    try {
      data = await global.api.postJson(SEARCH_ENDPOINT, { query });
    } catch (error) {
      fail(error);
      return null;
    }

    if (data.error) {
      fail(new Error(data.error));
      return null;
    }

    // Busy slot: wait for it to free, then retry THIS search.
    //
    // downloads_page.js used to call searchSoulseek() here, which belongs to
    // the other Soulseek UI in downloads.js — it reads #slskdSearchQuery and
    // writes #slskdSearchResults, neither of which exists on that page. A
    // busy slot therefore silently switched to a dead search path.
    if (data.slotBusy) {
      const depth = opts._retryDepth || 0;
      if (opts.waitForSlot === false) {
        fail(new Error('Soulseek search slot is busy'));
        return null;
      }
      if (depth >= SLOT_MAX_REENTRIES) {
        fail(new Error(
          'Soulseek search slot is still busy after several attempts. Try again shortly.'
        ));
        return null;
      }
      if (typeof opts.onUpdate === 'function') {
        session.state = 'Waiting for a free search slot…';
        opts.onUpdate(session);
      }
      try {
        await global.poller.until(
          () => global.api.getJson(SLOT_ENDPOINT),
          (slot) => slot && slot.slotFree,
          {
            interval: SLOT_RETRY_INTERVAL_MS,
            maxAttempts: SLOT_MAX_RETRIES,
            timeoutMessage: 'Soulseek search slot did not free up. Try again shortly.',
          }
        );
      } catch (error) {
        fail(error);
        return null;
      }
      if (session.cancelled) return null;
      return search(rawQuery, Object.assign({}, opts, { _retryDepth: depth + 1 }));
    }

    session.searchId = data.searchId;
    if (!session.searchId) {
      fail(new Error('Search did not return an id'));
      return null;
    }

    return pollUntilComplete(session, opts);
  }

  /**
   * Poll a session's search id until the search finishes.
   * @returns {Promise<Object>} the session
   */
  function pollUntilComplete(session, opts = {}) {
    const filterBusy = opts.filterBusy !== false;

    return new Promise((resolve) => {
      session.poll = global.poller.create({
        interval: POLL_INTERVAL_MS,
        maxAttempts: MAX_POLL_ATTEMPTS,
        onTick: async (ctx) => {
          if (session.cancelled) return ctx.stop();

          const data = await global.api.getJson(
            RESULT_ENDPOINT + encodeURIComponent(session.searchId)
          );
          if (data.error) throw new Error(data.error);

          const raw = data.results || [];
          session.results = filterBusy
            ? raw.filter((r) => (r.freeUploadSlots === undefined ? 1 : r.freeUploadSlots) > 0)
            : raw;
          session.responseCount = data.responseCount || 0;
          session.isComplete = data.isComplete || false;
          session.state = data.state || 'Searching';

          if (typeof opts.onUpdate === 'function') opts.onUpdate(session);

          if (!session.isComplete) {
            session.graceCount = 0;
            return;
          }

          // slskd reports a terminal state while responses are still
          // streaming in. Keep polling briefly before declaring failure.
          if (session.results.length === 0 && session.graceCount < TERMINAL_GRACE_POLLS) {
            session.graceCount += 1;
            return;
          }

          ctx.stop();
          if (typeof opts.onComplete === 'function') opts.onComplete(session);
          resolve(session);
        },
        onTimeout: () => {
          session.isComplete = true;
          session.state = 'Timed out';
          if (typeof opts.onComplete === 'function') opts.onComplete(session);
          resolve(session);
        },
        onError: (error) => {
          // Transient poll failures are normal on a busy slskd instance.
          // Only surface them; the loop keeps going until maxAttempts.
          console.warn('[slskd] poll error:', error.message);
        },
      });
      session.poll.start();
    });
  }

  /** Stop an in-flight search. */
  function cancel(context) {
    const session = sessionFor(context);
    if (!session) return;
    session.cancelled = true;
    if (session.poll) session.poll.stop();
  }

  // ── Grouping ──────────────────────────────────────────────────────────

  /**
   * Group flat results by username, then by containing folder.
   * Used by the downloads page's album-oriented view.
   *
   * @param {Array<Object>} results
   * @returns {Object} username -> { username, albums, albumOrder, totalFiles }
   */
  function groupByUser(results) {
    const grouped = Object.create(null);

    (results || []).forEach((result) => {
      const username = result.username;
      if (!username) return;

      // Soulseek paths use backslashes; some clients report forward slashes.
      const parts = String(result.filename || '').split(/[\\/]/);
      const trackName = parts.pop() || result.filename;
      const albumPath = parts.length ? parts.join('/') : '(Unknown folder)';
      const albumKey = albumPath || '(Unknown folder)';

      if (!grouped[username]) {
        grouped[username] = { username, albums: {}, albumOrder: [], totalFiles: 0 };
      }
      const user = grouped[username];
      if (!user.albums[albumKey]) {
        user.albums[albumKey] = { key: albumKey, displayName: albumPath, files: [] };
        user.albumOrder.push(albumKey);
      }
      user.albums[albumKey].files.push(
        Object.assign({}, result, { trackName, albumPath, albumKey })
      );
      user.totalFiles += 1;
    });

    return grouped;
  }

  /**
   * Pick the single best result for an automated download.
   * The API ranks by relevance, so the first playable result wins; ties
   * break on bitrate then size.
   *
   * @param {Array<Object>} results
   * @returns {Object|null}
   */
  function bestMatch(results) {
    const playable = (results || []).filter(
      (r) => (r.freeUploadSlots === undefined ? 1 : r.freeUploadSlots) > 0
    );
    if (!playable.length) return null;
    return playable.slice().sort((a, b) => {
      const bitrate = (Number(b.bitrate) || 0) - (Number(a.bitrate) || 0);
      if (bitrate !== 0) return bitrate;
      return (Number(b.size) || 0) - (Number(a.size) || 0);
    })[0];
  }

  // ── Rendering ─────────────────────────────────────────────────────────

  /**
   * Render a flat results table into a container.
   *
   * Consolidates the two near-identical table builders in artist_detail and
   * downloads.js. Buttons carry data-* attributes and are wired with
   * addEventListener — the old markup used inline onclick with
   * escapeJsString, which broke on filenames containing quotes.
   *
   * @param {HTMLElement|string} target
   * @param {Object} session
   * @param {Object} [opts]
   */
  function renderTable(target, session, opts = {}) {
    const container = typeof target === 'string' ? document.getElementById(target) : target;
    if (!container) return;

    const results = session.results || [];
    if (!results.length) {
      container.innerHTML =
        '<div class="alert alert-info"><i class="bi bi-info-circle"></i> ' +
        `No results found${session.state ? ` (${esc(session.state)})` : ''}.</div>`;
      return;
    }

    const rows = results.map((result, index) => {
      const sizeMB = result.size_mb
        || (result.size ? (result.size / BYTES_TO_MB).toFixed(2) : 'N/A');
      return `
        <tr>
          <td data-label="File">
            <div class="small text-truncate" style="max-width:500px;">
              ${esc(result.filename || 'Unknown')}
            </div>
          </td>
          <td class="text-center" data-label="User">
            <small class="text-muted">${esc(result.username || 'N/A')}</small>
          </td>
          <td class="text-center" data-label="Size">${esc(String(sizeMB))} MB</td>
          <td class="text-center" data-label="Bitrate">
            <small class="text-muted">${esc(String(result.bitrate || 'unknown'))}</small>
          </td>
          <td class="text-center" data-label="Action">
            <button class="btn btn-sm btn-success slskd-download-btn" data-result-index="${index}">
              <i class="bi bi-download"></i> Download
            </button>
          </td>
        </tr>`;
    }).join('');

    container.innerHTML = `
      <div class="table-responsive">
        <table class="table table-hover" data-mobile-cards>
          <thead>
            <tr>
              <th>File</th>
              <th class="text-center">User</th>
              <th class="text-center">Size</th>
              <th class="text-center">Bitrate</th>
              <th class="text-center">Action</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="text-muted small mt-2">
        Found ${results.length} result(s) — ${esc(session.state)}${session.isComplete ? ' (complete)' : ''}
      </div>`;

    container.querySelectorAll('.slskd-download-btn').forEach((button) => {
      button.addEventListener('click', function () {
        const result = results[parseInt(this.dataset.resultIndex, 10)];
        if (!result) return;
        return global.buttonState.withBusy(this, '', async () => {
          // opts.queueId is forwarded so the manual-search modal can attach the
          // chosen file to the queue row it was opened from (that routes the
          // request to /api/slskd/queue-download instead of /api/slskd/download
          // — see download()). Without it the modal's Download button queued a
          // brand-new item and orphaned the row the user was fixing.
          const ok = await download(
            [{ username: result.username, filename: result.filename, size: result.size || 0 }],
            { label: result.filename, confirm: opts.confirm !== false, queueId: opts.queueId }
          );
          if (ok) global.buttonState.flashDone(this);
        });
      });
    });
  }

  // ── Download ──────────────────────────────────────────────────────────

  /**
   * Enqueue one or more files for download.
   *
   * A single download is a batch of one — the separate
   * /api/slskd/download-single path is not used.
   *
   * @param {Array<Object>} files  [{ username, filename, size }]
   * @param {Object} [opts]
   * @param {boolean} [opts.confirm=true]
   * @param {string}  [opts.label]    used in the confirmation text
   * @param {string|number} [opts.queueId] routes to the queue-download endpoint
   * @returns {Promise<boolean>}
   */
  async function download(files, opts = {}) {
    if (!files || !files.length) return false;

    if (opts.confirm !== false) {
      const label = opts.label || `${files.length} file${files.length === 1 ? '' : 's'}`;
      const confirmFn = (global.ui && global.ui.confirm)
        ? global.ui.confirm
        : (message) => Promise.resolve(global.confirm(message));

      const accepted = await confirmFn({
        title: 'Download from Soulseek',
        message: `Download ${label}?`,
        detail: files.length > 1
          ? `${files.length} files will be added to the download queue.`
          : undefined,
        items: files.length > 1 ? files.map((f) => f.filename) : undefined,
        tone: 'primary',
        confirmLabel: 'Download',
      });
      if (!accepted) return false;
    }

    const endpoint = opts.queueId ? QUEUE_DOWNLOAD_ENDPOINT : DOWNLOAD_ENDPOINT;
    const payload = opts.queueId
      ? { files: files, queue_id: opts.queueId }
      : { files: files };

    try {
      const data = await global.api.postJson(endpoint, payload);
      if (!data.success && data.error) {
        notifyError('Error: ' + data.error);
        return false;
      }
      notifySuccess(
        files.length === 1
          ? 'Download started. Check the Downloads page for progress.'
          : `${files.length} downloads enqueued.`
      );
      return true;
    } catch (error) {
      notifyError('Error: ' + error.message);
      return false;
    }
  }

  /**
   * Search for a track and download the best match automatically.
   *
   * Replaces artist_detail's downloadTrack + pollSlskdResultsForDownload
   * pair, whose button handling was broken by the duplicate
   * downloadSlskdFile definition.
   *
   * @param {string} artist
   * @param {string} title
   * @param {Object} [opts]
   * @param {HTMLElement} [opts.button]
   * @returns {Promise<boolean>}
   */
  async function searchAndDownloadBest(artist, title, opts = {}) {
    const query = normalizeQuery(`${artist} ${title}`);

    const run = async () => {
      const session = await search(query, {
        context: 'auto',
        filterBusy: true,
        waitForSlot: false,
      });
      if (!session) return false;

      const best = bestMatch(session.results);
      if (!best) {
        notifyError('No results found. Try searching manually.');
        return false;
      }
      return download(
        [{ username: best.username, filename: best.filename, size: best.size || 0 }],
        { confirm: false }
      );
    };

    if (opts.button && global.buttonState) {
      return global.buttonState.withBusy(opts.button, '', run);
    }
    return run();
  }

  // ── Selection helpers ─────────────────────────────────────────────────

  /**
   * A selection set for the grouped downloads view.
   * Previously a module-level `const slskdSelected = new Map()` shared by
   * every view on the page.
   */
  function createSelection() {
    const map = new Map();
    return {
      toggle(username, filename, size, checked) {
        const key = selectionKey(username, filename);
        if (checked) map.set(key, { username, filename, size: size || 0 });
        else map.delete(key);
        return map.size;
      },
      has(username, filename) { return map.has(selectionKey(username, filename)); },
      clear() { map.clear(); },
      get size() { return map.size; },
      values() { return Array.from(map.values()); },
    };
  }

  // ── Manual search modal ──────────────────────────────────────────────
  //
  // IMPLEMENTED 2026-09-18 — this was an un-migrated flow, not a broken one.
  //
  // Two callers have been waiting for it, both behind a `typeof` guard so they
  // silently did nothing:
  //     pages/download-queue.js  manualQueueSearch(query, queueId)   line 755
  //     pages/search.js          the search tab's per-track search   line 53
  //
  // The legacy implementation lived in static/js/downloads.js
  // (ensureSoulseekManualSearchModal / openSoulseekManualSearchModal /
  // runSoulseekManualSearch) and was never carried across. Its modal markup is
  // now a proper partial — components/modals/_soulseek_manual_search.html — so
  // there is no ensure*() function here: the template owns the markup, this
  // module only drives it.
  //
  // The `queueId` is the whole point of the modal. Opened from a queue row, it
  // lets the user pick a DIFFERENT file for an item that failed to match, and
  // /api/slskd/queue-download then attaches that file to the existing row
  // rather than creating a second one.

  const MANUAL_CONTEXT = 'manual-search';
  const manualState = { queueId: null };

  function openManualSearchModal(defaultQuery, queueId) {
    const modalEl = document.getElementById('soulseekManualSearchModal');
    if (!modalEl) {
      notifyError('The manual search modal is not loaded on this page.');
      return;
    }

    manualState.queueId = queueId ? (parseInt(queueId, 10) || null) : null;

    const input = document.getElementById('soulseekManualQuery');
    if (input) input.value = normalizeQuery(defaultQuery || '');

    const status = document.getElementById('soulseekManualStatus');
    if (status) {
      status.textContent = manualState.queueId
        ? 'Linked to a queue item — the file you pick replaces its download.'
        : 'Edit the query if needed, then click Search.';
    }

    const results = document.getElementById('soulseekManualResults');
    if (results) results.innerHTML = '';

    if (global.modal) global.modal.show(modalEl);
    if (input) input.focus();
  }

  async function runManualSearch() {
    const input = document.getElementById('soulseekManualQuery');
    const status = document.getElementById('soulseekManualStatus');
    const results = document.getElementById('soulseekManualResults');
    const btn = document.getElementById('soulseekManualSearchBtn');

    const query = normalizeQuery(input ? input.value : '');
    if (!query) {
      notifyError('Enter search terms first.');
      return;
    }
    if (input) input.value = query;
    if (results) results.innerHTML = '';

    const setStatus = (html) => { if (status) status.innerHTML = html; };
    setStatus('<span class="spinner-border spinner-border-sm me-1"></span>Starting search…');

    // search() owns the poll loop, the terminal-state grace window and the
    // busy-slot retry — the legacy version hand-rolled all three, including a
    // `waitingForSlot` flag and a poll timer on window.soulseekManualSearchState.
    return global.buttonState.withBusy(btn, '', () => search(query, {
      context: MANUAL_CONTEXT,
      onUpdate: (session) => {
        setStatus(
          `<span class="spinner-border spinner-border-sm me-1"></span>Searching… ` +
          `(${session.results.length} file${session.results.length === 1 ? '' : 's'})`
        );
      },
      onComplete: (session) => {
        setStatus(manualState.queueId
          ? 'Pick a file to attach to the queue item.'
          : 'Pick a file to download.');
        renderTable(results, session, { queueId: manualState.queueId, confirm: true });
      },
      onError: (error) => {
        setStatus(`<span class="text-danger">Search failed: ${esc(error.message)}</span>`);
      },
    }));
  }

  /** Enter submits, matching every other search box in the app. */
  function initManualSearch() {
    const modalEl = document.getElementById('soulseekManualSearchModal');
    if (!modalEl) return;

    const input = document.getElementById('soulseekManualQuery');
    if (input) {
      input.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        runManualSearch();
      });
    }

    const btn = document.getElementById('soulseekManualSearchBtn');
    if (btn) btn.addEventListener('click', () => runManualSearch());

    // Drop the queue link when the modal closes, so the NEXT open (from a
    // different row, or from the search page where queueId is null) cannot
    // attach a file to a row the user has moved on from. The legacy version
    // reset its window state here for the same reason.
    modalEl.addEventListener('hidden.bs.modal', () => {
      manualState.queueId = null;
      cancel(MANUAL_CONTEXT);
    });
  }

  document.addEventListener('DOMContentLoaded', initManualSearch);

  global.slskd = {
    search,
    cancel,
    download,
    searchAndDownloadBest,
    renderTable,
    groupByUser,
    bestMatch,
    createSelection,
    normalizeQuery,
    selectionKey,
    formatSize,
    getSession: sessionFor,
    POLL_INTERVAL_MS,
    TERMINAL_GRACE_POLLS,
    openManualSearchModal,
    runManualSearch,
  };

  // The two call sites were written against these names before the modal
  // existed, so keep them:
  //   manualQueueSearch() in pages/download-queue.js
  //   the per-track search in pages/search.js
  global.openSoulseekManualSearchModal = openManualSearchModal;
  global.runSoulseekManualSearch = runManualSearch;

  // ── Legacy aliases ────────────────────────────────────────────────────
  // downloadSlskdFile reconciled from the TWO conflicting definitions in
  // artist_detail.html: the 3-arg form (confirms, no button) and the 5-arg
  // form (no confirm, updates a button). Both signatures work here.
  global.downloadSlskdFile = function (username, filename, size, btn) {
    const run = () => download(
      [{ username, filename, size: parseInt(size, 10) || 0 }],
      { confirm: !btn, label: filename }
    );
    return btn && global.buttonState
      ? global.buttonState.withBusy(btn, '', run)
      : run();
  };
  global.normalizeSoulseekQuery = normalizeQuery;
})(window);
