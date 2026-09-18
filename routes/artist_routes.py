"""Artist-related API routes.

Handles:
- Artist corrections (merge, rename).
- Missing release detection and import.
- Artist metadata display.
"""

from __future__ import annotations

from typing import Any

from quart import Blueprint, request, jsonify
import structlog
from sqlalchemy import text

from routes.utils import json_response as _json_response
from services.metadata import artist_service as corrections
from services.metadata.release_service import get_cached_missing_releases
from services.metadata.artist_scan_service import (
    get_missing_releases as scan_get_missing_releases,
    import_release as scan_import_release,
    scan_all_missing_releases as scan_all_missing,
    add_artist as scan_add_artist,
)
from services.metadata import artist_metadata_service as metadata
from services.enrichment.musicbrainz_service import get_shared_mb_client
from db.engine import db_session
from helpers.artist_sort import artist_sort_sql

logger = structlog.get_logger(__name__)
artist_bp = Blueprint("artist", __name__)


# =============================
# CORRECTIONS
# =============================

@artist_bp.route("/api/artist/corrections/delete-track", methods=["POST"])
async def api_artist_corrections_delete_track() -> Any:
    payload = (await request.get_json(silent=True)) or {}

    return _json_response(
        corrections.delete_track(
            track_id=payload.get("track_id", ""),
            delete_file=payload.get("delete_file", True),
        )
    )


@artist_bp.route("/api/artist/corrections/clear-disc-number", methods=["POST"])
async def api_artist_corrections_clear_disc_number() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    data, code = corrections.clear_disc_number(
        artist=payload.get("artist", ""),
        album=payload.get("album", ""),
        force=payload.get("force_clear", False),
    )
    return jsonify(data), code


@artist_bp.route("/api/artist/corrections/apply-album-mbid", methods=["POST"])
async def api_artist_corrections_apply_album_mbid() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    data, code = corrections.apply_album_mbid(
        payload
    )
    return jsonify(data), code


@artist_bp.route("/api/artist/corrections/merge-albums", methods=["POST"])
async def api_artist_corrections_merge_albums() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    data, code = corrections.merge_albums(
        artist=payload.get("artist", ""),
        source_albums=payload.get("source_albums", []),
        canonical_name=payload.get("canonical_name", ""),
    )
    return jsonify(data), code


@artist_bp.route("/api/artist/corrections-albums")
def api_artist_corrections_albums() -> Any:
    artist = request.args.get("artist")
    data, code = corrections.get_correction_albums(artist or "")
    return jsonify(data), code


@artist_bp.route("/api/artists/corrections")
def api_artists_corrections() -> Any:
    """Return per-artist correction indicators for the artist list page."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT
                LOWER(REGEXP_REPLACE(
                    COALESCE(NULLIF(album_artist, ''), artist),
                    '(\s+[\[\(]?\s*(feat\.?|ft\.?|featuring)\s+.*?[\]\)]?$)',
                    '',
                    'i'
                )) AS artist_key,
                COALESCE(NULLIF(album_artist, ''), artist) AS artist_name,
                0 AS duplicate_track_count,
                COUNT(*) FILTER (WHERE disc_number IS NULL OR disc_number = '') AS disc_inconsistent_count,
                COUNT(*) FILTER (WHERE mbid IS NULL OR mbid = '') AS mbid_inconsistent_count,
                COUNT(*) FILTER (WHERE file_path IS NULL OR file_path = '') AS missing_tracks_count
            FROM tracks
            GROUP BY artist_key, artist_name
            HAVING
                COUNT(*) FILTER (WHERE disc_number IS NULL OR disc_number = '') > 0
                OR COUNT(*) FILTER (WHERE mbid IS NULL OR mbid = '') > 0
                OR COUNT(*) FILTER (WHERE file_path IS NULL OR file_path = '') > 0
        """))
        rows = result.fetchall()
        corrections_out = {}
        for row in rows:
            row_dict = dict(row._mapping)
            artist_name = row_dict.get("artist_name") or ""
            needs_correction = False
            dup_count = int(row_dict.get("duplicate_track_count") or 0)
            disc_ic = int(row_dict.get("disc_inconsistent_count") or 0)
            mbid_ic = int(row_dict.get("mbid_inconsistent_count") or 0)
            missing_ic = int(row_dict.get("missing_tracks_count") or 0)
            if any([dup_count, disc_ic, mbid_ic, missing_ic]):
                needs_correction = True
            corrections_out[artist_name.lower()] = {
                "needs_correction": needs_correction,
                "duplicate_track_count": dup_count,
                "disc_inconsistent_count": disc_ic,
                "mbid_inconsistent_count": mbid_ic,
                "missing_tracks_count": missing_ic,
                "_display_name": artist_name,
            }
        return jsonify({"success": True, "corrections": corrections_out})
    except Exception as exc:
        logger.error("Failed to fetch artist corrections list", error=str(exc))
        return jsonify({"error": str(exc)}), 500


# =============================
# METADATA
# =============================

@artist_bp.route("/api/artist/exists")
def api_artist_exists() -> Any:
    artist = request.args.get("artist")

    if not artist:
        return jsonify({
            "success": False,
            "error": "artist required"
        }), 400

    data, code = corrections.artist_exists(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/cached-missing-releases")
def api_cached_missing_releases() -> Any:
    artist = request.args.get("artist", "").strip()
    data, code = get_cached_missing_releases(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/cleanup-false-positive-missing", methods=["POST"])
async def api_cleanup_false_positive_missing() -> Any:
    payload = (await request.get_json()) or {}
    data, code = metadata.cleanup_false_positive_missing(payload.get("artist", ""))
    return jsonify(data), code


@artist_bp.route("/api/artist/bio")
def api_artist_bio() -> Any:
    artist = request.args.get("name", "")
    data, code = metadata.get_artist_bio(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/singles-count")
def api_artist_singles_count() -> Any:
    artist = request.args.get("name", "")
    data, code = metadata.get_singles_count(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/covered-by")
def api_artist_covered_by() -> Any:
    artist = request.args.get("artist", "")
    data, code = metadata.get_covered_by(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/favourite", methods=["GET", "POST", "DELETE"])
def api_artist_favourite() -> Any:
    data, code = metadata.artist_favourite(request)
    return jsonify(data), code


@artist_bp.route("/api/artist/image")
def api_artist_image() -> Any:
    from quart import redirect as quart_redirect
    artist = request.args.get("name", "")
    data, code = metadata.get_artist_image(artist)
    if data.get("success") and data.get("image_url"):
        return quart_redirect(data["image_url"])
    from quart import Response
    return Response(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect fill="#2a2a2a" width="200" height="200"/></svg>',
        mimetype="image/svg+xml",
        status=200,
    )


@artist_bp.route("/api/artist/search-images")
def api_artist_search_images() -> Any:
    artist = request.args.get("name", "")
    source = request.args.get("source", "")
    data, code = metadata.search_images(artist, source)
    return jsonify(data), code


@artist_bp.route("/api/artist/set-image", methods=["POST"])
async def api_artist_set_image() -> Any:
    payload = (await request.get_json()) or {}
    data, code = metadata.set_image(payload)
    return jsonify(data), code


@artist_bp.route("/api/artist/update-ids", methods=["POST"])
async def api_artist_update_ids() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    data, code = metadata.update_ids(payload)
    if code == 200 and data.get("success"):
        from helpers.logging_config import log_unified
        log_unified(f"[ARTIST] Updated external IDs for '{payload.get('name') or '?'}'")
    return jsonify(data), code


@artist_bp.route("/api/artist/lookup-ids", methods=["POST"])
async def api_artist_lookup_ids() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    data, code = metadata.lookup_ids(payload)
    if code == 200 and data.get("success"):
        from helpers.logging_config import log_unified
        log_unified(f"[ARTIST] Looked up + saved external IDs for '{payload.get('name') or '?'}'")
    return jsonify(data), code


@artist_bp.route("/api/artist/<path:artist>/similar")
def api_get_similar_artists(artist: str) -> Any:
    data, code = metadata.get_similar_artists(artist, request.args)
    return jsonify(data), code


@artist_bp.route("/api/artist/compilations")
def api_artist_compilations() -> Any:
    artist = request.args.get("name", "")
    data, code = metadata.get_compilations(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/main-tracks")
def api_artist_main_tracks() -> Any:
    artist = request.args.get("name", "")
    data, code = metadata.get_main_tracks(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/stats")
def api_artist_stats() -> Any:
    artist = request.args.get("name", "")
    data, code = metadata.get_stats(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/apply-genres", methods=["POST"])
async def api_artist_apply_genres() -> Any:
    payload = await request.get_json()
    data, code = metadata.apply_genres(payload)
    return jsonify(data), code


@artist_bp.route("/api/artist/genre-recommendations")
def api_artist_genre_recommendations() -> Any:
    artist = request.args.get("artist", "")
    data, code = metadata.genre_recommendations(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/genre-management/save", methods=["POST"])
async def api_artist_genre_management_save() -> Any:
    payload = (await request.get_json(silent=True)) or {}
    data, code = metadata.genre_management(payload)
    return jsonify(data), code


# =============================
# SCAN / HEAVY LOGIC
# =============================

@artist_bp.route("/api/artist/missing-releases")
def api_artist_missing_releases() -> Any:
    artist = request.args.get("artist", "").strip()
    background = str(request.args.get("background", "0")).strip().lower() in ("1", "true", "yes", "on")
    data, code = scan_get_missing_releases(artist or "", background=background)
    return jsonify(data), code


@artist_bp.route("/api/artist/missing-release-tracks")
def api_artist_missing_release_tracks() -> Any:
    """Fetch the MusicBrainz tracklist for a missing release."""
    release_id = str(request.args.get("release_id", "") or "").strip()
    if not release_id:
        return jsonify({"error": "release_id required"}), 400
    try:
        # ✅ Use shared MusicBrainz client singleton
        client = get_shared_mb_client()
        releases = client.browse_releases_for_group(release_id, inc="media") or []
        if not releases:
            return jsonify({"error": "No release found for this release group"}), 404
        release = releases[0]
        release_mbid = str(release.get("id") or "")
        if not release_mbid:
            return jsonify({"error": "Release has no MusicBrainz ID"}), 404
        detail = client.get_release(
            release_mbid, inc="recordings+artist-credits+media", timeout=15.0,
        )
        media = detail.get("media") or []
        tracks: list[dict[str, Any]] = []
        disc_number = 1
        for medium in media:
            if medium.get("disc") is not None:
                disc_number = int(medium.get("disc") or 1)
            for track in medium.get("tracks") or []:
                recording = track.get("recording") or {}
                tracks.append({
                    "position": track.get("number") or track.get("position") or "",
                    "title": track.get("title") or recording.get("title") or "",
                    "length": track.get("length") or recording.get("length") or 0,
                    "disc_number": disc_number,
                })
        if not tracks:
            for rec in detail.get("recordings") or []:
                tracks.append({
                    "position": "",
                    "title": rec.get("title") or "",
                    "length": rec.get("length") or 0,
                    "disc_number": 1,
                })
        return jsonify({
            "release_id": release_id,
            "release_mbid": release_mbid,
            "title": detail.get("title") or release.get("title") or "",
            "date": detail.get("date") or release.get("date") or "",
            "tracks": tracks,
        })
    except Exception as exc:
        logger.debug("Tracklist fetch failed for missing release", release_id=release_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@artist_bp.route("/api/artist/import-release", methods=["POST"])
async def api_import_release() -> Any:
    payload = (await request.get_json()) or {}
    artist = str(payload.get("artist") or "").strip()
    release_id = str(payload.get("release_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    data, code = scan_import_release(artist, release_id, title)
    return jsonify(data), code


@artist_bp.route("/api/artist/scan-all-missing-releases", methods=["POST"])
def api_scan_all_missing_releases() -> Any:
    data, code = scan_all_missing()
    return jsonify(data), code


@artist_bp.route("/api/artist/add", methods=["POST"])
async def api_add_artist() -> Any:
    payload = (await request.get_json()) or {}
    artist = (payload.get("artist") or "").strip()
    data, code = scan_add_artist(artist)
    return _json_response(data)


@artist_bp.route("/api/missing/overview")
def api_missing_overview() -> Any:
    """Return dashboard overview of gap-detected missing tracks and cached missing releases."""
    from sqlalchemy import text as sa_text
    from db.engine import db_session

    gap_albums = []
    missing_artists = []

    try:
        with db_session() as session:
            result = session.execute(sa_text("""
                SELECT COALESCE(NULLIF(album_artist, ''), artist) as artist, album,
                       COUNT(*) as track_count
                FROM tracks
                WHERE musicbrainz_album_mbid IS NOT NULL
                  AND (pending_mb_updates IS NOT NULL AND pending_mb_updates != '')
                GROUP BY COALESCE(NULLIF(album_artist, ''), artist), album
                ORDER BY artist, album
            """))
            for row in result.fetchall() or []:
                gap_albums.append(dict(row._mapping))
    except Exception as exc:
        logger.error("Failed to fetch gap albums", error=str(exc))

    try:
        with db_session() as session:
            # COUNT(*) restores the per-artist total the client renders
            # ("N missing"); this query previously selected the name alone, so
            # the badge read "undefined missing" for every row.
            #
            # Ordered by the FILED name, so "The Offspring" sits among the O's
            # exactly as it does on /artists.
            result = session.execute(sa_text(f"""
                SELECT artist, COUNT(*) AS missing_count
                FROM missing_releases
                GROUP BY artist
                ORDER BY {artist_sort_sql('artist')}
            """))
            missing_artists = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.error("Failed to fetch missing releases", error=str(exc))

    return jsonify({
        "gap_albums": gap_albums,
        "artists_with_missing_releases": missing_artists,
    })
