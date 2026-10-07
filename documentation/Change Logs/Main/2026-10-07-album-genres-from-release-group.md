# Album genres come from the release group and the release itself

**Date:** 2026-10-07
**Area:** metadata / enrichment (MusicBrainz album genre proposal)
**Commit:** `fix(metadata): album genres come from the release group and release`

## Report

> MusicBrainz has both Genres and other tags. Are all of these collected?
> Do the artist tags, Release Group Tags, Release Tags and Track tags all
> get populated and used when doing the genre detection? The album genres
> should be pulled from the release group and the specific release.

## Audit

Probed against the live MusicBrainz API (Nevermind, `inc=…+genres`):

| MB entity | genres fetched? | raw tags | used in genre detection |
|---|---|---|---|
| **Release group** | ✅ cascaded into the release lookup **and** fetched per-track in `track_stage` (`inc=genres+tags`) | fetched, deliberately unused | ✅ per-track `musicbrainz_genres`; ❌ **album proposal dropped them** |
| **Release** | ✅ in `_RELEASE_INC_SUPERSET` | not fetched | ❌ **fetched but never surfaced** (dropped in `_flatten_release`) |
| **Recording/track** | ✅ recording superset + lookup search | fetched, unused | ✅ |
| **Artist** | ❌ never fetched from MusicBrainz | — | artist genres come from Last.fm top tags, TheAudioDB and Wikidata |

The raw "other tags" in the report (`english`, `offizielle charts`,
`1–4 wochen`) are **not genres** — MusicBrainz's curated `genres` list is
the genre subset derived from them, and that curated list is what the
pipeline consumes. The probe confirmed the RG endpoint returns both, and
the curated set is the meaningful one.

Only the **recordings'** genres reached the album proposal
(`_album_genres` unioned `musicbrainz_genres` per track), so an album
whose recordings are untagged proposed nothing even when the release
group is fully tagged — exactly the reported gap.

## Fix

- `services/enrichment/musicbrainz_service.py` — `_flatten_release`
  surfaces `release_group_genres` (from the embedded release-group, which
  MusicBrainz populates under `inc=genres` — zero extra requests) and
  `release_genres` (the release's own), via the new `_join_genre_names`
  helper (comma-joined, case-insensitive dedupe, first-seen order — the
  same contract as the per-track `musicbrainz_genres` string).
- `services/metadata/metadata_proposal_service.py` — `_album_genres` now
  takes the whole metadata and layers: **release-group genres → release
  genres → union of the recordings' genres**, so the album's review
  proposal (the genre chips behind "Include") is led by the album-level
  MusicBrainz genres as reported.

## Tests

`tests/test_album_genres_from_release_group.py` (10): flatten surfaces
both layers (and empty strings when absent), the join helper dedupes,
precedence group → release → recordings with legacy recordings-only
fallback, the proposal proposes group genres even when every recording
is untagged, and an identical genre set in another order is not
re-proposed.

**Gate:** targeted 186/186 across proposal/flatten/save tests · oracle
(8 of 10 fail on revert; the 2 passing are both-direction guards) ·
affected set (17 files): 4 failed / 418 passed, all 4 pre-existing,
0 NEW · full suite 223 failed / 4954 passed → **0 NEW** (only the known
flaky `test_sibling_torrents_root_is_searched`), 81 fixed vs baseline.
