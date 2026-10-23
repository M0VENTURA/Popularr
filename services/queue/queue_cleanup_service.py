"""Queue cleanup service.

Provides queue maintenance and cleanup operations:
    - queue_cleanup(): Remove stale/expired queue entries.
    - queue_reset_moving(): Reset items stuck in 'moving' state.
    - cleanup_stuck_items(): Reset items stuck in 'searching'/'downloading'.

Architecture:
    Uses direct repository calls (``db.repositories.queue``) exclusively.
    No legacy helpers or raw SQL. Returns consistent (response, status)
    tuples for route integration.
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories import queue as queue_repository
from helpers.response_helpers import _fail, _ok, _safe_int

logger = structlog.get_logger(__name__)

# =============================================================================
# CLEANUP OPERATIONS
# =============================================================================

_STUCK_SEARCH_SECONDS = 300
_STUCK_DOWNLOAD_SECONDS = 6 * 3600
_STUCK_MOVING_MINUTES = 10


def _retry_delay_minutes() -> int:
    try:
        from db.repositories.queue import _queue_retry_delay_minutes

        return int(_queue_retry_delay_minutes())
    except Exception:
        return 30


def reset_abandoned_items() -> dict[str, int]:
    """Reset items left in 'searching'/'processing' by a previous worker.

    Run at worker startup. Rows that already have a downloaded file are left
    alone: putting one back to 'queued' would make it unclaimable (the claim
    requires ``file_path IS NULL``) and it would sit in the queue forever.
    """
    reset = 0
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status IN ('searching', 'processing')
                      AND file_path IS NULL
                """)
            )
            reset = int(result.rowcount or 0)
        if reset:
            logger.warning("Reset abandoned queue items at worker startup", count=reset)
        return {"abandoned_reset": reset}
    except Exception as exc:
        logger.exception("reset_abandoned_items failed", error=str(exc))
        return {"abandoned_reset": 0, "error": str(exc)}


def cleanup_stuck_items() -> dict[str, Any]:
    """Reset queue items stuck in 'searching'/'downloading'/'moving'."""
    searching_reset = 0
    downloading_reset = 0
    processing_reset = 0
    moving_reset = 0
    
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'searching'
                      AND file_path IS NULL
                      AND updated_at < CURRENT_TIMESTAMP - make_interval(secs => :seconds)
                """),
                {"seconds": _STUCK_SEARCH_SECONDS},
            )
            searching_reset = int(result.rowcount or 0)

        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'processing'
                      AND file_path IS NULL
                      AND updated_at < CURRENT_TIMESTAMP - make_interval(secs => :seconds)
                """),
                {"seconds": _STUCK_SEARCH_SECONDS},
            )
            processing_reset = int(result.rowcount or 0)

        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'failed',
                        failure_reason = 'Stuck in downloading state',
                        -- A hung transfer IS an attempt. Without counting it the
                        -- row was re-queued and hung again every 6 hours forever.
                        retry_count = COALESCE(retry_count, 0) + 1,
                        -- Wait before the auto-retry picks it up, instead of
                        -- re-queuing it on the very next cycle.
                        next_retry_at = CURRENT_TIMESTAMP + make_interval(mins => :delay),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'downloading'
                      AND updated_at < CURRENT_TIMESTAMP - make_interval(secs => :seconds)
                """),
                {"seconds": _STUCK_DOWNLOAD_SECONDS, "delay": _retry_delay_minutes()},
            )
            downloading_reset = int(result.rowcount or 0)

        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'downloading',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'moving'
                      AND updated_at < CURRENT_TIMESTAMP - make_interval(mins => :minutes)
                """),
                {"minutes": _STUCK_MOVING_MINUTES},
            )
            moving_reset = int(result.rowcount or 0)

        if searching_reset or downloading_reset or processing_reset or moving_reset:
            logger.warning(
                "Reset stuck queue items",
                searching=searching_reset,
                processing=processing_reset,
                downloading=downloading_reset,
                moving=moving_reset,
            )
            
        return {
            "searching_reset": searching_reset,
            "processing_reset": processing_reset,
            "downloading_reset": downloading_reset,
            "moving_reset": moving_reset,
        }

    except Exception as exc:
        logger.exception("cleanup_stuck_items failed", error=str(exc))
        return {
            "searching_reset": 0,
            "processing_reset": 0,
            "downloading_reset": 0,
            "moving_reset": 0,
            "error": str(exc),
        }


def queue_cleanup() -> Any:
    try:
        result = queue_repository.cleanup()
        if isinstance(result, tuple):
            return result
        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500
        return _ok(result=result)
    except Exception as exc:
        logger.exception("queue_cleanup failed", error=str(exc))
        return _fail(str(exc), 500)


def queue_reset_moving(data: Mapping[str, Any]) -> Any:
    try:
        queue_ids = data.get("queue_ids") or []
        stale_minutes = _safe_int(data.get("stale_minutes"), 5)

        result = queue_repository.reset_moving(queue_ids, stale_minutes)
        if isinstance(result, tuple):
            return result
        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500
        return _ok(reset=result)
    except Exception as exc:
        logger.exception("queue_reset_moving failed", error=str(exc))
        return _fail(str(exc), 500)


def queue_cleanup_copied_sources() -> Any:
    try:
        result = queue_repository.cleanup_copied_sources()
        if isinstance(result, tuple):
            return result
        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500
        return _ok(cleaned=result)
    except Exception as exc:
        logger.exception("queue_cleanup_copied_sources failed", error=str(exc))
        return _fail(str(exc), 500)


def queue_cleanup_orphaned(data: Mapping[str, Any]) -> Any:
    try:
        result = queue_repository.cleanup_orphaned(dict(data or {}))
        if isinstance(result, tuple):
            return result
        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500
        return _ok(cleaned=result)
    except Exception as exc:
        logger.exception("queue_cleanup_orphaned failed", error=str(exc))
        return _fail(str(exc), 500)


def queue_verify_and_prune(data: Mapping[str, Any]) -> Any:
    try:
        result = queue_repository.verify_and_prune(dict(data or {}))
        if isinstance(result, tuple):
            return result
        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500
        return _ok(result=result)
    except Exception as exc:
        logger.exception("queue_verify_and_prune failed", error=str(exc))
        return _fail(str(exc), 500)


def queue_delete_folder(data: Mapping[str, Any]) -> Any:
    try:
        result = queue_repository.delete_folder(dict(data or {}))
        if isinstance(result, tuple):
            return result
        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500
        return _ok(result=result)
    except Exception as exc:
        logger.exception("queue_delete_folder failed", error=str(exc))
        return _fail(str(exc), 500)


def queue_remove_group(data: Mapping[str, Any]) -> Any:
    try:
        result = queue_repository.remove_group(dict(data or {}))
        if isinstance(result, tuple):
            return result
        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500
        return _ok(result=result)
    except Exception as exc:
        logger.exception("queue_remove_group failed", error=str(exc))
        return _fail(str(exc), 500)


# =============================================================================
# COMPATIBILITY ROUTES
# =============================================================================

def clear(request: Any) -> Any:
    try:
        data = request.get_json(force=True) if request else {}
        result = queue_repository.clear_queue(data)
        return result, 200 if result.get("success") else 500
    except Exception as exc:
        logger.exception("clear failed", error=str(exc))
        return _fail(str(exc), 500)


def purge_all() -> Any:
    try:
        result = queue_repository.purge_all()
        return result, 200 if result.get("success") else 500
    except Exception as exc:
        logger.exception("purge_all failed", error=str(exc))
        return _fail(str(exc), 500)
