# VA queue items verify title and duration, not the artist string

**Date:** 2026-10-07
**Area:** queue / downloads (completion verification)
**Commit:** `fix(queue): VA queue items verify title and duration, not artist`

## Report

```
[QUEUE] Various Artists - Weak and Powerless → failed: downloaded file
artist did not match queue item (deleted + rescheduled)
[QUEUE] Various Artists - Unholy Confessions → failed: downloaded file
artist did not match queue item (deleted + rescheduled)
```

Correct downloads of compilation tracks (A Perfect Circle, Avenged
Sevenfold on *MTV2 Headbangers Ball* / *Resident Evil*) were deleted and
rescheduled in a loop.

## Root causes

1. `_file_artist_matches_queue_item` guarded the **file** side against
   placeholder artists (a file tagged `Various Artists` defers), but the
   **queue** side had no mirror: a queue row artist of `Various Artists`
   could never equal the file's real artist, so the gate returned `False`
   and the download was destroyed.
2. Even with the gate deferring, `_metadata_matches_queue_item` returned
   `None` (defer) for VA rows, and the fuzzy-claim path only accepts on
   `True` — so the loop would have continued without the matcher change.

## Fix

- `services/downloads/download_completion_service.py`
  — `_file_artist_matches_queue_item`: when **all** queue-side artists are
  in `_GENERIC_COMPILATION_ARTISTS`, return `None` (defer) instead of
  `False`, mirroring the existing file-side guard.
- `services/queue/queue_metadata_matcher.py`
  — for a VA queue item, **exact title + strict duration (≤ tolerance) +
  no variant conflict** now returns `True`: title and duration ARE the
  verification on a compilation row. Different title or duration > 30 s
  still hard-rejects; concrete queue items keep their strict artist rule
  (`artist_score == 0` paths unchanged).

## Tests

Extended `tests/test_download_completion_unmatched_artist.py` (+5): VA
queue item + concrete file artist defers; VA accepts exact title + strict
duration; VA still rejects a different duration and a different title; a
concrete queue item still hard-rejects a mismatched artist.

**Gate:** targeted ✓ · oracle (the two acceptance tests fail on revert) ·
affected set 42 failed / 0 NEW · full suite 0 NEW (the file's 4 remaining
failures are pre-existing baseline items).
