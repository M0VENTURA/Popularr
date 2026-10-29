/* ==========================================================================
   test_site/static/js/pages/artist-detail-extras.js

   The artist-page handlers that `pages/artist.js` does NOT provide.

   ── WHY THIS FILE EXISTS ─────────────────────────────────────────────────
   `pages/artist.js` owns most of the artist page (bio, similar artists,
   favourite, covered-by, download search, External-IDs modal, track edit) and
   is left alone here. But the page template calls EIGHT more handlers that no
   file in the rebuilt tree defines, so every one of them threw

       ReferenceError: <name> is not defined

   the moment its button was clicked — an artist image that cannot be changed,
   a country that cannot be looked up, a "Needs Correcting" flag that never
   loads, and a "Play Top Tracks" button that does nothing.

   Verified missing (grepped the whole rebuilt JS tree):
       openArtistImageModal   setArtistImage
       fetchArtistCountry     editArtistCountry
       forceArtistMetadataRefresh
       toggleArtistBio        goToArtistAbout
       checkMissingReleases   playArtistTopTracks

   ── ENDPOINT CONTRACTS (verified against routes/) ────────────────────────
     POST /api/artist/search-images   {name, source}         -> {images:[…]}
     POST /api/artist/set-image       {artist, image_url}
     POST /api/artist/country         {artist}               -> {country}
     POST /api/artist/country/update  {artist, country}
     GET  /api/artist/missing-releases?artist=
   `misc_api` carries /api/artist/country*; the others are on the `artist`
   blueprint.

   ── STYLE ────────────────────────────────────────────────────────────────
   Uses the rebuild's own primitives (global.api, global.toast, global.ui.modal,
   global.escapeHtml) with feature-detection, matching pages/artist.js.
   ========================================================================== */

(function (global) {
  'use strict';

  var doc = global.document;

  function esc(value) {
    return (typeof global.escapeHtml === 'function')
      ? global.escapeHtml(String(value == null ? '' : value))
      : String(value == null ? '' : value);
  }

  function notifySuccess(message) {
    if (global.toast && typeof global.toast.success === 'function') global.toast.success(message);
    else if (typeof global.showToast === 'function') global.showToast('', message, 'success');
  }

  function notifyError(message) {
    if (global.toast && typeof global.toast.error === 'function') global.toast.error(message);
    else if (typeof global.showToast === 'function') global.showToast('', message, 'error');
  }

  function artistName() {
    var el = doc.querySelector('[data-artist-name]');
    return el ? (el.getAttribute('data-artist-name') || '') : '';
  }

  /** POST JSON through the shared api helper when present, else fetch. */
  function postJson(url, body) {
    if (global.api && typeof global.api.postJson === 'function') {
      return global.api.postJson(url, body || {});
    }
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json(); });
  }

  function getJson(url) {
    if (global.api && typeof global.api.getJson === 'function') return global.api.getJson(url);
    return fetch(url, { headers: { Accept: 'application/json' } }).then(function (r) { return r.json(); });
  }

  /** Show a modal by id, preferring the rebuild's ui.modal wrapper. */
  function showModal(id) {
    if (global.ui && global.ui.modal && typeof global.ui.modal.show === 'function') {
      try { global.ui.modal.show(id); return true; } catch (e) { /* fall through */ }
    }
    var el = doc.getElementById(id);
    if (!el || !global.bootstrap || !global.bootstrap.Modal) return false;
    (global.bootstrap.Modal.getInstance(el) || new global.bootstrap.Modal(el)).show();
    return true;
  }

  function hideModal(id) {
    if (global.ui && global.ui.modal && typeof global.ui.modal.hide === 'function') {
      try { global.ui.modal.hide(id); return; } catch (e) { /* fall through */ }
    }
    var el = doc.getElementById(id);
    if (!el || !global.bootstrap || !global.bootstrap.Modal) return;
    var instance = global.bootstrap.Modal.getInstance(el);
    if (instance) instance.hide();
  }

  // ── Play top tracks ─────────────────────────────────────────────────────

  /** Reads window._artistPlaylist, emitted by the page template. */
  function playArtistTopTracks() {
    var tracks = global._artistPlaylist || [];
    if (!tracks.length) {
      notifyError('No ranked tracks for this artist yet.');
      return;
    }
    if (global.Player && typeof global.Player.playQueue === 'function') {
      global.Player.playQueue(tracks);
      return;
    }
    notifyError('The audio player has not loaded.');
  }

  // ── Artist image ────────────────────────────────────────────────────────

  function openArtistImageModal(artist) {
    var name = artist || artistName();
    if (!showModal('artistImageModal')) return;

    var container = doc.getElementById('artistImageResults');
    if (container) {
      container.innerHTML = '<div class="text-center py-3"><span class="spinner-border spinner-border-sm"></span></div>';
    }

    postJson('/api/artist/search-images', { name: name, source: '' })
      .then(function (data) {
        if (!container) return;
        var images = (data && (data.images || data.results)) || [];
        var urls = images
          .map(function (img) { return (typeof img === 'string') ? img : (img.url || img.image_url || ''); })
          .filter(Boolean);

        if (!urls.length) {
          container.innerHTML = '<div class="text-muted small">No alternative images found. Paste a URL below.</div>';
          return;
        }
        var html = '<div class="row g-2">';
        urls.forEach(function (url) {
          html += '<div class="col-4 col-md-3">'
            + '<button type="button" class="btn p-0 border-0 w-100" data-action="set-artist-image"'
            + ' data-url="' + esc(url) + '" title="Use this image">'
            + '<img src="' + esc(url) + '" alt="" class="img-fluid rounded" style="aspect-ratio:1; object-fit:cover;">'
            + '</button></div>';
        });
        container.innerHTML = html + '</div>';
      })
      .catch(function () {
        if (container) container.innerHTML = '<div class="text-danger small">Image search failed. Paste a URL below.</div>';
      });
  }

  function setArtistImage(url) {
    if (!url) { notifyError('No image URL supplied.'); return; }
    postJson('/api/artist/set-image', { artist: artistName(), image_url: url })
      .then(function (data) {
        if (data && data.success !== false) {
          notifySuccess('Artist image updated.');
          hideModal('artistImageModal');
          var img = doc.getElementById('artistImage');
          if (img) img.src = url;
        } else {
          notifyError((data && (data.error || data.message)) || 'Could not save the image.');
        }
      })
      .catch(function () { notifyError('Could not save the image.'); });
  }

  // ── Country ─────────────────────────────────────────────────────────────

  function fetchArtistCountry() {
    var btn = doc.getElementById('fetchArtistCountryBtn');
    if (btn) btn.disabled = true;
    postJson('/api/artist/country', { artist: artistName() })
      .then(function (data) {
        if (btn) btn.disabled = false;
        var country = data && data.country;
        if (country) {
          var display = doc.getElementById('artistCountryDisplay');
          if (display) display.innerHTML = '<span class="badge bg-info">' + esc(country) + '</span>';
          notifySuccess('Country set to ' + country + '.');
        } else {
          notifyError('MusicBrainz has no country recorded for this artist.');
        }
      })
      .catch(function () {
        if (btn) btn.disabled = false;
        notifyError('Country lookup failed.');
      });
  }

  function editArtistCountry() {
    var current = '';
    var el = doc.getElementById('artistCountryDisplay');
    if (el) current = (el.textContent || '').trim();
    var value = global.prompt('Artist country (name or ISO code):', current);
    if (value === null) return;
    var trimmed = String(value).trim();
    if (!trimmed) return;

    postJson('/api/artist/country/update', { artist: artistName(), country: trimmed })
      .then(function (data) {
        if (data && data.success !== false) {
          var display = doc.getElementById('artistCountryDisplay');
          if (display) display.innerHTML = '<span class="badge bg-info">' + esc(trimmed) + '</span>';
          notifySuccess('Country updated.');
        } else {
          notifyError((data && (data.error || data.message)) || 'Update failed.');
        }
      })
      .catch(function () { notifyError('Update failed.'); });
  }

  // ── Metadata refresh ────────────────────────────────────────────────────

  /**
   * Open the shared scan sheet for THIS artist.
   *
   * The hero's Scan button, and the workbook behind it, is the same sheet the
   * dashboard uses with `scope: 'library'` — one surface for one question.
   * In the artist scope the Run dispatches a FORM to `/scan/artist-custom`,
   * so every type runs against this artist rather than the whole library.
   */
  function openArtistScanSheet(opts) {
    var config = opts || {};
    if (!global.ScanModal || typeof global.ScanModal.open !== 'function') {
      notifyError('The scan sheet is not available on this page.');
      return;
    }
    global.ScanModal.open({
      scope: 'artist',
      artist: artistName(),
      preselect: config.preselect || '',
      force: !!config.force,
    });
  }

  /**
   * Re-run the artist pipeline in metadata mode.
   *
   * ⚠️ This used to find `#artistScanForm` and submit it. That form is GONE:
   * the inline `scan-toolbar` it lived on was replaced by the action row (Play /
   * Scan /⋮), because the sheet answers the same question better. It now opens
   * the sheet with Metadata preselected and Force ticked, which is the same job.
   *
   * The pre-flight guard is still made — the sheet runs it before every start.
   * The `confirmIfRunning` call that used to live here is gone rather than
   * duplicated: two prompts for one action is the failure mode the gate's own
   * docstring warns about.
   */
  function forceArtistMetadataRefresh() {
    openArtistScanSheet({ preselect: 'metadata', force: true });
  }

  /**
   * Run the deep cover-song detector across every track of this artist
   * (⋮ → Run Cover Detection).
   *
   * The pass makes several MusicBrainz round trips per track, so the server
   * runs it in the background and this only reports that it started. Verdicts
   * and file tags are written by the detector when it finishes.
   */
  function runArtistCoverDetection() {
    var artist = artistName();
    if (!artist) {
      notifyError('Artist context is unavailable.');
      return;
    }
    postJson('/api/artist/cover-detection', { artist: artist })
      .then(function (data) {
        if (data && data.success) {
          notifySuccess(data.message || 'Cover detection started.');
        } else {
          notifyError((data && data.error) || 'Could not start cover detection.');
        }
      })
      .catch(function (err) {
        notifyError((err && (err.error || err.message)) || 'Could not start cover detection.');
      });
  }

  // ── Playlist exclusion ──────────────────────────────────────────────────
  /*
    "Exclude This Artist from Playlists" (⋮ → Show More).

    Writes ``playlists.excluded_artists`` through the shared artist API, which
    both generated-playlist builders read: the genre top-tracks builder filters
    its rows by it, and the Essential Collection builder treats it as an
    exclusion (alongside its own placeholder list for "Various Artists" etc.).

    ⚠️ THE LABEL IS SET FROM THE SERVER'S ANSWER, NEVER FROM THE CLICK.
    Optimistically flipping the label would show "Exclude" even when the config
    write failed, and the next scan would generate the very playlist the user
    had just excluded. The endpoint re-reads config.yaml from disk on every
    call, so its response is the only trustworthy statement of stored state.
  */
  function applyPlaylistExclusionState(excluded) {
    var item = doc.getElementById('artistPlaylistExclusionItem');
    if (item) item.setAttribute('data-excluded', excluded ? 'true' : 'false');

    var icon = doc.getElementById('artistPlaylistExclusionIcon');
    if (icon) {
      icon.className = excluded
        ? 'bi bi-music-note-beamed me-2'
        : 'bi bi-slash-circle me-2';
    }
    var label = doc.getElementById('artistPlaylistExclusionLabel');
    if (label) {
      label.textContent = excluded
        ? 'Include This Artist in Playlists'
        : 'Exclude This Artist from Playlists';
    }
  }

  function setArtistPlaylistExclusion(excluded) {
    var artist = artistName();
    if (!artist) {
      notifyError('Could not determine the artist name');
      return Promise.resolve();
    }
    return postJson('/api/artist/playlist-exclusion', {
      artist: artist,
      excluded: !!excluded,
    })
      .then(function (data) {
        if (!data || !data.success) {
          notifyError((data && data.error) || 'Could not update playlist exclusion');
          return;
        }
        // Re-derive from the returned list rather than trusting `excluded`:
        // the server de-duplicates case-insensitively, so its list is truth.
        var on = (data.artists || []).some(function (a) {
          return String(a).toLowerCase() === artist.toLowerCase();
        });
        applyPlaylistExclusionState(on);
        notifySuccess(
          on
            ? '"' + artist + '" will be skipped by playlist generation'
            : '"' + artist + '" can be included in playlists again'
        );
      })
      .catch(function (error) {
        notifyError('Could not update playlist exclusion: ' + (error && error.message ? error.message : error));
      });
  }

  /** Called by the ⋮ item: flip whatever the item currently says. */
  function toggleArtistPlaylistExclusion() {
    var item = doc.getElementById('artistPlaylistExclusionItem');
    var currently = item ? item.getAttribute('data-excluded') === 'true' : false;
    return setArtistPlaylistExclusion(!currently);
  }

  function loadArtistPlaylistExclusion() {
    var item = doc.getElementById('artistPlaylistExclusionItem');
    if (!item) return Promise.resolve();
    var artist = artistName();
    if (!artist) return Promise.resolve();
    return getJson('/api/artist/playlist-exclusion?artist=' + encodeURIComponent(artist))
      .then(function (data) {
        if (data && data.success) applyPlaylistExclusionState(!!data.excluded);
      })
      .catch(function () {
        // Leave the default label. The toggle still works and re-reads state
        // afterwards, so a failed initial load is self-correcting.
      });
  }

  // ── Bio clamp ───────────────────────────────────────────────────────────

  function toggleArtistBio() {
    var clamp = doc.getElementById('artistBioClamp');
    var toggle = doc.getElementById('artistBioToggle');
    if (!clamp) return;
    var expanded = clamp.classList.toggle('expanded');
    if (toggle) {
      toggle.innerHTML = expanded
        ? '<i class="bi bi-chevron-up me-1"></i>Read Less'
        : '<i class="bi bi-chevron-down me-1"></i>Read More';
    }
  }

  /** Hero "Read more" link: open the About sheet. */
  function goToArtistAbout() {
    openArtistAboutModal();
  }

  // ── About sheet ─────────────────────────────────────────────────────────

  /*
    The page's third TAB is gone; its contents live on this sheet, reached from
    "Read more" under the hero biography. Everything on it is served by ids this
    module already drove (`artistBioClamp`, `artistCountryDisplay`,
    `musicbrainzArtistId`, …), so moving the markup changed nothing about how it
    works — a rename would have.
  */
  function openArtistAboutModal() {
    if (!showModal('artistAboutModal')) return;
    loadArtistPendingUpdates();
  }

  /*
    Per-album counts of updates the scan STASHED instead of applying — the
    "Recommend only" metadata mode. Cached after the first fetch per page load:
    the sheet can be opened repeatedly and the answer only changes when a scan
    writes, so a second request would be pure latency.
  */
  var _pendingLoaded = false;

  function loadArtistPendingUpdates() {
    if (_pendingLoaded) return;
    var panel = doc.getElementById('artistPendingUpdates');
    var body = doc.getElementById('artistPendingUpdatesBody');
    if (!panel || !body) return;
    var artist = artistName();
    if (!artist) return;
    _pendingLoaded = true;

    getJson('/api/artist/metadata-recommendations?artist=' + encodeURIComponent(artist))
      .then(function (data) {
        var albums = (data && data.albums) || [];
        if (!albums.length) return;   // stays hidden — nothing pending

        var html = '<div class="d-flex align-items-center gap-2 mb-2">'
          + '<span class="badge bg-warning text-dark">' + esc(String((data && data.total) || 0)) + ' updates</span>'
          + '<span class="text-muted small">across ' + esc(String(albums.length))
          + ' album' + (albums.length === 1 ? '' : 's') + '</span></div>';
        html += '<div class="list-group">';
        albums.forEach(function (row) {
          var album = row.album || '';
          var count = row.track_count || 0;
          html += '<a class="list-group-item list-group-item-action bg-transparent border-secondary text-light d-flex justify-content-between align-items-center gap-2"'
            + ' href="/album/' + encodeURIComponent(artist) + '/' + encodeURIComponent(album) + '">'
            + '<span class="text-truncate">' + esc(album) + '</span>'
            + '<span class="badge bg-secondary flex-shrink-0">' + esc(String(count)) + ' track' + (count === 1 ? '' : 's') + '</span>'
            + '</a>';
        });
        body.innerHTML = html + '</div>';
        panel.hidden = false;
      })
      .catch(function () {
        // Best-effort: the sheet is still useful without this panel, and an
        // error toast for a side panel would be noise.
        _pendingLoaded = false;
      });
  }

  // ── Missing releases ────────────────────────────────────────────────────

  /**
   * Ask the backend to enumerate releases MusicBrainz knows but the library
   * lacks, then re-run the release sections so the new rows appear.
   *
   * `background=1` because the scan can take a while; the page does not block
   * on it, and the reply reports whether the work was queued.
   */
  function checkMissingReleases(artist) {
    var name = artist || artistName();
    if (!name) return;

    var btns = doc.querySelectorAll('.release-check-missing-btn');
    btns.forEach(function (b) { b.disabled = true; });

    getJson('/api/artist/missing-releases?artist=' + encodeURIComponent(name) + '&background=1')
      .then(function (data) {
        btns.forEach(function (b) { b.disabled = false; });
        var found = data && (data.count != null ? data.count : (data.added != null ? data.added : null));
        if (data && data.error) {
          notifyError(data.error);
        } else if (found) {
          notifySuccess(found + ' missing release(s) found — reloading…');
          global.setTimeout(function () { global.location.reload(); }, 800);
        } else {
          notifySuccess('No new missing releases found.');
        }
      })
      .catch(function () {
        btns.forEach(function (b) { b.disabled = false; });
        notifyError('Missing-release check failed.');
      });
  }

  // ── Alternate names ───────────────────────────────────────────────────────

  /** Re-render the group list from an API reply ({aliases: [...]}). */
  function renderArtistAliases(group) {
    var list = doc.getElementById('artistAliasesList');
    if (!list) return;
    var aliases = (group && group.aliases) || [];
    if (!aliases.length) {
      list.innerHTML = '<span class="text-muted small" id="artistAliasesEmpty">No alternate names.</span>';
      return;
    }
    var html = '';
    aliases.forEach(function (alias) {
      html += '<span class="badge bg-secondary artist-alias-badge d-inline-flex align-items-center gap-1">'
        + esc(alias)
        + '<button type="button" class="btn btn-link p-0 lh-1 text-decoration-none text-white-50"'
        + ' title="Remove this name" aria-label="Remove ' + esc(alias) + '"'
        + ' data-action="remove-artist-alias" data-alias="' + esc(alias) + '">&times;</button>'
        + '</span>';
    });
    list.innerHTML = html;
  }

  /**
   * Add the input's name to this artist's group.
   *
   * Errors come back IN the JSON body (409 for a name already owned by
   * another artist), so they are surfaced from the reply, not swallowed by a
   * generic catch — the user needs to know the name belongs elsewhere.
   */
  function addArtistAlias() {
    var input = doc.getElementById('artistAliasInput');
    var value = input ? String(input.value || '').trim() : '';
    if (!value) { notifyError('Enter an alternate name first.'); return; }

    postJson('/api/artist/aliases', { artist: artistName(), alias: value })
      .then(function (data) {
        if (data && data.success) {
          if (input) input.value = '';
          renderArtistAliases(data);
          notifySuccess(data.already
            ? 'That name is already linked.'
            : 'Alternate name added.');
        } else {
          notifyError((data && (data.error || data.message)) || 'Could not add the name.');
        }
      })
      .catch(function (err) {
        notifyError((err && (err.error || err.message)) || 'Could not add the name.');
      });
  }

  /** Unlink one name; the reply carries the refreshed group. */
  function removeArtistAlias(alias) {
    if (!alias) return;

    postJson('/api/artist/aliases/remove', { artist: artistName(), alias: alias })
      .then(function (data) {
        if (data && data.success) {
          renderArtistAliases(data);
          notifySuccess('Alternate name removed.');
        } else {
          notifyError((data && (data.error || data.message)) || 'Could not remove the name.');
        }
      })
      .catch(function (err) {
        notifyError((err && (err.error || err.message)) || 'Could not remove the name.');
      });
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function init() {
    if (!doc.querySelector('.artist-page')) return;

    // The image grid is re-rendered after every search, so one delegated
    // handler covers every tile.
    doc.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-action="set-artist-image"]');
      if (!btn) return;
      setArtistImage(btn.getAttribute('data-url') || '');
    });

    // Alias badges (server-rendered AND re-rendered) carry data-alias rather
    // than an inline onclick: names can contain quotes, so the value is read
    // back HTML-escaped from the attribute instead of embedded in JS.
    doc.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-action="remove-artist-alias"]');
      if (!btn) return;
      removeArtistAlias(btn.getAttribute('data-alias') || '');
    });

    // ── The release row's ⋮ menu needs NO forwarding ────────────────
    //
    // This file used to forward a ⋮ click to an INLINE twin, because the menu
    // was a phone-only addition and the shared module bound the inline copies.
    // Both halves of that reason are gone: the ⋮ is now the row's design at
    // every width, and each action class appears exactly ONCE (in the menu), so
    // `artist-releases.js` binds the menu item itself. The forwarder was left
    // here as a no-op for a while — its `.release-inline-only` lookup could
    // never match, so it always returned early. Removed rather than kept: a
    // handler that silently does nothing reads as working wiring, and if a row
    // ever rendered an inline twin again it would double-fire the action.

    // Enter submits the alternate-name field — the button's onclick does not
    // fire on a keypress, and a form wrapper here would submit the page.
    var aliasInput = doc.getElementById('artistAliasInput');
    if (aliasInput) {
      aliasInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          addArtistAlias();
        }
      });
    }

    // The region banner is hidden until the API says otherwise; asking
    // unconditionally would flag every artist whose scan found nothing.
    var artist = artistName();
    if (artist) {
      loadArtistPlaylistExclusion();
      getJson('/api/artist/missing-releases?artist=' + encodeURIComponent(artist))
        .then(function (data) {
          var count = data && (data.count || data.total);
          if (!count) return;
          var banner = doc.getElementById('artist-corrections-banner');
          var badge = doc.getElementById('artist-missing-tracks-badge');
          if (banner) banner.style.display = '';
          if (badge) { badge.textContent = count + ' release(s) missing'; badge.style.display = ''; }
        })
        .catch(function () { /* the banner is advisory only */ });
    }
  }

  if (doc.readyState === 'loading') {
    doc.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Inline onclick attributes resolve by GLOBAL name.
  global.playArtistTopTracks = playArtistTopTracks;
  global.openArtistImageModal = openArtistImageModal;
  global.setArtistImage = setArtistImage;
  global.fetchArtistCountry = fetchArtistCountry;
  global.editArtistCountry = editArtistCountry;
  global.forceArtistMetadataRefresh = forceArtistMetadataRefresh;
  global.runArtistCoverDetection = runArtistCoverDetection;
  global.toggleArtistPlaylistExclusion = toggleArtistPlaylistExclusion;
  global.setArtistPlaylistExclusion = setArtistPlaylistExclusion;
  global.loadArtistPlaylistExclusion = loadArtistPlaylistExclusion;
  global.toggleArtistBio = toggleArtistBio;
  global.goToArtistAbout = goToArtistAbout;
  global.openArtistAboutModal = openArtistAboutModal;
  global.openArtistScanSheet = openArtistScanSheet;
  global.checkMissingReleases = checkMissingReleases;
  global.addArtistAlias = addArtistAlias;

  global.artistPage = global.artistPage || {};
  global.artistPage.playTopTracks = playArtistTopTracks;
  global.artistPage.checkMissingReleases = checkMissingReleases;
})(window);
