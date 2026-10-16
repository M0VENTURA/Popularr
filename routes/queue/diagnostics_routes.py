"""
Queue diagnostics routes.
"""

from __future__ import annotations

from typing import Any

import structlog
from quart import Blueprint, request

from routes.utils import json_response as _json_response
from services.queue.queue_diagnostics_service import (
    queue_processor_status,
    queue_processor_restart,
    queue_search_events,
    queue_events,
    queue_check_collection_batch,
    queue_slskd_eligibility_diagnostics,
)

logger = structlog.get_logger(__name__)
queue_diagnostics_bp = Blueprint("queue_diagnostics", __name__)


@queue_diagnostics_bp.route("/api/queue-processor/status", methods=["GET"])
def api_queue_processor_status() -> Any:
    return _json_response(queue_processor_status())


@queue_diagnostics_bp.route("/api/queue-processor/restart", methods=["POST"])
def api_queue_processor_restart() -> Any:
    return _json_response(queue_processor_restart())


@queue_diagnostics_bp.route("/api/queue/events", methods=["GET"])
def api_queue_events() -> Any:
    return _json_response(queue_events(request.args))


@queue_diagnostics_bp.route("/api/queue/search-events", methods=["GET"])
def api_queue_search_events() -> Any:
    return _json_response(queue_search_events(request.args))


@queue_diagnostics_bp.route("/api/queue/check-collection-batch", methods=["POST"])
async def api_queue_check_collection_batch() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(queue_check_collection_batch(payload))


@queue_diagnostics_bp.route("/api/queue/diagnostics/slskd-eligibility", methods=["GET"])
def api_queue_slskd_eligibility_diagnostics() -> Any:
    return _json_response(queue_slskd_eligibility_diagnostics())


@queue_diagnostics_bp.route("/api/queue/rejections", methods=["GET"])
def api_queue_rejections() -> Any:
    """Downloads that were DISCARDED, and why.

    Read from ``queue_rejections`` rather than from the queue rows, and that is
    not an implementation detail — it is the whole reason the endpoint can
    answer at all. A rejected download lands in ``mark_failed``, which returns
    the row to ``queued`` and lets the next attempt overwrite
    ``failure_reason``, so by the time anyone looks the live row says nothing
    about the discard. The record has to outlive the row's status changes.

    ``?limit=`` caps the page (default 50, maximum 500). ``?clear=1`` empties
    the log — an explicit operator action, never a side effect of reading.
    """
    from db.repositories.queue_rejections import (
        clear_rejections,
        get_recent_rejections,
        rejection_counts,
    )

    args = request.args or {}
    try:
        limit = int(args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50

    cleared = 0
    if str(args.get("clear") or "").strip().lower() in ("1", "true", "yes"):
        cleared = clear_rejections()

    return _json_response(
        {
            "success": True,
            "rejections": get_recent_rejections(limit=limit),
            "counts": rejection_counts(),
            "cleared": cleared,
        }
    )
