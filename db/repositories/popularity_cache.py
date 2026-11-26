"""Track popularity cache repository.

Stores bulk-fetched Last.fm / ListenBrainz popularity for tracks keyed by
``(artist, title)``.  Non-forced scans read from this table instead of making
per-track API calls, which avoids rate limits and inconsistent data.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import bindparam, text

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

    ⚠️ ``LIMIT 500`` IS A CAP ON THE ARTIST-WIDE SWEEP, NOT ON THE ANSWER.
    The whole-artist query is capped so a huge artist cannot return unbounded
    rows, but the cap is ordered by listener count — so for an artist with more
    than 500 cached titles the DEEP CUTS were silently dropped, and every scan
    re-fetched them from Last.fm/ListenBrainz because the cache never appeared to
    have them. "Various Artists" always exceeds it, since every compilation tile
    shares that artist key.

    The requested titles are therefore fetched SEPARATELY and cannot be truncated
    — they are exactly what the caller asked for. The two sets are merged, with
    the exact-title rows winning.
    """
    if not titles:
        return {}
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist, title, lastfm_listeners, lastfm_playcount,
                           listenbrainz_listens, listenbrainz_users, lastfm_tags, source, updated_at,
                           recording_mbid, release_track_title
                    FROM track_popularity_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                    ORDER BY lastfm_listeners DESC
                    LIMIT 500
                """),
                {"artist": artist},
            )
            rows = result.fetchall() or []

            # The rows the caller actually needs, regardless of the sweep cap.
            # Bounded by the caller's own list, so this cannot blow up.
            wanted = sorted({str(t).strip() for t in titles if str(t).strip()})
            if wanted:
                try:
                    exact = session.execute(
                        text("""
                            SELECT artist, title, lastfm_listeners, lastfm_playcount,
                                   listenbrainz_listens, listenbrainz_users, lastfm_tags, source, updated_at,
                                   recording_mbid, release_track_title
                            FROM track_popularity_cache
                            WHERE LOWER(artist) = LOWER(:artist)
                              AND LOWER(title) IN :wanted
                        """).bindparams(
                            bindparam("wanted", expanding=True)
                        ),
                        {"artist": artist, "wanted": [w.lower() for w in wanted]},
                    )
                    rows = list(exact.fetchall() or []) + list(rows)
                except Exception:
                    # A failed top-up must not lose the artist-wide sweep.
                    pass

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
    ``source``, ``recording_mbid``, optional ``release_track_title``.
    Returns the number of rows written.

    ``recording_mbid`` is NEVER clobbered back to NULL by a row that does not
    carry one: the album-tracklist identity is the ONLY writer that knows it,
    and the artist-prefetch writer (listeners only) runs far more often. The
    same applies to ``release_track_title`` (it describes the identity, so it
    is only ever written by the same writer).
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
                             listenbrainz_listens, listenbrainz_users, lastfm_tags, source, updated_at, recording_mbid,
                             release_track_title)
                        VALUES
                            (:artist, :title, :lastfm_listeners, :lastfm_playcount,
                             :listenbrainz_listens, :listenbrainz_users, :lastfm_tags, :source, CURRENT_TIMESTAMP, :recording_mbid,
                             :release_track_title)
                        ON CONFLICT (artist, title) DO UPDATE SET
                            lastfm_listeners = EXCLUDED.lastfm_listeners,
                            lastfm_playcount = EXCLUDED.lastfm_playcount,
                            listenbrainz_listens = EXCLUDED.listenbrainz_listens,
                            listenbrainz_users = EXCLUDED.listenbrainz_users,
                            lastfm_tags = COALESCE(EXCLUDED.lastfm_tags, track_popularity_cache.lastfm_tags),
                            source = EXCLUDED.source,
                            recording_mbid = COALESCE(EXCLUDED.recording_mbid, track_popularity_cache.recording_mbid),
                            release_track_title = COALESCE(EXCLUDED.release_track_title, track_popularity_cache.release_track_title),
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
                        "release_track_title": str(row.get("release_track_title") or "").strip() or None,
                    },
                )
            return len(rows)
    except Exception as exc:
        logger.error("Failed to bulk upsert track popularity", artist=rows[0].get("artist"), error=str(exc))
        return 0
