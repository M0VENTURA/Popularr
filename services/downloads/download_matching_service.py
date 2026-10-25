"""
DOWNLOAD MATCHING SERVICE

Handles:
- Folder matching wrappers for queue/release matching
- MusicBrainz release match batch application
- MusicBrainz metadata prefetch after match assignment
- Optional release track expansion into queue rows
- Filename-to-queue-item matching for cleanup logic
- Folder/release scoring
- Auto-match suggestion logic

Architecture rules:
✅ Routes call this service
✅ This service may orchestrate DB updates and enrichment lookups
✅ MusicBrainz lookup stays in services.enrichment.musicbrainz_service
✅ Queue item creation is delegated to download_processing_service.add_to_queue
✅ Cleanup matching can call filename_matches_queue_item()
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import structlog
from sqlalchemy import text

from api_clients.musicbrainz_http import MUSICBRAINZ_UUID_RE
from db.engine import db_session
from db.repositories.queue import (
    get_album_queue_tracks,
    get_queue_match_targets,
)
from db.repositories.tracks import find_library_track
from helpers.normalization_service import (
    edition_annotations_compatible,
    extract_track_disc,
    normalize_match_text,
)
from services.enrichment.musicbrainz_service import (
    MusicBrainzService,
    fetch_musicbrainz_release_metadata,
    fetch_release_metadata,
)

logger = structlog.get_logger(__name__)


def _is_valid_mbid(value: str | None) -> bool:
    if not value:
        return False
    return bool(MUSICBRAINZ_UUID_RE.match(str(value).strip()))


def select_best_musicbrainz_candidate(candidates: list[dict[str, Any]] | None) -> tuple[dict[str, Any] | None, str | None]:
    """Return the first valid MusicBrainz candidate from a candidate list."""
    if not candidates:
        return None, None

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        source = (candidate.get("source") or "").strip().lower()
        candidate_id = candidate.get("id")
        if source == "musicbrainz" and _is_valid_mbid(candidate_id):
            return candidate, str(candidate_id).strip()

    return None, None


def match_folder_group_with_musicbrainz(
    folder_path: str,
    artist: str,
    album: str,
    allow_discogs_fallback: bool = False,
) -> dict[str, Any]:
    """Resolve MusicBrainz release candidates for a folder/artist/album match."""
    if not artist or not album:
        return {"success": True, "candidates": []}

    try:
        service = MusicBrainzService(enabled=True)
        groups = service.search_releasegroup_matches(artist, album, limit=5)
        candidates = []
        for group in groups:
            candidates.append(
                {
                    "id": group.get("id"),
                    "source": "musicbrainz",
                    "title": group.get("title"),
                    "artist": artist,
                    "date": (
                        group.get("first-release-date")
                        or group.get("first_release_date")
                        or group.get("date")
                    ),
                    "match_score": group.get("match_score"),
                }
            )
        return {"success": True, "candidates": candidates}
    except Exception as exc:
        logger.debug("MusicBrainz release-group lookup failed", error=str(exc))
        return {"success": False, "error": str(exc), "candidates": []}


def match_folder(folder_path: str, data: Dict[str, Any]) -> Any:
    return match_folder_group_with_musicbrainz(folder_path, data.get("artist", "") or "", data.get("album", "") or "", allow_discogs_fallback=False)


def auto_match_folder(folder_path: str, data: Dict[str, Any]) -> Any:
    return match_folder(folder_path, data)


def search_and_update_musicbrainz(queue_id: int, artist: str, title: str, album: str) -> dict[str, Any]:
    """Search MusicBrainz for a release match and update the queue item metadata."""
    if not album:
        return {"success": False, "error": "No album provided"}

    try:
        match_result = match_folder_group_with_musicbrainz("", artist, album, allow_discogs_fallback=False)
        releases = match_result.get("candidates", []) if isinstance(match_result, dict) else []
        candidate, release_mbid = select_best_musicbrainz_candidate(releases)

        if not candidate or not release_mbid:
            return {"success": False, "error": "No MusicBrainz match found"}

        release_year = None
        if candidate.get("date"):
            release_year = str(candidate.get("date"))[:4]

        release_artist = candidate.get("artist") or artist
        cover_art_url = candidate.get("cover_art_url") or candidate.get("cover_art") or ""

        if not cover_art_url:
            try:
                from api_clients.coverartarchive import get_release_image_from_caa
                cover_art_url = get_release_image_from_caa(release_mbid) or ""
            except Exception as exc:
                logger.debug("Cover Art Archive lookup failed", queue_id=queue_id, error=str(exc))

        from db.repositories.queue import update_queue_item

        update_queue_item(
            queue_id,
            release_mbid=release_mbid,
            release_id=release_mbid,
            release_source="musicbrainz",
            release_year=release_year,
            album_artist=release_artist,
            cover_art_url=cover_art_url or None,
            status="matched",
        )

        return {
            "success": True,
            "queue_id": queue_id,
            "release_mbid": release_mbid,
            "release_year": release_year,
            "cover_art_url": cover_art_url or None,
        }
    except Exception as exc:
        logger.error("MusicBrainz auto-search failed", queue_id=queue_id, error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


# =============================================================================
# Optional MusicBrainz cache compatibility
# =============================================================================

try:
    from db.repositories.musicbrainz_cache import (
        cache_release_metadata,
        get_cached_release_metadata,
    )
except ImportError:
    get_cached_release_metadata = None
    cache_release_metadata = None


# =============================================================================
# Optional Discogs track fetcher
# =============================================================================

try:
    from services.queue.queue_matching_service import (
        _fetch_discogs_tracks as fetch_discogs_tracks,
    )
except ImportError:
    fetch_discogs_tracks = None


# =============================================================================
# Small DB/helper utilities
# =============================================================================

def _row_get(row: Any, key: str, index: int, default: Any = None) -> Any:
    """Safely read either dict-like DB rows or tuple/list rows."""
    if row is None:
        return default
    if hasattr(row, "_mapping"):
        return row._mapping.get(key, default)
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[index]
    except Exception:
        return default


def _extract_release_year(raw_year: Any) -> int | None:
    """Extract a 4-digit release year from MusicBrainz metadata."""
    if raw_year in (None, ""):
        return None
    match = re.search(r"(19|20)\d{2}", str(raw_year))
    if not match:
        return None
    try:
        return int(match.group(0))
    except Exception:
        return None


# =============================================================================
# MBID batch match application
# =============================================================================

_ALLOWED_RELEASE_UPDATES = {"album_artist", "album", "release_year"}
_ALLOWED_TRACK_UPDATES = {"artist", "title"}


def _prefetch_mbid_metadata_batch(
    mbid: str,
    queue_ids: List[int],
) -> None:
    """Fetch MusicBrainz release metadata and enrich already matched queue rows."""
    if not mbid or not queue_ids:
        return

    try:
        release_meta = fetch_release_metadata(mbid) or {}

        if not release_meta:
            logger.debug("No release metadata found for MBID", mbid=mbid)
            return

        release_artist = (release_meta.get("artist") or "").strip() or None
        release_title = (release_meta.get("release_title") or "").strip() or None
        release_year = _extract_release_year(release_meta.get("release_year"))

        tracks = release_meta.get("tracks") or []
        tracks_by_rec_mbid = {
            (track.get("recording_mbid") or track.get("id")): track
            for track in tracks
            if (track.get("recording_mbid") or track.get("id"))
        }

        release_updates: Dict[str, Any] = {}
        if release_artist:
            release_updates["album_artist"] = release_artist
        if release_title:
            release_updates["album"] = release_title
        if release_year is not None:
            release_updates["release_year"] = release_year

        # Filter strictly to prevent SQL injection via dynamic column mapping
        safe_release_updates = {k: v for k, v in release_updates.items() if k in _ALLOWED_RELEASE_UPDATES}

        try:
            with db_session() as session:
                if safe_release_updates:
                    ids_placeholders = ", ".join(f":qid{i}" for i in range(len(queue_ids)))
                    set_clause = ", ".join(f"{column} = :{column}" for column in safe_release_updates.keys())

                    session.execute(
                        text(f"""
                            UPDATE download_queue
                            SET {set_clause},
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id IN ({ids_placeholders})
                        """),
                        {
                            **{col: safe_release_updates[col] for col in safe_release_updates},
                            **{f"qid{i}": qid for i, qid in enumerate(queue_ids)},
                        },
                    )

                if tracks_by_rec_mbid:
                    ids_placeholders = ", ".join(f":qid{i}" for i in range(len(queue_ids)))
                    rows = session.execute(
                        text(f"""
                            SELECT id, recording_mbid
                            FROM download_queue
                            WHERE id IN ({ids_placeholders})
                        """),
                        {f"qid{i}": qid for i, qid in enumerate(queue_ids)},
                    ).fetchall()

                    for row in rows:
                        item_id = _row_get(row, "id", 0)
                        recording_mbid = (_row_get(row, "recording_mbid", 1, "") or "").strip()

                        if not item_id or not recording_mbid:
                            continue

                        track = tracks_by_rec_mbid.get(recording_mbid)
                        if not track:
                            continue

                        track_updates: Dict[str, Any] = {}
                        track_artist = (track.get("artist") or "").strip()
                        track_title = (track.get("title") or track.get("recording_title") or "").strip()

                        if track_artist:
                            track_updates["artist"] = track_artist
                        if track_title:
                            track_updates["title"] = track_title

                        safe_track_updates = {k: v for k, v in track_updates.items() if k in _ALLOWED_TRACK_UPDATES}
                        if not safe_track_updates:
                            continue

                        set_clause = ", ".join(f"{column} = :{column}" for column in safe_track_updates.keys())

                        session.execute(
                            text(f"""
                                UPDATE download_queue
                                SET {set_clause},
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = :item_id
                            """),
                            {
                                **{col: safe_track_updates[col] for col in safe_track_updates},
                                "item_id": item_id,
                            },
                        )

            logger.info("Prefetched MB metadata for MBID", mbid=mbid, count=len(queue_ids), fields=sorted(safe_release_updates.keys()))

        except Exception:
            raise

    except Exception as exc:
        logger.warning("Metadata prefetch failed for MBID", mbid=mbid, error=str(exc))


def _expand_release_tracks(
    mbid: str,
    artist_name: str,
    album_name: str,
) -> None:
    """Expand a MusicBrainz release into queue rows."""
    if not mbid:
        return

    try:
        from services.downloads.download_processing_service import add_to_queue

        release_data = fetch_release_metadata(mbid) or {}
        release_tracks = release_data.get("tracks") or []

        inferred_year_raw = release_data.get("release_year")
        inferred_year = (
            str(inferred_year_raw).strip()
            if inferred_year_raw not in (None, "")
            else None
        )

        added_tracks = 0

        for track in release_tracks:
            track_title = (track.get("title") or "").strip()
            if not track_title:
                continue

            track_artist = (track.get("artist") or artist_name or "").strip() or artist_name
            track_duration = track.get("duration")
            if track_duration:
                try:
                    track_duration = int(track_duration) // 1000
                except (TypeError, ValueError):
                    track_duration = None

            result = add_to_queue(
                artist=track_artist,
                title=track_title,
                album=album_name,
                source="soulseek",
                album_artist=artist_name,
                release_mbid=mbid,
                release_id=mbid,
                release_source="musicbrainz",
                track_number=track.get("track_number"),
                disc_number=track.get("disc_number"),
                recording_mbid=track.get("recording_mbid") or track.get("id"),
                year=inferred_year,
                duration=track_duration,
            )

            if result.get("success"):
                added_tracks += 1

        logger.info("Expanded MBID release tracks", mbid=mbid, added_tracks=added_tracks)

    except Exception as exc:
        logger.warning("Release expansion failed for MBID", mbid=mbid, error=str(exc))


# =============================================================================
# Release metadata / track lookups
# =============================================================================

def get_musicbrainz_release_metadata(
    release_id: str,
) -> Optional[Dict[str, Any]]:
    """Fetch release metadata from cache first, then MusicBrainz live service."""
    if not release_id:
        return None

    cached_metadata = None

    if get_cached_release_metadata:
        try:
            cached_metadata = get_cached_release_metadata(release_id)
            if cached_metadata and cached_metadata.get("tracks"):
                return cached_metadata
        except Exception as cache_err:
            logger.debug("Could not read cached MusicBrainz metadata", release_id=release_id, error=str(cache_err))

    try:
        live_metadata = fetch_musicbrainz_release_metadata(release_id)

        if live_metadata and live_metadata.get("tracks") and cache_release_metadata:
            try:
                cache_release_metadata(release_id, live_metadata)
            except Exception as cache_write_err:
                logger.debug("Could not cache MusicBrainz metadata", release_id=release_id, error=str(cache_write_err))

        return live_metadata or cached_metadata

    except Exception as live_err:
        logger.error("Error fetching MusicBrainz release metadata", release_id=release_id, error=str(live_err))
        return cached_metadata


def get_release_tracks(
    release_id: str,
    source: str = "musicbrainz",
) -> List[Dict[str, Any]]:
    """Fetch full track listing for a MusicBrainz or Discogs release."""
    if not release_id:
        return []

    try:
        if source == "musicbrainz":
            metadata = get_musicbrainz_release_metadata(release_id)
            return metadata.get("tracks", []) if metadata else []

        if source == "discogs":
            if not fetch_discogs_tracks:
                logger.warning("Discogs track fetcher is not available")
                return []
            return fetch_discogs_tracks(release_id)

        logger.error("Unknown release source", source=source)
        return []

    except Exception as exc:
        logger.error("Error fetching release tracks", source=source, release_id=release_id, error=str(exc))
        return []


def get_musicbrainz_release_tracks(
    release_id: str,
    source: str = "musicbrainz",
) -> List[Dict[str, Any]]:
    """Backwards-compatible wrapper for legacy callers."""
    if not release_id:
        return []
    return get_release_tracks(release_id=release_id, source=source)


def _find_matching_queue_item(
    track: dict[str, Any],
    queue_items: list[dict[str, Any]],
    used_queue_ids: set[int],
) -> dict[str, Any] | None:
    """Match a release track against queue items."""
    track_title = track.get("title") or ""
    track_num, _ = extract_track_disc(str(track.get("track_number") or ""))
    title_norm = normalize_match_text(track_title)

    if track_num is not None:
        for candidate in queue_items:
            if candidate.get("id") in used_queue_ids:
                continue

            if not edition_annotations_compatible(
                track_title, candidate.get("title") or ""
            ):
                continue

            candidate_num, _ = extract_track_disc(str(candidate.get("track_number") or ""))
            if candidate_num == track_num:
                return candidate

    for candidate in queue_items:
        if candidate.get("id") in used_queue_ids:
            continue

        if not edition_annotations_compatible(
            track_title, candidate.get("title") or ""
        ):
            continue

        candidate_title_norm = normalize_match_text(candidate.get("title") or "")
        if candidate_title_norm and candidate_title_norm == title_norm:
            return candidate

    return None


def _get_library_tracks_for_album(artist: str, album: str) -> dict[str, dict[str, Any]]:
    """Bulk fetch library tracks for an album to prevent N+1 queries."""
    tracks_by_norm_title = {}
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, title, file_path, album 
                    FROM tracks 
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) 
                      AND LOWER(album) = LOWER(:album)
                """),
                {"artist": artist, "album": album}
            ).fetchall()
            for row in rows:
                title = row._mapping.get("title") or ""
                title_norm = normalize_match_text(title)
                if title_norm:
                    tracks_by_norm_title[title_norm] = dict(row._mapping)
    except Exception as exc:
        logger.debug("Failed to bulk fetch library tracks", error=str(exc))
    return tracks_by_norm_title


def get_release_tracks_with_status(
    artist: str,
    album: str,
    release_id: str,
    current_folder_files: list[str] | None = None,
) -> dict[str, Any]:
    """Return release tracks with queue/library status."""
    release_tracks = get_release_tracks(
        release_id=release_id,
        source="musicbrainz",
    )

    if not release_tracks:
        return {
            "success": False,
            "tracks": [],
            "summary": {},
        }

    queue_items = get_album_queue_tracks(
        artist=artist,
        album=album,
    )

    library_tracks_by_title = _get_library_tracks_for_album(artist, album)

    current_folder_files = [
        f.lower()
        for f in (current_folder_files or [])
    ]

    used_queue_ids: set[int] = set()
    tracks_with_status: list[dict[str, Any]] = []

    for track in release_tracks:
        status = "missing"
        status_details: dict[str, Any] = {}

        queue_item = _find_matching_queue_item(
            track,
            queue_items,
            used_queue_ids,
        )

        if queue_item:
            used_queue_ids.add(int(queue_item["id"]))
            status = "downloading"
            status_details = {
                "queue_id": queue_item.get("id"),
                "queue_status": queue_item.get("status"),
                "queue_title": queue_item.get("title"),
                "queue_track_number": queue_item.get("track_number"),
            }
        elif current_folder_files:
            track_title_norm = normalize_match_text(track.get("title") or "")
            for filename in current_folder_files:
                filename_norm = normalize_match_text(os.path.splitext(filename)[0])
                if track_title_norm and (
                    track_title_norm in filename_norm
                    or (len(filename_norm) >= 6 and filename_norm in track_title_norm)
                ):
                    status = "in_folder"
                    status_details = {"filename": filename}
                    break

        if status == "missing":
            track_title_norm = normalize_match_text(track.get("title") or "")
            library_track = library_tracks_by_title.get(track_title_norm)
            if library_track:
                status = "other_folder"
                status_details = {
                    "track_id": library_track.get("id"),
                    "file_path": library_track.get("file_path"),
                    "album": library_track.get("album"),
                }

        tracks_with_status.append(
            {
                **track,
                "status": status,
                "status_details": status_details,
            }
        )

    statuses = [track["status"] for track in tracks_with_status]

    return {
        "success": True,
        "summary": {
            "total": len(tracks_with_status),
            "downloading": statuses.count("downloading"),
            "in_folder": statuses.count("in_folder"),
            "other_folder": statuses.count("other_folder"),
            "missing": statuses.count("missing"),
        },
        "tracks": tracks_with_status,
    }


def apply_musicbrainz_match(data: dict[str, Any]) -> dict[str, Any]:
    """Apply a MusicBrainz release group match to all queue items for an artist/album."""
    artist = (data or {}).get("artist", "").strip()
    album = (data or {}).get("album", "").strip()
    release_group_id = (data or {}).get("release_group_id", "").strip()

    if not artist or not album or not release_group_id:
        return {"success": False, "error": "artist, album, and release_group_id are required"}

    try:
        from services.downloads.match_orchestrator import apply_mbid_match_batch

        queue_items = get_album_queue_tracks(artist, album)
        if not queue_items:
            return {"success": False, "error": "No queue items found for this artist/album"}

        queue_ids = [int(item["id"]) for item in queue_items if item.get("id")]

        result = apply_mbid_match_batch(
            queue_ids=queue_ids,
            new_mbid=release_group_id,
            new_artist=artist,
            new_album=album,
            expand_tracks=False,
        )

        return {
            "success": result.get("success", False),
            "processed": len(queue_ids),
            "total": len(queue_ids),
            "error": result.get("error"),
        }
    except Exception as exc:
        logger.error("apply_musicbrainz_match failed", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


def _is_safe_read_folder(folder_path: str) -> bool:
    """Validate a folder path strictly belongs to an allowed external download/torrent root."""
    if not folder_path:
        return False
    try:
        real_target = os.path.realpath(folder_path)
        if not os.path.isdir(real_target):
            return False

        allowed_roots = set()
        try:
            from services.downloads.download_scan_service import resolve_downloads_dir, resolve_torrents_dir
            d_dir = resolve_downloads_dir()
            if d_dir and os.path.isdir(d_dir):
                allowed_roots.add(os.path.realpath(d_dir))
                _parent = os.path.dirname(os.path.realpath(d_dir))
                for _cand in ("torrents", "Torrents", "TORRENTS"):
                    _sib = os.path.join(_parent, _cand)
                    if os.path.isdir(_sib):
                        allowed_roots.add(os.path.realpath(_sib))
            t_dir = resolve_torrents_dir()
            if t_dir and os.path.isdir(t_dir):
                allowed_roots.add(os.path.realpath(t_dir))
        except Exception:
            pass

        for allowed in allowed_roots:
            if os.path.commonpath([real_target, allowed]) == allowed:
                return True
    except Exception:
        return False
    return False


def get_release_status(data: dict[str, Any]) -> dict[str, Any]:
    """Get release tracks with queue/library status for a given artist/album/release."""
    artist = (data or {}).get("artist", "").strip()
    album = (data or {}).get("album", "").strip()
    release_id = (data or {}).get("release_group_id", "").strip()
    folder_path = (data or {}).get("folder_path", "").strip()

    if not artist or not album or not release_id:
        return {"success": False, "tracks": [], "summary": {}, "error": "artist, album, and release_group_id are required"}

    try:
        current_folder_files: list[str] = []
        if folder_path and _is_safe_read_folder(folder_path):
            current_folder_files = [
                f for f in os.listdir(folder_path)
                if os.path.isfile(os.path.join(folder_path, f))
            ]

        return get_release_tracks_with_status(
            artist=artist,
            album=album,
            release_id=release_id,
            current_folder_files=current_folder_files,
        )
    except Exception as exc:
        logger.error("get_release_status failed", error=str(exc), exc_info=True)
        return {"success": False, "tracks": [], "summary": {}, "error": str(exc)}
