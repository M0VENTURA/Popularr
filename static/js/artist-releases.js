/* ==========================================================================
   static/js/artist-releases.js
   Release sections for the artist page (templates/pages/artist_detail_v2.html).

   ── WHAT THIS FILE IS FOR ────────────────────────────────────────────────
   Renders and wires the per-category release lists produced by
   templates/components/_release_section.html:

     * assigns each row a stable tracklist id (derived from artist + title)
     * expands / collapses the tracklist, fetched on first open
     * the per-section All / Library / Missing radio filter
     * the row action buttons (open album, edit, import, MB search, toggle)

   ── WHY IT EXISTS SEPARATELY ─────────────────────────────────────────────
   It was previously part of `static/js/artist_detail.js`, which is NOT
   JavaScript. That file is a 5346-line Jinja PAGE TEMPLATE whose first line is
   `{% extends "base.html" %}`, yet the page loads it with `<script src>`. The
   browser parses the Jinja as JS, throws

       SyntaxError: Unexpected token '%'

   on line 1, and discards the ENTIRE file. Every function it defined silently
   ceased to exist — which is the reported "the filter between In Library and
   Missing on the artist page still doesn't work".

   A SyntaxError in one `<script src>` does not stop LATER scripts from running,
   so this module is independent of that larger repair. See
   tests/test_static_js_is_not_jinja.py, which guards the whole class.

   ── MARKUP CONTRACT (templates/components/_release_section.html) ──────────
     .release-section[data-category]            the category card
       .release-section-toggle                  collapse the whole section
       .release-filter-radio[value=all|library|missing]
       .release-list[data-category]
         .release-item[data-status=library|missing][data-title]
           .release-summary[data-artist][data-album][data-mbid]
             .release-art / .release-art-placeholder
             .release-title
             .album-missing-tracks-badge          filled in here
             .release-actions
               .release-open-btn / .release-edit-btn
               .release-import-btn / .release-mb-search-btn
               .release-toggle-btn
           .release-tracklist > .release-tracklist-content

   ── GLOBALS ──────────────────────────────────────────────────────────────
   This file is served UNCHANGED by both UI trees (static/ and
   test_site/static/), so it must work under either set of globals. Both trees
   expose escapeHtml / showToast / toast / Player / bootstrap, and the rebuilt
   tree additionally provides `api` (js/utils/api.js). Every access below is
   feature-detected and prefers the richer helper when present, so one file
   covers both trees rather than two copies that drift.
   ========================================================================== */

(function (global) {
  'use strict';

  var doc = global.document;

  // ── Helpers ─────────────────────────────────────────────────────────────

  function esc(value) {
    if (typeof global.escapeHtml === 'function') return global.escapeHtml(String(value == null ? '' : value));
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /** Same id-mangling the server uses elsewhere, so ids stay URL/DOM safe. */
  function safeForDomId(value) {
    return String(value == null ? '' : value).replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  }

  function toastSuccess(message) {
    if (global.toast && typeof global.toast.success === 'function') global.toast.success(message);
    else if (typeof global.showToast === 'function') global.showToast('', message, 'success');
  }

  function toastError(message) {
    if (global.toast && typeof global.toast.error === 'function') global.toast.error(message);
    else if (typeof global.showToast === 'function') global.showToast('', message, 'error');
  }

  function artistName() {
    var el = doc.querySelector('[data-artist-name]');
    return el ? (el.getAttribute('data-artist-name') || '') : '';
  }

  /** Prefers the rebuilt tree's api helper; falls back to raw fetch. */
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

  function showModal(id) {
    // The rebuilt tree wraps Bootstrap's modal in `ui.modal`; the live tree
    // does not. Prefer the wrapper when it exists, else use Bootstrap directly.
    if (global.ui && global.ui.modal && typeof global.ui.modal.show === 'function') {
      var wrapped = doc.getElementById(id);
      if (wrapped) {
        try { global.ui.modal.show(id); return wrapped; } catch (e) { /* fall through */ }
      }
    }
    var el = doc.getElementById(id);
    if (!el) {
      toastError('This dialog is not available on this page.');
      return null;
    }
    if (!global.bootstrap || !global.bootstrap.Modal) return null;
    var instance = global.bootstrap.Modal.getInstance(el) || new global.bootstrap.Modal(el);
    instance.show();
    return el;
  }

  function hideModal(el) {
    if (!el) return;
    if (global.ui && global.ui.modal && typeof global.ui.modal.hide === 'function') {
      try { global.ui.modal.hide(el.id); return; } catch (e) { /* fall through */ }
    }
    if (!global.bootstrap || !global.bootstrap.Modal) return;
    var instance = global.bootstrap.Modal.getInstance(el);
    if (instance) instance.hide();
  }

  // ── Tracklist ids ───────────────────────────────────────────────────────

  /*
    The macro deliberately does NOT emit ids: a release-GROUP title is not
    unique on its own (three self-titled Weezer albums), and only the client
    can see which rows actually collide on one page. Assigning here keeps ids
    unique per rendered page without the server needing to know.
  */
  function wireTracklistIds() {
    var seen = Object.create(null);
    Array.prototype.forEach.call(doc.querySelectorAll('.release-item'), function (item) {
      var summary = item.querySelector('.release-summary');
      var tracklistEl = item.querySelector('.release-tracklist');
      var contentEl = item.querySelector('.release-tracklist-content');
      if (!summary || !tracklistEl || !contentEl) return;

      var base = safeForDomId(summary.getAttribute('data-artist')) + '-' + safeForDomId(summary.getAttribute('data-album'));
      // Disambiguate repeated titles (reissues, self-titled albums).
      var count = seen[base] || 0;
      seen[base] = count + 1;
      if (count > 0) base = base + '-' + count;

      tracklistEl.id = 'tracklist-' + base;
      contentEl.id = 'tracklist-content-' + base;
    });
  }

  // ── Tracklist load / toggle ─────────────────────────────────────────────

  function loadTracklist(item) {
    var summary = item.querySelector('.release-summary');
    var tracklistEl = item.querySelector('.release-tracklist');
    var contentEl = item.querySelector('.release-tracklist-content');
    if (!summary || !tracklistEl || !contentEl) return;

    // Load once; a re-open reuses what is already there.
    if (tracklistEl.getAttribute('data-loaded') === '1') return;
    tracklistEl.setAttribute('data-loaded', '1');

    var artist = summary.getAttribute('data-artist') || '';
    var album = summary.getAttribute('data-album') || '';
    var mbid = summary.getAttribute('data-mbid') || '';
    var releaseId = summary.getAttribute('data-release-id') || '';
    var isMissing = item.getAttribute('data-status') === 'missing';

    contentEl.innerHTML = '<div class="text-center py-2"><span class="spinner-border spinner-border-sm"></span> Loading tracks...</div>';

    /*
      OWNED albums read the `tracks` table, which is the source of truth for
      the collection: /api/album/tracklist keys on (artist, album) and the
      repository compares CASE-INSENSITIVELY, because this page selected the
      album with a LOWER() match on the album artist.

      MISSING releases have no `tracks` rows at all, so they must come from
      MusicBrainz — via /api/artist/release/tracklist, which serves the CACHED
      missing_releases.tracklist first (filled in the background) and only
      reaches the API when that cache is empty.

      The URL used to be '/api/musicbrainz/release/tracks?mbid=&release_id='.
      No such route exists — the only match is
      POST /api/album/musicbrainz/release/tracks, a different blueprint, method
      and argument name — so EVERY missing release 404'd. It also read
      data-release-id off the SUMMARY while the attribute only existed on the
      Import button, so the id was always undefined even had the path been
      right. Both are fixed: the attribute is now emitted on the summary (see
      components/_release_section.html) and read here.
    */
    var url = isMissing
      ? '/api/artist/release/tracklist?release_id=' + encodeURIComponent(releaseId) + '&artist=' + encodeURIComponent(artist)
      : '/api/album/tracklist?artist=' + encodeURIComponent(artist) + '&album=' + encodeURIComponent(album) + '&mbid=' + encodeURIComponent(mbid);

    getJson(url)
      .then(function (data) {
        var tracks = (data && (data.tracklist || data.tracks)) || [];
        if (!tracks.length) {
          contentEl.innerHTML = '<div class="text-muted small py-2">No tracks found.</div>';
          return;
        }
        var html = '<div class="list-group list-group-flush small">';
        tracks.forEach(function (track) {
          var position = track.position || track.number || track.track_number || '';
          var title = track.title || track.name || '';
          html += '<div class="list-group-item d-flex justify-content-between align-items-center bg-transparent text-muted border-secondary py-1">'
            + '<span class="text-truncate">' + (position ? esc(position) + '. ' : '') + esc(title) + '</span>'
            + (track.length || track.duration
                ? '<span class="text-muted small ms-2 flex-shrink-0">' + esc(track.length || track.duration) + '</span>'
                : '')
            + '</div>';
        });
        contentEl.innerHTML = html + '</div>';
      })
      .catch(function () {
        tracklistEl.setAttribute('data-loaded', '0'); // allow a retry
        contentEl.innerHTML = '<div class="text-danger small py-2">Error loading tracks.</div>';
      });
  }

  function toggleTracklist(item) {
    var tracklistEl = item.querySelector('.release-tracklist');
    var icon = item.querySelector('.release-toggle-btn i');
    var btn = item.querySelector('.release-toggle-btn');
    if (!tracklistEl) return;

    var hidden = tracklistEl.style.display === 'none' || !tracklistEl.style.display;
    tracklistEl.style.display = hidden ? '' : 'none';
    if (icon) icon.className = hidden ? 'bi bi-chevron-up' : 'bi bi-chevron-down';
    if (btn) btn.setAttribute('aria-expanded', hidden ? 'true' : 'false');
    if (hidden) loadTracklist(item);
  }

  function wireTracklistToggles() {
    Array.prototype.forEach.call(doc.querySelectorAll('.release-item'), function (item) {
      var summary = item.querySelector('.release-summary');
      var toggleBtn = item.querySelector('.release-toggle-btn');

      if (summary) {
        summary.addEventListener('click', function () { toggleTracklist(item); });
        summary.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleTracklist(item); }
        });
      }
      if (toggleBtn) {
        toggleBtn.addEventListener('click', function (e) { e.stopPropagation(); toggleTracklist(item); });
      }
    });
  }

  // ── Per-section All / Library / Missing filter ──────────────────────────

  /*
    Scoped per section rather than page-wide: a page-wide "Missing" filter hid
    entire categories, so an artist with one missing single and 20 owned albums
    looked like they owned nothing. Each section keeps its own selection.
  */
  function applySectionFilter(section) {
    if (!section) return;
    var checked = section.querySelector('.release-filter-radio:checked');
    var value = checked ? checked.value : 'all';
    var list = section.querySelector('.release-list');
    if (!list) return;

    var visible = 0;
    Array.prototype.forEach.call(list.querySelectorAll('.release-item'), function (item) {
      var show = value === 'all' || item.getAttribute('data-status') === value;
      item.style.display = show ? '' : 'none';
      if (show) visible += 1;
    });

    var note = section.querySelector('.release-empty-note');
    if (note) note.style.display = visible === 0 ? '' : 'none';
  }

  function applyAllSectionFilters() {
    Array.prototype.forEach.call(doc.querySelectorAll('.release-section'), applySectionFilter);
  }

  function wireFilters() {
    // Delegated: the release lists are re-rendered by checkMissingReleases(),
    // so per-element listeners would be lost on every refresh.
    doc.addEventListener('change', function (e) {
      var radio = e.target;
      if (!radio || !radio.classList || !radio.classList.contains('release-filter-radio')) return;
      if (!radio.checked) return;
      applySectionFilter(radio.closest('.release-section'));
    });
  }

  // ── Row action buttons ──────────────────────────────────────────────────

  function wireRowActions() {
    Array.prototype.forEach.call(doc.querySelectorAll('.release-item'), function (item) {
      var summary = item.querySelector('.release-summary');
      if (!summary || item.getAttribute('data-wired') === '1') return;
      item.setAttribute('data-wired', '1');

      var artist = summary.getAttribute('data-artist') || '';
      var album = summary.getAttribute('data-album') || '';

      var editBtn = item.querySelector('.release-edit-btn');
      if (editBtn) {
        editBtn.addEventListener('click', function (e) {
          e.stopPropagation();
          openEditReleaseModal(editBtn.dataset);
        });
      }

      var importBtn = item.querySelector('.release-import-btn');
      if (importBtn) {
        importBtn.addEventListener('click', function (e) {
          e.stopPropagation();
          importMissingRelease(artist, album, importBtn, summary);
        });
      }

      var mbBtn = item.querySelector('.release-mb-search-btn');
      if (mbBtn) {
        mbBtn.addEventListener('click', function (e) {
          e.stopPropagation();
          searchMusicBrainzForAlbum(artist, album);
        });
      }
    });
  }

  function wireSectionControls() {
    // Expand / collapse every tracklist in one section.
    Array.prototype.forEach.call(doc.querySelectorAll('.release-section'), function (section) {
      var toggle = section.querySelector('.release-section-toggle');
      if (toggle) {
        toggle.addEventListener('click', function () {
          // Bootstrap's collapse handles visibility; refresh the chevron.
          var body = section.querySelector('.release-section-body');
          var chevron = toggle.querySelector('.section-chevron');
          if (body && chevron) {
            global.setTimeout(function () {
              var shown = body.classList.contains('show');
              chevron.style.transform = shown ? '' : 'rotate(-90deg)';
            }, 0);
          }
        });
      }

      var searchAll = section.querySelector('.release-search-all-btn');
      if (searchAll) {
        searchAll.addEventListener('click', function () {
          if (global.artistPage && typeof global.artistPage.searchAllReleases === 'function') {
            global.artistPage.searchAllReleases(searchAll.getAttribute('data-artist') || artistName());
          } else {
            searchMusicBrainzForAlbum(searchAll.getAttribute('data-artist') || artistName(), '');
          }
        });
      }

      var checkMissing = section.querySelector('.release-check-missing-btn');
      if (checkMissing) {
        checkMissing.addEventListener('click', function () { checkMissingReleases(checkMissing.getAttribute('data-artist') || artistName()); });
      }
    });
  }

  function openEditReleaseModal(data) {
    var el = showModal('editReleaseModal');
    if (!el) return;
    var set = function (id, value) { var f = doc.getElementById(id); if (f) f.value = value == null ? '' : value; };
    set('editReleaseArtist', data.artist || '');
    set('editReleaseOriginalTitle', data.album || '');
    set('editReleaseTitle', data.album || '');
    set('editReleaseYear', data.year || '');
    set('editReleaseMbid', data.mbid || '');
  }

  function importMissingRelease(artist, album, button, summary) {
    var original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';

    /*
      The route signature is
          /api/artist/import-release  {artist, release_id, title}
      — NOT {album, mbid}. `title` is the RELEASE-GROUP title the user clicked,
      and `release_id` is the MusicBrainz release-group id.

      The macro puts data-release-id on the IMPORT BUTTON (not on the summary),
      so the button is the primary source and the summary is the fallback.
    */
    var releaseId = (button.dataset && button.dataset.releaseId)
      || (summary && summary.getAttribute('data-release-id'))
      || '';

    postJson('/api/artist/import-release', {
      artist: artist,
      release_id: releaseId,
      title: album,
    })
      .then(function (data) {
        button.disabled = false;
        button.innerHTML = original;
        if (data && (data.success || data.queued || data.download_id)) {
          toastSuccess('Import queued for “' + album + '”.');
        } else {
          toastError((data && (data.error || data.message)) || 'Import failed.');
        }
      })
      .catch(function () {
        button.disabled = false;
        button.innerHTML = original;
        toastError('Import failed.');
      });
  }

  function searchMusicBrainzForAlbum(artist, album) {
    var el = showModal('artistMbSearchModal');
    if (!el) return;

    var label = doc.getElementById('artistMbSearchArtist');
    if (label) label.textContent = album ? artist + ' — ' + album : artist;

    var status = doc.getElementById('artistMbSearchStatus');
    var results = doc.getElementById('artistMbSearchResults');
    var errorEl = doc.getElementById('artistMbSearchError');
    if (status) status.style.display = 'block';
    if (results) results.innerHTML = '';
    if (errorEl) errorEl.style.display = 'none';

    postJson('/api/musicbrainz/search', { query: album ? artist + ' ' + album : artist, artist_only: !album })
      .then(function (data) {
        if (status) status.style.display = 'none';
        var releases = (data && (data.releases || data.results)) || [];
        if (!releases.length) {
          if (results) results.innerHTML = '<div class="text-muted small">No releases found.</div>';
          return;
        }
        var html = '<div class="list-group">';
        releases.forEach(function (rel) {
          var title = rel.title || '';
          var date = rel.date || rel.first_release_date || '';
          html += '<div class="list-group-item d-flex justify-content-between align-items-center bg-transparent text-light border-secondary">'
            + '<span class="text-truncate"><span class="fw-semibold">' + esc(title) + '</span>'
            + (date ? ' <span class="text-muted small">' + esc(String(date).slice(0, 4)) + '</span>' : '')
            + '</span>'
            + '<button type="button" class="btn btn-sm btn-success flex-shrink-0" data-action="mb-download"'
            + ' data-artist="' + esc(artist) + '" data-album="' + esc(title) + '">'
            + '<i class="bi bi-download"></i></button>'
            + '</div>';
        });
        if (results) results.innerHTML = html + '</div>';
      })
      .catch(function () {
        if (status) status.style.display = 'none';
        if (errorEl) { errorEl.textContent = 'MusicBrainz search failed.'; errorEl.style.display = 'block'; }
      });
  }

  // ── Import tracking: mark rows that are already queued ──────────────────

  function markDownloadingRows(ids) {
    if (!ids || !ids.length) return;
    Array.prototype.forEach.call(doc.querySelectorAll('.release-item[data-status="missing"]'), function (item) {
      var summary = item.querySelector('.release-summary');
      if (!summary) return;
      var key = String(summary.getAttribute('data-album') || '').toLowerCase();
      if (ids.indexOf(key) === -1) return;

      var badge = item.querySelector('.release-status-badge');
      if (badge && !badge.getAttribute('data-queued')) {
        badge.setAttribute('data-queued', '1');
        badge.className = 'badge release-status-badge';
        badge.style.backgroundColor = 'rgba(34,211,238,0.15)';
        badge.style.color = '#22d3ee';
        badge.style.border = '1px solid rgba(34,211,238,0.5)';
        badge.textContent = 'Queued';
      }
    });
  }

  function fetchQueuedImports() {
    getJson('/api/musicbrainz/downloads')
      .then(function (data) {
        var downloads = (data && (data.downloads || data.items)) || [];
        var names = downloads
          .map(function (d) { return String(d.album || d.title || '').toLowerCase(); })
          .filter(Boolean);
        markDownloadingRows(names);
      })
      .catch(function () { /* non-fatal: the badge is cosmetic */ });
  }

  // ── Missing-track badges ────────────────────────────────────────────────

  /*
    The inverse of "I can't hide the missing releases that are populated": an
    OWNED album with gaps should advertise them. Only rows that have a
    MusicBrainz id are probed, because the endpoint keys on the MBID.

    WHY THE PROBES ARE BOUNDED AND STAND DOWN DURING A SCAN
    ------------------------------------------------------  
    /api/album/missing-tracks is a SYNCHRONOUS handler that calls
    MusicBrainz, and Quart runs synchronous handlers in the loop's DEFAULT
    executor -- the same pool routes/ui_routes.py::artist_detail uses via
    asyncio.to_thread. MusicBrainz is globally throttled to ~1 req/s by
    api_clients/musicbrainz_http.py::_strict_throttle, which enforces the
    budget by RESERVING a future slot and only then sleeping to it. So one
    probe per owned album, fired at page load, used to:

      1. claim N slots in that throttle, pushing the running scan's own
         MusicBrainz calls ~1.2s x N further out -- the scan looked stuck;
      2. hold one executor thread per probe while it slept; and
      3. starve the default executor, so every other request in the worker,
         including this page's own render, could not start.

    The artist page is reloaded the instant the scan form is POSTed, which is
    exactly why starting a scan from the artist page froze the whole server.

    Two guards, both cheap:
      * MISSING_PROBE_CONCURRENCY caps how many probes are in flight, so this
        page can never monopolise the shared executor or the MB budget.
      * While a scan is running the probes stand down completely
        (data-scan-active="1" on #releases-sections, rendered by the route):
        the scan owns the MusicBrainz budget and is rewriting this data
        anyway. An ABSENT attribute means "not scanning", so an older template
        or a cached script degrades to the safe bounded path, never the storm.
  */

  var MISSING_PROBE_CONCURRENCY = 3;

  function scanIsActive() {
    var marker = doc.getElementById('releases-sections');
    return !!marker && marker.getAttribute('data-scan-active') === '1';
  }

  function fetchMissingTrackCounts() {
    var artist = artistName();
    if (!artist) return;
    if (scanIsActive()) return;

    var jobs = [];
    Array.prototype.forEach.call(doc.querySelectorAll('.release-item[data-status="library"][data-mbid]'), function (item) {
      var summary = item.querySelector('.release-summary');
      var badge = item.querySelector('.album-missing-tracks-badge');
      var mbid = summary && summary.getAttribute('data-mbid');
      if (!badge || !mbid) return;
      jobs.push({ summary: summary, badge: badge, mbid: mbid });
    });
    if (!jobs.length) return;

    var next = 0;

    function runNext() {
      if (next >= jobs.length) return;
      var job = jobs[next++];
      getJson('/api/album/missing-tracks?artist=' + encodeURIComponent(artist)
        + '&album=' + encodeURIComponent(job.summary.getAttribute('data-album') || '')
        + '&mbid=' + encodeURIComponent(job.mbid))
        .then(function (data) {
          var count = data && data.missing_count;
          if (count > 0) {
            job.badge.textContent = count + ' missing';
            job.badge.style.display = '';
          }
        })
        .catch(function () { /* cosmetic */ })
        .then(runNext);
    }

    // Prime the pool: each completion pulls the next job, so at most
    // MISSING_PROBE_CONCURRENCY requests are ever in flight.
    for (var i = 0; i < MISSING_PROBE_CONCURRENCY; i++) runNext();
  }

  // ── Expanding / collapsing every section's tracklists ───────────────────

  /**
   * Open or shut every SECTION's accordion body.
   *
   * Without this, "Expand All" only revealed tracklists inside sections that
   * were already open, so a section collapsed because it has nothing in the
   * library (see the release-section macro) could never be revealed by the
   * button that claims to expand everything.
   *
   * `aria-expanded` is kept in step rather than relying on the collapse event:
   * the chevron and the "none in your library" hint are both driven by it, and
   * collapsing a section whose body is ALREADY shut fires no event at all —
   * leaving the header stale.
   */
  function setSectionExpanded(section, expand) {
    var body = section.querySelector('.release-section-body');
    var toggle = section.querySelector('.release-section-toggle');
    if (!body) return;

    if (global.bootstrap && global.bootstrap.Collapse) {
      var instance = global.bootstrap.Collapse.getOrCreateInstance(body, { toggle: false });
      if (expand) instance.show();
      else instance.hide();
    } else {
      // No Bootstrap: flip the class directly so the body still responds.
      body.classList.toggle('show', !!expand);
    }
    if (toggle) toggle.setAttribute('aria-expanded', expand ? 'true' : 'false');
  }

  /** Snap back to the state the page was rendered with (used on collapse-all). */
  function restoreInitialSectionState() {
    Array.prototype.forEach.call(doc.querySelectorAll('.release-section'), function (section) {
      setSectionExpanded(section, section.getAttribute('data-auto-collapsed') !== '1');
    });
  }

  function expandAll(expand) {
    Array.prototype.forEach.call(doc.querySelectorAll('.release-item'), function (item) {
      var tracklistEl = item.querySelector('.release-tracklist');
      var icon = item.querySelector('.release-toggle-btn i');
      var btn = item.querySelector('.release-toggle-btn');
      if (!tracklistEl) return;
      tracklistEl.style.display = expand ? '' : 'none';
      if (icon) icon.className = expand ? 'bi bi-chevron-up' : 'bi bi-chevron-down';
      if (btn) btn.setAttribute('aria-expanded', expand ? 'true' : 'false');
      if (expand) loadTracklist(item);
    });

    if (expand) {
      // Reveal every section, including ones auto-collapsed for having no
      // library items.
      Array.prototype.forEach.call(doc.querySelectorAll('.release-section'), function (section) {
        setSectionExpanded(section, true);
      });
    } else {
      restoreInitialSectionState();
    }
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function init() {
    // The marker element gates initialisation, so this file is inert on any
    // page that does not render release sections.
    if (!doc.getElementById('releases-sections')) return;

    wireTracklistIds();
    wireTracklistToggles();
    wireFilters();
    wireRowActions();
    wireSectionControls();
    applyAllSectionFilters();
    fetchMissingTrackCounts();
    fetchQueuedImports();

    var expandBtn = doc.getElementById('releasesExpandAllBtn');
    if (expandBtn) expandBtn.addEventListener('click', function () { expandAll(true); });

    var collapseBtn = doc.getElementById('releasesCollapseAllBtn');
    if (collapseBtn) collapseBtn.addEventListener('click', function () { expandAll(false); });

    // MusicBrainz results: one delegated handler rather than inline onclick,
    // so a title containing a quote cannot break the markup.
    doc.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-action="mb-download"]');
      if (!btn) return;
      openDownloadSearchFor(btn.getAttribute('data-artist'), btn.getAttribute('data-album'));
    });

    // Save handler for the Edit Release modal.
    var saveBtn = doc.getElementById('editReleaseSaveBtn');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        var artist = (doc.getElementById('editReleaseArtist') || {}).value || '';
        var original = (doc.getElementById('editReleaseOriginalTitle') || {}).value || '';
        var title = (doc.getElementById('editReleaseTitle') || {}).value || '';
        var year = (doc.getElementById('editReleaseYear') || {}).value || '';
        if (!title) { toastError('A release title is required.'); return; }

        var form = new FormData();
        form.set('album_title', title);
        form.set('album_artist', artist);
        if (year) form.set('album_originalyear', year);

        saveBtn.disabled = true;
        fetch('/album/' + encodeURIComponent(artist) + '/' + encodeURIComponent(original), {
          method: 'POST',
          body: form,
        })
          .then(function (r) {
            saveBtn.disabled = false;
            if (r.ok) { toastSuccess('Release updated.'); global.location.reload(); }
            else toastError('Save failed.');
          })
          .catch(function () {
            saveBtn.disabled = false;
            toastError('Save failed.');
          });
      });
    }
  }

  /** Bridge to whichever download dialog the page provides. */
  function openDownloadSearchFor(artist, album) {
    if (typeof global.openDownloadSearch === 'function') {
      global.openDownloadSearch(artist, album);
      return;
    }
    // Fall back to the Soulseek search page, which needs no modal.
    var url = '/downloads/search/soulseek?artist=' + encodeURIComponent(artist || '')
      + (album ? '&album=' + encodeURIComponent(album) : '');
    global.location.href = url;
  }

  if (doc.readyState === 'loading') {
    doc.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Exposed for tests and for other modules on the page.
  global.artistReleases = {
    init: init,
    applyAllSectionFilters: applyAllSectionFilters,
    applySectionFilter: applySectionFilter,
    toggleTracklist: toggleTracklist,
    loadTracklist: loadTracklist,
    expandAll: expandAll,
    safeForDomId: safeForDomId,
  };
})(window);
