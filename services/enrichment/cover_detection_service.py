"""Cover song detection service.

Identifies cover songs using the ``CoverDetector`` class, which uses
MusicBrainz relations, writer/composer analysis, ISRC matching, and
heuristic title annotations.

Key Functions:
    - detect_covers_for_artist(): Scan all tracks for an artist and mark
      covers in the database.
    - detect_covers_for_album(): Detect covers within a single album (used
      during popularity scan).
    - detect_cover_song(): Quick single-track check used during popularity
      scanning.

Rebuilt with the following corrections:

- Added `album_artist` to SQL extraction so `detect_cover_song` can fall
  back to it if the track artist is "Soundtrack" or differs.
- The row mapping in ``detect_covers_for_artist`` read ``composer`` (index 4)
  into the ``writer`` field and never read the real ``writer`` column
  (index 5), so writer-based attribution ran on the wrong data.
- ``detect_cover_song`` returned ``False`` for tracks it was only meant to
  skip, so a caller writing the verdict back cleared the flag on confirmed
  and manually-overridden covers. Skips now return the existing verdict.
- The second tuple element mixed normalised titles with reason strings. It is
  now always a reason; the normalised title is available separately.
- A songwriter differing from the performer no longer independently returns
  ``True``, since external songwriters are normal for commercial releases.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.utils import row_get
from helpers.normalization_service import detect_cover_and_normalize_title
from services.enrichment.cover_detector_impl import CoverDetector

logger = structlog.get_logger(__name__)
logging.getLogger(__name__).setLevel(logging.DEBUG)

_ARTIST_TRACK_COLUMNS = (
    "id",                       # 0
    "title",                    # 1
    "artist",                   # 2
    "album_artist",             # 3
    "album",                    # 4
    "composer",                 # 5
    "writer",                   # 6
    "isrc",                     # 7
    "mbid",                     # 8
    "musicbrainz_album_mbid",   # 9
    "file_path",                # 10
    "is_cover",                 # 11
    "genres",                   # 12
    "musicbrainz_genres",       # 13
    "original_cover_artist",    # 14
    "cover_manual_override",    # 15
    "cover_last_checked",       # 16
)


def detect_covers_for_artist(
    artist_name: str,
    conn: Any = None,
    force: bool = False,
) -> int:
    """Scan all tracks for *artist_name* and mark covers in the database.

    Uses the full ``CoverDetector`` pipeline (ISRC, MB relations, writer
    analysis, heuristics) for each track individually.  Returns the number
    of tracks updated.  ``conn`` is kept for backward compatibility — DB
    access runs on SQLAlchemy sessions.
    """
    try:
        detector = CoverDetector(db_connection=conn)
    except Exception as exc:
        logger.warning("CoverDetector unavailable", error=str(exc))
        return 0

    columns = ", ".join(_ARTIST_TRACK_COLUMNS)
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    f"SELECT {columns} FROM tracks "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"
                ),
                {"artist": artist_name},
            )
            rows = result.fetchall() or []
    except Exception as exc:
        logger.error(
            "Failed to query tracks for artist cover scan",
            artist=artist_name,
            error=str(exc),
        )
        return 0

    # Build a pseudo-album context so per-album caching works.
    albums: dict[str, list[dict[str, Any]]] = {}
    updated = 0
    for row in rows:
        track = {
            "id": str(row_get(row, "id", 0, "")),
            "title": str(row_get(row, "title", 1, "")),
            "artist": str(row_get(row, "artist", 2, "")),
            "album_artist": str(row_get(row, "album_artist", 3, "")),
            "album": str(row_get(row, "album", 4, "")),
            "composer": row_get(row, "composer", 5),
            "writer": row_get(row, "writer", 6),
            "isrc": row_get(row, "isrc", 7),
            "mbid": row_get(row, "mbid", 8),
            "musicbrainz_album_mbid": row_get(row, "musicbrainz_album_mbid", 9),
            "file_path": row_get(row, "file_path", 10),
            "is_cover": row_get(row, "is_cover", 11),
            "genres": row_get(row, "genres", 12),
            "musicbrainz_genres": row_get(row, "musicbrainz_genres", 13),
            "original_cover_artist": row_get(row, "original_cover_artist", 14, ""),
            "cover_manual_override": row_get(row, "cover_manual_override", 15, False),
            "cover_last_checked": row_get(row, "cover_last_checked", 16),
        }
        album_key = track["album"] or "_no_album"
        albums.setdefault(album_key, []).append(track)

    for album_name, tracks in albums.items():
        # Prefer the album artist the scan was requested for; the first
        # track's artist can be a featured or per-track credit.
        artist = artist_name or (tracks[0].get("album_artist") or tracks[0].get("artist") or album_name)
        try:
            results = detector.detect_covers_for_album(
                album=album_name, artist=artist, tracks=tracks, force=force,
            )
        except Exception as exc:
            logger.warning(
                "Cover detection failed for album",
                artist=artist_name,
                album=album_name,
                error=str(exc),
            )
            continue
        updated += len(results)

    logger.info(
        "Cover detection complete for artist",
        artist=artist_name,
        updated_count=updated,
        album_count=len(albums),
    )
    return updated


def detect_covers_for_album(
    album: str,
    artist: str,
    tracks: list[dict[str, Any]],
    conn: Any = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Detect covers for a single album context (called during popularity scan).

    Args:
        album: Album title.
        artist: Album artist.
        tracks: List of track dicts with metadata fields.
        conn: Optional DB connection (used by the underlying detector).
        force: If True, re-check even already-confirmed covers.

    Returns the list of cover result dicts so the caller can integrate them
    into the track update payload.
    """
    try:
        detector = CoverDetector(db_connection=conn)
        return detector.detect_covers_for_album(
            album=album, artist=artist, tracks=tracks, force=force,
        )
    except Exception as exc:
        logger.warning(
            "Cover detection failed for album",
            album=album,
            artist=artist,
            error=str(exc),
        )
        return []


def detect_cover_song(
    title: str,
    artist: str,
    composer: str | None = None,
    writer: str | None = None,
    track_data: dict[str, Any] | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Quick single-track cover check used during popularity scanning.

    Returns ``(is_cover, reason)``. The second element is always a reason
    code, never a normalised title.

    Caching behaviour (avoids re-detecting on every scan):
    - ``cover_manual_override=True`` → keep the stored verdict.
    - ``is_cover`` truthy AND ``original_cover_artist`` populated → keep the
      stored verdict unless ``force=True``.
    - Otherwise run the standard detection pipeline.

    Skips return the *existing* verdict rather than ``False``, so a caller
    writing the result back cannot clear a confirmed cover flag.
    """
    # Evaluate if 'Soundtrack' or mismatched track/album artists need correcting
    if track_data:
        t_artist = str(track_data.get("artist") or "").strip()
        a_artist = str(track_data.get("album_artist") or "").strip()
        
        if t_artist and a_artist and t_artist != a_artist:
            # If the track artist is practically useless for detection (e.g., 'Soundtrack'), use the album artist
            if t_artist.lower() == "soundtrack":
                artist = a_artist
            elif a_artist.lower() == "soundtrack":
                artist = t_artist
            else:
                # Typically, when they differ, the specific track artist is the better metric to verify a cover
                artist = t_artist

    if not force and track_data:
        existing_cover = bool(track_data.get("is_cover"))

        if track_data.get("cover_manual_override"):
            logger.debug("Cover check skipped: manual override", track=title)
            return existing_cover, "manual_override"

        existing_orig = str(track_data.get("original_cover_artist") or "").strip()
        if existing_cover and existing_orig:
            logger.debug("Cover check skipped: already confirmed", track=title)
            return True, "already_confirmed"

    is_cover, _normalized = detect_cover_and_normalize_title(title)
    if is_cover:
        logger.debug("Cover detected via title annotation", track=title)
        return True, "title_annotation"

    # NOTE: a songwriter differing from the performer is normal for most
    # commercially released music (staff writers, producers, session
    # composers) and is not on its own sufficient to call a track a cover.
    # It is reported as weak corroboration for the caller to combine with
    # other signals, not as a positive verdict.
    if composer or writer:
        for credit in (composer, writer):
            if not credit:
                continue
            from helpers.normalization_service import names_match

            if not names_match(credit, artist):
                logger.debug("Cover check: External writer only, insufficient evidence", track=title, credit=credit)
                return False, "external_writer_only"

    logger.debug("Cover check: No match", track=title)
    return False, "no_match"
