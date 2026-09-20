# Album Title vs Release Name, and version-aware recording matching

**Date:** 2026-09-20
**Area:** `metadata`, `single`, `popularity`, `config`
**Status:** fixed

## Reports

> "Release name and album title matching isn't working correctly. Green Day
> American Idiot has the Album Title as **American Idiot (holiday edition deluxe)**
> and the Release Name as **Punk**. But Release Name should be
> *American Idiot (holiday edition deluxe)* and album title should be *American Idiot*."

> "The album was **Helden X Hymnen by DArtagnan**. It had the same number of tracks,
> the tracks were similar length with similar names, but some of the tracks were
> missing 'unplugged version' so it didn't match the album correctly during the
> metadata scan, then due to that incorrectly used the wrong version of the tracks
> when doing the popularity scoring."

Three independent defects, one per symptom. All three were reproduced with a
probe before any change was made.

## Defect 1 — the album name was never cleaned

Every alternative in `_ALBUM_EDITION_STRIP_RE` (`helpers/normalization_service.py`)
pins the WHOLE parenthetical, so a marker that puts words **before** the keyword
never matched. Measured:

| Input | Before | After |
|---|---|---|
| `American Idiot (Holiday Edition Deluxe)` | unchanged | `American Idiot` |
| `American Idiot (Holiday Edition)` | unchanged | `American Idiot` |
| `Some Album (20th Anniversary Deluxe Edition)` | unchanged | `Some Album` |
| `Some Album (Live)` | preserved | preserved |
| `Some Album (Acoustic Version)` | preserved | preserved |
| `Some Album (Unplugged Version)` | preserved | preserved |

This explained the Album Title. Because the same function builds the lookup
keys used by the Navidrome album diff, the edition also leaked into every
name-based comparison.

**Fix:** a new alternative matches when the parenthetical *contains* an
unambiguous edition/pressing word, in any order. The leading negative lookahead
is the load-bearing part: markers naming a **form** (`live`, `remix`, `acoustic`,
`unplugged`, `instrumental`, `demo`, `karaoke`) are still preserved, because they
distinguish different *recordings* rather than different *pressings*, and a bare
`Version` is deliberately not a keyword so `(Acoustic Version)` and
`(Boogie Version)` survive.

## Defect 2 — Release Name was an arbitrary edition

`release_title` comes from `_select_primary_release()`'s chosen release, and
that function matched on **release-GROUP identity** — identical for every
edition of an album — then tie-broke on the **earliest date**. Measured:

| Anchor (the library album name) | `specific_title` before | after |
|---|---|---|
| `American Idiot (Holiday Edition Deluxe)` | `American Idiot` (the 2004 pressing) | `American Idiot (Holiday Edition Deluxe)` |
| `Punk` compilation in the release list | `Punk` | not selected at all |

So `release_title` was the *original* release rather than the edition in the
collection, and when the anchoring failed entirely it fell through to the
earliest release of whatever release list the recording carried — **which is how
the unrelated compilation titled "Punk" became this album's Release Name.**

**Fix — two parts:**

* **Stage 0**: a release whose OWN title names the same edition as the local
  album name is chosen first. This is the only way to recover *which* edition is
  held, because the release group cannot distinguish editions. Deliberately
  strict — the album must carry an edition annotation, the two titles must be
  edition-annotation **compatible** (so `(Deluxe Edition)` never matches
  `(Holiday Edition Deluxe)`), and they must be ≥0.9 similar.
* **Anchor gate**: when an album name *was* supplied and nothing in the release
  list resembles it, **no release is nominated**. An arbitrary release here is
  what wrote "Punk" into `release_title`. No release is better than a wrong one —
  the caller keeps the library's own album name.

`album_artist` from this path is not consumed by any caller, so returning
nothing is safe.

## Defect 3 — a plainly tagged track resolved to the studio recording

This is the "Helden X Hymnen" case, and it is the same *class* as the earlier
live-album fix — but for every other version marker.

`edition_annotations_compatible` drives the candidate filter in
`get_suggested_mbid`. A release routinely marks only SOME of its titles:

```
edition_annotations_compatible("Song", "Song (Unplugged Version)")  -> False
edition_annotations_compatible("Song", "Song")                      -> True
```

so for the titles tagged plainly as `Song`, the search **rejected** the unplugged
candidate and took the identically titled studio one. The track then carried the
studio recording's MBID, and every popularity figure read through it —
ListenBrainz listens by MBID, Last.fm via the release-scoped match — was the
studio version's.

**Fix — three parts:**

* New `helpers.normalization_service.album_version_annotation()` derives the
  album's own annotation: from the album NAME first, else by **majority** across
  its track titles (minimum two, so one stray marker cannot redefine the album).
* `get_suggested_mbid(..., edition_annotation=…)` uses it **only when the track's
  own title carries none**, and uses it to *recognise* rather than reject: a
  candidate carrying it is admitted even though
  `edition_annotations_compatible` answers "no", and is ranked above the plain
  candidate. Candidates that do not carry it stay eligible, so a track still
  resolves (to the plain recording) rather than resolving to nothing when
  MusicBrainz holds no such version.
* `_cache_key` includes the annotation. A studio and an unplugged cut share
  title+artist **and** liveness, so without it whichever was scanned first
  answered for the other — exactly the defect the `::live` suffix fixed.

`track_stage` computes the annotation once per track and passes it to BOTH
recording lookups (the MB metadata pass and the ListenBrainz MBID fallback).

**Live albums are unaffected**: `(Live)` is deliberately not an edition
annotation, so `edition_annotation` is `None` for a live album and the existing
`_recording_live_affinity` ranking still decides.

## Verification

| Check | Result |
|---|---|
| New `tests/test_album_title_and_version_matching.py` (38) on the **unpatched** tree | **15 failed / 23 passed** — each naming a real defect: the six `(Holiday Edition Deluxe)`-class strips, `'American Idiot' == 'American Idiot (Holiday Edition Deluxe)'`, the `Punk` compilation being selected, the missing `album_version_annotation`, `a plainly tagged track on an unplugged album must resolve to the unplugged recording`, and `_cache_key() got an unexpected keyword argument 'annotation'` |
| Same suite, patched | **38 passed** |
| 11 related suites (album naming, MB matching, punctuation lookup, live-album resolution, album identity, release-group resolution, release picker, title-track boost, **MB batch version resolution**, album MB tag files, edition single matching) | **24 failed / 80 passed on BOTH trees** — byte-identical failure lists, **0 new** |

The 24 are pre-existing environment failures (they fail identically on a clean
`origin/develop`, including all 11 in `test_mb_batch_version_resolution.py`).

## Files

* `helpers/normalization_service.py` — new edition-marker alternative in
  `_ALBUM_EDITION_STRIP_RE`; new `album_version_annotation()`
* `services/enrichment/musicbrainz_service.py` — `_edition_title_matches()`;
  stage 0 + anchor gate in `_select_primary_release()`; `edition_annotation`
  through `get_suggested_mbid()` / `lookup_recording_metadata()` and both
  module-level wrappers; annotation in `_cache_key()`; annotation rank in the
  candidate ordering
* `services/popularity/stages/track_stage.py` — `_album_edition_annotation()`;
  `edition_annotation` threaded into `_resolve_track_mb_metadata()`'s lookup and
  the ListenBrainz MBID fallback
* `tests/test_album_title_and_version_matching.py` — new
