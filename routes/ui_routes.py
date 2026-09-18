"""UI page routes, auth, config — migrated from old app.py."""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import structlog
import yaml
from quart import (
    Blueprint, flash, jsonify, redirect, render_template, request,
    Response, session, url_for,
)
from sqlalchemy import text

from api_clients.navidrome import NavidromeClient
from db.engine import db_session
from db.repositories.tracks import insert_or_update_track
from helpers.artist_sort import (
    artist_sort_key,
    artist_sort_letter,
    artist_sort_name,
    artist_sort_sql,
)
from services.catalog.release_categories import (
    category_for_musicbrainz,
    normalise_category,
    ordered_specs,
)
from helpers.config_helpers import (
    clear_config_cache,
    get_config,
    needs_setup,
    save_config,
    save_partial_config,
)
from helpers.logging_config import resolve_log_dir, set_log_level
from helpers.normalization_service import strip_featured_artist
from services.catalog.album_classification_service import classify_album_type
from services.enrichment.genre_tag_aggregator import (
    get_album_genre_sources,
    get_artist_genre_sources,
    get_track_genre_sources,
)
from services.enrichment.musicbrainz_service import MusicBrainzService
from services.favourites_service import is_favourite as _user_is_favourite
from services.infrastructure.filesystem_service import resolve_downloads_dir
from services.metadata.artist_metadata_service import get_artist_members_cached
from services.metadata.artist_service import get_artist_corrections
from services.metadata.conflict_service import get_conflict_stats
from services.metadata.correction_service import get_album_tag_inconsistencies
from services.metadata.tag_file_service import (
    build_tag_updates,
    resolve_music_file_path,
    update_file_tags,
    write_tags_to_file,
)
from services.popularity.stages.album_stage import revert_track_live_state
from services.scanning.scan_history_service import get_recent_album_scans
from services.scheduler.scheduler_service import reschedule_jobs_from_config

try:
    import markdown
except ImportError:
    markdown = None

logger = structlog.get_logger(__name__)

ui_bp = Blueprint("ui", __name__)


# ===========================================================================
# AUTH HELPERS
# ===========================================================================

def _similar_artist_display_list(session: Any, entries: list) -> list[dict]:
    """Normalise cached similar-artist entries and flag ones already owned."""
    normalised: list[dict] = []
    names: set[str] = set()
    
    for entry in entries or []:
        if isinstance(entry, str):
            item = {"name": entry.strip(), "match": 0.0}
        elif isinstance(entry, dict):
            item = dict(entry)
            item["name"] = str(item.get("name") or "").strip()
            try:
                item["match"] = float(item.get("match") or 0.0)
            except Exception:
                item["match"] = 0.0
        else:
            continue
            
        if not item.get("name"):
            continue
            
        item["in_collection"] = False
        normalised.append(item)
        names.add(item["name"].lower())

    if not normalised:
        return []

    owned: set[str] = set()
    if names:
        name_list = list(names)
        placeholders = ", ".join([f":_n{i}" for i in range(len(name_list))])
        params = {f"_n{i}": n for i, n in enumerate(name_list)}
        rows = session.execute(
            text(f"""
                SELECT DISTINCT LOWER(COALESCE(NULLIF(album_artist, ''), artist))
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN ({placeholders})
            """),
            params,
        ).fetchall()
        owned = {str(r[0]) for r in rows if r[0]}

    for item in normalised:
        item["in_collection"] = item["name"].lower() in owned

    return normalised


def _needs_setup(cfg: Any = None) -> bool:
    """True while the first-run setup wizard should be shown."""
    return needs_setup(cfg)


def login_required(f: Any) -> Any:
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        if _needs_setup():
            return redirect(url_for("ui.setup"))
        if "username" not in session:
            return redirect(url_for("ui.login"))
        return f(*args, **kwargs)
    return decorated


# ===========================================================================
# LOGIN / LOGOUT
# ===========================================================================

@ui_bp.route("/login", methods=["GET", "POST"])
async def login() -> Any:
    if request.method == "POST":
        form = await request.form
        username = (form.get("username") or "").strip()
        password = (form.get("password") or "").strip()
        cfg = get_config()
        nav_users = cfg.get("navidrome_users", [])

        for user in nav_users:
            if user.get("user") == username:
                base_url = str(user.get("base_url", "")).rstrip("/")
                if not base_url:
                    continue
                try:
                    client = NavidromeClient(base_url=base_url, username=username, password=password, use_token_auth=False)
                    if client.ping():
                        session["username"] = username
                        await flash(f"Welcome back, {username}!", "success")
                        return redirect(url_for("ui.dashboard"))
                        
                    client = NavidromeClient(base_url=base_url, username=username, password=password, use_token_auth=True)
                    if client.ping():
                        session["username"] = username
                        await flash(f"Welcome back, {username}!", "success")
                        return redirect(url_for("ui.dashboard"))
                except Exception as exc:
                    logger.debug("Login ping failed", username=username, error=str(exc))

        await flash(
            "Invalid credentials — could not verify against Navidrome. "
            "Check that Navidrome is reachable and your username/password are correct.",
            "error",
        )
        return await render_template("auth/login.html")
        
    return await render_template("auth/login.html")


@ui_bp.route("/logout")
async def logout() -> Any:
    username = session.pop("username", None)
    await flash(f"Goodbye, {username}!", "info")
    return redirect(url_for("ui.login"))


# ===========================================================================
# SETUP
# ===========================================================================

@ui_bp.route("/setup", methods=["GET", "POST"])
async def setup() -> Any:
    cfg = get_config()
    nav_users = cfg.get("navidrome_users", [])
    nav_first = nav_users[0] if nav_users else {}
    api = cfg.get("api_integrations", {})
    dl = cfg.get("downloads", {})
    watcher_cfg = cfg.get("watcher", {})
    features_cfg = cfg.get("features", {})
    playlists_cfg = cfg.get("playlists", {})
    tagging_cfg = cfg.get("tagging", {})
    slskd_cfg = cfg.get("slskd", {})

    setup_defaults = {
        "nav_url": nav_first.get("base_url", ""),
        "nav_user": nav_first.get("user", ""),
        "nav_pass": nav_first.get("pass", ""),
        "lfm_enabled": api.get("lastfm", {}).get("enabled", False),
        "lfm_api_key": api.get("lastfm", {}).get("api_key", ""),
        "dg_enabled": api.get("discogs", {}).get("enabled", False),
        "dg_token": api.get("discogs", {}).get("token", ""),
        "lb_enabled": api.get("listenbrainz", {}).get("enabled", True),
        "lb_token": nav_first.get("listenbrainz_user_token", ""),
        "slskd_enabled": bool(slskd_cfg.get("enabled", False)),
        "slskd_url": slskd_cfg.get("web_url", ""),
        "slskd_api_key": slskd_cfg.get("api_key", ""),
        "essentia_enabled": bool(cfg.get("essentia", {}).get("script_path")),
        "essentia_tag_moods": cfg.get("essentia", {}).get("tag_moods", True),
        "essentia_tag_genres": cfg.get("essentia", {}).get("tag_genres", False),
        "file_name_format": dl.get("file_name_format", ""),
        "conversion_enabled": bool(dl.get("conversion", {}).get("enabled", False)),
        "conversion_mode": dl.get("conversion", {}).get("mode", "flac_to_mp3"),
        "conversion_bitrate": dl.get("conversion", {}).get("mp3_bitrate_kbps", 320),
        "auto_import_enabled": watcher_cfg.get("auto_import_enabled", True),
        "auto_popularity_scan": watcher_cfg.get("auto_popularity_scan", True),
        "downloads_watcher_enabled": watcher_cfg.get("downloads_watcher_enabled", True),
        "cover_detection_enabled": features_cfg.get("cover_detection_enabled", True),
        "upcoming_releases_scan_enabled": features_cfg.get("upcoming_releases_scan_enabled", True),
        "sync_ratings_to_all_users": features_cfg.get("sync_ratings_to_all_users", False),
        "essential_playlists_enabled": playlists_cfg.get("essential_playlists_enabled", True),
        "genre_playlists_enabled": playlists_cfg.get("genre_playlists_enabled", True),
        "new_music_playlist_enabled": playlists_cfg.get("new_music_playlist_enabled", True),
        "write_tags_to_file": tagging_cfg.get("write_tags_to_file", True),
        "quality_filter_enabled": bool((dl.get("quality_filter") or {}).get("enabled", False)),
    }

    pg_defaults = {
        "host": os.environ.get("PG_HOST", ""),
        "port": os.environ.get("PG_PORT", "5432"),
        "user": os.environ.get("PG_USER", "popularr"),
        "password": os.environ.get("PG_PASSWORD", ""),
        "database": os.environ.get("PG_DATABASE", "popularr"),
    }

    if request.method == "POST":
        return await render_template("auth/setup.html", message="Setup not yet implemented", pg=pg_defaults, defaults=setup_defaults)
        
    return await render_template("auth/setup.html", pg=pg_defaults, defaults=setup_defaults)


# ===========================================================================
# SETUP & TEST API
# ===========================================================================

@ui_bp.route("/api/test-navidrome-connection", methods=["POST"])
async def api_test_navidrome_connection() -> Any:
    """Test Navidrome connectivity with provided credentials before saving."""
    try:
        data = (await request.get_json()) or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400
        
    base_url = str(data.get("base_url", "")).rstrip("/")
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))

    if not base_url or not username:
        return jsonify({"success": False, "error": "URL and username are required"}), 400

    if "://" not in base_url:
        base_url = f"http://{base_url}"

    parsed = urlparse(base_url)
    if not parsed.hostname:
        return jsonify({"success": False, "error": "Invalid URL format — expected something like http://navidrome:4533"}), 400

    try:
        client = NavidromeClient(base_url=base_url, username=username, password=password, use_token_auth=False)
        sub_data = client._get_subsonic_response("ping", timeout=10)
        
        if not sub_data or sub_data.get("status") != "ok":
            client2 = NavidromeClient(base_url=base_url, username=username, password=password, use_token_auth=True)
            sub_data2 = client2._get_subsonic_response("ping", timeout=10)
            if sub_data2 and sub_data2.get("status") == "ok":
                sub_data = sub_data2
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": "❌ Cannot reach the server",
            "detail": str(exc),
        }), 200

    if not sub_data:
        return jsonify({
            "success": False,
            "error": "❌ Cannot reach the server",
            "detail": "Connection refused or DNS failure — check that Navidrome is running and reachable from this container",
        }), 200

    status = sub_data.get("status")
    if status == "ok":
        return jsonify({"success": True, "message": "✅ Connected successfully"})

    error_code = sub_data.get("error", {}).get("code") if isinstance(sub_data.get("error"), dict) else None
    error_msg = sub_data.get("error", {}).get("message", "") if isinstance(sub_data.get("error"), dict) else str(sub_data.get("error", ""))

    if error_code == 10:
        return jsonify({
            "success": False,
            "error": "❌ Credentials rejected — wrong username or password, or token auth mismatch",
            "detail": error_msg,
        }), 200
        
    if error_code:
        return jsonify({
            "success": False,
            "error": f"❌ Server returned error {error_code}",
            "detail": error_msg,
        }), 200

    return jsonify({
        "success": False,
        "error": "❌ Unknown error — check server logs",
        "detail": str(sub_data),
    }), 200


@ui_bp.route("/api/setup/save", methods=["POST"])
async def api_setup_save() -> Any:
    """Save the first-run setup wizard configuration."""
    try:
        data = (await request.get_json()) or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400
        
    if not data:
        return jsonify({"success": False, "error": "No configuration data received"}), 400

    nav_users = data.get("navidrome_users", [])
    if not nav_users or not nav_users[0].get("base_url") or not nav_users[0].get("user"):
        return jsonify({"success": False, "error": "Navidrome URL and username are required"}), 400

    pg_host = (data.pop("PG_HOST", "") or "").strip()
    if pg_host:
        data.setdefault("database", {})
        data["database"]["host"] = pg_host
        data["database"]["port"] = (data.pop("PG_PORT", "") or "5432").strip()
        data["database"]["user"] = (data.pop("PG_USER", "") or "popularr").strip()
        data["database"]["password"] = data.pop("PG_PASSWORD", "")
        data["database"]["name"] = (data.pop("PG_DATABASE", "") or "popularr").strip()
        
        os.environ["PG_HOST"] = data["database"]["host"]
        os.environ["PG_PORT"] = data["database"]["port"]
        os.environ["PG_USER"] = data["database"]["user"]
        os.environ["PG_PASSWORD"] = data["database"]["password"]
        os.environ["PG_DATABASE"] = data["database"]["name"]

    success = save_config(data)
    if success:
        session["username"] = nav_users[0].get("user", "")
        return jsonify({"success": True, "message": "Configuration saved"})
        
    return jsonify({"success": False, "error": "Failed to save config file"}), 500


@ui_bp.route("/api/setup/save-partial", methods=["POST"])
async def api_setup_save_partial() -> Any:
    """Save partial wizard configuration — merges into existing config."""
    try:
        data = (await request.get_json()) or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400
        
    if not data:
        return jsonify({"success": False, "error": "No data received"}), 400

    nav_users = data.get("navidrome_users", [])
    if nav_users and nav_users[0].get("user"):
        session["username"] = nav_users[0]["user"]

    pg_host = (data.pop("PG_HOST", "") or "").strip()
    if pg_host:
        data.setdefault("database", {})
        data["database"]["host"] = pg_host
        data["database"]["port"] = (data.pop("PG_PORT", "") or "5432").strip()
        data["database"]["user"] = (data.pop("PG_USER", "") or "popularr").strip()
        data["database"]["password"] = data.pop("PG_PASSWORD", "")
        data["database"]["name"] = (data.pop("PG_DATABASE", "") or "popularr").strip()
        
        os.environ["PG_HOST"] = data["database"]["host"]
        os.environ["PG_PORT"] = data["database"]["port"]
        os.environ["PG_USER"] = data["database"]["user"]
        os.environ["PG_PASSWORD"] = data["database"]["password"]
        os.environ["PG_DATABASE"] = data["database"]["name"]

    success = save_partial_config(data)
    if success:
        return jsonify({"success": True, "message": "Progress saved"})
        
    return jsonify({"success": False, "error": "Failed to save config file"}), 500


# ===========================================================================
# STATIC PAGES
# ===========================================================================

@ui_bp.route("/")
def index() -> Any:
    return redirect(url_for("ui.dashboard"))


@ui_bp.route("/dashboard")
async def dashboard() -> Any:
    try:
        recent_scans = get_recent_album_scans(limit=10) or []
        cfg = get_config()
        nav_users = cfg.get("navidrome_users", [])
        
        if not nav_users and cfg.get("navidrome"):
            nav_users = [cfg["navidrome"]]
            
        features = cfg.get("features", {})

        try:
            with db_session() as session:
                result = session.execute(text("SELECT COUNT(*) as tc, COUNT(DISTINCT album) as ac, COUNT(DISTINCT COALESCE(NULLIF(album_artist, ''), artist)) as artists_c, ROUND(AVG(stars), 1) as avg_stars FROM tracks"))
                stats = dict(result.fetchone()._mapping)
        except Exception:
            stats = {"tc": 0, "ac": 0, "artists_c": 0, "avg_stars": None}

        try:
            with db_session() as session:
                result = session.execute(text("""
                    SELECT
                        COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                        album,
                        MAX(updated_at) AS added_at,
                        COUNT(*) AS track_count,
                        MAX(COALESCE(NULLIF(musicbrainz_albumtype, ''),
                                     NULLIF(spotify_album_type, ''))) AS album_type,
                        MAX(COALESCE(
                            NULLIF(SUBSTRING(COALESCE(year, '') FROM '^[0-9]{4}'), ''),
                            NULLIF(CAST(release_year AS TEXT), '')
                        )) AS album_year
                    FROM tracks
                    WHERE updated_at >= CURRENT_TIMESTAMP - INTERVAL '14 days'
                      AND album IS NOT NULL AND TRIM(album) <> ''
                      AND file_path IS NOT NULL AND TRIM(file_path) <> ''
                    GROUP BY
                        COALESCE(NULLIF(album_artist, ''), artist),
                        album,
                        COALESCE(
                            NULLIF(SUBSTRING(COALESCE(year, '') FROM '^[0-9]{4}'), ''),
                            NULLIF(CAST(release_year AS TEXT), '')
                        )
                    ORDER BY added_at DESC
                    LIMIT 12
                """))
                recent_albums = [dict(r._mapping) for r in result.fetchall()]
        except Exception:
            recent_albums = []

        return await render_template(
            "pages/dashboard.html",
            recent_scans=recent_scans,
            recent_albums=recent_albums,
            nav_users=nav_users,
            stats=stats,
            scan_running=False,
            perpetual=bool(features.get("perpetual", False)),
            forced=bool(features.get("force", False)),
            launch_on_startup=bool(features.get("launch_on_startup", False)),
            first_full_scan_done=True,
        )
    except Exception as exc:
        logger.error("Dashboard error", error=str(exc), exc_info=True)
        return await render_template("pages/dashboard.html", recent_scans=[], recent_albums=[], nav_users=[], stats={}, error=str(exc))


@ui_bp.route("/artists")
async def artists() -> Any:
    # Filing expression shared by the SQL ordering and the Python sort below.
    # "The Offspring" is filed as "offspring, the" so it sorts among the O's.
    _artist_expr = "COALESCE(NULLIF(album_artist, ''), artist)"
    _artist_order = artist_sort_sql(_artist_expr)

    with db_session() as session:
        result = session.execute(text(f"""
            SELECT
                {_artist_expr} AS canonical,
                COUNT(DISTINCT album) AS album_count,
                COUNT(*) AS track_count,
                COALESCE(SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count,
                MAX(last_scanned) AS last_updated
            FROM tracks
            WHERE {_artist_expr} IS NOT NULL
              AND {_artist_expr} != ''
            GROUP BY canonical
            HAVING COUNT(DISTINCT album) > 0
            ORDER BY {_artist_order}
        """))
        rows = [dict(r._mapping) for r in result.fetchall()]

    merged: dict[str, dict[str, Any]] = {}

    for row in rows:
        raw_name = row.get("canonical") or ""
        clean = strip_featured_artist(raw_name).strip().lower()

        if not clean:
            continue

        # Section letter comes from the CORE name, so "The Offspring" files
        # under O and the O section and the scan-letter action agree.
        sort_letter = artist_sort_letter(clean)

        if clean not in merged:
            merged[clean] = {
                "sort_key": artist_sort_key(clean),
                "sort_letter": sort_letter,
                "display_name": raw_name,
                "link_artist": raw_name,
                "sort_name": artist_sort_name(raw_name),
                "album_count": 0,
                "track_count": 0,
                "five_star_count": 0,
                "last_updated": row.get("last_updated"),
            }

        entry = merged[clean]

        if row.get("album_count", 0) > entry.get("album_count", 0):
            entry["display_name"] = raw_name
            entry["link_artist"] = raw_name

        entry["album_count"] += int(row.get("album_count") or 0)
        entry["track_count"] += int(row.get("track_count") or 0)
        entry["five_star_count"] += int(row.get("five_star_count") or 0)

        row_updated = row.get("last_updated")
        entry_updated = entry.get("last_updated")

        if row_updated and (not entry_updated or row_updated > entry_updated):
            entry["last_updated"] = row_updated

    # Re-derive the label from whichever spelling won the merge above.  Several
    # raw variants ("The Offspring", "the offspring ") collapse into one entry,
    # and display_name tracks the one with the most albums — so the label has to
    # follow it rather than being fixed when the entry was created.
    for _entry in merged.values():
        _entry["sort_name"] = artist_sort_name(_entry["display_name"])

    artists_data = sorted(
        merged.values(),
        key=lambda a: (
            0 if a["sort_letter"] == "#" else 1,
            a["sort_letter"],
            a["sort_key"],
        ),
    )

    with db_session() as session:
        result = session.execute(text("""
            SELECT
                COUNT(*) AS track_count,
                COUNT(DISTINCT album) AS album_count,
                COALESCE(
                    SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END),
                    0
                ) AS five_star_count
            FROM tracks
        """))
        row = result.fetchone()
        total_stats = dict(row._mapping) if row else {
            "track_count": 0,
            "album_count": 0,
            "five_star_count": 0,
        }

    artist_groups: list[dict[str, Any]] = []
    current_letter: str | None = None
    
    for artist in artists_data:
        letter = artist["sort_letter"]
        if letter != current_letter:
            current_letter = letter
            artist_groups.append({"letter": letter, "artists": [artist]})
        else:
            artist_groups[-1]["artists"].append(artist)

    return await render_template(
        "pages/artist_list.html",
        artists=artists_data,
        artist_groups=artist_groups,
        existing_group_letters={g["letter"] for g in artist_groups},
        total_artists=len(artists_data),
        total_stats=total_stats,
    )


@ui_bp.route("/artist/<path:name>")
async def artist_detail(name: str) -> Any:
    name = unquote(name or "").strip()
    cfg = get_config()

    def safe_float(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def safe_int(value: Any) -> int | None:
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def first_value(rows: list[dict[str, Any]], *keys: str) -> Any:
        for row in rows:
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    return value
        return None

    def split_tag_values(value: Any) -> list[str]:
        if not value:
            return []

        if isinstance(value, list):
            raw_items = value
        else:
            text_val = str(value)
            if text_val.startswith("["):
                try:
                    parsed = json.loads(text_val)
                    if isinstance(parsed, list):
                        raw_items = []
                        for item in parsed:
                            if isinstance(item, dict):
                                raw_items.append(str(item.get("name") or ""))
                            else:
                                raw_items.append(str(item))
                    else:
                        raw_items = re.split(r"[,;|\\]+", text_val)
                except Exception:
                    raw_items = re.split(r"[,;|\\]+", text_val)
            else:
                raw_items = re.split(r"[,;|\\]+", text_val)

        cleaned: list[str] = []
        seen: set[str] = set()

        for item in raw_items:
            tag = str(item).strip()
            if not tag:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(tag)

        return cleaned

    def collect_top_genres(rows: list[dict[str, Any]], limit: int = 30) -> list[str]:
        genre_fields = [
            "manual_genres", "navidrome_genres", "musicbrainz_genres",
            "spotify_genres", "discogs_genres", "lastfm_tags",
            "listenbrainz_genres", "essentia_genres", "mood",
        ]

        counts: dict[str, int] = {}
        display_names: dict[str, str] = {}

        for row in rows:
            for field in genre_fields:
                for genre in split_tag_values(row.get(field)):
                    key = genre.lower()
                    counts[key] = counts.get(key, 0) + 1
                    display_names.setdefault(key, genre)

        sorted_keys = sorted(
            counts.keys(),
            key=lambda k: (-counts[k], display_names[k].lower()),
        )

        return [display_names[k] for k in sorted_keys[:limit]]

    def classify_album(album_row: dict[str, Any]) -> str:
        return classify_album_type(album_row)

    with db_session() as session:
        result = session.execute(
            text("""
                SELECT *
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
                ORDER BY
                    LOWER(COALESCE(album, '')),
                    COALESCE(disc_number, '1'),
                    track_number,
                    title
            """),
            {"name": name},
        )
        tracks = [dict(r._mapping) for r in result.fetchall()]

        appears_result = session.execute(
            text("""
                SELECT *
                FROM tracks
                WHERE LOWER(COALESCE(artist, '')) = LOWER(:name)
                  AND LOWER(COALESCE(NULLIF(album_artist, ''), artist)) != LOWER(:name)
                ORDER BY
                    LOWER(COALESCE(album_artist, '')),
                    LOWER(COALESCE(album, '')),
                    COALESCE(disc_number, '1'),
                    track_number,
                    title
            """),
            {"name": name},
        )
        appears_tracks = [dict(r._mapping) for r in appears_result.fetchall()]

    albums_by_key: dict[str, dict[str, Any]] = {}

    def _leading_year(track: dict[str, Any]) -> int | None:
        raw = str(track.get("year") or "").strip()
        if raw[:4].isdigit():
            return int(raw[:4])
        try:
            rv = int(track.get("release_year") or 0) or None
            return rv
        except (TypeError, ValueError):
            return None

    for track in tracks:
        album_name = str(track.get("album") or "").strip()
        if not album_name:
            continue

        track_year = _leading_year(track)
        album_key = f"{album_name.lower().strip()}::{track_year or ''}"

        if album_key not in albums_by_key:
            albums_by_key[album_key] = {
                "album": album_name,
                "album_year": track_year,
                "track_count": 0,
                "avg_stars": None,
                "total_duration": 0,
                "last_updated": track.get("updated_at"),
                # The SPECIFIC release/edition held in the collection. The
                # release-GROUP name is ``album``; this is rendered as a
                # second line under the album name when it differs.
                "release_title": track.get("release_title"),
                "release_year": track.get("release_year"),
                "spotify_album_type": track.get("musicbrainz_albumtype")
                    or track.get("spotify_album_type")
                    or track.get("album_type"),
                "album_type": track.get("musicbrainz_albumtype")
                    or track.get("album_type")
                    or track.get("spotify_album_type"),
                "is_missing": False,
            }

        album_entry = albums_by_key[album_key]
        album_entry["track_count"] += 1

        duration = safe_float(track.get("duration"))
        if duration:
            album_entry["total_duration"] += duration

        year = _leading_year(track)
        if year and not album_entry.get("album_year"):
            album_entry["album_year"] = year

        # Backfill the edition name/year from any track that carries them —
        # the first track of an album is not necessarily the one the scan
        # enriched first.
        if not album_entry.get("release_title") and track.get("release_title"):
            album_entry["release_title"] = track.get("release_title")
        if not album_entry.get("release_year") and track.get("release_year"):
            album_entry["release_year"] = track.get("release_year")

        updated = track.get("updated_at")
        existing_updated = album_entry.get("last_updated")
        if updated and (not existing_updated or updated > existing_updated):
            album_entry["last_updated"] = updated

    for album_key, album_entry in albums_by_key.items():
        key_name = album_key.rsplit("::", 1)[0]
        key_year = album_entry.get("album_year")
        
        album_tracks = [
            track for track in tracks
            if str(track.get("album") or "").strip().lower() == key_name
            and (key_year is None or _leading_year(track) == key_year)
        ]

        stars = [
            safe_float(track.get("stars"))
            for track in album_tracks
            if safe_float(track.get("stars")) is not None
        ]

        album_entry["avg_stars"] = (
            round(sum(stars) / len(stars), 2) if stars else None
        )

        album_entry["tracks"] = sorted(
            album_tracks,
            key=lambda t: (
                safe_int(t.get("disc_number")) or 1,
                safe_int(t.get("track_number")) or 0,
                str(t.get("title") or "").lower(),
            ),
        )

    albums = sorted(
        albums_by_key.values(),
        key=lambda album: (
            album.get("album_year") is None,
            -(album.get("album_year") or 0),
            str(album.get("album") or "").lower(),
        ),
    )

    album_count = len(albums)
    track_count = len(tracks)

    star_values = [
        safe_float(track.get("stars"))
        for track in tracks
        if safe_float(track.get("stars")) is not None
    ]

    duration_values = [
        safe_float(track.get("duration"))
        for track in tracks
        if safe_float(track.get("duration")) is not None
    ]

    year_values = [
        safe_int(track.get("year"))
        for track in tracks
        if safe_int(track.get("year")) is not None
    ]

    five_star_count = sum(
        1 for track in tracks
        if safe_int(track.get("stars")) == 5
    )

    stats = {
        "track_count": track_count,
        "album_count": album_count,
        "avg_stars": round(sum(star_values) / len(star_values), 2) if star_values else None,
        "five_star_count": five_star_count,
        "total_duration": sum(duration_values) if duration_values else None,
        "earliest_year": min(year_values) if year_values else None,
        "latest_year": max(year_values) if year_values else None,
        "musicbrainz_artist_id": first_value(
            tracks,
            "musicbrainz_artist_id", "musicbrainz_artistid",
            "musicbrainz_albumartistid", "artist_mbid",
        ),
        "lastfm_artist_mbid": first_value(tracks, "lastfm_artist_mbid", "lastfm_mbid"),
        "discogs_artist_id": first_value(tracks, "discogs_artist_id", "discogs_artistid"),
    }

    top_tracks = sorted(
        tracks,
        key=lambda track: (
            safe_float(track.get("artist_z_score"))
            if safe_float(track.get("artist_z_score")) is not None
            else safe_float(track.get("final_score"))
            if safe_float(track.get("final_score")) is not None
            else safe_float(track.get("stars"))
            if safe_float(track.get("stars")) is not None
            else 0
        ),
        reverse=True,
    )[:20]

    for track in top_tracks:
        if track.get("artist_z_score") is None:
            track["artist_z_score"] = track.get("final_score") or 0

        if track.get("popularity_score") is None:
            track["popularity_score"] = track.get("final_score") or 0

        if track.get("is_single") is None:
            track["is_single"] = 0

        if track.get("file_path") is None:
            track["file_path"] = ""

    genre_rows = tracks
    genre_sources = {}
    
    try:
        genre_sources = get_artist_genre_sources(genre_rows)
    except Exception as exc:
        logger.debug("Failed to aggregate artist genre sources", error=str(exc))

    genres = collect_top_genres(tracks)

    missing_entries: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            missing_result = session.execute(
                text("""
                    SELECT title, release_id, primary_type, first_release_date,
                           cover_art_url, category
                    FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:name)
                    ORDER BY first_release_date DESC NULLS LAST
                """),
                {"name": name},
            )
            
            for row in missing_result.fetchall():
                mr = dict(row._mapping)
                mr_title = str(mr.get("title") or "").strip()
                if not mr_title:
                    continue
                    
                album_key = mr_title.lower()
                if album_key in albums_by_key:
                    continue

                release_year = None
                first_release = str(mr.get("first_release_date") or "")
                
                if len(first_release) == 4 and first_release.isdigit():
                    release_year = safe_int(first_release)
                elif first_release and len(first_release) >= 4:
                    try:
                        release_year = safe_int(first_release[:4])
                    except Exception:
                        pass

                missing_entry = {
                    "album": mr_title,
                    "title": mr_title,
                    "album_year": release_year,
                    "track_count": 0,
                    "avg_stars": None,
                    "total_duration": 0,
                    "is_missing": True,
                    "is_upcoming": bool(
                        release_year and release_year > datetime.now().year
                    ),
                    "first_release_date": first_release,
                    "cover_art_url": mr.get("cover_art_url") or "",
                    "release_id": mr.get("release_id") or "",
                }

                category = str(mr.get("category") or "").strip()
                if category:
                    # ``normalise_category`` resolves canonical keys, legacy
                    # display labels ("Live Album") AND composite type strings,
                    # so rows written before the category registry existed land
                    # in the right section instead of defaulting to Studio.
                    missing_entry["_category"] = normalise_category(category)
                else:
                    # No stored category: derive it from the release-group type
                    # rather than guessing "album", which is what put field
                    # recordings and DJ-mixes under Studio Albums.
                    missing_entry["_category"] = category_for_musicbrainz(
                        str(mr.get("primary_type") or ""),
                        mr.get("secondary_types") or "",
                    )

                missing_entries.append(missing_entry)
    except Exception as exc:
        logger.debug("Failed to load missing releases", artist=name, error=str(exc))

    all_albums = albums + missing_entries
    all_albums.sort(
        key=lambda a: (
            a.get("album_year") is None,
            -(a.get("album_year") or 0),
            str(a.get("album") or a.get("title") or "").lower(),
        ),
    )

    # Buckets are built from the DATA, not a fixed list: a category exists only
    # if something is in it, so an artist with no field recordings never grows a
    # "Field Recordings" section.  ``album_categories`` carries the ordered
    # (key, label, icon) list the template iterates, so section order is
    # decided by the registry rather than by row order.
    albums_by_category: dict[str, list[dict[str, Any]]] = {}
    for album_entry in all_albums:
        category = normalise_category(
            album_entry.get("_category") or classify_album(album_entry)
        )
        albums_by_category.setdefault(category, []).append(album_entry)

    album_categories = ordered_specs(albums_by_category.keys())

    appears_by_key: dict[str, dict[str, Any]] = {}

    for track in appears_tracks:
        album_name = str(track.get("album") or "").strip()
        album_artist = str(track.get("album_artist") or track.get("artist") or "").strip()

        if not album_name:
            continue

        appears_year = _leading_year(track)
        key = f"{album_artist.lower()}::{album_name.lower()}::{appears_year or ''}"

        if key not in appears_by_key:
            appears_by_key[key] = {
                "album": album_name,
                "album_artist": album_artist,
                "album_year": appears_year,
                "track_count": 0,
                "avg_stars": None,
                "is_missing": False,
            }

        entry = appears_by_key[key]
        entry["track_count"] += 1

        year = _leading_year(track)
        if year and not entry.get("album_year"):
            entry["album_year"] = year

    for key, entry in appears_by_key.items():
        album_artist_key, album_key, year_key = key.split("::", 2)
        entry_year = entry.get("album_year")

        matching_tracks = [
            track for track in appears_tracks
            if str(track.get("album") or "").strip().lower() == album_key
            and str(track.get("album_artist") or track.get("artist") or "").strip().lower() == album_artist_key
            and (entry_year is None or _leading_year(track) == entry_year)
        ]

        stars = [
            safe_float(track.get("stars"))
            for track in matching_tracks
            if safe_float(track.get("stars")) is not None
        ]

        entry["avg_stars"] = round(sum(stars) / len(stars), 2) if stars else None

    appears_on_albums = sorted(
        appears_by_key.values(),
        key=lambda item: (
            item.get("album_year") is None,
            -(item.get("album_year") or 0),
            str(item.get("album_artist") or "").lower(),
            str(item.get("album") or "").lower(),
        ),
    )

    artist_bio = first_value(
        tracks,
        "artist_bio", "bio", "lastfm_bio", "musicbrainz_bio",
    ) or ""

    if not artist_bio:
        try:
            with db_session() as session:
                row = session.execute(
                    text("SELECT bio FROM artists WHERE name = :name AND bio IS NOT NULL AND bio != '' LIMIT 1"),
                    {"name": name},
                ).fetchone()
                if row:
                    artist_bio = str(row[0])
        except Exception:
            pass

    artist_country = first_value(
        tracks,
        "artist_country", "country", "origin_country", "musicbrainz_country",
    ) or ""

    if not artist_country:
        try:
            with db_session() as session:
                row = session.execute(
                    text("SELECT country FROM artists WHERE name = :name AND country IS NOT NULL AND country != '' LIMIT 1"),
                    {"name": name},
                ).fetchone()
                if row:
                    artist_country = str(row[0])
        except Exception:
            pass

    artist_members_value = first_value(
        tracks, "artist_members", "musicbrainz_members", "members",
    )

    artist_members: list[Any] = []

    if isinstance(artist_members_value, list):
        artist_members = artist_members_value
    elif isinstance(artist_members_value, str) and artist_members_value.strip():
        try:
            parsed_members = json.loads(artist_members_value)
            if isinstance(parsed_members, list):
                artist_members = parsed_members
        except Exception:
            artist_members = []

    if not artist_members:
        try:
            artist_members = get_artist_members_cached(name)
        except Exception as exc:
            logger.debug("Failed to fetch artist members", artist=name, error=str(exc))

    similar_artists: dict[str, list[dict]] = {"lastfm": [], "listenbrainz": [], "display": []}
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT similar_artists_lastfm, similar_artists_listenbrainz FROM artists WHERE name = :name"),
                {"name": name},
            ).fetchone()
            
            if row:
                lf_raw = str(row[0] or "") if row[0] else ""
                lb_raw = str(row[1] or "") if row[1] else ""
                if lf_raw:
                    try:
                        similar_artists["lastfm"] = json.loads(lf_raw)
                    except Exception:
                        pass
                if lb_raw:
                    try:
                        similar_artists["listenbrainz"] = json.loads(lb_raw)
                    except Exception:
                        pass
                        
            for source in ("lastfm", "listenbrainz"):
                similar_artists[source] = _similar_artist_display_list(
                    session, similar_artists[source]
                )
                
            merged: dict[str, dict] = {}
            for source in ("lastfm", "listenbrainz"):
                for item in similar_artists[source]:
                    key = item["name"].lower()
                    if key not in merged:
                        entry = dict(item)
                        entry["sources"] = []
                        merged[key] = entry
                    merged[key]["sources"].append(source)
                    if item.get("in_collection"):
                        merged[key]["in_collection"] = True
                        
            similar_artists["display"] = list(merged.values())
    except Exception as exc:
        logger.debug("Failed to load similar artists", artist=name, error=str(exc))

    return await render_template(
        "pages/artist_detail_v2.html",
        artist_name=name,
        albums=albums,
        stats=stats,
        top_tracks=top_tracks,
        genre_sources=genre_sources,
        genres=genres,
        albums_by_category=albums_by_category,
        # Ordered (key, label, icon) specs for the sections that actually have
        # content — the template loops this instead of hard-coding sections.
        album_categories=album_categories,
        appears_on_albums=appears_on_albums,
        artist_bio=artist_bio,
        artist_country=artist_country,
        artist_members=artist_members,
        similar_artists=similar_artists,
        slskd_config=cfg.get("slskd", {}),
    )


def _coerce_track_numerics(track: dict[str, Any]) -> dict[str, Any]:
    """Coerce numeric track fields to float/int (or None) for safe Jinja rounds."""
    float_fields = (
        "duration", "final_score", "popularity", "lastfm_score",
        "listenbrainz_score", "bpm", "loudness_lufs", "replaygain",
        "bitrate", "sample_rate",
    )
    int_fields = ("stars",)
    
    for field in int_fields:
        value = track.get(field)
        if value in (None, ""):
            track[field] = None
            continue
        try:
            track[field] = int(float(value))
        except (TypeError, ValueError):
            track[field] = None
            
    for field in float_fields:
        value = track.get(field)
        if value in (None, ""):
            track[field] = None
            continue
        try:
            track[field] = float(value)
        except (TypeError, ValueError):
            track[field] = None
            
    return track


def _values_equal(a: Any, b: Any) -> bool:
    """Compare a DB value and an incoming form/API value loosely."""
    a = "" if a is None else str(a).strip()
    b = "" if b is None else str(b).strip()
    return a == b


@ui_bp.route("/album/<path:album_path>", methods=["GET", "POST"])
async def album_detail(album_path: str) -> Any:
    raw_path = str(album_path or "")
    parts = [p for p in raw_path.split("/") if p != ""]
    
    if len(parts) >= 3:
        artist = "/".join(parts[:-2])
        album = parts[-2]
        year_seg = parts[-1]
    else:
        artist, sep, album = raw_path.rpartition("/")
        if not sep:
            artist, album = raw_path, ""
        year_seg = ""
        
    artist_name = unquote(artist or "").strip()
    album_name = unquote(album or "").strip()
    album_year_seg = unquote(year_seg or "").strip()
    
    try:
        album_year_filter = int(album_year_seg) if album_year_seg.isdigit() else None
    except (TypeError, ValueError):
        album_year_filter = None
        
    if album_year_filter is not None and not (1900 <= album_year_filter <= 2100):
        album_year_filter = None
        
    cfg = get_config()

    with db_session() as session:
        result = session.execute(
            text("""
                SELECT *
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                  AND LOWER(COALESCE(album, '')) = LOWER(:album)
                ORDER BY
                    COALESCE(disc_number, '1'),
                    NULLIF(regexp_replace(COALESCE(track_number::text, ''), '[^0-9].*$', ''), '')::int NULLS LAST,
                    track_number,
                    title
            """),
            {"artist": artist_name, "album": album_name},
        )
        tracks = [dict(r._mapping) for r in result.fetchall()]

    def _track_year(t: dict[str, Any]) -> int | None:
        raw = str(t.get("year") or "").strip()
        if raw[:4].isdigit():
            return int(raw[:4])
        try:
            rv = int(t.get("release_year") or 0) or None
            return rv
        except (TypeError, ValueError):
            return None

    # ── Same-name albums across years ────────────────────────────────────
    # When the URL has NO year segment and the (artist, album) matches tracks
    # from MULTIPLE distinct years (a band re-releasing an album, or two
    # different albums sharing a name), the tracks are NOT merged into one
    # page — each year is its own album.  Default to the most recent year so
    # the page matches the latest release; a year selector links to the
    # year-scoped URLs (``/album/<artist>/<album>/<year>``).
    all_album_years = sorted(
        {y for y in (_track_year(t) for t in tracks) if y is not None},
        reverse=True,
    )
    if album_year_filter is None:
        if len(all_album_years) > 1:
            album_year_filter = all_album_years[0]

    if album_year_filter is not None and tracks:
        year_tracks = [t for t in tracks if _track_year(t) == album_year_filter]
        if year_tracks:
            tracks = year_tracks

    tracks = [_coerce_track_numerics(t) for t in tracks]

    if request.method == "POST":
        form = await request.form

        new_title = (form.get("album_title") or "").strip()
        new_artist = (form.get("album_artist") or "").strip()
        release_year = (form.get("release_year") or "").strip()
        # The album's ORIGINAL year (the release group's first release), which
        # is what ``year`` stores so a reissue groups with the original
        # pressing.  The edition's own year is ``release_year``.
        original_year = (form.get("album_originalyear") or "").strip()
        # SPECIFIC release/edition name, e.g. "Experience: Expanded
        # (Remixes and B-Sides)".  Distinct from the release-GROUP name, which
        # lives in the album's ``album`` column.
        release_title = (form.get("album_release_title") or "").strip()
        album_type = (form.get("album_type") or "").strip()
        track_artist = (form.get("track_artist") or "").strip()
        track_composer = (form.get("track_composer") or "").strip()
        track_comment = (form.get("track_comment") or "").strip()
        album_mbid = (form.get("album_mbid") or "").strip()
        album_rg_mbid = (form.get("album_release_group_mbid") or "").strip()
        discogs_id = (form.get("album_discogs_id") or "").strip()
        artist_mbid = (form.get("artist_mbid") or "").strip()
        genres_str = (form.get("album_genres") or "").strip()
        cover_url = (form.get("cover_art_url") or "").strip()

        release_fields = [
            "recordlabel", "catalognumber", "barcode", "asin", "releasedate",
            "media", "releasetype", "releasestatus", "releasecountry", "copyright",
            "language", "explicitstatus", "originalyear", "originaldate",
            "tracktotal", "disctotal", "script", "discsubtitle", "albumversion",
        ]
        release_values = {
            f: (form.get(f"album_{f}") or "").strip()
            for f in release_fields
        }

        # Disc Total drives per-track disc_number:
        # - disctotal > 1  → multi-disc release; leave each track's disc_number
        #   as-is (or re-derive below when a value is present), it is valid.
        # - disctotal <= 1 (or empty/"0"/"1") → SINGLE-disc: strip disc_number
        #   from every track so Navidrome/file tags don't carry a bogus "1/x"
        #   or "0/x" disc position (the reported "disc 1 and disc 0" split AND
        #   the "one track has disc 1, the rest are empty — keeps the 1" bug).
        _disc_total_raw = release_values.get("disctotal") or ""
        _strip_disc_numbers = False
        _multi_disc = False
        try:
            _disc_total = int(_disc_total_raw)
            _multi_disc = _disc_total > 1
            # A "0" or "1" (or negative/absurd) disctotal is a SINGLE disc —
            # never a multi-disc release.  Empty disctotal defaults to
            # single-disc stripping too (the overwhelming case).
            _strip_disc_numbers = _disc_total <= 1
        except (TypeError, ValueError):
            # Empty or non-numeric disctotal: if it was explicitly provided as
            # a single value, treat as single-disc (strip); otherwise leave
            # tracks untouched.
            _strip_disc_numbers = bool(_disc_total_raw)

        # ── Disc-number inference when disctotal is empty ─────────────────
        # An album whose tracks only carry disc 1 (or mixed "1" + empty) is a
        # SINGLE-disc release — the stray "1" must be cleared so it does not
        # keep rendering/being stored.  Only actual multi-disc evidence
        # (any track with disc_number > 1) keeps the disc numbers.
        if not _disc_total_raw and not _multi_disc:
            try:
                _max_track_disc = max(
                    (int(str(t.get("disc_number") or "").split("/")[0].strip() or 0)
                     for t in tracks
                     if str(t.get("disc_number") or "").strip()),
                    default=0,
                )
            except Exception:
                _max_track_disc = 0
            if _max_track_disc <= 1:
                # Mixed "1"/empty disc numbers on an album with no disctotal
                # set = single-disc: strip the bogus "1"s.
                _strip_disc_numbers = True
            elif _max_track_disc > 1:
                _multi_disc = True

        # ── MusicBrainz album-artist backfill ─────────────────────────────
        # The release picker populates ``album_mbid`` (release) + the release
        # group MBID, but NOT the album-artist MBID / release type / status /
        # country — so saving writes only a subset of the MB tags, and tracks
        # that never got MB-enriched stay missing the album-level tags
        # Navidrome needs to merge them into one album.  When a release MBID
        # is present, fetch the release's artist credit and backfill the
        # album-artist MBID (plus other MB album fields) onto EVERY track so
        # the file tags are complete and consistent.
        _mb_albumartist_mbid = ""
        _mb_albumtype = album_type
        _mb_albumstatus = release_values.get("releasestatus") or ""
        _mb_releasecountry = release_values.get("releasecountry") or ""
        _mb_originalyear = release_values.get("originalyear") or ""
        # recording_mbid → {writer, is_cover, original_cover_artist,
        # musicbrainz_genres, work_mbid} — used to update covers + writers on
        # every track of the album when saving from the release picker.
        _mb_track_map: dict[str, dict[str, Any]] = {}
        if album_mbid:
            try:
                from services.enrichment.musicbrainz_service import (
                    build_artist_credit_string,
                    fetch_musicbrainz_release_metadata,
                    primary_album_artist,
                )
                _mb_release = fetch_musicbrainz_release_metadata(album_mbid)
                if _mb_release:
                    # Re-fetch raw release for artist-credit MBIDs (the
                    # metadata helper returns display names only).
                    from services.enrichment.musicbrainz_service import get_shared_mb_client
                    _raw = get_shared_mb_client().get_release(
                        album_mbid, inc="artist-credits+release-groups",
                    )
                    if _raw:
                        _credit = _raw.get("artist-credit") or []
                        if _credit:
                            _first = _credit[0]
                            _art = _first.get("artist") or {}
                            if isinstance(_art, dict):
                                _mb_albumartist_mbid = str(_art.get("id") or "")
                        if not _mb_albumtype:
                            _mb_albumtype = (
                                ((_raw.get("release-group") or {}).get("primary-type") or "")
                                or album_type
                            )
                        if not _mb_albumstatus:
                            _mb_albumstatus = str(_raw.get("status") or "")
                        if not _mb_releasecountry:
                            _mb_releasecountry = str(_raw.get("country") or "")
                        if not _mb_originalyear:
                            _mb_originalyear = (
                                ((_raw.get("release-group") or {}).get("first-release-date") or "")
                            )[:4]

                    # Per-recording enrichment: the enriched fetch includes
                    # writer / cover / genre data per track.
                    for _mt in (_mb_release.get("tracks") or []):
                        _rmbid = str(_mt.get("recording_mbid") or "").strip()
                        if not _rmbid:
                            continue
                        _entry: dict[str, Any] = {}
                        if _mt.get("writer"):
                            _entry["writer"] = _mt["writer"]
                        if _mt.get("is_cover"):
                            _entry["is_cover"] = True
                            if _mt.get("original_cover_artist"):
                                _entry["original_cover_artist"] = _mt["original_cover_artist"]
                        if _mt.get("musicbrainz_genres"):
                            _entry["musicbrainz_genres"] = _mt["musicbrainz_genres"]
                        if _mt.get("work_mbid"):
                            _entry["work_mbid"] = _mt["work_mbid"]
                        if _entry:
                            _mb_track_map[_rmbid] = _entry
            except Exception as _mb_exc:
                logger.debug("Album MB backfill failed", error=str(_mb_exc))

        updated_count = 0
        reverted_live_count = 0
        file_sync_failures = 0

        for track in tracks:
            track_id = track.get("id")
            if not track_id:
                continue

            payload: dict[str, Any] = {"id": track_id}

            if new_title and new_title != track.get("album"):
                payload["album"] = new_title

            if new_artist and new_artist != (track.get("album_artist") or track.get("artist")):
                payload["album_artist"] = new_artist
                payload["artist"] = new_artist

            # Backfilled MB album metadata — applied to EVERY track so the
            # audio files carry the complete, consistent tag set.
            if _mb_albumartist_mbid:
                payload["musicbrainz_albumartistid"] = _mb_albumartist_mbid
            if _mb_albumtype:
                payload["musicbrainz_albumtype"] = _mb_albumtype
            if _mb_albumstatus:
                payload["musicbrainz_albumstatus"] = _mb_albumstatus
            if _mb_releasecountry:
                payload["releasecountry"] = _mb_releasecountry
            if _mb_originalyear:
                payload["originalyear"] = _mb_originalyear

            # ── Per-track MusicBrainz enrichment (writer / cover / genre) ─
            # Tracks that have a related WORK (via the recording's work-rels)
            # get their writer(s) updated; tracks whose work is by a DIFFERENT
            # artist are covers and are marked ``is_cover`` with the original
            # artist attributed.  MB genres are also applied per recording.
            _cover_original_artist: str | None = None
            _cover_renamed_title: str | None = None
            if _mb_track_map:
                _rec_mbid = str(track.get("recording_mbid") or "").strip()
                _mb_tt = _mb_track_map.get(_rec_mbid)
                if _mb_tt:
                    if _mb_tt.get("writer") and not track_composer:
                        payload["writer"] = _mb_tt["writer"]
                    if _mb_tt.get("is_cover"):
                        payload["is_cover"] = 1
                        _cover_original_artist = str(_mb_tt.get("original_cover_artist") or "").strip()
                        if _cover_original_artist:
                            payload["original_cover_artist"] = _cover_original_artist
                    if _mb_tt.get("musicbrainz_genres"):
                        payload["musicbrainz_genres"] = _mb_tt["musicbrainz_genres"]
                    if _mb_tt.get("work_mbid"):
                        payload["musicbrainz_workid"] = _mb_tt["work_mbid"]

            # ── Cover rename + genre (same convention as the cover-detection
            #    area) ─────────────────────────────────────────────────────
            # A confirmed cover is renamed to "Title (Original Artist Cover)"
            # and gets the "Cover" genre, matching the standalone cover
            # detector so the whole library uses one convention.
            if payload.get("is_cover") and _cover_original_artist:
                _cur_title = str(track.get("title") or "").strip()
                if _cur_title and "cover)" not in _cur_title.lower():
                    _cover_renamed_title = f"{_cur_title} ({_cover_original_artist} Cover)"
                    payload["title"] = _cover_renamed_title

            # The two years are DISTINCT and stored separately:
            #   year          -> the album's ORIGINAL year (the release group's
            #                    first release), so a reissue groups with the
            #                    original pressing.
            #   release_year  -> THIS edition's year.
            # Falling back to release_year preserves the old behaviour for a
            # library that carries only a single date.
            if original_year:
                payload["year"] = original_year
            elif release_year:
                payload["year"] = release_year

            if release_year:
                try:
                    payload["release_year"] = int(release_year)
                except ValueError:
                    pass

            if release_title:
                payload["release_title"] = release_title

            if album_type:
                payload["spotify_album_type"] = album_type
                payload["musicbrainz_albumtype"] = album_type

            if track_artist and track_artist != track.get("artist"):
                payload["artist"] = track_artist
            if track_composer:
                payload["writer"] = track_composer

            if album_mbid:
                payload["musicbrainz_album_mbid"] = album_mbid
                payload["musicbrainz_albumid"] = album_mbid
            if album_rg_mbid:
                payload["musicbrainz_releasegroupid"] = album_rg_mbid
            if artist_mbid:
                payload["musicbrainz_artistid"] = artist_mbid
            if discogs_id:
                payload["discogs_album_id"] = discogs_id

            if cover_url:
                payload["cover_art_url"] = cover_url

            for field, value in release_values.items():
                if value:
                    payload[field] = value

            # Single-disc albums: strip the per-track disc_number so neither
            # the DB nor the audio file tags carry a bogus disc position.
            # Only set this when the payload is otherwise being written (the
            # field is TEXT; an empty string clears the frame on file write).
            if _strip_disc_numbers:
                _cur_disc = str(track.get("disc_number") or "").strip()
                # A "0" disc is a bogus single-disc value — always clear it.
                if _cur_disc and _cur_disc != "0":
                    payload["disc_number"] = ""
                elif _cur_disc == "0":
                    payload["disc_number"] = ""
            elif _multi_disc:
                # Multi-disc: ensure every track has a non-empty disc_number
                # (default to "1" when unset, and never "0") so the album
                # displays correctly.
                _cur_disc = str(track.get("disc_number") or "").strip()
                if not _cur_disc or _cur_disc == "0":
                    payload["disc_number"] = "1"

            if genres_str:
                genres_list = [
                    g.strip()
                    for g in re.split(r"[,;/\\]+", genres_str)
                    if g.strip()
                ]
                genres_str_clean = ", ".join(genres_list)
                
                if genres_list:
                    from db.repositories.metadata import update_track_genres
                    update_track_genres(track_id=track_id, genres_str=genres_str_clean)
                    
                    file_path = resolve_music_file_path(track.get("file_path"))
                    if file_path:
                        try:
                            update_file_tags(file_path, {"genres": genres_list})
                        except Exception as tag_err:
                            logger.debug("Tag write failed", track_id=track_id, error=str(tag_err))

            if len(payload) > 1:
                try:
                    insert_or_update_track(track_id, payload)
                    updated_count += 1
                except Exception as db_err:
                    logger.debug("DB update failed", track_id=track_id, error=str(db_err))

            _resolved_file = resolve_music_file_path(track.get("file_path"))
            _file_write_ok = False
            
            if _resolved_file:
                try:
                    _file_tags = build_tag_updates(payload)
                    # Cover convention: add the "Cover" genre to the file
                    # tags (mirrors the standalone cover-detection area).
                    if payload.get("is_cover"):
                        _existing_genres = _file_tags.get("genres") or _file_tags.get("genre")
                        if isinstance(_existing_genres, list):
                            if "cover" not in [str(g).lower() for g in _existing_genres]:
                                _file_tags["genre"] = "; ".join([str(g) for g in _existing_genres] + ["Cover"])
                        else:
                            _file_tags["genre"] = "Cover"
                    # Single-disc strip: build_tag_updates drops EMPTY values,
                    # so push disc_number="" explicitly to clear the frame.
                    if _strip_disc_numbers:
                        _file_tags["disc_number"] = ""
                    if _file_tags:
                        _file_write_ok = bool(update_file_tags(_resolved_file, _file_tags))
                except Exception as tag_err:
                    logger.debug("File tag write failed", track_id=track_id, error=str(tag_err))
                    
            if not _file_write_ok:
                file_sync_failures += 1
                logger.warning(
                    "File tag write SKIPPED - DB updated only",
                    track_id=track_id, file_path=_resolved_file,
                )

            if album_type and "+live" not in album_type.lower() and "(live)" not in album_type.lower():
                try:
                    if revert_track_live_state(str(track_id)):
                        reverted_live_count += 1
                except Exception as revert_err:
                    logger.debug("Live-state revert failed", track_id=track_id, error=str(revert_err))

        # ── Album-level cover art: download + embed ──────────────────────
        # The MB lookup fills ``cover_art_url`` (a CAA URL string).  Saving
        # the album must actually DOWNLOAD the image and embed it into the
        # track files (and store it in album_art) so Navidrome picks it up on
        # the next scan — previously only the URL string was stored.
        _cover_embedded = False
        if cover_url:
            try:
                from services.enrichment.album_art_service import (
                    apply_album_art_to_tracks,
                    save_album_art_to_db,
                )
                _cover_bytes: bytes | None = None
                _cover_mime = "image/jpeg"
                if str(cover_url).startswith("data:"):
                    import base64 as _b64
                    _header, _, _b64p = cover_url.partition(",")
                    _cover_mime = _header[5:].split(";")[0] or "image/jpeg"
                    try:
                        _cover_bytes = _b64.b64decode(_b64p)
                    except Exception:
                        _cover_bytes = None
                elif str(cover_url).startswith("http"):
                    # A plain HTTP(S) cover URL — download the image directly
                    # (the reported gap: a URL-set cover never reached the
                    # album page image or the audio files because only data:
                    # URLs and CAA-via-MBID were handled).
                    import httpx as _httpx
                    try:
                        _resp = _httpx.get(cover_url, timeout=10)
                        if _resp.status_code == 200 and _resp.content:
                            _cover_bytes = _resp.content
                            _cover_mime = _resp.headers.get("content-type", "image/jpeg") or "image/jpeg"
                    except Exception as _http_exc:
                        logger.debug("Album cover URL download failed", url=cover_url, error=str(_http_exc))
                elif album_mbid:
                    # Prefer Cover Art Archive via the release MBID.
                    from api_clients.coverartarchive import get_release_front_image_bytes
                    _cover_bytes = get_release_front_image_bytes(album_mbid, size="500")
                    if _cover_bytes is None and album_rg_mbid:
                        from api_clients.coverartarchive import get_release_group_front_image_bytes
                        _cover_bytes = get_release_group_front_image_bytes(album_rg_mbid, size="500")
                if _cover_bytes:
                    save_album_art_to_db(artist_name, album_name, _cover_bytes, source="url", mime_type=_cover_mime)
                    _cover_embedded = apply_album_art_to_tracks(
                        new_artist or artist_name,
                        new_title or album_name,
                        _cover_bytes,
                        _cover_mime,
                    ) > 0
            except Exception as _cover_exc:
                logger.debug("Album save cover embed failed", artist=artist_name, album=album_name, error=str(_cover_exc))

        if updated_count > 0:
            await flash(f"Album metadata saved — {updated_count} track(s) updated.", "success")
        if file_sync_failures > 0:
            await flash(
                f"⚠️ {file_sync_failures} track(s) updated in the database but NOT in the audio "
                "files (could not write tags).",
                "warning",
            )
        if reverted_live_count > 0:
            await flash(f"Removed \"(Live)\"/\"(Acoustic)\" suffixes from {reverted_live_count} track(s).", "info")
        if _cover_embedded:
            await flash("🎨 Album cover art downloaded and embedded into track files.", "success")
            
        if updated_count == 0 and reverted_live_count == 0 and file_sync_failures == 0:
            await flash("No changes were made.", "info")

        redirect_artist = new_artist or artist_name
        redirect_album = new_title or album_name
        redirect_year = f"/{album_year_filter}" if album_year_filter is not None else ""
        return redirect(url_for("ui.album_detail", album_path=f"{redirect_artist}/{redirect_album}{redirect_year}"))

    first_track = tracks[0] if tracks else {}

    def first_value(*keys: str) -> Any:
        for row in tracks:
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    return value
        return None

    def split_tag_values(value: Any) -> list[str]:
        if not value:
            return []

        if isinstance(value, list):
            raw_items = value
        else:
            text_val = str(value)
            if text_val.startswith("["):
                try:
                    parsed = json.loads(text_val)
                    if isinstance(parsed, list):
                        raw_items = []
                        for item in parsed:
                            if isinstance(item, dict):
                                raw_items.append(str(item.get("name") or ""))
                            else:
                                raw_items.append(str(item))
                    else:
                        raw_items = re.split(r"[,;|\\]+", text_val)
                except Exception:
                    raw_items = re.split(r"[,;|\\]+", text_val)
            else:
                raw_items = re.split(r"[,;|\\]+", text_val)

        cleaned: list[str] = []
        seen: set[str] = set()

        for item in raw_items:
            tag = str(item).strip()
            if not tag:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(tag)

        return cleaned

    def collect_album_genres() -> list[str]:
        genre_fields = [
            "manual_genres", "navidrome_genres", "musicbrainz_genres",
            "spotify_genres", "discogs_genres", "lastfm_tags",
            "listenbrainz_genres", "essentia_genres", "mood",
        ]

        # Consolidation: apply the same synonym normalisation the genre
        # aggregation service uses (e.g. "Hip Hop" → "hip-hop", "R&B" /
        # "Rhythm and Blues" → "rnb") so the album page never shows
        # near-duplicate genre chips ("Hip Hop" + "Hip-Hop", "R&B" + "RnB").
        try:
            from services.enrichment.genre_aggregation_service import (
                is_admin_genre,
                is_junk_genre,
                normalize_genre,
            )
        except Exception:
            is_admin_genre = lambda g: False
            is_junk_genre = lambda g: False
            normalize_genre = lambda g: str(g or "").lower().strip()

        genres: list[str] = []
        seen: set[str] = set()

        for row in tracks:
            for field in genre_fields:
                for genre in split_tag_values(row.get(field)):
                    if is_admin_genre(genre) or is_junk_genre(genre):
                        continue
                    # Canonical display name (synonym-resolved, original case
                    # preserved where possible).
                    canon = normalize_genre(genre)
                    # Dedupe key ignores separators/punctuation so
                    # "hip-hop" == "hip hop" == "hiphop".
                    key = re.sub(r"[^a-z0-9]+", "", canon)
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    genres.append(genre if genre else canon)
        return genres

    def safe_float(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def safe_int(value: Any) -> int | None:
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None
            
    durations = [
        safe_float(track.get("duration"))
        for track in tracks
        if safe_float(track.get("duration")) is not None
    ]

    star_values = [
        safe_float(track.get("stars"))
        for track in tracks
        if safe_float(track.get("stars")) is not None
    ]

    disc_values = [
        safe_int(track.get("disc_number"))
        for track in tracks
        if safe_int(track.get("disc_number")) is not None
        and safe_int(track.get("disc_number")) >= 1
    ]

    singles_count = sum(
        1
        for track in tracks
        if safe_int(track.get("is_single")) == 1
        and str(track.get("single_confidence") or "").strip().lower() == "high"
    )

    album_data = {
        **first_track,
        "track_count": len(tracks),
        "avg_stars": round(sum(star_values) / len(star_values), 2) if star_values else None,
        "total_duration": sum(durations) if durations else None,
        "total_discs": max(disc_values) if disc_values else 1,
        "singles_count": singles_count,
        "spotify_release_date": first_value("spotify_release_date", "release_date", "date"),
        "spotify_album_type": first_value("musicbrainz_albumtype", "spotify_album_type", "album_type"),
        "record_label": first_value("record_label", "label"),
        "catalog_number": first_value("catalog_number", "catalog"),
        "recordlabel": first_value("recordlabel"),
        "catalognumber": first_value("catalognumber", "catalog_number", "catalog"),
        "barcode": first_value("barcode"),
        "asin": first_value("asin"),
        "releasedate": first_value("releasedate", "release_date", "date"),
        "media": first_value("media"),
        "releasetype": first_value("releasetype", "album_type", "musicbrainz_albumtype"),
        "releasestatus": first_value("releasestatus"),
        "releasecountry": first_value("releasecountry"),
        "copyright": first_value("copyright"),
        "language": first_value("language"),
        "explicitstatus": first_value("explicitstatus"),
        "originalyear": first_value("originalyear"),
        "originaldate": first_value("originaldate"),
        "tracktotal": first_value("tracktotal"),
        "disctotal": first_value("disctotal"),
        "discsubtitle": first_value("discsubtitle"),
        "albumversion": first_value("albumversion"),
        "script": first_value("script"),
        # Album-level years/names. Declared explicitly (rather than relying on
        # the ``**first_track`` spread) so every track agrees on the same
        # value instead of inheriting whichever track happened to be first.
        #   year          the album's ORIGINAL year
        #   release_year  this specific edition's year
        "year": first_value("year"),
        "release_year": first_value("release_year"),
        # The SPECIFIC release/edition name. Shown as a tagline beneath the
        # album title only when it differs from the release-group name.
        "release_title": first_value("release_title"),
        "last_scanned": first_value("last_scanned", "updated_at", "created_at"),
        "musicbrainz_album_mbid": first_value("musicbrainz_album_mbid", "musicbrainz_releaseid", "musicbrainz_albumid"),
        "musicbrainz_releasegroupid": first_value("musicbrainz_releasegroupid", "musicbrainz_release_group_id"),
        "discogs_album_id": first_value("discogs_album_id", "discogs_release_id"),
    }

    year_values = [
        safe_int(track.get("year"))
        for track in tracks
        if safe_int(track.get("year")) is not None
    ]
    album_data["album_year"] = max(year_values) if year_values else None

    is_album_favourite = False
    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT 1 FROM bookmarks
                    WHERE bookmark_type = 'album'
                      AND LOWER(artist_name) = LOWER(:artist)
                      AND LOWER(album_name) = LOWER(:album)
                    LIMIT 1
                """),
                {"artist": artist_name, "album": album_name},
            ).fetchone()
            if row:
                is_album_favourite = True
    except Exception as exc:
        logger.debug("Failed to check album bookmark", artist=artist_name, album=album_name, error=str(exc))

    tracks_by_disc: dict[int, list[dict[str, Any]]] = {}
    for track in tracks:
        # Normalise a bogus disc_number of 0 (bad source tags / imports) to
        # disc 1 — a "0" disc must never render as its own "disc 0" group on
        # a single-disc album (the reported "disc 1 and disc 0" split).
        disc_number = safe_int(track.get("disc_number"))
        if not disc_number or disc_number < 1:
            disc_number = 1
        tracks_by_disc.setdefault(disc_number, []).append(track)

    album_genres = collect_album_genres()

    genre_sources = {}
    try:
        genre_sources = get_album_genre_sources(tracks)
    except Exception as exc:
        logger.debug("Failed to aggregate album genre sources", error=str(exc))

    album_artist_mbid = first_value(
        "artist_mbid", "musicbrainz_artistid", "musicbrainz_albumartistid",
    ) or ""

    def collect_hero_genres() -> list[str]:
        ranked: dict[str, int] = {}
        for source_key, tags in (genre_sources or {}).items():
            for tag in tags or []:
                name = str(tag.get("name") or "").strip()
                if not name:
                    continue
                try:
                    count = int(tag.get("count") or 0)
                except (TypeError, ValueError):
                    count = 1
                ranked[name] = ranked.get(name, 0) + max(count, 1)

        ordered: list[str] = []
        seen: set[str] = set()
        for genre in album_genres:
            key = genre.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(genre)
                
        for name, _count in sorted(ranked.items(), key=lambda x: x[1], reverse=True):
            key = name.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(name)
        return ordered

    hero_genres = collect_hero_genres()

    return await render_template(
        "pages/album_detail.html",
        artist_name=artist_name,
        album_name=album_name,
        artist=artist_name,
        album=album_name,
        album_data=album_data,
        tracks=tracks,
        tracks_by_disc=tracks_by_disc,
        album_genres=album_genres,
        genre_sources=genre_sources,
        hero_genres=hero_genres,
        album_artist_mbid=album_artist_mbid,
        is_album_favourite=is_album_favourite,
        slskd_config=cfg.get("slskd", {}),
        all_album_years=all_album_years,
        active_album_year=album_year_filter,
    )


@ui_bp.route("/track/<track_id>", methods=["GET", "POST"])
async def track_detail(track_id: str) -> Any:
    """View and edit track details."""
    cfg = get_config()

    def split_tag_values(value: Any) -> list[str]:
        if value in (None, ""):
            return []

        try:
            if isinstance(value, str) and value.strip().startswith("["):
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    raw_items = parsed
                else:
                    raw_items = [value]
            elif isinstance(value, list):
                raw_items = value
            else:
                raw_items = re.split(r"[,;|\\]+", str(value))
        except Exception:
            raw_items = re.split(r"[,;|\\]+", str(value))

        cleaned: list[str] = []
        seen: set[str] = set()

        for item in raw_items:
            text_val = str(item).strip()
            if not text_val:
                continue

            key = text_val.lower()
            if key in seen:
                continue

            seen.add(key)
            cleaned.append(text_val)

        return cleaned

    def stringify_tag_field(value: Any) -> str:
        return ", ".join(split_tag_values(value))

    def get_track_column_types(db: Any) -> dict[str, str]:
        try:
            result = db.execute(text("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'tracks'
            """))
            return {
                str(row._mapping["column_name"]): str(row._mapping["data_type"]).lower()
                for row in result.fetchall()
            }
        except Exception:
            return {}

    def quote_identifier(column_name: str) -> str:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", column_name):
            raise ValueError(f"Unsafe column name: {column_name}")
        return f'"{column_name}"'

    def parse_optional_int(value: Any, allow_prefix: bool = False) -> int | None:
        text_value = str(value or "").strip()
        if not text_value:
            return None

        if allow_prefix and "/" in text_value:
            text_value = text_value.split("/", 1)[0].strip()

        try:
            return int(text_value)
        except ValueError:
            return None

    def parse_optional_float(value: Any) -> float | None:
        text_value = str(value or "").strip()
        if not text_value:
            return None

        try:
            return float(text_value)
        except ValueError:
            return None

    def parse_bool(value: Any) -> bool:
        return str(value or "").strip().lower() in {
            "1", "true", "yes", "on", "checked",
        }

    def normalize_for_column(
        column_name: str,
        value: Any,
        column_types: dict[str, str],
    ) -> Any:
        column_type = column_types.get(column_name, "")

        if column_type in {"integer", "bigint", "smallint"}:
            return parse_optional_int(
                value,
                allow_prefix=(column_name == "track_number"),
            )

        if column_type in {"numeric", "double precision", "real", "decimal"}:
            return parse_optional_float(value)

        if column_type == "boolean":
            return parse_bool(value)

        text_value = str(value or "").strip()
        return text_value if text_value else None

    def normalize_flag_for_column(
        column_name: str,
        value: bool,
        column_types: dict[str, str],
    ) -> Any:
        column_type = column_types.get(column_name, "")
        if column_type == "boolean":
            return bool(value)
        return 1 if value else 0

    def apply_track_template_aliases(track: dict[str, Any]) -> dict[str, Any]:
        for field in [
            "navidrome_genres", "lastfm_tags", "discogs_genres",
            "musicbrainz_genres", "essentia_genres", "listenbrainz_genres",
        ]:
            if track.get(field):
                track[field] = stringify_tag_field(track.get(field))

        if not track.get("genres"):
            track["genres"] = (
                track.get("manual_genres")
                or track.get("top_genres")
                or track.get("navidrome_genres")
                or ""
            )

        track["mood_list"] = split_tag_values(track.get("mood"))

        if track.get("writer"):
            track["writer"] = stringify_tag_field(track.get("writer"))

        _sources_raw = (
            track.get("single_sources")
            or track.get("single_detection_sources")
            or ""
        )
        _source_keys: list[str] = []
        try:
            _parsed = json.loads(_sources_raw) if isinstance(_sources_raw, str) and _sources_raw.strip() else _sources_raw
            if isinstance(_parsed, list):
                for _entry in _parsed:
                    if isinstance(_entry, dict):
                        if _entry.get("matched"):
                            _k = str(_entry.get("source") or "").strip()
                            if _k:
                                _source_keys.append(_k)
                    elif isinstance(_entry, str) and _entry.strip():
                        _source_keys.append(_entry.strip())
        except Exception:
            _source_keys = split_tag_values(_sources_raw)
            
        track["single_sources_list"] = _source_keys

        if not track.get("musicbrainz_albumid"):
            track["musicbrainz_albumid"] = (
                track.get("musicbrainz_album_mbid")
                or track.get("musicbrainz_releaseid")
                or ""
            )

        if not track.get("musicbrainz_album_mbid"):
            track["musicbrainz_album_mbid"] = (
                track.get("musicbrainz_albumid")
                or track.get("musicbrainz_releaseid")
                or ""
            )

        if not track.get("musicbrainz_trackid"):
            track["musicbrainz_trackid"] = (
                track.get("mbid")
                or track.get("beets_mbid")
                or ""
            )

        if not track.get("mbid"):
            track["mbid"] = (
                track.get("musicbrainz_trackid")
                or track.get("beets_mbid")
                or ""
            )

        if not track.get("beets_mbid"):
            track["beets_mbid"] = (
                track.get("mbid")
                or track.get("musicbrainz_trackid")
                or ""
            )

        if not track.get("musicbrainz_artist_id"):
            track["musicbrainz_artist_id"] = (
                track.get("musicbrainz_artistid")
                or track.get("artist_mbid")
                or ""
            )

        if not track.get("musicbrainz_artistid"):
            track["musicbrainz_artistid"] = (
                track.get("musicbrainz_artist_id")
                or track.get("artist_mbid")
                or ""
            )

        if not track.get("album_artist"):
            track["album_artist"] = (
                track.get("albumartist")
                or track.get("artist")
                or ""
            )

        if track.get("artist_z_score") is None:
            track["artist_z_score"] = (
                track.get("final_score")
                or track.get("popularity_score")
            )

        if track.get("popularity_score") is None:
            track["popularity_score"] = (
                track.get("final_score")
                or track.get("artist_z_score")
            )

        if track.get("is_single") is None:
            track["is_single"] = 0

        if not track.get("single_confidence"):
            track["single_confidence"] = "low"

        if track.get("file_path") is None:
            track["file_path"] = ""

        return track

    def build_tags_to_write_local(update_values: dict[str, Any]) -> dict[str, Any]:
        return build_tag_updates(update_values)

    try:
        with db_session() as db:
            column_types = get_track_column_types(db)

            result = db.execute(
                text("""
                    SELECT *
                    FROM tracks
                    WHERE CAST(id AS TEXT) = :id
                    LIMIT 1
                """),
                {"id": str(track_id)},
            )
            row = result.fetchone()

            if not row:
                await flash("Track not found", "error")
                return redirect(url_for("ui.dashboard"))

            raw_db_columns = set(row._mapping.keys())
            track = apply_track_template_aliases(dict(row._mapping))
            track = _coerce_track_numerics(track)
            existing_columns = raw_db_columns

            if request.method == "POST":
                form = await request.form

                direct_fields = [
                    "title", "artist", "album", "album_artist", "stars",
                    "is_single", "single_confidence", "mbid", "suggested_mbid",
                    "suggested_mbid_confidence", "genres", "year", "composer",
                    "writer", "arranger", "mixer", "producer", "work",
                    "track_number", "disc_number", "comment", "isrc", "bpm",
                    "bitrate", "sample_rate", "titlesort", "albumsort",
                    "artistsort", "composersort", "albumartistsort",
                    "lyricistsort", "artistssort", "albumartistssort",
                    "artists", "albumartists", "conductor", "performer",
                    "director", "djmixer", "engineer", "remixer", "lyricist",
                    "albumversion", "recordlabel", "copyright", "releasedate",
                    "releasetype", "releasestatus", "releasecountry", "media",
                    "barcode", "catalognumber", "asin", "originalyear",
                    "originaldate", "tracktotal", "disctotal", "script",
                    "discsubtitle", "lyrics", "subtitle", "grouping",
                    "movement", "movementname", "movementtotal", "key",
                    "language", "license", "website", "encodedby",
                    "encodersettings", "explicitstatus", "musicbrainz_albumid",
                    "musicbrainz_artistid", "musicbrainz_albumartistid",
                    "musicbrainz_releasegroupid", "musicbrainz_releasetrackid",
                    "musicbrainz_workid", "musicbrainz_trackid",
                    "replaygain_track_gain", "replaygain_track_peak",
                    "replaygain_album_gain", "replaygain_album_peak",
                    "r128_track_gain", "r128_album_gain",
                ]

                flag_fields = [
                    "is_cover", "cover_manual_override", "alternate_take",
                    "is_compilation", "is_live", "is_acoustic", "is_remix",
                    "single_manual_override",
                ]

                update_values: dict[str, Any] = {}

                for field_name in direct_fields:
                    if field_name not in existing_columns:
                        continue
                    if field_name not in form:
                        continue
                    update_values[field_name] = normalize_for_column(
                        field_name, form.get(field_name), column_types,
                    )

                mbid_value = form.get("mbid", "").strip() if "mbid" in form else None
                if mbid_value:
                    for column_name in ["mbid", "beets_mbid", "musicbrainz_trackid"]:
                        if column_name in existing_columns:
                            update_values[column_name] = mbid_value

                mb_album_value = (
                    form.get("musicbrainz_albumid", "").strip()
                    if "musicbrainz_albumid" in form else None
                )
                if mb_album_value:
                    for column_name in ["musicbrainz_albumid", "musicbrainz_album_mbid", "musicbrainz_releaseid"]:
                        if column_name in existing_columns:
                            update_values[column_name] = mb_album_value

                genres_value = form.get("genres", "").strip() if "genres" in form else None
                if genres_value is not None:
                    _g_parts = [
                        g.strip()
                        for g in re.split(r"[,;/\\]+", genres_value)
                        if g.strip()
                    ]
                    genres_value = ", ".join(_g_parts) if _g_parts else None
                    for column_name in ["genres", "manual_genres"]:
                        if column_name in existing_columns:
                            update_values[column_name] = genres_value or None

                for field_name in flag_fields:
                    if field_name not in existing_columns:
                        continue
                    update_values[field_name] = normalize_flag_for_column(
                        field_name, parse_bool(form.get(field_name)), column_types,
                    )

                if "single_manual_override" in existing_columns:
                    update_values["single_manual_override"] = normalize_flag_for_column(
                        "single_manual_override", True, column_types,
                    )

                if update_values:
                    params: dict[str, Any] = {"id": str(track_id)}
                    set_clauses: list[str] = []

                    for index, (column_name, value) in enumerate(update_values.items()):
                        param_name = f"value_{index}"
                        set_clauses.append(f"{quote_identifier(column_name)} = :{param_name}")
                        params[param_name] = value

                    if "updated_at" in existing_columns:
                        set_clauses.append("updated_at = CURRENT_TIMESTAMP")

                    db.execute(
                        text(f"""
                            UPDATE tracks
                            SET {", ".join(set_clauses)}
                            WHERE CAST(id AS TEXT) = :id
                        """),
                        params,
                    )

                    apply_to_album = parse_bool(form.get("apply_to_album", ""))
                    album_scoped_fields = {
                        "album", "album_artist", "year",
                        "musicbrainz_albumid", "musicbrainz_album_mbid",
                        "musicbrainz_releasegroupid", "musicbrainz_albumartistid",
                    }
                    album_updates = {k: v for k, v in update_values.items() if k in album_scoped_fields}
                    
                    if apply_to_album and album_updates and update_values.get("album"):
                        try:
                            raw_row = dict(row._mapping)
                            old_album = raw_row.get("album")
                            if old_album:
                                changed = {
                                    k: v for k, v in album_updates.items()
                                    if not _values_equal(v, raw_row.get(k))
                                }
                                if changed:
                                    album_set_clause = ", ".join(
                                        f"{quote_identifier(k)} = :{k}" for k in changed
                                    )
                                    album_params = {
                                        **changed,
                                        "id": str(track_id),
                                        "old_album": old_album,
                                        "old_album_artist": (
                                            raw_row.get("album_artist")
                                            or raw_row.get("artist")
                                            or ""
                                        ),
                                    }
                                    album_conditions = [
                                        "CAST(id AS TEXT) <> :id",
                                        "album = :old_album",
                                        "COALESCE(NULLIF(album_artist, ''), artist) = :old_album_artist",
                                    ]
                                    for k in changed:
                                        if k in ("album", "album_artist"):
                                            continue
                                        old = raw_row.get(k)
                                        album_params[f"old_{k}"] = ("" if old is None else str(old))
                                        album_conditions.append(f"COALESCE(NULLIF({k}::text, ''), '') = :old_{k}")
                                        
                                    album_result = db.execute(
                                        text(
                                            "UPDATE tracks SET "
                                            + album_set_clause
                                            + " WHERE "
                                            + " AND ".join(album_conditions)
                                        ),
                                        album_params,
                                    )
                                    album_tracks_updated = album_result.rowcount or 0
                        except Exception as album_err:
                            logger.debug("Album-scoped propagation failed", track_id=track_id, error=str(album_err))
                            album_tracks_updated = 0
                            
                        if album_tracks_updated:
                            await flash(f"Album metadata also applied to {album_tracks_updated} other track(s).", "info")
                            
                    db.commit()

                    if any(f in update_values for f in ("is_live", "is_acoustic")) \
                            and not (update_values.get("is_live") or update_values.get("is_acoustic")):
                        try:
                            if revert_track_live_state(str(track_id)):
                                await flash("Removed \"(Live)\"/\"(Acoustic)\" suffix from the track title.", "info")
                        except Exception as revert_err:
                            logger.debug("Live-state revert failed", track_id=track_id, error=str(revert_err))

                    file_path = track.get("file_path")
                    resolved_path = resolve_music_file_path(file_path)

                    if resolved_path:
                        try:
                            tags_to_write = build_tags_to_write_local(update_values)
                            if tags_to_write:
                                file_write_success = write_tags_to_file(resolved_path, tags_to_write)
                                if file_write_success:
                                    await flash("Track metadata updated and written to the audio file.", "success")
                                else:
                                    await flash("Track metadata updated, but writing tags failed.", "warning")
                            else:
                                await flash("Track metadata updated.", "success")
                        except Exception as tag_err:
                            logger.warning("Tag writing unavailable", track_id=track_id, error=str(tag_err))
                            await flash("Track metadata updated. Audio tag writing was unavailable.", "info")
                    elif file_path:
                        await flash("Track metadata updated, but the audio file could not be found on disk.", "warning")
                    else:
                        await flash("Track metadata updated. No file path is available for audio tag writing.", "info")

                return redirect(url_for("ui.track_detail", track_id=str(track_id)))

            recommended_genres: list[str] = []
            artist_name = track.get("artist") or ""

            if artist_name:
                try:
                    genre_result = db.execute(
                        text("""
                            SELECT genres
                            FROM tracks
                            WHERE artist = :artist
                              AND genres IS NOT NULL
                              AND genres != ''
                            LIMIT 10
                        """),
                        {"artist": artist_name},
                    )

                    genre_set: set[str] = set()
                    for genre_row in genre_result.fetchall():
                        genre_text = genre_row._mapping.get("genres")
                        for genre in split_tag_values(genre_text):
                            genre_set.add(genre)
                    recommended_genres = sorted(genre_set)
                except Exception as rec_err:
                    logger.debug("Could not get recommended genres", track_id=track_id, error=str(rec_err))

            is_track_favourite = False
            try:
                is_track_favourite = _user_is_favourite("track", str(track_id))
            except Exception:
                pass
                
            if not is_track_favourite:
                try:
                    fav_result = db.execute(
                        text("""
                            SELECT 1
                            FROM bookmarks
                            WHERE type = 'track_favourite'
                              AND LOWER(name) = LOWER(:track_id)
                            LIMIT 1
                        """),
                        {"track_id": str(track_id)},
                    )
                    is_track_favourite = fav_result.fetchone() is not None
                except Exception as fav_err:
                    logger.debug("Track favourite check failed", track_id=track_id, error=str(fav_err))

        genre_sources = {}
        try:
            genre_sources = get_track_genre_sources(track)
        except Exception as ge_err:
            logger.debug("Could not get genre sources", track_id=track_id, error=str(ge_err))

        # Last.fm display score: the stored ``lastfm_score`` is the
        # ALBUM-RELATIVE z-score — a track far below its album's mean
        # listener count scores 0 even when it has tens of thousands of
        # listeners (e.g. 49k listeners → 0.0), while the ListenBrainz score
        # uses an absolute log-scale and shows ~70.  That mismatch is
        # confusing: the page should present comparable "source popularity"
        # numbers.  Show the absolute log-scaled score (the same function
        # ListenBrainz uses, and what the combined score uses as its
        # Last.fm component) whenever the stored z-score is 0 but listeners
        # exist; otherwise fall back to the stored score.
        _lf_display_score = None
        try:
            _lf_stored = track.get("lastfm_score")
            _lf_listeners = track.get("lastfm_listeners")
            if _lf_listeners and _lf_listeners > 0 and (
                _lf_stored is None or float(_lf_stored or 0) <= 0
            ):
                from services.popularity.popularity_math import calculate_lastfm_popularity_score
                _lf_display_score = round(
                    calculate_lastfm_popularity_score(int(_lf_listeners), 0), 1
                )
            else:
                _lf_display_score = (
                    round(float(_lf_stored), 1)
                    if _lf_stored is not None and str(_lf_stored).strip() != ""
                    else None
                )
        except Exception as lf_err:
            logger.debug("Last.fm display score fallback failed", track_id=track_id, error=str(lf_err))

        return await render_template(
            "pages/track_detail.html",
            track=track,
            recommended_genres=recommended_genres,
            track_id=str(track_id),
            is_track_favourite=is_track_favourite,
            genre_sources=genre_sources,
            lastfm_display_score=_lf_display_score,
            slskd_config=cfg.get("slskd", {"enabled": False}),
        )

    except Exception as exc:
        logger.error("Error loading track", track_id=track_id, error=str(exc), exc_info=True)
        await flash(f"Error loading track: {exc}", "error")
        return redirect(url_for("ui.dashboard"))


@ui_bp.route("/search")
async def search() -> Any:
    query = request.args.get("q", "").strip()
    return await render_template("pages/search.html", initial_query=query)


def _sanitize_config_sections(config: dict) -> dict:
    cleaned: dict = {}
    for key, value in (config or {}).items():
        if isinstance(value, dict):
            cleaned[key] = dict(value)
        elif isinstance(value, list):
            cleaned[key] = value
        else:
            cleaned[key] = {}

    nested_children = {
        "slskd": ("timeouts",),
        "features": ("downloads_duplicate_cleanup", "retry_scheduler", "download_queue_cleanup_scheduler"),
        "downloads": ("quality_filter", "conversion"),
        "queue": ("matching",),
        "popularity": ("weights",),
        "genres": ("weights", "synonyms"),
        "metadata_update": ("update_on_files",),
    }
    
    for parent, children in nested_children.items():
        parent_val = cleaned.get(parent)
        if not isinstance(parent_val, dict):
            continue
        for child in children:
            if child in parent_val and not isinstance(parent_val[child], dict):
                parent_val[child] = {}

    api_services = cleaned.get("api_integrations")
    if isinstance(api_services, dict):
        for service, service_val in list(api_services.items()):
            if not isinstance(service_val, (dict, list)):
                api_services[service] = {}

    return cleaned


@ui_bp.route("/config/sandbox")
async def config_sandbox_route() -> Any:
    return await render_template("pages/sandbox.html")


@ui_bp.route("/config", methods=["GET", "POST"])
async def config_editor() -> Any:
    config, raw = {}, ""
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")

    # Test-site cutover diagnostics. Surfaced so the Config page can show whether
    # the rebuilt UI is actually being served, rather than only reading the
    # config value back (which would still say "true" if the tree were missing
    # and the cutover had silently declined).
    try:
        from helpers.test_site_mode import status as _test_site_status
        test_site = _test_site_status()
    except Exception:
        test_site = {"enabled": False, "available": False, "active": False}

    try:
        if os.path.exists(config_path):
            with open(config_path) as f:
                raw = f.read()
            config = yaml.safe_load(raw) or {}
            config = _sanitize_config_sections(config)
    except Exception:
        pass
        
    if request.method == "POST":
        form = await request.form
        config_content = str(form.get("config_content", "") or "")
        
        if config_content.strip():
            try:
                parsed = yaml.safe_load(config_content)
                if parsed is not None and not isinstance(parsed, dict):
                    await flash("Config must be a YAML mapping (top-level object)", "error")
                    return await render_template(
                        "pages/config.html", config=config, config_raw=config_content,
                        needs_setup=needs_setup(), test_site=test_site,
                    )
                    
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(config_content)
                clear_config_cache()
                
                try:
                    reschedule_jobs_from_config()
                except Exception:
                    pass
                await flash("Configuration saved from raw YAML", "success")
            except yaml.YAMLError as exc:
                await flash(f"Invalid YAML — not saved: {exc}", "error")
                return await render_template(
                    "pages/config.html", config=config, config_raw=config_content,
                    needs_setup=needs_setup(), test_site=test_site,
                )
        return redirect(url_for("ui.config_editor"))
        
    return await render_template(
        "pages/config.html",
        config=config,
        config_raw=raw,
        needs_setup=needs_setup(),
        test_site=test_site,
    )


@ui_bp.route("/config/env", methods=["GET"])
def config_env_vars() -> Any:
    return jsonify({})


@ui_bp.route("/config/env", methods=["POST"])
def config_env_vars_post() -> Any:
    return redirect(url_for("ui.config_editor"))


@ui_bp.route("/config/save-json", methods=["POST"])
async def config_save_json() -> Any:
    try:
        data = (await request.get_json()) or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400
        
    success = save_config(data)
    if success:
        try:
            log_cfg = data.get("logging") or {}
            level = log_cfg.get("level") or data.get("log_level")
            if level:
                applied = set_log_level(level)
                logger.info("Log level set", applied=applied)
        except Exception as exc:
            logger.warning("Could not apply log level at runtime", error=str(exc))
            
        try:
            _sched_stats = reschedule_jobs_from_config()
            logger.info("Scheduler re-applied after save", stats=_sched_stats)
        except Exception as exc:
            logger.warning("Scheduler re-apply failed", error=str(exc))
            
        return jsonify({"success": True})
    return jsonify({"error": "Failed to save config"}), 500


@ui_bp.route("/config/migrate_postgres", methods=["POST"])
def config_migrate_postgres() -> Any:
    return jsonify({"success": True})


@ui_bp.route("/logs")
async def logs() -> Any:
    _USED_LOG_FILES = {
        "unified_scan.log",
        "info.log",
        "debug.log",
        "queue.log",
        "search.log",
        "access.log",
        "error.log",
        # The alert→toast shim (main.js) records every converted UI alert
        # here; the queue worker process (entrypoint) redirects its stdout to
        # queue_processor.log.  Both belong on the /logs page.
        "client.log",
        "queue_processor.log",
    }
    log_dir = resolve_log_dir()
    log_files = []
    
    if os.path.isdir(log_dir):
        for f in sorted(os.listdir(log_dir)):
            if f.endswith(".log") and f in _USED_LOG_FILES:
                full = os.path.join(log_dir, f)
                size = os.path.getsize(full) if os.path.isfile(full) else 0
                log_files.append({"name": f, "path": full, "size": size})
                
    log_files.sort(key=lambda f: (f["name"] != "unified_scan.log", f["name"]))
    return await render_template("pages/logs.html", log_dir=log_dir, log_files=log_files)


@ui_bp.route("/help")
@ui_bp.route("/help/<path:doc_name>")
async def help_page(doc_name: str | None = None) -> Any:
    doc_path = os.path.join(os.path.dirname(__file__), "..", "documentation")
    doc_root = os.path.realpath(doc_path)
    doc_files = []
    
    try:
        doc_files = sorted(
            os.path.relpath(p, doc_root).replace("\\", "/")
            for p in glob.glob(os.path.join(doc_root, "**", "*.md"), recursive=True)
        )
    except Exception:
        pass

    _DOC_ALIASES = {
        "FEATURES_DOWNLOADS": "USER_GUIDE.md#11-downloads",
        "FEATURES_PLAYLISTS": "USER_GUIDE.md#10-playlists",
        "FEATURES_LIBRARY": "USER_GUIDE.md",
        "MULTI_USER_CONFIG_GUIDE": "Services/CONFIGURATION_GUIDE.md",
    }

    def _doc_url(rel_path: str) -> str:
        anchor = ""
        if "#" in rel_path:
            rel_path, _, anchor = rel_path.partition("#")
        url = "/help/" + rel_path.replace(".md", "").replace("\\", "/")
        return url + (f"#{anchor}" if anchor else "")

    content = ""
    doc_title = "Help"
    
    if not doc_name:
        for candidate in ("USER_GUIDE.md", "README.md"):
            if os.path.exists(os.path.join(doc_root, candidate)):
                doc_name = candidate
                break
                
    if doc_name:
        doc_name = os.path.normpath(str(doc_name)).replace("\\", "/")
        full_path = os.path.realpath(os.path.join(doc_root, doc_name))
        
        if os.path.commonpath([doc_root, full_path]) != doc_root or not os.path.isfile(full_path):
            alias = _DOC_ALIASES.get(os.path.basename(doc_name).replace(".md", ""))
            if alias:
                alias_file = alias.partition("#")[0]
                if os.path.isfile(os.path.join(doc_root, alias_file)):
                    return redirect(_doc_url(alias))
                    
            for fallback in ("USER_GUIDE.md", "README.md"):
                if os.path.isfile(os.path.join(doc_root, fallback)):
                    return redirect(_doc_url(fallback))
                    
            if doc_files:
                available = "\n".join(f"- [{d}]({_doc_url(d)})" for d in doc_files[:15])
                content = (
                    "# Document Not Found\n\n"
                    f"The document **{os.path.basename(doc_name)}** does not exist in the\n"
                    "documentation folder.\n\n"
                    "### Available documents\n\n" + available
                )
            else:
                content = (
                    "# Document Not Found\n\n"
                    f"The document **{os.path.basename(doc_name)}** does not exist in the\n"
                    "documentation folder.\n\n"
                    "- [📘 User Guide](/help/USER_GUIDE) — how the app works, scoring and settings\n"
                    "- [📖 Documentation Index](/help/README) — all available documents\n"
                )
        else:
            with open(full_path, encoding="utf-8") as f:
                md_content = f.read()
            if markdown is not None:
                try:
                    content = markdown.markdown(md_content, extensions=["extra", "toc"])
                except Exception:
                    content = "<pre>" + md_content + "</pre>"
            else:
                content = "<pre>" + md_content + "</pre>"
        doc_title = os.path.basename(doc_name).replace(".md", "").replace("_", " ").title()
        
    return await render_template(
        "pages/help.html", content=content, doc_title=doc_title,
        doc_files=doc_files, current_doc=doc_name,
    )


@ui_bp.route("/bookmarks")
async def bookmarks() -> Any:
    return await render_template("pages/bookmarks.html")


@ui_bp.route("/correcting")
async def correcting() -> Any:
    try:
        page = max(request.args.get("page", 1, type=int), 1)
        per_page = 20

        inconsistencies = get_album_tag_inconsistencies(artist_filter=None)
        total = len(inconsistencies)
        total_pages = max(1, (total + per_page - 1) // per_page)

        start = (page - 1) * per_page
        page_items = inconsistencies[start:start + per_page]

        conflict_stats = get_conflict_stats()

        return await render_template(
            "pages/corrections.html",
            inconsistencies=page_items,
            total=total,
            page=page,
            total_pages=total_pages,
            conflict_stats=conflict_stats,
        )
    except Exception as exc:
        logger.error("Corrections page error", error=str(exc), exc_info=True)
        return await render_template(
            "pages/corrections.html",
            inconsistencies=[],
            total=0,
            page=1,
            total_pages=1,
            conflict_stats={"total_pending": 0, "by_provider": [], "by_field": []},
            error=str(exc),
        )


@ui_bp.route("/missing")
async def missing_page() -> Any:
    cfg = get_config()
    return await render_template("pages/missing_releases.html", slskd_config=cfg.get("slskd", {}))


@ui_bp.route("/discover")
async def discover() -> Any:
    return await render_template("pages/discover.html")


@ui_bp.route("/downloads/monitor")
async def downloads_monitor() -> Any:
    cfg = get_config()
    return await render_template(
        "pages/downloads/monitor.html",
        slskd_config=cfg.get("slskd", {}),
        downloads_dir=resolve_downloads_dir(prefer_music_subfolder=False),
    )


@ui_bp.route("/downloads/banned-words")
async def banned_words_page() -> Any:
    return await render_template("pages/banned_words.html")


@ui_bp.route("/downloads")
async def downloads_page() -> Any:
    cfg = get_config()
    return await render_template(
        "pages/downloads/queue.html",
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/search")
async def downloads_search() -> Any:
    cfg = get_config()
    return await render_template(
        "pages/downloads/search.html",
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/search/soulseek")
async def downloads_search_soulseek() -> Any:
    return redirect(url_for("ui.downloads_search") + "#soulseek")


@ui_bp.route("/downloads/search/musicbrainz")
async def downloads_search_musicbrainz() -> Any:
    return redirect(url_for("ui.downloads_search"))


@ui_bp.route("/downloads/search/playlists")
async def downloads_search_playlists() -> Any:
    return redirect(url_for("ui.downloads_search") + "#playlists")


# REMOVED 2026-09-18: @ui_bp.route("/downloads/manager") -> downloads_manager()
#
# It rendered "pages/downloads/manager.html", which no longer exists. The file
# was added in a75ce08c and deleted in 9454d21b ("updated page layouts") while
# this route was left behind, so the endpoint raised TemplateNotFound.
#
# Dropped rather than restored, because the template was a shell: all seven
# handlers its buttons called — scanDownloads, processAllDownloads,
# scanRetryQueue, openDownloadsFolder, processRetryQueue, processOneFile,
# removeQueueItem — are defined nowhere in static/js or New/static/js, and it
# hardcoded a Windows path (\\downloads\\Soulseek\\Incomplete) in user-facing
# copy for an app that runs in Linux containers.
#
# Nothing referenced this route: no url_for('ui.downloads_manager') anywhere,
# and no template linked to /downloads/manager. The downloads hub
# (@ui_bp.route("/downloads") -> downloads_page -> pages/downloads/queue.html)
# is the current equivalent.
#
# If an existing bookmark matters, the sibling routes above show how the
# codebase keeps a dead URL alive — this instead of a 404:
#
#     @ui_bp.route("/downloads/manager")
#     async def downloads_manager() -> Any:
#         return redirect(url_for("ui.downloads_page"))


@ui_bp.route("/downloads/discover/similar-artists")
async def downloads_discover_similar_artists() -> Any:
    return await render_template("pages/downloads/similar_artists.html")


@ui_bp.route("/downloads/discover/upcoming")
async def downloads_discover_upcoming() -> Any:
    return await render_template("pages/downloads/upcoming.html")


@ui_bp.route("/artist/<path:name>/corrections")
async def artist_corrections(name: str) -> Any:
    data, code = get_artist_corrections(unquote(name or "").strip())
    if code != 200:
        return jsonify(data), code
    return await render_template("pages/artist_corrections.html", **data)


@ui_bp.route("/artist/<path:name>/genre-management")
async def artist_genre_management(name: str) -> Any:
    """Genre management for one artist, grouped artist → album → track.

    BUG FIXED: this passed only ``artist_name`` while the template renders
    ``albums`` / ``artist_current_genres`` / ``artist_recommended_genres``. In
    Jinja an undefined name is falsy rather than an error, so the page rendered
    "No genres" and "No tracks found for this artist." for every artist —
    silently, with no exception to point at it.

    Genre data comes from the same fields the rest of the app aggregates from:
    the artist's stored ``genres`` are "current", and the per-source columns
    (Discogs / Last.fm / MusicBrainz) are "recommended" once lower-cased
    de-duplication drops anything already current.
    """
    artist_name = unquote(name or "").strip()

    def _values(value: Any) -> list[str]:
        """Coerce a stored genre column (CSV, JSON list, JSON dict) to names.

        Handles the shapes the DB actually holds: a comma/backslash CSV string,
        a JSON array, or a JSONB list of ``{"name": ...}`` dicts (Discogs and
        MusicBrainz store the latter).
        """
        if value in (None, ""):
            return []

        raw: list[Any]
        if isinstance(value, (list, tuple)):
            raw = list(value)
        else:
            text = str(value).strip()
            if text.startswith(("[", "{")):
                try:
                    parsed = json.loads(text)
                    raw = list(parsed) if isinstance(parsed, (list, tuple)) else [parsed]
                except Exception:
                    raw = re.split(r"[,;|\\]+", text)
            else:
                raw = re.split(r"[,;|\\]+", text)

        cleaned: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                item = item.get("name") or ""
            text_val = str(item).strip()
            if text_val:
                cleaned.append(text_val)
        # Preserve first-seen order/casing, drop case-insensitive duplicates.
        return list(dict.fromkeys(cleaned))

    rows: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, album, title, track_number, disc_number,
                           file_path, artist,
                           COALESCE(NULLIF(album_artist, ''), artist) AS album_artist,
                           COALESCE(genres, '') AS genres,
                           discogs_genres, lastfm_tags, musicbrainz_genres
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                    ORDER BY album, disc_number, track_number, title
                """),
                {"artist": artist_name},
            )
            rows = [dict(r._mapping) for r in result.fetchall()]
    except Exception as exc:
        logger.warning("Genre management load failed", artist=artist_name, error=str(exc))

    all_current: set[str] = set()
    all_recommended_raw: set[str] = set()
    albums_map: dict[str, dict[str, Any]] = {}

    for row in rows:
        album = str(row.get("album") or "")
        current = _values(row.get("genres"))
        current_lower = {g.casefold() for g in current}

        rec_raw: list[str] = []
        for column in ("discogs_genres", "lastfm_tags", "musicbrainz_genres"):
            rec_raw.extend(_values(row.get(column)))
        # Preserve first-seen casing while dropping duplicates.
        rec_raw = list(dict.fromkeys(rec_raw))
        recommended = [g for g in rec_raw if g.casefold() not in current_lower]

        track_dict = {
            "id": row.get("id"),
            "title": str(row.get("title") or ""),
            "album": album,
            "track_number": row.get("track_number"),
            "disc_number": row.get("disc_number"),
            "file_path": str(row.get("file_path") or ""),
            "artist": str(row.get("artist") or ""),
            "current_genres": current,
            "recommended_genres": recommended,
        }

        bucket = albums_map.setdefault(
            album, {"tracks": [], "current": set(), "rec_raw": set()}
        )
        bucket["tracks"].append(track_dict)
        bucket["current"].update(current)
        bucket["rec_raw"].update(rec_raw)
        all_current.update(current)
        all_recommended_raw.update(rec_raw)

    albums: list[dict[str, Any]] = []
    for album_name, bucket in albums_map.items():
        current_lower = {g.casefold() for g in bucket["current"]}
        albums.append({
            "album": album_name,
            "current_genres": sorted(bucket["current"]),
            "recommended_genres": [
                g for g in dict.fromkeys(bucket["rec_raw"])
                if g.casefold() not in current_lower
            ],
            "tracks": bucket["tracks"],
        })

    artist_current_lower = {g.casefold() for g in all_current}

    return await render_template(
        "pages/artist_genres.html",
        artist_name=artist_name,
        albums=albums,
        artist_current_genres=sorted(all_current),
        artist_recommended_genres=[
            g for g in dict.fromkeys(all_recommended_raw)
            if g.casefold() not in artist_current_lower
        ],
    )


@ui_bp.route("/metadata-compare")
async def metadata_compare() -> Any:
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT
                    album,
                    COALESCE(NULLIF(album_artist, ''), artist) AS artist_name,
                    year AS navidrome_year,
                    beets_year,
                    navidrome_genres,
                    musicbrainz_genres,
                    COUNT(*) AS track_count
                FROM tracks
                WHERE album IS NOT NULL
                  AND album != ''
                  AND COALESCE(NULLIF(album_artist, ''), artist) IS NOT NULL
                  AND COALESCE(NULLIF(album_artist, ''), artist) != ''
                GROUP BY
                    album,
                    COALESCE(NULLIF(album_artist, ''), artist),
                    year,
                    beets_year,
                    navidrome_genres,
                    musicbrainz_genres
                ORDER BY
                    LOWER(COALESCE(NULLIF(album_artist, ''), artist)),
                    LOWER(album)
            """))
            rows = [dict(row._mapping) for row in result.fetchall()]

        album_comparisons: list[dict[str, Any]] = []

        for row in rows:
            album = row.get("album") or ""
            artist = row.get("artist_name") or ""
            nav_year = row.get("navidrome_year")
            beets_year = row.get("beets_year")
            nav_genres_raw = row.get("navidrome_genres") or ""
            beets_genres_raw = row.get("musicbrainz_genres") or ""
            track_count = int(row.get("track_count") or 0)

            nav_genres = [
                genre.strip()
                for genre in str(nav_genres_raw).split(",")
                if genre.strip()
            ]

            beets_genres = [
                genre.strip()
                for genre in str(beets_genres_raw).split(",")
                if genre.strip()
            ]

            has_year_mismatch = str(nav_year or "") != str(beets_year or "")
            has_genre_mismatch = sorted(g.lower() for g in nav_genres) != sorted(
                g.lower() for g in beets_genres
            )

            if has_year_mismatch or has_genre_mismatch:
                album_comparisons.append({
                    "album": album,
                    "artist": artist,
                    "track_count": track_count,
                    "navidrome": {
                        "year": nav_year,
                        "genres": nav_genres,
                    },
                    "beets": {
                        "year": beets_year,
                        "genres": beets_genres,
                    },
                })

        return await render_template(
            "pages/metadata_compare.html",
            album_comparisons=album_comparisons,
        )

    except Exception as exc:
        logger.error("metadata-compare error", error=str(exc), exc_info=True)
        await flash(f"Error loading metadata comparison: {exc}", "danger")
        return redirect(url_for("ui.dashboard"))


@ui_bp.route("/api/metadata-compare/search-musicbrainz", methods=["POST"])
async def metadata_compare_search_mb() -> Any:
    data = (await request.get_json(silent=True)) or {}
    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()

    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400

    try:
        svc = MusicBrainzService()
        results: list[dict[str, Any]] = []
        raw_results = None

        if hasattr(svc, "search_releases"):
            raw_results = svc.search_releases(artist=artist, album=album)
        elif hasattr(svc, "search_album"):
            raw_results = svc.search_album(artist=artist, album=album)
        elif hasattr(svc, "search_release"):
            raw_results = svc.search_release(artist=artist, album=album)

        if raw_results:
            if isinstance(raw_results, dict):
                raw_results = [raw_results]

            for item in raw_results:
                if not isinstance(item, dict):
                    continue

                release_date = (
                    item.get("first-release-date")
                    or item.get("first_release_date")
                    or item.get("date")
                    or item.get("release_date")
                    or ""
                )

                year = item.get("year")
                if not year and release_date:
                    year = str(release_date).split("-")[0]

                genres = (
                    item.get("genres")
                    or item.get("tags")
                    or item.get("musicbrainz_genres")
                    or []
                )

                if isinstance(genres, str):
                    genres = [
                        genre.strip()
                        for genre in genres.split(",")
                        if genre.strip()
                    ]

                results.append({
                    "id": item.get("id") or item.get("mbid") or item.get("release_id"),
                    "title": item.get("title") or item.get("album") or album,
                    "album": item.get("album") or item.get("title") or album,
                    "artist": item.get("artist") or item.get("artist-credit") or artist,
                    "artist-credit": item.get("artist-credit") or item.get("artist") or artist,
                    "first-release-date": release_date,
                    "year": year,
                    "genres": genres,
                    "confidence": item.get("confidence"),
                })

        if not results and hasattr(svc, "get_suggested_mbid"):
            suggested = svc.get_suggested_mbid(album, artist)
            mbid = None
            confidence = None

            if isinstance(suggested, tuple):
                mbid = suggested[0] if len(suggested) > 0 else None
                confidence = suggested[1] if len(suggested) > 1 else None
            else:
                mbid = suggested

            if mbid:
                results.append({
                    "id": mbid,
                    "title": album,
                    "album": album,
                    "artist": artist,
                    "artist-credit": artist,
                    "first-release-date": "",
                    "year": None,
                    "genres": [],
                    "confidence": confidence,
                })

        return jsonify({
            "success": True,
            "results": results,
        })

    except Exception as exc:
        logger.error("metadata-compare MB search failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@ui_bp.route("/api/metadata-compare/apply-musicbrainz", methods=["POST"])
async def metadata_compare_apply_mb() -> Any:
    data = (await request.get_json(silent=True)) or {}
    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()
    mb_data = data.get("mb_data") or {}

    if not artist or not album or not isinstance(mb_data, dict):
        return jsonify({"error": "artist, album, and mb_data required"}), 400

    try:
        release_date = (
            mb_data.get("first-release-date")
            or mb_data.get("first_release_date")
            or mb_data.get("release_date")
            or ""
        )

        year = mb_data.get("year")
        if not year and release_date:
            year = str(release_date).split("-")[0]

        genres = mb_data.get("genres") or []
        if isinstance(genres, str):
            genres = [
                genre.strip()
                for genre in genres.split(",")
                if genre.strip()
            ]

        genres_text = ",".join(genres)
        mbid = (
            mb_data.get("id")
            or mb_data.get("mbid")
            or mb_data.get("release_id")
            or ""
        )

        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE tracks
                    SET
                        year = COALESCE(:year, year),
                        beets_year = COALESCE(:year, beets_year),
                        musicbrainz_genres = COALESCE(NULLIF(:genres, ''), musicbrainz_genres),
                        musicbrainz_album_mbid = COALESCE(NULLIF(:mbid, ''), musicbrainz_album_mbid),
                        mb_override = TRUE
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    RETURNING id
                """),
                {
                    "year": year,
                    "genres": genres_text,
                    "mbid": mbid,
                    "artist": artist,
                    "album": album,
                },
            )
            updated_ids = [row[0] for row in result.fetchall()]

        return jsonify({
            "success": True,
            "message": f"Applied MusicBrainz data to {artist} - {album} ({len(updated_ids)} tracks)",
            "tracks_updated": len(updated_ids),
        })

    except Exception as exc:
        logger.error("metadata-compare apply MB failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@ui_bp.route("/api/metadata-compare/accept-navidrome", methods=["POST"])
async def metadata_compare_accept_navidrome() -> Any:
    data = (await request.get_json(silent=True)) or {}
    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()

    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400

    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE tracks
                    SET metadata_locked = TRUE
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                """),
                {
                    "artist": artist,
                    "album": album,
                },
            )

        return jsonify({
            "success": True,
            "message": f"Navidrome data locked for {artist} - {album}",
        })

    except Exception as exc:
        logger.error("metadata-compare accept navidrome failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


# REMOVED 2026-09-18: @ui_bp.route("/smart-playlists") -> smart_playlists()
#
# It rendered "pages/smart_playlists.html" — a SECOND smart-playlist builder
# (its own addFilter / buildJSON / updateJsonPreview, ~454 lines of inline JS)
# living alongside the one in static/js/pages/playlist.js (SPB_FIELDS /
# SPB_OPERATORS / spbBuildJson), which is the builder wired into the /playlists
# hub and the one the backend's .nsp schema is transcribed against.
#
# Nothing linked to it: no url_for('ui.smart_playlists'), no href, no entry in
# the navbar or any page. It was reachable only by typing the URL.
#
# The template pages/smart_playlists.html should be deleted with it.


@ui_bp.route("/analytics/genres-moods")
async def analytics_genres_moods_page() -> Any:
    return await render_template("pages/analytics.html")


@ui_bp.route("/debug/static")
def debug_static() -> Any:
    return jsonify({"static_folder": ""})


# ===========================================================================
# TEMPLATE FILTERS
# ===========================================================================

@ui_bp.app_template_filter("split_genres")
def split_genres(s: Any) -> list[str]:
    if not s:
        return []
    return [g.strip() for g in re.split(r"[\\,]+", str(s)) if g.strip()]


@ui_bp.app_template_filter("format_datetime")
def format_datetime(value: Any) -> str:
    if not value:
        return ""
    try:
        if "T" in str(value):
            dt = datetime.fromisoformat(str(value).split(".")[0])
        else:
            dt = datetime.fromisoformat(str(value))
        return dt.strftime("%d-%m-%y at %I:%M %p")
    except Exception:
        return str(value)
