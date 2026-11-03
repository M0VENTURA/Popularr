# Queue processes searches again after a restart

**Date:** 2026-11-02
**Area:** download queue worker / scheduler
**Files:** `services/scheduler/scheduler_service.py`,
`services/queue/queue_orchestrator.py`, `services/queue/queue_worker.py`,
`tests/test_queue_starvation_visibility.py` (new)

## Report

> The queue and search queue isn't showing any search attempts since the
> server was restarted.

`activity.log` showed `event='queued'` entries but no `event='searching'`
lines: items were inserted, nothing ever claimed them.

## Cause

Both the standalone `services.queue.queue_worker` (started by
`entrypoint.sh`/systemd) and the APScheduler `download_queue_processor` job
were registered. They serialise on a **non-expiring PostgreSQL advisory lock**.
When the redundant scheduler cycle hung — a filesystem move inside
`check_completed_downloads` is the reported shape — it held that lock for ever,
so the standalone worker logged "another queue cycle holds the lock" at DEBUG
and skipped every cycle. No searches, no visible reason.

A second gap hid the other failure mode: when every in-flight slot was busy,
`process_next_batch` returned `throttled` at DEBUG level. A full queue looked
exactly like a dead one.

## Fix

1. **One driver when one is managed.** `entrypoint.sh` already exports
   `SPTNR_QUEUE_PROCESSOR_MANAGED_EXTERNALLY=1`; the scheduler now honours it
   and removes the redundant `download_queue_processor` job. The watchdog job
   remains, so a dead standalone worker still falls back to an in-process cycle.
2. **A hung maintenance hook cannot hold the cycle lock.** Each hook runs in a
   worker thread with `QUEUE_MAINTENANCE_HOOK_TIMEOUT_SECONDS` (default 120s).
   On timeout the hook is skipped, the batch runs, and the hook is not retried
   for `QUEUE_MAINTENANCE_HOOK_RETRY_SECONDS` (default 600s) so it cannot leak a
   thread every cycle.
3. **Visible reasons.** Startup now writes an activity line with
   `queued`, `searching`, `downloading` and `ready` counts. A saturated queue
   with ready work logs, at most once every 5 minutes:
   `N queue item(s) ready but every in-flight slot is busy
   (searching=X, downloading=Y, max=Z)`. A cycle that fetched ready items but
   started none logs a warning with the fetched/skipped counts.

## Tests

`tests/test_queue_starvation_visibility.py`:

* the external-worker flag disables the redundant scheduler job;
* a hung maintenance hook is abandoned after the timeout, and the next cycle
  skips it instead of starting another thread;
* saturation is reported to the activity log with the in-flight counts, and the
  warning is rate-limited.
