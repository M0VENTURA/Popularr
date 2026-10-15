"""Maintenance routes — quiescing the application for a clean database stop.

Updating the PostgreSQL server means stopping it, and stopping it safely has two
halves: stop doing work, and stop holding connections. ``docker compose stop db``
alone does neither — see ``services/maintenance/safe_shutdown_service.py`` for why
PostgreSQL's SIGTERM "smart shutdown" waits on an idle pooled connection until
Docker escalates to SIGKILL.

Every route here is reversible. ``cancel`` undoes a quiesce, and the flag itself
auto-expires so a forgotten shutdown cannot leave the app permanently dormant.
"""

from __future__ import annotations

from typing import Any

import structlog
from quart import Blueprint, jsonify, request

logger = structlog.get_logger(__name__)
maintenance_bp = Blueprint("maintenance", __name__)


@maintenance_bp.route("/api/maintenance/safe-shutdown", methods=["GET"])
def api_safe_shutdown_status() -> Any:
    """Report whether it is safe to stop PostgreSQL, and why not if it is not."""
    from services.maintenance.safe_shutdown_service import safe_shutdown_status

    try:
        return jsonify(safe_shutdown_status())
    except Exception as exc:
        logger.error("Safe shutdown status failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@maintenance_bp.route("/api/maintenance/safe-shutdown", methods=["POST"])
async def api_request_safe_shutdown() -> Any:
    """Quiesce the application so the database can be stopped cleanly.

    Body (all optional)::

        {"drain": true, "reason": "postgres 17 upgrade"}

    ``drain`` defaults to true: wait for in-flight scans and queue items before
    releasing the connections. Draining does not FAIL on timeout — a queue item
    can be mid-move of a large file, and abandoning that is worse than reporting
    it — so the response says what is still running and leaves the decision to
    the operator.

    Quiescing is reversible, so this needs no confirmation gate of its own.
    """
    payload = (await request.get_json(silent=True)) or {}

    drain = payload.get("drain", True)
    # A JSON ``false`` must mean "do not drain", so normalise rather than
    # truth-test the raw value — the string "false" is truthy.
    if isinstance(drain, str):
        drain = drain.strip().lower() not in ("false", "0", "no", "")

    reason = str(payload.get("reason") or "manual").strip() or "manual"

    from services.maintenance.safe_shutdown_service import (
        prepare_for_database_shutdown,
    )

    try:
        result = prepare_for_database_shutdown(reason=reason, drain=bool(drain))
    except Exception as exc:
        logger.error("Safe shutdown failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500

    if not result.get("success"):
        return jsonify(result), 500
    return jsonify(result)


@maintenance_bp.route("/api/maintenance/safe-shutdown/cancel", methods=["POST"])
def api_cancel_safe_shutdown() -> Any:
    """Resume normal operation: clear the flag and unpause the scheduler.

    The connections are NOT reopened explicitly — the engine was kept, so the
    next query opens a fresh connection by itself. That is what makes a quiesce
    survivable without a restart.
    """
    from services.maintenance.safe_shutdown_service import cancel_safe_shutdown

    try:
        return jsonify(cancel_safe_shutdown(reason="api"))
    except Exception as exc:
        logger.error("Safe shutdown cancel failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@maintenance_bp.route("/api/maintenance/safe-shutdown/release-connections", methods=["POST"])
def api_release_connections() -> Any:
    """Close the pool without quiescing.

    Exposed separately because releasing connections is the step that makes the
    app unable to answer until the database returns, and an operator may want it
    on its own — for example after quiescing by hand, or to prove the pool closes.
    """
    from services.maintenance.safe_shutdown_service import (
        release_database_connections,
        safe_shutdown_status,
    )

    try:
        report = release_database_connections()
        return jsonify({"success": True, **report, **safe_shutdown_status()})
    except Exception as exc:
        logger.error("Releasing database connections failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


# =============================================================================
# ALBUM SAVE — background file tagging
# =============================================================================

@maintenance_bp.route("/api/maintenance/album-file-sync", methods=["GET"])
def api_album_file_sync_status() -> Any:
    """What album tagging is running in the background, and what just finished.

    A large album save returns as soon as its DATABASE half is committed, and the
    file tagging continues on a background thread. The user has already navigated
    away by then, so this is the only way to see whether that work succeeded —
    without it a failed tag write would be silent.

    ``recent`` is a bounded tail (newest first) so a failure stays visible after
    the job has left the running list.
    """
    from services.metadata.album_file_sync_service import (
        get_album_file_sync_state,
    )

    try:
        return jsonify(get_album_file_sync_state())
    except Exception as exc:
        logger.error("Album file sync status failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@maintenance_bp.route("/api/maintenance/album-file-sync/drain", methods=["POST"])
async def api_album_file_sync_drain() -> Any:
    """Wait for one album's background tagging to finish.

    Body: ``{"artist": "...", "album": "...", "timeout": 30}``

    For a script or an operator who wants to be sure the files are written before
    doing something else — for example before a database stop, which is why this
    lives beside the safe-shutdown routes rather than on the album page.
    """
    from services.metadata.album_file_sync_service import (
        get_album_file_sync_state,
        wait_for_album_file_sync,
    )

    # ⚠️ ``request.get_json()`` is a COROUTINE in Quart — a plain ``def`` handler
    # gets the coroutine object and every ``.get`` on it raises AttributeError.
    payload = (await request.get_json(silent=True)) or {}
    artist = str(payload.get("artist") or "").strip()
    album = str(payload.get("album") or "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album are required"}), 400

    try:
        timeout = float(payload.get("timeout") or 30)
    except (TypeError, ValueError):
        return jsonify({"error": "timeout must be a number"}), 400

    try:
        drained = wait_for_album_file_sync(artist, album, timeout=max(0.0, timeout))
    except Exception as exc:
        logger.error("Album file sync drain failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"drained": drained, **get_album_file_sync_state()})
