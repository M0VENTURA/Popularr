# Live releases rejected by the corroboration guard (is_live_album=False)

## Symptom

A genuinely live album was reported by the scan as **not** live:

```
[ENRICH] local album type detected        detected_type='album+live'
[ENRICH] MusicBrainz album type result    primary_type='album' secondary_types=['live']
                                          resolved_type='album+live' match_score=1.0
[ENRICH] MusicBrainz secondary type rejected
    reason='neither the album title nor the track titles corroborate a live or
            acoustic release; refusing to retitle tracks'
    musicbrainz_type='album+live' live_track_count=0 track_count=21
[ENRICH] album type resolved              detected_type='album+live' musicbrainz_type='album'
[ENRICH] live/remix tagging evaluated     is_live_album=False
```

Album: *All My Friends, We're Glorious: Death of a Bachelor Tour Live* (Panic!
at the Disco). MusicBrainz said `album+live`, and the local detector **also**
said `album+live` — then the guard threw it away and every track was left
`is_live=False`.

## Root cause

`_album_title_suggests_live` in `services/popularity/stages/album_stage.py` —
the album-title half of `_mb_type_is_corroborated` — matched a **private copy**
of the live-album patterns, `album_stage._LIVE_ALBUM_PATTERNS`. That copy is
the *narrow* list tuned for classifying a title with no other evidence, and it
is **missing the bare trailing-` Live ` form** (`\s+live\s*$`) that the
canonical list in `services/catalog/album_classification_service.py`
(`LIVE_ALBUM_PATTERNS`) has.

The album title ends in exactly "…Tour Live", so:

1. the guard's title check found no match;
2. the track-title fallback (`_live_track_ratio`) also found nothing — all 21
   tracks are studio titles, correctly, because this is a *tour* recording that
   does not annotate `(Live)` per track;
3. the guard returned `False`, so `_resolve_album_type` downgraded
   `album+live` → `album`;
4. `_apply_live_remix_album_tagging` derives `is_live_album` from the type
   string, so it saw `album` and skipped tagging entirely.

The guard runs in **two** places (`_resolve_album_type` and
`enrich_album_extras`), which is why the downgrade was visible twice in the log
and reached the tagger.

This is a **drift bug**: two pattern lists that must agree, neither generated
from the other. Adding the missing pattern to the private copy would have fixed
the symptom while leaving the next divergence.

## Fix

`_album_title_suggests_live` now **delegates** to the canonical
`is_live_or_alternate_album` instead of matching its own list, wrapped in
`try/except` so a classification-helper failure can never reject MusicBrainz's
own answer (it falls back to the narrow local patterns).

`_LIVE_ALBUM_PATTERNS` is kept, because `_detect_album_type` still legitimately
uses it to classify a title **with no other evidence** — that is a different
question from "does the title corroborate a type MusicBrainz already gave us".

### Why `is_live_or_alternate_album` and NOT `is_live_album_enhanced`

The two canonical detectors differ, and picking the wrong one would have
introduced a new bug:

| title | `is_live_or_alternate_album` | `is_live_album_enhanced` |
|---|---|---|
| `MTV Unplugged` | ✅ | ❌ |
| `Unplugged in New York` | ✅ | ❌ |
| `The Acoustic Album` | ✅ | ❌ |
| `Acoustic` | ✅ | ❌ |

`is_live_album_enhanced` deliberately matches only unambiguous `live` format
tags, so it drops unplugged/acoustic/orchestral coverage. Because
`_DESTRUCTIVE_SECONDARY_TYPES` includes `+acoustic`, delegating to it would
have silently stopped corroborating **acoustic** releases.

`is_live_or_alternate_album` is also a **strict superset** of the narrow list
this guard used before, so every title previously corroborated still is.

## Verification

`tests/test_live_secondary_corroboration_guard.py` (35 tests):

- the bug album corroborates live, and `_mb_type_is_corroborated("album+live", …)`
  returns `True`;
- `+acoustic` / unplugged still corroborate;
- the `"How to Live"` false-positive exemption survives (inherited from the
  canonical detector rather than duplicated);
- ordinary studio titles stay un-corroborated;
- a **superset property test** asserts that nothing the old narrow matching
  accepted is now rejected — the delegation may only ever accept *more*;
- a test pins *why* `is_live_or_alternate_album` is the right target, by
  asserting `is_live_album_enhanced` fails on the acoustic titles;
- `+remix` and marker-free (`album`/`single`/`ep`) behaviour is unchanged;
- the `try/except` fallback works when the helper raises.

**Proven to catch the bug**: reverting the fix makes 3 of the tests fail and
reproduces the exact production log line (`musicbrainz_type=album+live …
reason='neither the album title nor the track titles corroborate…'`). With the
fix applied, all 35 pass.

Regression check on neighbouring suites (`test_track_sync_and_live_detection`,
`test_live_alternate_title_and_mbid`, `test_compilation_tracklist_guard`,
`test_album_type_persistence`): **identical** results before and after
(88 passed, 5 failed either way — see pre-existing failures below).

## Pre-existing failures (NOT caused by this change)

These 5 fail identically on the unpatched tree, i.e. they were already red:

- `test_track_sync_and_live_detection.py::TestLiveAlbumTypeIsAuthoritative::test_mb_secondary_live_type_maps_to_live`
  — the test monkeypatches `mbm.MusicBrainzService`, but
  `_lookup_musicbrainz_album_type` calls `get_shared_mb_client()`, so the fake
  is never used and the real (stubbed) client answers. Stale test, needs
  re-pointing at the shared-client factory.
- `test_track_sync_and_live_detection.py::TestHowToLiveStarRating::test_high_single_on_how_to_live_reaches_five_star`
- `test_live_alternate_title_and_mbid.py::TestLiveTitleCapsAtFourStars::test_normal_high_confidence_single_still_five`
- `test_live_alternate_title_and_mbid.py::TestLiveTitleCapsAtFourStars::test_bare_live_word_title_not_capped`
- `test_album_type_persistence.py::TestEnsureAlbumTypeReturnsVerdict::test_detects_when_missing`

## Config

No new config keys.
