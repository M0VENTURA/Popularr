# A track with a rejected source is rated among tracks that share its fate

**Date:** 2026-10-07
**Requested:**

> Rate within cohorts — compare a REJECT_LB track against the median of other
> tracks that also lack a usable ListenBrainz value.

## The problem

A track whose ListenBrainz value is unusable is scored from **Last.fm alone** —
but it was being measured against a pool its siblings had *lifted* with a
healthy ListenBrainz component. On *Undertow*:

| track | LB points | final |
|---|---|---|
| Intolerance / Swamp Song / Crawl Away | ~89 each | 60.8 – 62.8 |
| **Prison Sex** (LB rejected) | 0 | **57.3** |

`57.3` is a *good* Last.fm score — its siblings' pure Last.fm scores were
44–48. It rated 3★ only because the album median (~57) it was compared against
was inflated by the very component it did not have: an album Z of **+0.05**.
The comparison, not the score, was wrong.

## What changed

**1. Which tracks share the fate — `_lb_unusable_cohort(album_results)`**

Returns `(track_ids, scores)` for tracks whose ListenBrainz source is
*unusable*: zero listens, **or** the Log-MAD audit rejects them against this
album's own LF/LB pairs. That is deliberately the same test `track_stage`
applies when it zeroes the count, so a track classified here is treated exactly
as it was scored — one rule, two places, no drift.

**2. Which pool the rating uses — `_assign_stars(..., source_unusable,
cohort_scores)`**

One pool, computed once, used by *every* album-relative decision:

```python
_album_pool = _cohort_pool if _use_lb_cohort else album_scores
```

fed to the top-of-function z, to **`_album_z_band_star`** (which decides
`base_stars`), to `_album_rank` (the 5★ top-N gate) and to the live-album path.

Two guards keep the default intact:

* the track must be flagged `source_unusable`, **and**
* the cohort must hold at least `_LB_COHORT_MIN_TRACKS = 3` — below that there
  is no distribution to speak of, so the album pool remains (and
  `calculate_robust_zscore` would return 0 anyway).

**3. Compilations are excluded on purpose.** Their stars come from the credited
artist's *online* catalogue or absolute thresholds — the local-catalogue branch
is hard-disabled in the code (`has_usable_catalogue = False`) — so there is no
album-relative pool for a cohort to replace. Applying it there would have
changed nothing while implying it did.

There is no config switch: this is a correctness fix for a comparison that was
measuring against the wrong distribution, and it only ever fires for tracks
whose source was rejected.

## The near-miss worth recording

The first version fed the cohort only to the top-of-function `album_z` — which
looked complete and changed almost nothing, because `_album_z_band_star` (the
call that actually decides `base_stars`) re-derived its own z from
`album_scores`. A test that asserted on `_compute_album_z` alone passed while
the rating stayed the same. The tests now assert on **the band call**, which is
the decision that matters.

A second trap in the same edit: the cohort block was briefly defined *after* its
first use — `py_compile` does not catch read-before-assignment, and only an AST
order check surfaced it.

## Tests

`tests/test_lb_cohort_rating.py` — **12**:

* the classifier flags the reported pair (477k Last.fm / 290 ListenBrainz) and
  the zero-LB track, leaves healthy tracks alone, and — with no comparable
  pairs — flags only the missing ones;
* **the star band is rated against the cohort** (the decision, not just the
  top z), and so is the top z;
* CONTROL: a healthy track still uses the album pool;
* CONTROL: a cohort of 2 falls back to the album pool;
* CONTROL: a compilation keeps its own rating path (no band call, artist pool);
* the cohort's z is higher than the lifted-album z and the rating never drops;
* the caller builds it once per album and passes the flag per track id.

**Oracle:** with `finalise_stage.py` stashed → **12 failed / 0 passed**.
Restored, 12/12; markers 6/6; stash count back to 3.

## Verification

- new suite: **12 passed**
- affected set (32 files): **444 passed / 61 failed**, against a baseline
  subset of 114 for the same files → **0 new, 53 fixed**
- full suite: **224 failed / 4775 passed / 2 skipped** in 670 s against a
  baseline of 303 failures → **79 fixed, 0 new** — a clean run, with the
  usually-flaky `test_sibling_torrents_root_is_searched` passing too.

## Effect

Existing stars are recomputed on the next scan (ratings only skip when
unchanged, and these *do* change). A track in this position moves up toward the
rating its own single source supports; no track outside this cohort is touched.

## Files

- `services/popularity/stages/finalise_stage.py`
- `tests/test_lb_cohort_rating.py` (new)
