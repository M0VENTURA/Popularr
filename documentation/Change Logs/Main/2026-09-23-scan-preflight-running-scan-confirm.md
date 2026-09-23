# Pre-flight warning before starting a scan while one is already running

**Date:** 2026-09-23
**Area:** `ui` / `scan`

## What was asked

> "When a scan is selected to run and a scan is already running, I want a popup to
> appear before running the scan advising 'A scan is currently running. Would you
> like to cancel this scan and start the new scan?' It should list the name of the
> scan thats running that it's asking to cancel. Whether its a full scan, artist
> scan, the current artist being scanned."

## What changed

**NEW `test_site/static/js/services/scan-preflight.js`** (live mirror:
`static/js/scan-preflight.js`) — one shared gate, `ScanPreflight.confirmIfRunning()`.

### Nothing new was needed server-side

The status was already there. `GET /api/scan-progress` →
`progress_service.get_scan_progress()` already returns, per running scan, the
`scan_type`, `percent_complete`, `current_artist` and `current_album` — and it
already puts `full_scan` first so `active_scans[0]` is the primary scan.
`POST /scan/stop-all` already cancels every scan family. This change is the
missing *client*: before starting, ask, and act on the answer.

### The hard part: stopping is asynchronous

`/scan/stop-all` only sets a flag. The running scan checks `is_stop_requested`
**between artists/albums**, so it keeps running for a moment afterwards. Firing
the new start immediately would hit the server's duplicate guard and fail — the
confusing outcome this feature exists to remove. So the order is:

1. probe `/api/scan-progress`;
2. if nothing is running → proceed immediately (no dialog, no cost);
3. if something is running → name it and ask;
4. on accept → `POST /scan/stop-all`, then **poll until nothing is running**;
5. only then let the caller start.

If it does not go idle inside 20s, the gate **aborts** and says so rather than
starting a scan the server is about to reject.

### The dialog names the scan

`Full Scan — Madball (42%)` / `Artist Scan — Ateez` / `Popularity Scan`.
Built from the scan-type label plus `current_artist` (+ `current_album` for an
album scan) and the percentage. Labels live in the module, not dashboard.js,
because the gate also runs on the artist and artist-list pages, which do not load
the dashboard script. A test asserts every scan type the server reports has a
label, so a raw `some_new_scan` can never reach the user.

### Wiring — every scan-start path

| Path | Surface |
|---|---|
| `runDashboardPopularityScan` | dashboard Run (popularity / singles / metadata / all) |
| `startNavidromeImport` | dashboard Navidrome Import |
| `startNavidromeServerScan` | dashboard Sync Changes |
| `startEssentiaScan` | dashboard Run Mood Scan |
| `scanLetterArtists` | artist list letter scan |
| artist scan form | artist page Run (`data-scan-preflight`) |
| `forceArtistMetadataRefresh` | artist page metadata refresh |

The artist page starts scans with a **server-rendered POST form**, which has no
JS to hook, so forms opt in declaratively with `data-scan-preflight` and the
module attaches on load.

⚠️ `form.submit()` does **not** dispatch a submit event (that is
`requestSubmit()`). An earlier draft set a "cleared" flag for the re-submit;
because nothing consumed it, it stayed set and **silently skipped the gate on the
next click**. The re-submit is safe without a flag, and re-entrancy is guarded
with a `WeakSet` instead. A test asserts the dead flag never returns.

### Failing open

If the status probe fails, the gate **proceeds**. Blocking a legitimate scan
because a status endpoint hiccuped is worse than the bug being fixed, and the
server-side duplicate guard still protects correctness.

## Tests

**NEW `tests/test_scan_preflight_gate.py`** (41 tests) plus
**NEW `tests/js/scan-preflight-probe.js`**, which extracts the shipped module,
runs it against stubbed `fetch`/timers and reports what actually happened.

A structural test ("the file mentions /scan/stop-all") cannot distinguish the
required ordering from a broken one, so the probe asserts behaviour:

| Scenario | Expected |
|---|---|
| idle | proceed, no dialog, no stop call |
| running + declined | do not start anything |
| running + accepted | `stop-all`, then ≥1 wait-poll, then proceed |
| never goes idle | **abort** + tell the user |
| status probe fails | fail open (proceed) |

**Oracle:** with the module moved aside and the integration reverted, the suite
reports **41 failed**. Restored: **41 passed**.

**Regression sweep** (8 scan/JS suites, 163 tests): 3 failing on **both** trees —
**0 regressions**:

* `test_full_scan_startup_fix.py::TestPopularityRunRouteStaleRowSelfHeal` (2) —
  SQLite harness has no `scan_states` table.
* `test_scan_stop_returns_json.py::...[\/scan\/stop]` (1) — same missing table.

Both were confirmed identical before and after the change.

`node --check` passes on all 8 touched JS files; `import app` reports 391 routes.

## Notes

* Both trees were updated for every change — `helpers/test_site_mode` serves the
  rebuilt tree first, so a single-tree edit is invisible to the other user.
* `templates/pages/artist_detail.html` and `test_site/.../artist_detail.html` are
  dead code (the route renders `artist_detail_v2.html`); not touched.
