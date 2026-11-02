# A track artist is registered in the artists table

**Date:** 2026-10-31
**Area:** metadata scan / artist identity
**Files:** `services/popularity/stages/track_stage.py`,
`tests/test_va_individual_mbid_lookup.py`

## Report

> During the metadata lookup, if a track artist is different from the album
> artist on Musicbrainz, it's not adding to the track artist table.

## Cause

The scan wrote the recording credit to `tracks.artist` and
`tracks.musicbrainz_artistid`, but the `artists` registry was populated **only
by the album stage, for the ALBUM artist**. A performer who exists solely as a
track credit (a VA compilation, a guest, a split release) had no `artists` row —
so no profile/bio/image/members and nothing in the registry for that name.

## Fix

`_ensure_track_artist_registered(name, mbid)` upserts the **primary** track
artist (`strip_featured_artist`, so "Artist feat. Guest" registers `Artist`) into
`artists` with the recording's MBID when it was resolved.

It is called from `_resolve_track_mb_metadata` when the primary track artist
differs from the album artist (from `album_context` or the row) and the batch
identity is trustworthy. A process-level memo prevents one DB session per track
on a large VA compilation.

The album stage still owns the album artist; this only fills the track-only
names.

## Verification

`tests/test_va_individual_mbid_lookup.py::TestTrackArtistRegistry`

| Test | Pins |
|---|---|
| `test_a_track_artist_different_from_the_album_artist_is_registered` | ⭐ VA row |
| `test_the_same_album_artist_is_not_registered` | no redundant upsert |
