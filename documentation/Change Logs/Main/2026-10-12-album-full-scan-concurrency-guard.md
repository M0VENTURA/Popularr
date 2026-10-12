# Album-page "full" scans now pass through the shared popularity-scan guard (2026-10-12)

## Report

One-run log for a **forced** "Various Artists - Alternative 90s" scan:

```
[SCAN] section abandoned (timeout) section='album_track_phase' elapsed_s=900.001 budget_seconds=900.0
[POPULARITY] Album 'Various Artists - Alternative 90s' skipped (exceeded 900.0s budget)
Popularity Scan - no tracks processed: 1 album(s) exceeded their per-album
time budget ... Raise the 'Per-Album Scan Timeout' in Config ...
```

An album that had been scanning for **~5 wall-clock seconds** was abandoned at
`elapsed_s=900.001`. The same log shows a second, interleaved generation of
the same tracks (16:10 "1979" re-processed at 16:26) — evidence of two scan
runs overlapping.

## Root cause — the album "full" trigger skipped the duplicate-run guard

Every popularity-scan trigger in the app is guarded so two scans can never
overlap:

* dashboard `/api/popularity/run` — runtime registry + cross-process
  `is_popularity_scan_active()` + stale-row self-heal (`routes/scan_routes/api.py`);
* scheduled scans — same `is_popularity_scan_active()` (`scheduler_service`);
* artist/album **targeted** scans — `_start_targeted_popularity_scan`
  (`routes/scan_routes/artist_album.py`).

**But the album page's "Full" scan (`scan_type == "full"`) launched
`run_album_pipeline` with a bare `run_async` and NO guard** — the one entry
point that skipped it. `run_album_pipeline` Step 2/3 *is* the popularity
pipeline (`services.popularity.pipeline.run_popularity_scan`), so a second
trigger — a double click, or the album page during a library scan — overlapped
the first:

* both runs wrote the SAME shared `popularity_scan` progress row as running;
* both hit the shared 1 req/s MusicBrainz client (`get_shared_mb_client`) and
  the shared in-process MB LRU/ETag caches → every call ~2× slower;
* the runner's skip-guards saw the *other generation's* freshly-written rows
  as current and self-froze them, so no track looked stale enough to re-score
  fast, and the album crawled at 2 generations of ~1½–2-min tracks against a
  single 900 s budget → `elapsed_s=900.001` for ~5 s of real work.

The `elapsed_s=900.001` itself is genuine — `_bounded_call_report`
(`scan_stage_runner.py:162–205`) joins the thread for `seconds` and measures
`time.monotonic()` from the call's own start. Two overlapping generations
simply made that wall-clock real. `album='Unknown Album'` on cover detection
is the comingled rows' `album` field (prepared copies), not a source of the
abandonment.

## Changes

1. **`routes/scan_routes/_common.py`** — new
   `guard_and_launch_popularity_scan(runner, *args, **kwargs) -> bool`,
   extracted verbatim from `_start_targeted_popularity_scan` (runtime-registry
   guard → cross-process `is_popularity_scan_active()` → stale-row self-heal →
   worker spawn + `set_runtime`). Now THE single guard for every route-level
   popularity trigger.
2. **`routes/scan_routes/artist_album.py`** — the album **and** artist "full"
   branches route through the shared guard (instead of bare `run_async`); the
   targeted branches now call the shared helper too. The in-route private
   `_start_targeted_popularity_scan` is deleted (one definition — the two
   pages can no longer drift). A rejected duplicate flashes "A popularity scan
   is already running — skipped duplicate trigger" and redirects, matching the
   targeted branches.

The album pipeline itself (`services/scanning/pipelines/album_pipeline.py`) is
unchanged — it remains the album-scoped workflow, now started under the same
guard as everything else. `_bounded_call_report`/abandoned-worker machinery is
also unchanged (its log message was honest; the overlap was the trigger).

## Tests

`tests/test_album_full_scan_concurrency_guard.py` (14):

* wiring: album "full" branch → `guard_and_launch_popularity_scan(run_album_pipeline,…)`; no bare `run_async`; artist branch likewise; single-definition drift guard; old private helper gone; the 2 targeted branches still go through the guard;
* behaviour (stubbed runtime): rejects when the in-process registry is live; rejects when a LIVE owner holds the shared state (self-heal must never clear a running scan); **self-heals a stale phantom row and starts**; starts directly when idle; the worker clears the runtime registry in its `finally`.

**Oracle** (revert production only): `test_album_full_no_longer_uses_bare_run_async` +
`test_album_full_passes_through_the_album_pipeline` FAIL; restored → 14 pass.

**Sweep**: scan/album/route suites green except the 2
`test_full_scan_startup_fix::TestPopularityRunRouteStaleRowSelfHeal` and 8
`test_scan_stall_fixes`/`test_async_routes_do_not_block_event_loop` failures —
**byte-identical at pristine `e62fa315`** (SQLite `no such table: scan_states`
harness flake + pre-existing stall-guard source assertions). `import app` OK
(399 routes), `py_compile` clean.

## Note for the user

* The "no tracks processed / exceeded budget" message was **honest** — the
  message itself had already been made truthful (2026-10-05 category); what
  was wrong was the trigger letting two scans overlap. With the guard, the
  album either rescans cleanly or the duplicate click is rejected up front.
* Existing abandoned-album config ("Per-Album Scan Timeout", Config → Scan
  Behaviour) is untouched: `0` disables the budget entirely; `force` does not
  bypass it (unchanged, documented).
* If you kept hitting this, first check you are running a build **after** this
  commit — deployment lags `origin/develop`.