"""Filesystem Service

This module provides filesystem operations and utilities for the application.

Key Responsibilities:
    - Audio file format validation and support
    - Path resolution with conversion rules (e.g., FLAC→MP3)
    - Directory safety validation (path traversal prevention)
    - Empty directory cleanup after file moves
    - File metadata manipulation (modification times)
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from helpers.config_helpers import conversion_target, get_config, get_supported_audio_formats

SUPPORTED_AUDIO_FORMATS = get_supported_audio_formats()
logger = structlog.get_logger(__name__)


# ==============================================================================
# PATH RESOLUTION (POLICY)
# ==============================================================================

def get_import_destination_path(
    source_path: str,
    dest_path: str,
    settings: dict[str, Any] | None = None,
) -> str:
    """Return final import path after applying conversion rules."""
    if settings is None:
        cfg = get_config() or {}
        settings = cfg.get("download_conversion", {})

    source_ext = os.path.splitext(source_path or "")[1].lower()
    # ``download_conversion.mode`` and ``downloads.conversion.mode`` share the
    # same vocabulary, so both are resolved through the one mapping — an inline
    # comparison against a single mode here would send a FLAC to the wrong
    # extension as soon as another mode exists.
    target = conversion_target(settings.get("mode"))
    should_convert = bool(target) and source_ext == ".flac"

    if should_convert:
        dest_root, _ = os.path.splitext(dest_path)
        return f"{dest_root}.{target}"

    return dest_path


# ==============================================================================
# SAFETY (PATH VALIDATION)
# ==============================================================================

def is_path_under_directory(path: str, root: str) -> bool:
    """Check if path is inside root directory using Pathlib safety."""
    if not path or not root:
        return False
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except Exception:
        return False


# ==============================================================================
# FILESYSTEM OPERATIONS (MECHANICS)
# ==============================================================================

def cleanup_empty_parents(start_path: str, root: str) -> None:
    """Remove empty parent directories up to root."""
    parent = Path(start_path).parent
    root_path = Path(root).resolve()

    while parent and parent.is_dir():
        if parent.resolve() == root_path:
            break

        try:
            if any(parent.iterdir()):
                break

            parent.rmdir()
            logger.debug("Cleaned up empty directory", path=str(parent))
            parent = parent.parent
        except OSError:
            break


def apply_release_year_mtime(file_path: str, year: Any, queue_id: int | None = None) -> None:
    """Set file modification time to Jan 1 of release year."""
    if not file_path or not year:
        return

    try:
        year_int = int(str(year).strip()[:4])
        if 1900 <= year_int <= 2100:
            ts = datetime(year_int, 1, 1).timestamp()
            os.utime(file_path, (ts, ts))
            logger.debug("Set mtime for file", queue_id=queue_id, year=year_int, path=file_path)
    except Exception as e:
        logger.debug("Failed to set mtime", queue_id=queue_id, year=year, error=str(e))


# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================

def _prefer_music_subfolder(path: str) -> str:
    if not path:
        return path

    normalized = os.path.normpath(path)
    if os.path.basename(normalized).lower() == "downloads":
        music_subdir = os.path.join(normalized, "Music")
        if os.path.isdir(music_subdir):
            return music_subdir

    return normalized


def create_monitoring_folder(artist: str, album: str, year: Any) -> Path:
    base_dir = Path(resolve_downloads_dir())

    year_text = str(year).strip() if year not in (None, "") else ""
    year_match = re.search(r"(19|20)\d{2}", year_text)
    folder_year = year_match.group(0) if year_match else "Unknown"

    folder_name = f"{folder_year} - {artist} - {album}".replace("/", "_").replace("\\", "_")[:200]
    folder_path = base_dir / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    logger.info("Created monitoring folder", path=str(folder_path))
    return folder_path


def ensure_music_directories(downloads_dir: str, music_dir: str) -> None:
    Path(downloads_dir).mkdir(parents=True, exist_ok=True)
    Path(music_dir).mkdir(parents=True, exist_ok=True)


def transfer_download_to_music(source_path: str, dest_path: str) -> dict[str, Any]:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    try:
        shutil.move(source_path, dest_path)
        return {"success": True, "target_path": dest_path}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _unique_path(root: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    path = os.path.join(root, filename)
    counter = 1

    while os.path.exists(path):
        path = os.path.join(root, f"{base}_{counter}{ext}")
        counter += 1

    return path


def resolve_original_archive_dir() -> str:
    """Absolute path of the FLAC conversion archive folder."""
    try:
        config = get_config() or {}
        conversion_cfg = (config.get("downloads") or {}).get("conversion") or {}
        subfolder = str(conversion_cfg.get("original_subfolder", "Original") or "Original").strip()
    except Exception:
        subfolder = "Original"
    root = resolve_downloads_dir(prefer_music_subfolder=False)
    return os.path.normpath(os.path.join(root, subfolder or "Original"))


def archive_dir_path() -> str:
    return resolve_original_archive_dir()


def _original_archive_subfolder_name() -> str:
    return os.path.basename(resolve_original_archive_dir())


def resolve_music_dir(config: dict[str, Any] | None = None) -> str:
    """Resolve music library root directory utilizing the central config."""
    cfg = config or get_config() or {}
    
    return (
        cfg.get("music", {}).get("root")
        or cfg.get("music_root")
        or cfg.get("navidrome", {}).get("music_folder")
        or "/music"
    )


def resolve_downloads_dir(prefer_music_subfolder: bool = True) -> str:
    """Single source of truth for download folder resolution."""
    def _resolve(value: str) -> str:
        normalized = os.path.normpath(value.strip())
        if not prefer_music_subfolder:
            return normalized
        return _prefer_music_subfolder(normalized)

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir:
        return _resolve(env_dir)

    try:
        config = get_config() or {}
        downloads_cfg = config.get("downloads") or {}
        
        monitor_folder = downloads_cfg.get("monitor_folder")
        if monitor_folder:
            return _resolve(monitor_folder)

        folder = downloads_cfg.get("folder")
        if folder:
            return _resolve(folder)
    except Exception:
        pass

    return "/downloads/Music"


def _get_files_in_folder(folder_path: str, max_depth: int = 3, max_files: int = 500) -> list[dict[str, Any]]:
    """List files in *folder_path*, recursing into subfolders."""
    files = []

    try:
        folder = Path(folder_path)
        if not folder.exists():
            return []

        archive_name = _original_archive_subfolder_name()
        archive_path_str = archive_dir_path()
        root_depth = len(folder.parts)

        for root, dirs, names in os.walk(folder):
            depth = len(Path(root).parts) - root_depth
            if depth >= max_depth:
                dirs[:] = []

            dirs[:] = [
                d for d in dirs
                if d != archive_name
                and os.path.normpath(os.path.join(root, d)) != archive_path_str
            ]
            for name in names:
                full = Path(root) / name
                try:
                    stat = full.stat()
                except OSError:
                    continue
                files.append({
                    "name": str(full.relative_to(folder)),
                    "size": stat.st_size,
                    "extension": full.suffix.lower(),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "is_audio": full.suffix.lower() in SUPPORTED_AUDIO_FORMATS,
                })
                if len(files) >= max_files:
                    return files

        files.sort(key=lambda x: x["modified"], reverse=True)
        return files

    except Exception as e:
        logger.error("Failed to list files in folder", path=folder_path, error=str(e), exc_info=True)
        return []


def get_folder_group_details(folder_path: str) -> dict[str, Any]:
    """Detailed folder inspection."""
    try:
        folder = Path(folder_path)
        if not folder.exists():
            return {"success": False, "error": "Folder not found"}

        files = _get_files_in_folder(folder_path)
        return {
            "success": True,
            "folder": folder_path,
            "name": folder.name,
            "file_count": len(files),
            "audio_files": len([f for f in files if f["is_audio"]]),
            "files": files,
        }
    except Exception as e:
        logger.error("Error inspecting folder details", path=folder_path, error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


def is_under_music_root(path_value: str, music_root: str) -> bool:
    return is_path_under_directory(path_value, music_root)


def sanitize_collection_segment(value: str) -> str:
    value = str(value or "").strip()
    invalid = '<>:"|?*\\'
    for ch in invalid:
        value = value.replace(ch, "_")
    return value.strip(". ").lower()


def is_valid_collection_location(path_value: Any, artist: str, album: str, album_artist: str, music_root: str) -> bool:
    if not path_value or not os.path.isfile(path_value):
        return False

    norm = os.path.normpath(str(path_value)).replace("\\", "/").rstrip("/")
    if norm.lower().startswith("__queued_for_download__"):
        return False

    if not is_under_music_root(norm, music_root):
        return False

    try:
        rel_path = os.path.relpath(norm, music_root).replace("\\", "/")
    except Exception:
        return False

    parts = [p for p in rel_path.split("/") if p and p not in (".", "..")]
    if len(parts) < 3:
        return False

    expected_artist = sanitize_collection_segment(album_artist or artist or "Unknown Artist")
    expected_album = sanitize_collection_segment(album or "Unknown Album")

    artist_dir = sanitize_collection_segment(parts[0])
    album_dir = sanitize_collection_segment(parts[-2])

    return artist_dir == expected_artist and (
        album_dir == expected_album or album_dir.endswith(f" - {expected_album}")
    )


def is_valid_source_music_path(path_value: Any, music_root: str) -> bool:
    if not path_value or not os.path.isfile(path_value):
        return False

    norm = os.path.normpath(str(path_value)).replace("\\", "/").rstrip("/")
    if norm.lower().startswith("__queued_for_download__"):
        return False

    return is_under_music_root(norm, music_root)
