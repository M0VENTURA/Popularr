# Album-page metadata saves are reverted by the next Navidrome import

**Date:** 2026-10-03
**Reported:** *"When updating the metadata using the album page on test_site,
it says the metadata is updated on the tracks, but the next navidrome import
puts the old data back in. It's meant to update the files, which navidrome
would then sync."*

## Root cause (three breaks in one chain)

The album save **does** write the corrected tags into the audio files
(verified earlier with a real-mutagen probe). What was broken was the
"which Navidrome would then sync" half:

1. **The wait lied.** `NavidromeClient.trigger_and_wait_for_scan()` treated
   `scanning == False` as "scan finished". Navidrome's own Subsonic
   `startScan` handler only waits ~3s for the scanner goroutine to start (its
   source then logs *"response may be stale"*), so `scanning` reads `False`
   **before the scan has begun** — and an unrelated finishing scan also reads
   as `False`. The wait reported "finished" while no scan covering the file
   writes had run.
2. **The pipeline ignored the result.** `run_navidrome_import_scan` logged
   *"Remote Navidrome scan finished"* unconditionally — even when
   `startScan` failed outright (Navidrome requires an **admin** user for
   `startScan`).
3. **Nothing asked Navidrome to rescan after a save.** The album/track/tag
   save paths triggered no remote scan at all (the per-write triggers were
   removed 2026-08-30 to stop scan storms), and the remaining helper
   functions returned `True` while firing nothing — so the UI reported
   `navidrome_scan_triggered: true` as a lie.

The import then read Navidrome's **pre-save rows** and the
`ON CONFLICT DO UPDATE` upsert overwrote the metadata just saved — "the
next import puts the old data back in".

## Fix

- **`api_clients/navidrome.py` — verified drain-and-wait.**
  `trigger_and_wait_for_scan` now (phase 1) waits out any scan already
  running — Navidrome rejects a concurrent `startScan` with
  `ErrAlreadyScanning`, and a scan that started *before* our file writes may
  not see them — then records `lastScan` as the baseline, triggers
  `startScan`, and (phase 2) only reports success when `lastScan` **changes**.
  Any scan completing after that baseline also started after it, so it saw
  every file write. Three consecutive status failures fast-fail (no spinning
  a dead server for the whole deadline); the failure streak resets on every
  successful poll. Servers exposing no `lastScan` fall back to observing a
  `scanning` True→False cycle — never a bare "not scanning".

- **`services/scanning/navidrome_rescan_service.py` (new) — one coalesced
  rescan per save.** `request_rescan(reason)` is fire-and-forget (daemon
  thread) and **coalesced**: at most one scan runs, one queued follow-up, so
  a burst of N saves costs ≤ 2 scans instead of N (the property that keeps
  the 2026-08-30 scan storm from returning). It is a no-op when Navidrome is
  unconfigured, never blocks the caller, and resets its state even if the
  worker crashes.

- **`routes/ui_routes.py` — the album save requests a rescan** when it
  actually changed something (`updated_count`, `genre_only_writes`,
  `_cover_embedded`, or `reverted_live_count`); a no-op save requests none.

- **`routes/track_routes.py` / `routes/misc_routes.py` — the trigger helpers
  now delegate** to `request_rescan` and return whether the request was
  accepted, so `navidrome_scan_triggered` in the JSON responses is truthful
  again.

- **`services/scanning/pipelines/navidrome_pipeline.py` — honour the sync
  result.** The pre-import sync is extracted into
  `sync_remote_navidrome_before_import()`; on failure it logs a loud
  ⚠️ *"Remote Navidrome scan FAILED or timed out … recently saved edits could
  be overwritten"* warning (and the completion line is suffixed) instead of
  claiming "scan finished".

## Tests

- `tests/test_navidrome_remote_sync_consolidation.py` — rewritten to the new
  contract: `lastScan` must advance before success (the exact stale-read race),
  drain-before-trigger ordering, fast-fail + streak reset, legacy-server
  fallbacks, and the helpers delegating to the rescan service (they used to be
  pinned as no-ops).
- `tests/test_navidrome_rescan_after_save.py` (new) — rescan service
  coalescing (bursts ≤ 2 scans), config gating, crash reset; the album-save
  route requesting a rescan only when it changed something; and the pipeline
  helper reporting the truth for ok/failed/raising/unconfigured.
- `tests/conftest.py` — autouse stub for `request_rescan` so no route test
  spawns a real scan thread.

**Oracle:** with the source fixes stashed (tests kept) → **18 failed /
14 passed**; restored → **32 passed**. Failing-set sweep before/after across
all navidrome/album-save/rescan suites: **identical 21 pre-existing failures,
0 regressions** (auth-gate ×4, heartbeat, `delall` mutagen ×3, playlist
post-body ×5, album-art ×6, genre ×2, star-posting ×2, genre-fallback ×2,
login ×1 — each verified failing on the clean tree too). `import app` →
394 routes.
