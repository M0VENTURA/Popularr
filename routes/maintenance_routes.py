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
