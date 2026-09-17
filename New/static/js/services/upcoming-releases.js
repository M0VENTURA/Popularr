/* ==========================================================================
   static/js/services/upcoming-releases.js
   UpcomingReleasesService — shared logic for the upcoming-releases UI
   (dedicated page, download monitor card, dashboard).

   Load order:
       utils/dom.js  →  utils/api.js
       ui/toast.js  →  ui/button-state.js  →  ui/status-badge.js
       upcoming_releases.js

   Single source of truth for:
     - fetching paginated releases from /api/upcoming-releases
     - triggering the Wikipedia scrape + background MusicBrainz refresh
     - live progress polling (renderProgressBadge + /scrape/status)
     - matching releases to MusicBrainz (server-side fallback when no MBID)
     - queueing released albums (POST /api/downloads/queue-upcoming)
     - rendering the month-grouped accordion table

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. THE QUEUE BUTTON THREW A SYNTAX ERROR ON EVERY CLICK.
      Its onclick was assembled as:

          'onclick="UpcomingReleasesService.queueFromRow(' + releaseId + ', ' +
          JSON.stringify(release.release_group_mbid || '') + ', ' +
          artistEnc + ', ' + albumEnc + ', this)"'

      `artistEnc` is encodeInlineArg() output — percent-encoded text such as
      `%22Pink%20Floyd%22` — and it was interpolated WITHOUT surrounding
      quotes, producing:

          queueFromRow(42, "abc-123", %22Pink%20Floyd%22, %22Animals%22, this)

      which is not valid JavaScript: "Unexpected token '%'". The sibling
      Search button does it correctly (`'\'' + artistEnc + '\''`); this one
      just never wrapped its two encoded args. Every row's Queue button was
      dead. Now built with addEventListener and a payload held in a closure,
      so there is no string-assembled call to get wrong.

   2. escapeHtml DID NOT ESCAPE QUOTES, AND ITS OUTPUT WENT INTO ATTRIBUTES.
      The local implementation was:

          div.appendChild(document.createTextNode(String(text)));
          return div.innerHTML;

      That serialisation escapes `&`, `<` and `>` but leaves `"` and `'`
      untouched — verified, not assumed. Its result was then interpolated
      into attribute values, e.g.
          title="Scraper rule: ' + escapeHtml(sourceKey) + '"
      so a source_key or artist name containing a double quote closed the
      attribute early and anything after it became markup. utils/dom.js's
      escapeHtml escapes quotes as well, so it is safe in both contexts.

   3. fetchJson LEAKED ITS TIMEOUT. The abort timer was never cleared, so a
      fast response still left a pending setTimeout holding the controller
      until it fired. It also could not distinguish "timed out" from "the
      server returned an error" — every abort surfaced as a bare
      AbortError. utils/api.js handles both.

   ── CHECK BEFORE SHIPPING ─────────────────────────────────────────────────
   injectSourceBadgeStyles() wrote a `.source-key-badge` rule into a <style>
   tag from JavaScript. I flagged `.source-key-badge` earlier as one of five
   near-identical pill recipes in popularr.css — if that rule is already
   there, this injection is a second, competing definition and the JS copy
   should go. The injection is REMOVED here on that basis; if popularr.css
   does NOT define it, add the rule from the supplied CSS addendum instead
   of reinstating the JS.
   ========================================================================== */

(function (global) {
  'use strict';

  const RELEASES_ENDPOINT = '/api/upcoming-releases';
  const QUEUE_ENDPOINT = '/api/downloads/queue-upcoming';
  const DEFAULT_LIMIT = 50;
  const SCRAPE_TIMEOUT_MS = 120000;

  const state = {
    items: [],
    filters: { source: 'all', page: 1, limit: DEFAULT_LIMIT },
    total: 0,
    hasMore: false,
  };

  /** A page can hook this to re-render after service mutations. */
  let onRefresh = null;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function notifyError(message) {
    if (global.toast) global.toast.error(message);
    else global.alert(message);
  }

  // ── API ─────────────────────────────────────────────────────────────────

  async function fetchReleases(params) {
    const filters = Object.assign({}, state.filters, params || {});
    const qs = new URLSearchParams();

    if (filters.filter && filters.filter !== 'all') qs.set('filter', filters.filter);
    if (filters.source && filters.source !== 'all') qs.set('source', filters.source);
    if (filters.include_queue) qs.set('include_queue', 'true');
    if (filters.window) qs.set('window', filters.window);
    qs.set('page', String(filters.page || 1));
    qs.set('limit', String(filters.limit || DEFAULT_LIMIT));

    const data = await global.api.getJson(`${RELEASES_ENDPOINT}?${qs.toString()}`);

    state.items = data.releases || [];
    state.total = data.total || 0;
    state.hasMore = !!data.has_more;
    state.filters = filters;
    return data;
  }

  /** Distinct release sources + counts, for the Source filter dropdown. */
  async function fetchSources() {
    const data = await global.api.getJson(`${RELEASES_ENDPOINT}/sources`, { timeoutMs: 10000 });
    return data.sources || [];
  }

  function triggerScrape() {
    return global.api.postJson(`${RELEASES_ENDPOINT}/scrape`, {}, { timeoutMs: SCRAPE_TIMEOUT_MS });
  }

  function fetchScrapeStatus() {
    return global.api.getJson(`${RELEASES_ENDPOINT}/scrape/status`, { timeoutMs: 10000 });
  }

  /**
   * Match a release to a MusicBrainz release-group.
   * When `mbid` is null the server falls back to its own MB search.
   *
   * @param {number|string} releaseId
   * @param {string|null} mbid
   * @returns {Promise<Object>}
   */
  async function matchRelease(releaseId, mbid) {
    const body = mbid
      ? { release_group_mbid: mbid, source: 'candidate_confirm' }
      : { source: 'auto_search' };

    const data = await global.api.postJson(
      `${RELEASES_ENDPOINT}/${encodeURIComponent(releaseId)}/match`, body
    );
    if (!data || !data.success) {
      throw new Error((data && data.error) || 'Match failed');
    }
    return data;
  }

  /** Queue a released album (album-typed download_queue item). */
  async function queueDownload(id) {
    const data = await global.api.postJson(QUEUE_ENDPOINT, { upcoming_release_id: id });
    if (!data.success) throw new Error(data.error || 'Queue failed');
    return data;
  }

  // ── Row rendering ───────────────────────────────────────────────────────

  function sourceBadge(release) {
    const sourceKey = String(release.source_key || '').trim();
    if (sourceKey) {
      return `<span class="source-key-badge" title="Scraper rule: ${esc(sourceKey)}">` +
        `<i class="bi bi-wikipedia"></i> ${esc(sourceKey)}</span>`;
    }
    const isMusicBrainz = String(release.source || '').toLowerCase().includes('musicbrainz');
    return isMusicBrainz
      ? '<span class="badge bg-info"><i class="bi bi-hexagon-fill"></i> MusicBrainz</span>'
      : '<span class="badge bg-secondary"><i class="bi bi-wikipedia"></i> Wikipedia</span>';
  }

  /** True when the release date is a real date that is today or earlier. */
  function isReleased(release) {
    const dateText = (release.release_date || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}/.test(dateText)) return false;
    return dateText <= new Date().toISOString().slice(0, 10);
  }

  function typeBadge(release) {
    const rawType = String(release.primary_type || '').trim();
    const type = rawType.toLowerCase();
    if (type === 'album') return '<span class="badge bg-primary-subtle text-primary-emphasis ms-1">Album</span>';
    if (type === 'ep') return '<span class="badge bg-success-subtle text-success-emphasis ms-1">EP</span>';
    if (type === 'single') return '<span class="badge bg-warning-subtle text-warning-emphasis ms-1">Single</span>';
    if (rawType) {
      return `<span class="badge bg-secondary-subtle text-secondary-emphasis ms-1">${esc(rawType)}</span>`;
    }
    return '';
  }

  function linkedBadge(release) {
    if (!release.release_group_mbid) return '';
    const score = release.mbid_match_score
      ? ` (score ${esc(String(release.mbid_match_score))})`
      : '';
    return `<span class="badge bg-info-subtle text-info-emphasis ms-1" ` +
      `title="Linked to MusicBrainz${score}"><i class="bi bi-hexagon-fill"></i></span>`;
  }

  function mbidCell(release, isCandidate) {
    if (release.release_group_mbid) {
      return `<code class="small">${esc(String(release.release_group_mbid).slice(0, 8))}…</code>`;
    }
    if (isCandidate) {
      return `<span class="text-warning small">candidate · ${esc(String(release.mbid_match_score || ''))}</span>`;
    }
    return '<span class="text-muted small">unmatched</span>';
  }

  /**
   * Build one table row.
   *
   * Buttons carry only `data-release-index`; the release object stays in
   * `renderedReleases`. Nothing is serialised into an attribute, so the
   * class of failure that killed the Queue button cannot recur.
   */
  function renderReleaseRow(release, index) {
    const released = isReleased(release);
    const dateBadge = released
      ? '<span class="badge bg-primary">Out now</span>'
      : '<span class="badge bg-success">Upcoming</span>';

    const isCandidate = release.mbid_match_status === 'candidate'
      && !!release.candidate_release_group_mbid;

    const actions = [];
    actions.push(
      `<button type="button" class="btn btn-sm btn-outline-info ur-search-btn" ` +
      `data-release-index="${index}" title="Search / Download on MusicBrainz">` +
      '<i class="bi bi-search"></i></button>'
    );

    if (!release.release_group_mbid && isCandidate) {
      actions.push(
        `<button type="button" class="btn btn-sm btn-outline-warning ur-confirm-btn" ` +
        `data-release-index="${index}" ` +
        `title="Confirm MusicBrainz match (score ${esc(String(release.mbid_match_score || ''))})">` +
        '<i class="bi bi-link-45deg"></i> Match</button>'
      );
    } else if (!release.release_group_mbid) {
      actions.push(
        `<button type="button" class="btn btn-sm btn-outline-warning ur-automatch-btn" ` +
        `data-release-index="${index}" title="Auto-match with MusicBrainz">` +
        '<i class="bi bi-magic"></i></button>'
      );
    }

    if (release.in_queue) {
      actions.push('<span class="badge bg-info text-dark align-middle">In Queue</span>');
    } else if (released) {
      actions.push(
        `<button type="button" class="btn btn-sm btn-success ur-queue-btn" ` +
        `data-release-index="${index}" title="Queue download (release is out)">` +
        '<i class="bi bi-download"></i> Queue</button>'
      );
    }

    return '<tr>' +
      `<td data-label="Artist">${esc(release.artist_name || '')}</td>` +
      `<td data-label="Album">${esc(release.album_name || '')}${typeBadge(release)}${linkedBadge(release)}</td>` +
      `<td data-label="Date"><small>${esc(release.release_date || 'TBA')} ${dateBadge}</small></td>` +
      `<td data-label="Source">${sourceBadge(release)}</td>` +
      `<td data-label="MBID">${mbidCell(release, isCandidate)}</td>` +
      `<td data-label="Action"><div class="d-flex gap-1 flex-wrap">${actions.join('')}</div></td>` +
      '</tr>';
  }

  /** Releases currently on screen, indexed to match data-release-index. */
  let renderedReleases = [];

  function attachRowHandlers(container) {
    const byIndex = (el) => renderedReleases[parseInt(el.dataset.releaseIndex, 10)];

    container.querySelectorAll('.ur-search-btn').forEach((btn) => {
      btn.addEventListener('click', function () {
        const release = byIndex(this);
        if (release) search(release.artist_name || '', release.album_name || '');
      });
    });

    container.querySelectorAll('.ur-automatch-btn').forEach((btn) => {
      btn.addEventListener('click', function () {
        const release = byIndex(this);
        if (release) autoMatch(release.id, this);
      });
    });

    container.querySelectorAll('.ur-confirm-btn').forEach((btn) => {
      btn.addEventListener('click', function () {
        const release = byIndex(this);
        if (release) confirmCandidate(release.id, release.candidate_release_group_mbid, this);
      });
    });

    container.querySelectorAll('.ur-queue-btn').forEach((btn) => {
      btn.addEventListener('click', function () {
        const release = byIndex(this);
        if (release) queueFromRow(release, this);
      });
    });
  }

  function monthLabel(monthKey) {
    // "Unknown Date".substring(0,7) is not parseable — guard rather than
    // rendering "Invalid Date" as a heading.
    const parsed = new Date(monthKey + '-01');
    if (Number.isNaN(parsed.getTime())) return 'Undated';
    return parsed.toLocaleDateString('en-AU', { year: 'numeric', month: 'long' });
  }

  /**
   * Render the month-grouped accordion.
   *
   * @param {string} containerId
   * @param {Array<Object>} items
   * @param {Object} [options]
   */
  function renderTable(containerId, items, options) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const opts = options || {};
    const releases = items || [];

    if (!releases.length) {
      container.innerHTML = opts.emptyHtml ||
        '<div class="text-center py-4"><p class="text-muted mb-0">' +
        esc(opts.emptyMessage || 'No upcoming releases found.') + '</p></div>';
      renderedReleases = [];
      return;
    }

    renderedReleases = releases.slice();

    const grouped = Object.create(null);
    releases.forEach((release, index) => {
      const month = (release.release_date || 'Unknown Date').substring(0, 7);
      if (!grouped[month]) grouped[month] = [];
      grouped[month].push({ release, index });
    });

    const months = Object.keys(grouped).sort();
    const todayMonth = new Date().toISOString().slice(0, 7);
    const defaultOpen = months.indexOf(todayMonth) >= 0 ? todayMonth : months[0];

    const sections = months.map((month, idx) => {
      const entries = grouped[month];
      const open = month === defaultOpen;
      const rows = entries.map((e) => renderReleaseRow(e.release, e.index)).join('');

      return `
        <div id="upcomingMonthCard${idx}" class="accordion-item">
          <h2 class="accordion-header">
            <button class="accordion-button${open ? '' : ' collapsed'}" type="button"
                    data-bs-toggle="collapse" data-bs-target="#ucm${idx}">
              <strong>${esc(monthLabel(month))}</strong>
              <span class="badge bg-primary ms-2">${entries.length} release${entries.length === 1 ? '' : 's'}</span>
            </button>
          </h2>
          <div id="ucm${idx}" class="accordion-collapse collapse${open ? ' show' : ''}">
            <div class="accordion-body p-0">
              <div class="table-responsive upcoming-table">
                <table class="table table-hover table-striped table-sm mb-0" data-mobile-cards>
                  <thead>
                    <tr class="d-none d-md-table-row">
                      <th class="col-artist">Artist</th>
                      <th class="col-album">Album</th>
                      <th class="col-date">Date</th>
                      <th class="col-source">Source</th>
                      <th class="col-mbid">MBID</th>
                      <th class="col-action">Action</th>
                    </tr>
                  </thead>
                  <tbody>${rows}</tbody>
                </table>
              </div>
            </div>
          </div>
        </div>`;
    }).join('');

    container.innerHTML = `<div class="accordion" id="upcomingReleaseAccordion">${sections}</div>`;
    attachRowHandlers(container);
  }

  function renderProgressBadge(statusData) {
    const s = statusData || {};
    if (s.status !== 'running') return '';

    const progress = s.total > 0
      ? `${Math.min(Number(s.progress) || 0, Number(s.total))}/${s.total}`
      : String(s.progress || 0);
    const artist = s.current_artist ? ` · ${esc(s.current_artist)}` : '';

    return '<span class="badge bg-info text-dark"><i class="bi bi-arrow-repeat"></i> ' +
      `Refreshing… (${esc(progress)})${artist}</span>`;
  }

  // ── Actions ─────────────────────────────────────────────────────────────

  function search(artist, album) {
    if (typeof global.searchMusicBrainzRelease === 'function') {
      global.searchMusicBrainzRelease(null, artist, album);
      return;
    }
    console.warn('[upcoming] searchMusicBrainzRelease is unavailable on this page');
    notifyError('MusicBrainz search is unavailable on this page.');
  }

  async function autoMatch(releaseId, button) {
    if (!releaseId) return;
    return global.buttonState.withBusy(button, '', async () => {
      try {
        await matchRelease(releaseId, null);
        if (onRefresh) onRefresh();
      } catch (error) {
        notifyError('Error matching: ' + error.message);
      }
    });
  }

  async function confirmCandidate(releaseId, mbid, button) {
    if (!releaseId || !mbid) return;
    return global.buttonState.withBusy(button, '', async () => {
      try {
        await matchRelease(releaseId, mbid);
        if (onRefresh) onRefresh();
      } catch (error) {
        notifyError('Error confirming match: ' + error.message);
      }
    });
  }

  async function queueById(releaseId, button, title) {
    if (!releaseId) return;
    return global.buttonState.withBusy(button, '', async () => {
      try {
        const result = await queueDownload(releaseId);
        if (result.already_queued) {
          global.toast.warning(`Already in queue${title ? ` — "${title}"` : ''}`);
        } else {
          global.toast.queued(title || 'Release');
        }
        if (onRefresh) onRefresh();
      } catch (error) {
        notifyError('Error queueing: ' + error.message);
      }
    });
  }

  /**
   * Queue from a table row. When the release is linked to a MusicBrainz
   * release-group AND a release picker is available, let the user choose the
   * specific release first; otherwise queue directly.
   */
  function queueFromRow(release, button) {
    const rgMbid = release.release_group_mbid || '';

    if (rgMbid && typeof global.openReleasePicker === 'function') {
      global.openReleasePicker(
        rgMbid,
        release.album_name || '',
        release.artist_name || '',
        function () { if (onRefresh) onRefresh(); }
      );
      return;
    }
    return queueById(release.id, button, release.album_name || '');
  }

  global.UpcomingReleasesService = {
    state,
    set onRefresh(fn) { onRefresh = fn; },
    get onRefresh() { return onRefresh; },
    fetchReleases,
    fetchSources,
    triggerScrape,
    fetchScrapeStatus,
    matchRelease,
    queueDownload,
    renderTable,
    renderProgressBadge,
    search,
    autoMatch,
    confirmCandidate,
    queueById,
    queueFromRow,
    isReleased,
  };

  // NOTE: escapeHtml / encodeInlineArg / decodeInlineArg were exported from
  // this service and are NOT re-exported. escapeHtml now lives in
  // utils/dom.js. The two encode helpers existed only to smuggle values
  // through inline onclick attributes, which no longer happens here — if
  // another file imported them from this service, point it at utils/dom.js's
  // escapeHtml or switch that call site to data attributes too.
})(window);
