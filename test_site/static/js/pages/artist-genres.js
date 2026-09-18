/*
  Artist genre management (/artist/<name>/genre-management).

  Extracted from the inline <script> in templates/pages/artist_genres.html.

  WHAT CHANGED
  ------------
  * `fetch()` replaced with global.api.postJson, which throws on failure instead
    of silently resolving. The old `.then(res => res.json())` treated an HTML
    login page or a 500 as valid JSON and reported "Unknown error".
  * `alert`-style status moved to the page's #save-status banner AND global.toast,
    so the result is visible where the user clicked rather than only at the top.
  * The per-position spinner/icon lookup is unchanged (two save buttons, top and
    bottom), but the enable/disable is idempotent so a double click cannot leave
    a button stuck disabled.

  Server endpoint (unchanged): POST /api/artist/genre-management/save
*/
(function (global) {
  'use strict';

  var ARTIST_NAME = '';
  var dataEl = document.getElementById('page-data');
  if (dataEl) {
    try {
      ARTIST_NAME = (JSON.parse(dataEl.textContent) || {}).artistName || '';
    } catch (err) {
      console.warn('[artist-genres] page-data was not valid JSON', err);
    }
  }

  function api() {
    return global.api || {};
  }

  /* Save buttons exist at the top and the bottom of the page. */
  var POSITIONS = ['top', 'bottom'];

  function setSpinner(on) {
    POSITIONS.forEach(function (pos) {
      var spinner = document.getElementById('save-spinner-' + pos);
      var icon = document.getElementById('save-icon-' + pos);
      var btn = document.getElementById('save-btn-' + pos);
      if (spinner) spinner.classList.toggle('d-none', !on);
      if (icon) icon.classList.toggle('d-none', on);
      if (btn) btn.disabled = !!on;
    });
  }

  function showStatus(message, isError) {
    var el = document.getElementById('save-status');
    if (el) {
      el.textContent = message;
      el.className = 'alert mb-3 ' + (isError ? 'alert-danger' : 'alert-success');
      el.classList.remove('d-none');
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    var t = global.toast;
    if (t && typeof t[isError ? 'error' : 'success'] === 'function') {
      t[isError ? 'error' : 'success'](message);
    }
  }

  /* Tick a badge: current -> mark for removal, recommended -> mark for adding. */
  function onChange(event) {
    var chk = event.target;
    if (!chk.classList || !chk.classList.contains('genre-chk')) return;
    var wrap = chk.parentElement;
    var badge = wrap ? wrap.querySelector('[data-badge]') : null;
    if (!badge) return;

    var action = chk.dataset.action;
    if (action === 'remove') {
      badge.classList.toggle('marked-remove', chk.checked);
    } else if (action === 'add') {
      badge.classList.toggle('marked-add', chk.checked);
    }
  }

  /* Group ticked checkboxes into one change entry per scope+album+track, because
     the API applies a set of adds/removals per target rather than one call per
     genre. */
  function collectChanges(checked) {
    var changesMap = Object.create(null);

    checked.forEach(function (chk) {
      var scope = chk.dataset.scope || '';
      var action = chk.dataset.action || '';
      var genre = chk.dataset.genre || '';
      var album = chk.dataset.album || null;
      var trackIdRaw = chk.dataset.trackId;
      var trackId = trackIdRaw ? parseInt(trackIdRaw, 10) : null;
      if (Number.isNaN(trackId)) trackId = null;

      var key = scope + '|' + (album || '') + '|' + (trackId !== null ? trackId : '');
      if (!changesMap[key]) {
        changesMap[key] = {
          scope: scope,
          album: (scope === 'album' || scope === 'track') ? album : null,
          track_id: scope === 'track' ? trackId : null,
          add: [],
          remove: []
        };
      }
      if (action === 'add') {
        changesMap[key].add.push(genre);
      } else if (action === 'remove') {
        changesMap[key].remove.push(genre);
      }
    });

    return Object.keys(changesMap).map(function (k) { return changesMap[k]; });
  }

  function resetTicks(checked) {
    checked.forEach(function (chk) {
      chk.checked = false;
      var wrap = chk.parentElement;
      var badge = wrap ? wrap.querySelector('[data-badge]') : null;
      if (badge) badge.classList.remove('marked-remove', 'marked-add');
    });
  }

  function saveChanges() {
    var checked = Array.prototype.slice.call(document.querySelectorAll('.genre-chk:checked'));
    if (!checked.length) {
      showStatus('No changes selected.', false);
      return;
    }

    setSpinner(true);

    api().postJson('/api/artist/genre-management/save', {
      artist: ARTIST_NAME,
      changes: collectChanges(checked)
    }).then(function (data) {
      setSpinner(false);
      if (!data || !data.success) {
        showStatus('Error: ' + ((data && data.error) || 'Unknown error'), true);
        return;
      }
      var failed = data.failed || 0;
      showStatus(
        'Saved! Updated ' + (data.updated || 0) + ' track(s).' +
          (failed ? ' ' + failed + ' failed.' : ''),
        failed > 0
      );
      resetTicks(checked);
    }).catch(function (err) {
      setSpinner(false);
      showStatus('Network error: ' + (err && err.message ? err.message : err), true);
    });
  }

  function init() {
    /* Bind by id rather than inline onclick, and via addEventListener so the two
       buttons share one handler. */
    POSITIONS.forEach(function (pos) {
      var btn = document.getElementById('save-btn-' + pos);
      if (btn) btn.addEventListener('click', saveChanges);
    });

    /* One delegated listener for every badge on the page (there can be
       thousands of tracks, so per-element binding would be wasteful). */
    document.addEventListener('change', onChange);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  global.artistGenres = { saveChanges: saveChanges };
})(window);
