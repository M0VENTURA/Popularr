# Forced Navidrome rescan after a metadata save kept the server scanning (2026-10-05)

**Report:**

> I think the forced navidrome scan after a metadata update is crashing the
> system

…followed by a log in which every album/track save ends in
`Navidrome rescan already running — coalesced into follow-up`, full-library
`Navidrome remote scan complete count=67877` lines repeating every one to
three minutes, and `getScanStatus … ReadTimeout` warnings. Confirmed symptom:
**Popularr's own UI/API hung.**

## Root cause

`services/scanning/navidrome_rescan_service.py` fires **one full Navidrome
library rescan per user-initiated metadata save**, coalesced only by a
`pending` flag.

The `pending` flag collapses a **burst** — it was designed for exactly that —
but it did nothing for a **trickle**. Editing albums one after another
produced a fresh scan after each one:

    12 rescans in 35 minutes, gaps as short as 44 seconds

Counting from the reported log:

| Completed | Gap |
|---|---|
| 20:16:15 | 44 s |
| 20:24:08 | 7 m 53 s |
| 20:32:12 | 1 m 53 s |
| 20:33:29 | 1 m 17 s |
| 20:34:24 | 55 s |
| 20:39:15 | 1 m 08 s |

Navidrome therefore **never got to rest**: its `getScanStatus` began timing
out (`ReadTimeout` on attempt 1/2 throughout the log), and because
`/api/navidrome/scan/status` is a *synchronous* view — Quart runs those in
the default executor (`asyncio_*` threads, which is exactly the thread tag in
the log) — every dashboard poll of that endpoint sat behind a slow Navidrome
response. The dashboard polls it every 5 s and the genre dialog every 1 s, so
once Navidrome stalled, Popularr's own requests queued behind it and the UI
stopped responding.

This is the same failure mode that removed the original per-tag-write
`startScan` triggers on 2026-08-30 ("firing a scan after EVERY tag write
paused the Navidrome server and locked its database") — the coalescer fixed
bursts but not a steady trickle of saves.

## Why rate limiting is safe

The post-save rescan only keeps **Navidrome's own index** fresh. It is *not*
load-bearing for correctness: the import runs its own drain-and-wait sync
(`sync_remote_navidrome_before_import`) before reading Navidrome, so an
import can never read pre-save rows just because this background refresh was
deferred. A save is also never **dropped** — the run that would have started
immediately is started once the interval elapses.

## Changes

- `services/scanning/navidrome_rescan_service.py`
  - New `MIN_SCAN_INTERVAL_SECONDS = 300` — a minimum gap between two rescan
    **runs**, measured from the *start* of the previous run (so a slow run
    pays part of its own cooldown instead of being padded again).
  - `_worker` now, after seeing `pending`, sleeps out the remainder of that
    interval before the follow-up run. The `pending` flag is cleared *before*
    the wait so a save arriving during the wait is the one recorded next.
  - The deferral is logged to the unified scan log
    (`Navidrome rescan deferred Ns (rate limit 300s) (reason)`) so an
    operator watching Navidrome go stale sees why.
  - Kept as a module constant (not a config option) to match the sibling
    tuning knobs of this feature — `poll_interval_seconds` /
    `max_wait_seconds` are constants too.
- `tests/test_navidrome_rescan_after_save.py`
  - The harness now stubs `sleep` (otherwise the suite would cost five real
    minutes) and records the requested wait.
  - New `TestRateLimitBetweenRuns` (5 tests): a lone save is **never**
    delayed, a queued save is **delayed, not dropped**, a run that already
    outlasted the interval needs no extra wait, the wait is
    `interval − elapsed` only, and the coalescer resets and is reusable.

## Verification

- **Oracle:** stashing `navidrome_rescan_service.py` fails **3 of the 5** new
  tests with behavioural messages ("the follow-up must be deferred exactly
  once", "must expose a positive MIN_SCAN_INTERVAL_SECONDS"); the other two
  are guards already true at baseline. Markers restored after `stash pop`.
- Rescan suites: `test_navidrome_rescan_after_save.py` +
  `test_navidrome_remote_sync_consolidation.py` → **37 passed**.

## Not changed (observed, for follow-up)

Three other load paths were inspected while tracing the hang and left alone
because they are outside this report:

1. `/api/navidrome/scan/status` is a synchronous view doing a blocking HTTP
   call (`timeout=10`, one retry) in the default executor — it is the path
   that stalled once Navidrome slowed down. Rate limiting removes the trigger;
   caching the status briefly would remove the exposure entirely.
2. `/api/navidrome/scan/start` is also synchronous and can block an executor
   thread for up to `max_wait_seconds=1800` (30 min) — one manual click pins a
   worker for that long.
3. The album save route (`async def album_detail`) performs the per-track
   tag writes synchronously on the event loop, so a large album stalls every
   other request for the duration of the writes.
