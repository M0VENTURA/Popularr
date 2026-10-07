# A Lookup MBID save no longer writes genres to the files (2026-10-07)

## Reported

> Can you confirm that no genres save to the albums when doing an mbid
> lookup? They should only be saving to the tables, genres should only be
> written to the files during the metadata popularity or finalise scan.

## Confirmation (the audit)

**No — one path did write them.** The Lookup-MBID review stages per-track
values (including `musicbrainz_genres`) into the save's hidden
`#staged_track_updates` payload; the save persists that to the DATABASE
correctly (Phase 2), but Phase 3 (the file-tag jobs) ran
`build_tag_updates(payload)` — so the lookup also wrote genre tags to disk.
The suite even pinned it as intended behaviour
(`test_staged_genres_reach_the_file`), which is how it survived this long.

Full genre→file writer map **after** this fix:

| Writer | Status |
|---|---|
| album save / Lookup MBID review | **DB only** (this fix) |
| `/api/album/apply-genres` → `apply_genres_to_album` | explicit "write these genres to every file" action — untouched |
| bulk tag endpoint → `bulk_tag_tracks` | explicit bulk action — untouched |
| `essentia_scanner` | analysis scan — untouched |
| `sync_album_file_tags` (popularity / finalise scan) | **the** scan sync — untouched |

The "Cover" marker written when a save carries a cover verdict is deliberately
kept: it is the cover convention that mirrors the cover detector, not genre
data (still pinned by `test_album_save_phases`).

## Fix

- `_FILE_GENRE_TAG_FIELDS` in `routes/ui_routes.py` — every genre-family key a
  tag payload can carry (`genre`, `genres`, `musicbrainz_genres`,
  `lastfm_tags`, `manual_genres`, the other source columns).
- `_write_album_track_file_tags` drops them from `file_tags` **after**
  `build_tag_updates` and **before** `update_file_tags`; the Cover marker runs
  after the strip and is unaffected. The DB payload (Phase 2) is untouched —
  staged genres still land in the tables.
- The strip lives in the save's file phase, not in the tag writer, so the
  scan's sync keeps writing genres exactly as before.

## Tests

`tests/test_lookup_leaves_genres_to_the_scan.py` (8): staged MB genre never
reaches the file (title still does), every genre-family key is stripped, the
tag writer itself still understands genres (control), the Cover marker still
writes (control) and wins over stripped genres, the strip sits before the
write in the helper, and the documented writer inventory still exists.

`tests/test_album_review_save_persists.py`: `test_staged_genres_reach_the_file`
→ **`test_staged_genres_stay_out_of_the_file`** (the old test asserted the
superseded behaviour; the DB half remains pinned by
`test_staged_genres_are_written`).

## Oracle

Stashed `routes/ui_routes.py` → **5 failed / 34 passed** (every strip/rewrite
assertion; the writer/cover controls stay green) → popped → markers present →
stash list back to 3.

## Verification

- Affected set (24 files, `ui_routes` / `_write_album_track_file_tags` /
  `build_tag_updates`): **0 new** vs the baseline subset.
- Full suite → baseline reconciliation (`_final_full.txt`, shared with the
  Finalise-scan change — the two are disjoint file sets, one run validates the
  combined tree).
