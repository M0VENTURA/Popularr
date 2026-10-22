# A detected cover now keeps the `Cover` genre through the album-wide genre write

**Date:** 2026-10-22
**Area:** `services/metadata/album_tag_sync_service.py`
**Commit:** `fix(genres): a detected cover keeps its Cover genre`

## Report

> "Can you confirm that the 'Cover' genre is being added to any track that is
> detected as a cover song? It doesn't seem to be applying it."

It **was** being applied — and then destroyed, by the album-wide genre write
later in the same scan.

## What actually happened — three writes, in order

| # | Where | What it wrote |
| --- | --- | --- |
| 1 | `track_stage.process_track` | `is_cover = 1`, `musicbrainz_genres += "Cover"`, and the aggregation carried "Cover" into the per-track `genres` |
| 2 | `cover_detector_impl` (deep pass) | `is_cover = 1`, `musicbrainz_genres += "Cover"` — **never the `genres` column** |
| 3 | `sync_album_file_tags` | `UPDATE tracks SET genres = :g WHERE artist = :a AND album = :alb` — **every** track |

The blend built in step 3 deliberately has the affiliation tags **stripped**
from it (Cover/Live/Acoustic describe ONE recording, not the album — see
`_TRACK_AFFILIATION_FILTER_TAGS`). So step 3 replaced the cover track's
`genres` with a list that could not contain "Cover".

Measured with the real `sync_album_file_tags` on a two-track album:

```
before  cpt-cover  genres='Cover'
after   cpt-cover  genres='Progressive Metal'     <- the marker is gone
```

The flag survived, the genre did not — which reads exactly as "the cover is
detected but the Cover genre is missing".

## The fix

The album blend still owns the musical genres, but a track now keeps the
affiliation markers **it** earned:

* `_track_affiliation_markers(track)` reads them from the track's own **flags**
  first (`is_cover`/`is_live`/`is_acoustic`/`is_remix` are what the detection
  actually decided), then from its own genre sources — so a hand-tagged "Live"
  survives too. Whole-token matching, so "Cover Band Rock" is not a marker.
* `_merge_album_blend_with_affiliations(blend, markers)` **appends** them,
  deduped case-insensitively, leaving the album's genres in the leading
  positions (the playlist builder reads only the first few).
* The write is split **only when a track earned a marker**: the un-marked
  tracks still take the album blend in ONE statement (the common case is
  unchanged), and each marked track gets the blend plus its own markers.

```sql
-- un-marked tracks (unchanged, still a single query)
UPDATE tracks SET genres = :g WHERE artist = :a AND album = :alb
-- when markers exist
UPDATE tracks SET genres = :g WHERE artist = :a AND album = :alb AND id NOT IN :ids
UPDATE tracks SET genres = :g WHERE id = :id      -- per marked track
```

`manual_genres` is deliberately untouched — it is a per-track SOURCE column,
and writing the blend there is the "fabricated evidence survives every later
aggregation" trap the album-save path already guards against.

## Verified before/after (the same real function)

| Track | Before | After |
| --- | --- | --- |
| detected cover | `'Progressive Metal'` | `'Progressive Metal, Cover'` |
| ordinary sibling | `'Progressive Metal'` | `'Progressive Metal'` (control — no leak) |

## Tests

`tests/test_cover_genre_survives_album_write.py` (new, 20): the marker reader
(flags incl. every truthy spelling, own sources, CSV and JSON forms, the
whole-token rule, dedupe), the merge (append, no duplicate, empty blend,
leading-position preservation), and the end-to-end sync — the cover keeps both
its marker and the album's genres, the sibling takes only the blend, a Live
flag works the same way, an album with no markers still writes once, and
`manual_genres` stays empty.

**Oracle:** 17 of the 20 fail against the pre-fix code; the 3 passers are the
controls (the ordinary-sibling no-leak test, the no-marker single-write test,
and the `manual_genres` guard).

Sweep of 25 cover/genre/album suites diffed against a pristine clone —
**identical failure sets, 0 new failures** (the 2 pre-existing failures are a
source-window assertion in `test_recent_rescan_skips_completed_sections.py` and
`test_metadata_fanout_to_files.py`, both unrelated). `import app` → 425 routes.

## Files

* `services/metadata/album_tag_sync_service.py` — `_AFFILIATION_MARKERS`,
  `_AFFILIATION_FLAGS`, `_track_affiliation_markers`, `_genre_tokens`,
  `_merge_album_blend_with_affiliations`, and the split write.
* `tests/test_cover_genre_survives_album_write.py` — new.
