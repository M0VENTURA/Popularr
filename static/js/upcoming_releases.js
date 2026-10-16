/**
 * UpcomingReleasesService — shared front-end logic for the upcoming releases
 * UI (dedicated page, download monitor card, dashboard).
 *
 * Single source of truth for:
 *   - fetching paginated releases from /api/upcoming-releases
 *   - triggering the Wikipedia scrape + background MusicBrainz refresh
 *   - live progress polling (renderProgressBadge + /scrape/status)
 *   - matching releases to MusicBrainz (server-side fallback when no MBID)
 *   - queueing released albums (POST /api/downloads/queue-upcoming)
 *   - rendering the month-grouped accordion table
 */
(function () {
  'use strict';

  // Self-contained styles for the specific source-rule badge (rendered from
  // JS so every page that loads this service gets the same pill).
  (function injectSourceBadgeStyles() {
    if (document.getElementById('upcomingSourceBadgeStyles')) return;
    var style = document.createElement('style');
    style.id = 'upcomingSourceBadgeStyles';
    style.textContent =
      '.source-key-badge{display:inline-block;padding:0 0.5rem;font-size:10px;' +
      'font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.5;' +
      'color:#c4b5fd;background:rgba(88,28,135,0.55);border:1px solid rgba(168,85,247,0.45);' +
      'border-radius:0.25rem;white-space:nowrap}';
    document.head.appendChild(style);
  })();

  // NOTE: the thumbnail's size is NOT injected here.
  //
  // ⚠️ This service renders rows on THREE pages (dashboard, dedicated upcoming
  // page, downloads monitor) but `css/dashboard.css` is linked by the DASHBOARD
  // ONLY, so a rule declared there left the other two emitting an <img> with no
  // rule to size it — a full-size Cover Art Archive image in a table cell.
  // The fix is to declare `.upcoming-thumb` in `css/popularr.css`, which
  // `base.html` loads on every page, rather than to inject it from JavaScript.
  //
  // (The `injectSourceBadgeStyles` IIFE above is itself a second definition of
  // `.source-key-badge`, which `css/popularr.css` already declares. That
  // duplication is pre-existing and not touched here — it is a styling change
  // to the badge, not to artwork.)

  var state = {
    items: [],
    filters: { source: 'all', page: 1, limit: 50 },
    total: 0,
    hasMore: false,
  };

  /* A page can hook this to re-render after service mutations (match/queue). */
  var onRefresh = null;

  function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(String(text)));
    return div.innerHTML;
  }

  function encodeInlineArg(value) {
    return encodeURIComponent(JSON.stringify(value === null || value === undefined ? '' : value));
  }

  function decodeInlineArg(value, fallback) {
    try {
      var decoded = decodeURIComponent(value || '');
      try { return JSON.parse(decoded); } catch (_e) { return decoded || fallback; }
    } catch (_e) { return fallback; }
  }

  async function fetchJson(url, options, timeoutMs) {
    var opts = options || {};
    var controller = null;
    if (timeoutMs) {
      controller = new AbortController();
      opts = Object.assign({}, opts, { signal: controller.signal });
      setTimeout(function () { controller.abort(); }, timeoutMs);
    }
    var resp = await fetch(url, opts);
    var data = null;
    try { data = await resp.json(); } catch (_e) { /* no body */ }
    if (!resp.ok) {
      throw new Error((data && data.error) || ('Request failed (HTTP ' + resp.status + ')'));
    }
    return data;
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------

  async function fetchReleases(params) {
    var filters = Object.assign({}, state.filters, params || {});
    var qs = new URLSearchParams();
    if (filters.filter && filters.filter !== 'all') qs.set('filter', filters.filter);
    if (filters.source && filters.source !== 'all') qs.set('source', filters.source);
    if (filters.include_queue) qs.set('include_queue', 'true');
    if (filters.window) qs.set('window', filters.window);
    qs.set('page', filters.page || 1);
    qs.set('limit', filters.limit || 50);

    var data = await fetchJson('/api/upcoming-releases?' + qs.toString(), {}, 30000);
    state.items = data.releases || [];
    state.total = data.total || 0;
    state.hasMore = !!data.has_more;
    state.filters = filters;
    return data;
  }

  /** Distinct release sources + counts, for the Source filter dropdown. */
  async function fetchSources() {
    var data = await fetchJson('/api/upcoming-releases/sources', {}, 10000);
    return data.sources || [];
  }

  async function triggerScrape() {
    return fetchJson('/api/upcoming-releases/scrape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }, 120000);
  }

  async function fetchScrapeStatus() {
    return fetchJson('/api/upcoming-releases/scrape/status', {}, 10000);
  }

  /**
   * Match a release to a MusicBrainz release-group.
   * When `mbid` is null the server falls back to its own MB search.
   */
  async function matchRelease(releaseId, mbid) {
    var body = mbid
      ? { release_group_mbid: mbid, source: 'candidate_confirm' }
      : { source: 'auto_search' };
    var resp = await fetch('/api/upcoming-releases/' + releaseId + '/match', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var data = null;
    try { data = await resp.json(); } catch (_e) { /* empty body */ }
    if (!resp.ok || !data || !data.success) {
      throw new Error((data && data.error) || 'Match failed');
    }
    return data;
  }

  /** Queue a released album (album-typed download_queue item). */
  async function queueDownload(id) {
    var data = await fetchJson('/api/downloads/queue-upcoming', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ upcoming_release_id: id }),
    }, 30000);
    if (!data.success) throw new Error(data.error || 'Queue failed');
    return data;
  }

  // ------------------------------------------------------------------
  // Row rendering
  // ------------------------------------------------------------------

  function sourceBadge(release) {
    var sourceKey = String(release.source_key || '').trim();
    if (sourceKey) {
      return '<span class="source-key-badge" title="Scraper rule: ' + escapeHtml(sourceKey) + '">' +
        '<i class="bi bi-wikipedia"></i> ' + escapeHtml(sourceKey) + '</span>';
    }
    var isMusicBrainz = String(release.source || '').toLowerCase().includes('musicbrainz');
    return isMusicBrainz
      ? '<span class="badge bg-info"><i class="bi bi-hexagon-fill"></i> MusicBrainz</span>'
      : '<span class="badge bg-secondary"><i class="bi bi-wikipedia"></i> Wikipedia</span>';
  }

  function isReleased(release) {
    var dateText = (release.release_date || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}/.test(dateText)) return false;
    return dateText <= new Date().toISOString().slice(0, 10);
  }

  // ── Artwork ─────────────────────────────────────────────────────────────
  // The ONLY place the archive host is named. Every artwork URL has to come
  // through here, so the two sources below stay in one priority order.
  var CAA_ART_BASE = 'https://coverartarchive.org/release-group';

  /**
   * The artwork URL for a release, or '' when there is nothing to ask for.
   *
   * PRIORITY, and the first two rungs are different KINDS of request:
   *
   *   1. Owned (album_in_collection) -> our own /api/album art endpoint, which
   *      prefers Navidrome. Same-origin, already cached, and the picture the
   *      rest of the app shows for that album.
   *   2. Matched (release_group_mbid) -> the Cover Art Archive, addressed by
   *      RELEASE GROUP. This is what makes art appear at all: an upcoming
   *      release is by definition one you do not own yet, so rung 1 is a
   *      guaranteed 404 for it and every row fell back to a placeholder.
   *   3. Otherwise '' — the caller draws its placeholder WITHOUT a request.
   *      That is why this is a function rather than an inline expression: a
   *      request that can only fail costs a 404 per row (fifty on a full page)
   *      to draw something that could have been drawn for free.
   *
   * A CANDIDATE match is deliberately NOT used. mbid_match_status 'candidate'
   * means the scorer found a plausible release group and is waiting for a human
   * to confirm it; showing its cover would present a GUESS as this album's
   * artwork, and art is the most believable thing on the row. Only
   * release_group_mbid is a confirmed identity — same reasoning as the blue
   * hexagon badge, which is also only drawn for a confirmed match.
   */
  function upcomingArtUrl(release, size) {
    var rel = release || {};
    var px = Number(size) > 0 ? Number(size) : 250;
    if (rel.album_in_collection) {
      var artist = encodeURIComponent(rel.artist_name || '');
      var album = encodeURIComponent(rel.album_name || '');
      if (artist && album) {
        return '/api/album/' + artist + '/' + album + '/art?s=' + px;
      }
    }
    if (rel.release_group_mbid) {
      return CAA_ART_BASE + '/' + encodeURIComponent(String(rel.release_group_mbid)) + '/front-' + px;
    }
    return '';
  }

  /**
   * A small inline thumbnail, or '' when there is no art to ask for.
   *
   * loading="lazy" so a full page of covers does not block the first paint, and
   * onerror REMOVES the element rather than leaving a broken-image glyph: a
   * release group the archive has no art for answers 404, and a torn icon is
   * worse than a cell showing only the album name.
   */
  function upcomingArtThumb(release, size, cls) {
    var url = upcomingArtUrl(release, size);
    if (!url) return '';
    return '<img src="' + escapeHtml(url) + '" alt="" loading="lazy" ' +
      'class="' + (cls || 'upcoming-thumb') + '" onerror="this.remove()">';
  }

  function renderReleaseRow(release) {
    var releaseDate = release.release_date || 'TBA';
    var dateBadge = isReleased(release)
      ? '<span class="badge bg-primary">Out now</span>'
      : '<span class="badge bg-success">Upcoming</span>';

    var artistEnc = encodeInlineArg(release.artist_name || '');
    var albumEnc = encodeInlineArg(release.album_name || '');
    var releaseId = Number(release.id) || 0;

    var actions = [];
    actions.push(
      '<button type="button" class="btn btn-sm btn-outline-info" ' +
      'onclick="UpcomingReleasesService.searchFromEncoded(\'' + artistEnc + '\', \'' + albumEnc + '\')" ' +
      'title="Search / Download on MusicBrainz"><i class="bi bi-search"></i></button>'
    );

    var isCandidate = release.mbid_match_status === 'candidate' && !!release.candidate_release_group_mbid;
    if (!release.release_group_mbid && isCandidate) {
      actions.push(
        '<button type="button" class="btn btn-sm btn-outline-warning" ' +
        'onclick="UpcomingReleasesService.confirmCandidateById(' + releaseId + ', \'' + release.candidate_release_group_mbid + '\', this)" ' +
        'title="Confirm MusicBrainz match (score ' + escapeHtml(String(release.mbid_match_score || '')) + ')"><i class="bi bi-link-45deg"></i> Match</button>'
      );
    } else if (!release.release_group_mbid) {
      actions.push(
        '<button type="button" class="btn btn-sm btn-outline-warning" ' +
        'onclick="UpcomingReleasesService.autoMatchById(' + releaseId + ', this)" ' +
        'title="Auto-match with MusicBrainz"><i class="bi bi-magic"></i></button>'
      );
    }

    if (release.in_queue) {
      actions.push('<span class="badge bg-info text-dark align-middle">In Queue</span>');
    } else if (isReleased(release)) {
      actions.push(
        '<button type="button" class="btn btn-sm btn-success" ' +
        'onclick="UpcomingReleasesService.queueFromRow(' + releaseId + ', ' +
        JSON.stringify(release.release_group_mbid || '') + ', ' +
        artistEnc + ', ' + albumEnc + ', this)" ' +
        'title="Queue download (release is out)"><i class="bi bi-download"></i> Queue</button>'
      );
    }

    var linkedBadge = release.release_group_mbid
      ? '<span class="badge bg-info-subtle text-info-emphasis ms-1" ' +
        'title="Linked to MusicBrainz' + (release.mbid_match_score ? ' (score ' + escapeHtml(String(release.mbid_match_score)) + ')' : '') + '">' +
        '<i class="bi bi-hexagon-fill"></i></span>'
      : '';

    var typeBadge = '';
    var rawType = String(release.primary_type || '').trim();
    var pt = rawType.toLowerCase();
    if (pt === 'album') {
      typeBadge = '<span class="badge bg-primary-subtle text-primary-emphasis ms-1">Album</span>';
    } else if (pt === 'ep') {
      typeBadge = '<span class="badge bg-success-subtle text-success-emphasis ms-1">EP</span>';
    } else if (pt === 'single') {
      typeBadge = '<span class="badge bg-warning-subtle text-warning-emphasis ms-1">Single</span>';
    } else if (rawType) {
      typeBadge = '<span class="badge bg-secondary-subtle text-secondary-emphasis ms-1">' +
        escapeHtml(rawType) + '</span>';
    }

    var mbidCell = release.release_group_mbid
      ? '<code class="small">' + escapeHtml(String(release.release_group_mbid).slice(0, 8)) + '…</code>'
      : (isCandidate
          ? '<span class="text-warning small">candidate · ' + escapeHtml(String(release.mbid_match_score || '')) + '</span>'
          : '<span class="text-muted small">unmatched</span>');

    // FIXED: Added data-label so mobile cards know what to name the fields
    // The thumbnail goes inside the existing Album cell — a new <td> would need
    // a matching <th>, a data-label and every colspan kept in step.
    return '<tr>' +
      '<td data-label="Artist">' + escapeHtml(release.artist_name || '') + '</td>' +
      '<td data-label="Album">' + upcomingArtThumb(release, 250, 'upcoming-thumb') +
        escapeHtml(release.album_name || '') + typeBadge + linkedBadge + '</td>' +
      '<td data-label="Date"><small>' + escapeHtml(releaseDate) + ' ' + dateBadge + '</small></td>' +
      '<td data-label="Source">' + sourceBadge(release) + '</td>' +
      '<td data-label="MBID">' + mbidCell + '</td>' +
      '<td data-label="Action"><div class="d-flex gap-1 flex-wrap">' + actions.join('') + '</div></td>' +
      '</tr>';
  }

  function renderTable(containerId, items, options) {
    var container = document.getElementById(containerId);
    if (!container) return;
    options = options || {};
    var releases = items || [];

    if (releases.length === 0) {
      container.innerHTML = options.emptyHtml ||
        '<div class="text-center py-4"><p class="text-muted mb-0">' +
        (options.emptyMessage || 'No upcoming releases found.') + '</p></div>';
      return;
    }

    var grouped = {};
    releases.forEach(function (r) {
      var month = (r.release_date || 'Unknown Date').substring(0, 7);
      if (!grouped[month]) grouped[month] = [];
      grouped[month].push(r);
    });
    var sortedMonths = Object.keys(grouped).sort();

    var todayMonth = new Date().toISOString().slice(0, 7);
    var defaultOpenMonth = sortedMonths.indexOf(todayMonth) >= 0 ? todayMonth : sortedMonths[0];

    var html = '<div class="accordion" id="upcomingReleaseAccordion">';
    sortedMonths.forEach(function (month, idx) {
      var monthReleases = grouped[month];
      var monthLabel = new Date(month + '-01').toLocaleDateString('en-US', { year: 'numeric', month: 'long' });
      var open = month === defaultOpenMonth;
      html += '<div id="upcomingMonthCard' + idx + '" class="accordion-item">' +
        '<h2 class="accordion-header">' +
        '<button class="accordion-button' + (open ? '' : ' collapsed') + '" type="button" data-bs-toggle="collapse" data-bs-target="#ucm' + idx + '">' +
        '<strong>' + escapeHtml(monthLabel) + '</strong>' +
        '<span class="badge bg-primary ms-2">' + monthReleases.length + ' release' + (monthReleases.length === 1 ? '' : 's') + '</span>' +
        '</button></h2>' +
        '<div id="ucm' + idx + '" class="accordion-collapse collapse' + (open ? ' show' : '') + '">' +
        '<div class="accordion-body p-0"><div class="table-responsive upcoming-table">' +
        // FIXED: Added data-mobile-cards and d-none d-md-table-row for responsive table stacking
        '<table class="table table-hover table-striped table-sm mb-0" data-mobile-cards>' +
        '<thead><tr class="d-none d-md-table-row">' +
        '<th class="col-artist">Artist</th>' +
        '<th class="col-album">Album</th>' +
        '<th class="col-date">Date</th>' +
        '<th class="col-source">Source</th>' +
        '<th class="col-mbid">MBID</th>' +
        '<th class="col-action">Action</th>' +
        '</tr></thead>' +
        '<tbody>';
      monthReleases.forEach(function (release) {
        html += renderReleaseRow(release);
      });
      html += '</tbody></table></div></div></div></div>';
    });
    html += '</div>';
    container.innerHTML = html;
  }

  function renderProgressBadge(statusData) {
    var s = statusData || {};
    if (s.status !== 'running') return '';
    var progress = s.total > 0
      ? Math.min(Number(s.progress) || 0, Number(s.total)) + '/' + s.total
      : String(s.progress || 0);
    var artist = s.current_artist ? ' · ' + escapeHtml(s.current_artist) : '';
    return '<span class="badge bg-info text-dark"><i class="bi bi-arrow-repeat"></i> Refreshing... (' + progress + ')' + artist + '</span>';
  }

  // ------------------------------------------------------------------
  // UI actions (onclick targets)
  // ------------------------------------------------------------------

  function searchFromEncoded(artistEnc, albumEnc) {
    var fn = window.searchMusicBrainzReleaseFromEncoded ||
      function () { console.warn('searchMusicBrainzReleaseFromEncoded unavailable'); };
    fn(null, artistEnc, albumEnc);
  }

  async function autoMatchById(releaseId, buttonEl) {
    if (!releaseId) return;
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    }
    try {
      await matchRelease(releaseId, null);
    } catch (error) {
      if (buttonEl) {
        buttonEl.disabled = false;
        buttonEl.innerHTML = '<i class="bi bi-magic"></i>';
      }
      if (typeof window.showTopToast === 'function') {
        window.showTopToast('Error matching: ' + error.message, 'danger');
      } else {
        alert('Error matching: ' + error.message);
      }
      return;
    }
    if (onRefresh) onRefresh();
  }

  async function confirmCandidateById(releaseId, mbid, buttonEl) {
    if (!releaseId || !mbid) return;
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    }
    try {
      await matchRelease(releaseId, mbid);
    } catch (error) {
      if (buttonEl) {
        buttonEl.disabled = false;
        buttonEl.innerHTML = '<i class="bi bi-link-45deg"></i> Match';
      }
      if (typeof window.showTopToast === 'function') {
        window.showTopToast('Error confirming match: ' + error.message, 'danger');
      } else {
        alert('Error confirming match: ' + error.message);
      }
      return;
    }
    if (onRefresh) onRefresh();
  }

  async function queueById(releaseId, buttonEl, title) {
    if (!releaseId) return;
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    }
    try {
      var result = await queueDownload(releaseId);
      if (buttonEl && result.already_queued) {
        if (typeof window.showTopToast === 'function') {
          window.showTopToast('Already in queue' + (title ? ' — "' + title + '"' : ''), 'warning');
        }
      } else if (typeof window.showQueueToast === 'function') {
        window.showQueueToast(title || 'Release');
      }
    } catch (error) {
      if (buttonEl) {
        buttonEl.disabled = false;
        buttonEl.innerHTML = '<i class="bi bi-download"></i> Queue';
      }
      if (typeof window.showTopToast === 'function') {
        window.showTopToast('Error queueing: ' + error.message, 'danger');
      } else {
        alert('Error queueing: ' + error.message);
      }
      return;
    }
    if (onRefresh) onRefresh();
  }

  function queueFromRow(releaseId, rgMbid, artistEnc, albumEnc, buttonEl) {
    var picker = window.openReleasePicker;
    if (rgMbid && typeof picker === 'function') {
      picker(
        rgMbid,
        decodeInlineArg(albumEnc, ''),
        decodeInlineArg(artistEnc, ''),
        function () {
          if (onRefresh) onRefresh();
        }
      );
      return;
    }
    queueById(releaseId, buttonEl, decodeInlineArg(albumEnc, ''));
  }

  window.UpcomingReleasesService = {
    state: state,
    set onRefresh(fn) { onRefresh = fn; },
    fetchReleases: fetchReleases,
    fetchSources: fetchSources,
    triggerScrape: triggerScrape,
    fetchScrapeStatus: fetchScrapeStatus,
    matchRelease: matchRelease,
    queueDownload: queueDownload,
    renderTable: renderTable,
    renderProgressBadge: renderProgressBadge,
    searchFromEncoded: searchFromEncoded,
    autoMatchById: autoMatchById,
    confirmCandidateById: confirmCandidateById,
    queueById: queueById,
    queueFromRow: queueFromRow,
    isReleased: isReleased,
    // Exposed so the artwork rule has ONE implementation a caller (and a test)
    // can use directly, rather than three pages each building their own URL.
    // The live dashboard delegates here rather than keeping a second copy.
    upcomingArtUrl: upcomingArtUrl,
    upcomingArtThumb: upcomingArtThumb,
    escapeHtml: escapeHtml,
    encodeInlineArg: encodeInlineArg,
    decodeInlineArg: decodeInlineArg,
  };
})();