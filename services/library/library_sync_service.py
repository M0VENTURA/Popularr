"""Incremental Navidrome library sync service with WebUI-visible status."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict

import structlog
from sqlalchemy import text

from api_clients.navidrome import NavidromeClient
from db.engine import db_session
from db.repositories.navidrome import bulk_upsert_navidrome_tracks
from db.utils import row_get
from helpers.config_helpers import get_scan_pipeline_config
from services.scanning.navidrome_import import scan_artist_to_db
from services.scanning.navidrome_service import build_delta_artist_index
from services.scanning.scan_state import (
    get_scan_checkpoint_path,
    load_scan_checkpoint,
    save_artist_scan_checkpoint,
)

logger = structlog.get_logger(__name__)

_scan_cfg = get_scan_pipeline_config()

_SYNC_CHECKPOINT = get_scan_checkpoint_path("library_sync")

_library_sync_lock = threading.Lock()
_library_sync_state: dict[str, bool] = {"running": False, "pending": False}
_last_processed_scan_marker: int | None = None
_library_status_lock = threading.Lock()
_library_status: dict[str, Any] = {
    "running": False,
    "pending": False,
    "started_at": None,
    "finished_at": None,
    "last_run": None,
    "last_processed_scan_marker": None,
    "artists_total": 0,
    "artists_processed": 0,
    "artists_failed": 0,
    "tracks_attempted": 0,
    "message": "Idle",
}


def get_library_sync_state() -> dict[str, Any]:
    with _library_status_lock:
        return dict(_library_status)


def request_library_sync() -> dict[str, Any]:
    with _library_sync_lock:
        if _library_sync_state["running"]:
            _library_sync_state["pending"] = True
            with _library_status_lock:
                _library_status["pending"] = True
                _library_status["message"] = "Sync already running; queued follow-up"
            return {"coalesced": True, "running": True, "pending": True}
        _library_sync_state.update({"running": True, "pending": False})

    with _library_status_lock:
        _library_status.update({
            "running": True,
            "pending": False,
            "started_at": time.time(),
            "finished_at": None,
            "message": "Starting library sync...",
            "artists_total": 0,
            "artists_processed": 0,
            "artists_failed": 0,
            "tracks_attempted": 0,
        })
    threading.Thread(target=_run_library_sync_worker, daemon=True, name="library-sync-worker").start()
    return {"started": True, "running": True}


def _run_library_sync_worker() -> None:
    try:
        result = perform_library_sync()
        try:
            from services.playlists.playlist_service import sync_playlists_public
            pl = sync_playlists_public()
            if pl.get("deferred"):
                logger.info(
                    "Playlist visibility sync deferred (Navidrome import running)",
                )
            elif pl.get("enabled"):
                logger.info(
                    "Playlist visibility sync complete",
                    checked=pl.get("checked", 0),
                    made_public=pl.get("made_public", 0),
                    failed=pl.get("failed", 0),
                )
        except Exception as exc:
            logger.debug("Playlist public sync skipped", error=str(exc))

        with _library_status_lock:
            _library_status.update({
                "message": result.get("reason") or "Completed",
                "last_run": time.time(),
                "artists_processed": result.get("artists_processed", _library_status.get("artists_processed", 0)),
                "artists_failed": result.get("artists_failed", _library_status.get("artists_failed", 0)),
                "tracks_attempted": result.get("tracks_attempted", _library_status.get("tracks_attempted", 0)),
                "last_processed_scan_marker": result.get("marker", _library_status.get("last_processed_scan_marker")),
            })
    except Exception as exc:
        logger.exception("Unexpected error during library sync")
        with _library_status_lock:
            _library_status["message"] = "Failed"
    finally:
        with _library_sync_lock:
            _library_sync_state["running"] = False
            pending = bool(_library_sync_state["pending"])
            _library_sync_state["pending"] = False

        with _library_status_lock:
            _library_status["running"] = False
            _library_status["pending"] = False
            _library_status["finished_at"] = time.time()

        if pending:
            request_library_sync()


def get_navidrome_config() -> dict[str, str] | None:
    try:
        from helpers.config_helpers import get_navidrome_first_user
        return get_navidrome_first_user() or None
    except Exception as exc:
        logger.debug("Could not load Navidrome config", error=str(exc))
        return None


def perform_library_sync() -> dict[str, Any]:
    global _last_processed_scan_marker
    started_at = time.time()
    cfg = get_navidrome_config()
    if not cfg:
        return {"success": False, "skipped": True, "reason": "missing_config"}

    client = NavidromeClient(
        base_url=cfg["base_url"],
        username=cfg["user"],
        password=cfg["pass"],
        # ⚠️ This IS an import path too, so it must be exempt from the import
        # gate (see services/navidrome/import_gate). An unmarked client here
        # would defer its own getArtists/getAlbum calls whenever the manual
        # import happened to be running, and the scheduled sync would silently
        # discover no artists.
        is_import=True,
    )
    scan_status = client.get_scan_status()
    if not scan_status.get("success"):
        return {"success": False, "skipped": True, "reason": "scan_status_failed"}
    if scan_status.get("scanning"):
        return {"success": False, "skipped": True, "reason": "navidrome_scanning"}

    marker = scan_status.get("count")
    last_scan_ts = scan_status.get("lastScan")

    checkpoint = load_scan_checkpoint(_SYNC_CHECKPOINT) or {}
    last_marker = checkpoint.get("scan_marker")
    last_scan_ts_stored = checkpoint.get("last_scan_ts")
    
    marker_unchanged = (
        marker is not None
        and last_marker is not None
        and marker == last_marker
        and _last_processed_scan_marker in (None, marker)
    )
    scan_unchanged = (
        last_scan_ts is not None
        and last_scan_ts_stored is not None
        and str(last_scan_ts) == str(last_scan_ts_stored)
    )
    if marker_unchanged and scan_unchanged:
        return {"success": True, "skipped": True, "reason": "marker_already_processed", "marker": marker}

    since_ts = last_scan_ts_stored or last_scan_ts
    candidate_artists = get_candidate_artists(client, since_ts=since_ts)
    if not candidate_artists:
        return {"success": True, "skipped": True, "reason": "no_candidate_artists"}

    with _library_status_lock:
        _library_status.update({"artists_total": len(candidate_artists), "message": f"Starting sync for {len(candidate_artists)} artists"})

    logger.info("Starting bulk diff-sync for artists", count=len(candidate_artists))

    batch_size = _scan_cfg.get("library_sync_batch_size", 100)
    all_tracks_to_upsert: list[dict[str, Any]] = []
    seen_track_ids: set[str] = set()
    artists_processed = 0
    artists_failed = 0
    tracks_attempted = 0
    total = len(candidate_artists)

    for index, (artist_name, artist_id) in enumerate(candidate_artists.items(), start=1):
        if not artist_id:
            continue
        with _library_status_lock:
            _library_status["message"] = f"Processing {artist_name} ({index}/{total})"
        try:
            result = sync_artist_with_diff(artist_name, artist_id)
            artists_processed += 1
            with _library_status_lock:
                _library_status["artists_processed"] = artists_processed

            for track in result.get("tracks", []) or []:
                track_id = track.get("id") if isinstance(track, dict) else None
                if track_id and track_id in seen_track_ids:
                    continue
                if track_id:
                    seen_track_ids.add(str(track_id))
                if isinstance(track, dict):
                    all_tracks_to_upsert.append(track)

            if len(all_tracks_to_upsert) >= batch_size:
                tracks_attempted += run_bulk_commit(all_tracks_to_upsert)
                all_tracks_to_upsert.clear()
                with _library_status_lock:
                    _library_status["tracks_attempted"] = tracks_attempted
        except Exception as exc:
            artists_failed += 1
            with _library_status_lock:
                _library_status["artists_failed"] = artists_failed
            logger.error("Artist sync failed", artist=artist_name, error=str(exc))

    if all_tracks_to_upsert:
        tracks_attempted += run_bulk_commit(all_tracks_to_upsert)

    _last_processed_scan_marker = marker
    marker_extra: dict[str, Any] = {}
    if marker is not None:
        marker_extra["scan_marker"] = marker
    if last_scan_ts is not None:
        marker_extra["last_scan_ts"] = str(last_scan_ts)
    if marker_extra:
        try:
            save_artist_scan_checkpoint(
                "__marker__",
                checkpoint_path=_SYNC_CHECKPOINT,
                extra=marker_extra,
            )
        except Exception as exc:
            logger.debug("Could not persist library sync marker", error=str(exc))

    duration = time.time() - started_at
    logger.info("Bulk sync complete", duration_seconds=round(duration, 2))
    
    return {
        "success": True,
        "marker": marker,
        "artists_processed": artists_processed,
        "artists_failed": artists_failed,
        "tracks_attempted": tracks_attempted,
        "duration_seconds": duration,
    }


def sync_artist_with_diff(artist_name: str, artist_id: str) -> dict[str, Any]:
    result = scan_artist_to_db(artist_name, artist_id, verbose=False, force=False, diff_mode=True)
    if not isinstance(result, dict):
        return {
            "skipped_mtime": False,
            "skipped_album_diff": False,
            "changed": bool(result is None),
            "changed_albums": 0,
            "tracks": [],
        }
    return {
        "skipped_mtime": bool(result.get("skipped_mtime", False)),
        "skipped_album_diff": bool(result.get("skipped_album_diff", False)),
        "changed": bool(result.get("changed", False)),
        "changed_albums": int(result.get("changed_albums", 0) or 0),
        "tracks": result.get("tracks", []) or [],
    }


def run_bulk_commit(tracks: list[dict[str, Any]]) -> int:
    if not tracks:
        return 0
    try:
        bulk_upsert_navidrome_tracks(tracks=tracks)
        return len(tracks)
    except Exception as exc:
        logger.error("Bulk commit failed", error=str(exc))
        return 0


def get_candidate_artists(client: NavidromeClient, since_ts: Any = None) -> dict[str, str]:
    """Resolve the set of artists to diff-sync."""
    if since_ts is not None:
        try:
            delta_index = build_delta_artist_index(client, since_ts)
            delta_resolved = {
                str(name): info["id"]
                for name, info in delta_index.items()
                if isinstance(info, dict) and info.get("id")
            }
            if delta_resolved:
                logger.info("Library sync: delta artists changed", count=len(delta_resolved), since=since_ts)
                return delta_resolved
        except Exception as exc:
            logger.debug("Library sync delta resolution failed", error=str(exc))

    db_artists: dict[str, str | None] = {}
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            rows = session.execute(
                _text("""
                    SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS aa
                    FROM tracks
                    WHERE (album_artist IS NOT NULL AND TRIM(album_artist) <> '')
                       OR (artist IS NOT NULL AND TRIM(artist) <> '')
                """)
            ).fetchall() or []
        for row in rows:
            name = row[0]
            if name:
                db_artists[str(name)] = None
    except Exception as exc:
        logger.debug("Could not query DB artists", error=str(exc))

    if db_artists:
        try:
            index = client.build_artist_index() or {}
            case_insensitive_index = {
                str(name).lower(): info.get("id")
                for name, info in index.items()
                if isinstance(info, dict) and info.get("id")
            }
            for name in list(db_artists.keys()):
                info = index.get(name)
                db_artists[name] = info.get("id") if isinstance(info, dict) and info.get("id") else case_insensitive_index.get(name.lower())
            resolved = {name: artist_id for name, artist_id in db_artists.items() if artist_id}
            if resolved:
                return resolved
        except Exception as exc:
            logger.debug("Failed to resolve DB artist IDs", error=str(exc))

    try:
        index = client.build_artist_index() or {}
        return {str(name): info["id"] for name, info in index.items() if isinstance(info, dict) and info.get("id")}
    except Exception:
        return {}
