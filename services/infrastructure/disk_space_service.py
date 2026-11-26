"""Free-space check for the volumes Popularr writes to.

WHY THIS EXISTS
---------------
Nothing in the app watched disk space. A full music volume fails in the worst
possible way: downloads complete, the import copies the file, the tag write
truncates, and the track ends up half-written — each failure looking like an
unrelated bug in a different subsystem. The operator only finds out afterwards.

Two roots matter:

* the MUSIC library (``music_directory``) — where imported files and re-tagged
  audio live, and
* the DOWNLOADS directory — where each downloaded file lands first.

Both are read here and reported as one small dict. Every failure is tolerated:
a missing directory, an unreadable path or a platform without ``statvfs``
simply reports ``known: False`` for that root, because a diagnostic must never
become a new failure mode.
"""

from __future__ import annotations

import shutil
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Below this percentage free, the dashboard warns. 5% is the practical floor
#: for "a large album import will still fit"; below 2% a single FLAC set can
#: fail mid-write.
WARN_PERCENT = 5.0
CRITICAL_PERCENT = 2.0


def _resolve(path: str) -> str:
    """The directory to stat: *path* itself when it exists, else its nearest
    existing parent (the target may not have been created yet)."""
    import os

    candidate = str(path or "").strip()
    if not candidate:
        return ""
    if os.path.isdir(candidate):
        return candidate
    parent = os.path.dirname(candidate.rstrip("/\\"))
    while parent and parent != os.path.dirname(parent):
        if os.path.isdir(parent):
            return parent
        parent = os.path.dirname(parent)
    return ""


def _space_for(label: str, path: str) -> dict[str, Any]:
    resolved = _resolve(path)
    if not resolved:
        return {
            "label": label,
            "path": str(path or ""),
            "known": False,
            "free_bytes": 0,
            "total_bytes": 0,
            "free_percent": None,
        }
    try:
        usage = shutil.disk_usage(resolved)
    except Exception as exc:
        logger.debug("Disk usage unavailable", path=resolved, error=str(exc))
        return {
            "label": label,
            "path": str(path or ""),
            "known": False,
            "free_bytes": 0,
            "total_bytes": 0,
            "free_percent": None,
        }

    total = int(usage.total or 0)
    free = int(usage.free or 0)
    percent = (free / total * 100.0) if total else None
    return {
        "label": label,
        "path": str(path or ""),
        "resolved": resolved,
        "known": True,
        "free_bytes": free,
        "total_bytes": total,
        "free_percent": round(percent, 1) if percent is not None else None,
    }


def read_disk_space() -> dict[str, Any]:
    """Free space for the music and downloads volumes.

    ``worst_percent`` is the lower of the two roots that could be measured, so
    one caller-side comparison covers both. ``level`` is
    ``"ok" | "warn" | "critical" | "unknown"``.
    """
    volumes: list[dict[str, Any]] = []
    try:
        from helpers.config_helpers import get_config

        cfg = get_config() or {}
    except Exception:
        cfg = {}

    music = ""
    try:
        from helpers.settings import get_settings

        music = str(getattr(get_settings(), "music_root", "") or "").strip()
    except Exception:
        music = ""
    if not music:
        import os

        music = str(os.environ.get("MUSIC_ROOT") or "").strip()
    if music:
        volumes.append(_space_for("Music library", music))

    try:
        from services.infrastructure.filesystem_service import resolve_downloads_dir

        downloads = str(resolve_downloads_dir() or "").strip()
    except Exception:
        downloads = ""
    if downloads:
        volumes.append(_space_for("Downloads", downloads))

    measured = [
        v["free_percent"] for v in volumes
        if v.get("known") and v.get("free_percent") is not None
    ]
    worst = min(measured) if measured else None

    if worst is None:
        level = "unknown"
    elif worst <= CRITICAL_PERCENT:
        level = "critical"
    elif worst <= WARN_PERCENT:
        level = "warn"
    else:
        level = "ok"

    return {
        "known": bool(measured),
        "level": level,
        "worst_percent": worst,
        "warn_percent": WARN_PERCENT,
        "critical_percent": CRITICAL_PERCENT,
        "volumes": volumes,
    }
