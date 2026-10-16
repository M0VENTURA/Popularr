"""Queue rejection log — WHY a downloaded file was discarded.

See ``migrations/versions/027_queue_rejections.py`` for why this is a table
rather than a column: ``mark_failed`` returns the queue row to ``queued`` and the
next attempt overwrites ``failure_reason``, so a discard recorded only there is
gone within minutes and a page reading the live queue can never show it.

⚠️ Every write here is BEST-EFFORT. This is diagnostic bookkeeping attached to a
destructive path (a rejected download is deleted and the item rescheduled), so a
failure to record must never become a failure to reject — the caller has already
decided, and swallowing the error is the correct trade. Failures are logged at
DEBUG because a broken log table is not worth a WARNING on every discard.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)

#: Rows kept. The table is a diagnostic window, not an audit trail, and a busy
#: queue can discard hundreds of candidates an hour — left alone this grows
#: without bound for information nobody reads that far back.
DEFAULT_KEEP = 500

#: Prune at most every this many inserts. A DELETE on every write would be the
#: dominant cost of recording a rejection, and exact retention is not worth it.
_PRUNE_EVERY = 50

_prune_lock = threading.Lock()
_since_prune = 0


def record_rejection(
    *,
    reason: str,
    queue_id: int | None = None,
    artist: str = "",
    title: str = "",
    album: str = "",
    detail: str = "",
    found_artist: str = "",
    found_title: str = "",
    found_length_seconds: float | None = None,
    expected_length_seconds: float | None = None,
    filename: str = "",
) -> bool:
    """Record one discarded candidate. Returns True when it was written.

    ``reason`` is a stable CODE (``wrong_artist``, ``wrong_length``, …) because
    the UI groups on it; ``detail`` is the sentence shown to the operator.
    """
    if not reason:
        return False
    try:
        with db_session() as session:
            session.execute(
                text("""
                    INSERT INTO queue_rejections
                        (queue_id, artist, title, album, reason, detail,
                         found_artist, found_title, found_length_seconds,
                         expected_length_seconds, filename, created_at)
                    VALUES
                        (:queue_id, :artist, :title, :album, :reason, :detail,
                         :found_artist, :found_title, :found_length_seconds,
                         :expected_length_seconds, :filename, CURRENT_TIMESTAMP)
                """),
                {
                    "queue_id": int(queue_id) if queue_id else None,
                    "artist": (artist or "").strip() or None,
                    "title": (title or "").strip() or None,
                    "album": (album or "").strip() or None,
                    "reason": reason,
                    "detail": (detail or "").strip() or None,
                    "found_artist": (found_artist or "").strip() or None,
                    "found_title": (found_title or "").strip() or None,
                    "found_length_seconds": _as_float(found_length_seconds),
                    "expected_length_seconds": _as_float(expected_length_seconds),
                    "filename": (filename or "").strip()[:500] or None,
                },
            )
        _maybe_prune()
        return True
    except Exception as exc:
        logger.debug("Could not record queue rejection", reason=reason, error=str(exc))
        return False


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_recent_rejections(limit: int = 50) -> list[dict[str, Any]]:
    """The most recent discards, newest first.

    Returns ``[]`` on any failure — the caller renders an empty section rather
    than an error, because a missing log must not break the queue page.
    """
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, queue_id, artist, title, album, reason, detail,
                           found_artist, found_title, found_length_seconds,
                           expected_length_seconds, filename,
                           CAST(created_at AS TEXT) AS created_at
                    FROM queue_rejections
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                """),
                {"limit": max(1, min(int(limit or 50), 500))},
            ).fetchall()
        return [dict(row._mapping) for row in rows or []]
    except Exception as exc:
        logger.debug("Could not read queue rejections", error=str(exc))
        return []


def rejection_counts() -> dict[str, int]:
    """How many discards per reason, over the whole retained window."""
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT reason, COUNT(*) AS n
                    FROM queue_rejections
                    GROUP BY reason
                    ORDER BY n DESC
                """)
            ).fetchall()
        return {str(row._mapping["reason"]): int(row._mapping["n"] or 0) for row in rows or []}
    except Exception as exc:
        logger.debug("Could not summarise queue rejections", error=str(exc))
        return {}


def clear_rejections() -> int:
    """Delete every recorded rejection. Returns the number removed."""
    try:
        with db_session() as session:
            result = session.execute(text("DELETE FROM queue_rejections"))
            return max(0, int(result.rowcount or 0))
    except Exception as exc:
        logger.debug("Could not clear queue rejections", error=str(exc))
        return 0


def prune_rejections(keep: int = DEFAULT_KEEP) -> int:
    """Keep only the newest ``keep`` rows. Returns the number removed."""
    keep = max(1, int(keep or DEFAULT_KEEP))
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    DELETE FROM queue_rejections
                    WHERE id NOT IN (
                        SELECT id FROM queue_rejections
                        ORDER BY created_at DESC, id DESC
                        LIMIT :keep
                    )
                """),
                {"keep": keep},
            )
            return max(0, int(result.rowcount or 0))
    except Exception as exc:
        logger.debug("Could not prune queue rejections", error=str(exc))
        return 0


def _maybe_prune() -> None:
    """Prune once every ``_PRUNE_EVERY`` writes.

    ⚠️ The counter is module-level and guarded, because rejections are recorded
    from the scan/queue worker threads. An unguarded increment could lose updates
    (harmless — it only makes pruning rarer) but the DELETE itself must not be
    entered concurrently, which the lock also prevents.
    """
    global _since_prune
    with _prune_lock:
        _since_prune += 1
        due = _since_prune >= _PRUNE_EVERY
        if due:
            _since_prune = 0
    if due:
        prune_rejections()
