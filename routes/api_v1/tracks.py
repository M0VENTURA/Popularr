"""API v1 — Track endpoints."""

from __future__ import annotations

import json
from typing import Any

import structlog
from quart import jsonify, request
from sqlalchemy import text

from db.engine import db_session
from helpers.response_helpers import _fail, _ok
from services.enrichment.genre_tag_aggregator import get_track_genre_sources

from . import api_v1_bp

logger = structlog.get_logger(__name__)


@api_v1_bp.route("/tracks/<track_id>")
async def get_track(track_id: str) -> Any:
    """Get track metadata."""
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                payload, status = _fail("Track not found", 404)
                return jsonify(payload), status

            payload, status = _ok(track=dict(row._mapping))
            return jsonify(payload), status
    except Exception as exc:
        logger.error("Failed to get track metadata", track_id=track_id, error=str(exc))
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status


@api_v1_bp.route("/tracks/<track_id>/genres")
async def get_track_genres(track_id: str) -> Any:
    """Get all genre sources for a track."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""SELECT spotify_genres, lastfm_tags, musicbrainz_genres,
                    discogs_genres, essentia_genres, mood, listenbrainz_genres,
                    navidrome_genres, manual_genres
                    FROM tracks WHERE CAST(id AS TEXT) = :id"""),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                payload, status = _fail("Track not found", 404)
                return jsonify(payload), status

            track_dict = dict(row._mapping)
            sources = get_track_genre_sources(track_dict)

            # Flatten to simple name lists for the API response.
            genres = {
                source: [t["name"] for t in tags]
                for source, tags in sources.items()
            }

            payload, status = _ok(genres=genres)
            return jsonify(payload), status

    except Exception as exc:
        logger.error("Failed to get track genres", track_id=track_id, error=str(exc))
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status


# ---------------------------------------------------------------------------
# Apply / ignore a single MusicBrainz diff field
# ---------------------------------------------------------------------------
# Backs the album page's "Compare with MusicBrainz" review UI: each per-field
# row's Apply button calls apply-mb-field; Ignore calls ignore-mb-field.
# Whitelisted so a client can never write to an arbitrary column — only the
# fields services.enrichment.musicbrainz_service.compare_musicbrainz_release
# actually diffs are writable via this route.
_APPLIABLE_MB_FIELDS = {"title", "track_number", "disc_number", "mbid"}


@api_v1_bp.route("/tracks/<track_id>/apply-mb-field", methods=["POST"])
async def apply_track_mb_field(track_id: str) -> Any:
    """Write ONE metadata field on ONE track, as chosen in the album page's
    per-field MusicBrainz diff review (or by "Update All", which calls this
    once per remaining field across the tracklist).
    """
    try:
        data = await request.get_json(force=True, silent=True) or {}
        field = str(data.get("field") or "").strip()
        value = data.get("value")

        if field not in _APPLIABLE_MB_FIELDS:
            payload, status = _fail(f"Field '{field}' is not editable via this route", 400)
            return jsonify(payload), status
        if value is None or str(value).strip() == "":
            payload, status = _fail("value is required", 400)
            return jsonify(payload), status

        with db_session() as session:
            result = session.execute(
                text(f"UPDATE tracks SET {field} = :value WHERE CAST(id AS TEXT) = :id"),
                {"value": str(value), "id": track_id},
            )
            session.commit()
            if result.rowcount == 0:
                payload, status = _fail("Track not found", 404)
                return jsonify(payload), status

        payload, status = _ok(track_id=track_id, field=field, value=value)
        return jsonify(payload), status
    except Exception as exc:
        logger.error("Failed to apply MB field", track_id=track_id, error=str(exc))
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status


@api_v1_bp.route("/tracks/<track_id>/ignore-mb-field", methods=["POST"])
async def ignore_track_mb_field(track_id: str) -> Any:
    """Persist `field` into tracks.mb_ignored_fields (a JSON array column).

    This is the SAME column compare_musicbrainz_release()'s matching engine
    already reads to suppress diff_fields it has been told to ignore, so a
    field ignored here stays ignored on every future Compare — no separate
    ignore table required.
    """
    try:
        data = await request.get_json(force=True, silent=True) or {}
        field = str(data.get("field") or "").strip()
        if not field:
            payload, status = _fail("field is required", 400)
            return jsonify(payload), status

        with db_session() as session:
            row = session.execute(
                text("SELECT mb_ignored_fields FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": track_id},
            ).fetchone()
            if not row:
                payload, status = _fail("Track not found", 404)
                return jsonify(payload), status

            try:
                ignored = set(json.loads(row[0]) if row[0] else [])
            except (TypeError, ValueError):
                ignored = set()
            ignored.add(field)

            session.execute(
                text("UPDATE tracks SET mb_ignored_fields = :ignored WHERE CAST(id AS TEXT) = :id"),
                {"ignored": json.dumps(sorted(ignored)), "id": track_id},
            )
            session.commit()

        payload, status = _ok(track_id=track_id, field=field)
        return jsonify(payload), status
    except Exception as exc:
        logger.error("Failed to ignore MB field", track_id=track_id, error=str(exc))
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status
