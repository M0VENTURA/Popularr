/* ==========================================================================
   static/js/pages/analytics.js
   Genres & Moods analytics — the three bar lists (genres, moods, genre×mood).

   Load order: utils/dom.js → utils/api.js, then this file. Both come from
   base.html.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/analytics.html carried a 46-line inline <script> and a
   ~67-line inline <style> block. The script is this file; the CSS is
   static/css/analytics.css.

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. THE LABEL WAS ESCAPED FOR THE WRONG CONTEXT. The original did:

          const name = (item.name || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');

      which escapes `<` and `>` but NOT quotes — and that same `name` was then
      interpolated into an attribute:

          <div class="metric-label" title="${name}">${name}</div>

      So a genre containing a double quote closed the title attribute early and
      the rest of the value became live markup. Genre names are user-editable
      (the genre-management page writes them) and arrive from /api/analytics,
      so this was reachable. utils/dom.js's escapeHtml escapes all five
      entities and is safe in both text and attribute position.

   2. `fetch` + `.json()` → `api.getJson()`. The old version had NO error
      handling at all: a non-JSON response (session expiry, HTML error page)
      rejected the promise inside a DOMContentLoaded handler and the page just
      sat there with three empty cards and a JS error in the console.

   3. The three counts were written with `document.getElementById(...)` on
      unguarded elements; they now no-op if a card is missing, so a future
      layout change cannot throw mid-render.
   ========================================================================== */

(function (global) {
  'use strict';

  const ENDPOINT = '/api/analytics/genres-moods';
  const LIMIT = 50;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  /**
   * Render one bar list.
   *
   * @param {string} targetId
   * @param {Array<{name: string, count: number}>} items
   * @param {string} fillClass  'genre' | 'mood' | 'combo' (see analytics.css)
   */
  function renderBars(targetId, items, fillClass) {
    const container = document.getElementById(targetId);
    if (!container) return;

    if (!items || !items.length) {
      container.innerHTML = '<div class="empty-state">No data available yet.</div>';
      return;
    }

    const counts = items.map((item) => Number(item.count) || 0);
    const maxCount = Math.max.apply(null, counts.concat([1]));

    container.innerHTML = items.map((item) => {
      const name = esc(item.name);
      const count = Number(item.count) || 0;
      // A floor of 2% so a non-zero count is still a visible sliver rather
      // than an invisible bar.
      const width = Math.max(2, Math.round((count / maxCount) * 100));
      return `
        <div class="metric-row">
          <div class="metric-label" title="${name}">${name}</div>
          <div class="metric-track">
            <div class="metric-fill ${esc(fillClass)}" style="width:${width}%"></div>
          </div>
          <div class="metric-value">${count}</div>
        </div>`;
    }).join('');
  }

  function setCount(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  async function loadAnalytics() {
    // Show something during the request — three empty cards read as "broken".
    ['genresBars', 'moodsBars', 'combosBars'].forEach((id) => {
      const container = document.getElementById(id);
      if (container) {
        container.innerHTML = '<div class="empty-state">Loading…</div>';
      }
    });

    try {
      const data = await global.api.getJson(`${ENDPOINT}?limit=${LIMIT}`);
      const genres = data.genres || [];
      const moods = data.moods || [];
      const combos = data.combos || [];

      setCount('genresCount', genres.length);
      setCount('moodsCount', moods.length);
      setCount('combosCount', combos.length);

      renderBars('genresBars', genres, 'genre');
      renderBars('moodsBars', moods, 'mood');
      renderBars('combosBars', combos, 'combo');
    } catch (error) {
      ['genresBars', 'moodsBars', 'combosBars'].forEach((id) => {
        const container = document.getElementById(id);
        if (container) {
          container.innerHTML =
            `<div class="empty-state text-danger">Could not load: ${esc(error.message)}</div>`;
        }
      });
      if (global.toast) global.toast.error('Could not load analytics: ' + error.message);
    }
  }

  document.addEventListener('DOMContentLoaded', loadAnalytics);

  global.loadAnalytics = loadAnalytics;
  global.renderAnalyticsBars = renderBars;
})(window);
