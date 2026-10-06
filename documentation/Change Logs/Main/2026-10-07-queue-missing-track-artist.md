# Queue a missing track as its own artist — not the album artist (2026-10-07)

## Reported

> When items are added to the queue, they are still adding as album artist…
> When missing releases are added to the album pages, are they added along with
> track artist to the table? **Maybe that's why they aren't adding track artist.**

Reproduced with **MTV2 Headbangers Ball, Volume 2** — a compilation where every
row queued as `Various Artists`, so Soulseek searched under the label instead of
the band.

## Root cause

Four queue buttons read a field the response never sends, so the value was
`undefined` and the fallback supplied the album artist (or, in one case, nothing
at all). The earlier fix (`f17d81f7`) had already corrected the **album page**
and the **release download path** — this was the same defect in the two pages it
did not touch:

| Page | Payload read | What that response actually has |
|---|---|---|
| `missing-releases.js` (row `data-artist`) | `t.track_artist \|\| t.artist` ✗ — `.artist` never sent → `undefined` → fallback = **album artist** | rows are keyed `track_artist` (the DB column name) |
| `missing-releases.js` (bulk queue) | `track.track_artist \|\| track.artist` — same `.artist` trap | `track_artist` only |
| `artist-corrections.js` (`queueMissingTrackFromCorrections`) | `track.album_artist \|\| track.artist` ✗ — **neither exists** → queued with **no artist at all** | `track_artist` only |
| `artist-corrections.js` (`queueTitleMismatch`) | `m.album_artist \|\| m.artist` ✗ — wrong order: rows carry both, and `album_artist` wins on a compilation | both, with `artist` = the performer |

Verified **not** broken (left alone): `album_detail.js` / test_site `album.js`
(`track_artist || mb_artist || pageArtist`), `musicbrainz-queue.js` and
`downloads.js` (their `_flatten_release` tracklists do carry `artist`), and
`playlist-import-csv.js` (`artist: track.artist` with `album_artist` separate).

## Fix

Read the field the response actually sends, and prefer the recording's own
credit in every payload:

- `missing-releases.js` — `data-artist="${esc(t.track_artist || t.artist || artist)}"`
  and `artist: track.track_artist || track.artist || artist,`
- `artist-corrections.js` — `artist: track.track_artist || track.artist || artistName,`
  (with `album_artist: track.album_artist || artistName`) and, for title
  mismatches, the order swapped to `artist: m.artist || m.album_artist,`.

The album artist still travels in its own `album_artist` field, so folder layout
is unaffected; the album artist remains the fallback when a row carries no track
credit.

## Tests

`tests/test_missing_release_queue_artist.py` (9):

- the four payload/`data-artist` field orders;
- functional: a persisted `missing_album_tracks` row comes back from
  `get_missing_tracks_from_db` as `track_artist` with **no** `artist` key —
  documenting exactly why the JS must name the field;
- controls: the rest of the queue payload (`title`, `track_number`,
  `release_id`, `recording_mbid`) and `album_artist` are still sent; both files
  still parse under `node --check`.

## Oracle

Stashed the two JS sources → **4 failed, 5 passed** (every source assertion
failed; the functional/control/syntax tests are source-independent) → popped →
markers present → stash list back to 3.

## Verification

- Affected set (`missing-releases` / `artist-corrections` /
  `album_missing_service` / `album/missing-tracks`) → 14 files, 5 failed / 324
  passed → **0 new** vs the baseline subset.
- Full suite → baseline reconciliation (`_qartist2_full.txt`).
