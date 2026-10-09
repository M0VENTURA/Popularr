# The Download Queue Processor card no longer sits on "Loading…"

**Date:** 2026-10-09 · **Area:** `queue` / `ui`

## Reported

> Under active queue and monitor, the download processor is stuck on loading
> with a "checking processor status" that never changes, and the restart is
> greyed out. But on the downloads page, it shows as running with a restart and
> stop button.

The **Download Queue Processor** card (included by `pages/downloads/queue.html`
and `pages/downloads/monitor.html` from
`components/search/_queue_status.html`) rendered:

- badge: `Loading…`
- body: `Checking processor status…`
- `Restart Processor`: `disabled`, permanently

## Root cause — two halves of one unfinished port

1. **Backend stub.** `services/queue/queue_diagnostics_service.py::queue_processor_status()`
   was hardcoded:

   ```python
   return _ok(running=False, migrated=False,
              message="Queue processor status not implemented")
   ```

   The endpoint could never say "running", whatever the UI did with it.

2. **Frontend renderer never ported.** `updateProcessorStatusCard(pData)` still
   exists only in `old_system/templates/downloads.html`. Nothing in the current
   tree writes `#processorStatusBadge`, `#processorStatusContent` or
   `#restartProcessorBtn` — and that button ships `disabled` in the markup, so
   it was never enabled by anything.

`restartQueueProcessor()` existed and posted to `/api/queue-processor/restart`,
but that endpoint's whole body was
`os.remove(os.environ["QUEUE_PROCESSOR_HEALTH_FILE"])` — a no-op, because
nothing ever wrote that file. The button did nothing even when it could be
pressed.

## The difficulty: there is no processor process to inspect

The queue is driven by ONE of two interchangeable processes:

| driver | where it lives |
|---|---|
| `services.queue.queue_worker` | started by `entrypoint.sh` / `popularr-queue-worker.service` |
| APScheduler `download_queue_processor` | inside the web process (`services/scheduler/scheduler_service.py`) |

Neither PID is visible to the other, and `services/queue/queue_signal.py` is a
`threading.Event` — in-process only. So "is it running?" has to be answered
with **evidence**, not with a process lookup.

## Fix

**New `services/queue/queue_heartbeat.py`.** Every finished `process_cycle()`
stamps `<state>/queue_processor_health.json` (atomic `os.replace`, so a reader
can never see a half-written file while two processes write it):

- `queue_worker` stamps `standalone-worker` after each cycle
- the scheduler tick stamps `scheduler` after each cycle
- the Restart button stamps `restart-requested`

The stamp is taken **after** the call, so a cycle that raised leaves no
evidence of life — a permanently crashing processor reports Stopped.
`QUEUE_PROCESSOR_HEALTH_FILE` now names that file (it used to be read only so a
restart could delete it).

**`queue_processor_status()`** now weighs two signals:

1. a heartbeat younger than `stale_after_seconds()` — `max(60, 3 ×
   queue.worker.interval_seconds)`, config-driven rather than hard-coded; and
2. an armed in-process scheduler job — the fresh-boot case, where the first
   tick has not fired yet. Read through the new
   `scheduler_service.queue_processor_schedule_snapshot()`, which inspects
   `_SCHEDULER` instead of calling `get_scheduler()`, so a poll every 10
   seconds can never build that singleton or open its SQLAlchemy jobstore.

Otherwise it reports `stopped` and says why. The payload now carries
`running`, `processor_running`, `status` (`running` / `restarting` /
`stopped`), `driver`, `seconds_since_cycle`, `stale_after_seconds`, `schedule`,
`queue_stats`, `detail` and `message`. The `migrated=False` placeholder key is
gone (nothing read it).

**`queue_processor_restart()`** now does real work and reports it:

- `scheduler_service.kick_download_queue_processor()` sets the queue job's
  `next_run_time` to now, so the periodic driver fires immediately;
- when there is no job to re-arm (split deployment, scheduler stopped, job
  disabled) it runs a cycle itself in a background thread (`_kick_queue_cycle()`)
  and calls `signal_new_item()` for an in-process worker;
- it stamps the heartbeat with `outcome="requested"`, so the card reads
  **Restarting** straight away and flips to **Running** once a cycle actually
  completes — the badge changes because the queue moved, not because a flag was
  cleared.

**Both frontends** gained the missing renderer. `static/js/downloads.js` and
`test_site/static/js/pages/download-queue.js` now define
`loadQueueProcessorStatus()` + `updateProcessorStatusCard()`, and
`loadQueueStatus()` awaits the processor fetch **first**, before its early
return, so the card can never be left on the loading text. The badge renders
Running / Restarting / Stopped / Unknown, the body shows the server's message
plus queued · active · failed · ready, and `#restartProcessorBtn` is enabled in
every state (colour changes: danger while down, primary while up).

The rebuild tree's partial documents the contract: those three ids are written
by one function and nothing else.

## Tests

`tests/test_queue_processor_status.py` — 32 tests, green:

- endpoint is no longer a stub (payload shape, message, evidence)
- verdict follows evidence: fresh heartbeat → running, stale → stopped, no
  stamp → stopped, armed-but-stopped scheduler → stopped, armed scheduler →
  running before its first tick, staleness window follows config
- restart kicks a cycle, signals the worker, reports `restarting`, and a
  finished kick becomes `running`
- both drivers stamp, and a cycle that raises stamps nothing
- both JS modules define the renderer, touch all three ids, enable the button,
  refresh on every poll, render all three states, and export the helper
- both partials carry the ids and ship the neutral initial state
- `GET /api/queue-processor/status` serves a real verdict

**Oracle:** reverting `queue_diagnostics_service.py` → 11 failed + 3 errors;
reverting both JS files → 11 failed. Both restored.

**Sweep:** 34 affected test files, one pytest process each. Four files fail —
`test_busy_popup` (1), `test_download_match_lifecycle` (10),
`test_max_retries_column_removed` (1), `test_scan_stall_fixes` (7) — all with
identical failure IDs against a clean baseline, i.e. pre-existing.

## Not done

The card has **no Stop button**. Stopping would mean killing the standalone
`queue_worker` process, which the WebUI does not own (`entrypoint.sh` starts it
and the shell holds the PID). A Stop that could not stop anything would be the
same class of false affordance this fix just removed.
