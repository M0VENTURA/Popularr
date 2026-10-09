"""Track API routes — migrated from the old monolithic app.py."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import structlog
from quart import Blueprint, Response, jsonify, request, send_file
from sqlalchemy import text

from api_clients.lrclib import fetch_lyrics
from db.engine import db_session
from helpers.config_helpers import get_config
from services.enrichment.musicbrainz_service import get_shared_mb_client
from services.infrastructure.filesystem_service import cleanup_empty_parents, is_path_under_directory
from services.metadata.tag_file_service import sync_track_tags_to_file

logger = structlog.get_logger(__name__)

track_bp = Blueprint("track_api", __name__, url_prefix="/api/track")


def _trigger_navidrome_scan() -> bool:
    """Request ONE coalesced Navidrome rescan after a file-tag write.

    Historically this fired a raw ``startScan`` after EVERY tag write, which
    paused the Navidrome server and locked its database (removed 2026-08-30).
    It then became a no-op that returned True anyway, so callers reported
    ``navidrome_scan_triggered: true`` while NOTHING ran — and Navidrome kept
    serving pre-save rows that the next import wrote back over the edit.

    Now: a single fire-and-forget request to the coalescing rescan service
    (at most one scan running + one queued follow-up, never blocks), so the
    reported value is truthful without reintroducing the scan storm.
    Returns True when the request was accepted (scan running or queued).
    """
    try:
        from services.scanning.navidrome_rescan_service import request_rescan

        return bool(request_rescan("track metadata save"))
    except Exception as exc:
        logger.debug("Navidrome rescan request failed", error=str(exc))
        return False


def _coerce_optional_int(value: Any, allow_prefix: bool = False) -> int | None:
    """Return an int for numeric input, otherwise None.

    JSON booleans map to 1/0 so BIGINT flag columns (``is_cover``, ``is_live``,
    ``is_remix``, ``alternate_take``, …) accept the album/track edit modals'
    checkbox values — previously ``True``/``False`` were ``str()``-ed to
    ``"True"``/``"False"`` and coerced to None, silently nulling the flags.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text_val = str(value).strip()
    if not text_val:
        return None
    candidate = text_val
    if allow_prefix and "/" in text_val:
        candidate = text_val.split("/", 1)[0].strip()
    if not candidate:
        return None
    signless = candidate[1:] if candidate.startswith("-") else candidate
    if not signless.isdigit():
        return None
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def _parse_flag_bool(value: Any) -> bool:
    """Coerce JSON 0/1, ``true``/``false`` and checkbox strings to a bool."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def _values_equal(a: Any, b: Any) -> bool:
    """Compare a DB value and an incoming API value loosely."""
    a_str = "" if a is None else str(a).strip()
    b_str = "" if b is None else str(b).strip()
    return a_str == b_str


def _get_track_column_types(session: Any) -> dict[str, str]:
    """Return tracks column name -> normalized data type."""
    try:
        result = session.execute(
            text("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'tracks'
            """)
        )
        return {
            str(row._mapping["column_name"]).lower(): str(row._mapping["data_type"]).lower()
            for row in result.fetchall()
        }
    except Exception:
        return {}


def _normalize_track_updates(
    updates: dict[str, Any],
    column_types: dict[str, str],
) -> dict[str, Any]:
    """Coerce incoming values to match the real tracks column types."""
    int_types = {"integer", "bigint", "smallint"}
    numeric_types = {"numeric", "double precision", "real", "decimal"}

    normalized: dict[str, Any] = {}
    for column_name, value in updates.items():
        column_type = column_types.get(column_name, "")

        if column_type == "boolean":
            normalized[column_name] = _parse_flag_bool(value)
        elif column_type in int_types:
            int_value = _coerce_optional_int(
                value,
                allow_prefix=(column_name == "track_number"),
            )
            normalized[column_name] = int_value
        elif column_type in numeric_types:
            try:
                normalized[column_name] = float(value) if str(value).strip() else None
            except (TypeError, ValueError):
                normalized[column_name] = None
        else:
            normalized[column_name] = value

    return normalized


# ---------------------------------------------------------------------------
# GET /api/track/<track_id>
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>", methods=["GET"])
def api_get_track(track_id: str) -> Any:
    """Get track metadata by ID."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
            track = dict(row._mapping)
            payload = {"success": True, "track": track}
            payload.update(track)
            return jsonify(payload)
    except Exception as exc:
        logger.error("Error fetching track", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/track/<track_id>/audio
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/audio")
async def api_track_audio(track_id: str) -> Any:
    """Stream an audio file for in-browser playback."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT file_path FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            if not row or not row._mapping.get("file_path"):
                return Response("", status=404)
            file_path = str(row._mapping.get("file_path"))
            
        if "__queued_for_download__" in file_path:
            return Response("", status=404)
            
        resolved = os.path.realpath(file_path)
        if not os.path.isfile(resolved):
            return Response("", status=404)
            
        cfg = get_config()
        music_folder = os.path.realpath(
            cfg.get("navidrome", {}).get("music_folder", "")
            or os.environ.get("MUSIC_FOLDER", "")
            or os.environ.get("MUSIC_DIR", "/music")
        )
        
        if not resolved.startswith(music_folder + os.sep) and resolved != music_folder:
            return Response("", status=403)
            
        ext = os.path.splitext(resolved)[1].lower()
        mime_map = {
            ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg",
            ".opus": "audio/ogg; codecs=opus", ".m4a": "audio/mp4",
            ".aac": "audio/aac", ".wav": "audio/wav",
        }
        return await send_file(resolved, mimetype=mime_map.get(ext, "application/octet-stream"), conditional=True)
    except Exception as exc:
        logger.error("Error streaming track audio", track_id=track_id, error=str(exc))
        return Response("", status=500)


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/rename-file
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/rename-file", methods=["POST"])
def api_track_rename_file(track_id: str) -> Any:
    """Rename/move a single track's file using the configured naming format."""
    try:
        from services.downloads.download_organize_helpers import _build_target_path

        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT file_path, artist, album_artist, album, title,
                           track_number, disc_number, year
                    FROM tracks WHERE CAST(id AS TEXT) = :id
                """),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                return jsonify({"success": False, "error": "Track not found"}), 404
                
            mapping = row._mapping
            src = str(mapping.get("file_path") or "").strip()
            artist = str(mapping.get("artist") or "").strip()
            album_artist = str(mapping.get("album_artist") or "").strip()
            album = str(mapping.get("album") or "").strip()
            title = str(mapping.get("title") or "").strip()
            track_number = mapping.get("track_number")
            disc_number = mapping.get("disc_number")
            year = mapping.get("year")
            
        cfg = get_config() or {}
        music_root = os.path.realpath(
            (cfg.get("music", {}) or {}).get("root")
            or os.environ.get("MUSIC_ROOT")
            or os.environ.get("MUSIC_FOLDER")
            or "/music"
        )
        src_resolved = src if os.path.isabs(src) else os.path.join(music_root, src)
        if not src or not os.path.isfile(src_resolved):
            return jsonify({"success": False, "error": f"File not found: {src}"}), 404

        dest = _build_target_path(
            music_root,
            album_artist or artist,
            year,
            album,
            artist,
            title,
            track_number,
            src_resolved,
            disc_number=disc_number,
        )
        dest = os.path.realpath(dest)
        if not is_path_under_directory(dest, music_root):
            return jsonify({"success": False, "error": "Refusing to move file outside the music library"}), 403
        if os.path.normpath(dest) == os.path.normpath(src_resolved):
            return jsonify({"success": True, "renamed": False, "unchanged": True, "old_path": src, "new_path": dest})

        if os.path.exists(dest):
            stem, suffix = os.path.splitext(dest)
            counter = 1
            while os.path.exists(dest):
                dest = f"{stem} ({counter}){suffix}"
                counter += 1

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src_resolved, dest)

        try:
            if is_path_under_directory(src_resolved, music_root):
                cleanup_empty_parents(src_resolved, music_root)
        except Exception:
            pass

        store_path = (
            os.path.relpath(dest, music_root)
            if not os.path.isabs(src)
            else dest
        )
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET file_path = :path WHERE CAST(id AS TEXT) = :id"),
                {"path": store_path, "id": track_id},
            )
        return jsonify({"success": True, "renamed": True, "old_path": src, "new_path": dest})
    except Exception as exc:
        logger.error("Error renaming track file", track_id=track_id, error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/toggle-manual-single
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/toggle-manual-single", methods=["POST"])
def api_toggle_manual_single(track_id: str) -> Any:
    """Toggle single_manual_override flag for a track."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT single_manual_override FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
                
            current = bool(row._mapping.get("single_manual_override"))
            new_val = 0 if current else 1
            session.execute(text("UPDATE tracks SET single_manual_override = :val WHERE CAST(id AS TEXT) = :id"), {"val": new_val, "id": track_id})
            return jsonify({"success": True, "single_manual_override": bool(new_val)})
    except Exception as exc:
        logger.error("Toggle manual single failed", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Track favourites
# ---------------------------------------------------------------------------

@track_bp.get("/favourite")
async def api_track_favourite_get() -> Any:
    """Check whether a track is favourited."""
    track_id = request.args.get("track_id", "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
        
    with db_session() as session:
        result = session.execute(
            text("SELECT 1 FROM bookmarks WHERE type = 'track_favourite' AND LOWER(name) = LOWER(:id) LIMIT 1"),
            {"id": track_id},
        )
        return jsonify({"success": True, "is_favourite": result.fetchone() is not None}), 200


@track_bp.post("/favourite")
async def api_track_favourite_add() -> Any:
    """Add a track to favourites."""
    data = (await request.get_json()) or {}
    track_id = str(data.get("track_id") or "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
        
    with db_session() as session:
        session.execute(
            text("INSERT INTO bookmarks (type, name) VALUES ('track_favourite', :id) ON CONFLICT DO NOTHING"),
            {"id": track_id},
        )
    return jsonify({"success": True, "is_favourite": True}), 200


@track_bp.delete("/favourite")
async def api_track_favourite_remove() -> Any:
    """Remove a track from favourites."""
    track_id = request.args.get("track_id", "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
        
    with db_session() as session:
        session.execute(
            text("DELETE FROM bookmarks WHERE type = 'track_favourite' AND LOWER(name) = LOWER(:id)"),
            {"id": track_id},
        )
    return jsonify({"success": True, "is_favourite": False}), 200


# ---------------------------------------------------------------------------
# POST /api/track/update-metadata
# ---------------------------------------------------------------------------

@track_bp.route("/update-metadata", methods=["POST"])
async def api_track_update_metadata() -> Any:
    """Update track metadata comprehensively."""
    try:
        data = (await request.get_json()) or {}
        track_id = str(data.get("track_id") or "").strip()
        if not track_id:
            return jsonify({"error": "track_id required"}), 400
            
        with db_session() as session:
            allowed_fields = {
                "title", "artist", "album", "album_artist", "writer", "work",
                "genres", "musicbrainz_genres", "stars", "is_single", "single_confidence",
                "year", "track_number", "disc_number", "mbid", "isrc",
                "is_cover", "original_cover_artist", "alternate_take", "is_compilation",
                "is_live", "is_acoustic", "is_remix",
                "musicbrainz_albumid", "musicbrainz_artistid", "musicbrainz_albumartistid",
                "musicbrainz_releasegroupid", "musicbrainz_releasetrackid", "musicbrainz_workid",
                "musicbrainz_albumtype", "spotify_album_type",
            }
            updates = {}
            for field in allowed_fields:
                if field in data:
                    updates[field] = data[field]
            if not updates:
                return jsonify({"error": "No fields to update"}), 400

            updates = _normalize_track_updates(updates, _get_track_column_types(session))

            if "genres" in updates and updates["genres"] is not None:
                _g_raw = updates["genres"]
                if isinstance(_g_raw, list):
                    _g_parts = [str(g).strip() for g in _g_raw if str(g).strip()]
                else:
                    _g_parts = [
                        g.strip()
                        for g in re.split(r"[,;/\\]+", str(_g_raw))
                        if g.strip()
                    ]
                updates["genres"] = ", ".join(_g_parts) if _g_parts else None

            album_scoped_fields = {
                "album", "album_artist", "year",
                "musicbrainz_albumid", "musicbrainz_albumartistid",
                "musicbrainz_releasegroupid",
            }
            album_tracks_updated = 0
            apply_to_album = data.get("apply_to_album") is True
            album_updates = {
                k: v for k, v in updates.items() if k in album_scoped_fields
            }
            if apply_to_album and album_updates:
                try:
                    current_row = session.execute(
                        text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"),
                        {"id": track_id},
                    ).fetchone()
                    current = dict(current_row._mapping) if current_row else {}
                    old_album = current.get("album")
                    if old_album:
                        changed = {
                            k: v for k, v in album_updates.items()
                            if not _values_equal(v, current.get(k))
                        }
                        if changed:
                            old_album_artist = (
                                current.get("album_artist")
                                or current.get("artist")
                                or ""
                            )
                            set_clause = ", ".join(
                                f"{k} = :{k}" for k in changed
                            )
                            conditions = [
                                "CAST(id AS TEXT) <> :id",
                                "album = :old_album",
                                "COALESCE(NULLIF(album_artist, ''), artist) = :old_album_artist",
                            ]
                            params = {
                                **changed,
                                "id": track_id,
                                "old_album": old_album,
                                "old_album_artist": old_album_artist,
                            }
                            for k, v in changed.items():
                                if k in ("album", "album_artist"):
                                    continue
                                old = current.get(k)
                                params[f"old_{k}"] = "" if old is None else str(old)
                                conditions.append(
                                    f"COALESCE(NULLIF({k}::text, ''), '') = :old_{k}"
                                )
                            result = session.execute(
                                text(
                                    "UPDATE tracks SET "
                                    + set_clause
                                    + " WHERE "
                                    + " AND ".join(conditions)
                                ),
                                params,
                            )
                            album_tracks_updated = result.rowcount or 0
                except Exception as album_err:
                    logger.warning("Album-scoped propagation failed", track_id=track_id, error=str(album_err))

            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            params = {**updates, "id": track_id}
            session.execute(text(f"UPDATE tracks SET {set_clause} WHERE CAST(id AS TEXT) = :id"), params)

            if any(f in updates for f in ("is_live", "is_acoustic")) \
                    and not (updates.get("is_live") or updates.get("is_acoustic")):
                try:
                    from services.popularity.stages.album_stage import revert_track_live_state
                    revert_track_live_state(track_id)
                except Exception as revert_err:
                    logger.debug("Live-state revert failed", track_id=track_id, error=str(revert_err))

        file_synced = False
        if data.get("sync_to_file", True):
            try:
                file_synced = bool(sync_track_tags_to_file(track_id))
            except Exception as sync_err:
                logger.warning("File tag sync failed after DB update", track_id=track_id, error=str(sync_err))

        navidrome_scan_triggered = False
        if file_synced:
            # The file tags changed — trigger a Navidrome library scan so its
            # "mapped tags" index re-reads the new frames.  Without this the
            # server keeps serving the stale mapped tags even after a manual
            # rescan (raw tags update immediately, mapped tags do not).
            navidrome_scan_triggered = _trigger_navidrome_scan()

        return jsonify({
            "success": True,
            "updated": list(updates.keys()),
            "file_synced": file_synced,
            "album_tracks_updated": album_tracks_updated,
            "navidrome_scan_triggered": navidrome_scan_triggered,
        })
    except Exception as exc:
        logger.error("Update metadata failed", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/bulk-update
# ---------------------------------------------------------------------------

@track_bp.route("/bulk-update", methods=["POST"])
async def api_track_bulk_update() -> Any:
    """Apply the Edit Album page's IDENTITY fields to SELECTED tracks.

    Backs the album page's bulk "Edit Tracks" button (beside "Rename Selected").
    Where "Rename Selected" moves FILES, this edits the rows — the point being
    to realign a handful of tracks onto a *different* album (or to correct a
    year/title/artist) without touching the rest of the tracklist.

    Body::

        {"track_ids": ["…"], "fields": {"album": "…", "year": "2007"},
         "sync_to_file": true}

    A field that is ABSENT or blank means "leave unchanged" — the modal only
    posts what the user filled in, so a bulk edit is a partial update by
    construction. Every id in ``track_ids`` is written; the DB update is one
    statement and the file fan-out is the same ``sync_track_tags_to_file`` the
    single-track endpoint uses, so the tags Navidrome reads stay in step with
    the rows (the album-save fan-out rule).
    """
    data = (await request.get_json(silent=True)) or {}
    track_ids = [str(t).strip() for t in (data.get("track_ids") or []) if str(t).strip()]
    fields = data.get("fields") or {}

    if not track_ids:
        return jsonify({"success": False, "error": "track_ids required"}), 400
    if not isinstance(fields, dict) or not fields:
        return jsonify({"success": False, "error": "fields required"}), 400

    allowed = {
        "album", "album_artist", "year", "release_year",
        "title", "artist", "writer", "track_number", "disc_number",
    }
    unknown = sorted(set(str(k) for k in fields) - allowed)
    if unknown:
        # Refused rather than silently dropped: a field the caller believes it
        # set must never look applied.
        return jsonify({
            "success": False,
            "error": f"Unsupported field(s): {', '.join(unknown)}",
        }), 400

    updates = {
        str(k): v for k, v in fields.items()
        if v is not None and str(v).strip() != ""
    }
    if not updates:
        return jsonify({"success": False, "error": "No fields to update"}), 400

    try:
        with db_session() as session:
            updates = _normalize_track_updates(updates, _get_track_column_types(session))
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            placeholders = ", ".join(f":t{i}" for i in range(len(track_ids)))
            params = {
                **updates,
                **{f"t{i}": track_id for i, track_id in enumerate(track_ids)},
            }
            session.execute(
                text(
                    f"UPDATE tracks SET {set_clause} "
                    f"WHERE CAST(id AS TEXT) IN ({placeholders})"
                ),
                params,
            )
    except Exception as exc:
        logger.error("Bulk track update failed", count=len(track_ids), error=str(exc))
        return jsonify({"success": False, "error": str(exc)}), 500

    files_updated = 0
    file_failures: list[str] = []
    if data.get("sync_to_file", True):
        for track_id in track_ids:
            try:
                if sync_track_tags_to_file(track_id):
                    files_updated += 1
                else:
                    file_failures.append(track_id)
            except Exception as sync_err:
                file_failures.append(track_id)
                logger.warning(
                    "File tag sync failed for bulk update",
                    track_id=track_id, error=str(sync_err),
                )

    navidrome_scan_triggered = False
    if files_updated:
        # One scan for the whole batch, not one per track.
        navidrome_scan_triggered = _trigger_navidrome_scan()

    return jsonify({
        "success": True,
        "updated": sorted(updates),
        "tracks_requested": len(track_ids),
        "files_updated": files_updated,
        "file_failures": file_failures,
        "navidrome_scan_triggered": navidrome_scan_triggered,
    })


# ---------------------------------------------------------------------------
# GET /api/track/genre-recommendations
# ---------------------------------------------------------------------------

@track_bp.route("/genre-recommendations", methods=["GET"])
def track_genre_recommendations() -> Any:
    """Get genre recommendations for a track from various sources."""
    track_id = request.args.get("track_id", "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
        
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
                
            mapping = row._mapping
            genres = {}
            keys = ["spotify_genres", "lastfm_tags", "musicbrainz_genres", "discogs_genres"]
            
            for key in keys:
                val = mapping.get(key)
                if val:
                    if isinstance(val, str):
                        try:
                            parsed = json.loads(val) if val.startswith("[") else [val]
                        except json.JSONDecodeError:
                            parsed = [g.strip() for g in val.replace("\\", ",").split(",") if g.strip()]
                    elif isinstance(val, list):
                        parsed = val
                    else:
                        parsed = []
                    genres[key] = parsed if isinstance(parsed, list) else [str(parsed)]
                    
        return jsonify({"success": True, "genres": genres})
    except Exception as exc:
        logger.error("Get genre recommendations failed", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/rescan-single
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/rescan-single", methods=["POST"])
async def api_rescan_single_track(track_id: str) -> Any:
    """Force a fresh single detection scan for one track."""
    try:
        with db_session() as session:
            source = ""
            try:
                body = (await request.get_json(silent=True)) or {}
                source = str(body.get("source") or "").strip()
            except Exception:
                source = ""

            if source:
                row = session.execute(
                    text("SELECT single_sources FROM tracks WHERE CAST(id AS TEXT) = :id"),
                    {"id": track_id},
                ).fetchone()
                
                raw = str(row._mapping.get("single_sources") or "") if row else ""
                remaining: list[Any] = []
                
                if raw.strip():
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, list):
                            remaining = [
                                entry for entry in parsed
                                if not (
                                    isinstance(entry, dict)
                                    and str(entry.get("source") or "") == source
                                ) and not (
                                    isinstance(entry, str) and entry.strip() == source
                                )
                            ]
                    except Exception:
                        remaining = []

                if not remaining:
                    session.execute(
                        text("""
                            UPDATE tracks
                            SET single_sources = '',
                                single_detection_last_updated = NULL,
                                is_single = FALSE,
                                single_confidence = 'low',
                                single_confidence_score = 0.0
                            WHERE CAST(id AS TEXT) = :id
                        """),
                        {"id": track_id},
                    )
                else:
                    session.execute(
                        text("""
                            UPDATE tracks
                            SET single_sources = :sources_json,
                                single_detection_last_updated = NULL
                            WHERE CAST(id AS TEXT) = :id
                        """),
                        {
                            "id": track_id,
                            "sources_json": json.dumps(remaining, default=str),
                        },
                    )
                return jsonify({
                    "success": True,
                    "message": f"Source '{source}' cleared for re-scan",
                })
            else:
                session.execute(
                    text("UPDATE tracks SET single_detection_last_updated = NULL WHERE CAST(id AS TEXT) = :id"),
                    {"id": track_id},
                )
                return jsonify({"success": True, "message": "Single detection cleared for re-scan"})
    except Exception as exc:
        logger.error("Rescan single failed", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/apply-mb-release
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/apply-mb-release", methods=["POST"])
async def api_track_apply_mb_release(track_id: str) -> Any:
    """Apply a chosen MusicBrainz release MBID to a track."""
    try:
        data = (await request.get_json()) or {}
        release_mbid = str(data.get("release_mbid") or "").strip()
        if not release_mbid:
            return jsonify({"error": "release_mbid required"}), 400
            
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET musicbrainz_album_mbid = :mbid WHERE CAST(id AS TEXT) = :id"),
                {"mbid": release_mbid, "id": track_id},
            )
        return jsonify({"success": True})
    except Exception as exc:
        logger.error("Apply MB release failed", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/lyrics/fetch
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/lyrics/fetch", methods=["POST"])
async def api_fetch_track_lyrics(track_id: str) -> Any:
    """Fetch plain + synced lyrics from LRCLIB and store them on the track."""
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
            track = dict(row._mapping)

        result = fetch_lyrics(
            track_name=str(track.get("title") or ""),
            artist_name=str(track.get("artist") or ""),
            album_name=str(track.get("album") or "") or None,
            duration=(
                float(track["duration"])
                if track.get("duration")
                else None
            ),
        )
        plain = result.get("plain") or ""
        synced = result.get("synced") or ""
        if not plain and not synced:
            return jsonify({"found": False, "lyrics": None, "synced": False, "source": "lrclib"})

        stored = synced or plain
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET lyrics = :lyrics WHERE CAST(id AS TEXT) = :id"),
                {"lyrics": stored, "id": str(track_id)},
            )
        return jsonify({
            "found": True,
            "lyrics": stored,
            "synced": bool(synced),
            "plain": plain,
            "synced_lrc": synced,
            "source": "lrclib",
        })
    except Exception as exc:
        logger.error("Lyrics fetch failed", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/fetch-credits
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/fetch-credits", methods=["POST"])
async def api_fetch_track_credits(track_id: str) -> Any:
    """Fetch recording credits from MusicBrainz artist relations."""
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT recording_mbid, musicbrainz_trackid FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
                
            mapping = row._mapping
            rec_mbid = str(mapping.get("recording_mbid") or mapping.get("musicbrainz_trackid") or "").strip()

        if not rec_mbid:
            return jsonify({
                "found": False,
                "error": "Track has no MusicBrainz recording ID — run a MusicBrainz lookup first.",
            })

        client = get_shared_mb_client()
        data = client.get_recording(rec_mbid, inc="artist-rels") or {}
        relations = data.get("relations") or []

        credits: dict[str, list[str]] = {
            "composer": [], "lyricist": [], "producer": [],
            "engineer": [], "conductor": [],
        }
        for rel in relations:
            rtype = str(rel.get("type") or "").lower()
            if rtype not in credits:
                continue
            name = str((rel.get("artist") or {}).get("name") or "").strip()
            if name and name not in credits[rtype]:
                credits[rtype].append(name)

        found = {k: "; ".join(v) for k, v in credits.items() if v}
        return jsonify({"found": bool(found), **found})
    except Exception as exc:
        logger.error("Credits fetch failed", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/track/<track_id>/mb-releases
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/mb-releases", methods=["GET"])
def api_track_mb_releases(track_id: str) -> Any:
    """Fetch all MusicBrainz releases containing this track's recording."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT artist, title, mbid FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
                
            mapping = row._mapping
            artist = mapping.get("artist")
            title = mapping.get("title")
            
        client = get_shared_mb_client()
        recordings = client.search_recordings(
            f'artist:"{artist}" AND recording:"{title}"',
            limit=10,
        )
        return jsonify({"success": True, "recordings": recordings})
    except Exception as exc:
        logger.error("Failed to fetch MB releases for track", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/match-missing
# ---------------------------------------------------------------------------

@track_bp.route("/match-missing", methods=["POST"])
async def api_track_match_missing() -> Any:
    """Match a MusicBrainz 'missing' track to an existing track."""
    try:
        data = (await request.get_json()) or {}
        track_id = str(data.get("track_id") or "").strip()
        mb_title = str(data.get("mb_title") or "").strip()
        if not track_id or not mb_title:
            return jsonify({"error": "track_id and mb_title required"}), 400
            
        with db_session() as session:
            session.execute(text("UPDATE tracks SET title = :title WHERE CAST(id AS TEXT) = :id"), {"title": mb_title, "id": track_id})
        return jsonify({"success": True, "updated_title": mb_title})
    except Exception as exc:
        logger.error("Match missing track failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/ignore-mb-field
# ---------------------------------------------------------------------------

@track_bp.route("/ignore-mb-field", methods=["POST"])
async def api_track_ignore_mb_field() -> Any:
    """Permanently ignore a specific MusicBrainz diff field for a track."""
    try:
        data = (await request.get_json()) or {}
        track_id = str(data.get("track_id") or "").strip()
        field = str(data.get("field") or "").strip()
        if not track_id or not field:
            return jsonify({"error": "track_id and field required"}), 400
            
        with db_session() as session:
            result = session.execute(text("SELECT mb_ignored_fields FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            
            ignored = []
            if row:
                raw = row._mapping.get("mb_ignored_fields")
                if raw:
                    try:
                        ignored = json.loads(raw) if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
                    except (TypeError, json.JSONDecodeError):
                        ignored = []
                        
            if field not in ignored:
                ignored.append(field)
                
            session.execute(text("UPDATE tracks SET mb_ignored_fields = :fields WHERE CAST(id AS TEXT) = :id"),
                           {"fields": json.dumps(ignored), "id": track_id})
        return jsonify({"success": True, "ignored_fields": ignored})
    except Exception as exc:
        logger.error("Ignore MB field failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500
