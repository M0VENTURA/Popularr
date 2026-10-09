# A Finalise pass over an already-scanned album: 4 minutes → three fixes

**Date:** 2026-10-09 · **Area:** `scan` / `popularity`

## Reported

> I started this scan after running one already, so it should have moved past
> it fairly quick, but still took almost 4 minutes to progress one album that
> had all recent scan data using a finalize scan.

Config in play: **Full Scan Rescan Window 0**, **Singles Scan Window 7**,
**Singles Old-Album Window 100**, **Skip Unchanged Albums True**, **Run Singles
Detection on Skipped Albums True**, **Mature Track Freeze Cutoff 2 years**.

## Reading the log

Birds of Tokyo (2010, 11 tracks, every track mature and therefore frozen):

| time | line | window |
|---|---|---|
| 18:48:53 | `[POPULARITY] Album 1/7 (singles): … (11 tracks)` | |
| 18:49:38 | first `[TRACK] 🎵 …` | **~45s of nothing** |
| 18:51:08 | `[SCAN] section completed section='album_track_phase' elapsed_s=89.201` | **89s** |
| 18:51:51 | `Album file tags synced files_updated=0 corrections_recorded=11` | **40s** |

`elapsed_s=89.201` ending at 18:51:08 puts the track phase's start at
18:49:38.8 — so 45 seconds elapsed **after** the album header and **before**
any job ran. Of the 11 tracks, 4 logged `(cached)` at 0.0s and 7 logged
`Single: LOW [Discogs: ✖, MB: ✖]` at **30–58s each** — two batches of four
on a 4-worker pool, which is the 89s.

## Three defects

### 1. A finalise pass asked two questions and threw the answers away

`_pop_due` was computed from **two** `was_album_scanned` lookups
(`popularity`, then `combined`) and the very next block did:

```python
if _mode_finalise:
    _pop_due = False
```

A finalise pass must never touch a score that exists, so the result was
always discarded — but the lookups still ran, on every album, between the
header line and the first track. The decision is now extracted to
`scan_stage_runner._singles_popularity_due(..., finalise=)` which returns
`False` **before** touching `scan_history`.

### 2. `was_album_scanned` could not use its own index

The query filtered with `LOWER(COALESCE(artist, ''))` /
`LOWER(COALESCE(album, ''))`. Those are EXPRESSIONS, and
`idx_scan_history_scope (scan_type, artist, album, status, started_at DESC)`
can only serve the leading `scan_type` equality — so every row of that scan
type was read and filtered (and when the type is a large slice of the table
the planner seq-scans the whole table instead). It runs **three times per
album**: the skip check, plus the two lookups from defect 1.

`was_album_scanned` now runs an **exact match first**, which constrains the
whole index prefix plus the range on `started_at`, so PostgreSQL answers it
from the index alone. The tolerant statement is unchanged and still runs as
the fallback, so differing casing or an edition-marker rewrite still resolves.

The cutoff also moved from `NOW() - (:days * INTERVAL '1 day')` to a Python
`timedelta` — it matches the `utcnow()` timestamps `record_scan` stamps, and
it parses on every dialect, which is why the helper could finally be tested
against the SQLite test engine at all.

### 3. "Not a single" was the one verdict that could never be cached

```python
_sd_has_evidence = bool(track.get("is_single")) or any(matched source)
_sd_fresh = _sd_age_ok and _sd_has_evidence
```

`single_detection_last_updated` is written only when detection **completed**,
so its presence already means "assessed" — but the extra `and _sd_has_evidence`
demanded a POSITIVE result. The commonest outcome, "this is not a single", is
therefore never cacheable, and every pass re-ran Discogs + MusicBrainz for
those tracks: exactly the 7 tracks × 30–58s in the log.

It also disagreed with the album-level `Skip Unchanged Albums` gate in the same
codebase, which has always asked only whether the timestamp exists.

The decision is now `popularity_cache_policy.singles_detection_is_fresh()`,
sitting beside `should_freeze_track` / `should_use_cached_score` /
`get_cache_duration_hours`, and it tests **freshness only**:

- no timestamp → not fresh (so an ERROR, which never stamps one, still retries)
- older than `get_cache_duration_hours(year)` (168h for a 2010 track) → not fresh
- otherwise → fresh, whatever `is_single` says

## What this does to the numbers

- **45s gap**: the two discarded lookups are gone, and the surviving skip
  lookup is index-served.
- **89s track phase**: on any pass that reaches an already-assessed album the
  negative verdicts come back `(cached)` instead of hitting two providers. The
  7 tracks in this log were re-derived only because nothing had ever stored a
  usable verdict for them — that first assessment is unavoidable, but it is now
  a one-off inside the TTL rather than a per-pass cost.
- **40s tag sync**: not addressed — see below.

## Not fixed / worth knowing

- **The ~40s file-tag sync wrote nothing** (`files_updated=0`,
  `corrections_recorded=11`, `perfect_match=False`). It re-reads all 11 files,
  runs `_fetch_artist_genres` (two `artist_tags`/`artist_metadata` queries),
  does `_resolve_mb_release` → **a MusicBrainz `get_release` for every album on
  every sync**, then records 11 conflicts for the corrections page. Every one
  of those is defensible on its own; together they are 40s per album per pass.
  Worth its own pass — the cheap first question is whether the MB release
  fetch is hitting the HTTP cache.
- **`features.run_singles_on_skipped_albums` is a dead option.** It is on the
  Config page (both trees) and in `USER_GUIDE.md`, but **nothing in
  `services/` reads it** — `grep run_singles_on_skipped_albums` across all
  Python returns only the template and the guide. So "Run Singles Detection on
  Skipped Albums" currently does nothing. Wiring it would make scans slower,
  which is the opposite of this report, so it was left alone for a decision:
  implement it, or drop it from the Config page.
- **`record_scan`'s completion UPDATE is the same index class of problem**
  (`artist IS NOT DISTINCT FROM …` with no `album` constraint, then
  `ORDER BY started_at DESC`) and runs once per album. It was left untouched
  because it reads only that scan type's `started` rows and it is the most
  heavily tested line in the file.

## Tests

`tests/test_finalise_scan_already_assessed_speed.py` — 19 tests:

- finalise returns `False` **with zero** `was_album_scanned` calls; a
  non-finalise singles pass still calls it; window `0` short-circuits
- `was_album_scanned`: exact hit, case-insensitive fallback, miss, expired
  window, wrong scan type, `days=0`, and the exact-before-tolerant ordering
  (with the SQL scoped out of the `INTERVAL` assertion, because the docstring
  quotes the retired form)
- `singles_detection_is_fresh`: fresh negative verdict, no timestamp, expired,
  per-age TTL (168h vs 24h), string timestamps, unparsable timestamps, and a
  positive verdict as a control
- wiring: `track_stage` delegates and `_sd_has_evidence` is gone from the code;
  the runner passes `finalise=_mode_finalise`

`tests/test_finalise_scan_mode.py::test_a_stored_score_is_never_window_refreshed`
was **rewritten, not deleted**: the invariant it guards is unchanged, but it
moved into `_singles_popularity_due`, and the old assertion was a source-text
window that could not tell "guarded before the lookup" from "discarded after
it". It now calls the decision with `was_album_scanned` patched to raise.

**Oracle:** reverting the four source files → **18 failed** (17 in the new
file + the rewritten one), restored → **55 passed**.

**Sweep:** 36 test files grepping the changed modules, one pytest process each,
baseline (source reverted) vs changed: **0 new failures**; 19 baseline-only
failures = the oracle's own discriminators. 42 failures on both sides.

**Pre-existing (verified on a clean baseline):**
`tests/test_scan_history_completion.py` (2) — `record_scan`'s completion
statement uses `EXTRACT(EPOCH FROM …)`, PostgreSQL-only, so the write is
swallowed by its own handler on the SQLite test engine.

`import app` → 399 routes.
