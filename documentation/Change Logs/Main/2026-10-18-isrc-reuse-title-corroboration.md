# ISRC reuse requires title corroboration (Arion bleed)

**Date:** 2026-10-18
**Scope:** `services/popularity/` — shared by both trees.

## The report

From a scoring review:

> **Arion — Life Is Not Beautiful:** 3 low-listen tracks (The End Of The Fall and
> two 2018 bonus tracks with 214 listens) initially logged Score: 91.7 (frozen).
> **Duplicate ISRC Collision:** All three tracks share ISRC: FIRAN1800001 with the
> massive hit At The Break Of Dawn (42k LF). During album_track_phase, the ISRC
> cache/lookup bled the 91.7 score across all four tracks before the final pass
> reconciled them.

## Root cause — TWO paths reused data on the identity ALONE

Both matched on ISRC / recording MBID with **no title check and no album scope**:

**1. `finalise_stage._sync_isrc_popularity`** — the SQL "freeze":

```sql
FROM (SELECT isrc, MAX(COALESCE(popularity, final_score, 0)) AS max_score,
             BOOL_OR(COALESCE(is_single, FALSE)) AS max_single_status
      FROM tracks GROUP BY isrc) AS source
WHERE target.isrc = source.isrc
```

Three bonus tracks sharing the single's (wrong) ISRC therefore all received its
**MAX score (91.7)** — and, via `BOOL_OR`, were **marked as singles** too.

**2. `popularity_cache_service.get_recording_identity_popularity`** — the cache
copy used the same identity-only rule, so a bonus track could also inherit the
single's 42k Last.fm listeners.

⚠️ **The reuse itself is deliberate and sound** — it exists because the user
asked for it ("if another copy of the same track is found, can it copy the data
from the other track"). An ISRC really does identify a performance. The defect is
that a **WRONG upstream ISRC** (three different songs tagged with one code) was
indistinguishable from a genuine duplicate.

## The fix — require the TITLES to corroborate

* **SQL path:** group by `isrc, LOWER(TRIM(title))` and join on the title key.
  `LOWER(TRIM(...))` is the strongest key available in portable SQL (no user
  function; works on SQLite and PostgreSQL), and an exact-title duplicate is what
  this sync exists for.
* **Cache path:** compare `normalize_for_aggregation` keys. That normaliser folds
  `(feat. X)` / `(Remastered)` / version markers, so a genuine **variant
  spelling** of the same song still corroborates while a different song cannot.

A variant spelling no longer pools across the two albums — it keeps the score it
actually earned. That is the smaller error, and it is the trade the review
explicitly asks for.

## ⚠️ A CONTRACT WAS REVERSED, so six existing tests were REWRITTEN

`tests/test_isrc_recording_identity_reuse.py` deliberately pinned *"the identity
ALONE answers"* — its probes were titled `"By Isrc"`, `"By Mbid"`, `"Legacy"`,
`"Mbid Only"`, `"Isrc Only"`, `"Both"`, i.e. labels chosen to prove the identity
was doing the work. That is precisely the rule this fix removes, so six tests
changed shape:

* each keeps its original PROPERTY (an MBID with no ISRC resolves; an ISRC with
  no MBID resolves; a bracketed `["CODE"]` row is caught by the LIKE arm; each
  SQL arm builds valid SQL alone) but now uses a title that corroborates;
* a NEW test pins the reversal itself — `test_a_different_title_does_not_answer`;
* the reversal and its cause are documented in the docstrings, per the house rule
  "rewrite the test with the reversal documented; never quietly delete it".

The legit pair (`"Song (feat. Someone)"` → `"Song"`) still passes untouched,
which is why the two "control" tests never moved.

## Tests / oracle

`tests/test_isrc_reuse_requires_title_corroboration.py` — **7 tests**: the cache
path (a different song sharing a bad ISRC does not inherit; the same song does; a
`(feat. X)` variant does; a `(Remastered)` variant does) and the SQL path driven
against **real SQLite** (a bonus track does not inherit the single's score or its
single flag; the same title still pools; casing/whitespace still pools).

| | result |
|---|---|
| without the fix | **2 failed** / 5 passed — exactly the two "must not inherit" discriminators |
| with the fix | **7 passed** |

The 5 controls pass on **both** sides, so the feature the user asked for provably
still works.

Regression sweep of the ISRC / cache / finalise / scan suites: **232 passed**,
1 = the documented pre-existing `test_the_identity_is_marked_unambiguous`
(`KeyError: 'title_is_unique'`).

## Files

* `services/popularity/stages/finalise_stage.py` — `_sync_isrc_popularity`
* `services/popularity/popularity_cache_service.py` —
  `get_recording_identity_popularity`
* `tests/test_isrc_reuse_requires_title_corroboration.py` (new)
* `tests/test_isrc_recording_identity_reuse.py` (6 tests rewritten, 1 added)

## Related, NOT changed

`popularity_math.evaluate_log_ratio_deviation` → `apply_log_ratio_audit_to_stored_score`
is the separate mechanism behind the **Red Flag "LB: 0.0"** report (a
`REJECT_LB` verdict sets the LB weight to **0.0**). That is a different audit with
its own calibration question and is tracked separately.
