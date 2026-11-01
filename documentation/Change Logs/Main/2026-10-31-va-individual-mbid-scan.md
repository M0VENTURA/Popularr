# The scan resolves individual recording MBIDs on an unmatched VA compilation

**Date:** 2026-10-31
**Area:** `popularity` (metadata scan) / MBID resolution
**Files:** `services/popularity/stages/track_stage.py`,
`services/popularity/scan_stage_runner.py`,
`tests/test_va_individual_mbid_lookup.py` (new),
`tests/test_metadata_scan_mbid_first_and_completeness.py`

## Reported symptom

> The manual MBID lookup works — can we adjust so that individual MBIDs are
> looked up via MusicBrainz when an album isn't matched to a MusicBrainz
> release, such as a Various Artists compilation?

(Clarified: the **automatic metadata scan**, not the album page's button.)

## Root cause

`_resolve_track_mb_metadata` already searches per track, independently of any
album release. But it searched with the ROW's artist. A VA compilation's
unresolved row can still carry the album placeholder — `artist = "Various
Artists"` — so every search was:

```
artist:"Various Artists" AND recording:"<title>"   → 0 results
```

and the miss was then stamped durably (`recording_mbid_checked_at`). The
album-completeness gate treats a fresh stamp as terminal, so the album was
skipped for the negative-cache TTL and the tracks never got another chance.

## Fix

* `track_stage._file_tag_artist(track)` — the file's own ARTIST tag, used when
  the row's artist is empty/placeholder. It is read **lazily**, only when a
  per-track search is actually needed, so ordinary scans never open files.
* `_resolve_track_mb_metadata` searches with that performer:
  * real row artist → unchanged (no file read);
  * placeholder → file ARTIST tag;
  * neither → the search is skipped and **no durable miss is stamped** (a miss
    we could not learn anything from must not lock the track out).
* `scan_stage_runner._track_phase_is_incomplete` — a fresh
  `recording_mbid_checked_at` on a **placeholder-artist row that has a file**
  no longer hides the album from the pass that can now resolve it. The stamp is
  still terminal for real artists, and for rows with no file.
* The search itself is unchanged: it remains artist-constrained (no title-only
  fallback), so the safety rule pinned by
  `tests/test_compilation_track_artist.py` is preserved — the performer is real,
  just read from the file.

## Verification

`tests/test_va_individual_mbid_lookup.py`:

| Test | Pins |
|---|---|
| `test_a_placeholder_row_uses_the_files_artist` | the file ARTIST is the fallback |
| `test_a_placeholder_inside_the_file_is_ignored` | a tag that also says "Various Artists" is not a performer |
| `test_an_unmatched_compilation_searches_individually` | ⭐ the scan searches `artist:"Bushido"` and persists the MBID |
| `test_a_real_track_artist_never_reads_the_file` | ordinary tracks are untouched |
| `test_no_performer_means_no_search_and_no_durable_miss` | no unusable miss is stamped |

`tests/test_metadata_scan_mbid_first_and_completeness.py`:

| Test | Pins |
|---|---|
| `test_a_placeholder_artist_miss_is_not_terminal_with_a_file` | a stale bad-search stamp no longer skips the album |
| `test_a_real_artist_miss_stays_terminal_with_a_file` | the durable-miss relief is preserved |

A full/forced metadata scan is needed to revisit albums that were already
skipped; normal scans will pick it up once the album is next considered.
