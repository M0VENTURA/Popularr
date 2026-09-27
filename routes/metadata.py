"""Metadata lookup and conflict resolution routes.

Provides API endpoints for:
- Querying MusicBrainz release metadata by MBID.
- Viewing and resolving metadata conflicts (shadow table).
"""

from __future__ import annotations

from typing import Any

from quart import Blueprint, jsonify, request
import structlog

from services.metadata.release_service import get_release_details
from services.metadata.conflict_service import (
    fetch_pending_conflicts,
    count_pending_conflicts,
    get_conflict_stats,
    resolve_conflict,
    resolve_conflicts_batch,
    ignore_conflict,
)
from services.metadata.pending_update_service import fetch_albums_pending_review

logger = structlog.get_logger(__name__)
metadata_bp = Blueprint("metadata", __name__)


# ---------------------------------------------------------------------------
# Pending recommendations (the dashboard's "needs approval" panel)
# ---------------------------------------------------------------------------

@metadata_bp.route("/api/metadata/pending-recommendations", methods=["GET"])
async def api_pending_recommendations() -> Any:
    """Albums whose stashed MusicBrainz recommendations await a decision.

    ⭐ WHY THIS IS A READ OF EXISTING STATE. The scan already stashes what a
    metadata import WOULD write onto ``tracks.pending_mb_updates``, with a
    ``stashed_at`` timestamp, whenever the Config page's metadata updating is
    set to recommend-only. Nothing new is stored here — this endpoint only
    surfaces that state so the dashboard can show it without visiting each
    album.

    An album leaves the list by itself once its review is saved or discarded,
    because both paths clear the stash (``routes/ui_routes.py`` calls
    ``discard_album_recommendations`` on a successful save).

    ``limit`` caps the number of ALBUMS returned, newest-first; the recency
    ordering itself lives in the service (the timestamp is inside the JSON
    envelope, not a column).
    """
    try:
        limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    except (TypeError, ValueError):
        limit = 50

    try:
        albums = fetch_albums_pending_review(limit=limit)
    except Exception as exc:
        logger.error("Failed to read pending recommendations", error=str(exc))
        return jsonify({"success": False, "error": str(exc), "albums": []}), 500

    return jsonify({"success": True, "albums": albums, "count": len(albums)})


# ---------------------------------------------------------------------------
# MusicBrainz release lookup
# ---------------------------------------------------------------------------

@metadata_bp.route("/api/musicbrainz/release/<release_id>")
def api_get_release_details(release_id: str) -> Any:
    result = get_release_details(release_id)
    if not result:
        return jsonify({"error": "Release not found"}), 404
    return jsonify({"success": True, **result})


# ---------------------------------------------------------------------------
# Conflict API
# ---------------------------------------------------------------------------

@metadata_bp.route("/api/conflicts/pending", methods=["GET"])
async def api_get_pending_conflicts() -> Any:
    """Return unresolved metadata conflicts, paginated."""
    try:
        limit = min(request.args.get("limit", 100, type=int), 500)
        offset = max(request.args.get("offset", 0, type=int), 0)
        provider = request.args.get("provider") or None
        artist = request.args.get("artist") or None

        conflicts = fetch_pending_conflicts(
            limit=limit, offset=offset,
            provider=provider, artist=artist,
        )
        total = count_pending_conflicts(provider=provider, artist=artist)

        return jsonify({
            "success": True,
            "conflicts": conflicts,
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.error("Failed to fetch conflicts", error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@metadata_bp.route("/api/conflicts/stats", methods=["GET"])
async def api_get_conflict_stats() -> Any:
    """Return aggregate conflict statistics for the corrections page."""
    try:
        stats = get_conflict_stats()
        return jsonify({"success": True, **stats})
    except Exception as exc:
        logger.error("Failed to fetch conflict stats", error=str(exc))
        return jsonify({"success": False, "error": str(exc)}), 500


@metadata_bp.route("/api/conflicts/resolve", methods=["POST"])
async def api_resolve_conflict() -> Any:
    """Resolve a single conflict, optionally applying a chosen value."""
    try:
        data = await request.get_json() or {}
        conflict_id = data.get("conflict_id")
        if not conflict_id:
            return jsonify({"success": False, "error": "conflict_id is required"}), 400

        accepted_value = data.get("accepted_value")
        resolved_by = data.get("resolved_by", "webui")

        result = resolve_conflict(
            conflict_id=int(conflict_id),
            accepted_value=accepted_value,
            resolved_by=resolved_by,
        )
        status = 200 if result.get("success") else 400
        return jsonify(result), status
    except Exception as exc:
        logger.error("Failed to resolve conflict", error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@metadata_bp.route("/api/conflicts/resolve-batch", methods=["POST"])
async def api_resolve_conflicts_batch() -> Any:
    """Resolve all pending conflicts for a track atomically."""
    try:
        data = await request.get_json() or {}
        track_id = data.get("track_id")
        resolutions = data.get("resolutions", {})

        if not track_id or not resolutions:
            return jsonify({
                "success": False,
                "error": "track_id and resolutions are required",
            }), 400

        resolved_by = data.get("resolved_by", "webui")
        result = resolve_conflicts_batch(
            track_id=track_id,
            resolutions=resolutions,
            resolved_by=resolved_by,
        )
        return jsonify(result)
    except Exception as exc:
        logger.error("Failed to batch-resolve conflicts", error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@metadata_bp.route("/api/conflicts/ignore", methods=["POST"])
async def api_ignore_conflict() -> Any:
    """Mark a conflict as ignored (keep local value)."""
    try:
        data = await request.get_json() or {}
        conflict_id = data.get("conflict_id")
        if not conflict_id:
            return jsonify({"success": False, "error": "conflict_id is required"}), 400

        resolved_by = data.get("resolved_by", "webui")
        result = ignore_conflict(
            conflict_id=int(conflict_id),
            resolved_by=resolved_by,
        )
        return jsonify(result)
    except Exception as exc:
        logger.error("Failed to ignore conflict", error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500
