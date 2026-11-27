# Missing tracks use the owned edition; imports don't block scans; duplicate artists are album-scoped

**Date:** 2026-11-24
**Area:** `services/metadata/album_missing_service.py` ·
`services/enrichment/musicbrainz_service.py` ·
`services/popularity/stages/album_stage.py` ·
`routes/misc_routes.py` · `test_site/static/js/services/scan-preflight.js` ·
`test_site/static/js/pages/album.js` ·
`test_site/static/js/pages/artist-corrections.js`
**Commit:** `fix(album): compare against the owned edition; unblock scans; scope duplicate artists`

## Reported

> "This is showing missing tracks for Planets from Adema, but one of them is a
> bonus track and it's not comparing correctly to the base release of 14 tracks.
> Same with the ones from Against Me.
>
> I also can't start a full scan while a Navidrome import is happening.
>
> I think duplicate artists is showing all various artists collections rather than
> comparing album artist and not track artist."

## 1. Bonus tracks were reported as missing album tracks

Three things combined:

1. `resolve_release_id` chooses the release in a group with **the most tracks**
   (`max(candidates, key=_total_tracks)`), so the stored release id for a
   standard+deluxe group is the deluxe edition.
2. Nothing in the missing-track path ever asked which edition matched the
   library, and the scan called `get_missing_tracks(artist, album)` with no
   release id.
3. `is_bonus_missing_track` only flags tracks numbered ABOVE the library's
   highest position, so an edition mismatch *inside* positions 1–14 is classified
   as a genuinely-missing album track.

Fixes:

* `resolve_release_id(release_id, expected_track_count=None)` — an edition whose
  track count **equals the library's** now wins over the biggest one. The
  album stage passes the album's real track count.
* `_prefer_edition_matching_library` in the missing-track path — browses the
  release group and swaps to an edition matching `library_count`. An explicit
  release id (the album page's Lookup, a user pick) is **never** second-guessed.
* `is_bonus` is now labelled in the UI: a bonus row shows a grey **Bonus** badge
  (no Download button implication) instead of a yellow **Missing** one, and the
  header badge reports `N tracks missing · M bonus` rather than counting bonus
  cuts as missing.

## 2. A Navidrome import no longer blocks a full scan

`scan-preflight.js::fetchRunningScans` filtered `active_scans` on `is_running`
alone. `navidrome_scan` is in `progress_service.SCAN_TYPES`, so a running IMPORT
publishes an entry there and the preflight gate asked "cancel this scan and start
the new scan?" — and accepting calls `/scan/stop-all`, aborting a real import for
a scan that was never blocked. The dashboard card already excluded imports
(`IMPORT_ONLY_TYPES`); the gate now uses the same `NON_BLOCKING_SCANS` list.

## 3. Duplicate artists listed every performer on every compilation

`api_get_duplicate_artists` grouped by `artist` (the PERFORMER) library-wide, so a
shared/legacy `musicbrainz_artistid` on compilations produced one row of ~90
"variations" (Red Hot Chili Peppers, Green Day, Gomez, Skulker…) totalling 5174
tracks. Clicking **Merge** there would have rewritten the track **and** album
artist (plus file tags) of all 90 acts to one name.

Fixed:
* groups by the **album-artist** key (`COALESCE(NULLIF(album_artist,''), artist)`);
* resolves the album-artist MBID first (`musicbrainz_albumartistid`), falling back
  to the performer MBID only when the album-artist one does not exist;
* the group is scoped to the requested act (album-artist MBID, or a name match),
  so a polluted performer MBID cannot drag unrelated acts in;
* placeholder identities ("Various Artists", "Unknown Artist", …) are excluded as
  variations **and refused as a merge target**;
* each variation is a link to its artist page and the row has an **Open** action,
  so a release can be corrected instead of merging ("not every release should be
  merged").

## Tests

`test_missing_tracks_use_the_library_edition.py` (edition swap, no-swap controls,
`resolve_release_id` preference, bonus classification),
`test_scan_preflight_excludes_imports.py` (the gate and the dashboard agree),
`test_duplicate_artists_album_scope.py` (VA compilations produce no group; genuine
spellings still do; a polluted performer MBID does not join unrelated acts;
merging into a placeholder is refused).
