# Genre playlists: monotonic ordering, a cap that binds, a strength-preserving spread (2026-10-07)

## Why

Three defects in the genre/Essential playlist ordering pipeline, all measured
with the shipped functions before they were changed.

### 1. The ordering key was NOT monotonic in popularity

`_effective_order_score(mode="prominence")` used `album_prominence_score`, a
weighted MEAN of the two log scores that **renormalises its weights over
whichever signals are present**. An absent signal was therefore DROPPED (no
penalty) while a present-but-low one was averaged in (a penalty), so a track
with more listens on BOTH sources could rank below one with less:

| track | old score |
|---|---|
| 5,000,000 LF, **no LB data** | **100.0** |
| 9,000,000 LF, 10,000 LB | 83.8 |
| 50,000,000 LF, **no LB** | **100.0** |
| 50,000,000 LF, 500,000 LB | 96.0 |
| 200,000 LF, no LB | 84.8 |
| 200,000 LF, 5,000 LB | 73.3 |

Real playlist order produced from those rows: `200,000 (no LB)` ranked 5th,
**ahead** of `9,000,000 + 10,000 LB` at 6th. Having ListenBrainz data actively
hurt a track.

### 2. The key SATURATED at 100

`calculate_lastfm_popularity_score` is `min(100, log10(n+1) * 16)`, which
reaches 100 at about **1.78M** Last.fm listeners. Every mainstream track tied at
exactly 100.0, so the order among them fell through to the album-relative
`stars` and stored score and then the TITLE — reintroducing the very
album-context ranking the prominence mode exists to remove.

### 3. `Max Tracks Per Artist` was not a cap

The pipeline was rank → cap → spread → **slice**, and the cap DEFERRED the
overflow to the end "rather than dropping it", so the slice pulled the deferred
tracks straight back in. Measured on a 600-track pool (1 artist x200, 4 x100,
cap 25, `max_tracks` 300):

* with spreading off, the dominant artist took **200 of 300 slots (67%)**;
* with spreading on, **60 each** — 60 > the configured 25.

The Config page promised the cap "stops one prolific artist filling a genre
playlist".

### 4. The spread was a round-robin

`_interleave_artists` emitted each artist's #2 after EVERY artist's #1, so the
global ranking held only for the first round. Measured on a 3-artist pool it
emitted `A0, B1, C3, **A5**, B2, C4` — the 6th-strongest track jumped ahead of
the 3rd.

## Fix

`services/popularity/popularity_math.py` — new `playlist_popularity_score`:
the **maximum** of the two raw log scores, each **UNCLAMPED**. It is monotonic
(if A >= B on both sources then score(A) >= score(B)), non-saturating, and
imputation-free (an absent signal contributes 0, so it can neither lift a track
above nor drag it below its known signal). `album_prominence_score` is
deliberately UNCHANGED — it is the era BENCHMARK measure and
`_build_album_model` / `album_prominence_median` depend on its 0-100 scale.

`services/popularity/stages/finalise_stage.py`:

* `_effective_order_score` now uses `playlist_popularity_score`; the stored-score
  fallback for tracks with NO listener data is unchanged.
* `_apply_artist_cap` **drops** the overflow, so the quota binds on the emitted
  playlist.
* `_interleave_artists` is now greedy: at each step it takes the STRONGEST
  remaining track whose artist did not just play, falling back to a run only
  when no other artist remains.

`test_site/templates/Pages/config.html` — the per-artist cap help text now
describes the binding behaviour.

## Cost, stated rather than hidden

The cap is now a real quota, so a genre whose qualifying pool is dominated by
**fewer artists than `ceil(max_tracks / max_per_artist)` yields a SHORTER
playlist**. That is the cap working as configured; setting the option to `0`
restores the previous (uncapped) behaviour exactly. The previous
"never shrink" property was the reason the cap could not bind at all.

## Files

* `services/popularity/popularity_math.py` — new `playlist_popularity_score`
* `services/popularity/stages/finalise_stage.py` — key, cap, interleave
* `test_site/templates/Pages/config.html` — cap help text
* `tests/test_playlist_ordering.py` — 62 tests

## Tests

`tests/test_playlist_ordering.py` (62, +6): the three measured inversions, the
saturation, dominance monotonicity, the binding cap (including the single-artist
pool still yielding its quota), the strength-preserving spread, and the existing
guards (determinism, membership-unchanged, single-artist untouched, `stored`
mode, Essential ordering, config exposure, config round-trip).

Two tests were re-labelled because the contract changed: the cap no longer
defers overflow (`test_the_cap_defers_rather_than_drops` →
`test_the_cap_limits_the_playlist`), and the case-insensitive cap test now
counts the bounded playlist instead of the old deferral.

**Oracle:** reverting `popularity_math.py` + `finalise_stage.py` → **9 failed /
53 passed** (the guards stay green, as they must).

**Suite sweep** (14 suites: genre playlists, playlist sync/cap, Essential
sections, Essential refresh/dedup, Christmas exclusion, prominence era
benchmark, playlist dedupe, dropped-ids, per-album star posting, finalise scan
mode, compilation 5★, compilation online catalogue, LB cohort, ordering):
**base = 21 failed, new = 21 failed, `Compare-Object` = EMPTY** → the failing
sets are identical (all 21 pre-existing at `origin/develop`).
