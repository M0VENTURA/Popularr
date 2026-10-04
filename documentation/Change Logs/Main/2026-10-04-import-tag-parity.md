# Apply ISRC, track totals, original date and artist MBID at import (2026-10-04)

**Report:** "Tracks imported from the soulseek queue are importing as unknown for
year … It's missing the track number and the year of release. These are the raw
tags for other files on that album in Navidrome. Are all of these tags being
pulled from Musicbrainz to the tracks in the download queue and applied before
transfer?"

The year/track-number half was fixed in `bbf704e4` (see
`2026-10-04-soulseek-queue-import-identity.md`). A tag-coverage audit of the
user's raw-tag list against the import path then found FOUR fields that were
never applied on ANY path — they had no source, no storage, and no forwarding:

| Field | Tag frame | Gap |
|---|---|---|
| `isrc` | TSRC / Vorbis `isrc` | flatten never emitted it on the download path; fetch `inc` lacked `isrcs` (not implied by `recordings`) |
| `originaldate` | ORIGINALDATE | flatten emitted `original_date` but `_album_level_mb_fields` didn't map it and `_ALBUM_LEVEL_COLUMNS` lacked it |
| `tracktotal` / `disctotal` | TRACKTOTAL / DISCTOTAL | never emitted, never mapped, never forwarded |
| `musicbrainz_artistid` | MUSICBRAINZ ARTIST ID | flatten had no per-track artist MBID, so nothing could store or write it |

## Root cause

Tag parity between the album page and the download import depends on three
halves working together: **source** (the flatten emits the field), **storage**
(the queue row keeps it), and **forwarding** (`update_file_metadata` passes it
to the writer). Each of the four fields was missing at least one half, and
`_RELEASE_FETCH_INC` omitted `isrcs` so even a per-recording ISRC could never
arrive from MusicBrainz.

## Changes

- `services/enrichment/musicbrainz_service.py`
  - `_RELEASE_FETCH_INC` now includes `+isrcs`.
  - New `credit_artist_mbid()` helper; `_flatten_release` track entries now
    emit `isrc`, `tracktotal` (medium's `track-count`) and `artist_mbid`
    (recording credit first, release credit as fallback); the release payload
    now emits `disctotal` alongside `original_date`.
- `services/downloads/download_completion_service.py`
  - `_ALBUM_LEVEL_COLUMNS` gains `originaldate` and `disctotal`;
    `_album_level_mb_fields` maps them, so queue-time storage, the
    missing-column refresh trigger and the tracks-table write all pick them up.
  - `_mb_cols` (tracks-table write) gains `originaldate`, `disctotal`, `isrc`,
    `tracktotal` (`musicbrainz_artistid` was already present).
  - `_apply_stored_metadata` now reads `isrc`/`tracktotal`/`musicbrainz_artistid`
    from the row's stored `metadata` JSON, and — for rows that stored none
    (single `queue_add` rows carry no JSON, and rows queued before this change)
    — backfills them from the same HTTP-layer-cached release payload, matched
    to the track by recording-MBID/title. Stored values always win.
- `services/queue/queue_processing_service.py`
  - `add_release_tracks_to_queue_detailed` persists `isrc`, `tracktotal` and
    `artist_mbid`→`musicbrainz_artistid` on the row's `metadata` JSON.
- `services/metadata/tag_file_service.py`
  - `update_file_metadata` forwards `isrc` and `musicbrainz_artistid`
    explicitly, and `tracktotal`/`disctotal` join `_ALBUM_MB_TAG_COLUMNS` —
    the writer already supported all four frames (TSRC branch, TXXX allow-list,
    `_MB_ID_FIELDS`).
  - The None-means-suppress contract holds: absent keys are never forwarded.

## Fields deliberately NOT written by the import

- `encoding`, `soulsync_verification` — come WITH the downloaded file (soulsync
  does not exist anywhere in this repo); not MusicBrainz-derived.
- `label` — the user's odd value (`Republic Records - Stray Kids`) is a
  source-file tag; MusicBrainz labels are written as `recordlabel`.
- plain `genre` (TCON) — MB genres land in `musicbrainz_genres` (GENRE on
  FLAC / TXXX MUSICBRAINZ GENRES on MP3); plain TCON is left to the source
  file so the import never overwrites the peer's genre tags.

## Tests

- `tests/test_download_import_matches_mb_release.py` extended with 13 tests:
  `TestFlattenExposesPerTrackParityFields` (source),
  `TestAlbumLevelMappingIncludesNewColumns` (mapping),
  `TestImportAppliesPerTrackParityFields` (applied to tags + DB, stored-wins,
  offline refresh),
  `TestUpdateFileMetadataForwardsPerTrackFields` (forwarding + None contract),
  `TestQueueTimePersistsPerTrackEnrichment` (storage).
- Oracle: stashing the four source files fails 11 of the 13 (the two that pass
  are negative-contract guards); markers restored after pop.
- Full suite diffed against the clean-HEAD baseline: no new failures.
