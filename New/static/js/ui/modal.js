/* ==========================================================================
   static/js/ui/modal.js
   Dynamically-built Bootstrap modals.

   Load AFTER utils/dom.js:

       <script src="{{ url_for('static', filename='js/ui/modal.js') }}"></script>

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   Seven places build a modal from an HTML string at runtime, each repeating
   the same recipe with a different set of mistakes:

     artist_detail.html  openEditArtistIdsModal
     artist_detail.html  openArtistImageModal
     artist_detail.html  editArtistCountry
     artist_detail.html  openArtistMusicBrainzLookup
     add_to_playlist.js  _atpBuildModal
     downloads.js        ensureSoulseekManualSearchModal
     genre_utils.js      showScanProgressModal
     musicbrainz-folder-groups.js  viewFolderContents

   The recipe is: build a template string → remove any existing node with
   the same id → `document.body.insertAdjacentHTML('beforeend', html)` →
   `new bootstrap.Modal(el)` → `.show()`.

   Three problems this module fixes:

   1. ORPHANED NODES. Of the four modals built in artist_detail.html, NONE
      removes itself on hide. Each `openEditArtistIdsModal()` call re-runs
      `existingModal.remove()` first, so the DOM does not grow without
      bound — but the Bootstrap instance attached to the removed node is
      never disposed, so its backdrop and its `body.modal-open` class can
      be left behind, which is what makes the page occasionally
      unscrollable after closing one of these.
      `viewFolderContents` is the only one that gets this right
      (`modal.addEventListener('hidden.bs.modal', () => modal.remove())`).

   2. DUPLICATE IDS. `_atpBuildModal` caches its element in `_atpModalEl`
      and returns early if set — but it never checks whether that node is
      still IN the document. After any re-render that replaces body
      content, the cached reference is stale and a second modal with
      id="addToPlaylistModal" is appended. `getElementById` then resolves
      to the FIRST one, which is the detached copy.

   3. MISSING BOOTSTRAP GUARD. Several call `new bootstrap.Modal(...)`
      with no check, so a page where Bootstrap JS failed to load throws
      "bootstrap is not defined" rather than degrading.
      `searchMusicBrainzRelease` and `searchMusicBrainzForAllReleases` DO
      guard, with a hand-rolled fallback that sets `.show`/`display:block`
      manually — but that fallback never adds a backdrop and never restores
      `body.modal-open`, so the page stays locked after closing.

   Everything below builds on ONE helper so the behaviour is identical
   everywhere.
   ========================================================================== */

(function (global) {
  'use strict';

  function hasBootstrapModal() {
    return !!(global.bootstrap && global.bootstrap.Modal);
  }

  /**
   * Remove a previously-built modal, disposing its Bootstrap instance first.
   *
   * Disposal matters: removing the node alone orphans the backdrop element
   * and leaves `modal-open` on <body>, which disables page scrolling.
   *
   * @param {string} id
   */
  function destroy(id) {
    const existing = document.getElementById(id);
    if (!existing) return;

    if (hasBootstrapModal()) {
      const instance = global.bootstrap.Modal.getInstance(existing);
      if (instance) {
        try {
          instance.dispose();
        } catch (_e) {
          /* already disposed */
        }
      }
    }
    existing.remove();

    // Belt and braces: if no other modal is open, clear the leftovers that
    // a disposed-mid-transition modal can leave behind.
    if (!document.querySelector('.modal.show')) {
      document.body.classList.remove('modal-open');
      document.body.style.removeProperty('padding-right');
      document.querySelectorAll('.modal-backdrop').forEach((el) => el.remove());
    }
  }

  /**
   * Build (or rebuild) a modal from an HTML string and return its element.
   *
   * The html MUST contain exactly one root element carrying the given id.
   *
   * @param {string} id
   * @param {string} html
   * @param {Object} [opts]
   * @param {boolean} [opts.replace=true]  rebuild if it already exists
   * @param {boolean} [opts.autoDestroy=true] remove from the DOM on hide
   * @returns {HTMLElement|null}
   */
  function build(id, html, opts = {}) {
    const replace = opts.replace !== false;
    const autoDestroy = opts.autoDestroy !== false;

    const existing = document.getElementById(id);
    if (existing && !replace) return existing;
    if (existing) destroy(id);

    document.body.insertAdjacentHTML('beforeend', html);
    const el = document.getElementById(id);
    if (!el) {
      console.error(`[modal] build("${id}") — markup has no element with that id`);
      return null;
    }

    if (autoDestroy) {
      el.addEventListener('hidden.bs.modal', function () {
        destroy(id);
      });
    }
    return el;
  }

  /**
   * Show a modal element (or id).
   *
   * Degrades gracefully when Bootstrap JS is unavailable: the manual
   * fallback now also renders a backdrop and sets `modal-open`, so the
   * page is not left in a half-open state — which the two hand-rolled
   * fallbacks in artist_detail.html both got wrong.
   *
   * @param {HTMLElement|string} target
   * @returns {HTMLElement|null}
   */
  function show(target) {
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return null;

    if (hasBootstrapModal()) {
      global.bootstrap.Modal.getOrCreateInstance(el).show();
      return el;
    }

    el.style.display = 'block';
    el.classList.add('show');
    el.removeAttribute('aria-hidden');
    el.setAttribute('aria-modal', 'true');
    document.body.classList.add('modal-open');

    if (!document.querySelector('.modal-backdrop')) {
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop fade show';
      backdrop.dataset.fallbackFor = el.id || '';
      document.body.appendChild(backdrop);
    }
    return el;
  }

  /**
   * Hide a modal element (or id). Mirrors `show`'s fallback handling.
   *
   * @param {HTMLElement|string} target
   */
  function hide(target) {
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return;

    if (hasBootstrapModal()) {
      const instance = global.bootstrap.Modal.getInstance(el);
      if (instance) instance.hide();
      return;
    }

    el.style.display = 'none';
    el.classList.remove('show');
    el.setAttribute('aria-hidden', 'true');
    el.removeAttribute('aria-modal');
    document.body.classList.remove('modal-open');
    document.querySelectorAll('.modal-backdrop').forEach((b) => b.remove());
  }

  /**
   * Build and immediately show — the common case.
   *
   * @param {string} id
   * @param {string} html
   * @param {Object} [opts] passed through to build()
   * @returns {HTMLElement|null}
   */
  function open(id, html, opts) {
    const el = build(id, html, opts);
    return el ? show(el) : null;
  }

  /**
   * Assemble standard modal markup so callers only supply the parts that
   * differ. Escapes the title; `bodyHtml` and `footerHtml` are inserted
   * as-is, so callers MUST escape any interpolated values themselves
   * (use escapeHtml from utils/dom.js).
   *
   * @param {Object} opts
   * @param {string} opts.id
   * @param {string} opts.title        plain text; escaped here
   * @param {string} [opts.icon]       bootstrap-icons class, e.g. 'bi-pencil'
   * @param {string} opts.bodyHtml
   * @param {string} [opts.footerHtml]
   * @param {string} [opts.size]       'modal-sm' | 'modal-lg' | 'modal-xl'
   * @param {boolean} [opts.scrollable=false]
   * @param {boolean} [opts.centered=false]
   * @param {boolean} [opts.staticBackdrop=false]
   * @returns {string}
   */
  function template(opts) {
    const esc = global.escapeHtml || ((v) => String(v == null ? '' : v));
    const dialogClasses = [
      'modal-dialog',
      opts.size || '',
      opts.scrollable ? 'modal-dialog-scrollable' : '',
      opts.centered ? 'modal-dialog-centered' : '',
    ].filter(Boolean).join(' ');

    const staticAttrs = opts.staticBackdrop
      ? ' data-bs-backdrop="static" data-bs-keyboard="false"'
      : '';
    const iconHtml = opts.icon ? `<i class="bi ${esc(opts.icon)}"></i> ` : '';
    const footer = opts.footerHtml
      ? `<div class="modal-footer">${opts.footerHtml}</div>`
      : '';
    const labelId = `${opts.id}Label`;

    return `
      <div class="modal fade" id="${esc(opts.id)}" tabindex="-1" aria-labelledby="${esc(labelId)}" aria-hidden="true"${staticAttrs}>
        <div class="${dialogClasses}">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title" id="${esc(labelId)}">${iconHtml}${esc(opts.title || '')}</h5>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">${opts.bodyHtml || ''}</div>
            ${footer}
          </div>
        </div>
      </div>`;
  }

  /**
   * Standard Cancel + primary-action footer.
   *
   * @param {Object} [opts]
   * @param {string} [opts.confirmLabel='Save']
   * @param {string} [opts.confirmIcon='bi-check-lg']
   * @param {string} [opts.confirmClass='btn-primary']
   * @param {string} [opts.confirmId]
   * @param {string} [opts.onConfirm]  inline handler body (legacy call sites)
   * @param {string} [opts.cancelLabel='Cancel']
   * @param {string} [opts.extraHtml]  buttons inserted before Cancel
   * @returns {string}
   */
  function footer(opts = {}) {
    const esc = global.escapeHtml || ((v) => String(v == null ? '' : v));
    const idAttr = opts.confirmId ? ` id="${esc(opts.confirmId)}"` : '';
    const onClick = opts.onConfirm ? ` onclick="${esc(opts.onConfirm)}"` : '';
    return (
      (opts.extraHtml || '') +
      `<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">${esc(opts.cancelLabel || 'Cancel')}</button>` +
      `<button type="button" class="btn ${esc(opts.confirmClass || 'btn-primary')}"${idAttr}${onClick}>` +
      `<i class="bi ${esc(opts.confirmIcon || 'bi-check-lg')}"></i> ${esc(opts.confirmLabel || 'Save')}</button>`
    );
  }

  /**
   * Fetch the live instance for an existing modal, or null.
   * @param {HTMLElement|string} target
   */
  function instance(target) {
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el || !hasBootstrapModal()) return null;
    return global.bootstrap.Modal.getInstance(el);
  }

  global.modal = {
    build,
    open,
    show,
    hide,
    destroy,
    template,
    footer,
    instance,
    hasBootstrap: hasBootstrapModal,
  };
})(window);
