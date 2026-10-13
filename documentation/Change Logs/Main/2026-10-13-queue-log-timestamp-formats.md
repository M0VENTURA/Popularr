# Queue log timestamps: the 1970 rows (2026-10-13)

## Report

Some rows in the Download Queue Events Log showed **01/01/1970**, while others
showed the correct date — e.g.

```
13/10/2026, 10:53:49 am   INFO   2026-10-13 10:53:49 [INFO] [popularr.queue] [SEARCHING] BIGBANG - SOBER …
01/01/1970, 10:00:00 am   INFO   2026-10-13T10:53:31.169453 [WARNING] [services.downloads.download_completion_service] slskd succeeded …
```

The pattern is exact: `[popularr.queue]` lines were right, `[services.*]` lines
were 1970.

## Root cause — ONE log file, TWO timestamp formats, and a parser that knew one

`queue.log` is written by two different logging paths:

| Writer | Format | Example |
| --- | --- | --- |
| structlog loggers (`services.downloads.*`, `services.queue.*`, …) → `TimeStamper(fmt="iso")` | ISO, `T` separator + microseconds | `2026-10-13T10:53:31.169453` |
| `log_queue` / `log_unified` → the `popularr.*` stdlib loggers (`%Y-%m-%d %H:%M:%S`) | space separator | `2026-10-13 10:53:49` |

`services/queue/queue_diagnostics_service.py` parsed both with a **space-only**
pattern:

```python
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
```

so every `services.*` line yielded `created_at=None`, and the viewer evaluated
`new Date(null)` — which is the **epoch**, not an error — and rendered
01/01/1970. `null` is a *valid* input to `new Date()`, so nothing anywhere
raised: the row simply claimed to be from 1970.

The other two parsers of these same logs already accepted both separators:

* `services/log_service.py::_filter_lines_last_hour` → `[ T]`
* `templates/pages/logs.html` → `[ T]`

This one had drifted. **The format mismatch is the defect class; the parser was
the one that couldn't cope with it.**

## Fix

**Backend (root cause).** One module-level constant, used by all three readers:

```python
_LOG_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
```

`_tail_queue_log`, `_tail_search_log` (both had their own copy — a second copy
that accepts a different subset is exactly how one viewer shows dates and
another shows 1970) and `queue_events` now share it.

**Frontend (defensive).** `new Date(null)` is the epoch, so a row the backend
still cannot parse must not claim 1970. Both `loadQueueEvents` implementations
(live + test_site) now render `—` for a missing **or** unparseable timestamp,
and the queue/search activity logs guard `Invalid Date` instead of printing it.

## Why the log format was NOT unified

Writing one format everywhere is the deeper fix, but it changes the log format
for every consumer (external log tooling, the `/logs` page, exports) and is a
much larger blast radius than the bug warrants. The tolerant parser is also the
**existing convention** in this codebase — three of four parsers already used
`[ T]`. Flagged here rather than changed unilaterally.

## Verified

`tests/test_queue_log_timestamp_formats.py` (new, 13):

* the shared pattern accepts BOTH forms and captures the expected value;
* the pattern is declared exactly once, and no space-only copy remains
  (a structural guard — a second copy silently reintroduces the regression);
* `_tail_queue_log` and `_tail_search_log` populate `created_at` for both
  formats from a real two-format log file, normalised to ISO for `new Date()`;
* `queue_events` returns events with a parseable timestamp;
* both `loadQueueEvents` renderers fall back to a dash, not the epoch.

**Oracle: 13 failed / 0 passed with the source reverted to `origin/develop`**,
restored → 13 passed. Sweep of 9 queue/log suites (215 tests) → **0
regressions**.

## Notes

- The fix touches `static/js/downloads.js` (the LIVE tree) as well as
  `test_site`, deliberately: live is the DEFAULT mode and the 1970 rows come
  from that renderer.
- The viewer only misreported the *displayed* time. The underlying events, and
  everything the queue actually did, were unaffected.
