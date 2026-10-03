"""Coalesced, one-shot Navidrome rescans after local metadata writes.

Why this exists
---------------
Popularr's album/track save paths write the corrected metadata into the
**audio file tags**.  Navidrome serves data from **its own database**, which
only refreshes when Navidrome re-scans the library.  If no rescan happens
before the next Popularr import reads Navidrome, the import upserts Navidrome's
STALE rows over the values just saved — "the next import puts the old data
back".

The original fix for this (per-file-tag-write ``startScan`` triggers) was
removed on 2026-08-30 because firing a scan after EVERY tag write paused the
Navidrome server and locked its database.  This module is the compromise
described in the change log: **ONE** rescan request per user-initiated save,
COALESCED so that rapid saves collapse into at most one running scan plus one
queued follow-up.  It is fire-and-forget (daemon thread) — the save response
never waits for Navidrome.

Contract
--------
``request_rescan()`` returns True when a scan is running or queued (the
request was accepted), False when Navidrome is not configured or the request
could not be queued.  Callers may report that value to the UI truthfully.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from helpers.logging_config import log_unified

logger = structlog.get_logger(__name__)

_state_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "pending": False, "reason": None}


def get_rescan_state() -> dict[str, Any]:
    """Return a snapshot of the coalescer state (for diagnostics/tests)."""
    with _state_lock:
        return dict(_state)


def request_rescan(reason: str = "metadata save") -> bool:
    """Request ONE background Navidrome rescan, coalescing bursts of saves.

    At most one scan thread runs at a time; a request arriving during a run
    sets a single ``pending`` flag so exactly ONE follow-up run happens after
    the current scan — a burst of N saves collapses into ≤ 2 scans instead of
    N.  Never blocks the caller.
    """
    from services.scanning.navidrome_scan_service import get_navidrome_config

    try:
        cfg = get_navidrome_config() or {}
    except Exception as exc:
        logger.debug("Navidrome rescan skipped — config unreadable", error=str(exc))
        return False
    if not all([cfg.get("base_url"), cfg.get("user"), cfg.get("pass")]):
        logger.debug("Navidrome rescan skipped — Navidrome not configured")
        return False

    with _state_lock:
        if _state["running"]:
            _state["pending"] = True
            _state["reason"] = reason
            logger.info(
                "Navidrome rescan already running — coalesced into follow-up",
                reason=reason,
            )
            return True
        _state["running"] = True
        _state["reason"] = reason

    thread = threading.Thread(
        target=_worker, args=(reason,), daemon=True, name="navidrome-rescan"
    )
    thread.start()
    return True


def _worker(reason: str) -> None:
    try:
        while True:
            _run_once(reason)
            with _state_lock:
                if _state["pending"]:
                    _state["pending"] = False
                    reason = _state["reason"] or reason
                    continue  # another save landed while scanning — run once more
                _state["running"] = False
                _state["reason"] = None
                return
    except BaseException:
        with _state_lock:
            _state["running"] = False
            _state["pending"] = False
            _state["reason"] = None
        raise


def _run_once(reason: str) -> bool:
    """Run one drain-and-wait rescan; never raises."""
    try:
        from services.scanning.navidrome_scan_service import get_nav_client

        client = get_nav_client()
    except Exception as exc:
        logger.debug("Navidrome rescan skipped — client unavailable", reason=reason, error=str(exc))
        return False

    log_unified(f"Navidrome rescan requested ({reason}) — waiting for completion…")
    ok = bool(client.trigger_and_wait_for_scan())
    if ok:
        log_unified(f"Navidrome rescan complete ({reason})")
    else:
        log_unified(
            f"⚠️ Navidrome rescan did NOT complete ({reason}) — Navidrome may still "
            "serve stale metadata until its next scheduled scan."
        )
    return ok
