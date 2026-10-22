"""Playlist management routes.

Handles:
- Playlist file creation (.nsp).
- Navidrome playlist listing.
- Track search for playlist editing.
- Download session initiation for batch imports.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time

from quart import Blueprint, render_template, redirect, url_for, session, request, jsonify, send_file

from helpers.config_helpers import get_config

# ✅ Service imports
from services.playlists import (
    create_playlist_file,
    list_playlists,
    load_playlist,
    search_songs_in_db,
    start_download_session,
    match_playlist_tracks,
)


        
from services.playlists.playlist_matching_service import (
    match_playlist_tracks
)    

# -----------------------------------------------------------------------------
# Blueprint
# -----------------------------------------------------------------------------

playlists_bp = Blueprint("playlists", __name__)



# =============================================================================
# PLAYLIST DOWNLOAD SESSIONS (lightweight JSON-backed)
# =============================================================================

import json as _json
import os as _os
import uuid as _uuid
from datetime import datetime as _datetime


def _playlist_sessions_file() -> str:
    """State-dir path for the lightweight playlist download sessions file."""
    try:
        from helpers.config_helpers import get_state_directory
        return _os.path.join(get_state_directory(), "playlist_sessions.json")
    except Exception:
        return _os.environ.get("PLAYLIST_SESSIONS_FILE", "/data/playlist_sessions.json")


def _load_sessions() -> list[dict]:
    """Load all playlist download sessions from the JSON file."""
    path = _playlist_sessions_file()
    if not _os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as _fh:
            return _json.load(_fh)
    except Exception:
        return []


def _save_sessions(sessions: list[dict]) -> None:
    """Persist playlist download sessions to the JSON file."""
    path = _playlist_sessions_file()
    _os.makedirs(_os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as _fh:
        _json.dump(sessions, _fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# GET /api/playlist-downloads
# ---------------------------------------------------------------------------

@playlists_bp.route("/api/playlist-downloads", methods=["GET"])
def api_playlist_downloads_list():
    """Return all active playlist download sessions."""
    sessions = _load_sessions()
    return jsonify({"sessions": sessions})


# ---------------------------------------------------------------------------
# POST /api/playlist-downloads/create
# ---------------------------------------------------------------------------

@playlists_bp.route("/api/playlist-downloads/create", methods=["POST"])
async def api_playlist_downloads_create():
    """Create a new playlist download session."""
    data = (await request.get_json(silent=True)) or {}
    session_name = str(data.get("session_name", "Unnamed Session")).strip()
    total_tracks = data.get("total_tracks")
    try:
        total_tracks = int(total_tracks) if total_tracks is not None else 0
    except (TypeError, ValueError):
        total_tracks = 0

    new_session = {
        "id": str(_uuid.uuid4()),
        "session_name": session_name or "Unnamed Session",
        "status": "active",
        "completed_tracks": 0,
        "total_tracks": total_tracks,
        "priority_queue": bool(data.get("priority_queue", False)),
        "created_at": _datetime.now().isoformat(),
    }

    sessions = _load_sessions()
    sessions.append(new_session)
    _save_sessions(sessions)

    return jsonify({"success": True, "session_id": new_session["id"]})

# =============================================================================
# UI ROUTES
# =============================================================================

@playlists_bp.route("/playlist-manager")
async def playlist_manager():
    # The standalone manager page was folded into the /playlists hub.
    return redirect(url_for("playlists.playlists_index"))


@playlists_bp.route("/playlist/import/csv")
async def playlist_importer_csv():
    return await render_template("playlists/importer_csv.html")


@playlists_bp.route("/playlists")
async def playlists_index():
    """Rebuilt Playlists page: all smart + regular playlists in one view."""
    return await render_template("playlists/index.html")


@playlists_bp.route("/playlists/browse")
async def playlists_browse():
    return redirect(url_for("playlists.playlists_index"))


# =============================================================================
# API ROUTES
# =============================================================================

# --------------------------------------------------
# IMPORT (JSON → DB MATCHING)
# --------------------------------------------------

@playlists_bp.route("/api/playlist/import", methods=["POST"])
async def api_playlist_import():
    try:
        data = (await request.get_json(silent=True)) or {}
        spotify_tracks = data.get("tracks", [])

        if not spotify_tracks:
            return jsonify({"error": "No tracks provided"}), 400

        from db.repositories.tracks import find_library_track

        def _match_track(track, cursor, **kwargs):
            """Match a single playlist track against the library."""
            library_track = find_library_track(
                artist=track.get("artist", ""),
                title=track.get("title", ""),
                album=track.get("album", ""),
                strict_album=False,
            )
            if library_track:
                return library_track, 1.0, "db_match"
            return None, 0.0, "unmatched"

        matched, missing, stats = match_playlist_tracks(
            spotify_tracks,
            _match_track
        )

        return jsonify({
            "success": True,
            "matched_tracks": matched,
            "missing_tracks": missing,
            "match_stats": stats
        })

    except Exception as e:
        logging.error(f"Playlist import error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# CSV IMPORT (Exportify → parse → queue-ready)
# --------------------------------------------------

@playlists_bp.route("/api/playlist/import/csv", methods=["POST"])
async def api_playlist_import_csv():
    """Import a playlist from an Exportify-exported CSV file (multipart).

    Form fields:
        file                 – CSV file (required)
        playlist_name        – import name (required)
        playlist_description – optional description
        target_user          – optional Navidrome user
        skip_matching        – "true" returns the parsed tracks only; the
                               Downloads page and /playlist/import/csv page
                               use this, then batch-queue the tracks as one
                               album group (import_group = playlist name,
                               album_artist = caller-chosen, default
                               "Various Artists")

    Exportify columns parsed: Track Name, Artist Name(s), Album Name,
    Album Artist, ISRC, Spotify URI, Duration (ms), Release Date, Genres,
    Record Label, Popularity, Explicit.  Semicolon-joined multi-artist
    fields are reduced to the primary artist so queue matching stays clean.
    """
    try:
        import csv
        import io
        import re

        form = await request.form
        files = await request.files

        uploaded_file = files.get("file")
        if uploaded_file is None or not uploaded_file.filename:
            return jsonify({"error": "No file uploaded"}), 400
        if not uploaded_file.filename.lower().endswith(".csv"):
            return jsonify({"error": "Only .csv files are supported"}), 400

        playlist_name = str(form.get("playlist_name") or "").strip()
        playlist_description = str(form.get("playlist_description") or "").strip()
        target_user = str(form.get("target_user") or "").strip()
        if not playlist_name:
            return jsonify({"error": "playlist_name is required"}), 400

        # Strip the UTF-8 BOM if present, then normalise header names.
        content = uploaded_file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        normalised = {(h or "").lower().strip(): h for h in (reader.fieldnames or [])}

        def col(aliases):
            """Return the actual header name for the first matching alias."""
            for alias in aliases:
                if alias in normalised:
                    return normalised[alias]
            return None

        title_col = col(["track name", "name", "title"])
        artist_col = col(["artist name(s)", "artist names", "artist name", "artist", "artists"])
        album_col = col(["album name", "album"])
        album_artist_col = col(["album artist", "album_artist", "albumartist"])
        isrc_col = col(["isrc"])
        uri_col = col(["spotify uri", "track uri", "uri"])
        duration_col = col(["duration (ms)", "duration_ms", "duration"])
        date_col = col(["release date", "release_date", "date"])
        genres_col = col(["genres", "genre"])
        label_col = col(["record label", "label"])
        popularity_col = col(["popularity"])
        explicit_col = col(["explicit"])

        if not title_col or not artist_col:
            return jsonify({
                "error": "CSV must contain at least 'Track Name' and 'Artist Name(s)' columns. "
                         "Please export using Exportify (exportify.net)."
            }), 400

        def _extract_year(date_str):
            """First 4-digit year found in a date string (or None)."""
            if not date_str:
                return None
            m = re.search(r"\b(\d{4})\b", str(date_str))
            return m.group(1) if m else None

        tracks_from_csv = []
        for row in reader:
            title = (row.get(title_col) or "").strip()
            artist = (row.get(artist_col) or "").strip()
            if not title and not artist:
                continue  # skip blank rows

            # Exportify joins multi-artist tracks with semicolons — keep the
            # primary artist so matching and Soulseek searches stay clean.
            if ";" in artist:
                artist = artist.split(";")[0].strip()
            raw_album_artist = (row.get(album_artist_col) or "").strip() if album_artist_col else ""
            album_artist = raw_album_artist or artist

            duration_s = None
            if duration_col:
                raw_dur = (row.get(duration_col) or "").strip()
                try:
                    duration_s = int(round(float(raw_dur) / 1000)) if raw_dur else None
                except (TypeError, ValueError):
                    duration_s = None

            tracks_from_csv.append({
                "title": title,
                "artist": artist,
                "album_artist": album_artist,
                "album": (row.get(album_col) or "").strip() if album_col else "",
                "isrc": (row.get(isrc_col) or "").strip() if isrc_col else "",
                "spotify_id": (row.get(uri_col) or "").strip().split(":")[-1] if uri_col else "",
                "duration_s": duration_s,
                "year": _extract_year(row.get(date_col)) if date_col else None,
                "genres": (row.get(genres_col) or "").strip() if genres_col else "",
                "record_label": (row.get(label_col) or "").strip() if label_col else "",
                "popularity": (row.get(popularity_col) or "").strip() if popularity_col else "",
                "explicit": (row.get(explicit_col) or "").strip().upper() if explicit_col else "",
            })

        if not tracks_from_csv:
            return jsonify({"error": "No tracks found in CSV"}), 400

        skip_matching = str(form.get("skip_matching") or "").lower() in ("true", "1", "yes")
        if skip_matching:
            logging.info(
                "CSV parse-only (skip_matching) for '%s': %d tracks parsed",
                playlist_name, len(tracks_from_csv),
            )
            return jsonify({
                "success": True,
                "all_tracks": tracks_from_csv,
                "total": len(tracks_from_csv),
            })

        # Library matching: ISRC first, then normalised artist+title.
        from db.repositories.tracks import find_library_track
        from sqlalchemy import text as _text
        from db.engine import db_session

        matched_tracks = []
        missing_tracks = []
        match_stats = {"isrc": 0, "fuzzy": 0, "strict": 0, "unmatched": 0}

        for track in tracks_from_csv:
            library_track = None
            strategy = "fuzzy"
            if track.get("isrc"):
                with db_session() as session:
                    row = session.execute(
                        _text(
                            "SELECT * FROM tracks "
                            "WHERE isrc = :isrc AND file_path IS NOT NULL AND file_path <> '' LIMIT 1"
                        ),
                        {"isrc": track["isrc"]},
                    ).fetchone()
                    if row:
                        library_track = dict(row._mapping)
                        strategy = "isrc"
            if library_track is None:
                library_track = find_library_track(
                    artist=track["artist"],
                    title=track["title"],
                    album=track.get("album") or None,
                    strict_album=False,
                )
                if library_track:
                    strategy = "strict"

            if library_track:
                matched_tracks.append({
                    "id": library_track.get("id"),
                    "title": library_track.get("title"),
                    "artist": library_track.get("artist"),
                    "album": library_track.get("album"),
                    "stars": library_track.get("stars"),
                    "file_path": library_track.get("file_path"),
                    "duration_s": library_track.get("duration"),
                    "confidence": 1.0,
                    "strategy": strategy,
                })
                match_stats[strategy] += 1
            else:
                missing_tracks.append({
                    "title": track["title"],
                    "artist": track["artist"],
                    "album_artist": track.get("album_artist", ""),
                    "album": track["album"],
                    "spotify_id": track["spotify_id"],
                    "isrc": track["isrc"],
                    "best_score": 0.0,
                    "duration_s": track.get("duration_s"),
                    "year": track.get("year"),
                    "genres": track.get("genres"),
                    "record_label": track.get("record_label"),
                    "popularity": track.get("popularity"),
                    "explicit": track.get("explicit"),
                })
                match_stats["unmatched"] += 1

        from helpers.config_helpers import get_config
        slskd_enabled = bool(get_config().get("slskd", {}).get("enabled", False))

        logging.info(
            "CSV playlist import '%s': matched %d/%d tracks (ISRC=%d, strict=%d)",
            playlist_name, len(matched_tracks), len(tracks_from_csv),
            match_stats["isrc"], match_stats["strict"],
        )
        return jsonify({
            "success": True,
            "playlist_name": playlist_name,
            "playlist_description": playlist_description,
            "target_user": target_user,
            "matched_tracks": matched_tracks,
            "missing_tracks": missing_tracks,
            "slskd_enabled": slskd_enabled,
            "message": f"Matched {len(matched_tracks)}/{len(tracks_from_csv)} tracks",
            "match_stats": match_stats,
        })
    except UnicodeDecodeError:
        return jsonify({"error": "Could not decode CSV file. Please save it as UTF-8."}), 400
    except Exception as exc:
        logging.error("CSV playlist import error: %s", exc, exc_info=True)
        return jsonify({"error": f"An error occurred while processing the CSV file: {exc}"}), 500


# --------------------------------------------------
# CREATE (NSP FILE)
# --------------------------------------------------

@playlists_bp.route("/api/playlist/create", methods=["POST"])
async def api_playlist_create():
    try:
        data = (await request.get_json(silent=True)) or {}

        name = data.get("playlist_name")
        description = data.get("playlist_description", "")
        tracks = data.get("matched_tracks", [])

        if not name or not tracks:
            return jsonify({"error": "Missing data"}), 400

        # ``format`` selects the file type: "m3u" writes a regular .m3u into
        # the Playlists folder (needs file paths), "nsp" writes a smart-
        # playlist JSON (legacy default).
        file_format = str(data.get("format") or "nsp").lower()
        if file_format == "m3u":
            from services.playlists.playlist_service import create_m3u_file
            m3u_tracks = []
            for t in tracks:
                if not (t.get("file_path") or "").strip():
                    continue
                m3u_tracks.append({
                    "file_path": t["file_path"],
                    "title": t.get("title") or "",
                    "artist": t.get("artist") or "",
                    "duration": t.get("duration_s") or t.get("duration") or 0,
                })
            file_path = create_m3u_file(name, m3u_tracks)
            if not file_path:
                return jsonify({"error": "No tracks with file paths could be written"}), 400
            return jsonify({
                "success": True,
                "file_path": file_path,
                "track_count": len(m3u_tracks),
            }), 201

        track_ids = [t["id"] for t in tracks if t.get("id")]

        file_path = create_playlist_file(name, description, track_ids)

        return jsonify({
            "success": True,
            "file_path": file_path,
            "track_count": len(track_ids)
        }), 201

    except Exception as e:
        logging.error(f"Playlist create error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# NAVIDROME LIST
# --------------------------------------------------

@playlists_bp.route("/api/playlist/list")
def api_playlist_list():
    try:
        cfg = get_config()
        nav = cfg.get("navidrome", {})

        playlists = list_playlists(
            nav.get("base_url"),
            nav.get("user"),
            nav.get("pass")
        )

        return jsonify({"playlists": playlists})

    except Exception as e:
        logging.error(f"List playlists error: {e}")
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# NAVIDROME LOAD
# --------------------------------------------------

@playlists_bp.route("/api/playlist/load", methods=["POST"])
async def api_playlist_load():
    try:
        data = (await request.get_json(silent=True)) or {}
        playlist_id = data.get("playlist_id")

        if not playlist_id:
            return jsonify({"error": "Missing playlist_id"}), 400

        cfg = get_config()
        nav = cfg.get("navidrome", {})

        playlist = load_playlist(
            nav.get("base_url"),
            nav.get("user"),
            nav.get("pass"),
            playlist_id
        )

        return jsonify({"success": True, "playlist": playlist})

    except Exception as e:
        logging.error(f"Load playlist error: {e}")
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# SEARCH SONGS
# --------------------------------------------------

@playlists_bp.route("/api/playlist/search-songs", methods=["POST"])
async def api_playlist_search():
    try:
        data = (await request.get_json(silent=True)) or {}
        query = data.get("query", "").strip()

        if not query:
            return jsonify({"error": "Query required"}), 400

        results = search_songs_in_db(query)

        return jsonify({"songs": results})

    except Exception as e:
        logging.error(f"Search error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# DOWNLOAD SESSION
# --------------------------------------------------

@playlists_bp.route("/api/playlist/session", methods=["POST"])
async def api_playlist_session():
    try:
        data = (await request.get_json(silent=True)) or {}
        tracks = data.get("tracks", [])
        playlist_name = data.get("playlist_name", "Imported Playlist")

        if not tracks:
            return jsonify({"error": "No tracks"}), 400

        session = start_download_session(
            name=playlist_name,
            user="default",
            total=len(tracks),
            priority=False,
        )

        return jsonify({
            "success": True,
            "session": session,
        })

    except Exception as e:
        logging.error(f"Session error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# RECOMMENDED PLAYLISTS
# --------------------------------------------------

@playlists_bp.route("/api/recommended-playlists", methods=["GET"])
def api_recommended_playlists():
    """Get recommended playlists from Last.fm / ListenBrainz.

    Returns empty recommendations gracefully when APIs are unavailable.
    """
    try:
        cfg = get_config()
        lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
        api_key = lastfm_cfg.get("api_key", "")

        from services.playlists.recommendation_service import PlaylistRecommender
        from db.engine import db_session

        lf_client = None
        if api_key:
            from api_clients.lastfm import LastFmClient
            lf_client = LastFmClient(api_key)

        recommender = PlaylistRecommender(lastfm_client=lf_client, db_connection=db_session)
        recommendations = recommender.get_recommendations()

        return jsonify({"success": True, "recommendations": recommendations})
    except Exception as exc:
        logging.error("Failed to fetch recommended playlists: %s", exc, exc_info=True)
        return jsonify({"success": True, "recommendations": {}})


@playlists_bp.route("/api/recommended-playlists/create", methods=["POST"])
async def api_recommended_playlists_create():
    """Create a Navidrome playlist from a recommendation category/type."""
    data = (await request.get_json(silent=True)) or {}
    category = data.get("category", "")
    playlist_type = data.get("type", "")

    if not category or not playlist_type:
        return jsonify({"success": False, "error": "category and type required"}), 400

    try:
        cfg = get_config()
        lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
        api_key = lastfm_cfg.get("api_key", "")

        from services.playlists.recommendation_service import PlaylistRecommender
        from db.engine import db_session

        lf_client = None
        if api_key:
            from api_clients.lastfm import LastFmClient
            lf_client = LastFmClient(api_key)

        recommender = PlaylistRecommender(lastfm_client=lf_client, db_connection=db_session)

        # Re-fetch recommendations and find matching playlist
        recs = recommender.get_recommendations()
        category_recs = recs.get(category, []) if isinstance(recs, dict) else []
        target = None
        for p in category_recs:
            if isinstance(p, dict) and p.get("type") == playlist_type:
                target = p
                break

        if not target:
            return jsonify({"success": False, "error": f"No playlist found for {category}/{playlist_type}"}), 404

        track_ids = target.get("track_ids", [])
        playlist_name = target.get("name", f"{category} - {playlist_type}")

        from services.playlists import create_playlist_file
        file_path = create_playlist_file(playlist_name, target.get("description", ""), track_ids)

        return jsonify({
            "success": True,
            "playlist": {
                "name": playlist_name,
                "file_path": file_path,
                "track_count": len(track_ids),
            }
        })
    except Exception as exc:
        logging.error("Failed to create recommended playlist: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# =============================================================================
# PLAYLIST INDEX (rebuilt /playlists page)
# =============================================================================

@playlists_bp.route("/api/playlists/all")
def api_playlists_all():
    """Return every playlist in the system.

    Smart playlists come from .nsp files in the Playlists directory (with
    their file name/path); anything else is a Navidrome smart/regular
    playlist fetched from the Subsonic API.  .nsp files win over same-named
    Navidrome entries so the file-backed version (which we can rename) is
    shown.
    """
    try:
        from routes.navidrome import get_navidrome_client
        from services.playlists.playlist_service import list_nsp_playlists

        file_playlists = list_nsp_playlists()
        file_names = {entry["name"].strip().lower(): entry for entry in file_playlists}

        client = get_navidrome_client()
        nav_playlists = []
        if client:
            try:
                nav_playlists = client.fetch_all_playlists() or []
            except Exception as exc:
                logging.error("Failed to fetch Navidrome playlists: %s", exc, exc_info=True)

        merged = []
        for entry in file_playlists:
            is_m3u = entry.get("kind") == "m3u"
            merged.append({
                "id": f"file::{entry['file_name']}",
                "source": "file",
                "type": "regular" if is_m3u else "smart",
                "name": entry["name"],
                "file_name": entry["file_name"],
                "file_path": entry["file_path"],
                "comment": entry["comment"],
                "track_count": entry["track_count"],
                "rule_based": entry["rule_based"],
                "kind": entry.get("kind", "nsp"),
                "cover_art_url": _playlist_cover_url(
                    "file", f"file::{entry['file_name']}", entry["file_path"], entry["name"]
                ),
            })

        for nav in nav_playlists:
            name = str(nav.get("name") or "").strip()
            if not name or name.strip().lower() in file_names:
                continue  # already shown as a file-backed smart playlist
            nav_type = nav.get("type")
            merged.append({
                "id": str(nav.get("id") or ""),
                "source": "navidrome",
                "type": nav_type if nav_type in ("smart", "regular") else "regular",
                "name": name,
                "file_name": None,
                "file_path": None,
                "comment": str(nav.get("comment") or ""),
                "track_count": int(nav.get("songCount") or 0) or len(nav.get("entry") or []),
                "rule_based": bool(nav.get("criteria")),
                "cover_art_url": _playlist_cover_url(
                    "navidrome", str(nav.get("id") or ""), None, name
                ),
            })

        merged.sort(key=lambda item: (item["type"] != "smart", item["name"].lower()))
        return jsonify({
            "playlists": merged,
            "navidrome_configured": bool(client),
            "ai_cover_available": _ai_cover_available(),
        })
    except Exception as exc:
        # Never let an exception escape to an HTML error page — the frontend
        # expects JSON and would choke on a rendered template.
        logging.error("Failed to list playlists: %s", exc, exc_info=True)
        return jsonify({"error": f"Failed to list playlists: {exc}", "playlists": []}), 500


# =============================================================================
# PLAYLIST COVER ART
# =============================================================================
#
# Every playlist thumbnail goes through ``/api/playlist/art`` rather than
# pointing the browser at Navidrome directly:
#
#   * the browser has no Navidrome session, and ``getCoverArt`` needs the
#     Subsonic credentials — an ``<img src>`` cannot carry them;
#   * which playlist maps to which Navidrome cover is a SERVER decision (a
#     file-backed smart playlist is only a name until Navidrome is asked).
#
# The proxy answers with the placeholder SVG on any miss, so a playlist with no
# art renders as a neutral tile instead of a broken image.

def _ai_cover_available() -> bool:
    """True when the Regenerate button should be shown.

    Both halves must be configured: the Gemini integration (billable, off with
    no key by default) and the cover toggle. The button is hidden otherwise —
    offering an action that can only fail is worse than not offering it.
    """
    try:
        from helpers.config_helpers import get_gemini_config, get_genre_cover_config

        gem = get_gemini_config()
        cfg = get_genre_cover_config()
        return bool(gem["enabled"] and gem["api_key"] and cfg["enabled"])
    except Exception as exc:
        logging.debug("AI cover availability check failed: %s", exc)
        return False


def _playlist_cover_url(
    source: str, playlist_id: str, file_path: str | None, name: str
) -> str:
    """The in-app URL for a playlist's thumbnail (query params, cacheable)."""
    from urllib.parse import urlencode

    params = {"source": source, "id": playlist_id, "name": name or ""}
    if file_path:
        params["file_path"] = file_path
    return f"/api/playlist/art?{urlencode(params)}"



@playlists_bp.route("/api/playlists/generate/recommendations", methods=["POST"])
async def api_playlists_generate():
    """Generate a recommendations playlist from Last.fm / ListenBrainz.

    Candidates are grounded in the library's top artists, matched against the
    local tracks table, written to an ``{name}.m3u`` when matches exist, and
    missing tracks are queued to Soulseek via ``queue_add``.  The work runs in
    a worker thread (``asyncio.to_thread``) so the per-source API throttling
    (Last.fm ~3 req/s, ListenBrainz 1 req/s) never blocks the event loop.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
        source = str(data.get("source") or "both").strip().lower()
        name = str(data.get("name") or "Recommended Mix").strip()
        limit = max(1, min(int(data.get("limit") or 12), 25))
        if source not in ("lastfm", "listenbrainz", "both"):
            return jsonify({"error": "source must be 'lastfm', 'listenbrainz' or 'both'"}), 400

        from services.playlists.generator_service import generate_recommendations
        result = await asyncio.to_thread(generate_recommendations, source, name, limit)
        return jsonify(result)
    except Exception as exc:
        logging.error("Playlist generation failed: %s", exc, exc_info=True)
        return jsonify({"error": f"Playlist generation failed: {exc}"}), 500


class _PlaylistTracksError(Exception):
    """Raised by ``_fetch_playlist_tracks`` for user-facing failures."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _fetch_playlist_tracks(source: str, playlist_id: str, file_path: str) -> tuple[dict, list[dict]]:
    """Resolve one playlist into ``(meta, tracks)``.

    ``source`` selects the backend: ``file`` reads the .nsp JSON directly
    (falling back to a Navidrome name lookup for rule-based files), while
    ``navidrome`` loads the playlist from the Subsonic API.  Raises
    ``_PlaylistTracksError`` for missing or unknown input.
    """
    from routes.navidrome import get_navidrome_client
    from services.playlists.playlist_service import read_nsp_playlist

    if source == "file":
        file_path = str(file_path or "")
        if not file_path or not _os.path.exists(file_path):
            raise _PlaylistTracksError("Playlist file not found", 404)
        # Path-traversal guard: only files inside the Playlists directory may
        # be read/exported — never arbitrary paths on disk.
        from services.playlists.playlist_service import is_safe_playlist_path
        if not is_safe_playlist_path(file_path):
            raise _PlaylistTracksError("Invalid playlist file path", 400)

        # Generated .m3u playlists are plain text — parse directly.
        if str(file_path).lower().endswith((".m3u", ".m3u8")):
            from services.playlists.playlist_service import read_m3u_file
            return ({
                "id": playlist_id,
                "source": "file",
                "type": "regular",
                "name": _os.path.splitext(_os.path.basename(file_path))[0],
                "file_name": _os.path.basename(file_path),
                "file_path": file_path,
                "comment": "Generated playlist",
                "rule_based": False,
            }, read_m3u_file(file_path))

        playlist = read_nsp_playlist(file_path)
        if not playlist:
            raise _PlaylistTracksError("Could not read playlist file", 500)

        tracks = playlist.get("_tracks") or []
        if not tracks:
            # Rule-based or trackIds-only: let Navidrome resolve the entries.
            client = get_navidrome_client()
            if client:
                try:
                    found = client.find_playlist_by_name(playlist.get("name") or "")
                    if found:
                        detail = client.fetch_playlist(found.get("id"))
                        tracks = detail.get("tracks") or []
                except Exception as exc:
                    logging.error("Failed to resolve rule-based playlist via Navidrome: %s", exc, exc_info=True)

        return ({
            "id": playlist_id,
            "source": "file",
            "type": "smart",
            "name": playlist.get("name"),
            "file_name": playlist.get("_file_name"),
            "file_path": playlist.get("_file_path"),
            "comment": playlist.get("comment") or "",
            "rule_based": bool(playlist.get("rules")),
        }, tracks)

    if source == "navidrome":
        client = get_navidrome_client()
        if not client:
            raise _PlaylistTracksError("Navidrome not configured", 400)
        playlist = client.fetch_playlist(playlist_id)
        if not playlist:
            raise _PlaylistTracksError("Playlist not found", 404)
        tracks = []
        for entry in playlist.get("tracks") or []:
            if not isinstance(entry, dict):
                continue
            tracks.append({
                "id": str(entry.get("id") or ""),
                "title": str(entry.get("title") or entry.get("name") or ""),
                "artist": str(entry.get("artist") or ""),
                "album": str(entry.get("album") or ""),
                "duration": entry.get("duration"),
                "rating": entry.get("userRating") or entry.get("rating"),
            })
        return ({
            "id": playlist_id,
            "source": "navidrome",
            "type": playlist.get("type") if playlist.get("type") in ("smart", "regular") else "regular",
            "name": playlist.get("name"),
            "file_name": None,
            "file_path": None,
            "comment": playlist.get("comment") or "",
            "rule_based": False,
        }, tracks)

    raise _PlaylistTracksError("Unknown playlist source", 400)


@playlists_bp.route("/api/playlists/tracks", methods=["POST"])
async def api_playlists_tracks():
    """Return the track list of one playlist.

    ``source`` selects the backend: ``file`` reads the .nsp JSON directly
    (falling back to a Navidrome name lookup for rule-based files), while
    ``navidrome`` loads the playlist from the Subsonic API.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
        meta, tracks = _fetch_playlist_tracks(
            str(data.get("source") or ""),
            str(data.get("id") or ""),
            str(data.get("file_path") or ""),
        )
        return jsonify({"playlist": meta, "tracks": tracks})
    except _PlaylistTracksError as exc:
        return jsonify({"error": exc.message}), exc.status
    except Exception as exc:
        logging.error("Failed to load playlist tracks: %s", exc, exc_info=True)
        return jsonify({"error": f"Failed to load playlist tracks: {exc}", "tracks": []}), 500


# -----------------------------------------------------------------------------
# ADD TRACK TO PLAYLIST (album/track page picker)
# -----------------------------------------------------------------------------

def _resolve_navidrome_playlist_for_art(
    source: str, playlist_id: str, file_path: str, name: str
) -> dict | None:
    """The Navidrome playlist a thumbnail request refers to, or None.

    A file-backed playlist has no Navidrome id of its own, so it is resolved by
    NAME — the same lookup ``_fetch_playlist_tracks`` uses for a rule-based
    ``.nsp``. Failure is a normal outcome (an unimported playlist has no cover),
    so every path returns None rather than raising.
    """
    from routes.navidrome import get_navidrome_client

    client = get_navidrome_client()
    if not client:
        return None
    try:
        if source == "navidrome":
            if not playlist_id:
                return None
            return client.fetch_playlist(playlist_id) or None
        lookup = str(name or "").strip()
        if not lookup:
            return None
        return client.find_playlist_by_name(lookup)
    except Exception as exc:
        logging.debug("Playlist art lookup failed: %s", exc)
        return None


@playlists_bp.route("/api/playlist/art", methods=["GET"])
async def api_playlist_art():
    """Serve one playlist's cover art.

    The browser cannot fetch this itself — Navidrome's ``getCoverArt`` needs the
    Subsonic credentials and an ``<img src>`` cannot carry them.

    ⚠️ The cover id comes from the PLAYLIST payload's own ``coverArt`` field,
    not from the playlist id. ``getCoverArt`` expects a coverArt ID
    (``pl-<hash>`` for a playlist with uploaded art); passing the playlist id
    would 404 or return an unrelated image.
    """
    from services.enrichment.album_art_service import get_album_art_placeholder_svg

    source = str(request.args.get("source") or "navidrome").strip()
    playlist_id = str(request.args.get("id") or "").strip()
    file_path = str(request.args.get("file_path") or "").strip()
    name = str(request.args.get("name") or "").strip()

    try:
        from routes.navidrome import get_navidrome_client

        client = get_navidrome_client()
        if client:
            resolved = await asyncio.to_thread(
                _resolve_navidrome_playlist_for_art, source, playlist_id, file_path, name
            )
            cover_id = str((resolved or {}).get("coverArt") or "").strip()
            if cover_id:
                data = await asyncio.to_thread(client.get_cover_art_bytes, cover_id, 300)
                if data:
                    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                    response = await send_file(io.BytesIO(data), mimetype=mime)
                    # Short cache: a regenerated cover must appear promptly, but
                    # a list of 60 playlists should not re-fetch on every paint.
                    response.headers["Cache-Control"] = "public, max-age=300"
                    return response
    except Exception as exc:
        logging.debug("Playlist art fetch failed: %s", exc)

    placeholder = get_album_art_placeholder_svg(size=300)
    placeholder.headers["Cache-Control"] = "no-store"
    return placeholder


def _genre_from_playlist_name(name: str) -> str:
    """``"Metalcore - Top Tracks"`` → ``"Metalcore"``.

    The template is configurable, so the affixes are read from config rather
    than hard-coded; when the name does not match the template the whole name is
    the genre — the AI is art-directing THIS playlist, and a name it was never
    briefed on is still a better brief than an empty one.
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    try:
        from helpers.config_helpers import get_playlists_config

        template = str(
            (get_playlists_config() or {}).get("genre_playlist_name_template")
            or "{genre} - Top Tracks"
        )
    except Exception:
        template = "{genre} - Top Tracks"

    if "{genre}" in template:
        prefix, _, suffix = template.partition("{genre}")
        prefix, suffix = prefix.strip(), suffix.strip()
        if prefix and raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
        if suffix and raw.endswith(suffix):
            raw = raw[: -len(suffix)].strip()
    return raw or str(name or "").strip()


@playlists_bp.route("/api/playlist/regenerate-cover", methods=["POST"])
async def api_playlist_regenerate_cover():
    """Regenerate a playlist's cover with Gemini, on demand.

    Gated on BOTH the Gemini integration and the cover toggle — the same gate
    the scan uses — so the button can only appear where it can actually work.
    """
    data = (await request.get_json(silent=True)) or {}
    name = str(data.get("name") or "").strip()
    playlist_id = str(data.get("id") or "").strip()

    if not name:
        return jsonify({"ok": False, "error": "Playlist name is required"}), 400

    if not _ai_cover_available():
        return jsonify({
            "ok": False,
            "error": (
                "AI cover art is not configured. Enable the Gemini integration "
                "and add an API key on the Config page."
            ),
        }), 400

    from routes.navidrome import get_navidrome_client

    client = get_navidrome_client()
    if not client:
        return jsonify({"ok": False, "error": "Navidrome is not configured"}), 400

    genre = _genre_from_playlist_name(name)

    def _run() -> dict:
        from services.playlists.genre_cover_service import generate_genre_cover

        return generate_genre_cover(
            genre,
            name,
            client,
            regenerate=True,
            playlist_id=playlist_id or None,
        )

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        logging.error("Cover regeneration failed: %s", exc, exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500

    if not result.get("ok"):
        return jsonify({
            "ok": False,
            "status": result.get("status"),
            "error": result.get("reason") or "Cover generation failed",
        }), 400

    # The stored cover changes but the thumbnail URL does not, so the browser
    # would keep its cached copy — ``cache_bust`` lets the client force a refetch.
    return jsonify({
        "ok": True,
        "status": result.get("status"),
        "playlist_id": result.get("playlist_id") or playlist_id,
        "cache_bust": int(time.time()),
    })


def _find_playlist_entry(playlist_id: str) -> dict | None:
    """Look up a file-backed playlist entry by its ``file::<name>`` id."""
    from services.playlists.playlist_service import list_nsp_playlists
    return next(
        (p for p in list_nsp_playlists() if str(p.get("id")) == str(playlist_id)),
        None,
    )


@playlists_bp.route("/api/playlists/add-track", methods=["POST"])
async def api_playlists_add_track():
    """Add one library track to a file-backed playlist (.nsp/.m3u).

    Payload: ``{track_id, playlist_id?}`` (existing playlist) or
    ``{track_id, new_name}`` (creates a new .nsp playlist, or appends when a
    playlist with that name already exists).  Rule-based playlists are
    rejected — they resolve their entries dynamically and cannot be edited.
    """
    try:
        from db.engine import db_session
        from sqlalchemy import text

        data = (await request.get_json(silent=True)) or {}
        track_id = str(data.get("track_id") or "").strip()
        if not track_id:
            return jsonify({"error": "Missing track_id"}), 400

        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT CAST(id AS TEXT) AS id, title, artist, album, file_path, duration "
                    "FROM tracks WHERE CAST(id AS TEXT) = :id"
                ),
                {"id": track_id},
            ).fetchone()
        if not row:
            return jsonify({"error": "Track not found"}), 404
        mapping = row._mapping
        track = {
            "id": str(mapping.get("id") or ""),
            "title": str(mapping.get("title") or ""),
            "artist": str(mapping.get("artist") or ""),
            "album": str(mapping.get("album") or ""),
            "file_path": str(mapping.get("file_path") or ""),
            "duration": 0,
        }
        try:
            track["duration"] = max(0, int(float(mapping.get("duration") or 0) or 0))
        except (TypeError, ValueError):
            track["duration"] = 0

        new_name = str(data.get("new_name") or "").strip()
        playlist_id = str(data.get("playlist_id") or "").strip()

        from services.playlists.playlist_service import (
            create_nsp_file,
            playlist_path,
        )

        entry = None
        if playlist_id:
            entry = _find_playlist_entry(playlist_id)
            if not entry:
                return jsonify({"error": "Playlist not found"}), 404
        elif new_name:
            existing = [
                p for p in _all_entries()
                if str(p.get("name") or "").strip().lower() == new_name.lower()
            ]
            entry = existing[0] if existing else None

        if entry:
            target_name = str(entry.get("name") or "")
            path = str(entry.get("file_path") or "")

            # Defense-in-depth: the entry lookup normally returns paths from
            # the Playlists dir; reject anything outside it anyway.
            from services.playlists.playlist_service import is_safe_playlist_path
            if not is_safe_playlist_path(path):
                return jsonify({"error": "Invalid playlist file path"}), 400

            # ── .m3u: append one #EXTINF block ────────────────────────────
            if entry.get("kind") == "m3u":
                if not track["file_path"]:
                    return jsonify({"error": "Track has no audio file on disk"}), 400
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    lines = [line.rstrip("\n") for line in handle]
                existing_paths = {
                    line.strip() for line in lines
                    if line.strip() and not line.startswith("#")
                }
                added = track["file_path"].strip() not in existing_paths
                if added:
                    lines.append(f"#EXTINF:{track['duration']},{track['artist']} - {track['title']}")
                    lines.append(track["file_path"])
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write("\n".join(lines) + "\n")
                return jsonify({"success": True, "playlist": target_name, "added": added})

            # ── .nsp: append id + embedded display entry ──────────────────
            with open(path, "r", encoding="utf-8") as handle:
                nsp_data = _json.load(handle)
            if not isinstance(nsp_data, dict):
                return jsonify({"error": "Playlist file is not valid JSON"}), 500
            if nsp_data.get("rules"):
                return jsonify({"error": "Cannot add tracks to a rule-based playlist"}), 400
        else:
            # ── Create-new: fresh .nsp holding this single track ──────────
            if not new_name:
                return jsonify({"error": "Provide playlist_id or new_name"}), 400
            target_name = new_name
            nsp_data = {"name": new_name, "comment": "", "trackIds": [], "tracks": []}
            path = playlist_path(new_name)

        track_ids = [str(t) for t in (nsp_data.get("trackIds") or [])]
        embedded = nsp_data.get("tracks") or []
        if not isinstance(embedded, list):
            embedded = []
        added = track["id"] not in track_ids
        if added:
            track_ids.append(track["id"])
            nsp_data["trackIds"] = track_ids
            if not any(
                isinstance(e, dict) and str(e.get("id")) == track["id"] for e in embedded
            ):
                embedded.append({
                    "id": track["id"],
                    "title": track["title"],
                    "artist": track["artist"],
                    "album": track["album"],
                    "rating": 0,
                })
            nsp_data["tracks"] = embedded
            if entry:
                # Write back to the existing file — its embedded ``name`` may
                # differ from the sanitized file name, so never re-derive it.
                with open(path, "w", encoding="utf-8") as handle:
                    _json.dump(nsp_data, handle, indent=2, ensure_ascii=False)
            elif not create_nsp_file(target_name, nsp_data):
                return jsonify({"error": "Failed to write playlist file"}), 500

        return jsonify({
            "success": True,
            "playlist": target_name,
            "added": added,
            "track_count": len(track_ids),
        })
    except Exception as exc:
        logging.error("Playlist add-track error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


def _all_entries() -> list[dict]:
    """Every file-backed playlist entry (used for create-new name matching)."""
    from services.playlists.playlist_service import list_nsp_playlists
    return list_nsp_playlists()


# -----------------------------------------------------------------------------
# PLAYLIST EXPORT (zip + progress)
# -----------------------------------------------------------------------------

@playlists_bp.route("/api/playlists/export", methods=["POST"])
async def api_playlists_export():
    """Start a background job that zips a playlist's local audio files."""
    try:
        data = (await request.get_json(silent=True)) or {}
        meta, tracks = _fetch_playlist_tracks(
            str(data.get("source") or ""),
            str(data.get("id") or ""),
            str(data.get("file_path") or ""),
        )
        if not tracks:
            return jsonify({"error": "Playlist has no tracks to export"}), 400

        from services.playlists.playlist_export_service import create_export_job
        job = create_export_job(str(meta.get("name") or "Playlist"), tracks)
        return jsonify({
            "job_id": job["id"],
            "status": job["status"],
            "total": job["total"],
            "skipped": len(job["skipped"]),
            "error": job["error"],
        })
    except _PlaylistTracksError as exc:
        return jsonify({"error": exc.message}), exc.status
    except Exception as exc:
        logging.error("Playlist export error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@playlists_bp.route("/api/playlists/export/status/<job_id>")
def api_playlists_export_status(job_id: str):
    """Poll progress for a running export job."""
    from services.playlists.playlist_export_service import get_export_job
    job = get_export_job(str(job_id))
    if not job:
        return jsonify({"error": "Export job not found"}), 404
    return jsonify(job)


@playlists_bp.route("/api/playlists/export/download/<job_id>")
async def api_playlists_export_download(job_id: str):
    """Serve the finished ZIP archive for a completed export job."""
    from services.playlists.playlist_export_service import get_export_job
    job = get_export_job(str(job_id))
    if not job or job.get("status") != "done" or not job.get("zip_path"):
        return jsonify({"error": "Export not ready"}), 404
    # Attachment filename comes from the path basename (the playlist name).
    return await send_file(job["zip_path"], as_attachment=True)


@playlists_bp.route("/api/playlists/rename", methods=["POST"])
async def api_playlists_rename():
    """Rename a playlist.

    File-backed smart playlists also rename the .nsp file on disk
    (``file_name``); Navidrome playlists are renamed via the Subsonic API.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
        source = str(data.get("source") or "")
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Playlist name is required"}), 400

        if source == "file":
            from services.playlists.playlist_service import rename_nsp_playlist, is_safe_playlist_path
            file_path = str(data.get("file_path") or "")
            if not file_path or not _os.path.exists(file_path):
                return jsonify({"error": "Playlist file not found"}), 404
            # Path-traversal guard: only files inside the Playlists directory
            # may be renamed — never arbitrary paths on disk.
            if not is_safe_playlist_path(file_path):
                return jsonify({"error": "Invalid playlist file path"}), 400
            # Generated .m3u playlists are renamed as plain text files.
            if str(file_path).lower().endswith((".m3u", ".m3u8")):
                from services.playlists.playlist_service import sanitize_playlist_name
                new_file_name = str(data.get("file_name") or name or "").strip()
                base = sanitize_playlist_name(new_file_name or name)
                new_path = _os.path.join(_os.path.dirname(file_path), f"{base}.m3u")
                if new_path != file_path:
                    _os.rename(file_path, new_path)
                return jsonify({"success": True, "file_path": new_path})
            try:
                result = rename_nsp_playlist(file_path, name, data.get("file_name"))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            except Exception as exc:
                logging.error("Failed to rename playlist file: %s", exc, exc_info=True)
                return jsonify({"error": f"Rename failed: {exc}"}), 500
            return jsonify({"success": True, **result})

        if source == "navidrome":
            from routes.navidrome import get_navidrome_client
            client = get_navidrome_client()
            if not client:
                return jsonify({"error": "Navidrome not configured"}), 400
            playlist_id = str(data.get("id") or "")
            if not playlist_id:
                return jsonify({"error": "Missing playlist id"}), 400
            if client.rename_playlist(playlist_id, name):
                return jsonify({"success": True})
            return jsonify({"error": "Navidrome rejected the rename"}), 500

        return jsonify({"error": "Unknown playlist source"}), 400
    except Exception as exc:
        logging.error("Failed to rename playlist: %s", exc, exc_info=True)
        return jsonify({"error": f"Rename failed: {exc}"}), 500


@playlists_bp.route("/api/playlists/delete", methods=["POST"])
async def api_playlists_delete():
    """Delete a playlist.

    ``source == "file"`` removes the .nsp/.m3u file from the Playlists
    directory (path-validated so arbitrary files can never be deleted);
    ``source == "navidrome"`` deletes the playlist via the Subsonic API.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
        source = str(data.get("source") or "")

        if source == "file":
            from services.playlists.playlist_service import _playlists_dir
            file_path = str(data.get("file_path") or "")
            if not file_path:
                return jsonify({"error": "Missing playlist file path"}), 400
            abs_path = _os.path.abspath(file_path)
            root = _os.path.abspath(_playlists_dir())
            if not abs_path.startswith(root + _os.sep) or not _os.path.isfile(abs_path):
                return jsonify({"error": "Invalid playlist file path"}), 400
            _os.remove(abs_path)
            return jsonify({"success": True, "file_path": abs_path})

        if source == "navidrome":
            from routes.navidrome import get_navidrome_client
            client = get_navidrome_client()
            if not client:
                return jsonify({"error": "Navidrome not configured"}), 400
            playlist_id = str(data.get("id") or "")
            if not playlist_id:
                return jsonify({"error": "Missing playlist id"}), 400
            if client.delete_playlist(playlist_id):
                return jsonify({"success": True})
            return jsonify({"error": "Navidrome rejected the delete"}), 500

        return jsonify({"error": "Unknown playlist source"}), 400
    except Exception as exc:
        logging.error("Failed to delete playlist: %s", exc, exc_info=True)
        return jsonify({"error": f"Delete failed: {exc}"}), 500