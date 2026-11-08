"""Download cleanup engine.

Handles post-download file cleanup:
- Removing empty parent directories.
- Cleaning up orphaned download artifacts.
- Ensuring downloaded files are under monitored paths.

Delegates filesystem operations to ``services.infrastructure.filesystem_service``.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional, TypedDict

import structlog

from helpers.normalization_service import normalize_match_text
from services.infrastructure.filesystem_cache_service import get_download_files
from services.infrastructure.filesystem_service import (
    cleanup_empty_parents,
    is_path_under_directory,
    resolve_downloads_dir,
)

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------------------
# TYPES
# ------------------------------------------------------------------------------

class QueueItem(TypedDict, total=False):
    id: str
    artist: str
    title: str


# ------------------------------------------------------------------------------
# CONSTANTS
# ------------------------------------------------------------------------------

_CLEANUP_SIBLING_MIN_AGE_SECONDS: int = 30

_CLEANUP_DISCOVERY_MIN_INTERVAL_SECONDS: int = int(
    os.getenv("DISCOVERY_INTERVAL_SECONDS", "300")
)

_discovery_last_run_at: float = 0.0


# ==============================================================================
# PUBLIC BUSINESS LOGIC
# ==============================================================================

def cleanup_stale_downloads() -> dict[str, Any]:
    """Clean up stale/orphaned download entries.

    ⚠️ STAGE TIMINGS are returned in ``timings_ms`` and logged at INFO when a
    pass takes >= 5 s. This hook is the other one that hit the 120 s
    maintenance timeout: ``discover_files`` rescans and re-probes every audio
    file, and the reason was previously invisible.
    """
    global _discovery_last_run_at

    started = time.monotonic()
    timings: dict[str, float] = {}
    now = time.time()
    result: Any = []
    scanned = 0
    queued = 0
    already_in_queue = 0

    if now - _discovery_last_run_at >= _CLEANUP_DISCOVERY_MIN_INTERVAL_SECONDS:
        _discovery_last_run_at = now
        _t = time.monotonic()
        from services.downloads.download_scan_service import discover_files
        result = discover_files()
        timings["discovery_ms"] = round((time.monotonic() - _t) * 1000.0, 1)
        if isinstance(result, dict):
            _stats = result.get("stats") or {}
            scanned = int(_stats.get("scanned") or 0)
            queued = int(_stats.get("queued") or 0)
            already_in_queue = int(_stats.get("already_in_queue") or 0)
        elif isinstance(result, list):
            scanned = len(result)
    else:
        timings["discovery_ms"] = 0.0

    auto_deleted = 0
    _t = time.monotonic()
    try:
        from services.downloads.download_folder_service import auto_delete_imported_folders
        auto_deleted = auto_delete_imported_folders()
    except Exception as exc:
        logger.debug("Auto-delete imported folders failed", error=str(exc))
    timings["auto_delete_ms"] = round((time.monotonic() - _t) * 1000.0, 1)
    timings["total_ms"] = round((time.monotonic() - started) * 1000.0, 1)

    if timings["total_ms"] >= 5000:
        logger.info(
            "cleanup_stale_downloads stage timings",
            scanned=scanned,
            queued=queued,
            **timings,
        )

    return {
        "scanned": scanned,
        "folders_deleted": auto_deleted,
        "queued": queued,
        "already_in_queue": already_in_queue,
        "timings_ms": timings,
    }


def cleanup_sibling_downloads(
    queue_item: QueueItem,
    keep_path: str | None = None,
) -> None:
    """Remove duplicate downloads for a queue item."""
    artist: str = queue_item.get("artist") or ""
    title: str = queue_item.get("title") or ""
    item_id: str = queue_item.get("id") or "unknown"

    artist_norm: str = normalize_match_text(artist)
    title_norm: str = normalize_match_text(title)

    if not artist_norm or not title_norm:
        return

    downloads_root: str = resolve_downloads_dir()
    if not os.path.isdir(downloads_root):
        return

    keep_abs: str | None = os.path.abspath(keep_path) if keep_path else None

    artist_lower: str = artist.lower()
    title_lower: str = title.lower()

    for fpath in get_download_files():
        if keep_abs and os.path.abspath(fpath) == keep_abs:
            continue

        try:
            age_seconds: float = time.time() - os.path.getmtime(fpath)
            if age_seconds < _CLEANUP_SIBLING_MIN_AGE_SECONDS:
                continue
        except OSError:
            continue

        basename_lower: str = os.path.basename(fpath).lower()
        if artist_lower not in basename_lower and title_lower not in basename_lower:
            continue

        rel_path: str = os.path.relpath(fpath, downloads_root).lower()
        if artist_norm not in rel_path or title_norm not in rel_path:
            continue

        _safe_delete(
            file_path=fpath,
            item_id=item_id,
            reason="duplicate",
            root=downloads_root,
        )


def delete_mismatched_download(
    file_path: str,
    item_id: str,
    reason: str,
) -> bool:
    """Delete a mismatched downloaded file safely."""
    downloads_root: str = resolve_downloads_dir()
    return _safe_delete(
        file_path=file_path,
        item_id=item_id,
        reason=reason,
        root=downloads_root,
    )


# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================

def _safe_delete(
    file_path: str,
    item_id: str,
    reason: str,
    root: str,
) -> bool:
    """Central deletion logic with safety rails."""
    if not file_path:
        return False

    if not is_path_under_directory(file_path, root):
        logger.warning(
            "Refusing delete outside downloads directory",
            queue_id=item_id,
            path=file_path,
        )
        return False

    try:
        if not os.path.isfile(file_path):
            logger.debug(
                "File already gone during deletion check",
                queue_id=item_id,
                path=file_path,
            )
            return False

        os.remove(file_path)

        logger.info(
            "Safely deleted download file",
            queue_id=item_id,
            reason=reason,
            path=file_path,
        )

        cleanup_empty_parents(file_path, root)
        return True

    except OSError as exc:
        logger.warning(
            "Failed deleting file",
            queue_id=item_id,
            path=file_path,
            error=str(exc),
        )
        return False
