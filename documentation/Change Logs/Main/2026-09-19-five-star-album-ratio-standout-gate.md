# 5★ standout gate: album RATIO multipliers instead of z-scores alone

**Date:** 2026-09-19
**Area:** `services/popularity` · `helpers` · `templates/pages/config.html` + test_site mirror
**Status:** implemented, guarded by `tests/test_album_ratio_standout.py` and a new class in `tests/test_per_album_star_posting.py`

---

## The problem

A 5★ rating was decided entirely by **z-scores**: album-z and artist-z above
configured bounds, plus a catalogue-top flag. A z-score measures deviation
relative to the album's *own spread* — and on a small, flat album the spread
shrinks too. So a 3-listener gap across counts of 8..14 produces a perfectly
respectable z-score even though the numbers are indistinguishable from noise.

A real report made this concrete — Anti-Flag's acoustic album, where the
listener counts were **14, 13, 13, ~11 median, 8**:

| Test | Formula | Result | What a real 5★ needs |
|---|---|---|---|
| Runner-up gap | `top ÷ #2` | 14/13 = **1.07×** | 1.5×–2.0× |
| Baseline | `top ÷ median` | 14/11 = **1.27×** | 3×–5× |
| Ceiling-to-floor | `top ÷ bottom` | 14/8 = **1.75×** | 10×–50× |

All three say the same thing: the top track is only ~7% ahead of its
runner-up. That album has no standout, and no z-score can see it.

## The fix

Ratios are **scale-free** — "is #1 three times the median?" means the same
thing whether the artist has 100 listeners or 10 million. Three independent
multipliers are now required on every popularity-derived 5★.

### New pure helpers (`services/popularity/popularity_math.py`)

- `album_standout_ratios(counts)` → the three multipliers plus the raw
  counts behind them (so a log line can show the numbers a decision used).
- `album_ratio_standout(count, counts, ...)` → `(ok, reason, ratios)`.

Two properties are load-bearing and documented in the module:

1. **Inconclusive data is permissive.** Fewer than 3 usable counts means the
   question cannot be answered, so the gate returns `ok=True`. A short album
   must never be *penalised* for lacking a distribution to measure.
2. **The candidate must BE the album's top track.** The multipliers describe
   the distance between #1 and the rest, so any other track is ineligible by
   definition — which is also what stops a flat album from minting several 5★.

### Gate wiring (`services/popularity/stages/finalise_stage.py`)

`_album_ratio_standout_ok()` applies the test, and it runs on **all three**
popularity-derived 5★ paths:

| Path | Behaviour on failure |
|---|---|
| `is_standout` (z-bounds + marking/z_standout source) | 5★ withheld, stays on the band |
| `_global_5star_locked` (catalogue-top pre-pass) | falls back to the existing 4★ floor |
| Live albums (`_live_album_stars`) | demoted to 4★, reason appended |

A **user override is never demoted** — it is an explicit instruction, not a guess.

### Two implementation traps found and fixed during development

These were caught by my own probes, not by review, and are worth recording:

1. **Log compression before the ratio destroys the signal.** The first
   implementation converted each track's counts through
   `album_prominence_score` (which is `log10(n) * 16`) and then took the
   ratio. 500,000 vs 10,000 is a **50×** spread in raw listeners but only
   **1.42×** after log compression — so *every* album looked flat and the gate
   blocked everything. The multipliers must be taken on the **linear** counts.
2. **The series must come from ONE provider.** Last.fm listeners and
   ListenBrainz listens differ by orders of magnitude for the same recording,
   so pairing or concatenating them makes the multipliers meaningless.
   Last.fm is preferred, ListenBrainz is the fallback.

## Configuration

Surfaced on the Config page (both trees) and in `config.js` (both trees),
following the repo rule that **the Config page is the source of truth** for
user-editable settings:

`single_detection.album_ratio_standout`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `1` | master switch; OFF restores z-score-only rating |
| `runner_up_min` | `1.5` | `top ÷ #2` |
| `median_min` | `3.0` | `top ÷ median` |
| `floor_min` | `10.0` | `top ÷ bottom` |
| `min_passed` | `3` | how many of the three must hold (1–3) |
| `live_*` variants | `1.5 / 2.0 / 5.0 / 2` | looser, see below |

The **live** variants are deliberately looser and require only **2 of 3** by
default: a live recording carries crowd noise, applause and tuning on *every*
track, so the least-played cut is never far below the most-played and the
floor ratio carries little signal.

`get_standout_config()` merges the defaults, so the key is always present and a
partial user block overrides only the keys it names — the same passthrough bug
that silently discarded `album_scaling` and `live_album_scaling` settings
before they were added.

## Tests

**New:** `tests/test_album_ratio_standout.py` (16 tests) — the pure arithmetic,
including the Anti-Flag reference values (1.08× / 1.27× / 1.75× reproduced to
3 decimals), scale-freedom, zero-handling, inconclusive-data permissiveness and
threshold configurability.

**New class** in `tests/test_per_album_star_posting.py` — `TestAlbumRatioStandout`
(8 tests) covering the wired behaviour: the reference album is rejected, a real
standout still reaches 5★, thin data never blocks, a non-top track is
ineligible, the gate can be disabled, live uses the looser thresholds, and a
user override survives.

### Two existing tests changed — deliberately

Both had encoded the behaviour this change replaces, so they were updated
rather than left to fail:

- `test_album_top_listeners_honour_standout_flag` reused a
  **uniformly-declining** album (`[10000, 9000, … 5000]` — runner-up 1.11×,
  median 1.54×). Under the new rule that album contains no standout, so the
  fixture became an actually-standout album (50,000 → 500 → … → 100) to keep
  testing what its name says.
- `test_popularity_marked_standout_not_listener_gated` asserted the artist
  top-10% marking was exempt from listener gating. It is still exempt from the
  **listener-z** check, but it is now subject to the **ratio** check: a
  catalogue-level marking says nothing about whether the album contains a
  standout. Renamed to
  `test_popularity_marked_is_ratio_gated_but_not_listener_z_gated` and asserts
  both halves.

## Verification

Oracle: a pristine worktree of `origin/develop` versus the same tree with these
edits applied, comparing `FAILED` lists.

| Scope | before | after | new failures |
|---|---|---|---|
| 8 star-rating suites | 32 failed | 32 failed | **0** |
| repo-wide `-k 'popularity or star or single or live or compilation or score'` | 96 failed | 96 failed | **0** |

The 32/96 are **pre-existing** (missing DB fixtures in this environment) and
identical before and after. The new ratio suite contributes 16 passing tests.

## Follow-ups

- The 32 pre-existing failures are a **separate** issue: they are the 5★ tests
  whose expected paths are not implemented in the current `_assign_stars`
  (a high-confidence single at a low score returns 2★, etc.). This change does
  not touch that; it adds a gate downstream of it.
- The `TestZStandoutSourceReVerification` fixtures still use the flat
  `[10000 … 5000]` album to exercise the *listener-z* re-verification, which is
  a different gate and still behaves as documented.
