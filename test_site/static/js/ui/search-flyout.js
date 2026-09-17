/* ==========================================================================
   static/js/ui/search-flyout.js
   Unified hybrid search — All / In Library / MusicBrainz.
   Typing syncs the input only; searches run on Enter or a filter change.

   Load order:
       utils/dom.js  →  utils/api.js  →  ui/toast.js
       unified_search.js

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. THE MUSICBRAINZ COUNT BADGE RESET ITSELF TO ZERO ON THE LIBRARY TAB.
      On SCOPE_LIBRARY the MB promise was built as:

          fetchMb(...).then(r => { _counts.mb = r.length; updateScopeCounts();
                                   return []; })      // <- resolves EMPTY

      and the shared `.then` that follows did:

          _counts.mb = mbReleases.length;             // <- always 0 here

      without calling updateScopeCounts(). So the real count was written and
      painted, then immediately overwritten with 0. Whether the user saw the
      right number came down to a race: if the library request resolved
      AFTER the MB one, renderLocal() called updateScopeCounts() again and
      repainted the badge as 0. The count is now carried on the resolved
      value instead of via a side effect, so there is one write and one
      paint.

   2. THE ADVANCED-FILTERS PANEL COULD NEVER OPEN.
          setAdvancedFiltersVisible(!panel || panel.classList.get
            ? !panel.classList.contains('d-none') : false);
      `||` binds tighter than `?:`, so the condition was
      `(!panel || panel.classList.get)`. DOMTokenList has no `.get` method,
      so with a panel present that evaluated to `undefined` — falsy — and the
      ternary ALWAYS produced `false`. Every click called
      setAdvancedFiltersVisible(false).
      (This was already annotated in the file; the corrected form is kept.)

   3. `_mbIndex` GREW WITHOUT BOUND. Every rendered MusicBrainz release was
      cached into it and nothing was ever removed, so a long session held
      every release object from every search. It is now cleared per run.

   4. `hasAdvanced` WAS COMPUTED TWO DIFFERENT WAYS. runSearch() used
      hasAnyAdvancedFilter(adv) — driven by ADVANCED_FILTER_KEYS — while
      fetchMb() re-derived it from its own hand-written OR chain. Both now
      use the one helper, so adding a filter key cannot leave one of them
      behind. (The previous fix to forward year_to/genre is preserved.)

   NOTE ON esc(): the local implementation is replaced by utils/dom.js's
   escapeHtml, which escapes &, <, >, " and ' — the same five entities.
   ========================================================================== */

(function (global) {
  'use strict';

  const SCOPE_ALL = 'all';
  const SCOPE_LIBRARY = 'library';
  const SCOPE_MB = 'mb';
  const MB_LIMIT_ALL_TAB = 12;
  const MB_LIMIT_MB_TAB = 25;
  const MIN_QUERY_LENGTH = 2;
  const SECTION_INLINE_LIMIT = 5;

  let _scope = SCOPE_ALL;
  let _runSeq = 0;
  const _queuedIds = Object.create(null);
  let _mbIndex = Object.create(null);
  const _counts = { all: 0, library: 0, mb: 0 };
  let _lastQuery = null;
  let _lastScope = null;
  let _mbDone = false;
  let _mbResults = [];

  function emptyLocalResult() {
    return {
      artists: [], albums: [], compilations: [], live_albums: [],
      eps: [], singles: [], tracks: [],
    };
  }

  let localResult = emptyLocalResult();

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  const getModalEl = () => document.getElementById('unifiedSearchModal');
  const getInputEl = () => document.getElementById('unifiedSearchInput');
  const getResultsEl = () => document.getElementById('unifiedSearchResults');
  const getMetaEl = () => document.getElementById('usResultMeta');
  const getErrorEl = () => document.getElementById('unifiedSearchError');

  // ── Counts & scope ──────────────────────────────────────────────────────

  function countLibrary(local) {
    const albumBuckets = (local.albums || []).length + (local.compilations || []).length
      + (local.live_albums || []).length + (local.eps || []).length + (local.singles || []).length;
    return (local.artists || []).length + albumBuckets + (local.tracks || []).length;
  }

  function updateScopeCounts() {
    const labels = { all: _counts.all, library: _counts.library, mb: _counts.mb };
    document.querySelectorAll('#unifiedScopeTabs .nav-link').forEach((tab) => {
      const span = tab.querySelector('.us-scope-count');
      if (!span) return;
      const n = labels[tab.getAttribute('data-scope')] || 0;
      span.textContent = n > 0 ? String(n) : '';
      span.classList.toggle('d-none', n <= 0);
    });
  }

  function markRendered(query) {
    _lastQuery = query;
    _lastScope = _scope;
  }

  function selectScope(scope) {
    _scope = (scope === SCOPE_MB || scope === SCOPE_LIBRARY) ? scope : SCOPE_ALL;
    document.querySelectorAll('#unifiedScopeTabs .nav-link').forEach((tab) => {
      const active = tab.getAttribute('data-scope') === _scope;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
  }

  // ── Filters ─────────────────────────────────────────────────────────────

  function getTypeFilter() {
    const el = document.getElementById('unifiedSearchType');
    return el ? el.value : '';
  }

  /**
   * Every key returned by getAdvancedFilters(). Deriving `hasAdvanced` and
   * the fetchMb payload from ONE list stops them drifting apart: hasAdvanced
   * previously tested only artist/album/track/year and the payload forwarded
   * only those four, so Year-To and Genre were read and silently dropped.
   */
  const ADVANCED_FILTER_KEYS = ['artist', 'album', 'track', 'year', 'year_to', 'genre'];

  function getAdvancedFilters() {
    const val = (id) => {
      const el = document.getElementById(id);
      return el ? el.value.trim() : '';
    };
    return {
      artist: val('unifiedFilterArtist'),
      album: val('unifiedFilterAlbum'),
      track: val('unifiedFilterTrack'),
      year: val('unifiedFilterYear'),
      year_to: val('unifiedFilterYearTo'),
      genre: val('unifiedFilterGenre'),
    };
  }

  function hasAnyAdvancedFilter(adv) {
    return ADVANCED_FILTER_KEYS.some((key) => !!adv[key]);
  }

  // ── Fetching ────────────────────────────────────────────────────────────

  function fetchLibrary(query) {
    return global.api.postJson('/api/search', { query });
  }

  /**
   * Search MusicBrainz.
   * @returns {Promise<Array>} releases, capped at `limit`. Never rejects.
   */
  function fetchMb(query, limit, opts) {
    const options = opts || {};
    // One source of truth — see bug 4.
    const hasAdvanced = hasAnyAdvancedFilter(options);

    if (!hasAdvanced && !options.type
        && typeof global.searchMusicBrainzReleases === 'function') {
      return global.searchMusicBrainzReleases(query, '', limit)
        .then((data) => data.releases || [])
        .catch(() => []);
    }

    const payload = {};
    if (hasAdvanced) {
      ADVANCED_FILTER_KEYS.forEach((key) => { payload[key] = options[key] || ''; });
      // Artist-only needs the server's artist strategy, not a text search.
      const onlyArtist = options.artist
        && !ADVANCED_FILTER_KEYS.filter((k) => k !== 'artist').some((k) => options[k]);
      if (onlyArtist) payload.artist_only = true;
    } else {
      payload.query = query;
    }
    if (options.type) payload.type = options.type;

    return global.api.postJson('/api/musicbrainz/search', payload)
      .then((data) => (data.releases || []).slice(0, limit))
      .catch(() => []);
  }

  // ── Row builders ────────────────────────────────────────────────────────

  const IMG_PLACEHOLDER =
    'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2244%22 height=%2244%22%3E%3Crect fill=%22%232a2a2a%22 width=%2244%22 height=%2244%22/%3E%3C/svg%3E';

  /**
   * Build a 44px thumbnail.
   *
   * There is deliberately no inline `onerror` attribute here — the fallback
   * is a single delegated capture-phase listener registered in init() (see
   * `resultsEl.addEventListener('error', …, true)`). One listener replaces
   * an attribute on every row, and `error` events do not bubble, which is
   * why that listener must use capture.
   */
  function thumbHtml(src, cls, alt) {
    const source = src || IMG_PLACEHOLDER;
    return `<img src="${esc(source)}" alt="${esc(alt || '')}" loading="lazy" ` +
      `class="${esc(cls)} flex-shrink-0 us-thumb" style="width:44px;height:44px;object-fit:cover;">`;
  }

  function thumbWithBadge(thumb, badgeIcon, badgeClass) {
    return `<span class="us-thumb-wrap">${thumb}` +
      `<span class="us-thumb-badge ${esc(badgeClass)}">${badgeIcon}</span></span>`;
  }

  function fmtDuration(seconds) {
    const s = Number(seconds);
    if (!isFinite(s) || s <= 0) return '';
    const m = Math.round(s / 60);
    if (m >= 60) return `${Math.floor(m / 60)}h ${m % 60}m`;
    return `${m}m`;
  }

  function buildSection(title, items, rower) {
    const count = items.length;
    const visible = items.slice(0, SECTION_INLINE_LIMIT);
    const hidden = items.slice(SECTION_INLINE_LIMIT);
    const toggle = hidden.length
      ? `<button type="button" class="btn btn-sm btn-link us-section-toggle py-0 text-decoration-none" ` +
        `data-count="${count}">Show All ${count} <i class="bi bi-chevron-down"></i></button>`
      : '';

    return '<div class="us-section mb-1">' +
      `<div class="us-section-title d-flex align-items-center gap-2">${title}${toggle}</div>` +
      rower(visible) +
      (hidden.length ? `<div class="us-section-more d-none">${rower(hidden)}</div>` : '') +
      '</div>';
  }

  function toggleSection(btn) {
    const section = btn.closest('.us-section');
    const hiddenEl = section && section.querySelector('.us-section-more');
    if (!hiddenEl) return;
    const expanded = !hiddenEl.classList.contains('d-none');
    hiddenEl.classList.toggle('d-none', expanded);
    btn.innerHTML = expanded
      ? `Show All ${esc(btn.dataset.count || '')} <i class="bi bi-chevron-down"></i>`
      : 'Show Less <i class="bi bi-chevron-up"></i>';
  }

  function filterLocalByType(local, type) {
    if (!type) return local;
    const bucketMap = {
      album: ['albums'],
      single: ['singles'],
      ep: ['eps'],
      compilation: ['compilations'],
      live: ['live_albums'],
      soundtrack: ['albums', 'compilations'],
      remix: ['albums'],
    };
    const keep = bucketMap[type] || [];
    const out = emptyLocalResult();
    keep.forEach((k) => { out[k] = local[k] || []; });
    return out;
  }

  function artistRows(artists) {
    return artists.map((a) => {
      const isVarious = String(a.name || '').trim().toLowerCase() === 'various artists';
      return `<a class="us-row" href="/artist/${encodeURIComponent(a.name)}">` +
        thumbHtml('/api/artist/image?name=' + encodeURIComponent(a.name),
          'rounded-circle border border-secondary', a.name) +
        '<span class="us-row-main">' +
          `<span class="us-row-title d-block">${esc(a.name)}</span>` +
          `<span class="us-row-sub d-block">${a.track_count} track${a.track_count === 1 ? '' : 's'}` +
          ` - ${a.album_count} album${a.album_count === 1 ? '' : 's'}</span>` +
        '</span>' +
        '<span class="us-row-meta badge bg-secondary-subtle text-secondary-emphasis">Artist</span>' +
        (isVarious
          ? '<span class="us-row-meta badge bg-secondary ms-1" title="Compilation placeholder entity">Compilation</span>'
          : '') +
      '</a>';
    }).join('');
  }

  function trackRows(tracks) {
    return tracks.map((t) =>
      `<a class="us-row" href="/track/${encodeURIComponent(t.id)}">` +
        '<span class="us-row-icon"><i class="bi bi-music-note"></i></span>' +
        '<span class="us-row-main">' +
          `<span class="us-row-title d-block">${esc(t.title)}</span>` +
          `<span class="us-row-sub d-block">${esc(t.artist)}${t.album ? ' - ' + esc(t.album) : ''}</span>` +
        '</span>' +
        (t.stars
          ? `<span class="us-row-meta text-warning"><i class="bi bi-star-fill"></i> ${esc(t.stars)}</span>`
          : '') +
      '</a>'
    ).join('');
  }

  function mbReleaseArtist(r) {
    if (r.artist) return r.artist;
    if (r['artist-credit']) {
      return r['artist-credit']
        .map((a) => (typeof a === 'string' ? a : (a.name || a.artist || '')))
        .join(', ');
    }
    return 'Unknown Artist';
  }

  function mbReleaseYear(r) {
    return String(r.first_release_date || r.date || r.year || '').split('-')[0] || '?';
  }

  const SECTION_DEFS = [
    { key: 'albums', label: 'Albums', localLabel: 'Studio Album' },
    { key: 'compilations', label: 'Compilations', localLabel: 'Compilation' },
    { key: 'live_albums', label: 'Live Albums', localLabel: 'Live Album' },
    { key: 'eps', label: 'EPs', localLabel: 'EP' },
    { key: 'singles', label: 'Singles', localLabel: 'Single' },
  ];

  function classifyMbRelease(r) {
    const secondary = (r.secondary_types || []).map((s) => String(s).toLowerCase());
    if (secondary.indexOf('live') !== -1) return 'live_albums';
    if (secondary.indexOf('compilation') !== -1) return 'compilations';
    if (secondary.indexOf('ep') !== -1) return 'eps';
    if (secondary.indexOf('single') !== -1) return 'singles';
    const pt = String(r.category || r.primary_type || '').toLowerCase();
    if (pt === 'ep') return 'eps';
    if (pt === 'single') return 'singles';
    return 'albums';
  }

  function bucketTypeLabel(bucketKey) {
    const def = SECTION_DEFS.find((d) => d.key === bucketKey);
    return def ? def.localLabel : 'Release';
  }

  function releaseRows(items, withQueue) {
    return items.map((it) => {
      const yearSuffix = it.year ? ` (${esc(it.year)})` : '';
      const trackLine = it.track_count
        ? `<span class="us-row-sub d-block">${it.track_count} track${it.track_count === 1 ? '' : 's'}` +
          `${it.duration_total ? ' - ' + fmtDuration(it.duration_total) : ''}</span>`
        : '';

      if (it.local || it.owned) {
        const yearPath = it.year ? '/' + it.year : '';
        const href = `/album/${encodeURIComponent(it.artist)}/${encodeURIComponent(it.title)}${yearPath}`;
        const localArt = `/api/album/${encodeURIComponent(it.artist)}/${encodeURIComponent(it.title)}/art`;
        return `<a class="us-row" href="${href}">` +
          thumbWithBadge(thumbHtml(localArt, 'rounded border border-secondary', it.title),
            '<i class="bi bi-music-note-fill"></i>', 'accent-library-bg') +
          '<span class="us-row-main">' +
            `<span class="us-row-title d-block">${esc(it.title)}${yearSuffix}</span>` +
            `<span class="us-row-sub d-block"><span class="us-artist">By ${esc(it.artist)}</span> · ` +
            `<span class="us-type">${esc(it.typeLabel || 'Album')}</span></span>` +
            trackLine +
          '</span>' +
          '<span class="us-row-action btn btn-sm btn-outline-secondary" title="View in library">' +
            '<i class="bi bi-box-arrow-up-right"></i></span>' +
        '</a>';
      }

      const id = it.id || '';
      const queued = !!_queuedIds[id];
      _mbIndex[id] = it.release || null;
      const rel = it.release || {};
      const cover = rel.cover_art_url
        || (id ? `https://coverartarchive.org/release-group/${encodeURIComponent(id)}/front-250` : '');
      const mbUrl = id
        ? `https://musicbrainz.org/release-group/${encodeURIComponent(id)}`
        : '#';

      return `<a class="us-row" href="${mbUrl}" target="_blank" rel="noopener noreferrer">` +
        thumbWithBadge(thumbHtml(cover, 'rounded border border-secondary', it.title),
          '<i class="bi bi-hexagon-fill"></i>', 'accent-mb-bg') +
        '<span class="us-row-main">' +
          `<span class="us-row-title d-block">${esc(it.title)}${yearSuffix}</span>` +
          `<span class="us-row-sub d-block"><span class="us-artist">By ${esc(it.artist)}</span> · ` +
          `<span class="us-type">${esc(it.typeLabel || 'Release')}</span></span>` +
          trackLine +
        '</span>' +
        (withQueue
          ? (queued
            ? '<button class="btn btn-sm btn-success" disabled title="Already queued"><i class="bi bi-check2"></i> Queued</button>'
            : `<button class="btn btn-sm btn-outline-primary us-queue-btn" data-mbid="${esc(id)}" ` +
              'title="Queue download via Soulseek"><i class="bi bi-download"></i> Queue</button>')
          : '') +
      '</a>';
    }).join('');
  }

  function ownedKey(artist, title) {
    const norm = (s) => String(s || '').toLowerCase()
      .replace(/\([^)]*\)/g, ' ').replace(/\s+/g, ' ').trim();
    return norm(artist) + '::' + norm(title);
  }

  function buildBuckets(local, mbReleases) {
    const buckets = { albums: [], compilations: [], live_albums: [], eps: [], singles: [] };
    const owned = Object.create(null);

    (local.albums || []).forEach((al) => {
      const bucket = buckets[al.type] ? al.type : 'albums';
      buckets[bucket].push({
        title: al.album, artist: al.artist, year: al.year || null,
        typeLabel: al.type_label || 'Album', local: true,
        track_count: al.track_count || null, duration_total: al.duration_total || null,
      });
      owned[ownedKey(al.artist, al.album)] = true;
    });

    (mbReleases || []).forEach((r) => {
      const artist = mbReleaseArtist(r);
      const bucket = classifyMbRelease(r);
      buckets[bucket].push({
        title: r.title, artist, year: mbReleaseYear(r),
        typeLabel: bucketTypeLabel(bucket), local: false, id: r.id || '', release: r,
        track_count: r.track_count || null,
        owned: !!owned[ownedKey(artist, r.title)],
      });
    });

    Object.keys(buckets).forEach((k) => {
      buckets[k].sort((a, b) => {
        const ay = (a.year === '?' || a.year == null) ? 0 : Number(a.year) || 0;
        const by = (b.year === '?' || b.year == null) ? 0 : Number(b.year) || 0;
        if ((ay > 0) !== (by > 0)) return ay > 0 ? -1 : 1;
        if (ay !== by) return by - ay;
        if (a.local !== b.local) return a.local ? -1 : 1;
        return String(a.title || '').toLowerCase() < String(b.title || '').toLowerCase() ? -1 : 1;
      });
    });

    return buckets;
  }

  function renderReleaseSections(buckets, withQueue) {
    return SECTION_DEFS.map((def) => {
      const items = buckets[def.key] || [];
      if (!items.length) return '';
      return buildSection(
        `${def.label} <span class="badge bg-secondary ms-1">${items.length}</span>`,
        items,
        (list) => releaseRows(list, withQueue)
      );
    }).join('');
  }

  function renderBucketedResults(local, mbReleases, query, opts) {
    const options = opts || {};
    let html = '';

    const artists = local.artists || [];
    if (artists.length) html += buildSection('Artists', artists, artistRows);
    html += renderReleaseSections(buildBuckets(local, mbReleases), options.withQueue);

    if (options.allMbButton && options.mbPending) {
      html += '<div class="text-center my-2 text-muted small">' +
        '<span class="spinner-border spinner-border-sm me-1" role="status"></span> ' +
        'Loading MusicBrainz results…</div>';
    } else if (options.allMbButton && mbReleases.length) {
      html += '<div class="text-center my-2">' +
        '<button type="button" class="btn btn-sm btn-outline-info us-all-mb-btn">' +
        '<i class="bi bi-search"></i> All MusicBrainz results</button></div>';
    }

    const tracks = local.tracks || [];
    if (tracks.length) html += buildSection('Tracks', tracks, trackRows);

    if (!artists.length && !html) {
      html += `<div class="text-center text-muted py-4 small">No library matches for "${esc(query)}"</div>`;
    }
    return html;
  }

  function renderMbTab(local, releases, query) {
    if (!releases.length) {
      return '<div class="text-center text-muted py-4"><i class="bi bi-hexagon" style="font-size:2rem;"></i>' +
        `<p class="mt-2 mb-0 small">No MusicBrainz releases found for "${esc(query)}"</p></div>`;
    }
    return renderReleaseSections(buildBuckets(local || {}, releases), true);
  }

  // ── Search ──────────────────────────────────────────────────────────────

  function runSearch() {
    const input = getInputEl();
    const resultsEl = getResultsEl();
    if (!input || !resultsEl) return;

    const query = input.value.trim();
    const adv = getAdvancedFilters();
    const hasAdvanced = hasAnyAdvancedFilter(adv);
    const seq = ++_runSeq;

    if (query.length < MIN_QUERY_LENGTH && !hasAdvanced) {
      resultsEl.innerHTML = '<div class="text-center text-muted py-5">' +
        '<i class="bi bi-search" style="font-size:2rem;"></i>' +
        `<p class="mt-2 mb-0 small">Type at least ${MIN_QUERY_LENGTH} characters and press Enter</p></div>`;
      if (getMetaEl()) getMetaEl().textContent = '';
      markRendered(query);
      return;
    }

    resultsEl.innerHTML = '<div class="text-center py-5">' +
      '<div class="spinner-border text-primary" role="status">' +
      '<span class="visually-hidden">Loading…</span></div></div>';

    // Forward EVERY advanced filter — year_to and genre were previously read
    // and then dropped before the request was built.
    const mbOpts = { type: getTypeFilter() };
    ADVANCED_FILTER_KEYS.forEach((key) => { mbOpts[key] = adv[key]; });

    let localQuery = query;
    if (localQuery.length < MIN_QUERY_LENGTH && hasAdvanced) {
      localQuery = adv.artist || adv.album || adv.track || adv.year || adv.genre || '';
    }

    // Reset per-run state. Both are module-level, so without this an
    // in-flight promise could render the PREVIOUS query's results, and
    // _mbIndex would accumulate every release object ever rendered.
    localResult = emptyLocalResult();
    _mbIndex = Object.create(null);
    _mbDone = false;
    _mbResults = [];

    // Tracks whether the library response has arrived yet, so the two
    // promises below can settle in either order without rendering a
    // misleading intermediate state. See the notes on each `.then`.
    let localSettled = false;

    const localPromise = localQuery.length >= MIN_QUERY_LENGTH
      ? fetchLibrary(localQuery).catch((e) => ({ error: e.message }))
      : Promise.resolve(emptyLocalResult());

    // One MB request in every scope. The count travels on the resolved
    // value; nothing writes _counts.mb as a side effect any more — see bug 1.
    const mbLimit = _scope === SCOPE_ALL ? MB_LIMIT_ALL_TAB : MB_LIMIT_MB_TAB;
    const mbPromise = fetchMb(query, mbLimit, mbOpts);

    function renderLocal(local) {
      if (seq !== _runSeq) return;

      const typeFilter = getTypeFilter();
      const filtered = typeFilter ? filterLocalByType(local, typeFilter) : local;

      _counts.library = countLibrary(filtered);
      _counts.all = _counts.library + (_counts.mb || 0);
      updateScopeCounts();
      if (_scope === SCOPE_MB) return;

      const displayQuery = localQuery || query;
      const releaseCount = ['albums', 'compilations', 'live_albums', 'eps', 'singles']
        .reduce((n, k) => n + ((filtered[k] || []).length), 0);

      const counts = [
        `${(filtered.artists || []).length} artist${(filtered.artists || []).length === 1 ? '' : 's'}`,
        `${releaseCount} album${releaseCount === 1 ? '' : 's'}`,
        `${(filtered.tracks || []).length} track${(filtered.tracks || []).length === 1 ? '' : 's'}`,
      ];
      if (_scope === SCOPE_ALL) counts.push(`${_counts.mb || 0} musicbrainz`);
      if (getMetaEl()) getMetaEl().textContent = counts.join(' · ');

      const warnHtml = filtered.error
        ? '<div class="alert alert-warning py-2 small mb-2"><i class="bi bi-exclamation-triangle-fill"></i> ' +
          `Library search failed (${esc(filtered.error)}) — showing MusicBrainz only.</div>`
        : '';

      // On the Library tab, MB results inform the count badge but are not
      // rendered into the list.
      const mbForRender = _scope === SCOPE_LIBRARY ? [] : (_mbResults || []);

      resultsEl.innerHTML = warnHtml + renderBucketedResults(filtered, mbForRender, displayQuery, {
        withQueue: true,
        allMbButton: _scope === SCOPE_ALL,
        mbPending: _scope === SCOPE_ALL && !_mbDone,
      });
      markRendered(query);
    }

    mbPromise.then((mbReleases) => {
      if (seq !== _runSeq) return;
      _mbResults = mbReleases || [];
      _mbDone = true;
      _counts.mb = _mbResults.length;
      _counts.all = _counts.library + _counts.mb;
      updateScopeCounts();

      if (_scope === SCOPE_MB) {
        renderMbScope();
        return;
      }

      // Only render from here once the library response is in. Rendering
      // with a still-empty localResult briefly paints "No library matches
      // for X" — or drops the local albums out of the merged list — and
      // then repaints a moment later when the library request lands.
      // Whether the user saw that flash was pure request-timing luck.
      if (localSettled) renderLocal(localResult);
    });

    localPromise.then((local) => {
      if (seq !== _runSeq) return;
      localResult = local;
      localSettled = true;

      // The MB tab marks releases already in the library with an "owned"
      // badge, and that marking is derived from localResult. If the MB
      // request resolved FIRST, the tab was rendered before the library
      // data existed and every badge was missing until the next search.
      if (_scope === SCOPE_MB) {
        if (_mbDone) renderMbScope();
        return;
      }
      renderLocal(local);
    });

    /** Render the MusicBrainz tab from whatever data has arrived. */
    function renderMbScope() {
      if (getMetaEl()) {
        getMetaEl().textContent =
          `${_mbResults.length} musicbrainz result${_mbResults.length === 1 ? '' : 's'}`;
      }
      resultsEl.innerHTML = renderMbTab(localResult, _mbResults, localQuery || query);
      markRendered(query);
    }
  }

  // ── Queueing ────────────────────────────────────────────────────────────

  function notifyError(message) {
    const errEl = getErrorEl();
    if (!errEl) {
      if (global.toast) global.toast.error(message);
      return;
    }
    errEl.textContent = message;
    errEl.classList.remove('d-none');
    clearTimeout(notifyError._timer);
    notifyError._timer = setTimeout(() => errEl.classList.add('d-none'), 5000);
  }

  function markQueued(btn) {
    btn.classList.replace('btn-outline-primary', 'btn-success');
    btn.innerHTML = '<i class="bi bi-check2"></i> Queued';
    btn.disabled = true;
  }

  async function queueRelease(rel, btn) {
    const id = rel.id || '';
    if (!id || _queuedIds[id]) return;
    const artist = mbReleaseArtist(rel);

    if (typeof global.openReleasePicker === 'function') {
      global.openReleasePicker(id, rel.title || '', artist, function () {
        _queuedIds[id] = true;
        markQueued(btn);
      });
      return;
    }

    _queuedIds[id] = true;
    try {
      await global.buttonState.withBusy(btn, '', async () => {
        await global.api.postJson('/api/musicbrainz/download', {
          release_id: id,
          release_title: rel.title || '',
          artist,
          method: 'slskd',
          queue_items_only: true,
        });
      });
      markQueued(btn);
      if (global.toast) global.toast.queued(rel.title || 'Release');
    } catch (error) {
      // Delete rather than set false, so the key cannot linger as a
      // permanently-falsy entry in the map.
      delete _queuedIds[id];
      notifyError('Queue failed: ' + error.message);
    }
  }

  // ── Panel / flyout ──────────────────────────────────────────────────────

  function setAdvancedFiltersVisible(visible) {
    const panel = document.getElementById('unifiedAdvancedFilters');
    const toggle = document.getElementById('unifiedFiltersToggle');
    const icon = document.getElementById('unifiedFiltersToggleIcon');
    if (!panel) return;
    panel.classList.toggle('d-none', !visible);
    if (icon) icon.className = 'bi bi-chevron-' + (visible ? 'up' : 'right');
    if (toggle) toggle.setAttribute('aria-expanded', visible ? 'true' : 'false');
  }

  function openUnifiedSearch(scope, prefill) {
    const modalEl = getModalEl();
    const input = getInputEl();
    if (!modalEl || !input) return;

    let value = prefill;
    if (value === undefined || value === null) {
      const navEl = document.getElementById('navSearchInput');
      const dashEl = document.getElementById('dashboardTopSearchInput');
      value = (navEl && navEl.value) || (dashEl && dashEl.value) || '';
    }

    selectScope(scope || _scope);
    input.value = value;
    setAdvancedFiltersVisible(false);
    if (getErrorEl()) getErrorEl().classList.add('d-none');

    // The flyout's `top` comes from --navbar-height in popularr.css, which
    // main.js keeps in sync with the real navbar. Setting an inline `top`
    // here would make this a SECOND source of truth that only updated on
    // open — so a resize or mobile-menu expand while the flyout was open
    // would leave it detached from the navbar.
    const backdrop = document.getElementById('searchBackdrop');
    modalEl.classList.remove('d-none');
    if (backdrop) backdrop.classList.remove('d-none');

    const navInput = document.getElementById('navSearchInput');
    if (navInput && navInput !== document.activeElement) navInput.focus();

    if (value === _lastQuery && _scope === _lastScope) return;
    runSearch();
  }

  function closeUnifiedSearch() {
    const modalEl = getModalEl();
    if (!modalEl) return;
    modalEl.classList.add('d-none');
    const backdrop = document.getElementById('searchBackdrop');
    if (backdrop) backdrop.classList.add('d-none');
  }

  function syncNavSearchQuery() {
    const navEl = document.getElementById('navSearchInput');
    const dashEl = document.getElementById('dashboardTopSearchInput');
    const input = getInputEl();
    if (!input) return;
    const value = (navEl && navEl.value) || (dashEl && dashEl.value) || '';
    if (input.value !== value) input.value = value;
    if (getErrorEl()) getErrorEl().classList.add('d-none');
  }

  // ── MusicBrainz tracklist table (used by the release picker) ────────────

  function renderMbReleaseTrackTable(tracks) {
    if (!tracks || !tracks.length) {
      return '<div class="text-muted small px-3 py-2">No tracklist available.</div>';
    }

    const hasDiscs = tracks.some((t) => parseInt(t.disc_number || t.disc || 0, 10) > 1);

    const rows = tracks.map((t) => {
      const num = t.track_number != null ? t.track_number
        : (t.position != null ? t.position : '');
      const disc = parseInt(t.disc_number || t.disc || 0, 10);

      let numLabel = '';
      if (num !== '') numLabel = hasDiscs ? `${disc || 1}-${num}` : String(num);
      else if (disc > 1) numLabel = 'D' + disc;

      const durRaw = parseInt(t.duration_ms || t.duration || t.length || 0, 10);
      let dur = '';
      if (durRaw > 0) {
        // >= 10000 is treated as milliseconds; below that, seconds.
        const secs = durRaw >= 10000 ? Math.round(durRaw / 1000) : Math.round(durRaw);
        dur = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
      }

      return '<tr>' +
        `<td class="text-center text-muted" data-label="#">${esc(String(numLabel))}</td>` +
        `<td data-label="Title">${esc(t.title || '?')}</td>` +
        `<td class="text-muted" data-label="Duration">${esc(dur)}</td>` +
        '</tr>';
    }).join('');

    return '<table class="table table-sm table-hover mb-0 small" data-mobile-cards>' +
      '<thead><tr class="d-none d-md-table-row">' +
      '<th style="width:44px;" class="text-center">#</th><th>Title</th>' +
      '<th style="width:90px;">Duration</th></tr></thead>' +
      `<tbody>${rows}</tbody></table>`;
  }

  // ── Filter sheet (mobile) ───────────────────────────────────────────────

  function updateFilterButtonState() {
    const select = document.getElementById('unifiedSearchType');
    const btn = document.getElementById('unifiedFilterBtn');
    const badge = document.getElementById('unifiedFilterBadge');
    if (!select || !btn) return;

    const active = select.value !== '';
    btn.classList.toggle('active', active);
    if (!badge) return;

    if (active) {
      const opt = select.options[select.selectedIndex];
      badge.textContent = opt ? opt.text : '';
      badge.classList.remove('d-none');
    } else {
      badge.classList.add('d-none');
    }
  }

  function closeUnifiedFilterSheet() {
    const sheet = document.getElementById('usFilterSheet');
    const backdrop = document.getElementById('usFilterBackdrop');
    if (sheet) sheet.remove();
    if (backdrop) backdrop.remove();
  }

  function openUnifiedFilterSheet() {
    const select = document.getElementById('unifiedSearchType');
    if (!select) return;
    closeUnifiedFilterSheet();

    const backdrop = document.createElement('div');
    backdrop.className = 'us-filter-backdrop';
    backdrop.id = 'usFilterBackdrop';
    backdrop.addEventListener('click', closeUnifiedFilterSheet);

    const sheet = document.createElement('div');
    sheet.className = 'us-filter-sheet';
    sheet.id = 'usFilterSheet';

    let html = '<div class="us-filter-sheet-header d-flex justify-content-between align-items-center px-3 py-2">' +
      '<strong><i class="bi bi-funnel me-1"></i>Release Type</strong>' +
      '<button type="button" class="btn-close" aria-label="Close" data-us-sheet-close></button></div>';

    Array.prototype.forEach.call(select.options, (opt) => {
      const value = opt.value || '';
      const active = select.value === value ? ' active' : '';
      html += `<button type="button" class="us-filter-option${active}" data-type="${esc(value)}">` +
        `<span>${esc(opt.text)}</span>` +
        (value === '' ? '' : '<i class="bi bi-check-lg us-filter-check"></i>') +
        '</button>';
    });

    sheet.innerHTML = html;
    document.body.appendChild(backdrop);
    document.body.appendChild(sheet);

    // `.show` drives the slide-in transition defined in popularr.css.
    requestAnimationFrame(() => sheet.classList.add('show'));

    const closeBtn = sheet.querySelector('[data-us-sheet-close]');
    if (closeBtn) closeBtn.addEventListener('click', closeUnifiedFilterSheet);

    sheet.querySelectorAll('.us-filter-option').forEach((btn) => {
      btn.addEventListener('click', function () {
        select.value = this.getAttribute('data-type') || '';
        updateFilterButtonState();
        closeUnifiedFilterSheet();
        runSearch();
      });
    });
  }

  // ── Init ────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    const modalEl = getModalEl();
    const input = getInputEl();
    const resultsEl = getResultsEl();
    if (!modalEl || !input || !resultsEl) return;

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modalEl.classList.contains('d-none')) {
        closeUnifiedSearch();
        input.blur();
      }
    });

    // Typing syncs text only — no live searching.
    input.addEventListener('input', function () {
      const navEl = document.getElementById('navSearchInput');
      if (navEl && navEl.value !== input.value) navEl.value = input.value;
      const dashEl = document.getElementById('dashboardTopSearchInput');
      if (dashEl && dashEl.value !== input.value) dashEl.value = input.value;
    });

    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      runSearch();
      setAdvancedFiltersVisible(false);
    });

    input.addEventListener('focus', function () { this.select(); });

    document.querySelectorAll('#unifiedAdvancedFilters input, #unifiedSearchType')
      .forEach((el) => {
        el.addEventListener('focus', function () {
          if (typeof this.select === 'function') this.select();
        });
        el.addEventListener('change', runSearch);
        el.addEventListener('keydown', function (e) {
          if (e.key !== 'Enter') return;
          e.preventDefault();
          runSearch();
          setAdvancedFiltersVisible(false);
        });
      });

    const filtersToggle = document.getElementById('unifiedFiltersToggle');
    if (filtersToggle) {
      filtersToggle.addEventListener('click', function () {
        // The panel is hidden while it HAS d-none, so "should it now be
        // visible?" is simply "does it currently have d-none?".
        const panel = document.getElementById('unifiedAdvancedFilters');
        setAdvancedFiltersVisible(panel ? panel.classList.contains('d-none') : false);
      });
    }

    const filterBtn = document.getElementById('unifiedFilterBtn');
    if (filterBtn) filterBtn.addEventListener('click', openUnifiedFilterSheet);
    updateFilterButtonState();

    document.querySelectorAll('#unifiedScopeTabs .nav-link').forEach((tab) => {
      tab.addEventListener('click', function () {
        selectScope(this.getAttribute('data-scope'));
        runSearch();
      });
    });

    // Broken-artwork fallback. The old markup carried an inline
    // onerror="this.onerror=null;this.src='…'" on every <img>; this replaces
    // all of them with one listener.
    //
    // CAPTURE IS REQUIRED: `error` events from <img> do not bubble, so a
    // normal (bubbling) listener on the container would never fire. The
    // src check is the equivalent of the old `this.onerror = null` guard —
    // without it, a placeholder that itself failed would loop.
    resultsEl.addEventListener('error', function (e) {
      const img = e.target;
      if (!img || img.tagName !== 'IMG') return;
      if (!img.classList.contains('us-thumb')) return;
      if (img.getAttribute('src') === IMG_PLACEHOLDER) return;
      img.setAttribute('src', IMG_PLACEHOLDER);
    }, true);

    // One delegated listener covers the queue buttons, the section
    // expanders and the "All MusicBrainz results" button, all of which are
    // re-rendered on every search.
    resultsEl.addEventListener('click', function (e) {
      if (!e.target.closest) return;

      const queueBtn = e.target.closest('.us-queue-btn');
      if (queueBtn) {
        e.preventDefault();
        const rel = _mbIndex[queueBtn.getAttribute('data-mbid')];
        if (rel) queueRelease(rel, queueBtn);
        return;
      }

      const toggle = e.target.closest('.us-section-toggle');
      if (toggle) {
        e.preventDefault();
        toggleSection(toggle);
        return;
      }

      const allMb = e.target.closest('.us-all-mb-btn');
      if (allMb) {
        e.preventDefault();
        openUnifiedSearch(SCOPE_MB);
      }
    });
  });

  global.openUnifiedSearch = openUnifiedSearch;
  global.closeUnifiedSearch = closeUnifiedSearch;
  global.syncNavSearchQuery = syncNavSearchQuery;
  global.closeUnifiedFilterSheet = closeUnifiedFilterSheet;
  global.openUnifiedFilterSheet = openUnifiedFilterSheet;
  global._renderMbReleaseTrackTable = renderMbReleaseTrackTable;

  // `toggleUsSection` was a window global solely to serve an inline onclick
  // in buildSection(). That markup now uses the delegated listener above.
})(window);
