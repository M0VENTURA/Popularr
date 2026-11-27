/**
 * Missing-track counts — ONE rule for every page.
 *
 * ⚠️ WHY THIS FILE EXISTS.
 *
 * The server has always returned the split (`/api/album/missing-tracks` →
 * `missing_count`, `bonus_count`, `missing_core_count`). The ALBUM page
 * subtracts the bonus rows; the ARTIST page used `missing_count` raw. So an
 * album the user owns in full, plus a deluxe edition's trailing extras, showed
 *
 *     album page : "3 bonus"
 *     artist page: "3 tracks missing"     <- reported
 *
 * for the same album. Three separate writers had the same omission
 * (`artist.js`, `artist-releases.js`, and a third surfaced by the tracklist),
 * which is exactly the kind of thing a helper prevents: the rule now lives in
 * one place and every caller reads it.
 *
 * The server is authoritative when it sends `missing_core_count`; the local
 * subtraction is only a fallback for an older payload.
 *
 * Loaded by base.html so any page module can use it.
 */
(function (global) {
  'use strict';

  /**
   * Split the server's missing-track payload into core gaps and bonus cuts.
   *
   * @param {Object} data the `/api/album/missing-tracks` response
   * @returns {{missing: number, bonus: number, queued: number}}
   */
  function splitMissingCounts(data) {
    var payload = data || {};
    var rows = Array.isArray(payload.missing_tracks) ? payload.missing_tracks : [];
    var bonus = Number(payload.bonus_count);
    if (!isFinite(bonus)) {
      // Older payload: derive it from the rows rather than trusting the total.
      bonus = rows.filter(function (row) { return row && row.is_bonus; }).length;
    }
    bonus = Math.max(0, bonus);

    var core = Number(payload.missing_core_count);
    if (!isFinite(core)) {
      var total = Number(payload.missing_count);
      if (!isFinite(total)) total = rows.length;
      core = Math.max(0, total - bonus);
    }
    return {
      missing: Math.max(0, core),
      bonus: bonus,
    };
  }

  /**
   * The badge text for an album row, e.g. "3 bonus" or "2 tracks missing · 1 bonus".
   *
   * Returns '' when there is nothing to report, so callers can leave the badge
   * hidden (an album with no gap must never render "0 tracks missing").
   *
   * @param {Object} data the `/api/album/missing-tracks` response
   * @returns {string}
   */
  function missingBadgeText(data) {
    var counts = splitMissingCounts(data);
    var bits = [];
    if (counts.missing > 0) {
      bits.push(counts.missing + ' track' + (counts.missing === 1 ? '' : 's') + ' missing');
    }
    if (counts.bonus > 0) {
      bits.push(counts.bonus + ' bonus');
    }
    return bits.join(' · ');
  }

  /**
   * True when a row is an album GAP (not a bonus cut).
   *
   * Bonus rows are worth downloading but are not an incomplete album, so they
   * must not mark anything "needs fixing".
   *
   * @param {Object} row a `missing_album_tracks` row
   * @returns {boolean}
   */
  function isAlbumGap(row) {
    return !(row && row.is_bonus);
  }

  global.missingTracks = {
    splitMissingCounts: splitMissingCounts,
    missingBadgeText: missingBadgeText,
    isAlbumGap: isAlbumGap,
  };
})(window);
