# A completed metadata scan no longer re-proposes its own Genres and Writer edits

**Date:** 2026-10-31
**Area:** metadata review / popularity scan
**Files:** `services/metadata/metadata_proposal_service.py`,
`services/popularity/stages/track_stage.py`,
`tests/test_metadata_compare_version_markers_and_genres.py`,
`tests/test_va_individual_mbid_lookup.py`

## Report

> The album Ego Trip had completed a full metadata scan through the popularity
> scanning, but when I matched to another release from the same release group, I
> had these recommended edits even though these should have been adjusted during
> the popularity scan (if set to update in the config), or left as suggested if
> not set to update.

The review proposed Genres (release-group list → one recording genre) and a
Writer rewrite for tracks the scan had already processed.

## Causes

### Genres compared two different sources

The scan writes `musicbrainz_genres` from the release **group** on a
single-artist release (and from the **recording** on a VA compilation). The
proposal built its "proposed" side from the recording's genres only, so after a
completed scan it compared:

    stored:   alternative metal, alternative rock, … rock   (release group)
    proposed: hard rock                                     (recording)

— a difference on every track, for every release in the group.

`_track_proposals` now mirrors the scan exactly: the release-group genres for a
single-artist release, the recording's genres for a VA compilation (with the
other as fallback). The existing unordered-set comparison then sees no change.
As a second guard, when the matched release's **release group** is the one the
album already stores, the single-artist genre field is skipped entirely — a
different release in the same group cannot change it, and this does not depend
on the release lookup having populated the embedded group's genres.

### The release's writers were fetched and thrown away

The album release tracklist carries each recording's writers (the batch
metadata). `_resolve_track_mb_metadata` skipped its writer block entirely when
the identity came from that batch (`not _from_batch`), and even on the per-track
path it only ever **filled a missing** writer — an existing local writer was
never replaced. So the local file-tag writer survived every scan and the review
proposed the same rewrite forever.

When `metadata_update.apply_during_scan` is **on**, MusicBrainz's writers are now
authoritative — the album release's included — and replace the local value. When
it is **off**, the local writer is left untouched and the recommendation
panel/stash owns the change. Comma-separated writer strings (the per-track
lookup builds them with `", ".join`) are split into individual names instead of
being stored as one name.

### What still appears (correctly)

Matching a **different release** changes track positions, recording MBIDs and
track artists (the deluxe edition's track 19 → 18, a different recording, a
featured-artist credit). Those are genuine consequences of the release the user
chose and remain in the review.

## Verification

`tests/test_metadata_compare_version_markers_and_genres.py::TestTheGenreSourceMatchesTheScan`

| Test | Pins |
|---|---|
| `test_a_single_artist_release_compares_the_release_group` | same group → no Genres bar |
| `test_a_second_release_in_the_same_group_proposes_no_genres` | ⭐ the Ego Trip case, even without group genres in the lookup |
| `test_a_va_compilation_compares_the_recording` | VA still compares the recording |

`tests/test_va_individual_mbid_lookup.py::TestTheWriterFollowsTheApplySetting`

| Test | Pins |
|---|---|
| `test_the_batch_writer_is_applied_when_updates_are_on` | ⭐ batch writers are written |
| `test_the_local_writer_stays_when_updates_are_off` | recommend-only keeps local |
