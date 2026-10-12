# Audit — the known-red test areas (singles detection + migrations)

**Date:** 2026-10-12. **Baseline:** `origin/develop` = `e9d38ab3`.
**Scope:** the ~36 pre-existing singles-detection failures + the 6 migration-chain
(011-era) failures that keep showing up in every sweep.

## Bottom line

**All 37 are PRE-EXISTING at `origin/develop` — ZERO regressions from today's
slskd / artist-gate / artist-metadata changes.**

But they are NOT a monolith. They split into **five distinct classes**, and only
two of them are "tests are stale". One is a **test-harness duplicate-keyword
bug**, one is a **deliberate behaviour change** the tests weren't updated for,
and one (the migration version pin) is a **hard-coded stale expectation**. None
of them indicates a PRODUCTION defect, but all five deserve different fixes.

## The five classes

### A. Stale `determine_final_status` signature (the biggest group)

The service signature changed (commit `5f197df5` → the single_detection split).
The OLD API took per-source booleans; the CURRENT API takes source COUNTS:

```
OLD: discogs, musicbrainz, discogs_video, lastfm, mb_video, mb_compilation,
     radio_edit, popularity, album_mean, has_metadata, date_match, ...
NEW: high_sources, medium_sources, metadata_medium_sources, z_standout,
     discogs_promo, musicbrainz_promo (the booleans are gone)
```

Tests still pass `radio_edit=`/`has_metadata=`/`lastfm=`/`discogs_video=`
→ `TypeError: determine_final_status() got an unexpected keyword argument …`.

**This is NOT a mechanical translation.** I verified two representative cases:
feeding the equivalent source-counts to the new function gives OPPOSITE
verdicts from what the old function produced (`title-track low-z weak-source`:
old `medium` vs new `none`; `medium-z` case inverted too). The new engine
(rework in the singles/popularity split) deliberately changed the 
title-track-boost and metadata-poor rules. **Rewriting these tests = asserting
the OLD rules against the NEW engine and will fail by design** — the correct
move is to REWRITE the assertions to the new semantics + document why the old
expectations are obsolete, not to "fix the calls".

Affected: `test_compilation_single_detection.py` (2), `test_lb_realism_and_single_detection.py` (2), `test_single_detection_overzealous.py` (7), `test_single_detection_popularity_standout.py` (7), `test_single_detection_title_track.py` (10).

### B. Duplicate `use_advanced_detection` KWARG (harness bug)

`test_single_detection_source_gates.py::_detect()` passes
`use_advanced_detection=False` as a keyword, AND the individual tests ALSO pass
`use_advanced_detection=True` via `**kw` → `TypeError: got multiple values for
keyword argument 'use_advanced_detection'`.

The parameter is checked in production (`if use_advanced_detection:` twice) so
it's LIVE, not dead — but the two tests never actually run their assertions.
**Fix**: remove the hard-coded `use_advanced_detection=False` from `_detect()`
and let each test pass its own. 2 failures.

### C. Video/source gates not tested as written (part of B's file)

`TestDiscogsVideoConfigurable` uses `_detect(mb_client=client,
use_advanced_detection=True)` which collides with B. Once B is fixed, these
test the "discogs_video after MB confirm" config paths. 2 failures, same fix
as B.

### D. Deliberate behaviour change: user-marked live track → 5★ (not 4★)

`test_live_4star_requires_single.py::TestLive4StarRequiresSingleDefault::
test_user_marked_live_track_reaches_four` expects **4** but the production
`_assign_stars` returns **5** for `single_confidence == "user"`:

```python
if single_confidence == "user":
    track["_global_5star_locked"] = True
    return 5     # <-- user override, since the user-override work
```

This is the DOCUMENTED user-override contract ("A user override is never
demoted — it is an explicit instruction", `finalise_stage.py`). The test was
written before the user-override semantics and its expectation is stale.
**Fix**: update the expectation to 5 (the live-4★ gate is about ALGORITHMIC
tracks; a user-marked single is exempt). 1 failure.

### E. Migration harness — two sub-problems

`test_migrations_idempotent.py` (6 failures):

1. **Stale head pin** (4 failures): every test asserts
   `version == "011_add_tracks_album_artist"` — the chain is now at
   `019_upcoming_releases_type_in_key` (added `018` + `019` after 011). The
   **assertions are hard-coded to an old head**, not the migration behavior.
   The 011-era work (bare `tracks` upgrade, album_artist backfill) is genuinely
   tested and passing; the only defect is the literal `"011..."` string.
2. **`NoSuchTableError: download_queue`** (2 failures: `bootstrap_built` /
   `partially_bootstrapped`): revision `017_add_download_queue_release_group`
   inspector-guards `download_queue` columns; the harness pre-creates some
   tables but NOT `download_queue`, so the inspector raises. Production always
   has the table (bootstrap ensures it), so this is a **harness fidelity gap —
   the bootstrap DDL that the harness replays does include it as of the
   `17_/18_` additions? (verify) -- at minimum, `TABLES_TO_ENSURE` has it.**
   The migration chain is correct; the harness must create the queue table
   before stamping at 008/001.

**Fix**: replace every `"011_add_tracks_album_artist"` head pin with the
current head (`019_upcoming_releases_type_in_key`), or better, import the head
from the migration chain (no magic string to rot); add `download_queue` to the
partial-DDL harness lists.

## What to do with them

| Class | Kind | Fix |
|---|---|---|
| A (26) | Stale-signature tests asserting OLD rules | REWRITE assertions to new semantics + document; do NOT mechanically re-map calls |
| B+C (2+2) | Duplicate kwarg harness bug | Remove `use_advanced_detection=False` from the shared `_detect()` helper |
| D (1) | Deliberate user-override→5★ change | Update expectation 4→5; the live-4★ gate is algorithmic-only |
| E1 (4) | Hard-coded dead head pin | Import the real head / update the string |
| E2 (2) | Harness missing `download_queue` | Pre-create the queue table in the harness |

Total 37. Recommend fixing in the order B+C (trivial), D, E1, E2 — then A as a
separate, deliberate re-assertion pass (it's a semantic rewrite of the singles
tests to the NEW engine, with the old expectations documented as superseded in
each docstring per the standing "rewrite the test with the reversal
documented" rule).

## Verification

- All 37 fail identically with today's source changes STASHED — verified
  byte-identical failing-id sets on the artist-gate sweep (9 shared pre-existing
  ids) and on the full 37-file enumeration run.
- The 37 were enumerated in one process-per-file run over the 8 related suites
  (singles + migrations), versus the previously-recorded "36 singles + 6
  migrations" (count drift: one distinction above covers the extra).
- None of the 37 exercises the slskd hardening, the artist-metadata
  once-per-artist change, or the batch-alias artist gate — they're all in the
  singles-detection / migration harnesses.