# 2026-10-12 — Queue search: early termination, wait shrinking, weekly persistent-miss retries

## Report

An external review of the queue + search logs (`queue (1).log`, `search (1).log`):

1. **Searches take 7–9 minutes for tracks that find nothing** — `0 results in 459s`, `535s`, `534s`, `408s`.
2. **Hopeless tracks are hammered day after day** — Phil Jamieson / JENNIE / Gothminister entries re-searched daily.
3. Search concurrency constrained by slskd `429 Only one concurrent operation is permitted`.
4. Cleanup working too hard (`TimedOut`/`ResponseLimitReached`/stuck searches cancelled/cleanup budget exhausted).
5. Lock-contention log line should be DEBUG.

## What was verified, item by item

| # | Claim | Verdict |
|---|---|---|
| 1 | Long searches | **Real.** Worst case = 150s primary + up to 20 fallbacks x 20s ≈ 550s. `search_and_filter` waits the FULL deadline of an empty search — slskd keeps empty searches `InProgress` until they time out; only the 180s stale-search cleaner cancels them. |
| 2 | Hopeless tracks daily | **Real.** `_DEFAULT_BACKOFF_TIER_HOURS = (4, 12, 24)` clamps flat; a never-findable track retries every 24h forever. The analysis proposed a `UNAVAILABLE` state; **Aaron chose the escalation path** ("use 1, but not a week, just until the following Thursday evening — Friday morning the new tracks get released"), which preserves the pinned `TestSearchBackoffNeverAbandons` never-abandon design. |
| 3 | 429 concurrency | **Already mitigated.** `SlskdService.start_search` retries the `only one concurrent operation` 429 with backoff (0.8–2s); serialization is inherent to slskd's API. No change. |
| 4 | Cleanup workload | **Real root cause:** the queue stops polling a search but never cancels it — every search lingers until the stale-search cleaner picks it up. Fixed by cancelling our own search when polling ends. |
| 5 | Lock log → DEBUG | **Already shipped** in `3cc48aa3` — both lock lines are `logger.debug`. No change. |

## Changes

### Search duration / early termination (Config → **Queue Search Tuning** card, both trees)

New `queue` config keys (typed getter `helpers.config_helpers.get_queue_search_tuning()`):

- `search_max_wait_seconds` (150) — primary-query ceiling.
- `fallback_search_max_wait_seconds` (20) — fallback-query ceiling.
- `search_empty_abort_seconds` (90; **0 disables**) — inside `SlskdService.search_and_filter`: a search that has streamed ZERO results for this long is cancelled (`Empty search cancelled early`) and the caller moves to the next query. Only bites the primary (>90s) — fallbacks are ≤20s.
- `search_empty_shrink_after` (4; **0 disables**) — once this many CONSECUTIVE empty queries have returned nothing, the remaining queries wait only `search_shrunken_wait_seconds` instead of the fallback ceiling. Pure helper `_search_query_wait(...)` extracted for tests.
- `search_shrunken_wait_seconds` (8).

Worst-case hopeless track: 90s (primary aborted) + 3×20s + 17×8s ≈ **4½ min** vs ~9 min before; most real tracks are unchanged (they find something early and break).

### Eager search cleanup (fixes "cleanup working too hard")

`search_and_filter(..., cancel_when_done=True)` now cancels the just-polled search in a `finally` block. The queue never re-polls a finished search, so each one leaving a live slskd search behind was exactly what the 180s stale-search cleaner (`clear_stale_searches`) had to clear — with its `budget_seconds=6` cap ("cleanup budget exhausted"). Only production caller is the queue (`download_pipeline_service`); manual-search flows poll their own searches and are untouched. Opt-out via `cancel_when_done=False`.

### Persistent-miss retries → weekly calendar slot (Config → Queue Retry Windows)

After the escalating backoff tiers (4h/12h/24h) are spent, `_schedule_search_retry` now schedules the next attempt on ONE slot per week instead of repeating every 24h:

- New `queue` config: `miss_retry_weekday` (default **3 = Thursday**) + `miss_retry_time` (default **"18:00" UTC**) — the evening before Friday's release day, when fresh uploads of a previously-unfindable track appear.
- New generic `_next_weekday_slot(now_utc, weekday, clock)` — strict-future arithmetic; the pre-release rescan day (06:00) now delegates to it, so both weekly retries share ONE implementation.
- The item STILL returns to the queue automatically (`status="backed_off"`) and is **never parked or abandoned** — the pinned `TestSearchBackoffNeverAbandons` invariant survives (verified: 27 passed).
- Note says `persistent miss, retrying Thursday … local` so the search log explains the weekly gap instead of looking stuck.

## Tests

`tests/test_queue_search_early_termination.py` (35):

- wait-ceiling helper: primary/long-first/fallback, shrink after threshold, shrink disabled by 0, shrunken never exceeds normal, 1s floor.
- config getter: defaults, overrides, **0 preserved as disabled** (a `or default` style would silently re-enable the abort).
- `search_and_filter`: empty search aborted early + cancel calls recorded; late result preserved; abort disabled at 0; `cancel_when_done=False` leaves the search.
- `_next_weekday_slot`: later-today, passed-today→next week, exact-time future, clock override, clamp vs parse-fallback, pre-release delegation unchanged (control).
- miss-slot config defaults/override/fallback.
- `_schedule_search_retry`: tiers below ladder, weekly slot at/above ladder, never-terminal at count 99, EVERY miss count (0..10_000) still schedules a future window.
- Config page surface (both trees): the 7 new ids + labels render; both collectors read them.

**Oracle** (3 source files stashed, tests kept): **26 failed / 9 passed** — the passers are the controls (config-page surface, tier backoff, never-terminal, pre-release slot unchanged). Restored → **35 passed**.

**Regression sweep** (12 related suites, one process per file): all green — `test_queue_retry_chain_wiring` 27, `test_queue_search_miss_and_slot_fallback` 16, `test_slskd_filename_layout_matching` 30, `test_queue_zero_track_false_success` 22, `test_queue_cycle_maintenance_is_locked` 6, `test_queue_counts_match_the_rendered_rows` 14, `test_clear_imported_records` 20, `test_download_import_mbids_and_disc` 14, `test_terminal_queue_rows_do_not_block_requeue` 31, `test_scan_windows_and_scheduled_scans` 38, `test_interlude_lb_config_page` 3, `test_queue_processor_status` 32.

`node --check` ×2, Jinja parse ×2, `import app` → 399 routes.

## Notes

- Config-page contract satisfied: every new key appears on the Config page (live + test_site) and in both collectors; numeric keys use `parseNumber` so `0` (a legal "disable") survives.
- Search-miss backoff tiers (`search_backoff_hours`) are unchanged; the weekly slot is what happens AFTER them.