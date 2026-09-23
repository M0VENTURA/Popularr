/* ==========================================================================
   static/js/pages/artists.js
   Artist list page — Metro jump grid and scan-from-letter.

   Requires: utils/api.js, ui/toast.js, ui/confirm.js

   ── CHANGES FROM THE PREVIOUS VERSION ─────────────────────────────────────
   1. `confirm()` → `ui.confirm()` (async). scanLetterArtists is already
      `async`, so the added `await` is safe.
   2. The confirmation message contained a literal `\n\n` inside a
      single-quoted string built by concatenation — that one was correct, but
      it is now a structured `detail` field so it cannot regress the way the
      `n`-instead-of-`\n` strings elsewhere in the codebase did.
   3. `fetch` + manual `.json()` → `api.postJson`, which surfaces an HTML
      error page as a readable message instead of a JSON parse error.
   4. `alert()` → toast.
   ========================================================================== */

(function (global) {
  'use strict';

  function overlay() {
    return document.getElementById('metroJumpOverlay');
  }

  /** Open the jump grid. */
  function openJumpPicker() {
    const el = overlay();
    if (el) el.classList.remove('d-none');
  }

  /**
   * Close the jump grid. Ignores clicks that land inside the panel.
   * @param {Event} [event]
   */
  function closeJumpPicker(event) {
    if (event && event.target !== event.currentTarget && !event.target.closest('.btn-close')) {
      return;
    }
    const el = overlay();
    if (el) el.classList.add('d-none');
  }

  /**
   * Scroll to a letter section.
   * @param {string} letter  a letter, or '#' for the numeric bucket
   */
  function jumpToLetter(letter) {
    closeJumpPicker();
    const sectionId = letter === '#' ? 'section-num' : 'section-' + letter;
    const target = document.getElementById(sectionId);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /**
   * Start a scan beginning at the first artist under a letter.
   * @param {string} letter
   * @param {string} scanMode  'forced' for a full rescan, anything else = changes only
   */
  async function scanLetterArtists(letter, scanMode) {
    const fullScan = scanMode === 'forced';
    const scanModeValue = fullScan ? 'forced' : 'changes';

    // Gate on a running scan FIRST: offering to cancel it is the whole point,
    // and asking twice (gate + the letter confirm below) would be noise, so the
    // gate's own dialog replaces the generic one when a scan is active.
    if (global.ScanPreflight) {
      const proceed = await global.ScanPreflight.confirmIfRunning({
        scanName: 'Artist Scan',
      });
      if (!proceed) return;
    }

    const accepted = await global.ui.confirm({
      title: 'Start scan',
      message: `Start a ${fullScan ? 'Full (Forced)' : 'Changes'} scan from letter "${letter}"?`,
      detail: 'This resolves the first matching artist in your local library and scans from there.',
      tone: 'primary',
      confirmLabel: 'Start scan',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson('/api/scan/from-artist', {
        letter: letter,
        scan_mode: scanModeValue,
      });

      if (data.success) {
        global.toast.success(
          `Starting from ${data.artist}. Check the dashboard for progress.`,
          `Scan started (${data.mode})`
        );
      } else {
        global.toast.error(data.error || 'Could not start the scan');
      }
    } catch (error) {
      console.error('Error starting scan:', error);
      global.toast.error('Error starting scan: ' + error.message);
    }
  }

  // ── Data-quality indicators ("needs fixing") ────────────────────────────
  //
  // WHY THIS IS HERE: GET /api/artists/corrections already computes exactly
  // this — per-artist duplicate_track_count, disc_inconsistent_count,
  // mbid_inconsistent_count, missing_tracks_count and a needs_correction flag —
  // and its own docstring says "for the artist list page". But nothing ever
  // called it: `git grep artists/corrections` across the whole repo returned
  // only its route decorator. The numbers were computed on every request and
  // thrown away, so there was no way to SEE which artists had bad data without
  // opening each one.
  //
  // This annotates the server-rendered artist rows with what the API reports,
  // adds a reason tooltip, and provides a "needs fixing only" filter. Clicking
  // through still goes to the existing per-artist fixer (artist_corrections),
  // which is where the actual repairs live — this page's job is discovery.
  //
  // KEY MATCHING CAVEAT: the API groups by COALESCE(NULLIF(album_artist,''),
  // artist) — i.e. the ALBUM artist — while these rows come from the artists
  // query. They agree for the normal case, but a collaboration credited
  // differently at album level can miss. The row therefore carries BOTH its
  // display name and its link name as keys and we try each, lowercased.

  const CORRECTIONS_ENDPOINT = '/api/artists/corrections';
  const PROBLEM_LABELS = {
    duplicate_track_count: 'duplicate track',
    disc_inconsistent_count: 'missing disc number',
    mbid_inconsistent_count: 'missing MusicBrainz ID',
    missing_tracks_count: 'missing audio file',
  };

  /** lowercase artist name -> API entry */
  let correctionsByArtist = new Map();
  let correctionsLoaded = false;

  function normaliseKey(value) {
    return String(value || '').trim().toLowerCase();
  }

  /** Human-readable list of what is wrong with one artist. */
  function describeProblems(entry) {
    return Object.keys(PROBLEM_LABELS)
      .map((field) => {
        const count = Number(entry[field]) || 0;
        if (!count) return null;
        const label = PROBLEM_LABELS[field];
        return `${count} ${label}${count === 1 ? '' : 's'}`;
      })
      .filter(Boolean);
  }

  function entryForRow(row) {
    // Try both keys the row exposes; the first hit wins.
    const keys = [row.dataset.artistKey, row.dataset.artistLinkKey]
      .map(normaliseKey)
      .filter(Boolean);
    for (const key of keys) {
      const entry = correctionsByArtist.get(key);
      if (entry) return entry;
    }
    return null;
  }

  function annotateRows() {
    document.querySelectorAll('[data-artist-key]').forEach((row) => {
      const entry = entryForRow(row);
      const badge = row.querySelector('.artist-needs-fixing');
      if (!badge) return;

      const problems = entry ? describeProblems(entry) : [];
      if (!problems.length) {
        // Hide rather than remove: this function is safe to re-run (e.g. after a
        // refresh), and a removed element could never be shown again.
        badge.style.display = 'none';
        badge.textContent = '';
        badge.removeAttribute('title');
        row.removeAttribute('data-needs-fixing');
        return;
      }

      badge.style.display = '';
      badge.textContent = problems.length === 1 ? problems[0] : `${problems.length} issues`;
      badge.title = `Needs fixing: ${problems.join(', ')}`;
      row.setAttribute('data-needs-fixing', '1');
    });
  }

  function updateCorrectionsSummary() {
    const flagged = document.querySelectorAll('[data-artist-key][data-needs-fixing]').length;
    const summary = document.getElementById('correctionsSummary');
    if (summary) {
      summary.textContent = flagged
        ? `${flagged} artist${flagged === 1 ? '' : 's'} need fixing`
        : 'No artists need fixing';
      summary.className = flagged ? 'badge bg-danger' : 'badge bg-success';
    }
    return flagged;
  }

  /**
   * Show only the rows flagged as needing fixes, and hide letter sections that
   * end up empty so the page does not become a run of blank headings.
   * @param {boolean} onlyProblems
   */
  function applyCorrectionsFilter(onlyProblems) {
    const filtered = onlyProblems === true;

    document.querySelectorAll('[data-artist-key]').forEach((row) => {
      const flagged = row.hasAttribute('data-needs-fixing');
      row.style.display = filtered && !flagged ? 'none' : '';
    });

    document.querySelectorAll('.artist-letter-section').forEach((section) => {
      if (!filtered) {
        section.style.display = '';
        return;
      }
      const anyVisible = Array.from(section.querySelectorAll('[data-artist-key]'))
        .some((row) => row.style.display !== 'none');
      section.style.display = anyVisible ? '' : 'none';
    });

    const emptyNote = document.getElementById('correctionsFilterEmpty');
    if (emptyNote) {
      const flagged = document.querySelectorAll('[data-artist-key][data-needs-fixing]').length;
      emptyNote.classList.toggle('d-none', !(filtered && flagged === 0));
    }
  }

  async function loadCorrections() {
    try {
      const data = await global.api.getJson(CORRECTIONS_ENDPOINT);
      correctionsByArtist = new Map(
        Object.entries(data.corrections || {}).map(([key, entry]) => [normaliseKey(key), entry])
      );
      correctionsLoaded = true;
      annotateRows();
      updateCorrectionsSummary();
    } catch (error) {
      // A failure here must not take the artist list down with it — the page is
      // still fully usable without the indicators.
      console.warn('[artists] correction indicators unavailable:', error.message);
      const summary = document.getElementById('correctionsSummary');
      if (summary) summary.textContent = 'Fix indicators unavailable';
    }
  }

  function initCorrections() {
    const toggle = document.getElementById('correctionsOnlyToggle');
    if (toggle) {
      toggle.addEventListener('change', () => applyCorrectionsFilter(toggle.checked));
    }
    if (document.querySelector('[data-artist-key]')) loadCorrections();
  }

  // Escape closes the overlay.
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    const el = overlay();
    if (el && !el.classList.contains('d-none')) el.classList.add('d-none');
  });

  document.addEventListener('DOMContentLoaded', initCorrections);

  global.openJumpPicker = openJumpPicker;
  global.closeJumpPicker = closeJumpPicker;
  global.jumpToLetter = jumpToLetter;
  global.scanLetterArtists = scanLetterArtists;
  global.applyCorrectionsFilter = applyCorrectionsFilter;
  global.loadArtistCorrections = loadCorrections;
})(window);
