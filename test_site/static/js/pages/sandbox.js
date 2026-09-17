/* ==========================================================================
   static/js/pages/sandbox.js
   Algorithm Sandbox — simulate popularity weight changes against live data
   without writing anything.

   Load order: utils/dom.js → utils/api.js → ui/toast.js, then this file.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/sandbox.html carried a 233-line inline IIFE with 11
   functions. The script is this file. There was no inline <style>, but the
   classes the script emits had no definitions anywhere — see css/sandbox.css.

   ── BUGS FIXED / GAPS CLOSED ──────────────────────────────────────────────
   1. `.sandbox-delta.upgrade` / `.downgrade` WERE UNSTYLED. The script toggles
      those classes on every row and NOTHING defined them (grepped: zero
      matches across every stylesheet). Since highlighting the rows a weight
      change would affect is the entire purpose of the page, every row looked
      identical. css/sandbox.css supplies them.

   2. `updateSummary` CLOBBERED THE DELTA SPAN'S CLASS. It did
      `el.className = 'text-success'` / `'text-danger'`, discarding the
      `text-muted` the markup put there — and on a delta of 0 it left whatever
      the previous colour was. It now keeps `text-muted` as the base and only
      swaps the tone, restoring it when the delta returns to zero.

   3. UNGUARDED `document.getElementById(...)` THROUGHOUT — `syncSliderUI()`
      dereferenced six elements and the slider-binding loop dereferenced three
      more, with no null checks. A single missing element threw during setup and
      the whole page (including the initial load) never ran. Everything now goes
      through `byId()`.

   4. `fetch` → `api.getJson`. The original checked `resp.ok` but called
      `.json()` first, so an HTML error page surfaced as a JSON parse error.

   5. The slider binding used an IIFE-inside-a-for-loop to capture the key. That
       was correct but obscure; it is a named helper now.

   ── THE ALGORITHM MIRROR ──────────────────────────────────────────────────
   Z_MID / Z_SCALE / MIN_SPREAD / LOGISTIC_K / SINGLE_BOOST are a deliberate
   transcription of services/popularity/popularity_math.py. This page's value is
   that its numbers MATCH the backend, so do not "tidy" them — if the Python
   constants change, change these in the same commit. The page's own banner
   already warns that artist-z standout marking and the era 5★ caps are excluded,
   so results can differ slightly from a real scan.
   ========================================================================== */

(function (global) {
  'use strict';

  const METRICS_ENDPOINT = '/api/sandbox/metrics';

  // ── Mirrors services/popularity/popularity_math.py — see the header. ────
  const Z_MID = 50.0;
  const Z_SCALE = 16.7;
  const MIN_SPREAD = 8.0;
  const LOGISTIC_K = Z_SCALE / 25.0; // 0.668
  const SINGLE_BOOST = 1.15;

  const SLIDERS = ['wLf', 'wLb', 'wAge'];
  const KEYS = ['lf', 'lb', 'age'];
  const PCT_IDS = { wLf: 'wLfPct', wLb: 'wLbPct', wAge: 'wAgePct' };

  const weights = { lf: 0.55, lb: 0.35, age: 0.10 };

  let tracks = [];
  /** Parallel DOM refs, so a slider drag only patches textContent. */
  let nodes = [];

  function byId(id) {
    return document.getElementById(id);
  }

  // ── The maths ───────────────────────────────────────────────────────────

  function zscoreToPopularity(z) {
    if (z <= 0) {
      return Math.min(100, Math.max(0, Z_MID + z * Z_SCALE));
    }
    return Math.min(100, Math.max(0, 100 / (1 + Math.exp(-LOGISTIC_K * z))));
  }

  function starsFromZ(z, single) {
    if ((single && z >= 1.0) || z >= 1.5) return 5;
    if (z >= 0.5) return 4;
    if (z >= -0.5) return 3;
    if (z >= -1.2) return 2;
    return 1;
  }

  function starsLabel(n) {
    const clamped = Math.max(1, Math.min(5, Math.round(Number(n) || 0)));
    return '★'.repeat(clamped) + '☆'.repeat(5 - clamped);
  }

  function fmt(value) {
    return (Number(value) || 0).toFixed(1);
  }

  // ── Load ────────────────────────────────────────────────────────────────

  async function loadMetrics() {
    const listEl = byId('sandboxTrackList');
    const scopeEl = byId('sandboxScope');
    const artistEl = byId('sandboxArtist');
    const statusEl = byId('sandboxStatus');
    if (!listEl || !scopeEl || !statusEl) return;

    const scope = scopeEl.value;
    const params = new URLSearchParams({ scope });

    if (scope === 'artist') {
      const artist = artistEl ? artistEl.value.trim() : '';
      if (!artist) {
        statusEl.textContent = 'Enter an artist name first.';
        return;
      }
      params.set('artist', artist);
    }

    listEl.innerHTML =
      '<div class="text-center text-muted py-5"><i class="bi bi-hourglass-split d-block mb-2 fs-4"></i>Loading track metrics…</div>';
    statusEl.textContent = 'Fetching metrics…';

    try {
      const data = await global.api.getJson(`${METRICS_ENDPOINT}?${params.toString()}`);

      tracks = data.tracks || [];
      if (data.truncated) {
        statusEl.textContent =
          `Showing first ${tracks.length} tracks (library larger than the sandbox cap) — use an artist scope for the full picture.`;
      } else {
        statusEl.textContent =
          `${tracks.length.toLocaleString()} track(s) loaded — drag a slider to simulate.`;
      }

      buildList();
      recalculate();
    } catch (error) {
      listEl.innerHTML =
        `<div class="alert alert-danger py-2 small mb-0">${global.escapeHtml ? global.escapeHtml(error.message) : error.message}</div>`;
      statusEl.textContent = '';
    }
  }

  // ── Build the row DOM once ──────────────────────────────────────────────

  function buildList() {
    const listEl = byId('sandboxTrackList');
    if (!listEl) return;

    listEl.innerHTML = '';
    nodes = [];

    tracks.forEach((track) => {
      const card = document.createElement('div');
      card.className = 'border rounded p-2 sandbox-delta';

      const header = document.createElement('div');
      header.className = 'd-flex justify-content-between align-items-baseline gap-2';

      const title = document.createElement('strong');
      title.className = 'text-truncate';
      // textContent, never innerHTML: track titles are user data.
      title.textContent = track.title || 'Unknown Track';

      const sub = document.createElement('span');
      sub.className = 'small text-muted text-nowrap';
      sub.textContent = track.artist || '';

      header.appendChild(title);
      header.appendChild(sub);

      const compare = document.createElement('div');
      compare.className = 'small d-flex align-items-center gap-2 flex-wrap';

      const oldEl = document.createElement('span');
      oldEl.className = 'score-old text-muted';
      oldEl.textContent = `Live: ${starsLabel(track.stars)} (${fmt(track.score)})`;

      const arrow = document.createElement('span');
      arrow.className = 'score-arrow';
      arrow.textContent = '➔';

      const newEl = document.createElement('span');
      newEl.className = 'score-new';
      newEl.textContent = `Sim: ${starsLabel(track.stars)} (${fmt(track.score)})`;

      compare.appendChild(oldEl);
      compare.appendChild(arrow);
      compare.appendChild(newEl);

      card.appendChild(header);
      card.appendChild(compare);
      listEl.appendChild(card);

      nodes.push({ card, newEl, live: track.stars || 0, changed: false });
    });
  }

  // ── The engine ──────────────────────────────────────────────────────────

  function recalculate() {
    const onlyChangesEl = byId('sandboxOnlyChanges');
    const statusEl = byId('sandboxStatus');
    const onlyChanges = !!(onlyChangesEl && onlyChangesEl.checked);

    const dist = { 5: 0, 4: 0, 3: 0, 2: 0, 1: 0 };
    let changed = 0;

    tracks.forEach((track, i) => {
      let raw = (track.lf || 0) * weights.lf
        + (track.lb || 0) * weights.lb
        + (track.age || 0) * weights.age;
      if (track.single) raw *= SINGLE_BOOST;

      let simScore = raw;
      let z = 0;

      // Album-relative re-map, exactly as popularity_math.py does it.
      if (track.a_med != null && track.a_med > 0) {
        const spread = Math.max((track.a_mad || 0) * 1.4826, MIN_SPREAD);
        z = (raw - track.a_med) / spread;
        simScore = zscoreToPopularity(z);
      }

      const simStars = starsFromZ(z, !!track.single);
      dist[simStars] += 1;

      const node = nodes[i];
      if (!node) return;

      node.changed = simStars !== node.live;
      if (node.changed) changed += 1;

      node.newEl.textContent = `Sim: ${starsLabel(simStars)} (${fmt(simScore)})`;
      node.card.classList.toggle('upgrade', simStars > node.live);
      node.card.classList.toggle('downgrade', simStars < node.live);
      node.card.classList.toggle('d-none', onlyChanges && !node.changed);
    });

    updateSummary(dist);

    if (onlyChanges && statusEl) {
      statusEl.textContent =
        `${changed.toLocaleString()} track(s) would change star rating (of ${tracks.length.toLocaleString()}).`;
    }
  }

  function liveDistribution() {
    const dist = { 5: 0, 4: 0, 3: 0, 2: 0, 1: 0 };
    tracks.forEach((track) => {
      const stars = Math.max(1, Math.min(5, Math.round(Number(track.stars) || 0)));
      dist[stars] += 1;
    });
    return dist;
  }

  function updateSummary(dist) {
    const live = liveDistribution();

    // 2★ and 1★ are reported as one combined bucket.
    const rows = [
      { liveId: 'sumLive5', star: 5, deltaId: 'sumDelta5' },
      { liveId: 'sumLive4', star: 4, deltaId: 'sumDelta4' },
      { liveId: 'sumLive3', star: 3, deltaId: 'sumDelta3' },
      { liveId: 'sumLive21', star: 2, deltaId: 'sumDelta21' },
    ];

    rows.forEach((row) => {
      const combined = row.star === 2;
      const liveCount = live[row.star] + (combined ? live[1] : 0);
      const delta = (dist[row.star] - live[row.star])
        + (combined ? (dist[1] - live[1]) : 0);

      const liveEl = byId(row.liveId);
      if (liveEl) liveEl.textContent = liveCount.toLocaleString();

      const deltaEl = byId(row.deltaId);
      if (!deltaEl) return;

      // Keep `text-muted` as the base class and only swap the tone — the
      // original replaced className outright and never restored it (bug 2).
      if (delta === 0) {
        deltaEl.textContent = '';
        deltaEl.className = 'text-muted';
      } else if (delta > 0) {
        deltaEl.textContent = '▲ +' + delta;
        deltaEl.className = 'text-success';
      } else {
        deltaEl.textContent = '▼ ' + delta;
        deltaEl.className = 'text-danger';
      }
    });
  }

  // ── Slider auto-balance ─────────────────────────────────────────────────
  //
  // Dragging one weight scales the other two so the three always sum to 1.0 —
  // which is what the backend does with configured weights.

  function sliderChanged(changedKey, newValue) {
    const value = Math.min(1, Math.max(0, Number(newValue) || 0));
    const others = KEYS.filter((key) => key !== changedKey);
    const othersSum = others.reduce((sum, key) => sum + weights[key], 0);

    if (othersSum <= 0) {
      others.forEach((key) => { weights[key] = 0; });
    } else {
      const scale = (1 - value) / othersSum;
      others.forEach((key) => {
        weights[key] = Math.min(1, Math.max(0, weights[key] * scale));
      });
    }
    weights[changedKey] = value;

    syncSliderUI();
    recalculate();
  }

  function syncSliderUI() {
    SLIDERS.forEach((sliderId, i) => {
      const key = KEYS[i];
      const slider = byId(sliderId);
      if (slider) slider.value = weights[key];

      const pct = byId(PCT_IDS[sliderId]);
      if (pct) pct.textContent = Math.round(weights[key] * 100) + '%';
    });
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  function bindSlider(sliderId, key) {
    const slider = byId(sliderId);
    if (!slider) return;
    slider.addEventListener('input', function () {
      sliderChanged(key, parseFloat(this.value));
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    SLIDERS.forEach((sliderId, i) => bindSlider(sliderId, KEYS[i]));

    const scopeEl = byId('sandboxScope');
    const artistEl = byId('sandboxArtist');
    if (scopeEl && artistEl) {
      scopeEl.addEventListener('change', function () {
        artistEl.classList.toggle('d-none', this.value !== 'artist');
      });
    }

    const loadBtn = byId('sandboxLoadBtn');
    if (loadBtn) loadBtn.addEventListener('click', loadMetrics);

    const onlyChangesEl = byId('sandboxOnlyChanges');
    if (onlyChangesEl) onlyChangesEl.addEventListener('change', recalculate);

    syncSliderUI();
    loadMetrics();
  });

  global.sandbox = {
    loadMetrics,
    recalculate,
    zscoreToPopularity,
    starsFromZ,
    starsLabel,
  };
})(window);
