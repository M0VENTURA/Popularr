/* ==========================================================================
   static/js/pages/discover.js
   Discover — personalised playlist suggestions grouped into four sections
   (similar artists, top genres, mood playlists, discovery), each card able to
   be saved as a real playlist.

   Load order: utils/dom.js → utils/api.js → ui/toast.js → ui/button-state.js,
   then this file. All come from base.html.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/discover.html carried a 233-line inline <script> and its own
   toast implementation:
     * the script is this file
     * `showToast()` + the `#discoverToastContainer` markup are GONE. That was a
       fourth toast implementation (after main.js, downloads.js and
       bookmarks.html). ui/toast.js is global and already does the stack, the
       auto-dismiss and the dismiss button.
     * `escDiscover()` is gone with it. ui/toast.js writes titles and messages
       with textContent, so it escapes internally — meaning this module should
       pass PLAIN TEXT, not HTML. The original built `<strong>`/`<code>` markup
       and hand-escaped it into a toast that then injected it with innerHTML.

   No CSS file is needed: the script only ever applies Bootstrap utility classes.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. `loadRecommendations` DID NOT CHECK `resp.ok`, and every element it touched
      was fetched without a guard:

          elLoading.style.display = "block";

      With a missing element that throws on the first line, before the try, so
      the failure is not even caught by the function's own catch — the page just
      sits on the spinner. Everything now goes through byId() with a
      `setDisplay()` helper.

   2. THE REFRESH BUTTON WAS AN INLINE onclick (`onclick="loadRecommendations(true)"`)
      AND WAS NEVER RE-DISABLED. It is a data-action now, and the disabled state
      is set in a `finally` — the original set it at the top and relied on the
      success path to clear it, so a thrown error left it disabled.

   3. `fetch` + unconditional `.json()` → api.getJson / api.postJson.

   4. The card's Save button was bound with addEventListener per card, which is
      correct for one render but means a re-render must re-bind everything. The
      handler is delegated now (one listener, any number of renders) — the card
      still carries `data-key`/`data-index` for the lookup.
   ========================================================================== */

(function (global) {
  'use strict';

  const RECS_ENDPOINT = '/api/recommended-playlists';
  const CREATE_ENDPOINT = '/api/recommended-playlists/create';

  /** Section key -> DOM ids. The order is the order they render in. */
  const SECTIONS = [
    { key: 'similar_artists', sectionId: 'section-similar-artists', cardsId: 'cards-similar-artists' },
    { key: 'top_genres', sectionId: 'section-top-genres', cardsId: 'cards-top-genres' },
    { key: 'mood_playlists', sectionId: 'section-mood-playlists', cardsId: 'cards-mood-playlists' },
    { key: 'discovery', sectionId: 'section-discovery', cardsId: 'cards-discovery' },
  ];

  /**
   * sectionKey -> the `type` string /api/recommended-playlists/create expects.
   * Note these are NOT the same as the section keys (mood_playlists → "mood"),
   * which is why the map exists rather than passing the key through.
   */
  const CREATE_TYPES = {
    similar_artists: 'similar-artists',
    top_genres: 'top-genres',
    mood_playlists: 'mood',
    discovery: 'discovery',
  };

  /** Last loaded payload, keyed by section — the create call indexes into it. */
  let recsData = {};

  function byId(id) {
    return document.getElementById(id);
  }

  /** Safe style change: no-ops when the element is absent. */
  function setDisplay(id, value) {
    const el = byId(id);
    if (el) el.style.display = value;
  }

  // ── Load ────────────────────────────────────────────────────────────────

  async function loadRecommendations(forceRefresh) {
    const refreshBtn = byId('refreshBtn');
    if (refreshBtn) refreshBtn.disabled = true;

    setDisplay('discoverLoading', 'block');
    setDisplay('discoverError', 'none');
    setDisplay('discoverEmpty', 'none');
    setDisplay('discoverContent', 'none');

    try {
      const query = forceRefresh ? '?refresh=1' : '';
      const data = await global.api.getJson(`${RECS_ENDPOINT}${query}`);
      if (!data.success) throw new Error(data.error || 'Unknown error');

      recsData = data.recommendations || {};

      let hasAny = false;
      SECTIONS.forEach(({ key, sectionId, cardsId }) => {
        const playlists = recsData[key] || [];
        if (playlists.length) {
          hasAny = true;
          renderSection(sectionId, cardsId, key, playlists);
        } else {
          setDisplay(sectionId, 'none');
        }
      });

      setDisplay('discoverLoading', 'none');
      setDisplay(hasAny ? 'discoverContent' : 'discoverEmpty', 'block');
    } catch (error) {
      console.error('[discover] load failed:', error);
      setDisplay('discoverLoading', 'none');
      setDisplay('discoverError', 'block');
    } finally {
      // Restored here rather than on the success path, so a thrown error cannot
      // leave Refresh permanently disabled (bug 2).
      if (refreshBtn) refreshBtn.disabled = false;
    }
  }

  function renderSection(sectionId, cardsId, sectionKey, playlists) {
    const section = byId(sectionId);
    const container = byId(cardsId);
    if (!section || !container) return;

    container.innerHTML = '';
    playlists.forEach((playlist, index) => {
      container.appendChild(buildCard(sectionKey, index, playlist));
    });

    section.style.display = 'block';
  }

  // ── Cards ───────────────────────────────────────────────────────────────

  function buildCard(sectionKey, index, playlist) {
    const name = String(playlist.name || playlist.title || 'Unnamed');
    const description = String(playlist.description || '');
    const icon = String(playlist.icon || '🎵');
    const trackCount = parseInt(playlist.track_count || playlist.tracks || 0, 10);

    const col = document.createElement('div');
    col.className = 'col-12 col-sm-6 col-lg-4 col-xl-3';

    const card = document.createElement('div');
    card.className = 'card h-100 shadow-sm';
    const body = document.createElement('div');
    body.className = 'card-body d-flex flex-column';

    const top = document.createElement('div');
    top.className = 'd-flex align-items-start gap-2 mb-2';

    // textContent for the icon: it is an emoji from the API, and this way it
    // cannot be anything else.
    const iconEl = document.createElement('span');
    iconEl.style.cssText = 'font-size:1.5rem;line-height:1;';
    iconEl.textContent = icon;
    top.appendChild(iconEl);

    const titleWrap = document.createElement('div');
    titleWrap.className = 'flex-grow-1 min-width-0';

    const titleEl = document.createElement('h6');
    titleEl.className = 'card-title mb-1 text-truncate';
    titleEl.title = name;
    titleEl.textContent = name;
    titleWrap.appendChild(titleEl);

    if (description) {
      const descEl = document.createElement('p');
      descEl.className = 'text-muted small mb-0';
      descEl.textContent = description;
      titleWrap.appendChild(descEl);
    }

    top.appendChild(titleWrap);
    body.appendChild(top);

    const footer = document.createElement('div');
    footer.className = 'mt-auto pt-2';

    const badgeRow = document.createElement('div');
    badgeRow.className = 'd-flex justify-content-between align-items-center mb-2';
    const badge = document.createElement('span');
    badge.className = 'badge bg-secondary';
    badge.textContent = `${trackCount} track${trackCount !== 1 ? 's' : ''}`;
    badgeRow.appendChild(badge);
    footer.appendChild(badgeRow);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm btn-outline-primary w-100';
    btn.innerHTML = '<i class="bi bi-plus-circle me-1"></i>Save as Playlist';
    // The delegated listener reads these — see the wiring section.
    btn.dataset.action = 'discover-create';
    btn.dataset.sectionKey = sectionKey;
    btn.dataset.index = String(index);
    btn.dataset.playlistName = name;
    footer.appendChild(btn);

    body.appendChild(footer);
    card.appendChild(body);
    col.appendChild(card);
    return col;
  }

  // ── Save as playlist ────────────────────────────────────────────────────

  async function createPlaylist(sectionKey, index, playlistName, button) {
    const type = CREATE_TYPES[sectionKey] || sectionKey;

    return global.buttonState.withBusy(button, 'Saving…', async () => {
      try {
        const data = await global.api.postJson(CREATE_ENDPOINT, {
          type,
          index,
          name: playlistName,
        });

        if (!data.success) throw new Error(data.error || 'Failed to create playlist');

        const count = data.track_count || 0;
        const path = data.output_path || '/music/playlists';
        // PLAIN TEXT — ui/toast.js renders with textContent, so markup here
        // would show up literally. That is also why escDiscover was deleted.
        global.toast.success(`${count} track${count === 1 ? '' : 's'} at ${path}`, `"${playlistName}" saved`);

        if (button) {
          button.innerHTML = '<i class="bi bi-check-circle me-1"></i>Saved!';
          button.classList.replace('btn-outline-primary', 'btn-success');
        }
      } catch (error) {
        console.error('[discover] createPlaylist failed:', error);
        global.toast.error('Failed to save playlist: ' + error.message);
      }
    });
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;

    const refresh = event.target.closest('[data-action="discover-refresh"]');
    if (refresh) {
      event.preventDefault();
      loadRecommendations(true);
      return;
    }

    const save = event.target.closest('[data-action="discover-create"]');
    if (!save) return;
    event.preventDefault();
    createPlaylist(save.dataset.sectionKey, parseInt(save.dataset.index, 10) || 0, save.dataset.playlistName, save);
  });

  document.addEventListener('DOMContentLoaded', () => loadRecommendations(false));

  global.discover = {
    loadRecommendations,
    renderSection,
    buildCard,
    createPlaylist,
    SECTIONS,
  };
})(window);
