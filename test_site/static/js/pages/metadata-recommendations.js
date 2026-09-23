/* ==========================================================================
   test_site/static/js/pages/metadata-recommendations.js
   Artist page — per-album MusicBrainz recommendations awaiting review.

   When the Config page has metadata updating set to "Recommend only", a scan
   does not apply the MusicBrainz metadata it resolves; it stores what it would
   have changed on the album's track rows. This lists the affected albums with
   a count and links each one to the album page, where the full review (orange
   bars + Save/Discard) lives.

   Deliberately READ-ONLY: saving and discarding happen on the album page,
   against one album's actual tracklist. Offering "apply all" here would apply
   changes the user has not seen.
   ========================================================================== */

(function (global) {
  'use strict';

  const ENDPOINT = '/api/artist/metadata-recommendations';

  function esc(value) {
    if (global.escapeHtml) return global.escapeHtml(value);
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function artistName() {
    const holder = document.querySelector('[data-artist-name]');
    if (holder && holder.dataset.artistName) return holder.dataset.artistName;
    return global._pageData ? global._pageData.artistName : '';
  }

  function albumUrl(artist, album) {
    return '/album/' + encodeURIComponent(artist) + '/' + encodeURIComponent(album);
  }

  async function load() {
    const host = document.getElementById('artistMetadataRecommendations');
    if (!host) return null;

    const artist = artistName();
    if (!artist) return null;

    let data;
    try {
      const resp = await fetch(ENDPOINT + '?artist=' + encodeURIComponent(artist));
      const text = await resp.text();
      try {
        data = JSON.parse(text);
      } catch (_e) {
        return null;
      }
    } catch (_e) {
      return null;
    }

    if (!data || !data.success || !data.albums || !data.albums.length) {
      host.classList.add('d-none');
      return null;
    }

    const rows = data.albums.map((album) => `
      <div class="d-flex align-items-center gap-2">
        <span class="text-warning-emphasis">
          <i class="bi bi-lightning-fill me-1"></i>${esc(String(album.track_count))}
        </span>
        <a href="${albumUrl(data.artist, album.album)}"
           class="text-light text-decoration-none flex-grow-1">${esc(album.album)}</a>
        <a href="${albumUrl(data.artist, album.album)}"
           class="btn btn-warning btn-sm py-0 px-2" style="font-size:.7rem;">
          <i class="bi bi-eye"></i> Review
        </a>
      </div>`).join('');

    host.className = 'card-body py-2';
    host.innerHTML =
      '<div class="alert alert-warning mb-0 py-2">' +
        '<div class="mb-2" style="font-size:.85rem;">' +
          '<i class="bi bi-lightning-fill"></i> ' +
          '<strong>MusicBrainz updates are waiting for review</strong> ' +
          '<span class="text-muted">— ' + esc(String(data.album_count)) + ' album(s), ' +
          esc(String(data.total)) + ' track(s). Nothing has been changed yet.</span>' +
        '</div>' +
        '<div class="d-flex flex-column gap-1">' + rows + '</div>' +
      '</div>';
    return data;
  }

  global.artistMetadataRecommendations = { load };

  document.addEventListener('DOMContentLoaded', () => {
    const run = () => load().catch(() => {});
    // Idle-deferred: this is a secondary panel and must never delay the
    // artist's release sections from rendering.
    if (global.requestIdleCallback) global.requestIdleCallback(run);
    else setTimeout(run, 400);
  });
})(window);
