"""Download organisation service (high-level).

Orchestrates the complete file organisation flow:
- Sanitises filenames for filesystem safety.
- Resolves target paths from configurable naming format.
- Delegates file moves to ``download_organize_helpers``.

Path format is configurable via ``naming_format`` in ``config.yaml``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import structlog

from helpers.config_helpers import get_config
from helpers.normalization_service import sanitize_path
from services.downloads.download_organize_helpers import move_track_to_library
from services.infrastructure.base import get_infra

logger = structlog.get_logger(__name__)

_sanitize = lambda value: sanitize_path(value).lower()


def _get_path_format() -> str:
    return get_config().get("naming_format", "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}")


def build_target_path(metadata: dict[str, Any], ext: str, music_root: Path) -> Path:
    """Generates the target path based on metadata rules."""
    vars_map = {
        "track_number": str(metadata.get("track_number", "00")).zfill(2),
        "artist": _sanitize(metadata.get("artist", "Unknown")),
        "album_artist": _sanitize(metadata.get("album_artist") or metadata.get("artist", "Unknown")),
        "title": _sanitize(metadata.get("title", "Unknown")),
        "album": _sanitize(metadata.get("album", "Unknown")),
        "year": str(metadata.get("year", "0000"))[:4],
    }

    relative_path = _get_path_format().format(**vars_map)
    relative_path = relative_path.replace("\\", "/").strip("/")

    filename = f"{vars_map['track_number']}. {vars_map['artist']} - {vars_map['title']}{ext}"
    return music_root / relative_path / filename


def organize_track(track_metadata: dict[str, Any] | int, payload: dict[str, Any] | None = None) -> Dict[str, Any]:
    """The orchestrator for moving a track into the library."""
    queue_item = None
    if isinstance(track_metadata, int):
        from db.repositories.queue import get_queue_item

        queue_item = get_queue_item(track_metadata)
        if not queue_item:
            return {"success": False, "error": "Queue item not found"}

        payload = dict(payload or {})
        track_metadata = {
            "file_path": payload.get("file_path") or queue_item.get("file_path"),
            "artist": payload.get("artist") or queue_item.get("artist"),
            "title": payload.get("title") or queue_item.get("title"),
            "track_number": payload.get("track_number") or queue_item.get("track_number"),
            "album_artist": payload.get("album_artist") or queue_item.get("album_artist"),
            "album": payload.get("album") or queue_item.get("album"),
            "year": payload.get("year") or queue_item.get("year"),
        }

    if not track_metadata.get("file_path"):
        return {"success": False, "error": "Missing file_path"}

    src = Path(track_metadata["file_path"])
    cfg = get_config()
    
    # --- STRICT BOUNDARY GUARD ---
    downloads_folder = Path(cfg.get("downloads", {}).get("folder", "/downloads")).resolve()
    src_resolved = src.resolve()
    
    try:
        is_in_downloads = src_resolved.is_relative_to(downloads_folder)
    except AttributeError:
        # Fallback for Python < 3.9
        is_in_downloads = str(src_resolved).startswith(str(downloads_folder))
        
    if not is_in_downloads:
        logger.warning(
            "Rejected auto-import: file is not in the configured downloads folder", 
            src=str(src), 
            downloads_folder=str(downloads_folder)
        )
        return {"success": False, "error": f"File originates outside the downloads folder ({downloads_folder})"}

    infra = get_infra()
    music_root_resolved = infra.fs.music_root.resolve()
    
    try:
        is_in_library = src_resolved.is_relative_to(music_root_resolved)
    except AttributeError:
        is_in_library = str(src_resolved).startswith(str(music_root_resolved))
        
    if is_in_library:
        logger.warning("Rejected auto-import: file is already inside the music library", src=str(src))
        return {"success": False, "error": "File is already inside the music library"}
    # -----------------------------

    if queue_item:
        try:
            _apply_stored_metadata(queue_item, track_metadata["file_path"])
        except Exception:
            pass

    target = build_target_path(
        track_metadata,
        src.suffix.lower(),
        infra.fs.music_root,
    )

    return infra.fs.move_to_library(
        source_path=str(src),
        target=target,
        year=track_metadata.get("year"),
    )


def _apply_stored_metadata(queue_item: dict[str, Any], file_path: str) -> None:
    """Best-effort write of the queue item's stored metadata to a file."""
    if not queue_item or not file_path:
        return
    meta: dict[str, Any] = {
        "title": queue_item.get("title"),
        "artist": queue_item.get("artist"),
        "album": queue_item.get("album"),
        "album_artist": queue_item.get("album_artist") or queue_item.get("artist"),
        "year": queue_item.get("year"),
        "track_number": queue_item.get("track_number"),
        "disc_number": queue_item.get("disc_number"),
    }
    meta = {k: v for k, v in meta.items() if v not in (None, "")}
    if queue_item.get("recording_mbid"):
        meta["recording_mbid"] = queue_item["recording_mbid"]
    if queue_item.get("release_mbid") or queue_item.get("release_id"):
        meta["release_mbid"] = queue_item.get("release_mbid") or queue_item.get("release_id")
    if not meta:
        return
        
    from services.metadata.tag_file_service import update_file_metadata
    update_file_metadata(file_path, meta)


def rename_and_move_file(file_path: str, metadata: dict[str, Any]) -> Dict[str, Any]:
    """Compatibility wrapper used by the download processing pipeline."""
    src = Path(file_path)
    cfg = get_config()
    
    # --- STRICT BOUNDARY GUARD ---
    downloads_folder = Path(cfg.get("downloads", {}).get("folder", "/downloads")).resolve()
    src_resolved = src.resolve()
    
    try:
        is_in_downloads = src_resolved.is_relative_to(downloads_folder)
    except AttributeError:
        is_in_downloads = str(src_resolved).startswith(str(downloads_folder))
        
    if not is_in_downloads:
        logger.warning("Rejected move: file is not in the configured downloads folder", file_path=file_path)
        return {"success": False, "error": "File originates outside the downloads folder"}
    # -----------------------------

    track = {
        "file_path": file_path,
        "artist": metadata.get("artist"),
        "title": metadata.get("title"),
        "track_number": metadata.get("track_number"),
    }
    release_metadata = {
        "album_artist": metadata.get("album_artist"),
        "album": metadata.get("album"),
        "year": metadata.get("year"),
    }
    return move_track_to_library(track, release_metadata, os.environ.get("MUSIC_ROOT", "/music"))


def _build_target_path(*args: Any, **kwargs: Any) -> Path:
    return build_target_path(*args, **kwargs)


def organize_folder(folder_path: str, payload: dict[str, Any] | None = None) -> Dict[str, Any]:
    return {"success": True, "folder_path": folder_path, "processed": 0, "results": []}


def organize_single_file(file_path: str, payload: dict[str, Any] | None = None) -> Dict[str, Any]:
    return organize_track({"file_path": file_path, **(payload or {})})


def merge_folders(payload: dict[str, Any] | None = None) -> Dict[str, Any]:
    return {"success": True, "merged": 0, "payload": payload or {}}
