/**
 * Unified hybrid search — All / In Library / MusicBrainz.
 * (Typing triggers input sync only; searches execute on Enter or filter change.)
 */
(function () {
  'use strict';

  var SCOPE_ALL = 'all';
  var SCOPE_LIBRARY = 'library';
  var SCOPE_MB = 'mb';
  var MB_LIMIT_ALL_TAB = 12;
  var MB_LIMIT_MB_TAB = 25;
  var MIN_QUERY_LENGTH = 2;

  var _scope = SCOPE_ALL;
  var _runSeq = 0;
  var _queuedIds = {};
  // Transient "a queue request is in flight for this release" state, kept
  // separate from _queuedIds (which means "already queued"). Both the lookup
  // and the queue POST take seconds, and the button previously gave no
  // feedback at all, so a second click was indistinguishable from the first —
  // see queueRelease().
  var _queuedInFlight = {};
  var _mbIndex = {};
  var _counts = { all: 0, library: 0, mb: 0 };
  var _lastQuery = null;
  var _lastScope = null;
  var _mbDone = false;
  var _mbResults = [];

  function emptyLocalResult() {
    return { artists: [], albums: [], compilations: [], live_albums: [], eps: [], singles: [], tracks: [] };
  }

  var localResult = emptyLocalResult();

  function getModalEl() { return document.getElementById('unifiedSearchModal'); }
  function getInputEl() { return document.getElementById('unifiedSearchInput'); }
  function getResultsEl() { return document.getElementById('unifiedSearchResults'); }
  function getMetaEl() { return document.getElementById('usResultMeta'); }
  function getErrorEl() { return document.getElementById('unifiedSearchError'); }

  function esc(text) {
    if (text === undefined || text === null) return '';
    return String(text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function countLibrary(local) {
    var albumBuckets = (local.albums || []).length + (local.compilations || []).length +
      (local.live_albums || []).length + (local.eps || []).length + (local.singles || []).length;
    return (local.artists || []).length + albumBuckets + (local.tracks || []).length;
  }

  function updateScopeCounts() {
    var labels = { all: _counts.all, library: _counts.library, mb: _counts.mb };
    var tabs = document.querySelectorAll('#unifiedScopeTabs .nav-link');
    for (var i = 0; i < tabs.length; i++) {
      var span = tabs[i].querySelector('.us-scope-count');
      if (!span) continue;
      var n = labels[tabs[i].getAttribute('data-scope')] || 0;
      if (n > 0) {
        span.textContent = n;
        span.classList.remove('d-none');
      } else {
        span.classList.add('d-none');
      }
    }
  }

  function markRendered(query) {
    _lastQuery = query;
    _lastScope = _scope;
  }

  function selectScope(scope) {
    _scope = (scope === SCOPE_MB || scope === SCOPE_LIBRARY) ? scope : SCOPE_ALL;
    var tabs = document.querySelectorAll('#unifiedScopeTabs .nav-link');
    for (var i = 0; i < tabs.length; i++) {
      var active = tabs[i].getAttribute('data-scope') === _scope;
      tabs[i].classList.toggle('active', active);
      tabs[i].setAttribute('aria-selected', active ? 'true' : 'false');
    }
  }

  function fetchLibrary(query) {
    return fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query })
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
  }

  function getTypeFilter() {
    var el = document.getElementById('unifiedSearchType');
    return el ? el.value : '';
  }

  function getAdvancedFilters() {
    function val(id) {
      var el = document.getElementById(id);
      return el ? el.value.trim() : '';
    }
    return {
      artist: val('unifiedFilterArtist'),
      album: val('unifiedFilterAlbum'),
      track: val('unifiedFilterTrack'),
      year: val('unifiedFilterYear'),
      year_to: val('unifiedFilterYearTo'),
      genre: val('unifiedFilterGenre')
    };
  }

  // Every key returned by getAdvancedFilters(). Deriving `hasAdvanced` and the
  // fetchMb payload from ONE list stops them drifting apart: `hasAdvanced`
  // previously tested only artist/album/track/year and `mbOpts` forwarded only
  // those four, so the Year-To and Genre inputs were read, then silently
  // dropped — fetchMb() supports opts.year_to and opts.genre, but never
  // received them, and typing in either field alone did not even count as an
  // advanced search.
  var ADVANCED_FILTER_KEYS = ['artist', 'album', 'track', 'year', 'year_to', 'genre'];

  function hasAnyAdvancedFilter(adv) {
    for (var i = 0; i < ADVANCED_FILTER_KEYS.length; i++) {
      if (adv[ADVANCED_FILTER_KEYS[i]]) return true;
    }
    return false;
  }

  function fetchMb(query, limit, opts) {
    opts = opts || {};
    var hasAdvanced = opts.artist || opts.album || opts.track || opts.year || opts.year_to || opts.genre;

    if (!hasAdvanced && !opts.type && typeof window.searchMusicBrainzReleases === 'function') {
      return window.searchMusicBrainzReleases(query, '', limit)
        .then(function (data) { return data.releases || []; })
        .catch(function () { return []; });
    }

    var payload = {};
    if (hasAdvanced) {
      payload.artist = opts.artist || '';
      payload.album = opts.album || '';
      payload.track = opts.track || '';
      payload.year = opts.year || '';
      payload.year_to = opts.year_to || '';
      payload.genre = opts.genre || '';
      if (opts.artist && !opts.album && !opts.track && !opts.year && !opts.year_to && !opts.genre) payload.artist_only = true;
    } else {
      payload.query = query;
    }
    if (opts.type) payload.type = opts.type;

    return fetch('/api/musicbrainz/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (res) { return res.ok ? res.json() : { releases: [] }; })
      .then(function (data) { return (data.releases || []).slice(0, limit); })
      .catch(function () { return []; });
  }

  var _IMG_PLACEHOLDER = 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2244%22 height=%2244%22%3E%3Crect fill=%22%232a2a2a%22 width=%2244%22 height=%2244%22/%3E%3C/svg%3E';

  function thumbHtml(src, cls, alt) {
    if (!src) src = _IMG_PLACEHOLDER;
    return '<img src="' + esc(src) + '" alt="' + esc(alt || '') + '" loading="lazy" ' +
      'class="' + cls + ' flex-shrink-0" ' +
      'style="width:44px;height:44px;object-fit:cover;" ' +
      'onerror="this.onerror=null;this.src=\'' + _IMG_PLACEHOLDER + '\';">';
  }

  function thumbWithBadge(thumb, badgeIcon, badgeClass) {
    return '<span class="us-thumb-wrap">' + thumb +
      '<span class="us-thumb-badge ' + badgeClass + '">' + badgeIcon + '</span></span>';
  }

  function fmtDuration(seconds) {
    var s = Number(seconds);
    if (!isFinite(s) || s <= 0) return '';
    var m = Math.round(s / 60);
    if (m >= 60) return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
    return m + 'm';
  }

  var SECTION_INLINE_LIMIT = 5;

  function buildSection(title, items, rower) {
    var count = items.length;
    var visible = items.slice(0, SECTION_INLINE_LIMIT);
    var hidden = items.slice(SECTION_INLINE_LIMIT);
    var toggle = hidden.length
      ? '<button type="button" class="btn btn-sm btn-link us-section-toggle py-0 text-decoration-none" data-count="' + count + '" onclick="toggleUsSection(this)">' +
        'Show All ' + count + ' <i class="bi bi-chevron-down"></i></button>'
      : '';
    return '<div class="us-section mb-1">' +
      '<div class="us-section-title d-flex align-items-center gap-2">' + title + toggle + '</div>' +
      rower(visible) +
      (hidden.length ? '<div class="us-section-more d-none">' + rower(hidden) + '</div>' : '') +
      '</div>';
  }

  window.toggleUsSection = function (btn) {
    var hiddenEl = btn.closest('.us-section').querySelector('.us-section-more');
    if (!hiddenEl) return;
    var expanded = !hiddenEl.classList.contains('d-none');
    hiddenEl.classList.toggle('d-none', expanded);
    btn.innerHTML = expanded
      ? 'Show All ' + (btn.dataset.count || '') + ' <i class="bi bi-chevron-down"></i>'
      : 'Show Less <i class="bi bi-chevron-up"></i>';
  };

  function filterLocalByType(local, type) {
    if (!type) return local;
    var bucketMap = {
      album: ['albums'],
      single: ['singles'],
      ep: ['eps'],
      compilation: ['compilations'],
      live: ['live_albums'],
      soundtrack: ['albums', 'compilations'],
      remix: ['albums']
    };
    var keep = bucketMap[type] || [];
    var out = emptyLocalResult();
    keep.forEach(function (k) { out[k] = local[k] || []; });
    return out;
  }

  // base.html loads js/artist_names.js before this module. Artists are shown
  // filed ("Offspring, The") so the bucket matches the /artists sections and the
  // ordering the server already applies.
  //
  // The href and the image URL below deliberately keep the RAW name: the
  // inverted form is a display label, not the artist's identity, and using it
  // for either would break the link and the avatar lookup.
  //
  // Falls back to the raw name rather than throwing — search must keep working
  // if that script is ever missing.
  var artistNames = global.artistNames || {
    sortName: function (v) { return (v === null || v === undefined) ? '' : String(v); },
  };

  function artistRows(artists) {
    return artists.map(function (a) {
      var isVarious = String(a.name || '').trim().toLowerCase() === 'various artists';
      return '<a class="us-row" href="/artist/' + encodeURIComponent(a.name) + '">' +
        thumbHtml('/api/artist/image?name=' + encodeURIComponent(a.name), 'rounded-circle border border-secondary', a.name) +
        '<span class="us-row-main">' +
          '<span class="us-row-title d-block">' + esc(artistNames.sortName(a.name)) + '</span>' +
          '<span class="us-row-sub d-block">' + a.track_count + ' track' + (a.track_count === 1 ? '' : 's') + ' - ' + a.album_count + ' album' + (a.album_count === 1 ? '' : 's') + '</span>' +
        '</span>' +
        '<span class="us-row-meta badge bg-secondary-subtle text-secondary-emphasis">Artist</span>' +
        (isVarious ? '<span class="us-row-meta badge bg-secondary ms-1" title="Compilation placeholder entity">Compilation</span>' : '') +
      '</a>';
    }).join('');
  }

  function trackRows(tracks) {
    return tracks.map(function (t) {
      return '<a class="us-row" href="/track/' + encodeURIComponent(t.id) + '">' +
        '<span class="us-row-icon"><i class="bi bi-music-note"></i></span>' +
        '<span class="us-row-main">' +
          '<span class="us-row-title d-block">' + esc(t.title) + '</span>' +
          '<span class="us-row-sub d-block">' + esc(t.artist) + (t.album ? ' - ' + esc(t.album) : '') + '</span>' +
        '</span>' +
        (t.stars ? '<span class="us-row-meta text-warning"><i class="bi bi-star-fill"></i> ' + esc(t.stars) + '</span>' : '') +
      '</a>';
    }).join('');
  }

  function mbReleaseArtist(r) {
    if (r.artist) return r.artist;
    if (r['artist-credit']) {
      return r['artist-credit'].map(function (a) {
        return typeof a === 'string' ? a : (a.name || a.artist || '');
      }).join(', ');
    }
    return 'Unknown Artist';
  }

  function mbReleaseYear(r) {
    return String(r.first_release_date || r.date || r.year || '').split('-')[0] || '?';
  }

  var SECTION_DEFS = [
    { key: 'albums', label: 'Albums', localLabel: 'Studio Album' },
    { key: 'compilations', label: 'Compilations', localLabel: 'Compilation' },
    { key: 'live_albums', label: 'Live Albums', localLabel: 'Live Album' },
    { key: 'eps', label: 'EPs', localLabel: 'EP' },
    { key: 'singles', label: 'Singles', localLabel: 'Single' }
  ];

  function classifyMbRelease(r) {
    var secondary = (r.secondary_types || []).map(function (s) { return String(s).toLowerCase(); });
    if (secondary.indexOf('live') !== -1) return 'live_albums';
    if (secondary.indexOf('compilation') !== -1) return 'compilations';
    if (secondary.indexOf('ep') !== -1) return 'eps';
    if (secondary.indexOf('single') !== -1) return 'singles';
    var pt = String(r.category || r.primary_type || '').toLowerCase();
    if (pt === 'ep') return 'eps';
    if (pt === 'single') return 'singles';
    return 'albums';
  }

  function bucketTypeLabel(bucketKey) {
    for (var i = 0; i < SECTION_DEFS.length; i++) {
      if (SECTION_DEFS[i].key === bucketKey) return SECTION_DEFS[i].localLabel;
    }
    return 'Release';
  }

  function releaseRows(items, withQueue) {
    var html = '';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var yearSuffix = it.year ? ' (' + esc(it.year) + ')' : '';
      var trackLine = '';
      if (it.track_count) {
        trackLine = '<span class="us-row-sub d-block">' + it.track_count + ' track' + (it.track_count === 1 ? '' : 's') + (it.duration_total ? ' - ' + fmtDuration(it.duration_total) : '') + '</span>';
      }

      if (it.local || it.owned) {
        var yearPath = it.year ? '/' + it.year : '';
        var href = '/album/' + encodeURIComponent(it.artist) + '/' + encodeURIComponent(it.title) + yearPath;
        var localArt = '/api/album/' + encodeURIComponent(it.artist) + '/' + encodeURIComponent(it.title) + '/art';
        html += '<a class="us-row" href="' + href + '">' +
          thumbWithBadge(thumbHtml(localArt, 'rounded border border-secondary', it.title),
            '<i class="bi bi-music-note-fill"></i>', 'accent-library-bg') +
          '<span class="us-row-main">' +
            '<span class="us-row-title d-block">' + esc(it.title) + yearSuffix + '</span>' +
            '<span class="us-row-sub d-block"><span class="us-artist">By ' + esc(it.artist) + '</span> · <span class="us-type">' + esc(it.typeLabel || 'Album') + '</span></span>' +
            trackLine +
          '</span>' +
          '<span class="us-row-action btn btn-sm btn-outline-secondary" title="View in library">' +
            '<i class="bi bi-box-arrow-up-right"></i>' +
          '</span>' +
        '</a>';
      } else {
        var id = it.id || '';
        var queued = !!_queuedIds[id];
        _mbIndex[id] = it.release || null;
        var rel = it.release || {};
        var cover = rel.cover_art_url || (id ? 'https://coverartarchive.org/release-group/' + id + '/front-250' : '');
        var mbUrl = id ? 'https://musicbrainz.org/release-group/' + encodeURIComponent(id) : '#';
        html += '<a class="us-row" href="' + mbUrl + '" target="_blank" rel="noopener">' +
          thumbWithBadge(thumbHtml(cover, 'rounded border border-secondary', it.title),
            '<i class="bi bi-hexagon-fill"></i>', 'accent-mb-bg') +
          '<span class="us-row-main">' +
            '<span class="us-row-title d-block">' + esc(it.title) + yearSuffix + '</span>' +
            '<span class="us-row-sub d-block"><span class="us-artist">By ' + esc(it.artist) + '</span> · <span class="us-type">' + esc(it.typeLabel || 'Release') + '</span></span>' +
            trackLine +
          '</span>' +
          (withQueue ? (queued
            ? '<button class="btn btn-sm btn-success" disabled title="Already queued"><i class="bi bi-check2"></i> Queued</button>'
            : '<button class="btn btn-sm btn-outline-primary us-queue-btn" data-mbid="' + esc(id) + '" title="Queue download via Soulseek"><i class="bi bi-download"></i> Queue</button>')
            : '') +
        '</a>';
      }
    }
    return html;
  }

  function _ownedKey(artist, title) {
    function norm(s) {
      return String(s || '').toLowerCase().replace(/\([^)]*\)/g, ' ').replace(/\s+/g, ' ').trim();
    }
    return norm(artist) + '::' + norm(title);
  }

  function buildBuckets(local, mbReleases) {
    var buckets = { albums: [], compilations: [], live_albums: [], eps: [], singles: [] };
    var owned = {};

    (local.albums || []).forEach(function (al) {
      var bucket = buckets[al.type] ? al.type : 'albums';
      buckets[bucket].push({
        title: al.album, artist: al.artist, year: al.year || null,
        typeLabel: al.type_label || 'Album', local: true,
        track_count: al.track_count || null, duration_total: al.duration_total || null
      });
      owned[_ownedKey(al.artist, al.album)] = true;
    });

    (mbReleases || []).forEach(function (r) {
      var artist = mbReleaseArtist(r);
      var bucket = classifyMbRelease(r);
      buckets[bucket].push({
        title: r.title, artist: artist, year: mbReleaseYear(r),
        typeLabel: bucketTypeLabel(bucket), local: false, id: r.id || '', release: r,
        track_count: r.track_count || null,
        owned: !!owned[_ownedKey(artist, r.title)]
      });
    });

    Object.keys(buckets).forEach(function (k) {
      buckets[k].sort(function (a, b) {
        var ay = (a.year === '?' || a.year === null || a.year === undefined) ? 0 : Number(a.year) || 0;
        var by = (b.year === '?' || b.year === null || b.year === undefined) ? 0 : Number(b.year) || 0;
        if ((ay > 0) !== (by > 0)) return ay > 0 ? -1 : 1;
        if (ay !== by) return by - ay;
        if (a.local !== b.local) return a.local ? -1 : 1;
        return String(a.title || '').toLowerCase() < String(b.title || '').toLowerCase() ? -1 : 1;
      });
    });

    return buckets;
  }

  function renderReleaseSections(buckets, withQueue) {
    var html = '';
    for (var i = 0; i < SECTION_DEFS.length; i++) {
      var def = SECTION_DEFS[i];
      var items = buckets[def.key] || [];
      if (!items.length) continue;
      html += buildSection(
        def.label + ' <span class="badge bg-secondary ms-1">' + items.length + '</span>',
        items,
        function (list) { return releaseRows(list, withQueue); }
      );
    }
    return html;
  }

  function renderBucketedResults(local, mbReleases, query, opts) {
    opts = opts || {};
    var html = '';
    var artists = local.artists || [];
    if (artists.length) html += buildSection('Artists', artists, artistRows);
    html += renderReleaseSections(buildBuckets(local, mbReleases), opts.withQueue);

    if (opts.allMbButton && opts.mbPending) {
      html += '<div class="text-center my-2 text-muted small">' +
        '<span class="spinner-border spinner-border-sm me-1" role="status"></span> Loading MusicBrainz results…</div>';
    } else if (opts.allMbButton && mbReleases.length) {
      html += '<div class="text-center my-2">' +
        '<button type="button" class="btn btn-sm btn-outline-info" onclick="openUnifiedSearch(\'mb\')">' +
        '<i class="bi bi-search"></i> All MusicBrainz results</button></div>';
    }

    var tracks = local.tracks || [];
    if (tracks.length) html += buildSection('Tracks', tracks, trackRows);

    if (!artists.length && !html) {
      html += '<div class="text-center text-muted py-4 small">No library matches for "' + esc(query) + '"</div>';
    }
    return html;
  }

  function renderMbTab(local, releases, query) {
    if (!releases.length) {
      return '<div class="text-center text-muted py-4"><i class="bi bi-hexagon" style="font-size:2rem;"></i>' +
        '<p class="mt-2 mb-0 small">No MusicBrainz releases found for "' + esc(query) + '"</p></div>';
    }
    return renderReleaseSections(buildBuckets(local || {}, releases), true);
  }

  function runSearch() {
    var input = getInputEl();
    var resultsEl = getResultsEl();
    if (!input || !resultsEl) return;

    var query = input.value.trim();
    var adv = getAdvancedFilters();
    var hasAdvanced = hasAnyAdvancedFilter(adv);
    var seq = ++_runSeq;

    if (query.length < MIN_QUERY_LENGTH && !hasAdvanced) {
      resultsEl.innerHTML = '<div class="text-center text-muted py-5">' +
        '<i class="bi bi-search" style="font-size: 2rem;"></i>' +
        '<p class="mt-2 mb-0 small">Type at least ' + MIN_QUERY_LENGTH + ' characters and press Enter</p></div>';
      if (getMetaEl()) getMetaEl().textContent = '';
      markRendered(query);
      return;
    }

    resultsEl.innerHTML = '<div class="text-center py-5"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div></div>';

    // Forward EVERY advanced filter. `year_to` and `genre` were previously
    // omitted here, so the two inputs were read by getAdvancedFilters() and
    // then thrown away before the request was built.
    var mbOpts = { type: getTypeFilter() };
    ADVANCED_FILTER_KEYS.forEach(function (key) { mbOpts[key] = adv[key]; });

    var localQuery = query;
    if (localQuery.length < MIN_QUERY_LENGTH && hasAdvanced) {
      localQuery = adv.artist || adv.album || adv.track || adv.year || adv.genre || '';
    }

    // Reset the cached local result for THIS run. It is module-level state, so
    // without this an in-flight MusicBrainz promise could call renderLocal()
    // with the previous query's library hits and briefly show results from a
    // search the user has already replaced.
    localResult = emptyLocalResult();

    var localPromise = localQuery.length >= MIN_QUERY_LENGTH
      ? fetchLibrary(localQuery).catch(function (e) { return { error: e.message }; })
      : Promise.resolve(emptyLocalResult());

    var mbPromise;
    if (_scope === SCOPE_ALL) {
      mbPromise = fetchMb(query, MB_LIMIT_ALL_TAB, mbOpts);
    } else if (_scope === SCOPE_LIBRARY) {
      mbPromise = fetchMb(query, MB_LIMIT_MB_TAB, mbOpts)
        .then(function (r) {
          if (seq !== _runSeq) return [];
          _counts.mb = r.length;
          _counts.all = _counts.library + _counts.mb;
          updateScopeCounts();
          return [];
        })
        .catch(function () { return []; });
    } else {
      mbPromise = fetchMb(query, MB_LIMIT_MB_TAB, mbOpts);
    }

    var renderLocal = function (local) {
      if (seq !== _runSeq) return;
      if (getTypeFilter()) local = filterLocalByType(local, getTypeFilter());
      _counts.library = countLibrary(local);
      _counts.all = _counts.library + (_counts.mb || 0);
      updateScopeCounts();
      if (_scope === SCOPE_MB) return;

      var displayQuery = localQuery || query;
      var releaseCount = ['albums', 'compilations', 'live_albums', 'eps', 'singles'].reduce(function (n, k) {
        return n + ((local[k] || []).length);
      }, 0);

      var counts = [
        (local.artists || []).length + ' artist' + ((local.artists || []).length === 1 ? '' : 's'),
        releaseCount + ' album' + (releaseCount === 1 ? '' : 's'),
        (local.tracks || []).length + ' track' + ((local.tracks || []).length === 1 ? '' : 's')
      ];
      if (_scope === SCOPE_ALL) counts.push((_counts.mb || 0) + ' musicbrainz');
      if (getMetaEl()) getMetaEl().textContent = counts.join(' · ');

      var warnHtml = local.error
        ? '<div class="alert alert-warning py-2 small mb-2"><i class="bi bi-exclamation-triangle-fill"></i> Library search failed (' + esc(local.error) + ') — showing MusicBrainz only.</div>'
        : '';

      var mbReleases = _mbResults || [];
      resultsEl.innerHTML = warnHtml + renderBucketedResults(local, mbReleases, displayQuery, {
        withQueue: true,
        allMbButton: _scope === SCOPE_ALL,
        mbPending: _scope === SCOPE_ALL && !_mbDone
      });
      markRendered(query);
    };

    _mbDone = false;
    _mbResults = [];

    mbPromise.then(function (mbReleases) {
      if (seq !== _runSeq) return;
      _mbResults = mbReleases || [];
      _mbDone = true;
      _counts.mb = mbReleases.length;
      if (_scope !== SCOPE_LIBRARY) {
        _counts.all = _counts.library + _counts.mb;
        updateScopeCounts();
      }
      if (_scope === SCOPE_MB) {
        if (getMetaEl()) getMetaEl().textContent = mbReleases.length + ' musicbrainz result' + (mbReleases.length === 1 ? '' : 's');
        resultsEl.innerHTML = renderMbTab(localResult, mbReleases, localQuery || query);
        markRendered(query);
        return;
      }
      if (_scope === SCOPE_LIBRARY) return;
      renderLocal(localResult);
    });

    localPromise.then(function (local) {
      if (seq !== _runSeq) return;
      localResult = local;
      renderLocal(local);
    });
  }

  function notifyError(message) {
    var errEl = getErrorEl();
    if (!errEl) return;
    errEl.textContent = message;
    errEl.classList.remove('d-none');
    clearTimeout(notifyError._timer);
    notifyError._timer = setTimeout(function () { errEl.classList.add('d-none'); }, 5000);
  }

  // ── Queue button states ─────────────────────────────────────────────────
  //
  // Three distinct states, and the button has to move through them:
  //   1. idle      -> "Queue"            (this file's markup)
  //   2. in flight -> spinner + disabled (setQueueBtnBusy)
  //   3. queued    -> tick + disabled    (markQueueBtnDone)
  //
  // State 2 did not exist before, which is why clicking appeared to do
  // nothing and could be repeated freely.
  //
  // NOTE: this tree has no `ui/button-state.js`, so the live dashboard has no
  // `global.buttonState`. These helpers therefore manage the button directly,
  // and mirror what buttonState.setBusy/setDone do (including `aria-busy`) so
  // the two trees behave identically.
  var QUEUE_BTN_IDLE_HTML = '<i class="bi bi-download"></i> Queue';

  function setQueueBtnBusy(btn, label) {
    if (!btn) return;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.innerHTML =
      '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>' +
      (label ? ' ' + esc(label) : '');
  }

  function restoreQueueBtn(btn) {
    if (!btn) return;
    if (btn.classList.contains('btn-success')) return; // already marked queued
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    btn.innerHTML = QUEUE_BTN_IDLE_HTML;
  }

  function markQueueBtnDone(btn) {
    if (!btn) return;
    btn.disabled = true;
    btn.removeAttribute('aria-busy');
    btn.classList.replace('btn-outline-primary', 'btn-success');
    btn.innerHTML = '<i class="bi bi-check2"></i> Queued';
  }

  function queueRelease(rel, btn) {
    var id = rel.id || '';
    if (!id || _queuedIds[id]) return;

    // In-flight guard. The picker path below is asynchronous, and it used to
    // `return` immediately WITHOUT marking the button busy or recording any
    // state — so the click looked ignored, nothing blocked a second click, and
    // each click started ANOTHER MusicBrainz probe. N clicks therefore produced
    // N popups/toasts arriving together ~20s later.
    if (_queuedInFlight[id]) return;
    _queuedInFlight[id] = true;

    var artist = mbReleaseArtist(rel);

    // Show progress immediately: the release-picker probe is a network round
    // trip (measured in seconds), and the queue POST is another.
    setQueueBtnBusy(btn, 'Queuing…');

    // Shared settle path for BOTH branches. Always restores the busy state
    // first so the button cannot be left stuck, then either marks it queued or
    // returns it to normal.
    var settle = function (queued) {
      delete _queuedInFlight[id];
      restoreQueueBtn(btn);
      if (queued) {
        _queuedIds[id] = true;
        markQueueBtnDone(btn);
      }
    };

    if (typeof window.openReleasePicker === 'function') {
      var didQueue = false;
      var promise = window.openReleasePicker(id, rel.title || '', artist, function () {
        didQueue = true;
        settle(true);
      });

      if (promise && typeof promise.then === 'function') {
        // The picker resolved WITHOUT queueing: either the multi-version
        // flyout is now open for the user to choose from, or the lookup
        // failed. Either way the request is no longer in flight, so clear the
        // busy state and let the button be used again.
        promise.then(
          function () { if (!didQueue) settle(false); },
          function () { if (!didQueue) settle(false); }
        );
      } else {
        // No promise to await (an older openReleasePicker signature). Release
        // the guard on the next tick so the button can never be stuck forever.
        setTimeout(function () { if (!didQueue) settle(false); }, 0);
      }
      return;
    }

    fetch('/api/musicbrainz/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        release_id: id,
        release_title: rel.title || '',
        artist: artist,
        method: 'slskd',
        queue_items_only: true
      })
    })
      .then(function (res) { return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, data: data }; }); })
      .then(function (out) {
        if (!out.ok) throw new Error(out.data.error || 'Queue request failed');
        settle(true);
        if (typeof window.showQueueToast === 'function') window.showQueueToast(rel.title || 'Release');
      })
      .catch(function (e) {
        settle(false);
        notifyError('Queue failed: ' + e.message);
      });
  }

  function setAdvancedFiltersVisible(visible) {
    var panel = document.getElementById('unifiedAdvancedFilters');
    var toggle = document.getElementById('unifiedFiltersToggle');
    var icon = document.getElementById('unifiedFiltersToggleIcon');
    if (!panel) return;
    panel.classList.toggle('d-none', !visible);
    if (icon) icon.className = 'bi bi-chevron-' + (visible ? 'up' : 'right');
    if (toggle) toggle.setAttribute('aria-expanded', visible ? 'true' : 'false');
  }

  window.openUnifiedSearch = function (scope, prefill) {
    var modalEl = getModalEl();
    var input = getInputEl();
    if (!modalEl || !input) return;

    if (prefill === undefined || prefill === null) {
      var navEl = document.getElementById('navSearchInput');
      var dashEl = document.getElementById('dashboardTopSearchInput');
      prefill = (navEl && navEl.value) || (dashEl && dashEl.value) || '';
    }

    selectScope(scope || _scope);
    input.value = prefill;
    setAdvancedFiltersVisible(false);
    if (getErrorEl()) getErrorEl().classList.add('d-none');

    // The flyout's `top` comes from `--navbar-height` in popularr.css, which
    // main.js keeps in sync with the real navbar. Setting an inline `top` here
    // made this a SECOND source of truth that only updated when the flyout was
    // opened — so a resize or a mobile-menu expand while the flyout was open
    // left it detached from the navbar.
    var backdrop = document.getElementById('searchBackdrop');
    modalEl.classList.remove('d-none');
    if (backdrop) backdrop.classList.remove('d-none');

    var navInput = document.getElementById('navSearchInput');
    if (navInput && navInput !== document.activeElement) navInput.focus();

    if (prefill === _lastQuery && _scope === _lastScope) return;
    runSearch();
  };

  window.closeUnifiedSearch = function () {
    var modalEl = getModalEl();
    if (!modalEl) return;
    modalEl.classList.add('d-none');
    var backdrop = document.getElementById('searchBackdrop');
    if (backdrop) backdrop.classList.add('d-none');
  };

  window.syncNavSearchQuery = function () {
    var navEl = document.getElementById('navSearchInput');
    var dashEl = document.getElementById('dashboardTopSearchInput');
    var input = getInputEl();
    if (!input) return;
    var value = (navEl && navEl.value) || (dashEl && dashEl.value) || '';
    if (input.value !== value) input.value = value;
    if (getErrorEl()) getErrorEl().classList.add('d-none');
  };

  window._renderMbReleaseTrackTable = function (tracks) {
    if (!tracks || !tracks.length) {
      return '<div class="text-muted small px-3 py-2">No tracklist available.</div>';
    }
    var html = '<table class="table table-sm table-hover mb-0 small" data-mobile-cards>' +
      '<thead><tr class="d-none d-md-table-row">' +
      '<th style="width:44px;" class="text-center">#</th>' +
      '<th>Title</th>' +
      '<th style="width:90px;">Duration</th>' +
      '</tr></thead><tbody>';

    var hasDiscs = tracks.some(function (t) { return parseInt(t.disc_number || t.disc || 0, 10) > 1; });

    tracks.forEach(function (t) {
      var num = t.track_number != null ? t.track_number : (t.position != null ? t.position : '');
      var disc = parseInt(t.disc_number || t.disc || 0, 10);
      var numLabel = '';
      if (num !== '') numLabel = hasDiscs ? (disc || 1) + '-' + num : String(num);
      else if (disc > 1) numLabel = 'D' + disc;

      var durRaw = parseInt(t.duration_ms || t.duration || t.length || 0, 10);
      var dur = '';
      if (durRaw > 0) {
        var secs = durRaw >= 10000 ? Math.round(durRaw / 1000) : Math.round(durRaw);
        dur = Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0');
      }

      html += '<tr>' +
        '<td class="text-center text-muted" data-label="#">' + esc(String(numLabel)) + '</td>' +
        '<td data-label="Title">' + esc(t.title || '?') + '</td>' +
        '<td class="text-muted" data-label="Duration">' + esc(dur) + '</td>' +
        '</tr>';
    });

    html += '</tbody></table>';
    return html;
  };

  document.addEventListener('DOMContentLoaded', function () {
    var modalEl = getModalEl();
    var input = getInputEl();
    var resultsEl = getResultsEl();
    if (!modalEl || !input || !resultsEl) return;

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modalEl.classList.contains('d-none')) {
        closeUnifiedSearch();
        input.blur();
      }
    });

    // Input syncs text ONLY — no live searching on typing
    input.addEventListener('input', function () {
      var navEl = document.getElementById('navSearchInput');
      if (navEl && navEl.value !== input.value) navEl.value = input.value;
      var dashEl = document.getElementById('dashboardTopSearchInput');
      if (dashEl && dashEl.value !== input.value) dashEl.value = input.value;
    });

    // Search runs explicitly on Enter
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        runSearch();
        setAdvancedFiltersVisible(false);
      }
    });

    input.addEventListener('focus', function () { this.select(); });

    var filterInputs = document.querySelectorAll('#unifiedAdvancedFilters input, #unifiedSearchType');
    for (var i = 0; i < filterInputs.length; i++) {
      filterInputs[i].addEventListener('focus', function () {
        if (typeof this.select === 'function') this.select();
      });
      filterInputs[i].addEventListener('change', function () {
        runSearch(); // Change triggers search (e.g. drop-down selection)
      });
      filterInputs[i].addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          runSearch();
          setAdvancedFiltersVisible(false);
        }
      });
    }

    var filtersToggle = document.getElementById('unifiedFiltersToggle');
    if (filtersToggle) {
      filtersToggle.addEventListener('click', function () {
        var panel = document.getElementById('unifiedAdvancedFilters');
        // The panel is hidden while it HAS `d-none`, so "should it now be
        // visible?" is simply "does it currently have d-none?".
        //
        // This was:
        //   setAdvancedFiltersVisible(!panel || panel.classList.get ? !panel.classList.contains('d-none') : false);
        // `||` binds tighter than `?:`, so the condition was
        // `(!panel || panel.classList.get)`. DOMTokenList has no `.get`, so
        // with a panel present that evaluated to `undefined` → falsy → the
        // ternary ALWAYS returned `false`. Every click called
        // setAdvancedFiltersVisible(false) and the panel could never open.
        setAdvancedFiltersVisible(panel ? panel.classList.contains('d-none') : false);
      });
    }

    function updateFilterButtonState() {
      var select = document.getElementById('unifiedSearchType');
      var btn = document.getElementById('unifiedFilterBtn');
      var badge = document.getElementById('unifiedFilterBadge');
      if (!select || !btn) return;
      var active = select.value !== '';
      btn.classList.toggle('active', active);
      if (badge) {
        if (active) {
          var opt = select.options[select.selectedIndex];
          badge.textContent = opt ? opt.text : '';
          badge.classList.remove('d-none');
        } else {
          badge.classList.add('d-none');
        }
      }
    }

    function closeUnifiedFilterSheet() {
      var sheet = document.getElementById('usFilterSheet');
      var backdrop = document.getElementById('usFilterBackdrop');
      if (sheet) sheet.remove();
      if (backdrop) backdrop.remove();
    }
    window.closeUnifiedFilterSheet = closeUnifiedFilterSheet;

    function openUnifiedFilterSheet() {
      var select = document.getElementById('unifiedSearchType');
      if (!select) return;
      closeUnifiedFilterSheet();

      var backdrop = document.createElement('div');
      backdrop.className = 'us-filter-backdrop';
      backdrop.id = 'usFilterBackdrop';
      backdrop.addEventListener('click', closeUnifiedFilterSheet);

      var sheet = document.createElement('div');
      sheet.className = 'us-filter-sheet';
      sheet.id = 'usFilterSheet';

      var html = '<div class="us-filter-sheet-header d-flex justify-content-between align-items-center px-3 py-2">' +
        '<strong><i class="bi bi-funnel me-1"></i>Release Type</strong>' +
        '<button type="button" class="btn-close" aria-label="Close" onclick="closeUnifiedFilterSheet()"></button></div>';

      Array.prototype.forEach.call(select.options, function (opt) {
        var value = opt.value || '';
        var active = select.value === value ? ' active' : '';
        html += '<button type="button" class="us-filter-option' + active + '" data-type="' + esc(value) + '">' +
          '<span>' + esc(opt.text) + '</span>' +
          (value === '' ? '' : '<i class="bi bi-check-lg us-filter-check"></i>') +
          '</button>';
      });

      sheet.innerHTML = html;
      document.body.appendChild(backdrop);
      document.body.appendChild(sheet);
      // `.show` drives the slide-in transition defined in popularr.css.
      requestAnimationFrame(function () { sheet.classList.add('show'); });

      sheet.querySelectorAll('.us-filter-option').forEach(function (btn) {
        btn.addEventListener('click', function () {
          select.value = btn.getAttribute('data-type') || '';
          updateFilterButtonState();
          closeUnifiedFilterSheet();
          runSearch();
        });
      });
    }
    window.openUnifiedFilterSheet = openUnifiedFilterSheet;

    var filterBtn = document.getElementById('unifiedFilterBtn');
    if (filterBtn) filterBtn.addEventListener('click', openUnifiedFilterSheet);
    updateFilterButtonState();

    var tabs = document.querySelectorAll('#unifiedScopeTabs .nav-link');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function () {
        selectScope(this.getAttribute('data-scope'));
        runSearch();
      });
    }

    resultsEl.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.us-queue-btn') : null;
      if (!btn) return;
      e.preventDefault();
      var rel = _mbIndex[btn.getAttribute('data-mbid')];
      if (rel) queueRelease(rel, btn);
    });
  });
})();
