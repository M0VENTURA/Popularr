"""Cross-process state for the Soulseek search breaker.

WHY THIS EXISTS
---------------
``download_pipeline_service`` already knows when the search service cannot be
reached: it trips one shared breaker and parks every queue row behind it. That
breaker lives in PROCESS MEMORY, and the queue is driven by whichever process
gets there first — the standalone worker (``popularr-queue-worker.service``) or
the scheduler inside the web process. The dashboard runs in the WEB process, so
it could not see an outage the worker had detected: the log filled with
"search service unavailable" while every page looked healthy.

The state is therefore written to a small JSON file in the runtime-state
directory, the same pattern as ``queue_processor_health.json``. Reads are
tolerant of every failure mode:

* no file            → no known problem (fresh install, or never tripped);
* window in the past → probing again, so NOT an active outage;
* unreadable file    → no known problem, logged at debug.

A stale file must never produce a permanent red banner: the breaker's own
window decides, not the file's existence.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

STATE_FILE_NAME = "search_service_state.json"

#: Environment override for the whole path, mirroring the queue heartbeat.
_STATE_FILE_ENV = "SEARCH_SERVICE_STATE_FILE"


def state_file_path() -> str:
    override = str(os.environ.get(_STATE_FILE_ENV) or "").strip()
    if override:
        return override
    from helpers.config_helpers import get_state_directory

    return os.path.join(get_state_directory(), STATE_FILE_NAME)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _write(payload: dict[str, Any]) -> None:
    path = state_file_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_path, path)
    except Exception as exc:
        # Never let diagnostics take the queue down.
        logger.debug("Search service state not written", path=path, error=str(exc))


def mark_search_unavailable(
    reason: str,
    *,
    pause_seconds: int,
    tier: int,
) -> None:
    """Record an OPEN breaker window ending at ``now + pause_seconds``."""
    now = time.time()
    _write({
        "available": False,
        "reason": str(reason or "search service unavailable"),
        "tier": int(tier or 0),
        "paused_until": now + float(pause_seconds or 0),
        "paused_until_iso": _iso(now + float(pause_seconds or 0)),
        "since_iso": _iso(now),
        "updated_at": _iso(now),
        "pid": os.getpid(),
    })


def mark_search_available() -> None:
    """Record that a search succeeded, closing the breaker.

    Only writes when the stored state says UNAVAILABLE, so a healthy queue does
    not touch the disk after every successful search.
    """
    current = _read_raw()
    if not current or current.get("available") is True:
        return
    now = time.time()
    _write({
        "available": True,
        "reason": "",
        "tier": 0,
        "paused_until": 0,
        "paused_until_iso": "",
        "since_iso": "",
        "updated_at": _iso(now),
        "pid": os.getpid(),
    })


def _read_raw() -> dict[str, Any] | None:
    path = state_file_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("Search service state not readable", path=path, error=str(exc))
        return None
    return payload if isinstance(payload, dict) else None


def read_search_service_state() -> dict[str, Any]:
    """Current breaker state, safe for an API response.

    ``available`` is True whenever no window is open — including when the last
    recorded outage has expired and a probe is due. ``known`` is False when no
    state file exists at all.
    """
    payload = _read_raw()
    if not payload:
        return {
            "known": False,
            "available": True,
            "reason": "",
            "tier": 0,
            "paused_until_iso": "",
            "retry_in_seconds": 0,
            "updated_at": "",
        }

    try:
        paused_until = float(payload.get("paused_until") or 0)
    except (TypeError, ValueError):
        paused_until = 0.0
    remaining = max(0, int(round(paused_until - time.time())))
    explicitly_available = payload.get("available") is True
    return {
        "known": True,
        "available": bool(explicitly_available or remaining <= 0),
        "reason": str(payload.get("reason") or ""),
        "tier": int(payload.get("tier") or 0),
        "paused_until_iso": str(payload.get("paused_until_iso") or ""),
        "retry_in_seconds": remaining,
        "updated_at": str(payload.get("updated_at") or ""),
    }
