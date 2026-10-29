"""
Repository helpers for queue database operations.

✅ ALL database access is centralized here.
✅ Services must call this repository — no direct SQL elsewhere.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session
from db.utils import interval_minutes_expr, numeric_track_number_expr, row_get

from helpers.config_helpers import get_config
from services.downloads.download_scan_service import resolve_downloads_dir
from helpers.normalization_service import normalize_match_text

from services.queue.queue_constraints import COMPLETED_QUEUE_STATUSES

logger = structlog.get_logger(__name__)

UPDATE_ALLOWED_COLUMNS = frozenset({
    "artist", "title", "album", "album_artist", "source", "priority",
    "track_number", "disc_number", "year", "duration",
    "release_id", "release_source", "release_mbid", "recording_mbid",
    "musicbrainz_releasegroupid",
    "release_year", "cover_art_url",
    "import_group", "import_type",
    "file_path", "matched_file_path", "music_file_path", "found_filename",
    "progress", "speed",
    "slskd_username", "slskd_transfer_id", "is_manual_download",
    "retry_count", "retry_delay_minutes", "next_retry_at",
    "failure_reason",
    "release_date",
    "status",
    "collection_track_id", "in_collection",
    "copied_individually", "copied_individually_at",
})


# =============================================================================
# CORE GETTERS
# =============================================================================

def _row_to_dict(row: Any, cursor: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return dict(row)
    return dict(zip([c[0] for c in cursor.description], row))


def get_queue_item(queue_id: int) -> dict[str, Any] | None:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM download_queue WHERE id = :id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("Failed to fetch queue item", queue_id=queue_id, error=str(e))
        return None


def get_completed_group_queue_items(import_group: str) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, file_path, artist, album, title, track_number, disc_number, album_artist, year
                    FROM download_queue
                    WHERE import_group = :group AND status = 'completed'
                    ORDER BY id
                """),
                {"group": import_group},
            )
            rows = result.fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.error("Failed to get completed group items", import_group=import_group, error=str(exc))
        return []


def item_has_status(queue_id: int, expected_status: str) -> bool:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT status FROM download_queue WHERE id = :id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return str(row[0]).lower() == str(expected_status).lower() if row else False
    except Exception as e:
        logger.error("Failed to check status for queue item", queue_id=queue_id, error=str(e))
        return False


def get_queue_file_path(queue_id: int) -> str | None:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT file_path FROM download_queue WHERE id=:id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error("Failed to get queue file path", queue_id=queue_id, error=str(e))
        return None


# =============================================================================
# CORE MUTATIONS
# =============================================================================

def find_blocking_queue_item(
    *,
    artist: str,
    title: str,
    source: str = "soulseek",
    session: Any = None,
) -> dict[str, Any] | None:
    from services.queue.queue_constraints import BLOCKING_REQUEUE_STATUSES

    _blocking = ",".join(f"'{s}'" for s in sorted(BLOCKING_REQUEUE_STATUSES))
    _is_local = str(source or "").lower() in ("local", "discovered")
    _sql = text(f"""
        SELECT * FROM download_queue
        WHERE LOWER(artist) = LOWER(:artist)
          AND LOWER(title) = LOWER(:title)
          AND status IN ({_blocking})
          AND (LOWER(COALESCE(source, '')) IN ('local', 'discovered')) = :is_local
        ORDER BY created_at ASC
        LIMIT 1
    """)
    _params = {"artist": artist, "title": title, "is_local": _is_local}

    def _run(_session: Any) -> dict[str, Any] | None:
        row = _session.execute(_sql, _params).fetchone()
        return dict(row._mapping) if row else None

    if session is not None:
        return _run(session)
    try:
        with db_session() as own_session:
            return _run(own_session)
    except Exception as exc:
        logger.error(
            "Find blocking queue item failed", artist=artist, title=title, error=str(exc)
        )
        return None


def insert_queue_item(
    artist: str,
    title: str,
    album: str | None = None,
    source: str = "soulseek",
    priority: int = 5,
    **kwargs: Any,
) -> dict[str, Any]:
    if not (artist or "").strip() and not (title or "").strip():
        return {"success": False, "error": "Artist and title are required"}

    with db_session() as session:
        existing_row = find_blocking_queue_item(
            artist=artist, title=title, source=source, session=session,
        )

        if existing_row is not None:
            existing_row["already_queued"] = True
            logger.info(
                "Duplicate skipped: already in queue",
                artist=artist, title=title, queue_id=existing_row.get("id"),
            )
            return existing_row

        _status = str(kwargs.get("status") or "queued")
        if str(kwargs.get("source") or "soulseek").lower() in ("local", "discovered"):
            _status = "unmatched"
            
        result = session.execute(
            text("""
                INSERT INTO download_queue
                    (artist, title, album, source, priority, track_number, disc_number,
                     album_artist, year, release_id, release_mbid, recording_mbid,
                     musicbrainz_releasegroupid, duration, import_group, import_type,
                     status, file_path, found_filename, created_at, updated_at)
                VALUES (:artist, :title, :album, :source, :priority, :track_number, :disc_number,
                        :album_artist, :year, :release_id, :release_mbid, :recording_mbid,
                        :musicbrainz_releasegroupid, :duration, :import_group, :import_type,
                        :status, :file_path, :found_filename,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
            """),
            {
                "artist": artist, "title": title, "album": album, "source": source,
                "priority": priority,
                "track_number": kwargs.get("track_number"),
                "disc_number": kwargs.get("disc_number"),
                "album_artist": kwargs.get("album_artist"),
                "year": kwargs.get("year"),
                "release_id": kwargs.get("release_id"),
                "release_mbid": kwargs.get("release_mbid"),
                "recording_mbid": kwargs.get("recording_mbid"),
                "musicbrainz_releasegroupid": kwargs.get("musicbrainz_releasegroupid"),
                "duration": kwargs.get("duration"),
                "import_group": kwargs.get("import_group"),
                "import_type": kwargs.get("import_type") or "song",
                "status": _status,
                "file_path": kwargs.get("file_path"),
                "found_filename": kwargs.get("found_filename"),
            },
        )
        new_id = result.scalar()

    return get_queue_item(new_id) or {}


def update_queue_item(queue_id: int, **kwargs: Any) -> dict[str, Any] | None:
    if not kwargs:
        return get_queue_item(queue_id)

    updates = {k: v for k, v in kwargs.items() if k in UPDATE_ALLOWED_COLUMNS}
    if not updates:
        logger.warning("No whitelisted columns for queue update", queue_id=queue_id)
        return get_queue_item(queue_id)

    set_clauses = []
    params = {}

    for key, value in updates.items():
        set_clauses.append(f"{key} = :{key}")
        params[key] = json.dumps(value) if isinstance(value, (dict, list)) else value

    if "copied_individually" in params:
        params["copied_individually"] = bool(params["copied_individually"])

    params["id"] = queue_id

    query = text(f"""
        UPDATE download_queue
        SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP
        WHERE id = :id
        RETURNING *
    """)

    try:
        with db_session() as session:
            result = session.execute(query, params)
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("Update queue item failed", queue_id=queue_id, error=str(e))
        return None


def claim_queue_item(queue_id: int, status: str = "searching") -> dict[str, Any] | None:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = :status, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                      AND file_path IS NULL
                      AND status IN ('queued', 'backed_off', 'pending_release')
                      AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                    RETURNING *
                """),
                {"qid": queue_id, "status": status},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("Failed to claim queue item", queue_id=queue_id, error=str(e))
        return None


def delete_queue_item(queue_id: int) -> bool:
    try:
        with db_session() as session:
            result = session.execute(
                text("DELETE FROM download_queue WHERE id=:id"),
                {"id": queue_id},
            )
            return result.rowcount > 0
    except Exception as e:
        logger.error("Failed to delete queue item", queue_id=queue_id, error=str(e))
        return False


def get_queue_item_by_path(path: str) -> dict[str, Any] | None:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT * FROM download_queue
                    WHERE file_path = :p
                       OR found_filename = :p
                       OR music_file_path = :p
                    ORDER BY updated_at DESC
                    LIMIT 1
                """),
                {"p": path},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("Failed to get queue item by path", path=path, error=str(e))
        return None


# =============================================================================
# GROUP / MATCHING HELPERS
# =============================================================================

def get_items_by_group(group_id: str) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM download_queue WHERE import_group = :group"),
                {"group": group_id},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get items by group", group_id=group_id, error=str(e))
        return []


def apply_release_mbid(queue_ids: list[int], mbid: str, artist: str, album: str) -> int:
    if not queue_ids:
        return 0

    try:
        with db_session() as session:
            placeholders = ",".join([f":id_{i}" for i in range(len(queue_ids))])
            params: dict[str, Any] = {"mbid": mbid, "artist": artist, "album": album}
            params.update({f"id_{i}": qid for i, qid in enumerate(queue_ids)})
            result = session.execute(
                text(f"""
                    UPDATE download_queue
                    SET release_mbid = :mbid,
                        album_artist = :artist,
                        album = :album
                    WHERE id IN ({placeholders})
                """),
                params,
            )
            return result.rowcount
    except Exception as e:
        logger.error("Failed to apply release mbid", error=str(e))
        return 0


# =============================================================================
# STATUS / PROCESSING
# =============================================================================

def get_queue_status_counts() -> dict[str, int]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT status, COUNT(*) FROM download_queue GROUP BY status")
            )
            return {str(r[0]): int(r[1]) for r in result.fetchall()}
    except Exception as e:
        logger.error("Failed to get queue status counts", error=str(e))
        return {}


def get_active_queue(limit: int = 200) -> list[dict[str, Any]]:
    from services.queue.queue_constraints import (
        ACTIVE_QUEUE_STATUSES,
        FAILED_STATUSES,
        PENDING_RETRY_STATUSES,
    )
    status_sql = ", ".join(
        f"'{s}'" for s in sorted(ACTIVE_QUEUE_STATUSES | FAILED_STATUSES | PENDING_RETRY_STATUSES)
    )
    try:
        with db_session() as session:
            track_num_expr = numeric_track_number_expr(session)
            result = session.execute(
                text(f"""
                    SELECT *
                    FROM download_queue
                    WHERE status IN ({status_sql})
                      AND LOWER(COALESCE(source, '')) NOT IN ('local', 'discovered')
                    ORDER BY created_at ASC,
                             {track_num_expr},
                             id ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get active queue", error=str(e))
        return []


def get_processing_snapshot() -> dict[str, int]:
    counts = get_queue_status_counts()
    return {
        "queued": counts.get("queued", 0),
        "failed": counts.get("failed", 0),
        "completed": counts.get("completed", 0),
        "total": sum(counts.values())
    }


def get_queue_display_items(
    statuses: frozenset[str] | set[str],
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not statuses:
        return []
    status_sql = ", ".join(f"'{s}'" for s in sorted(statuses))
    try:
        with db_session() as session:
            track_num_expr = numeric_track_number_expr(session)
            result = session.execute(
                text(f"""
                    SELECT *
                    FROM download_queue
                    WHERE status IN ({status_sql})
                    ORDER BY created_at ASC,
                             {track_num_expr},
                             id ASC
                    LIMIT :limit OFFSET :offset
                """),
                {"limit": limit, "offset": offset},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get queue display items", error=str(e))
        return []


def get_ready_for_processing(limit: int = 100) -> list[dict[str, Any]]:
    """Items that can be claimed RIGHT NOW, fairest first.

    ⚠️ SAME eligibility as ``claim_queue_item``. A 'queued' row waiting on its
    ``next_retry_at`` used to be returned anyway, oldest first, so the
    orchestrator spent every in-flight slot on rows the claim then refused —
    newer items never started, and the old ones were retried the moment their
    wait ended (the reported "keeps retrying old files"). 'failed' rows are
    returned to the queue by ``requeue_due_failed_items``, never claimed here.

    Order: priority, then FEWEST attempts (never-tried rows first, so the
    whole queue gets one turn before anything is retried), then the row that
    has been due the longest.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE file_path IS NULL
                      AND status IN ('queued', 'backed_off', 'pending_release')
                      AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                      AND LOWER(COALESCE(source, '')) NOT IN ('local', 'discovered')
                    ORDER BY priority DESC,
                             COALESCE(retry_count, 0) ASC,
                             COALESCE(next_retry_at, created_at) ASC,
                             id ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get items ready for processing", error=str(e))
        return []


def count_ready_for_processing() -> int:
    """How many rows ``get_ready_for_processing`` could return right now."""
    try:
        with db_session() as session:
            return int(
                session.execute(
                    text("""
                        SELECT COUNT(*)
                        FROM download_queue
                        WHERE file_path IS NULL
                          AND status IN ('queued', 'backed_off', 'pending_release')
                          AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                          AND LOWER(COALESCE(source, '')) NOT IN ('local', 'discovered')
                    """)
                ).scalar()
                or 0
            )
    except Exception as e:
        logger.error("Failed to count items ready for processing", error=str(e))
        return 0


def requeue_due_failed_items(limit: int = 50) -> list[dict[str, Any]]:
    """Return due 'failed' rows to the queue, READY to claim.

    ⚠️ Three changes from the old version:

    * ``retry_count`` is NOT incremented here. The attempt was counted when
      the row failed; counting it again doubled every row's attempts.
    * ``next_retry_at`` is CLEARED. Setting a fresh future time made the
      re-queued row unclaimable while the old ready query still put it at the
      front of the list — the loop that starved newer items.
    * Rows past ``queue.max_retry_attempts``, rows the USER cancelled, and
      rows that already have a file are left alone. Cancelled downloads used
      to come back on their own.
    """
    try:
        with db_session() as session:
            rows = [
                dict(r._mapping)
                for r in session.execute(
                    text("""
                        SELECT *
                        FROM download_queue
                        WHERE status = 'failed'
                          AND file_path IS NULL
                          AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                          AND COALESCE(retry_count, 0) < :max_attempts
                          AND COALESCE(failure_reason, '') <> :cancelled
                          AND LOWER(COALESCE(source, '')) NOT IN ('local', 'discovered')
                        ORDER BY COALESCE(retry_count, 0) ASC, updated_at ASC
                        LIMIT :limit
                    """),
                    {
                        "limit": limit,
                        "max_attempts": _queue_max_attempts(),
                        "cancelled": CANCELLED_REASON,
                    },
                ).fetchall()
            ]
            if rows:
                placeholders = ", ".join(f":id{i}" for i in range(len(rows)))
                session.execute(
                    text(f"""
                        UPDATE download_queue
                        SET status = 'queued',
                            next_retry_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders})
                          AND status = 'failed'
                    """),
                    {f"id{i}": r["id"] for i, r in enumerate(rows)},
                )
        return rows
    except Exception as exc:
        logger.error("Failed to requeue due failed items", error=str(exc))
        return []


def get_failed_queue(limit: int = 100) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE status = 'failed'
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as exc:
        logger.error("Failed to get failed queue", error=str(exc))
        return []


def requeue_queue_item(queue_id: int) -> dict[str, Any] | None:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = CASE
                            WHEN file_path IS NOT NULL THEN 'unmatched'
                            WHEN LOWER(COALESCE(source, '')) IN ('local', 'discovered') THEN 'unmatched'
                            ELSE 'queued'
                        END,
                        retry_count = 0,
                        failure_reason = NULL,
                        next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                      AND status IN ('failed', 'removed', 'cancelled')
                    RETURNING *
                """),
                {"qid": queue_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.error("Failed to requeue queue item", queue_id=queue_id, error=str(exc))
        return None

    
# =============================================================================
# COMPLETED / POST-DOWNLOAD QUEUE
# =============================================================================

def get_completed_queue(limit: int = 50) -> list[dict[str, Any]]:
    status_sql = ", ".join(f"'{s}'" for s in sorted(COMPLETED_QUEUE_STATUSES))
    try:
        with db_session() as session:
            result = session.execute(
                text(f"""
                    SELECT *
                    FROM download_queue
                    WHERE status IN ({status_sql})
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get completed queue", error=str(e))
        return []


# =============================================================================
# GROUP QUERIES
# =============================================================================

def get_completed_by_group(group_id: str) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, file_path, artist, album, title,
                           track_number, disc_number, album_artist, year
                    FROM download_queue
                    WHERE import_group = :group
                    AND status = 'completed'
                """),
                {"group": group_id},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get completed items by group", group_id=group_id, error=str(e))
        return []


# =============================================================================
# STATUS HELPERS (STATE TRANSITIONS)
# =============================================================================

#: ``failure_reason`` written by a user cancel. Rows with it are never
#: re-queued automatically — the user asked for the download to stop.
CANCELLED_REASON = "Cancelled by user"


def _queue_cfg() -> dict[str, Any]:
    try:
        return (get_config() or {}).get("queue") or {}
    except Exception:
        return {}


def _queue_retry_delay_minutes() -> int:
    """First retry delay (``queue.failure_retry_delay_minutes``, default 30)."""
    try:
        return max(1, int(_queue_cfg().get("failure_retry_delay_minutes", 30) or 30))
    except (TypeError, ValueError):
        return 30


def _queue_max_delay_minutes() -> int:
    """Backoff ceiling (``queue.max_retry_delay_minutes``, default 24 h)."""
    try:
        return max(1, int(_queue_cfg().get("max_retry_delay_minutes", 1440) or 1440))
    except (TypeError, ValueError):
        return 1440


def _queue_max_attempts() -> int:
    """Attempts before a row stays 'failed' (``queue.max_retry_attempts``, default 8)."""
    try:
        return max(1, int(_queue_cfg().get("max_retry_attempts", 8) or 8))
    except (TypeError, ValueError):
        return 8


def schedule_queue_retry(
    queue_id: int,
    status: str,
    next_retry_at: str,
    reason: str = "",
) -> dict[str, Any] | None:
    """Park a row until ``next_retry_at`` with the caller's chosen status.

    'backed_off' / 'pending_release' are deliberate long waits (persistent
    misses, unreleased albums) and are honoured as given. A row scheduled
    back to plain 'queued' is an ordinary failed attempt, so it obeys the
    attempt limit like ``mark_failed`` — otherwise a file that keeps failing
    verification (the log's BIGBANG / Snot / Eurythmics rows, retried ~hourly
    for days) would cycle forever.
    """
    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = CASE
                            WHEN :status = 'queued'
                                 AND COALESCE(retry_count, 0) + 1 >= :max_attempts THEN 'failed'
                            ELSE :status
                        END,
                        next_retry_at = :next_retry,
                        failure_reason = :reason,
                        retry_count = COALESCE(retry_count, 0) + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                    RETURNING id, status, retry_count
                """),
                {
                    "qid": queue_id,
                    "status": status,
                    "next_retry": next_retry_at,
                    "reason": reason,
                    "max_attempts": _queue_max_attempts(),
                },
            ).fetchone()
        if not row:
            return None
        m = row._mapping
        return {"success": True, "id": m["id"], "status": m["status"], "retry_count": m["retry_count"]}
    except Exception as exc:
        logger.error("Failed to schedule queue retry", queue_id=queue_id, error=str(exc))
        return None


def mark_failed(
    queue_id: int,
    reason: str,
    *,
    count_attempt: bool = True,
) -> dict[str, Any] | None:
    """Record a failed attempt and schedule the retry with exponential backoff.

    Delay = ``retry_delay_minutes`` (row) or ``queue.failure_retry_delay_minutes``
    × 2^attempts, capped at ``queue.max_retry_delay_minutes``. With the defaults:
    30 min, 1 h, 2 h, 4 h, 8 h, 16 h, then daily. After
    ``queue.max_retry_attempts`` the row becomes 'failed' and stops cycling;
    "Retry all failed" gives it a fresh start.

    The old version retried after a FIXED delay and never gave up, so a
    file that always failed was downloaded, rejected and deleted about once
    an hour indefinitely.

    ``count_attempt=False`` is for infrastructure outages (slskd down): the row
    waits the first-retry delay but does not use up an attempt.
    """
    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    UPDATE download_queue
                    SET failure_reason = :reason,
                        retry_count = COALESCE(retry_count, 0) + :inc,
                        next_retry_at = GREATEST(
                            COALESCE(next_retry_at, CURRENT_TIMESTAMP),
                            CURRENT_TIMESTAMP + (
                                LEAST(
                                    :cap,
                                    COALESCE(retry_delay_minutes, :delay)
                                        * POWER(2, CASE WHEN :inc = 1
                                                        THEN LEAST(COALESCE(retry_count, 0), 10)
                                                        ELSE 0 END)
                                ) * INTERVAL '1 minute'
                            )
                        ),
                        status = CASE
                            WHEN :inc = 1
                                 AND COALESCE(retry_count, 0) + 1 >= :max_attempts THEN 'failed'
                            WHEN status IN ('backed_off', 'pending_release') THEN status
                            ELSE 'queued'
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                    RETURNING id, status, retry_count
                """),
                {
                    "qid": queue_id,
                    "reason": reason,
                    "inc": 1 if count_attempt else 0,
                    "delay": _queue_retry_delay_minutes(),
                    "cap": _queue_max_delay_minutes(),
                    "max_attempts": _queue_max_attempts(),
                },
            ).fetchone()
        if not row:
            return None
        m = row._mapping
        if str(m["status"]) == "failed":
            logger.info(
                "Queue item reached its retry limit — left as failed",
                queue_id=queue_id,
                attempts=m["retry_count"],
                reason=reason,
            )
        return {"success": True, "id": m["id"], "status": m["status"], "retry_count": m["retry_count"]}
    except Exception as exc:
        logger.error("Failed to mark queue item failed", queue_id=queue_id, error=str(exc))
        return None


# =============================================================================
# REJECTED CANDIDATES (per queue row)
# =============================================================================
#
# The log shows the SAME file from the SAME peer downloaded, rejected and
# deleted for one row ~20 times (queue_id 6667: mugilaw's "FXXK IT (KR ver.)"
# from 15 Oct to 17 Oct) — the "Blocked peer" warning fired every time, but
# the next search selected that candidate again. These helpers remember the
# rejected (username, filename) pairs ON THE ROW (``metadata`` JSON), so the
# pipeline's candidate selection can skip them. They must be called from the
# completion service (record) and the pipeline's selection (skip).

_REJECTED_KEY = "rejected_candidates"
_REJECTED_MAX = 50


def _candidate_key(username: Any, filename: Any) -> str:
    name = str(filename or "").replace("\\", "/").strip().casefold()
    return f"{str(username or '').strip().casefold()}|{name}"


def record_rejected_candidate(
    queue_id: int,
    username: str,
    filename: str,
    reason: str = "",
) -> bool:
    """Remember that ``username``/``filename`` was rejected for this row."""
    if not queue_id or not (username or filename):
        return False
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT metadata FROM download_queue WHERE id = :id FOR UPDATE"),
                {"id": queue_id},
            ).fetchone()
            if not row:
                return False
            raw = row[0]
            if isinstance(raw, str):
                try:
                    meta = json.loads(raw) if raw.strip() else {}
                except (TypeError, ValueError):
                    meta = {}
            elif isinstance(raw, dict):
                meta = dict(raw)
            else:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            rejected = [r for r in (meta.get(_REJECTED_KEY) or []) if isinstance(r, dict)]
            key = _candidate_key(username, filename)
            if any(r.get("key") == key for r in rejected):
                return True
            rejected.append({
                "key": key,
                "username": str(username or ""),
                "filename": str(filename or ""),
                "reason": str(reason or "")[:200],
                "at": datetime.now().isoformat(timespec="seconds"),
            })
            meta[_REJECTED_KEY] = rejected[-_REJECTED_MAX:]
            session.execute(
                text("UPDATE download_queue SET metadata = :meta WHERE id = :id"),
                {"meta": json.dumps(meta, ensure_ascii=False), "id": queue_id},
            )
        return True
    except Exception as exc:
        logger.error("Failed to record rejected candidate", queue_id=queue_id, error=str(exc))
        return False


def get_rejected_candidate_keys(item_or_id: Any) -> set[str]:
    """Keys of the candidates rejected for a row (pass the row or its id)."""
    try:
        if isinstance(item_or_id, dict):
            raw = item_or_id.get("metadata")
        else:
            row = get_queue_item(int(item_or_id))
            raw = (row or {}).get("metadata")
        if isinstance(raw, str):
            raw = json.loads(raw) if raw.strip() else {}
        if not isinstance(raw, dict):
            return set()
        return {
            str(r.get("key"))
            for r in (raw.get(_REJECTED_KEY) or [])
            if isinstance(r, dict) and r.get("key")
        }
    except Exception:
        return set()


def is_rejected_candidate(item_or_id: Any, username: str, filename: str) -> bool:
    """True when this candidate was already rejected for the row.

    Matches the exact ``username|filename`` record, and also a record with NO
    username (written when the peer could not be identified), which rejects
    that filename from any peer.
    """
    keys = get_rejected_candidate_keys(item_or_id)
    if not keys:
        return False
    return (
        _candidate_key(username, filename) in keys
        or _candidate_key("", filename) in keys
    )


def mark_imported(queue_id: int, target_path: str) -> dict[str, Any] | None:
    return update_queue_item(
        queue_id,
        status="imported",
        music_file_path=target_path,
        copied_individually=1,
        copied_individually_at=datetime.now().isoformat()
    )


def mark_processing(queue_id: int) -> dict[str, Any] | None:
    return update_queue_item(queue_id, status="processing")


def mark_completed(queue_id: int) -> dict[str, Any] | None:
    return update_queue_item(queue_id, status="completed")


# =============================================================================
# MATCH TARGETS / REMATCH HELPERS
# =============================================================================

def get_queue_match_targets(
    artist: str,
    album: str,
    selected_queue_id: int | None = None,
) -> list[dict[str, Any]]:
    active_statuses = (
        'queued', 'searching', 'downloading',
        'matched', 'completed',
        'unmatched', 'queried',
        'discovered', 'pending_match',
        'possible_duplicate', 'duplicate'
    )
    try:
        with db_session() as session:
            status_placeholders = ", ".join([f":status_{i}" for i in range(len(active_statuses))])
            params: dict[str, Any] = {f"status_{i}": s for i, s in enumerate(active_statuses)}
            params["artist"] = artist
            params["album"] = album
            params["selected_queue_id"] = selected_queue_id

            result = session.execute(
                text(f"""
                SELECT
                    id, artist, title, album, status, track_number, release_mbid, found_filename, created_at
                FROM download_queue
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                  AND LOWER(COALESCE(NULLIF(album, ''), '')) = LOWER(:album)
                  AND status IN ({status_placeholders})
                ORDER BY
                    CASE
                        WHEN :selected_queue_id IS NOT NULL AND id = :selected_queue_id THEN 0
                        ELSE 1
                    END,
                    {numeric_track_number_expr(session)},
                    COALESCE(NULLIF(TRIM(track_number), ''), '9999'),
                    id
                LIMIT 250
                """),
                params,
            )

            return [
                {
                    "id": r[0],
                    "artist": r[1] or '',
                    "title": r[2] or '',
                    "album": r[3] or '',
                    "status": r[4] or '',
                    "track_number": r[5],
                    "release_mbid": r[6] or '',
                    "found_filename": r[7] or '',
                    "created_at": r[8],
                }
                for r in result.fetchall() or []
            ]
    except Exception as e:
        logger.error("Failed to get queue match targets", error=str(e))
        return []


def get_album_queue_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    """Queue rows that belong to this album or carry this artist credit.

    ⚠️ EITHER CREDIT COLUMN COUNTS. A Various Artists compilation stores
    "Various Artists" in ``album_artist`` and the performer in ``artist``; a
    ``COALESCE(album_artist, artist)``-only filter missed rows whose album
    artist was the performer (or empty) and made an already-queued track look
    missing. The album name is kept as an OR so a queue row titled with the
    requested album is found even when neither credit names the caller.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text(f"""
                    SELECT id, title, file_path, status, track_number,
                           disc_number, artist, album_artist, album
                    FROM download_queue
                    WHERE (
                            LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                         OR LOWER(COALESCE(NULLIF(artist, ''), album_artist)) = LOWER(:artist)
                         OR LOWER(COALESCE(album, '')) = LOWER(:album)
                    )
                    ORDER BY
                        {numeric_track_number_expr(session)},
                        title
                """),
                {"artist": artist, "album": album},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get album queue tracks", error=str(e))
        return []


def get_queue_tracks_for_artists(artists: list[str]) -> list[dict[str, Any]]:
    """Queue rows whose TRACK or ALBUM artist is one of *artists*.

    Used to find a track queued from a DIFFERENT release. The caller matches
    the returned rows by title (and artist), never by album, because the queue
    row's album is precisely what differs.
    """
    names = sorted({
        str(value or "").strip().lower()
        for value in (artists or [])
        if str(value or "").strip()
    })
    if not names:
        return []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, file_path, status, track_number,
                           disc_number, artist, album_artist, album
                    FROM download_queue
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN :artists
                       OR LOWER(COALESCE(NULLIF(artist, ''), album_artist)) IN :artists
                    ORDER BY id
                """).bindparams(bindparam("artists", expanding=True)),
                {"artists": names},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get queue tracks for artists", error=str(e))
        return []


# =============================================================================
# BACKWARD-COMPATIBLE REDIRECTS
# =============================================================================

from db.repositories.queue_admin import (
    clear_queue, purge_all, get_imported, cleanup,
    cleanup_copied_sources, cleanup_orphaned,
    count_pending_by_release, verify_and_prune,
    apply_release_mbid as admin_apply_release_mbid,
    find_existing_discovered_file, find_duplicate_queue_item,
    insert_discovered_file, delete_duplicate_queue_entries,
    mark_in_collection, get_active_queue_signatures,
    get_queue_items_by_folder, slskd_eligibility_diagnostics,
    delete_folder, remove_group, reset_moving,
)
