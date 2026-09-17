/* ==========================================================================
   static/js/ui/button-state.js
   Busy/spinner state for buttons.

   Load AFTER utils/dom.js:

       <script src="{{ url_for('static', filename='js/ui/button-state.js') }}"></script>

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   The same six lines appear 19 times across the codebase:

       const originalHtml = btn.innerHTML;
       btn.disabled = true;
       btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> …';
       ... await something ...
       btn.disabled = false;
       btn.innerHTML = originalHtml;

   The copies are NOT equivalent, and the differences are bugs:

   1. RESTORE ON FAILURE. Only some wrap the restore in `.finally()`.
      `fetchArtistGenreRecommendations`, `applySelectedArtistGenres` and
      `applySelectedArtistSourceTags` restore the button by hand in EACH
      branch — and each hard-codes the label rather than reusing the
      captured `originalHtml`, so two of them restore the WRONG text:
      applySelectedArtistGenres restores "Apply Selected to All Artist
      Tracks (MP3 Files)" over a button whose real label ends
      "(MP3/FLAC Files)".

   2. IMPLICIT `event`. Five call sites resolve their own button from the
      global `event` object:
          importMissingRelease   `event.target.closest('button')`
          createEssentialPlaylist `event.target.closest('button')`
          downloadTrack          `event.target.closest('button')`
          checkMissingReleases   `window.event?.target?.closest('button')`
          importRelease          `window.event ? … : document.activeElement…`
      `window.event` is a legacy non-standard global. It is undefined in
      Firefox and in any async continuation, so these throw
      "Cannot read properties of undefined (reading 'target')" — and
      checkMissingReleases is called on page load with no event at all,
      where its optional chaining silently yields `undefined` and the
      button never shows progress.

   3. DOUBLE-CLICK. Nothing guards re-entry, so a second click while a
      request is in flight captures the SPINNER as `originalHtml` and
      restores a permanently-spinning button.

   Using `withBusy()` fixes all three: the button is resolved from the
   element you pass, restore always runs, and re-entry is refused.
   ========================================================================== */

(function (global) {
  'use strict';

  const BUSY_FLAG = '_popularrBusy';
  const DEFAULT_SPINNER =
    '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';

  /**
   * Resolve an argument to an actual button element.
   * Accepts: an element, an element inside a button, or an id string.
   *
   * @param {HTMLElement|string} target
   * @returns {HTMLElement|null}
   */
  function resolveButton(target) {
    if (!target) return null;
    if (typeof target === 'string') return document.getElementById(target);
    if (target.tagName === 'BUTTON' || target.tagName === 'A') return target;
    if (typeof target.closest === 'function') {
      return target.closest('button, a.btn') || target;
    }
    return target;
  }

  /**
   * Put a button into its busy state. Returns a restore function.
   *
   * Prefer `withBusy()` — use this directly only when the busy window is
   * not a single awaitable call (e.g. a polling loop that ends elsewhere).
   *
   * @param {HTMLElement|string} target
   * @param {string} [label] optional text shown beside the spinner
   * @returns {Function} call to restore the button
   */
  function setBusy(target, label) {
    const btn = resolveButton(target);
    if (!btn) return function noop() {};

    // Re-entry guard. Without this, a second click captures the spinner
    // markup as "original" and the button never returns to normal.
    if (btn[BUSY_FLAG]) return btn[BUSY_FLAG].restore;

    const originalHtml = btn.innerHTML;
    const originalDisabled = btn.disabled;
    const originalAriaBusy = btn.getAttribute('aria-busy');

    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.innerHTML = label ? `${DEFAULT_SPINNER} ${label}` : DEFAULT_SPINNER;

    let restored = false;
    const restore = function () {
      if (restored) return;
      restored = true;
      btn.innerHTML = originalHtml;
      btn.disabled = originalDisabled;
      if (originalAriaBusy === null) btn.removeAttribute('aria-busy');
      else btn.setAttribute('aria-busy', originalAriaBusy);
      delete btn[BUSY_FLAG];
    };

    btn[BUSY_FLAG] = { restore, originalHtml };
    return restore;
  }

  /**
   * Run an async function with the button in its busy state.
   *
   * The button is ALWAYS restored — on success, on thrown error, and on
   * rejected promise. The error is re-thrown so callers still handle it.
   *
   *     await withBusy(btn, 'Saving…', async () => {
   *       await api.postJson('/api/artist/update-ids', payload);
   *     });
   *
   * @param {HTMLElement|string} target
   * @param {string|Function} labelOrFn label, or the function if no label
   * @param {Function} [maybeFn]
   * @returns {Promise<*>} whatever the wrapped function resolves to
   */
  async function withBusy(target, labelOrFn, maybeFn) {
    const label = typeof labelOrFn === 'string' ? labelOrFn : undefined;
    const fn = typeof labelOrFn === 'function' ? labelOrFn : maybeFn;
    if (typeof fn !== 'function') {
      throw new TypeError('withBusy: no function supplied');
    }

    const btn = resolveButton(target);
    // Already busy: refuse the duplicate action outright rather than
    // running it twice with a corrupted button state.
    if (btn && btn[BUSY_FLAG]) return undefined;

    const restore = setBusy(btn, label);
    try {
      return await fn();
    } finally {
      restore();
    }
  }

  /**
   * Mark a button as permanently done — a green tick that does NOT revert.
   *
   * Used by the "queue this track" buttons, which currently hand-roll
   * `classList.remove('btn-outline-success')` + `add('btn-success')` +
   * a tick in five places (queueMissingTrack, markMBTrackQueued ×2,
   * addUpcomingReleaseToQueueDashboard, confirmUpcomingCandidate).
   *
   * @param {HTMLElement|string} target
   * @param {Object} [opts]
   * @param {string} [opts.label='']      text beside the tick
   * @param {string} [opts.title]         tooltip
   * @param {string} [opts.icon='bi-check2']
   * @param {string} [opts.fromClass]     class to remove (e.g. 'btn-outline-success')
   * @param {string} [opts.toClass='btn-success']
   */
  function setDone(target, opts = {}) {
    const btn = resolveButton(target);
    if (!btn) return;

    // Clear any in-flight busy state first, otherwise its restore() would
    // later overwrite the completed state we are about to set.
    if (btn[BUSY_FLAG]) {
      btn[BUSY_FLAG].restore();
    }

    const icon = opts.icon || 'bi-check2';
    const label = opts.label ? ` ${opts.label}` : '';
    btn.innerHTML = `<i class="bi ${icon}"></i>${label}`;
    btn.disabled = true;
    btn.removeAttribute('aria-busy');
    if (opts.title) btn.title = opts.title;
    if (opts.fromClass) btn.classList.remove(opts.fromClass);
    btn.classList.add(opts.toClass || 'btn-success');
  }

  /**
   * Flash a temporary success state, then revert.
   *
   * Replaces the hand-rolled `setTimeout(... 2000)` revert in
   * `downloadSlskdFile` and `restartQueueProcessor`.
   *
   * @param {HTMLElement|string} target
   * @param {Object} [opts]
   * @param {number} [opts.duration=2000]
   */
  function flashDone(target, opts = {}) {
    const btn = resolveButton(target);
    if (!btn) return;

    const originalHtml = btn[BUSY_FLAG] ? btn[BUSY_FLAG].originalHtml : btn.innerHTML;
    if (btn[BUSY_FLAG]) btn[BUSY_FLAG].restore();

    const icon = opts.icon || 'bi-check-circle';
    const from = opts.fromClass || 'btn-outline-success';
    const to = opts.toClass || 'btn-success';

    btn.innerHTML = `<i class="bi ${icon}"></i>${opts.label ? ' ' + opts.label : ''}`;
    btn.classList.remove(from);
    btn.classList.add(to);
    btn.disabled = true;

    setTimeout(function () {
      // The node may have been removed by a re-render while we waited.
      if (!btn.isConnected) return;
      btn.innerHTML = originalHtml;
      btn.classList.remove(to);
      btn.classList.add(from);
      btn.disabled = false;
    }, opts.duration || 2000);
  }

  global.buttonState = { setBusy, withBusy, setDone, flashDone, resolveButton };

  // Convenience globals for inline onclick="" handlers during migration.
  global.withBusy = withBusy;
  global.setButtonBusy = setBusy;
})(window);
