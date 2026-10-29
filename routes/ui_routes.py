"""UI page routes, auth, config — migrated from old app.py."""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
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
from db.repositories.tracks import upsert_tracks_bulk
from helpers.artist_sort import (
    artist_sort_key,
    artist_sort_letter,
    artist_sort_name,
    artist_sort_sql,
)
from services.catalog.artist_release_entries import build_missing_and_upcoming_entries
from services.catalog.release_categories import (
    normalise_category,
    ordered_specs,
)
from helpers.config_helpers import (
    clear_config_cache,
    get_album_save_background_config,
    get_config,
    needs_setup,
    read_config_from_disk,
    save_config,
    save_partial_config,
)
from helpers.logging_config import log_unified, resolve_log_dir, set_log_level
from helpers.normalization_service import strip_featured_artist
from helpers.track_ordering import disc_sort_value, sort_album_tracks
from services.catalog.album_classification_service import (
    classify_album_type,
    normalize_primary_release_type,
)
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
from services.popularity.popularity_math import (
    cross_album_track_score,
    star_then_popularity_order_key,
)
from services.popularity.stages.album_stage import (
    revert_track_live_state,
    track_carries_live_state,
)
from services.scanning.scan_history_service import get_recent_album_scans
from services.scheduler.scheduler_service import reschedule_jobs_from_config

try:
    import markdown
except ImportError:
    markdown = None

logger = structlog.get_logger(__name__)

ui_bp = Blueprint("ui", __name__)


#: Fallback affiliation filter, used only if
#: ``services.enrichment.genre_aggregation_service`` cannot be imported.
#: It FAILS CLOSED: an album-level genre list that carries "Cover" is a bug
#: (the marker belongs to the one track that earned it), so a missing helper
#: must not silently let it through.
#:
#: The Christmas/Holiday family is here too, because this filter governs
#: what a track INHERITS — and "Christmas" describes the album's occasion,
#: not the track. (The album's OWN genre list keeps Christmas; that is a
#: different question.)
_FALLBACK_AFFILIATION_FILTER = frozenset({
    "cover", "live", "acoustic", "unplugged", "remix", "remixed",
    "instrumental", "comedy", "orchestral", "symphonic", "soundtrack",
    "christmas", "holiday",
})


def _fallback_is_affiliation_tag(value: Any) -> bool:
    return str(value or "").strip().lower() in _FALLBACK_AFFILIATION_FILTER


def _album_type_is_live_state(raw: str | None) -> bool:
    """True when an album-type string describes a LIVE-state release.

    "Live state" covers the forms the tagging pipeline applies and the revert in
    ``album_detail`` undoes: live, acoustic and unplugged.
    (``helpers.normalization_service.strip_live_acoustic_suffix`` strips all
    three, so they must be recognised together or the revert deletes a label it
    does not consider live.)

    ⚠️ WORD BOUNDARIES, not a substring test. The type strings arrive in several
    shapes — ``album+live`` from the Album Type select, ``Live`` / ``Live Album``
    / ``(live)`` from older writes and the classifier — and a bare substring
    check both misses some of them and matches words that merely CONTAIN a
    marker: ``"delivery"`` contains ``"live"``, so a naive check would classify
    an album called *Delivery* as a live release and strip its titles.

    The OLD guard here tested only the two ``+live`` / ``(live)`` spellings, so
    an ``album+acoustic`` release was treated as an ordinary studio album and had
    its "(Acoustic)" titles stripped on every save — the acoustic half of the
    same report.
    """
    return bool(re.search(r"\b(?:live|acoustic|unplugged)\b", str(raw or "").lower()))


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
    nav_cfg = cfg.get("navidrome") if isinstance(cfg.get("navidrome"), dict) else {}
    api = cfg.get("api_integrations", {})
    dl = cfg.get("downloads", {})
    dl_quality = dl.get("quality") or {}
    watcher_cfg = cfg.get("watcher", {})
    features_cfg = cfg.get("features", {})
    playlists_cfg = cfg.get("playlists", {})
    tagging_cfg = cfg.get("tagging", {})
    tag_opts = tagging_cfg.get("write_options", {}) or {}
    metadata_update_cfg = cfg.get("metadata_update", {}) or {}
    popularity_cfg = cfg.get("popularity", {}) or {}
    pop_weights = popularity_cfg.get("weights", {}) or {}
    genres_cfg = cfg.get("genres", {}) or {}
    genres_selection = genres_cfg.get("selection", {}) or {}
    slskd_cfg = cfg.get("slskd", {})
    essentia_cfg = cfg.get("essentia", {}) or {}

    allowed_formats_raw = dl_quality.get("allowed_formats", ["flac", "mp3"])
    allowed_formats = [str(f).strip().lower().lstrip(".") for f in (allowed_formats_raw or []) if str(f).strip()]

    setup_defaults = {
        "nav_url": nav_first.get("base_url", ""),
        "nav_user": nav_first.get("user", ""),
        "nav_pass": nav_first.get("pass", ""),
        "nav_display_name": nav_first.get("display_name", ""),
        "lfm_username": nav_first.get("lastfm_username", ""),
        "nav_auto_public_playlists": bool(nav_cfg.get("auto_public_playlists", False)),
        "nav_playlist_cover_art": bool(nav_cfg.get("playlist_cover_art", False)),
        "lfm_enabled": api.get("lastfm", {}).get("enabled", False),
        "lfm_api_key": api.get("lastfm", {}).get("api_key", ""),
        "dg_enabled": api.get("discogs", {}).get("enabled", False),
        "dg_token": api.get("discogs", {}).get("token", ""),
        "lb_enabled": api.get("listenbrainz", {}).get("enabled", True),
        "lb_token": nav_first.get("listenbrainz_user_token", "") or api.get("listenbrainz", {}).get("token", ""),
        "mb_enabled": bool(api.get("musicbrainz", {}).get("enabled", True)),
        "audiodb_api_key": api.get("audiodb", {}).get("api_key", ""),
        "gemini_api_key": api.get("gemini", {}).get("api_key", ""),
        "gemini_model": api.get("gemini", {}).get("model", "gemini-2.5-flash-image"),
        "slskd_enabled": bool(slskd_cfg.get("enabled", False)),
        "slskd_url": slskd_cfg.get("web_url", ""),
        "slskd_api_key": slskd_cfg.get("api_key", ""),
        "essentia_enabled": bool(essentia_cfg.get("script_path")),
        "essentia_tag_moods": essentia_cfg.get("tag_moods", True),
        "essentia_tag_genres": essentia_cfg.get("tag_genres", False),
        "essentia_parse_json_features": essentia_cfg.get("parse_json_features", True),
        "essentia_num_genres": essentia_cfg.get("num_genres", 3),
        "essentia_genre_format": essentia_cfg.get("genre_format", "parent_child"),
        "downloads_folder": dl.get("folder", "/downloads/Music"),
        "file_name_format": dl.get("file_name_format", ""),
        "conversion_enabled": bool(dl.get("conversion", {}).get("enabled", False)),
        "conversion_mode": dl.get("conversion", {}).get("mode", "flac_to_mp3"),
        "conversion_bitrate": dl.get("conversion", {}).get("mp3_bitrate_kbps", 320),
        "conversion_original_handling": dl.get("conversion", {}).get("original_handling", "move_to_original"),
        "quality_filter_enabled": bool((dl.get("quality_filter") or {}).get("enabled", False)),
        "min_bitrate": dl_quality.get("min_bitrate", 192),
        "allowed_formats": allowed_formats,
        "write_tags_to_file": tagging_cfg.get("write_tags_to_file", True),
        "ratings_only": bool(tag_opts.get("ratings_only", False)),
        "fill_missing_only": bool(tag_opts.get("fill_missing_only", False)),
        "sync_album_tags_on_scan": bool(tagging_cfg.get("sync_album_tags_on_scan", True)),
        "skip_unchanged_ratings": bool(tagging_cfg.get("skip_unchanged_ratings", True)),
        "apply_metadata_during_scan": bool(metadata_update_cfg.get("apply_during_scan", True)),
        "album_name_source": metadata_update_cfg.get("album_name_source", "dedupe"),
        "album_name_update_target": metadata_update_cfg.get("album_name_update_target", "db"),
        "pop_weight_lastfm": pop_weights.get("lastfm", 0.55),
        "pop_weight_listenbrainz": pop_weights.get("listenbrainz", 0.35),
        "pop_weight_age": pop_weights.get("age", 0.10),
        "scan_threads": popularity_cfg.get("scan_threads", 4),
        "track_timeout_seconds": popularity_cfg.get("track_timeout_seconds", 30),
        "album_skip_days": features_cfg.get("album_skip_days", 7),
        "deep_genre_search": bool(features_cfg.get("deep_genre_search", False)),
        "canonical_genres_enabled": bool(genres_selection.get("enabled", True)),
        "auto_import_enabled": watcher_cfg.get("auto_import_enabled", True),
        "auto_popularity_scan": watcher_cfg.get("auto_popularity_scan", True),
        "downloads_watcher_enabled": watcher_cfg.get("downloads_watcher_enabled", True),
        "cover_detection_enabled": features_cfg.get("cover_detection_enabled", True),
        "upcoming_releases_scan_enabled": features_cfg.get("upcoming_releases_scan_enabled", True),
        "sync_ratings_to_all_users": features_cfg.get("sync_ratings_to_all_users", False),
        "essential_playlists_enabled": playlists_cfg.get("essential_playlists_enabled", True),
        "essential_include_featured": playlists_cfg.get("essential_include_featured", True),
        "genre_playlists_enabled": playlists_cfg.get("genre_playlists_enabled", True),
        "new_music_playlist_enabled": playlists_cfg.get("new_music_playlist_enabled", True),
        "favourite_playlists_enabled": playlists_cfg.get("favourite_playlists_enabled", True),
        "decade_playlists_enabled": playlists_cfg.get("decade_playlists_enabled", False),
        "hit_singles_playlist_enabled": playlists_cfg.get("hit_singles_playlist_enabled", False),
        "hidden_gems_playlist_enabled": playlists_cfg.get("hidden_gems_playlist_enabled", False),
        "new_releases_playlist_enabled": playlists_cfg.get("new_releases_playlist_enabled", False),
        "covers_playlist_enabled": playlists_cfg.get("covers_playlist_enabled", False),
        "acoustic_playlist_enabled": playlists_cfg.get("acoustic_playlist_enabled", False),
        "high_energy_playlist_enabled": playlists_cfg.get("high_energy_playlist_enabled", False),
        "mood_playlists_enabled": playlists_cfg.get("mood_playlists_enabled", False),
        "exclude_christmas_from_playlists": playlists_cfg.get("exclude_christmas_from_playlists", True),
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

    # Preserve any secondary Navidrome users already configured on disk if the
    # wizard was opened from the Config page on an existing multi-user setup.
    existing_disk = read_config_from_disk()
    existing_users = existing_disk.get("navidrome_users", [])
    if isinstance(existing_users, list) and len(existing_users) > 1 and len(nav_users) == 1:
        merged_first = dict(existing_users[0]) if isinstance(existing_users[0], dict) else {}
        merged_first.update(nav_users[0])
        data["navidrome_users"] = [merged_first] + existing_users[1:]

    success = save_partial_config(data)
    if success:
        session["username"] = nav_users[0].get("user", "")
        try:
            reschedule_jobs_from_config(get_config())
        except Exception:
            pass
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
        existing_disk = read_config_from_disk()
        existing_users = existing_disk.get("navidrome_users", [])
        if isinstance(existing_users, list) and len(existing_users) > 1 and len(nav_users) == 1:
            merged_first = dict(existing_users[0]) if isinstance(existing_users[0], dict) else {}
            merged_first.update(nav_users[0])
            data["navidrome_users"] = [merged_first] + existing_users[1:]

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


def _fetch_recent_albums(*, days: int = 14, limit: int = 12) -> list[dict[str, Any]]:
    """Albums with tracks updated in the last ``days``, newest first.

    ⭐ ONE QUERY, TWO SURFACES. The dashboard's Recently Added card and the
    Activity page show the same list at different lengths; a second copy of this
    SQL is how the two start disagreeing about what "recently added" means.

    ⚠️ THE CUTOFF IS COMPUTED IN PYTHON, NOT IN SQL. It used to read
    ``CURRENT_TIMESTAMP - INTERVAL '14 days'``, which is PostgreSQL-only — on
    SQLite the statement raises, the caller's ``except`` swallows it, and the
    list comes back EMPTY. That made the query untestable and any bug in it
    invisible: the failure mode was identical to "you added nothing recently".
    A ``timedelta`` parameter is portable and uses the same clock as the rest of
    the app.

    ⚠️ ``limit`` is BOUND, not interpolated. It was a literal ``LIMIT 12`` in the
    SQL text, so a caller wanting a different length had to edit the string.
    """
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(days=max(1, int(days)))
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT
                        COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                        album,
                        MAX(updated_at) AS added_at,
                        COUNT(*) AS track_count,
                        MAX(COALESCE(NULLIF(musicbrainz_albumtype, ''),
                                     NULLIF(spotify_album_type, ''),
                                     NULLIF(releasetype, ''))) AS album_type,
                        MAX(COALESCE(
                            NULLIF(SUBSTRING(COALESCE(year, '') FROM '^[0-9]{4}'), ''),
                            NULLIF(CAST(release_year AS TEXT), '')
                        )) AS album_year
                    FROM tracks
                    WHERE updated_at >= :cutoff
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
                    LIMIT :limit
                """),
                {"cutoff": cutoff, "limit": max(1, int(limit))},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as exc:
        logger.debug("Recent albums query failed", error=str(exc))
        return []


@ui_bp.route("/activity")
async def activity() -> Any:
    """The full Activity view — every feed the dashboard card summarises.

    ⭐ WHY THIS EXISTS. The dashboard's activity card holds three feeds in tabs,
    each capped for a card (12 albums, 25 scans, 50 reviews). The card's own
    "View all" linked to `/downloads/monitor`, which is a downloads page — a
    destination that does not show any of the three feeds. This is the page that
    link should have pointed at all along.

    ⭐ MIGRATED TO THE LIVE TREE (2026-10-26). This route used to REDIRECT to
    the dashboard whenever the test_site cutover was off, which is the default —
    so the feature existed only for anyone who had switched UIs. The template
    and both scripts now exist here too, so the page renders for everyone.

    ⚠️ The redirect is GONE rather than kept as a fallback: a redirect that
    silently swaps the destination makes the feature look like it is missing,
    which is exactly how it was reported.
    """
    # Wider window and a longer list than the card: this page exists to show what
    # the card had to cut off, so a matching cap would defeat it.
    recent_albums = _fetch_recent_albums(days=30, limit=50)
    recent_scans, nav_users = [], []
    try:
        recent_scans = get_recent_album_scans(limit=100)
    except Exception as exc:
        logger.debug("Activity scans fetch failed", error=str(exc))

    return await render_template(
        "pages/activity.html",
        recent_albums=recent_albums,
        recent_scans=recent_scans,
        nav_users=nav_users,
        scan_running=False,
        stats={},
    )


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

        recent_albums = _fetch_recent_albums(days=14, limit=12)

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


def plan_artist_release_cards(
    tracks: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    """Decide which artist-page CARD each ``(album name, release group)`` pair
    belongs to.

    Returns ``{(name_key, release_group_or_empty): album_key}``. A row is looked
    up as ``plan[(name_key, its_own_rg)]``, falling back to
    ``plan[(name_key, "")]`` when it carries no release-group tag.

    ⭐⭐ THE ARTIST PAGE AND THE ALBUM PAGE MUST GROUP ALBUMS THE SAME WAY.
    Reported: "the artist page is sometimes showing releases multiple times,
    selecting edit or opening the album will only show one item with all the
    releases inside it." They disagreed in two places:

    * **Several release groups under one name.** ``album_detail`` opens the
      PRIMARY one (the most tracks) when the URL carries no scope, and lists the
      others only under an explicit ``/<release-group>`` address. Rows carrying
      no group belong to that primary release. This used to key such a row by
      its own YEAR, inventing a third card that no address could ever open.
    * **No release-group data at all.** ``album_detail`` merges every year for
      one name on purpose ("the year no longer scopes anything"). This page used
      to emit ``name + year`` cards, so a re-press tagged with its pressing
      year, or a compilation crediting its source recordings, appeared several
      times — every copy linking to the SAME single album page.

    ``album_key`` is the grouping key ``_build_artist_detail_payload`` uses for
    ``albums_by_key``/``tracks_by_key``, so the card and its tracks can never
    diverge.
    """
    counts: dict[tuple[str, str], int] = {}
    for track in tracks:
        name_key = str(track.get("album") or "").strip().lower()
        rg = str(track.get("musicbrainz_releasegroupid") or "").strip()
        if name_key and rg:
            counts[(name_key, rg)] = counts.get((name_key, rg), 0) + 1

    groups_by_name: dict[str, list[str]] = {}
    for name_key, rg in counts:
        groups_by_name.setdefault(name_key, []).append(rg)

    plan: dict[tuple[str, str], str] = {}
    for name_key, groups in groups_by_name.items():
        if len(groups) == 1:
            # ONE release → ONE card, whatever the per-track years say.
            only = groups[0]
            plan[(name_key, only)] = f"{name_key}::rg:{only}"
            plan[(name_key, "")] = plan[(name_key, only)]
            continue
        # Several releases share this name: one card EACH, so every one stays
        # browsable (and editable) from the artist page. First-seen order
        # breaks a tie, matching ``album_detail``'s
        # ``_primary = max(counts, key=lambda r: counts[r])``.
        primary = groups[0]
        for rg in groups[1:]:
            if counts[(name_key, rg)] > counts[(name_key, primary)]:
                primary = rg
        for rg in groups:
            plan[(name_key, rg)] = f"{name_key}::rg:{rg}"
        plan[(name_key, "")] = f"{name_key}::rg:{primary}"

    # A name with NO release-group data is absent from the plan on purpose: the
    # caller then uses the name-only card, which is what ``album_detail`` shows.
    return plan


def _build_artist_detail_payload(name: str) -> dict[str, Any]:
    """Build the artist-page template context.

    Deliberately SYNCHRONOUS and module-level. It performs blocking work —
    several ``db_session`` reads plus MusicBrainz/Last.fm lookups via
    ``get_artist_members_cached`` and ``get_artist_genre_sources`` — so it must
    run in a worker thread (``asyncio.to_thread``), never on the event loop.

    WHY THIS MATTERS: as an ``async`` handler this body used to run inline on
    the event loop. ``get_artist_members_cached`` calls MusicBrainz through
    ``_strict_throttle``, which enforces a global 1 req/s budget by SLEEPING to
    claim a future slot. While a popularity scan is running it saturates that
    budget, so this lookup can block for tens of seconds (the log shows 30-40s
    MusicBrainz calls and repeated "call still running" warnings). Every other
    request handled by the same hypercorn worker stalled behind it, which is
    the reported "app freezes when running a scan from the artist page".

    Only the render stays on the event loop; it needs the app/request context,
    which a worker thread does not have.
    """
    name = unquote(name or "").strip()
    cfg = get_config()

    # Is a scan running right now? The page's per-album "N missing" badges are
    # filled in client-side by /api/album/missing-tracks, which needs
    # MusicBrainz. A running scan OWNS the process-wide MusicBrainz budget
    # (1 req/s, reserve-then-sleep), so a page-load burst of those probes both
    # starves the scan and occupies the loop's default executor — the same pool
    # ``asyncio.to_thread`` uses for this very payload. Publishing the flag lets
    # the page skip the probes while the scan is working (see
    # static/js/artist-releases.js).
    #
    # Probed defensively: a failure here must never stop the page rendering.
    scan_active = False
    try:
        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        scan_active = bool(is_popularity_scan_active())
    except Exception as exc:
        logger.debug("Scan-active probe failed", artist=name, error=str(exc))

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
        """The artist page's genre chips, which are applied to EVERY track.

        ⚠️ Per-TRACK affiliation markers (Cover/Live/Acoustic/…) are DROPPED:
        these chips are checkboxes, and "Apply Selected" writes the chosen
        genres to every track of the artist. A "Cover" here would stamp the
        whole catalogue — the reported "albums are still assigning the cover
        genre to non cover tracks".
        """
        genre_fields = [
            "manual_genres", "navidrome_genres", "musicbrainz_genres",
            "spotify_genres", "discogs_genres", "lastfm_tags",
            "listenbrainz_genres", "essentia_genres", "mood",
        ]

        try:
            from services.enrichment.genre_aggregation_service import (
                is_track_affiliation_tag,
            )
        except Exception as exc:
            logger.debug("Artist genres: affiliation filter unavailable", error=str(exc))
            is_track_affiliation_tag = _fallback_is_affiliation_tag

        counts: dict[str, int] = {}
        display_names: dict[str, str] = {}

        for row in rows:
            for field in genre_fields:
                for genre in split_tag_values(row.get(field)):
                    if is_track_affiliation_tag(genre):
                        continue
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

    # ⚠️ Group by RELEASE, not by year. A compilation credits its source
    # recordings, so the tracks of ONE release carry many different years —
    # `2003 - Battlefield Vietnam` holds tracks tagged 1963 … 2004 — and
    # "name + year" split that single release into several cards (the reported
    # `/2004` and `/1963` links being the same release, same MBID).
    def _release_group_of(t: dict[str, Any]) -> str:
        return str(t.get("musicbrainz_releasegroupid") or "").strip()

    # ⭐ ONE grouping rule, in ``plan_artist_release_cards``, so the artist page
    # and the album page can be compared and tested side by side.
    card_plan = plan_artist_release_cards(tracks)

    tracks_by_key: dict[str, list[dict[str, Any]]] = {}

    for track in tracks:
        album_name = str(track.get("album") or "").strip()
        if not album_name:
            continue

        track_year = _leading_year(track)
        _name_key = album_name.lower().strip()
        rg = _release_group_of(track)

        # The card this row belongs to. A row carrying no release-group tag
        # joins the name's DEFAULT card (the primary release when several
        # exist) rather than inventing a year-keyed one — see
        # ``plan_artist_release_cards`` for why that mattered.
        album_key = (
            card_plan.get((_name_key, rg))
            or card_plan.get((_name_key, ""))
            # No release-group data for this name at all: ONE card per name,
            # mirroring the album page's merge across years.
            or f"{_name_key}::name"
        )
        entry_rg = rg or (
            album_key.rsplit("::rg:", 1)[1] if "::rg:" in album_key else ""
        )

        tracks_by_key.setdefault(album_key, []).append(track)

        if album_key not in albums_by_key:
            albums_by_key[album_key] = {
                "album": album_name,
                "album_year": track_year,
                # The RELEASE this card represents — the album page is scoped
                # by it (``/album/<artist>/<album>/<rg>``), which is what keeps
                # several releases sharing a name browsable side by side.
                "release_group_mbid": entry_rg,
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
                    or track.get("album_type")
                    or track.get("releasetype"),
                "album_type": track.get("musicbrainz_albumtype")
                    or track.get("album_type")
                    or track.get("spotify_album_type")
                    # LAST: ``releasetype`` is the tag value (file tag or
                    # AlbumID3 ``releaseTypes``) or the track-count guess a
                    # Navidrome import derives. The confirmed column above it
                    # still wins, so this only shows a type for an album no
                    # metadata scan has classified yet.
                    or track.get("releasetype"),
                "is_missing": False,
            }

        album_entry = albums_by_key[album_key]
        album_entry["track_count"] += 1

        duration = safe_float(track.get("duration"))
        if duration:
            album_entry["total_duration"] += duration

        year = _leading_year(track)
        # The card's year is the ALBUM PAGE's year: the MAXIMUM of the original
        # years on its rows (``album_detail`` computes ``max(year_values)``).
        # Taking the first track's year made a merged card display whichever
        # year the scan reached first, so one album could read 1996 on one load
        # and 1997 on the next — a second way the "same album, several rows"
        # confusion showed up.
        if year and (album_entry.get("album_year") is None or year > album_entry["album_year"]):
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
        # The same key that grouped them — never a re-derivation. Filtering by
        # name + year here would put the wrong tracks on a release-group card.
        album_tracks = tracks_by_key.get(album_key, [])

        stars = [
            safe_float(track.get("stars"))
            for track in album_tracks
            if safe_float(track.get("stars")) is not None
        ]

        album_entry["avg_stars"] = (
            round(sum(stars) / len(stars), 2) if stars else None
        )

        album_entry["tracks"] = sort_album_tracks(album_tracks)

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

    # ⭐⭐ TOP TRACKS ARE ORDERED ACROSS ALBUMS, STAR TIER FIRST.
    #
    # The previous key led with ``artist_z_score``, which is measured against
    # the ALBUM's own spread — so on a 3-track live album (uniformly small
    # counts, so a small spread) a track nobody has listened to earns a
    # respectable z and outranks the artist's global single. That is the same
    # defect class as "the album-relative score ranked a live cut #1".
    #
    # ``star_then_popularity_order_key`` is the SHARED rule: stars, then the
    # cross-album ``playlist_popularity_score``, then the stored score, then
    # the title. It is deliberately the same shape as the generated playlists'
    # ``_playlist_order_key`` so "top" cannot mean two things in one app.
    top_tracks = sorted(tracks, key=star_then_popularity_order_key)[:20]

    for track in top_tracks:
        if track.get("artist_z_score") is None:
            track["artist_z_score"] = track.get("final_score") or 0

        if track.get("popularity_score") is None:
            track["popularity_score"] = track.get("final_score") or 0

        # The number the row is ACTUALLY ranked by, so the page can show what
        # it sorts on. Displaying ``popularity_score`` here while ordering by
        # something else is how a table ends up looking unsorted.
        track["rank_score"] = cross_album_track_score(track)

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

    #: Missing + upcoming release entries for the discography sections.
    #:
    #: The decision rules live in
    #: ``services.catalog.artist_release_entries`` as a PURE function, because
    #: inline here they were untestable without driving the whole page (DB plus
    #: MusicBrainz-adjacent lookups), and the rules are subtle: upcoming is
    #: decided from the DATE (not the year, which was wrong for most of the
    #: calendar), it overrides the release TYPE, undated rows are never
    #: promoted, and one entry per title is emitted across BOTH sources.
    missing_rows: list[dict[str, Any]] = []
    upcoming_rows: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            missing_rows = [
                dict(r._mapping)
                for r in session.execute(
                    text("""
                        SELECT title, release_id, primary_type, first_release_date,
                               cover_art_url, category
                        FROM missing_releases
                        WHERE LOWER(artist) = LOWER(:name)
                        ORDER BY first_release_date DESC NULLS LAST
                    """),
                    {"name": name},
                ).fetchall() or []
            ]
    except Exception as exc:
        logger.debug("Failed to load missing releases", artist=name, error=str(exc))

    # `upcoming_releases` is filled by an INDEPENDENT pipeline
    # (`services/upcoming_releases/`: Wikipedia scraper + MusicBrainz fetcher)
    # that drives the Upcoming Releases page. The artist page never read it, so
    # an announcement already discovered there stayed invisible until the scan
    # happened to cache the same release group into `missing_releases`.
    # Probed defensively: a missing/older schema must not stop the page.
    try:
        with db_session() as session:
            upcoming_rows = [
                dict(r._mapping)
                for r in session.execute(
                    text("""
                        SELECT album_name, release_date, release_group_mbid,
                               primary_type, release_year
                        FROM upcoming_releases
                        WHERE LOWER(TRIM(artist_name)) = LOWER(TRIM(:name))
                    """),
                    {"name": name},
                ).fetchall() or []
            ]
    except Exception as exc:
        logger.debug(
            "Failed to load upcoming releases", artist=name, error=str(exc),
        )

    missing_entries = build_missing_and_upcoming_entries(
        missing_rows=missing_rows,
        upcoming_rows=upcoming_rows,
        owned_titles=[
            str(a.get("album") or "") for a in albums
        ] + [str(a.get("title") or "") for a in albums],
    )

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

    # Alternate names (Electric Callboy / Eskimo Callboy): the About tab's
    # list, plus the group's canonical name so the page can say "merged as".
    # A failure must never take the page down — the section simply renders
    # empty.
    artist_aliases: list[str] = []
    artist_alias_canonical = name
    try:
        from services.metadata.artist_alias_service import get_alias_group

        _alias_group, _alias_code = get_alias_group(name)
        if _alias_code == 200:
            artist_aliases = list(_alias_group.get("aliases") or [])
            artist_alias_canonical = str(_alias_group.get("canonical") or name)
    except Exception as exc:
        logger.debug("Failed to load artist aliases", artist=name, error=str(exc))

    # ── "Included in Playlists" ────────────────────────────────────────────
    #
    # A plain indexed read of the playlist index the scan's finalise pass
    # maintains — NOT a live playlist walk, which would cost one Subsonic
    # request PER PLAYLIST on every page load, and the artist page is the most
    # visited page in the app.
    #
    # A failure leaves the section empty rather than taking the page down: it is
    # a convenience view, not the reason the page exists, and this whole payload
    # is built on a worker thread where an escaping exception is a 500.
    artist_playlists: list[dict[str, Any]] = []
    try:
        from services.playlists.playlist_index_service import playlists_for_artist

        artist_playlists = playlists_for_artist(name)
    except Exception as exc:
        logger.debug("Failed to load artist playlists", artist=name, error=str(exc))

    return {
        "artist_name": name,
        "albums": albums,
        "stats": stats,
        "top_tracks": top_tracks,
        "genre_sources": genre_sources,
        "genres": genres,
        "albums_by_category": albums_by_category,
        # Ordered (key, label, icon) specs for the sections that actually have
        # content — the template loops this instead of hard-coding sections.
        "album_categories": album_categories,
        "appears_on_albums": appears_on_albums,
        "artist_bio": artist_bio,
        "artist_country": artist_country,
        "artist_members": artist_members,
        "artist_aliases": artist_aliases,
        "artist_alias_canonical": artist_alias_canonical,
        "similar_artists": similar_artists,
        "scan_active": scan_active,
        "slskd_config": cfg.get("slskd", {}),
        # Playlists containing any of this artist's tracks, grouped by playlist.
        # Maintained by the scan (services/playlists/playlist_index_service).
        "artist_playlists": artist_playlists,
    }


@ui_bp.route("/artist/<path:name>")
async def artist_detail(name: str) -> Any:
    """Render the artist page.

    The context build is blocking (sync DB reads + rate-limited MusicBrainz
    lookups), so it is executed in a worker thread. Doing that work inline on
    the event loop froze every request in this worker while a scan held the
    MusicBrainz rate-limit budget.
    """
    payload = await asyncio.to_thread(_build_artist_detail_payload, name)
    return await render_template("pages/artist_detail_v2.html", **payload)


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


def _fetch_album_mb_backfill(
    album_mbid: str,
    album_type: str,
    release_values: dict[str, str],
) -> dict[str, Any]:
    """MusicBrainz backfill for one album save — BLOCKING, call via a thread.

    Returns the album-level values to merge (each only filled when the caller
    passed nothing) plus the per-recording track map
    (``recording_mbid -> {writer, is_cover, original_cover_artist,
    musicbrainz_genres, work_mbid}``).

    ⚠️ This runs two calls through the shared MusicBrainz client, whose
    ``_strict_throttle`` SLEEPS to reserve a slot in a 1 req/s budget shared
    with any running scan (the production log shows 30-40s calls). Running it
    inline on ``album_detail``'s event loop is what made saving metadata time
    out and stalled every other request in the same hypercorn worker.
    """
    out: dict[str, Any] = {
        "albumartist_mbid": "",
        "albumtype": album_type,
        "albumstatus": release_values.get("releasestatus") or "",
        "releasecountry": release_values.get("releasecountry") or "",
        "originalyear": release_values.get("originalyear") or "",
        "track_map": {},
    }
    if not album_mbid:
        return out
    try:
        from services.enrichment.musicbrainz_service import (
            fetch_musicbrainz_release_metadata,
            get_shared_mb_client,
        )

        mb_release = fetch_musicbrainz_release_metadata(album_mbid)
        if not mb_release:
            return out

        # Re-fetch the raw release for artist-credit MBIDs (the metadata helper
        # returns display names only).
        raw = get_shared_mb_client().get_release(
            album_mbid, inc="artist-credits+release-groups",
        )
        if raw:
            credit = raw.get("artist-credit") or []
            if credit:
                first = credit[0]
                art = first.get("artist") or {}
                if isinstance(art, dict):
                    out["albumartist_mbid"] = str(art.get("id") or "")
            if not out["albumtype"]:
                out["albumtype"] = (
                    ((raw.get("release-group") or {}).get("primary-type") or "")
                    or album_type
                )
            if not out["albumstatus"]:
                out["albumstatus"] = str(raw.get("status") or "")
            if not out["releasecountry"]:
                out["releasecountry"] = str(raw.get("country") or "")
            if not out["originalyear"]:
                out["originalyear"] = (
                    ((raw.get("release-group") or {}).get("first-release-date") or "")
                )[:4]

        # Per-recording enrichment: writer / cover / genre data per track.
        for mt in (mb_release.get("tracks") or []):
            recording = str(mt.get("recording_mbid") or "").strip()
            if not recording:
                continue
            entry: dict[str, Any] = {}
            if mt.get("writer"):
                entry["writer"] = mt["writer"]
            if mt.get("is_cover"):
                entry["is_cover"] = True
                if mt.get("original_cover_artist"):
                    entry["original_cover_artist"] = mt["original_cover_artist"]
            if mt.get("musicbrainz_genres"):
                entry["musicbrainz_genres"] = mt["musicbrainz_genres"]
            if mt.get("work_mbid"):
                entry["work_mbid"] = mt["work_mbid"]
            if entry:
                out["track_map"][recording] = entry
    except Exception as exc:
        logger.debug("Album MB backfill failed", error=str(exc))
    return out


#: Columns that count as GENRE EVIDENCE for one track. When any of them
#: holds a value, the track's own genres outrank the ALBUM's genre list on
#: save: that list is itself an aggregate of these columns across the album
#: (see ``collect_top_genres``), so writing it back flattened every track to
#: the album blend — the reported "album genres overwrite track genres when
#: an album is saved" — and fabricated ``manual_genres``, which the next
#: aggregation then read as per-track evidence, making the overwrite stick.
_TRACK_GENRE_EVIDENCE_FIELDS: tuple[str, ...] = (
    "genres",
    "manual_genres",
    "navidrome_genres",
    "musicbrainz_genres",
    "lastfm_tags",
    "listenbrainz_genres",
    "discogs_genres",
    "spotify_genres",
    "essentia_genres",
    "audiodb_genres",
    "wikidata_genres",
)

#: Genre-family keys a tag payload can carry. They are dropped from every
#: FILE write the album save makes: the save — and the Lookup MBID review that
#: stages per-track values into it — may only write the DATABASE. Genres reach
#: the files from the popularity / finalise scan's
#: ``sync_album_file_tags`` (and from the two explicit genre actions,
#: ``apply_genres_to_album`` / ``bulk_tag_tracks``). The "Cover" marker is set
#: AFTER this strip and is unaffected — it is the cover convention, not genre
#: data.
_FILE_GENRE_TAG_FIELDS: tuple[str, ...] = (
    "genre",
    "genres",
    "musicbrainz_genres",
    "lastfm_genres",
    "lastfm_tags",
    "navidrome_genres",
    "manual_genres",
    "discogs_genres",
    "listenbrainz_genres",
    "essentia_genres",
    "spotify_genres",
    "audiodb_genres",
    "wikidata_genres",
)


def _has_genre_value(raw: Any) -> bool:
    """True when a stored genre column actually holds genres.

    JSONB columns come back as lists, TEXT as strings; empty containers and
    the literal markers ``[]`` / ``null`` count as no evidence at all.
    """
    if raw is None:
        return False
    if isinstance(raw, (list, tuple, set)):
        return any(str(v).strip() for v in raw)
    if isinstance(raw, dict):
        return bool(raw)
    text_val = str(raw).strip()
    if not text_val:
        return False
    return text_val.casefold() not in {"[]", "{}", "null", "none"}


def _track_keeps_its_own_genres(
    track: dict[str, Any],
    staged: dict[str, Any] | None = None,
) -> bool:
    """Whether this track's genres outrank the album's genre list.

    "Genres attached to tracks for MusicBrainz, Last.fm, etc should have a
    greater preference than genres saved to albums." A track with ANY genre
    evidence of its own keeps it; only a track with no genres at all receives
    the album's list — a fill, never an overwrite.

    ``staged`` is the Lookup-MBID review for this track: a per-track
    ``musicbrainz_genres`` lands in its column later in the SAME loop, so it
    must count as evidence now — otherwise the album list wins the race and
    the reviewed value arrives next to a ``genres`` that contradicts it.
    """
    if any(_has_genre_value(track.get(field)) for field in _TRACK_GENRE_EVIDENCE_FIELDS):
        return True
    return bool(staged) and _has_genre_value(staged.get("musicbrainz_genres"))


def _apply_album_track_genres(track_id: Any, genres_str: str) -> tuple[int, int]:
    """Write one track's genre chips to the **database only**.

    Only ever called for a track with NO genre evidence of its own — see
    ``_track_keeps_its_own_genres`` — so this is a fill, and the value lands
    in ``genres`` alone (never ``manual_genres``, which is per-track).

    Returns ``(rows_written, failed)``.

    ⚠️ Deliberately does NOT touch the audio file. File genres belong to the
    popularity scan, which syncs them DB → file via
    ``album_tag_sync_service.sync_album_file_tags``; writing them here meant
    two writers for the same tag, and the save's own value only survives a
    Navidrome import because the file had been updated too.

    Genres bypass ``payload`` and are counted separately — otherwise a
    genres-only save reports "No changes were made". A JSONB rejection or a
    connection blip must not vanish silently either: it is counted and logged
    at WARNING.

    ⚠️ The ALBUM's genre list may not carry a per-TRACK affiliation marker.
    "Cover" (and Live/Acoustic/…) describes ONE recording's performance, and
    this fill writes the album's list onto a track that has no genre evidence
    of its own — so a "Cover" in the album list lands on every non-cover
    track. That is the reported "albums are still assigning the cover genre to
    non cover tracks if the tag is attached to a album genres". The album-level
    sources are filtered too (see ``get_album_genre_sources``), so this is the
    LAST line of defence for a list that reached the form by any other route.

    ⚠️⭐ ONLY MUSICAL GENRES PROPAGATE. The Christmas/Holiday family is
    excluded here as well: it describes the ALBUM's occasion, not a fact about
    a track that has no genres of its own. A Christmas compilation must not
    stamp "Christmas" (or "Holiday") on every one of its tracks — the
    reported "Holiday, Christmas, Cover shouldn't automatically assign to all
    tracks if the track doesn't have any genres. Only musical genres should
    propagate to all tracks."

    ⚠️ The filter FAILS CLOSED. If the helper cannot be imported, the
    affiliation markers are still dropped (by name) — a filter that silently
    stopped filtering would re-open exactly the bug this guards.
    """
    genres_list = [
        g.strip()
        for g in re.split(r"[,;/\\]+", genres_str)
        if g.strip()
    ]
    try:
        from services.enrichment.genre_aggregation_service import (
            is_propagatable_genre,
        )
    except Exception as exc:
        logger.debug("Album save: affiliation filter unavailable", error=str(exc))
        is_propagatable_genre = _fallback_is_affiliation_tag

    genres_list = [g for g in genres_list if is_propagatable_genre(g)]
    if not genres_list:
        return 0, 0

    rows = 0
    failed = 0
    from db.repositories.metadata import update_track_genres

    try:
        # ``write_manual=False``: the album's list may fill a track's display
        # genres but must never become its ``manual_genres`` — that column is
        # a per-track SOURCE the aggregators read back, so stamping it with
        # the album blend would fabricate manual genres for every track.
        rows = update_track_genres(
            track_id=track_id, genres_str=", ".join(genres_list),
            write_manual=False,
        ) or 0
    except Exception as genre_err:
        failed = 1
        logger.warning(
            "Album save: genre write FAILED",
            track_id=track_id, error=str(genre_err),
        )
    return rows, failed


def _persist_album_track_payloads(
    payloads: list[dict[str, Any]],
) -> tuple[int, int]:
    """Write every collected track payload in ONE session + commit.

    Returns ``(updated, failed)``.

    ⚠️ ONE TRANSACTION, NOT ONE PER ROW. The save used to open a fresh
    ``db_session`` and COMMIT for every track (the old singular helper called
    ``insert_or_update_track`` per row), so an album cost N transactions. That
    per-row commit is the wall clock behind the reported slow save — a
    14-track album spending ~68 s in the DB loop.
    ``upsert_tracks_bulk`` runs the whole album through a single session and
    wraps each row in a SAVEPOINT, so one malformed row cannot abort the rest
    (PostgreSQL aborts the whole transaction on a failed statement otherwise).

    Counting is all-or-nothing at the BATCH level: ``upsert_tracks_bulk``
    returns False when ANY row failed, and each bad row is logged with its
    ``track_id`` by the repository. Reporting the batch as a FAILURE rather
    than a no-op is the safe side of the contract this file keeps defending —
    a refused write must never read as "No changes were made."
    """
    if not payloads:
        return 0, 0
    try:
        if upsert_tracks_bulk(payloads):
            # The DB correction is authoritative even if a later FILE write
            # fails. Mark it before the file phase so a stale Navidrome import
            # can never reverse it; a verified import clears the mark only when
            # it observes the corrected values.
            try:
                from services.scanning.reconciliation import (
                    mark_local_tags_written_for_track_ids,
                )

                mark_local_tags_written_for_track_ids(
                    [str(p.get("id")) for p in payloads if p.get("id")]
                )
            except Exception as exc:
                logger.debug("Album save metadata authority stamp failed", error=str(exc))
            return len(payloads), 0
        return 0, len(payloads)
    except Exception as db_err:
        logger.warning(
            "Album save: bulk DB update FAILED",
            track_count=len(payloads), error=str(db_err),
        )
        return 0, len(payloads)


def _write_album_track_file_tags(
    track_id: Any,
    track: dict[str, Any],
    payload: dict[str, Any],
    *,
    strip_disc_numbers: bool,
    disc_staged: bool,
) -> tuple[bool, str]:
    """Write one track's tags to its audio file. Returns ``(ok, file_path)``.

    Carries the conventions the DB payload cannot express:

    * the "Cover" genre, mirroring the standalone cover-detection area;
    * the single-disc ``disc_number=""`` clear — ``build_tag_updates`` drops
      empty values, so it must be pushed explicitly. Skipped when the review
      supplied a disc number: the strip is an album-level heuristic while a
      staged value is a confirmed correction, and this line used to undo that
      correction on the file after the DB had accepted it;
    * the Release Name → ``albumversion`` write/clear, for the same reason (see
      the comment on it below).
    """
    resolved = resolve_music_file_path(track.get("file_path"))
    if not resolved:
        return False, resolved
    try:
        file_tags = build_tag_updates(payload)
        # Genres never leave this path for the FILES: a Lookup MBID review
        # stages per-track ``musicbrainz_genres`` into the same payload, and
        # writing them here made the LOOKUP the genre writer — genres may only
        # be written to files by the popularity / finalise scan's sync. The DB
        # payload (Phase 2) is untouched: the values still land in the tables.
        for _genre_field in _FILE_GENRE_TAG_FIELDS:
            file_tags.pop(_genre_field, None)
        if payload.get("is_cover"):
            existing = file_tags.get("genres") or file_tags.get("genre")
            if isinstance(existing, list):
                if "cover" not in [str(g).lower() for g in existing]:
                    file_tags["genre"] = "; ".join([str(g) for g in existing] + ["Cover"])
            else:
                file_tags["genre"] = "Cover"
        if strip_disc_numbers and not disc_staged:
            file_tags["disc_number"] = ""
        # ── The RELEASE NAME → the file's ALBUMVERSION frame ────────────────
        # ⚠️ ANOTHER FIELD THAT COULD NOT BE CLEARED, and the one that makes a
        # cleared Release Name come BACK.
        #
        # Two reasons it needed handling here rather than through
        # ``build_tag_updates``: ``release_title`` is not in that function's
        # column map at all (the FILE's name for it is ``albumversion``), and
        # that function deliberately DROPS empty values — so an explicit clear
        # could never reach the writer. Written explicitly, like the
        # single-disc ``disc_number`` clear above.
        #
        # Leaving the old frame in the file is not cosmetic: Navidrome reads
        # ``albumversion`` and appends it to the album NAME as a subtitle (see
        # ``strip_appended_album_version``), so a file still carrying the deleted
        # edition would re-clip a name the album page no longer shows.
        # ``albumversion`` in the payload wins when a caller supplies it directly.
        if "release_title" in payload and "albumversion" not in payload:
            file_tags["albumversion"] = str(payload.get("release_title") or "").strip()

        # ── Cleared identifiers must reach the FILE too ─────────────────────
        # ``build_tag_updates`` drops empty values, so an explicitly cleared
        # MBID never reached the writer. The old frame then survived in the
        # file, and a later Navidrome import could read it back and undo the
        # removal. Pushing "" is the writers' explicit "delete this frame"
        # signal (the same contract the disc/release-name clears above use).
        for _column, _tag_name in (
            ("musicbrainz_album_mbid", "musicbrainz_albumid"),
            ("musicbrainz_albumid", "musicbrainz_albumid"),
            ("musicbrainz_releasegroupid", "musicbrainz_releasegroupid"),
            ("musicbrainz_artistid", "musicbrainz_artistid"),
            ("musicbrainz_albumartistid", "musicbrainz_albumartistid"),
        ):
            if _column in payload and not str(payload.get(_column) or "").strip():
                file_tags[_tag_name] = ""
        if file_tags:
            return bool(update_file_tags(resolved, file_tags)), resolved
    except Exception as tag_err:
        logger.debug("File tag write failed", track_id=track_id, error=str(tag_err))
    return False, resolved


def _write_album_track_files_bounded(
    jobs: list[tuple[Any, dict, dict, bool, bool]],
) -> list[tuple[bool, str]]:
    """Run one album save's per-track FILE writes, bounded. SYNC.

    Each job is ``(track_id, track, payload, strip_disc_numbers, disc_staged)``
    — the exact arguments of :func:`_write_album_track_file_tags`, which does the
    actual write.

    ⚠️ WHY THIS EXISTS. The save used to write each file inside its per-track
    loop, so an album cost N SEQUENTIAL disk passes (each one reads the file
    twice to honour ``preserve_file_timestamps`` and then rewrites it). The loop
    was the wall clock behind the reported **Cloudflare 524** — an album save
    that took longer than Cloudflare's 100s origin timeout. Tracks are
    independent files and every write is an atomic temp+replace, so they can run
    together; the pool bounds how many hit one disk at once.

    ⚠️ SYNC ON PURPOSE, and it is the ONE implementation of this phase. It is
    called from a worker thread inline (``asyncio.to_thread``) and directly from
    the background service's own thread, so a small album and a large one go
    through exactly the same code — same cap, same ordering, same crash
    isolation. An ``async`` version would have forced the background path to spin
    up a nested event loop to reach it.

    Returns one ``(ok, file_path)`` per job, IN ORDER, so the caller's failure
    counter still attributes the right file to the right track. One track
    blowing up must not cost the rest of the album its tags.
    """
    if not jobs:
        return []

    def _one(job: tuple[Any, dict, dict, bool, bool]) -> tuple[bool, str]:
        track_id, track, payload, strip_disc_numbers, disc_staged = job
        try:
            # ``strip_disc_numbers``/``disc_staged`` are KEYWORD-ONLY on the
            # phase, so they cannot ride along in an ``*job`` splat.
            return _write_album_track_file_tags(
                track_id, track, payload,
                strip_disc_numbers=strip_disc_numbers,
                disc_staged=disc_staged,
            )
        except Exception as exc:  # defensive: keep the other tracks going
            logger.warning(
                "File tag write CRASHED",
                track_id=track_id, error=str(exc),
            )
            return (False, "")

    # ``ThreadPoolExecutor.map`` preserves INPUT order, which the caller depends
    # on to attribute a failure to its own file.
    with ThreadPoolExecutor(max_workers=_FILE_WRITE_CONCURRENCY) as pool:
        return list(pool.map(_one, jobs))


def _run_album_file_sync_phases(payload: dict[str, Any]) -> dict[str, Any]:
    """The album save's DISK phase: file tags → cover art → Navidrome rescan.

    SYNC, and the ONE implementation of this phase. It is reached two ways:

    * **inline**, through ``asyncio.to_thread``, for an album small enough that
      the request comfortably fits inside Cloudflare's 100s origin window;
    * **in the background**, on the album-file-sync service's own thread, for an
      album large enough that it does not (the reported 524).

    Keeping one implementation is what makes the background path trustworthy: the
    tag-write policy gates, the concurrency cap and the crash isolation are the
    same code the inline path has always run.

    ``payload`` carries plain data only — the job outlives the request that
    created it, so it can hold no request or session objects.

    ⚠️ THE RESCAN IS THE LAST STEP, AFTER THE TAGS AND THE COVER. Navidrome serves
    its own database and only refreshes when it re-scans; a rescan that ran before
    the files were written would import the PRE-save values and the next import
    would put the old metadata back. Backgrounding the writes without moving the
    rescan would have introduced exactly that bug.
    """
    jobs = payload.get("file_jobs") or []
    _tags_started = time.monotonic()
    results = _write_album_track_files_bounded(jobs)
    tags_ms = (time.monotonic() - _tags_started) * 1000.0

    file_failures = 0
    # Reported per file for BOTH paths. A backgrounded save used to be the case
    # where this diagnostic disappeared, because the response had already gone.
    for job, (ok, resolved_file) in zip(jobs, results):
        if not ok:
            file_failures += 1
            logger.warning(
                "File tag write SKIPPED - DB updated only",
                track_id=job[0], file_path=resolved_file,
            )

    cover_embedded = 0
    cover_bytes = payload.get("cover_bytes")
    _cover_started = time.monotonic()
    if cover_bytes:
        try:
            from services.enrichment.album_art_service import (
                apply_album_art_to_tracks,
            )

            cover_embedded = apply_album_art_to_tracks(
                str(payload.get("album_artist") or ""),
                str(payload.get("album") or ""),
                cover_bytes,
                str(payload.get("cover_mime") or "image/jpeg"),
            ) or 0
        except Exception as _cover_exc:
            logger.debug("Album save cover embed failed", error=str(_cover_exc))
    cover_ms = (time.monotonic() - _cover_started) * 1000.0

    rescanned = False
    if payload.get("should_rescan"):
        try:
            from services.scanning.navidrome_rescan_service import request_rescan

            rescanned = bool(request_rescan("album metadata save"))
        except Exception as _rescan_exc:
            logger.debug(
                "Post-save Navidrome rescan request failed", error=str(_rescan_exc)
            )

    return {
        "file_failures": file_failures,
        "cover_embedded": cover_embedded,
        "rescanned": rescanned,
        "tracks": len(jobs),
        # ⚠️ PER-STEP timings, and they are the point rather than a nicety.
        # "file_tags_ms" used to be the WHOLE disk phase, so a report of "the
        # save is slow" could not say WHICH pass was slow — and the two passes
        # fail for completely different reasons (tag writes scale with the
        # track count, the cover embed scales with file SIZE and runs on a
        # second full rewrite of every file). The two are now separately
        # bounded and separately measured, so the next report names one.
        "tags_ms": round(tags_ms, 1),
        "cover_ms": round(cover_ms, 1),
    }


def _resolve_album_cover_bytes(
    cover_url: str,
    album_mbid: str,
    album_rg_mbid: str,
) -> tuple[bytes | None, str]:
    """Fetch the image bytes for an album save — BLOCKING, call via a thread.

    Handles the three shapes the album page can submit: an inline ``data:``
    URL, a plain HTTP(S) URL (the reported gap: a URL-set cover never reached
    the album art or the audio files), or neither — in which case the Cover Art
    Archive is asked for the release MBID (then the release group).

    Returns ``(bytes, mime_type)``; ``bytes`` is ``None`` when nothing could
    be resolved, and failures are logged rather than raised so a bad URL can
    never fail the save.
    """
    if str(cover_url).startswith("data:"):
        import base64 as _b64

        header, _, b64p = cover_url.partition(",")
        mime = header[5:].split(";")[0] or "image/jpeg"
        try:
            return _b64.b64decode(b64p), mime
        except Exception:
            return None, "image/jpeg"

    if str(cover_url).startswith("http"):
        import httpx as _httpx

        try:
            resp = _httpx.get(cover_url, timeout=10)
            if resp.status_code == 200 and resp.content:
                return resp.content, (
                    resp.headers.get("content-type") or "image/jpeg"
                )
        except Exception as exc:
            logger.debug("Album cover URL download failed", url=cover_url, error=str(exc))
        return None, "image/jpeg"

    if album_mbid:
        # Prefer the Cover Art Archive via the release MBID.
        from api_clients.coverartarchive import (
            get_release_front_image_bytes,
            get_release_group_front_image_bytes,
        )

        try:
            image = get_release_front_image_bytes(album_mbid, size="500")
            if image is None and album_rg_mbid:
                image = get_release_group_front_image_bytes(album_rg_mbid, size="500")
            if image:
                return image, "image/jpeg"
        except Exception as exc:
            logger.debug("Cover Art Archive fetch failed", release_mbid=album_mbid, error=str(exc))
    return None, "image/jpeg"


#: How long an album save may wait for the MusicBrainz enrichment.
#:
#: The backfill runs two calls through the SHARED MusicBrainz client, whose
#: throttle sleeps to claim a slot in a 1 req/s budget a running scan also
#: spends (production has seen 30-40s calls). Cloudflare ends an unanswered
#: origin request after 100s (error 524 — the reported "timeout when
#: saving"), so the enrichment is given a deadline and the save continues
#: with the values the form already carries: those fields are additive, and a
#: later save or scan fills anything a deadline dropped.
_MB_BACKFILL_DEADLINE_SECONDS = 15.0

#: How many of the album's audio files are rewritten at the same time.
#:
#: One write reads the file twice (``preserve_file_timestamps`` compares the
#: bytes before/after) and then rewrites it — measured at ~370ms for an 8MB
#: MP3 on a local disk, seconds each on a NAS. Doing that once per track in
#: sequence is what made an album-sized save outlive the same 100s timeout.
#: Four keeps the wall clock near a quarter of the sequential cost while
#: leaving the disk (and a network mount) enough headroom to stay fast.
_FILE_WRITE_CONCURRENCY = 4

#: How long the album save may spend FETCHING the cover image.
#:
#: The CAA client has a 10s per-request timeout, but a request is not the unit
#: that matters here: ``follow_redirects=True`` gives EVERY hop its own 10s, and
#: the resolver tries the release and then (on a miss) the release group, so the
#: sequence is unbounded in practice. ``_httpx.get(cover_url, timeout=10)`` on the
#: plain-URL branch has the same shape.
#:
#: This is the same defect the MusicBrainz backfill had — an ``await`` off the
#: event loop is not the same as an ``await`` UNDER A DEADLINE — so it gets the
#: same treatment, and for the same reason: a cover is additive. Losing it costs
#: one picture; waiting past the origin timeout costs the whole save.
_COVER_FETCH_DEADLINE_SECONDS = 12.0

#: How long the INLINE disk phase may run before the save responds without it.
#:
#: ⚠️ THE BACKSTOP. The phases above are individually bounded, but the disk phase
#: is the one whose cost is set by the USER'S STORAGE rather than by this code:
#: N file rewrites for the tags plus N more for the cover, at ~370ms each on a
#: local disk and seconds each on a network mount. Two albums of the same track
#: count can differ by an order of magnitude, which is why a per-phase deadline
#: cannot be tuned to be both safe and useful.
#:
#: So the budget is set against the thing that actually matters — the 100s
#: origin timeout — with room reserved for everything that runs before it
#: (backfill 15s + cover 12s + the database loop). Past the budget the response
#: goes back and the worker thread FINISHES ON ITS OWN: the tags still land, the
#: user is told they are still being written, and a later save or scan confirms
#: them. That is the same contract the MB backfill deadline already uses.
#:
#: This is a backstop, not the fix — the concurrent pools are what bring a
#: normal album comfortably inside it.
_INLINE_DISK_BUDGET_SECONDS = 45.0

#: Hard ceiling for the WHOLE save, kept under Cloudflare's 100s origin timeout
#: with headroom for the redirect + flash. The inline disk phase gets whatever
#: is left of this (capped by ``_INLINE_DISK_BUDGET_SECONDS``), so slow earlier
#: phases can no longer push the total past the 524 line.
_SAVE_TOTAL_BUDGET_SECONDS = 85.0

#: The inline disk phase always gets at least this long, so a save never
#: abandons its tagging instantly just because the DB phase was slow.
_INLINE_DISK_MIN_SECONDS = 5.0

# A release-group MBID is the identity of a RELEASE — the same album NAME can
# belong to several of them (a re-issue, a deluxe edition), and a single
# release's tracks can carry several different YEARS (a compilation credits the
# original recordings). Scoping by release group therefore beats scoping by
# year: digits in the path segment keep meaning a year, so existing links work.
_RG_SEGMENT_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


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

    # The final path segment is a SCOPE: a UUID selects one release group,
    # digits select a year (kept for existing links), anything else selects
    # nothing.
    album_rg_filter = (
        album_year_seg if _RG_SEGMENT_RE.fullmatch(album_year_seg) else ""
    )

    try:
        album_year_filter = int(album_year_seg) if album_year_seg.isdigit() else None
    except (TypeError, ValueError):
        album_year_filter = None

    # Distinguish "the URL asked for a year" from "a year was chosen for me" —
    # the latter happens below when no scope is given at all.
    explicit_year = album_year_filter is not None

    if album_year_filter is not None and not (1900 <= album_year_filter <= 2100):
        album_year_filter = None
        explicit_year = False
        
    cfg = get_config()

    # Off the event loop: shared by GET and POST, and a slow database here
    # used to stall every other request on this worker (timeout fix).
    def _load_album_tracks() -> list[dict[str, Any]]:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    ORDER BY
                        COALESCE(disc_number, '1'),
                        CAST(NULLIF(regexp_replace(COALESCE(CAST(track_number AS TEXT), ''), '[^0-9].*$', ''), '') AS INTEGER) NULLS LAST,
                        track_number,
                        title
                """),
                {"artist": artist_name, "album": album_name},
            )
            return [dict(r._mapping) for r in result.fetchall()]

    tracks = await asyncio.to_thread(_load_album_tracks)

    # ⚠️ The ORDER BY above is only a PRE-SORT. ``disc_number``/``track_number``
    # are TEXT and ``COALESCE`` does not treat '' as missing, so a blank DISC
    # tag sorts ahead of '1' — which is how Track 13 ended up above Track 1.
    # The order the user sees is decided here, where it is testable.
    tracks = sort_album_tracks(tracks)

    def _track_year(t: dict[str, Any]) -> int | None:
        raw = str(t.get("year") or "").strip()
        if raw[:4].isdigit():
            return int(raw[:4])
        try:
            rv = int(t.get("release_year") or 0) or None
            return rv
        except (TypeError, ValueError):
            return None

    # ── Scoping one album page ────────────────────────────────────────────
    #
    # ⚠️ YEARS ARE A BAD IDENTITY FOR AN ALBUM. A compilation credits the
    # original recordings, so ONE release's tracks carry many different years
    # (`2003 - Battlefield Vietnam` holds tracks tagged 1963 … 2004) and the
    # old year-based split chopped that release into several pages — the
    # reported `/2004` and `/1963` being the same release, same MBID.
    #
    # Release group first, year only as a fallback:
    #   * an explicit RG segment -> that release;
    #   * an explicit year segment -> the old behaviour (existing links);
    #   * exactly one RG on the rows -> NO split, whatever the years say;
    #   * several RGs sharing the name -> the one holding the most tracks;
    #   * no RG data at all -> the year split, as before.
    def _release_group_of(t: dict[str, Any]) -> str:
        return str(t.get("musicbrainz_releasegroupid") or "").strip()

    all_album_years = sorted(
        {y for y in (_track_year(t) for t in tracks) if y is not None},
        reverse=True,
    )
    release_groups = sorted({_release_group_of(t) for t in tracks} - {""})

    if album_rg_filter:
        scoped = [t for t in tracks if _release_group_of(t) == album_rg_filter]
        if scoped:
            tracks = scoped
    elif len(release_groups) == 1:
        # ONE release group → ONE page, even when the URL carries a year.
        # An old /<year> link used to slice this release into pieces (the
        # reported bug), and the dashboard still builds those links.
        #
        # ⚠️ The scope is RECORDED, not merely tolerated: the save redirect
        # and the canonicalisation below both need to know which release this
        # page resolved to, or the album keeps offering a year address.
        album_rg_filter = release_groups[0]
    elif len(release_groups) > 1:
        # Several release groups share this name: ONE card's worth of rows —
        # the one holding the most tracks — plus any row carrying no group.
        #
        # ⚠️⚠️ THIS BRANCH USED TO SIT *BELOW* ``explicit_year``. The order was
        # single-RG → year → several RGs, so an album whose rows carried a
        # release group but whose link carried a year was still sliced by that
        # year — reported as "the same release, the same release-group ID,
        # opens as /2007 and /2011 with different track lists". A year is not
        # an identity once a release group exists; it never was.
        counts: dict[str, int] = {}
        for t in tracks:
            rg = _release_group_of(t)
            if rg:
                counts[rg] = counts.get(rg, 0) + 1
        _primary = max(counts, key=lambda r: counts[r]) if counts else ""
        scoped = [
            t for t in tracks
            if not _release_group_of(t) or _release_group_of(t) == _primary
        ]
        if scoped:
            tracks = scoped
            if _primary:
                album_rg_filter = _primary
    else:
        # No release-group data — but the SAME NAME across years is the same
        # album to the person looking at it (a re-press tagged with the pressing
        # year). Reported: "I also want it to show on similar albums with no
        # release ID but the same name with a different year" → merged, exactly
        # like the release-group case above. The year no longer scopes anything;
        # it survives only as a LEGACY address, canonicalised below.
        pass

    # ── One release, one URL ───────────────────────────────────────────────
    #
    # A trailing year is a LEGACY address — the dashboard and the unified
    # search still build them. When the rows carry release-group data the page
    # resolves the SAME release whichever year is asked for, so `/…/2007` and
    # `/…/2011` were two addresses for one album: two "pages" that differed
    # only in the address bar (and, when the release-group data was partial,
    # in the track list). Collapse them onto the release group so a shared
    # link can never look like a second, differently-scoped album.
    #
    # Not applied to POST (the save below issues its own redirect).  With no
    # release group the address collapses to the bare album URL instead — the
    # year identifies nothing, so there is nothing for it to select.
    if request.method != "POST" and explicit_year:
        _scope = album_rg_filter or ""
        if _scope != album_year_seg:
            _canonical = url_for(
                "ui.album_detail",
                album_path=f"{artist_name}/{album_name}/{_scope}".rstrip("/"),
            )
            _qs = request.query_string.decode() if request.query_string else ""
            return redirect(f"{_canonical}?{_qs}" if _qs else _canonical)

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
        # ⚠️ TWO field names for the release MBID, and the second one was the
        # reason the artist page's "Edit Release" modal silently discarded it.
        #
        # The ALBUM page renders ``name="album_mbid"``; the artist page's edit
        # modal sent the value as ``mbid``. Nothing read ``mbid`` here, so the
        # modal's MBID box was write-only — the user typed an id, saved, and
        # the field was thrown away with no error ("Save Changes" still
        # reported success). Accept both rather than pick a winner, since
        # either caller may be the one posting.
        album_mbid = (
            form.get("album_mbid") or form.get("musicbrainz_release_id") or form.get("mbid") or ""
        ).strip()
        album_rg_mbid = (
            form.get("album_release_group_mbid")
            or form.get("musicbrainz_release_group_id")
            or form.get("release_group_mbid")
            or ""
        ).strip()
        discogs_id = (form.get("album_discogs_id") or "").strip()
        artist_mbid = (form.get("artist_mbid") or "").strip()
        genres_str = (form.get("album_genres") or "").strip()
        cover_url = (form.get("cover_art_url") or "").strip()

        # ── Identifier fields: PRESENT, not truthy ─────────────────────────
        # An emptied identifier box is a request to REMOVE the stored value.
        # The writes below used to be ``if album_mbid:`` / ``if artist_mbid:``,
        # so deleting an MBID and saving simply omitted the column and the old
        # value stayed — the reported "it doesn't remove them". Presence of the
        # form field is what distinguishes "leave it alone" (field absent, e.g.
        # a partial caller) from "clear it" (field present and empty).
        _album_mbid_field = next(
            (key for key in ("album_mbid", "musicbrainz_release_id", "mbid") if key in form),
            None,
        )
        _album_rg_field = next(
            (
                key
                for key in (
                    "album_release_group_mbid",
                    "musicbrainz_release_group_id",
                    "release_group_mbid",
                )
                if key in form
            ),
            None,
        )
        _artist_mbid_present = "artist_mbid" in form
        _discogs_present = "album_discogs_id" in form

        # The page renders ONE "Artist ID" box from the first non-empty of
        # these two columns (see the render's ``first_value``). Remember which
        # value it showed, so clearing the box removes the column that supplied
        # it without wiping a genuinely DIFFERENT album-artist id on a
        # compilation.
        _artist_mbid_displayed = ""
        for _t in tracks:
            for _col in ("musicbrainz_artistid", "musicbrainz_albumartistid"):
                _value = str(_t.get(_col) or "").strip()
                if _value:
                    _artist_mbid_displayed = _value
                    break
            if _artist_mbid_displayed:
                break

        # Read the value from the SAME field that proved presence, so an
        # emptied ``album_mbid`` cannot pick up a value from another alias.
        if _album_mbid_field:
            album_mbid = str(form.get(_album_mbid_field) or "").strip()
        if _album_rg_field:
            album_rg_mbid = str(form.get(_album_rg_field) or "").strip()

        # ── Staged per-track MusicBrainz review ──────────────────────────────
        # The album page's "Lookup MBID" preview stages every per-track change
        # (title, track/disc number, recording MBID, writer, genres, cover
        # verdict) as JSON in a hidden form field instead of writing them on
        # lookup. Honouring it here is what makes the review a SINGLE atomic
        # save — see services/metadata/metadata_proposal_service.py for the
        # shape, which is {"<track_id>": {"changes": [{"field", "value"|"proposed", ...}]}}.
        #
        # A malformed payload must never break the whole save: the album-level
        # fields still apply, and the staged part is simply skipped.
        _staged_updates: dict[str, dict[str, Any]] = {}
        _staged_raw = (form.get("staged_track_updates") or "").strip()
        if _staged_raw:
            try:
                _parsed = json.loads(_staged_raw)
                if isinstance(_parsed, dict):
                    for _tid, _entry in _parsed.items():
                        if not isinstance(_entry, dict):
                            continue
                        _changes: dict[str, Any] = {}
                        for _change in _entry.get("changes") or []:
                            if not isinstance(_change, dict):
                                continue
                            _field_name = str(_change.get("field") or "").strip()
                            if not _field_name:
                                continue
                            if "value" in _change:
                                _changes[_field_name] = _change["value"]
                            else:
                                _changes[_field_name] = _change.get("proposed")
                            # A cover verdict carries the original artist too.
                            if _change.get("original_cover_artist"):
                                _changes["original_cover_artist"] = _change["original_cover_artist"]
                        if _changes:
                            _staged_updates[str(_tid)] = _changes
            except (TypeError, ValueError) as _staged_err:
                logger.debug("Staged track updates were not valid JSON", error=str(_staged_err))

        # Columns a staged review may write. Whitelisted (never derived from
        # request input) so a crafted payload cannot reach an arbitrary column.
        _STAGED_WRITABLE = frozenset({
            "title", "track_number", "disc_number", "mbid",
            "writer", "musicbrainz_genres", "is_cover", "original_cover_artist",
            "artist",
        })

        # A SAVED review clears the stashed recommendations for this album —
        # they have just been applied, so offering them again would be stale.
        # Read here (before the per-track loop) so the flag is known up front.
        _has_pending_recommendations = bool(
            (form.get("pending_recommendations") or "").strip()
        )

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
        # ⚠️ ONLY WHEN THE RELEASE CHANGED — and off the event loop.
        #
        # This backfill belongs to the release picker (the values it fills are
        # the ones a newly-linked release contributes). Re-fetching the SAME
        # release on every ordinary edit costs two MusicBrainz calls whose
        # shared throttle SLEEPS to hold a slot in a 1 req/s budget — tens of
        # seconds while a scan runs. In an ``async`` handler that stalled the
        # whole worker, which is the reported "timeouts when saving metadata".
        #
        # It is ALSO bounded (see ``_MB_BACKFILL_DEADLINE_SECONDS``): off the
        # event loop is not the same as off the critical path — a saturated
        # throttle would still hold THIS response past Cloudflare's 100s origin
        # timeout (the reported error 524). ``wait_for`` abandons the await,
        # not the work: the worker thread finishes on its own and its result is
        # dropped, so nothing is left half-written. Losing it costs only the
        # additive enrichment — the form already carries the album-level
        # fields, and a later save or scan fills the rest.
        _save_started = time.monotonic()
        _mb_backfill_ms = 0.0
        _prev_mbids = {
            str(t.get("musicbrainz_album_mbid") or "").strip() for t in tracks
        }
        _prev_mbid = next((m for m in sorted(_prev_mbids) if m), "")
        if album_mbid and album_mbid != _prev_mbid:
            _mb_started = time.monotonic()
            try:
                _back = await asyncio.wait_for(
                    asyncio.to_thread(
                        _fetch_album_mb_backfill,
                        album_mbid, _mb_albumtype, release_values,
                    ),
                    timeout=_MB_BACKFILL_DEADLINE_SECONDS,
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "Album save: MusicBrainz backfill hit its deadline — "
                    "saving with the values the form provided",
                    release_mbid=album_mbid,
                    deadline_seconds=_MB_BACKFILL_DEADLINE_SECONDS,
                )
                _back = {}
            _mb_backfill_ms = (time.monotonic() - _mb_started) * 1000.0
            _mb_albumartist_mbid = _back.get("albumartist_mbid") or _mb_albumartist_mbid
            _mb_albumtype = _back.get("albumtype") or _mb_albumtype
            _mb_albumstatus = _back.get("albumstatus") or _mb_albumstatus
            _mb_releasecountry = _back.get("releasecountry") or _mb_releasecountry
            _mb_originalyear = _back.get("originalyear") or _mb_originalyear
            _mb_track_map = _back.get("track_map") or _mb_track_map

        updated_count = 0
        reverted_live_count = 0
        file_sync_failures = 0
        # Writes the DB REFUSED. Counted so the page can say the save failed
        # instead of claiming there was nothing to change.
        db_failures = 0
        # Genre writes that matched no rows (see ``update_track_genres``).
        genre_write_failures = 0
        # Genres are written through a dedicated repository call rather than
        # through ``payload``, so the loop below never counted them.  Saving an
        # album whose ONLY edit was the genre chips therefore wrote the genres
        # and still reported "No changes were made."  Counted separately here.
        genre_only_writes = 0
        # Tracks that KEPT their own track-level genres because they outrank
        # the album's list (see ``_track_keeps_its_own_genres``).
        genre_tracks_kept = 0
        # File-tag work is COLLECTED here and run as one concurrent phase after
        # the loop — see ``_write_album_track_files_concurrently``.
        _file_jobs: list[tuple[Any, dict, dict, bool, bool]] = []
        # Per-track DB payloads are likewise collected so the whole album can
        # commit in one transaction instead of paying for one session per row.
        _db_payloads: list[dict[str, Any]] = []

        _write_loop_started = time.monotonic()
        # ⚠️ THE DATABASE PHASE RUNS IN A WORKER THREAD (timeout fix).
        #
        # Everything below — per-track payload build, the per-track genre fills
        # (one session each), the live-state reverts, the bulk upsert and the
        # lookup-skip write — is SYNCHRONOUS database work. It used to run
        # inline in this ``async`` handler, so a slow database stalled the
        # whole hypercorn worker AND had no deadline: the one unbounded phase
        # of the save, and the remaining cause of the Cloudflare 524. It now
        # runs via ``asyncio.to_thread``; nothing in it awaits, and ``form`` is
        # an already-materialised MultiDict, so it is thread-safe to read.
        def _album_db_phase() -> None:
            nonlocal updated_count, reverted_live_count, db_failures
            nonlocal genre_write_failures, genre_only_writes, genre_tracks_kept
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
                elif "release_year" in form:
                    # An EMPTIED edition-year box means "this edition has no
                    # year", so the stored value must go. ``""`` is coerced to
                    # NULL for an INTEGER column by
                    # ``coerce_track_value_for_pg_type``.
                    payload["release_year"] = ""

                # ── The RELEASE NAME (the specific edition) ────────────────
                # ⚠️⚠️ PRESENCE-GATED, NOT TRUTHINESS-GATED — and this is the
                # reported "deleting the release name and saving puts the same
                # one back".
                #
                # ``if release_title:`` treated an EMPTY box as "nothing to say",
                # so the column was simply not in the payload, ``_execute_save``
                # never touched it, and the value the user had just deleted was
                # still there on the next render. The box was write-only in the
                # one direction a user actually needs — removing an edition name
                # the collection no longer holds.
                #
                # Blank is a MEANINGFUL input here, not a missing one: the field's
                # own hint says "Leave blank when the collection holds the
                # original release." So the question is not "is the value
                # empty?" but "did the form CARRY this field?" — the same
                # convention ``album_lookup_skip`` below already uses. A caller
                # that omits the input (a partial save) leaves the column alone.
                if "album_release_title" in form:
                    payload["release_title"] = release_title

                if album_type:
                    payload["spotify_album_type"] = album_type
                    payload["musicbrainz_albumtype"] = album_type
                    # ``releasetype`` is what the FILE carries (and what Navidrome
                    # reads back), so a type chosen here must reach it too — and it
                    # is the PRIMARY form, the same normalisation the scan writes.
                    # Without it the file kept the old release type, so the album
                    # page's choice was contradicted everywhere outside the DB.
                    payload["releasetype"] = normalize_primary_release_type(album_type)
                # ⚠️ ``album_type`` is DELIBERATELY still truthiness-gated, unlike
                # the fields above. Its select cannot render every type the scan
                # can store (``album+acoustic``, ``album+dj-mix``, a mixtape…), and
                # when no option matches the browser falls back to the first one —
                # which is the BLANK "Unknown" option. Presence-gating it would
                # therefore treat "this album's type is not in my list" as "the
                # user cleared it" and wipe a type the page never showed. To clear
                # a type safely the select needs an option for every value first.

                if track_artist and track_artist != track.get("artist"):
                    payload["artist"] = track_artist
                if track_composer:
                    payload["writer"] = track_composer

                # ⚠️ PRESENCE-gated, not truthiness-gated. ``None`` clears the
                # column; an omitted form field leaves it untouched.
                if _album_mbid_field:
                    payload["musicbrainz_album_mbid"] = album_mbid or None
                    # The library stores BOTH spellings of the release id; the
                    # album page reads the ``_mbid`` one, so a clear must reach
                    # both or the page would keep showing the other.
                    payload["musicbrainz_albumid"] = album_mbid or None
                if _album_rg_field:
                    payload["musicbrainz_releasegroupid"] = album_rg_mbid or None
                if _artist_mbid_present:
                    payload["musicbrainz_artistid"] = artist_mbid or None
                    if not artist_mbid:
                        # The page's single "Artist ID" box is populated from
                        # EITHER artist column. Clear the album-artist column
                        # too when it is empty or held the value the page
                        # displayed — otherwise the box would refill from it.
                        # A genuinely different album-artist id is preserved.
                        _current_album_artist_mbid = str(
                            track.get("musicbrainz_albumartistid") or ""
                        ).strip()
                        if (
                            not _current_album_artist_mbid
                            or _current_album_artist_mbid == _artist_mbid_displayed
                        ):
                            payload["musicbrainz_albumartistid"] = None
                if _discogs_present:
                    payload["discogs_album_id"] = discogs_id or None

                if cover_url:
                    payload["cover_art_url"] = cover_url

                # ── Apply this track's staged MusicBrainz review ─────────────
                # Written AFTER the album-level values so a per-track value the
                # user reviewed and kept wins over the album-wide default.
                # Whitelisted fields only; anything else in the payload is ignored.
                _staged_for_track = _staged_updates.get(str(track_id))
                if _staged_for_track:
                    for _sfield, _svalue in _staged_for_track.items():
                        if _sfield not in _STAGED_WRITABLE:
                            continue
                        if _svalue is None or str(_svalue).strip() == "":
                            continue
                        payload[_sfield] = _svalue
                    # A reviewed cover verdict renames the title to the library's
                    # "Title (Original Artist Cover)" convention — mirroring the
                    # compare/apply path — unless the title was staged explicitly.
                    _staged_cover_artist = str(
                        _staged_for_track.get("original_cover_artist") or ""
                    ).strip()
                    if _staged_for_track.get("is_cover") and _staged_cover_artist:
                        _base_title = str(
                            payload.get("title") or track.get("title") or ""
                        ).strip()
                        if _base_title and "cover)" not in _base_title.lower():
                            payload["title"] = f"{_base_title} ({_staged_cover_artist} Cover)"

                # The same presence gate as the fields above: a release field the
                # form did not carry must never be written, or every save would
                # blank metadata the page has no box for. But a field the form DID
                # carry and the user EMPTIED must be cleared — ``if value:`` made
                # each of these boxes write-only, exactly like the Release Name.
                for field, value in release_values.items():
                    if value or f"album_{field}" in form:
                        payload[field] = value

                # Single-disc albums: strip the per-track disc_number so neither
                # the DB nor the audio file tags carry a bogus disc position.
                # Only set this when the payload is otherwise being written (the
                # field is TEXT; an empty string clears the frame on file write).
                #
                # ⚠️ An EXPLICITLY REVIEWED disc number wins over the inference
                # below. The strip is an album-level heuristic about the library's
                # existing tags; the staged value is the user having just confirmed
                # the correct position against MusicBrainz. Without this guard,
                # saving a review for a track whose disc number was actually wrong
                # threw the correction away and wrote back the bogus value —
                # silently, because the surrounding save still reported success.
                # ``disc_number`` is in ``_STAGED_WRITABLE``, so this is also what
                # that whitelist promises the user.
                _disc_staged = bool(_staged_updates.get(str(track_id), {}).get("disc_number"))
                if _strip_disc_numbers and not _disc_staged:
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
                    _cur_disc = str(payload.get("disc_number") or track.get("disc_number") or "").strip()
                    if not _cur_disc or _cur_disc == "0":
                        payload["disc_number"] = "1"

                if genres_str:
                    # Phase 1 — genres: the DATABASE columns only. The file's
                    # genre tag is the popularity scan's job (DB → file), so this
                    # pass never writes it.
                    #
                    # Track-level genres WIN: a track with its own evidence
                    # (MusicBrainz, Last.fm, Navidrome, manual, …) keeps it, and
                    # the album's list is only a FILL for tracks that have none.
                    if _track_keeps_its_own_genres(track, _staged_for_track):
                        genre_tracks_kept += 1
                    else:
                        _genre_rows, _genre_failed = _apply_album_track_genres(
                            track_id, genres_str,
                        )
                        # Genres bypass ``payload``, so they must be counted here or
                        # a genres-only save is reported as "No changes were made".
                        if _genre_rows:
                            genre_only_writes += 1
                        if _genre_failed:
                            genre_write_failures += 1

                # Phase 2 — persist: collect the DB payload. The actual write is
                # batched across the whole album after the loop, so we pay for one
                # transaction instead of one session + commit per track.
                if len(payload) > 1:
                    _db_payloads.append(payload)

                # Phase 3 — file tags: the cover genre and the single-disc clear.
                #
                # QUEUED, not written inline: the write below reads the file twice
                # (``preserve_file_timestamps`` compares the bytes) and rewrites it,
                # so doing it here made the loop one N-fold sequence of disk passes
                # — the wall clock behind the reported **Cloudflare 524** on save.
                # The job carries the payload built above; nothing mutates it after
                # this point, so the worker sees exactly what this track was saved
                # with.
                _file_jobs.append(
                    (track_id, track, payload, _strip_disc_numbers, _disc_staged)
                )

                # ── Live-state revert: ONLY when the album is RECLASSIFIED ──────
                #
                # ⚠️ THIS USED TO RUN ON EVERY SAVE OF A NON-LIVE ALBUM, and it
                # DELETED the live/acoustic/unplugged marker from the title.
                #
                # `revert_track_live_state` does two things: it clears the
                # is_live/is_acoustic/album_context_live flags AND it rewrites the
                # title through `strip_live_acoustic_suffix`. So on an ordinary
                # album save, a track whose metadata had just been updated to
                # "Song (Unplugged)" was silently reduced back to "Song" — the
                # reported "saving removes live, unplugged or acoustic from a track
                # even if the metadata had updated to include them".
                #
                # The old guard only asked "is the SAVED type non-live?", which is
                # true for every ordinary album — so it fired essentially always.
                # That is the wrong question. The revert exists to undo tagging the
                # pipeline had applied because it believed the album was live; it is
                # only meaningful when that belief has just CHANGED.
                #
                # This save path cannot express that change either: it never writes
                # is_live / is_acoustic / album_context_live (none is in the payload
                # built above, and none is in `_STAGED_WRITABLE`). Nothing about the
                # live state was cleared here, so there is nothing to undo.
                #
                # The correct condition is therefore "the album WAS live and is now
                # being saved as non-live" — a RECLASSIFICATION. The stored type is
                # read from the track rows the page already loaded, so no extra
                # query is introduced. A track-level edit that genuinely clears
                # `is_live` is handled by the track routes, which compare the old
                # and new values directly.
                _stored_type = str(
                    track.get("musicbrainz_albumtype")
                    or track.get("spotify_album_type")
                    or track.get("album_type")
                    # See ``category_for_album_row``: the tag/guessed type is a
                    # fallback for an unclassified album, never a competing
                    # opinion about a classified one.
                    or track.get("releasetype")
                    or ""
                ).lower()
                # ⚠️ `+acoustic` counts as live-state too. The OLD guard only
                # checked "+live"/"(live)", so an album typed `album+acoustic` was
                # treated as an ordinary album and had its "(Acoustic)" titles
                # stripped on every save — the acoustic half of the same report.
                # `_apply_live_remix_album_tagging` applies the Acoustic label on
                # exactly that type, so the revert must recognise it as live state.
                _stored_type_was_live = _album_type_is_live_state(_stored_type)
                _saved_type_is_live = _album_type_is_live_state((album_type or "").lower())

                if _stored_type_was_live and not _saved_type_is_live:
                    # Guard first: the revert opens its own session and re-reads the
                    # row, so calling it for every track on a plainly studio album
                    # cost one SELECT per track and logged two INFO lines each for
                    # no work. `track_carries_live_state` inspects the row we
                    # already hold and only lets genuinely suspicious tracks through.
                    if track_carries_live_state(track):
                        try:
                            if revert_track_live_state(str(track_id)):
                                reverted_live_count += 1
                        except Exception as revert_err:
                            logger.debug("Live-state revert failed", track_id=track_id, error=str(revert_err))

            # ── Phase 2b: commit all per-track DB writes in one transaction ──
            # Every payload collected by the loop lands in ONE session + commit
            # instead of one per track (the reported ~68 s DB loop on a 14-track
            # album). Counting is all-or-nothing at the batch level; see
            # ``_persist_album_track_payloads``.
            _updated, _failed = _persist_album_track_payloads(_db_payloads)
            updated_count += _updated
            db_failures += _failed

            # ── Do-not-search-album-release flag ─────────────────────────────
            # A custom album (not on MusicBrainz/Discogs) can be marked so the
            # album-release lookups are skipped. Written ALBUM-WIDE AFTER the
            # rename below so every track agrees on the final artist/album.
            #
            # ⚠️ PRESENCE-GATED: the checkbox now lives in its OWN card with its
            # own Save button (POST /api/album/lookup-skip), so this big save no
            # longer carries the field. If the handler kept reading
            # ``form.get(...)`` unconditionally, every big save would compute False
            # and CLEAR the flag — and a live-tree save (whose form never had the
            # field at all) would clear a custom album marked from the rebuilt page.
            # "Absent" must mean "not part of this save", so only write when the
            # field was actually posted.
            try:
                if "album_lookup_skip" in form:
                    from services.metadata.album_lookup_skip_service import set_album_lookup_skip
                    _skip_lookup = str(form.get("album_lookup_skip") or "").strip().lower() in (
                        "1", "true", "on", "yes"
                    )
                    set_album_lookup_skip(
                        new_artist or artist_name,
                        new_title or album_name,
                        _skip_lookup,
                    )
            except Exception as _skip_lookup_err:
                logger.debug("Album lookup-skip flag write failed", error=str(_skip_lookup_err))

        await asyncio.to_thread(_album_db_phase)

        # The database half is done — recorded here so the phase log can still
        # separate the DB loop from everything that touches the disk.
        _write_loop_ms = (time.monotonic() - _write_loop_started) * 1000.0

        # ── Phase 3: cover art — fetch and STORE (both paths) ─────────────
        # The download + the ``album_art`` row stay in the request even when the
        # disk phase is backgrounded. They are what makes the new art VISIBLE on
        # the album page, and one HTTP fetch plus one small insert is nothing
        # next to the N file writes that follow. Only the EMBED (a pass over
        # every audio file) moves with the rest of the disk phase.
        _cover_embedded = False
        _cover_bytes: bytes | None = None
        _cover_mime = "image/jpeg"
        # ⚠️ THE TRIGGER IS UNCHANGED — still only when the form carries a cover
        # URL. Widening it to "or we have an MBID" would ADD a Cover Art Archive
        # fetch to every ordinary save that has an MBID, which is the opposite of
        # what this fix is for. The lookup fills ``#cover_art_url`` (see
        # ``applyAlbumMatch``), so a lookup-then-save already arrives with one.
        if cover_url:
            try:
                from services.enrichment.album_art_service import (
                    save_album_art_to_db,
                )
                # Blocking (an HTTP fetch, or the Cover Art Archive) — run it in
                # a thread so a slow image host cannot stall the worker, AND
                # under a deadline so it cannot stall the SAVE. Off the event
                # loop is not the same as bounded: the CAA resolver follows
                # redirects (a fresh timeout per hop) and falls back from the
                # release to the release group, so without this the sequence had
                # no ceiling at all.
                _cover_bytes, _cover_mime = await asyncio.wait_for(
                    asyncio.to_thread(
                        _resolve_album_cover_bytes, cover_url, album_mbid, album_rg_mbid
                    ),
                    timeout=_COVER_FETCH_DEADLINE_SECONDS,
                )
                if _cover_bytes:
                    await asyncio.to_thread(
                        save_album_art_to_db,
                        artist_name, album_name, _cover_bytes,
                        source="url", mime_type=_cover_mime,
                    )
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "Album save: cover fetch hit its deadline — saving without new art",
                    artist=artist_name, album=album_name,
                    deadline_seconds=_COVER_FETCH_DEADLINE_SECONDS,
                )
                _cover_bytes, _cover_mime = None, "image/jpeg"
            except Exception as _cover_exc:
                logger.debug("Album save cover art fetch failed", artist=artist_name, album=album_name, error=str(_cover_exc))

        # ── Phase 4: the DISK phase — inline, or in the background ─────────
        # One payload, one runner, two ways to reach it (see
        # ``_run_album_file_sync_phases``). A large album's N file writes plus
        # the cover embed are what outlive Cloudflare's 100s origin timeout, so
        # past a configured track count they are handed to the background
        # service and the response goes back immediately.
        _file_payload: dict[str, Any] = {
            "runner": _run_album_file_sync_phases,
            "file_jobs": _file_jobs,
            # The TARGET names — the album may be being renamed, and the cover
            # embed looks its rows up by name.
            "album_artist": new_artist or artist_name,
            "album": new_title or album_name,
            "cover_bytes": _cover_bytes,
            "cover_mime": _cover_mime,
            # ⚠️ Decided HERE, from values only the database half knows, and
            # carried in the payload. The background path has no access to
            # ``updated_count`` / ``reverted_live_count`` / whether art was
            # fetched, so it could not work this out for itself.
            #
            # ⚠️ ``genre_only_writes`` is deliberately NOT part of this: genres
            # are database-only now, so a genres-only save changes no file and
            # there would be nothing for Navidrome to re-read.
            "should_rescan": bool(
                updated_count > 0 or reverted_live_count > 0 or _cover_bytes
            ),
        }

        _file_started = time.monotonic()
        _file_backgrounded = False
        # Set when the inline disk phase hit its budget. Tracked separately from
        # ``_file_backgrounded`` because they mean different things: one is a
        # DECISION (this album is big enough to queue), the other is an
        # OUTCOME (it was queued inline and ran long). Only the messages care,
        # but they must not tell the user their files were written when the
        # response gave up waiting for them.
        _file_abandoned = False
        # Split out of ``_file_write_ms`` by the runner, so a slow save names
        # WHICH pass was slow. Both stay 0 for a backgrounded save, where the
        # work has not happened yet.
        _tags_ms = 0.0
        _cover_ms = 0.0
        try:
            _bg_cfg = get_album_save_background_config()
        except Exception:
            _bg_cfg = {"enabled": False, "track_threshold": 0}

        if _bg_cfg.get("enabled") and len(_file_jobs) >= int(
            _bg_cfg.get("track_threshold") or 0
        ):
            try:
                from services.metadata.album_file_sync_service import (
                    enqueue_album_file_sync,
                )

                enqueue_album_file_sync(
                    artist=new_artist or artist_name,
                    album=new_title or album_name,
                    payload=_file_payload,
                    reason=f"album save ({len(_file_jobs)} tracks)",
                )
                _file_backgrounded = True
            except Exception as _bg_exc:
                # Falling back to INLINE is the safe direction: the database half
                # is already durable, and doing the disk work here is exactly what
                # the user asked for — it just risks the timeout it always did.
                logger.warning(
                    "Album file sync could not be queued — running it inline",
                    artist=artist_name, album=album_name, error=str(_bg_exc),
                )
                _file_backgrounded = False

        if not _file_backgrounded:
            # ⚠️ THE INLINE PHASE IS BOUNDED TOO, AND THAT IS NOT REDUNDANT.
            #
            # The tag writes and the cover embed are each concurrent now, which
            # brings a normal album well inside the budget. But this phase's cost
            # is set by the USER'S STORAGE, not by anything this code decides:
            # the same album is ~370ms a file on a local disk and seconds a file
            # on a network mount. No per-phase constant can be tuned to be both
            # safe and useful across that range.
            #
            # So the budget is measured against the only deadline that actually
            # exists — Cloudflare's 100s origin timeout. Expiry abandons the
            # AWAIT, not the work: the worker thread keeps writing and lands
            # every tag it was given. The response just stops claiming to have
            # confirmed them, which is what ``_disk_abandoned`` records below.
            # Budget = what is LEFT of the total, not a fixed 45s on top of
            # whatever the earlier phases already spent.
            _disk_budget = max(
                _INLINE_DISK_MIN_SECONDS,
                min(
                    _INLINE_DISK_BUDGET_SECONDS,
                    _SAVE_TOTAL_BUDGET_SECONDS - (time.monotonic() - _save_started),
                ),
            )
            try:
                _disk = await asyncio.wait_for(
                    asyncio.to_thread(_run_album_file_sync_phases, _file_payload),
                    timeout=_disk_budget,
                )
            except (asyncio.TimeoutError, TimeoutError):
                _disk = {}
                _file_abandoned = True
                logger.warning(
                    "Album save: file tagging exceeded its inline budget — "
                    "responding now, the writes continue in the background",
                    artist=artist_name, album=album_name,
                    tracks=len(_file_jobs),
                    budget_seconds=round(_disk_budget, 1),
                )
                log_unified(
                    f"⏱ Album save: {len(_file_jobs)} file(s) still being tagged "
                    f"after {_disk_budget:.0f}s — the database is "
                    f"saved and the files are being written in the background"
                )
            file_sync_failures += int(_disk.get("file_failures") or 0)
            _cover_embedded = bool(_disk.get("cover_embedded"))
            _tags_ms = float(_disk.get("tags_ms") or 0.0)
            _cover_ms = float(_disk.get("cover_ms") or 0.0)
        _file_write_ms = (time.monotonic() - _file_started) * 1000.0

        # ── Ask Navidrome to re-scan — now the DISK phase's last step ──────
        # ⚠️ THE RESCAN MOVED, IT WAS NOT DELETED. It used to sit here, after the
        # file writes, for the good reason in the comment that lived here: the
        # save writes corrected tags into the AUDIO FILES, but Navidrome serves
        # its OWN database, so without a rescan the next Popularr import reads
        # Navidrome's PRE-save rows and the upsert overwrites the metadata just
        # saved ("the import puts the old data back").
        #
        # That ordering is now enforced inside ``_run_album_file_sync_phases``,
        # because for a BACKGROUNDED save this point in the handler is reached
        # before the files have been written — rescanning here would have scanned
        # the old tags and reintroduced that exact bug. The decision of WHETHER to
        # rescan is still made here (``should_rescan`` in the payload), from the
        # database half's counters, which the background thread cannot see.
        #
        # Per-tag-write triggers were removed on 2026-08-30 for hammering the
        # server; this remains ONE fire-and-forget request per user-initiated
        # save, coalesced so bursts collapse into one scan.
        #
        # ⚠️ ``genre_only_writes`` is deliberately NOT part of ``should_rescan``:
        # genres are database-only now, so a genres-only save changes no file and
        # there would be nothing for Navidrome to re-read.

        if _file_backgrounded or _file_abandoned:
            await flash(
                f"Album metadata saved — {updated_count} track(s) updated. "
                f"Tagging {len(_file_jobs)} audio file(s) in the background — "
                "this page does not need to stay open.",
                "success",
            )
        elif updated_count > 0:
            await flash(f"Album metadata saved — {updated_count} track(s) updated.", "success")
        # A REFUSED write is reported as a failure, and never allowed to fall
        # through to the "No changes were made." branch below — that message
        # told the user their edit was a no-op when the database had actually
        # rejected it.
        if db_failures > 0:
            await flash(
                f"⚠️ {db_failures} track(s) could NOT be saved to the database. "
                "The metadata was not written — check the logs.",
                "danger",
            )
        if genre_write_failures > 0:
            await flash(
                f"⚠️ Genres could not be saved for {genre_write_failures} track(s).",
                "danger",
            )
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

        # A saved review means the stashed recommendations were just applied —
        # clear them so the album page does not keep offering stale suggestions
        # (the same "clear then re-stash" lifecycle the scan-side writer uses).
        if _has_pending_recommendations and updated_count > 0:
            try:
                from services.metadata.pending_update_service import (
                    discard_album_recommendations,
                )
                discard_album_recommendations(
                    new_artist or artist_name, new_title or album_name
                )
            except Exception as _discard_exc:
                logger.debug(
                    "Could not clear saved recommendations",
                    artist=artist_name, album=album_name, error=str(_discard_exc),
                )

        if (
            updated_count == 0
            and reverted_live_count == 0
            and file_sync_failures == 0
            and db_failures == 0
            and genre_write_failures == 0
        ):
            # Genres are written outside the payload, so a save whose ONLY edit
            # was the genre chips did reach the DB even though ``updated_count``
            # is 0.  Reporting "No changes were made." there was simply wrong.
            if genre_only_writes:
                _kept_note = (
                    f" · {genre_tracks_kept} track(s) kept their own track-level genres."
                    if genre_tracks_kept
                    else ""
                )
                await flash(
                    f"Album genres saved — {genre_only_writes} track(s) updated.{_kept_note}",
                    "success",
                )
            elif genre_tracks_kept:
                # Every track already had genres of its own. "No changes were
                # made" would read as the genre edit silently failing — it was
                # refused, on purpose, in favour of the tracks' own values.
                await flash(
                    "Album genres not applied — every track has its own "
                    f"track-level genres ({genre_tracks_kept} kept).",
                    "info",
                )
            elif not tracks:
                # There is genuinely nothing to write: the album matched no
                # track rows.  Saying "no changes" implied the values were
                # already correct when nothing was even considered.
                await flash(
                    "⚠️ No tracks found for this album — nothing could be saved.",
                    "warning",
                )
            else:
                await flash("No changes were made.", "info")

        # One line saying WHERE a save spent its time. Every slow phase of
        # this handler has produced a timeout report at some point — first the
        # MusicBrainz calls on the event loop, then the wall clock that
        # outlived Cloudflare's 100s origin timeout (error 524) — and without
        # numbers the next one is a guess. ``file_tags_ms`` is the concurrent
        # phase's TOTAL wall time, not the sum of its writes.
        #
        # ⚠️ ALSO SENT TO ``log_unified``, so it lands in the Logs page rather
        # than only in the JSON app log. The last two reports of this timeout
        # arrived without the numbers this line already collected, because the
        # person reading the UI logs could not see them.
        _total_ms = (time.monotonic() - _save_started) * 1000.0
        logger.info(
            "Album save phases",
            artist=artist_name,
            album=album_name,
            tracks=len(tracks),
            updated=updated_count,
            db_failures=db_failures,
            file_failures=file_sync_failures,
            backgrounded=_file_backgrounded,
            abandoned=_file_abandoned,
            mb_backfill_ms=round(_mb_backfill_ms, 1),
            write_loop_ms=round(_write_loop_ms, 1),
            file_tags_ms=round(_file_write_ms, 1),
            tag_writes_ms=round(_tags_ms, 1),
            cover_embed_ms=round(_cover_ms, 1),
            total_ms=round(_total_ms, 1),
        )
        log_unified(
            f"💾 Album save: {album_name} — {len(tracks)} track(s) in "
            f"{_total_ms / 1000.0:.1f}s "
            f"| MB backfill {_mb_backfill_ms / 1000.0:.1f}s "
            f"| DB {_write_loop_ms / 1000.0:.1f}s "
            f"| tags {_tags_ms / 1000.0:.1f}s "
            f"| cover {_cover_ms / 1000.0:.1f}s"
            + (" | TAGGING CONTINUES IN BACKGROUND" if _file_abandoned else "")
        )

        redirect_artist = new_artist or artist_name
        redirect_album = new_title or album_name
        # Scope the redirect the way the page is scoped. A release GROUP is the
        # identity and the canonical URL, so it wins; a year only follows a
        # page that is genuinely year-scoped (no release-group data).
        _redirect_scope = ""
        if album_rg_filter:
            _redirect_scope = f"/{album_rg_filter}"
        # No year branch: a year identifies nothing now, so a save lands on the
        # canonical address (release group when known, else the bare album).
        # Fragment: the save is submitted from the EDIT ALBUM tab, but the
        # reload always came back on the default TRACKS tab — so the user saved
        # and was immediately dropped on the tracklist with a different Save
        # affordance waiting ("saves once, then asks to save twice").  The JS
        # in both album pages restores the tab from this hash.
        return redirect(
            url_for(
                "ui.album_detail",
                album_path=f"{redirect_artist}/{redirect_album}{_redirect_scope}",
            )
            + "#tab-details"
        )

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
        "spotify_album_type": first_value("musicbrainz_albumtype", "spotify_album_type", "album_type", "releasetype"),
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
        # Custom albums not on MusicBrainz/Discogs: '1' = skip the album
        # release lookups. Read album-wide (same value on every track).
        "album_lookup_skip": first_value("album_lookup_skip"),
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
        # disc_sort_value is the SINGLE definition of "what disc is this" —
        # shared with album_track_sort_key, so the group header and the row
        # order can never disagree. It keeps the reported behaviour: a bogus
        # disc_number of 0 (bad source tags / imports) folds into disc 1
        # rather than rendering as its own "disc 0" group on a single-disc
        # album (the reported "disc 1 and disc 0" split).
        disc_number = disc_sort_value(track)
        track["disc_group"] = disc_number
        tracks_by_disc.setdefault(disc_number, []).append(track)

    # Group headers only when there is something to group — a single-disc
    # album must not grow a header that says "Disc 1" above one row.
    show_disc_headers = len(tracks_by_disc) > 1

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
        show_disc_headers=show_disc_headers,
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
                        # Guarded for the same reason as the album save path:
                        # the user is CLEARING the live flag, so the row may
                        # legitimately need a revert — make the cheap check
                        # decide rather than opening a session per track.
                        if track_carries_live_state(track):
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
                                file_write_success = await asyncio.to_thread(
                                    write_tags_to_file, resolved_path, tags_to_write,
                                )
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
        "downloads": ("quality_filter", "conversion", "metadata_heal"),
        "queue": ("matching",),
        "popularity": ("weights",),
        "genres": ("weights", "synonyms"),
        "metadata_update": ("update_on_files", "album_mbid_guard"),
        # ``playlists.genre_exclusions`` holds two booleans AND a list, so it
        # must be kept as a dict — without this entry the Config page save
        # coerced it to ``{}`` and the rules silently stopped applying.
        "playlists": ("genre_exclusions",),
        # Same shape, same trap: a nested block absent from this map is coerced
        # to ``{}`` on save, so the safe-shutdown settings would appear to save
        # and then read back as defaults.
        "maintenance": ("safe_shutdown",),
        # And again: ``album_save.background`` is the album save's disk phase.
        "album_save": ("background",),
        # ``genres.selection`` holds nested dicts (source_weights,
        # entity_weights, thresholds) AND lists (keep_together,
        # conditional_keep). Without this entry the Config page save coerces the
        # whole block to ``{}`` and the engine silently falls back to its
        # defaults — the same trap as playlists.genre_exclusions.
        "genres": ("weights", "synonyms", "selection"),
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

    # Carry forward any top-level section this page does not collect.
    # ``save_config`` REPLACES config.yaml wholesale, so a section the
    # collector never emits — ``scheduler:`` held every scheduled-scan
    # interval — would be DELETED by an ordinary save. Only keys the payload
    # does not already carry are filled in, so an explicit value always wins,
    # and a payload that already contains the section is untouched.
    if isinstance(data, dict):
        try:
            for _section_key, _section_value in read_config_from_disk().items():
                data.setdefault(_section_key, _section_value)
        except Exception:
            logger.debug("Config section carry-forward skipped")

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

        # ⚠️ The Navidrome client list is CACHED for 300s, so without this a
        # change to `navidrome_users` would not take effect until the TTL
        # expired — the user edits the config, re-runs a scan, and the old
        # accounts (and the old playlist ownership) are still in use. Worse, the
        # "already pushed" signature cache would SKIP a push to a server that
        # was only just added, because the same playlist name had been pushed
        # elsewhere moments earlier.
        #
        # Both caches are cleared together by the one function, which is why it
        # is called here rather than reaching into either directly.
        try:
            from services.popularity.stages.finalise_stage import reset_playlist_push_cache

            reset_playlist_push_cache()
            logger.info("Navidrome client and playlist-push caches cleared")
        except Exception as exc:
            logger.warning("Playlist cache reset failed", error=str(exc))

        return jsonify({"success": True})
    return jsonify({"error": "Failed to save config"}), 500


@ui_bp.route("/config/migrate_postgres", methods=["POST"])
def config_migrate_postgres() -> Any:
    return jsonify({"success": True})


@ui_bp.route("/logs")
async def logs() -> Any:
    # ── CONSOLIDATION (2026-10-14): the rebuilt UI has no logs PAGE ────────
    # Two viewers existed side by side — this page and the global modal opened
    # from the status bar — showing the same data with different controls. The
    # modal wins: it preserves the page the user was on, it carries the file
    # picker / filter / level toggles this page used to own, and it is reachable
    # from every screen. So this route hands old bookmarks to the dashboard
    # WITH the flag that opens the modal (`?logs=1`, honoured by base.html)
    # rather than to a page that no longer exists.
    #
    # Gate = the SAME helper the cutover uses, never a re-read of
    # `features.use_test_site`. Live mode keeps its own page: its viewer is an
    # inline script with no stylesheet and none of the modal work shipped with
    # this change, so redirecting there would remove functionality outright.
    from helpers.test_site_mode import config_enables_test_site

    if config_enables_test_site():
        return redirect(url_for("ui.dashboard", logs="1"))

    # Live mode: unchanged behaviour, but the file list now comes from the
    # shared allow-listed helper so the page and the modal's picker
    # (GET /api/logs/list) cannot drift apart. The route enriches it with the
    # absolute path its template renders into `data-path` — deliberately NOT
    # part of the API payload, which the modal does not need.
    from services.log_service import list_log_files

    log_dir = resolve_log_dir()
    log_files = list_log_files()
    for entry in log_files:
        entry["path"] = os.path.join(log_dir, entry["name"])

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
        downloads_dir=resolve_downloads_dir(),
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
