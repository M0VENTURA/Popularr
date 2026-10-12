"""Per-album "do not search the album release" flag.

A custom album (a bootleg, a local tape rip, a self-made compilation that is
not on MusicBrainz/Discogs) has no release to look up. The album save and the
CSV tracklist upload mark ALL of the album's tracks with
``album_lookup_skip = '1'``; the scan then skips the album-release lookups
(album type from MusicBrainz, Discogs release id, art from CAA/Discogs/
AudioDB, and the per-album missing-track refresh against a MusicBrainz
release). Per-track recording metadata is untouched — this only stops the
ALBUM release lookup.

The flag lives on the ``tracks`` column ('' = search allowed, '1' = skip) and
is always written ALBUM-WIDE, matching the album → tracks fan-out rule.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)


def _album_scope_clause() -> str:
    """The WHERE that scopes a tracks query to one album.

    Matches the house definition:
    ``COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist))`` + ``album``.
    """
    return (
        "LOWER(TRIM(COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)))) = LOWER(:artist) "
        "AND LOWER(TRIM(album)) = LOWER(:album)"
    )


def album_lookup_skip(artist: str, album: str) -> bool:
    """True when the (artist, album) tracks carry ``album_lookup_skip = '1'``.

    The flag is written ALBUM-WIDE, so ANY track carrying it means the album
    is flagged. A missing/empty value reads as False (lookups on) — searching
    is the default.
    """
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    f"SELECT 1 FROM tracks "
                    f"WHERE {_album_scope_clause()} "
                    f"AND TRIM(COALESCE(album_lookup_skip, '')) = '1' "
                    f"LIMIT 1"
                ),
                {"artist": artist, "album": album},
            ).fetchone()
            return bool(row)
    except Exception as exc:
        # Fail OPEN to searching: a flag read must never silently block the
        # album's enrichment because the column is missing on a stale DB.
        logger.debug("Album lookup-skip flag read failed", error=str(exc))
        return False


def set_album_lookup_skip(artist: str, album: str, skip: bool) -> int:
    """Write the flag to EVERY track of the album (fan-out).

    Returns the number of rows updated. ``skip=True`` writes '1';
    ``skip=False`` clears it to NULL (the default, lookups on).
    """
    value = "1" if skip else None
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    f"UPDATE tracks SET album_lookup_skip = :value "
                    f"WHERE {_album_scope_clause()}"
                ),
                {"value": value, "artist": artist, "album": album},
            )
            session.commit()
            return result.rowcount or 0
    except Exception as exc:
        logger.warning(
            "Album lookup-skip flag write failed",
            artist=artist,
            album=album,
            error=str(exc),
        )
        return 0


def album_lookup_skip_value(track: dict[str, Any]) -> bool:
    """Read the flag from a raw track row dict (page render / load path)."""
    return str(track.get("album_lookup_skip") or "").strip().lower() in ("1", "true", "on", "yes")