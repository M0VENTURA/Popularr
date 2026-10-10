# `(1999 Album)` glued onto the album name on every Navidrome import

**Date:** 2026-10-10
**Area:** `config` / `ui` → album naming · `helpers/normalization_service.py` · `services/scanning/navidrome_import.py`
**Reported:** *"This album is still being grabbed from Navidrome with the subtitle added to the track: 17: Greatest Hits (1999 Album)."*

## Symptom

The album page showed `17: Greatest Hits (1999 Album)` glued onto every track
of the album, even though the file's own `album` tag was the clean
`17: Greatest Hits`. It was the *second* report of this bug class — the first
was the `(repress)` edition marker (see `2026-10-09-repress-edition-marker.md`)
— but this suffix is **not** an edition keyword, so the previous fix did not
cover it.

## Root cause

The suffix comes from the MusicBrainz **release disambiguation** that Picard
writes into the `musicbrainz_albumcomment` tag. Navidrome folds that tag onto
its internal `albumversion` tag (`resources/mappings.yaml`), then
`Album.FullName()` appends it via `appendSuffix` whenever
`Subsonic.AppendAlbumVersion` is on — the default — so
`server/subsonic/helpers.go::buildAlbumID3` sets `Name = FullName()`. The
Subsonic API therefore only ever hands out the merged form:

```
"17: Greatest Hits" + comment "1999 Album"
    -> AlbumID3.name = "17: Greatest Hits (1999 Album)"
```

`scan_artist_to_db` strips that with `strip_album_edition_marker()` before
storing `tracks.album`, but the edition vocabulary is a fixed keyword list
(`reissue`, `deluxe`, `anniversary`, ...) and the disambiguation is **free
text** — `1999 Album` is just one example; it could be `1999 Live Recordings`,
`2003 Remaster`, or anything a tagger chose. No keyword list can catch it, so
the suffix survived every scan, was written into `tracks.album`, and glued the
subtitle onto every track of the album.

Two properties of the fix make this tractable where the keyword approach is
not:

* **The value is not a guess.** The API *also* carries the same string in the
  album's separate `version` field (`OpenSubsonicAlbumID3.Version`), so the
  suffix can be removed by **matching** the value Navidrome appended, exactly
  as it built it. This is robust to any disambiguation text, present or
  future — no vocabulary to maintain.
* **A mismatch is a no-op, not a heuristic.** If the name does not end with
  the version, it is returned unchanged. This is deliberately *not* a blind
  "strip the last bracket" rule, which would eat a genuine subtitle like
  `(Live at Wembley)`.

## Fix

`helpers/normalization_service.py` gains
`strip_appended_album_version(name, version)`, which mirrors Navidrome's own
`appendSuffix`: a version that already carries its bracket is appended bare,
otherwise it is wrapped in parentheses; the match is case-insensitive and
idempotent. It never empties a name that *is* only the version.

`services/scanning/navidrome_import.py` derives the album name through one
new function, `clean_navidrome_album_name(album)`, which strips the version
first (it is the **outermost** suffix — Navidrome appends it last) and then
the edition markers. Every album-name derivation in the import — the artist
album diff, the duplicate-name counter, and the main import loop — now goes
through it, so the version can no longer leak into `tracks.album` from any
path.

The version itself is not lost: `extract_album_metadata` already stores it in
the `albumversion` column, so the release information stays queryable even
though it is no longer pasted onto the title.

## Two knock-on hazards this fix had to close

Fixing the name created a transition problem for every album **already**
stored with the glued suffix, and the fix would have deleted the very tracks
it was repairing:

* **A renamed album is not a removed album.** The album diff compares raw DB
  album names against cleaned Navidrome names, so a legacy row storing
  `17: Greatest Hits (1999 Album)` no longer matches the cleaned
  `17: Greatest Hits` and lands in the diff's `removed` set. The removed-album
  cleanup deletes by name, so it would have destroyed the tracks the import
  had just re-homed under the clean title. The cleanup now takes the set of
  track ids Navidrome still holds *anywhere* for the artist
  (`live_track_ids`) and never deletes an id that is still alive — the
  rename-safe guard. Because the diff does not strip the version on the DB
  side, the mismatch is deliberate and load-bearing: it flags the album
  `CHANGED` so the import re-runs and rewrites `tracks.album` to the clean
  title, then the next scan sees a clean name and skips.

* **A stop-halted import must not declare albums removed.** The live-id set is
  only complete once the album loop has finished. `is_stop_requested` breaks
  the loop early, leaving that set partial — and running the removed-album
  cleanup off a partial set would delete tracks that are still in Navidrome.
  The cleanup is now gated on the loop having completed, so a partial import
  never declares an album removed.

## Verification

* `tests/test_navidrome_album_version_suffix.py` — 18 tests over the new
  helper and the derivation point: the reported case, an already-bracketed
  version appended bare, a case-insensitive match, a version that itself
  contains a year, and the guard set (a real subtitle that is *not* the
  version, a missing or empty version, a name that is only the version, an
  inner year that must survive, idempotence). Oracle: source reverted → 9
  failed; source fixed → 28 passed.
* `tests/test_navidrome_import_removals.py` — 2 new tests, both of which
  caught real defects while being written: the re-home test (a DB album seeded
  with the subtitle name and the same track ids the import returns clean) and
  the stop-halt test. The stop-halt test initially failed because it never
  passed `progress_file`, so the halt never fired — passing it is what makes
  the test exercise the real path.
* Regression sweep over 18 related suites (normalization, scanning, cleanup,
  payload builder): 372 passed baseline, 372 passed after — **0 new
  regressions**.

## Not fixed here

`strip_album_edition_marker` still cannot catch a disambiguation on the DB
side by itself; a legacy row self-heals only when a Navidrome scan re-imports
the album (the mismatch flags it changed). A one-off backfill that strips
known-glued suffixes from existing `tracks.album` rows would repair rows for
artists that are no longer scanned, and is the natural follow-up.
