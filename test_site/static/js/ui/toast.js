/* ==========================================================================
   static/js/ui/toast.js
   The single toast implementation for the whole app.

   Load AFTER utils/dom.js and BEFORE any page script:

       <script src="{{ url_for('static', filename='js/ui/toast.js') }}"></script>

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   There are currently FOUR toast implementations and they fight:

   1. main.js   `window.showToast`   bottom-left stack, builds its own
                                     #popularrToastWrap. Works everywhere.
   2. config.js `showToast`          Bootstrap Toast bound to #configToast.
                                     config.js loads AFTER main.js, so on the
                                     config page it SHADOWS the global one.
                                     It also silently no-ops when #configToast
                                     is absent (`if (!toastEl) return;`).
   3. main.js   `window.showTopToast` centred pill ~25% down. Different
                                     position, different signature
                                     (message, type) vs (title, message, type).
   4. main.js   `window.showQueueToast` centred pill that COUNTS repeats
                                     ("Queued 3 items") instead of stacking.

   On top of that, downloads.js defines `showToastMsg(message, isError)` — a
   fifth signature — purely to paper over not knowing which of the above
   exists on the current page. Its own comment notes it was called three
   times by confirmOrganizeGroup while being defined nowhere, so a successful
   organize threw instead of reporting its result.

   main.js ALSO overrides `window.alert` to route through showTopToast. That
   override is kept (see `installAlertBridge` below) because ~120 call sites
   still use alert(), but it now routes through this module.

   The four behaviours are preserved as ONE api with explicit placement:

       toast.show({ title, message, type, placement })
       toast.success(message, title)
       toast.error(message, title)
       toast.warning(message, title)
       toast.queued(albumTitle)          // the counting pill

   Every legacy name is aliased at the bottom, so nothing breaks mid-migration.
   ========================================================================== */

(function (global) {
  'use strict';

  const STACK_ID = 'popularrToastWrap';
  const PILL_ID = 'queueToastPill';

  const AUTO_HIDE_MS = 4000;
  const PILL_HIDE_MS = 2600;
  const FADE_MS = 250;

  /** type -> bootstrap background class + bootstrap-icons glyph */
  const TYPES = {
    success: { cls: 'bg-success', icon: 'bi-check-circle-fill' },
    error: { cls: 'bg-danger', icon: 'bi-x-circle-fill' },
    danger: { cls: 'bg-danger', icon: 'bi-x-circle-fill' },
    warning: { cls: 'bg-warning text-dark', icon: 'bi-exclamation-triangle-fill' },
    info: { cls: 'bg-dark', icon: 'bi-info-circle-fill' },
  };

  /** Solid colours for the centred pill (no Bootstrap class equivalent). */
  const PILL_COLOURS = {
    success: '#198754',
    warning: '#b45309',
    error: '#b91c1c',
    danger: '#b91c1c',
    info: '#1f2937',
  };

  function typeInfo(type) {
    return TYPES[type] || TYPES.info;
  }

  // ── Bottom-left stack ─────────────────────────────────────────────────

  function stackContainer() {
    let wrap = document.getElementById(STACK_ID);
    if (wrap) return wrap;
    wrap = document.createElement('div');
    wrap.id = STACK_ID;
    wrap.setAttribute('role', 'status');
    wrap.setAttribute('aria-live', 'polite');
    wrap.style.cssText =
      'position:fixed;bottom:1rem;left:1rem;z-index:2200;' +
      'display:flex;flex-direction:column;gap:0.5rem;max-width:min(90vw,420px);';
    document.body.appendChild(wrap);
    return wrap;
  }

  function showStacked(title, message, type, duration) {
    const info = typeInfo(type);
    const el = document.createElement('div');
    el.className = 'shadow-sm ' + info.cls;
    el.style.cssText =
      'padding:0.65rem 1rem;border-radius:0.5rem;font-size:0.85rem;' +
      'display:flex;align-items:center;gap:0.5rem;opacity:0;transition:opacity 0.2s ease;';
    el.innerHTML = `<i class="bi ${info.icon}"></i><span></span>`;

    // textContent, never innerHTML: titles and messages come from API
    // responses and user input and must never be parsed as markup.
    el.querySelector('span').textContent =
      title && message ? `${title}: ${message}` : (message || title || '');

    stackContainer().appendChild(el);
    requestAnimationFrame(() => { el.style.opacity = '1'; });

    setTimeout(function () {
      el.style.opacity = '0';
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, FADE_MS);
    }, duration || AUTO_HIDE_MS);

    return el;
  }

  // ── Centred pill ──────────────────────────────────────────────────────

  function showCentred(message, type, duration) {
    const colour = PILL_COLOURS[type] || PILL_COLOURS.info;
    const info = typeInfo(type);
    const el = document.createElement('div');
    el.setAttribute('role', 'status');
    el.style.cssText =
      'position:fixed;top:25%;left:50%;transform:translateX(-50%);z-index:2400;' +
      `background:${colour};color:#fff;border-radius:999px;padding:0.55rem 1.1rem;` +
      'font-size:0.85rem;font-weight:600;box-shadow:0 4px 14px rgba(0,0,0,0.35);' +
      'display:flex;align-items:center;gap:0.5rem;max-width:min(90vw,480px);' +
      'white-space:nowrap;overflow:hidden;transition:opacity 0.2s ease;';
    el.innerHTML = `<i class="bi ${info.icon} flex-shrink-0"></i><span class="text-truncate"></span>`;
    el.querySelector('span').textContent = message;

    document.body.appendChild(el);
    requestAnimationFrame(() => { el.style.opacity = '1'; });

    setTimeout(function () {
      el.style.opacity = '0';
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, FADE_MS);
    }, duration || PILL_HIDE_MS);

    return el;
  }

  // ── Counting queue pill ───────────────────────────────────────────────
  // Rapid queueing updates ONE pill in place rather than stacking toasts:
  //   'Queued "Album"'  ->  'Queued 3 items'

  let queueCount = 0;
  let queueHideTimer = null;

  function showQueued(title) {
    let el = document.getElementById(PILL_ID);
    if (!el) {
      el = document.createElement('div');
      el.id = PILL_ID;
      el.className = 'd-none';
      el.setAttribute('role', 'status');
      el.style.cssText =
        'position:fixed;top:25%;left:50%;transform:translateX(-50%);z-index:2400;' +
        `background:${PILL_COLOURS.success};color:#fff;border-radius:999px;padding:0.55rem 1.1rem;` +
        'font-size:0.85rem;font-weight:600;box-shadow:0 4px 14px rgba(0,0,0,0.35);' +
        'display:flex;align-items:center;gap:0.5rem;max-width:min(90vw,480px);' +
        'white-space:nowrap;overflow:hidden;transition:opacity 0.2s ease;';
      el.innerHTML =
        '<i class="bi bi-check-circle-fill flex-shrink-0"></i><span class="text-truncate"></span>';
      document.body.appendChild(el);
    }

    queueCount += 1;
    el.querySelector('span').textContent =
      queueCount === 1 ? `Queued "${title}"` : `Queued ${queueCount} items`;

    el.classList.remove('d-none');
    el.style.opacity = '1';

    clearTimeout(queueHideTimer);
    queueHideTimer = setTimeout(function () {
      el.style.opacity = '0';
      setTimeout(function () { el.classList.add('d-none'); }, FADE_MS);
      queueCount = 0;
    }, PILL_HIDE_MS);

    return el;
  }

  // ── Public API ────────────────────────────────────────────────────────

  /**
   * @param {Object} opts
   * @param {string} [opts.title]
   * @param {string} [opts.message]
   * @param {string} [opts.type='info']  success | error | warning | info
   * @param {string} [opts.placement]    'stack' (bottom-left, default) |
   *                                     'centre' (pill, ~25% down)
   * @param {number} [opts.duration]
   */
  function show(opts = {}) {
    const type = opts.type || 'info';
    if (opts.placement === 'centre' || opts.placement === 'center') {
      const text = opts.title && opts.message
        ? `${opts.title}: ${opts.message}`
        : (opts.message || opts.title || '');
      return showCentred(text, type, opts.duration);
    }
    return showStacked(opts.title, opts.message, type, opts.duration);
  }

  function success(message, title) { return show({ title, message, type: 'success' }); }
  function error(message, title) { return show({ title, message, type: 'error' }); }
  function warning(message, title) { return show({ title, message, type: 'warning' }); }
  function info(message, title) { return show({ title, message, type: 'info' }); }

  // ── alert() bridge ────────────────────────────────────────────────────
  // Kept from main.js: ~120 call sites still use alert(). Guess the tone
  // from the message text (most already carry ✅ / ❌ prefixes) and log it
  // server-side so UI feedback is greppable in client.log.
  //
  // confirm() is deliberately NOT overridden — destructive flows keep their
  // blocking native confirmation.

  function toneFromMessage(message) {
    const m = String(message || '');
    const good = /✅|✓|success|completed|updated|added|queued|deleted|saved|matched|started|lookup complete/i.test(m);
    const bad = /❌|✗|error|failed|invalid|missing|network|could not|unable|please enter|please select/i.test(m);
    if (good && !bad) return 'success';
    if (bad) return 'error';
    return 'warning';
  }

  function logClientMessage(message) {
    try {
      fetch('/api/logs/client', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: String(message || '').slice(0, 500) }),
      }).catch(function () {});
    } catch (_e) {
      /* never block the UI on logging */
    }
  }

  function installAlertBridge() {
    global.alert = function (message) {
      logClientMessage(message);
      showCentred(String(message == null ? '' : message), toneFromMessage(message));
    };
  }

  const toast = {
    show,
    success,
    error,
    warning,
    info,
    queued: showQueued,
    installAlertBridge,
    toneFromMessage,
  };

  global.toast = toast;

  // ── Legacy aliases ────────────────────────────────────────────────────
  // Every existing call site keeps working. Delete an alias only once its
  // callers have been migrated.
  //
  // NOTE: config.js currently defines its OWN `showToast` and loads after
  // main.js, so it shadows the global. Delete that definition (and its
  // #configToast markup) when wiring this module in — otherwise the config
  // page keeps the old Bootstrap-Toast behaviour and this has no effect
  // there.
  global.showToast = function (title, message, type) {
    return show({ title, message, type });
  };
  global.showTopToast = function (message, type) {
    return showCentred(message, type || 'success');
  };
  global.showQueueToast = showQueued;
  // downloads.js: showToastMsg(message, isError)
  global.showToastMsg = function (message, isError) {
    return showCentred(message, isError ? 'error' : 'success');
  };
})(window);
