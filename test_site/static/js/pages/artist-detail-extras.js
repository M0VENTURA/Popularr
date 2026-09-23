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
   * Re-run the artist pipeline in metadata mode.
   *
   * No dedicated endpoint exists for "refresh this artist", so this submits the
   * scan form the page already renders — one code path for scans, not two.
   */
  function forceArtistMetadataRefresh() {
    var form = doc.getElementById('artistScanForm');
    if (!form) { notifyError('Scan form unavailable on this page.'); return; }
    var select = form.querySelector('select[name="scan_type"]');
    if (select) select.value = 'metadata';

    // `form.submit()` does NOT fire submit listeners, so the declarative
    // `data-scan-preflight` guard on this form cannot see this path — ask here
    // as well. (No "cleared" flag: nothing consumes one, and a stale flag would
    // silently skip the gate later.)
    if (global.ScanPreflight) {
      global.ScanPreflight.confirmIfRunning({ scanName: 'Metadata Scan' })
        .then(function (proceed) {
          if (!proceed) return;
          notifySuccess('Starting metadata scan…');
          form.submit();
        });
      return;
    }

    notifySuccess('Starting metadata scan…');
    form.submit();
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

  /** Hero "[more]" link: scroll to the bio and expand it. */
  function goToArtistAbout() {
    var clamp = doc.getElementById('artistBioClamp');
    var target = doc.getElementById('artist-bio-section') || clamp;
    if (target && typeof target.scrollIntoView === 'function') {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    if (clamp && !clamp.classList.contains('expanded')) toggleArtistBio();
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

    // The region banner is hidden until the API says otherwise; asking
    // unconditionally would flag every artist whose scan found nothing.
    var artist = artistName();
    if (artist) {
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
  global.toggleArtistBio = toggleArtistBio;
  global.goToArtistAbout = goToArtistAbout;
  global.checkMissingReleases = checkMissingReleases;

  global.artistPage = global.artistPage || {};
  global.artistPage.playTopTracks = playArtistTopTracks;
  global.artistPage.checkMissingReleases = checkMissingReleases;
})(window);
