# A known recording MBID is no longer re-searched

**Date:** 2026-10-11
**Area:** scan / MusicBrainz
**Commit:** `fix(scan): a known recording MBID is no longer re-searched`

## Reported

> "I noticed repeated patterns like 'recording suggestion completed' for
> every track, even after a recording MBID is known."

## Root cause

The scan enriches a track that has an MBID but **no genres**
(`_track_needs_metadata_enrichment` returns `True` when
`not _has_real_genres`), because genres are the last field the MB metadata
fetch supplies. That path calls:

```python
mb_data = mb_service.lookup_recording_metadata(..., mbid=_track_mbid or None)
```

but `lookup_recording_metadata` **ignored the `mbid` kwarg** — it always ran
`get_suggested_mbid(title, artist)`, a full `recording.search`, before the
`get_recording` metadata fetch. So every track with an MBID-but-no-genres
paid for a redundant search against the shared 1 req/s budget, on every
scan, producing the "recording suggestion completed" spam.

The search is also *harmful* here, not just wasteful: `get_suggested_mbid`
re-ranks candidates, so a tie-break can pick a **different** recording than
the one already stored — MBID churn.

## Fix

`lookup_recording_metadata` now reads `kwargs["mbid"]`:

- **Known MBID** → skip the search entirely, fetch metadata directly from
  `get_recording(mbid)` (confidence 1.0). This is the only call still
  needed — it supplies the genres/ISRC/work-rels the track is missing.
- **No MBID** → unchanged (search, then fetch).
- **Known MBID that no longer resolves** (deleted/invalid recording) →
  recover with **one** search rather than leaving the track un-enriched.
  A single recovery search, not a per-scan one.

The module-level `lookup_recording_metadata` wrapper now also accepts and
forwards `mbid`, so a future caller routed through it cannot silently lose
the optimization.

Note: the second per-track call site (`track_stage.py:1569`, the
ListenBrainz identity path) was already correctly guarded by
`if listenbrainz_listens == 0 and not recording_mbid`, so it never
re-searched a known MBID.

## Tests

`tests/test_known_mbid_skips_recording_search.py` (8):

- Known MBID → **zero** `recording.search` calls, one `get_recording` call,
  correct `recording_mbid` returned.
- No MBID → search still runs (unchanged).
- Stale MBID → one `get_recording` on the known id, then one recovery
  search; result is the searched recording.
- Stale MBID + empty search result → `{}` (no infinite loop).
- Blank/whitespace MBID treated as unknown (search still runs).
- Source-level contract: the `track_stage` call site forwards `mbid=`; the
  module wrapper's signature accepts `mbid`.

**Oracle** at `7594fa96`: 4 of 8 fail (the core bug + stale-MBID behaviour
+ wrapper), 4 pass (controls). **Sweep**: 11 MB/recording/scan suites →
17 failed / 141 passed, failing set **identical** to baseline (all 17
pre-existing, mostly `test_mb_batch_version_resolution` +
`test_metadata_update_album_split` fixtures that predate this change).
