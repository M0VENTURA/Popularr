# A sparse library no longer position-matches unique-titled tracks to WRONG release slots

**Date:** 2026-10-12
**Area:** metadata / scan / matching
**Commit:** `fix(metadata): a sparse library no longer aliases a unique-titled track to a missing neighbour's release slot`

## Reported

> It's detecting wrong information for The Power of Love, but Love Shack is
> track 5 and still in the queue to be downloaded so it's incorrectly matching
> and trying to change the wrong track.

The album is "Take 40 Australia: The Number Ones" — a compilation. "Love
Shack" (release track 5) is queued for download and **not in the library**, so
the local folder's files are numbered 1..M while those recordings sit at
release positions shifted by the gap. The local file "The Power of Love"
(release position 13) sits at **local position 5** — directly on top of
"Love Shack"'s release slot.

## Root cause

The per-row identity loop in `get_listenbrainz_album_tracklist_with_release`
emitted a **position-qualified alias for EVERY local row**, even when the
title-keyed pass had ALREADY resolved that row by its unique title. So:

1. "The Power of Love"'s unique title-keyed identity was already correct.
2. The per-row loop ALSO aliased `local#5` → `release#5` = "Love Shack"
   (recording MBID + `release_track_title`).
3. `_build_album_recording_batch` prefers the position alias → the batch
   carried "Love Shack" as the title → the track-stage promotion/recommendation
   proposed changing "The Power of Love" to "Love Shack".

This is the sparse-library hazard the earlier "Little Nicky" comment already
warned about — *a library holding a subset numbers its files 1..M while those
recordings sit at release positions k..k+M-1, so local#N is a DIFFERENT song
than release#N* — but the per-row alias loop never had a guard.

## Fix

The alias loop now skips emitting a position alias when the **local** title is
unique among the **local** rows AND the release resolves that title uniquely:

```python
if (_existing_title.get("recording_mbid")
        and _existing_title.get("title_is_unique")
        and _local_title_counts.get(local_key, 0) <= 1):
    continue
```

Key details:

- **LOCAL uniqueness, not release uniqueness.** dArtagnan's "Helden X Hymnen"
  is TWO local rows sharing one title (the unplugged rendition lost its
  marker) — the release resolves the title to one recording, but that identity
  cannot name which local row. Those rows still fall through and keep their
  position aliases (the disambiguation the loop was built for, pinned by
  `test_same_titled_rows_get_their_own_recording`).
- **Version-marker tracks** ("Farewell (feat. Patty Gurdy)" vs release
  "Farewell [Unplugged Version]") get no title-keyed identity at all, so their
  aliases are still emitted.
- A **sparse library** where a unique-titled file sits at a shifted local
  number now keeps its correct title-keyed identity and never picks up a
  missing neighbour's release slot.

## Tests

`tests/test_sparse_library_position_match_guard.py` (3):

- `test_unique_title_gets_no_wrong_position_alias` — "The Power of Love"
  (unique title) at local#5 keeps its OWN recording; no alias points at Love
  Shack. The fixture omits the local duration deliberately: the position
  pass SKIPS its length guard when either side's duration is missing, which is
  the production trigger for the wrong recommendation. (A fixture WITH both
  durations differing >5s would skip the alias harmlessly even at baseline —
  the first version of the test did not discriminate.)
- `test_the_wrong_release_title_is_never_published` — a row carrying
  "The Power of Love"'s recording never carries "Love Shack" as its release
  title.
- `test_a_missing_track_does_not_resolve_anything` — no LIBRARY row resolves
  to Love Shack's recording (the release catalog legitimately retains its own
  keys; consumers read local titles only).

**Oracle** at `d061adfd`: `test_unique_title_gets_no_wrong_position_alias`
fails (the wrong alias is emitted), the 2 control assertions pass.
**Sweep** (7 position/identity/title suites): base 4 failed / new 3 failed —
the only-in-BASE is exactly my oracle test; the 3 shared failures
(`test_album_release_track_identity … test_the_identity_is_marked_unambiguous`
and the two `test_lb_album_tracklist_translated_titles` Korean-position tests)
fail at pristine baseline in isolation → **0 regressions**, and the dArtagnan
same-titled disambiguation stays green.