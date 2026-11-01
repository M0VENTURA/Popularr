# The recording lookup now returns and saves the track's release MBID

**Date:** 2026-10-31
**Area:** `enrichment` / `popularity` (metadata scan) / cover detection
**Files:** `services/enrichment/musicbrainz_service.py`,
`services/popularity/stages/track_stage.py`,
`services/enrichment/cover_detector_impl.py`,
`services/enrichment/cover_detection_service.py`,
`db/models.py`, `db/schema.py`,
`migrations/versions/031_tracks_recording_release_mbid.py`,
`tests/test_recording_release_mbid.py` (new),
`tests/test_va_individual_mbid_lookup.py`

## Question

> Can you confirm that the track release MBID is fetched during the metadata
> lookup for each track? Or saved during the popularity matching? Then that MBID
> can be used for cover detection rather than a simple artist and track lookup,
> as it's creating some false positives.

## Answer

**Fetched: yes. Saved before this change: no.**

`lookup_recording_metadata` requests the recording with
`inc="…releases+release-groups…"`, and `_recording_to_metadata` already SELECTED
one of the recording's releases (`_select_primary_release`) for the release
title/year. It returned `release_title`/`release_year` but **discarded the
release's MBID** (and the release-group MBID). Popularity matching never saved
it either.

## Fix

* `_recording_to_metadata` now returns the selected release's
  `release_mbid` and `release_group_mbid`.
* New per-track column **`tracks.recording_release_mbid`** (model, runtime
  schema bootstrap, migration 031). `track_stage` persists it whenever the
  per-track lookup resolved one. It is deliberately separate from the album's
  `musicbrainz_album_mbid`: a VA compilation's tracks can each come from a
  different release, and writing them into the album column would make the
  album identity inconsistent.
* Cover detection:
  * loads `recording_release_mbid` (`cover_detection_service._ARTIST_TRACK_COLUMNS`);
  * counts it as a logged identity;
  * `_resolve_recording_mbid` prefers it over the album release and over a
    default album release resolved by name;
  * step 3 passes it to `_detect_via_recording_relation`
    (`_check_release_for_cover`).
  So a VA compilation with no album release now resolves each recording from
  its own release's tracklist instead of a broad artist+title search.

The file's `MUSICBRAINZ_ALBUMID` tag is NOT rewritten with the per-track
release: keeping the album-level tag avoids splitting a compilation into
per-release albums in Navidrome. The value is a DB-side resolution aid.

## Verification

`tests/test_recording_release_mbid.py`:

| Test | Pins |
|---|---|
| `test_the_selected_release_mbid_and_group_are_returned` | the lookup returns both ids |
| `test_a_recording_without_releases_returns_no_release_id` | no data → no id |
| `test_the_per_track_release_is_resolved_before_the_album_release` | ⭐ the row's own release wins and resolves the recording |
| `test_a_per_track_release_counts_as_a_logged_identity` | the step-3 gate accepts it |

`tests/test_va_individual_mbid_lookup.py` asserts the scan persists
`recording_release_mbid` alongside `recording_mbid`.
