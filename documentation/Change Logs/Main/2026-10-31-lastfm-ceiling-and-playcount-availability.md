# Last.fm ceiling, listener-only confidence and missing playcounts

**Date:** 2026-10-31
**Area:** popularity scoring / Last.fm source
**Files:** `services/popularity/popularity_math.py`,
`services/popularity/stages/track_stage.py`,
`tests/test_sequel_and_version_popularity_matching.py` (new),
`tests/test_album_star_curve_and_resync.py`

## Reports

* 59 release-tracks logged `LF: 100.0` — including 2.3M-listener tracks
  ("The Unforgiven", "Just Like Heaven") and 64M-listener hits ("Creep").
* 2,428 rows had `lastfm_listeners > 1,000` and `lastfm_playcount = 0`, some with
  `lastfm_score = 100`.
* Paul Kelly's "How to Make Gravy" reached `LF: 100` with only 47,281 listeners
  — proof the 100s were not only the absolute curve.

## Causes

1. **The absolute curve saturated at ~1.78M listeners.**
   `16 * log10(n)` reaches 100 at `10^6.25`; every track from a 2M album cut to a
   64M global hit tied at exactly 100.
2. **The album-relative z was not bounded by absolute popularity.**
   `calculate_lastfm_zscore_popularity` maps the top track of *every* album to
   ~100. A 16k-listener album lead and a 4M-listener single both logged
   `LF: 100`. This is also why 47k-listener Paul Kelly could show 100.
3. **Missing playcount was folded into zero, and then ignored by the scorer.**
   The fetch kept `lastfm_playcount = 0`, and the persistence block derived
   `lastfm_playcount_available` from `value > 0`. The scorer never read the
   flag, so a listeners-only row could still reach the top of the scale.

## Fix

* `calculate_lastfm_popularity_score` now reaches 100 at
  `LASTFM_SCORE_LISTENER_CEILING = 100,000,000` listeners: 1.8M ≈ 76,
  4.2M ≈ 80, 64M ≈ 94. The top of the catalogue keeps its ordering instead of
  collapsing to 100.
* `calculate_combined_popularity_score` caps the album-relative Last.fm score at
  the absolute curve (`min(relative, absolute)`). Within-album ordering is
  preserved, but a small album's lead cannot claim global-hit confidence.
* When `lastfm_playcount_available` is false, the Last.fm component is capped at
  `LISTENER_ONLY_LASTFM_CEILING = 85`. Listeners-only evidence still ranks, but
  not at full confidence.
* `track_stage` now records the **source** `playcount_available` (prefetch,
  aggregate, direct and search paths) and writes `lastfm_playcount = NULL` when
  it is unknown instead of 0. A measured zero with the flag true is preserved.
* `lastfm_playcount_available` is passed into the scorer (`_as_bool` handles
  `0`/`"0"` from the database).

## What this does not do

It does not backfill historical rows. Re-scanning an album rewrites the
availability flag and the score; until then the old `lastfm_playcount = 0`
rows still carry the old value and the scorer will treat them as listener-only
from the persisted flag where it exists.

## Verification

`tests/test_sequel_and_version_popularity_matching.py`:

| Test | Pins |
|---|---|
| `test_the_top_of_the_catalogue_is_no_longer_flat_100` | 2M vs 4.2M stay ordered |
| `test_the_old_saturation_point_is_now_mid_scale` | 1.78M < 85 |
| `test_a_missing_playcount_caps_the_lastfm_component` | ⭐ listener-only ≤ 85 |
| `test_a_measured_playcount_keeps_the_full_score` | flag respected |
| `test_a_small_albums_lead_does_not_reach_100` | ⭐ relative capped by absolute |

`tests/test_album_star_curve_and_resync.py` now derives the expected curve value
from `calculate_lastfm_popularity_score` instead of hard-coding `16 * log10`.
