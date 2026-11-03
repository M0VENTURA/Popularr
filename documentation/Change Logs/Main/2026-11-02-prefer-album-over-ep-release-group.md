# Prefer an Album release group over a same-named EP

**Date:** 2026-11-02
**Area:** MusicBrainz album matching / album type
**Files:** `services/enrichment/musicbrainz_service.py`,
`services/popularity/stages/album_stage.py`,
`tests/test_album_prefers_album_over_ep.py` (new)

## Report

> “The Unauthorized Biography of Reinhold Messner” by Ben Folds Five has 11
> tracks but is marked EP. MusicBrainz has an EP and an album with the same
> title.

## Cause

MusicBrainz carries **two release groups with the same title** — an Album and
an EP. The matcher ranked candidates purely on the title/artist score, so the
EP could win a tie, and once an EP `musicbrainz_albumtype` + release-group
MBID were stored, `_lookup_musicbrainz_album_type` trusted the pair forever:

```python
if stored_type and stored_rg:
    return stored_type, stored_rg
```

The track-count refinement penalised `abs(local - candidate)` symmetrically,
so a 5-track EP was only 0.30 behind an 11-track album — close enough for the
name score, and an expanded (higher-count) edition was penalised just as hard
as a truncated one.

## Fix

1. **Album-sized releases reject EP/Single candidates.** With more than 6
   local tracks, an EP/Single release-group candidate scores ×0.5 and the
   resolver explicitly prefers an Album candidate (score ≥ 0.6) over an
   EP/Single best match.
2. **Prefer “similar or higher” track counts.** The count penalty is now
   asymmetric: a candidate at/above the local count is penalised 0.02 per
   extra track; a candidate below it is penalised 0.10 per missing track. A
   deluxe edition (13 tracks for an 11-track album) stays a viable match; a
   5-track EP cannot outrank it.
3. **Stop trusting a stored EP/Single binding on an album-sized release.**
   When the local release has more than 6 tracks and the stored type is
   `ep`/`single`, the lookup re-resolves instead of returning the cached pair.
4. **Healing persistence.** `_persist_album_type_to_tracks` can replace a
   stored `ep`/`single` type (and the release-group MBID) when the resolved
   type is a plain album (`replace_secondary` / `replace_release_group`).
   Ordinary fill-only behaviour is unchanged.

EP-sized local releases (≤ 6 tracks) are untouched, so a genuine EP still
keeps its stored EP binding.

## Tests

`tests/test_album_prefers_album_over_ep.py`:

* the asymmetric track-count penalty (exact, higher, lower, mixed);
* an EP scoring 0.99 loses to an Album scoring 0.90 for an 11-track release;
* a stored EP + RG is re-resolved for an 11-track release;
* a 5-track release still trusts its stored EP without searching;
* persistence replaces the EP type and release-group MBID, and leaves both
  alone without the override flags.
