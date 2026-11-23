# The APScheduler thread died on a Postgres restart, taking every job with it

**Date:** 2026-11-23
**Area:** `services/scheduler/scheduler_service.py` ·
`tests/test_scheduler_jobstore_outage.py` (new)
**Commit:** `fix(scheduler): keep the main loop alive when the jobstore is unreachable`

## Reported

```
Exception in thread APScheduler:
...
File "/usr/local/lib/python3.11/site-packages/apscheduler/schedulers/base.py",
line 1237, in _process_jobs
    jobstore_next_run_time = jobstore.get_next_run_time()
File "/app/services/scheduler/scheduler_service.py", line 155, in get_next_run_time
    return self._execute(super().get_next_run_time)
File "/app/services/scheduler/scheduler_service.py", line 97, in _execute
    return Retrying(
File "/usr/local/lib/python3.11/site-packages/tenacity/__init__.py", line 216, in reraise
    raise self.last_attempt.result()
...
sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server
at "popularr-postgres" (192.168.1.225), port 5432 failed:
FATAL:  the database system is in recovery mode
```

## Root cause: the one call APScheduler does not guard

`BaseScheduler._process_jobs` guards `get_due_jobs`:

```python
try:
    due_jobs = jobstore.get_due_jobs(now)
except Exception as e:
    self._logger.warning("Error getting due jobs from job store %r: %s", ...)
    next_wakeup_time = retry_wakeup_time
    continue
```

and then, twenty lines later, calls `get_next_run_time()` at method indent with
**no** surrounding `try`. An exception there unwinds out of `_process_jobs`,
out of `_main_loop`, and ends the scheduler thread:

```
Exception in thread APScheduler
```

That is the whole failure: **every** scheduled job stops — scans, playlist syncs,
the queue watchdog, the heartbeat — because of a condition that lasts seconds and
was measured ~350 times over four days.

`_execute` did retry it (`Retrying(... stop_after_attempt(3))`), correctly, but
ended with `reraise=True` — right for a write, wrong for a read whose only
consumer is a loop that will ask again moments later.

## The fix

`get_next_run_time` now returns the last value that actually reached the database
when the error is transient, and only re-raises a non-transient one (a missing
column or a permission fault is real and must not be papered over). The loop
keeps running and picks the schedule up again on its next iteration.

The retry budget and `_is_transient` are unchanged — the fix is one `try`, not a
second retry policy.

## Verification

`tests/test_scheduler_jobstore_outage.py` (6 tests): a transient
`OperationalError` is swallowed, the last known value is served, a non-transient
error still propagates, a successful read is cached, and — so nobody
"simplifies" this back to one line — the docstring must keep naming the original
symptom.

Proven to fail without the fix by stashing `scheduler_service.py`: 4 of the 6 go
red.

Full suite: 405 failed, an identical failure set to baseline.
