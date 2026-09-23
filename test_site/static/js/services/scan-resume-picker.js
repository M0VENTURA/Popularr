/* ==========================================================================
   test_site/static/js/services/scan-resume-picker.js
   "Which artist would you like to resume from?" prompt.

   ── WHEN IT APPEARS ───────────────────────────────────────────────────────
   On the dashboard, when a scan is started with **Restart UNCHECKED** — i.e.
   the user asked to continue rather than start over. Restart already means
   "from the top", so prompting there would be contradictory.

   ── WHAT IT OFFERS ────────────────────────────────────────────────────────
   At the top, the artist the interrupted FULL scan stopped at; then the most
   recently touched artists (manually scanned, or with albums scanned). Each row
   states what will happen to it, because the two cases genuinely differ:

   * **completed** — "Continue from the next artist" (re-scanning it would
     redo finished work)
   * **interrupted** — "Restart this artist" then carry on with the full scan

   That resolution happens server-side (`resolve_resume_target`) so the rule
   lives in one place; this module only renders it and passes the resolved
   ``resume_from`` back.

   ── WHY resume_from IS NOT ALWAYS THE CHOSEN ARTIST ───────────────────────
   The scan loop skips every artist BEFORE ``resume_from`` and PROCESSES
   ``resume_from`` itself. So "continue from the next artist" is expressed by
   sending that next artist, while "restart this artist" sends the artist
   itself. The server computes which; the client must not guess.

   ── SELF-CONTAINED BY DESIGN ──────────────────────────────────────────────
   The rebuilt tree has a `global.modal` helper; the live tree does not. Rather
   than depend on it, this module builds its own Bootstrap modal and falls back
   to the native prompt when Bootstrap is absent — so ONE implementation serves
   both trees with identical behaviour.
   ========================================================================== */

(function (global) {
  'use strict';

  const MODAL_ID = 'scanResumePickerModal';
  const ENDPOINT = '/api/scan/resume-options';

  function esc(value) {
    if (global.escapeHtml) return global.escapeHtml(value);
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(message, kind) {
    if (kind === 'error') {
      if (global.toast && global.toast.error) return global.toast.error(message);
      if (typeof global.showToast === 'function') return global.showToast('Scan', message, 'error');
      return;
    }
    if (global.toast && global.toast.success) return global.toast.success(message);
  }

  function hasBootstrap() {
    return !!(global.bootstrap && global.bootstrap.Modal);
  }

  /** Fetch the candidate list. Returns null when the probe fails. */
  async function fetchOptions() {
    try {
      const response = await fetch(ENDPOINT, {
        headers: { 'Accept': 'application/json' },
        credentials: 'same-origin',
      });
      if (!response.ok) return null;
      const data = await response.json();
      return (data && data.success !== false) ? data : null;
    } catch (_error) {
      return null;
    }
  }

  function sourceBadge(option) {
    if (option.source === 'full_scan') {
      return '<span class="badge bg-info text-dark ms-1" style="font-size:.65rem;">' +
        'last full scan</span>';
    }
    return '<span class="badge bg-secondary ms-1" style="font-size:.65rem;">' +
      'recent</span>';
  }

  function modeBadge(option) {
    if (option.mode === 'next') {
      return '<span class="badge bg-success" style="font-size:.65rem;">' +
        '<i class="bi bi-arrow-right-short"></i> finished — continue from the next artist</span>';
    }
    if (option.mode === 'restart') {
      return '<span class="badge bg-warning text-dark" style="font-size:.65rem;">' +
        '<i class="bi bi-arrow-counterclockwise"></i> ' +
        'restart this artist</span>';
    }
    return '';
  }

  function buildRows(options) {
    return options.map((option, index) => {
      const checked = index === 0 ? ' checked' : '';
      const albums = option.album_count
        ? `<span class="text-muted ms-1">${option.album_count} album${
            option.album_count === 1 ? '' : 's'}</span>`
        : '';
      const missing = option.in_library === false
        ? '<span class="badge bg-danger ms-1" style="font-size:.65rem;">not in library</span>'
        : '';

      return `
        <label class="list-group-item list-group-item-action d-flex gap-2 align-items-start"
               style="cursor:pointer;">
          <input class="form-check-input mt-1" type="radio" name="scanResumeArtist"
                 value="${esc(option.artist)}"
                 data-resume-from="${esc(option.resume_from || option.artist)}"
                 data-mode="${esc(option.mode || '')}"${checked}>
          <span class="flex-grow-1">
            <span class="d-block fw-semibold">${esc(option.artist)}${sourceBadge(option)}${missing}</span>
            <span class="d-block small">${modeBadge(option)}${albums}</span>
            <span class="d-block text-muted" style="font-size:.75rem;">${esc(option.reason || '')}</span>
          </span>
        </label>`;
    }).join('');
  }

  function buildHtml(options) {
    return `
      <div class="modal fade" id="${MODAL_ID}" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-lg modal-dialog-centered modal-dialog-scrollable">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title">Resume scan — choose an artist</h5>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">
              <p class="mb-2">The scan will continue from the artist you select.</p>
              <div class="list-group">${buildRows(options)}</div>
              <p class="text-muted small mt-2 mb-0">
                Artists marked <em>finished</em> are skipped — the scan continues from the
                artist after them. Artists marked <em>restart</em> are scanned again from the
                beginning, then the scan carries on.
              </p>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
              <button type="button" class="btn btn-primary" data-resume-accept>
                <i class="bi bi-play-fill"></i> Resume scan</button>
            </div>
          </div>
        </div>
      </div>`;
  }

  /** Remove any previous instance, disposing Bootstrap's copy first. */
  function destroyExisting() {
    const existing = document.getElementById(MODAL_ID);
    if (!existing) return;
    if (hasBootstrap()) {
      const instance = global.bootstrap.Modal.getInstance(existing);
      if (instance) {
        try { instance.dispose(); } catch (_e) { /* already gone */ }
      }
    }
    existing.remove();
    // Removing the node alone can leave the backdrop and the scroll lock.
    document.querySelectorAll('.modal-backdrop').forEach((el) => el.remove());
    document.body.classList.remove('modal-open');
    document.body.style.removeProperty('overflow');
    document.body.style.removeProperty('padding-right');
  }

  /**
   * Show the picker.
   *
   * @returns {Promise<{resume_from: string, mode: string}|null>}
   *   null when the user cancelled or there was nothing to choose.
   */
  function showPicker(options) {
    return new Promise((resolve) => {
      // No Bootstrap: degrade to the native prompt rather than silently
      // resuming from an artist the user was never shown.
      if (!hasBootstrap()) {
        const names = options.map((o) => o.artist);
        const picked = global.prompt(
          'Resume scan from which artist?\n\n' + names.map((n) => `  • ${n}`).join('\n'),
          names[0] || ''
        );
        if (picked === null) return resolve(null);
        const match = options.find((o) => o.artist === picked.trim());
        resolve(match
          ? { resume_from: match.resume_from || match.artist, mode: match.mode || '' }
          : null);
        return;
      }

      destroyExisting();
      document.body.insertAdjacentHTML('beforeend', buildHtml(options));
      const el = document.getElementById(MODAL_ID);
      if (!el) return resolve(null);

      const instance = new global.bootstrap.Modal(el);
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        try { instance.hide(); } catch (_e) { /* fine */ }
        resolve(value);
      };

      const accept = el.querySelector('[data-resume-accept]');
      if (accept) {
        accept.addEventListener('click', () => {
          const chosen = el.querySelector('input[name="scanResumeArtist"]:checked');
          if (!chosen) {
            toast('Choose an artist to resume from.', 'error');
            return;
          }
          finish({
            resume_from: chosen.dataset.resumeFrom || chosen.value,
            mode: chosen.dataset.mode || '',
          });
        });
      }

      // Cancelled (X, backdrop, Cancel, Esc) must NOT start a scan — the user
      // backed out of a decision they were explicitly asked to make.
      el.addEventListener('hidden.bs.modal', () => {
        destroyExisting();
        finish(null);
      }, { once: true });

      instance.show();
    });
  }

  /**
   * Ask which artist to resume from.
   *
   * @returns {Promise<{resume_from, mode}|null>} null = do not start.
   */
  async function chooseResumeArtist() {
    const data = await fetchOptions();
    if (!data || !data.has_options || !(data.artists || []).length) {
      // Nothing recorded to resume from — let the caller start normally and
      // use the stored checkpoint. Not an error.
      return { resume_from: null, mode: '' };
    }
    return showPicker(data.artists);
  }

  global.ScanResumePicker = {
    chooseResumeArtist,
    showPicker,
    fetchOptions,
    MODAL_ID,
  };
})(window);
