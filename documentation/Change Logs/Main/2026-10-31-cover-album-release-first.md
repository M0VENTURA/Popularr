# Cover detection resolves the album's release before searching each track

**Date:** 2026-10-31
**Area:** `enrichment` (cover detection)
**Files:** `services/enrichment/cover_detector_impl.py`,
`tests/test_cover_album_release_resolution.py` (new)

## Reported symptom

> Covers are still not being found. Should cover detection check for a release
> MBID if it doesn't exist for the track it's checking first?

## Why a release lookup first is the right move

`_resolve_recording_mbid` resolves a recording in two ways:

1. from the **release tracklist**, when the row has `musicbrainz_album_mbid`;
2. otherwise from a **bare title search**.

Rows the metadata pass never resolved have no recording/work identity. If the
album rows also carry no release MBID, they had only route 2 — and on a
**non-Various-Artists** album step 3's identity gate skipped them entirely.
The release tracklist is the more reliable route (title + artist matched
against a known tracklist), and it was simply unavailable.

## Fix

* New `_resolve_album_release_mbid(album, artist, tracks)` — resolved **once per
  album per pass**, cached:
  * any row's stored `musicbrainz_album_mbid`/`musicbrainz_albumid` wins, with
    no network call;
  * otherwise a MusicBrainz `artist:"…" AND release:"…"` search, accepting only
    a release whose title matches the album (edition markers are ignored by the
    canonical comparison);
  * a Various-Artists album is skipped: its "release" is the folder, so only the
    per-track ARTIST + TITLE route applies.
* `_resolve_recording_mbid` takes `default_release_mbid` and uses it when the row
  has no release id of its own, so the release tracklist is tried first.
* Step 3's identity gate now admits a row with no identity when the album's
  release was resolved, not only on a VA compilation. Step 6 follows the same
  rule. Rows that still have no route are skipped as before, so the duplicate
  search guard is preserved.
* The local `_titles_match` closure became a module-level helper so both the
  release resolver and the recording matcher use the same comparison.

## Verification

`tests/test_cover_album_release_resolution.py`:

| Test | Pins |
|---|---|
| `test_a_stored_row_mbid_wins_without_a_search` | a stored release id costs no request |
| `test_the_release_is_searched_by_artist_and_album` | the lookup query shape |
| `test_a_non_matching_release_is_rejected` | a same-artist different album is not used |
| `test_a_va_compilation_does_not_search_for_a_release` | VA keeps the per-track route |
| `test_the_default_release_is_used_when_the_row_has_none` | the tracklist resolves the recording |
| `test_the_release_unlocks_step_three` | ⭐ a non-VA album with unresolved rows is now assessed |

Stale negative verdicts are still skipped for `COVER_RECHECK_DAYS = 90`; the
album page's detection button passes `force=True`, and a forced/full scan
bypasses the window.
