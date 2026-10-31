# Stale MusicBrainz IDs in files are now corrected, so the import conflict stops repeating

**Date:** 2026-10-31
**Area:** `metadata` (file-tag sync, save path) / `scanning` (Navidrome import)
**Files:** `services/metadata/tag_file_service.py`,
`services/metadata/album_tag_sync_service.py`,
`tests/test_album_tag_sync_service.py`

## Reported symptom

> ```
> [NAVIDROME_IMPORT] metadata conflict — local correction preserved
> artist='Slipknot' album='Iowa' track='...'
> fields='mbid,musicbrainz_trackid,recording_mbid'
> winner='existing' reason='local correction newer than last verified import'
> ```
>
> This keeps happening even though the settings are set in the config to update
> the metadata on save.

## Root cause — the local correction never reached the FILE

The import protects a dirty row's local values (correct, and intentional), so
the DB keeps the corrected recording MBID. But Navidrome reports the
**file's** `MUSICBRAINZ_TRACKID` tag, and nothing had corrected that tag:

1. **`build_tag_updates` did not map `recording_mbid`.**
   The save path maps DB columns to tag fields, and it knew the legacy aliases
   (`mbid`, `musicbrainz_trackid`) but **not the canonical `recording_mbid`
   column**. A save that corrected the recording ID was DB-only — the exact
   "update metadata on save" the report expects to work.
2. **The scan's file sync is fill-if-missing**, so it could never repair an
   existing-but-wrong identifier either. On the next scan the file still held
   the old ID, Navidrome echoed it back, the import preserved the DB correction,
   and the same warning was logged forever.

The `reason` text is accurate: the correction *is* newer and *is* preserved. The
bug is that the thing Navidrome reads — the file tag — was never updated.

## Fix

* `tag_file_service._COLUMN_TO_TAG_FIELD` — added
  `recording_mbid → mbid` (last, so the canonical column wins over a stale
  `mbid`/`musicbrainz_trackid` alias in the same payload). The writer already
  maps `mbid` to the canonical `MUSICBRAINZ_TRACKID` frame.
* `album_tag_sync_service.sync_album_file_tags` —
  * a dirty row (`metadata_dirty_for_navidrome`) is now **included in the file
    pass** even when the scan wrote nothing else, so an old stale tag can heal;
  * a dirty row's `recording_mbid` is offered to the file **even on a non-perfect
    album match** (the perfect gate still protects a mass sync — see the existing
    `test_db_tag_candidates_imperfect_excludes_mbids` — but a row the app has
    already corrected is not a mass sync);
  * `_FILE_IDENTITY_TAGS` (the MusicBrainz IDs) are **corrected**, not merely
    filled, when the file value differs. Verified identifiers are not a user
    spelling choice; fill-if-missing left them permanently stale;
  * identifiers just written are dropped before `_record_corrections`, so the
    pass does not re-open the discrepancy it just healed.

## Verification

`tests/test_album_tag_sync_service.py`:

| Test | Pins |
|---|---|
| `test_recording_mbid_maps_to_the_recording_tag` | the save path writes `MUSICBRAINZ_TRACKID` |
| `test_the_canonical_column_wins_over_a_stale_alias` | `recording_mbid` beats stale `mbid` |
| `test_a_dirty_rows_stale_recording_mbid_is_corrected` | ⭐ a dirty row's file tag is rewritten even when the album is not a perfect match |
| `test_a_clean_row_keeps_fill_if_missing_semantics` | CONTROL: the perfect-match safety gate is unchanged |

A full/forced metadata scan is needed to repair rows that were already sticky;
a re-save on the album page writes the ID immediately.
