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

- Added database persistence write-backs to `detect_covers_for_artist()`
  so detection results are updated in the tracks table.
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
# Verbosity follows the global logging config (the module used to force
# itself to DEBUG, flooding the file logs on every scan).

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
    "musicbrainz_workid",       # 17
    # Added: the deep pass reads these. Without ``recording_mbid`` a track the
    # metadata scan resolved looked "unresolved" and was skipped by the
    # identity-gated steps; ``is_cover_reason`` is kept when a confirmed
    # cover's title suffix is repaired.
    "recording_mbid",           # 18
    "is_cover_reason",          # 19
    "musicbrainz_trackid",      # 20
)


def detect_covers_for_artist(
    artist_name: str,
    conn: Any = None,
    force: bool = False,
) -> int:
    """Scan all tracks for *artist_name* and mark covers in the database.

    Uses the full ``CoverDetector`` pipeline (ISRC, MB relations, writer
    analysis, heuristics) for each track individually. Returns the number
    of tracks updated. ``conn`` is kept for backward compatibility — DB
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
            # Read into the detector's in-memory name: the detector normalises
            # ``musicbrainz_workid`` -> ``work_mbid`` at the top of
            # ``detect_covers_for_album``, so selecting the column is enough.
            "musicbrainz_workid": row_get(row, "musicbrainz_workid", 17, ""),
            "recording_mbid": row_get(row, "recording_mbid", 18, ""),
            "is_cover_reason": row_get(row, "is_cover_reason", 19, ""),
            "musicbrainz_trackid": row_get(row, "musicbrainz_trackid", 20, ""),
        }
        album_key = track["album"] or "_no_album"
        albums.setdefault(album_key, []).append(track)

    updated = 0
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

        # The detector has ALREADY persisted every verdict, title and genre
        # (``apply_cover_metadata_batch``), cleared unconfirmed flags and
        # stamped ``cover_last_checked``. This loop only COUNTS the results.
        #
        # ⚠️ It used to write them again, reading ``res["id"]`` and
        # ``res["original_cover_artist"]`` — but results carry ``track_id``
        # and ``original_artist``. Every UPDATE matched ``id = NULL`` (no
        # row), so the artist-level scan reported "updated" for writes that
        # never happened; and had the id matched, the wrong key would have
        # blanked the original artist the batch had just stored.
        for res in results:
            track_id = str(res.get("track_id") or "").strip()
            if not track_id:
                continue
            if not res.get("repaired"):
                updated += 1

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
            if t_artist.lower() == "soundtrack":
                artist = a_artist
            elif a_artist.lower() == "soundtrack":
                artist = t_artist
            else:
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

    work_mbid = ""
    if track_data:
        work_mbid = str(
            track_data.get("work_mbid") or track_data.get("musicbrainz_workid") or ""
        ).strip()
    if work_mbid and artist:
        # ⭐ SIDE-EFFECT FREE. This used to run the FULL album pipeline on a
        # one-track mock album. That pipeline WRITES: it persists covers
        # (renaming the title), clears "unconfirmed" verdicts and stamps
        # ``cover_last_checked`` — from inside the track stage, mid-scan, racing
        # the stage's own save. With no ``id`` on ``track_data`` the mock used
        # id "1", so those writes landed on whatever REAL track has id 1.
        #
        # The work-first check is the only part this quick check needs: one
        # cached browse of the work's recordings, no database access.
        try:
            from helpers.normalization_service import is_track_artist_placeholder

            if not is_track_artist_placeholder(artist):
                original = CoverDetector()._detect_via_work_id(
                    work_mbid=work_mbid,
                    performer=artist,
                    exclude_mbid=str(
                        track_data.get("recording_mbid") or track_data.get("mbid") or ""
                    ).strip(),
                )
                if original:
                    # Published for the caller, which may record the original
                    # artist so the deep pass treats the verdict as confirmed.
                    track_data["_detected_original_artist"] = str(
                        original.get("artist") or ""
                    ).strip()
                    logger.debug("Cover detected via MusicBrainz work", track=title)
                    return True, "musicbrainz_work_relation"
                if original == {}:
                    logger.debug("Cover check: work recordings are the performer's own", track=title)
                    return False, "work_original"
        except Exception as exc:
            logger.debug("Work-based cover check failed", track=title, error=str(exc))

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
