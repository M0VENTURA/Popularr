/* ==========================================================================
   ⛔ SUPERSEDED — DELETE THIS FILE. DO NOT LOAD IT.

   This was a full second implementation of library search, written before the
   page was identified as superseded. Everything it does already lives in
   ui/search-flyout.js (loaded globally by base.html, backing the navbar
   search): same /api/search endpoint, same release buckets
   (albums / compilations / live_albums / eps / singles), plus release-type
   filters, MusicBrainz merging and queue buttons this file never had.

   templates/Pages/search.html no longer references it — /search is now a thin
   entry point (js/pages/search-entry.js) that opens the flyout.

   Keeping this would mean THREE search implementations, which would drift the
   same way the two smart-playlist builders did. The original inline script it
   was extracted from is in git history if any of the individual fixes listed
   below are ever wanted elsewhere.

   ==========================================================================
   (Original header follows, for reference only.)

   static/js/pages/library-search.js
   Library search page (/search) — artists, releases and tracks from the
   Navidrome library.

   NAMING: this is NOT pages/search.js. That file is the initialiser for the
   DOWNLOADS unified search page (/downloads/search) and concerns `_trackPayloads`
   and Soulseek. This one is the library search page. Keep them apart.

   Load order: utils/dom.js → utils/api.js → ui/toast.js, then this file.
   All come from base.html.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/search.html carried a 261-line inline <script>, an 11-line
   second script for the initial query, and a 132-line inline <style>. Those are
   now this file and static/css/search.css. The page-data block stays in the
   template (it carries `initialQuery` from the server) but is read here.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. `escapeHtml` USED createTextNode + innerHTML, which escapes `&`, `<` and
      `>` but NOT quotes. On this page it only reached text position, so it was
      not exploitable — but it was the same broken helper that caused real
      attribute-context bugs on other pages, and it is now the shared one from
      utils/dom.js.

   2. `fetch` + `.json()` → api.postJson. The original checked `response.ok` but
      still called `.json()` on failure, so an HTML error page (session expiry)
      surfaced as "Unexpected token '<'" rather than "your session expired".

   3. INLINE onclick IN GENERATED MARKUP → data-action + data-url, handled by
      one delegated listener. Three call sites built
      `onclick="navigateTo('/artist/' + encodeURIComponent(...) + ')"`, and two
      more carried `onclick="event.stopPropagation()"`. `navigateTo` no longer
      needs to be a window global.

   4. THE DEBOUNCE RAN A SEARCH PER KEYSTROKE PAUSE WITH NO CANCELLATION. A
      500ms pause fired a request; typing again mid-flight started another and
      the SLOWER one could land last, showing results for an older query. The
      in-flight request is now aborted when a new one starts.

   5. The initial-query bootstrap lived in its own <script> block and called
      performSearch() before the debounce handler was bound. It is folded into
      this module's init.

   ── WHAT DELIBERATELY STAYS ───────────────────────────────────────────────
   * NOTE: the backend splits releases into buckets (albums, compilations,
     live_albums, eps, singles). All five are counted for the "has results"
     decision, so a query matching only a single or EP still renders.
   * The 500ms debounce and the 2-character minimum are unchanged.
   * Results are built with innerHTML rather than DOM nodes — every value goes
     through escapeHtml, and the markup is a fixed template.
   ========================================================================== */

(function (global) {
  'use strict';

  const SEARCH_ENDPOINT = '/api/search';
  const DEBOUNCE_MS = 500;
  const MIN_QUERY_LENGTH = 2;

  /** Release buckets the backend returns, with their icon and heading. */
  const ALBUM_BUCKETS = [
    { key: 'albums', icon: 'bi-disc', label: 'Albums' },
    { key: 'compilations', icon: 'bi-collection', label: 'Compilations' },
    { key: 'live_albums', icon: 'bi-mic', label: 'Live Albums' },
    { key: 'eps', icon: 'bi-music-note-list', label: 'EPs' },
    { key: 'singles', icon: 'bi-music-note-beamed', label: 'Singles' },
  ];

  let debounceTimer = null;
  /** Aborted when a newer search starts, so a slow response cannot win. */
  let inFlight = null;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function el(id) {
    return document.getElementById(id);
  }

  const EMPTY_STATE_HTML = `
    <div class="row">
      <div class="col-12">
        <div class="card text-center">
          <div class="card-body py-5">
            <i class="bi bi-search" style="font-size: 3rem; color: var(--text-secondary); margin-bottom: 1rem; display: block;"></i>
            <h5 class="text-secondary">Start searching</h5>
            <p class="text-tertiary">Enter an artist name, album, or song title to find results</p>
          </div>
        </div>
      </div>
    </div>`;

  function navigateTo(url) {
    global.location.href = url;
  }

  // ── Search ──────────────────────────────────────────────────────────────

  async function performSearch(event) {
    if (event) event.preventDefault();

    const input = el('searchQuery');
    const query = input ? input.value.trim() : '';
    if (!query) return;

    const results = el('searchResults');
    const empty = el('emptyState');
    if (!results || !empty) return;

    // Shareable/bookmarkable URL, without a reload.
    const url = new URL(global.location);
    url.searchParams.set('q', query);
    global.history.replaceState({}, '', url);

    results.style.display = 'block';
    empty.style.display = 'none';
    results.innerHTML = `
      <div class="row">
        <div class="col-12">
          <div class="card">
            <div class="card-body search-loading">
              <div class="spinner-border text-success" role="status" style="margin-bottom: 1rem; display: block;">
                <span class="visually-hidden">Loading…</span>
              </div>
              <p>Searching for "${esc(query)}"…</p>
            </div>
          </div>
        </div>
      </div>`;

    // Supersede any request still running for an earlier keystroke.
    if (inFlight) inFlight.abort();
    inFlight = new AbortController();

    try {
      const data = await global.api.postJson(
        SEARCH_ENDPOINT,
        { query },
        { signal: inFlight.signal }
      );
      inFlight = null;
      displaySearchResults(data, query);
    } catch (error) {
      inFlight = null;
      if (error && error.name === 'AbortError') return; // superseded — drop it
      results.innerHTML = `
        <div class="row">
          <div class="col-12">
            <div class="card border-danger">
              <div class="card-body search-error">
                <div class="alert alert-danger mb-0">
                  <i class="bi bi-exclamation-circle"></i>
                  Error: ${esc(error.message)}
                </div>
              </div>
            </div>
          </div>
        </div>`;
    }
  }

  function displaySearchResults(data, query) {
    const results = el('searchResults');
    const empty = el('emptyState');
    if (!results || !empty) return;

    const artists = data.artists || [];
    const tracks = data.tracks || [];

    const buckets = {};
    ALBUM_BUCKETS.forEach((b) => { buckets[b.key] = data[b.key] || []; });
    const albumTotal = ALBUM_BUCKETS.reduce((n, b) => n + buckets[b.key].length, 0);

    if (!artists.length && !albumTotal && !tracks.length) {
      results.style.display = 'none';
      empty.style.display = 'block';
      empty.innerHTML = `
        <div class="row">
          <div class="col-12">
            <div class="card text-center">
              <div class="card-body py-5">
                <i class="bi bi-search" style="font-size: 3rem; color: var(--text-secondary); margin-bottom: 1rem; display: block;"></i>
                <h5 class="text-secondary">No results found</h5>
                <p class="text-tertiary">No artists, albums, or songs match "${esc(query)}"</p>
              </div>
            </div>
          </div>
        </div>`;
      return;
    }

    let html = '';

    if (artists.length) {
      html += `
        <div class="results-section">
          <h5 class="results-section-title">
            <i class="bi bi-people"></i> Artists (${artists.length})
          </h5>
          <div class="results-grid">
            ${artists.map((artist) => `
              <div class="card search-result-card" role="button" tabindex="0"
                   data-action="libsearch-navigate"
                   data-url="/artist/${encodeURIComponent(artist.name)}">
                <div class="card-body">
                  <span class="badge badge-success result-type-badge">Artist</span>
                  <div class="result-title">${esc(artist.name)}</div>
                  <div class="result-meta">
                    <i class="bi bi-disc"></i> ${esc(String(artist.album_count))} album${artist.album_count !== 1 ? 's' : ''}
                    <br>
                    <i class="bi bi-music-note"></i> ${esc(String(artist.track_count))} track${artist.track_count !== 1 ? 's' : ''}
                  </div>
                </div>
              </div>`).join('')}
          </div>
        </div>`;
    }

    ALBUM_BUCKETS.forEach((bucket) => {
      const items = buckets[bucket.key];
      if (!items.length) return;

      html += `
        <div class="results-section">
          <h5 class="results-section-title">
            <i class="${esc(bucket.icon)}"></i> ${esc(bucket.label)} (${items.length})
          </h5>
          <div class="results-grid">
            ${items.map((album) => `
              <div class="card search-result-card" role="button" tabindex="0"
                   data-action="libsearch-navigate"
                   data-url="/album/${encodeURIComponent(album.artist)}/${encodeURIComponent(album.album)}">
                <div class="card-body">
                  <span class="badge badge-success result-type-badge">${esc(album.type_label || bucket.label.replace(/s$/, ''))}</span>
                  <div class="result-title">${esc(album.album)}</div>
                  <div class="result-subtitle"><i class="bi bi-person"></i> ${esc(album.artist)}</div>
                  <div class="result-meta">
                    <i class="bi bi-music-note"></i> ${esc(String(album.track_count))} track${album.track_count !== 1 ? 's' : ''}
                    ${album.avg_stars && !isNaN(parseFloat(album.avg_stars))
                      ? `<br><span class="result-rating">★ ${parseFloat(album.avg_stars).toFixed(1)}</span>`
                      : ''}
                  </div>
                </div>
              </div>`).join('')}
          </div>
        </div>`;
    });

    if (tracks.length) {
      html += `
        <div class="results-section">
          <h5 class="results-section-title">
            <i class="bi bi-music-note"></i> Tracks (${tracks.length})
          </h5>
          <div class="table-responsive">
            <table class="table table-hover">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Artist</th>
                  <th>Album</th>
                  <th class="text-center">Rating</th>
                  <th class="text-center">Action</th>
                </tr>
              </thead>
              <tbody>
                ${tracks.map((track) => `
                  <tr>
                    <td><strong>${esc(track.title)}</strong></td>
                    <td>
                      <a href="/artist/${encodeURIComponent(track.artist)}" data-stop-propagation>
                        ${esc(track.artist)}
                      </a>
                    </td>
                    <td>
                      <a href="/album/${encodeURIComponent(track.artist)}/${encodeURIComponent(track.album)}" data-stop-propagation>
                        ${esc(track.album)}
                      </a>
                    </td>
                    <td class="text-center">
                      ${track.stars ? `<span class="result-rating">★ ${esc(String(track.stars))}</span>` : '—'}
                    </td>
                    <td class="text-center">
                      <button type="button" class="btn btn-sm btn-outline-primary"
                              data-action="libsearch-navigate"
                              data-url="/track/${encodeURIComponent(track.id)}">
                        <i class="bi bi-eye"></i> View
                      </button>
                    </td>
                  </tr>`).join('')}
              </tbody>
            </table>
          </div>
        </div>`;
    }

    results.innerHTML = html;
    results.style.display = 'block';
    empty.style.display = 'none';
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function bindSearchInput() {
    const input = el('searchQuery');
    if (!input) return;

    input.addEventListener('input', function () {
      clearTimeout(debounceTimer);
      const query = this.value.trim();

      if (query.length >= MIN_QUERY_LENGTH) {
        debounceTimer = setTimeout(() => performSearch(null), DEBOUNCE_MS);
        return;
      }

      if (query.length === 0) {
        const results = el('searchResults');
        const empty = el('emptyState');
        if (results) results.style.display = 'none';
        if (empty) {
          empty.style.display = 'block';
          empty.innerHTML = EMPTY_STATE_HTML;
        }
        const url = new URL(global.location);
        url.searchParams.delete('q');
        global.history.replaceState({}, '', url);
      }
    });
  }

  function bindActions() {
    document.addEventListener('click', function (event) {
      if (!event.target.closest) return;

      // Links inside a clickable result card must not trigger the card.
      if (event.target.closest('[data-stop-propagation]')) {
        event.stopPropagation();
        return;
      }

      const card = event.target.closest('[data-action="libsearch-navigate"]');
      if (!card) return;
      event.preventDefault();
      navigateTo(card.dataset.url);
    });

    // The result cards are clickable divs, so Enter/Space must work too.
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const card = event.target.closest && event.target.closest('[data-action="libsearch-navigate"]');
      if (!card || card.tagName === 'BUTTON') return;
      event.preventDefault();
      navigateTo(card.dataset.url);
    });
  }

  /** Run the server-supplied ?q= on load (read from #page-data). */
  function runInitialQuery() {
    const pd = el('page-data');
    if (!pd) return;

    let initialQuery = '';
    try {
      initialQuery = (JSON.parse(pd.textContent) || {}).initialQuery || '';
    } catch (error) {
      console.error('[library-search] #page-data is not valid JSON:', error.message);
      return;
    }
    if (!initialQuery) return;

    const input = el('searchQuery');
    if (input) input.value = initialQuery;
    performSearch(null);
  }

  document.addEventListener('DOMContentLoaded', function () {
    bindSearchInput();
    bindActions();
    runInitialQuery();
  });

  global.librarySearch = {
    performSearch,
    displaySearchResults,
    navigateTo,
  };
})(window);
