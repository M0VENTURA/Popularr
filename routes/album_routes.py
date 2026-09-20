"""Album-related API routes.

Handles:
- Album track lists and artwork.
- Album-level genre and MBID assignment.
- Favourite toggling.
- File renaming for organised albums.
"""

from __future__ import annotations

import io
from urllib.parse import unquote
from typing import Any

from quart import Blueprint, jsonify, request, send_file
import structlog

from services.metadata.album_service import (
    rename_album_files_service,
    is_album_favourite,
    set_album_favourite,
    get_local_album_art,
    get_album_tracklist_from_db,
    get_album_queue_status_db,
    apply_genres_to_album,
    apply_mbid_to_album,
    apply_discogs_id_to_album,
    ignore_missing_track,
    match_album_tracklist,
    get_majority_artist,
    add_album_to_missing_releases,
    get_track_recommendations,
)

from services.enrichment.album_art_service import (
    search_album_art_external,
    set_album_art_from_url,
    set_album_art_from_upload,
    get_album_art_placeholder_svg,
)

from services.enrichment.musicbrainz_service import (
    lookup_musicbrainz_album,
    get_release_group_releases,
    compare_musicbrainz_release,
    get_musicbrainz_best_release,
)

from services.enrichment.discogs_service import lookup_discogs_album
from services.downloads.download_matching_service import get_musicbrainz_release_tracks

logger = structlog.get_logger(__name__)
album_bp = Blueprint('album_routes', __name__, url_prefix='/api/album')


def _pop_status(result: Any, default: int = 200) -> tuple[dict[str, Any], int]:
    """Normalize service result to (dict, status). Handles both tuple and dict returns."""
    if isinstance(result, tuple):
        return result
    status = result.pop("status", default)
    return result, status


@album_bp.route("/<path:artist>/<path:album>/rename-files", methods=["POST"])
def api_album_rename_files(artist: str, album: str) -> Any:
    """Rename all files in an album based on current metadata."""
    artist, album = unquote(artist), unquote(album)
    result = rename_album_files_service(artist, album)
    return jsonify(result), 200


@album_bp.route("/favourite", methods=["GET", "POST", "DELETE"])
async def api_album_favourite() -> Any:
    """Check, add, or remove an album from favourites."""
    if request.method in ["GET", "DELETE"]:
        artist = request.args.get("artist", "").strip()
        album = request.args.get("album", "").strip()
    else:
        data = (await request.get_json(silent=True)) or {}
        artist = data.get("artist", "").strip()
        album = data.get("album", "").strip()

    if not artist or not album:
        return jsonify({"error": "Artist and album required"}), 400

    if request.method == "GET":
        is_fav = is_album_favourite(artist, album)
        return jsonify({"is_favourite": is_fav})
        
    if request.method == "POST":
        success = set_album_favourite(artist, album, True)
        if success:
            return jsonify({"success": True, "is_favourite": True})
        return jsonify({"error": "DB Error"}), 500
        
    if request.method == "DELETE":
        success = set_album_favourite(artist, album, False)
        if success:
            return jsonify({"success": True, "is_favourite": False})
        return jsonify({"error": "DB Error"}), 500

    return jsonify({"error": "Method not allowed"}), 405


@album_bp.route("/art-placeholder", methods=["GET"])
def api_album_art_placeholder() -> Any:
    """Return a placeholder SVG for missing album art."""
    return get_album_art_placeholder_svg(size=300)


@album_bp.route("/<path:artist>/<path:album>/art")
async def api_album_art(artist: str, album: str) -> Any:
    """Get album art."""
    try:
        artist, album = unquote(artist), unquote(album)
        img_data, mime_type = get_local_album_art(artist, album)
        if img_data:
            return await send_file(io.BytesIO(img_data), mimetype=mime_type)
    except Exception as exc:
        logger.debug("Album art fetch failed", artist=artist, album=album, error=str(exc))
    return get_album_art_placeholder_svg(size=300)


@album_bp.route("/tracklist")
def api_album_tracklist() -> Any:
    """Get tracklist for an album."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    
    if not artist or not album:
        return jsonify({"error": "Artist and album parameters required"}), 400
        
    local_tracks = get_album_tracklist_from_db(artist, album)
    if local_tracks:
        return jsonify({
            "success": True,
            "artist": artist,
            "album": album,
            "tracklist": local_tracks,
            "source": "database"
        })
        
    return jsonify({"error": "Tracks not found"}), 404


@album_bp.route("/tracklist/match", methods=["GET"])
def api_album_tracklist_match() -> Any:
    """Check which tracks from an album already exist in the library."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    
    if not artist or not album:
        return jsonify({"error": "Artist and album parameters required"}), 400
        
    result, status_code = _pop_status(match_album_tracklist(artist, album))
    return jsonify(result), status_code


@album_bp.route("/queue-status", methods=["GET"])
def api_album_queue_status() -> Any:
    """Return the current download-queue status for every queued track in an album."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    
    if not artist or not album:
        return jsonify({"error": "artist and album are required"}), 400

    result = get_album_queue_status_db(artist, album)
    return jsonify({"success": True, "tracks": result})


@album_bp.route("/apply-genres", methods=["POST"])
async def api_album_apply_genres() -> Any:
    """Apply selected genres to all audio files in an album."""
    data = (await request.get_json(silent=True)) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    genres = data.get("genres", [])
    
    if not artist or not album or not genres:
        return jsonify({"error": "Missing required fields"}), 400
        
    result = apply_genres_to_album(artist, album, genres)
    status = 200 if result.get("success") else 500
    return jsonify(result), status


@album_bp.route("/apply-mbid", methods=["POST"])
async def api_album_apply_mbid() -> Any:
    """Apply MusicBrainz ID and cover art to all tracks in an album."""
    data = (await request.get_json(silent=True)) or {}
    artist = data.get("artist", "")
    album = data.get("album", "")
    mbid = str(data.get("mbid", "") or "").strip()
    rg_mbid = str(data.get("release_group_mbid", "") or "").strip()
    cover_url = data.get("cover_art_url", "")
    
    if not artist or not album:
        return jsonify({"error": "Missing artist or album"}), 400
        
    result, status = _pop_status(apply_mbid_to_album(artist, album, mbid, rg_mbid, cover_url), 200)
    if status == 200 and result.get("success"):
        from helpers.logging_config import log_unified
        log_unified(f"[ALBUM] Applied MusicBrainz ID to '{artist} - {album}' ({mbid or rg_mbid or 'n/a'})")
    return jsonify(result), status


@album_bp.route("/apply-discogs-id", methods=["POST"])
async def api_album_apply_discogs_id() -> Any:
    """Apply Discogs ID to all tracks in an album."""
    data = (await request.get_json(silent=True)) or {}
    artist = data.get("artist", "")
    album = data.get("album", "")
    discogs_id = data.get("discogs_id", "")
    is_single = data.get("is_single", False)
    
    if not artist or not album or not discogs_id:
        return jsonify({"error": "Missing required fields"}), 400
        
    result, status = _pop_status(apply_discogs_id_to_album(artist, album, discogs_id, is_single))
    if status == 200 and result.get("success"):
        from helpers.logging_config import log_unified
        log_unified(f"[ALBUM] Applied Discogs ID to '{artist} - {album}' ({discogs_id})")
    return jsonify(result), status


@album_bp.route("/ignore-missing-track", methods=["POST"])
async def api_album_ignore_missing_track() -> Any:
    """Mark a persisted missing track as ignored."""
    data = (await request.get_json(force=True, silent=True)) or {}
    missing_id = data.get("id")
    artist = (data.get("artist") or "").strip()
    album = (data.get("album") or "").strip()
    title = (data.get("title") or "").strip()
    disc = int(data.get("disc_number") or 1)

    if not missing_id and not (artist and album and title):
        return jsonify({"error": "Provide id or (artist, album, title)"}), 400

    success = ignore_missing_track(missing_id, artist, album, title, disc)
    if success:
        return jsonify({"success": True})
    return jsonify({"error": "Database error"}), 500


@album_bp.route("/search-art", methods=["GET"])
def api_album_search_art() -> Any:
    """Search for album art on external sources."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    source = request.args.get("source", "musicbrainz").strip()
    
    if not artist or not album:
        return jsonify({"error": "Artist and album name required"}), 400
        
    result, status_code = search_album_art_external(artist, album, source)
    return jsonify(result), status_code


@album_bp.route("/set-art", methods=["POST"])
async def api_album_set_art() -> Any:
    """Set custom album art from a URL."""
    data = (await request.get_json(silent=True)) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    image_url = data.get("image_url", "").strip()

    if not artist or not album or not image_url:
        return jsonify({"error": "Artist, album name, and image URL required"}), 400

    result, status_code = _pop_status(set_album_art_from_url(artist, album, image_url))
    return jsonify(result), status_code


@album_bp.route("/upload-art", methods=["POST"])
async def api_album_upload_art() -> Any:
    """Set custom album art from an uploaded file."""
    try:
        form = await request.form
        artist = form.get("artist", "").strip()
        album = form.get("album", "").strip()
        image_file = request.files.get("image")

        if not artist or not album:
            return jsonify({"error": "Artist and album name required"}), 400
        if not image_file or not image_file.filename:
            return jsonify({"error": "No image file provided"}), 400

        mime_type = image_file.mimetype or "image/jpeg"
        if not mime_type.startswith("image/"):
            return jsonify({"error": "Uploaded file must be an image"}), 400

        image_data = image_file.read()
        if not image_data:
            return jsonify({"error": "Uploaded image is empty"}), 400

        result, status_code = _pop_status(set_album_art_from_upload(artist, album, image_data, mime_type))
        return jsonify(result), status_code
    except Exception as exc:
        # Quart's generic "An internal server error occurred" hid the real
        # cause (multipart parse / FileStorage access).  Surface it so the
        # upload failure is diagnosable.
        logger.error("Album art upload failed", artist=artist, album=album, error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": f"Upload failed: {exc}"}), 500


@album_bp.route("/submit-musicbrainz", methods=["POST"])
async def api_album_submit_musicbrainz() -> Any:
    """Generate a MusicBrainz submission URL for an album."""
    data = (await request.get_json(silent=True)) or {}
    artist = str(data.get("artist", "")).strip()
    album = str(data.get("album", "")).strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    submit_url = (
        f"https://musicbrainz.org/login?redirect=/release/create?"
        f"artist-credit.names.0.artist.name={__import__('urllib.parse').quote(artist)}"
        f"&name={__import__('urllib.parse').quote(album)}"
    )
    return jsonify({"success": True, "url": submit_url, "artist": artist, "album": album})


@album_bp.route("/<path:artist>/<path:album>/track-recommendations", methods=["GET"])
def api_album_track_recommendations(artist: str, album: str) -> Any:
    """Get genre recommendations for all tracks in an album."""
    artist, album = unquote(artist), unquote(album)
    result, status_code = _pop_status(get_track_recommendations(artist, album))
    return jsonify(result), status_code


@album_bp.route("/musicbrainz", methods=["POST"])
async def api_album_musicbrainz_lookup() -> Any:
    """Lookup album on MusicBrainz."""
    data = (await request.get_json(force=True, silent=True)) or {}
    album = data.get("album", "")
    artist = data.get("artist", "")
    existing_mbid = (data.get("existing_mbid") or "").strip()
    
    if not album or not artist:
        return jsonify({"error": "Missing album or artist"}), 400
        
    result, status_code = _pop_status(lookup_musicbrainz_album(artist, album, existing_mbid))
    return jsonify(result), status_code


@album_bp.route("/musicbrainz/release-group/releases", methods=["POST"])
async def api_release_group_releases() -> Any:
    """Fetch all specific releases in a MusicBrainz release group."""
    data = (await request.get_json(force=True, silent=True)) or {}
    rg_mbid = (data.get("release_group_mbid") or "").strip()
    if not rg_mbid:
        return jsonify({"error": "release_group_mbid is required"}), 400
        
    result, status_code = _pop_status(get_release_group_releases(rg_mbid, include_track_counts=True))
    return jsonify(result), status_code


@album_bp.route("/musicbrainz/compare", methods=["POST"])
async def api_album_musicbrainz_compare() -> Any:
    """Compare MusicBrainz release tracks with library tracks."""
    data = (await request.get_json(force=True, silent=True)) or {}
    rg_mbid = (data.get("release_group_mbid") or "").strip()
    artist = (data.get("artist") or "").strip()
    album = (data.get("album") or "").strip()

    if not rg_mbid or not artist or not album:
        return jsonify({"error": "release_group_mbid, artist, and album are required"}), 400
        
    result, status_code = _pop_status(compare_musicbrainz_release(artist, album, rg_mbid))
    return jsonify(result), status_code


@album_bp.route("/discogs", methods=["POST"])
async def api_album_discogs_lookup() -> Any:
    """Lookup album on Discogs."""
    data = (await request.get_json(force=True, silent=True)) or {}
    album = data.get("album", "")
    artist = data.get("artist", "")
    
    if not album or not artist:
        return jsonify({"error": "Missing album or artist"}), 400
        
    result, status_code = _pop_status(lookup_discogs_album(artist, album))
    return jsonify(result), status_code


@album_bp.route("/majority-artist", methods=["POST"])
async def api_album_majority_artist() -> Any:
    """Get the most common artist from all tracks in an album."""
    data = (await request.get_json(silent=True)) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    
    if not artist or not album:
        return jsonify({"error": "Missing required fields"}), 400
        
    result, status_code = _pop_status(get_majority_artist(artist, album))
    return jsonify(result), status_code


@album_bp.route("/add-to-missing-releases", methods=["POST"])
async def api_album_add_to_missing_releases() -> Any:
    """Add an album to the missing releases tracking list."""
    data = (await request.get_json(silent=True)) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    year = data.get("year", "").strip()
    
    if not artist or not album:
        return jsonify({"error": "Artist and album are required"}), 400
        
    result, status_code = _pop_status(add_album_to_missing_releases(artist, album, year))
    return jsonify(result), status_code


@album_bp.route("/musicbrainz/best-release", methods=["POST"])
async def api_album_musicbrainz_best_release() -> Any:
    """Find the best matching release inside a release group for a local album."""
    data = (await request.get_json(force=True, silent=True)) or {}
    rg_mbid = (data.get("release_group_mbid") or "").strip()
    artist = (data.get("artist") or "").strip()
    album = (data.get("album") or "").strip()

    if not rg_mbid:
        return jsonify({"success": False, "error": "release_group_mbid is required"}), 400
        
    result, status_code = _pop_status(get_musicbrainz_best_release(artist, album, rg_mbid))
    return jsonify(result), status_code


@album_bp.route("/musicbrainz/release/tracks", methods=["POST"])
async def api_album_musicbrainz_release_tracks() -> Any:
    """Fetch the track list for a specific MusicBrainz release."""
    data = (await request.get_json(force=True, silent=True)) or {}
    release_mbid = (data.get("release_mbid") or "").strip()
    
    if not release_mbid:
        return jsonify({"success": False, "error": "release_mbid is required"}), 400
        
    tracks = get_musicbrainz_release_tracks(release_mbid)
    if isinstance(tracks, tuple):
        tracks, status_code = tracks
    else:
        status_code = 200
    if not isinstance(tracks, dict):
        return jsonify({
            "success": True,
            "release_mbid": release_mbid,
            "tracks": tracks or [],
        }), status_code
    return jsonify(tracks), status_code


@album_bp.route("/library-tracks", methods=["GET"])
def api_album_library_tracks() -> Any:
    """Get all library tracks for a specific artist/album."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    try:
        from services.metadata.album_missing_service import get_library_tracks
        tracks = get_library_tracks(artist, album)
        return jsonify({"tracks": tracks})
    except Exception as exc:
        logger.error("Failed to get library tracks", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@album_bp.route("/missing-tracks", methods=["GET"])
def api_album_missing_tracks() -> Any:
    """The album's missing tracks — read from the database only.

    The MusicBrainz release fetch and the missing-set computation are done by
    the SCAN (``scan_stage_runner`` refreshes ``missing_album_tracks`` per
    album). This endpoint used to recompute on every request, and the artist
    page requests it ONCE PER OWNED ALBUM on load, so opening an artist page
    fired that many MusicBrainz calls — which queued behind the shared 1 req/s
    throttle whenever a scan was running and froze the worker.
    """
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    try:
        from services.metadata.album_missing_service import get_missing_tracks_from_db
        result = get_missing_tracks_from_db(artist, album)
        return jsonify(result)
    except Exception as exc:
        logger.error("Failed to get missing tracks", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@album_bp.route("/title-mismatches", methods=["GET"])
def api_album_title_mismatches() -> Any:
    """Compare library track titles against the full MusicBrainz release tracklist."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    try:
        from services.metadata.album_missing_service import get_title_mismatches
        result = get_title_mismatches(artist, album)
        return jsonify(result)
    except Exception as exc:
        logger.error("Failed to get title mismatches", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@album_bp.route("/bulk-tag", methods=["POST"])
async def api_album_bulk_tag() -> Any:
    """Add genre tags to multiple selected tracks."""
    payload = (await request.get_json(silent=True)) or {}
    from services.metadata.album_service import bulk_tag_tracks
    result, code = bulk_tag_tracks(payload)
    if code == 200 and result.get("success"):
        from helpers.logging_config import log_unified
        log_unified(f"[ALBUM] Bulk-tagged {result.get('updated_count') or 0} track(s) ({payload.get('artist') or '?'} - {payload.get('album') or '?'})")
    return jsonify(result), code


@album_bp.route("/bulk-delete", methods=["POST"])
async def api_album_bulk_delete() -> Any:
    """Delete multiple tracks from the DB."""
    payload = (await request.get_json(silent=True)) or {}
    from services.metadata.album_service import bulk_delete_tracks
    result, code = bulk_delete_tracks(payload)
    return jsonify(result), code


@album_bp.route("/update-ids", methods=["POST"])
async def api_album_update_ids() -> Any:
    """Update MusicBrainz/Discogs release IDs for an album's tracks."""
    payload = (await request.get_json(silent=True)) or {}
    from services.metadata.album_service import update_album_ids
    result, code = update_album_ids(payload)
    if code == 200 and result.get("success"):
        from helpers.logging_config import log_unified
        log_unified(f"[ALBUM] Updated album IDs ({payload.get('artist') or '?'} - {payload.get('album') or '?'})")
    return jsonify(result), code
