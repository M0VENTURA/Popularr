# Navidrome import: visible skips, bounded load, composer bloat, empty-local false conflicts

**Date:** 2026-11-02
**Area:** scanning / Navidrome import
**Files:** `services/scanning/navidrome_import.py`,
`services/scanning/pipelines/navidrome_pipeline.py`,
`services/scanning/metadata_extractor.py`,
`services/scanning/reconciliation.py`,
`services/metadata/album_tag_sync_service.py`,
`db/repositories/popularity_repository.py`,
`db/repositories/navidrome.py`,
`tests/test_navidrome_import_report_quirks.py` (new)

## Report

* Deftones imported `album_index` 1,2,3,5,6 — index 4 vanished with no
  `Importing album` line and no skip/error, indistinguishable from a crash.
* 4 outer artist workers × 4 album-tracklist workers = up to 16 concurrent
  requests; Navidrome's `Fetched X album(s)` rose from 0.0s to 6.7–9.8s.
* `composer` accumulated producers/engineers/the band and repeated names:
  `"Gary Rossington, Ronnie Van Zant • Gary Rossington • … • Terry Date • … •
   Deftones"`.
* An empty local `disc_number` / `albumversion` was protected against
  Navidrome's value just because the row had a title correction.

## Fixes

### 1. A skipped album index is always explainable

The album loop now emits a scan-log line for every non-fast skip:
album filter, missing Navidrome album id, duplicate album id for another
artist view, and duplicate CONTENT for another view. Unchanged albums
(fast/fingerprint skips) are normal and would flood the log, so they are
summarised once at the end of the artist:

```
[NAVIDROME_IMPORT] Deftones: skipped 1 unchanged album(s) (already imported, no changes)
```

### 2. Outer artist concurrency defaults to 2

`POPULARLR_IMPORT_WORKERS` (default `2`, clamped 1–4). Each artist still uses
up to `navidrome.album_fetch_workers` (default 4), so the burst is 8 concurrent
Navidrome requests instead of 16.

### 3. `composer` is a composer credit, not a credit roll

- `extract_track_metadata` now prefers an explicit `composer` tag; when it must
  fall back to `displayComposer`, names credited as producer / engineer /
  mixer / remixer / performer / conductor / DJ-mixer, and the track/album
  artist, are removed.
- Credits are split on `•`, `;`, `,`, `/`, `|`, ` & ` and `and`, then
  deduplicated (order-preserving).
- `album_tag_sync_service` no longer copies the mixed `writer` store into the
  COMPOSER tag. It prefers `tracks.composer`, falls back to `writer` only when
  empty, and applies the same non-composer exclusion before writing.
- `local_authority_conflicts` compares composer names after removing the same
  non-composer roles, so an existing bloated value now compares EQUAL to the
  cleaned provider value and is overwritten (healed) on the next import.

### 4. An empty local value is never a correction

- `local_authority_conflicts` ignores a field whose local value is empty when
  the provider sends a value.
- The upsert CASE (`popularity_repository`, and the newer
  `bulk_upsert_navidrome_tracks`) accepts `EXCLUDED` when the stored value is
  NULL/blank, before the dirty-row authority check. A title edit no longer
  freezes `disc_number=1` / `albumversion`.

### 5. `Various Artists; HIM` is recognised

`_has_various_artist_part` split only on `" ; "` with whitespace on both sides,
so the no-space form failed. The separator now accepts optional whitespace.

## Tests

`tests/test_navidrome_import_report_quirks.py` — composer filtering/dedupe,
tag-sync composer source, empty-local acceptance, bullet-grouped comparison,
bloated-composer healing, skip logging, and the worker default.

`tests/test_navidrome_va_album_artist.py` also passes with the semicolon fix.

## Not changed

Deep Purple's `0 known track(s)` when its only appearance is on a Various
Artists compilation is a data-indexing limitation, not a display bug. The
prefetch deliberately scopes by album artist so a VA album cannot be treated
as the artist's own for cleanup. Indexing compilation appearances needs a
separate per-track-artist index and is left for a dedicated change.
