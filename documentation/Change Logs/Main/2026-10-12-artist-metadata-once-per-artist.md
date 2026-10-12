# 2026-10-12 — Artist-level metadata is refreshed once per artist, not checked every album

## Report

Album-level caching review: **release-country, artist image, artist bio and
similar artists** are already cached but still checked on every album during a
scan; the suggestion was to move those checks to the artist stage so they
happen once per artist scan.

## What was verified

- **Similar artists** — already artist-level (`9bb2ae5a`, 2026-10-11):
  `refresh_similar_artists` runs ONCE PER ARTIST in the scan runner's prefetch
  thread; the per-album path is the DB-only `_read_similar_artists`. Verified
  intact. Not re-done.
- **Artist metadata (bio / country / image / members)** — still per-album:
  `_run_full_enrichment` (called once per album on metadata/full passes, and
  again per album in post-singles `enrich_album_extras`) called
  `_fetch_artist_metadata`, which re-reads the `artists` row and re-runs the
  "already cached" skip-checks for EVERY album of the artist. Until the cache
  write landed (first scan), the network lookups ran per album too.

## Fix (mirrors the similar-artists pattern)

- `album_stage._fetch_artist_metadata` stays the NETWORK refresh (biography /
  MusicBrainz country / AudioDB image / MusicBrainz members with its 7-day TTL)
  and is now invoked **once per artist** by the scan runner's prefetch thread
  (`_prefetch_artist_work`, inside the existing `if not _singles_pass:` gate).
  No config change.
- New `album_stage._read_artist_metadata(artist)` — a **DB-only** read of the
  `artists` cache (same SELECT the old path used). The per-album
  `_run_full_enrichment` now calls it, so an artist with N albums costs N
  cheap local SELECTs instead of N DB reads + N skip-check passes. The
  release-country backfill reads the value the prefetch persisted.
- The prefetch runs synchronously before the album loop, so the per-album read
  sees the freshly-persisted values; if the prefetch budget abandons it, the
  per-album path degrades to reading the previous cache (same graceful
  degradation as the popularity/similar-artists prefetch).

## Tests

`tests/test_artist_metadata_once_per_artist.py` (12):

- **Wiring (album path is DB-only):** `_run_full_enrichment` source reads
  `_read_artist_metadata(artist)` and no longer calls `_fetch_artist_metadata(`;
  the country backfill still consumes the read value.
- **Wiring (prefetch owns the refresh):** `_prefetch_artist_work` contains
  `_fetch_artist_metadata(artist)` AND `refresh_similar_artists(artist, options)`,
  both inside the `if not _singles_pass:` gate.
- **Behaviour:** `_read_artist_metadata` returns cached values / all-None on no
  row / all-None on a failing session, and with a full row performs ZERO
  network calls (`_call_with_heartbeat` replaced with `pytest.fail`).
- **Controls:** a fully-cached `_fetch_artist_metadata` is a true no-op; an
  empty row still runs the lookups.

**Oracle** (both source files stashed, tests kept): **8 failed / 4 passed** —
the failures name each defect (album path still refreshing, prefetch lacking
the call, `_read_artist_metadata` absent); the passers are controls.

**Baseline comparison for the 4 suites with failures** (changed vs stash of the
2 sources): 11 failing ids IDENTICAL on both trees (`test_album_type_persistence`
, `test_artist_tracklist_loading` ×4, `test_release_category_persistence` ×6 —
all documented pre-existing). The remaining 2 (`test_full_scan_startup_fix`
`TestPopularityRunRouteStaleRowSelfHeal`) failed on ALL THREE baseline runs too
— the known `no such table: scan_states/scan_history` SQLite-harness flake,
**not** a regression.

**Sweep** (10 suites, one process per file): all green — `test_artist_page_is_db_only` 28,
`test_similar_artists_artist_level_only` 14, `test_similar_artists` 6,
`test_album_scan_budget` 54, `test_prefetch_cache_config_and_per_source` 14,
`test_scan_windows_and_scheduled_scans` 38 (+ the two 2026-10-12 queue-search suites).

`py_compile` ×2, `git diff --check` clean, `import app` → 399 routes.