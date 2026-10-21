# A downloaded album now imports with the Release Name, Release Year and MusicBrainz genres

**Date:** 2026-10-22
**Area:** `services/downloads/download_completion_service.py` ·
`services/queue/queue_processing_service.py` ·
`services/metadata/tag_file_service.py` ·
`services/downloads/download_metadata_heal_service.py`
**Commit:** `fix(downloads): import the Release Name, Release Year and album MusicBrainz genres`

## Report

> "When downloading an album, it updates all the metadata from Musicbrainz,
> but currently is missing adding Catalog Number, Release Date, Release Name
> and Release year and musicbrainz genres."

## What the download path actually delivered

Measured against the real import chain
(`_flatten_release` → `_album_level_mb_fields` → the queue row →
`_resolve_album_level_metadata` → `_apply_stored_metadata` →
`update_file_metadata` + `insert_or_update_track`), not read off the source:

| Field | Release payload | Queue row | File tag | `tracks` row |
| --- | --- | --- | --- | --- |
| Catalog Number | ✅ | ✅ | ✅ | ✅ |
| Release Date | ✅ | ✅ | ❌ **dropped** | ✅ |
| Release Name | ✅ | ❌ **dropped** | ❌ | ❌ |
| Release Year | ✅ | ❌ **dropped** | ❌ | ❌ |
| MB genres (album) | ✅ | ❌ **dropped** | ❌ | ❌ |

Two of the five the user named were already carried; the other three were not,
and the release date reached the database but not the file.

## Root causes

1. **`_ALBUM_LEVEL_COLUMNS` did not name `release_title` / `release_year`.**
   That list is what the queue row persists and what
   `_resolve_album_level_metadata` reads back, so the two edition fields were
   discarded at the very first step. `_flatten_release` had emitted them all
   along (`specific_release_title` / `version_release_year`), which is why the
   album page's **Lookup MBID** proposed both while a download got neither.

2. **`_ALBUM_MB_TAG_COLUMNS` did not name `releasedate`.** The album page
   writes it (via `build_tag_updates`), the import did not — so the database
   and the file disagreed about the same release.

3. **The album-level genres were never collected.** The release payload
   carries the release **group's** and the release's own curated genres
   (`release_group_genres` / `release_genres`, surfaced by `_flatten_release`),
   but only the per-**recording** genres travelled. An album whose release
   group is fully tagged and whose recordings are not therefore came back with
   no genre at all.

## The fix

* `_album_level_mb_fields` now maps `release_title` (preferring
  `specific_release_title`) and `release_year` (preferring
  `version_release_year`), and attaches the album genres under
  `musicbrainz_album_genres`.
* `_album_level_genres()` builds that list — release **group** first, then the
  release — and strips the per-track affiliation markers. ⭐ It is deliberately
  **not** a `tracks` column, so it is kept out of `_ALBUM_LEVEL_COLUMNS`
  (which builds a `SELECT`) and travels under its own key.
* `_ALBUM_LEVEL_COLUMNS` gains `release_title` / `release_year`;
  `_ALBUM_MB_TAG_COLUMNS` gains `releasedate`; the import's `_mb_cols` gains
  the two edition columns; `HEAL_COLUMNS` gains `release_title`.
* The queue adder persists the album genres on the row.
* The import applies the album genres to a track's `genres` column **as a
  fill** — only when the track has no MusicBrainz genres of its own, matching
  the album page's rule. ⚠️ **Database only**: the file's genre tag belongs to
  the popularity/finalise scan's DB → file sync, and writing it here would make
  the import a second genre writer, which the album page's save already
  deliberately avoids.

## Tests

`tests/test_download_import_edition_and_genres.py` (new, 19 tests): the field
mapping (edition name, edition-vs-original year, the `specific_release_title`
preference), the genre list (group-then-release order, affiliation filtering,
case-insensitive dedupe), the queue row carrying all three, and the import
applying them.

**Oracle:** 15 of the 19 fail against the pre-fix code; all 19 pass after.

`tests/test_album_metadata_release_scoping.py` — its `SIBLING_A`/`SIBLING_B`
fixtures were **completed** with the two new columns. They model a library row
that is meant to be a *complete* source, so a sibling missing `release_title`
correctly fell through to MusicBrainz and the "same-release sibling is a LOCAL
source" test failed. The fixtures represent a full row; the code was right.

Sweep of 29 download/queue/tag/album suites diffed against a pristine
`develop` clone — **identical failure sets, 0 new failures.** `import app` →
425 routes.

## Files

* `services/downloads/download_completion_service.py` — the field mapping,
  `_album_level_genres`, `_ALBUM_LEVEL_COLUMNS`, the import's column set and
  the genre fill.
* `services/queue/queue_processing_service.py` — persist the album genres.
* `services/metadata/tag_file_service.py` — `releasedate` in
  `_ALBUM_MB_TAG_COLUMNS`.
* `services/downloads/download_metadata_heal_service.py` — `release_title` in
  `HEAL_COLUMNS`.
* `tests/test_download_import_edition_and_genres.py` — new.
* `tests/test_album_metadata_release_scoping.py` — fixtures completed.
