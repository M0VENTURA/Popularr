# A Various-Artists track keeps its own credit in the queue (2026-10-09)

**Area:** enrichment (MusicBrainz release flatten) → download queue
**Commit:** `fix(queue): read the track's MusicBrainz credit, not the release's`

## Reported

> Some tracks are still being added to the download queue as Various Artists.
> This is from the missing releases table on the album page.

(Compilation: **MTV2 Handpicked, Volume 2** — every row queued as
`Various Artists`, several *Backed off*.)

## Root cause — the TRACK's credit was never read

`_flatten_release` built the per-track artist as:

```
recording artist-credit  →  release's joined credit  →  primary credit
```

and read **only** `medium.tracks[].recording["artist-credit"]`. On a
Various-Artists release MusicBrainz puts the band on
`medium.tracks[].artist-credit` and frequently leaves the RECORDING's credit
unset — so the chain fell straight through to `Joined_artist_credit`
(`"Various Artists"`) for every track of the compilation.

Probed against the shipped function (one track, release credited
"Various Artists"):

| shape | before | after |
|---|---|---|
| recording credit only | `Jimmy Eat World` ✅ | `Jimmy Eat World` ✅ |
| **TRACK credit only (MB's usual VA shape)** | **`Various Artists`** ❌ | `Jimmy Eat World` ✅ |
| neither | `Various Artists` | `Various Artists` (honest fallback) |
| recording credit is the label, track credit is the band | `Various Artists` ❌ | `Jimmy Eat World` ✅ |

`artist` is what every consumer keys on, so one wrong value at the flatten
spread everywhere:

* `_match_mb_tracks_to_library` copies it into `mb_artist`;
* `get_missing_tracks` stores it as `missing_album_tracks.track_artist`;
* `persist_missing_from_comparison` reads `entry["mb_artist"]`;
* the album page's queue button sends `track_artist` to `/api/queue/add`.

That is also why so many of those rows show *Backed off* — Soulseek was asked
for a band that is a label, and the scorer's artist gate rejected everything.

## Fix

Read the **track's** credit between the recording's and the release's, and skip
a **placeholder** anywhere in the chain: MusicBrainz may credit a recording to
"Various Artists" while naming the band on the track, and taking the placeholder
first would defeat the whole point of reading the track's credit
(`helpers.normalization_service.is_track_artist_placeholder`).

The release credit stays the **last** resort — it is the right answer for a
single-artist album, and a queue row still needs a non-empty artist to search
with (`add_to_queue` refuses an empty artist).

## Self-healing for rows already persisted

`_persist_missing_tracks` already refreshes a surviving row's `track_artist`
when the newly computed value differs and is not a placeholder — so the stored
`missing_album_tracks` rows for these albums correct themselves on the next
scan or Compare. **Queue rows already created keep the artist they were queued
with**; delete and re-queue them (or re-run Compare first so the table is
right before you do).

## Tests

`tests/test_va_track_credit_is_read_from_the_track.py` — **8**:

* the four shapes above, including the placeholder-recording case;
* the credit reaches `mb_artist` on a comparison entry and
  `track_artist` for the missing-track row, and is not a placeholder (a
  placeholder would both queue wrongly and stop the stored row refreshing);
* **controls**: the recording credit still wins when it is real, the release
  credit is still the last resort, the ALBUM artist is still the release credit
  (folder layout keys on it), and the source shows the chain ordered
  recording → track → release.

## Verification

* New suite → **8 passed**.
* **Oracle** — reverting `musicbrainz_service.py` → **5 failed / 3 passed**: the
  two shapes that changed, the two payload-propagation tests and the source
  assertion. The 3 that pass either way are the controls. Restored → 8 passed.
* **Probe** — the shipped `_flatten_release` over the four shapes: the two
  broken ones now return `Jimmy Eat World`, the two fallbacks unchanged.
* **Sweep** — the 63 test files referencing the MusicBrainz flatten /
  track-artist / missing-track surfaces, clean `origin/develop` vs this change:
  **baseline 42 failures, changed 42**, `Compare-Object` on the sorted
  `^(FAILED|ERROR) tests/` lines = **identical, 0 regressions**.

## Not changed

* **No placeholder is ever invented.** When MusicBrainz supplies no credit at
  all, the release credit still stands — `add_to_queue` needs a non-empty
  artist, and an empty one would fail the add outright.
* **Existing `download_queue` rows are not rewritten.** Their artist is what
  they were queued with and the correct performer is not stored anywhere else
  on the row; re-queueing is the repair.
* `_persist_missing_tracks`'s "a placeholder must never overwrite a real
  performer" guard, the 0.6 matching gates, and the album/queue field names
  fixed in `2026-10-07-queue-missing-track-artist`.
