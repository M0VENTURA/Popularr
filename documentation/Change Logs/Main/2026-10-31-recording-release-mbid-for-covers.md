# Track identity is the MusicBrainz Recording ID, not a release ID

**Date:** 2026-10-31
**Area:** `enrichment` / cover detection / popularity scan
**Files:** `services/enrichment/cover_detector_impl.py`,
`services/enrichment/musicbrainz_service.py`,
`tests/test_recording_release_mbid.py`,
`tests/test_cover_work_original_artist_and_mbid_validation.py` (new)

## Question

> Can you confirm that the track release MBID is fetched during the metadata
> lookup for each track? … Then that MBID can be used for cover detection rather
> than a simple artist and track lookup, as it's creating some false positives.

## Answer

**Fetched: yes. Persisted: no — and it should not be.**

`lookup_recording_metadata` requests the recording with
`inc="…releases+release-groups…"` and `_recording_to_metadata` selects one
release for the release title/year. The **identity** the app stores (and the
track page's **MusicBrainz Recording ID**) is the recording MBID; a per-track
release MBID is not needed and would fight the album-level
`musicbrainz_album_mbid`.

An interim revision added `tracks.recording_release_mbid`; it was redundant.
Migration `032_drop_tracks_recording_release_mbid` removes it (031 stays in the
chain for databases that already ran it). Nothing writes a release MBID for the
track any more.

## The real defect behind the "Swerve" false positive

Papa Roach's **"Swerve"** carried a *wrong* stored recording MBID:
`3bcdd7e1-0d28-440e-a6fb-fd1b13be4d3e` is Rise Against's "Historia calamitatum".
Cover detection trusted a stored MBID unconditionally, followed that recording's
work, and then the leftover writer/title heuristics matched the shared title of
Dub Sex's 1988 song "Swerve" — so the track was attributed to Dub Sex.

`_resolve_recording_mbid` now **validates** a stored recording MBID against the
track's title and first artist credit (`_recording_matches_track`). A successful
fetch that disagrees is ignored and the id is resolved again from the release
tracklist/title search. A fetch failure still trusts the stored id.

## Work-based cover detection

The deep pass now treats the recording's **work relationship** as the evidence:

* no explicit cover annotation but a work relation → the work's recordings are
  the candidate set (`_detect_via_work_id`);
* **no work relation at all → conclusive negative**, settled before the writer
  and title heuristics can run. A same-titled song by a different artist is not
  a cover.

This is why the cover detector no longer needs a release MBID: the recording
MBID leads to the work, and the work names the composition.

## Verification

`tests/test_recording_release_mbid.py` — the lookup returns the release ids;
cover detection resolves from a known release when it has one.

`tests/test_cover_work_original_artist_and_mbid_validation.py` — ⭐ the Swerve
validation and the no-work negative.
