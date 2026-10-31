# Cover detection's VA search gate is per row, not per album

**Date:** 2026-10-31
**Area:** `enrichment` (cover detection)
**Files:** `services/enrichment/cover_detector_impl.py`,
`tests/test_compilation_cover_uses_track_artist.py`

## Reported symptom

> The ones that are working have successfully matched the recording mbid, but
> the others haven't.

On a Various-Artists compilation, some rows resolved a recording MBID and were
detected; the rest were never searched. Investigation showed the fallback was
decided once for the WHOLE album by `_va_compilation_without_release`, which
returned `False` as soon as **any** row carried
`musicbrainz_album_mbid` / `musicbrainz_albumid` / `musicbrainz_releasegroupid`.

A folder assembled from several downloads routinely mixes files: the importer
tags a release MBID onto the files that carried one, and leaves the others with
none. One tagged row then silenced the artist+title search for every unresolved
row on the album — exactly the "some match, the others don't" report.

## Fix

`_va_compilation_without_release(album_artist, tracks)` is replaced by
`_is_va_compilation(album_artist)`, and both identity-gated steps now decide per
**row**:

* step 3 (recording relations) — a row with no logged identity is searched by
  its own ARTIST + TITLE when the album is a VA compilation, regardless of what
  its siblings carry;
* step 6 (work fallback) — the same rule for an already-flagged cover row.

The duplicate-network-work guard is unchanged for every other album: a track
with no logged identity on a single-artist release is still skipped.

## Verification

`tests/test_compilation_cover_uses_track_artist.py`:

| Test | Pins |
|---|---|
| `test_a_va_album_is_recognised` | the album-level predicate |
| `test_a_non_va_album_is_not_a_compilation` | single-artist albums keep the skip |
| `test_one_resolved_sibling_does_not_silence_an_unresolved_row` | ⭐ a sibling with a release MBID no longer suppresses the unresolved row's search |
