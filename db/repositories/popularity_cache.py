"""Track popularity cache repository.

Stores bulk-fetched Last.fm / ListenBrainz popularity for tracks keyed by
``(artist, title)``.  Non-forced scans read from this table instead of making
per-track API calls, which avoids rate limits and inconsistent data.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)


def get_cached_track_popularity(artist: str, title: str) -> dict[str, Any] | None:
    """Return a single cached popularity row, or None."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist, title, lastfm_listeners, lastfm_playcount,
                           listenbrainz_listens, listenbrainz_users, lastfm_tags, source, updated_at
                    FROM track_popularity_cache
                    WHERE LOWER(artist) = LOWER(:artist) AND LOWER(title) = LOWER(:title)
                """),
                {"artist": artist, "title": title},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.error("Failed to get cached track popularity", artist=artist, title=title, error=str(exc))
        return None


def get_cached_popularity_for_titles(
    artist: str,
    titles: list[str],
) -> dict[str, dict[str, Any]]:
    """Return cached popularity for all given titles of one artist.

    Rows are returned for the whole artist (not just the exact requested
    titles) so feat variants of a song — e.g. a local ``Herzblut`` and a
    cached ``Herzblut (feat. Melissa Bonny)`` — are both fetched; callers
    re-key by their NORMALISED title, which collapses such variants onto
    one entry.  Returns a dict keyed by lowercased title.
    """
    if not titles:
        return {}
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist, title, lastfm_listeners, lastfm_playcount,
                           listenbrainz_listens, listenbrainz_users, lastfm_tags, source, updated_at,
                           recording_mbid
                    FROM track_popularity_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                    ORDER BY lastfm_listeners DESC
                    LIMIT 500
                """),
                {"artist": artist},
            )
            rows = result.fetchall() or []
            out: dict[str, dict[str, Any]] = {}
            for row in rows:
                # SQLAlchemy 2.0 Row objects do NOT support string indexing
                title = row._mapping["title"]
                if title:
                    out[str(title).lower()] = dict(row._mapping)
            return out
    except Exception as exc:
        logger.error("Failed to bulk get cached popularity", artist=artist, error=str(exc))
        return {}


def upsert_track_popularity_bulk(rows: list[dict[str, Any]]) -> int:
    """Upsert popularity rows for one artist.

    Each row: ``artist``, ``title``, optional ``lastfm_listeners``,
    ``lastfm_playcount``, ``listenbrainz_listens``, ``listenbrainz_users``,
    ``source``, ``recording_mbid``.  Returns the number of rows written.

    ``recording_mbid`` is NEVER clobbered back to NULL by a row that does not
    carry one: the album-tracklist identity is the ONLY writer that knows it,
    and the artist-prefetch writer (listeners only) runs far more often.
    """
    if not rows:
        return 0
    try:
        with db_session() as session:
            for row in rows:
                session.execute(
                    text("""
                        INSERT INTO track_popularity_cache
                            (artist, title, lastfm_listeners, lastfm_playcount,
                             listenbrainz_listens, listenbrainz_users, lastfm_tags, source, updated_at, recording_mbid)
                        VALUES
                            (:artist, :title, :lastfm_listeners, :lastfm_playcount,
                             :listenbrainz_listens, :listenbrainz_users, :lastfm_tags, :source, CURRENT_TIMESTAMP, :recording_mbid)
                        ON CONFLICT (artist, title) DO UPDATE SET
                            lastfm_listeners = EXCLUDED.lastfm_listeners,
                            lastfm_playcount = EXCLUDED.lastfm_playcount,
                            listenbrainz_listens = EXCLUDED.listenbrainz_listens,
                            listenbrainz_users = EXCLUDED.listenbrainz_users,
                            lastfm_tags = COALESCE(EXCLUDED.lastfm_tags, track_popularity_cache.lastfm_tags),
                            source = EXCLUDED.source,
                            recording_mbid = COALESCE(EXCLUDED.recording_mbid, track_popularity_cache.recording_mbid),
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    {
                        "artist": row["artist"],
                        "title": row["title"],
                        "lastfm_listeners": int(row.get("lastfm_listeners") or 0),
                        "lastfm_playcount": int(row.get("lastfm_playcount") or 0),
                        "listenbrainz_listens": int(row.get("listenbrainz_listens") or 0),
                        "listenbrainz_users": int(row.get("listenbrainz_users") or 0),
                        "lastfm_tags": row.get("lastfm_tags") or None,
                        "source": row.get("source") or "bulk",
                        "recording_mbid": str(row.get("recording_mbid") or "").strip() or None,
                    },
                )
            return len(rows)
    except Exception as exc:
        logger.error("Failed to bulk upsert track popularity", artist=rows[0].get("artist"), error=str(exc))
        return 0
