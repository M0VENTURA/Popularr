# "Process download queue" no longer discards ticks that run 38s long (2026-10-05)

**Report (popularity-scan log):**
`Run time of job "Process download queue" was missed by 0:00:38`.

## The arithmetic

`get_scheduler()` sets `job_defaults = {"coalesce": True, "max_instances": 1,
"misfire_grace_time": 300}` — and the download-queue job then **overrode the
grace DOWN to 30**:

```python
_put("download_queue_processor", "Process download queue",
     IntervalTrigger(seconds=interval_seconds),
     func=_download_queue_processor_tick,
     max_instances=1, coalesce=True,
     misfire_grace_time=30,     # ← 30, below the 300s default
)
```

APScheduler discards a run whose start is late by more than the grace. A tick
that ran 38s long passed 30s by 8 seconds, so the next run was **thrown away**
and downloads idled for a whole extra interval — for no reason. It was the only
job with a grace below the global default.

## Fix

Drop the override so the job inherits the 300s default like every other job.
`coalesce=True` and `max_instances=1` stay on the call: they are what makes a
*late* tick safe, since a backlog collapses into a single run and overlap is
forbidden. Lateness therefore can never turn into a pile of runs — the reason
30s was defensible in the first place does not survive the arithmetic.

## Tests

Covered in `tests/test_abandoned_album_workers.py::TestTheQueueTickIsNotDiscarded`
(AST-based, so a later hand-edit cannot quietly reintroduce the override):

- the queue job's `_put(...)` carries no `misfire_grace_time`;
- `job_defaults` still sets one, and it is ≥ 300 (a 38s overrun must fit);
- `coalesce`/`max_instances` are set on the call *and* in `job_defaults`.
