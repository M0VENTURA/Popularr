# A rejected source already hands its weight to the survivor — now it is pinned

**Date:** 2026-10-06
**Reported:**

> When the audit rejected ListenBrainz, it zeroed out the LB component
> (`LB: 0.0`), but the scoring function did not redistribute the missing 30%
> weight back to Last.fm … the track received 0 points for the ListenBrainz
> share.
>
> **The Fix in `calculate_combined_popularity_score`**
> `if source_audit == "REJECT_LB" …: eff_lf_weight = lf_weight + lb_weight;
> eff_lb_weight = 0.0`

## What the code actually does

Both scorers already renormalize — they divide by `sum(ACTIVE weights)`, so a
rejected source's share goes to whatever survives:

```python
# calculate_combined_popularity_score
if _audit == "REJECT_LB":
    lf_weight, lb_weight = 1.0, 0.0      # ← Last.fm gets the whole weight
...
total_weight = sum(active_weights)         # only ACTIVE weights
combined = sum(s * w for s, w in ...) / total_weight
```

and the post-scoring path used by singles scans does the same:

```python
# apply_log_ratio_audit_to_stored_score
if verdict == "REJECT_LB":
    lf_w, lb_w, age_w = 1.0, 0.0, 0.0
...
combined = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
```

**The report's own log line is the proof:** `Final: 57.3 (LF: 57.3 | LB: 0.0)`
has Final *equal* to the Last.fm component. If 30% of the weight had been
dropped, Final could not exceed `0.7 × 57.3 ≈ 40`. `57.3` **is** the
renormalized result — the proposed `eff_lf_weight = lf_weight + lb_weight` is
already in effect.

A probe with the reported numbers (477,200 LF / 290 LB) confirms it:

```
audit verdict: REJECT_LB
with audit   : combined 75.941  lastfm 75.941  lb 0.0   ← combined == LF
combined == 0.7 x lastfm (the reported bug)? False
```

## What is genuinely different for that track

The remaining gap is **downstream, in the rating step**, not in the weights:
siblings keep a healthy ListenBrainz component (`LB: 89.4`) that blends their
finals up to ~60–62, while a track whose second source was rejected scores on
Last.fm alone. Compared against that album median its Z-score lands near 0 and
it rates 3★.

That is a real asymmetry, but it is a *scoring-design* question — three ways to
address it, none taken here because each changes ratings across the whole
library:

1. **Leave it** (current): fewer trustworthy signals → less evidence, so the
   track sits where its single source puts it.
2. **Compare within cohorts**: rate a REJECT_LB track against the median of the
   other tracks that also lack a usable ListenBrainz value.
3. **Lift the survivor**: score a rejected-source track on its *absolute* Last.fm
   evidence (`log10(477k)` ≈ 89) instead of the album z-score, so a strong
   single source can still reach 5★.

Say which one you want and it becomes its own change with its own gate.

## Why this change exists at all

Nothing in the suite pinned the arithmetic — the existing
`test_reject_lb_scores_on_lastfm_only` only asserts `audited >= lf_only`,
which a broken build can still satisfy. That is why a correct mechanism could
be reported as missing.

## Tests

`tests/test_log_ratio_audit.py` — **24** (was 17; +7 in
`TestARejectedSourceDoesNotLoseItsWeightShare`):

* the audit really does return `REJECT_LB` for the reported pair;
* `REJECT_LB` gives Last.fm the **whole** weight (`combined == lastfm_score`);
* the verdict wins even when a *healthy* LB value is passed — the rule is the
  verdict, not the data;
* CONTROL: the score is **not** the `0.7 × LF` the report describes;
* CONTROL: `REJECT_LF` is symmetric;
* the post-scoring re-blend renormalizes the same way;
* CONTROL: a valid track still blends both sources (renormalization must not
  collapse every score to one source).

**Oracles (mutation, since this change is test-only):**

* changing `1.0, 0.0` → `0.7, 0.0` leaves all 24 green — correctly: dividing
  by the active sum makes the absolute weight irrelevant, so the tests assert
  *behaviour*, not implementation;
* deleting the `REJECT_LB` branch (verdict ignored) fails exactly
  `test_reject_lb_wins_even_when_a_healthy_lb_value_is_passed` — the test
  bites.

## Verification

- new suite: **24 passed**
- affected set (14 files): **247 passed / 31 failed**, against a baseline
  subset of 52 for the same files → **0 new, 21 fixed**
- full suite: **225 failed / 4750 passed / 2 skipped** in 611 s against a
  baseline of 303 failures → **79 fixed, 0 real new**. The single NEW id is
  `test_download_completion_not_found_loop.py::TestDeepFileSearch::
  test_sibling_torrents_root_is_searched`, the known Windows path-case flake.

## Files

- `tests/test_log_ratio_audit.py`
