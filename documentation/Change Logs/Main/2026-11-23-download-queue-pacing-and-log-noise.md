# Download queue, second pass: pacing, the outage repair, log volume, watchdog window

**Date:** 2026-11-23
**Area:** `services/downloads/download_pipeline_service.py` ·
`services/queue/queue_cleaner.py` · `services/downloads/download_completion_service.py` ·
`services/queue/queue_heartbeat.py` · `helpers/config_helpers.py` ·
`routes/queue/processing_routes.py`
**Commit:** `fix(queue): pace searches, repair outage misses, quieten the log`

The remainder of the queue report: search pacing, the manual outage repair, the
title repair, and the two items deferred from the first pass.

---

## Search pacing (was: 57 s average, several hitting the 50-result limit)

Two changes, both aimed at the same cost:

**The fallback tail is capped at 4, and configurable.** Each fallback is a FULL
search with its own wait ceiling, so a fixed tail of 8 was what made a single
unlocatable track cost minutes. `queue.max_fallback_queries` (default 4, `0`
disables).

**A rejected primary no longer burns the tail.** `_build_fallback_search_queries`
exists for a query that surfaced NOTHING. When the primary query DID return
candidates and every one was rejected on its merits — wrong artist, wrong title,
wrong album — a looser query only finds the same files again. It now bails out.

⚠️ Worth recording: the first cut of the cap read
`get_queue_search_tuning()` without importing it, and the surrounding `except`
swallowed the `NameError`, so the pacing silently did nothing. Caught by the test
that asserts `0` disables fallbacks; the read is now a local import.

## The outage repair is reachable

`POST /api/queue/clear-outage-misses` wraps
`services.queue.outage_miss_service`, `dry_run` defaulting to **true** — it
reports which rows it would release before touching any. Only rows whose stamp
falls inside a detected run of consecutive zero-result/disconnected searches are
touched, so a genuine miss keeps the backoff it earned.

## The artist no longer repeats in the title

`Orpheus Omega / Triple Kill` + `Triple Kill - Stacy's Mom` → `Stacy's Mom`, in
`clean_mangled_queue_item`. Only a WHOLE credited artist is removed, only from
the front, and only when a tail remains — so `Triple Kill` is not a prefix of
`Killing Me Softly` and an ordinary title can never be eaten.

## Log volume (3,404 of 12,406 lines)

`check_completed_downloads`' stage timings were an unconditional INFO line from a
hook that runs every cycle. Now DEBUG normally and **WARNING when the pass takes
≥ 5 s** (`_SLOW_PASS_WARNING_MS`), so the breakdown is there exactly when someone
is looking for it.

## Watchdog window (12 restarts at "181–256 s against a 180 s limit")

Raised from 3 to **5** configured cycles (300 s at the 60 s default), and given
`QUEUE_PROCESSOR_STALE_AFTER_SECONDS` as an operator override.

The heartbeat refreshes every half-window WHILE a cycle runs, so an age past the
window means the cycle itself overran it — and a cycle runs `run_maintenance()`
inline, whose filesystem hooks were measured at a 44 s average and a 12-minute
peak. A legitimate four-minute cycle therefore looked dead, and the "restart" it
triggered only re-arms the driver.

---

## Verification

New tests: `test_queue_search_pacing.py` (11),
`test_queue_log_volume_and_watchdog.py` (6).
`test_the_staleness_window_follows_the_configured_interval` pinned the old 3×
multiplier and was updated to 5× with the reason recorded.

Full suite compared against the baseline failure set by diff.
