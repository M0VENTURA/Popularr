# Show what a folder was matched to (downloads monitor) (2026-10-07)

## Reported

> When on the downloads page, I select the manual match to an artist, it shows
> matched but doesn't list the details of the album it's matched with.
> **The End of Heartache (2004) [Album]** Matched ✓ /
> Killswitch Engage — The End of Heartache
>
> This was manually matched with Various Artists - MTV2 Headbangers Ball V2

## Root cause

The "Matched & Unmatched Folders" row (live `static/js/monitor.js` and its
test_site mirror) renders two things only:

1. the folder's **display name** (its on-disk name) plus a `Matched ✓` badge;
2. `artist — album`, which is derived from **the files' tags**
   (`_derive_folder_group`), *not* from the match.

Everything needed to describe the match was already being produced —
`get_unmatched_folders()` ships `match` (the `folder_matches` row: release
MBID, release title, artist, year) and the `folder/associate` response
returns the same — but **no code rendered it**. So after a manual match the
row flipped to "Matched ✓" while still showing the folder's own tags: on a
wrong match (a Killswitch Engage folder associated with an MTV2 Headbangers
Ball compilation) nothing on the page said so, and there was no way to check
what the matched album contained.

## Fix

- **Row details (both monitor pages):** when a folder has a match (or a
  tracked release), the row now renders `Matched to **release title** (year)
  — artist` with the release MBID in small text, directly under the folder's
  own tag line — the two facts are now visibly separate.
- **Matched album tracklist:** the block includes a `<details>` expander that
  loads the matched album's track titles through the new
  `GET /api/downloads/folder/match-tracklist?release_mbid=…` endpoint
  (`routes/downloads.py`), which reads cache-first via
  `fetch_missing_release_tracklist` (`missing_releases.tracklist`) and only
  reaches MusicBrainz on a miss — so expanding rows does not monopolise the
  shared 1 req/s throttle. Results are cached per MBID for the page session;
  re-renders (confirm/delete/queue refresh) show them instantly. A failed
  load is *not* cached — the row offers "click to retry".
- **Immediately after a manual match:** the associate callback now re-renders
  and then opens + loads the **new** association's tracklist (resolved from
  `release_mbid` in the associate response), so the details you matched for
  appear without a second click.
- Folders with no association render exactly as before.

## Tests

`tests/test_matched_folder_release_details.py` (18):

- endpoint: 400 without `release_mbid`, tracklist passthrough (asserting the
  artist-independent cache read), and a raising lookup → 200 with `[]`;
- functional: an associated folder's `get_unmatched_folders()` row carries
  the stored match (title/artist/year/MBID) **and** a different folder
  subtitle — the reason the match needed its own block;
- source assertions on both monitor pages: the `Matched to` block, the row
  append, the endpoint URL, toggle binding, per-MBID caching, retryable
  failures, no block without a match (control), and the fresh-match
  auto-open.

## Oracle

Stashed `routes/downloads.py` + both `monitor.js` → **17 failed / 1 passed**
(every source/endpoint assertion failed; only the source-independent payload
test passed) → popped → markers present → stash list back to 3.

## Verification

- Affected set (`monitor` / `unmatched-folders` / `folder_match` /
  `folder/associate` / `download_folder_service` / `match-tracklist`): 28
  files, 23 failed / 300 passed → **0 new** vs the baseline subset.
- Full suite → baseline reconciliation (`_mfd_full.txt`).
