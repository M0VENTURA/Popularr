# Previously-scanned albums reset to 3★ on a non-forced scan — pre-016 rows repaired, not eroded

Date: 2026-10-11
Branch: develop

## Reported

> When scanning an album using a non-forced scan, no tracks hit 5 star and
> reset to 3. … Only happening with albums that have been scanned before and
> have data. Clean releases scan fine.

## Root cause — the cached paths were still running the016 erosion loop

Migration 016 (`52abfe9f`,2026-10-09) persisted the pre-remap blend
(`tracks.raw_score`) so re-normalisation stops eroding album_z. But the
fallback it left in BOTH cached scoring paths —

```python
_raw_combined = raw_score if raw_score > 0 else final_score
```

— fed `final_score` (a value the album-relative remap had **already
rewritten**) back into the remap as if it were the raw blend. Every row
scored BEFORE migration 016 has `raw_score IS NULL`, so every non-forced
album scan of a previously-scanned album re-remapped a remapped value: the
sigmoid is monotonic but SATURATING, so the top track's z decays
monotonically each pass and crosses below the 5★ bound (album_z ≥ 1.0) —
after which **5★ is unreachable and the album flattens into the mid
bands** (how far it falls — 4★, 3★ — is pool-shape dependent; the user
saw 3★).

⭐ **The exact reason "clean releases scan fine": a clean album takes the
FRESH scoring path, which persists a genuine `raw_score` and never touches
the fallback. Only previously-scanned rows — cached within the 7-day
source-freshness window — hit it.** This is the same defect class fixed
twice today already: a value on the WRONG SCALE treated as if it were on
the right one (the Discogs "album guess", the `is_promo` clobber).

## Fix — rebuild the true blend from stored data; unknown is never raw

`services/popularity/stages/track_stage.py`:

* **New `_reconstruct_raw_blend(...)`** — rebuilds a pre-016 row's
  PRE-remap blend from its own stored data (listeners, cached verdict
  flags, album context) by calling the pipeline's own
  `_score_track_popularity`, which is a **pure function of stored data —
  no network**. The reconstruction is EXACTLY what a fresh scan would
  compute for the same row, and it is persisted as `raw_score`, so:
  * pass 1 **repairs** the row (the true spread comes back — 5★ becomes
    reachable again on the very next scan);
  * every later pass is the016 idempotence contract (remap(raw) == stored).
* **When reconstruction is impossible** (no stored listeners, scorer
  error) the raw stays UNKNOWN (`_raw_combined = 0`) and
  `_apply_album_relative_normalization` **skips** the row (`raw <= 0`) —
  the stored score is left untouched instead of being corrupted. A guess
  is never again written where a fact belongs.
* Both cached sites wired: the singles-pass block (Finalise/Singles) and
  the album-scan `_cached` branch (the reported path).

## Tests

* New `tests/test_pre016_rows_repair_not_erode.py` (13): the
  reconstruction is genuine (positive, spread-preserving, unknown → 0,
  broken scorer degrades to unknown NOT to final_score); the user's
  scenario simulated with the shipped math across 24 passes (the old
  fallback crosses below the 5★ bound; the repaired pipeline's pass 1 and
  pass 12 agree exactly); source probes (comment-stripped) pin both wired
  sites, the removal of both fallback literals, and the unknown→0 writes;
  controls (rows WITH raw_score skip reconstruction; the remap still
  skips raw≤0; the fresh-path guarded write intact).
* `tests/test_raw_score_preserves_the_pre_remap_blend.py`: the two pins
  that required the OLD fallback now require the reconstruction (oracle:
  they fail at pre-fix develop).
* **Oracle** at `c4f66fbb`: **9 failed / 4 passed** (4 = either-way
  controls/why-tests) + the two updated016 pins.
* **Sweep** 183 popularity/star/track-stage suites (3,363 tests): base
  `106 failed / 3257 passed` vs new `106 failed / 3257 passed`, failing
  sets **identical** → **0 regressions**.

## What the user will see after the rebuild

The next non-forced scan of a previously-scanned album repairs each
pre-016 row in place (one pass, no extra API cost — the blend is rebuilt
from stored listeners) and the stars settle back to their true values;
subsequent scans no longer move them. Rows already carrying `raw_score`
(post-016 fresh scores) were never affected.
