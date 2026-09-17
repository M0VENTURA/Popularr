/* ==========================================================================
   static/js/ui/add-to-playlist.js
   "Add to Playlist" picker — album page (per-track ⋮ menu) and track page.

   Requires: utils/dom.js, utils/api.js, ui/modal.js, ui/toast.js,
             ui/button-state.js

   ── CHANGES FROM THE PREVIOUS VERSION ─────────────────────────────────────
   1. STALE MODAL CACHE — FIXED BUG.
        let _atpModalEl = null;
        function _atpBuildModal() { if (_atpModalEl) return; ... }
      The cached element was never checked against the document. After any
      re-render that replaced body content, `_atpModalEl` pointed at a
      DETACHED node, the guard returned early, and a SECOND element with
      id="addToPlaylistModal" was appended. `getElementById` then resolved to
      the first (detached) copy, so the visible modal's fields were never
      read. ui/modal.js rebuilds by id and disposes the old instance.

   2. `_atpEsc` / `_atpAttr` deleted — two more copies of escapeHtml.
      `_atpAttr` additionally escaped `"` a second time on already-escaped
      output; escapeHtml in utils/dom.js escapes quotes once, correctly, so
      the same function now serves both text and attribute contexts.

   3. Inline `onchange="_atpToggleCreateNew()"` / `onchange="_atpClearNewName()"`
      / `onclick="submitAddToPlaylist()"` replaced with addEventListener.
      The radio values are playlist ids from the server; an id containing a
      quote broke the generated attribute.

   4. The save button used a bare `button.disabled = true` with no spinner and
      no restore on early return. Now ui/button-state.js, which always
      restores.
   ========================================================================== */

(function (global) {
  'use strict';

  const MODAL_ID = 'addToPlaylistModal';

  let trackId = null;
  let trackTitle = '';
  let playlists = [];

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function showError(message) {
    const el = document.getElementById('atpError');
    if (!el) return;
    el.textContent = message;
    el.classList.remove('d-none');
  }

  function clearError() {
    const el = document.getElementById('atpError');
    if (el) el.classList.add('d-none');
  }

  function buildModal() {
    const bodyHtml = `
      <p class="small text-muted mb-2 text-truncate" id="atpTrackLabel"></p>
      <div id="atpOptions" class="d-flex flex-column gap-2" style="max-height:38vh;overflow-y:auto;">
        <div class="text-center text-muted py-3 small">Loading playlists…</div>
      </div>
      <div class="form-check mt-3">
        <input class="form-check-input" type="radio" name="atpChoice" id="atpCreateNew" value="new">
        <label class="form-check-label" for="atpCreateNew">
          <i class="bi bi-plus-circle me-1"></i>Create New Playlist…
        </label>
      </div>
      <div class="d-none mt-2" id="atpNewNameRow">
        <input type="text" id="atpNewName" class="form-control form-control-sm"
               placeholder="Playlist name" maxlength="120" autocomplete="off">
      </div>
      <div id="atpError" class="alert alert-danger py-2 small d-none mt-2"></div>`;

    const footerHtml =
      '<button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>' +
      '<button type="button" class="btn btn-sm btn-success" id="atpSaveBtn">' +
      '<i class="bi bi-check-lg"></i> Add</button>';

    const el = global.modal.build(MODAL_ID, global.modal.template({
      id: MODAL_ID,
      title: 'Add to Playlist',
      icon: 'bi-list-plus',
      bodyHtml,
      footerHtml,
      scrollable: true,
    }));
    if (!el) return null;

    el.querySelector('#atpCreateNew').addEventListener('change', toggleCreateNew);
    el.querySelector('#atpSaveBtn').addEventListener('click', submit);
    return el;
  }

  function toggleCreateNew() {
    const row = document.getElementById('atpNewNameRow');
    const checked = document.getElementById('atpCreateNew').checked;
    row.classList.toggle('d-none', !checked);
    if (checked) document.getElementById('atpNewName').focus();
  }

  function clearNewName() {
    const input = document.getElementById('atpNewName');
    if (input) input.value = '';
    const row = document.getElementById('atpNewNameRow');
    if (row) row.classList.add('d-none');
  }

  /**
   * Open the picker for a track.
   * @param {string|number} id
   * @param {string} title
   */
  function open(id, title) {
    trackId = id;
    trackTitle = title || '';
    const el = buildModal();
    if (!el) return;
    global.modal.show(el);
    loadOptions();
  }

  async function loadOptions() {
    const label = document.getElementById('atpTrackLabel');
    if (label) label.textContent = `Track: ${trackTitle || trackId}`;
    clearError();

    const options = document.getElementById('atpOptions');
    if (!options) return;
    options.innerHTML = '<div class="text-center text-muted py-3 small">Loading playlists…</div>';

    try {
      const data = await global.api.getJson('/api/playlists/all');

      // Navidrome playlists are read-only from here — file-backed only.
      playlists = (data.playlists || []).filter((p) => p.source === 'file');

      if (!playlists.length) {
        options.innerHTML =
          '<div class="text-center text-muted py-3 small">' +
          'No editable playlists yet — use "Create New" below.</div>';
        return;
      }

      options.innerHTML = playlists.map((playlist, index) => `
        <div class="form-check">
          <input class="form-check-input" type="radio" name="atpChoice" id="atpP${index}"
                 value="${esc(playlist.id)}">
          <label class="form-check-label" for="atpP${index}" style="cursor:pointer;">
            <i class="bi ${playlist.kind === 'm3u' ? 'bi-music-note-list' : 'bi-stars'} me-1 text-warning"></i>
            ${esc(playlist.name)}
            <span class="badge bg-secondary ms-1">${esc(playlist.track_count || 0)}</span>
            <small class="text-muted ms-1">${playlist.kind === 'm3u' ? 'M3U' : 'Smart'}</small>
          </label>
        </div>`).join('');

      options.querySelectorAll('input[name="atpChoice"]').forEach((radio) => {
        radio.addEventListener('change', clearNewName);
      });
    } catch (error) {
      options.innerHTML = `<div class="text-danger small py-2">${esc(error.message)}</div>`;
    }
  }

  function submit() {
    clearError();

    const selected = document.querySelector('input[name="atpChoice"]:checked');
    if (!selected) {
      showError('Select a playlist or choose "Create New".');
      return;
    }

    const payload = { track_id: trackId };
    if (selected.value === 'new') {
      const name = (document.getElementById('atpNewName').value || '').trim();
      if (!name) {
        showError('Enter a playlist name.');
        return;
      }
      payload.new_name = name;
    } else {
      payload.playlist_id = selected.value;
    }

    const button = document.getElementById('atpSaveBtn');
    return global.buttonState.withBusy(button, 'Adding…', async () => {
      try {
        const data = await global.api.postJson('/api/playlists/add-track', payload);
        global.modal.hide(MODAL_ID);
        global.toast.show({
          title: 'Added to playlist',
          message: `"${trackTitle || 'Track'}" → ${data.playlist}`,
          type: data.added === false ? 'info' : 'success',
        });
      } catch (error) {
        showError(error.message);
      }
    });
  }

  global.addToPlaylist = { open, loadOptions };

  // Legacy names still referenced by inline onclick="" in the track and
  // album templates.
  global.openAddToPlaylistModal = open;
  global.submitAddToPlaylist = submit;
})(window);
