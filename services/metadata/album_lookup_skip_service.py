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

    ⚠️ TURNING IT OFF ALSO RELEASES THE RELEASE-DERIVED MISSING LIST.

    Reported: "I had an album that was previously linked to an MBID so it filled
    in the missing releases, I removed the MBID and selected 'Do not search album
    release', but the missing releases are still showing."

    The two halves are written by different paths and only one of them was
    reversible. The missing badges read ``missing_album_tracks``; the scan's
    refresh is what deletes rows that no longer match the release — and that
    refresh is SKIPPED for a "do not search" album (correctly: it would resolve
    the wrong release and delete a CSV tracklist). So once the flag is on, nothing
    can ever remove a row that a previous MBID-linked scan wrote.

    Clearing the flag re-enables the refresh, which deletes them on the next
    scan — but the page shows the stale list in the meantime, which is exactly
    what was reported. Release the release-derived rows here so the two agree
    immediately.

    A row is release-derived when it carries a ``release_id``. CSV-imported rows
    are written with ``release_id = NULL`` (see ``import_csv_tracklist``), so a
    user's own tracklist survives — and ``ignored = TRUE`` (a track the user
    rejected) always survives, as everywhere else.
    """
    value = "1" if skip else None
    updated = 0
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    f"UPDATE tracks SET album_lookup_skip = :value "
                    f"WHERE {_album_scope_clause()}"
                ),
                {"value": value, "artist": artist, "album": album},
            )
            updated = result.rowcount or 0
            session.commit()
    except Exception as exc:
        logger.warning(
            "Album lookup-skip flag write failed",
            artist=artist,
            album=album,
            error=str(exc),
        )
        return 0

    if not skip:
        _release_derived_missing_rows(artist, album)

    return updated


def _release_derived_missing_rows(artist: str, album: str) -> int:
    """Delete the missing tracks that came from a MusicBrainz release.

    Called when the album stops being "do not search" (or when its MBID is
    cleared), because those rows describe a release the album no longer claims.
    ``release_id IS NOT NULL`` is the discriminator; see ``set_album_lookup_skip``.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    "DELETE FROM missing_album_tracks "
                    "WHERE LOWER(TRIM(artist_name)) = LOWER(:artist) "
                    "  AND LOWER(TRIM(album_name)) = LOWER(:album) "
                    "  AND COALESCE(ignored, FALSE) = FALSE "
                    "  AND release_id IS NOT NULL "
                    "  AND TRIM(COALESCE(release_id, '')) <> ''"
                ),
                {"artist": artist, "album": album},
            )
            removed = int(result.rowcount or 0)
            session.commit()
    except Exception as exc:
        logger.debug(
            "Releasing release-derived missing rows failed",
            artist=artist, album=album, error=str(exc),
        )
        return 0

    if removed:
        logger.info(
            "Released missing tracks that came from a MusicBrainz release",
            artist=artist, album=album, removed=removed,
        )
    return removed


def release_missing_tracks_for_cleared_mbid(artist: str, album: str) -> int:
    """Public entry point for the MBID-clear path (``update_album_ids``)."""
    return _release_derived_missing_rows(artist, album)


def album_lookup_skip_value(track: dict[str, Any]) -> bool:
    """Read the flag from a raw track row dict (page render / load path)."""
    return str(track.get("album_lookup_skip") or "").strip().lower() in ("1", "true", "on", "yes")