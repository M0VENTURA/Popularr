/* ==========================================================================
   static/js/pages/playlist-import-csv.js
   Import from CSV — reads an Exportify CSV and QUEUES every track for download.

   Load order: utils/dom.js → utils/api.js → ui/toast.js → ui/modal.js, then
   this file. All come from base.html.

   ── THIS IS NOT features/csv-import.js. DO NOT MERGE THEM. ────────────────
   Both upload to the same endpoint — /api/playlist/import/csv — and both are
   called `importPlaylistFromCSV` in the original source. They then do completely
   different things with the result:

     playlists/importer_csv.html (this file)
         POST .../import/csv with skip_matching=true  → every track is QUEUED
         for download via /api/queue/add-batch. "Matching & tagging happens in
         the queue." This page never touches playlists.

     features/csv-import.js (the /downloads/search Playlists tab)
         POST .../import/csv                          → the result is MATCHED
         against the library and turned into a PLAYLIST. Missing tracks are
         listed separately for the user to queue by hand.

   They only avoided clashing because no page loads both. That is why this
   module exports `global.playlistQueueImport` and deliberately does NOT claim
   the global name `importPlaylistFromCSV`: if a future page ever loads both,
   the same name would silently mean two different things.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/playlists/importer_csv.html carried a 260-line inline <script> and a
   5-line inline <style>. The script is this file; the CSS is
   static/css/playlist-import.css (whose header notes the dropped :root block).

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. `escapeHtml` USED createTextNode + innerHTML — escapes `&`, `<`, `>` but
      not quotes. Here it only reached text position, so it was not exploitable,
      but it is replaced with utils/dom.js's shared helper.

   2. THE PROGRESS BAR COULD NOT REPORT A PARTIAL FAILURE HONESTLY. On a failed
      batch the code set `batchFailed = batch.length` — correct — but if the
      response was OK with an unexpected body and no `failed` key, the batch was
      counted as ZERO added and ZERO failed, so the totals silently under-reported
      and the run looked cleaner than it was. The "no breakdown in the response"
      case is now treated as a failure, matching the not-ok case.

   3. `alert()` was not used here, but TWO raw `fetch` calls were, each followed
      by an unconditional `.json()`. A non-JSON response (session expiry, HTML
      error page) reported a parse error instead of the real cause. Both use
      utils/api.js now — except the multipart upload, which must keep raw fetch
      because api.js JSON-encodes its body. That one is wrapped and its response
      parsed through `api.parseJsonResponse`.

   4. The form's `onsubmit` and the "Import CSV" buttons in the markup are now
      data-action / a bound submit listener rather than inline handlers.

   ── WHAT DELIBERATELY STAYS ───────────────────────────────────────────────
   * BATCH_SIZE = 10 and the per-batch progress update: the batches are what make
     the progress bar move and the failure counts meaningful.
   * `skip_matching=true` on the parse call — this page queues, it does not match.
   * Tracks are queued under ONE import_group (the playlist name) with
     import_type 'playlist', so they appear as a single group in the queue.
   * The year is the CURRENT year for every track: an Exportify CSV has no
     release year, and the queue is the thing that corrects it during matching.
   * The upload keeps raw fetch + FormData (see bug 3).
   ========================================================================== */

(function (global) {
  'use strict';

  const IMPORT_ENDPOINT = '/api/playlist/import/csv';
  const QUEUE_BATCH_ENDPOINT = '/api/queue/add-batch';
  const BATCH_SIZE = 10;
  /** The queue item's import_group is capped to keep the slug sane. */
  const IMPORT_GROUP_MAX = 80;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function byId(id) {
    return document.getElementById(id);
  }

  /** Restore the modal to its initial (form) state. */
  function resetCsvModal() {
    const form = byId('csvModalForm');
    const progress = byId('csvModalProgress');
    const result = byId('csvModalResult');
    if (form) form.style.display = '';
    if (progress) progress.style.display = 'none';
    if (result) result.style.display = 'none';

    const footer = byId('csvModalFooter');
    if (footer) {
      footer.innerHTML = `
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button type="submit" form="csvForm" class="btn btn-primary" id="csvSubmitBtn">
          <i class="bi bi-upload"></i> Import &amp; Queue All Tracks
        </button>`;
    }

    const formEl = byId('csvForm');
    if (formEl) formEl.reset();

    const bar = byId('csvProgressBar');
    if (bar) {
      bar.style.width = '0%';
      bar.setAttribute('aria-valuenow', 0);
      bar.textContent = '0%';
    }
    const detail = byId('csvProgressDetail');
    if (detail) detail.textContent = '';
  }

  function setProgress(label, sub, pct, detail) {
    const labelEl = byId('csvProgressLabel');
    if (labelEl) labelEl.textContent = label;

    if (sub !== undefined) {
      const subEl = byId('csvProgressSub');
      if (subEl) subEl.textContent = sub;
    }

    if (pct !== undefined) {
      const bar = byId('csvProgressBar');
      if (bar) {
        // Rounded here so no caller can render a fractional width or label.
        const value = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
        bar.style.width = value + '%';
        bar.setAttribute('aria-valuenow', value);
        bar.textContent = value + '%';
      }
    }

    if (detail !== undefined) {
      const detailEl = byId('csvProgressDetail');
      if (detailEl) detailEl.textContent = detail;
    }
  }

  /** Parse the CSV and queue every track. */
  async function importPlaylistFromCSV(event) {
    if (event) event.preventDefault();

    const fileInput = byId('csvFile');
    const nameInput = byId('csvPlaylistName');
    const importName = nameInput ? nameInput.value.trim() : '';

    if (!fileInput || !fileInput.files.length || !importName) return;

    // ── switch to the progress view ────────────────────────────────────────
    const formBody = byId('csvModalForm');
    const progressBody = byId('csvModalProgress');
    const footer = byId('csvModalFooter');
    if (formBody) formBody.style.display = 'none';
    if (progressBody) progressBody.style.display = '';
    // Nothing to click while it runs; the buttons come back with the result.
    if (footer) footer.innerHTML = '';

    try {
      // Step 1 — parse the CSV. skip_matching=true because this page QUEUES
      // rather than matching against the library.
      setProgress('Parsing CSV…', 'Reading track metadata from the file.', 0, '');

      const formData = new FormData();
      formData.append('file', fileInput.files[0]);
      formData.append('playlist_name', importName);
      formData.append('skip_matching', 'true');

      // Raw fetch is required: api.js JSON-encodes its body, and this is
      // multipart. The response is still parsed through api.parseJsonResponse so
      // an HTML error page is reported as such.
      const parseRes = await fetch(IMPORT_ENDPOINT, { method: 'POST', body: formData });
      const parseData = await global.api.parseJsonResponse(parseRes, 'CSV import');

      const allTracks = parseData.all_tracks || [];
      if (!allTracks.length) throw new Error('No tracks found in the CSV');

      // One import_group for the whole playlist, so the queue shows a single
      // group with every track nested under it.
      const nameSlug = importName.replace(/\s+/g, '_').substring(0, IMPORT_GROUP_MAX);
      const currentYear = String(new Date().getFullYear());
      const items = allTracks.map((track) => ({
        artist: track.artist,
        title: track.title,
        album: importName,
        album_artist: track.album_artist || 'Various Artists',
        year: currentYear,
        duration: track.duration_s || null,
        genres: track.genres || null,
        isrc: track.isrc || null,
        release_id: track.spotify_id || null,
        release_source: track.spotify_id ? 'spotify' : null,
      }));

      // Step 2 — queue in batches, so the bar moves and the counts are real.
      let totalAdded = 0;
      let totalSkipped = 0;
      let totalFailed = 0;
      let processed = 0;

      setProgress(
        'Adding to queue…',
        `Sending ${items.length} track${items.length !== 1 ? 's' : ''} to the download queue.`,
        2,
        'Starting…'
      );

      for (let i = 0; i < items.length; i += BATCH_SIZE) {
        const batch = items.slice(i, i + BATCH_SIZE);
        let added = 0;
        let skipped = 0;
        let failed = 0;

        try {
          const data = await global.api.postJson(QUEUE_BATCH_ENDPOINT, {
            items: batch,
            import_group: nameSlug,
            import_type: 'playlist',
            source: 'soulseek',
          });

          // A response with no breakdown cannot be trusted to mean "all fine" —
          // see bug 2. Anything without a numeric `failed` counts as a failure
          // for the whole batch rather than silently vanishing.
          if (data && typeof data === 'object' && 'failed' in data) {
            added = data.added || 0;
            skipped = data.skipped || 0;
            failed = data.failed || 0;
          } else {
            failed = batch.length;
          }
        } catch (error) {
          console.warn('[playlist-import] batch failed:', error);
          failed = batch.length;
        }

        totalAdded += added;
        totalSkipped += skipped;
        totalFailed += failed;
        processed += batch.length;

        setProgress(
          'Adding to queue…',
          `Sending ${items.length} track${items.length !== 1 ? 's' : ''} to the download queue.`,
          (processed / items.length) * 100,
          `Processed ${processed} of ${items.length}  •  Added ${totalAdded}  •  Failed ${totalFailed}`
        );
      }

      setProgress(
        'Finishing up…',
        '',
        100,
        `Processed ${processed} of ${items.length}  •  Added ${totalAdded}  •  Failed ${totalFailed}`
      );

      renderResult({ allTracks, importName, totalAdded, totalSkipped, totalFailed });
    } catch (error) {
      console.error('[playlist-import] import failed:', error);
      renderFailure(error);
    }
  }

  /** Result view: stat cards, the queued track list, and the page banner. */
  function renderResult({ allTracks, importName, totalAdded, totalSkipped, totalFailed }) {
    const progressBody = byId('csvModalProgress');
    const resultBody = byId('csvModalResult');
    if (progressBody) progressBody.style.display = 'none';
    if (resultBody) resultBody.style.display = '';

    const queueOk = totalFailed === 0;
    // Every track goes into ONE group (the playlist), so this is always 1.
    const albumCount = 1;
    const tone = queueOk ? 'success' : 'warning';

    const stats = byId('csvResultStats');
    if (stats) {
      stats.innerHTML = `
        <div class="col-6 col-md-4">
          <div class="card text-center bg-info bg-opacity-10 border-info h-100">
            <div class="card-body py-3">
              <div class="h3 text-info mb-1">${allTracks.length}</div>
              <div class="small text-secondary">Total Tracks</div>
            </div>
          </div>
        </div>
        <div class="col-6 col-md-4">
          <div class="card text-center bg-success bg-opacity-10 border-success h-100">
            <div class="card-body py-3">
              <div class="h3 text-success mb-1">${albumCount}</div>
              <div class="small text-secondary">Album Groups</div>
            </div>
          </div>
        </div>
        <div class="col-6 col-md-4">
          <div class="card text-center bg-${tone} bg-opacity-10 border-${tone} h-100">
            <div class="card-body py-3">
              <div class="h3 text-${tone} mb-1">${totalAdded}</div>
              <div class="small text-secondary">Added to Queue</div>
            </div>
          </div>
        </div>
        <div class="col-12">
          <div class="alert alert-${tone} mb-0 py-2">
            <i class="bi bi-${queueOk ? 'check-circle-fill' : 'exclamation-triangle-fill'} me-2"></i>
            ${queueOk
              ? `<strong>${totalAdded}</strong> track${totalAdded !== 1 ? 's' : ''} queued across
                 <strong>${albumCount}</strong> album group${albumCount !== 1 ? 's' : ''}.
                 ${totalSkipped > 0 ? `<span class="text-secondary">(${totalSkipped} already in queue)</span>` : ''}
                 Matching &amp; tagging can now be done in the queue.`
              : `Queued ${totalAdded}, skipped ${totalSkipped}, failed ${totalFailed}.`}
            <a href="/downloads" class="alert-link ms-2">View Queue →</a>
          </div>
        </div>`;
    }

    const queuedSection = byId('csvQueuedSection');
    if (queuedSection) queuedSection.style.display = '';

    const queuedList = byId('csvQueuedList');
    if (queuedList) {
      queuedList.innerHTML = allTracks.map((track) => {
        const meta = [
          track.album ? esc(track.album) : null,
          track.year ? esc(track.year) : null,
          track.genres ? esc(track.genres) : null,
        ].filter(Boolean).join(' · ');

        return `
          <div class="missing-row">
            <span class="fw-semibold">${esc(track.title)}</span>
            <span class="text-secondary ms-2">${esc(track.artist)}</span>
            ${meta ? `<div class="small text-tertiary mt-1">${meta}</div>` : ''}
          </div>`;
      }).join('');
    }

    const footer = byId('csvModalFooter');
    if (footer) {
      footer.innerHTML = `
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
        <a href="/downloads" class="btn btn-info">
          <i class="bi bi-cloud-arrow-down"></i> View Queue
        </a>`;
    }

    showBanner({
      tone,
      icon: 'check-circle-fill',
      html: `<strong>${esc(importName)}</strong> — ${totalAdded} track${totalAdded !== 1 ? 's' : ''} queued
             across ${albumCount} album group${albumCount !== 1 ? 's' : ''}.`,
    });
  }

  /** Put the form back and report the failure. */
  function renderFailure(error) {
    const progressBody = byId('csvModalProgress');
    const formBody = byId('csvModalForm');
    if (progressBody) progressBody.style.display = 'none';
    if (formBody) formBody.style.display = '';

    const footer = byId('csvModalFooter');
    if (footer) {
      footer.innerHTML = `
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button type="submit" form="csvForm" class="btn btn-primary">
          <i class="bi bi-upload"></i> Import &amp; Queue All Tracks
        </button>`;
    }

    showBanner({
      tone: 'danger',
      icon: 'exclamation-triangle-fill',
      html: `<strong>Error:</strong> ${esc(error.message)}`,
    });
  }

  function showBanner({ tone, icon, html }) {
    const banner = byId('importResultBanner');
    const alertEl = byId('importResultAlert');
    if (alertEl) {
      alertEl.className = `alert alert-${tone} mb-0`;
      alertEl.innerHTML = `<i class="bi bi-${icon} me-2"></i>${html}
        <a href="/downloads" class="alert-link ms-2">View Queue →</a>`;
    }
    if (banner) banner.style.display = '';
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    const modal = byId('csvImportModal');
    if (modal) {
      // Reset whenever the modal opens, so a previous run's result view is not
      // still on screen.
      modal.addEventListener('show.bs.modal', resetCsvModal);
    }

    const form = byId('csvForm');
    if (form) form.addEventListener('submit', importPlaylistFromCSV);

    const openBtn = byId('openCsvImportBtn');
    if (openBtn) openBtn.addEventListener('click', () => modal && global.modal.show(modal));

    // ?#import opens the modal directly (the nav links here for CSV import).
    if (global.location.hash === '#import' && modal) {
      global.modal.show(modal);
      global.history.replaceState(null, '', global.location.pathname + global.location.search);
    }
  });

  // NOTE the export name — see the collision warning at the top of this file.
  global.playlistQueueImport = { importPlaylistFromCSV, resetCsvModal };
})(window);
