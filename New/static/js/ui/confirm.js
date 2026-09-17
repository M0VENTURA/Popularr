/* ==========================================================================
   static/js/ui/confirm.js
   Themed confirmation dialogs.

   Load AFTER ui/modal.js:

       <script src="{{ url_for('static', filename='js/ui/modal.js') }}"></script>
       <script src="{{ url_for('static', filename='js/ui/confirm.js') }}"></script>

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   ~45 native `confirm()` calls across 11 files. Native dialogs are a
   problem here for three specific reasons, not just cosmetics:

   1. THEY ARE UNSTYLED AND UNTHEMED. Every other surface in the app is
      dark; the confirm dialog is the browser's. main.js already routes
      `alert()` through a toast for this reason, but deliberately left
      `confirm()` alone because it needs a return value — which a toast
      cannot provide. This module supplies the missing async equivalent.

   2. MESSAGES ARE BUILT BY STRING CONCATENATION. Confirm text is assembled
      inline with embedded `\n\n` separators and interpolated titles, e.g.
          `Download "${title}" by ${artist} via ${method}?${persistentSearch
            ? '\n\nPersistent search enabled - will auto-retry if failed.' : ''}`
      A native dialog renders that as flat monospace text with no hierarchy,
      and long file lists are untruncated. This module takes a structured
      `detail` field and an `items` array instead, so the secondary line and
      any affected-file list are styled and capped at 10 entries.

   3. THEY BLOCK THE EVENT LOOP. A native confirm inside an async handler
      freezes timers and in-flight polling until dismissed.

   ── IMPORTANT ─────────────────────────────────────────────────────────────
   `confirm()` is synchronous; `confirmAction()` returns a Promise. This is
   NOT a drop-in replacement — every call site must become `await`. A
   forgotten `await` yields a truthy Promise, so the action proceeds
   unconditionally. Migrate deliberately:

       if (!confirm('Delete?')) return;              // before
       if (!await ui.confirm('Delete?')) return;     // after (async fn)

   `window.confirm` is deliberately NOT overridden, for exactly that reason.
   ========================================================================== */

(function (global) {
  'use strict';

  const MODAL_ID = 'popularrConfirmModal';

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  /** Preset tones. `danger` is the default for destructive wording. */
  const TONES = {
    danger: { btn: 'btn-danger', icon: 'bi-exclamation-triangle-fill', iconClass: 'text-danger' },
    warning: { btn: 'btn-warning', icon: 'bi-exclamation-circle-fill', iconClass: 'text-warning' },
    primary: { btn: 'btn-primary', icon: 'bi-question-circle-fill', iconClass: 'text-primary' },
    success: { btn: 'btn-success', icon: 'bi-check-circle-fill', iconClass: 'text-success' },
  };

  /**
   * Ask the user to confirm an action.
   *
   * @param {string|Object} messageOrOpts
   * @param {Object} [maybeOpts]
   * @param {string} [opts.title='Are you sure?']
   * @param {string} [opts.message]
   * @param {string} [opts.detail]        secondary line, e.g. "This cannot be undone."
   * @param {Array<string>} [opts.items]  bullet list, e.g. affected filenames
   * @param {string} [opts.confirmLabel='Confirm']
   * @param {string} [opts.cancelLabel='Cancel']
   * @param {string} [opts.tone='danger']
   * @param {string} [opts.icon]          override the tone's icon
   * @returns {Promise<boolean>}
   */
  function confirmAction(messageOrOpts, maybeOpts) {
    const opts = typeof messageOrOpts === 'string'
      ? Object.assign({ message: messageOrOpts }, maybeOpts || {})
      : (messageOrOpts || {});

    // No modal module, or no Bootstrap: fall back to the native dialog
    // rather than silently returning false and dropping the user's action.
    if (!global.modal || !global.modal.hasBootstrap()) {
      const parts = [opts.message || opts.title || 'Are you sure?'];
      if (opts.detail) parts.push(opts.detail);
      if (opts.items && opts.items.length) parts.push(opts.items.join('\n'));
      return Promise.resolve(global.confirm(parts.join('\n\n')));
    }

    const tone = TONES[opts.tone] || TONES.danger;
    const icon = opts.icon || tone.icon;

    const itemsHtml = (opts.items && opts.items.length)
      ? `<ul class="small text-secondary mb-0 mt-2 ps-3">${
          opts.items.slice(0, 10).map((i) => `<li>${esc(i)}</li>`).join('')
        }${opts.items.length > 10
          ? `<li class="text-tertiary">…and ${opts.items.length - 10} more</li>`
          : ''}</ul>`
      : '';

    const detailHtml = opts.detail
      ? `<p class="small text-secondary mb-0 mt-2">${esc(opts.detail)}</p>`
      : '';

    const bodyHtml = `
      <div class="d-flex gap-3">
        <div class="flex-shrink-0">
          <i class="bi ${esc(icon)} ${esc(tone.iconClass)}" style="font-size:1.75rem;"></i>
        </div>
        <div class="flex-grow-1">
          <p class="mb-0">${esc(opts.message || 'Are you sure?')}</p>
          ${detailHtml}
          ${itemsHtml}
        </div>
      </div>`;

    const footerHtml =
      `<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">${
        esc(opts.cancelLabel || 'Cancel')
      }</button>` +
      `<button type="button" class="btn ${esc(tone.btn)}" data-confirm-accept>${
        esc(opts.confirmLabel || 'Confirm')
      }</button>`;

    const html = global.modal.template({
      id: MODAL_ID,
      title: opts.title || 'Are you sure?',
      bodyHtml: bodyHtml,
      footerHtml: footerHtml,
      centered: true,
    });

    return new Promise((resolve) => {
      const el = global.modal.build(MODAL_ID, html);
      if (!el) {
        resolve(global.confirm(opts.message || 'Are you sure?'));
        return;
      }

      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };

      el.querySelector('[data-confirm-accept]').addEventListener('click', function () {
        finish(true);
        global.modal.hide(el);
      });

      // Covers the Cancel button, the X, Escape, and a backdrop click.
      // Runs after the accept handler, so `settled` keeps the true result.
      el.addEventListener('hidden.bs.modal', function () {
        finish(false);
      });

      // Focus the confirm button so Enter accepts and Escape cancels,
      // matching the keyboard behaviour of the native dialog.
      el.addEventListener('shown.bs.modal', function () {
        const btn = el.querySelector('[data-confirm-accept]');
        if (btn) btn.focus();
      });

      global.modal.show(el);
    });
  }

  /**
   * Confirm a destructive action, requiring the user to type a phrase.
   *
   * For the genuinely irreversible operations — "Delete ALL completed
   * downloads", "Clear the entire queue", "Remove all unmatched folders" —
   * which currently use the same one-click confirm() as trivial actions.
   *
   * @param {Object} opts  as confirmAction, plus:
   * @param {string} opts.phrase  the text the user must type
   * @returns {Promise<boolean>}
   */
  function confirmTyped(opts = {}) {
    if (!global.modal || !global.modal.hasBootstrap()) {
      const typed = global.prompt(
        `${opts.message || 'This cannot be undone.'}\n\nType "${opts.phrase}" to continue:`
      );
      return Promise.resolve(typed === opts.phrase);
    }

    const bodyHtml = `
      <div class="d-flex gap-3">
        <div class="flex-shrink-0">
          <i class="bi bi-exclamation-triangle-fill text-danger" style="font-size:1.75rem;"></i>
        </div>
        <div class="flex-grow-1">
          <p class="mb-2">${esc(opts.message || 'This action cannot be undone.')}</p>
          <label class="form-label small text-secondary mb-1">
            Type <strong>${esc(opts.phrase)}</strong> to confirm
          </label>
          <input type="text" class="form-control" data-confirm-input autocomplete="off">
        </div>
      </div>`;

    const footerHtml =
      '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
      `<button type="button" class="btn btn-danger" data-confirm-accept disabled>${
        esc(opts.confirmLabel || 'Delete')
      }</button>`;

    const html = global.modal.template({
      id: MODAL_ID,
      title: opts.title || 'Confirm destructive action',
      bodyHtml,
      footerHtml,
      centered: true,
    });

    return new Promise((resolve) => {
      const el = global.modal.build(MODAL_ID, html);
      if (!el) return resolve(false);

      const input = el.querySelector('[data-confirm-input]');
      const accept = el.querySelector('[data-confirm-accept]');
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };

      input.addEventListener('input', function () {
        accept.disabled = this.value !== opts.phrase;
      });

      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' && !accept.disabled) {
          finish(true);
          global.modal.hide(el);
        }
      });

      accept.addEventListener('click', function () {
        finish(true);
        global.modal.hide(el);
      });

      el.addEventListener('hidden.bs.modal', function () { finish(false); });
      el.addEventListener('shown.bs.modal', function () { input.focus(); });

      global.modal.show(el);
    });
  }

  global.ui = global.ui || {};
  global.ui.confirm = confirmAction;
  global.ui.confirmTyped = confirmTyped;

  // NOTE: `window.confirm` is intentionally left alone. Overriding a
  // synchronous API with an async one silently breaks every existing
  // `if (!confirm(...)) return;` guard — the Promise is always truthy, so
  // the guard never fires and destructive actions run unconditionally.
})(window);
