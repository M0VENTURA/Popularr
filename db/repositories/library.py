"""Library statistics and artist listing queries."""


from __future__ import annotations
from typing import Any, List, Dict
from sqlalchemy import text
from db.engine import db_session
import logging 

import structlog

logger = structlog.get_logger(__name__)

def fetch_artist_library_stats(conn: Any = None) -> tuple | None:
    """Fetch total library album/track/five-star counts."""
    with db_session() as session:
        result = session.execute(text("""
            SELECT
                COUNT(DISTINCT album) AS album_count,
                COUNT(*) AS track_count,
                COALESCE(SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count
            FROM tracks
        """))
        return result.fetchone()


def fetch_all_artists_with_stats(conn: Any = None, has_album_artist: bool = False) -> list[Any]:
    """Fetch artists with album, track, five-star, and last-updated counts."""
    with db_session() as session:
        if has_album_artist:
            query = """
                WITH normalized_tracks AS (
                    SELECT
                        COALESCE(
                            NULLIF(TRIM(musicbrainz_album_mbid), ''),
                            LOWER(TRIM(album)) || '|' || COALESCE(NULLIF(TRIM(CAST(year AS TEXT)), ''), '')
                        ) AS album_key,
                        album,
                        CASE
                            WHEN album_artist IS NOT NULL AND TRIM(album_artist) != '' THEN TRIM(album_artist)
                            WHEN artist IS NOT NULL AND TRIM(artist) != '' THEN TRIM(artist)
                            ELSE NULL
                        END AS candidate_album_artist,
                        last_scanned AS artist_last_updated,
                        stars,
                        CASE
                            WHEN LOWER(TRIM(COALESCE(album_artist, ''))) IN (
                                'various artists', 'various', 'v/a', 'va',
                                'compilation', 'soundtrack', 'original soundtrack'
                            ) THEN 1 ELSE 0
                        END AS is_compilation_artist
                    FROM tracks
                    WHERE album IS NOT NULL AND TRIM(album) != ''
                ),
                ranked_album_artists AS (
                    SELECT
                        album_key,
                        candidate_album_artist,
                        COUNT(*) AS matched_track_count,
                        MAX(artist_last_updated) AS artist_last_updated,
                        MAX(is_compilation_artist) AS is_compilation_artist,
                        ROW_NUMBER() OVER (
                            PARTITION BY album_key
                            ORDER BY MAX(is_compilation_artist) DESC, COUNT(*) DESC, candidate_album_artist ASC
                        ) AS artist_rank
                    FROM normalized_tracks
                    WHERE candidate_album_artist IS NOT NULL AND candidate_album_artist != ''
                    GROUP BY album_key, candidate_album_artist
                ),
                canonical_albums AS (
                    SELECT album_key, candidate_album_artist AS canonical_album_artist, artist_last_updated
                    FROM ranked_album_artists
                    WHERE artist_rank = 1
                ),
                best_artist_name AS (
                    SELECT DISTINCT ON (LOWER(canonical_album_artist))
                        canonical_album_artist AS display_name
                    FROM canonical_albums
                    ORDER BY LOWER(canonical_album_artist)
                )
                SELECT
                    ban.display_name AS display_name,
                    COUNT(DISTINCT ca.album_key) AS album_count,
                    COUNT(nt.album_key) AS track_count,
                    COALESCE(SUM(CASE WHEN nt.stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count,
                    MAX(ca.artist_last_updated) AS last_updated
                FROM canonical_albums ca
                JOIN normalized_tracks nt ON nt.album_key = ca.album_key
                JOIN best_artist_name ban ON LOWER(ca.canonical_album_artist) = LOWER(ban.display_name)
                GROUP BY ban.display_name
                HAVING COUNT(DISTINCT ca.album_key) > 0
                ORDER BY display_name
            """
        else:
            query = """
                SELECT
                    artist AS display_name,
                    COUNT(DISTINCT album) AS album_count,
                    COUNT(*) AS track_count,
                    COALESCE(SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count,
                    MAX(last_scanned) AS last_updated
                FROM tracks
                WHERE artist IS NOT NULL AND artist != ''
                GROUP BY artist
                HAVING COUNT(DISTINCT album) > 0
                ORDER BY display_name
            """
        result = session.execute(text(query))
        return result.fetchall() or []


def fetch_genre_mood_analytics(conn: Any = None, top_n: int = 50):
    """Fetch top genres, moods, and genre+mood combinations."""
    with db_session() as session:
        genres = session.execute(text("""
            SELECT genre, COUNT(*) AS count
            FROM tracks
            WHERE genre IS NOT NULL AND TRIM(genre) != ''
            GROUP BY genre
            ORDER BY count DESC
            LIMIT :limit
        """), {"limit": top_n}).fetchall() or []

        moods = session.execute(text("""
            SELECT mood, COUNT(*) AS count
            FROM tracks
            WHERE mood IS NOT NULL AND TRIM(mood) != ''
            GROUP BY mood
            ORDER BY count DESC
            LIMIT :limit
        """), {"limit": top_n}).fetchall() or []

        combos = session.execute(text("""
            SELECT genre, mood, COUNT(*) AS count
            FROM tracks
            WHERE genre IS NOT NULL AND mood IS NOT NULL
              AND TRIM(genre) != '' AND TRIM(mood) != ''
            GROUP BY genre, mood
            ORDER BY count DESC
            LIMIT :limit
        """), {"limit": top_n}).fetchall() or []

        return genres, moods, combos


def get_tracks_for_album(artist: str, album: str) -> List[Dict[str, Any]]:
    """Return all tracks for a given artist/album."""
    with db_session() as session:
        result = session.execute(text("""
            SELECT *
            FROM tracks
            WHERE TRIM(COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist))) = :artist
              AND album = :album
            ORDER BY disc_number NULLS FIRST, track_number NULLS FIRST
        """), {"artist": artist, "album": album})
        return [dict(r._mapping) for r in result.fetchall() or []]

def get_all_artists() -> list[str]:
    """Return all distinct artists based on album_artist fallback logic."""
    with db_session() as session:
        result = session.execute(text("""
            SELECT DISTINCT
                COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) AS artist_name
            FROM tracks
            WHERE COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) IS NOT NULL
              AND COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) != ''
            ORDER BY artist_name
        """))
        return [str(row[0]) for row in result.fetchall() or [] if row[0]]

def get_albums_for_artist(artist: str) -> list[str]:
    """Return all albums for a given artist using album_artist fallback logic."""
    with db_session() as session:
        result = session.execute(text("""
            SELECT DISTINCT album
            FROM tracks
            WHERE COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) = :artist
              AND album IS NOT NULL
              AND TRIM(album) != ''
            ORDER BY album
        """), {"artist": artist})
        return [str(row[0]) for row in result.fetchall() or [] if row[0]]


def get_album_counts_by_artist() -> dict[str, int]:
    """Distinct album count per artist, in ONE query.

    Exists so a caller sizing work PER ARTIST does not have to query per
    artist: a full library scan needs the count for every artist it is about
    to process, and a per-artist loop would be hundreds of round-trips.

    Grouping matches :func:`get_all_artists` exactly
    (``COALESCE(NULLIF(TRIM(album_artist),''), TRIM(artist))``), so the keys
    line up with the names the scan iterates.  An artist whose name differs
    only by surrounding whitespace or case WILL still match after
    ``.strip()``, which is what the callers rely on.

    Returns ``{}`` on failure rather than raising: a scan must not die because
    a sizing hint could not be read.  Callers must treat a missing artist as
    "count unknown" and fall back to their un-scaled behaviour.
    """
    try:
        with db_session() as session:
            rows = session.execute(text("""
                SELECT COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) AS artist_name,
                       COUNT(DISTINCT album) AS album_count
                FROM tracks
                WHERE COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) IS NOT NULL
                  AND COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) != ''
                  AND album IS NOT NULL
                  AND TRIM(album) != ''
                GROUP BY 1
            """)).fetchall()
    except Exception as exc:
        logger.warning("Album counts per artist read failed", error=str(exc))
        return {}
    return {
        str(row[0]): int(row[1] or 0)
        for row in rows or []
        if row and row[0]
    }




def upsert_musicbrainz_release(
    release_id,
    release_title,
    artist,
    release_year,
    total_tracks,
    monitoring_folder_path,
    method="slskd",
    album_artist=None,
    release_source=None,
):
    """Create or update release entry in database."""

    try:
        with db_session() as session:

            result = session.execute(text("""
                SELECT id FROM musicbrainz_releases
                WHERE release_id = :rid
            """), {"rid": release_id})
            existing = result.fetchone()

            if existing:
                release_db_id = existing[0]

                session.execute(text("""
                    UPDATE musicbrainz_releases
                    SET status = 'active',
                        album_artist = COALESCE(NULLIF(:aa, ''), album_artist),
                        release_source = COALESCE(NULLIF(:rs, ''), release_source),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                """), {
                    "aa": album_artist,
                    "rs": release_source or "musicbrainz",
                    "id": release_db_id,
                })

                logging.info(f"[RELEASE_ENTRY] Updated {release_db_id}")

            else:
                result = session.execute(text("""
                    INSERT INTO musicbrainz_releases
                    (release_id, release_title, artist, release_year,
                     total_tracks, monitoring_folder_path,
                     status, method, album_artist, release_source,
                     created_at, updated_at)
                    VALUES (:rid, :rtitle, :artist, :ryear,
                            :ttracks, :mfp,
                            'active', :method, :aa, :rs,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING id
                """), {
                    "rid": release_id,
                    "rtitle": release_title,
                    "artist": artist,
                    "ryear": release_year,
                    "ttracks": total_tracks,
                    "mfp": str(monitoring_folder_path),
                    "method": method,
                    "aa": album_artist,
                    "rs": release_source or "musicbrainz",
                })

                release_db_id = result.scalar()
                logging.info(f"[RELEASE_ENTRY] Created {release_db_id}")

        return release_db_id

    except Exception as e:
        logging.error(f"[RELEASE_ENTRY] Error: {e}")
        raise
