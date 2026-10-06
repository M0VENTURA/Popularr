# A persisted missing track no longer keeps the album artist

**Date:** 2026-10-06
**Reported:**

> Missing tracks are still adding "various artists" to the queue search
> `Various Artists - Do You Hear What I Hear? [A Very Special Christmas]`
> **I think it's due to the way missing tracks are populated.**

The report's diagnosis was right.

## Why it survived the earlier fix

`f17d81f7` fixed the *computation*: comparison entries now carry `mb_artist`
(the recording's own credit) and the derivation path reads the recording credit
from `_flatten_release`, so a freshly computed missing track stores its
performer.

But the computed value only ever reaches the table through
`_persist_missing_tracks`, and that upsert **skipped any row whose
`(disc, track, title)` already existed**:

```python
existing_keys = {_missing_row_key(row) for row in existing}
for m in missing:
    if key in existing_keys:
        continue          # ← the row is left exactly as it was
    INSERT ... ON CONFLICT DO NOTHING
```

Every row written *before* `f17d81f7` therefore kept its original
`track_artist = 'Various Artists'`, the album page reads that column into
`data-artist`, and the queue searches under it — indefinitely, because no scan
or Compare ever rewrites it. Correcting the source without repairing what is
already stored fixes nothing for the rows that exist.

## Changes

`services/metadata/album_missing_service.py::_persist_missing_tracks`:

* the existing-rows `SELECT` now reads `track_artist`;
* an already-persisted row whose artist differs from the freshly computed one
  is **updated** instead of skipped;
* **a placeholder never overwrites a real performer.** MusicBrainz returns no
  credit for some recordings and `_flatten_release` then falls back to the
  release credit — which on a compilation *is* "Various Artists". Letting that
  win would trade a stored good value for a placeholder and reintroduce the bug
  one step back. So the update runs when the new value differs *and* is either
  a real performer or the stored value is empty;
* one INFO line — `Refreshed stale missing-track artists … refreshed=N` — so an
  operator can see the repair happen instead of guessing why the queue is still
  searching for "Various Artists" after a scan.

Inserts and the staleness `DELETE` are untouched: a genuinely new missing row
still inserts, and a row that is no longer missing still goes away.

## Tests

`tests/test_album_missing_and_disc_cleanup.py` — **16** (was 9; +7 in
`TestAPersistedRowNeverKeepsTheAlbumArtist`, driven against a recording fake
session so the SQL itself is asserted):

* a stored placeholder is refreshed to the performer — the reported bug;
* an empty stored artist is filled;
* CONTROL: a real performer is **not** downgraded when MusicBrainz returns no
  credit (and the row is not duplicated either);
* CONTROL: an unchanged artist is not rewritten;
* CONTROL: a new missing track is still inserted;
* CONTROL: a row that disappeared is still deleted;
* the repair is logged.

**Oracle:** with `album_missing_service.py` stashed → **3 failed / 13 passed**,
the 3 being exactly the tests that describe the new behaviour and all 4
controls passing. Restored, 16/16.

## Verification

- new suite: **29 passed** (this file + `test_queue_track_artist.py`)
- affected set (35 files): **658 passed / 20 failed**, against a baseline
  subset of 20 for the same files → **0 new, 0 fixed**
- full suite: **224 failed / 4737 passed / 2 skipped** in 613 s against a
  baseline of 303 failures → **79 fixed, 0 new**.

  The first attempt at this run died at 34% with `Windows fatal exception:
  access violation` inside Python's own `linecache`/`traceback` — the known
  environment flake this machine has produced several times today (disk had
  11 GB free), not a test failure. Re-ran clean.

## Files

- `services/metadata/album_missing_service.py`
- `tests/test_album_missing_and_disc_cleanup.py`
