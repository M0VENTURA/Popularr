# Live albums rated from the studio recordings

**Date:** 2026-09-19
**Area:** `popularity` / `musicbrainz`

## Reported symptom

> Live albums seem to be getting high ratings in the popularity scan
>
> They seem to be getting ratings based on the original versions of the song,
> not the live versions when matching

A live album's tracks were being scored with the **studio** recordings'
popularity data, so a live album read like the studio album it was drawn from.

## Root cause

Popularity is read from Last.fm and ListenBrainz for whichever **MusicBrainz
recording** the track resolves to. That resolution happens in
`MusicBrainzService.get_suggested_mbid`, and for a live album it picks the wrong
recording. Two properties of live albums combine to cause it:

**1. Live releases routinely ship plainly titled tracks.** Every track on
Metallica's "S&M" is titled exactly as its studio original. So for a track
called "Enter Sandman":

```
edition_annotations_compatible("Enter Sandman", "Enter Sandman")  -> True   (studio)
edition_annotations_compatible("Enter Sandman", "Enter Sandman")  -> True   (live)

_mbid_similarity("enter sandman", "enter sandman")                -> 1.0000 (studio)
_mbid_similarity("enter sandman", "enter sandman")                -> 1.0000 (live)
```

Both candidates score **exactly 1.0**. Nothing in the ranking could separate
them, so MusicBrainz's relevance ordering decided it — and that puts the famous
studio recording first. Measured against the real code:

```
candidates=studio_first -> mbid=11111111... score=1.000  STUDIO   <- wrong
```

The track was then stamped with the studio recording's MBID, and every
popularity figure read from that MBID — the ListenBrainz listen count, and the
release-scoped Last.fm match — was the studio recording's.

**2. `inc` was not requested.** The search call asked for no release data at
all, so even a correct ranking rule would have had nothing to judge on:

```python
self.http.search_recordings(
    query,
    limit=limit,
    # no inc= -> no "releases" -> no release-group -> no way to tell live from studio
)
```

**3. A third, independent path.** The MBID cache was keyed on
`(artist, title)` alone. A plainly titled live track and its studio namesake
therefore produced the *same* key (`metallica::enter sandman`), so whichever was
scanned first resolved the other one too.

Note the surrounding code was already aware of this hazard: the Last.fm
aggregation is explicitly `is_live_release`-aware and skips its title+artist
fallback on live releases, with a comment explaining that the fallback "CANNOT
distinguish a live performance from its studio namesake". That guard was
correct — but it only protects the *count* once the liveness flag is right. The
recording had already been resolved to the studio MBID upstream, so the
ListenBrainz path and the album-identity path still inherited studio data.

## Fixes

### 1. Rank candidates by liveness, then similarity — `musicbrainz_service.py`

New `_recording_live_affinity(recording)` classifies a search candidate from its
release groups' secondary types. It returns `True`, `False`, or **`None` when
the response carries no release data** — a real outcome that must never be
treated as "verified studio".

`get_suggested_mbid` now ranks on `(liveness_agreement, similarity)`:

| liveness agreement | rank |
|---|---|
| candidate's type matches the release being scanned | 2 |
| candidate cannot be classified | 1 |
| candidate's type contradicts it | 0 |

so liveness decides first and text similarity breaks ties. Unclassifiable
candidates sit in the middle, which means a live lookup is never satisfied by an
unclassifiable hit and a studio lookup is never blocked by one either — the
pre-existing behaviour is preserved when the data is absent.

The search now passes `inc="releases+release-groups"` so the decision is
possible at all.

### 2. Cache isolation

`_cache_key(title, artist, is_live=False)` — the live lookup no longer shares an
entry with its studio twin:

```
studio key: 'metallica::enter sandman'
live   key: 'metallica::enter sandman::live'
```

### 3. Thread the release liveness through

- `lookup_recording_metadata(..., is_live_release=False)` accepts and forwards
  the flag to the search. Both the service method and the module-level wrapper.
- `track_stage._resolve_track_mb_metadata` computes it from
  `_album_type_indicates_live(track, album_context, album_result)` or the
  track's own alternate-performance title, and passes it in.
- The ListenBrainz MBID fallback resolves with the same flag, so the listen
  count is read from the live recording.

### A bug the tests caught in my own fix

My first version of `_recording_live_affinity` tested
`if "live" in _release_group_secondary_types_of(release)`. `_parse_secondary_types`
returns MusicBrainz's **raw casing** (`"Live"`), so that membership test never
matched and the fix silently did nothing — the probe failed with
`live lookup picked STUDIO`. Each element is now casefolded. The guard test
exists precisely because this class of failure is invisible.

## Verified

| Check | Result |
|---|---|
| Real-code probe, pre-fix | live track → **STUDIO** MBID (reproduces the report) |
| Real-code probe, post-fix | live track → **LIVE** MBID, both candidate orderings |
| Studio track, post-fix | still → **STUDIO** MBID (no over-correction) |
| New suite, pre-fix | **9 failed**, 3 passed |
| New suite, post-fix | **12 passed** |
| Full related file set, baseline | 35 failed, 107 passed |
| Full related file set, with fix | **26 failed, 116 passed** |
| New failures introduced | **0** (35 − 9 fixed = 26) |

## New guard tests

`tests/test_live_album_recording_resolution.py` (12 tests)

1. A plainly titled live track resolves to the LIVE recording — parametrised over
   **both** MusicBrainz candidate orderings, since relevance order was what
   silently decided the bug.
2. A studio track still resolves to the studio recording.
3. Unclassifiable candidates do not masquerade as verified studio.
4. **The search requests release data.** Guards the subtlest failure mode: with
   correct ranking but no `inc`, every candidate is unclassifiable and the fix
   does nothing at all.
5. The cache key separates live from studio, and the two lookups do not
   cross-poison the cache end-to-end.
6. `lookup_recording_metadata` and the module-level wrapper expose and forward
   the flag.
7. `track_stage` passes the release liveness into both the metadata lookup and
   the ListenBrainz fallback (static source check — without this the fix is
   inert in production).

## Test doubles updated

Four existing tests stubbed `get_suggested_mbid` with a lambda hard-coding the
old signature, so extending the interface broke them even though production
behaviour for their inputs is unchanged. Their doubles now take `**kwargs`:

- `tests/test_album_identity_release_selection.py` (1 lambda)
- `tests/test_single_detection_title_track.py` (2 lambdas)
- `tests/test_live_alternate_title_and_mbid.py` (2 fake methods)

## Pre-existing failures (not caused by this change)

21 failures across `test_mb_batch_version_resolution.py`,
`test_single_detection_title_track.py`, `test_live_4star_requires_single.py` and
`test_metadata_update_album_split.py` are identical before and after. Most are
test-fixture gaps (`'_FakeHTTP' object has no attribute 'search_release_groups'`,
`'get_recordings_bulk'`) and would need fixture work, not production changes.
