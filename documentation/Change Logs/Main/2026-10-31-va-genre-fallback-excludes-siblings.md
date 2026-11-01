# A VA track's artist genre fallback no longer reads sibling tracks

**Date:** 2026-10-31
**Area:** `enrichment` (genre aggregation)
**Files:** `services/enrichment/genre_aggregation_service.py`,
`tests/test_va_genre_no_sibling_bleed.py` (new)

## Reported symptom

> On these compilations the genres are being matched correctly for the tracks
> and using genres that are matched to other tracks on these compilations,
> while the popularity data for the tracks are matching correctly.

## What was happening

`sync_various_artists_track_genres` gives each VA track the genres ITS OWN
source columns rate. When a track has no own sources it calls
`fallback_genres_for_artist`, which reads the PERFORMER's genres already in the
database — excluding only the current track id:

```sql
WHERE LOWER(TRIM(artist)) = LOWER(TRIM(:artist))
  AND CAST(id AS TEXT) <> :exclude_id
```

On a compilation several rows can share the same `artist` value (the same
performer appears twice, or the folder's credit was imported for every track).
Those sibling tracks were then legitimate evidence, so a sparse track was given
the genres of its neighbours instead of its own sources or its performer's
catalogue elsewhere. Popularity was unaffected because its data is per track.

## Fix

* **The release group is not a per-track genre source on a compilation.**
  The scan wrote `musicbrainz_genres` from the row's `musicbrainz_releasegroupid`
  FIRST and only fell back to the recording. On a VA album that release group is
  shared by every track, so each track was painted with the album's genres. For
  `album_context.is_va_compilation`, the RECORDING's genres are now preferred
  and the release group is the fallback. Single-artist albums keep the old
  release-group-first order.
* `_artist_genres_from_library` gained `exclude_album`: the album being synced
  is excluded alongside the current track id.
* `fallback_genres_for_artist` forwards `exclude_album`, and its cache key now
  includes the excluded album (the answer changes per album).
* `sync_various_artists_track_genres` passes `exclude_album=album`.

The fallback still uses the artist's OTHER albums, and falls back to
MusicBrainz/Discogs/Last.fm when the artist has no local genres elsewhere.

## Verification

`tests/test_va_genre_no_sibling_bleed.py`:

| Test | Pins |
|---|---|
| `test_sibling_tracks_on_the_same_album_are_not_evidence` | ⭐ a compilation sibling's genres are not read |
| `test_without_the_exclusion_the_old_behaviour_is_visible` | CONTROL: the filter is what changes the result |
| `test_the_sync_excludes_its_own_album` | the VA sync passes the album through |
| `test_the_scan_branches_on_the_va_flag` | ⭐ a VA track prefers its recording's genres over the album release group |
