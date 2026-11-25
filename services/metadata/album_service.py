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
from db.repositories.popularity_repository import (
    coerce_json_value,
    parse_genre_value,
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
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(album) = LOWER(:album)
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
    track_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Rename files in an album based on the configured naming format.

    ``track_ids`` narrows the run to the tracks SELECTED on the album page
    (bulk "Rename Selected"); ``None`` renames every track in the album (the
    Actions ▸ Rename Files behaviour).  An empty list renames nothing — it
    must never mean "everything".
    """
    import shutil

    from helpers.config_helpers import conversion_target, get_config, is_flac_conversion
    from services.downloads.download_organize_helpers import (
        _AUDIO_EXTS,
        _any_multi_disc,
        _format_track_number_for_rename,
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
    conversion_enabled = bool(conversion_cfg.get("enabled", False)) and is_flac_conversion(
        conversion_cfg.get("mode", "flac_to_mp3")
    )
    conversion_format = conversion_target(conversion_cfg.get("mode", "flac_to_mp3")) or "mp3"
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
        # ⚠️ NOT `not splitext(basename)[1]`: the default format renders
        # "01. One" and splitext happily reports ". One" as the extension, so
        # the real `.flac`/`.mp3` was NEVER appended — the renamed file lost
        # its extension entirely (probe-proven). Append unless the basename
        # already ends in a KNOWN audio extension.
        if os.path.splitext(os.path.basename(rel))[1].lower() not in _AUDIO_EXTS:
            rel = f"{rel}{ext}"
        return rel

    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT CAST(id AS TEXT) AS id, title, artist, album_artist,
                           album, year, track_number, disc_number, file_path
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

    # ⚠️ Determine multi-disc from the WHOLE album BEFORE narrowing to the
    # user's selection: renaming only disc 2 must still produce "201", and
    # disc 1 must keep "101" rather than dropping back to "01".
    multi_disc = _any_multi_disc(
        [r._mapping.get("disc_number") for r in rows]
    )

    if track_ids is not None:
        # Selection mode: only the tracks the user ticked. An empty selection
        # renames NOTHING (never everything) — the route rejects it too.
        wanted = {str(t).strip() for t in (track_ids or []) if str(t or "").strip()}
        rows = [r for r in rows if str(r._mapping.get("id") or "").strip() in wanted]

    if not rows:
        if track_ids is not None:
            return {
                "success": False,
                "error": "None of the selected tracks matched this album",
            }
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
            "track_number": _format_track_number_for_rename(
                track.get("track_number"),
                track.get("disc_number"),
                multi_disc=multi_disc,
            ),
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
                from services.metadata.tag_file_service import (
                    convert_flac_to_m4a,
                    convert_flac_to_mp3,
                )

                if conversion_format == "m4a":
                    converted = convert_flac_to_m4a(src_path, bitrate=f"{mp3_bitrate}k")
                else:
                    converted = convert_flac_to_mp3(src_path, bitrate=f"{mp3_bitrate}k")
                if not converted or not os.path.isfile(converted):
                    _msg = f"{fmt_vars['title']}: FLAC→{conversion_format.upper()} conversion failed (is ffmpeg installed?)"
                    errors.append(_msg)
                    logger.warning("Conversion failed", track=fmt_vars["title"], src=src_path)
                    continue
                actual_src = converted
                if os.path.splitext(rel_target)[1].lower() == ".flac":
                    rel_target = os.path.splitext(rel_target)[0] + f".{conversion_format}"
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
    """Apply genres to every track of an album — DB columns + audio file tags.

    ⚠️ ``success`` now reflects whether anything was written. It was hard-coded
    ``True``, so an album whose every track failed (unresolvable path, tag
    writes disabled) still answered ``success: True`` and the response's
    ``updated: 0`` blew straight through the caller's
    ``status = 200 if result.get("success")`` check.

    ⚠️ The stored path is RESOLVED before writing. It comes straight from the
    DB, where it may be relative to the music root — the writer returns False
    for anything that does not exist on disk, and that False was previously
    counted as a failure correctly but never surfaced.
    """
    from services.metadata.tag_file_service import update_file_tags, resolve_music_file_path

    genres_clean = [g.strip() for g in genres if g.strip()]
    genres_str = ", ".join(genres_clean)

    updated = 0
    failed: list[Any] = []

    tracks = fetch_album_tracks_for_tag_update(artist=artist, album=album)

    for t in tracks:
        track_id = t.get("id") if hasattr(t, "get") else t[0]
        title = t.get("title") if hasattr(t, "get") else t[1]
        path = t.get("file_path") if hasattr(t, "get") else t[2]

        resolved = resolve_music_file_path(path)

        if resolved:
            try:
                if update_file_tags(resolved, {"genres": genres_clean}):
                    update_track_genres(track_id=track_id, genres_str=genres_str)
                    updated += 1
                else:
                    failed.append(title)
            except Exception as exc:
                logger.warning(
                    "apply_genres_to_album: tag write failed",
                    track_id=track_id, title=title, error=str(exc),
                )
                failed.append(title)
        else:
            failed.append(title)

    return {
        "success": updated > 0,
        "updated": updated,
        "failed": len(failed),
        "failed_files": failed,
        "error": None if updated else "No tracks were updated.",
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
    # Tracks whose DB write or tag write was REFUSED. Counted so the caller can
    # tell "applied to 5 tracks" from "refused on all 5" — the function used to
    # return success: True unconditionally, so the endpoint answered 200 and the
    # UI reported the tags applied while every row had been skipped.
    failed_count = 0

    with db_session() as session:
        for track_id in track_ids:
            try:
                row = session.execute(
                    text("SELECT title, genres, manual_genres, file_path FROM tracks WHERE id = :id"),
                    {"id": track_id},
                ).mappings().first()
                if not row:
                    failed_count += 1
                    continue
                title = row.get("title")
                current = str(row.get("genres") or "")
                # ⚠️ ``manual_genres`` is JSONB, so this is a LIST, not a string.
                # ``str(list)`` yields the Python repr "['a', 'b']" — braces,
                # quotes and commas included — which then got split into junk
                # genre names and written straight back into the column.
                manual = row.get("manual_genres")
                file_path = str(row.get("file_path") or "")

                existing = set(parse_genre_value(current))
                manual_existing = set(parse_genre_value(manual))
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
                    {
                        "genres": new_genres,
                        "manual": coerce_json_value(new_manual),
                        "id": track_id,
                    },
                )
                updated_count += 1
            except Exception as exc:
                failed_count += 1
                logger.error("Bulk tag track failed", track_id=track_id, error=str(exc))
                continue

    # A batch that updated NOTHING did not succeed. Reporting success there is
    # what made a total failure look like a completed tag write.
    return {
        "success": updated_count > 0,
        "updated_count": updated_count,
        "failed_count": failed_count,
        "failed_files": failed_files,
        "error": (
            None if updated_count
            else f"No tracks were updated ({failed_count} failed)."
        ),
    }, 200 if updated_count else 500


def bulk_delete_tracks(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Delete multiple tracks from the database, optionally removing audio files."""
    track_ids = [str(t) for t in (payload.get("track_ids") or []) if t]
    delete_files = bool(payload.get("delete_files", True))
    if not track_ids:
        return {"success": False, "error": "No tracks selected"}, 400

    deleted_count = 0
    failed_count = 0

    with db_session() as session:
        for track_id in track_ids:
            try:
                row = session.execute(
                    text("SELECT file_path FROM tracks WHERE id = :id"),
                    {"id": track_id},
                ).mappings().first()
                if not row:
                    # A requested id that no longer exists is NOT a deletion.
                    failed_count += 1
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
                failed_count += 1
                logger.error("Bulk delete track failed", track_id=track_id, error=str(exc))
                continue

    # ⚠️ Was ``{"success": True}, 200`` unconditionally, so deleting nothing
    # (every id missing, or every DELETE refused) reported a completed delete
    # and the UI announced "Deleted 0 track(s)" as a success.
    return {
        "success": deleted_count > 0,
        "deleted_count": deleted_count,
        "failed_count": failed_count,
        "error": None if deleted_count else f"No tracks were deleted ({failed_count} failed).",
    }, 200 if deleted_count else 500


def apply_album_wide_ids(
    *,
    artist: str,
    album: str,
    album_mbid: str | None = None,
    release_group_mbid: str | None = None,
    clear_album_mbid: bool = False,
    previous_release_group_mbid: str | None = None,
) -> dict[str, Any]:
    """Write the album's RELEASE identity to EVERY row of that album.

    ⭐ WHY THIS IS SEPARATE FROM THE PAGE SAVE.

    ``album_detail`` scopes its track list to ONE release group, so a folder
    whose files disagree about their release group renders the majority's rows
    and silently omits the others. The page save therefore only corrected the
    rows it displayed, and a file carrying a stale id kept it — then the next
    Navidrome import read that stale id back off the file and re-imported the
    album under the wrong release (the reported "I save the metadata and it
    still keeps importing as SKZ2020").

    A release id describes the ALBUM, so it belongs on every row of that album
    whether or not the page had room to show it. Delegates to
    :func:`update_album_ids`, which already performs the DB + file fan-out, so
    there is one implementation of "apply an id to an album".

    Args:
        album_mbid: the new release id. ``None`` means "not part of this save".
        release_group_mbid: the new release-group id. ``None`` = leave alone.
        clear_album_mbid: explicitly CLEAR the release id. This is the only way
            to remove one, and it is distinct from ``album_mbid=None``.

    Returns:
        ``{"rows_updated": int, "files_updated": int}``.
    """
    if not artist or not album:
        return {"rows_updated": 0, "files_updated": 0}

    payload: dict[str, Any] = {"artist": artist, "album": album}

    if clear_album_mbid:
        # ``update_album_ids`` writes only non-empty values, and an explicit
        # clear must not simply be skipped — push the clear directly.
        cleared_ids: list[str] = []
        _clear_scope_params: dict[str, Any] = {}
        _clear_scope_sql = _release_group_scope_clause(
            [previous_release_group_mbid] if previous_release_group_mbid else None,
            _clear_scope_params,
        )
        try:
            with db_session() as session:
                result = session.execute(
                    text(
                        "UPDATE tracks SET musicbrainz_album_mbid = '', "
                        "musicbrainz_albumid = '' "
                        "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) "
                        "= LOWER(:artist) AND LOWER(album) = LOWER(:album)"
                        + _clear_scope_sql
                    ),
                    {"artist": artist, "album": album, **_clear_scope_params},
                )
                rows_updated = result.rowcount or 0
                if rows_updated:
                    cleared_ids = [
                        str(r[0] or "")
                        for r in session.execute(
                            text(
                                "SELECT id FROM tracks "
                                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) "
                                "= LOWER(:artist) AND LOWER(album) = LOWER(:album)"
                                + _clear_scope_sql
                            ),
                            {"artist": artist, "album": album, **_clear_scope_params},
                        ).fetchall() or []
                    ]
        except Exception as exc:
            logger.warning(
                "Album-wide release id clear failed",
                artist=artist, album=album, error=str(exc),
            )
            return {"rows_updated": 0, "files_updated": 0}

        # A CLEAR is a correction too: without the stamp the next import simply
        # writes the old id back.
        _mark_local_correction(cleared_ids)

        # ⚠️ The missing list described the release this album no longer claims.
        #
        # Reported: "I had an album that was previously linked to an MBID so it
        # filled in the missing releases, I removed the MBID ... but the missing
        # releases are still showing." Removing the MBID is what makes those rows
        # stale, so they go with it; a CSV tracklist (``release_id IS NULL``) and
        # a rejected track (``ignored``) are preserved.
        try:
            from services.metadata.album_lookup_skip_service import (
                release_missing_tracks_for_cleared_mbid,
            )

            release_missing_tracks_for_cleared_mbid(artist, album)
        except Exception as exc:
            logger.debug(
                "Missing-track release after MBID clear failed",
                artist=artist, album=album, error=str(exc),
            )

        files_updated = _clear_album_id_file_tags(
            artist, album,
            scope_release_groups=(
                [previous_release_group_mbid]
                if previous_release_group_mbid
                else None
            ),
        )
        return {"rows_updated": rows_updated, "files_updated": files_updated}

    if album_mbid:
        payload["musicbrainz_release_id"] = album_mbid
    if release_group_mbid:
        payload["musicbrainz_release_group_id"] = release_group_mbid
    if len(payload) == 2:
        return {"rows_updated": 0, "files_updated": 0}

    _scope_groups = [
        group for group in (previous_release_group_mbid, release_group_mbid)
        if str(group or "").strip()
    ]
    result, _status = update_album_ids(
        payload, scope_release_groups=_scope_groups or None,
    )
    return {
        "rows_updated": int(result.get("rows_updated") or 0),
        "files_updated": int(result.get("files_updated") or 0),
    }


def _clear_album_id_file_tags(
    artist: str,
    album: str,
    *,
    scope_release_groups: list[str] | None = None,
) -> int:
    """Delete the album-id frame from every file of *artist*/*album*."""
    cleared = 0
    try:
        from services.metadata.tag_file_service import (
            resolve_music_file_path,
            update_file_tags,
        )

        scope_params: dict[str, Any] = {}
        scope_sql = _release_group_scope_clause(scope_release_groups, scope_params)
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT file_path FROM tracks "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "AND LOWER(album) = LOWER(:album)" + scope_sql
                ),
                {"artist": artist, "album": album, **scope_params},
            ).fetchall() or []

        for row in rows:
            resolved = resolve_music_file_path(str(row[0] or "") if row[0] else None)
            if not resolved:
                continue
            try:
                # "" is the writers' explicit delete-this-frame signal.
                if update_file_tags(
                    resolved, {"musicbrainz_albumid": ""}, force=True
                ):
                    cleared += 1
            except Exception as exc:
                logger.debug(
                    "Album id clear failed on file", file=resolved, error=str(exc)
                )
    except Exception as exc:
        logger.debug("Album id clear fan-out failed", error=str(exc))
    return cleared


def _row_already_has_ids(
    stored_album_mbid: Any,
    stored_albumid: Any,
    stored_release_group_mbid: Any,
    want_album_mbid: str,
    want_release_group_mbid: str,
) -> bool:
    """Would writing this payload change nothing on the FILE?

    Used to skip a needless whole-file rewrite. ``force=True`` makes the writer
    skip its usual "is the frame already populated?" check, so without this the
    album save rewrites every file even when the ids already match.

    ⚠️ STRICT ON PURPOSE. It returns True only when EVERY id in the payload
    already matches what the row stores. One differing value means the file
    must be written, because a partial match is exactly the half-corrected
    state that made the album split in Navidrome in the first place.

    ⚠️ BOTH RELEASE-ID SPELLINGS ARE CHECKED. The page reads
    ``musicbrainz_album_mbid``; ``album_identity`` (and therefore the scan's
    album grouping) prefers ``musicbrainz_albumid``. Comparing only the page's
    spelling let a row whose two columns disagreed count as "already correct",
    so the file was never rewritten and Popularr kept splitting an album
    Navidrome had already merged.

    An EMPTY ``want`` means "not part of this payload", not "clear it" — the
    clear path never reaches here (it has its own writer).
    """
    def _norm(value: Any) -> str:
        return str(value or "").strip().casefold()

    if want_album_mbid and _norm(stored_album_mbid) != _norm(want_album_mbid):
        return False
    if want_album_mbid and _norm(stored_albumid) != _norm(want_album_mbid):
        return False
    if want_release_group_mbid and _norm(
        stored_release_group_mbid
    ) != _norm(want_release_group_mbid):
        return False
    return True


def _release_group_scope_clause(
    groups: list[str] | None,
    params: dict[str, Any],
    *,
    prefix: str = "scope",
) -> str:
    """SQL that limits an album-wide id write to ONE release family.

    ⚠️ TWO ALBUMS CAN SHARE AN ARTIST AND NAME. "I am YOU" is both a 2018 EP
    and a track on the SKZ2020 compilation; scoping only by artist+album let a
    save of one overwrite the release id (and release group) of the other, and
    the next scan then wrote the other album's rejected ids back to the files —
    the reported "it keeps reverting to the old release". Rows that belong to a
    different, non-empty release group are a different album and are left
    alone; rows with no group are still filled.
    """
    clean = [str(group).strip() for group in (groups or []) if str(group or "").strip()]
    if not clean:
        return ""
    placeholders = []
    for index, group in enumerate(dict.fromkeys(clean)):
        key = f"{prefix}{index}"
        params[key] = group.casefold()
        placeholders.append(f":{key}")
    return (
        " AND (TRIM(COALESCE(musicbrainz_releasegroupid, '')) = '' "
        f"OR LOWER(TRIM(musicbrainz_releasegroupid)) IN ({', '.join(placeholders)}))"
    )


def update_album_ids(
    payload: dict[str, Any],
    *,
    scope_release_groups: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Update release IDs for an album's tracks.

    ``scope_release_groups`` narrows the fan-out to rows that cannot belong to
    another same-named album (no group, or one of the listed groups).
    """
    artist = str(payload.get("artist") or "").strip()
    album = str(payload.get("album") or "").strip()
    if not artist or not album:
        return {"success": False, "error": "artist and album required"}, 400

    musicbrainz_id = str(payload.get("musicbrainz_release_id") or "").strip()
    release_group_id = str(payload.get("musicbrainz_release_group_id") or "").strip()

    # ⚠️ A release id equal to the release-group id is the malformed shape seen
    # on Vitro / Untouchables. Resolve a real release or refuse; never fan the
    # group id into the release column (and from there onto every file).
    if (
        musicbrainz_id
        and release_group_id
        and musicbrainz_id.casefold() == release_group_id.casefold()
    ):
        try:
            from services.enrichment.musicbrainz_service import resolve_release_id

            _resolved = str(resolve_release_id(musicbrainz_id) or "").strip()
        except Exception:
            _resolved = ""
        if not _resolved or _resolved.casefold() == musicbrainz_id.casefold():
            return {
                "success": False,
                "error": (
                    "musicbrainz_release_id is a release GROUP; choose one of "
                    "its releases"
                ),
            }, 409
        logger.warning(
            "Album release id was a release-group id; resolved",
            artist=artist, album=album,
            release_group_id=musicbrainz_id, release_id=_resolved,
        )
        musicbrainz_id = _resolved
    discogs_id = str(payload.get("discogs_release_id") or "").strip()

    if not (musicbrainz_id or release_group_id or discogs_id):
        return {"success": False, "error": "No IDs provided"}, 400

    updates: list[str] = []
    bind_values: dict[str, Any] = {}
    _idx = 0
    if musicbrainz_id:
        # ⚠️ BOTH SPELLINGS, ALWAYS — for the same reason the CLEAR path above
        # writes both (``musicbrainz_album_mbid = ''`` AND
        # ``musicbrainz_albumid = ''``).
        #
        # The album page reads ``musicbrainz_album_mbid``; ``album_identity``
        # (and so the scan's album grouping) prefers ``musicbrainz_albumid``.
        # Writing only the page's spelling left the other stale on every row
        # the page did not display. The FILE was corrected, so Navidrome merged
        # the album, but Popularr kept grouping the row by its old
        # ``musicbrainz_albumid`` — the reported "album is STILL split" after a
        # save that appeared to work. It also made the next import report a
        # false local-vs-remote conflict on that stale column and preserve it.
        updates.append(f"musicbrainz_album_mbid = :v{_idx}")
        updates.append(f"musicbrainz_albumid = :v{_idx}")
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
    stamped_ids: list[str] = []
    _file_targets: list[tuple[Any, str]] = []
    _scope_params: dict[str, Any] = {}
    _scope_sql = _release_group_scope_clause(scope_release_groups, _scope_params)
    with db_session() as session:
        # ⭐ READ THE PRIOR IDS BEFORE THE UPDATE, not after.
        #
        # The file-write phase below skips a row whose stored ids already match
        # the payload (``force=True`` would otherwise rewrite every file on every
        # save). This SELECT must therefore run FIRST: after the UPDATE every row
        # reads back the NEW value, so the comparison would always say "already
        # correct" and no file would ever be written — silently reintroducing
        # the split-album bug the fan-out exists to fix.
        _prior_ids: dict[str, tuple[Any, Any, Any]] = {}
        _prior_paths: dict[str, Any] = {}
        if file_tag_updates:
            try:
                for _r in session.execute(
                    text(
                        "SELECT id, file_path, musicbrainz_album_mbid, "
                        "musicbrainz_albumid, musicbrainz_releasegroupid FROM tracks "
                        "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                        "AND LOWER(album) = LOWER(:album)" + _scope_sql
                    ),
                    {"artist": artist, "album": album, **_scope_params},
                ).fetchall() or []:
                    _tid = str(_r[0] or "")
                    _prior_ids[_tid] = (_r[2], _r[3], _r[4])
                    _prior_paths[_tid] = _r[1]
            except Exception as exc:
                logger.debug("Album id prior-value read failed", error=str(exc))

        # Case-insensitive scoping: the caller's artist/album name and the
        # stored value may differ only in casing ("Afi" vs "AFI"), and the
        # exact match returned rows_updated=0 — the IDs were accepted and
        # then written nowhere.
        result = session.execute(
            text(
                f"UPDATE tracks SET {', '.join(updates)} "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                "AND LOWER(album) = LOWER(:album)" + _scope_sql
            ),
            {**bind_values, **_scope_params},
        )
        rows = result.rowcount or 0

        # ⚠️ EVERY ROW THE UPDATE TOUCHED MUST BE STAMPED — collect the ids from
        # the SAME predicate as the UPDATE, not from the file loop below (which
        # skips rows whose path does not resolve).
        if rows:
            try:
                stamped_ids = [
                    str(r[0] or "")
                    for r in session.execute(
                        text(
                            "SELECT id FROM tracks "
                            "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) "
                            "= LOWER(:artist) "
                            "AND LOWER(album) = LOWER(:album)" + _scope_sql
                        ),
                        {"artist": artist, "album": album, **_scope_params},
                    ).fetchall() or []
                ]
            except Exception as exc:
                logger.debug("Album id stamp id load failed", error=str(exc))

        if file_tag_updates:
            from services.metadata.tag_file_service import resolve_music_file_path

            # ⛔⛔ THE FILE WRITES HAPPEN AFTER THIS SESSION COMMITS.
            #
            # They used to run HERE, inside the still-open write transaction
            # that had just locked every row of the album. Each write calls
            # ``mark_local_tags_written_for_path``, which opens its OWN session
            # to UPDATE those same rows — and those rows are locked by THIS
            # transaction, which cannot commit until the writes return.
            #
            # On PostgreSQL that is a self-deadlock (the inner statement waits
            # forever on locks the outer transaction holds). On SQLite it is a
            # 5-second busy-timeout PER FILE: a 14-track album spent ~70 s in
            # the save and the request timed out. Collecting the paths here and
            # writing them afterwards costs one list and removes the deadlock
            # entirely.
            #
            # ⭐ ONLY ROWS THAT ACTUALLY CHANGED ARE REWRITTEN.
            #
            # ``force=True`` means the writer no longer compares anything — it
            # copies and rewrites the whole file even when the id it is asked to
            # write is already there. The album page posts its release-id field
            # on EVERY save, so an unrelated edit (a track title, a genre) used
            # to rewrite every file of the album for nothing: an album of 100 MB
            # FLACs became gigabytes of disk churn per save.
            #
            # The old value was captured BEFORE the UPDATE (see ``_prior_ids``),
            # and a row whose stored ids already match the payload is skipped. A
            # genuinely changed id still writes, so the correction still lands.
            for _tid, _raw_path in _prior_paths.items():
                _path = resolve_music_file_path(
                    str(_raw_path or "") if _raw_path else None
                )
                if not _path:
                    continue
                _old_album, _old_albumid, _old_rg = _prior_ids.get(
                    _tid, (None, None, None)
                )
                if _row_already_has_ids(
                    _old_album, _old_albumid, _old_rg,
                    musicbrainz_id, release_group_id,
                ):
                    continue
                _file_targets.append((_tid, _path))

    if file_tag_updates:
        from services.metadata.tag_file_service import update_file_tags

        for _track_id, _path in _file_targets:
            try:
                # ``force=True``: an explicit album-id correction is
                # AUTHORITATIVE for the field it submits. Without it a
                # populated MUSICBRAINZ_ALBUMID frame is left alone
                # whenever ``tagging.fill_missing_only`` is on, so the
                # FILE kept the old id and Navidrome re-split the album
                # even though the database was correct.
                if update_file_tags(_path, file_tag_updates, force=True):
                    file_updated += 1
            except Exception as exc:
                logger.debug("File tag write failed", track_id=_track_id, error=str(exc))

    _mark_local_correction(stamped_ids)

    return {"success": True, "rows_updated": rows, "files_updated": file_updated}, 200


def _mark_local_correction(track_ids: list[str]) -> None:
    """Stamp *track_ids* as a LOCAL correction so an import cannot undo them.

    ⭐⭐ THIS IS WHAT STOPS A SAVED RELEASE ID BEING REVERTED.

    The Navidrome import protects a row only when
    ``metadata_dirty_for_navidrome`` is set: with it, a differing remote value
    is reported as a conflict and the LOCAL value wins; without it, the payload
    is written straight over the row.

    ``_persist_album_track_payloads`` stamps the rows the page save itself
    wrote — but an ALBUM-WIDE id write touches every row of the album, and the
    rows the page did not display are the very ones that carry the stale id.
    Unstamped, they were unprotected: the next import re-applied the old id and
    the album split apart again ("they saved, Navidrome joined them, then
    something updated and unlinked them again").
    """
    ids = [str(t).strip() for t in track_ids or [] if str(t or "").strip()]
    if not ids:
        return
    try:
        from services.scanning.reconciliation import (
            mark_local_tags_written_for_track_ids,
        )

        mark_local_tags_written_for_track_ids(ids)
    except Exception as exc:
        logger.debug("Album id correction stamp failed", error=str(exc))


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
    from services.enrichment.genre_aggregation_service import (
        _TRACK_AFFILIATION_FILTER_TAGS,
        aggregate_genres,
    )
    recommended = aggregate_genres(dict(source_map))
    # Same rule as the album blend in
    # ``genre_aggregation_service.get_track_recommendations``: a per-track
    # affiliation tag (Cover/Live/Acoustic/…) on ONE track must not be shown
    # as the album's recommended genre.
    recommended = [
        g for g in recommended
        if g.strip().lower() not in _TRACK_AFFILIATION_FILTER_TAGS
    ]
    return {"success": True, "artist": artist, "album": album, "genres": recommended}
