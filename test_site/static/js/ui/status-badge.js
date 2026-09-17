/* ==========================================================================
   static/js/ui/status-badge.js
   One status -> badge/pill mapping for the whole app.

   Load AFTER utils/dom.js:

       <script src="{{ url_for('static', filename='js/ui/status-badge.js') }}"></script>

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   Status markup is hand-built in ~55 places across 6 files, via two
   competing named helpers plus a lot of inline template strings:

       downloads.js   getMbStatusBadge(status)     -> `badge bg-*`
       playlist.js    _lbQueueStatusBadge(track)   -> different classes
       downloads_page.js                           -> `badge status-pill status-*`
       musicbrainz-folder-groups.js                -> `badge status-pill status-*`

   The two CSS systems are NOT interchangeable:

       .badge bg-success        solid fill, from Bootstrap
       .status-pill.status-complete   outlined, transparent, from downloads.css

   Both are in use for the same statuses on different pages, which is why
   queue state looks solid in one view and outlined in another.

   Worse, the status VOCABULARY is inconsistent. downloads.js treats
   'downloading', 'in_progress' and 'initiating_download' as one state, and
   'queued'/'pending' as another — but downloads_page.js maps 'pending' to
   its own pill and never handles 'initiating_download', so that status
   falls through to a raw grey badge showing the literal database string.

   This module owns the vocabulary once, and renders it in either style.
   ========================================================================== */

(function (global) {
  'use strict';

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  /**
   * Canonical states. Every raw status string from the API maps onto one of
   * these keys; `aliases` lists the raw values seen in the codebase.
   *
   *   label    human text
   *   icon     bootstrap-icons class
   *   bg       Bootstrap contextual class, for the solid `badge` style
   *   pill     modifier class, for the outlined `status-pill` style
   */
  const STATES = {
    complete: {
      label: 'Completed',
      icon: 'bi-check-circle',
      bg: 'bg-success',
      pill: 'status-complete',
      aliases: ['completed', 'complete', 'done', 'finished', 'success', 'organized'],
    },
    downloading: {
      label: 'Downloading',
      icon: 'bi-download',
      bg: 'bg-info',
      pill: 'status-downloading',
      aliases: ['downloading', 'in_progress', 'inprogress', 'initiating_download', 'active'],
    },
    queued: {
      label: 'Queued',
      icon: 'bi-clock',
      bg: 'bg-secondary',
      pill: 'status-queued',
      aliases: ['queued', 'waiting', 'enqueued'],
    },
    pending: {
      label: 'Pending',
      icon: 'bi-hourglass-split',
      bg: 'bg-secondary',
      pill: 'status-pending',
      aliases: ['pending', 'new', 'not_started'],
    },
    searching: {
      label: 'Searching',
      icon: 'bi-search',
      bg: 'bg-warning',
      pill: 'status-queued',
      aliases: ['searching', 'matching', 'scanning'],
    },
    awaiting: {
      label: 'Select File',
      icon: 'bi-hand-index',
      bg: 'bg-primary',
      pill: 'status-queued',
      aliases: ['awaiting_selection', 'awaiting', 'needs_selection', 'manual'],
    },
    failed: {
      label: 'Failed',
      icon: 'bi-x-circle',
      bg: 'bg-danger',
      pill: 'status-failed',
      aliases: ['failed', 'error', 'errored', 'cancelled', 'canceled', 'timeout', 'timedout'],
    },
    skipped: {
      label: 'Skipped',
      icon: 'bi-skip-forward',
      bg: 'bg-secondary',
      pill: 'status-pending',
      aliases: ['skipped', 'ignored', 'duplicate'],
    },
    paused: {
      label: 'Paused',
      icon: 'bi-pause-circle',
      bg: 'bg-secondary',
      pill: 'status-pending',
      aliases: ['paused', 'stopped', 'held'],
    },
  };

  /** Raw alias -> canonical key. Built once. */
  const ALIAS_MAP = (function () {
    const map = Object.create(null);
    Object.keys(STATES).forEach((key) => {
      map[key] = key;
      STATES[key].aliases.forEach((alias) => { map[alias] = key; });
    });
    return map;
  })();

  /**
   * Resolve any raw status string to a canonical state key.
   * Unknown values return null so callers can decide what to do.
   *
   * @param {string} status
   * @returns {string|null}
   */
  function resolve(status) {
    if (!status) return null;
    const key = String(status).toLowerCase().trim().replace(/[\s-]+/g, '_');
    return ALIAS_MAP[key] || null;
  }

  /**
   * Render a status badge.
   *
   * @param {string} status  raw status from the API
   * @param {Object} [opts]
   * @param {string} [opts.style='badge']  'badge' (solid) | 'pill' (outlined)
   * @param {boolean} [opts.icon=true]
   * @param {string} [opts.label]          override the text
   * @param {string} [opts.extraClass]
   * @returns {string} HTML
   */
  function render(status, opts = {}) {
    const key = resolve(status);
    const state = key ? STATES[key] : null;
    const usePill = opts.style === 'pill';
    const showIcon = opts.icon !== false;

    // Unknown status: show the raw value rather than swallowing it, so a new
    // server-side status is visible instead of silently rendering blank.
    if (!state) {
      const cls = usePill ? 'badge status-pill status-pending' : 'badge bg-secondary';
      return `<span class="${cls} ${esc(opts.extraClass || '')}">${esc(status || 'Unknown')}</span>`;
    }

    const cls = usePill
      ? `badge status-pill ${state.pill}`
      : `badge ${state.bg}`;
    const iconHtml = showIcon ? `<i class="bi ${state.icon}"></i> ` : '';
    const label = opts.label || state.label;

    return `<span class="${cls} ${esc(opts.extraClass || '')}">${iconHtml}${esc(label)}</span>`;
  }

  /** Outlined pill variant — shorthand for render(status, {style:'pill'}). */
  function pill(status, opts = {}) {
    return render(status, Object.assign({}, opts, { style: 'pill' }));
  }

  /**
   * Just the contextual class, for callers styling their own element.
   * @param {string} status
   * @param {string} [style='badge']
   * @returns {string}
   */
  function className(status, style) {
    const key = resolve(status);
    const state = key ? STATES[key] : null;
    if (!state) return style === 'pill' ? 'status-pending' : 'bg-secondary';
    return style === 'pill' ? state.pill : state.bg;
  }

  /**
   * Human label for a status, with no markup.
   * @param {string} status
   * @returns {string}
   */
  function label(status) {
    const key = resolve(status);
    return key ? STATES[key].label : String(status || 'Unknown');
  }

  /** True when the status means "nothing more will happen". */
  function isTerminal(status) {
    const key = resolve(status);
    return key === 'complete' || key === 'failed' || key === 'skipped';
  }

  /** True when the status means work is in progress. */
  function isActive(status) {
    const key = resolve(status);
    return key === 'downloading' || key === 'searching';
  }

  global.statusBadge = {
    render,
    pill,
    className,
    label,
    resolve,
    isTerminal,
    isActive,
    STATES,
  };

  // Legacy alias. downloads.js's getMbStatusBadge used the solid style.
  global.getMbStatusBadge = function (status) {
    return render(status, { style: 'badge' });
  };
})(window);
