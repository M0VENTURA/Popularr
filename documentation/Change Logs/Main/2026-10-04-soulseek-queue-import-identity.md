# Soulseek queue imports keep their year and track number (2026-10-04)

**Report:** *"Tracks imported from the soulseek queue are importing as unknown
for year. `/music/Stray Kids/Unknown - SKZ‐REPLAY 2026 Pt.1/00. Stray Kids -
LOVER.mp3` — It's missing the track number and the year of release. Are all of
these tags being pulled from Musicbrainz to the tracks in the download queue
and applied before transfer?"*

## Root cause — two compounding defects

1. **`queue_add` dropped the identity payload.** The single-item
   `/api/queue/add` endpoint forwarded only `artist/title/album/source/
   priority` to `insert_queue_item`. The album page's missing-track button
   (`queueMissingTrack`), the dashboard upcoming-release button and the
   search form also send `year`, `track_number`, `disc_number`,
   `album_artist`, `release_mbid`, `recording_mbid`, `release_id`,
   `duration`, `import_group`, `import_type` — **all silently discarded**
   (`queue_add_batch` always forwarded them; the single-item path never did).
   The row was therefore created with `year=NULL` and `track_number=NULL`,
   and at import `_extract_year_for_path(None)` → `"Unknown - <album>"`
   folder and `_format_track_number_for_rename(None)` → `00.` file prefix.
   With `release_mbid` gone too, `_resolve_album_level_metadata` had
   nothing to refresh the release identity from.

2. **`update_file_metadata` turned "unknown" into a delete.** It
   unconditionally rebuilt `tag_updates` with
   `"year": metadata.get("release_year") or metadata.get("year")` (and the
   same for `track_number`, `disc_number`, `title`, …). When the queue row
   lacked a value the key arrived as `None` — and both writers treat `None`
   as "clear this frame" (`_set_text_frame` → `delall`, FLAC `del`). So an
   incomplete queue row actively **wiped the downloaded file's own
   TDRC/TRCK frames** instead of leaving them alone: the reported file lost
   its date *and* its track number even though the source file carried them.
   Note `sync_track_tags_to_file` already documented this exact trap
   ("passing None/"" for a frame would make the writer DELETE that frame")
   — `update_file_metadata` just never applied the rule.

## Changes

* `services/queue/queue_processing_service.py` —
  `queue_add` now forwards the **full identity payload**
  (`track_number`, `disc_number`, `album_artist`, `year`, `release_id`,
  `release_mbid`, `recording_mbid`, `duration`, `import_group`,
  `import_type`); empty-string dataset values (`String(t.track_number || '')`)
  normalize to `NULL` instead of storing an unparseable `""`.
* `services/metadata/tag_file_service.py` —
  `update_file_metadata` only forwards base fields the caller actually
  supplied (`value is not None`); an explicit `""` still clears a frame (the
  single-disc `disc_number=""` clear keeps working). "Not known" no longer
  deletes what the downloaded file already had.
* `services/downloads/download_completion_service.py` —
  new `_resolve_missing_identity(item, file_path)` heals rows queued **before**
  the fix, at import time, from the cheapest source that has the data:
  the row's own `release_year`/`release_date` columns → the album metadata
  JSON persisted at queue time (`releasedate`/`originalyear`) → a
  MusicBrainz release refresh keyed by the row's release MBID (recording-MBID
  then normalized-title tracklist match; never guesses a track number) → the
  downloaded file's own tags and filename. `_move_and_import` applies the
  fills **before** the tag write and the path build, and persists them back to
  the queue row so `_reconcile_stale_moving` rebuilds the same target path.
  A backfill failure logs a warning and the import proceeds (best-effort).
* `helpers/metadata_reader.py` —
  the reader now exposes `year` (FLAC `DATE`, MP3 `TDRC`/`TYER`), which it
  never did before — so `_extract_discovered_metadata`'s year path (dead
  until now) and the new backfill can fall back to the source file's tags.

## Tests

`tests/test_queue_import_identity.py` (23 tests): queue_add forwards every
identity field + normalizes `""`; `update_file_metadata` never forwards
absent/`None` fields but still clears on `""` and still prefers
`release_year` for the DATE tag; `_resolve_missing_identity` per source
tier (columns, stored JSON, MB recording/title match, file tags, filename
prefix, never-overwrite, never-guess); `_move_and_import` end-to-end proving
the fills reach the tag write, the `move_track_to_library` path **and** the
persisted queue row (folder resolves to `2026 - …/03. …`, not
`Unknown - …/00. …`); backfill failure does not abort the import; the
reader exposes `year` on both formats.

## Answer to "are all these tags pulled from MusicBrainz and applied?"

Album-level tags (barcode, media, recordlabel, releasecountry,
releasetype/status, originalyear, releasedate, the MusicBrainz IDs) **are**
— stored on the row at queue time and refreshed from MB at import when the
row carries a `release_mbid`. Track-level identity (year, track number) was
only as good as what the queue row carried — which the single-item endpoint
was dropping (defect 1) — and could then be actively deleted (defect 2).
Both halves are fixed above; rows queued before the fix are healed at import
by `_resolve_missing_identity`.
