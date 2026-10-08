# Duplicate queue log line — and what the pasted advice got right (2026-10-08)

## Reported

Three items pasted from an external review. Each was checked against the code
before touching anything; **one was a real bug, one was already implemented,
and one would have contradicted a deliberate, test-pinned design.**

---

## Item 4 — duplicate `[QUEUE] Checking N completed download(s)` ✅ REAL, FIXED

The advice:

> These double lines indicate a Python `logging` configuration issue where a
> handler is either registered twice, or child logs are incorrectly propagating
> to the root handler. **Fix:** set `logger.propagate = False`.

**`propagate` was already `False` — twice.** `helpers/logging_config.py` sets
`"popularr.unified": {handlers: ["unified_file"], propagate: False}` in
dictConfig **and** re-asserts `_lg.propagate = False` before every emit. The
same is true of `popularr.queue` and `popularr.search`. That fix would have
been a no-op. (Pinned by `TestTheSuggestedFixWouldNotHaveWorked`.)

**The real cause was execution, not logging.** `services/queue/queue_lock.py`
opens by documenting the hazard:

> The standalone `queue_worker` process and the APScheduler
> `download_queue_processor` job both dispatch queue items independently.
> Without serialisation they can claim and process the same items concurrently.

Both drivers **do** exist — `entrypoint.sh:195` and
`popularr-queue-worker.service` start `python -m services.queue.queue_worker`,
and `_download_queue_processor_tick()` spawns a `queue-cycle` thread.

But `process_cycle()` ran `run_maintenance()` **before** taking the lock; only
`process_next_batch()` took it. So **both drivers ran every maintenance hook**,
including `check_completed_downloads` — whose line therefore printed twice per
interval. It is not merely cosmetic: that hook moves files and writes rows, so
two processes could reconcile the same transfer concurrently — precisely what
`queue_lock` exists to prevent.

**Fix:** `process_cycle()` now takes `queue_cycle_lock()` **first** and runs
both the maintenance pass and the batch inside it (`process_next_batch(...,
use_cycle_lock=False)`, so the cycle never waits on its own lock). A contended
lock skips the whole cycle — its holder's maintenance pass covers the same work.

**Two secondary defects found while fixing it:**

* `_ok()` already returns `(payload, status)`. My first draft wrote
  `return _ok(...), 200`, which **nests the tuple** and hands the caller a tuple
  where a dict belongs. Caught by the test, not by reading.
* `queue_lock._file_lock` did a bare `import fcntl` — POSIX-only. On a
  non-Postgres, non-POSIX platform it raised `ModuleNotFoundError` straight out
  of `process_cycle`, taking the whole cycle down instead of degrading. It is a
  **best-effort** lock by contract, so it now degrades to "always acquired" with
  a one-time warning. Production always takes the PostgreSQL advisory path.

---

## Item 3 — debounce Navidrome rescans ✅ ALREADY IMPLEMENTED (2026-10-05)

The advice:

> Instead of firing a scan command immediately after every single album save,
> batch the requests … at a maximum frequency of once every 15 minutes.

`services/scanning/navidrome_rescan_service.py` already does exactly this:

* **Coalescing** — at most one running scan plus one queued follow-up, so bursts
  of saves collapse into one scan;
* **Rate limiting** — `MIN_SCAN_INTERVAL_SECONDS = 300` (5 min) between runs,
  measured from the *start* of the previous run, specifically because the
  reported log showed *12 rescans in 35 minutes with gaps as short as 44
  seconds*, which is what starved `getScanStatus`;
* All three save paths go through it (`routes/misc_routes.py`,
  `routes/track_routes.py`, `routes/ui_routes.py`) — no call site fires
  `start_scan()` directly.

**Not changed.** 300 s is a tuned constant with its reasoning in the docstring;
raising it to 900 s would make Navidrome's index up to 15 min staler for a
benefit that has not been measured. Say the word and it is a one-line change.

---

## Item 2 — strict timeouts and a 5-attempt cap ⚠️ NOT APPLIED, as written

**The timeout already exists.** `api_clients/slskd_http.py` sets
`default_timeout = 60` and every `request()` passes it (with per-call
overrides: 8 s for list/status polls, 4 s for cleanup). `start_search` already
retries at most `max_attempts = 5`. The 180 s `STUCK_SEARCH_TIMEOUT_MS` is not
a worker block — it is the threshold at which `clear_stale_searches()` *cancels*
a search slskd has left running.

**The 5-attempt cap contradicts a pinned design and was not applied.**

* `queue.search_backoff_hours` (Config → *Search Miss Backoff*) already
  controls the ladder: **4 h → 12 h → 24 h → 24 h …**, so a search miss does
  **not** retry 60 times quickly — after three misses it retries once a day.
* `tests/test_queue_retry_chain_wiring.py::TestSearchBackoffNeverAbandons`
  pins this deliberately: *"Even absurd retry counts keep producing a window"*,
  *"backoff must not shrink as attempts grow"*, *"After many search misses the
  item still returns to the queue."* A `no_qualifying_result` count of 60 is
  what a 24 h cap looks like after two months — it is the design working.
* Marking an item permanently failed after 5 attempts (i.e. ~40 h) would drop
  tracks that appear on Soulseek later, which is the case the ladder exists for.

**The advice also said "delete … directly from your queue interface or SQLite
database"** — the application is PostgreSQL-only (`db/engine.py`,
`docker-compose.yml`, `entrypoint.sh`). Use the queue UI or `psql`; there is no
SQLite data file to edit.

If you still want a hard cap it is a small change with a real trade-off, and it
must update `TestSearchBackoffNeverAbandons` — but I would want that decision
to be explicit rather than inherited from a review that assumed SQLite.

---

## Verification

* `tests/test_queue_cycle_maintenance_is_locked.py` — **6** (4 behavioural + 2
  controls, one of which pins that `propagate` was already `False`).
* **Oracle** — reverting only `queue_orchestrator.py` → **4 failed / 2 passed**;
  exactly the 4 behavioural tests fail and both controls pass. Restored → 6.
* **Sweep** — 45 queue/download/lock/worker/retry files, clean `origin/develop`
  vs this change: **base `25 failed / 468 passed`** vs **new `21 failed / 472
  passed`**; `Compare-Object` on the sorted `^FAILED` lines = **empty for "only
  in CHANGED"**. The 4 that only fail at BASE are this change's own tests (the
  oracle), so **0 regressions** and the other 21 are identical pre-existing
  failures — among them the documented `test_sibling_torrents_root_is_searched`
  hash-order flake.
