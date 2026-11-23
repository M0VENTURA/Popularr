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

import os
from datetime import datetime, timezone
from typing import Any, Mapping, Tuple

import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session
from db.repositories import queue as queue_repository
from helpers.response_helpers import _fail, _ok, _safe_int

logger = structlog.get_logger(__name__)

# =============================================================================
# CLEANUP OPERATIONS
# =============================================================================

_STUCK_SEARCH_SECONDS = 300
#: Was 6 HOURS. Nothing touched a downloading row for that long, so a frozen
#: transfer survived a whole working day — and because this hook runs late in
#: maintenance (and maintenance could be starved by a slow filesystem walk), the
#: 6-hour window was routinely postponed further. One hour is still comfortably
#: longer than any legitimate single-file transfer.
_STUCK_DOWNLOAD_SECONDS = 3600
_STUCK_MOVING_MINUTES = 10


def _retry_delay_minutes() -> int:
    try:
        from db.repositories.queue import _queue_retry_delay_minutes

        return int(_queue_retry_delay_minutes())
    except Exception:
        return 30


def _no_progress_window_seconds() -> float:
    """The no-progress window, in seconds (see ``slskd_reaper_service``).

    Read here too so BOTH stuck-item paths agree on what "frozen" means: the
    reaper cancels the transfer while this sweeper fails the row, and a
    disagreement would leave each one waiting for the other.
    """
    try:
        from services.downloads.slskd_reaper_service import _no_progress_minutes

        return float(_no_progress_minutes()) * 60.0
    except Exception:
        return 20.0 * 60.0


def reset_abandoned_items() -> dict[str, int]:
    """Reset items left mid-flight by a previous worker.

    Run at worker startup. Rows that already have a downloaded file are left
    alone: putting one back to 'queued' would make it unclaimable (the claim
    requires ``file_path IS NULL``) and it would sit in the queue forever.

    ⚠️ ``downloading`` IS INCLUDED, and that is what clears the reported "nine
    queue items have been 'downloading' with no further progress for about 3
    days. Every worker restart reports downloading=9."

    A ``downloading`` row's transfer belongs to the PREVIOUS process: slskd is
    holding it, but nobody is polling it. Nothing else in the app touches that
    status at startup, so the count was reported verbatim by every restart and
    the only escape was a 6-hour ``cleanup_stuck_items`` pass that maintenance
    starvation could postpone indefinitely. Two cases are resettable and one is
    not:

    * no ``found_filename`` yet — the row was marked downloading before a
      transfer was even recorded (``download_pipeline_service`` writes the
      status before the enqueue is confirmed), so there is nothing to resume;
    * a ``found_filename`` that is not on disk — the download was deleted,
      moved or never landed, so the row is waiting for a file that will never
      arrive. This is the "deleted on 20 Nov but never reset" case.

    A ``found_filename`` that IS on disk is left alone: the completion pass will
    import it, and resetting it to ``queued`` would re-download a file that is
    already here.
    """
    reset = 0
    downloading_reset = 0
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

        # ── downloading ──────────────────────────────────────────────────
        # Existence is checked in Python: ``found_filename`` is a REMOTE path
        # (sometimes relative to the music root), so a SQL-side test is not
        # possible and a wrong one would silently re-download live transfers.
        try:
            from services.metadata.tag_file_service import resolve_music_file_path
        except Exception:
            resolve_music_file_path = None  # type: ignore[assignment]

        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, found_filename
                    FROM download_queue
                    WHERE status = 'downloading'
                      AND file_path IS NULL
                """)
            ).fetchall() or []
            candidates = [dict(r._mapping) for r in rows]

        abandoned_ids: list[int] = []
        for row in candidates:
            found = str(row.get("found_filename") or "").strip()
            if not found:
                abandoned_ids.append(row["id"])
                continue
            on_disk = False
            if resolve_music_file_path is not None:
                try:
                    resolved = resolve_music_file_path(found)
                    on_disk = bool(resolved) and os.path.isfile(resolved)
                except Exception:
                    on_disk = False
            if not on_disk:
                abandoned_ids.append(row["id"])

        if abandoned_ids:
            with db_session() as session:
                result = session.execute(
                    text("""
                        UPDATE download_queue
                        SET status = 'queued',
                            failure_reason = 'reset: download abandoned by a previous worker',
                            found_filename = NULL,
                            slskd_transfer_id = NULL,
                            slskd_bytes_seen = NULL,
                            slskd_progress_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE CAST(id AS TEXT) IN :ids
                    """).bindparams(bindparam("ids", expanding=True)),
                    {"ids": [str(i) for i in abandoned_ids]},
                )
                downloading_reset = int(result.rowcount or 0)

        if reset:
            logger.warning("Reset abandoned queue items at worker startup", count=reset)
        if downloading_reset:
            logger.warning(
                "Reset abandoned DOWNLOADING queue items at worker startup",
                count=downloading_reset,
                queue_ids=sorted(abandoned_ids)[:20],
            )
        return {
            "abandoned_reset": reset,
            "downloading_reset": downloading_reset,
        }
    except Exception as exc:
        logger.exception("reset_abandoned_items failed", error=str(exc))
        return {"abandoned_reset": 0, "downloading_reset": 0, "error": str(exc)}


_STUCK_COLUMNS = (
    "id, status, updated_at, found_filename, slskd_transfer_id, "
    "retry_count, failure_reason"
)


def _stuck_rows(session: Any, where_sql: str, params: dict[str, Any]) -> list[Any]:
    """Read the rows an UPDATE is about to touch, so the log can name them."""
    result = session.execute(
        text(f"SELECT {_STUCK_COLUMNS} FROM download_queue WHERE {where_sql}"),
        params,
    )
    fetch = getattr(result, "fetchall", None)
    if not callable(fetch):
        return []
    return fetch() or []


def _log_stuck_resets(
    rows: list[Any],
    *,
    old_status: str,
    new_status: str,
    reason: str,
) -> None:
    """Log one line PER ITEM — counts alone made the reset untraceable.

    The old message was ``Reset stuck queue items ... downloading=1`` with no
    queue id, so there was no way to tell which item was reset or whether the
    next search was the reset row or a waiting one.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        value = mapping if mapping is not None else {}
        updated_at = value.get("updated_at")
        try:
            age_seconds = int((now - updated_at).total_seconds())
        except Exception:
            age_seconds = None
        logger.warning(
            "Reset stuck queue item",
            queue_id=value.get("id"),
            old_status=old_status,
            new_status=new_status,
            transfer_id=value.get("slskd_transfer_id"),
            found_filename=value.get("found_filename"),
            age_seconds=age_seconds,
            last_progress_at=str(updated_at) if updated_at else None,
            retry_count=value.get("retry_count"),
            reset_reason=reason,
        )


def cleanup_stuck_items() -> dict[str, Any]:
    """Reset queue items stuck in 'searching'/'downloading'/'moving'."""
    searching_reset = 0
    downloading_reset = 0
    processing_reset = 0
    moving_reset = 0

    try:
        # ── searching ────────────────────────────────────────────────────
        where = (
            "status = 'searching' AND file_path IS NULL "
            "AND updated_at < CURRENT_TIMESTAMP - make_interval(secs => :seconds)"
        )
        params = {"seconds": _STUCK_SEARCH_SECONDS}
        with db_session() as session:
            rows = _stuck_rows(session, where, params)
            result = session.execute(
                text(
                    "UPDATE download_queue SET status = 'queued', "
                    "updated_at = CURRENT_TIMESTAMP WHERE " + where
                ),
                params,
            )
            searching_reset = int(result.rowcount or 0)
            if searching_reset:
                _log_stuck_resets(
                    rows,
                    old_status="searching",
                    new_status="queued",
                    reason=f"searching for more than {_STUCK_SEARCH_SECONDS}s",
                )

        # ── processing ───────────────────────────────────────────────────
        where = (
            "status = 'processing' AND file_path IS NULL "
            "AND updated_at < CURRENT_TIMESTAMP - make_interval(secs => :seconds)"
        )
        params = {"seconds": _STUCK_SEARCH_SECONDS}
        with db_session() as session:
            rows = _stuck_rows(session, where, params)
            result = session.execute(
                text(
                    "UPDATE download_queue SET status = 'queued', "
                    "updated_at = CURRENT_TIMESTAMP WHERE " + where
                ),
                params,
            )
            processing_reset = int(result.rowcount or 0)
            if processing_reset:
                _log_stuck_resets(
                    rows,
                    old_status="processing",
                    new_status="queued",
                    reason=f"processing for more than {_STUCK_SEARCH_SECONDS}s",
                )

        # ── downloading ──────────────────────────────────────────────────
        #
        # TWO clocks, deliberately. ``updated_at`` catches a row nothing has
        # touched at all; ``slskd_progress_at`` catches the case the report
        # describes — a progress poll keeps touching the row (so ``updated_at``
        # stays fresh) while the byte count never moves. Whichever is older
        # wins, and ``slskd_bytes_seen``/``slskd_progress_at`` are cleared so the
        # retry starts with a clean marker.
        where = (
            "status = 'downloading' "
            "AND ("
            "updated_at < CURRENT_TIMESTAMP - make_interval(secs => :seconds) "
            "OR (slskd_progress_at IS NOT NULL "
            "AND slskd_progress_at < CURRENT_TIMESTAMP - make_interval(secs => :no_progress))"
            ")"
        )
        select_params = {
            "seconds": _STUCK_DOWNLOAD_SECONDS,
            "no_progress": int(_no_progress_window_seconds()),
        }
        update_params = {
            **select_params,
            "delay": _retry_delay_minutes(),
            # ``next_retry_at`` is an explicit UTC wall clock everywhere else;
            # the session's CURRENT_TIMESTAMP is not assumed.
            "now_utc": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        with db_session() as session:
            rows = _stuck_rows(session, where, select_params)
            result = session.execute(
                text(
                    "UPDATE download_queue SET status = 'failed', "
                    "failure_reason = 'Stuck in downloading state', "
                    # A hung transfer IS an attempt. Without counting it the
                    # row was re-queued and hung again every 6 hours forever.
                    "retry_count = COALESCE(retry_count, 0) + 1, "
                    # Wait before the auto-retry picks it up, instead of
                    # re-queuing it on the very next cycle.
                    "next_retry_at = :now_utc + make_interval(mins => :delay), "
                    # The next attempt is a different candidate; a stale marker
                    # would reap it instantly.
                    "slskd_bytes_seen = NULL, slskd_progress_at = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE " + where
                ),
                update_params,
            )
            downloading_reset = int(result.rowcount or 0)
            if downloading_reset:
                _log_stuck_resets(
                    rows,
                    old_status="downloading",
                    new_status="failed",
                    reason=(
                        "downloading for more than "
                        f"{_STUCK_DOWNLOAD_SECONDS // 3600}h; queued for retry"
                    ),
                )

        # ── moving ───────────────────────────────────────────────────────
        where = (
            "status = 'moving' "
            "AND updated_at < CURRENT_TIMESTAMP - make_interval(mins => :minutes)"
        )
        params = {"minutes": _STUCK_MOVING_MINUTES}
        with db_session() as session:
            rows = _stuck_rows(session, where, params)
            result = session.execute(
                text(
                    "UPDATE download_queue SET status = 'downloading', "
                    "updated_at = CURRENT_TIMESTAMP WHERE " + where
                ),
                params,
            )
            moving_reset = int(result.rowcount or 0)
            if moving_reset:
                _log_stuck_resets(
                    rows,
                    old_status="moving",
                    new_status="downloading",
                    reason=f"moving for more than {_STUCK_MOVING_MINUTES}m",
                )

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
