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


def _track_from_row(row: Any) -> dict[str, Any]:
    """Map one ``_ARTIST_TRACK_COLUMNS`` row to the detector's track dict."""
    return {
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


def _load_tracks(where_sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the detector's fields for every track matching *where_sql*."""
    columns = ", ".join(_ARTIST_TRACK_COLUMNS)
    with db_session() as session:
        result = session.execute(
            text(f"SELECT {columns} FROM tracks WHERE {where_sql}"),
            params,
        )
        rows = result.fetchall() or []
    return [_track_from_row(row) for row in rows]


def _detect_for_tracks(
    tracks: list[dict[str, Any]],
    artist_name: str,
    *,
    conn: Any = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Run the deep detector album-by-album and return every result.

    The detector persists each verdict itself (``apply_cover_metadata_batch``),
    so callers use the results for reporting only.
    """
    if not tracks:
        return []

    try:
        detector = CoverDetector(db_connection=conn)
    except Exception as exc:
        logger.warning("CoverDetector unavailable", error=str(exc))
        return []

    # Group by album so the detector's per-album context and caching apply.
    albums: dict[str, list[dict[str, Any]]] = {}
    for track in tracks:
        album_key = track["album"] or "_no_album"
        albums.setdefault(album_key, []).append(track)

    results: list[dict[str, Any]] = []
    for album_name, album_tracks in albums.items():
        # Prefer the artist the scan was requested for; the first track's
        # artist can be a featured or per-track credit.
        artist = artist_name or (
            album_tracks[0].get("album_artist")
            or album_tracks[0].get("artist")
            or album_name
        )
        try:
            results.extend(
                detector.detect_covers_for_album(
                    album=album_name, artist=artist,
                    tracks=album_tracks, force=force,
                ) or []
            )
        except Exception as exc:
            logger.warning(
                "Cover detection failed for album",
                artist=artist_name,
                album=album_name,
                error=str(exc),
            )
    return results


def detect_covers_for_artist_report(
    artist_name: str,
    conn: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the full cover pipeline for every track of *artist_name*.

    Returns a summary dict (``track_count``, ``cover_count`` and the raw
    ``results``). ``conn`` is kept for backward compatibility — DB access runs
    on SQLAlchemy sessions.
    """
    try:
        tracks = _load_tracks(
            "LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)",
            {"artist": artist_name},
        )
    except Exception as exc:
        logger.error(
            "Failed to query tracks for artist cover scan",
            artist=artist_name,
            error=str(exc),
        )
        return {"track_count": 0, "cover_count": 0, "results": []}

    results = _detect_for_tracks(tracks, artist_name, conn=conn, force=force)
    # Results carry ``track_id`` and ``original_artist``; a repair is counted
    # separately by the detector. This is the same count the old implementation
    # reported as "updated".
    covers = [
        res for res in results
        if str(res.get("track_id") or "").strip() and not res.get("repaired")
    ]
    logger.info(
        "Cover detection complete for artist",
        artist=artist_name,
        updated_count=len(covers),
        album_count=len({track["album"] or "_no_album" for track in tracks}),
    )
    return {"track_count": len(tracks), "cover_count": len(covers), "results": results}


def detect_covers_for_artist(
    artist_name: str,
    conn: Any = None,
    force: bool = False,
) -> int:
    """Scan all tracks for *artist_name* and mark covers in the database.

    Uses the full ``CoverDetector`` pipeline (ISRC, MB relations, writer
    analysis, heuristics) for each track individually. Returns the number
    of covers found. ``conn`` is kept for backward compatibility — DB
    access runs on SQLAlchemy sessions.
    """
    return int(detect_covers_for_artist_report(
        artist_name, conn=conn, force=force,
    )["cover_count"])


def detect_covers_for_album_name(
    album: str,
    artist: str,
    conn: Any = None,
    force: bool = True,
) -> dict[str, Any]:
    """Run the full cover pipeline for one album's stored tracks."""
    try:
        tracks = _load_tracks(
            "LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
            "AND LOWER(COALESCE(album, '')) = LOWER(:album)",
            {"artist": artist, "album": album},
        )
    except Exception as exc:
        logger.error(
            "Failed to query tracks for album cover scan",
            artist=artist,
            album=album,
            error=str(exc),
        )
        return {"track_count": 0, "cover_count": 0, "results": []}

    results = _detect_for_tracks(tracks, artist, conn=conn, force=force)
    covers = [res for res in results if res.get("is_cover")]
    logger.info(
        "Cover detection complete for album",
        artist=artist,
        album=album,
        track_count=len(tracks),
        cover_count=len(covers),
    )
    return {"track_count": len(tracks), "cover_count": len(covers), "results": results}


def detect_covers_for_track(
    track_id: str,
    conn: Any = None,
    force: bool = True,
) -> dict[str, Any] | None:
    """Run the full cover pipeline for a single track.

    Returns ``None`` when the track does not exist, otherwise the track's
    resulting verdict. The detector persists it, so the caller only reports.
    """
    try:
        tracks = _load_tracks(
            "CAST(id AS TEXT) = :track_id", {"track_id": str(track_id)},
        )
    except Exception as exc:
        logger.error(
            "Failed to query track for cover scan",
            track_id=track_id,
            error=str(exc),
        )
        return None
    if not tracks:
        return None

    track = tracks[0]
    results = _detect_for_tracks(
        tracks,
        track.get("album_artist") or track.get("artist") or "",
        conn=conn,
        force=force,
    )
    result = next(
        (res for res in results if str(res.get("track_id") or "") == str(track_id)),
        None,
    )
    verdict = {
        "track_id": str(track_id),
        "is_cover": bool((result or {}).get("is_cover")),
        "original_artist": str((result or {}).get("original_artist") or ""),
        "reason": str((result or {}).get("reason") or ""),
    }
    logger.info(
        "Cover detection complete for track",
        track_id=track_id,
        is_cover=verdict["is_cover"],
    )
    return verdict


#: Runtime-registry key guarding concurrent manual cover-detection passes.
_COVER_DETECTION_RUNTIME_KEY = "cover_detection"


def cover_detection_running() -> bool:
    """True while a manual cover-detection pass runs in this process."""
    from services.scanning.runtime_state import is_runtime_running

    return is_runtime_running(_COVER_DETECTION_RUNTIME_KEY)


def start_cover_detection(
    scope: str,
    *,
    artist: str = "",
    album: str = "",
    track_id: str = "",
    force: bool = True,
) -> dict[str, Any]:
    """Run a cover-detection pass in the background.

    The pass is the same deep detector the scan pipeline uses (ISRC, MB
    relations, writer analysis, heuristics) and it persists every verdict
    itself. One pass at a time per worker; the returned dict is JSON-ready.
    """
    import threading

    from helpers.logging_config import log_unified
    from services.scanning.runtime_state import (
        clear_runtime,
        is_runtime_running,
        scan_lock,
        set_runtime,
    )

    scope = str(scope or "").strip().lower()
    if scope not in {"artist", "album", "track"}:
        return {"started": False, "error": f"Unknown cover detection scope: {scope}"}
    if scope == "track" and not str(track_id or "").strip():
        return {"started": False, "error": "track_id is required"}
    if scope == "album" and not (str(artist or "").strip() and str(album or "").strip()):
        return {"started": False, "error": "artist and album are required"}
    if scope == "artist" and not str(artist or "").strip():
        return {"started": False, "error": "artist is required"}

    def _worker() -> None:
        try:
            if scope == "track":
                verdict = detect_covers_for_track(track_id, force=force)
                if verdict is None:
                    log_unified(f"[COVER_DETECT] track {track_id}: not found")
                else:
                    if verdict["is_cover"]:
                        detail = (
                            f"cover of {verdict['original_artist']}"
                            if verdict["original_artist"]
                            else "cover"
                        )
                    else:
                        detail = "not a cover"
                    log_unified(f"[COVER_DETECT] track {track_id}: {detail}")
            elif scope == "album":
                summary = detect_covers_for_album_name(album, artist, force=force)
                log_unified(
                    f"[COVER_DETECT] {artist} - {album}: "
                    f"{summary['track_count']} track(s), "
                    f"{summary['cover_count']} cover(s)"
                )
            else:
                summary = detect_covers_for_artist_report(artist, force=force)
                log_unified(
                    f"[COVER_DETECT] {artist}: {summary['track_count']} track(s), "
                    f"{summary['cover_count']} cover(s)"
                )
        except Exception as exc:
            logger.error("Cover detection worker failed", scope=scope, error=str(exc))
            log_unified(f"[COVER_DETECT] {scope} detection failed: {exc}")
        finally:
            clear_runtime(_COVER_DETECTION_RUNTIME_KEY)

    with scan_lock:
        # The guard is intentionally narrow: only another cover-detection pass
        # is refused. A full scan may still be running, which the page's scan
        # sheet already warns about.
        if is_runtime_running(_COVER_DETECTION_RUNTIME_KEY):
            return {"started": False, "error": "Cover detection is already running"}

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        set_runtime(
            _COVER_DETECTION_RUNTIME_KEY,
            {"thread": thread, "type": "cover_detection"},
        )

    return {"started": True}


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
