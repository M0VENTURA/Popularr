# A GLOBAL 5★ lock is no longer revoked, and a best-of can reach 5★

**Date:** 2026-10-14
**Files:** `services/popularity/stages/finalise_stage.py`

Reported from a scan log: *"Every single track that used to be 5★ (including
tracks explicitly marked GLOBAL 5★ LOCKED) was demoted to 4★ in
finalise_stage"*, plus *"Garage Inc. / The Weight of Words / Black Crosses get
25 three-stars or 22 one/two-stars instead of a normal bell curve"*.

## Defect 1 — the album shape test vetoed the GLOBAL 5★ lock

### What was actually happening

Traced with a probe against the shipped `_assign_stars`, not by reading. The
demotion was **not** `organic_floor`, **not** `listener_5star_z`, and **not**
`top_tier_min_listeners` (that one is inside the compilation branch and cannot
reach a studio album). All three suspicions in the report were ruled out.

The gate is `_album_ratio_standout_ok`, and it is called in **two** places. The
first is *inside the lock branch* — which the branch's own condition
(`if track.get("_global_5star_locked") and not popularity_only and
not is_compilation:`) means it **can only ever veto a locked track**. The
probe's verdict string was explicit:

```
ratio_flat(0/3 passed; failed=runner_up,median,floor;
           runner_up=1.11x/1.50x, median=2.19x/3.00x, floor=3.40x/10.00x)
```

That is a **double-count of the same guard**, and with narrower evidence:

* The lock's pre-pass (`_compute_global_5star_locked_titles`) ranks the
  artist's **whole scanned catalogue** by `_raw_combined` and locks the top
  `global_5star_catalog_top_pct` (20%) that also clear
  `global_5star_min_raw_score` (default 60). That raw-score floor *is* the
  "these counts are real, not noise" test the ratio gate exists to supply.
* The ratio gate then re-judged the lock using **one album's** linear Last.fm
  spread.

It is also unsatisfiable by construction on some albums. Measured on Abney
Park: "The Wake" (6.8k LF, raw 74.4, the artist's catalogue top) cleared
`album_z=3.22` / `artist_z=2.12` and then failed at `runner_up=1.11x/1.50x` —
because the album's **#2** ("The Only One", 6.1k LF) is *also* one of the
artist's best tracks. Two hits on one album can never be 1.5x apart, so **no
track on that album could reach 5★**.

Worse, the veto **erased the flag** (`track["_global_5star_locked"] = False`)
before setting `_force_floor = 4`. That disarmed every downstream consumer of
the flag:

* the era slot cap's `protected_five_star_tracks` list, and
* `_short_track_cap_is_protected`.

### The fix

A track holding the lock satisfies the shape test, and the diagnostic records
`lock_bypasses_album_shape_gate` so a log reader can tell "the shape test was
skipped" from "the shape test passed".

This is **not** an unconditional award — the z-bounds still gate the lock, so
a track that is not genuinely exceptional within its album *and* its artist
catalogue is still floored at 4★. The ratio gate remains in force on the
ordinary, **unlocked** 5★ path, where it is the only shape guard; that is the
Anti-Flag case (counts 14/13/13 vs an 11 median) and it is pinned as a control.

## Defect 2 — a single-artist compilation was forced onto the online chart

`has_usable_catalogue = False` was hard-wired with the comment *"Force online
catalogue lookup by bypassing the local threshold"*. That was right for a **VA**
compilation — each credited artist owns one or two songs locally, so the local
catalogue can never answer "is this that artist's hit?" — but it was applied to
**every** compilation, including a single artist's own best-of.

The cost:

* The online rater's 5★ band is the artist's **global top 2%**
  (`rank_percentile_5star`). Across Metallica's 150-track career that is its
  three biggest songs, so **Whiskey in the Jar** (CAT-Z +2.00, 928.2k LF,
  rank 12/150 = top 7.7%) could only ever be 4★.
* Tracks outside the online list fell through to the **absolute** fallback — a
  rater whose `score >= 70 → 3★` bands are calibrated for an *album-relative*
  score but are fed a *track-artist-relative* one. On "The Weight of Words" 22
  of 24 tracks flattened to 1★/2★.

### The fix

The rater is now selected by `is_va_compilation`, using the thinness guard the
config already shipped (`min_local_catalogue`, default 5):

| compilation | catalogue used |
|---|---|
| VA | the credited artist's **online** chart — always, unchanged |
| single-artist, ≥ `min_local_catalogue` scores | the **local** artist catalogue (CAT-Z bands) |
| single-artist, thinner than that | the online chart (fallback) |

The VA path is deliberately untouched: the 2026-09-21 decision and the
`test_compilation_online_catalogue` / `test_nightmare_revisited_scan_findings`
suites are unchanged. The thin-catalogue fallback is what keeps a barely-owned
artist working.

⚠️ **This also fixes a contract the code had already broken.**
`test_compilation_online_catalogue::test_healthy_local_catalogue_skips_the_online_lookup`
("a well-represented artist's ratings must NOT change") had been failing since
the force-online change. It passes again.

### Cleanup

`_assign_stars` declared `generic_compilation_artist`, `post_album_star_ratings`
computed it, and **nothing ever read it**. It is replaced by the authoritative
`is_va_compilation` (which also covers the "4+ credited artists" heuristic a
name check cannot see).

## Contract reversal — one test rewritten

`test_greatest_hits_artist_catalogue` previously pinned *"a track ranked last on
a greatest-hits album but ranked #1 globally reaches 5★ **through the online
chart**"*, using a fixture whose `artist_scores` was the album's own six tracks
— i.e. a catalogue declared healthy while the test depended on going online
anyway.

That sub-case is exactly what the report asked to stop for a single-artist
compilation, so the test is **rewritten with the reversal documented**, not
deleted:

* `test_a_thin_local_catalogue_still_reaches_five_stars` — the same intent,
  honestly declared: a *thin* library means the online chart answers, and the
  buried global hit still reaches 5★.
* `test_a_healthy_local_catalogue_rates_on_cat_z` — the new route: the local
  CAT-Z bands decide, and a catalogue-top track reaches 5★.
* `test_a_healthy_local_catalogue_never_calls_online` — the cost contract.

`test_compilation_more_5star::test_many_hits_all_stay_five` is renamed and
rewritten: its old assertion was satisfied by the **absolute fallback's**
`score >= 95 → 5★` band, the rater the report asked to take out of the way.
The invariant its class owns (the era slot cap is skipped for compilations)
lives in `post_album_star_ratings` and is pinned there.

## Tests

`tests/test_locked_five_star_not_revoked.py` — **17 tests**: the four reported
lock shapes, plus controls proving the ratio gate still vetoes unlocked tracks,
the z-bounds still gate the lock, and compilations / `popularity_only` / live
albums still bypass the lock branch.

Oracle (source reverted, tests kept): **10 failed / 7 passed** — the 7 passing
are the deliberate controls.

## Related changes in the same sweep

`test_greatest_hits_artist_catalogue` 20 → **22 passed** ·
`test_compilation_online_catalogue` 1 failed → **28 passed** (a pre-existing
failure fixed) · `test_nightmare_revisited_scan_findings` **22 passed** ·
`test_compilation_more_5star` 2F/2P unchanged · 34 suites swept, **zero new
failures**.

Pre-existing failures confirmed identical before and after (not regressions):
`test_per_album_star_posting` 8F/40P, `test_prominence_era_benchmark` 3F/7P,
`test_instrumental_standout_block_and_weight_penalty` 4F/13P,
`test_live_alternate_title_and_mbid` 4F/7P, `test_track_sync_and_live_detection`
2F/13P.

## Reported, not changed

**Unlocked** tracks on an old, minor-era album are still vetoed by the shape
test by design (e.g. *"Intergalatic Juke Joint" — "Deep in Space", 237 LF*).
The gate is scale-free, so it cannot distinguish "14 vs 13 plays" (noise, the
case it was built for) from "6800 vs 6100" (two hits). Adding an absolute
*noise floor* — below which the multipliers are treated as inconclusive, the
same way `< 3 usable values` already is — would unblock those without
re-opening the noise case. Left out deliberately so this change stays scoped
to what was reported; say the word and it is a small follow-up.
