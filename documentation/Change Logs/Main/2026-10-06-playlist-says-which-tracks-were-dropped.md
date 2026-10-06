# The playlist sync says which tracks Navidrome did not store — and the import stops leaving a stale row cache

**Date:** 2026-10-06
**Reported:**

> `[PLAYLISTS] Navidrome stored a different number of tracks than requested —
> stale song IDs are the usual cause …; re-run the Navidrome import scan to
> refresh them … requested=300 stored=298`
>
> **A full forced Navidrome import has been run. Is something missing from the
> Navidrome import?**

## What the import already does (verified, no change needed)

A forced import (`mode=force`) reaches every album: `force` bypasses
`should_skip_cached_album`, and with `diff_mode=False` the "not changed in diff
mode" skip in `should_skip_album` cannot fire either. It then finishes with
`cleanup_stale_artist_tracks_if_needed`, which deletes **every id for that
artist that Navidrome no longer returns** — `tracks.id` *is* the Navidrome song
id, so this is the refresh the warning asks for.

The other modes are covered too, by the other half of the split: album-level
`cleanup_stale_album_tracks_if_needed` runs when `diff_mode=True`
(`all`, `missing`), artist-level when it is `force`. No mode leaves both
disabled — checked, because "the cleanup only runs in some modes" was the first
suspect.

## So what *was* missing

**1. The import never invalidated the playlist row cache.**

Genre and `* - Top Tracks` playlists are built from `_GENRE_ROWS_CACHE` — a
**120-second snapshot of `tracks` rows, including `tracks.id`**. The import
rewrites exactly that column, and nothing in the import path cleared the cache,
so a finalise inside that window pushed the ids the import had *just*
replaced: the sync reported "stored a different number of tracks than
requested" **after** a clean re-import, and told the operator to re-run the
import they had already run.

`scan_artist_to_db` now calls `invalidate_genre_playlist_rows()` once the
albums are processed (and their stale rows pruned), so the next playlist build
reads the ids the import just wrote.

**2. The diagnosis was a count.**

`stored=298, requested=300` cannot distinguish two very different situations:

* a **stale** id — a Navidrome import fixes it; and
* a song Navidrome has **not indexed yet** — no import can fix that (there is
  no id to refresh until Navidrome scans the file), only a Navidrome scan can.

The advice was therefore followable *and* useless. The ids were known at that
point — the read-back has both lists — they were simply never printed.

## Changes

* `services/playlists/playlist_navidrome_service.py`
  * computes the set difference on every verified update and returns
    `dropped_ids` (requested-but-not-stored) and `extra_ids` (stored-but-not-
    requested — the replace not fully replacing);
  * the warning now carries `dropped=`, `dropped_ids=` (first 8), `extra=`,
    `extra_ids=`, and its guidance covers the second case: *"If they survive an
    import, Navidrome has not indexed those files yet — rescan Navidrome
    first."*
* `services/popularity/stages/finalise_stage.py`
  * `_sync_playlist_to_navidrome(..., rows=)` takes the rows that produced the
    ids, and `_log_dropped_playlist_tracks()` turns ids into
    `Stabbing Westward - Shame` at WARNING, with the same hint. All three call
    sites (New Music, artist collections, genre playlists) pass their rows.
* `services/scanning/navidrome_import.py` — `invalidate_genre_playlist_rows()`
  after the album loop (lazy import: `finalise_stage` is heavy and the import
  path must not depend on it at module load).

## Tests

`tests/test_playlist_dropped_ids_name_the_tracks.py` — **10**:

* the sync returns the dropped ids, and logs `dropped=`/`extra=` with them;
* the guidance admits the import cannot fix an un-indexed file;
* CONTROL: matching counts stay silent and return no id list;
* the caller logs `artist - title` for each dropped id, reports an id it cannot
  match as `<unknown id …>` rather than hiding it, and stays quiet when
  nothing dropped;
* the premise: `_GENRE_ROWS_SQL` really does `SELECT id … FROM tracks` (if it
  ever stops, invalidating the cache is pointless and these tests should be
  deleted rather than trusted);
* the import calls `invalidate_genre_playlist_rows()` **after** the album loop;
* CONTROL: `invalidate_genre_playlist_rows()` clears the snapshot.

**Oracle:** with the three source files stashed → **7 failed / 3 passed**, the
3 being exactly the controls. Restored, 10/10.

## Verification

- new suite: **10 passed**; with `test_playlist_sync_auth_and_cap.py` → **44 passed**
- affected set (35 files): **527 passed / 37 failed**, against a baseline
  subset of 91 for the same files → **0 new, 54 fixed**
- full suite: **225 failed / 4724 passed / 2 skipped** in 689 s against a
  baseline of 303 failures → **79 fixed, 0 real new**. The single NEW id is
  `test_download_completion_not_found_loop.py::TestDeepFileSearch::
  test_sibling_torrents_root_is_searched`, the known Windows path-case flake.

  An earlier attempt at this run reported 29 "new" failures — every one of
  them in files that do not reference these modules and all green in
  isolation. The confirming run above reproduces the usual profile exactly
  (225, same as the two runs before this change), so that first result was a
  contaminated run, not a regression.

## Files

- `services/playlists/playlist_navidrome_service.py`
- `services/popularity/stages/finalise_stage.py`
- `services/scanning/navidrome_import.py`
- `tests/test_playlist_dropped_ids_name_the_tracks.py` (new)
