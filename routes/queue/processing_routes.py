"""
Queue processing routes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
from typing import Any

import structlog
from quart import Blueprint, Response, request
from sqlalchemy import text

from db.engine import db_session
from routes.utils import json_response as _json_response
from services.downloads.download_processing_service import (
    queue_add,
    queue_add_batch,
    queue_cancel,
    queue_clear,
    queue_delete,
    queue_force_start,
    queue_imported,
    queue_purge_all,
    queue_requeue,
    queue_requeue_all_unmatched,
    queue_retry_all_failed,
    queue_status,
    queue_update,
)
from services.downloads.download_matching_service import get_release_tracks

try:
    from rapidfuzz import fuzz as _ratio_fuzz
    def _fuzzy_ratio(a: str, b: str) -> float:
        return _ratio_fuzz.token_set_ratio(a or "", b or "") / 100.0
except ImportError:
    from difflib import SequenceMatcher as _SequenceMatcher
    def _fuzzy_ratio(a: str, b: str) -> float:
        return _SequenceMatcher(None, a or "", b or "").ratio()

logger = structlog.get_logger(__name__)
queue_processing_bp = Blueprint("queue_processing", __name__)


def _norm_text(value: Any) -> str:
    """Helper to normalize text for fuzzy matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()


@queue_processing_bp.route("/api/queue/add", methods=["POST"])
async def api_queue_add() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(queue_add(payload))


@queue_processing_bp.route("/api/queue/add-batch", methods=["POST"])
async def api_queue_add_batch() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(queue_add_batch(payload))


@queue_processing_bp.route("/api/queue/status", methods=["GET"])
def api_queue_status() -> Any:
    return _json_response(queue_status(request.args))


@queue_processing_bp.route("/api/queue/imported", methods=["GET"])
def api_queue_imported() -> Any:
    return _json_response(queue_imported(request.args))


@queue_processing_bp.route("/api/queue/<int:queue_id>/update", methods=["POST"])
async def api_queue_update(queue_id: int) -> Any:
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(queue_update(queue_id, payload))


@queue_processing_bp.route("/api/queue/<int:queue_id>/send", methods=["POST"])
def api_queue_send_to_download(queue_id: int) -> Any:
    return _json_response(queue_requeue(queue_id))


@queue_processing_bp.route("/api/queue/<int:queue_id>/requeue", methods=["POST"])
def api_queue_requeue_item(queue_id: int) -> Any:
    return _json_response(queue_requeue(queue_id))


@queue_processing_bp.route("/api/queue/<int:queue_id>/force-start", methods=["POST"])
def api_queue_force_start(queue_id: int) -> Any:
    return _json_response(queue_force_start(queue_id))


@queue_processing_bp.route("/api/queue/<int:queue_id>/cancel", methods=["POST"])
def api_queue_cancel_item(queue_id: int) -> Any:
    return _json_response(queue_cancel(queue_id))


@queue_processing_bp.route("/api/queue/<int:queue_id>/delete", methods=["DELETE"])
def api_queue_delete(queue_id: int) -> Any:
    delete_download_file = request.args.get("delete_download_file", "0").lower() in {"1", "true", "yes"}
    return _json_response(queue_delete(queue_id, delete_download_file=delete_download_file))


@queue_processing_bp.route("/api/queue/clear", methods=["POST", "DELETE"])
async def api_queue_clear() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(queue_clear(payload))


@queue_processing_bp.route("/api/queue/purge-all", methods=["POST", "DELETE"])
def api_queue_purge_all() -> Any:
    return _json_response(queue_purge_all())


@queue_processing_bp.route("/api/queue/requeue-all-unmatched", methods=["POST"])
def api_queue_requeue_all_unmatched() -> Any:
    return _json_response(queue_requeue_all_unmatched())


@queue_processing_bp.route("/api/queue/retry-all-failed", methods=["POST"])
def api_queue_retry_all_failed() -> Any:
    return _json_response(queue_retry_all_failed())


@queue_processing_bp.route("/api/queue/clear-outage-misses", methods=["POST"])
async def api_queue_clear_outage_misses() -> Any:
    """Release retry dates that a SEARCH OUTAGE stamped, not a genuine miss.

    Body (all optional)::

        {"dry_run": true}        # default — report what WOULD be cleared
        {"dry_run": false}       # actually clear
        {"lookback_days": 7}
        {"streak_minimum": 5}

    Only rows whose stamp falls inside a detected run of consecutive
    zero-result/disconnected searches are touched, so a genuine miss keeps the
    backoff it earned. See ``services.queue.outage_miss_service``.
    """
    from services.queue.outage_miss_service import clear_outage_misses

    payload = (await request.get_json(silent=True)) or {}

    def _as_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().casefold() not in ("", "0", "false", "no", "off")
        return bool(value)

    dry_run = _as_bool(payload.get("dry_run"), default=True)

    try:
        lookback_days = max(1, int(payload.get("lookback_days") or 7))
    except (TypeError, ValueError):
        lookback_days = 7
    try:
        streak_minimum = max(2, int(payload.get("streak_minimum") or 5))
    except (TypeError, ValueError):
        streak_minimum = 5

    try:
        result = clear_outage_misses(
            lookback_days=lookback_days,
            streak_minimum=streak_minimum,
            dry_run=dry_run,
        )
    except Exception as exc:
        return _json_response({"success": False, "error": str(exc)})

    return _json_response(result)


# ── SSE: real-time queue progress stream ──────────────────────────────────

@queue_processing_bp.route("/api/queue/stream", methods=["GET"])
async def api_queue_stream() -> Any:
    """Server-Sent Events endpoint for live queue status updates."""
    async def event_generator():
        last_payload = None
        while True:
            try:
                from db.repositories.queue import get_queue_status_counts
                counts = get_queue_status_counts()
                payload = json.dumps(counts)

                if payload != last_payload:
                    yield f"data: {payload}\n\n"
                    last_payload = payload
            except Exception:
                yield f"data: {json.dumps({'error': 'failed to read queue status'})}\n\n"

            await asyncio.sleep(2)

    return Response(
        event_generator(),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# -----------------------------------------------------------------------------
# Copy from local music path
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/<int:queue_id>/copy-from-local", methods=["POST"])
def api_queue_copy_from_local(queue_id: int) -> Any:
    """Copy a file from its existing /music location to /downloads."""
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT * FROM download_queue WHERE id = :qid"),
                {"qid": queue_id},
            ).fetchone()

        if not row:
            return _json_response({"error": "Queue item not found"}), 404

        item = dict(row._mapping)
        source_path = (item.get("source_music_path") or "").strip()
        
        if not source_path:
            return _json_response({"success": False, "error": "No source music path stored"}), 400
        if not os.path.isfile(source_path):
            return _json_response({"success": False, "error": f"Source file not found: {source_path}"}), 400

        downloads_dir = os.environ.get("DOWNLOADS_DIR", "/downloads")
        dest_filename = os.path.basename(source_path)
        dest_path = os.path.join(downloads_dir, dest_filename)

        if os.path.exists(dest_path):
            base, ext = os.path.splitext(dest_filename)
            dest_path = os.path.join(downloads_dir, f"{base}_local_copy{ext}")

        def _background_copy() -> None:
            try:
                shutil.copy2(source_path, dest_path)
                logger.info("File copied locally", queue_id=queue_id, source=source_path, dest=dest_path)
                queue_update(queue_id, status="matched", file_path=dest_path)
            except Exception as bg_err:
                logger.error("Background copy failed", queue_id=queue_id, error=str(bg_err))
                try:
                    queue_update(queue_id, status="failed", failure_reason=str(bg_err)[:200])
                except Exception:
                    pass

        threading.Thread(target=_background_copy, daemon=True, name=f"copy-from-local-{queue_id}").start()

        return _json_response({
            "success": True,
            "message": "File copy started — will be tagged and moved automatically.",
            "dest_path": dest_path,
        })

    except Exception as exc:
        logger.error("Copy from local failed", queue_id=queue_id, error=str(exc))
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Migrate existing queue items
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/migrate-existing", methods=["POST"])
async def api_queue_migrate_existing() -> Any:
    """Backfill legacy queue rows into current grouped/source conventions."""
    try:
        from services.queue.migration_service import migrate_existing_queue_items_to_grouped_setup as _migrate
    except ImportError:
        return _json_response({
            "success": False,
            "error": "Migration service not available — see services/queue/migration_service.py",
        }), 501

    try:
        payload = (await request.get_json(silent=True)) or {}
        limit = request.values.get("limit") or payload.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
                if limit <= 0:
                    return _json_response({"error": "limit must be a positive integer"}), 400
            except (TypeError, ValueError):
                return _json_response({"error": "limit must be an integer"}), 400
                
        result = _migrate(limit=limit)
        status_code = 200 if result.get("success") else 500
        return _json_response((result, status_code))
    except Exception as exc:
        logger.error("Error migrating existing queue rows", error=str(exc), exc_info=True)
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Update album MBID
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/update-album-mbid", methods=["POST"])
async def api_queue_update_album_mbid() -> Any:
    """Update all queue items for an album with a new MusicBrainz release ID."""
    try:
        data = (await request.get_json(silent=True)) or {}
        old_artist = (data.get("old_artist") or "").strip()
        old_album = (data.get("old_album") or "").strip()
        new_mbid = (data.get("new_mbid") or "").strip()
        new_artist = (data.get("new_artist") or "").strip()
        new_album = (data.get("new_album") or "").strip()

        if not all([old_artist, old_album, new_mbid]):
            return _json_response({"error": "Missing required fields (old_artist, old_album, new_mbid)"}), 400

        mbid_import_group = f"mbid_{new_mbid}"

        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET release_mbid = :new_mbid, release_id = :new_mbid, release_source = 'musicbrainz',
                        album = :new_album, album_artist = :new_artist, import_group = :mbid_import_group,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:old_artist)
                      AND LOWER(album) = LOWER(:old_album)
                """),
                {
                    "new_mbid": new_mbid,
                    "new_album": new_album,
                    "new_artist": new_artist,
                    "mbid_import_group": mbid_import_group,
                    "old_artist": old_artist,
                    "old_album": old_album,
                },
            )
            updated_count = result.rowcount or 0

            result2 = session.execute(
                text("""
                    UPDATE download_queue
                    SET import_group = :mbid_import_group, release_source = 'musicbrainz',
                        album_artist = COALESCE(NULLIF(album_artist, ''), :new_artist),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) = :new_mbid
                """),
                {"mbid_import_group": mbid_import_group, "new_artist": new_artist, "new_mbid": new_mbid},
            )
            merged_count = result2.rowcount or 0

        return _json_response({
            "success": True,
            "message": f"Updated {updated_count} queue items with new MBID",
            "updated_count": updated_count,
            "merged_count": merged_count,
            "release_mbid": new_mbid,
        })

    except Exception as exc:
        logger.error("Error updating album MBID", error=str(exc), exc_info=True)
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Missing tracks
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/missing-tracks", methods=["GET"])
def api_queue_missing_tracks() -> Any:
    """Return MusicBrainz release tracks not currently present in queue rows."""
    try:
        release_mbid = (request.args.get("release_mbid") or "").strip()
        queue_ids_raw = (request.args.get("queue_ids") or "").strip()

        if not release_mbid:
            return _json_response({"success": False, "error": "release_mbid is required"}), 400

        queue_ids = []
        if queue_ids_raw:
            for value in queue_ids_raw.split(","):
                v = value.strip()
                if not v:
                    continue
                try:
                    qid = int(v)
                    if qid > 0:
                        queue_ids.append(qid)
                except Exception:
                    continue
            queue_ids = list(dict.fromkeys(queue_ids))

        release_tracks = get_release_tracks(release_id=release_mbid, source="musicbrainz") or []

        if not release_tracks:
            return _json_response({
                "success": True, "release_mbid": release_mbid,
                "total_release_tracks": 0, "missing_tracks": [],
            })

        with db_session() as session:
            if queue_ids:
                ids_placeholders = ", ".join(f":qid{i}" for i in range(len(queue_ids)))
                queue_rows = session.execute(
                    text(f"""
                        SELECT id, title, track_number, disc_number, recording_mbid
                        FROM download_queue
                        WHERE id IN ({ids_placeholders})
                          AND status NOT IN ('removed', 'cancelled', 'deleted')
                    """),
                    {f"qid{i}": qid for i, qid in enumerate(queue_ids)},
                ).fetchall() or []
            else:
                queue_rows = session.execute(
                    text("""
                        SELECT id, title, track_number, disc_number, recording_mbid
                        FROM download_queue
                        WHERE COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) = :release_mbid
                          AND status NOT IN ('removed', 'cancelled', 'deleted')
                    """),
                    {"release_mbid": release_mbid},
                ).fetchall() or []

        existing_recording_mbids = set()
        existing_track_keys = set()

        for row in queue_rows:
            mapping = row._mapping
            recording_mbid = (str(mapping.get("recording_mbid") or "")).strip()
            if recording_mbid:
                existing_recording_mbids.add(recording_mbid.lower())
                
            disc = str(mapping.get("disc_number") or "").strip()
            track = str(mapping.get("track_number") or "").strip()
            title = _norm_text(mapping.get("title"))
            key = f"{disc}:{track}:{title}" if (disc or track or title) else ""
            
            if key:
                existing_track_keys.add(key)

        missing_tracks = []
        for track_dict in release_tracks:
            recording_mbid = (track_dict.get("recording_mbid") or "").strip()
            disc_number = track_dict.get("disc_number")
            track_number = track_dict.get("track_number")
            title = (track_dict.get("title") or "").strip()

            disc = str(disc_number or "").strip()
            track_str = str(track_number or "").strip()
            title_norm = _norm_text(title)
            track_key = f"{disc}:{track_str}:{title_norm}" if (disc or track_str or title_norm) else ""

            if recording_mbid and recording_mbid.lower() in existing_recording_mbids:
                continue
            if track_key and track_key in existing_track_keys:
                continue

            missing_tracks.append({
                "disc_number": disc_number,
                "track_number": track_number,
                "title": title,
                "artist": track_dict.get("artist") or release_tracks[0].get("artist") or "",
                "duration": track_dict.get("duration") or 0,
                "recording_mbid": recording_mbid,
            })

        return _json_response({
            "success": True,
            "release_mbid": release_mbid,
            "release_title": "",
            "release_artist": release_tracks[0].get("artist") or "",
            "total_release_tracks": len(release_tracks),
            "missing_tracks": missing_tracks,
        })

    except Exception as exc:
        logger.error("Error fetching missing tracks", error=str(exc))
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Import missing tracks
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/import-missing-tracks", methods=["POST"])
async def api_queue_import_missing_tracks() -> Any:
    """Match selected missing MusicBrainz release tracks to existing queue rows."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        release_mbid = (payload.get("release_mbid") or "").strip()
        selected_keys = payload.get("selected_keys") or []
        queue_ids_raw = payload.get("queue_ids") or []

        if not release_mbid:
            return _json_response({"success": False, "error": "release_mbid is required"}), 400
        if not selected_keys:
            return _json_response({"success": False, "error": "selected_keys is required"}), 400

        selected_keys = list(dict.fromkeys(str(k).strip() for k in selected_keys if str(k).strip()))
        queue_ids = list(dict.fromkeys(int(v) for v in queue_ids_raw if str(v).strip().isdigit()))

        release_tracks = get_release_tracks(release_id=release_mbid, source="musicbrainz") or []
        if not release_tracks:
            return _json_response({"success": False, "error": "No MusicBrainz track metadata available"}), 400

        with db_session() as session:
            ref_artist = ref_album = ref_import_group = ""
            if queue_ids:
                ids_ph = ", ".join(f":qid{i}" for i in range(len(queue_ids)))
                ref = session.execute(
                    text(f"""
                        SELECT artist, album, album_artist, import_group
                        FROM download_queue WHERE id IN ({ids_ph}) ORDER BY id LIMIT 1
                    """),
                    {f"qid{i}": qid for i, qid in enumerate(queue_ids)},
                ).fetchone()
                
                if ref:
                    mapping = ref._mapping
                    ref_artist = str(mapping.get("artist") or "")
                    ref_album = str(mapping.get("album") or "")
                    ref_import_group = str(mapping.get("import_group") or "")

            release_title = ref_album
            release_artist = release_tracks[0].get("artist") or ""
            import_group = ref_import_group or f"mbid_{release_mbid}"

            candidate_rows = session.execute(
                text("""
                    SELECT id, title, track_number, disc_number, recording_mbid
                    FROM download_queue
                    WHERE (LOWER(COALESCE(NULLIF(album, ''), '')) = LOWER(:release_title)
                           OR (import_group IS NOT NULL AND import_group = :import_group))
                      AND COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) IS DISTINCT FROM :release_mbid
                      AND status NOT IN ('removed', 'cancelled', 'deleted', 'imported', 'in_collection')
                """),
                {"release_title": release_title, "import_group": import_group, "release_mbid": release_mbid},
            ).fetchall() or []

            available = {}
            for row in candidate_rows:
                mapping = row._mapping
                row_id = mapping.get("id")
                if row_id:
                    title_norm = _norm_text(mapping.get("title"))
                    available[row_id] = {
                        "id": row_id,
                        "title_norm": title_norm,
                        "track_number": str(mapping.get("track_number") or "").strip(),
                        "disc_number": str(mapping.get("disc_number") or "").strip(),
                        "recording_mbid": str(mapping.get("recording_mbid") or "").strip(),
                    }

            matched_count = no_match_count = failed_count = 0
            matched_items = []
            no_match_items = []
            failed_items = []

            for track_dict in release_tracks:
                title = (track_dict.get("title") or "").strip()
                if not title:
                    continue
                recording_mbid = (track_dict.get("recording_mbid") or "").strip()
                disc_number = track_dict.get("disc_number")
                track_number = track_dict.get("track_number")
                key = (recording_mbid or f"{disc_number or ''}:{track_number or ''}:{_norm_text(title)}")
                
                if key not in selected_keys:
                    continue

                title_norm = _norm_text(title)
                best_id = None
                best_score = -1.0

                for row_id, cand in available.items():
                    score = 0.0
                    if recording_mbid and cand["recording_mbid"] == recording_mbid:
                        score = 1.0
                    elif title_norm and cand["title_norm"]:
                        score = _fuzzy_ratio(title_norm, cand["title_norm"])
                        if track_number and cand["track_number"] and str(track_number).strip() == cand["track_number"]:
                            score = min(1.0, score + 0.15)
                    if score > best_score:
                        best_score = score
                        best_id = row_id

                if best_id is None or best_score < 0.6:
                    no_match_count += 1
                    no_match_items.append({"title": title})
                    continue

                try:
                    updates = {
                        "release_mbid": release_mbid,
                        "release_id": release_mbid,
                        "release_source": "musicbrainz",
                        "import_group": import_group,
                        "album_artist": release_artist or None,
                        "album": release_title or None,
                    }
                    if recording_mbid:
                        updates["recording_mbid"] = recording_mbid
                    if title:
                        updates["title"] = title
                    if track_number:
                        updates["track_number"] = str(track_number)
                    if disc_number:
                        updates["disc_number"] = str(disc_number)

                    set_clause = ", ".join(f"{col} = :{col}" for col in updates)
                    session.execute(
                        text(f"UPDATE download_queue SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = :item_id"),
                        {**updates, "item_id": best_id},
                    )
                    session.commit()
                    del available[best_id]
                    matched_count += 1
                    matched_items.append({"queue_id": best_id, "title": title, "match_score": round(best_score, 3)})
                except Exception as upd_err:
                    logger.warning("Failed to update queue row", row_id=best_id, error=str(upd_err))
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    failed_count += 1
                    failed_items.append({"title": title})

        return _json_response({
            "success": True,
            "release_mbid": release_mbid,
            "matched_count": matched_count,
            "no_match_count": no_match_count,
            "failed_count": failed_count,
            "matched_items": matched_items,
            "no_match_items": no_match_items,
            "failed_items": failed_items,
        })

    except Exception as exc:
        logger.error("Error matching missing tracks", error=str(exc))
        return _json_response({"success": False, "error": str(exc)}), 500
