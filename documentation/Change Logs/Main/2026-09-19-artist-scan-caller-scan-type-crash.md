# Artist/letter popularity scans died before starting

**Date:** 2026-09-19
**Area:** `popularity` / `scan` / `routes`

## Reported symptom

```
Traceback (most recent call last):
  File "/usr/local/lib/python3.11/threading.py", line 1045, in _bootstrap_inner
    self.run()
  File "/usr/local/lib/python3.11/threading.py", line 982, in run
    self._target(*self._args, **self._kwargs)
TypeError: run_popularity_from_artist() got an unexpected keyword argument
'caller_scan_type'
```

## Root cause

`routes/scan_routes/popularity.py::api_scan_from_artist` launches the worker
like this:

```python
run_async(
    run_popularity_from_artist,
    artist=artist,
    force_rescan=force_rescan,
    progress_file=progress_file,
    caller_scan_type="popularity",   # <-- not in the target's signature
    daemon=False,
)
```

but the entry point did not accept that keyword:

```python
def run_popularity_from_artist(
    *,
    artist: str,
    force_rescan: bool = False,
    progress_file: str | None = None,
    verbose: bool = False,
):
```

`run_async` forwards `**kwargs` straight to `threading.Thread`, so the
`TypeError` was raised inside `Thread.run()`.

### It has never worked

`git log -S` shows the route gained `caller_scan_type` in **`e1b133a8`**
(2026-07-24) and the function's signature was unchanged in every revision since
it was introduced in `88af005b`. The keyword was never accepted, so **every
artist- and letter-initiated scan from the artist page died instantly**. This is
not a regression from recent work — the path has been broken since it was
written.

### Why it went unnoticed for two months

This is the part worth fixing structurally. `run_async` starts a **daemon
thread** and returns immediately:

```python
thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=daemon)
thread.start()
return thread
```

So the failure never propagated to the request. The route still returned

```json
{"success": true, "message": "Popularity scan started from artist: ..."}
```

and wrote `status: "starting"` to the progress file. The traceback went only to
the daemon thread's stderr — which nothing collects. In the app log and on the
dashboard a dead scan is **indistinguishable from a slow one**.

Note that sibling route (`routes/scan_routes/api.py`) already wraps its worker
in `try/except` with an explicit comment about exactly this hazard:

> *A daemon-thread exception would otherwise be swallowed by `run_async` (no
> handler) — the route returns "started", the worker dies silently…*

That guard was applied to the other worker but not to the shared helper and not
to this one.

## Fixes

### 1. Accept the keyword — `services/popularity/pipeline.py`

```python
def run_popularity_from_artist(
    *,
    artist: str,
    force_rescan: bool = False,
    progress_file: str | None = None,
    verbose: bool = False,
    caller_scan_type: str | None = None,
):
    ...
    _effective_scan_type = caller_scan_type or "popularity"
```

The value now **flows through** rather than sitting unused: the internal
`run_popularity_scan(...)` call sends `_effective_scan_type` instead of a
hardcoded `"popularity"`. Defaulting to `None` and resolving to `"popularity"`
preserves the previous behaviour for any caller that omits it.

### 2. Make the helper fail loudly — `routes/scan_routes/_common.py`

`run_async` now validates the target's signature **before** starting the thread:

```python
assert_callable_accepts_kwargs(target, kwargs)
```

A mismatch raises a `TypeError` naming the offending keyword, synchronously at
the call site, while the request is still being handled — instead of killing a
thread nobody is watching. Targets with `**kwargs` are exempt, and callables
that expose no signature (builtins) are skipped.

The worker is also wrapped so any exception it raises is logged via `structlog`
before propagating, so a crashed scan reaches the application log rather than
only the thread's stderr.

### 3. Swept the rest of the codebase

An AST audit resolved every `run_async(target, **kwargs)` call site in
`routes/scan_routes/` against the target's real signature: **19 call sites
checked, this was the only mismatch.** No other route passes a keyword its
worker does not accept.

## Verified

| Check | Result |
|---|---|
| Original bug reproduced on the unmodified tree | `TypeError: run_popularity_from_artist() got an unexpected keyword argument 'caller_scan_type'` |
| New regression suite, pre-fix | **5 failed**, 3 passed |
| New regression suite, post-fix | **8 passed** |
| Scan/stall/route suites, pre-fix vs post-fix | identical (48 passed / 10 pre-existing failures both ways) |
| `run_async` call-site sweep | 19 checked, 0 remaining mismatches |

## New guard tests

`tests/test_scan_async_signature_contract.py`

1. `run_popularity_from_artist` accepts `caller_scan_type`, defaulting to `None`.
2. The `None` default resolves to `"popularity"`.
3. Every `run_async(run_popularity_from_artist, ...)` call site is checked against
   the real signature. Note the routes never *call* the function directly — they
   pass it as the thread target, so the audit inspects `run_async` calls whose
   first argument names it.
4. `run_async` raises `TypeError` **synchronously** on a signature mismatch.
5. `run_async` still runs matching targets, and does not block `**kwargs` ones.
6. `run_async` logs a crashing worker (the failure mode that hid this bug).
7. A sweep of every local `run_async` worker target in `routes/scan_routes/`.

## Pre-existing failures (not caused by this change)

`tests/test_scan_stall_fixes.py`, `tests/test_full_scan_startup_fix.py`,
`tests/test_full_scan_as_artist_pipeline.py` and `tests/test_auth_gate.py` have
failures on `origin/develop` **before** any of this work; the counts are
identical with and without the fix (48 passed / 10 failed on the scan subset).
