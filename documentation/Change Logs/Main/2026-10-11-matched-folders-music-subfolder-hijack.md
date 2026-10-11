# Matched Folders went from 27 items to 0 — a stray empty `downloads/Music` folder hijacked resolution

Date: 2026-10-11
Branch: develop

## Reported

*"A download queue and a matching folder queue … it was working approx 24
hours ago, now the matched folders is missing on the test_site."*

## Diagnosis (proven live, not inferred)

Probed the running instance (`scanner.ungoogle.au`) with the user logged in:

* `GET /api/downloads/unmatched-folders` → `{"count": 0, "folders": [],
  "success": true}` — a CLEAN empty (no exception path).
* `POST /api/downloads/discover` → **159 files** under `/downloads`
  (145 in `Torrents/`, 14 top-level album folders) — the downloads root is
  intact and populated.
* **0 files under `*/downloads/Music/*`**.
* Config page: `downloads.folder = "/downloads"` — correct.
* Queue log: completions/imports running against `/downloads/<album>/…`
  all day — the pipeline is healthy.

⭐ **ROOT CAUSE — a directory-existence heuristic, not the UI and not the
new monitor page.** `resolve_downloads_dir()` used to prefer a `Music`
**subfolder** of the downloads root whenever it merely EXISTED
(`_prefer_music_subfolder`) — an **empty** directory was enough. Something
(overwhelmingly likely a torrent literally named "Music" whose files were
then deleted by the mismatch cleanup the queue log shows running today)
left exactly that. Every caller using the DEFAULT resolution — the whole
Matched-Folders service, the watcher, the cleanup engine — switched to
scanning an empty folder, while the completion/import pipeline, which had
passed `prefer_music_subfolder=False` since August, kept finding files
under the real root. The heuristic silently split the app in two, and the
feature that "broke24 hours ago" broke the moment the empty folder
appeared — no code change was involved.

The rebuilt monitor page (`fce50f59`, shadow lifted by `06bb70de`) was
correctly hiding the section because the (hijacked) endpoint honestly
reported zero folders — the UI did its job; it was fed the wrong answer.

## Fix — retire the preference outright

`_prefer_music_subfolder` and the `resolve_downloads_dir` kwarg are GONE
(`services/infrastructure/filesystem_service.py`); the configured
downloads folder IS the downloads folder. The docstring records the
incident. Knock-ons, all aligned:

* default fallback `"/downloads/Music"` → `"/downloads"` (a path that need
  not exist; matches the Config page and `base.py`);
* every explicit `prefer_music_subfolder=False` call site simplified:
  completion service (×2), organize helpers, scan service (×2), the
  monitor route — plus `resolve_original_archive_dir()`, which the first
  grep MISSED and the new test suite caught at runtime (TypeError);
* the retired kwarg now raises TypeError, so a reintroduction fails fast.

Self-healing: the stray empty `Music` folder now shows up in the fixed
Matched Folders list as an **empty folder** the existing "Prune All"
button removes.

## Tests

* New `tests/test_downloads_dir_is_never_hijacked.py` (9): the empty
  Music-subfolder no longer redirects; config/env still win; default is
  the root; a source walk (comments/docstrings stripped — the recorded
  regex trap) forbids the retired kwarg anywhere in production code;
  `_prefer_music_subfolder` is gone; `get_unmatched_folders` lists the
  real album folders with the hijacker present; discover and resolution
  agree on one root.
* `tests/test_download_completion_compilation.py::TestMonitoredDownloadsDirResolution`
  updated: pins that the completion resolver passes NO kwargs.
* **Oracle** at pre-fix develop (`3821decc`): **7 failed / 2 passed**
  (2 = either-way controls) + the contract test fails.
* **Sweep** 31 downloads/completion/scan/organize suites: base `17 failed
  / 409 passed` vs new `17 failed / 409 passed`, failing sets **identical**
  → **0 regressions**.

## For the running instance

Rebuild/restart the container from this develop tip. The empty
`/downloads/Music` folder itself needs no manual deletion — it will appear
as a prunable empty folder in the restored list.
