"""Quiesce the application so PostgreSQL can be stopped cleanly.

The problem this solves
-----------------------
Updating the PostgreSQL server means stopping it. The obvious command is
``docker compose stop db``, and it is not safe on its own:

* ``docker stop`` sends **SIGTERM**, and PostgreSQL treats SIGTERM as a **smart
  shutdown** — it stops accepting work, then *waits for every client to
  disconnect*. The application's connection pool holds idle connections open, and
  an idle pooled connection is still a client, so the postmaster keeps waiting
  until Docker's stop timeout expires and the container is **SIGKILLed**. The
  next start then does crash recovery.
* Stopping the database underneath a running scan, download or queue cycle means
  the app keeps issuing writes against a server that is going away, which in this
  codebase surfaces as the "engine disposed" cascade — every in-flight statement
  fails and retries against a pool that is being torn down.

So "safe" has two halves, and this module owns both: **stop doing work**, and
**stop holding connections**.

The sequence
------------
1. ``request_safe_shutdown()`` writes a flag, pauses the scheduler, asks every
   active scan to stop, and tells the queue worker to go idle.
2. ``wait_for_quiescence()`` polls until no scan and no queue item is in flight,
   up to a timeout. It reports what is still running rather than pretending.
3. ``release_database_connections()`` disposes the pool — see
   ``db.engine.dispose_engine`` for why that is what makes the postmaster's smart
   shutdown finish promptly.
4. The operator stops the database (``docker compose stop db``), updates it, and
   starts it again. Nothing else is required: the engine is kept, so the next
   query opens a fresh connection and the app recovers on its own.

Reversibility
-------------
Every step here is reversible. ``cancel_safe_shutdown()`` resumes the scheduler
and clears the flag, and the worker picks work up again on its next cycle.

⚠️ The flag AUTO-EXPIRES. A persisted "quiesced" flag with no expiry would leave
the application permanently dormant after a forgotten shutdown — it would look
like a hang, and the cause would be invisible. ``auto_cancel_after_minutes``
bounds that, and the status reports the remaining time so it is never a surprise.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Filename under ``SCAN_STATE_DIR``. A JSON flag file, like the scan progress
#: and rate-limiter state beside it — deliberately NOT a database row, because
#: the whole point is to be readable while the database is unavailable.
STATE_FILE_NAME = "safe_shutdown.json"


def _state_path() -> str:
    from helpers.config_helpers import get_state_directory

    return os.path.join(get_state_directory(), STATE_FILE_NAME)


def get_safe_shutdown_config() -> dict[str, Any]:
    """The ``maintenance.safe_shutdown`` settings, with defaults."""
    from helpers.config_helpers import get_safe_shutdown_config as _getter

    return _getter()


def _read_state() -> dict[str, Any]:
    path = _state_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("Safe shutdown state unreadable", path=path, error=str(exc))
        return {}


def _write_state(state: dict[str, Any]) -> bool:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write-then-rename so a reader never sees a half-written flag: the
        # queue worker polls this file, and a truncated read would look like
        # "not requested" and let a cycle run against a database about to stop.
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.warning("Could not write safe shutdown state", path=path, error=str(exc))
        return False


def _parse_iso(value: Any) -> datetime | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _expiry(state: dict[str, Any]) -> datetime | None:
    requested = _parse_iso(state.get("requested_at"))
    if requested is None:
        return None
    minutes = state.get("auto_cancel_after_minutes")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return None
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=timezone.utc)
    return requested + timedelta(minutes=minutes)


def is_safe_shutdown_requested() -> bool:
    """True while the application should stay quiesced.

    This is the function the queue worker polls, so it also performs the
    auto-expiry: an expired request clears itself on read rather than lingering
    until something else happens to look at it.
    """
    state = _read_state()
    if not state.get("requested"):
        return False

    expiry = _expiry(state)
    if expiry is not None and datetime.now(timezone.utc) >= expiry:
        logger.info(
            "Safe shutdown request expired — resuming normal operation",
            requested_at=state.get("requested_at"),
            auto_cancel_after_minutes=state.get("auto_cancel_after_minutes"),
        )
        cancel_safe_shutdown(reason="auto-expired")
        return False

    return True


# ---------------------------------------------------------------------------
# Quiescing
# ---------------------------------------------------------------------------

def _scheduler_paused(scheduler: Any) -> bool:
    """True when the scheduler is running but not firing jobs.

    APScheduler's state is an int; the named constants are used rather than the
    literal so this does not silently invert if the library ever renumbers them.
    """
    try:
        from apscheduler.schedulers.base import STATE_PAUSED

        return bool(scheduler.running and scheduler.state == STATE_PAUSED)
    except Exception:
        return False


def _pause_scheduler() -> dict[str, Any]:
    """Pause the APScheduler jobs. Reversible, unlike ``stop_scheduler()``.

    ``stop_scheduler()`` shuts the scheduler down and drops the singleton, so
    resuming means rebuilding it — and rebuilding re-registers every job against
    a database that may still be down. ``pause()`` keeps the jobs and their
    triggers intact and simply stops firing them.
    """
    result: dict[str, Any] = {"paused": False, "was_running": False, "error": None}
    try:
        from services.scheduler.scheduler_service import peek_scheduler

        scheduler = peek_scheduler()
        if scheduler is None:
            # No scheduler has been built, so there are no jobs to stop firing.
            # Creating one here would register every job against a database the
            # operator is about to take down — the opposite of quiescing.
            return result
        result["was_running"] = bool(scheduler.running)
        if scheduler.running and not _scheduler_paused(scheduler):
            scheduler.pause()
            result["paused"] = True
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("Could not pause the scheduler", error=str(exc))
    return result


def _resume_scheduler() -> bool:
    try:
        from services.scheduler.scheduler_service import peek_scheduler

        scheduler = peek_scheduler()
        if scheduler is not None and _scheduler_paused(scheduler):
            scheduler.resume()
            logger.info("Scheduler resumed")
            return True
    except Exception as exc:
        logger.warning("Could not resume the scheduler", error=str(exc))
    return False


def _request_scan_stops() -> list[str]:
    """Ask every RUNNING scan to stop. Returns the scan types asked.

    Only running scans are asked, so a quiesce does not leave a spurious stop
    request lying around for a scan the operator starts later.
    """
    asked: list[str] = []
    try:
        from services.scanning.scan_state import (
            get_scan_progress_path,
            request_scan_stop,
        )

        running = _active_scan_types()
        for scan_type in running:
            try:
                request_scan_stop(get_scan_progress_path(scan_type), scan_type=scan_type)
                asked.append(scan_type)
            except Exception as exc:
                logger.debug(
                    "Could not request scan stop", scan_type=scan_type, error=str(exc)
                )
    except Exception as exc:
        logger.debug("Scan stop requests unavailable", error=str(exc))
    return asked


def _active_scan_types() -> list[str]:
    try:
        from services.scanning.pipelines.progress_service import get_scan_progress

        progress = get_scan_progress() or {}
        return [
            str(entry.get("scan_type") or "")
            for entry in (progress.get("active_scans") or [])
            if entry.get("scan_type")
        ]
    except Exception as exc:
        logger.debug("Could not read scan progress", error=str(exc))
        return []


def _inflight_queue_items() -> int:
    """Queue rows that are actively being worked, not merely waiting.

    ``ACTIVE_QUEUE_STATUSES`` is the shared definition of "active work"; using it
    rather than a hand-written list keeps this in step with what the queue page
    and the worker consider active.
    """
    try:
        from sqlalchemy import text

        from db.engine import db_session
        from services.queue.queue_constraints import ACTIVE_QUEUE_STATUSES

        statuses = ", ".join(f"'{s}'" for s in sorted(ACTIVE_QUEUE_STATUSES))
        with db_session() as session:
            row = session.execute(
                text(
                    f"SELECT COUNT(*) FROM download_queue WHERE status IN ({statuses})"
                )
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.debug("Could not count in-flight queue items", error=str(exc))
        return 0


def request_safe_shutdown(
    *,
    reason: str = "manual",
    auto_cancel_after_minutes: int | None = None,
) -> dict[str, Any]:
    """Quiesce the application: scheduler paused, scans stopping, worker idling.

    Does NOT release the database connections — that is a separate, deliberate
    step (``release_database_connections``) because it is the one action that
    makes the app unable to answer until the database returns.

    Returns the status, including what is still running.
    """
    cfg = get_safe_shutdown_config()
    minutes = (
        cfg.get("auto_cancel_after_minutes")
        if auto_cancel_after_minutes is None
        else auto_cancel_after_minutes
    )
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 0

    state = {
        "requested": True,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason or "manual"),
        "auto_cancel_after_minutes": max(0, minutes),
    }
    if not _write_state(state):
        return {
            "success": False,
            "error": (
                "could not write the safe shutdown flag — check that the state "
                "directory is writable"
            ),
        }

    scheduler = _pause_scheduler()
    scans = _request_scan_stops()

    logger.info(
        "Safe shutdown requested",
        reason=state["reason"],
        scheduler_paused=scheduler.get("paused"),
        scans_asked_to_stop=scans,
        auto_cancel_after_minutes=state["auto_cancel_after_minutes"],
    )

    try:
        from helpers.logging_config import log_unified

        log_unified(
            "[MAINTENANCE] Safe shutdown requested — scheduler paused, scans "
            "stopping, queue worker going idle. Stop PostgreSQL once the status "
            "reports SAFE TO STOP."
        )
    except Exception:
        pass

    return {"success": True, **safe_shutdown_status()}


def cancel_safe_shutdown(*, reason: str = "manual") -> dict[str, Any]:
    """Undo a safe shutdown: clear the flag and resume the scheduler."""
    path = _state_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        logger.warning("Could not remove the safe shutdown flag", error=str(exc))
        return {"success": False, "error": str(exc)}

    resumed = _resume_scheduler()
    logger.info("Safe shutdown cancelled", reason=reason, scheduler_resumed=resumed)

    try:
        from helpers.logging_config import log_unified

        log_unified("[MAINTENANCE] Safe shutdown cancelled — normal operation resumed")
    except Exception:
        pass

    return {"success": True, "scheduler_resumed": resumed, **safe_shutdown_status()}


def wait_for_quiescence(timeout_seconds: int | None = None) -> dict[str, Any]:
    """Poll until no scan and no queue item is in flight, or the timeout expires.

    Deliberately does NOT fail on timeout. A queue item can be mid-move of a large
    file, and killing that is worse than telling the operator it is still running
    — so the result reports what is left and leaves the decision to them.
    """
    cfg = get_safe_shutdown_config()
    timeout = (
        cfg.get("drain_timeout_seconds")
        if timeout_seconds is None
        else timeout_seconds
    )
    try:
        timeout = max(0, int(timeout))
    except (TypeError, ValueError):
        timeout = 60

    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        scans = _active_scan_types()
        queue_items = _inflight_queue_items()
        polls += 1
        if not scans and queue_items == 0:
            return {
                "drained": True,
                "polls": polls,
                "timed_out": False,
                "active_scans": [],
                "queue_items": 0,
            }
        if time.monotonic() >= deadline:
            return {
                "drained": False,
                "polls": polls,
                "timed_out": True,
                "active_scans": scans,
                "queue_items": queue_items,
            }
        time.sleep(1.0)


def release_database_connections() -> dict[str, Any]:
    """Close the pool so PostgreSQL's smart shutdown has no client to wait for.

    The engine is kept, so the app reconnects by itself once the database is back
    — this is what makes the whole sequence reversible without a restart.
    """
    from db.engine import dispose_engine

    report = dispose_engine()
    logger.info(
        "Database connections released for shutdown",
        disposed=report.get("disposed"),
        errors=report.get("errors"),
    )
    return report


def safe_shutdown_status() -> dict[str, Any]:
    """Everything an operator needs to decide whether it is safe to stop the DB.

    ``safe_to_stop`` is the single answer, and it is derived — not asserted. It
    reports False while a scan or a queue item is still running, because stopping
    PostgreSQL underneath one is precisely what this feature exists to avoid.
    """
    state = _read_state()
    requested = bool(state.get("requested"))
    expiry = _expiry(state) if requested else None

    scans = _active_scan_types()
    queue_items = _inflight_queue_items()

    scheduler_state = "not_started"
    scheduler_paused = False
    try:
        from services.scheduler.scheduler_service import peek_scheduler

        scheduler = peek_scheduler()
        if scheduler is not None:
            scheduler_paused = _scheduler_paused(scheduler)
            if not scheduler.running:
                scheduler_state = "stopped"
            elif scheduler_paused:
                scheduler_state = "paused"
            else:
                scheduler_state = "running"
    except Exception:
        scheduler_state = "unknown"

    status: dict[str, Any] = {
        "requested": requested,
        "requested_at": state.get("requested_at"),
        "reason": state.get("reason"),
        "auto_cancel_after_minutes": state.get("auto_cancel_after_minutes"),
        "expires_at": expiry.isoformat() if expiry else None,
        "scheduler": scheduler_state,
        "scheduler_paused": scheduler_paused,
        "active_scans": scans,
        "queue_items": queue_items,
        "safe_to_stop": bool(requested and not scans and queue_items == 0),
    }

    if expiry is not None:
        status["seconds_until_expiry"] = max(
            0, int((expiry - datetime.now(timezone.utc)).total_seconds())
        )

    return status


def prepare_for_database_shutdown(
    *,
    reason: str = "manual",
    drain: bool = True,
) -> dict[str, Any]:
    """The whole sequence: quiesce, drain, then release the connections.

    This is the call the UI button and the host script both make, so there is one
    implementation of the ordering. The order is not cosmetic: the scheduler's job
    store holds a database connection, so the pool must not be disposed until the
    scheduler has stopped firing.
    """
    requested = request_safe_shutdown(reason=reason)
    if not requested.get("success"):
        return requested

    result: dict[str, Any] = {"success": True, "quiesce": requested}
    if drain:
        result["drain"] = wait_for_quiescence()

    result["database"] = release_database_connections()
    result.update(safe_shutdown_status())
    result["success"] = True

    try:
        from helpers.logging_config import log_unified

        verdict = "SAFE TO STOP" if result.get("safe_to_stop") else "STILL BUSY"
        log_unified(
            f"[MAINTENANCE] Database prepared for shutdown — {verdict}"
            + (
                f" (still running: scans={result.get('active_scans')} "
                f"queue={result.get('queue_items')})"
                if not result.get("safe_to_stop")
                else ""
            )
        )
    except Exception:
        pass

    return result
