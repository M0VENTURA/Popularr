# The MusicBrainz rate limit was enforced per PROCESS, and 4 schedulers ran at once

**Date:** 2026-09-20
**Area:** `services/infrastructure/api_rate_limiter.py` / `helpers/leader_lock.py` (new) / `helpers/task_manager.py`
**Status:** fixed, guarded by `tests/test_api_rate_limit_enforcement.py`

---

## Symptom

A scan hit MusicBrainz far faster than the documented 1 request/second.
MusicBrainz answered `503`, which tripped the client's circuit breaker mid-scan:

```
[WARNING] [api_clients.musicbrainz_http] MusicBrainz overloaded (5xx). Circuit breaker open for 60s.
```

## First: the theory that had to be rejected

The natural suspect was a race in `get_shared_mb_client()` — several threads
seeing `_SHARED_MB_CLIENT is None` at once and each building its own client,
each with its own limiter. That is **not** what was happening, and it could not
have been:

* the singleton is already guarded — `_INIT_LOCK` (an `RLock`) with a
  double-checked re-test inside, and the losing caller logs
  `"shared HTTP client was initialized by another caller"`;
* the throttle is **module-level**, not per client:
  `api_clients/musicbrainz_http.py` holds `_THROTTLE_LOCK` +
  `_LAST_MB_REQUEST_TIME` (and delegates to the process-wide
  `get_rate_limiter()` when available), so extra client OBJECTS never multiply
  the budget;
* every client method (`search_*`, `get_release`, `get_recording`, `browse_*`,
  `lookup_by_isrc`, …) funnels through the single throttled `get()`.

The repeated `[MB] shared HTTP client initialization requested` lines are
therefore **one per PROCESS**, and that was the real clue: the budget was being
enforced once per process, and there were several processes.

## The two real multipliers

### 1. The limiter's state was in memory, per process

`APIRateLimiter.state` is loaded once at construction and persisted to
`<state>/api_rate_limiter_state.json` — but the reservation itself happened in
memory, and `_save_state()` was throttled to **once every 30s** (last-writer-wins
on top of that).

So with `entrypoint.sh` running hypercorn with `--workers 4` plus the standalone
queue-worker process, MusicBrainz effectively received **~5 requests per second
instead of 1** — and the daily counters were wrong as well.

### 2. Every web worker started its own scheduler

`app.py` calls `initialize_app_services(app)` in every process that imports the
app. The "leader" guard only consulted `ENABLE_BACKGROUND_WORKERS`, which
**defaults to `"true"` and is identical in every worker of a container** — so a
4-worker deployment ran **four APSchedulers**, each running the same periodic
jobs (library sync, popularity scan, upcoming releases, queue processor), each
making its own API calls with its own limiter.

This is also why a "massive background sync" appeared to start at the same
moment as the foreground scan.

## What was changed

### 1. The reservation is now cross-process

`APIRateLimiter` reserves its slot with a read-modify-write of the shared state
file **under an exclusive `flock`**, so two processes cannot claim the same slot.
`flock` (rather than a lock *file*) because the kernel releases it when the
holder dies — a crashed worker can never leave the API budget permanently
locked. Applied to MusicBrainz, Last.fm (including `wait_if_needed_lastfm`) and
ListenBrainz, with **one lock per provider** so MusicBrainz never waits on
Last.fm.

Details that matter:

* the reservation returns the wait and the **sleep stays outside every lock**
  (the previous stall fix — sleeping under the lock serialised all callers);
* the per-provider daily counters are now incremented inside the same locked
  write, fixing their 30-second lossiness;
* the date-based counter reset moved inside the lock, so it no longer depends on
  whichever process happened to reload the file first;
* the state file is written to a temp file + `os.replace` (atomic);
* **if locking is unavailable** (unusual filesystem) it logs ONE warning and
  falls back to the in-process behaviour — a rate limiter must never be the
  reason the app breaks.

### 2. Real leader election (`helpers/leader_lock.py`)

An exclusive, **non-blocking** `flock` on `<state>/background_workers.lock`,
held for the life of the process. The first worker to take it starts the
scheduler; the rest log why they skipped. `ENABLE_BACKGROUND_WORKERS=false`
still force-disables the background workers everywhere (the old escape hatch).

The winning PID is written into the lock file, so which worker owns the
background jobs is now visible instead of guesswork.

### 3. `get_rate_limiter()` is thread-safe

Even though the reservation is cross-process, an unprotected double-checked
`is None` could still build two limiter OBJECTS in one process, and
`api_clients/musicbrainz_http.py` caches whichever instance it first received.

## Verification

Worktree at `origin/develop`, same edits replayed (`api_rate_limiter.py` +
`task_manager.py` + the two new files).

| Check | Result |
|---|---|
| New suite, fixes reverted | **5 failed** — the second "process" sent without waiting, the state file lost updates, the daily count did not persist, `get_rate_limiter()` handed out 2 instances, and a non-leader started the scheduler |
| New suite, fixed | **12 passed, 1 skipped** (the POSIX-only `flock` exclusivity test skips on Windows) |
| `test_scan_stall_fixes` + `test_discogs_throttle_lock` + `test_scan_async_signature_contract` + `test_scan_continuation_guard` | 7 failed / 55 passed — the **identical 7 pre-existing failures** as the reverted baseline (7/8 for that file alone), **0 new** |
| `get_errors` on all changed/added files | clean |

The rate-limiter lock tests that already existed (`test_scan_stall_fixes` —
"lock free during the cooldown sleep") still pass: the reserve-then-sleep-outside
structure is preserved.

## Also noted (not changed)

Eight modules construct `MusicBrainzHttpClient(...)` directly instead of
`get_shared_mb_client()`:

`routes/musicbrainz_routes.py`, `routes/upcoming_releases_routes.py`,
`services/downloads/download_folder_service.py`,
`services/enrichment/single_detection_service.py`,
`services/popularity/release_cache_service.py`,
`services/popularity/scan_stage_runner.py`,
`services/upcoming_releases/matching_service.py`,
`services/upcoming_releases/musicbrainz_fetcher_service.py`.

They do **not** bypass the limit (the throttle is module-level), but each builds
its own HTTP session/connection pool. Converging them on the shared client is a
clean-up, not a correctness fix — deliberately left out of this change.

## Files

- `services/infrastructure/api_rate_limiter.py` — cross-process reservation,
  shared daily counts, atomic state writes, thread-safe singleton
- `helpers/leader_lock.py` — **new**, cross-process background-worker election
- `helpers/task_manager.py` — uses the election instead of the env default
- `tests/test_api_rate_limit_enforcement.py` — **new**, 13 tests

Note: the standalone queue worker (`python -m services.queue.queue_worker`) does
**not** call `initialize_app_services`, so it keeps running exactly as before —
only the duplicate per-web-worker schedulers are gone.
