"""Cross-process "a human is waiting for the Soulseek search slot" signal.

WHY THIS EXISTS
---------------
slskd serves ONE search at a time. The queue worker holds that slot almost
continuously — it runs one search per track, 35-145 s each, and starts the next
cycle immediately when more rows are ready (``queue_worker._MAX_IMMEDIATE_CYCLES``).
A manual search therefore has to win a race it cannot win.

Reported: *"Search failed: Soulseek search slot did not free up. Try again
shortly."* — and, correctly, *"It used to wait until it freed up."*

Two things were wrong:

1. **The client gave up in 60 s** (``SLOT_MAX_RETRIES`` x ``SLOT_RETRY_INTERVAL_MS``)
   while a single queue search can run for 145 s. It was timing out by
   arithmetic, not by failure.
2. **Nothing told the worker to stand aside.** Even a patient client could wait
   through several consecutive queue searches, because the worker re-claims the
   slot the instant each one ends.

This module is the missing signal: the interactive path records that a person is
waiting, and the queue worker checks it before starting each search and yields.
The file lives in the runtime-state directory, the same pattern as
``search_service_state.json`` and ``queue_processor_health.json``, because the
queue usually runs in a SEPARATE process from the web UI.

Reads tolerate every failure mode: a missing, unreadable or stale file simply
means "nobody is waiting", so a crashed browser can never stall the queue.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

STATE_FILE_NAME = "manual_search_priority.json"

#: Environment override for the whole path, mirroring the other state files.
_STATE_FILE_ENV = "MANUAL_SEARCH_PRIORITY_FILE"

#: How long a request is honoured. A closed tab or a user who walked away must
#: not park the queue indefinitely, and this is comfortably longer than one
#: queue search plus the interactive one that follows it.
PRIORITY_WINDOW_SECONDS = 90.0


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
        # Never let a priority hint take the queue (or the search) down.
        logger.debug("Manual-search priority not written", path=path, error=str(exc))


def claim_priority(query: str = "") -> None:
    """Record that a person is waiting for the search slot right now."""
    now = time.time()
    _write({
        "claimed_at": now,
        "claimed_at_iso": _iso(now),
        "expires_at": now + PRIORITY_WINDOW_SECONDS,
        "query": str(query or "")[:200],
        "pid": os.getpid(),
    })


def release_priority() -> None:
    """Clear the claim once the interactive search has the slot (or gave up)."""
    try:
        os.remove(state_file_path())
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.debug("Manual-search priority not cleared", error=str(exc))


def priority_remaining_seconds() -> float:
    """Seconds left on an active claim, or 0.0 when nobody is waiting."""
    path = state_file_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return 0.0
    except Exception as exc:
        logger.debug("Manual-search priority not readable", path=path, error=str(exc))
        return 0.0

    if not isinstance(payload, dict):
        return 0.0
    try:
        expires_at = float(payload.get("expires_at") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, expires_at - time.time())


def is_priority_claimed() -> bool:
    """True when an interactive search is waiting or about to start."""
    return priority_remaining_seconds() > 0.0
