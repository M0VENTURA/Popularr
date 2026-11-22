# Age-adjusted velocity and ListenBrainz collision dampening (opt-in)

**Date:** 2026-11-21
**Area:** `services/popularity/popularity_math.py` ·
`services/popularity/popularity_config.py` ·
`services/popularity/star_resync_service.py` ·
`templates/pages/config.html` · `static/js/config.js` ·
`static/js/pages/config.js`
**Commit:** `feat(popularity): add opt-in age-adjusted velocity and collision
dampening`

## Why

The blended score only ever sees **one track's** counts, so it cannot answer two
questions the collection's popularity report raised:

1. **A raw listen count rewards AGE.** A 1994 single and a 2024 single with the
   same listener count are not equally popular *today* — the older one
   accumulated its total over three more decades.
2. **Two different songs sharing an identical `(listens, score)` pair above
   50k** is not a coincidence: `listenbrainz_score` is a pure function of the
   listen count, so an exact tie means one recording's numbers were counted
   twice (a recording/MBID resolution miss). That is what let "Mama Said" tie
   "The Memory Remains" and "Jealous" inherit "Ironic"'s figures.

Both are implemented as **pure helpers** in `popularity_math` and applied by the
star resync — the only pass that holds an artist's whole catalogue at once.

## The three helpers

| Helper | What it does |
|---|---|
| `age_adjusted_velocity` | Listens per year of the track's life, using the **earliest studio release** so a greatest-hits reissue cannot make an old song look new. A `VELOCITY_MIN_AGE_YEARS` floor (1.5y) stops a brand-new release dividing by ~0 and manufacturing a huge velocity from a modest play count. |
| `normalise_velocity` | Log-scaled 0–100 against the **artist's fastest** track, so a viral single does not flatten the rest of the catalogue to zero. |
| `find_collision_keys` / `collision_damping` | 0.88× on a shared `(listens, score)` pair, and only when the two rows carry **different titles** (the same song on two albums is not a collision) and the count clears `VELOCITY_COLLISION_MIN_LISTENS` (50k) so small accidental ties are ignored. |

`calculate_combined_popularity_score` gains optional `velocity_score`,
`velocity_weight` and `collision_multiplier` parameters that default to a
**no-op**, so existing ratings are byte-for-byte unchanged unless a caller opts
in. The collision multiplier is applied *after* the metadata floor, because the
floor exists so a track with metadata but no source evidence is not scored as
unlistened — it must not undo a damping that says "these two songs cannot both
have these numbers".

## Configuration — OFF by default

Everything is gated on the new `popularity.velocity` block:

```yaml
popularity:
  velocity:
    enabled: false          # run the pass at all
    weight: 0.0             # share of the score the listens-per-year term carries
    collision_damping: 0.88 # multiplier for a shared (listens, score) pair
    collision_min_listens: 50000
```

`enabled` is **explicit**, not implied by `weight` alone: a weight of `0` still
runs the collision dampening, because the two terms are independently useful.
Setting a `weight` (without `enabled`) does turn the pass on.

The block is exposed on **Config → Analytics & Scoring**, so an operator can
preview the resync before switching it on. Enabling either term re-rates the
library on the next star resync, so preview first.

## Stars: what was and was NOT changed

The existing era-based 5★ caps (peak 4 / solid 3 / minor 2) already produce the
per-period limit the report asked for, and the star curve is already
artist-relative through z-scores. **No new star-assignment rule was added** —
this change only re-weights the *score* the resync reads.

## Files

* `services/popularity/popularity_math.py` — `age_adjusted_velocity`,
  `normalise_velocity`, `find_collision_keys`, `collision_damping`, and the
  opt-in parameters on `calculate_combined_popularity_score`
* `services/popularity/popularity_config.py` — `get_velocity_scoring_config()`
* `services/popularity/star_resync_service.py` — `_apply_velocity_adjustments()`
  wired into the resync, whose report includes the collision count
* `templates/pages/config.html`, `static/js/config.js`,
  `static/js/pages/config.js` — the Config page controls and save wiring
* `tests/test_velocity_scoring.py` — new (31 tests)

## Tests

`tests/test_velocity_scoring.py` → **31 passed**. It covers the pure helpers
(division by age, monotonicity, log scaling, collision detection/damping), the
opt-in defaults (the score is unchanged when nothing is supplied), config
reading/clamping, and the catalogue-wide application in the resync (no-op when
disabled, damping and earliest-release selection when enabled).
