# A MusicBrainz Single is no longer promoted to an EP by the local track count

**Date:** 2026-10-12
**Area:** scan / metadata / album-type
**Commit:** `fix(album-type): a MusicBrainz Single is no longer re-bucketed by the local track count`

## Reported

> During the metadata lookup, this stayed as EP, but it was correctly
> matched to a Single with the release group and release ID.

## Root cause

`_resolve_album_type` re-bucketed MusicBrainz's release-group
`primary_type` by the **local** track count:

```python
if mb_type == "single" and track_count > 6:
    mb_type = "album"
elif mb_type == "single" and track_count > 3:
    mb_type = "ep"
```

So a release MusicBrainz correctly matched to a **Single** (release-group
and release id present) became an **EP** whenever the local folder held 4-6
tracks — exactly the reported symptom.

This is the same defect class as the 2026-10-09 EP fix (`fa34e0b7`), which
removed the `ep` half of the rule but **deliberately kept the `single`
half** on the assumption that "a single with an implausible count is
genuinely ambiguous". No bug report ever justified that assumption, and it
is wrong for the same two reasons the EP half was:

- `track_count` is the **local folder** — bonus tracks, live bonus cuts, a
  flattened multi-disc set — **not** MusicBrainz's release tracklist; and
- a 4-6 track folder is exactly what a single *and* an EP can look like
  locally, so the count cannot separate them and merely substitutes a guess
  for MusicBrainz's answer.

## Fix

MusicBrainz's release-group `primary_type` is now authoritative for **both**
`single` and `ep` — the local track count never re-buckets it. The only
count-independent safety net that remains is `_mb_type_is_corroborated`,
which downgrades a *rich* type (`album+live`, `album+soundtrack`, …) that the
local title/track titles do not back up; it has never applied to a bare
`single`/`ep`, which is why MusicBrainz's answer for those two now stands.

The now-unused `adjusted by track count` diagnostic and the
`track_count` local are gone.

## Tests

`tests/test_ep_survives_track_count.py` (24):

- **New** `TestTheSingleHeuristicIsGoneToo`: a MusicBrainz single stays a
  single at every local track count (1, 2, 3, 4, 6, 7, 10, 20), including
  the reported 4-6 "stayed as EP" case (both the resolves-as and the
  persisted/presented type).
- **New** guard `test_no_track_count_rebucketing_remains`: a source probe
  (with comments stripped via `tokenize`) that fails if the count rule or
  its `adjusted by track count` log returns.
- The EP tests and the plain-album control are unchanged.

**Oracle** at `5422fbe1`: 8 of 24 fail (the single re-bucketing cases —
4/6 → EP, 7/10/20 → album); the EP tests, the ≤3 single cases and the album
control pass. **Sweep**: 10 album-type/live/compilation suites → 6 failed,
failing set **identical** to baseline (all pre-existing:
`test_album_type_persistence::test_detects_when_missing`,
`test_live_alternate_title_and_mbid` ×4,
`test_release_categories::test_no_type_falls_back_to_title`) → **0
regressions**.

## Note

A release-group MB calls a "single" is a single; a genuinely mis-typed MB
release group is a MusicBrainz data problem, not something a local folder
count can be trusted to correct. The `mb_type_raw` return value still
carries MusicBrainz's own classification for callers that need it.
