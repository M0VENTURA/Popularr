# Navidrome import: the requested known/unchanged fast flow

**Date:** 2026-10-31
**Area:** Navidrome import performance
**Files:** `services/scanning/navidrome_album_state.py` (new),
`migrations/versions/035_navidrome_album_state.py` (new),
`db/schema.py`, `services/scanning/navidrome_import.py`,
`services/scanning/pipelines/navidrome_pipeline.py`,
`tests/test_navidrome_album_state_fast_skip.py` (new),
`tests/test_navidrome_import_speedups.py` (new)

## Flow implemented

```
Artist
  ↓
Get Navidrome album summary (artist album list)
  ↓
Known album?
  ├─ No  → FULL IMPORT (fetch tracklist, store state)
  └─ Yes
       ↓
Clearly unchanged since last successful import?
       │  persisted summary hash + same Navidrome scan marker
       │  + matching local track count + no dirty/reimport rows
       ├─ Yes → FAST SKIP (no tracklist request)
       └─ No
            ↓
       Fetch tracklist (parallel across albums)
            ↓
       Track IDs + metadata fingerprint unchanged?
            ├─ Yes → SKIP (and refresh state)
            └─ No  → RECONCILE / IMPORT (and refresh state)
```

## State table

`navidrome_album_state` stores per Navidrome album ID:

- album key/name/artist and `song_count`;
- a hash of the Navidrome album summary (name, artist, song count,
  timestamps, MBID, release dates/types, etc.);
- the sorted track-ID hash and the existing metadata fingerprint;
- the Navidrome `lastScan` marker at the last successful import.

The table is created at startup (`TABLES_TO_ENSURE`) and by migration 035.

## Why the fast skip is safe

The summary hash alone is not proof — a track title can change while the album
summary stays identical. The fast skip additionally requires that the
**Navidrome scan marker is unchanged** since the last successful import. With no
new scan, Navidrome has not changed anything. If a scan did run, the marker
changes and the importer fetches the tracklist and falls back to the exact
track-ID + metadata fingerprint comparison.

Dirty local rows or albums needing reimport always take the full path.

## Other speed-ups in the same change

- **Parallel album-tracklist prefetch** (`navidrome.album_fetch_workers`,
  default 4, clamped 1–8) for albums that still need a full check. The loop
  consumes the cached result.
- **Delta mode is no longer disabled by dirty metadata.** Dirty rows are
  preserved and confirmed when their artist actually changes; they are not a
  reason to crawl every unchanged artist. `force` still does a full import.
- **No repeated remote Navidrome scan when it already scanned after the latest
  local tag write** (`navidrome.skip_scan_after_local_write`, default true).
  The import compares `getScanStatus.lastScan` with
  `MAX(local_tags_written_at)` for dirty rows.
- Exact fingerprint skip still refreshes the stored album state so the next
  import can take the fast path.

## Config

```yaml
navidrome:
  fast_skip_unchanged_albums: true
  album_fetch_workers: 4
  skip_scan_after_local_write: true
```

First import after deploying populates the state table; unchanged albums are
fast-skipped from the next import onward.
