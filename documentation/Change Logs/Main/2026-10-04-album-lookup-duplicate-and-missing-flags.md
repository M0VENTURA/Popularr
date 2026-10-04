# Album Lookup MBID flags duplicates + missing tracks in the tracklist (2026-10-04)

**Request:** *"On the album page, when looking up mbid, it should flag
duplicate tracks as well as tracks on the album not in the collection and
add them to the tracklist to download similar to how it does during a
metadata lookup during the popularity scan."* Clarified: *"Duplicate tracks
are tracks on that album that were downloaded twice."*

## What existed vs what was missing

* **Missing tracks** already render as queueable tracklist rows — but only
  from a manual *Compare with MusicBrainz* run, or from the snapshot the
  SCAN persists (`missing_album_tracks` → `loadAlbumMissingTracks` on page
  load). **Lookup MBID never touched them**: it fills the form and resolves
  the concrete release, yet the tracklist kept whatever the last scan/compare
  had measured — usually nothing for a newly linked release.
* **Duplicates** (the same track downloaded twice on one album) had no
  surface on the album page at all. The artist-corrections page already
  defines "duplicate": same `(title, track_artist, track_number, disc_number)`
  within the album, ≥2 rows, with version-variant file names excluded
  (`Song.mp3` + `Song (instrumental).flac` are variants, not a double
  download).

## Changes

* `helpers/normalization_service.py` — new shared
  `DUPLICATE_VARIANT_KEYWORDS` + `file_version_variant_key()`; the
  corrections page's list is now pinned to it by a parity test so the two
  surfaces cannot drift.
* `services/metadata/album_missing_service.py`
  * `find_duplicate_tracks(artist, album)` — per-album duplicate groups
    using the corrections rule, returned with a flat `duplicate_track_ids`
    list for row flagging;
  * `get_missing_tracks(..., release_mbid=None)` — an explicit release wins
    over the stored id and the name search (Lookup MBID resolves a release
    BEFORE the form is saved, so the stored id is still the old one). A
    failed fetch still returns `mb_total=0` **without persisting**, so a bad
    id can never wipe the stored list.
* `routes/album_routes.py`
  * `GET /api/album/duplicate-tracks` — the new flag source;
  * `GET /api/album/missing-tracks` gains `refresh=1&release_mbid=<id>` for
    the on-demand recompute — **page loads keep the DB-only path** (the
    artist page requests it once per owned album; the original perf bug).
* Album page (BOTH trees)
  * `album.js` / `album_detail.js`: new `loadAlbumDuplicateFlags()` — flags
    matching `tr[data-track-id]` rows with a red `Duplicate ×N` badge
    (idempotent, runs on page load AND after a lookup);
  * new `refreshAlbumTrackFindings(releaseMbid)` — replaces the rendered
    missing rows with the recompute against the picked release (guarded on
    `mb_total > 0` so an unfetchable release keeps the current list) and
    re-flags duplicates;
  * hooks: `applyAlbumMatch` (rebuilt) / `applyAlbumMbid` (live) call it
    while the lookup busy popup is still up; the missing-row render was
    extracted into a shared helper both the loader and the refresh use.

## Verification

* `tests/test_album_lookup_findings.py` (23): duplicate detection (double
  download, album scope, position key, version variants, year-prefixed
  album names), keyword-parity guard against `artist_service`, the
  release-override semantics (explicit release wins, failed fetch never
  persists, owned tracks excluded), both routes (incl. page-load-stays-
  DB-only), and JS wiring for both trees.
* **Oracle:** 19 failed / 4 controls with the five source files stashed →
  **23 passed** restored.
* Two existing guard tests were **re-anchored** (their old contracts are
  superseded): the loader-reuse tests now pin "loader → shared helper → row
  builder", and the DB-only route test pins "recompute sits inside the
  `refresh` branch" (behaviourally pinned by the new route test).
* Sweep (8 suites incl. missing/corrections/normalization): **203 passed,
  0 failed**; baseline for the re-anchored files was clean (46 passed).
* `node --check` both album JS files, Python syntax guard, `import app` OK
  (390 routes — +1 for the new endpoint).
