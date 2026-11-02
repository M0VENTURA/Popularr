# Trailing/bonus missing tracks are a green label, not a correction

**Date:** 2026-10-31
**Area:** missing tracks / artist list data quality
**Files:** `services/metadata/album_missing_service.py`,
`routes/artist_routes.py`, `db/models.py`,
`migrations/versions/033_missing_album_tracks_is_bonus.py`,
`test_site/static/js/pages/artists.js`,
`test_site/static/js/pages/artist-corrections.js`,
`test_site/templates/Pages/artist_list.html`,
`templates/pages/artist_corrections.html`,
`tests/test_bonus_trailing_tracks_and_artist_labels.py` (new)

## Report

> If the missing tracks are bonus tracks for an album and appear at the end of
> the existing tracks, they shouldn't show on the artists page as an issue
> needing correcting. Instead it should have a label similar to … "Bonus Tracks
> to Download" and have it as green.

> When filtering needing fixes, it should only show missing tracks within an
> album, or artists that need corrections applied.

## Classification

`missing_album_tracks.is_bonus` (migration 033) is set by
`is_bonus_missing_track`:

* the MB track's own title contains `bonus`; or
* its numeric position is **after the highest position the library already has
  on that disc**. A gap BEFORE the last present track is a real missing album
  track.

An album with no numeric positions has no positional evidence and relies on the
title marker only. `get_missing_tracks` and `get_missing_tracks_from_db` now
return `bonus_count` and `missing_core_count` alongside `missing_count`.

## Artist list (Data Quality)

`GET /api/artists/corrections` merges a new per-artist aggregate of
`missing_album_tracks`:

* `missing_mb_tracks_count` — genuine in-album gaps (red "missing track");
* `bonus_tracks_count` — trailing/bonus cuts (GREEN "N bonus tracks to
  download").

Bonus-only artists are **returned** (so the green label can show) but
`needs_correction` stays false, so they are excluded from the count and from
"Show only artists needing fixes".

The row badge is now a container that renders **one badge per correction type**
(duplicate track, disc inconsistency, missing MusicBrainz ID, missing audio
file, missing track) plus the green bonus badge. Previously several problems
collapsed into "N issues".

## Per-artist corrections console

The per-album missing badge shows amber `N missing` for core gaps and green
`N bonus to download` for bonus cuts; each bonus row is tagged `Bonus`. The
header total counts core gaps only, with the bonus count shown separately.

## Verification

`tests/test_bonus_trailing_tracks_and_artist_labels.py`:

| Test | Pins |
|---|---|
| `test_a_track_after_the_last_library_track_is_bonus` | ⭐ trailing |
| `test_a_gap_before_the_last_track_is_not_bonus` | in-album gap |
| `test_an_explicit_bonus_title_is_bonus_without_positions` | title marker |
| `TestTheArtistMissingMbAggregate` | bonus/core split + ignored rows |
| `test_bonus_only_artist_is_not_needs_fixing` | ⭐ green only |
| `test_a_real_gap_still_needs_fixing` | red gap |
| `test_every_correction_type_has_a_label` | label set |
| `test_bonus_is_a_green_separate_badge` | ⭐ not in the filter predicate |
| `test_the_template_has_a_label_container` | template |
