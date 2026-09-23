/* ==========================================================================
   static/js/services/item-groups.js
   Shared renderer + interaction helpers for "grouped item" lists.

   Load AFTER utils/dom.js (escapeHtml) and utils/api.js:

       <script src="{{ versioned_static('js/utils/dom.js') }}"></script>
       <script src="{{ versioned_static('js/utils/api.js') }}"></script>
       <script src="{{ versioned_static('js/services/item-groups.js') }}"></script>

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   Two pages render the same UI shape — a list of FOLDERS, each with a title,
   sub-title, status badge, an action cluster, and optionally a collapsible
   body of child rows:

     pages/monitor.js        "Matched & Unmatched Folders"
                             (GET /api/downloads/unmatched-folders)
     pages/download-queue.js "Active Queue" / "Completed & Ready to Organize"
                             / "Failed Downloads"  (GET /api/downloads/queue)

   They had drifted into two different implementations of the same pattern,
   and the completed list had even ended up repeating the album's actions on
   every track row beneath it. The behaviour now agrees; this module is what
   keeps them from drifting again.

   What moved here (all of it was duplicated):
     * the per-row ACTION BUTTON markup (btn-sm, py-0 px-2, ms-1, outline
       tone, bi- icon) — hand-written in both files, with slightly different
       spacing each time
     * the delegated click binding loop — both files wrote the same
       `querySelectorAll(selector).forEach(btn => btn.addEventListener(...))`
       over a list of [selector, handler] pairs
     * the collapsible group toggle + expansion memory (existing bodies had
       to survive a re-render, or an open album snapped shut every poll)
     * DOM-id sanitising for group keys, which both files need and which had
       two different implementations (one replaced every unsafe char, the
       other only whitespace)

   ── DESIGN NOTES ──────────────────────────────────────────────────────────
   * Class names and ids are PASSED IN, not hard-coded. The queue's markup
     (queue-group-toggle / -chevron / -body, queueGroupBody_<kind>_<key>) is
     relied on by attachRowHandlers and by restored expansion state, so
     renaming it here would silently break both. Callers keep their own names.
   * Expansion state is PER CALLER-CONTEXT (see expansionFor), because a
     queue album and a download folder can legitimately share an id.
   * escapeHtml comes from utils/dom.js. Every value rendered here is either
     a filesystem path or a MusicBrainz title — i.e. external input — so
     nothing is interpolated raw.
   ========================================================================== */

(function (global) {
  'use strict';

  /** Expansion sets, one per calling context ("queue", "folders", …). */
  const expansionState = new Map();

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  // ── DOM ids ─────────────────────────────────────────────────────────────

  /**
   * Make a value safe to use inside a DOM id.
   *
   * Group keys look like `alb_artist|album` and folder names are filesystem
   * paths, so they can contain `|`, spaces, quotes, slashes and dots — none of
   * which belong in an id, and some of which break querySelector.
   *
   * @param {string} value
   * @param {string} [fallback='x']
   * @returns {string}
   */
  function sanitizeId(value, fallback) {
    const cleaned = String(value == null ? '' : value).replace(/[^a-zA-Z0-9_-]/g, '_');
    return cleaned || (fallback || 'x');
  }

  // ── Small presentational helpers ────────────────────────────────────────

  /**
   * A pill badge. Delegates to ui/status-badge.js when the caller passes a
   * known status so the colour/label mapping stays in one place.
   *
   * @param {string} text
   * @param {string} [tone='secondary'] Bootstrap tone (secondary/success/…)
   * @param {Object} [opts]
   * @param {string} [opts.title]  tooltip
   * @param {string} [opts.extraClass]
   * @returns {string}
   */
  function badge(text, tone, opts) {
    const options = opts || {};
    const title = options.title ? ` title="${esc(options.title)}"` : '';
    return `<span class="badge bg-${esc(tone || 'secondary')}${options.extraClass ? ' ' + options.extraClass : ''}"${title}>` +
      `${esc(text)}</span>`;
  }

  /**
   * A status pill driven by ui/status-badge.js, with a plain-badge fallback.
   *
   * @param {string} status
   * @param {Object} [opts] forwarded to statusBadge.pill()
   * @returns {string}
   */
  function statusBadge(status, opts) {
    if (global.statusBadge && typeof global.statusBadge.pill === 'function') {
      return global.statusBadge.pill(status, opts || {});
    }
    return `<span class="badge status-pill">${esc(status)}</span>`;
  }

  /**
   * An action button.
   *
   * `data` is written as individual attributes rather than a JSON blob so the
   * values stay greppable and so a value containing a quote cannot escape into
   * the attribute (esc handles quotes).
   *
   * @param {Object} spec
   * @param {string} spec.className   e.g. 'queue-organize' (selector target)
   * @param {string} spec.icon        Bootstrap icon name, e.g. 'bi-trash'
   * @param {string} [spec.title]     tooltip / accessible name
   * @param {string} [spec.label]     visible text (icon-only when omitted)
   * @param {Object} [spec.data]      data-* attributes
   * @param {boolean} [spec.disabled]
   * @returns {string}
   */
  function actionButton(spec) {
    const s = spec || {};
    const attrs = Object.entries(s.data || {})
      .filter(([, value]) => value !== undefined && value !== null)
      .map(([key, value]) => `data-${key}="${esc(value)}"`)
      .join(' ');

    const label = s.label ? ` ${esc(s.label)}` : '';
    const title = s.title ? ` title="${esc(s.title)}"` : '';
    const ariaLabel = s.title && !s.label ? ` aria-label="${esc(s.title)}"` : '';
    const disabled = s.disabled ? ' disabled' : '';

    return `<button type="button" class="btn btn-sm ${esc(s.className)} py-0 px-2 ms-1"` +
      ` ${attrs}${title}${ariaLabel}${disabled}>` +
      `<i class="bi ${esc(s.icon || 'bi-circle')}"></i>${label}</button>`;
  }

  /** The flex wrapper the action buttons sit in. */
  function actionsCluster(buttonsHtml, opts) {
    const options = opts || {};
    const classes = options.className || 'd-flex align-items-center gap-1 flex-shrink-0';
    return `<div class="${esc(classes)}">${buttonsHtml || ''}</div>`;
  }

  // ── Row shell ───────────────────────────────────────────────────────────

  /**
   * The common "folder" row.
   *
   * Reproduces both callers' markup: the queue's row is centred with a chevron
   * toggle and a collapsible body, the monitor's is top-aligned with a
   * wrapping action cluster and no body.
   *
   * @param {Object} spec
   * @param {string} [spec.align='center']         'center' | 'start'
   * @param {string} [spec.titleHtml]              pre-built title cell content
   * @param {string} [spec.subtitleHtml]
   * @param {string} [spec.metaHtml]               small muted line under title
   * @param {string} [spec.actionsHtml]
   * @param {string} [spec.extraClass]             classes on the list-group-item
   * @param {Object} [spec.attrs]                  data-* on the list-group-item
   * @param {string} [spec.titleColumnClass]       override the title cell class
   * @param {string} [spec.actionsClass]           override the action wrapper class
   * @param {Object} [spec.collapsible]            { toggleClass, chevronClass, bodyClass, bodyId, expanded, toggleTitle }
   * @param {string} [spec.bodyHtml]
   * @returns {string}
   */
  function rowShell(spec) {
    const s = spec || {};
    const align = s.align === 'start' ? 'align-items-start' : 'align-items-center';

    const attrs = Object.entries(s.attrs || {})
      .filter(([, value]) => value !== undefined && value !== null)
      .map(([key, value]) => `data-${key}="${esc(value)}"`)
      .join(' ');

    const extraClass = s.extraClass ? ` ${esc(s.extraClass)}` : '';
    const attrsHtml = attrs ? ' ' + attrs : '';

    let toggleHtml = '';
    let bodyHtml = '';

    // Collapsible rows carry `text-truncate` on the title COLUMN (their title
    // is a single <strong> line). Non-collapsible rows must NOT: the monitor's
    // folder rows put a subtitle and a meta line in that same column, and
    // text-truncate would force them onto one clipped line.
    let titleColumnClass = s.titleColumnClass
      || (s.collapsible ? 'text-truncate flex-grow-1' : 'flex-grow-1');

    if (s.collapsible) {
      const c = s.collapsible;
      const expanded = !!c.expanded;

      toggleHtml =
        `<button type="button" class="btn btn-sm btn-link p-0 text-decoration-none ${esc(c.toggleClass)} flex-shrink-0"` +
        ` data-target="${esc(c.bodyId)}" title="${esc(c.toggleTitle || 'Expand')}" style="color:var(--text-secondary);">` +
        `<i class="bi bi-chevron-down ${esc(c.chevronClass)}${expanded ? ' rotated' : ''}"></i></button>`;

      bodyHtml =
        `<div id="${esc(c.bodyId)}" class="${esc(c.bodyClass)} ps-3 border-start ms-2 mt-2"` +
        ` style="display:${expanded ? 'block' : 'none'};">${s.bodyHtml || ''}</div>`;
    }

    const titleHtml = s.titleHtml || '';
    const subtitleHtml = s.subtitleHtml || '';
    const metaHtml = s.metaHtml || '';

    return `
      <div class="list-group-item${extraClass}"${attrsHtml}>
        <div class="d-flex justify-content-between ${align} gap-2">
          ${toggleHtml}
          <div class="${titleColumnClass}" style="min-width:0;">
            ${titleHtml}${subtitleHtml}${metaHtml}
          </div>
          ${actionsCluster(s.actionsHtml, { className: s.actionsClass })}
        </div>
        ${bodyHtml}
      </div>`;
  }

  // ── Delegated action binding ────────────────────────────────────────────

  /**
   * Bind a set of click handlers to everything matching a selector inside
   * `root`.
   *
   * Replaces the hand-rolled loop both callers had:
   *
   *     listEl.querySelectorAll(selector).forEach(btn => {
   *       btn.addEventListener('click', function () { … });
   *     });
   *
   * Handlers receive (element, event). A handler returning false does NOT
   * stop propagation — call event.preventDefault() yourself if needed, so the
   * behaviour is explicit rather than surprising.
   *
   * ⚠️ `handler.call(this, this, event)` — NOT `handler(this, event)`.
   *
   * The element is passed BOTH ways on purpose:
   *   * as the first ARGUMENT, which is the documented `(element, event)`
   *     contract; and
   *   * as `this`, because every existing caller was written as
   *     `function () { this.dataset.x }`.
   *
   * A bare `handler(this, event)` binds nothing, so `this` is `undefined` in a
   * strict handler and `globalThis` in a sloppy one — and `this.dataset` throws
   * either way. That single call shape silently killed EVERY action bound
   * through here: all ten queue row/group handlers in pages/download-queue.js
   * and the folder actions in pages/monitor.js. Inside an addEventListener
   * callback `this` already IS the element, which is what makes `.call` correct
   * rather than a trick.
   *
   * ⚠️ Do not "simplify" this back to a bare call, and do not pick one style:
   * the parameter is the contract, `this` is what the callers use. A test
   * (`tests/test_item_groups_bindactions_this.py`) drives the real extracted
   * code and fails on either regression.
   *
   * @param {Element} root
   * @param {Object<string, Function>} map selector -> handler(element, event)
   */
  function bindActions(root, map) {
    if (!root || !map) return;

    Object.entries(map).forEach(([selector, handler]) => {
      if (typeof handler !== 'function') return;
      root.querySelectorAll(selector).forEach((el) => {
        el.addEventListener('click', function (event) {
          handler.call(this, this, event);
        });
      });
    });
  }

  // ── Collapsible groups ──────────────────────────────────────────────────

  /**
   * Expansion state for one calling context.
   *
   * Kept per context because a queue album group and a downloads folder can
   * share an id, and one collapsing the other would be baffling.
   *
   * @param {string} contextKey
   * @returns {Set<string>}
   */
  function expansionFor(contextKey) {
    const key = String(contextKey || 'default');
    if (!expansionState.has(key)) expansionState.set(key, new Set());
    return expansionState.get(key);
  }

  /**
   * Wire chevron toggles to their bodies.
   *
   * @param {Element} root
   * @param {Object} opts
   * @param {string} opts.toggleClass
   * @param {string} opts.bodyClass
   * @param {string} opts.chevronClass
   * @param {string} [opts.contextKey='default']
   */
  function attachToggles(root, opts) {
    if (!root || !opts) return;
    const open = expansionFor(opts.contextKey);

    root.querySelectorAll('.' + opts.toggleClass).forEach((btn) => {
      btn.addEventListener('click', function () {
        const body = document.getElementById(this.getAttribute('data-target'));
        if (!body) return;

        // `''` counts as visible: only an explicit 'none' is collapsed. The
        // first toggle then hides it, which is what a user expects from a
        // chevron that is currently pointing down.
        const show = body.style.display === 'none' || body.style.display === '';
        body.style.display = show ? 'block' : 'none';

        const chevron = this.querySelector('.' + opts.chevronClass);
        if (chevron) chevron.classList.toggle('rotated', show);

        if (show) open.add(body.id);
        else open.delete(body.id);
      });
    });
  }

  /**
   * Re-apply remembered expansion after a re-render, and prune ids that no
   * longer exist so the set cannot grow without bound across polls.
   *
   * @param {Element} root
   * @param {Object} opts
   * @param {string} opts.bodyClass
   * @param {string} opts.chevronClass
   * @param {string} [opts.contextKey='default']
   */
  function restoreExpansion(root, opts) {
    if (!root || !opts) return;
    const open = expansionFor(opts.contextKey);
    const seen = new Set();

    root.querySelectorAll('.' + opts.bodyClass).forEach((body) => {
      seen.add(body.id);
      if (!open.has(body.id)) return;

      body.style.display = 'block';
      const row = body.closest('.list-group-item');
      const chevron = row && row.querySelector('.' + opts.chevronClass);
      if (chevron) chevron.classList.add('rotated');
    });

    Array.from(open).forEach((id) => {
      if (!seen.has(id)) open.delete(id);
    });
  }

  global.itemGroups = {
    esc,
    sanitizeId,
    badge,
    statusBadge,
    actionButton,
    actionsCluster,
    rowShell,
    bindActions,
    expansionFor,
    attachToggles,
    restoreExpansion,
  };
})(window);
