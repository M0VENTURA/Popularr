/* ==========================================================================
   static/js/pages/similar-artists.js
   Downloads ▸ Similar Artists — artists recommended by your own collection
   that are not in the library yet, sorted by how many of your artists
   recommend them.

   Load order: utils/dom.js → utils/api.js → ui/toast.js → services/musicbrainz-queue.js
   Then this file. (musicbrainz-queue.js publishes global.searchMusicBrainzRelease,
   which the Search button calls.)

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/downloads/similar_artists.html carried a 90-line inline
   <script> with its own escapeHtml, and loaded the legacy js/downloads.js.
   The script is this file. js/downloads.js is not loaded: the page never used
   anything from it, and it became pages/download-queue.js (a whole page's worth
   of queue rendering).

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. `escapeHtml` DID NOT ESCAPE QUOTES, AND WAS USED IN ATTRIBUTES. It was:

          div.appendChild(document.createTextNode(String(text || '')));
          return div.innerHTML;

      which escapes `&`, `<` and `>` and leaves `"` and `'` alone — and the
      result went into four attribute positions:

          title="${escapeHtml(artist.name)}"
          alt="${escapeHtml(artist.name)}"
          data-artist-name="${escapeHtml(artist.name)}"

      An artist name containing a double quote closed the attribute early and
      the remainder became live markup. Artist names come straight from the
      recommendation API, i.e. they are external input. Now uses
      utils/dom.js's escapeHtml, which covers all five entities.

   2. `fetch` → `api.getJson`. The original checked `resp.ok` but called
      `.json()` unconditionally, so an HTML error page (session expiry) was
      reported as a JSON problem.

   3. THE SEARCH BUTTONS WERE BOUND PER RENDER. After building the cards the
      script ran `container.querySelectorAll(...).forEach(addEventListener)`.
      That is fine while the container is only painted once, but it means a
      second render (a refresh) either double-binds or, if the code moves,
      silently stops working. One delegated listener now covers every card,
      however many times the grid is rebuilt.

   4. `searchMusicBrainzRelease` WAS CALLED WITHOUT LOADING THE MODULE THAT
      DEFINES IT. The page loaded js/downloads.js, which did not define it; the
      call was guarded by `typeof`, so the Search button did nothing at all.
      musicbrainz-queue.js is now loaded by the template — see its script block.

   ── WHAT DELIBERATELY STAYS ───────────────────────────────────────────────
   * The card markup, including the inline `aspect-ratio` and gradient styles,
     was moved verbatim. Those could go to a stylesheet, but they are the only
     thing this page paints and it is not worth a file.
   * The artist image falls back to a person icon via `onerror`, which shows the
     SIBLING element (`nextElementSibling`) — so the fallback div must stay
     immediately after the <img>.
   ========================================================================== */

(function (global) {
  'use strict';

  const ENDPOINT = '/api/library/artists/similar';
  /** How many "via <artist>" credits to show per card before "& more". */
  const MAX_CREDITORS = 3;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function container() {
    return document.getElementById('similarArtistsDiscoverContainer');
  }

  function note(html, tone) {
    const el = container();
    if (el) el.innerHTML = `<div class="alert alert-${tone} shadow-sm">${html}</div>`;
  }

  function card(artist) {
    const name = artist.name || '';
    const imageUrl = `/api/artist/image?name=${encodeURIComponent(name)}`;

    const countBadge = artist.count > 1
      ? `<span class="badge bg-warning text-dark position-absolute shadow-sm" style="top: 10px; right: 10px;"
               title="Recommended by ${esc(String(artist.count))} of your artists">${esc(String(artist.count))}×</span>`
      : '';

    const allCreditors = artist.from_artists || [];
    const creditors = allCreditors.slice(0, MAX_CREDITORS).map(esc).join(', ');
    const creditorNote = creditors
      ? `<small class="text-muted d-block mt-2 lh-sm">Via: ${creditors}${allCreditors.length > MAX_CREDITORS ? ' &amp; more' : ''}</small>`
      : '';

    return `
      <div class="col-6 col-md-4 col-lg-3 col-xl-2">
        <div class="card h-100 overflow-hidden shadow-sm" style="position: relative;">
          ${countBadge}
          <div style="aspect-ratio: 1; background: linear-gradient(135deg, #2b2b2b 0%, #1a1a1a 100%); display: flex; align-items: center; justify-content: center; position: relative;">
            <img src="${esc(imageUrl)}" alt="${esc(name)}" style="width: 100%; height: 100%; object-fit: cover;"
                 onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
            <div style="display: none; align-items: center; justify-content: center; width: 100%; height: 100%;">
              <i class="bi bi-person-circle" style="font-size: 4rem; color: #444;"></i>
            </div>
          </div>
          <div class="card-body p-3 d-flex flex-column">
            <h6 class="card-title mb-0 fw-bold text-truncate" title="${esc(name)}">${esc(name)}</h6>
            ${creditorNote}
            <div class="btn-group w-100 mt-auto pt-3">
              <button type="button" class="btn btn-outline-primary btn-sm"
                      data-action="sa-find-releases"
                      data-artist-name="${esc(name)}"
                      title="Search MusicBrainz for releases">
                <i class="bi bi-cloud-download"></i> Search
              </button>
              <a href="https://www.last.fm/music/${encodeURIComponent(name)}" target="_blank" rel="noopener"
                 class="btn btn-outline-secondary btn-sm" title="View on Last.fm">
                <i class="bi bi-box-arrow-up-right"></i>
              </a>
            </div>
          </div>
        </div>
      </div>`;
  }

  async function loadSimilarArtistsDiscover() {
    const el = container();
    if (!el) return;

    try {
      const data = await global.api.getJson(ENDPOINT);

      const all = data.similar_artists || [];
      if (!data.success || !all.length) {
        note('<i class="bi bi-exclamation-circle me-1"></i> No similar artists data found. ' +
          'Scan some artists first to populate recommendations.', 'warning');
        return;
      }

      const notInCollection = all.filter((a) => !a.in_collection);
      if (!notInCollection.length) {
        note('<i class="bi bi-check-circle-fill me-1"></i> Great news — all similar artists ' +
          'recommended by your collection are already in your library!', 'success');
        return;
      }

      el.innerHTML = `
        <div class="d-flex justify-content-between align-items-center mb-3">
          <span class="text-muted fw-semibold">${notInCollection.length} artist${notInCollection.length !== 1 ? 's' : ''} recommended but not in your collection</span>
        </div>
        <div class="row g-3">${notInCollection.map(card).join('')}</div>`;
    } catch (error) {
      note(`<i class="bi bi-exclamation-triangle-fill me-1"></i> Failed to load similar artists: ${esc(error.message)}`, 'danger');
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;
    const btn = event.target.closest('[data-action="sa-find-releases"]');
    if (!btn) return;

    event.preventDefault();
    const artistName = btn.dataset.artistName;
    if (!artistName) return;

    if (typeof global.searchMusicBrainzRelease !== 'function') {
      global.toast.error('MusicBrainz search is not available on this page.');
      return;
    }
    global.searchMusicBrainzRelease(null, artistName, '');
  });

  document.addEventListener('DOMContentLoaded', loadSimilarArtistsDiscover);

  global.similarArtistsDiscover = { loadSimilarArtistsDiscover };
})(window);
