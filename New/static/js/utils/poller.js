/* ==========================================================================
   static/js/utils/poller.js
   Managed polling loops.

   Load AFTER utils/dom.js:

       <script src="{{ url_for('static', filename='js/utils/poller.js') }}"></script>

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   There are 14 `setInterval` polling loops across 10 files, plus several
   recursive `setTimeout` pollers. They repeat the same four mistakes:

   1. HANDLE STORED IN A SHARED GLOBAL. `slskdPollInterval` is declared with
      `let` at the top level of downloads.js and deliberately reused — not
      redeclared — by downloads_page.js, because redeclaring it throws
      "Identifier has already been declared" and kills the whole file. Both
      files carry comments documenting this. Two files now share one mutable
      handle by convention only: whichever polls last owns it, and either can
      clear the other's loop.

   2. NO STOP ON NAVIGATION OR HIDE. None of the loops stop when the tab is
      backgrounded, and most never stop on page teardown. The dashboard,
      monitor and folder-group loops keep firing requests indefinitely in
      background tabs.

   3. OVERLAPPING REQUESTS. `setInterval` does not wait for the previous
      request to finish. When the server is slow, requests pile up and
      responses arrive out of order — the `slskdMonInFlight` flag in
      downloads_page.js is a hand-rolled guard against exactly this, added
      to only one of the loops.

   4. STATE THAT SURVIVES THE LOOP. `_slskdCompleteGraceCount` is
      module-level in downloads.js, and its own comment records the bug:
      an exhausted search left the counter at its cap, so the NEXT search
      declared "no results" on its very first terminal poll. Per-run state
      must be created per run.

   `poller.create()` gives each loop its own handle, waits for the previous
   tick, pauses when hidden, and stops on teardown.
   ========================================================================== */

(function (global) {
  'use strict';

  /** Every live poller, so stopAll() can reach them on teardown. */
  const active = new Set();

  /**
   * Create a managed polling loop.
   *
   * The loop does NOT start until you call `.start()`.
   *
   *     const p = poller.create({
   *       interval: 1000,
   *       maxAttempts: 120,
   *       onTick: async (ctx) => {
   *         const data = await api.getJson(url);
   *         if (data.isComplete) ctx.stop();
   *         return data;
   *       },
   *       onTimeout: () => toast.error('Timed out'),
   *     });
   *     p.start();
   *
   * @param {Object} opts
   * @param {Function} opts.onTick       async (ctx) => any. ctx has
   *                                     { attempt, stop(), reset() }
   * @param {number} [opts.interval=1000]   ms between ticks
   * @param {number} [opts.maxAttempts=0]   0 = unlimited
   * @param {Function} [opts.onTimeout]     called when maxAttempts is hit
   * @param {Function} [opts.onError]       called with each tick error
   * @param {boolean} [opts.pauseWhenHidden=true]
   * @param {boolean} [opts.immediate=true] run one tick straight away
   * @param {boolean} [opts.stopOnError=false]
   * @returns {Object} the poller handle
   */
  function create(opts = {}) {
    const interval = opts.interval || 1000;
    const maxAttempts = opts.maxAttempts || 0;
    const pauseWhenHidden = opts.pauseWhenHidden !== false;
    const immediate = opts.immediate !== false;
    const stopOnError = opts.stopOnError === true;

    if (typeof opts.onTick !== 'function') {
      throw new TypeError('poller.create: onTick is required');
    }

    let timerId = null;
    let running = false;
    let inFlight = false;
    let attempt = 0;
    let stopped = false;

    const ctx = {
      get attempt() { return attempt; },
      stop: () => handle.stop(),
      /** Reset the attempt counter — for "keep going, progress was made". */
      reset: () => { attempt = 0; },
    };

    async function tick() {
      // Never let a slow response overlap the next tick. This is the
      // hand-rolled `slskdMonInFlight` guard, applied to every loop.
      if (inFlight || stopped) return;

      if (pauseWhenHidden && document.hidden) {
        schedule();
        return;
      }

      attempt += 1;
      if (maxAttempts > 0 && attempt > maxAttempts) {
        handle.stop();
        if (typeof opts.onTimeout === 'function') opts.onTimeout();
        return;
      }

      inFlight = true;
      try {
        await opts.onTick(ctx);
      } catch (error) {
        if (typeof opts.onError === 'function') opts.onError(error);
        else console.error('[poller] tick failed:', error);
        if (stopOnError) {
          handle.stop();
          return;
        }
      } finally {
        inFlight = false;
      }

      if (running) schedule();
    }

    function schedule() {
      clearTimeout(timerId);
      timerId = setTimeout(tick, interval);
    }

    const handle = {
      /** Begin polling. Safe to call twice. */
      start() {
        if (running) return handle;
        running = true;
        stopped = false;
        attempt = 0;
        active.add(handle);
        if (immediate) tick();
        else schedule();
        return handle;
      },

      /** Stop polling and release the timer. Safe to call twice. */
      stop() {
        running = false;
        stopped = true;
        clearTimeout(timerId);
        timerId = null;
        active.delete(handle);
        return handle;
      },

      /** Stop, clear counters, and start again. */
      restart() {
        handle.stop();
        return handle.start();
      },

      get isRunning() { return running; },
      get attempts() { return attempt; },
    };

    return handle;
  }

  /**
   * Poll until a condition is met, as a promise.
   *
   * Replaces the "poll /api/slskd/search-slot until slotFree, then retry"
   * block, which is copy-pasted in three files with its own bare
   * `setInterval` and no timeout — so a permanently busy slot polls forever.
   *
   *     await poller.until(
   *       () => api.getJson('/api/slskd/search-slot'),
   *       (data) => data.slotFree,
   *       { interval: 2000, maxAttempts: 30 }
   *     );
   *
   * @param {Function} fetchFn   async () => value
   * @param {Function} testFn    (value) => boolean
   * @param {Object} [opts]
   * @returns {Promise<*>} resolves with the value that satisfied testFn
   */
  function until(fetchFn, testFn, opts = {}) {
    const interval = opts.interval || 1000;
    const maxAttempts = opts.maxAttempts || 60;

    return new Promise((resolve, reject) => {
      const p = create({
        interval,
        maxAttempts,
        pauseWhenHidden: false, // a promise must settle even in a background tab
        onTick: async () => {
          const value = await fetchFn();
          if (testFn(value)) {
            p.stop();
            resolve(value);
          }
        },
        onTimeout: () => reject(new Error(opts.timeoutMessage || 'Condition not met in time')),
        onError: (error) => {
          if (opts.ignoreErrors === false) {
            p.stop();
            reject(error);
          }
        },
      });
      p.start();
    });
  }

  /** Stop every live poller. */
  function stopAll() {
    Array.from(active).forEach((p) => p.stop());
  }

  // Teardown. `pagehide` fires on iOS Safari where `beforeunload` does not.
  global.addEventListener('pagehide', stopAll);
  global.addEventListener('beforeunload', stopAll);

  global.poller = { create, until, stopAll, get activeCount() { return active.size; } };
})(window);
