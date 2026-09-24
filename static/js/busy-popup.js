/* ==========================================================================
   static/js/busy-popup.js
   A compact centred "working…" popup for actions with no visible button.

   ⚠️ THIS FILE EXISTS IN BOTH TREES AND THE BODIES MUST NOT DRIFT:
       live    static/js/busy-popup.js
       rebuilt test_site/static/js/ui/busy-popup.js
   The live tree is FLAT (static/js/*.js); the rebuilt tree is FOLDERED
   (static/js/ui/*.js). Only the header path above may differ.

   Load AFTER utils/dom.js, and BEFORE any page script.

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   ``ui/button-state.js`` covers the common case: the user clicked a button,
   so the button itself can show a spinner. That is not enough for the
   actions this module was written for:

   1. A BUTTON SPINNER IS EASY TO MISS. Queueing a release is a release-picker
      probe plus a POST, measured in seconds. On a busy page the button can be
      off-screen or lost among others, so silence still reads as "nothing
      happened".

   2. SOME ACTIONS HAVE NO BUTTON AT ALL. The album page's MBID lookup runs
      from a dropdown item, and several steps continue after the click
      (lookup → metadata review → save). There is no single element that can
      carry a busy state for the whole window.

   3. THE WINDOW IS OFTEN NOT ONE AWAITABLE CALL. The lookup hands off to a
      modal and only completes when the user picks a match, so the popup has
      to be opened and closed in different places rather than wrapped.

   So this is deliberately NOT an overlay: the user must still be able to read
   the page and interact with the modal underneath. It is:

   * pointer-transparent except for the popup itself, so it cannot swallow a
     click meant for the modal;
   * a counter, not a boolean, so overlapping actions (lookup opens, save
     starts) cannot have one close the other's popup;
   * auto-releasing on page unload, so a navigation cannot leave it stuck.

   ── API ───────────────────────────────────────────────────────────────────
       busyPopup.show('Looking up MusicBrainz match…')   -> handle
       busyPopup.hide(handle)                 // release this one claim
       busyPopup.update(handle, 'Saving…')    // reuse the open popup
       busyPopup.showAndRun(label, fn)        // wrap an async action
       busyPopup.releaseAll()                 // panic button / teardown

   ``showAndRun`` is the one to reach for when the window IS a single
   awaitable call; it releases in ``finally`` so a throw cannot strand it.

       await busyPopup.showAndRun('Adding to queue…', async () => {
         await api.postJson('/api/queue/add', body);
       });
   ========================================================================== */

(function (global) {
  'use strict';

  const POPUP_ID = 'popularrBusyPopup';
  const STYLE_ID = 'popularrBusyPopupStyles';
  const SETTLE_MS = 150;

  /**
   * Reference count of outstanding claims. The popup is visible while > 0.
   * A counter (not a flag) because several independent actions can overlap.
   */
  let claims = 0;

  /** The single live popup element, reused across claims. */
  let popupEl = null;

  /** Claims released by the unload/pagehide safety net. */
  let unloadBound = false;

  function isBrowser() {
    return typeof document !== 'undefined' && !!document.body;
  }

  function injectStyles() {
    if (!isBrowser()) return;
    if (document.getElementById(STYLE_ID)) return;

    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #${POPUP_ID} {
        position: fixed;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        z-index: 20000;              /* above modals + the toast stack */
        display: flex;
        align-items: center;
        gap: .6rem;
        padding: .7rem 1.1rem;
        border-radius: .55rem;
        background: rgba(17, 24, 39, .96);
        border: 1px solid rgba(148, 163, 184, .35);
        box-shadow: 0 12px 32px rgba(0, 0, 0, .5);
        color: #e5e7eb;
        font-size: .9rem;
        line-height: 1.2;
        max-width: min(90vw, 30rem);
        opacity: 0;
        transition: opacity ${SETTLE_MS}ms ease;
        /* The wrapper must not eat clicks aimed at the page/modal behind. */
        pointer-events: none;
      }
      #${POPUP_ID}.popularr-busy-visible { opacity: 1; }
      #${POPUP_ID} .popularr-busy-spinner {
        width: 1.05rem;
        height: 1.05rem;
        flex: 0 0 auto;
        border: 2px solid rgba(148, 163, 184, .35);
        border-top-color: #60a5fa;
        border-radius: 50%;
        animation: popularr-busy-spin .7s linear infinite;
      }
      #${POPUP_ID} .popularr-busy-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      @keyframes popularr-busy-spin { to { transform: rotate(360deg); } }
      @media (prefers-reduced-motion: reduce) {
        #${POPUP_ID} .popularr-busy-spinner { animation-duration: 2s; }
      }
    `;
    document.head.appendChild(style);
  }

  function ensurePopup() {
    if (!isBrowser()) return null;
    injectStyles();
    if (popupEl && popupEl.isConnected) return popupEl;

    const existing = document.getElementById(POPUP_ID);
    if (existing) {
      popupEl = existing;
      return popupEl;
    }

    const el = document.createElement('div');
    el.id = POPUP_ID;
    // Exposed to assistive tech as a status region, so screen readers announce
    // the label without the user having to hunt for it.
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML =
      '<span class="popularr-busy-spinner" aria-hidden="true"></span>' +
      '<span class="popularr-busy-label"></span>';

    document.body.appendChild(el);
    popupEl = el;
    return popupEl;
  }

  function setLabel(text) {
    const el = ensurePopup();
    if (!el) return;
    const label = el.querySelector('.popularr-busy-label');
    if (label) label.textContent = text || '';
  }

  function paint() {
    const el = ensurePopup();
    if (!el) return;
    if (claims > 0) {
      el.classList.add('popularr-busy-visible');
    } else {
      el.classList.remove('popularr-busy-visible');
    }
  }

  function bindUnloadGuard() {
    if (unloadBound || !isBrowser()) return;
    unloadBound = true;
    // A navigation (or a bfcache freeze) must never leave the popup showing on
    // the NEXT page. This is the only path that can strand it, because every
    // other release goes through hide()/showAndRun's finally.
    const reset = function () { claims = 0; paint(); };
    global.addEventListener('pagehide', reset);
    global.addEventListener('beforeunload', reset);
  }

  /**
   * Show the popup and return a handle to release with hide().
   *
   * @param {string} [label] text shown beside the spinner
   * @returns {object|null} handle, or null outside a browser
   */
  function show(label) {
    const el = ensurePopup();
    if (!el) return null;

    bindUnloadGuard();
    claims += 1;
    setLabel(label);
    paint();

    let released = false;
    return {
      label: label,
      release: function () {
        if (released) return;
        released = true;
        claims = Math.max(0, claims - 1);
        if (claims === 0) setLabel('');
        paint();
      },
    };
  }

  /**
   * Release a handle. Safe to call twice, and safe to call with a falsy value
   * (so `hide(maybeHandle)` at the end of a branch needs no guard).
   *
   * @param {object|null} handle
   */
  function hide(handle) {
    if (handle && typeof handle.release === 'function') handle.release();
  }

  /**
   * Update the label of an open handle without changing the claim count.
   * Used when an action moves to its next phase (lookup → save).
   */
  function update(handle, label) {
    if (!handle) return;
    handle.label = label;
    // Only relabel while something is actually outstanding; if every claim has
    // been released there is nothing on screen to relabel.
    if (claims > 0) setLabel(label);
  }

  /**
   * Run an async function with the popup showing.
   *
   * Always released — on success, on throw, on rejection — then the error is
   * re-thrown so the caller still handles it.
   *
   * @param {string} label
   * @param {Function} fn
   * @returns {Promise<*>}
   */
  async function showAndRun(label, fn) {
    if (typeof fn !== 'function') {
      throw new TypeError('busyPopup.showAndRun: no function supplied');
    }
    const handle = show(label);
    try {
      return await fn();
    } finally {
      hide(handle);
    }
  }

  /** Release every outstanding claim. For teardown and diagnostics. */
  function releaseAll() {
    claims = 0;
    setLabel('');
    paint();
  }

  const busyPopup = {
    show,
    hide,
    update,
    showAndRun,
    releaseAll,
    /** Exposed for tests/diagnostics. */
    get active() { return claims; },
  };

  global.busyPopup = busyPopup;

  // Legacy-ish alias matching the other ui modules' verb naming, so page code
  // that only needs the simple case reads consistently.
  global.showBusyPopup = show;
  global.hideBusyPopup = hide;
})(window);
