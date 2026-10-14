# The library scan could not be stopped — a stop flag written to a name nothing reads

**Date:** 2026-10-14
**Files:** `routes/scan_routes/control.py`

Reported from a scan log:

> Even though `routes.scan_routes.control` logged `Stop requested for all scans
> scan_types_affected=10` at 16:54:55, the Library Scan never stopped! Both
> scans ran simultaneously ... causing a PostgreSQL `DeadlockDetected` crash on
> the `tracks` table.

## Root cause — a pure naming mismatch

`run_full_library_scan` (`services/scanning/pipeline.py`) resolves its progress
row through the canonical accessor and polls **that** name:

```python
progress = get_library_progress_path()      # -> "library"
clear_stop_request(progress)                # clears "library"
if is_stop_requested(progress):             # reads  "library"
```

Both stop routes flagged the prettier `_scan` spelling instead:

```python
path = get_scan_progress_path("library_scan")   # -> "library_scan"
```

`get_scan_progress_path()` is the **identity function**, so the request set
`stop_requested` on a `scan_states` row that **nothing polls**. The library scan
was unstoppable — and because `stop_response` still returns success, the UI
reported a successful stop every time.

The same spelling was also missing from the dashboard's "Clear stuck scans"
list, so a stuck library scan could not be repaired either.

## Why it matters beyond the button

A scan the user cannot stop keeps running while they start another, and the two
collide on `tracks`. That is the deadlock in the report — the stop flag is not a
cosmetic defect, it is the mechanism that is supposed to prevent the collision.

## The fix

Write the name through the accessor the **reader** uses, so the two sides cannot
drift apart again:

* `/scan/stop` → `get_library_progress_path()`
* `/scan/stop-all` → `get_library_progress_path()` in place of `"library_scan"`
* `/scan/clear-stuck` → the library row is now in the list

⚠️ `/scan/stop-all`'s entries must be **the scan_type each running scan actually
reads**, not a consistent-looking naming convention. Audited while fixing this:

| scan | polls | stop-all entry |
|---|---|---|
| library (`run_full_library_scan`) | `library` | **fixed** (`library_scan`) |
| navidrome (`run_navidrome_import_scan`) | `navidrome_scan` | ok |
| full / popularity / singles / mp3 import | own `_scan` names | ok |

The navidrome **checkpoint** path is `navidrome` while its **progress** path is
`navidrome_scan` — the two helpers genuinely differ, which is exactly the trap.

## Tests

`tests/test_library_scan_stop_flag.py` — **7 tests**. Behavioural, not
source-text: it creates `scan_states`, POSTs the real routes, and asserts the
**flag** (a 200 response proves nothing here — that is what made this invisible).

* the reported case: Stop All sets `stop_requested` on `library`
* the plain Stop button does too
* **control of the defect**: nothing writes `library_scan` any more
* **control**: the nine already-correct scans are still flagged
* a running library scan is picked up by Clear stuck scans
* the reader's name is pinned (`get_library_progress_path() == "library"`, and
  `run_full_library_scan` really resolves its row through it)

Oracle (source reverted, tests kept): **5 failed / 2 passed** — the 2 passing
are the controls.

Sweep of 12 scan suites: **zero new failures**. Pre-existing and identical
before/after: `test_scan_stale_stop_flag` 2F, `test_full_scan_startup_fix` 2F,
`test_full_scan_as_artist_pipeline` 1F.
