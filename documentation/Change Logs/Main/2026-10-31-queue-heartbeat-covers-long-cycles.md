# Queue liveness now covers the whole cycle (and search misses say why)

**Date:** 2026-10-31
**Area:** `queue` / `downloads`
**Files:** `services/queue/queue_heartbeat.py`,
`services/queue/queue_worker.py`, `services/scheduler/scheduler_service.py`,
`services/queue/queue_diagnostics_service.py`,
`services/downloads/download_pipeline_service.py`,
`tests/test_queue_processor_status.py`

## Reported symptom

> The queue doesn't seem to be working too often.

The log showed the processor restarting (`[PROCESSOR_RESTART]`), a worker
startup resetting an abandoned `searching` row, and long automatic searches.

## Why it looked stopped while it was working

A queue cycle is a **batch** of item searches, processed one after another.
The automatic search log lines in the same period show a single track taking
**35–145 seconds**, so a batch routinely runs for many minutes.

The liveness heartbeat was stamped **only after `process_cycle()` returned**
(`queue_heartbeat.record_queue_processor_cycle`). Its staleness window is
`max(60s, worker_interval × 3)` — 90s for the default 30s interval. So for the
whole of every long batch the processor read as **STOPPED**: the dashboard
offered "Restart Processor" on a live queue, and the auto-restart watchdog
(`queue.auto_restart`, enabled by default) could kick it.

## Fix

* New `queue_heartbeat.queue_processor_alive(driver)` context manager:
  stamps `running` **immediately**, refreshes every
  `refresh_interval_seconds()` (half the staleness window) while the cycle is
  in flight, and stops on exit. A cycle that RAISES still removes the stamp on
  the way out, preserving the original "a failed cycle leaves no stamp"
  contract.
* Used by all three drivers: the standalone worker, the APScheduler tick, and
  the Restart button's one-off kick.
* The APScheduler tick now has a **single-flight guard**. APScheduler's
  `max_instances` did not apply because the tick returns as soon as it spawns
  its thread; a slow cycle therefore got one new thread per interval, all
  waiting on the cross-process cycle lock. At most one in-process cycle thread
  now exists at a time.

## Also: the search log now says WHY candidates were rejected

`no_qualifying_result` carried only the candidate/query counts. The reject
breakdown (`year_mismatch`, `variant_mismatch`, `no_artist_evidence`,
`title_mismatch`, `album_mismatch`, `length_mismatch`, `below_floor`) and the
top score live only in the warning line. The automatic search event now
appends them, e.g.:

```
no_qualifying_result: candidates=112, queries=4, top_score=38.0,
rejected={'below_floor': 109, 'album_mismatch': 3}
```

That makes a too-strict gate or a parsing problem distinguishable from a
genuine "nothing available".

## What this does not change

* Items that fail a search still enter the retry/backoff ladder, so long gaps
  between searches are expected idle time between retries, not a dead
  processor.
* The batch loop still processes items sequentially (bounded by
  `max_in_flight`), so throughput is one search at a time.

## Verification

`tests/test_queue_processor_status.py`:

| Test | Pins |
|---|---|
| `test_the_heartbeat_is_fresh_while_the_cycle_is_running` | ⭐ status is `running` mid-cycle, not only after it |
| `test_a_raising_cycle_still_leaves_no_stamp` | the failed-cycle contract is preserved |
