"""Download management routes.

Handles:
- Download folder display and matching.
- Queue item management (add, cancel, retry).
- MusicBrainz matching workflows.
- Download orchestration triggers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import structlog
from quart import Blueprint, jsonify, request
from sqlalchemy import text

from api_clients.slskd_http import SlskdHttpClient
from db.engine import db_session
from db.repositories.queue import (
    get_queue_display_items,
    get_queue_status_counts,
    insert_queue_item,
    update_queue_item,
)
from db.repositories.queue_admin import clear_queue as _clear_queue
from helpers.config_helpers import get_config
from helpers.metadata_reader import read_mp3_metadata

# Exported from services.downloads.__init__.py
from services.downloads import (
    apply_musicbrainz_match,
    auto_match_folder,
    cancel_folder,
    check_folder_duplicates,
    discover_files,
    get_release_status,
    get_release_tracks,
    get_scan_progress,
    match_folder,
    process_album_existing,
    scan_downloads,
    scheduler_status,
    start_scheduler,
    stop_scheduler,
    verify_moved_files,
)
from services.downloads.download_folder_service import (
    associate_folder_to_release,
    cancel_folder_downloads,
    delete_download_folder,
    delete_folder_track,
    get_folder_details,
    get_folder_groups_with_musicbrainz,
    get_folder_tracks,
    get_unmatched_folders,
    match_folder_to_release,
    move_folder_track_to_library,
    refresh_folder_matches,
)
from services.downloads.download_organize_service import (
    merge_folders,
    organize_folder,
    organize_track,
)
from services.downloads.download_pipeline_service import (
    run_pipeline,
    start_release_download,
    sync_transfers,
)
from services.downloads.download_processing_service import (
    process_albums as _process_albums_impl,
    process_single_file as _process_single_file_impl,
)
from services.downloads.match_orchestrator import apply_mbid_match_batch
from services.downloads.slskd_service import SlskdService
from services.queue.queue_constraints import (
    ACTIVE_SECTION,
    FAILED_SECTION,
    QUEUE_DISPLAY_STATUSES,
    READY_SECTION,
)
from services.queue.queue_orchestrator import process_next_batch
from services.queue.queue_signal import signal_new_item

#: Retained for compatibility and as the single source of truth for "which
#: statuses the queue page shows". Kept as an alias of the shared constant so
#: there is exactly ONE definition of that set.
#:
#: ⚠️ This used to be ACTIVE|FAILED|PENDING_RETRY, which excluded
#: ``unmatched``/``matched``/``pending_match``/``discovered`` — the statuses the
#: CLIENT's "Queued" pill counted. That is why the page showed "74 queued" above
#: a list of 18 and no amount of paging reconciled them.
QUEUE_LISTED_STATUSES: frozenset[str] = QUEUE_DISPLAY_STATUSES

logger = structlog.get_logger(__name__)
downloads_bp = Blueprint("downloads", __name__)


# =============================================================================
# ✅ FOLDER APIs
# =============================================================================

@downloads_bp.route("/api/downloads/folder-groups")
def api_get_folder_groups() -> Any:
    return jsonify(get_folder_groups_with_musicbrainz())


@downloads_bp.route("/api/downloads/grouped-folders")
def api_get_grouped_folders() -> Any:
    """Alias used by monitor JS — returns same data as folder-groups."""
    return jsonify(get_folder_groups_with_musicbrainz())


@downloads_bp.route("/api/downloads/unmatched-folders")
def api_get_unmatched_folders() -> Any:
    """Folders under the downloads dir not tracked as MusicBrainz releases."""
    return jsonify(get_unmatched_folders())


@downloads_bp.route("/api/downloads/folder/match", methods=["POST"])
async def api_match_folder_to_release() -> Any:
    """Copy an unmatched folder into the library as a MusicBrainz release."""
    payload = (await request.get_json(silent=True)) or {}
    folder_path = (payload.get("folder_path") or "").strip()
    mb_id = (payload.get("mb_id") or payload.get("mbid") or "").strip()
    if not folder_path or not mb_id:
        return jsonify({"success": False, "error": "folder_path and mb_id (release/release-group URL or ID) are required"}), 400
        
    # Filesystem + DB work — offload so the event loop stays responsive.
    return jsonify(await asyncio.to_thread(match_folder_to_release, folder_path, mb_id))


@downloads_bp.route("/api/downloads/folder/associate", methods=["POST"])
async def api_associate_folder_to_release() -> Any:
    """Phase 1 of the two-phase folder-match flow: record the folder → release
    association WITHOUT moving any files. The folder stays passive on disk."""
    payload = (await request.get_json(silent=True)) or {}
    folder_path = (payload.get("folder_path") or "").strip()
    mb_id = (payload.get("mb_id") or payload.get("mbid") or "").strip()
    if not folder_path or not mb_id:
        return jsonify({"success": False, "error": "folder_path and mb_id (release/release-group URL or ID) are required"}), 400
        
    return jsonify(await asyncio.to_thread(associate_folder_to_release, folder_path, mb_id))


@downloads_bp.route("/api/downloads/confirm-match", methods=["POST"])
async def api_confirm_folder_match() -> Any:
    """Phase 2 of the two-phase folder-match flow: confirm an associated
    folder — write tags, format the path, move the files to /music and
    remove the folder from the Matched Folders list."""
    payload = (await request.get_json(silent=True)) or {}
    folder_path = (payload.get("folder_path") or "").strip()
    release_mbid = (
        payload.get("release_mbid")
        or payload.get("mb_id")
        or payload.get("mbid")
        or ""
    ).strip()
    
    if not folder_path or not release_mbid:
        return jsonify({"success": False, "error": "folder_path and release_mbid are required"}), 400
        
    return jsonify(await asyncio.to_thread(match_folder_to_release, folder_path, release_mbid))


@downloads_bp.route("/api/downloads/folder/delete", methods=["POST"])
async def api_delete_download_folder() -> Any:
    """Delete a folder under the downloads directory (safety-railed)."""
    payload = (await request.get_json(silent=True)) or {}
    folder_path = (payload.get("folder_path") or "").strip()
    if not folder_path:
        return jsonify({"success": False, "error": "folder_path required"}), 400
        
    return jsonify(await asyncio.to_thread(delete_download_folder, folder_path))


# =============================================================================
# ✅ PER-TRACK ACTIONS (Matched Folders)
# =============================================================================

@downloads_bp.route("/api/downloads/folder/<path:folder_path>/tracks")
def api_folder_tracks(folder_path: str) -> Any:
    """List the audio tracks (files) inside a Matched-Folders folder."""
    return jsonify(get_folder_tracks(folder_path))


@downloads_bp.route("/api/downloads/folder/<path:folder_path>/track/delete", methods=["POST"])
async def api_delete_folder_track(folder_path: str) -> Any:
    """Delete ONE audio file from a Matched-Folders folder."""
    payload = (await request.get_json(silent=True)) or {}
    file_name = (payload.get("file_name") or "").strip()
    if not file_name:
        return jsonify({"success": False, "error": "file_name required"}), 400
        
    return jsonify(await asyncio.to_thread(delete_folder_track, folder_path, file_name))


@downloads_bp.route("/api/downloads/folder/<path:folder_path>/track/move", methods=["POST"])
async def api_move_folder_track(folder_path: str) -> Any:
    """Move ONE audio file from a Matched-Folders folder into the library."""
    payload = (await request.get_json(silent=True)) or {}
    file_name = (payload.get("file_name") or "").strip()
    if not file_name:
        return jsonify({"success": False, "error": "file_name required"}), 400
        
    return jsonify(await asyncio.to_thread(move_folder_track_to_library, folder_path, file_name))


@downloads_bp.route("/api/downloads/folder-matches/refresh", methods=["POST"])
async def api_refresh_folder_matches() -> Any:
    """Re-sync stored folder → release associations with the current torrent flattening."""
    return jsonify(await asyncio.to_thread(refresh_folder_matches))


@downloads_bp.route("/api/downloads/folder-status")
def api_get_folder_status() -> Any:
    """Return folder status summary (stub for now)."""
    return jsonify({"success": True, "scanning": False, "folders": [], "total": 0})


@downloads_bp.route("/api/downloads/folder-duplicates")
def api_get_folder_duplicates() -> Any:
    """Return folder duplicate info (stub for now)."""
    return jsonify({"success": True, "duplicates": [], "total": 0})


@downloads_bp.route("/api/downloads/folder/<path:folder_path>")
def api_get_folder_details(folder_path: str) -> Any:
    return jsonify(get_folder_details(folder_path))


@downloads_bp.route("/api/downloads/folder/<path:folder_path>/cancel", methods=["POST"])
def api_cancel_folder_downloads_route(folder_path: str) -> Any:
    return jsonify(cancel_folder_downloads(folder_path))


# =============================================================================
# ✅ SCAN / DISCOVERY
# =============================================================================

@downloads_bp.route("/api/downloads/scan")
def api_downloads_scan() -> Any:
    return jsonify(scan_downloads(read_mp3_metadata))


@downloads_bp.route("/api/downloads/discover", methods=["POST"])
def api_downloads_discover() -> Any:
    return jsonify(discover_files())


@downloads_bp.route("/api/downloads/scan-progress")
def api_downloads_scan_progress() -> Any:
    return jsonify(get_scan_progress())


@downloads_bp.route("/api/downloads/verify-moved-files")
def api_downloads_verify_moved() -> Any:
    minutes = int(request.args.get("minutes_old", 30))
    return jsonify(verify_moved_files(minutes))


# =============================================================================
# ✅ GROUPING / MATCHING
# =============================================================================

@downloads_bp.route("/api/downloads/folder/<path:folder_path>/match-musicbrainz", methods=["POST"])
async def api_match_folder_musicbrainz(folder_path: str) -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(match_folder, folder_path, payload))


@downloads_bp.route("/api/downloads/folder/<path:folder_path>/auto-match", methods=["POST"])
async def api_auto_match_folder_route(folder_path: str) -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(auto_match_folder, folder_path, payload))


@downloads_bp.route("/api/downloads/release/<source>/<release_id>/tracks")
def api_release_tracks_route(source: str, release_id: str) -> Any:
    return jsonify(get_release_tracks(release_id=release_id, source=source))


@downloads_bp.route("/api/downloads/folder/<path:folder_path>/duplicates", methods=["POST"])
async def api_check_duplicates_route(folder_path: str) -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(check_folder_duplicates, folder_path, payload))


@downloads_bp.route("/api/queue/apply-mbid-match-batch", methods=["POST"])
async def api_queue_apply_mbid_match_batch() -> Any:
    try:
        data = (await request.get_json(force=True, silent=True)) or {}

        queue_ids = [
            int(x)
            for x in (data.get("queue_ids") or [])
            if str(x).isdigit()
        ]

        new_mbid = (data.get("new_mbid") or "").strip()
        new_artist = (data.get("new_artist") or "").strip()
        new_album = (data.get("new_album") or "").strip()

        result = await asyncio.to_thread(
            apply_mbid_match_batch,
            queue_ids=queue_ids,
            new_mbid=new_mbid,
            new_artist=new_artist,
            new_album=new_album,
        )

        if not result.get("success"):
            return jsonify(result), 400

        return jsonify(result)

    except Exception as exc:
        logger.error("Apply MBID batch failed", error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# =============================================================================
# ✅ ORGANIZE / MOVE / MERGE
# =============================================================================

@downloads_bp.route("/api/downloads/folder/<path:folder_path>/organize", methods=["POST"])
async def api_organize_folder_route(folder_path: str) -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(organize_folder, folder_path, payload))


@downloads_bp.route("/api/downloads/track/<int:track_index>/move", methods=["POST"])
async def api_move_track(track_index: int) -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(organize_track, track_index, payload))


@downloads_bp.route("/api/downloads/merge-folders", methods=["POST"])
async def api_merge_folders() -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(merge_folders, payload))


# =============================================================================
# ✅ PROCESSING
# =============================================================================

@downloads_bp.route("/api/downloads/process", methods=["POST"])
def api_process() -> Any:
    return jsonify(process_next_batch())


@downloads_bp.route("/api/downloads/process-one", methods=["POST"])
async def api_process_one() -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(process_single_file, payload))


@downloads_bp.route("/api/downloads/process-retry", methods=["POST"])
def api_process_retry() -> Any:
    return jsonify(process_retry_queue())


@downloads_bp.route("/api/downloads/process-albums", methods=["POST"])
def api_process_albums() -> Any:
    return jsonify(process_albums())


@downloads_bp.route("/api/downloads/albums/use-existing", methods=["POST"])
async def api_use_existing() -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(process_album_existing, payload))


@downloads_bp.route("/api/downloads/albums/apply-match", methods=["POST"])
async def api_apply_match() -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(apply_musicbrainz_match, payload))


@downloads_bp.route("/api/downloads/release-tracks", methods=["POST"])
async def api_release_status_route() -> Any:
    payload = await request.get_json()
    return jsonify(await asyncio.to_thread(get_release_status, payload))


# =============================================================================
# ✅ QUEUE
# =============================================================================

@downloads_bp.route("/api/downloads/queue")
def api_queue() -> Any:
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    try:
        status_counts = get_queue_status_counts()

        # ⚠️ DISPLAY query, NOT ``get_active_queue``. That one is the WORK query
        # (the slskd reaper cancels stalled transfers from it and the folder
        # matcher resolves tracks from it) and deliberately excludes
        # local/discovered sources, because a disk folder is not an active
        # transfer. Act-on-it and show-it are different questions.
        #
        # ⚠️ The three sections PARTITION every displayable status, so a row
        # lands in exactly one list. ``get_active_queue`` was used for the
        # active list, which is why an ``unmatched`` local folder was counted by
        # the pills but rendered by nothing.
        active_items = get_queue_display_items(
            ACTIVE_SECTION, limit=max(1, min(limit + offset, 500)), offset=offset,
        )
        if not offset:
            active_items = active_items[:limit]

        # ``Ready`` and ``Failed`` each come from their own section query rather
        # than being sliced out of the paged active list, so their badges show
        # that section's count and every row of it is renderable.
        ready_items = get_queue_display_items(READY_SECTION, limit=min(limit, 50))
        failed_items = get_queue_display_items(FAILED_SECTION, limit=min(limit, 100))

        # ``total`` is the pager's denominator, and the pager pages the ACTIVE
        # list, so it must describe the ACTIVE section and nothing else.
        # ⚠️ NOT the sum of every displayable status: that includes the Ready
        # card's rows, which are a different list — counting them here is what
        # produced a "showing 1-18 of 74" that could never be satisfied.
        queue_total = sum(
            int(count)
            for status, count in (status_counts or {}).items()
            if str(status) in ACTIVE_SECTION
        )

        # Per-section counts, derived from the SAME sets as the three queries —
        # so a pill reading "N" always has exactly N rows under it.
        def _section_total(section: frozenset[str]) -> int:
            return sum(
                int(count)
                for status, count in (status_counts or {}).items()
                if str(status) in section
            )

        # ⚠️ ``downloading_total`` is served so the client can present the two
        # sub-pills of the Active card (Queued = waiting, Active = downloading)
        # WITHOUT re-deriving either from a hand-written status list. Their sum
        # is exactly ``section_counts.active``, i.e. the number of rows in the
        # Active Queue list — which is what makes the pills reconcile with the
        # list instead of contradicting it.
        section_counts = {
            "active": _section_total(ACTIVE_SECTION),
            "ready": _section_total(READY_SECTION),
            "failed": _section_total(FAILED_SECTION),
            "downloading": _section_total(frozenset({"downloading"})),
        }

        return jsonify({
            "success": True,
            # ``queue`` keeps its name/meaning for existing callers: the active
            # section. ``completed``/``failed`` likewise.
            "queue": active_items,
            "completed": ready_items,
            "failed": failed_items,
            "section_counts": section_counts,
            "sections": {
                "active": sorted(ACTIVE_SECTION),
                "ready": sorted(READY_SECTION),
                "failed": sorted(FAILED_SECTION),
            },
            "status_counts": status_counts or {},
            "total": queue_total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.error("Failed to fetch queue status", error=str(exc))
        return jsonify({
            "success": False,
            "error": str(exc),
            "queue": [],
            "status_counts": {},
            "total": 0,
        })


@downloads_bp.route("/api/downloads/queue-upcoming", methods=["POST"])
async def api_queue_upcoming() -> Any:
    """Queue an upcoming release (by ``upcoming_release_id``) for download."""
    payload = await request.get_json(silent=True) or {}
    release_id = payload.get("upcoming_release_id") or payload.get("id")
    try:
        release_id = int(release_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "upcoming_release_id is required"}), 400

    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT * FROM upcoming_releases WHERE id = :id"),
                {"id": release_id},
            ).fetchone()
            
        if row is None:
            return jsonify({"success": False, "error": "release not found"}), 404

        release = dict(row._mapping)
        artist = (release.get("artist_name") or "").strip()
        album = (release.get("album_name") or "").strip()
        rel_date = (release.get("release_date") or "").strip()
        
        if not artist or not album:
            return jsonify({"success": False, "error": "release has no artist/album"}), 400

        today = datetime.now().date().isoformat()
        if not rel_date or rel_date > today:
            return jsonify({
                "success": False,
                "error": "release not out yet" if rel_date else "release date unknown",
                "release_date": rel_date,
            }), 400

        year = int(rel_date[:4]) if len(rel_date) >= 4 and rel_date[:4].isdigit() else None
        release_mbid = (release.get("release_group_mbid") or "").strip() or None

        if release_mbid:
            try:
                result = start_release_download(
                    release_mbid,
                    album,
                    artist,
                    method="slskd",
                    create_folder_group=False,
                )
                if result.get("success"):
                    queued_tracks = int(result.get("queue_items_created") or 0)
                    # ``start_release_download`` reports success: True even when
                    # MusicBrainz returned a release whose every track was
                    # already in the library / queue, which yielded ZERO new
                    # queue rows.  Marking the release "queued" on that basis
                    # showed a finished-looking release that had nothing
                    # downloading behind it.
                    if queued_tracks <= 0:
                        _reason = result.get("queue_reason") or "nothing_queued"
                        _message = (
                            result.get("queue_message")
                            or "No tracks could be queued for this release."
                        )
                        logger.warning(
                            "Upcoming release queued no tracks — not marking as queued",
                            release_id=release_id,
                            album=album,
                            reason=_reason,
                            message=_message,
                        )
                        # ``error`` is what the UI toasts, so it carries the
                        # human sentence; the machine code travels separately.
                        return jsonify({
                            "success": False,
                            "error": _message,
                            "reason": _reason,
                            "total_tracks": int(result.get("total_tracks") or 0),
                            "queued_tracks": 0,
                            "artist": artist,
                            "album": album,
                            "release_group_mbid": release_mbid,
                        }), 409

                    with db_session() as session:
                        session.execute(
                            text("UPDATE upcoming_releases SET status = 'queued' WHERE id = :id"),
                            {"id": release_id},
                        )
                    try:
                        signal_new_item()
                    except Exception:
                        pass
                        
                    return jsonify({
                        "success": True,
                        "total_tracks": queued_tracks,
                        "queued_tracks": queued_tracks,
                        "artist": artist,
                        "album": album,
                        "release_group_mbid": release_mbid,
                    })
                    
                logger.warning(
                    "MB pipeline failed, falling back to single item",
                    release_id=release_id, album=album, error=result.get("error"),
                )
            except Exception as exc:
                logger.warning(
                    "MB pipeline error, falling back to single item",
                    release_id=release_id, album=album, error=str(exc),
                )

        queued = insert_queue_item(
            artist=artist,
            title=album,
            album=album,
            source="soulseek",
            priority=5,
            year=year,
            release_mbid=release_mbid,
            import_type="album",
        )

        with db_session() as session:
            session.execute(
                text("UPDATE upcoming_releases SET status = 'queued' WHERE id = :id"),
                {"id": release_id},
            )

        return jsonify({
            "success": True,
            "already_queued": bool(queued.get("already_queued")),
            "queue_id": queued.get("id"),
            "artist": artist,
            "album": album,
        })
    except Exception as exc:
        logger.error("Failed to queue upcoming release", release_id=release_id, error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@downloads_bp.route("/api/downloads/clear-queue", methods=["POST"])
def api_clear_queue() -> Any:
    return jsonify(_clear_queue())


@downloads_bp.route("/api/downloads/retry-queue")
def api_retry_queue() -> Any:
    return jsonify(get_queue_status_counts())


@downloads_bp.route("/api/downloads/queue/grouped")
def api_grouped_queue() -> Any:
    return jsonify(get_queue_status_counts())


@downloads_bp.route("/api/downloads/queue/batch-group", methods=["POST"])
async def api_batch_group() -> Any:
    """Group selected queue items into a named album group."""
    data = (await request.get_json(silent=True)) or {}
    item_ids = data.get("item_ids") or []
    group_name = str(data.get("group_name") or "").strip()
    group_artist = str(data.get("group_artist") or "").strip()
    force_artist_override = bool(data.get("force_artist_override", False))

    if not isinstance(item_ids, list) or not item_ids or not group_name:
        return jsonify({"success": False, "error": "item_ids and group_name are required"}), 400

    empty_artists = {"", "unknown", "various", "various artists"}

    updated = artist_updated = artist_skipped = 0
    conflicts = []
    
    try:
        with db_session() as session:
            for item_id in item_ids:
                row = session.execute(
                    text("SELECT id, artist, title FROM download_queue WHERE id = :id"),
                    {"id": item_id},
                ).fetchone()
                
                if row is None:
                    continue

                mapping = row._mapping
                item_artist = str(mapping.get("artist") or "")
                item_title = str(mapping.get("title") or "")

                if group_artist:
                    current_norm = item_artist.strip().lower()
                    new_norm = group_artist.strip().lower()
                    should_update = (
                        force_artist_override
                        or current_norm in empty_artists
                        or current_norm == new_norm
                    )
                    
                    if should_update:
                        session.execute(
                            text("""
                                UPDATE download_queue
                                SET album = :album, artist = :artist,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = :id
                            """),
                            {"album": group_name, "artist": group_artist, "id": item_id},
                        )
                        artist_updated += 1
                    else:
                        session.execute(
                            text("""
                                UPDATE download_queue
                                SET album = :album, updated_at = CURRENT_TIMESTAMP
                                WHERE id = :id
                            """),
                            {"album": group_name, "id": item_id},
                        )
                        artist_skipped += 1
                        conflicts.append({
                            "id": item_id,
                            "title": item_title,
                            "artist": item_artist,
                        })
                else:
                    session.execute(
                        text("""
                            UPDATE download_queue
                            SET album = :album, updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                        """),
                        {"album": group_name, "id": item_id},
                    )
                updated += 1
    except Exception as exc:
        logger.error("Batch group failed", error=str(exc))
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify({
        "success": True,
        "updated": updated,
        "total": len(item_ids),
        "artist_updated": artist_updated,
        "artist_skipped": artist_skipped,
        "artist_conflicts": conflicts[:10],
        "message": f"Grouped {updated} items into '{group_name}'",
    })


@downloads_bp.route("/api/downloads/queue/<int:queue_id>", methods=["POST"])
async def api_manage_queue(queue_id: int) -> Any:
    _data = (await request.get_json()) or {}
    return jsonify(update_queue_item(queue_id, **_data) or {"success": False})


# =============================================================================
# ✅ SCHEDULER
# =============================================================================

@downloads_bp.route("/api/downloads/scheduler/start", methods=["POST"])
def api_scheduler_start_route() -> Any:
    return jsonify(start_scheduler())


@downloads_bp.route("/api/downloads/scheduler/stop", methods=["POST"])
def api_scheduler_stop_route() -> Any:
    return jsonify(stop_scheduler())


@downloads_bp.route("/api/downloads/scheduler/status")
def api_scheduler_status_route() -> Any:
    return jsonify(scheduler_status())


@downloads_bp.route("/api/downloads/pipeline/run", methods=["POST"])
def api_run_pipeline() -> Any:
    cfg = get_config()
    slskd_cfg = cfg.get("slskd", {})
    web_url = slskd_cfg.get("web_url", "http://localhost:5030")
    api_key = slskd_cfg.get("api_key", "")
    
    client = SlskdHttpClient(web_url=web_url, api_key=api_key)
    slskd = SlskdService(client)

    result = run_pipeline(slskd)
    return jsonify(result)


@downloads_bp.route("/api/downloads/pipeline/sync", methods=["POST"])
def api_pipeline_sync() -> Any:
    cfg = get_config()
    slskd_cfg = cfg.get("slskd", {})
    web_url = slskd_cfg.get("web_url", "http://localhost:5030")
    api_key = slskd_cfg.get("api_key", "")
    
    client = SlskdHttpClient(web_url=web_url, api_key=api_key)
    slskd = SlskdService(client)

    return jsonify(sync_transfers(slskd))


def process_single_file(data: dict[str, Any] | None = None) -> Any:
    return _process_single_file_impl(data or {})


def process_retry_queue() -> Any:
    return process_next_batch()


def process_albums() -> Any:
    return _process_albums_impl()
