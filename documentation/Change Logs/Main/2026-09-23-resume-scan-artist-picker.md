# Resume scan: choose which artist to resume from

**Date:** 2026-09-23
**Area:** `ui` / `scan`

## What was asked

> "When Starting a scan from the dashboard and restart isn't being selected, I want
> a popup asking which artist to resume from. At the top of the list should be the
> artist that was last scanned during a full scan, as well as the last three
> artists that were manually scanned or had albums scanned. If that artist had
> completed the scan (all albums scanned during that scan) it will continue from
> the next artist. If it was interrupted half way through, it will restart that
> artist and then continue the full scan."

## The two cases that had to differ

The resume plumbing already existed — `resume_from` was read from the
`full_scan` checkpoint and passed to the scan loop. What was missing was any way
to *choose* it, and the distinction that makes choosing worthwhile.

The full-scan loop skips every artist **before** `resume_from` and **processes
`resume_from` itself**:

* **interrupted** → pass the artist itself → it is re-scanned from the top ✅
* **completed** → pass the **next** artist → the finished one is not redone ✅

Getting that backwards silently redoes finished work, so it is computed on the
server and asserted directly in tests rather than left to the UI.

## What changed

**NEW `services/scanning/resume_options_service.py`**

* `get_resume_options()` — the picker's list: the interrupted full scan's artist
  first, then up to three recently-touched artists.
* `resolve_resume_target(artist)` — decides `next` vs `restart` and returns the
  `resume_from` the scan should actually use, plus a human-readable reason.
* `artist_scan_state(artist)` — `completed` / `interrupted` / `unknown`.

**How "completed" is decided.** Each album writes a `scan_history` row
(`record_scan(..., "started", artist, album)`) and later updates that same row to
`completed`. A scan killed mid-album leaves it at `started`. So the status of the
artist's most recent album-level row answers the question. Session-level rows use
the sentinel `_SCAN_SESSION_` and are excluded — they carry no artist.

`id DESC` is used for ordering rather than a timestamp: `id` is the table's
serial so it is monotonic, whereas legacy rows can have a NULL `started_at` and
would sort unpredictably.

**NEW endpoint** `GET /api/scan/resume-options` (`routes/scan_routes/api.py`).

**`/api/popularity/run` now honours an explicit `resume_from`.** It previously
always used the stored checkpoint, so a user's choice would have been silently
discarded. Precedence: `restart` → clear the checkpoint; else the request's
value; else the checkpoint. `resume_from` was added to `ScanRequest` so Pydantic
does not strip it.

**NEW `test_site/static/js/services/scan-resume-picker.js`** (+ live mirror
`static/js/scan-resume-picker.js`) — a radio list modal. Shown by
`runDashboardPopularityScan()` when **Restart is unchecked**; Restart means "from
the top", so prompting there would contradict it. Each row states what will
happen (`finished — continue from the next artist` / `restart this artist`).
Cancelling does **not** start a scan.

The picker is deliberately **self-contained**: the rebuilt tree has a
`global.modal` helper but the live tree does not, so it builds its own Bootstrap
modal and falls back to the native prompt when Bootstrap is absent — one
implementation, identical behaviour in both trees.

## Pre-existing bug fixed in passing

`services/scanning/scan_resume_service.py::get_resume_artist_from_db()` queried
`SELECT artist_name FROM scan_history ORDER BY scanned_at DESC`. **Neither column
exists** — they are `artist` and `started_at`. Every call raised, and a bare
`except` turned it into `None`, so it silently never returned anything. Corrected
to the real columns (and to exclude the session sentinel).

## Tests

**NEW `tests/test_resume_artist_picker.py`** (45 tests).

**Oracle:** with the new modules moved aside and the integration reverted,
**45 of 45 fail** (19 failed + 26 errors, 0 passed). Restored: **45 passed**.

**Regression sweep** (7 scan suites, 147 tests): failing set **identical** before
and after — `Compare-Object` diff **EMPTY**, i.e. **0 regressions**. All six
failures are pre-existing SQLite-harness issues (no `scan_states`/`scan_history`
tables; `EXTRACT(EPOCH FROM …)` is Postgres-only):

* `test_full_scan_startup_fix.py::TestPopularityRunRouteStaleRowSelfHeal` (2)
* `test_scan_history_completion.py::TestRecordScanCompletionWithoutStarted` (2)
* `test_scan_stale_stop_flag.py::TestFullScanClearsStaleStopFlag` (2)

`node --check` passes on all 4 touched JS files; `import app` reports 387 routes.
