"""API v1 — Album endpoints.

New file — no albums.py previously existed alongside artists.py/tracks.py in
api_v1. Add `from . import albums` to this package's __init__.py (wherever it
currently does `from . import artists, tracks` or similar) so these routes
get registered.

NOTE ON URL PREFIX: this module assumes api_v1_bp is registered with
url_prefix="/api/v1" (unconfirmed — please check __init__.py / the app
factory's `register_blueprint` call). If the prefix differs, update the
fetch() URLs in static/js/album_detail.js to match.

NOTE ON EXISTING /api/album/... ENDPOINTS: the album page already calls
/api/album/favourite and /api/album/recommend-genres, which are NOT in this
file and are presumably served by a separate, non-versioned blueprint. If
that blueprint's conventions (URL shape, sync vs async, response format)
should be used INSTEAD of api_v1 for consistency with the rest of the album
page, let me know and I'll move these two endpoints there and match that
file's Flask/Quart style instead.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import unquote

import structlog
from quart import jsonify, request
from sqlalchemy import text

from db.engine import db_session
from helpers.response_helpers import _fail, _ok
from services.enrichment.musicbrainz_service import compare_musicbrainz_release

from . import api_v1_bp

logger = structlog.get_logger(__name__)


@api_v1_bp.route("/albums/<path:artist>/<path:album>/musicbrainz-compare", methods=["POST"])
async def compare_album_musicbrainz(artist: str, album: str) -> Any:
    """Compare the current local tracklist against a linked MusicBrainz
    release. Powers the album page's "Compare with MusicBrainz" inline
    per-track/per-field diff review.

    Body: {"release_mbid": "..."} — the release (or release-group) MBID
    already saved on the album (see the Edit Album tab's MusicBrainz Release
    / Release Group ID fields). This endpoint does not run a fresh search.
    """
    artist = unquote(artist)
    album = unquote(album)
    try:
        data = await request.get_json(force=True, silent=True) or {}
        release_mbid = str(data.get("release_mbid") or "").strip()

        if not release_mbid:
            payload, status = _fail(
                'No MusicBrainz release linked yet. Use "Lookup on MusicBrainz" first, then save.',
                400,
            )
            return jsonify(payload), status

        result = compare_musicbrainz_release(artist, album, release_mbid)
        if not result.get("success"):
            payload, status = _fail(result.get("error") or "Comparison failed", 400)
            return jsonify(payload), status

        # Drop "success" before spreading into _ok(**result) — _ok already
        # sets that key itself, and passing it twice would raise a duplicate
        # keyword argument error if _ok names `success` as an explicit param.
        result_fields = {k: v for k, v in result.items() if k != "success"}
        payload, status = _ok(**result_fields)
        return jsonify(payload), status
    except Exception as exc:
        logger.error(
            "Failed to compare album with MusicBrainz",
            artist=artist,
            album=album,
            error=str(exc),
        )
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status


@api_v1_bp.route("/albums/<path:artist>/<path:album>/bulk-delete", methods=["POST"])
async def bulk_delete_album_tracks(artist: str, album: str) -> Any:
    """Delete multiple tracks at once — restores the album page's multi-
    select "Delete Selected" flow.

    Body: {"track_ids": [...], "delete_files": bool}
    When delete_files is true, each track's underlying audio file is also
    removed from disk (best-effort per track — a missing/unreadable file
    does not abort the rest of the batch, and is reported in `errors`).

    NOTE: this uses a plain os.remove() for file deletion. If there is an
    existing helper used by the single-track /track/<id>/delete route (for
    path resolution, permission handling, or logging), swap it in here so
    both delete paths behave identically.
    """
    artist = unquote(artist)
    album = unquote(album)
    try:
        data = await request.get_json(force=True, silent=True) or {}
        track_ids = data.get("track_ids") or []
        delete_files = bool(data.get("delete_files"))

        if not track_ids or not isinstance(track_ids, list):
            payload, status = _fail("track_ids is required", 400)
            return jsonify(payload), status

        deleted_count = 0
        errors: list[str] = []
        with db_session() as session:
            for track_id in track_ids:
                try:
                    if delete_files:
                        row = session.execute(
                            text("SELECT file_path FROM tracks WHERE CAST(id AS TEXT) = :id"),
                            {"id": str(track_id)},
                        ).fetchone()
                        file_path = row[0] if row else None
                        if file_path and os.path.isfile(file_path):
                            os.remove(file_path)
                    session.execute(
                        text("DELETE FROM tracks WHERE CAST(id AS TEXT) = :id"),
                        {"id": str(track_id)},
                    )
                    deleted_count += 1
                except Exception as row_exc:
                    errors.append(f"{track_id}: {row_exc}")
            session.commit()

        payload, status = _ok(
            deleted_count=deleted_count,
            errors=errors,
            artist=artist,
            album=album,
        )
        return jsonify(payload), status
    except Exception as exc:
        logger.error(
            "Failed to bulk-delete album tracks",
            artist=artist,
            album=album,
            error=str(exc),
        )
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status
