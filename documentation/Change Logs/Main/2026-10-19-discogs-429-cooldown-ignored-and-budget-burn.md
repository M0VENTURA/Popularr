# A Discogs 429 became a storm — the cooldown was ignored and each retry burned its budget

**Date:** 2026-10-19
**Area:** `api_clients/discogs_http.py` · Discogs rate limiting
**Commit:** `fix(api): honour the Discogs 429 cooldown and stop burning the request budget on it`

## Report

Scans showed a burst of Discogs push-back on a single album, then silent data
loss:

```
WARNING api_clients.discogs_http  Discogs rate limited wait_seconds=60
… × 8 …
WARNING api_clients.discogs_http  Discogs request budget exceeded — giving up url='…/database/search'
INFO    popularr.unified  [TRACK] 🎵 "Winter Wonderland" | … Single: HIGH [Discogs: ✓, MB: ✓] | 142.5s
INFO    popularr.unified  [TRACK] 🎵 "Tecato"          | … Single: LOW  [Discogs: ✖, MB: ✖] | 102.9s
```

Track wall-times of 100–142 s on an 11-track album, and the tracks that lost the
race came back `Discogs: ✖` — the Discogs signal was simply absent, not
"no match".

## Root cause — two defects, both introduced since the 2026-10-07 change

### 1. The 429 cooldown is dead on the shared-limiter path

`throttle_discogs()` was restructured (2026-10-07, cross-process throttle) to
ask the shared disk-backed limiter **first** and `return` immediately:

```python
if _rate_limiter:
    try:
        _rate_limiter.throttle_discogs()
        return          # ← returns before the cooldown is ever consulted
    except Exception as exc:
        logger.debug("External Discogs rate limiter failed, using local fallback", …)

sleep_time = 0.0
with _DISCOGS_THROTTLE_LOCK:
    cooldown_wait = _DISCOGS_RATE_LIMIT_UNTIL - now   # ← unreachable in a deployment
    …
```

`_DISCOGS_RATE_LIMIT_UNTIL` — the window a 429 arms — was therefore **only
honoured in the fallback branch**, which a real deployment never reaches.

The reference implementation had it the other way round and DID respect it:

```python
def _throttle_discogs():
    if _DISCOGS_RATE_LIMIT_UNTIL > time.time():
        time.sleep(_DISCOGS_RATE_LIMIT_UNTIL - time.time())
    …
```

Consequence: after a 429, the other worker threads and the queue-worker process
kept firing at 1 req/s into a limit Discogs had just refused. **One 429 became a
storm of them** — which is exactly the repeated `rate limited wait_seconds=60`
in the log.

### 2. The 429 branch slept the full cooldown and then abandoned the request

```python
if time.monotonic() + min(wait_seconds, 1.0) > _deadline:   # ← min(…, 1.0)
    logger.warning("Discogs request budget exceeded during 429 cooldown", url=url)
    return {}
time.sleep(wait_seconds)
```

The guard asks "is there **one second** left?" rather than "is the cooldown
within budget?". Against a 30 s budget a 60 s `Retry-After` cleared the check,
**slept the full 60 seconds**, and only then did the next loop iteration
abandon the request at the top-of-loop budget check. Every 429 cost a minute
*and* still lost its data — the worst of both behaviours.

`_DISCOGS_REQUEST_BUDGET_SECONDS` itself is new; the reference system had no
budget at all and merely waited. So the visible symptom —
`Discogs request budget exceeded — giving up` and `Discogs: ✖` — is new.

## Fix

`api_clients/discogs_http.py`:

* `throttle_discogs()` now waits out `_DISCOGS_RATE_LIMIT_UNTIL` **before**
  anything else, on every path, and only then asks the shared limiter for a
  slot. The lock is held solely to READ the window; the sleep stays outside it
  so one sleeping worker cannot serialise the others.
  The pacing clock is pinned to the cooldown's end
  (`_DISCOGS_LAST_REQUEST_TIME = max(…, _DISCOGS_RATE_LIMIT_UNTIL)`) so the next
  request still gets its full spacing *after* the cooldown rather than taking an
  immediate slot.
* The 429 guard compares the **actual** cooldown against the remaining budget
  (`time.monotonic() + wait_seconds > _deadline`) and bails out *before*
  sleeping, letting the now-honoured shared cooldown pace the next attempt.

## Tests

`tests/test_discogs_throttle_lock.py` (+2):

* `TestCooldownHonouredOnSharedLimiterPath.test_cooldown_is_waited_before_shared_limiter_slot`
  — stubs the shared limiter and asserts it is called **once**, and only more
  than ~0.4 s after entry while a 0.5 s cooldown is armed. Fails on the old code
  (the limiter was called immediately).
* `Test429LongCooldownDoesNotBurnBudget.test_long_retry_after_gives_up_within_budget`
  — a `Retry-After: 60` under a 3 s budget must return `{}` well under the
  header. Fails on the old code (it slept the full 60 s).

The tests stub `_rate_limiter` explicitly because the test environment leaves it
`None`, which is why the suite never exercised the production path the bug lived
on.

## Notes

`_DISCOGS_MIN_INTERVAL = 1.0` sits **exactly** on Discogs' authenticated quota
(60 req/min), so a sustained scan runs at the ceiling and any jitter trips a
429. Raising the interval slightly (or making it configurable) would give
headroom; not changed here, as the value is deliberate and the cooldown fix is
what removes the storm.
