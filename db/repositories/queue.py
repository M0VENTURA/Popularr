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
from sqlalchemy import text

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

def insert_queue_item(
    artist: str,
    title: str,
    album: str | None = None,
    source: str = "soulseek",
    priority: int = 5,
    **kwargs: Any,
) -> dict[str, Any]:
    """Insert a new item into the download queue."""
    if not (artist or "").strip() and not (title or "").strip():
        return {"success": False, "error": "Artist and title are required"}

    with db_session() as session:
        # Dedupe on normalized (artist, title) across ALL active statuses and
        # ANY source.  Previously the dedupe required ``source = :source``, so
        # adding the same song via different entry points (Soulseek search,
        # MusicBrainz release, playlist import, upcoming releases) created
        # multiple rows for the same track — "20 versions of one song".
        # Local/discovered rows are still kept separate (they represent files
        # already on disk, never a search) — a searchable insert only dedupes
        # against other searchable rows, and a local insert only against other
        # local rows.
        _is_local = str(source or "").lower() in ("local", "discovered")
        existing = session.execute(
            text("""
                SELECT * FROM download_queue
                WHERE LOWER(artist) = LOWER(:artist)
                  AND LOWER(title) = LOWER(:title)
                  AND status IN ('queued', 'searching', 'downloading', 'completed', 'unmatched', 'imported', 'in_collection', 'matched', 'processing', 'moving')
                  AND (LOWER(COALESCE(source, '')) IN ('local', 'discovered')) = :is_local
                ORDER BY created_at ASC
                LIMIT 1
            """),
            {"artist": artist, "title": title, "is_local": _is_local},
        ).fetchone()

        if existing is not None:
            row = dict(existing._mapping)
            row["already_queued"] = True
            logger.info(
                "Duplicate skipped: already in queue",
                artist=artist, title=title, queue_id=row.get("id"),
            )
            return row

        _status = str(kwargs.get("status") or "queued")
        if str(kwargs.get("source") or "soulseek").lower() in ("local", "discovered"):
            _status = "unmatched"
            
        result = session.execute(
            text("""
                INSERT INTO download_queue
                    (artist, title, album, source, priority, track_number, disc_number,
                     album_artist, year, release_id, release_mbid, recording_mbid,
                     duration, import_group, import_type, status, file_path, found_filename,
                     created_at, updated_at)
                VALUES (:artist, :title, :album, :source, :priority, :track_number, :disc_number,
                        :album_artist, :year, :release_id, :release_mbid, :recording_mbid,
                        :duration, :import_group, :import_type, :status, :file_path, :found_filename,
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
    """Atomically claim a queued item for processing."""
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
    """Find the most recently updated queue item owning ``path``."""
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
    """Get non-terminal queue items (active statuses + failed + pending retry)."""
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


def get_ready_for_processing(limit: int = 100) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE file_path IS NULL
                      AND (status = 'queued'
                           OR (status IN ('backed_off', 'pending_release')
                               AND next_retry_at <= CURRENT_TIMESTAMP))
                      AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                      AND LOWER(COALESCE(source, '')) NOT IN ('local', 'discovered')
                    ORDER BY priority DESC, created_at ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Failed to get items ready for processing", error=str(e))
        return []


def requeue_due_failed_items(limit: int = 50) -> list[dict[str, Any]]:
    """Requeue legacy ``failed`` rows whose retry window has arrived."""
    requeued: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE status = 'failed'
                      AND file_path IS NULL
                      AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                    ORDER BY updated_at ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = [dict(r._mapping) for r in result.fetchall()]
            _delay_default = _queue_retry_delay_minutes()
            next_retry_expr = interval_minutes_expr(session, ":delay")
            for row in rows:
                qid = row.get("id")
                delay_minutes = max(1, int(row.get("retry_delay_minutes") or _delay_default))
                session.execute(
                    text(f"""
                        UPDATE download_queue
                        SET status = 'queued',
                            retry_count = retry_count + 1,
                            failure_reason = NULL,
                            next_retry_at = {next_retry_expr},
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :qid
                    """),
                    {"qid": qid, "delay": delay_minutes},
                )
                requeued.append(row)
        return requeued
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

def schedule_queue_retry(
    queue_id: int,
    status: str,
    next_retry_at: str,
    reason: str = "",
) -> dict[str, Any] | None:
    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET status = :status,
                        next_retry_at = :next_retry,
                        failure_reason = :reason,
                        retry_count = retry_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                """),
                {"qid": queue_id, "status": status, "next_retry": next_retry_at, "reason": reason},
            )
        return {"success": True, "id": queue_id, "status": status}
    except Exception as exc:
        logger.error("Failed to schedule queue retry", queue_id=queue_id, error=str(exc))
        return None


def _queue_retry_delay_minutes() -> int:
    """Default retry window (minutes) for an unscheduled failure.

    Only the DELAY is configurable — there is no retry ceiling, because a
    track that fails to download must always return to the queue rather than
    being abandoned.  The dead ``max_retries`` column was removed in migration
    015; ``queue.max_retries`` in config.yaml is no longer read.
    """
    try:
        from helpers.config_helpers import get_config
        q = (get_config() or {}).get("queue") or {}
        return max(1, int(q.get("failure_retry_delay_minutes", 30) or 30))
    except Exception:
        return 30


def mark_failed(queue_id: int, reason: str) -> dict[str, Any] | None:
    delay = _queue_retry_delay_minutes()
    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET failure_reason = :reason,
                        retry_count = retry_count + 1,
                        next_retry_at = GREATEST(
                            COALESCE(next_retry_at, CURRENT_TIMESTAMP),
                            CURRENT_TIMESTAMP
                                + (COALESCE(retry_delay_minutes, :delay) * INTERVAL '1 minute')
                        ),
                        status = CASE
                            WHEN status IN ('backed_off', 'pending_release') THEN status
                            ELSE 'queued'
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                """),
                {"qid": queue_id, "reason": reason, "delay": delay},
            )
        return {"success": True, "id": queue_id}
    except Exception as exc:
        logger.error("Failed to mark queue item failed", queue_id=queue_id, error=str(exc))
        return None


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
    try:
        with db_session() as session:
            result = session.execute(
                text(f"""
                    SELECT id, title, file_path, status, track_number,
                           disc_number, artist, album_artist
                    FROM download_queue
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
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

