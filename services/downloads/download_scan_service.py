"""Download filesystem scanning services."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.queue_admin import (
    find_existing_discovered_file,
    insert_discovered_file,
)
from helpers.config_helpers import get_supported_audio_formats
from helpers.metadata_reader import read_mp3_metadata
from services.infrastructure.filesystem_service import (
    _original_archive_subfolder_name,
    resolve_downloads_dir,
    resolve_original_archive_dir,
)

logger = structlog.get_logger(__name__)

SUPPORTED_EXTENSIONS = get_supported_audio_formats()


@dataclass(slots=True)
class DiscoveredFile:
    filename: str
    full_path: str
    rel_path: str
    extension: str
    folder: str


def resolve_torrents_dir() -> str:
    root = os.environ.get("DOWNLOADS_DIR", "/downloads")
    torrents_dir = os.path.join(root, "torrents")
    return torrents_dir if os.path.isdir(torrents_dir) else resolve_downloads_dir()


def resolve_downloads_monitor_dir(_config: object | None = None) -> str:
    return resolve_downloads_dir()


_last_discovered_count: int | None = None


def discover_audio_files() -> list[DiscoveredFile]:
    """Filesystem-only scan for audio files across the configured downloads root."""
    downloads_dir = resolve_downloads_dir(prefer_music_subfolder=False)
    if not os.path.isdir(downloads_dir):
        logger.warning("Downloads directory not found", downloads_dir=downloads_dir)
        return []

    archive_dir = resolve_original_archive_dir()
    _archive_name = _original_archive_subfolder_name()

    discovered: list[DiscoveredFile] = []
    for root, dirs, files in os.walk(downloads_dir):
        dirs[:] = [
            d for d in dirs
            if d != _archive_name
            and os.path.normpath(os.path.join(root, d)) != archive_dir
        ]
        for filename in sorted(files):
            _, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext in SUPPORTED_EXTENSIONS:
                full_path = os.path.join(root, filename)
                discovered.append(DiscoveredFile(
                    filename=filename,
                    full_path=full_path,
                    rel_path=os.path.relpath(full_path, downloads_dir),
                    extension=ext,
                    folder=root,
                ))

    global _last_discovered_count
    if _last_discovered_count != len(discovered):
        logger.info("Discovered audio files", count=len(discovered))
        _last_discovered_count = len(discovered)
    return discovered


def scan_downloads(_metadata_reader: Any = None) -> dict[str, object]:
    return {"success": True, "files": [file.full_path for file in discover_audio_files()]}


def get_scan_progress() -> dict[str, object]:
    return {"success": True, "progress": 0, "status": "idle"}


def verify_moved_files(_minutes_old: int = 30) -> dict[str, object]:
    return {"success": True, "verified": 0}


def check_completed_downloads() -> dict[str, object]:
    from services.downloads.download_completion_service import check_completed_downloads as _check
    return _check()


def _extract_discovered_metadata(file_path: str, filename: str) -> dict[str, str | None]:
    meta: dict = {}
    try:
        meta = read_mp3_metadata(file_path) or {}
    except Exception:
        pass

    artist = str(meta.get("artist") or "").strip()
    album = str(meta.get("album") or "").strip()
    title = str(meta.get("title") or "").strip()
    year_raw = str(meta.get("year") or meta.get("date") or "").strip()
    track_raw = str(meta.get("track_number") or "").strip()

    stem = os.path.splitext(filename or os.path.basename(file_path or ""))[0]
    stem = re.sub(r"[\s_\-]?\d{10,}\s*$", "", stem).strip()
    folder = os.path.basename(os.path.dirname(file_path or "") or "")

    parts = [p.strip() for p in re.split(r"\s*[-–]\s*", stem) if p.strip()]
    if (not title or not artist) and parts:
        if not artist and len(parts) >= 2:
            artist = parts[0]
        if not album:
            if len(parts) >= 4:
                album = parts[1]
            elif len(parts) == 3 and re.match(r"^\d{1,3}$", parts[2]):
                album = parts[1]
        if not title:
            _rest = parts[1:] if len(parts) >= 2 else parts
            if _rest and re.match(r"^\d{1,3}$", _rest[0]):
                _rest = _rest[1:]
            title = " - ".join(_rest) if _rest else parts[-1]
            title = re.sub(r"\s*[-–]\s*\d{1,3}\s*$", "", title).strip()
        if not track_raw and parts and re.match(r"^\d{1,3}$", parts[0]):
            track_raw = parts[0]

    if not artist:
        folder_lower = folder.lower()
        if folder and folder_lower not in ("downloads", "music", "completed", "inbox"):
            artist = folder

    return {
        "artist": artist or "Unidentified Artist",
        "album": album or "Unidentified Release",
        "title": title or stem or "Unidentified Track",
        "track_number": track_raw or None,
        "year": year_raw[:4] if year_raw else None,
    }


def _duplicate_cleanup_config(key: str, default: bool = True) -> bool:
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("features", {}).get("downloads_duplicate_cleanup") or {}
        return bool(cfg.get(key, default))
    except Exception:
        return default


def _quality_filter_config() -> dict[str, Any]:
    try:
        from helpers.config_helpers import get_config
        qf = (get_config() or {}).get("downloads", {}).get("quality_filter") or {}
    except Exception:
        qf = {}

    priorities = []
    for p in qf.get("priorities") or []:
        if isinstance(p, dict) and str(p.get("format") or "").strip():
            priorities.append(p)

    return {
        "enabled": bool(qf.get("enabled")),
        "reject_others": bool(qf.get("reject_others", True)),
        "bitrate_tolerance": int(qf.get("bitrate_tolerance") or 5),
        "priorities": priorities,
    }


def _matches_quality_filter(file_path: str) -> tuple[bool, str | None]:
    cfg = _quality_filter_config()
    if not cfg["enabled"] or not cfg["priorities"]:
        return True, None

    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    bitrate: int | None = None
    try:
        meta = read_mp3_metadata(file_path) or {}
        if meta.get("bitrate"):
            bitrate = int(round(float(meta["bitrate"]) / 1000))
    except Exception:
        bitrate = None

    tolerance = cfg["bitrate_tolerance"]
    for priority in cfg["priorities"]:
        if ext != str(priority.get("format") or "").lower():
            continue
        want = priority.get("bitrate_kbps")
        if want in (None, ""):
            return True, None
        try:
            want = int(want)
        except (TypeError, ValueError):
            return True, None
        if bitrate is None or abs(bitrate - want) <= tolerance:
            return True, None

    return False, f"{ext or 'unknown'} @ {bitrate or '?'}kbps doesn't match quality priorities"


def _queue_has_active_match(meta: dict[str, Any]) -> bool:
    artist = str(meta.get("artist") or "").strip()
    title = str(meta.get("title") or "").strip()

    if not artist or not title or artist.lower().startswith("unidentified"):
        return False

    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT id FROM download_queue
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND LOWER(title) = LOWER(:title)
                      AND status IN ('queued', 'searching', 'downloading')
                    LIMIT 1
                """),
                {"artist": artist, "title": title},
            ).fetchone()
            return row is not None
    except Exception:
        return False


def enqueue_discovered_files(files: list[DiscoveredFile]) -> dict[str, int]:
    """Dedupe discovered files against the queue and insert the new ones."""
    queued = 0
    already_in_queue = 0
    quality_skipped = 0
    quality_filter = _quality_filter_config()

    for f in files:
        existing = find_existing_discovered_file(
            file_path=f.full_path,
            filename=f.filename,
            rel_path=f.rel_path,
        )
        if existing:
            already_in_queue += 1
            continue

        matches_qf, qf_reason = _matches_quality_filter(f.full_path)
        if not matches_qf:
            if quality_filter["reject_others"]:
                quality_skipped += 1
                logger.info("Skipped file due to quality filter", file_path=f.full_path, reason=qf_reason)
                continue
            logger.debug("Non-priority file kept", file_path=f.full_path, reason=qf_reason)

        meta = _extract_discovered_metadata(f.full_path, f.filename)
        if _queue_has_active_match(meta):
            already_in_queue += 1
            if _duplicate_cleanup_config("delete_duplicate_files", True):
                try:
                    from services.downloads.duplicate_pruning_service import prune_inferior_duplicate
                    prune_inferior_duplicate(meta, f.full_path)
                except Exception as _exc:
                    logger.debug("Duplicate prune skipped", error=str(_exc))
            continue

        # The three identity values the insert uses, computed ONCE so this
        # pre-check and the insert can never disagree about who the track is.
        artist = str(meta["artist"] or "Unidentified Artist")
        title = str(meta["title"] or f.filename)
        album = str(meta["album"] or "Unidentified Release")

        # The insert dedupes on (artist, title) and logs at INFO when it hits.
        # The two checks above look at the FILE, so a file whose TRACK is
        # already queued — under a path neither of them recognised — fell
        # through to the insert every single scan and re-logged
        # "Duplicate skipped: already in queue" for the same row, forever.
        # Ask the identical question first and count it instead.
        from db.repositories.queue import find_blocking_queue_item
        if find_blocking_queue_item(artist=artist, title=title, source="discovered"):
            already_in_queue += 1
            continue

        # ✅ FIX: import_group set to None so files don't all bundle together
        insert_discovered_file(
            artist=artist,
            title=title,
            album=album,
            album_artist=None,
            track_number=meta.get("track_number"),
            disc_number=None,
            year=meta.get("year"),
            duration=None,
            file_path=f.full_path,
            filename=f.filename,
            import_group=None,  
        )
        queued += 1

    return {"queued": queued, "already_in_queue": already_in_queue, "quality_skipped": quality_skipped}


def discover_files() -> dict[str, Any]:
    files = discover_audio_files()
    file_paths = [f.full_path for f in files]
    total = len(file_paths)

    enqueued = enqueue_discovered_files(files)
    queued = enqueued["queued"]
    already_in_queue = enqueued["already_in_queue"]
    quality_skipped = enqueued.get("quality_skipped", 0)

    if _duplicate_cleanup_config("prune_empty_folders", True):
        try:
            from services.infrastructure.fs_manager import FileSystemManager
            downloads_root = resolve_downloads_dir(prefer_music_subfolder=False)
            music_root = os.environ.get("MUSIC_FOLDER", "/music")
            if downloads_root:
                FileSystemManager(downloads_root, music_root).cleanup_empty_dirs(Path(downloads_root))
        except Exception as _exc:
            logger.debug("Empty-folder prune skipped", error=str(_exc))

    already_in_library = 0
    if files:
        try:
            with db_session() as session:
                for f in files:
                    row = session.execute(
                        text("SELECT COUNT(*) FROM tracks WHERE file_path = :path"),
                        {"path": f.full_path},
                    ).fetchone()
                    
                    mapping = getattr(row, "_mapping", None) if row else None
                    count = int(mapping.get("", list(row)[0] if row else 0) if mapping else (row[0] if row else 0))
                    if count > 0:
                        already_in_library += 1
        except Exception as exc:
            logger.debug("Library check failed", error=str(exc))

    return {
        "success": True,
        "stats": {
            "scanned": total,
            "queued": queued,
            "already_in_queue": already_in_queue,
            "already_in_library": already_in_library,
            "quality_skipped": quality_skipped,
            "errors": [],
        },
        "files": file_paths,
    }
