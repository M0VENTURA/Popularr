/* ==========================================================================
   test_site/static/js/services/metadata-review.js
   Album metadata review — preview a MusicBrainz import WITHOUT saving.

   ── WHY THIS EXISTS ───────────────────────────────────────────────────────
   "Lookup MBID" used to fill only a handful of Edit Album fields and leave the
   tracklist alone, so the user had to run "Compare with MusicBrainz" and apply
   changes field-by-field BEFORE the metadata they had just looked up was even
   in the form. This module makes the lookup show the FULL import: every
   album-level and per-track value a metadata import would write, each one as
   an orange bar beneath the field it affects.

   ── THE CONTRACT: NOTHING IS WRITTEN HERE ─────────────────────────────────
   Every proposal is STAGED, never applied:

     * album-level values are written into the form's own inputs;
     * per-track changes are serialised into #staged_track_updates (a hidden
       input) and posted with the form.

   So a lookup stays harmless — a wrong release is discarded by simply
   reloading the page — and the whole review becomes ONE atomic save when the
   user presses Save Metadata. That is the behaviour applyAlbumMatch()'s header
   already promised ("only fills the form"), now extended to the tracklist.

   ── WHY A SEPARATE ROW CLASS ──────────────────────────────────────────────
   The existing "Compare with MusicBrainz" flow uses `.mb-update-row` and its
   Apply button POSTs immediately (`/api/v1/tracks/<id>/apply-mb-field`). The
   rows here are deliberately `.mb-staged-row`: staged, not written. Sharing a
   class would make clearComparison()/updateAllTracksFromMB() pick up rows they
   must not touch, and would make "Apply" mean two different things on the same
   page.
   ========================================================================== */

(function (global) {
  'use strict';

  const PROPOSE_URL = '/api/album/musicbrainz/propose';

  // Album form fields the proposal engine can populate, with the label used in
  // the orange bar when the server has not supplied one.
  const ALBUM_FIELD_LABELS = {
    album_title: 'Album Title',
    album_artist: 'Album Artist',
    album_release_title: 'Release Name',
    album_originalyear: 'Original Year',
    release_year: 'Release Year',
    album_type: 'Album Type',
    album_mbid: 'MusicBrainz Release ID',
    album_release_group_mbid: 'MusicBrainz Release Group ID',
    artist_mbid: 'MusicBrainz Artist ID',
    album_recordlabel: 'Record Label',
    album_catalognumber: 'Catalog Number',
    album_barcode: 'Barcode',
    album_releasedate: 'Release Date',
    album_media: 'Media Format',
    album_releasecountry: 'Release Country',
    album_genres: 'Genres',
  };

  function esc(value) {
    if (global.escapeHtml) return global.escapeHtml(value);
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function notify(message, kind) {
    if (kind === 'error' && global.toast) return global.toast.error(message);
    if (kind !== 'error' && global.toast) return global.toast.success(message);
    // No toast module (or no kind): stay silent for success, alert for errors.
    if (kind === 'error') global.alert(message);
  }

  /** POST JSON through the page's api helper when present, else raw fetch. */
  async function postJson(url, body) {
    if (global.api && typeof global.api.postJson === 'function') {
      return global.api.postJson(url, body);
    }
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch (_e) {
      throw new Error('Server returned a non-JSON response (HTTP ' + resp.status + ').');
    }
  }

  function pageArtist() {
    return global._pageData ? global._pageData.artistName : '';
  }

  function pageAlbum() {
    return global._pageData ? global._pageData.albumName : '';
  }

  function markDirty() {
    if (typeof global.markFormDirty === 'function') {
      global.markFormDirty('albumMetadataForm');
    }
  }

  // ── Album-level form filling + orange bars ───────────────────────────────

  /** Write a value into a form field, flashing it so the change is visible. */
  function fillField(id, value) {
    const field = document.getElementById(id);
    if (!field) return false;
    if (field.tagName === 'SELECT') {
      // Only select an option that EXISTS — inventing one would change what
      // the form submits. Mirrors setAlbumTypeIfPresent().
      const wanted = String(value || '').toLowerCase();
      const match = Array.from(field.options).find((o) => {
        const v = String(o.value).toLowerCase();
        return v && (v === wanted || wanted.includes(v) || v.includes(wanted));
      });
      if (!match) return false;
      field.value = match.value;
    } else {
      field.value = value;
    }
    field.style.transition = 'background-color 0.3s';
    field.style.backgroundColor = 'var(--accent-color, rgba(255,193,7,.25))';
    setTimeout(() => { field.style.backgroundColor = ''; }, 600);
    return true;
  }

  /** The column/group element a form field lives in (bar anchor). */
  function fieldGroup(field) {
    return field.closest('[class*="col-"]') || field.closest('.form-group')
      || field.parentElement;
  }

  function clearAlbumBars() {
    document.querySelectorAll('.mb-field-suggest').forEach((el) => el.remove());
    document.querySelectorAll('.mb-field-changed').forEach((el) => {
      el.classList.remove('mb-field-changed');
    });
  }

  /**
   * Paint one orange "MusicBrainz recommends" bar beneath a form field.
   *
   * The bar is deliberately informational + dismissible: the value has ALREADY
   * been written into the input, so "Discard" restores the previous value and
   * "Keep" just removes the bar. That keeps the single source of truth (the
   * form) unambiguous — the user always sees the value that will be saved.
   */
  function renderAlbumBar(change) {
    const field = document.getElementById(change.field);
    if (!field) return false;
    const group = fieldGroup(field);
    if (!group) return false;

    const bar = document.createElement('div');
    bar.className = 'mb-field-suggest mt-1 rounded px-2 py-1';
    bar.style.cssText =
      'background:rgba(255,193,7,.14);border-left:3px solid #ffc107;font-size:.75rem;';
    bar.innerHTML =
      '<div class="d-flex align-items-start gap-2 flex-wrap">' +
        '<span class="text-warning-emphasis">' +
          '<i class="bi bi-lightning-fill me-1"></i>' +
          '<strong>' + esc(change.label || change.field) + '</strong>' +
        '</span>' +
        '<span class="text-muted">' +
          (change.current
            ? '<em>' + esc(change.current) + '</em> → '
            : '<em>empty</em> → ') +
          '<strong>' + esc(change.proposed) + '</strong>' +
        '</span>' +
        '<span class="ms-auto d-flex gap-1">' +
          '<button type="button" class="btn btn-warning btn-sm py-0 px-2 mb-keep-field" ' +
            'style="font-size:.7rem;"><i class="bi bi-check-lg"></i> Keep</button>' +
          '<button type="button" class="btn btn-outline-secondary btn-sm py-0 px-2 mb-discard-field" ' +
            'style="font-size:.7rem;"><i class="bi bi-arrow-counterclockwise"></i> Discard</button>' +
        '</span>' +
      '</div>';

    // "Keep" = accept the value already in the field: drop the bar only.
    bar.querySelector('.mb-keep-field').addEventListener('click', () => bar.remove());

    // "Discard" = restore the pre-lookup value and drop the bar.
    bar.querySelector('.mb-discard-field').addEventListener('click', () => {
      fillField(change.field, change.current);
      bar.remove();
      markDirty();
    });

    group.appendChild(bar);
    return true;
  }

  // ── Per-track staging ───────────────────────────────────────────────────

  let staged = {};

  function stagedInput() {
    return document.getElementById('staged_track_updates');
  }

  function syncStagedInput() {
    const input = stagedInput();
    if (input) input.value = JSON.stringify(staged);
  }

  function stagedCount() {
    return Object.keys(staged).length;
  }

  /** Read a track row's display cells so a bar can be anchored to it. */
  function trackRow(trackId) {
    const tbody = document.getElementById('albumTracksTbody');
    if (!tbody) return null;
    return tbody.querySelector('tr[data-track-id="' + (global.CSS && CSS.escape
      ? CSS.escape(String(trackId)) : String(trackId)) + '"]');
  }

  function clearTrackRows() {
    document.querySelectorAll('.mb-staged-row').forEach((el) => el.remove());
    staged = {};
    syncStagedInput();
  }

  function renderTrackRow(entry) {
    const row = trackRow(entry.track_id);
    if (!row) return false;

    // Sit immediately after the track row (and after any bars already added
    // for it, so several changes for one track stack in a stable order).
    let insertAfter = row;
    let sibling = insertAfter.nextElementSibling;
    while (sibling && sibling.classList.contains('mb-staged-row')) {
      insertAfter = sibling;
      sibling = sibling.nextElementSibling;
    }

    const tr = document.createElement('tr');
    tr.className = 'mb-staged-row';
    tr.dataset.trackId = String(entry.track_id);

    const lines = entry.changes.map((change) => {
      const label = change.label || change.field;
      const current = change.current ? '<em>' + esc(change.current) + '</em>' : '<em>empty</em>';
      return '<div><span class="text-muted">' + esc(label) + ':</span> ' +
        current + ' → <strong>' + esc(change.proposed) + '</strong></div>';
    }).join('');

    tr.innerHTML =
      '<td colspan="5" style="padding:.3rem .75rem;border-top:none;">' +
        '<div class="d-flex align-items-start gap-2 flex-wrap rounded px-2 py-1 ' +
             'mb-staged-inner" ' +
             'style="background:rgba(255,193,7,.14);border-left:3px solid #ffc107;">' +
          '<small class="text-warning-emphasis">' +
            '<i class="bi bi-lightning-fill me-1"></i><strong>MusicBrainz:</strong>' +
          '</small>' +
          '<small class="flex-grow-1" style="font-size:.75rem;">' + lines + '</small>' +
          '<button type="button" class="btn btn-warning btn-sm py-0 px-2 mb-stage-toggle" ' +
            'style="font-size:.7rem;white-space:nowrap;">' +
            '<i class="bi bi-check-lg"></i> Included</button>' +
          '<button type="button" class="btn btn-outline-secondary btn-sm py-0 px-2 mb-stage-drop" ' +
            'style="font-size:.7rem;white-space:nowrap;">' +
            '<i class="bi bi-x-lg"></i> Ignore</button>' +
        '</div>' +
      '</td>';

    // Toggle: the row is staged by default (it is a recommendation), and
    // Ignore simply removes it from the staged payload.
    const toggle = tr.querySelector('.mb-stage-toggle');
    const drop = tr.querySelector('.mb-stage-drop');

    function exclude() {
      delete staged[String(entry.track_id)];
      syncStagedInput();
      tr.remove();
      updateSummary();
    }

    drop.addEventListener('click', exclude);
    toggle.addEventListener('click', () => {
      const excluded = toggle.classList.toggle('btn-outline-secondary');
      toggle.classList.toggle('btn-warning', !excluded);
      toggle.innerHTML = excluded
        ? '<i class="bi bi-arrow-counterclockwise"></i> Excluded'
        : '<i class="bi bi-check-lg"></i> Included';
      if (excluded) delete staged[String(entry.track_id)];
      else staged[String(entry.track_id)] = entry;
      syncStagedInput();
      updateSummary();
    });

    insertAfter.insertAdjacentElement('afterend', tr);
    return true;
  }

  function stageTrackChanges(trackChanges) {
    staged = {};
    (trackChanges || []).forEach((entry) => {
      if (!entry || !entry.track_id || !(entry.changes || []).length) return;
      staged[String(entry.track_id)] = entry;
    });
    syncStagedInput();
    (trackChanges || []).forEach(renderTrackRow);
  }

  // ── Banner ──────────────────────────────────────────────────────────────

  function bannerHost() {
    const form = document.getElementById('albumMetadataForm');
    return form ? form.querySelector('.card-body') : null;
  }

  function clearBanner() {
    const existing = document.getElementById('mb-review-banner');
    if (existing) existing.remove();
  }

  function updateSummary() {
    const banner = document.getElementById('mb-review-banner');
    if (!banner) return;
    const slot = banner.querySelector('[data-mb-review-count]');
    if (!slot) return;
    const n = stagedCount();
    slot.textContent = n === 0
      ? 'No track changes staged.'
      : n + ' track' + (n === 1 ? '' : 's') + ' staged for saving.';
    const save = banner.querySelector('[data-mb-review-save]');
    if (save) save.disabled = n === 0;
  }

  function showBanner(counts, releaseTitle) {
    clearBanner();
    const host = bannerHost();
    if (!host) return null;

    const banner = document.createElement('div');
    banner.id = 'mb-review-banner';
    banner.className = 'alert alert-warning d-flex align-items-start gap-2 flex-wrap';
    banner.style.cssText = 'font-size:.85rem;';

    const albumCount = counts.album_changes || 0;
    const trackCount = counts.tracks_changed || 0;

    banner.innerHTML =
      '<i class="bi bi-lightning-fill"></i>' +
      '<div class="flex-grow-1">' +
        '<strong>MusicBrainz metadata ready to review' +
          (releaseTitle ? ': ' + esc(releaseTitle) : '') + '</strong>' +
        '<div class="text-muted">' +
          albumCount + ' album field' + (albumCount === 1 ? '' : 's') + ' and ' +
          trackCount + ' track' + (trackCount === 1 ? '' : 's') +
          ' can be updated. Nothing is saved until you press Save Metadata.' +
        '</div>' +
        '<div class="text-muted" data-mb-review-count></div>' +
      '</div>' +
      '<button type="button" class="btn btn-outline-secondary btn-sm py-0 px-2" ' +
        'data-mb-review-dismiss><i class="bi bi-x-lg"></i> Dismiss</button>';

    banner.querySelector('[data-mb-review-dismiss]').addEventListener('click', () => {
      clearAll();
    });

    host.insertBefore(banner, host.firstChild);
    updateSummary();
    return banner;
  }

  function clearAll() {
    clearBanner();
    clearAlbumBars();
    clearTrackRows();
  }

  // ── Entry points ────────────────────────────────────────────────────────

  /**
   * Fetch the full import proposal for a release and render it.
   *
   * Returns the server's payload (or null on failure) so a caller can tell
   * whether anything was staged.
   */
  async function applyProposal(releaseMbid) {
    const mbid = String(releaseMbid || '').trim();
    if (!mbid) return null;

    clearAll();

    let data;
    try {
      data = await postJson(PROPOSE_URL, {
        artist: pageArtist(),
        album: pageAlbum(),
        release_mbid: mbid,
      });
    } catch (error) {
      notify('Could not load MusicBrainz metadata: ' + error.message, 'error');
      return null;
    }

    if (!data || !data.success) {
      notify('Could not load MusicBrainz metadata: ' +
        ((data && data.error) || 'Unknown error'), 'error');
      return null;
    }

    (data.album_changes || []).forEach((change) => {
      const id = change.field;
      // Genres are chips + a hidden CSV field, not a plain input.
      if (id === 'album_genres') {
        applyAlbumGenres(change);
        return;
      }
      if (fillField(id, change.proposed)) renderAlbumBar(change);
    });

    stageTrackChanges(data.track_changes || []);
    showBanner(data.counts || {}, data.release_title);
    markDirty();
    return data;
  }

  /**
   * Replace the album genre chips with the proposed list.
   *
   * The chips are rendered by the page's own genre code, so the proposals are
   * merged through the page's helpers when they exist; the hidden CSV field is
   * always updated because that is what the form actually posts.
   */
  function applyAlbumGenres(change) {
    const input = document.getElementById('album_genres');
    if (!input) return;
    const proposed = String(change.proposed || '')
      .split(',').map((g) => g.trim()).filter(Boolean);
    if (!proposed.length) return;

    const existing = input.value
      ? input.value.split(',').map((g) => g.trim()).filter(Boolean) : [];
    const merged = existing.slice();
    proposed.forEach((genre) => {
      if (!merged.some((g) => g.toLowerCase() === genre.toLowerCase())) merged.push(genre);
    });
    input.value = merged.join(', ');

    if (typeof global.stageRemoveAlbumGenre === 'function' &&
        typeof global.addAlbumGenre === 'function') {
      // Best effort: let the page re-render its chips from the CSV field.
      const container = document.getElementById('albumGenresContainer');
      if (container) {
        container.innerHTML = '';
        merged.forEach((genre) => {
          const chip = document.createElement('span');
          chip.className = 'badge bg-secondary me-1 mb-1';
          chip.textContent = genre;
          container.appendChild(chip);
        });
      }
    }
    renderAlbumBar(change);
  }

  /**
   * Load recommendations a SCAN stashed for this album (metadata updating set
   * to "Recommend only") and render them exactly like a lookup proposal.
   *
   * The full recommended payload is staged into #pending_recommendations so
   * "Save Metadata" applies it atomically; "Discard" tells the server to drop
   * the stored copy so it never reappears.
   */
  async function loadPendingRecommendations() {
    const url = '/api/album/metadata-recommendations?artist=' +
      encodeURIComponent(pageArtist()) + '&album=' + encodeURIComponent(pageAlbum());

    let data;
    try {
      const resp = await fetch(url);
      const text = await resp.text();
      try {
        data = JSON.parse(text);
      } catch (_e) {
        return null;
      }
    } catch (_e) {
      return null;
    }

    if (!data || !data.success || !data.has_any) return null;

    // Reuse the lookup renderer so both sources look and behave identically.
    (data.album_changes || []).forEach((change) => {
      if (change.field === 'album_genres') { applyAlbumGenres(change); return; }
      if (fillField(change.field, change.proposed)) renderAlbumBar(change);
    });
    stageTrackChanges(data.track_changes || []);
    showBanner(data.counts || {}, data.release_title || '');

    const pendingInput = document.getElementById('pending_recommendations');
    if (pendingInput) pendingInput.value = JSON.stringify(data);

    renderPendingActions();
    markDirty();
    return data;
  }

  /** Add the Discard-all affordance to the review banner. */
  function renderPendingActions() {
    const banner = document.getElementById('mb-review-banner');
    if (!banner || banner.querySelector('[data-mb-review-discard-all]')) return;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline-danger btn-sm py-0 px-2';
    btn.setAttribute('data-mb-review-discard-all', '');
    btn.innerHTML = '<i class="bi bi-trash"></i> Discard all';
    btn.addEventListener('click', discardPendingRecommendations);
    banner.appendChild(btn);
  }

  /** Tell the server to forget the stashed recommendations for this album. */
  async function discardPendingRecommendations() {
    try {
      await postJson('/api/album/metadata-recommendations/discard', {
        artist: pageArtist(),
        album: pageAlbum(),
      });
    } catch (_e) {
      // Non-fatal: the local clear below still gives the user what they asked
      // for; the stored copy will simply be offered again on the next visit.
    }
    const pendingInput = document.getElementById('pending_recommendations');
    if (pendingInput) pendingInput.value = '';
    clearAll();
  }

  /**
   * Re-render album-level bars from the LAST successful proposal, e.g. after
   * the user changed a field by hand.
   */
  global.albumMetadataReview = {
    applyProposal,
    loadPendingRecommendations,
    discardPendingRecommendations,
    clearAll,
    stagedCount,
    stagedJson: () => JSON.stringify(staged),
    fillField,
    renderAlbumBar,
    ALBUM_FIELD_LABELS,
  };

  // Load scan-stashed recommendations on page load. Deferred to idle so it can
  // never delay the tracklist render.
  document.addEventListener('DOMContentLoaded', () => {
    const run = () => loadPendingRecommendations().catch(() => {});
    if (global.requestIdleCallback) global.requestIdleCallback(run);
    else setTimeout(run, 300);
  });
})(window);
