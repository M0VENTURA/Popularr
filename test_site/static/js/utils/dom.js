/* ==========================================================================
   static/js/utils/dom.js
   Shared DOM / string helpers.

   Load this BEFORE any page script that uses these functions:

       <script src="{{ url_for('static', filename='js/utils/dom.js') }}"></script>

   Then DELETE the local copies of escapeHtml, escapeJsString, sanitizeBio,
   formatBytes and formatDuration from:
       artist_detail.html   (escapeHtml x2, escapeJsString, sanitizeBio,
                             formatBytes, formatDuration)
       album_detail.js, config.js, dashboard.js, main.js  (escapeHtml)

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   escapeHtml was defined FIVE times across the codebase, and TWICE inside
   artist_detail.html alone. The two artist-page copies are not equivalent,
   and the second one silently wins for the entire page:

     Copy A (mid-file, "Download Search Modal" block):
         const div = document.createElement('div');
         div.textContent = text;          // coerces null/undefined/numbers
         return div.innerHTML;            // does NOT escape quotes

     Copy B (end of file, "Smart Artist Routing" block):
         return text.replace(/[&<>"']/g, m => map[m]);   // THROWS on non-string

   Because Copy B is parsed last it overwrites Copy A, so every call on the
   page gets the throwing version. Any call site passing a number, null or
   undefined raises "text.replace is not a function" — e.g.
   escapeHtml(track.position) in displayMusicBrainzResults, where position is
   often a number, and escapeHtml(data.error) where error may be undefined.

   The implementation below takes the correct behaviour from BOTH: it coerces
   like Copy A and escapes quotes like Copy B (Copy A left " and ' unescaped,
   which matters because the result is interpolated into HTML *attributes*
   throughout the codebase — e.g. alt="${escapeHtml(item.title)}").
   ========================================================================== */

(function (global) {
  'use strict';

  const HTML_ESCAPES = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
  };

  /**
   * Escape a value for safe interpolation into HTML text OR an attribute.
   *
   * Accepts any type. null and undefined become '' rather than the strings
   * "null"/"undefined", because call sites like escapeHtml(data.error) rely
   * on a falsy value rendering as empty.
   *
   * @param {*} value
   * @returns {string}
   */
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
  }

  /**
   * Escape a value for embedding inside a single- or double-quoted JS string
   * that itself sits inside an HTML attribute, e.g.
   *     onclick="doThing('${escapeJsString(name)}')"
   *
   * NOTE: this is a legacy pattern. New code should attach listeners with
   * addEventListener and pass data via dataset, as displayMusicBrainzResults
   * already does with data-release-key. Kept here because ~17 call sites in
   * artist_detail.html and album_detail.js still depend on it.
   *
   * @param {*} value
   * @returns {string}
   */
  function escapeJsString(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/\\/g, '\\\\')
      .replace(/'/g, "\\'")
      .replace(/"/g, '\\"')
      .replace(/\n/g, '\\n')
      .replace(/\r/g, '\\r');
  }

  /**
   * Escape a block of text but preserve line breaks as <br>.
   * Used for artist biographies, which arrive from the API containing either
   * literal newlines or escaped <br /> variants depending on the source.
   *
   * @param {*} value
   * @returns {string}
   */
  function sanitizeBio(value) {
    return escapeHtml(value)
      .replace(/&lt;br\s*\/?&gt;/gi, '<br>')
      .replace(/\n/g, '<br>');
  }

  /**
   * Format a byte count as a human-readable size.
   *
   * @param {number} bytes
   * @returns {string} e.g. "4.72 MB"
   */
  function formatBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n <= 0) return '0 B';
    const k = 1024;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(n) / Math.log(k)), units.length - 1);
    return Math.round((n / Math.pow(k, i)) * 100) / 100 + ' ' + units[i];
  }

  /**
   * Format a track duration as m:ss or h:mm:ss.
   *
   * Inputs are inconsistent across sources, so the magnitude is used to guess
   * the unit: MusicBrainz returns milliseconds, Essentia sometimes returns
   * microseconds, and the local DB stores seconds.
   *
   * @param {number|string} rawValue
   * @returns {string} formatted duration, or 'N/A'
   */
  function formatDuration(rawValue) {
    if (rawValue === null || rawValue === undefined || rawValue === '') return 'N/A';
    const n = Number(rawValue);
    if (!Number.isFinite(n) || n <= 0) return 'N/A';

    let seconds;
    if (n >= 100000000) {
      seconds = n / 1000000;   // microseconds
    } else if (n > 10000) {
      seconds = n / 1000;      // milliseconds
    } else {
      seconds = n;             // already seconds
    }

    seconds = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    const pad = (v) => String(v).padStart(2, '0');

    return hours > 0
      ? `${hours}:${pad(mins)}:${pad(secs)}`
      : `${mins}:${pad(secs)}`;
  }

  // Exposed on window so the existing inline onclick="" handlers keep working
  // during the migration. Once those are converted to addEventListener this
  // can become a real ES module with named exports.
  global.escapeHtml = escapeHtml;
  global.escapeJsString = escapeJsString;
  global.sanitizeBio = sanitizeBio;
  global.formatBytes = formatBytes;
  global.formatDuration = formatDuration;
})(window);
