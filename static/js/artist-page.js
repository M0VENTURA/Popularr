/* ==========================================================================
   static/js/artist-page.js
   Hero, biography, country, external IDs, image picker, favourite, "Play Top
   Tracks" and the covers list for templates/pages/artist_detail_v2.html.

   ── WHY THIS FILE EXISTS ─────────────────────────────────────────────────
   Every one of these functions USED TO live in `static/js/artist_detail.js`,
   which is not JavaScript. That file is a 5346-line Jinja PAGE TEMPLATE whose
   first line is `{% extends "base.html" %}`, yet the page loads it with
   `<script src>`. The browser parses the Jinja as JS, throws

       SyntaxError: Unexpected token '%'

   on line 1, and discards the ENTIRE file. So the artist image picker, the
   country lookup, the IDs editor and the bio toggle were ALL dead in the live
   tree — they only ever worked under features.use_test_site, where
   `js/pages/artist.js` (real JavaScript) is served instead.

   This module restores them as actual JavaScript in the LIVE tree. It is a
   port, not a rewrite: the endpoint contracts below are the ones the dead file
   already called and the routes already implement.

   ── ENDPOINTS (all on the `artist` blueprint, verified present) ───────────
     GET  /api/artist/bio?name=                    -> {bio}
     GET  /api/artist/singles-count?name=          -> {count}
     GET  /api/artist/<artist>/similar             -> {similar_artists:{lastfm,listenbrainz}}
     GET  /api/artist/favourite?artist=            -> {is_favourite}
     POST /api/artist/favourite   {artist}         -> {success}
     GET  /api/artist/covered-by?artist=           -> {covers:[…]}
     POST /api/artist/search-images {name, source} -> {images:[…]}
     POST /api/artist/set-image     {artist, image_url}
     POST /api/artist/update-ids    {artist, musicbrainz_artist_id, discogs_artist_id}
   And on `misc_api` (url_prefix /api):
     POST /api/artist/country          {artist}
     POST /api/artist/country/update   {artist, country}

   ── GLOBALS USED ─────────────────────────────────────────────────────────
   window.showToast and window.escapeHtml (js/main.js), window.Player
   (js/player.js), window.bootstrap (base.html) — all feature-detected.
   ========================================================================== */

(function (global) {
  'use strict';

  var doc = global.document;

  function esc(value) {
    if (typeof global.escapeHtml === 'function') return global.escapeHtml(String(value == null ? '' : value));
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function toast(title, message, type) {
    if (typeof global.showToast === 'function') global.showToast(title || '', message || '', type || 'info');
    else if (global.toast && typeof global.toast[type] === 'function') global.toast[type](message);
  }

  function artistName() {
    var el = doc.querySelector('[data-artist-name]');
    return el ? (el.getAttribute('data-artist-name') || '') : '';
  }

  function getJson(url) {
    return fetch(url, { headers: { Accept: 'application/json' } }).then(function (r) { return r.json(); });
  }

  function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json(); });
  }

  function showModal(id) {
    var el = doc.getElementById(id);
    if (!el || !global.bootstrap || !global.bootstrap.Modal) return null;
    (global.bootstrap.Modal.getInstance(el) || new global.bootstrap.Modal(el)).show();
    return el;
  }

  function hideModal(id) {
    var el = doc.getElementById(id);
    if (!el || !global.bootstrap || !global.bootstrap.Modal) return;
    var instance = global.bootstrap.Modal.getInstance(el);
    if (instance) instance.hide();
  }

  // ── Bio ─────────────────────────────────────────────────────────────────

  function loadArtistBio(artist) {
    var container = doc.getElementById('artistBio');
    if (!container) return;
    // The route renders the bio server-side when it already has one; only
    // fetch when it did not, so a populated page makes no extra request.
    if (container.getAttribute('data-has-initial-bio') === '1') return;

    getJson('/api/artist/bio?name=' + encodeURIComponent(artist))
      .then(function (data) {
        if (data && data.bio) {
          container.innerHTML = '<p>' + esc(data.bio).replace(/\n/g, '<br>') + '</p>';
        } else {
          container.innerHTML = '<p class="text-muted mb-0"><em>No biography available yet. Run a metadata scan for this artist.</em></p>';
        }
      })
      .catch(function () {
        container.innerHTML = '<p class="text-muted mb-0"><em>Could not load biography.</em></p>';
      });
  }

  function loadSinglesCount(artist) {
    var badge = doc.getElementById('singlesCount');
    if (!badge) return;
    getJson('/api/artist/singles-count?name=' + encodeURIComponent(artist))
      .then(function (data) { if (data && data.count !== undefined) badge.textContent = data.count; })
      .catch(function () { /* cosmetic */ });
  }

  // ── Play top tracks ─────────────────────────────────────────────────────

  /*
    Reads window._artistPlaylist, emitted by the page's <script> block with one
    entry per top track. Player.playQueue expects {id, title, artist,
    albumArtUrl} — the same shape static/js/player.js already documents.
  */
  function playArtistTopTracks() {
    var tracks = global._artistPlaylist || [];
    if (!tracks.length) { toast('Nothing to play', 'No ranked tracks for this artist yet.', 'warning'); return; }
    if (global.Player && typeof global.Player.playQueue === 'function') {
      global.Player.playQueue(tracks);
      return;
    }
    toast('Player unavailable', 'The audio player has not loaded.', 'error');
  }

  // ── Favourite ───────────────────────────────────────────────────────────

  function loadArtistFavouriteState(artist) {
    getJson('/api/artist/favourite?artist=' + encodeURIComponent(artist))
      .then(function (data) {
        if (!data || !data.is_favourite) return;
        var btn = doc.querySelector('[data-artist-fav-btn]');
        var icon = doc.querySelector('[data-artist-fav-icon]');
        if (btn) btn.classList.add('text-danger');
        if (icon) icon.className = 'bi bi-heart-fill fs-4';
      })
      .catch(function () { /* cosmetic */ });
  }

  function toggleArtistFavourite(artist) {
    postJson('/api/artist/favourite', { artist: artist })
      .then(function (data) {
        var icon = doc.querySelector('[data-artist-fav-icon]');
        var on = data && (data.is_favourite || data.success);
        if (icon) icon.className = on ? 'bi bi-heart-fill fs-4' : 'bi bi-heart fs-4';
        toast('Favourite', on ? 'Added to favourites.' : 'Removed from favourites.', 'success');
      })
      .catch(function () { toast('Favourite', 'Could not update favourite.', 'error'); });
  }

  // ── Artist image ────────────────────────────────────────────────────────

  function openArtistImageModal(artist) {
    var el = showModal('artistImageModal');
    if (!el) return;
    var container = doc.getElementById('artistImageResults');
    if (container) container.innerHTML = '<div class="text-center py-3"><span class="spinner-border spinner-border-sm"></span></div>';
    el.setAttribute('data-artist', artist);

    // No source = let the backend query every configured provider.
    postJson('/api/artist/search-images', { name: artist, source: '' })
      .then(function (data) {
        if (!container) return;
        var images = (data && (data.images || data.results)) || [];
        if (!images.length) {
          container.innerHTML = '<div class="text-muted small">No alternative images found.</div>';
          return;
        }
        var html = '<div class="row g-2">';
        images.forEach(function (img) {
          var url = img.url || img.image_url || '';
          if (!url) return;
          html += '<div class="col-4 col-md-3">'
            + '<button type="button" class="btn p-0 border-0 w-100" data-action="set-artist-image"'
            + ' data-url="' + esc(url) + '" title="Use this image">'
            + '<img src="' + esc(url) + '" alt="" class="img-fluid rounded" style="aspect-ratio:1; object-fit:cover;">'
            + '</button></div>';
        });
        container.innerHTML = html + '</div>';
      })
      .catch(function () {
        if (container) container.innerHTML = '<div class="text-danger small">Image search failed.</div>';
      });
  }

  function setArtistImage(url) {
    var modalEl = doc.getElementById('artistImageModal');
    var artist = (modalEl && modalEl.getAttribute('data-artist')) || artistName();
    postJson('/api/artist/set-image', { artist: artist, image_url: url })
      .then(function (data) {
        if (data && data.success !== false) {
          toast('Artist image', 'Image updated.', 'success');
          hideModal('artistImageModal');
          var img = doc.getElementById('artistImage');
          if (img) img.src = url;
        } else {
          toast('Artist image', (data && data.error) || 'Could not save image.', 'error');
        }
      })
      .catch(function () { toast('Artist image', 'Could not save image.', 'error'); });
  }

  // ── External IDs ────────────────────────────────────────────────────────

  function openEditArtistIdsModal() {
    var el = showModal('editArtistIdsModal');
    if (!el) return;
    var mb = doc.getElementById('editMusicbrainzArtistId');
    var dc = doc.getElementById('editDiscogsArtistId');
    // Seed from the read-only display fields the page always renders.
    var mbShown = doc.getElementById('musicbrainzArtistId');
    var dcShown = doc.getElementById('discogsArtistId');
    if (mb && mbShown) mb.value = (mbShown.textContent || '').trim().replace(/^Not linked$/, '');
    if (dc && dcShown) dc.value = (dcShown.textContent || '').trim().replace(/^Not linked$/, '');
  }

  function saveArtistIds() {
    var artist = artistName();
    var mb = doc.getElementById('editMusicbrainzArtistId');
    var dc = doc.getElementById('editDiscogsArtistId');
    postJson('/api/artist/update-ids', {
      artist: artist,
      musicbrainz_artist_id: mb ? mb.value.trim() : '',
      discogs_artist_id: dc ? dc.value.trim() : '',
    })
      .then(function (data) {
        if (data && data.success !== false) {
          toast('Artist IDs', 'Saved.', 'success');
          hideModal('editArtistIdsModal');
          global.setTimeout(function () { global.location.reload(); }, 400);
        } else {
          toast('Artist IDs', (data && data.error) || 'Save failed.', 'error');
        }
      })
      .catch(function () { toast('Artist IDs', 'Save failed.', 'error'); });
  }

  // ── Country ─────────────────────────────────────────────────────────────

  function fetchArtistCountry() {
    var artist = artistName();
    var btn = doc.getElementById('fetchArtistCountryBtn');
    if (btn) btn.disabled = true;
    postJson('/api/artist/country', { artist: artist })
      .then(function (data) {
        if (btn) btn.disabled = false;
        if (data && data.country) {
          var display = doc.getElementById('artistCountryDisplay');
          if (display) display.innerHTML = '<span class="badge bg-info">' + esc(data.country) + '</span>';
          toast('Country', 'Set to ' + data.country + '.', 'success');
        } else {
          toast('Country', 'MusicBrainz has no country for this artist.', 'warning');
        }
      })
      .catch(function () {
        if (btn) btn.disabled = false;
        toast('Country', 'Lookup failed.', 'error');
      });
  }

  function editArtistCountry() {
    var current = '';
    var el = doc.getElementById('artistCountryDisplay');
    if (el) current = (el.textContent || '').trim();
    var value = global.prompt('Artist country (ISO code or name):', current);
    if (value === null) return;
    var trimmed = String(value).trim();
    if (!trimmed) return;
    postJson('/api/artist/country/update', { artist: artistName(), country: trimmed })
      .then(function (data) {
        if (data && data.success !== false) {
          var display = doc.getElementById('artistCountryDisplay');
          if (display) display.innerHTML = '<span class="badge bg-info">' + esc(trimmed) + '</span>';
          toast('Country', 'Updated.', 'success');
        } else {
          toast('Country', (data && data.error) || 'Update failed.', 'error');
        }
      })
      .catch(function () { toast('Country', 'Update failed.', 'error'); });
  }

  // ── Metadata refresh ────────────────────────────────────────────────────

  /*
    Re-runs the artist pipeline in metadata mode. There is no dedicated
    endpoint for "refresh everything", so this posts the scan form the page
    already renders — one code path for scans instead of two.
  */
  function forceArtistMetadataRefresh() {
    var form = doc.getElementById('artistScanForm');
    if (!form) { toast('Refresh', 'Scan form unavailable.', 'error'); return; }
    var select = form.querySelector('select[name="scan_type"]');
    if (select) select.value = 'metadata';

    // The form carries `data-scan-preflight`, and the gate's own submit
    // listener intercepts both this programmatic path and a plain click on Run
    // — but form.submit() bypasses listeners, so ask here too when a scan is
    // already running. Everything else goes through the listener.
    // (No "cleared" flag: nothing consumes one, and a stale flag would
    // silently skip the gate later.)
    if (window.ScanPreflight) {
      window.ScanPreflight.confirmIfRunning({ scanName: 'Metadata Scan' }).then(function (proceed) {
        if (!proceed) return;
        toast('Metadata refresh', 'Starting metadata scan…', 'info');
        form.submit();
      });
      return;
    }

    toast('Metadata refresh', 'Starting metadata scan…', 'info');
    form.submit();
  }

  // ── Covers and similar artists ──────────────────────────────────────────

  function loadArtistCoveredBy(artist) {
    var container = doc.getElementById('artistCoveredByContainer');
    if (!container) return;
    getJson('/api/artist/covered-by?artist=' + encodeURIComponent(artist))
      .then(function (data) {
        var covers = (data && data.covers) || [];
        var countBadge = doc.getElementById('artistCoveredByCount');
        if (countBadge) countBadge.textContent = covers.length;
        if (!covers.length) {
          container.innerHTML = '<p class="p-3 mb-0 text-muted small">No covers of this artist\'s songs in the library.</p>';
          return;
        }
        var html = '<div class="list-group list-group-flush">';
        covers.forEach(function (cover) {
          var title = cover.title || cover.track_title || '';
          var by = cover.artist || cover.album_artist || '';
          html += '<div class="list-group-item bg-transparent border-secondary d-flex justify-content-between align-items-center">'
            + '<span class="text-truncate">' + esc(title) + '</span>'
            + '<span class="text-muted small flex-shrink-0 ms-2">' + esc(by) + '</span>'
            + '</div>';
        });
        container.innerHTML = html + '</div>';
      })
      .catch(function () {
        container.innerHTML = '<p class="p-3 mb-0 text-muted small">Could not load covers.</p>';
      });
  }

  function loadSimilarArtists(artist) {
    var container = doc.getElementById('artistSimilarArtistsContainer');
    if (!container) return;
    getJson('/api/artist/' + encodeURIComponent(artist) + '/similar')
      .then(function (data) {
        var similar = (data && data.similar_artists) || null;
        var list = (similar && (similar.display || similar.lastfm || similar.listenbrainz)) || [];
        if (!list.length) {
          container.innerHTML = '<div class="text-muted small">No similar artists available yet.'
            + '<br><small>Similar artists are collected during popularity scans from Last.fm and ListenBrainz.</small></div>';
          return;
        }
        var html = '<div class="d-flex flex-wrap gap-2">';
        list.forEach(function (entry) {
          var name = entry.name || '';
          if (!name) return;
          var cls = entry.in_collection ? 'bg-success' : 'bg-secondary';
          html += '<a href="/artist/' + encodeURIComponent(name) + '" class="badge ' + cls + ' artist-similar-card text-decoration-none">' + esc(name) + '</a>';
        });
        container.innerHTML = html + '</div>';
      })
      .catch(function () {
        container.innerHTML = '<div class="text-muted small">Could not load similar artists.</div>';
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

  /** Hero bio "[more]" link: reveal the clamped biography. */
  function goToArtistAbout() {
    var clamp = doc.getElementById('artistBioClamp');
    var target = doc.getElementById('artist-bio-section') || clamp;
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (clamp && !clamp.classList.contains('expanded')) toggleArtistBio();
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function init() {
    if (!doc.querySelector('.artist-page')) return;
    var artist = artistName();
    if (!artist) return;

    loadArtistBio(artist);
    loadSinglesCount(artist);
    loadSimilarArtists(artist);
    loadArtistFavouriteState(artist);
    loadArtistCoveredBy(artist);

    // Delegated: the image grid is re-rendered after every search.
    doc.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-action="set-artist-image"]');
      if (!btn) return;
      setArtistImage(btn.getAttribute('data-url') || '');
    });

    var saveIds = doc.getElementById('editArtistIdsSaveBtn');
    if (saveIds) saveIds.addEventListener('click', saveArtistIds);
  }

  if (doc.readyState === 'loading') {
    doc.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Inline onclick attributes resolve by GLOBAL name, so publish explicitly.
  global.openDownloadSearch = global.openDownloadSearch || function (artist, album) {
    // The download modal lives in the artist modals partial and is only
    // rendered when a downloader is configured; fall back to the dedicated
    // Soulseek search page, which needs no modal.
    var modal = doc.getElementById('downloadModal');
    if (modal) {
      var titleEl = doc.getElementById('downloadArtistName');
      if (titleEl) titleEl.textContent = album ? artist + ' - ' + album : artist;
      var q = doc.getElementById('slskdSearchInput');
      if (q) q.value = album ? artist + ' ' + album : artist;
      (global.bootstrap.Modal.getInstance(modal) || new global.bootstrap.Modal(modal)).show();
      return;
    }
    global.location.href = '/downloads/search/soulseek?artist=' + encodeURIComponent(artist || '')
      + (album ? '&album=' + encodeURIComponent(album) : '');
  };

  global.toggleArtistFavourite = toggleArtistFavourite;
  global.playArtistTopTracks = playArtistTopTracks;
  global.openArtistImageModal = openArtistImageModal;
  global.openEditArtistIdsModal = openEditArtistIdsModal;
  global.saveArtistIds = saveArtistIds;
  global.fetchArtistCountry = fetchArtistCountry;
  global.editArtistCountry = editArtistCountry;
  global.forceArtistMetadataRefresh = forceArtistMetadataRefresh;
  global.toggleArtistBio = toggleArtistBio;
  global.goToArtistAbout = goToArtistAbout;
  global.loadArtistCoveredBy = loadArtistCoveredBy;

  global.artistPage = global.artistPage || {};
  global.artistPage.playTopTracks = playArtistTopTracks;
  global.artistPage.loadCoveredBy = loadArtistCoveredBy;
})(window);
