# Refinement: a compilation track with no evidence can no longer hold 4★

**Date:** 2026-10-27
**Area:** `popularity` (compilation star rating), `config`
**Files:** `services/popularity/stages/finalise_stage.py`,
`helpers/config_helpers.py`, `templates/pages/config.html` +
`test_site/templates/Pages/config.html`,
`static/js/config.js`, `static/js/pages/config.js`,
`test_site/static/js/pages/config.js`,
`tests/test_compilation_scoring_report_findings.py`,
`tests/test_compilation_tier_guards.py`

## Context

The four defects from the previous audit landed in `0555bb4`. This change is the
first of the follow-up refinements, and it fixes a hole that fix **left open**.

## The hole: demoting a 5★ to a flat 4★ was only half the job

`0555bb4` changed the compilation top-tier gate from `or` to `and`, so a track
that fails the absolute bar loses 5★. But the code demoted it to a **flat 4★** —
and the score that got it to 5★ in the first place was a **scale-free "#1 of N"
rank**. So a track with no popularity evidence whatsoever still landed exactly one
tier below a rank it had never earned.

Measured on *Triple J's Hottest 100 (1996)*:

| track | final_score | Last.fm listeners | LB listens | single | stars (before) |
|---|---|---|---|---|---|
| Ballad of the Skeletons (Allen Ginsberg) | **0.00** | **26** | 0 | no | **4★** |
| devil may cry (Echochamber) | 44.8 | **713** | 0 | yes | 4★ |
| Do You Feel Alive? (Alchemize) | 35.3 | 1,400 | 0 | — | 4★ |
| KICK BACK (Private School) | 37.0 | 2,800 | 0 | — | 4★ |

A 26-listener track with a 0.0 blend was worth four stars because the album's
online rank table said "#1 of 40" for that artist.

## Fix: 4★ needs its own evidence

Two new config-driven bars, mirroring the 5★ pair and **also a conjunction**
(one bar alone cannot carry the tier — the same reasoning as `0555bb4`):

| key | default | meaning |
|---|---|---|
| `high_tier_min_score` | **45** | the track's blend must reach this |
| `high_tier_min_listeners` | **1000** | …and its Last.fm reach must reach this |

A track at 4★ or 5★ that clears **neither** bar is moved to the **absolute
ladder** — the tier its evidence actually supports — instead of being shaved to a
flat 4★. A 26-listener/0.0-score track lands on 1★; 713 listeners at 44.8 lands
on 3★.

Two implementation details that matter:

1. **The absolute ladder was extracted** into `_compilation_absolute_ladder_stars()`
   so the "no rank available" fallback and this "rank not justified" path are the
   same code and cannot drift apart.
2. **The guard can never raise a tier.** It combines with
   `max(1, min(3, min(comp_stars, ladder)))`, so it only removes evidence it
   could not justify. Setting either bar to 0 disables that bar; setting both to
   0 restores the previous behaviour exactly.

## Config UX

Per the Config-UX contract (§4.1) both bars are on the Config page first, in both
trees, with the client-side JS mapping that persists them. The 5★ listener bar's
help text also corrected — it still said *"Either this or the score clears the 5★
gate"*, which `0555bb4` had made untrue.

## Verification

| check | result |
|---|---|
| `tests/test_compilation_scoring_report_findings.py` | 26 passed (10 new) |
| `tests/test_compilation_tier_guards.py` | 55 passed together with the above |
| **Oracle** | 4 of the new guard tests fail without the implementation |
| **Sweep** (`compilation`, `star`, `popularity`, `config`, `no_undefined`, `no_python_syntax`) | **48 = 48, failure sets byte-identical** |

The oracle covered `config_helpers.py` + `finalise_stage.py` only, so the two
*presence* assertions (config page / JS mapping) passed at HEAD by design — they
guard the config wiring, not the scoring.

### Expectation changes this necessitated

The guard legitimately lowers four existing fixtures, and each was re-derived
rather than adjusted to whatever the code printed:

* `devil may cry` 4★ → **2★**, `Do You Feel Alive?` 4★ → **2★**,
  `KICK BACK` 4★ → **2★** — these fixtures are **non-singles**, and the
  non-single absolute ladder needs a 70 blend for 3★ (35.3 and 37.0 do not reach
  it). `devil may cry` on the SINGLE ladder is 3★, which is the discrepancy
  between the two ladders, not a bug.
* `Big Reach` (40.0 score, 300k listeners) 4★ → **3★** — it clears the 4★
  listener bar but not the 4★ score bar, so it drops to the ladder, which gives
  3★ on the 75,000-listener rung.
* The tier-guard fixture also gained the two new keys. Without them the guard read
  `0` (disabled) and those tests silently stopped covering it — a fixture that
  omits a key is not a test of the shipped default.

## Why the other four refinements were not implemented

Rechecked against the code, three of the five rested on premises that do not
hold, and one is already correct:

1. **"Mirror the malformed-record guard on Last.fm" — not applicable.** The
   ListenBrainz fix recomputes `score` from `listens` because the score is a
   **pure function** of the count. Last.fm has no such relationship:
   `lastfm_score` is `log10(listeners)` or an album z-score, and
   **`lastfm_playcount` is never read by the blend at all** (verified: no
   reference in `popularity_math.py` outside the separate playcount-audit and
   age helpers). The 4,698 rows with `playcount = 0` are a **reporting** defect,
   not a scoring one — synthesising a playcount would invent a number to fix a
   column nothing consumes. It also cannot be "rejected so it re-fetches
   cleanly": `popularity_frozen` is True on those rows, so there is no refetch to
   trigger. It needs its own fix at the ingestion boundary (a `track.getInfo`
   response whose `playcount` is absent), which is where the existing
   `test_search_track_playcount` guard already points.
2. **"Re-normalise provider weights per track" — already true.** The blend
   divides by `sum(active_weights)` (line 902), and a component whose score is 0
   is **not appended** to `active_scores` at all. So a track with `LB = 0` is
   already blended on Last.fm + age alone; `0.0 * 0.30` is never added. The
   67-vs-97 split on that album is the *five-star tier* difference plus the
   separate `has_album_context` floor, not an unnormalised weight.
3. **"Bogus cover suffixes from diacritic-differing artist names" — premise
   inverted.** `_same_artist` already folds Unicode punctuation and diacritics
   precisely so `Lïve` == `Live`; that was fixed before this audit. The real
   Lïve row reads `Lightning Crashes (Live Cover)` because the release's own
   title carries a **"(Live Cover)"** annotation — a *performance type* marker,
   not an attribution — and `_has_cover_annotation`/`_COVER_SUFFIX_RE` treat any
   `(… Cover)` as an attribution worth stripping. That is a distinct defect
   ("(Live Cover)" is not "(X Cover)"), worth its own change.
4. **"Split comma-joined credits" — valid, deferred.** Split on `,`/`&` and try
   the primary artist when the full string misses, with an MBID or whitelist
   guard for names that genuinely contain a comma (`"I, Forlorn"`,
   `"Crosby, Stills, Nash & Young"`). This is a matcher change affecting every
   provider lookup, so it needs its own commit, tests and sweep.
5. **Star inversion within one artist on one album** (`Hail, Hail` 85.73 = 3★ vs
   `Leaving Here` 70.27 = 5★ pre-`0555bb4`) is **fixed** by the conjunction: the
   probe now shows `Hail, Hail` 3★ / `Leaving Here` 4★ — no inversion, because
   neither can reach 5★ without absolute reach.
