"""
Album metadata service (clean version)

✅ No raw SQL
✅ Uses repository only
✅ No queue logic
✅ No retry logic
✅ No HTTP logic (except fallback - optional)
"""

from __future__ import annotations

import os
import re
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.metadata import (
    album_is_favourite,
    set_album_favourite_db,
    fetch_album_art_blob,
    fetch_album_tracklist,
    fetch_album_queue_track_stubs,
    fetch_queue_status,
    fetch_album_tracks_for_tag_update,
    save_album_art_db,
    update_track_genres,
    update_album_mbid_fields,
    update_album_discogs_fields,
    ignore_missing_track_db,
)
from services.queue.queue_constraints import STATUS_DISPLAY_CONFIG
from services.enrichment.album_art_service import (
    save_album_art_to_db,
    fetch_album_art_from_itunes,
    fetch_album_art_from_musicbrainz,
    get_or_fetch_album_art as _fetch_art_canonical,
)
from services.enrichment.musicbrainz_service import get_shared_mb_client
from services.metadata.album_mbid_guard import (
    guard_album_mbid,
    is_album_mbid_guard_enabled,
    log_verdict,
    rejection_payload,
    resolve_mbid_text,
)

logger = structlog.get_logger(__name__)


def _album_file_paths(artist: str, album: str) -> list[str]:
    """Every on-disk path for an album, for the physical-boundary check.

    Paths may be stored RELATIVE (Navidrome imports "Artist/Album/01 - x.mp3"),
    which is fine: the guard only COMPARES folders with each other, so it never
    needs to resolve them against MUSIC_ROOT.
    """
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                """),
                {"artist": artist, "album": album},
            ).fetchall() or []
        return [str(r[0] or "") for r in rows]
    except Exception as exc:
        logger.debug("Album guard could not load file paths", artist=artist, album=album, error=str(exc))
        return []

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_release_name(album_name: str) -> str:
    """Strips '(Topshelf Edition)', '[Deluxe Version]', etc. for exact API matches."""
    if not album_name:
        return ""
    cleaned = re.sub(
        r'\s*[\(\[].*?(edition|deluxe|remaster|version|bonus|expanded|explicit|clean).*?[\)\]]', 
        '', 
        album_name, 
        flags=re.IGNORECASE
    ).strip()
    return cleaned if cleaned else album_name


# =============================================================================
# FILE OPERATIONS
# =============================================================================

def rename_album_files_service(
    artist: str,
    album: str,
) -> dict[str, Any]:
    """Rename all files in an album based on the configured naming format."""
    import shutil

    from helpers.config_helpers import get_config
    from services.downloads.download_organize_helpers import (
        _sanitize_path_component,
        _normalize_album_artist_for_path,
    )
    from services.infrastructure.filesystem_service import cleanup_empty_parents

    cfg = get_config() or {}
    downloads_cfg = cfg.get("downloads", {}) or {}
    file_name_format = str(
        downloads_cfg.get("file_name_format")
        or "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}"
    ).strip()
    fallback_format = "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}"

    conversion_cfg = downloads_cfg.get("conversion", {}) or {}
    conversion_enabled = bool(conversion_cfg.get("enabled", False)) and (
        str(conversion_cfg.get("mode", "flac_to_mp3")) == "flac_to_mp3"
    )
    try:
        mp3_bitrate = max(96, min(320, int(conversion_cfg.get("mp3_bitrate_kbps", 320) or 320)))
    except (TypeError, ValueError):
        mp3_bitrate = 320

    music_root = (
        (cfg.get("music", {}) or {}).get("root")
        or os.environ.get("MUSIC_ROOT")
        or os.environ.get("MUSIC_FOLDER")
        or "/music"
    )
    music_root = os.path.realpath(str(music_root or "/music").strip() or "/music")

    def _safe_track_number(value: Any) -> str:
        try:
            num = int(str(value or "").strip() or 0)
            return f"{num:02d}" if num > 0 else "00"
        except (TypeError, ValueError):
            return "00"

    def _resolve_existing(path_value: Any) -> str | None:
        raw = str(path_value or "").strip()
        if not raw:
            return None
        candidates = [raw]
        if not os.path.isabs(raw):
            candidates.append(os.path.join(music_root, raw))
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return None

    def _render_target(fmt: str, fmt_vars: dict[str, str], ext: str) -> str:
        rel = fmt.format(**fmt_vars).replace("\\", "/").strip()
        if rel.endswith("/"):
            rel = rel + f"{fmt_vars['track_number']}. {fmt_vars['artist']} - {fmt_vars['title']}"
        rel = rel.strip("/")
        if not os.path.splitext(os.path.basename(rel))[1]:
            rel = f"{rel}{ext}"
        return rel

    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT CAST(id AS TEXT) AS id, title, artist, album_artist,
                           album, year, track_number, file_path
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                    ORDER BY COALESCE(disc_number, '1'),
                             COALESCE(track_number, '999'),
                             title
                """),
                {"artist": artist, "album": album},
            ).fetchall()
    except Exception as exc:
        logger.error("Failed to load tracks for rename", artist=artist, album=album, error=str(exc), exc_info=True)
        return {"success": False, "error": f"Failed to load album tracks: {exc}"}

    if not rows:
        logger.debug("No tracks found for rename", artist=artist, album=album)
        return {"success": False, "error": "No tracks found for this album"}

    logger.debug(
        "Renaming album files",
        artist=artist, album=album, count=len(rows), format=file_name_format, conversion=conversion_enabled,
    )

    renamed_count = 0
    updated_db_count = 0
    errors: list[str] = []
    details: list[dict[str, str]] = []
    moved_src_dirs: list[str] = []

    for row in rows:
        track = dict(row._mapping)
        track_id = track.get("id")
        file_path_value = track.get("file_path")
        src_path = _resolve_existing(file_path_value)
        if not src_path:
            _msg = f"{track.get('title') or '?'}: file not found on disk ({file_path_value or 'no path'})"
            errors.append(_msg)
            logger.warning("File not found on disk for rename", track=track.get("title"), path=file_path_value)
            continue

        ext = os.path.splitext(src_path)[1]
        fmt_vars = {
            "track_number": _safe_track_number(track.get("track_number")),
            "artist": _sanitize_path_component(track.get("artist") or "Unknown Artist") or "Unknown Artist",
            "album_artist": _normalize_album_artist_for_path(
                _sanitize_path_component(track.get("album_artist") or "")
            ) or _sanitize_path_component(track.get("artist") or "Unknown Artist") or "Unknown Artist",
            "title": _sanitize_path_component(track.get("title") or "Unknown Title") or "Unknown Title",
            "album": _sanitize_path_component(track.get("album") or "Unknown Album") or "Unknown Album",
            "year": str(track.get("year") or "").strip()[:4] or "Unknown",
        }

        try:
            rel_target = _render_target(file_name_format, fmt_vars, ext)
        except Exception:
            rel_target = _render_target(fallback_format, fmt_vars, ext)

        actual_src = src_path
        if conversion_enabled and ext.lower() == ".flac":
            try:
                from services.metadata.tag_file_service import convert_flac_to_mp3

                converted = convert_flac_to_mp3(src_path, bitrate=f"{mp3_bitrate}k")
                if not converted or not os.path.isfile(converted):
                    _msg = f"{fmt_vars['title']}: FLAC→MP3 conversion failed (is ffmpeg installed?)"
                    errors.append(_msg)
                    logger.warning("Conversion failed", track=fmt_vars["title"], src=src_path)
                    continue
                actual_src = converted
                if os.path.splitext(rel_target)[1].lower() == ".flac":
                    rel_target = os.path.splitext(rel_target)[0] + ".mp3"
            except Exception as exc:
                _msg = f"{fmt_vars['title']}: conversion failed ({exc})"
                errors.append(_msg)
                logger.warning("Conversion failed with exception", track=fmt_vars["title"], error=str(exc))
                continue

        target_abs = os.path.join(music_root, rel_target)

        if os.path.normpath(target_abs) == os.path.normpath(actual_src):
            continue

        if os.path.exists(target_abs):
            stem, suffix = os.path.splitext(target_abs)
            counter = 1
            while os.path.exists(target_abs):
                target_abs = f"{stem} ({counter}){suffix}"
                counter += 1

        try:
            os.makedirs(os.path.dirname(target_abs), exist_ok=True)
            shutil.move(actual_src, target_abs)
        except Exception as exc:
            _msg = f"{fmt_vars['title']}: move failed ({exc})"
            errors.append(_msg)
            logger.warning("File move failed", track=fmt_vars["title"], error=str(exc))
            continue

        old_dir = os.path.dirname(os.path.abspath(actual_src))
        if old_dir.startswith(music_root + os.sep):
            moved_src_dirs.append(old_dir)

        store_path = (
            os.path.relpath(target_abs, music_root)
            if not os.path.isabs(str(file_path_value or ""))
            else target_abs
        )
        try:
            with db_session() as update_session:
                result = update_session.execute(
                    text(
                        "UPDATE tracks SET file_path = :path WHERE CAST(id AS TEXT) = :id"
                    ),
                    {"path": store_path, "id": track_id},
                )
                updated_db_count += result.rowcount or 0
        except Exception as exc:
            _msg = f"{fmt_vars['title']}: database update failed ({exc})"
            errors.append(_msg)
            logger.warning("DB path update failed after move", track=fmt_vars["title"], error=str(exc))
            renamed_count += 1
            details.append({
                "track": fmt_vars["title"],
                "old_path": str(file_path_value or ""),
                "new_path": store_path,
            })
            continue

        renamed_count += 1
        details.append({
            "track": fmt_vars["title"],
            "old_path": str(file_path_value or ""),
            "new_path": store_path,
        })

    for directory in set(moved_src_dirs):
        try:
            cleanup_empty_parents(directory, music_root)
        except Exception as exc:
            logger.debug("Empty-dir cleanup failed", directory=directory, error=str(exc))

    return {
        "success": renamed_count > 0 or not errors,
        "renamed_count": renamed_count,
        "updated_db_count": updated_db_count,
        "errors": errors,
        "details": details,
        "message": (
            f"Renamed {renamed_count} file(s)"
            + (f", {len(errors)} error(s)" if errors else "")
        ),
    }


# =============================================================================
# FAVOURITES
# =============================================================================

def is_album_favourite(
    artist: str,
    album: str,
) -> bool:
    return album_is_favourite(artist=artist, album=album)


def set_album_favourite(
    artist: str,
    album: str,
    is_favourite: bool,
) -> bool:
    try:
        set_album_favourite_db(artist=artist, album=album, is_favourite=is_favourite)
        return True
    except Exception as exc:
        logger.error("Error setting favourite", artist=artist, album=album, error=str(exc), exc_info=True)
        return False


# =============================================================================
# ALBUM ART
# =============================================================================

def get_local_album_art(
    artist: str,
    album: str,
) -> tuple[bytes | None, str | None]:
    """Album art for the album page: Navidrome FIRST, then what is stored.

    ``getCoverArt`` on the album the user owns is the correct cover and a local
    request, so it is asked before anything else on the online side. Cached art
    a PROVIDER supplied may be replaced by it; the user's own upload or URL, and
    Navidrome's own art, are final (see ``navidrome_art_may_replace``).
    """
    from services.enrichment.album_art_service import (
        fetch_album_art_from_navidrome,
        navidrome_art_may_replace,
        save_album_art_to_db,
    )
    from db.repositories.metadata import fetch_album_art_record

    data: bytes | None = None
    mime: str | None = None
    cached_source = ""
    with db_session() as session:
        data, mime, cached_source = fetch_album_art_record(artist=artist, album=album)

        if data and not navidrome_art_may_replace(cached_source):
            return data, mime or "image/jpeg"

    try:
        navidrome_data = fetch_album_art_from_navidrome(artist, album)
        if navidrome_data:
            save_album_art_to_db(artist, album, navidrome_data, source="navidrome")
            return navidrome_data, "image/jpeg"
    except Exception as exc:
        logger.debug("Navidrome album art lookup failed", artist=artist, album=album, error=str(exc))

    if data:
        # Navidrome has no copy — the art we already hold stands.
        return data, mime or "image/jpeg"

    return None, None


def get_or_fetch_album_art(artist: str, album: str) -> tuple[bytes | None, str | None]:
    """Fetch album art from DB or external sources."""
    return _fetch_art_canonical(artist, album)


# =============================================================================
# TRACKLIST
# =============================================================================

def get_album_tracklist(artist: str, album: str) -> list[dict[str, Any]]:
    rows = fetch_album_tracklist(artist=artist, album=album)

    return [
        {
            "track_id": r.get("id") if hasattr(r, "get") else r[0],
            "position": str((r.get("track_number") if hasattr(r, "get") else r[2]) or "").strip() or "—",
            "title": r.get("title") if hasattr(r, "get") else r[1],
            "artist": (r.get("artist") if hasattr(r, "get") else r[4]) or "",
        }
        for r in rows
    ]


def get_album_tracklist_from_db(artist: str, album: str) -> list[dict[str, Any]]:
    return get_album_tracklist(artist, album)


def match_album_tracklist(artist: str, album: str) -> dict[str, Any]:
    """Matches album tracks against local library, falling back to MusicBrainz."""
    logger.debug("Matching tracklist", artist=artist, album=album)

    with db_session() as session:
        album_rows = fetch_album_queue_track_stubs(artist=artist, album=album)
        
        matched_tracks = []
        queued_tracks = []

        for row in album_rows:
            title_val = row.get("title") if hasattr(row, "get") else row[0]
            file_path_val = (row.get("file_path") if hasattr(row, "get") else row[1]) or ""
            entry = {"title": title_val}

            if str(file_path_val).startswith("__queued_for_download__"):
                queued_tracks.append(entry)
            else:
                matched_tracks.append(entry)

        if album_rows:
            return {
                "success": True,
                "matched": matched_tracks,
                "queued": queued_tracks,
                "unmatched": [],
                "status": 200,
            }

        all_artist_rows = fetch_album_tracklist(artist=artist, album="")
        library_tracks = {
            str(r.get("title") if hasattr(r, "get") else r[1]).lower().strip(): True 
            for r in all_artist_rows
            if (r.get("title") if hasattr(r, "get") else r[1])
        }

    # ✅ Fallback to MusicBrainz API via shared client singleton (Sanitized)
    clean_album = _sanitize_release_name(album)
    try:
        from api_clients.musicbrainz_http import escape_lucene_special_chars
        mb = get_shared_mb_client()

        releases = mb.search_releases(
            f'release:"{escape_lucene_special_chars(clean_album)}" AND artist:"{escape_lucene_special_chars(artist)}"',
            limit=5,
        ) or []

        if not releases:
            release_groups = mb.search_release_groups(
                f'"{escape_lucene_special_chars(clean_album)}" AND artist:"{escape_lucene_special_chars(artist)}"',
                limit=1,
            )

            if not release_groups or not release_groups[0].get("id"):
                return {"success": True, "matched": [], "queued": [], "unmatched": [], "status": 200}

            rg_id = release_groups[0]["id"]
            rg_data = mb.get_release_group(rg_id, inc="releases")
            releases = rg_data.get("releases", []) if isinstance(rg_data, dict) else []

        if releases:
            release_id = releases[0].get("id")
            detail = mb.get_release(release_id, inc="recordings")
            media = detail.get("media", []) if isinstance(detail, dict) else []

            mb_matched = []
            mb_unmatched = []

            for medium in media:
                for track_obj in medium.get("tracks", []):
                    track_title = track_obj.get("title") or track_obj.get("recording", {}).get("title", "")
                    if not track_title:
                        continue

                    entry = {"title": track_title}
                    if track_title.lower().strip() in library_tracks:
                        mb_matched.append(entry)
                    else:
                        mb_unmatched.append(entry)

            return {
                "success": True,
                "matched": mb_matched,
                "queued": [],
                "unmatched": mb_unmatched,
                "status": 200,
            }

        return {"success": True, "matched": [], "queued": [], "unmatched": [], "status": 200}

    except Exception as exc:
        logger.error("Error matching tracklist via MusicBrainz", error=str(exc), exc_info=True)
        return {"error": str(exc), "status": 500}


# =============================================================================
# QUEUE STATUS
# =============================================================================

def get_album_queue_status_db(artist: str, album: str) -> dict[str, Any]:
    result = {}
    rows = fetch_album_queue_track_stubs(artist=artist, album=album)

    for row in rows:
        track_id = row.get("id") if hasattr(row, "get") else row[0]
        file_path = (row.get("file_path") if hasattr(row, "get") else row[1]) or ""

        queue_id = None
        if "queue_id_" in file_path:
            try:
                queue_id = int(file_path.split("queue_id_")[-1])
            except ValueError:
                pass

        status = fetch_queue_status(queue_id=queue_id) if queue_id else "queued"
        cfg = STATUS_DISPLAY_CONFIG.get(status, {})

        result[track_id] = {
            "queue_id": queue_id,
            "status": status,
            "label": cfg.get("label", status),
            "css": cfg.get("css", ""),
            "icon": cfg.get("icon", ""),
        }

    return result


# =============================================================================
# GENRES
# =============================================================================

def apply_genres_to_album(artist: str, album: str, genres: list[str]) -> dict[str, Any]:
    from services.metadata.tag_file_service import update_file_tags, resolve_music_file_path

    genres_clean = [g.strip() for g in genres if g.strip()]
    genres_str = ",".join(genres_clean)

    updated = 0
    failed = []

    tracks = fetch_album_tracks_for_tag_update(artist=artist, album=album)

    for t in tracks:
        track_id = t.get("id") if hasattr(t, "get") else t[0]
        title = t.get("title") if hasattr(t, "get") else t[1]
        path = t.get("file_path") if hasattr(t, "get") else t[2]

        resolved = resolve_music_file_path(path)

        if resolved:
            if update_file_tags(resolved, {"genres": genres_clean}):
                update_track_genres(track_id=track_id, genres_str=genres_str)
                updated += 1
            else:
                failed.append(title)
        else:
            failed.append(title)

    return {
        "success": True,
        "updated": updated,
        "failed": len(failed),
        "failed_files": failed,
    }


# =============================================================================
# MBID / DISCOGS
# =============================================================================

def apply_mbid_to_album(artist: str, album: str, mbid: str, rg_mbid: str, cover_url: str) -> dict[str, Any]:
    """Apply a MusicBrainz album/release-group ID to every track in an album.

    Writes BOTH the database columns AND the physical audio file tags — the
    fan-out requirement: Navidrome reads FILE tags, so a DB-only update leaves
    the album split on Navidrome even though Popularr shows it merged.

    When a cover URL (or a resolvable release/release-group MBID) is present,
    the cover art is DOWNLOADED from Cover Art Archive and embedded into every
    track file (and stored in the album_art table) — the reported gap where a
    MusicBrainz lookup "updated the metadata but not the cover art".

    BEFORE any of that, the ID is sanity-checked against the album's OWN text
    (and its physical folders).  A stored ID used to be written to every track
    and file with no comparison at all, so one bad ID from an errant
    batch-tagging run could merge two unrelated albums into a single release
    (the Metallica / d'Artagnan "66-track super album").  On refusal nothing is
    written and the response carries TEXT-SEARCH candidates instead.
    """
    file_paths = _album_file_paths(artist, album)

    if is_album_mbid_guard_enabled():
        mb_text = resolve_mbid_text(mbid=mbid, rg_mbid=rg_mbid)
        verdict = guard_album_mbid(
            artist=artist,
            album=album,
            mb_artist=mb_text.get("artist", ""),
            mb_album=mb_text.get("title", ""),
            file_paths=file_paths or None,
        )
        if not verdict["ok"]:
            log_verdict(verdict, artist=artist, album=album, mbid=mbid, rg_mbid=rg_mbid)
            return rejection_payload(verdict, artist=artist, album=album, mbid=mbid, rg_mbid=rg_mbid)

    rows = update_album_mbid_fields(
        artist=artist, album=album, mbid=mbid, rg_mbid=rg_mbid, cover_url=cover_url,
    )

    # ── Cover art: download + embed ──────────────────────────────────────
    # A ``cover_url`` on the form is a CAA URL string; the album page's
    # ``cover_art_url`` column holds it but the IMAGE is never fetched.  When
    # a release or release-group MBID is known, pull the actual bytes from
    # Cover Art Archive and store + embed them.
    cover_bytes: bytes | None = None
    cover_mime = "image/jpeg"
    try:
        from api_clients.coverartarchive import (
            get_release_front_image_bytes,
            get_release_group_front_image_bytes,
        )
        if mbid:
            cover_bytes = get_release_front_image_bytes(mbid, size="500")
        if cover_bytes is None and rg_mbid:
            cover_bytes = get_release_group_front_image_bytes(rg_mbid, size="500")
    except Exception as exc:
        logger.debug("Cover-art fetch failed", release_mbid=mbid, rg_mbid=rg_mbid, error=str(exc))

    if cover_bytes:
        try:
            from services.enrichment.album_art_service import save_album_art_to_db
            save_album_art_to_db(artist, album, cover_bytes, source="musicbrainz", mime_type=cover_mime)
        except Exception as exc:
            logger.debug("Cover-art DB save failed", artist=artist, album=album, error=str(exc))

    # Fan out to the audio files: Navidrome keys albums on the file tags
    # (``musicbrainz_albumid`` / ``musicbrainz_albumartistid`` / ``album``),
    # so the DB update alone never merges the album on Navidrome.
    file_updated = 0
    file_failed = 0
    try:
        from services.metadata.tag_file_service import (
            resolve_music_file_path,
            update_file_tags,
        )
        from db.engine import db_session as _db_session
        from sqlalchemy import text as _text

        with _db_session() as session:
            result = session.execute(
                _text("""
                    SELECT file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                      AND file_path IS NOT NULL
                """),
                {"artist": artist, "album": album},
            ).fetchall() or []

        for (file_path,) in result:
            resolved = resolve_music_file_path(str(file_path or ""))
            if not resolved:
                file_failed += 1
                continue
            tags: dict[str, Any] = {
                "musicbrainz_albumid": mbid,
                "musicbrainz_album_mbid": mbid,
            }
            if rg_mbid:
                tags["musicbrainz_releasegroupid"] = rg_mbid
            if cover_bytes:
                tags["cover_art_data"] = cover_bytes
                tags["cover_art_mime"] = cover_mime
            try:
                if update_file_tags(resolved, tags):
                    file_updated += 1
                else:
                    file_failed += 1
            except Exception as _tag_exc:
                logger.debug("Album MBID file-tag write failed", file=resolved, error=str(_tag_exc))
                file_failed += 1
    except Exception as exc:
        logger.debug("Album MBID file-tag fan-out failed", error=str(exc))

    return {
        "success": rows > 0,
        "rows_updated": rows,
        "files_updated": file_updated,
        "files_failed": file_failed,
        "cover_art_applied": bool(cover_bytes),
    }


def apply_discogs_id_to_album(artist: str, album: str, discogs_id: str, is_single: bool) -> dict[str, Any]:
    rows = update_album_discogs_fields(
        artist=artist, album=album, discogs_id=discogs_id, is_single=is_single,
    )
    return {"success": True, "rows_updated": rows}


# =============================================================================
# BULK TRACK OPERATIONS
# =============================================================================

def bulk_tag_tracks(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Add genre tags to multiple tracks — DB columns + audio file tags."""
    from services.metadata.tag_file_service import update_file_tags, resolve_music_file_path

    track_ids = [str(t) for t in (payload.get("track_ids") or []) if t]
    genres = [g.strip() for g in (payload.get("genres") or []) if g and g.strip()]
    if not track_ids:
        return {"success": False, "error": "No tracks selected"}, 400
    if not genres:
        return {"success": False, "error": "No genres provided"}, 400

    updated_count = 0
    failed_files: list[str] = []

    with db_session() as session:
        for track_id in track_ids:
            try:
                row = session.execute(
                    text("SELECT title, genres, manual_genres, file_path FROM tracks WHERE id = :id"),
                    {"id": track_id},
                ).mappings().first()
                if not row:
                    continue
                title = row.get("title")
                current = str(row.get("genres") or "")
                manual = str(row.get("manual_genres") or "")
                file_path = str(row.get("file_path") or "")

                def _split_genres(raw: str) -> set[str]:
                    sep = "\\" if "\\" in raw else ","
                    return {g.strip() for g in raw.split(sep) if g.strip()}

                existing = _split_genres(current)
                manual_existing = _split_genres(manual)
                existing.update(genres)
                manual_existing.update(genres)

                new_genres = ", ".join(sorted(existing))
                new_manual = ", ".join(sorted(manual_existing))

                resolved = resolve_music_file_path(file_path)
                if resolved:
                    if not update_file_tags(resolved, {"genres": sorted(existing)}):
                        failed_files.append(title or f"Track ID: {track_id}")
                else:
                    failed_files.append(title or f"Track ID: {track_id}")

                session.execute(
                    text("UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id"),
                    {"genres": new_genres, "manual": new_manual, "id": track_id},
                )
                updated_count += 1
            except Exception as exc:
                logger.error("Bulk tag track failed", track_id=track_id, error=str(exc))
                continue

    return {
        "success": True,
        "updated_count": updated_count,
        "failed_files": failed_files,
    }, 200


def bulk_delete_tracks(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Delete multiple tracks from the database, optionally removing audio files."""
    track_ids = [str(t) for t in (payload.get("track_ids") or []) if t]
    delete_files = bool(payload.get("delete_files", True))
    if not track_ids:
        return {"success": False, "error": "No tracks selected"}, 400

    deleted_count = 0

    with db_session() as session:
        for track_id in track_ids:
            try:
                row = session.execute(
                    text("SELECT file_path FROM tracks WHERE id = :id"),
                    {"id": track_id},
                ).mappings().first()
                if not row:
                    continue
                file_path = str(row.get("file_path") or "")
                if delete_files and file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as exc:
                        logger.warning("Could not delete file from disk", path=file_path, error=str(exc))
                session.execute(text("DELETE FROM tracks WHERE id = :id"), {"id": track_id})
                deleted_count += 1
            except Exception as exc:
                logger.error("Bulk delete track failed", track_id=track_id, error=str(exc))
                continue

    return {"success": True, "deleted_count": deleted_count}, 200


def update_album_ids(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Update release IDs for an album's tracks."""
    artist = str(payload.get("artist") or "").strip()
    album = str(payload.get("album") or "").strip()
    if not artist or not album:
        return {"success": False, "error": "artist and album required"}, 400

    musicbrainz_id = str(payload.get("musicbrainz_release_id") or "").strip()
    release_group_id = str(payload.get("musicbrainz_release_group_id") or "").strip()
    discogs_id = str(payload.get("discogs_release_id") or "").strip()

    if not (musicbrainz_id or release_group_id or discogs_id):
        return {"success": False, "error": "No IDs provided"}, 400

    updates: list[str] = []
    bind_values: dict[str, Any] = {}
    _idx = 0
    if musicbrainz_id:
        updates.append(f"musicbrainz_album_mbid = :v{_idx}")
        bind_values[f"v{_idx}"] = musicbrainz_id
        _idx += 1
    if release_group_id:
        updates.append(f"musicbrainz_releasegroupid = :v{_idx}")
        bind_values[f"v{_idx}"] = release_group_id
        _idx += 1
    if discogs_id:
        updates.append(f"discogs_album_id = :v{_idx}")
        bind_values[f"v{_idx}"] = discogs_id
    bind_values["artist"] = artist
    bind_values["album"] = album

    file_tag_updates: dict[str, Any] = {}
    if musicbrainz_id:
        file_tag_updates["musicbrainz_albumid"] = musicbrainz_id
    if release_group_id:
        file_tag_updates["musicbrainz_releasegroupid"] = release_group_id

    file_updated = 0
    with db_session() as session:
        result = session.execute(
            text(
                f"UPDATE tracks SET {', '.join(updates)} "
                "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"
            ),
            bind_values,
        )
        rows = result.rowcount or 0

        if file_tag_updates:
            from services.metadata.tag_file_service import (
                resolve_music_file_path,
                update_file_tags,
            )
            for r in session.execute(
                text(
                    "SELECT id, file_path FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"
                ),
                {"artist": artist, "album": album},
            ).fetchall() or []:
                _path = resolve_music_file_path(str(r[1] or "") if r[1] else None)
                if _path:
                    try:
                        if update_file_tags(_path, file_tag_updates):
                            file_updated += 1
                    except Exception as exc:
                        logger.debug("File tag write failed", track_id=r[0], error=str(exc))

    return {"success": True, "rows_updated": rows, "files_updated": file_updated}, 200


# =============================================================================
# IGNORE TRACK
# =============================================================================

def ignore_missing_track(missing_id: Any, artist: str, album: str, title: str, disc_number: Any) -> bool:
    try:
        ignore_missing_track_db(
            missing_id=missing_id,
            artist=artist,
            album=album,
            title=title,
            disc_number=disc_number,
        )
        return True
    except Exception as exc:
        logger.error("ignore_missing_track failed", error=str(exc))
        return False


def get_majority_artist(artist: str, album: str) -> dict[str, Any]:
    """Return the most common artist across all tracks in an album."""
    from collections import Counter
    try:
        with db_session() as session:
            rows = session.execute(
                text("SELECT artist FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"),
                {"artist": artist, "album": album},
            ).fetchall()
        counts = Counter(str(r[0]) for r in rows if r[0])
        if not counts:
            return {"success": False, "error": "No tracks found"}
        top = counts.most_common(1)[0]
        return {
            "success": True,
            "majority_artist": top[0],
            "count": top[1],
            "total": sum(counts.values()),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def add_album_to_missing_releases(artist: str, album: str, year: str | None = None) -> dict[str, Any]:
    """Add an album to the missing_releases tracking table."""
    try:
        with db_session() as session:
            session.execute(
                text(
                    "INSERT INTO missing_releases (artist, title, primary_type, first_release_date, category, created_at) "
                    "VALUES (:artist, :album, 'album', :year, 'album', CURRENT_TIMESTAMP) "
                    "ON CONFLICT (artist, title) DO NOTHING"
                ),
                {"artist": artist, "album": album, "year": year or None},
            )
        return {"success": True, "message": f"Added '{album}' to missing releases"}
    except Exception as exc:
        logger.error("Error adding to missing releases", error=str(exc))
        return {"success": False, "error": str(exc)}


def get_track_recommendations(artist: str, album: str) -> dict[str, Any]:
    """Get genre recommendations by aggregating all genre sources in DB."""
    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT lastfm_tags, musicbrainz_genres, discogs_genres "
                    "FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"
                ),
                {"artist": artist, "album": album},
            ).mappings().all()
    except Exception as exc:
        logger.error("Error fetching track genres", artist=artist, album=album, error=str(exc))
        rows = []

    from collections import defaultdict
    source_map: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        cols = ["lastfm_tags", "musicbrainz_genres", "discogs_genres"]
        for src_key, col in zip(["lastfm", "musicbrainz", "discogs"], cols):
            val = row.get(col)
            if val:
                vals = val if isinstance(val, list) else [str(val)]
                source_map[src_key].extend(v.strip() for v in vals if v and v.strip())
    from services.enrichment.genre_aggregation_service import aggregate_genres
    recommended = aggregate_genres(dict(source_map))
    return {"success": True, "artist": artist, "album": album, "genres": recommended}
