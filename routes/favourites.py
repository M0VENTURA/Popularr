"""Per-user favourites (heart) API routes.

Exposes toggle/state/sync endpoints for the per-user favourite (heart) system
backed by Navidrome star/unstar.  Hearts are scoped to the active Navidrome
user (``session["username"]``), so one user's hearts never apply to another.
"""

from __future__ import annotations

import logging

from quart import Blueprint, jsonify, request

from services.favourites_service import (
    favourite_ids,
    is_favourite,
    sync_favourites_from_navidrome,
    toggle_favourite,
)

logger = logging.getLogger(__name__)

favourites_bp = Blueprint("favourites", __name__)

_VALID_TYPES = {"track", "album", "artist"}


def _validate(payload: dict, require_id: bool = True):
    entity_type = str(payload.get("entity_type") or "").strip().lower()
    entity_id = str(payload.get("entity_id") or "").strip()
    if entity_type not in _VALID_TYPES:
        return None, None, "entity_type must be one of: track, album, artist"
    if require_id and not entity_id:
        return None, None, "entity_id is required"
    return entity_type, entity_id, None


@favourites_bp.route("/api/favourites/state", methods=["GET"])
def api_favourites_state():
    """Return whether the ACTIVE user has hearted one entity.

    Query params: ``entity_type`` (track/album/artist), ``entity_id``.
    """
    entity_type = (request.args.get("entity_type") or "").strip().lower()
    entity_id = (request.args.get("entity_id") or "").strip()
    if entity_type not in _VALID_TYPES or not entity_id:
        return jsonify({"error": "entity_type and entity_id are required"}), 400
    return jsonify({
        "success": True,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "is_favourite": is_favourite(entity_type, entity_id),
    })


@favourites_bp.route("/api/favourites/ids", methods=["GET"])
def api_favourites_ids():
    """Return all entity IDs hearted by the ACTIVE user for a type.

    Query params: ``entity_type`` (track/album/artist).
    """
    entity_type = (request.args.get("entity_type") or "").strip().lower()
    if entity_type not in _VALID_TYPES:
        return jsonify({"error": "entity_type must be one of: track, album, artist"}), 400
    return jsonify({"success": True, "entity_type": entity_type, "ids": favourite_ids(entity_type)})


@favourites_bp.route("/api/favourites/toggle", methods=["POST"])
async def api_favourites_toggle():
    """Toggle a heart for the ACTIVE user and mirror to Navidrome.

    Payload: ``{entity_type, entity_id, is_favourite, navidrome_id?}``.
    """
    payload = (await request.get_json(silent=True)) or {}
    entity_type, entity_id, err = _validate(payload)
    if err:
        return jsonify({"error": err}), 400
    liked = bool(payload.get("is_favourite"))
    navidrome_id = str(payload.get("navidrome_id") or "").strip() or None
    result = toggle_favourite(entity_type, entity_id, liked, navidrome_id=navidrome_id)
    if not result.get("success"):
        return jsonify({"error": result.get("error") or "Failed to update favourite"}), 500
    return jsonify(result)


@favourites_bp.route("/api/favourites/sync", methods=["POST"])
def api_favourites_sync():
    """Pull the ACTIVE user's starred items from Navidrome into the DB."""
    result = sync_favourites_from_navidrome()
    if not result.get("success"):
        return jsonify({"error": result.get("error") or "Sync failed"}), 500
    return jsonify(result)


@favourites_bp.route("/api/favourites/loved-playlist", methods=["POST"])
def api_favourites_loved_playlist():
    """Build/refresh the ACTIVE user's 'Loved Tracks' .nsp playlist."""
    from services.playlists.playlist_service import create_or_update_loved_tracks_playlist
    result = create_or_update_loved_tracks_playlist()
    if not result.get("success"):
        return jsonify({"error": result.get("error") or "Failed to build Loved Tracks playlist"}), 500
    return jsonify(result)


@favourites_bp.route("/api/favourites/sync-all", methods=["POST"])
def api_favourites_sync_all():
    """Pull EVERY configured Navidrome user's stars into the DB.

    Each user gets their own ``user_favourites`` rows, so one account's
    favourites never appear in another's playlists.
    """
    from services.favourites_service import sync_favourites_for_all_users

    result = sync_favourites_for_all_users()
    if not result.get("success"):
        return jsonify({
            "error": "; ".join(result.get("errors") or []) or "Sync failed",
            **result,
        }), 500
    return jsonify(result)


@favourites_bp.route("/api/favourites/favourite-playlists", methods=["POST"])
def api_favourites_favourite_playlists():
    """Build/refresh the per-user 'Favourite Artists' + 'Favourite Albums'.

    Body: ``{username?: str, force?: bool}``.  With no ``username`` every
    configured user is processed.  A playlist is written to the Navidrome
    account of the user it belongs to.
    """
    from services.playlists.favourite_playlist_service import (
        sync_favourite_playlists,
        sync_favourite_playlists_for_user,
    )

    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    force = bool(payload.get("force"))

    if username:
        result = sync_favourite_playlists_for_user(username, force=force)
        if not result.get("success"):
            return jsonify(result), 500
        return jsonify(result)

    result = sync_favourite_playlists(force=force)
    if not result.get("success"):
        return jsonify({
            "error": "; ".join(result.get("errors") or []) or "Favourite playlist sync failed",
            **result,
        }), 500
    return jsonify(result)
