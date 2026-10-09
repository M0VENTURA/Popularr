"""Background queue worker process.

Standalone process entry point for the background queue processing loop.

Features:
    - Event-driven wake-up (no 30-second polling latency).
    - Signal-based graceful shutdown (SIGTERM/SIGINT).
    - Periodic processing cycles via ``queue_orchestrator.process_cycle``.
    - Error recovery and automatic restart logic.
    - Distributed leader election (Advisory Lock).
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any

import structlog
from sqlalchemy import text

from helpers.config_helpers import get_queue_worker_config
from services.queue.queue_heartbeat import record_queue_processor_cycle
from services.queue.queue_orchestrator import process_cycle

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

    # Wait for PostgreSQL to become available before electing leader
    while not _SHOULD_STOP:
        try:
            conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            
            result = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), 
                {"k": _QUEUE_WORKER_LOCK_KEY}
            ).scalar()

            if not result:
                logger.info("Another queue worker process is already running. Exiting.")
                conn.close()
                return
            
            # Lock acquired successfully
            break 
            
        except Exception as exc:
            logger.warning("Database connection failed during leader election. Retrying in 5 seconds...", error=str(exc))
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(5)
            continue

    if _SHOULD_STOP:
        return

    logger.info("Acquired queue worker leader lock.")
    
    effective_interval: int = _worker_cfg.get("interval_seconds", 30) if interval is None else interval
    effective_batch: int = _worker_cfg.get("batch_size", 50) if batch_size is None else batch_size
    
    logger.info(
        "Popularr Queue Worker started",
        interval_seconds=effective_interval,
        batch_size=effective_batch,
    )

    try:
        from services.queue.queue_cleanup_service import reset_abandoned_items
        reset_abandoned_items()
    except Exception as exc:
        logger.warning("Worker startup recovery failed", error=str(exc))

    loop_count = 0

    try:
        while not _SHOULD_STOP:
            loop_count += 1

            # Keep the DB connection alive so the lock doesn't drop
            try:
                conn.execute(text("SELECT 1"))
            except Exception:
                pass

            try:
                start_time = time.monotonic()

                payload, status = process_cycle(
                    batch_size=effective_batch,
                    run_maintenance_hooks=True,
                )

                # A finished cycle is this worker's ONLY cross-process proof of
                # life: the WebUI never sees this PID, it reads the heartbeat
                # file stamped here (services/queue/queue_heartbeat.py). Stamped
                # after the call so a cycle that raised leaves no stamp and the
                # status endpoint correctly reports the processor as stopped.
                record_queue_processor_cycle("standalone-worker")

                duration = round(time.monotonic() - start_time, 3)

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
                    )

            except Exception:
                logger.exception("Queue worker cycle crashed")

            from services.queue.queue_signal import wait_for_item
            woke = wait_for_item(timeout=float(effective_interval))
            if woke:
                logger.debug("Queue worker woke up early — new item signalled")

    finally:
        if conn is not None:
            conn.close()

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
