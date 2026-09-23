"""Metadata repository.

SQL-only helper functions for services.metadata.
All functions now use SQLAlchemy ``db_session()`` internally.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from sqlalchemy import text
from db.engine import db_session

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Album favourites / art / tracklists
# -----------------------------------------------------------------------------

def album_is_favourite(conn: Any = None, artist: str = "", album: str = "") -> bool:
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT id FROM bookmarks
                WHERE type = :t
                  AND LOWER(artist) = LOWER(:artist)
                  AND LOWER(album) = LOWER(:album)
                LIMIT 1
            """),
            {"t": "album_favourite", "artist": artist, "album": album},
        )
        return result.fetchone() is not None


def set_album_favourite_db(conn: Any = None, artist: str = "", album: str = "", is_favourite: bool = False) -> None:
    with db_session() as session:
        if is_favourite:
            session.execute(
                text("""
                    INSERT INTO bookmarks (type, artist, album)
                    VALUES (:t, :artist, :album)
                    ON CONFLICT DO NOTHING
                """),
                {"t": "album_favourite", "artist": artist, "album": album},
            )
        else:
            session.execute(
                text("""
                    DELETE FROM bookmarks
                    WHERE type = :t
                      AND LOWER(artist) = LOWER(:artist)
                      AND LOWER(album) = LOWER(:album)
                """),
                {"t": "album_favourite", "artist": artist, "album": album},
            )


def fetch_album_art_blob(conn: Any = None, artist: str = "", album: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT image_data, image_mime_type
            FROM album_art
            WHERE LOWER(COALESCE(artist_name, '')) = LOWER(:artist)
              AND LOWER(COALESCE(album_name, '')) = LOWER(:album)
            LIMIT 1
        """), {"artist": artist, "album": album})

        row = result.fetchone()
        if not row:
            return None, None
        return (row[0], row[1] or "image/jpeg")


def fetch_album_art_record(conn: Any = None, artist: str = "", album: str = ""):
    """``(image_data, mime, source)`` for one album's stored art.

    Same read as ``fetch_album_art_blob`` plus the SOURCE, which the caller needs
    to decide whether the stored picture may be replaced by Navidrome's own
    copy: art that came from an online provider is upgradable, art the user
    uploaded or pointed at a URL is not. Returned as one tuple so the existing
    two-value callers of ``fetch_album_art_blob`` are untouched.
    """
    with db_session() as session:
        result = session.execute(text("""
            SELECT image_data, image_mime_type, source
            FROM album_art
            WHERE LOWER(COALESCE(artist_name, '')) = LOWER(:artist)
              AND LOWER(COALESCE(album_name, '')) = LOWER(:album)
            LIMIT 1
        """), {"artist": artist, "album": album})

        row = result.fetchone()
        if not row:
            return None, None, ""
        return (row[0], row[1] or "image/jpeg", str(row[2] or ""))


def save_album_art_db(conn: Any = None, artist: str = "", album: str = "",
                      image_data: bytes | None = None, mime: str = "", source: str = "") -> None:
    with db_session() as session:
        session.execute(text("""
            INSERT INTO album_art (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
            VALUES (:artist, :album, :data, :mime, :source, CURRENT_TIMESTAMP)
            ON CONFLICT (artist_name, album_name)
            DO UPDATE SET
                image_data = EXCLUDED.image_data,
                image_mime_type = EXCLUDED.image_mime_type,
                source = EXCLUDED.source,
                downloaded_at = CURRENT_TIMESTAMP
        """), {"artist": artist, "album": album, "data": image_data, "mime": mime, "source": source})


def fetch_album_tracklist(conn: Any = None, artist: str = "", album: str = ""):
    """Every track of one album, for the artist page's expandable tracklist.

    The comparison is CASE-INSENSITIVE on both keys, and it has to be: the
    caller (``routes/ui_routes.py::_build_artist_detail_payload``) selected the
    album in the first place with

        WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)

    so the album is guaranteed to be this artist's only up to case. Feeding the
    URL's spelling of the artist back in as an EXACT match returns nothing
    whenever the two differ (e.g. ``/artist/dArtagnan`` against a stored
    ``dArtagnan`` vs ``DARTAGNAN``), and the artist page then reports
    "Tracks not found" for an album it is simultaneously listing as owned.

    ``LOWER(COALESCE(album, ''))`` mirrors the album-detail page's own lookup,
    so the two surfaces agree on what "the same album" means.
    """
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, title, track_number, duration, artist
            FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
              AND LOWER(COALESCE(album, '')) = LOWER(:album)
            ORDER BY COALESCE(disc_number, '1'),
                     COALESCE(track_number, '999'),
                     title
        """), {"artist": artist, "album": album})
        return result.fetchall() or []


def fetch_album_queue_track_stubs(conn: Any = None, artist: str = "", album: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, file_path
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
              AND file_path LIKE '__queued_for_download__%%'
        """), {"artist": artist, "album": album})
        return result.fetchall() or []


def fetch_queue_status(conn: Any = None, queue_id: int = 0) -> str:
    with db_session() as session:
        result = session.execute(
            text("SELECT status FROM download_queue WHERE id = :id"),
            {"id": queue_id},
        )
        row = result.fetchone()
        return str(row[0]) if row else "queued"


def fetch_album_tracks_for_tag_update(conn: Any = None, artist: str = "", album: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, title, file_path
            FROM tracks
            WHERE artist = :artist AND album = :album
        """), {"artist": artist, "album": album})
        return result.fetchall() or []


def update_track_genres(conn: Any = None, track_id: Any = None, genres_str: str = "") -> int:
    """Set a track's genres.

    ⚠️ ``genres`` is TEXT but ``manual_genres`` is **JSONB**, and both are fed
    the same comma-separated string here. A raw UPDATE bypasses ``save_to_db``
    (and therefore its JSON coercion entirely), so the JSONB column must be
    serialised at THIS call site or PostgreSQL rejects the statement with
    ``invalid input syntax for type json``.
    """
    from db.repositories.popularity_repository import coerce_json_value

    with db_session() as session:
        result = session.execute(text("""
            UPDATE tracks
            SET genres = :genres,
                manual_genres = :genres_json
            WHERE id = :id
        """), {
            "genres": genres_str,
            "genres_json": coerce_json_value(genres_str),
            "id": track_id,
        })
        return result.rowcount or 0


def update_album_mbid_fields(conn: Any = None, artist: str = "", album: str = "",
                              mbid: str | None = None, rg_mbid: str | None = None,
                              cover_url: str | None = None) -> int:
    with db_session() as session:
        updates = []
        params: dict[str, Any] = {"artist": artist, "album": album}

        if mbid:
            updates.append("musicbrainz_album_mbid = :mbid")
            params["mbid"] = mbid

        if rg_mbid:
            updates.append("musicbrainz_releasegroupid = :rg_mbid")
            params["rg_mbid"] = rg_mbid

        if cover_url:
            updates.append("cover_art_url = :cover_url")
            params["cover_url"] = cover_url

        if not updates:
            return 0

        result = session.execute(text(f"""
            UPDATE tracks
            SET {', '.join(updates)}
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
        """), params)
        return result.rowcount or 0


def update_album_discogs_fields(conn: Any = None, artist: str = "", album: str = "",
                                 discogs_id: str = "", is_single: bool = False) -> int:
    with db_session() as session:
        if is_single:
            result = session.execute(text("""
                UPDATE tracks
                SET discogs_album_id = :did,
                    is_single = TRUE,
                    single_confidence = 'high',
                    stars = 5
                WHERE artist = :artist AND album = :album
            """), {"did": discogs_id, "artist": artist, "album": album})
        else:
            result = session.execute(text("""
                UPDATE tracks
                SET discogs_album_id = :did
                WHERE artist = :artist AND album = :album
            """), {"did": discogs_id, "artist": artist, "album": album})
        return result.rowcount or 0


def ignore_missing_track_db(
    conn: Any = None,
    missing_id: str | None = None,
    artist: str = "",
    album: str = "",
    title: str = "",
    disc_number: int | None = None
) -> int:
    with db_session() as session:
        if missing_id:
            result = session.execute(
                text("UPDATE missing_album_tracks SET ignored = TRUE WHERE id = :id"),
                {"id": missing_id},
            )
        else:
            result = session.execute(text("""
                UPDATE missing_album_tracks
                SET ignored = TRUE
                WHERE artist_name = :artist
                  AND album_name = :album
                  AND title = :title
                  AND disc_number = :disc
            """), {"artist": artist, "album": album, "title": title, "disc": disc_number})

        return result.rowcount or 0


# -----------------------------------------------------------------------------
# Provider-specific album genres
# -----------------------------------------------------------------------------

def get_album_provider_genres(conn: Any = None, artist: str = "", album: str = "") -> dict[str, list[str]]:
    """Retrieve distinct provider genres for an album from musicbrainz_releases."""
    result = {"lastfm": [], "discogs": [], "musicbrainz": []}
    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT lastfm_genres, discogs_genres, musicbrainz_genres 
                    FROM musicbrainz_releases
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND LOWER(release_title) = LOWER(:album)
                    LIMIT 1
                """),
                {"artist": artist, "album": album},
            ).first()
            if row:
                if row[0]:
                    result["lastfm"] = [g.strip() for g in str(row[0]).split(",") if g.strip()]
                if row[1]:
                    result["discogs"] = [g.strip() for g in str(row[1]).split(",") if g.strip()]
                if row[2]:
                    result["musicbrainz"] = [g.strip() for g in str(row[2]).split(",") if g.strip()]
    except Exception as exc:
        logger.error("Failed to fetch provider genres for album %s - %s: %s", artist, album, exc)
    return result


def update_album_provider_genres(
    conn: Any = None,
    artist: str = "",
    album: str = "",
    lastfm_genres: list[str] | None = None,
    discogs_genres: list[str] | None = None,
    mb_genres: list[str] | None = None,
) -> bool:
    """Update individual provider genre columns for an album in musicbrainz_releases."""
    updates = []
    params: dict[str, Any] = {"artist": artist, "album": album}
    
    if lastfm_genres is not None:
        updates.append("lastfm_genres = :lf")
        params["lf"] = ", ".join(str(g).strip() for g in lastfm_genres if g)
    if discogs_genres is not None:
        updates.append("discogs_genres = :disc")
        params["disc"] = ", ".join(str(g).strip() for g in discogs_genres if g)
    if mb_genres is not None:
        updates.append("musicbrainz_genres = :mb")
        params["mb"] = ", ".join(str(g).strip() for g in mb_genres if g)
        
    if not updates:
        return False
        
    try:
        with db_session() as session:
            set_clause = ", ".join(updates)
            session.execute(
                text(f"""
                    UPDATE musicbrainz_releases
                    SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND LOWER(release_title) = LOWER(:album)
                """),
                params,
            )
            return True
    except Exception as exc:
        logger.error("Failed to update provider genres for album %s - %s: %s", artist, album, exc)
        return False


# -----------------------------------------------------------------------------
# Artist corrections / metadata
# -----------------------------------------------------------------------------

def find_track_row(conn: Any = None, track_id: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, file_path, artist, album, title
            FROM tracks
            WHERE CAST(id AS TEXT) = :id
            LIMIT 1
        """), {"id": track_id})
        return result.fetchone()


def delete_track_row(conn: Any = None, track_id: str = "") -> int:
    """Delete a track row by ID."""
    sql = text("DELETE FROM tracks WHERE CAST(id AS TEXT) = :id")
    params = {"id": track_id}
    with db_session() as session:
        result = session.execute(sql, params)
        return result.rowcount or 0


def fetch_track_for_delete(conn: Any = None, track_id: str = "") -> tuple | None:
    """Fetch track data by ID for deletion processing."""
    sql = text("""
        SELECT id, file_path, artist, album, title
        FROM tracks
        WHERE CAST(id AS TEXT) = :id
        LIMIT 1
    """)
    params = {"id": track_id}
    with db_session() as session:
        result = session.execute(sql, params)
        return result.fetchone()


def merge_album_names(conn: Any = None, artist: str = "", source_albums: list[str] | None = None, canonical_name: str = "") -> int:
    if not source_albums:
        return 0

    _file_rows: list[tuple[str, str, str]] = []
    try:
        from services.metadata.tag_file_service import (
            resolve_music_file_path,
            update_file_tags,
        )
        with db_session() as session:
            _rows = session.execute(
                text(f"""
                    SELECT file_path, artist, title FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album IN ({', '.join([f':s{i}' for i in range(len(source_albums))])})
                """),
                {"artist": artist, **{f"s{i}": s for i, s in enumerate(source_albums)}},
            ).fetchall() or []
            for row in _rows:
                mapping = getattr(row, "_mapping", None)
                fp = str((mapping.get("file_path") if mapping else row[0]) or "").strip()
                title = str((mapping.get("title") if mapping else row[2]) or "").strip()
                _file_rows.append((fp, title, canonical_name))
    except Exception as exc:
        logger.debug("Merge-album file rows load failed", artist=artist, error=str(exc))

    with db_session() as session:
        placeholders = ", ".join([f":s{i}" for i in range(len(source_albums))])
        params: dict[str, Any] = {"canonical": canonical_name, "artist": artist}
        params.update({f"s{i}": s for i, s in enumerate(source_albums)})
        result = session.execute(text(f"""
            UPDATE tracks
            SET album = :canonical
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album IN ({placeholders})
        """), params)
        rows_updated = result.rowcount or 0

    for fp, title, album_name in _file_rows:
        try:
            resolved = resolve_music_file_path(fp) or fp
            if resolved and os.path.exists(resolved):
                update_file_tags(resolved, {"album": album_name})
        except Exception as exc:
            logger.debug("Merge-album file tag write failed", file=fp, error=str(exc))

    return rows_updated


def count_album_disc_numbers(conn: Any = None, artist: str = "", album: str = "") -> int:
    with db_session() as session:
        result = session.execute(text("""
            SELECT COUNT(DISTINCT disc_number)
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
        """), {"artist": artist, "album": album})
        return int(result.scalar() or 0)


def clear_album_disc_numbers(conn: Any = None, artist: str = "", album: str = "") -> int:
    _file_rows: list[str] = []
    try:
        from services.metadata.tag_file_service import (
            resolve_music_file_path,
            update_file_tags,
        )
        with db_session() as session:
            result = session.execute(text("""
                SELECT file_path FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                  AND album = :album
                  AND disc_number IS NOT NULL
                  AND TRIM(CAST(disc_number AS TEXT)) != ''
            """), {"artist": artist, "album": album})
            _file_rows = [str(r[0] or "").strip() for r in result.fetchall() or [] if r[0]]
    except Exception as exc:
        logger.debug("Clear-disc file rows load failed", artist=artist, album=album, error=str(exc))

    with db_session() as session:
        result = session.execute(text("""
            UPDATE tracks
            SET disc_number = NULL
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
        """), {"artist": artist, "album": album})
        rows_updated = result.rowcount or 0

    for fp in _file_rows:
        try:
            resolved = resolve_music_file_path(fp) or fp
            if resolved and os.path.exists(resolved):
                update_file_tags(resolved, {"disc_number": ""})
        except Exception as exc:
            logger.debug("Clear-disc file tag write failed", file=fp, error=str(exc))

    return rows_updated


def artist_track_count(conn: Any = None, artist: str = "") -> int:
    with db_session() as session:
        result = session.execute(text("""
            SELECT COUNT(*)
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
        """), {"artist": artist})
        return int(result.scalar() or 0)


def fetch_cached_missing_releases(conn: Any = None, artist: str = ""):
    with db_session() as session:
        result = session.execute(
            text("SELECT title, release_id FROM missing_releases WHERE artist = :artist"),
            {"artist": artist},
        )
        return result.fetchall() or []


# -----------------------------------------------------------------------------
# Artist scan support
# -----------------------------------------------------------------------------

def fetch_artist_albums(conn: Any = None, artist: str = "") -> list[str]:
    with db_session() as session:
        result = session.execute(text("""
            SELECT DISTINCT album
            FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
        """), {"artist": artist})
        return [str(r[0]) for r in result.fetchall()]


def fetch_artist_mbid(conn: Any = None, artist: str = "") -> str | None:
    with db_session() as session:
        result = session.execute(text("""
            SELECT COALESCE(
                NULLIF(TRIM(musicbrainz_albumartistid), ''),
                NULLIF(TRIM(musicbrainz_artistid), '')
            )
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
            LIMIT 1
        """), {"artist": artist})
        row = result.fetchone()
        return str(row[0]) if row and str(row[0] or "").strip() else None


def fetch_all_distinct_artists(conn: Any = None) -> list[str]:
    with db_session() as session:
        result = session.execute(text("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL AND TRIM(artist) != ''"))
        return [str(r[0]) for r in result.fetchall()]
