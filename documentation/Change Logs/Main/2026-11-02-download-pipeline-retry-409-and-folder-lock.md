# Download pipeline: retry window, slskd 409, folder-import race, duplicate scheduler

**Date:** 2026-11-02
**Area:** downloads / search / queue
**Files:** `db/repositories/queue.py`, `services/queue/queue_cleanup_service.py`,
`services/downloads/slskd_service.py`, `services/downloads/download_folder_service.py`,
`app.py`, `tests/test_download_pipeline_hardening.py` (new)

## 1. A 12-hour backoff fired ~80 minutes later

Reported:

```
backing off 12h until 2026-11-04 12:24 local (attempt 2)
  | next_retry_at='2026-11-04T01:24:11+00:00'
```

…but the row was picked up at `01:44` local.

**Cause.** `download_queue.next_retry_at` is a naive `TIMESTAMP`. An aware
ISO value was assigned to it per SESSION `TimeZone`, and the due check
compared the naive column with `CURRENT_TIMESTAMP` (a `timestamptz`) — again
using the session timezone. A UTC wall clock written by one process was read
as LOCAL by another, making the row due almost immediately.

**Fix.**
- Every explicit write of `next_retry_at` is normalised to a naive UTC wall
  clock (`_as_utc_naive`).
- Every due-time comparison (`claim_queue_item`, `get_ready_for_processing`,
  `count_ready_for_processing`, `requeue_due_failed_items`, `mark_failed`,
  and the stuck-download cleanup) binds `_utc_now_naive()` / `:now_utc`
  instead of `CURRENT_TIMESTAMP`. Both sides are now the same UTC instant,
  independent of any session/process timezone.

## 2. slskd 409 "must be connected and logged in" was recorded as `no_results`

Reported twice (`23:43:19`, `00:24:10`):

```
slskd search start failed status_code=409
  preview='The server connection must be connected and logged in to perform a
           search (currently: Connected)'
```

`start_search` returned `None`, `search_and_filter` translated that to `[]`,
and the queue item climbed the 4h/12h/24h search-miss ladder even though the
search never ran.

**Fix.** `SlskdService.start_search` recognises the connection-state 409,
waits 10 s and retries (up to 3 attempts). If it still fails,
`search_unavailable_reason` is set and `search_and_filter` returns `None` —
the existing "disconnected" outcome, which parks the row for the 5-minute
retry WITHOUT counting an attempt or applying the miss backoff. A genuine
empty search still returns `[]`.

## 3. Two threads imported the same folder

Reported: `/downloads/Abyss (2024)/…/01 - My Halo.flac` was converted by two
threads at the same millisecond; the first moved/deleted it and the second
raised `[Errno 2]`. The same folder reported `moved=12` then `moved=8`.

**Fix.** `match_folder_to_release` now runs under a per-folder lock:
- a non-blocking in-process `threading.Lock` (the reported two-thread race);
- a best-effort cross-process `flock` in the shared state directory, which
  fails OPEN when locking is unavailable.

A concurrent caller returns immediately with `skipped: True`, `moved: 0`
instead of duplicating the conversion/move.

## 4. "Download scheduler started" logged twice per boot

**Cause.** `app.py` ran the leader election at IMPORT time. `entrypoint.sh`
imports the app for a pre-flight check (`python3 -c "from app import app"`)
before Hypercorn starts, so that throwaway process took the advisory lock and
started the scheduler; it exited seconds later and the real Hypercorn leader
started a second one.

**Fix.** The election moved to a Quart `@app.before_serving` hook, so importing
the app has no background side effects. A `_BACKGROUND_SERVICES_STARTED`
guard also prevents a second start in one process (the advisory lock is
per-connection, not per-process).

## 5. Mirror peers get a real cooldown

The reported `AndamentaStanly` / `InukshukTestar` pair were re-selected for the
same file every ~2h08–2h51. The reaper blocks the `(peer, file)` pair
immediately, but the PEER-wide cooldown required two stalls within
`SLSKD_PEER_STALL_COOLDOWN_SECONDS`, which defaulted to **2 hours** — just
shorter than the retry spacing, so the count always reset. The default is now
**24 hours**; two stalls in a day ban the peer for a day (still configurable).

## Tests

`tests/test_download_pipeline_hardening.py`:

| Test | Pins |
|---|---|
| `test_an_aware_utc_value_is_stored_without_an_offset` / `_non_utc_…` | `next_retry_at` is written as naive UTC |
| `test_the_due_comparison_uses_a_utc_now_bind` | reads bind a naive UTC now, never `CURRENT_TIMESTAMP` |
| `test_the_reported_body_is_recognised` / `_start_search_retries_…` | the 409 body is infrastructure; retries are bounded |
| `test_search_and_filter_returns_none_…` | the caller gets the disconnected outcome, not `[]` |
| `test_a_duplicate_import_is_skipped` / `_the_lock_holder_…` / `_released_…` | one folder import at a time |
| `test_the_election_runs_from_before_serving` / `_module_level_call_is_gone` | no scheduler on import |
