/* ==========================================================================
   static/js/pages/corrections.js
   Tag Corrections — the single home for tag/metadata correction work.

   Two independent halves, both on this one page:

     A. METADATA CONFLICTS  (/api/conflicts/*)  — fields that were blocked from
        auto-overwrite during scans because they differ from curated local
        values. Accept (take the provider's value) or Keep (drop the conflict).

     B. TAG INCONSISTENCIES (/api/correcting/*) — albums whose album-level tags
        disagree across their own tracks, which is what makes Navidrome show one
        album as several entries. Per field you can apply a specific value,
        apply the majority value for the whole album, pull MusicBrainz's
        authoritative values, or ignore the field.

   Load order: utils/dom.js → utils/api.js → ui/toast.js → ui/modal.js →
               ui/confirm.js → ui/button-state.js, then this file.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/corrections.html carried a 493-line inline <script> and a
   30-line inline <style>. The script is this file; the CSS is
   static/css/corrections.css.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. THE MUSICBRAINZ PANEL'S APPLY BUTTONS WERE WIRED TWICE, DIFFERENTLY.
      Server-rendered `.apply-field-btn` elements were bound in a
      querySelectorAll loop at load. The MusicBrainz panel then built MORE of
      the same class with innerHTML and wired each one again inline. The two
      paths did the same thing differently, and any button added later (another
      panel, a re-render) would have been dead. One delegated handler now covers
      every button with that class, however it was created.

   2. DATA ATTRIBUTES BUILT WITH `.replace(/"/g,'&quot;')`. The MB panel
      assembled its buttons by string concatenation, escaping only double
      quotes — not `&`, `<` or `'`. A MusicBrainz value such as `Rock & Roll`
      would render as `&amp;`-less text, and a value containing `<` could inject
      markup. Now built with utils/dom.js's escapeHtml, which covers all five
      entities, in both attribute and text position.

   3. THE CONFLICT COUNT BADGE WAS FOUND BY APPEARANCE:

          document.querySelector('.badge.bg-danger.fs-6')

      That matches ANY badge with those three classes, so it would have grabbed
      the wrong element the moment another red badge appeared. It now has an id.

   4. `new bootstrap.Modal(...)` AT IIFE TOP → ui/modal.js (with a Bootstrap
      fallback). The original constructed the progress modal before the page had
      finished parsing, and a missing element would have thrown for the whole
      IIFE, taking every handler with it.

   5. `alert()` / `confirm()` → toast / ui.confirm, and raw `fetch` +
      `.json()` → api.getJson/postJson, so an HTML error page reads as an auth or
      server problem instead of "Unexpected token '<'".

   ── WHY THE PAGE STILL RELOADS AFTER A FIX ────────────────────────────────
   The inconsistency list is rendered SERVER-SIDE, paginated, with each album's
   values already grouped. Patching it in place after a fix would mean
   re-deriving all of that in the browser and could disagree with what a
   refresh shows. The conflict list, by contrast, is client-rendered and IS
   patched in place (the row is removed) — the difference is deliberate.
   ========================================================================== */

(function (global) {
  'use strict';

  const FIX_ENDPOINT = '/api/correcting/fix-album-field';
  const IGNORE_ENDPOINT = '/api/correcting/ignore';
  const UNIGNORE_ENDPOINT = '/api/correcting/unignore';
  const IGNORES_ENDPOINT = '/api/correcting/ignores';
  const MB_SUGGESTIONS_ENDPOINT = '/api/correcting/mb-suggestions';
  const CONFLICTS_ENDPOINT = '/api/conflicts/pending';
  const CONFLICT_RESOLVE_ENDPOINT = '/api/conflicts/resolve';
  const CONFLICT_IGNORE_ENDPOINT = '/api/conflicts/ignore';
  const CONFLICT_STATS_ENDPOINT = '/api/conflicts/stats';
  const CONFLICT_LIMIT = 50;

  /** Field key -> display label, for MusicBrainz suggestions. */
  const FIELD_LABELS = {
    year: 'Year',
    releasetype: 'Release Type',
    releasestatus: 'Release Status',
    releasecountry: 'Release Country',
    label: 'Label',
    recordlabel: 'Record Label',
    tracktotal: 'Track Total',
    disctotal: 'Disc Total',
    compilation: 'Compilation',
    media: 'Media',
    script: 'Script',
    language: 'Language',
    discsubtitle: 'Disc Subtitle',
    catalognumber: 'Catalog Number',
    barcode: 'Barcode',
    asin: 'ASIN',
  };

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function showModal(el) {
    if (el && global.modal) global.modal.show(el);
  }

  function hideModal(el) {
    if (el && global.modal) global.modal.hide(el);
  }

  /** The blocking progress modal used while a fix runs. */
  function showProgress(text) {
    const el = byId('fixProgressModal');
    const label = byId('fixProgressText');
    if (label) label.textContent = text;
    showModal(el);
  }

  function hideProgress() {
    hideModal(byId('fixProgressModal'));
  }

  function reloadSoon(delay) {
    setTimeout(() => global.location.reload(), delay || 400);
  }

  // ── A. Single-field fixes ───────────────────────────────────────────────

  async function applyFix(albumArtist, album, field, value) {
    showProgress(`Setting ${field}…`);
    try {
      const data = await global.api.postJson(FIX_ENDPOINT, {
        album_artist: albumArtist,
        album,
        field,
        // Empty string is meaningful here: it clears the field, and the backend
        // distinguishes it from "not supplied".
        value: value,
      });
      hideProgress();

      if (data.success) {
        reloadSoon();
      } else {
        global.toast.error(data.error || 'Could not apply the fix');
      }
    } catch (error) {
      hideProgress();
      global.toast.error('Could not apply the fix: ' + error.message);
    }
  }

  async function applyFieldFix(button) {
    const { albumArtist, album, field, value } = button.dataset;
    const shown = value || '(empty)';

    const accepted = await global.ui.confirm({
      title: 'Apply value',
      message: `Apply "${shown}" to ALL tracks of "${album}" for ${field}?`,
      detail: 'This updates the database and rewrites the tag in each audio file.',
      tone: 'primary',
      confirmLabel: 'Apply',
    });
    if (!accepted) return;

    return applyFix(albumArtist, album, field, value);
  }

  /**
   * Apply the majority value for every inconsistent field on one album.
   *
   * "Majority" needs no extra data: within a row the values are rendered
   * highest-count-first, so the FIRST apply button in the row holds the most
   * common value.
   */
  async function fixAllMajority(button) {
    const { albumArtist, album, index } = button.dataset;
    const card = byId('album-' + index);
    if (!card) return;

    const fixes = [];
    card.querySelectorAll('tbody tr').forEach((row) => {
      // Skip the MusicBrainz suggestions table if its panel is open inside the
      // same card — only the main values table carries the album's fields.
      if (row.closest('.mb-suggestions-content')) return;
      const firstBtn = row.querySelector('.apply-field-btn');
      if (firstBtn) {
        fixes.push({ field: firstBtn.dataset.field, value: firstBtn.dataset.value });
      }
    });

    if (!fixes.length) {
      global.toast.info('No fields to fix on this album.');
      return;
    }

    const accepted = await global.ui.confirm({
      title: 'Fix all (majority)',
      message: `Apply the most common value for ${fixes.length} field(s) to ALL tracks of "${album}"?`,
      items: fixes.map((f) => `${f.field} = ${f.value || '(empty)'}`),
      tone: 'primary',
      confirmLabel: 'Fix all',
    });
    if (!accepted) return;

    showProgress('Applying fixes…');

    // Sequential on purpose: the endpoint rewrites audio file tags per album,
    // and firing several at once for the same album races on those files.
    for (const fix of fixes) {
      const label = byId('fixProgressText');
      if (label) label.textContent = `Setting ${fix.field} = "${fix.value || '(empty)'}"…`;

      try {
        const data = await global.api.postJson(FIX_ENDPOINT, {
          album_artist: albumArtist,
          album,
          field: fix.field,
          value: fix.value,
        });
        if (!data.success) {
          hideProgress();
          global.toast.error(`Could not set "${fix.field}": ${data.error || 'unknown error'}`);
          return;
        }
      } catch (error) {
        hideProgress();
        global.toast.error(`Could not set "${fix.field}": ${error.message}`);
        return;
      }
    }

    hideProgress();
    reloadSoon();
  }

  // ── Ignore / unignore a field ───────────────────────────────────────────

  async function ignoreField(button) {
    const { albumArtist, album, field, fieldLabel } = button.dataset;
    const label = fieldLabel || field;

    const accepted = await global.ui.confirm({
      title: 'Ignore inconsistency',
      message: `Ignore the "${label}" inconsistency for "${album}"?`,
      detail: 'It will stop appearing in the corrections list. You can restore it later from the Ignored panel.',
      tone: 'warning',
      confirmLabel: 'Ignore',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson(IGNORE_ENDPOINT, {
        album_artist: albumArtist,
        album,
        field,
      });
      if (!data.success) {
        global.toast.error(data.error || 'Could not ignore that field');
        return;
      }
      // The row is server-rendered, so remove it in place rather than reloading —
      // the rest of the table is still correct.
      const row = button.closest('tr');
      if (row) row.remove();
      global.toast.success(`"${label}" will no longer be reported for this album.`);
    } catch (error) {
      global.toast.error('Could not ignore that field: ' + error.message);
    }
  }

  async function unignoreField(ignore, badgeEl, rowsEl, emptyEl) {
    try {
      const data = await global.api.postJson(UNIGNORE_ENDPOINT, {
        album_artist: ignore.album_artist,
        album: ignore.album,
        field: ignore.field,
      });
      if (!data.success) {
        global.toast.error(data.error || 'Could not restore that field');
        return;
      }
      badgeEl.remove();
      if (rowsEl && !rowsEl.children.length && emptyEl) {
        emptyEl.classList.remove('d-none');
      }
    } catch (error) {
      global.toast.error('Could not restore that field: ' + error.message);
    }
  }

  /** Toggle the per-album "Ignored fields" panel. */
  async function toggleIgnores(button) {
    const { albumArtist, album, index } = button.dataset;
    const panel = byId('ignores-panel-' + index);
    if (!panel) return;

    if (!panel.classList.contains('d-none')) {
      panel.classList.add('d-none');
      return;
    }

    panel.classList.remove('d-none');
    const loadingEl = panel.querySelector('.ignores-loading');
    const emptyEl = panel.querySelector('.ignores-empty');
    const rowsEl = panel.querySelector('.ignores-rows');

    if (loadingEl) loadingEl.classList.remove('d-none');
    if (emptyEl) emptyEl.classList.add('d-none');
    if (rowsEl) rowsEl.innerHTML = '';

    try {
      const params = new URLSearchParams({ album_artist: albumArtist, album });
      const data = await global.api.getJson(`${IGNORES_ENDPOINT}?${params}`);
      if (loadingEl) loadingEl.classList.add('d-none');

      const ignores = data.ignores || [];
      if (!data.success || !ignores.length) {
        if (emptyEl) emptyEl.classList.remove('d-none');
        return;
      }

      ignores.forEach((ignore) => {
        const badge = document.createElement('span');
        badge.className = 'badge bg-secondary d-flex align-items-center gap-1';
        badge.style.fontSize = '0.8rem';
        badge.appendChild(document.createTextNode(ignore.field));

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn-close btn-close-white ms-1';
        removeBtn.style.cssText = 'font-size:0.6rem;width:0.8em;height:0.8em;';
        removeBtn.title = 'Restore this field to the corrections list';
        removeBtn.setAttribute('aria-label', `Restore ${ignore.field}`);
        removeBtn.addEventListener('click', function () {
          unignoreField(ignore, badge, rowsEl, emptyEl);
        });

        badge.appendChild(removeBtn);
        if (rowsEl) rowsEl.appendChild(badge);
      });
    } catch (error) {
      if (loadingEl) loadingEl.classList.add('d-none');
      if (emptyEl) {
        emptyEl.textContent = 'Could not load ignored fields: ' + error.message;
        emptyEl.classList.remove('d-none');
      }
    }
  }

  // ── MusicBrainz suggestions ─────────────────────────────────────────────

  async function lookupMusicBrainz(button) {
    const { albumArtist, album, index } = button.dataset;
    const panel = byId('mb-panel-' + index);
    if (!panel) return;

    const loadingEl = panel.querySelector('.mb-suggestions-loading');
    const errorEl = panel.querySelector('.mb-suggestions-error');
    const contentEl = panel.querySelector('.mb-suggestions-content');
    const rowsEl = panel.querySelector('.mb-suggestions-rows');
    const mbidLabel = panel.querySelector('.mb-mbid-label');

    const showError = (message) => {
      if (!errorEl) return;
      errorEl.textContent = message;
      errorEl.classList.remove('d-none');
    };

    panel.classList.remove('d-none');
    if (loadingEl) loadingEl.classList.remove('d-none');
    if (errorEl) errorEl.classList.add('d-none');
    if (contentEl) contentEl.classList.add('d-none');

    return global.buttonState.withBusy(button, '', async () => {
      try {
        const params = new URLSearchParams({ album_artist: albumArtist, album });
        const data = await global.api.getJson(`${MB_SUGGESTIONS_ENDPOINT}?${params}`);
        if (loadingEl) loadingEl.classList.add('d-none');

        if (!data.success) {
          showError('MusicBrainz: ' + (data.error || 'no data found'));
          return;
        }

        const suggestions = data.suggestions || {};
        if (!Object.keys(suggestions).length) {
          showError('MusicBrainz returned no relevant field values for this release.');
          return;
        }

        if (rowsEl) {
          // Built with escapeHtml for BOTH text and attribute position. The
          // original escaped only double quotes by hand — see bug 2.
          rowsEl.innerHTML = Object.entries(suggestions).map(([field, value]) => {
            const label = FIELD_LABELS[field] || field;
            const shown = value.length > 40 ? value.slice(0, 40) + '…' : value;
            return `
              <tr>
                <td class="fw-semibold align-middle">${esc(label)}</td>
                <td><span class="badge bg-info text-dark" title="${esc(value)}">${esc(shown)}</span></td>
                <td class="text-end align-middle">
                  <button type="button" class="btn btn-sm btn-outline-info fix-btn apply-field-btn"
                          data-action="corr-apply-field"
                          data-album-artist="${esc(albumArtist)}"
                          data-album="${esc(album)}"
                          data-field="${esc(field)}"
                          data-value="${esc(value)}"
                          title="Apply the MusicBrainz value &quot;${esc(value)}&quot; to all tracks">
                    <i class="bi bi-database-check me-1"></i>Apply MB value
                  </button>
                </td>
              </tr>`;
          }).join('');
        }

        if (mbidLabel) mbidLabel.textContent = data.mbid ? ' — MBID: ' + data.mbid : '';
        if (contentEl) contentEl.classList.remove('d-none');
      } catch (error) {
        if (loadingEl) loadingEl.classList.add('d-none');
        showError('Request failed: ' + error.message);
      }
    });
  }

  // ── B. Metadata conflicts ───────────────────────────────────────────────

  function conflictRow(conflict) {
    const tr = document.createElement('tr');

    const trackParts = [];
    if (conflict.track_title) trackParts.push(conflict.track_title);
    if (conflict.artist_name) trackParts.push('by ' + conflict.artist_name);
    if (conflict.album_name) trackParts.push('on ' + conflict.album_name);

    const cell = (className, text) => {
      const td = document.createElement('td');
      td.className = className;
      if (text != null) td.textContent = text;
      return td;
    };

    tr.appendChild(cell('align-middle small', trackParts.join(' ') || conflict.track_id));
    tr.appendChild(cell('align-middle fw-semibold small', conflict.field_name));

    const localCell = cell('align-middle small');
    const localBadge = document.createElement('span');
    localBadge.className = 'badge bg-info text-dark';
    localBadge.textContent = conflict.local_value || '(empty)';
    localCell.appendChild(localBadge);
    tr.appendChild(localCell);

    const remoteCell = cell('align-middle small');
    const remoteBadge = document.createElement('span');
    remoteBadge.className = 'badge bg-warning text-dark';
    remoteBadge.textContent = conflict.remote_value || '(empty)';
    remoteCell.appendChild(remoteBadge);
    if (conflict.provider) {
      remoteCell.appendChild(document.createElement('br'));
      const source = document.createElement('small');
      source.className = 'text-secondary';
      source.textContent = 'source: ' + conflict.provider;
      remoteCell.appendChild(source);
    }
    tr.appendChild(remoteCell);

    const actionCell = cell('text-end align-middle');

    const acceptBtn = document.createElement('button');
    acceptBtn.type = 'button';
    acceptBtn.className = 'btn btn-sm btn-outline-success me-1';
    acceptBtn.innerHTML = '<i class="bi bi-check-lg"></i> Accept';
    acceptBtn.title = 'Apply the provider value to this track';
    acceptBtn.addEventListener('click', function () {
      resolveConflict(conflict, tr, acceptBtn);
    });
    actionCell.appendChild(acceptBtn);

    const keepBtn = document.createElement('button');
    keepBtn.type = 'button';
    keepBtn.className = 'btn btn-sm btn-outline-secondary';
    keepBtn.innerHTML = '<i class="bi bi-x-lg"></i> Keep';
    keepBtn.title = 'Keep the local value and stop reporting this conflict';
    keepBtn.addEventListener('click', function () {
      ignoreConflict(conflict, tr, keepBtn);
    });
    actionCell.appendChild(keepBtn);

    tr.appendChild(actionCell);
    return tr;
  }

  async function loadConflicts() {
    const section = byId('conflicts-section');
    if (!section) return;

    const loadingEl = byId('conflicts-loading');
    const errorEl = byId('conflicts-error');
    const emptyEl = byId('conflicts-empty');
    const listEl = byId('conflicts-list');
    const tbody = byId('conflicts-tbody');
    const countEl = byId('conflicts-count');

    if (loadingEl) loadingEl.classList.remove('d-none');
    if (errorEl) errorEl.classList.add('d-none');
    if (emptyEl) emptyEl.classList.add('d-none');
    if (listEl) listEl.classList.add('d-none');
    if (tbody) tbody.innerHTML = '';

    try {
      const data = await global.api.getJson(`${CONFLICTS_ENDPOINT}?limit=${CONFLICT_LIMIT}`);
      if (loadingEl) loadingEl.classList.add('d-none');

      if (!data.success) {
        if (errorEl) {
          errorEl.textContent = 'Could not load conflicts: ' + (data.error || 'unknown error');
          errorEl.classList.remove('d-none');
        }
        return;
      }

      const items = data.conflicts || [];
      if (!items.length) {
        if (emptyEl) emptyEl.classList.remove('d-none');
        return;
      }

      items.forEach((conflict) => {
        if (tbody) tbody.appendChild(conflictRow(conflict));
      });

      if (countEl) countEl.textContent = (data.total != null ? data.total : items.length) + ' total';
      if (listEl) listEl.classList.remove('d-none');
    } catch (error) {
      if (loadingEl) loadingEl.classList.add('d-none');
      if (errorEl) {
        errorEl.textContent = 'Request failed: ' + error.message;
        errorEl.classList.remove('d-none');
      }
    }
  }

  async function resolveConflict(conflict, rowEl, button) {
    return global.buttonState.withBusy(button, '', async () => {
      try {
        const data = await global.api.postJson(CONFLICT_RESOLVE_ENDPOINT, {
          conflict_id: conflict.id,
          accepted_value: conflict.remote_value,
        });
        if (!data.success) {
          global.toast.error(data.error || 'Could not resolve the conflict');
          return;
        }
        rowEl.remove();
        await updateConflictCounts();
      } catch (error) {
        global.toast.error('Could not resolve the conflict: ' + error.message);
      }
    });
  }

  async function ignoreConflict(conflict, rowEl, button) {
    return global.buttonState.withBusy(button, '', async () => {
      try {
        const data = await global.api.postJson(CONFLICT_IGNORE_ENDPOINT, {
          conflict_id: conflict.id,
        });
        if (!data.success) {
          global.toast.error(data.error || 'Could not ignore the conflict');
          return;
        }
        rowEl.remove();
        await updateConflictCounts();
      } catch (error) {
        global.toast.error('Could not ignore the conflict: ' + error.message);
      }
    });
  }

  /**
   * Refresh the pending-conflict count and, when it reaches zero, drop the
   * whole conflicts section.
   *
   * The badge is found by ID. The original used
   * `document.querySelector('.badge.bg-danger.fs-6')`, which matches any badge
   * with those classes — see bug 3.
   */
  async function updateConflictCounts() {
    const badge = byId('conflicts-pending-badge');
    try {
      const data = await global.api.getJson(CONFLICT_STATS_ENDPOINT);
      if (!data.success || !badge) return;

      if (data.total_pending > 0) {
        badge.innerHTML =
          '<i class="bi bi-shield-exclamation me-1"></i> ' +
          `${data.total_pending} metadata conflict${data.total_pending !== 1 ? 's' : ''} pending`;
        return;
      }

      badge.remove();
      const section = byId('conflicts-section');
      if (section) section.remove();
    } catch (_error) {
      // Counts are cosmetic; leaving them stale is better than an error toast
      // on a page the user is mid-way through fixing.
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  const ACTIONS = {
    'corr-apply-field': (el) => applyFieldFix(el),
    'corr-fix-all': (el) => fixAllMajority(el),
    'corr-ignore-field': (el) => ignoreField(el),
    'corr-show-ignores': (el) => toggleIgnores(el),
    'corr-mb-lookup': (el) => lookupMusicBrainz(el),
    'corr-conflicts-refresh': () => loadConflicts(),
  };

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;
    const el = event.target.closest('[data-action]');
    if (!el) return;

    const handler = ACTIONS[el.getAttribute('data-action')];
    if (!handler) return;
    event.preventDefault();
    handler(el);
  });

  document.addEventListener('DOMContentLoaded', function () {
    // The conflicts section only exists when the server saw pending conflicts.
    if (byId('conflicts-section')) loadConflicts();
  });

  global.corrections = {
    applyFix,
    loadConflicts,
    resolveConflict,
    ignoreConflict,
    updateConflictCounts,
    lookupMusicBrainz,
    toggleIgnores,
    fixAllMajority,
  };
})(window);
