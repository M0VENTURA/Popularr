# Safe shutdown: quiesce the app so PostgreSQL can be stopped cleanly

**Date:** 2026-10-15
**Files:** `services/maintenance/safe_shutdown_service.py` (new),
`routes/maintenance_routes.py` (new), `db/engine.py`,
`services/scheduler/scheduler_service.py`, `services/queue/queue_worker.py`,
`helpers/config_helpers.py`, `helpers/app_bootstrap.py`, `routes/ui_routes.py`,
both Config pages + their JS, `scripts/safe-shutdown.sh` (new)

Requested:

> Would it be possible to have a safe shutdown option in the config that will
> stop the database for a clean update of the postgres server?

## Why `docker compose stop db` is not enough on its own

Two independent problems, and "safe" means fixing both.

**1. PostgreSQL does not get a clean stop.** `docker stop` sends **SIGTERM**, and
PostgreSQL treats SIGTERM as a **smart shutdown**: it stops accepting work, then
**waits for every client to disconnect**. The application's connection pool holds
idle connections open, and an idle pooled connection is still a client — so the
postmaster keeps waiting until Docker's stop timeout expires and the container is
**SIGKILLed**. The stop command "succeeds" either way, and the next start then
does crash recovery.

**2. The database is stopped underneath whatever the app is doing.** A scan or a
queue cycle mid-write against a server that is going away is the "engine
disposed" cascade this codebase already suffers from.

## What was built

The sequence, in the order that matters:

| step | what | why it is in this position |
|---|---|---|
| 1 | **quiesce** — pause the scheduler, ask running scans to stop, tell the queue worker to idle | the scheduler's job store holds a DB connection, so it must stop firing before the pool is touched |
| 2 | **drain** — wait for in-flight scans and queue items | a queue item can be mid-move of a large file; a timeout **reports** what is left rather than forcing it |
| 3 | **release** — dispose the pool | this is the step that makes the postmaster's smart shutdown finish at once instead of waiting to be killed |
| 4 | **stop** — `docker compose stop db` | and then **verify the exit code**, because a force-kill is otherwise invisible |

### Design points worth knowing

- **The flag is a state-dir file, not a database row** (`/state/safe_shutdown.json`).
  It has to be readable while the database is *down* — that is the whole point.
  Written via write-then-rename so the worker polling it never reads a truncated
  file and concludes "not requested".
- **The flag auto-expires** (`auto_cancel_after_minutes`, default 120). A
  persisted "quiesced" flag with no expiry would leave the application permanently
  dormant after a forgotten shutdown: it would look like a hang with no visible
  cause. Expiry happens on read, and the status reports the remaining time.
- **The engine is KEPT, only its pool is disposed.** `dispose_engine()` closes
  every connection but leaves the engine object, so SQLAlchemy opens a fresh one
  on the next checkout and the app recovers by itself when the database returns.
  Discarding the engine would drop the memoised session factory and any session
  bound to it, and would require a restart.
- **`peek_scheduler()` was added**, because `get_scheduler()` *creates* the
  scheduler on demand — including its SQLAlchemy job store, which opens a database
  connection. Asking "is the scheduler paused?" would have built a scheduler and a
  connection as a side effect, and would report a freshly-built, never-started
  instance as the current state. Status must observe and never cause.
- **`safe_to_stop` is derived, not asserted** — `requested AND no scans AND queue
  empty`. Reporting it as soon as the flag was set would tell the operator to stop
  the database while a scan was still writing to it.
- **The queue worker IDLES rather than exiting.** Exiting would leave no worker
  until the container restarted, so cancelling a safe shutdown could not resume
  anything. It keeps stamping its heartbeat, because a quiesced worker is alive and
  idle — not dead — and suppressing it would make the status card lie.
- **The worker's flag check FAILS OPEN.** An unreadable flag means "keep working".
  Failing closed would silently stop all download processing because of a
  permissions problem or a bad state directory, and the queue would look broken
  with nothing in the log to explain it. A missed quiesce is recoverable; a queue
  that stops for no visible reason is not.

### Surfacing it

- `GET/POST /api/maintenance/safe-shutdown`, `POST .../cancel`,
  `POST .../release-connections`. All behind the central auth gate (pinned by a
  parametrised test — including the status route, which discloses what the
  instance is doing).
- Config: `maintenance.safe_shutdown` (`enabled`, `drain_timeout_seconds`,
  `auto_cancel_after_minutes`, `release_connections`) on **both** Config pages,
  with the nested block registered in `_sanitize_config_sections` — without that
  entry the page save coerces it to `{}` and the settings silently reset.
- A **Safe Shutdown** card on the System tab with *Prepare for Database Stop*,
  *Resume Normal Operation* and *Refresh Status*, plus the exact host command and
  a note on why the plain `docker stop` is not enough. Status is fetched on load,
  so landing on the page shows a dormant app rather than a stale "not checked".
- `scripts/safe-shutdown.sh` with `status | prepare | resume | stop-db |
  start-db | upgrade`. It drives the app through `docker compose exec`, **not** the
  HTTP API — no session or credentials, and it still works when the web server is
  unhealthy.

## What this deliberately does NOT do

**The app cannot stop the database container itself.** There is no Docker socket
mounted (mounting it would give the application root-equivalent access to the
host), and adding a config field that runs an arbitrary shell command from a web
UI would be a remote-code-execution hole. So the app guarantees it is *safe* to
stop and names the command; the operator runs it. That is also why the script
verifies the exit code instead of trusting the stop.

`upgrade` also warns that a **major version change** (16 → 17) makes the data
directory incompatible — PostgreSQL will refuse to start and that needs
`pg_upgrade` or a dump/restore. The script only handles a clean stop and restart.

## Verification

- `tests/test_safe_shutdown.py` (27) and `tests/test_safe_shutdown_api.py` (27).
- **Oracle: 5 mutations, all 5 caught** — removing the auto-expiry, asserting
  `safe_to_stop` instead of deriving it, discarding the engine instead of keeping
  it, making the unreadable flag fail *closed*, and releasing connections even when
  the flag failed to write. Each mutation disables one guard and the test that
  claims to pin it fails.
- Sweep over `shutdown|maintenance|scheduler|queue_worker|config|engine|db_`:
  3 failures, **all three pre-existing** in the recorded baseline. Zero
  regressions. (The `/config` bottom-nav failure also passes in isolation — it is
  pre-existing test-order dependence, not the new card.)
- `import app` → 412 routes (4 new). 725 Python files parse.
