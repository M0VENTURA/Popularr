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

  // Escape closes the overlay.
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    const el = overlay();
    if (el && !el.classList.contains('d-none')) el.classList.add('d-none');
  });

  global.openJumpPicker = openJumpPicker;
  global.closeJumpPicker = closeJumpPicker;
  global.jumpToLetter = jumpToLetter;
  global.scanLetterArtists = scanLetterArtists;
})(window);
