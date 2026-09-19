# Stale startup self-check: "Scan unwrap fix NOT VERIFIED" on every boot

**Date:** 2026-09-20
**Area:** `entrypoint.sh` / `tests` / `services/popularity/scan_stage_runner.py` (checked only)
**Status:** fixed, guarded by `tests/test_entrypoint_startup_checks.py`

---

## Symptom

Every container boot printed, in the pre-flight section:

```
Scan unwrap fix check: source_marker_hits=0 loaded_module=MISSING
⚠ Scan unwrap fix NOT VERIFIED — source_marker_hits=0, loaded_module=MISSING
⚠ If this is the current image, check for stale __pycache__/old image.
```

## Root cause: the check pinned a LOG STRING, not the behaviour

`verify_scan_unwrap_fix()` greps `services/popularity/scan_stage_runner.py` for
the literal `"Album future completed"` and separately imports the module and
looks for the same literal via `inspect.getsource`.

That literal was a **log message** the original "Album failed: `<Future ...>`"
fix happened to emit (`8112bec0`). A later refactor of the track executor
(`5193741e` "Cleaned up some issues causing timeouts during scanning") removed
the message **while keeping the fix**, so both arms of the check went
permanently negative:

- `source_marker_hits=0` — the log line is gone from the source.
- `loaded_module=MISSING` — the same file, so the import agrees. (Note it was
  *not* `ERROR:` — the import itself was always fine. And `__pycache__` is
  purged at the top of `main()`, before this check runs, so the result was
  never a stale-bytecode signal either.)

**The fix itself was never broken.** `_execute_track_jobs_safely` still unwraps
every future — `results[idx] = future.result()` inside a guarded loop:

```python
for future in concurrent.futures.as_completed(future_to_idx.keys()):
    idx = future_to_idx[future]
    try:
        results[idx] = future.result()
    except Exception as exc:
        logger.warning("Track worker crashed", artist=artist, album=album, error=str(exc))
```

### Why a permanently-warning check is worse than no check

The only thing this check exists for is telling an operator whether a **stale
`.pyc` / old image** is running. A warning that fires on every single healthy
boot cannot make that distinction — it trains the reader to ignore it, which is
precisely the situation the check was written to prevent.

## Fix

`verify_scan_unwrap_fix()` now probes the **structural invariant**, which
survives rewording:

1. **Source arm** — grep the file for `as_completed`, and distinguish
   `FILE-MISSING` from "marker absent". Those two cases used to collapse into
   the same `0`, which hid a wrong path/image entirely.
2. **Import arm** — import the collector *by name*
   (`_execute_track_jobs_safely`) and inspect **that function's** source for
   both `as_completed` and `future.result()`, instead of scanning the whole
   module. A whole-module scan could be satisfied by unrelated code.

Log strings remain free to change; the two tokens are the actual fix.

```
Scan unwrap fix check: collector_source=1 loaded_function=PRESENT
  ✓ Scan unwrap fix VERIFIED (per-future unwrap present in source + fresh import)
```

## Guards: `tests/test_entrypoint_startup_checks.py` (4 tests)

1. `test_entrypoint_grep_tokens_still_exist` — **the class, not the instance.**
   Every literal `entrypoint.sh` greps out of a `/app/...` file must still exist
   in that file. Any future self-check pinned to a refactorable string fails
   here instead of silently rotting into a permanent warning.
2. `test_scan_unwrap_check_probes_the_collector_function` — the check must
   import the collector by name and require `future.result()`.
3. `test_scan_unwrap_check_does_not_use_a_retired_log_string` — the retired
   literal must not come back *in code* (the explanatory comment legitimately
   quotes it, so the guard reads a comment-stripped copy).
4. `test_scan_unwrap_check_would_pass_on_this_tree` — runs the shipped
   predicate in Python and requires both arms to be satisfied, so it cannot rot
   into a tautology: it asserts the behaviour, not the wording.

## Verification

Worktree at `origin/develop` (`9a1b4958`), edits replayed there.

| Check | Result |
|-------|--------|
| `bash -n entrypoint.sh` (Git bash) | OK |
| **Guard vs the unpatched entrypoint** | **3 failed / 1 passed** — greps-tokens (marker gone), probes-collector, retired-log-string. The 1 pass is `would_pass_on_this_tree`: the *code* was always fine, only the *check* was broken. |
| Guard vs the patched entrypoint | **4 passed** |
| Function executed against three simulated images | unwrap present → `PRESENT` / **VERIFIED**, return 0 · unwrap removed → `MISSING` / NOT VERIFIED, return 1 · file absent → `FILE-MISSING` / "not present in this image", return 1 |

## Files changed

- `entrypoint.sh` — `verify_scan_unwrap_fix()` probes the collector function's
  behaviour; new `FILE-MISSING` state; new log field names
  (`collector_source` / `loaded_function`)
- `tests/test_entrypoint_startup_checks.py` — new, 4 tests

`services/popularity/scan_stage_runner.py` was **not** modified — the fix it was
checking for was already present.
