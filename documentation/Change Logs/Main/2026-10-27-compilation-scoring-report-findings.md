# Four scoring defects found by auditing two exported popularity reports

**Date:** 2026-10-27
**Area:** `popularity` (scoring, compilation rating, ListenBrainz component)
**Files:** `services/popularity/popularity_math.py`,
`services/popularity/scan_stage_runner.py`,
`services/popularity/stages/track_step.py` → `track_stage.py`,
`services/popularity/stages/finalise_stage.py`,
`tests/test_compilation_scoring_report_findings.py` (new),
`tests/test_compilation_tier_guards.py`, `tests/test_compilation_more_5star.py`,
`tests/test_popularity_marking.py`

## Evidence

Two exported reports were audited and compared:
*Various Artists — Triple J's Hottest 100 (1996)* (100 tracks) against
*Cult of Luna — In the Shadow of Your Shadow* (2 tracks, single artist).

Each finding below is stated with the number that exposed it.

---

## 1. `final_score` on a compilation measured rank WITHIN each artist

**Measured.** Every artist's tracks clustered into their own band, and the bands
bore no relation to actual audience:

| Artist | mean Last.fm listeners | mean `final_score` |
|---|---|---|
| The Prodigy | 962,642 | **97.45** |
| Bush | 553,215 | **84.54** |
| The Smashing Pumpkins | **1,888,518** | **66.92** |
| Powderfinger | 16,977 | **66.99** |
| Beck | 525,260 | **60.27** |

Smashing Pumpkins averaged 1.89M listeners and scored 66.92; Powderfinger
averaged 17k and scored 66.99 — **111× the audience for 0.07 of a point**. Beck
(525k) scored 60.27 while Bush (553k) scored 84.54. Sorting the album by
`final_score` produced **25 adjacent star inversions**; the reported order ran
67.48 → 97.52 → 64.09.

**Cause.** `_apply_track_artist_relative_normalization` remapped each track
against **its own artist's catalogue** (`_load_track_artist_scores`), falling
back to the album band only when an artist had fewer than 3 stored tracks. Two
reference populations on one album, so the outputs were not comparable — and it
wrote the result into **both** `popularity_score` and `final_score`.

**Fix.** `popularity_score` keeps the remap (it is what the compilation star
tier reads — "is this a hit for THIS artist"). `final_score` is **forced back to
the pre-remap blend** so the whole album shares one scale. The old value is
preserved on the row as `_artist_relative_score`.

⚠️ It is *forced*, not merely left alone: a frozen row can still carry a
per-artist `final_score` written by an earlier scan, so `setdefault`-style
preservation would have kept the incomparable value forever.

⚠️ The report's own comment claimed *"within one album the two values are
monotonic in each other (the remap is order-preserving)"*. **That invariant was
false** — the report only read sensibly because it sorts by `raw_score`.

---

## 2. The compilation 5★ guard OR'd a scale-free score against absolute reach

```python
_clears_absolute = (
    (_min_score > 0 and score >= _min_score)
    or (_min_listeners > 0 and raw_lf >= _min_listeners)   # ← unreachable
)
```

`raw_lf` is absolute reach; `score` at that point is `popularity_score`, which
has **already been through the per-track-artist remap and is therefore
scale-free**. A scale-free score clears 55 almost always, so the listener bar
never enforced.

**Measured.** Pearl Jam's "Leaving Here" (19,396 Last.fm listeners) held 5★ on
score 70.27. 26 of the 100 tracks were 5★, and "Ballad of the Skeletons" earned
**4★ from 26 listeners**. The guard only ever fired for tracks failing *both*
bars (e.g. "Waiting for a Star to Fall", 175,111 listeners, score 49.5).

**Fix.** Conjunction — a top-tier track must be a hit **for its artist AND in
absolute terms**. Each bar stays independently disableable by setting it to 0,
so the old looser behaviour is one config edit away instead of an accident. The
gate log now names which bar failed.

**Effect on this album:** 26 five-star tracks → the genuine hits keep 5★ and the
rest are gated to 4★.

⚠️ **Contract reversal** — four assertions in `tests/test_compilation_tier_guards.py`
pinned the `or` semantics, including one that explicitly argued *"either bar is
enough… the two bars are alternatives, not a conjunction"*. Rewritten with the
reversal and its measurements recorded in each docstring.

---

## 3. `listenbrainz_score` stored 0.0 beside a real listen count

| Track | LB listens | LB users | LB score | formula |
|---|---|---|---|---|
| Comedown | 500,794 | **0** | **0.0** | 91.19 |
| Swallowed | 545,750 | **0** | **0.0** | 91.79 |

The other 16 rows carrying an LB count matched `16·log10(n+1)` **exactly**, so
these two were not a rounding artefact. `listenbrainz_score` is a pure function
of `listenbrainz_listens`, so the two columns can only disagree if they were
written at different times: the score during scoring, and the count backfilled
later by the album-tracklist prefetch. `popularity_frozen` then stops any
rescore, making the contradiction permanent. Each row lost ListenBrainz's whole
30% weight from its blend.

**Fix.** `reconcile_listenbrainz_score(listens, score)` — applied in
`track_stage` immediately before the payload returns, on **both** the fresh and
the stored branch, writing to `update_payload` (the dict that is persisted) as
well as the in-memory result.

⚠️ It **recomputes** rather than **discards**, deliberately. The listen count is
the real measurement — throwing away 500k listens to "reject a malformed row"
would lose more signal than it protects. It can only ever *raise* a score (a
positive score is returned unchanged), so it is safe to run on every row.

---

## 4. `listenbrainz_score` meant two different things

`lb_score = max(lb_log, lb_percentile * 100)` — the percentile is **scale-free**
("how does this sit within the album"). Persisting that under
`listenbrainz_score` made one column mean two things depending on album size:

* *In the Shadow of Your Shadow* (Cult of Luna, 2-track album): 438 listens →
  percentile 0.75 → stored score **75.0**, where the log score for the same 438
  listens is **42.28**;
* every other track in the collection stores the log score.

**Fix.** The returned `listenbrainz_score` is now always `lb_log` (absolute). The
percentile still drives the **blend** — a small album's best track deserves its
standing — it is simply no longer reported as an absolute figure.

---

## Comparison with the non-VA scan

Cult of Luna is mostly **healthier**:

* 2 tracks → `len(raw_scores) < 3` → the remap is **skipped entirely**, so
  `final_score` is the raw blend (69.2 / 51.6). Correct.
* Both tracks 3★, no rank inversion.

The one defect it exposed is finding #4 above — and it is the *only* reason the
two reports disagree about what a column means.

---

## Verification

`tests/test_compilation_scoring_report_findings.py` (17 tests):

| Group | Pins |
|---|---|
| The remap | `final_score` keeps the pre-remap blend; `popularity_score` is still the artist remap; **ordering by `final_score` follows the audience** (the Smashing Pumpkins / Powderfinger inversion); a stale per-artist `final_score` is repaired; a zero-raw row is untouched |
| The tier gate | the reported "Leaving Here" row is gated to 4★; a 300k-listener track keeps 5★ |
| LB reconciliation | the reported 500,794 row is recomputed to the formula; a consistent pair is unchanged; 0 listens stays 0.0; **it can only raise a score**; the result matches `16·log10(n+1)`; the repair reaches `update_payload` |
| LB absolute | a 2-track album no longer reports the percentile; the percentile still drives the blend; no-album-context is unchanged; 0 listens → 0.0 |

* **Oracle:** with the four source files stashed, **11 of 17** fail, plus exactly
  the **4** reversed tier-guard assertions. Nothing else changes.
* **Regression sweep** across `compilation`, `playlist`, `star`, `raw_score`,
  `live`, `single`, `album_type`, `no_undefined`, `no_python_syntax`,
  `bootstrap` — failure sets **byte-identical** to baseline in every chunk.
  `compilation` went **16 → 12** (the four reversals now pass).

### Fixture corrections made along the way

Two tests in `tests/test_popularity_marking.py` were failing on a **stale fake**
(a `_fake_load(track_artist)` that refused the `exclude_album=` keyword the real
loader is called with), and two in `tests/test_compilation_more_5star.py` built
"genuine hits" whose implied reach was only 46,000 listeners — they were
measuring the new absolute guard rather than the artist-catalogue rule they are
named for. Both fixed; the second by giving those specific fixtures explicit
reach rather than changing the shared `_track` helper (which two other tests
depend on at the old scale).

## ⚠️ Not fixed (found in the same audit, separate work)

* `lastfm_playcount = 0` with listeners > 0 on 3 rows — a Last.fm payload
  rejected at ingestion.
* Comma-joined credits never split (`"2Pac, Roger, Dr. Dre"`,
  `"Fugees, Ms. Lauryn Hill"`, `"Barry Adamson, Jarvis Cocker, Atticus Ross"`) —
  all three land in the lowest band, consistent with the artist-catalogue lookup
  failing on the joined string.
* Suspect cover suffixes (predating the 2026-10-26 cover fix):
  `Salvation (St. Christopher Cover)` is The Cranberries' own song,
  `Setting Sun (Greyhaven Cover)` is The Chemical Brothers' own.
* `popularity_frozen = True` on 99/100 rows, so none of this re-evaluates
  without a forced scan.
