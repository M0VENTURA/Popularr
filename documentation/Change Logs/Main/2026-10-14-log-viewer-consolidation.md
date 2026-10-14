# One log viewer — the modal absorbs the /logs page

**Date:** 2026-10-14
**Scope:** `test_site/` for the UI; shared backend for the route + API

Reported:

> The logs page and the progress ticker that brings up the logs are showing
> similar information. The dedicated log page should be removed and some of
> the features added into the modal instead.

## What the two views were

| | the page (`/logs`) | the modal (status bar / System ▸ Logs) |
|---|---|---|
| file picker | ✅ `unified_scan.log`, `access.log`, … | ❌ five fixed source tabs |
| filter box (`.*` / `Aa`) | ✅ | ❌ |
| level toggles | ✅ All/Errors/Warnings/Info/Debug | ❌ |
| wrap switch | ✅ (default off) | ❌ — horizontal scrolling |
| scroll-to-bottom FAB | ✅ | ❌ |
| keeps your page | ❌ navigates away | ✅ |

Two controllers for the same files: `test_site/static/js/pages/logs.js` (673
lines, the page) and a second fetch/render loop inside `main.js` (the modal).
They had already drifted — the modal could not filter, the page could not
stream into a panel you kept open.

## What was done

### 1. The route is retired — gated on the cutover helper

`/logs` now redirects to `/dashboard?logs=1` when `config_enables_test_site()`
is on, and `base.html` opens the modal for that flag (then strips the param, so
a refresh does not re-open a closed modal). **Live mode still serves its page**:
its viewer is an inline script with none of this work, so redirecting there
would have removed functionality with nothing to replace it.

The System ▸ Logs menu item keeps its real `href` — it is the JS-off fallback,
and it lands on the *same* viewer via that redirect — while `onclick`
`preventDefault`s so a normal click opens the modal **in place**. Navigating
away would defeat the point of a modal that preserves the page you were on.

### 2. The page's controls moved into the modal

The modal's markup moved out of `base.html` into
`test_site/templates/components/_log_modal.html` (~130 lines; the shell already
carries the navbar, the player and two modals). It carries every id `logs.js`
binds, plus:

* **file picker** (`#logSelector`), populated from **`GET /api/logs/list`** on
  first open. The five source tabs did not disappear — their labels became the
  option text (`Scanner — unified_scan.log (2227.7 KB)`), so one control does
  both jobs;
* **filter box** + `.*` regex and `Aa` case modes + the match counter;
* **level toggles** — the page's five, verbatim;
* **Wrap + Timestamps** switches.

### 3. Header condensed to one row

Title · picker · **⋮** · close. The second row of five large tab buttons and
the Download/Clear/Pause icon cluster are now a single `⋮` overflow menu
(Live Stream, Refresh, Download, Copy, Clear). The header costs ~40px instead
of ~100px, which matters most on the phone the modal is opened from.

The Queue/Soulseek **activity dots** went with the tabs; their signal was never
lost — it is printed on the status bar (`3 Downloading · 5 Queued`), which is
visible whether or not the modal is open.

### 4. Word wrap on by default

`setWrap` writes `white-space: pre-wrap` + `overflow-wrap: break-word`, the
checkbox ships `checked`, **and** `initToolbar()` applies it — a checkbox that
starts checked never fires `change`, so binding alone would have left the
`<pre>` un-wrapped (the exact state this change exists to end).

## One engine, not two

`main.js` lost its log block entirely: `LOG_SOURCE_FILES`, `LOG_TAG_CLASSES`,
`formatLogLine`, `renderLogLines`, `updateUnifiedLog`, `setLogTabDot`,
`smartSelectLogSource` (→ `logs.js`, which owns the picker), the five inline
`onclick` handlers and their six `global.*` exports. What remains there is the
**opener** and the **status bar's** queue summary — the modal's lifetime hooks
(`shown`/`hidden`) hand off to `logsPage.onOpen()/onClose()`.

Two behavioural rules followed from making the engine global:

* **No eager fetch.** `DOMContentLoaded` binds controls only. A `loadLog()` there
  would be a 500-line request *per page view* for a panel that may never open.
* **Stream stops on close.** An open SSE connection behind a dismissed modal is
  the same background-traffic problem the old bare `setInterval` was fixed for.

## Backend

* **`GET /api/logs/list`** — `services.log_service.list_log_files()`, a new
  single source of truth for the allow-list. The `/logs` route (live mode) now
  reads the same helper and enriches the result with the absolute path its
  template renders (deliberately *not* in the API payload). Only allow-listed
  names are ever offered; the picker cannot enumerate or leak a stray `.log`.
* The explanatory comment about `client.log` / `queue_processor.log` moved from
  the route to the allow-list, where the decision now lives.

## Tests

**`tests/test_log_view_consolidation.py` — 48 tests**, in five groups: the
route retirement (both modes + the cutover gate), every control that moved, the
condensed header, the one-engine invariants, and the list endpoint.

`tests/test_log_viewer_filters_and_jump.py` — **53, unchanged except its
`TEMPLATE`**: its subject moved from `Pages/logs.html` to the modal partial, so
the guards now watch the markup that is actually served. Every id assertion
passed against the new markup without relaxation — the partial carries all
eighteen ids the engine binds.

Oracle (sources reverted, tests kept): **22 failed / 26 passed** — the 26 are
the controls (markup contracts in the new partial, which has no "before"
state). `import app` clean on both sides (406 → 407 routes, `+1` for the list
endpoint).

Sweeps, all green: `test_log_viewer_filters_and_jump` 53 ·
`test_log_view_consolidation` 48 · `test_navbar_system_menu_and_search_shortcut`
25 · `test_templates_tag_attribute_integrity` 121 ·
`test_rebuilt_pages_have_no_dead_buttons` 6 ·
`test_layout_css_js_template_integrity` 262+1skip ·
`test_test_site_mobile_and_pwa` 72 · `test_unified_search_enter_submits` 12 ·
`test_label_for_targets_resolve` 11 · `test_scan_preflight_gate` 41 ·
`test_scan_log_filter` 26 · `test_queue_log_timestamp_formats` 13 ·
`test_shared_log_rotation` 12 · `test_no_undefined_names_in_production` 7 ·
`test_artist_page_contract` 26 · plus `node tests/js/logs-viewer-probe.js`
(25 checks) and `node --check` on both JS files.

The only failure in the sweep is `test_static_js_is_not_jinja[static/js/
artist_detail.js]`, the long-documented pre-existing case.

## Notes for later

* **The old page templates are still on disk** — `test_site/templates/Pages/
  logs.html` (unreachable in test_site mode) and `templates/pages/logs.html`
  (still live in live mode). The first cannot be deleted from the virtual
  workspace; delete both when the cutover is finished and live mode gets the
  modal work.
* `tests/js/logs-viewer-probe.js` (25 behavioural checks) is a manual harness —
  nothing in pytest runs it. Run it after touching `logs.js`.
* The modal's five-source `LOG_SOURCE_FILES` mapping survived server-side as
  `services.log_service.LOG_VIEW_LABELS`; `routes/logs.py`'s
  `export_log_lines_since(source=…)` still speaks source names, which is why the
  picker's labels are keyed by filename.
