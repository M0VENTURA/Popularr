"""Background queue worker process.

Standalone process entry point for the background queue processing loop.

Features:
    - Event-driven wake-up (no 30-second polling latency).
    - Signal-based graceful shutdown (SIGTERM/SIGINT).
    - Periodic processing cycles via ``queue_orchestrator.process_cycle``.
    - Error recovery and automatic restart logic.
    - Distributed leader election (Advisory Lock), re-taken after a lost
      connection so two workers can never process the queue at once.
    - Database-outage backoff: one warning per outage instead of an error
      from every hook on every cycle.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Any

import structlog
from sqlalchemy import text

from helpers.config_helpers import get_queue_worker_config
from services.queue.queue_heartbeat import (
    queue_processor_alive,
    record_queue_processor_cycle,
)
from services.queue.queue_orchestrator import (
    database_available,
    is_db_outage_error,
    process_cycle,
)

# Graceful degradation for sqlalchemy imports
try:
    from db.engine import get_engine
    _sqlalchemy_available = True
except Exception:
    _sqlalchemy_available = False

    def get_engine():
        return None

logger = structlog.get_logger(__name__)

_SHOULD_STOP = False
_worker_cfg = get_queue_worker_config()

# Unique 64-bit integer for the Queue Worker lock
_QUEUE_WORKER_LOCK_KEY = 0x5155455545  # "QUEUE"

#: Wait between database probes during an outage: doubles from the first value
#: up to the cap, so a Postgres restart is noticed quickly but a long outage is
#: not hammered every interval.
_DB_OUTAGE_BACKOFF_START = 5.0
_DB_OUTAGE_BACKOFF_MAX = 300.0

#: At most this many cycles run back-to-back when the orchestrator reports more
#: ready rows, then one normal wait — a guard against a tight loop if rows keep
#: being skipped (e.g. claimed by the scheduler thread between fetch and claim).
_MAX_IMMEDIATE_CYCLES = 5


# =============================================================================
# SIGNAL HANDLING
# =============================================================================

def _handle_signal(signum: int, frame: Any) -> None:
    global _SHOULD_STOP
    _SHOULD_STOP = True
    logger.info("Queue worker received stop signal", signal=signum)


# =============================================================================
# LOGGING SETUP
# =============================================================================

def _configure_logging() -> None:
    """Ensure logs appear correctly in stdout/system journal."""
    try:
        from helpers.logging_config import setup_logging
        setup_logging("QueueWorker")
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )


def _safe_shutdown_requested() -> bool:
    """Has the operator asked the application to quiesce?

    ⚠️ FAILS OPEN. A missing or unreadable flag means "keep working". A missed
    quiesce is recoverable; a queue that stops for no visible reason is not.
    """
    try:
        from services.maintenance.safe_shutdown_service import (
            is_safe_shutdown_requested,
        )

        return bool(is_safe_shutdown_requested())
    except Exception as exc:
        logger.debug("Safe shutdown flag unreadable — continuing to process", error=str(exc))
        return False


def _wait(timeout: float) -> bool:
    """Sleep up to ``timeout`` seconds, waking early for a new queue item."""
    try:
        from services.queue.queue_signal import wait_for_item

        return bool(wait_for_item(timeout=float(timeout)))
    except Exception:
        time.sleep(float(timeout))
        return False


# =============================================================================
# LEADER LOCK
# =============================================================================

def _acquire_leader(engine: Any) -> tuple[Any, bool]:
    """Connect and try the advisory lock.

    Returns ``(conn, acquired)``. ``conn`` is None when the database could not
    be reached; ``acquired`` is False when another worker holds the lock.
    """
    conn = None
    try:
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _QUEUE_WORKER_LOCK_KEY},
            ).scalar()
        )
        if not acquired:
            conn.close()
            return None, False
        return conn, True
    except Exception as exc:
        logger.warning("Database connection failed during leader election", error=str(exc))
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return None, True  # unreachable, not "someone else is leader"


def _connection_alive(conn: Any) -> bool:
    if conn is None:
        return False
    try:
        conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _close(conn: Any) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _reset_abandoned(reason: str) -> None:
    try:
        from services.queue.queue_cleanup_service import reset_abandoned_items

        reset_abandoned_items()
    except Exception as exc:
        logger.warning("Abandoned-item reset failed", reason=reason, error=str(exc))


# =============================================================================
# WORKER LOOP
# =============================================================================

def run(interval: int | None = None, batch_size: int | None = None) -> None:
    if not _sqlalchemy_available:
        logger.error("SQLAlchemy not available. Cannot run queue worker.")
        return

    engine = get_engine()
    if not engine:
        logger.error("Database engine not available. Cannot run queue worker.")
        return

    conn = None

    # Wait for PostgreSQL to become available before electing leader.
    while not _SHOULD_STOP:
        conn, acquired = _acquire_leader(engine)
        if conn is not None:
            break
        if not acquired:
            logger.info("Another queue worker process is already running. Exiting.")
            return
        time.sleep(5)

    if _SHOULD_STOP:
        _close(conn)
        return

    logger.info("Acquired queue worker leader lock.")

    effective_interval: int = _worker_cfg.get("interval_seconds", 30) if interval is None else interval
    effective_batch: int = _worker_cfg.get("batch_size", 50) if batch_size is None else batch_size

    logger.info(
        "Popularr Queue Worker started",
        interval_seconds=effective_interval,
        batch_size=effective_batch,
    )

    _reset_abandoned("startup")

    # ── Queue metadata heal ───────────────────────────────────────────────
    # Runs in a DAEMON THREAD so a large first pass cannot delay this worker's
    # first cycle. Idempotent; only the leader reaches this point.
    try:
        from helpers.config_helpers import get_metadata_heal_config

        _heal_cfg = get_metadata_heal_config()
        if _heal_cfg.get("enabled") and _heal_cfg.get("run_on_startup", True):

            def _startup_heal() -> None:
                try:
                    from services.downloads.download_metadata_heal_service import (
                        heal_queue_metadata,
                    )

                    _heal_summary = heal_queue_metadata(
                        limit=_heal_cfg.get("limit") or None
                    )
                    logger.info(
                        "[HEAL] Startup metadata heal finished",
                        examined=_heal_summary.get("examined"),
                        healed=_heal_summary.get("healed"),
                        tracks=_heal_summary.get("tracks_updated"),
                    )
                except Exception as exc:
                    logger.warning("[HEAL] Startup metadata heal failed", error=str(exc))

            threading.Thread(
                target=_startup_heal,
                name="queue-metadata-heal",
                daemon=True,
            ).start()
    except Exception as exc:
        logger.debug("Startup metadata heal not started", error=str(exc))

    loop_count = 0
    _safe_shutdown_logged = False
    _db_outage_since: float | None = None
    _db_backoff = _DB_OUTAGE_BACKOFF_START
    _immediate_cycles = 0

    try:
        while not _SHOULD_STOP:
            loop_count += 1

            # ── Safe shutdown: idle instead of processing ─────────────────
            if _safe_shutdown_requested():
                if not _safe_shutdown_logged:
                    logger.info(
                        "Safe shutdown requested — queue worker idling",
                        loop=loop_count,
                    )
                    _safe_shutdown_logged = True
                try:
                    record_queue_processor_cycle("standalone-worker-idle")
                except Exception:
                    pass
                _wait(float(effective_interval))
                continue

            if _safe_shutdown_logged:
                logger.info("Safe shutdown cancelled — queue worker resuming")
                _safe_shutdown_logged = False

            # ── Database outage: probe with backoff, do not run cycles ────
            if _db_outage_since is not None:
                if not database_available():
                    try:
                        record_queue_processor_cycle("standalone-worker-db-wait")
                    except Exception:
                        pass
                    time.sleep(_db_backoff)
                    _db_backoff = min(_DB_OUTAGE_BACKOFF_MAX, _db_backoff * 2)
                    continue
                logger.info(
                    "Database is back — queue worker resuming",
                    outage_seconds=round(time.monotonic() - _db_outage_since, 1),
                )
                _db_outage_since = None
                _db_backoff = _DB_OUTAGE_BACKOFF_START
                # Rows claimed when the database went away are still in
                # 'searching'; put them back now rather than waiting 5 minutes.
                _reset_abandoned("database recovery")

            # ── Leader lock: re-take it after a lost connection ───────────
            # A dropped connection releases the advisory lock. Carrying on (the
            # old ``except: pass``) let a second worker take the lock while this
            # one kept processing — two drivers claiming the same rows.
            if not _connection_alive(conn):
                _close(conn)
                conn, acquired = _acquire_leader(engine)
                if conn is None and not acquired:
                    logger.error(
                        "Queue worker lost its leader lock to another process — stopping"
                    )
                    break
                if conn is None:
                    logger.warning("Queue worker cannot reach the database — waiting")
                    _db_outage_since = time.monotonic()
                    continue
                logger.info("Queue worker reconnected and re-took the leader lock")

            more_ready = False
            try:
                start_time = time.monotonic()

                # Keep the heartbeat fresh WHILE the cycle runs: this batch can
                # take minutes (one search per track, 35-145s each), and a stamp
                # written only afterwards made the processor look stopped for the
                # whole batch — the dashboard offered "Restart Processor" and the
                # watchdog could kick a live processor.
                with queue_processor_alive("standalone-worker"):
                    payload, status = process_cycle(
                        batch_size=effective_batch,
                        run_maintenance_hooks=True,
                    )

                # A finished cycle is this worker's ONLY cross-process proof of
                # life (the WebUI reads the heartbeat stamped here).
                record_queue_processor_cycle("standalone-worker")

                duration = round(time.monotonic() - start_time, 3)
                more_ready = bool((payload or {}).get("more_ready"))

                if (payload or {}).get("db_outage") or (
                    status >= 500 and is_db_outage_error((payload or {}).get("error"))
                ):
                    logger.warning(
                        "Database unavailable — pausing queue processing until it returns",
                        loop=loop_count,
                    )
                    _db_outage_since = time.monotonic()
                    continue
                if status >= 500:
                    logger.error(
                        "Queue cycle failed",
                        loop=loop_count,
                        duration_seconds=duration,
                        response=payload,
                    )
                elif status >= 400:
                    logger.warning(
                        "Queue cycle warning",
                        loop=loop_count,
                        duration_seconds=duration,
                        response=payload,
                    )
                else:
                    logger.debug(
                        "Queue cycle success",
                        loop=loop_count,
                        duration_seconds=duration,
                        processed=payload.get("processed"),
                        succeeded=payload.get("succeeded"),
                        failed=payload.get("failed"),
                        skipped=payload.get("skipped"),
                        more_ready=more_ready,
                    )

            except Exception as exc:
                if is_db_outage_error(exc):
                    logger.warning("Database unavailable — pausing queue processing", error=str(exc))
                    _db_outage_since = time.monotonic()
                    continue
                logger.exception("Queue worker cycle crashed")

            # More claimable rows are waiting and slots are free: run the next
            # cycle straight away instead of sleeping the full interval.
            if more_ready and _immediate_cycles < _MAX_IMMEDIATE_CYCLES:
                _immediate_cycles += 1
                logger.debug("More queue items ready — starting next cycle immediately")
                continue
            _immediate_cycles = 0

            if _wait(float(effective_interval)):
                logger.debug("Queue worker woke up early — new item signalled")

    finally:
        _close(conn)

    logger.info("Queue worker stopped cleanly", total_cycles=loop_count)


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    _configure_logging()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        interval = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    except (TypeError, ValueError):
        interval = 30

    run(interval=interval)
