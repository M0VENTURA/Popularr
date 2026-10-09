# VA compilation downloads: album name mismatch was hard-rejecting the right file

**Date:** 2026-10-10 · **Area:** `downloads` / `slskd` / `queue`

## Reported

> Sometimes when tracks are downloaded from the queue, if they download a
> version from another release (especially with various artist collections) it
> doesn't match due to the album name being different even though the track
> artist matches the artist and song title.

## Root cause — the Hard Album gate in `_score_result`

When a Soulseek search returns a file whose name explicitly carries a
DIFFERENT album than the queue item's, the scorer hard-rejects it:

```python
if album_mismatch and title_score < 0.85:
    return 0.0
```

That gate exists to stop a wrong-album grab (pinned as
`TestWrongAlbumRejection`, "The Eternal" case). But **two legitimate situations
produce exactly that shape**:

1. **A track queued from a Various Artists compilation.** The song also lives
   on the artist's OWN release, and peers almost always carry it from there —
   so the file album ("Mer de Noms") differs from the queue album ("MTV2
   Headbangers Ball") even though artist + title agree. The old gate rejected
   the correct file outright; every search round then came back
   `no_qualifying_result` and the queue row sat there.

2. **A version/edition marker.** The raw `title_score` for
   "Weak and Powerless (Album Version)" vs "Weak and Powerless" drops below
   0.85 even though the BRACKET-STRIPPED core is the same song. The gate
   already had a carve-out for an EXACT title; a strong core match is the same
   carve-out, one level down.

## Fix — two relaxations, each still artist+title-gated

Both are on the album gate only; the artist and title gates are untouched, so
a genuinely wrong file is still rejected.

**`_queue_item_is_compilation(...)`** — the compilation signal. A queue row
whose `album_artist` (or per-track artist) is in `_GENERIC_COMPILATION_ARTISTS`
("Various Artists", "Various", "VA", "V/A", "compilation", "soundtrack", …)
skips the hard album reject. It mirrors `queue_metadata_matcher`'s
`queue_is_compilation`, which uses the SAME placeholder set — so the two
verifiers cannot disagree about what a compilation is. The parameter is
threaded from `process_queue_item` → `_select_best_result` → `_score_result`
as `expected_album_artist`, **keyword-only**, so the existing positional
contract (and every caller/test that passes duration/year positionally) is
untouched.

**Core-title override.** `not _title_core_match` was added to the reject
condition — a strong bracket-stripped title match overrides an album mismatch
the same way an exact title already did.

## Tests

`tests/test_slskd_wrong_artist_rejection.py` gains:

- `TestCompilationAlbumLeniency` — a VA row (album "MTV2 Headbangers Ball",
  performer "A Perfect Circle") accepts "Weak and Powerless" from "Mer de
  Noms" and clears the 45.0 accept floor; the "VA" placeholder variant; and
  the artist-fallback case.
- `TestCoreTitleOverridesTheAlbumGate` — the annotated-title-from-another-
  release case (with a 4-segment filename so the parser genuinely sees the
  mismatched album), and a CONTROL pinning that "The Eternal" wrong-album case
  is still rejected.
- `TestQueueItemIsCompilation` — the placeholder predicate both ways.

**Oracle:** reverting `download_pipeline_service.py` → 6 failed (4 new
discriminators + the 2 pre-existing suite failures), restored → 2 (the
pre-existing pair only).

**Sweep:** 20 test files grepping the changed module, one process each —
baseline 21 failing IDs vs changed 18, `Compare-Object`: **0 new**, 3 gone
(the discriminators). `test_download_match_lifecycle`'s 10 failures reproduce
identically on a clean baseline (pre-existing: queue-table harness drift).
`import app` → 399 routes.

## Notes

- The artist-evidence and hard-title gates are untouched, so the leniency only
  ever applies when a candidate is already a strong artist+title match — the
  reported case is precisely "the track artist matches the artist and song
  title".
- The completion-time verifiers (`_metadata_matches_queue_item`,
  `_file_artist_matches_queue_item`, `filename_matches_queue_item`) never
  compare the ALBUM, so no matching changes on the import side — the fix is
  purely on the search side where the album name was consulted.