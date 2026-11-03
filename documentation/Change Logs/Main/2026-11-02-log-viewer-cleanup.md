# Log viewer cleanup: one stream per real source

**Date:** 2026-11-02
**Area:** log viewer / logging
**Files:** `services/log_service.py`, `routes/logs.py`,
`tests/test_activity_log_merge.py`, `tests/test_log_view_consolidation.py`

## Report

The log picker had grown to nine entries, including two that looked like
Navidrome logs and three routinely-empty files:

```
Scanner — unified_scan.log
Activity (Queue + Search) — activity.log
Navidrome Import — navidrome_import.log
Navidrome — info.log                 <- INFO subset of debug.log
System — error.log
access.log (0 B)
client.log (34 KB)
debug.log (3.2 MB)
queue_processor.log (0 B)
```

## What the viewer now offers

| Entry | File | Why |
|---|---|---|
| Scanner | `unified_scan.log` | scan pipeline |
| Activity (Queue + Search + UI) | `activity.log` | queue, search AND UI alerts |
| Navidrome Import | `navidrome_import.log` | import pipeline |
| System | `error.log` | ERROR-level |
| Debug | `debug.log` | everything (INFO + DEBUG) |

## Hidden (still written / still downloadable)

* **`info.log`** — an INFO-level subset of `debug.log`. Its “Navidrome” label
  made the picker show two Navidrome-looking entries; kept on disk for
  compatibility, no longer offered.
* **`client.log`** — UI alerts are now ALSO appended to `activity.log`, so the
  toast/alert messages sit beside the queue/search events they belong to.
  `client.log` is still written for legacy readers.
* **`access.log`** — Hypercorn's access log; routinely 0 B.
* **`queue_processor.log`** — the queue worker's empty stdout redirect.
* **`queue.log` / `search.log`** — already merged into `activity.log`.

`GET /api/logs/list` now returns `viewer_log_files()` (the same helper the
live `/logs` page renders), so the modal's picker cannot re-expose a retired
source the page hides.

## Tests

`tests/test_activity_log_merge.py`:

| Test | Pins |
|---|---|
| `test_redundant_and_empty_logs_are_hidden_from_the_viewer` | exactly the five visible entries |
| `test_client_alerts_also_reach_the_activity_stream` | UI alerts in both activity.log and client.log |
| `test_the_api_uses_the_viewer_list` | `/api/logs/list` uses `viewer_log_files` |

`tests/test_log_view_consolidation.py` updated for the new labels
(`Application (info)`, `Activity (Queue + Search + UI)`, `Debug`) and the
`viewer_log_files()` endpoint contract. The fake-logger fixture now passes
`logging.getLogger()` (no name) through to the real root logger; pytest's
logging plugin calls it during teardown.
