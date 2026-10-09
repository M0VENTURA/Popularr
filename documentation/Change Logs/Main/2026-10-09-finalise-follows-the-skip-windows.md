# A non-forced Finalise scan follows the configured skip windows

**Date:** 2026-10-09 - **Area:** scan behaviour
**Reported:**

> This took 3 minutes to complete. ... The scan was non forced Finalise scan

> **It should follow the config rules for skipping of known scan types.**

## What the log shows

| time | elapsed | phase |
|---|---|---|
| 15:26:12 -> 15:26:15 | 3 s | album stage (`enrich_album` - DB-only in Finalise) |
| 15:26:15 -> 15:26:21 | 6 s | first track: `[MB] shared HTTP client initialization` + the 1 req/s throttle reserve |
| 15:26:21 -> 15:26:40 | 19 s | `album_track_phase` - 19 tracks, singles detection |
| 15:26:40 -> 15:26:45 | 5 s | deferred DB drain + `[TRACK_RESULT]` |
| **15:26:45 -> 15:28:09** | **84 s** | **file-tag sync** (19 files: tag read + rewrite, 47 corrections) |
| | **117 s** | one album - 13 queued, about 25 minutes |

Everything else that could have run in that last window is *off* in a Finalise
pass: full enrichment, post-singles enrichment, cover detection, the recommend
stash and the missing-track recompute (see
`2026-10-08-scan-windows-and-scheduled-scans.md`).

## Root cause

The album skip gates were explicitly exempted for the mode:

    if not force and not album_filter and not _mode_finalise:

shipped with `2026-10-07-dashboard-finalise-scan` on the reasoning that the
per-album file-tag sync IS the point of the pass and the freshness gates would
leave every already-scanned album (i.e. almost all of them) never finalised.
The consequence - which the reported log makes concrete - is that a
**non-forced** Finalise re-visited **every album in the library** regardless of
what Config says, and paid the full track phase + file-tag sync for each.

## The fix

One condition: `if not force and not album_filter:` - the finalise exemption is
gone, and everything inside it already does the right thing for this mode:

| gate | what decides it in a Finalise pass |
|---|---|
| scan type | `_resolve_scan_type` maps `singles_only` -> **`singles`**, which is the history this pass `record_scan`s - so the window reads the rows it writes |
| window | **Singles Scan Window** (`singles_skip_days`, default 7), or **Singles Old-Album Window** for albums past `old_album_age_months` |
| unchanged | **Skip Unchanged Albums** -> every track already carries `single_detection_last_updated` => skip |
| `0` | still means *always run* |
| Force | still bypasses the gates entirely |
| completeness | `is_album_incomplete` still forces an album through - a scan never leaves an album half-built |

For the reported album (19 tracks, all with verdicts, `Skip Unchanged Albums =
True`, Singles window = 7 days) the gate now skips it outright: **~0 s instead
of 117 s**, and the remaining 12 albums cost nothing either.

**Deliberate trade-off:** an album skipped here does **not** get its file-tag
sync on this pass - that is what "skip" means, and it is the rule that was
configured. Force (or a `0` window) still reaches the sync for every album.

## Tests

* `tests/test_finalise_scan_mode.py` - `test_the_skip_gates_are_bypassed`
  **rewritten** as `test_the_skip_gates_follow_the_config` (the old assertion
  pinned the now-removed exemption, and fails without this change), plus a new
  `test_the_finalise_pass_reads_the_history_it_writes` asserting
  `_resolve_scan_type({"singles_only": ..., "finalise_only": ...}) == "singles"`
  - otherwise the Singles window would be checked against another scan's
  history.
* `tests/test_album_scan_budget.py` - `test_forced_mode_really_cannot_bypass_the_budget`
  indexes the skip gate; re-anchored on the new string (its intent - *Force*
  cannot bypass - is unchanged).
* **Also fixed (pre-existing, verified failing at `HEAD` without these
  changes):** `test_the_endpoint_stays_database_only` used a fixed
  `routes[idx: idx + 1400]` window that `266ed296` pushed stale - the default
  branch fell outside it. Now a structural window (function start -> next
  `@album_bp.route`) that asserts the two branches separately: the default path
  stays DB-only, the one recompute sits behind `if refresh:`.

**Oracle** - `scan_stage_runner.py` reverted (tests kept): **2 failed, 88
passed**, exactly the two rewritten tests; restored: **90 passed**.

**Sweep** - 55 test files touching `scan_stage_runner` / `finalise` /
`scan_history` / `was_album_scanned` / the window keys, run **one process per
file** (running them together hits a known Windows access-violation between a
background scan worker and `conftest`'s schema recreation): baseline **116** ->
changed **113**. The only delta is `test_full_scan_startup_fix.py`, whose 2
failures are pre-existing (`TestPopularityRunRouteStaleRowSelfHeal`) - re-run
3x with the change and 3x without, **identical FAILED/FAILED/FAILED both ways,
no crash** => **0 regressions**.

`ast.parse` clean on the runner.

## Files

- `services/popularity/scan_stage_runner.py`
- `tests/test_finalise_scan_mode.py`
- `tests/test_album_scan_budget.py`
- `documentation/Change Logs/Main/2026-10-07-dashboard-finalise-scan.md` (superseded note)
- `documentation/Change Logs/Main/2026-10-08-scan-windows-and-scheduled-scans.md` (superseded note)
