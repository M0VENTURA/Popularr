"""
Queue matching routes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from quart import Blueprint, request
from sqlalchemy import text

from db.engine import db_session
from db.repositories.queue import get_queue_item, update_queue_item
from routes.utils import json_response as _json_response
from services.downloads.download_organize_service import organize_track
from services.downloads.download_matching_service import match_folder, auto_match_folder
from services.queue.queue_processing_service import organize_group_sync
from services.tasks.queue_tasks import start_organize_group

logger = structlog.get_logger(__name__)
queue_matching_bp = Blueprint("queue_matching", __name__)

# -----------------------------------------------------------------------------
# Move to music (single track)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/move-to-music/<int:queue_id>", methods=["POST"])
async def api_queue_move_to_music(queue_id: int) -> Any:
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(organize_track(queue_id, payload))


# -----------------------------------------------------------------------------
# Manual match: link an orphan queue item to a library track
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/<int:queue_id>/match-targets", methods=["GET"])
def api_queue_match_targets(queue_id: int) -> Any:
    """Search library tracks to manually link an orphan queue item."""
    query = (request.args.get("q") or "").strip()
    try:
        with db_session() as session:
            if query:
                like = f"%{query}%"
                rows = session.execute(
                    text("""
                        SELECT id, artist, album, title, album_artist, year
                        FROM tracks
                        WHERE artist ILIKE :like OR title ILIKE :like OR album ILIKE :like
                        ORDER BY artist, album, title
                        LIMIT 30
                    """),
                    {"like": like},
                ).fetchall()
            else:
                rows = session.execute(
                    text("""
                        SELECT id, artist, album, title, album_artist, year
                        FROM tracks
                        ORDER BY artist, album, title
                        LIMIT 30
                    """)
                ).fetchall()
                
        return _json_response({
            "success": True,
            "queue_id": queue_id,
            "tracks": [dict(r._mapping) for r in rows],
        })
    except Exception as exc:
        logger.error("Failed to fetch match targets", queue_id=queue_id, error=str(exc))
        return _json_response({"success": False, "error": str(exc)})


@queue_matching_bp.route("/api/queue/<int:queue_id>/link-track", methods=["POST"])
async def api_queue_link_track(queue_id: int) -> Any:
    """Point an orphan queue item at a library track.

    Copies the track's metadata onto the queue row, stores
    ``collection_track_id`` and moves the item to ``completed`` so it shows up
    under Completed with the Transfer button (organize_track builds the
    library path from the corrected metadata).
    """
    payload = (await request.get_json(silent=True)) or {}
    track_id = str(payload.get("track_id") or "").strip()
    if not track_id:
        return _json_response({"success": False, "error": "track_id is required"})

    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT id, artist, album, title, album_artist, year FROM tracks WHERE id = :tid"),
                {"tid": track_id},
            ).fetchone()
            
        if row is None:
            return _json_response({"success": False, "error": "Track not found"})

        track = dict(row._mapping)
        item = get_queue_item(queue_id)
        if item is None:
            return _json_response({"success": False, "error": "Queue item not found"})

        updated = update_queue_item(
            queue_id,
            artist=track.get("artist") or item.get("artist"),
            title=track.get("title") or item.get("title"),
            album=track.get("album") or item.get("album") or "",
            album_artist=track.get("album_artist") or track.get("artist"),
            year=str(track.get("year") or "")[:4] if track.get("year") else None,
            collection_track_id=str(track.get("id")),
            status="completed",
        )
        return _json_response({"success": True, "item": updated})
    except Exception as exc:
        logger.error("Failed to link track", queue_id=queue_id, track_id=track_id, error=str(exc))
        return _json_response({"success": False, "error": str(exc)})


@queue_matching_bp.route("/api/queue/<int:queue_id>/local-files", methods=["GET"])
def api_queue_local_files(queue_id: int) -> Any:
    """List audio files already sitting in downloads/ (including torrents/).

    Reported: the manual Soulseek search never offered the files the user had
    ALREADY downloaded, so a failed match could only be fixed by re-downloading.
    This is read-only; ``attach-local`` links the chosen file to the row.
    """
    import os
    import re

    from services.downloads.download_scan_service import discover_audio_files

    query = (request.args.get("q") or "").strip().casefold()
    tokens = [t for t in re.split(r"[^0-9a-z]+", query) if len(t) >= 2]
    limit = request.args.get("limit", default=50, type=int) or 50

    try:
        files = discover_audio_files()
    except Exception as exc:
        logger.warning("Local download scan failed", error=str(exc))
        return _json_response({"success": False, "error": str(exc), "files": []})

    rows = []
    for found in files:
        haystack = found.rel_path.casefold()
        if tokens and not all(token in haystack for token in tokens):
            continue
        try:
            stat = os.stat(found.full_path)
            size = stat.st_size
            modified = stat.st_mtime
        except OSError:
            size = 0
            modified = 0
        rows.append({
            "filename": found.filename,
            "full_path": found.full_path,
            "rel_path": found.rel_path,
            "folder": found.folder,
            "size": size,
            "modified": modified,
        })

    rows.sort(key=lambda r: r.get("modified") or 0, reverse=True)
    return _json_response({
        "success": True,
        "queue_id": queue_id,
        "files": rows[: max(1, min(limit, 200))],
        "total_matched": len(rows),
    })


@queue_matching_bp.route("/api/queue/<int:queue_id>/attach-local", methods=["POST"])
async def api_queue_attach_local(queue_id: int) -> Any:
    """Attach an existing downloads/torrents file to a queue row.

    The path must resolve INSIDE the downloads tree; a manual match must never
    reach into ``/music`` or an arbitrary filesystem location.
    """
    import os

    from services.downloads.download_scan_service import (
        resolve_downloads_dir,
        resolve_torrents_dir,
    )

    payload = (await request.get_json(silent=True)) or {}
    raw_path = str(payload.get("file_path") or "").strip()
    if not raw_path:
        return _json_response({"success": False, "error": "file_path is required"}), 400

    path = os.path.realpath(raw_path)
    roots = {
        os.path.realpath(resolve_downloads_dir()),
        os.path.realpath(resolve_torrents_dir()),
    }
    inside = any(
        path == root or path.startswith(root + os.sep) for root in roots if root
    )
    if not inside:
        return _json_response({
            "success": False,
            "error": "file_path must be inside the downloads or torrents folder",
        }), 400
    if not os.path.isfile(path):
        return _json_response({"success": False, "error": "File not found"}), 404

    item = get_queue_item(queue_id)
    if item is None:
        return _json_response({"success": False, "error": "Queue item not found"}), 404

    updated = update_queue_item(
        queue_id,
        status="matched",
        file_path=path,
        failure_reason=None,
    )
    logger.info(
        "Attached local download to queue row",
        queue_id=queue_id, file_path=path,
    )
    return _json_response({"success": True, "item": updated, "file_path": path})


# -----------------------------------------------------------------------------
# Organize
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/<int:queue_id>/organize", methods=["POST"])
async def api_queue_organize(queue_id: int) -> Any:
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(organize_track(queue_id, payload))


@queue_matching_bp.route("/api/queue/organize-group", methods=["POST"])
async def api_queue_organize_group() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    group_id = payload.get("group_id") or payload.get("import_group")

    if not group_id:
        return _json_response({
            "success": False,
            "error": "group_id is required"
        })

    async_requested = str(request.args.get("async", "0")).strip().lower() in {"1", "true", "yes", "on"}

    if async_requested:
        task_id = start_organize_group(group_id, payload.get("metadata") or {})
        return _json_response(({
            "success": True,
            "accepted": True,
            "task_id": task_id,
            "status": "running",
            "message": "Organization started in background",
        }, 202))

    return _json_response(await asyncio.to_thread(organize_group_sync, group_id, payload.get("metadata") or {}))

# -----------------------------------------------------------------------------
# Matching (optional but correct)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/match-folder", methods=["POST"])
async def api_match_folder() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    folder_path = payload.get("folder_path")
    if not folder_path:
        return _json_response({
            "success": False,
            "error": "folder_path is required"
        })

    return _json_response(match_folder(folder_path, payload))


@queue_matching_bp.route("/api/queue/auto-match-folder", methods=["POST"])
async def api_auto_match_folder() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    folder_path = payload.get("folder_path")
    if not folder_path:
        return _json_response({
            "success": False,
            "error": "folder_path is required"
        })

    return _json_response(auto_match_folder(folder_path, payload))


# -----------------------------------------------------------------------------
# Matched releases (for queue item match modal)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/matched-releases", methods=["GET"])
def api_queue_matched_releases() -> Any:
    """Return all unique releases currently in the download queue."""
    try:
        artist_filter = (request.args.get("artist") or "").strip()
        album_filter = (request.args.get("album") or "").strip()
        try:
            requested_limit = int(request.args.get("limit", 80))
        except Exception:
            requested_limit = 80
        limit = max(10, min(requested_limit, 250))

        active_statuses = (
            "queued", "searching", "downloading",
            "matched", "completed",
            "unmatched", "queried",
            "discovered", "pending_match",
            "possible_duplicate", "duplicate",
        )
        status_placeholders = ", ".join(f":st{i}" for i in range(len(active_statuses)))

        with db_session() as session:
            rows = session.execute(
                text(f"""
                    SELECT
                        COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) AS mbid,
                        MAX(COALESCE(NULLIF(album_artist, ''), artist)) AS release_artist,
                        MAX(COALESCE(NULLIF(album, ''), '')) AS release_album,
                        MAX(COALESCE(NULLIF(CAST(release_year AS TEXT), ''), NULLIF(CAST(year AS TEXT), ''))) AS resolved_year,
                        COUNT(*) AS track_count,
                        MAX(CASE WHEN :artist_filter <> '' AND LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist_filter) THEN 1 ELSE 0 END) AS artist_match,
                        MAX(CASE WHEN :album_filter <> '' AND LOWER(COALESCE(NULLIF(album, ''), '')) = LOWER(:album_filter) THEN 1 ELSE 0 END) AS album_match
                    FROM download_queue
                    WHERE COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) IS NOT NULL
                      AND status IN ({status_placeholders})
                    GROUP BY COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, ''))
                    ORDER BY artist_match DESC, album_match DESC, track_count DESC,
                             LOWER(MAX(COALESCE(NULLIF(album_artist, ''), artist))),
                             LOWER(MAX(COALESCE(NULLIF(album, ''), '')))
                    LIMIT :limit
                """),
                {
                    "artist_filter": artist_filter,
                    "album_filter": album_filter,
                    **{f"st{i}": s for i, s in enumerate(active_statuses)},
                    "limit": limit,
                },
            ).fetchall() or []

        releases = []
        for row in rows:
            mapping = row._mapping
            releases.append({
                "mbid": str(mapping.get("mbid") or ""),
                "artist": str(mapping.get("release_artist") or ""),
                "album": str(mapping.get("release_album") or ""),
                "year": str(mapping.get("resolved_year") or ""),
                "track_count": int(mapping.get("track_count") or 0),
            })

        return _json_response({"success": True, "releases": releases})

    except Exception as exc:
        logger.error("Error fetching matched releases", error=str(exc))
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Reset match (per queue item)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/<int:queue_id>/reset-match", methods=["POST"])
def api_queue_reset_match(queue_id: int) -> Any:
    """Reset a queue item's match so it can be rematched manually."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET release_mbid = NULL, release_id = NULL, release_source = NULL,
                        mb_release_group_id = NULL, mb_match_status = NULL, mb_match_score = NULL,
                        mb_match_candidates = NULL, mb_matched_title = NULL, mb_matched_artist = NULL,
                        mb_matched_year = NULL, mb_last_match_at = NULL,
                        status = 'unmatched',
                        file_path = NULL, matched_file_path = NULL, music_file_path = NULL,
                        found_filename = NULL, source_music_path = NULL,
                        in_collection = 0, collection_track_id = NULL, collection_matched_at = NULL,
                        failure_reason = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :queue_id
                """),
                {"queue_id": queue_id},
            )
            updated = result.rowcount > 0

        if not updated:
            return _json_response({"error": "Queue item not found"}), 404
        return _json_response({"success": True, "message": "Queue item match reset", "queue_id": queue_id})

    except Exception as exc:
        logger.error("Failed to reset queue match", queue_id=queue_id, error=str(exc))
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Apply MBID match (per queue item)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/<int:queue_id>/apply-mbid-match", methods=["POST"])
async def api_queue_apply_mbid_match(queue_id: int) -> Any:
    """Apply a MusicBrainz release match to a single queue item."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        new_mbid = (payload.get("new_mbid") or "").strip()
        new_artist = (payload.get("new_artist") or "").strip()
        new_album = (payload.get("new_album") or "").strip()

        if not new_mbid:
            return _json_response({"success": False, "error": "new_mbid is required"}), 400

        import_group = f"mbid_{new_mbid}"

        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET release_mbid = :new_mbid, release_id = :new_mbid, release_source = 'musicbrainz',
                        album = COALESCE(:new_album, album), album_artist = COALESCE(:new_artist, album_artist),
                        import_group = :import_group, status = 'matched', updated_at = CURRENT_TIMESTAMP
                    WHERE id = :queue_id
                """),
                {
                    "new_mbid": new_mbid,
                    "new_album": new_album,
                    "new_artist": new_artist,
                    "import_group": import_group,
                    "queue_id": queue_id,
                },
            )
            updated = result.rowcount > 0

        if not updated:
            return _json_response({"error": "Queue item not found"}), 404
        return _json_response({"success": True, "message": "MBID match applied", "queue_id": queue_id})

    except Exception as exc:
        logger.error("Error applying MBID match", queue_id=queue_id, error=str(exc))
        return _json_response({"success": False, "error": str(exc)}), 500
