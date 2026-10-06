# Album genres become database-only (the file is the scan's job)

**Date:** 2026-10-06 · **Area:** album / config
**Commit:** `fix(album): album genres are database-only`

## Requested

> The genres shouldn't update on the file during this pass. Genres update
> during the popularity scan, the genres should only be updated to the
> musicbrainz table.

## What the album save's genre phase did

`_apply_album_track_genres` wrote **two** things:

1. `update_track_genres()` → `tracks.genres` + `tracks.manual_genres` (**DB**)
2. `update_file_tags(file_path, {"genres": [...]})` → the **audio file tag**

(1) was already exactly what was asked for — the DB. The file write (2) is the
part removed here: the popularity scan owns file genres, through
`album_tag_sync_service.sync_album_file_tags` (DB → file).

## Why removing (2) needed a second change

`genres` and `manual_genres` are **not** in
`_POPULARITY_PROTECTED_COLUMNS`, so a Navidrome re-import writes
`genres=EXCLUDED.genres` — i.e. the value Navidrome last saw **in the file**.

Today that is harmless because the save also writes the file and asks Navidrome
to rescan, so the two stay in step. Remove only the file write and a later
import can write the **old** file genres back over the edit — the same
"metadata reverts a few hours later" pattern reported earlier in this work.

So both halves move together:

| | before | after |
|---|---|---|
| album save → DB | `genres`, `manual_genres` | unchanged |
| album save → **file** | written | **not written** |
| Navidrome import → DB `genres` | overwrites | **protected** |
| popularity scan → file | (also written by the save) | **sole owner** |

## The rescan trigger changes with it

`routes/ui_routes.py` requested a Navidrome rescan when
`updated_count > 0 or genre_only_writes > 0 or _cover_embedded or reverted_live_count > 0`,
on the stated rationale *"The save wrote the corrected tags into the AUDIO
FILES"* — and `tests/test_navidrome_rescan_after_save.py` pinned the genres
half of it: *"genres reach the audio files, so Navidrome must rescan"*.

With genres no longer touching a file, `genre_only_writes` no longer implies
anything to rescan, so it leaves the condition. A genres-only save still
reports *"Album genres saved — N track(s)"*; it just no longer asks Navidrome
to re-read files it was never told to change.

## Tests

* `tests/test_album_save_phases.py` — `test_the_file_tag_is_written_too`
  **inverts** to `test_the_save_does_not_touch_the_file`: the phase must never
  write a genre tag again (a guard against the line being re-added).
* `tests/test_navidrome_rescan_after_save.py` —
  `test_a_genre_only_save_requests_a_rescan` becomes
  `test_a_genre_only_save_does_not_request_a_rescan`, with the corrected
  rationale: nothing in the files changed.
* new: `genres` + `manual_genres` are in `_POPULARITY_PROTECTED_COLUMNS`, plus
  an UPDATE-clause assertion (mirroring
  `test_save_to_db_builds_navidrome_update_set_without_album_type`) proving a
  Navidrome sync cannot write them.

## Verification

- targeted (phases/rescan/save/persists/reimport/type suites): **114 passed**
  (1 failure = the known pre-existing `test_detects_when_missing`)
- **Oracle:** stashing `ui_routes.py` + `popularity_repository.py` → **7 failed,
  11 controls passed** = exactly the tests depending on this change; with it 18/18
- affected set (66 files): 1201 passed / 39 failed → **0 new vs baseline**
- full suite: **225 failed, 4664 passed, 2 skipped** vs the 303-failure
  baseline → **79 fixed, 0 real regressions** (only the known native-flaky
  `test_sibling_torrents_root_is_searched`)

## Files

- `routes/ui_routes.py` — the phase writes the DB only; `genre_only_writes`
  leaves the rescan condition
- `db/repositories/popularity_repository.py` — `genres` + `manual_genres` join
  `_POPULARITY_PROTECTED_COLUMNS`
- `tests/test_album_save_phases.py`
- `tests/test_navidrome_rescan_after_save.py`
