"""Queue processing service.

Handles post-download queue processing and organisation:
    - Adding items to queue
    - Batch ingestion
    - Matching completed downloads to queue items via metadata
    - Extracting metadata from downloaded audio files
    - Organising files into the library directory structure
    - Updating queue status and library records
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import structlog
from sqlalchemy import text

from api_clients.slskd_http import get_slskd_client
from db.engine import db_session
from db.repositories.queue import (
    delete_queue_item as _delete_from_db,
    get_active_queue,
    get_completed_group_queue_items,
    get_completed_queue,
    get_queue_item,
    get_queue_item_by_path,
    get_queue_status_counts,
    insert_queue_item,
    mark_failed,
    purge_all,
    requeue_queue_item,
    update_queue_item,
)
from db.repositories.tracks import find_library_track
from helpers.logging_config import log_unified
from helpers.metadata_reader import read_mp3_metadata
from helpers.normalization_service import normalize_artist, queue_duration_seconds
from services.downloads.download_organize_service import rename_and_move_file
from services.downloads.slskd_service import SlskdService
from services.metadata.tag_file_service import update_file_metadata
from services.queue.queue_diagnostics_service import log_queue_event
from services.queue.queue_signal import signal_new_item

logger = structlog.get_logger(__name__)


# =============================================================================
# HELPERS
# =============================================================================

def is_musicbrainz_backed(queue_item: dict[str, Any]) -> bool:
    source = str(queue_item.get("release_source") or "").lower()
    if source == "musicbrainz":
        return True
    mbid_pattern = r"^[0-9a-fA-F-]{36}$"
    return any(
        re.match(mbid_pattern, str(queue_item.get(field) or ""))
        for field in ("release_id", "release_mbid", "recording_mbid")
    )


def calculate_artist_similarity_score(expected_artist: str, candidate_artist: str) -> float:
    expected_norm = normalize_artist(expected_artist)
    candidate_norm = normalize_artist(candidate_artist)

    if not expected_norm or not candidate_norm:
        return 0.0

    if expected_norm == candidate_norm:
        return 1.0

    ratio = difflib.SequenceMatcher(None, expected_norm, candidate_norm).ratio()
    if expected_norm in candidate_norm or candidate_norm in expected_norm:
        ratio = max(ratio, 0.92)
    return ratio


# =============================================================================
# ADDING TO QUEUE
# =============================================================================

def add_to_queue(
    artist: str,
    title: str,
    album: str | None = None,
    source: str = "soulseek",
    priority: int = 5,
    **kwargs: Any,
) -> Dict[str, Any]:
    if not artist or not title:
        return {"success": False, "error": "Artist and title are required"}

    try:
        item = insert_queue_item(
            artist=artist.strip(),
            title=title.strip(),
            album=album.strip() if album else None,
            source=source,
            priority=priority,
            **kwargs,
        )

        try:
            queue_id = item.get("id") if isinstance(item, dict) else None
            log_queue_event(
                "queued",
                f"{artist.strip()} - {title.strip()}"
                + (f" [{album.strip()}]" if album and album.strip() else ""),
                queue_id=queue_id,
                source=source,
            )
        except Exception:
            pass

        if item.get("already_queued"):
            # ⚠️ A dedupe is NOT a plain success. The row was NOT inserted — an
            # existing one blocked it. Returning a bare success made every caller
            # (``/api/queue/add``, the album page, release/playlist importers) tell
            # the user "Added to queue" while nothing changed and the track did not
            # appear. Reported as "files I'm adding to download aren't showing".
            #
            # The message is derived from the EXISTING row's status so the user
            # learns WHY, and ``displayable`` flags the genuinely confusing case: a
            # blocker the queue page cannot render is a contradiction worth
            # surfacing rather than hiding.
            existing_status = str(item.get("status") or "").lower()
            try:
                from services.queue.queue_constraints import QUEUE_DISPLAY_STATUSES
                is_displayable = existing_status in QUEUE_DISPLAY_STATUSES
            except Exception:
                is_displayable = True

            return {
                "success": True,
                "already_queued": True,
                "inserted": False,
                "queue_id": item.get("id"),
                "status": existing_status,
                "displayable": is_displayable,
                "message": (
                    f"Already in the queue ({existing_status or 'unknown'})"
                    if is_displayable
                    else (
                        f"Already in the queue with status '{existing_status}', which "
                        "the queue page does not list — clear or retry that row to "
                        "re-add this track"
                    )
                ),
                "item": item,
            }

        try:
            signal_new_item()
        except Exception:
            pass

        return {
            "success": True,
            "inserted": True,
            "queue_id": item.get("id") if isinstance(item, dict) else None,
            "item": item,
        }
    except Exception as e:
        logger.error("Queue add failed", error=str(e), artist=artist, title=title)
        return {"success": False, "error": str(e)}


def queue_add(payload: Dict[str, Any]) -> Dict[str, Any]:
    return add_to_queue(
        artist=str(payload.get("artist") or "").strip(),
        title=str(payload.get("title") or "").strip(),
        album=str(payload.get("album")).strip() if payload.get("album") else None,
        source=str(payload.get("source", "soulseek")),
        priority=int(payload.get("priority", 5)),
    )


def queue_add_batch(data: Dict[str, Any]) -> Dict[str, Any]:
    items = data.get("items", [])
    if not isinstance(items, list):
        return {"success": False, "error": "items must be a list"}

    added, skipped, failed = 0, 0, 0
    results: List[Dict[str, Any]] = []

    for item in items:
        result = add_to_queue(
            artist=item.get("artist"),
            title=item.get("title"),
            album=item.get("album"),
            source=item.get("source", data.get("source", "soulseek")),
            priority=item.get("priority", 5),
            track_number=item.get("track_number"),
            album_artist=item.get("album_artist"),
            year=item.get("year"),
            release_id=item.get("release_id"),
            release_mbid=item.get("release_mbid"),
            recording_mbid=item.get("recording_mbid"),
            disc_number=item.get("disc_number"),
            duration=item.get("duration"),
            import_group=item.get("import_group") or data.get("import_group"),
            import_type=item.get("import_type") or data.get("import_type"),
        )
        results.append(result)
        if result.get("already_queued"):
            skipped += 1
        elif result.get("success"):
            added += 1
        else:
            failed += 1

    return {
        "success": True,
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


#: Human-readable explanation for every non-empty ``reason`` code.
_QUEUE_SKIP_MESSAGES: dict[str, str] = {
    "already_active": "This release already has active downloads in the queue.",
    "no_tracks": "MusicBrainz returned no tracks for this release.",
    "all_in_library": "Every track in this release is already in your library.",
    "already_queued": "Every track in this release is already in the download queue.",
    "all_present": "Every track in this release is already in your library or queue.",
    "all_duplicate": "The release listing repeated the same tracks.",
    "nothing_queued": "No tracks could be queued for this release.",
}


def add_release_tracks_to_queue_detailed(
    release_id: str,
    tracks: list[dict[str, Any]],
    artist: str,
    album: str,
    album_artist: str | None = None,
    queue_source: str = "soulseek",
    year: int | None = None,
    album_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add normalized tracks to the download queue, reporting WHY any were skipped.

    A bare ``list[int]`` return cannot distinguish "nothing to queue" from
    "nothing to queue because you already own every track" — both are ``[]``.
    Callers that surface the outcome to a user need that difference, so this
    returns a structured result and ``add_release_tracks_to_queue`` stays as the
    back-compat list-returning wrapper.

    ``album_metadata`` carries the ALBUM-scoped MusicBrainz fields (release
    type / status / country / album-artist MBID / original year …).  They are
    stored on every row's ``metadata`` JSON so the post-download import can
    apply the same release metadata an album-page lookup would, without
    re-fetching MusicBrainz.  Optional: a caller that does not supply it leaves
    behaviour exactly as before.

    Returns a dict with ``queue_ids`` (list[int]), ``queued`` (bool), ``reason``
    (None when something was queued, otherwise a code documented in
    ``_QUEUE_SKIP_MESSAGES``), a human-readable ``message``, and counts for
    ``already_active``, ``in_library``, ``already_queued`` and ``duplicate``.
    """
    _album_meta: dict[str, Any] = {}
    if isinstance(album_metadata, dict):
        from services.downloads.download_completion_service import (
            _ALBUM_LEVEL_COLUMNS as _ALBUM_COLS,
        )
        for _col in _ALBUM_COLS:
            _val = album_metadata.get(_col)
            if _val is not None and str(_val).strip() != "":
                _album_meta[_col] = _val
    queue_ids: list[int] = []
    in_library = 0
    already_queued = 0
    duplicate = 0
    total_tracks = len(tracks or [])
    already_active = 0
    normalized_source = (queue_source or "soulseek").strip().lower()
    if normalized_source not in ("soulseek",):
        normalized_source = "soulseek"

    def _result(reason: str | None) -> dict[str, Any]:
        return {
            "queue_ids": queue_ids,
            "queued": bool(queue_ids),
            "queued_count": len(queue_ids),
            "reason": reason,
            "message": _QUEUE_SKIP_MESSAGES.get(reason or "", ""),
            "total_tracks": total_tracks,
            "already_active": already_active,
            "in_library": in_library,
            "already_queued": already_queued,
            "duplicate": duplicate,
        }

    try:
        with db_session() as session:
            import_group = f"mbid_{release_id}"
            # ⚠️ MUST be the blocking set, not a hand-written list.
            #
            # This used to be a local set that ALSO contained the TERMINAL
            # statuses completed/imported/in_collection/unmatched. Those rows
            # are filtered out of the queue listing, so a release whose tracks
            # had all been imported reported "already_active" — the user saw
            # "a release already has items in the queue" while the queue was
            # empty of them, and nothing could be re-added.
            #
            # "Already in the library" is a DIFFERENT, intentional skip with its
            # own message (``all_in_library``), decided below per track.
            from services.queue.queue_constraints import BLOCKING_REQUEUE_STATUSES

            _active_statuses = set(BLOCKING_REQUEUE_STATUSES)
            # Same set, rendered for the per-track duplicate probe below. Both
            # MUST use the shared constant: they were two hand-written lists
            # that had already drifted from each other, and both wrongly
            # blocked on terminal statuses (see BLOCKING_REQUEUE_STATUSES).
            _blocking_status_sql = ", ".join(
                f"'{s}'" for s in sorted(BLOCKING_REQUEUE_STATUSES)
            )
            try:
                _existing_rows = session.execute(
                    text("""
                        SELECT id, status FROM download_queue
                        WHERE import_group = :grp OR release_id = :rid
                    """),
                    {"grp": import_group, "rid": release_id},
                ).fetchall() or []
                
                _active_rows = [
                    r for r in _existing_rows
                    if str((getattr(r, "_mapping", None) or {}).get("status") or r[1] or "").lower() in _active_statuses
                ]
                if _active_rows:
                    logger.info("Release already has active queue items — skipping re-queue", release_id=release_id, active_count=len(_active_rows))
                    already_active = len(_active_rows)
                    return _result("already_active")

                # ⚠️ STRICTLY the dead-end statuses — never the whole
                # non-active remainder. ``_existing_rows`` covers EVERY row for
                # this release, so deriving the purge from "whatever is not
                # blocking" would DELETE the user's imported/completed history
                # for the release the moment they re-queued it.
                #
                # These six are the ones a re-queue legitimately supersedes: the
                # download is gone or was rejected, so a fresh attempt replaces
                # the row rather than leaving a dead one behind.
                _SUPERSEDED_STATUSES = {"removed", "cancelled", "deleted", "failed"}
                _stale_ids = [
                    (getattr(r, "_mapping", None) or {}).get("id") or r[0]
                    for r in _existing_rows
                    if str((getattr(r, "_mapping", None) or {}).get("status") or r[1] or "").lower()
                    in _SUPERSEDED_STATUSES
                ]
                if _stale_ids:
                    _in_placeholders = ", ".join(f":sid_{i}" for i in range(len(_stale_ids)))
                    session.execute(
                        text(f"DELETE FROM download_queue WHERE id IN ({_in_placeholders})"),
                        {f"sid_{i}": sid for i, sid in enumerate(_stale_ids)},
                    )
            except Exception as _cleanup_exc:
                pass

            seen_recordings: set[str] = set()

            for track in (tracks or []):
                track_title = track.get("title") or track.get("mb_title") or "Unknown Track"
                track_artist = track.get("artist") or artist
                track_number = track.get("track_number")
                if track_number in (None, ""):
                    track_number = track.get("mb_track_number")
                disc_number = track.get("disc_number", 1)
                if disc_number in (None, ""):
                    disc_number = track.get("mb_disc_number", 1)
                recording_mbid = track.get("recording_mbid") or track.get("mb_recording_mbid")

                dedupe_key = str(recording_mbid or "").strip().lower()
                if not dedupe_key:
                    dedupe_key = re.sub(r"[^a-z0-9]+", " ", track_title.lower()).strip()
                if dedupe_key in seen_recordings:
                    duplicate += 1
                    continue
                seen_recordings.add(dedupe_key)

                duration = queue_duration_seconds(
                    track.get("duration")
                    or track.get("mb_duration")
                    or track.get("length")
                )
                existing = find_library_track(artist=track_artist, title=track_title, album=album)
                if existing:
                    in_library += 1
                    continue

                _dup_row = session.execute(
                    text(f"""
                        SELECT id FROM download_queue
                        WHERE LOWER(COALESCE(artist, '')) = LOWER(:artist)
                          AND LOWER(COALESCE(title, '')) = LOWER(:title)
                          AND status IN ({_blocking_status_sql})
                        ORDER BY created_at ASC LIMIT 1
                    """),
                    {"artist": track_artist, "title": track_title},
                ).fetchone()
                if _dup_row is not None:
                    already_queued += 1
                    continue

                search_query = f"{track_artist} - {track_title}"

                _mb_meta: dict[str, Any] = {}
                if track.get("writer"): _mb_meta["writer"] = track["writer"]
                if track.get("work_mbid"): _mb_meta["work_mbid"] = track["work_mbid"]
                if track.get("work_title"): _mb_meta["work_title"] = track["work_title"]
                if track.get("work_artist"): _mb_meta["work_artist"] = track["work_artist"]
                if track.get("is_cover"):
                    _mb_meta["is_cover"] = True
                    if track.get("original_cover_artist"):
                        _mb_meta["original_cover_artist"] = track["original_cover_artist"]
                # ``musicbrainz_genres`` is a comma-joined string on the tag
                # surface; the MB release payload also carries the list form
                # as ``mb_genres``, so accept either rather than silently
                # dropping the genres.
                _genres = track.get("musicbrainz_genres")
                if not _genres and track.get("mb_genres"):
                    _genres = ", ".join(str(g) for g in track["mb_genres"] if str(g).strip())
                if _genres:
                    _mb_meta["musicbrainz_genres"] = _genres

                # Album-scoped release metadata, persisted so the import can
                # apply the full release identity without re-querying MB.
                if _album_meta:
                    _mb_meta["album_metadata"] = dict(_album_meta)

                result = session.execute(
                    text("""
                        INSERT INTO download_queue
                        (
                            artist, album, title, search_query, source, status,
                            release_id, import_group, track_number, disc_number,
                            album_artist, recording_mbid, duration, year, release_year,
                            metadata, created_at, updated_at
                        )
                        VALUES
                        (
                            :artist, :album, :title, :search_query, :source, 'queued',
                            :release_id, :import_group, :track_number, :disc_number,
                            :album_artist, :recording_mbid, :duration, :year, :release_year,
                            :metadata, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        RETURNING id
                    """),
                    {
                        "artist": track_artist, "album": album, "title": track_title,
                        "search_query": search_query, "source": normalized_source,
                        "release_id": release_id, "import_group": import_group,
                        "track_number": track_number, "disc_number": disc_number,
                        "album_artist": album_artist or artist, "recording_mbid": recording_mbid,
                        "duration": duration, "year": str(year) if year else None,
                        "release_year": year, "metadata": json.dumps(_mb_meta) if _mb_meta else "{}",
                    },
                )
                
                queue_id = result.scalar_one_or_none()
                if queue_id is not None:
                    queue_ids.append(int(queue_id))

        if queue_ids:
            try:
                signal_new_item()
            except Exception:
                pass
            return _result(None)
        if not total_tracks:
            return _result("no_tracks")
        if in_library and already_queued:
            return _result("all_present")
        if in_library:
            return _result("all_in_library")
        if already_queued:
            return _result("already_queued")
        if duplicate:
            return _result("all_duplicate")
        return _result("nothing_queued")

    except Exception as e:
        logger.error("Failed adding tracks to queue", error=str(e), exc_info=True)
        raise


def add_release_tracks_to_queue(
    release_id: str,
    tracks: list[dict[str, Any]],
    artist: str,
    album: str,
    album_artist: str | None = None,
    queue_source: str = "soulseek",
    year: int | None = None,
    album_metadata: dict[str, Any] | None = None,
) -> list[int]:
    """Add normalized tracks to the download queue, returning the new row ids.

    Thin wrapper over :func:`add_release_tracks_to_queue_detailed` for callers
    that only need the ids. Prefer the detailed form when the user must be told
    why nothing was queued.
    """
    result = add_release_tracks_to_queue_detailed(
        release_id,
        tracks,
        artist,
        album,
        album_artist=album_artist,
        queue_source=queue_source,
        year=year,
        album_metadata=album_metadata,
    )
    return list(result.get("queue_ids") or [])


def handle_unmatched_file(file_path: str, file_metadata: dict[str, Any]) -> dict[str, Any] | None:
    """File found in /downloads but doesn't match queue."""
    artist = file_metadata.get("artist", "").strip()
    title = file_metadata.get("title", "").strip()
    album = file_metadata.get("album", "").strip()

    if not artist or not title:
        logger.warning("Cannot add unmatched file without artist/title", path=file_path)
        return None

    queue_id = add_to_queue(
        artist=artist,
        title=title,
        album=album,
        source="local",
        status="unmatched",
        matched_file_path=file_path,
    )

    if queue_id:
        qid = queue_id.get("id") if isinstance(queue_id, dict) else queue_id
        logger.info("Added unmatched file to queue", artist=artist, title=title, queue_id=qid)

    return queue_id


# =============================================================================
# QUEUE STATE OPERATIONS
# =============================================================================

def queue_requeue(queue_id: int) -> Dict[str, Any]:
    updated = requeue_queue_item(queue_id) or update_queue_item(
        queue_id, status="queued"
    )
    if not updated:
        return {"success": False, "error": "Queue item not found"}
    return {"success": True, "queue_id": queue_id, "status": "queued"}


def queue_force_start(queue_id: int) -> Dict[str, Any]:
    try:
        item = get_queue_item(queue_id)
        if not item:
            return {"success": False, "error": "Queue item not found"}
        if item.get("file_path"):
            return {"success": False, "error": "Item already has a downloaded file — use Transfer or Match instead"}
        if str(item.get("status") or "").lower() in ("completed", "imported", "in_collection"):
            return {"success": False, "error": "Item is already completed"}
        if str(item.get("source") or "").lower() in ("local", "discovered"):
            return {"success": False, "error": "Item is a local/discovered file — use Transfer or Match instead of searching"}

        updated = update_queue_item(
            queue_id,
            status="queued",
            retry_count=0,
            failure_reason=None,
            next_retry_at=None,
        )
        return {
            "success": bool(updated),
            "queue_id": queue_id,
            "status": "queued",
            "message": f"Item #{queue_id} forced back to active processing",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_requeue_all_unmatched() -> Dict[str, Any]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'unmatched'
                      AND LOWER(COALESCE(source, '')) NOT IN ('local', 'discovered')
                """)
            )
            count = int(result.rowcount or 0)
        return {"success": True, "updated_count": count}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_retry_all_failed() -> Dict[str, Any]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'failed'
                """)
            )
            count = int(result.rowcount or 0)
        return {"success": True, "updated_count": count}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_clear(data: Dict[str, Any]) -> Dict[str, Any]:
    """Delete queue rows.

    ``filters.status`` selects ONE status to delete (``{"status": "imported"}``
    is how the "Clear Imported" action works).

    ⚠️ The default branch deliberately EXCLUDES ``imported`` — those rows are
    the record of what has already been moved into the library, and wiping them
    as a side effect of a routine "clear the queue" would be destructive. That
    exclusion was previously the ONLY behaviour, with no way to remove the rows
    at all; ``filters.status`` is the deliberate, explicit escape hatch.

    ``filters.statuses`` accepts a LIST for multi-status clears (e.g. the
    terminal-history pair ``["imported", "in_collection"]``).
    """
    try:
        filters = data.get("filters", {}) or {}

        # ⚠️ Validate against the known statuses. The value is interpolated into
        # a bound parameter (so this is not injection), but an unknown status
        # silently deletes NOTHING while still reporting success — which reads
        # as "cleared" to the user. Reject it instead.
        from services.queue.queue_constraints import ALL_QUEUE_STATUSES

        requested = filters.get("statuses")
        if requested is None and filters.get("status"):
            requested = [filters["status"]]
        if requested is not None:
            if not isinstance(requested, (list, tuple, set)):
                return {"success": False, "error": "filters.statuses must be a list"}
            requested = [str(s) for s in requested]
            unknown = [s for s in requested if s not in ALL_QUEUE_STATUSES]
            if unknown:
                return {
                    "success": False,
                    "error": f"Unknown queue status(es): {unknown}",
                    "valid_statuses": sorted(ALL_QUEUE_STATUSES),
                }
            if not requested:
                return {"success": False, "error": "No statuses given"}

        with db_session() as session:
            if requested:
                placeholders = ", ".join(f":st{i}" for i in range(len(requested)))
                params = {f"st{i}": s for i, s in enumerate(requested)}
                result = session.execute(
                    text(f"DELETE FROM download_queue WHERE status IN ({placeholders})"),
                    params,
                )
                deleted = int(result.rowcount or 0)
            else:
                result = session.execute(
                    text("DELETE FROM download_queue WHERE status != :status"),
                    {"status": "imported"},
                )
                deleted = int(result.rowcount or 0)

        logger.info("Queue cleared", statuses=requested or ["!= imported"], deleted=deleted)
        return {"success": True, "deleted": deleted, "statuses": requested or None}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_status(args: Any = None) -> Dict[str, Any]:
    try:
        limit = 200
        if args and hasattr(args, "get"):
            try:
                limit = min(int(args.get("limit", 200)), 500)
            except (TypeError, ValueError):
                pass
        active = get_active_queue(limit=limit)
        completed = get_completed_queue(limit=min(limit, 50))
        counts = get_queue_status_counts()
        return {
            "success": True,
            "active": active,
            "completed": completed,
            "newly_completed": [],
            "total_active": len(active),
            "total_completed": len(completed),
            **counts,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_update(queue_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        field_map = {
            "status": "status",
            "priority": "priority",
            "artist": "artist",
            "title": "title",
            "album": "album",
            "album_artist": "album_artist",
            "year": "year",
        }
        kwargs = {}
        for json_key, col_name in field_map.items():
            if json_key in payload:
                kwargs[col_name] = payload[json_key]

        updated = update_queue_item(queue_id, **kwargs)
        return {"success": updated is not None, "item": updated}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_imported(args: Any = None) -> Dict[str, Any]:
    try:
        limit = 50
        if args and hasattr(args, "get"):
            try:
                limit = min(int(args.get("limit", 50)), 200)
            except (TypeError, ValueError):
                pass
        items = get_completed_queue(limit=limit)
        return {"success": True, "items": items}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_delete(queue_id: int, delete_download_file: bool = False) -> Dict[str, Any]:
    try:
        if delete_download_file:
            item = get_queue_item(queue_id)
            if item:
                file_path = (
                    item.get("file_path") or item.get("music_file_path")
                    or item.get("matched_file_path") or item.get("found_filename") or ""
                )
                file_path = str(file_path or "").replace("\\", "/")
                if file_path and os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
        deleted = _delete_from_db(queue_id)
        return {"success": bool(deleted), "deleted": deleted}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_cancel(queue_id: int) -> Dict[str, Any]:
    try:
        item = get_queue_item(queue_id)
        if not item:
            return {"success": False, "error": "Queue item not found"}

        status = str(item.get("status") or "").lower()
        found_filename = (item.get("found_filename") or "").strip()
        
        if found_filename or status in ("downloading", "searching", "processing"):
            try:
                client = get_slskd_client()
                if client is not None:
                    slskd = SlskdService(http_client=client)
                    for transfer in slskd.get_active_downloads():
                        filename = (transfer.get("filename") or "").strip()
                        if filename and (
                            filename.replace("\\", "/").lower()
                            == (found_filename or "").replace("\\", "/").lower()
                        ):
                            transfer_id = str(transfer.get("id") or "")
                            username = str(transfer.get("username") or "")
                            if transfer_id and username:
                                slskd.cancel_download(username, transfer_id, remove=True)
                            break
            except Exception as exc:
                logger.debug("Cancel transfer best-effort failed", queue_id=queue_id, error=str(exc))

        updated = update_queue_item(
            queue_id,
            status="failed",
            failure_reason="Cancelled by user",
        )
        return {"success": updated is not None, "queue_id": queue_id, "status": "failed"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_purge_all() -> Dict[str, Any]:
    return purge_all()


# =============================================================================
# PROCESSING & ORGANIZATION
# =============================================================================

def build_organize_group_target_path(
    music_root: str | os.PathLike[str],
    album_artist: str,
    year: Any,
    album_name: str,
    artist: str,
    title: str,
    track_number: Any,
    source_file: str | os.PathLike[str],
) -> Path:
    ext = Path(source_file).suffix.lower()
    track_prefix = f"{int(track_number):02d} - " if track_number is not None else ""
    album_folder = f"({year}) {album_name}" if year else album_name or "Unknown Album"
    relative_path = Path(album_artist or artist or "Unknown Artist") / album_folder
    return Path(music_root) / relative_path / f"{track_prefix}{title}{ext}"


def organize_group_sync(group_id: Any, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Organize all completed queue items in an import group."""
    metadata = metadata or {}
    group_id = str(group_id)

    items = get_completed_group_queue_items(group_id)
    if not items:
        return {"success": False, "error": "No completed items found for this group"}

    album_artist = str(metadata.get("album_artist") or metadata.get("artist", "") or "").strip()
    year = str(metadata.get("year", "") or "").strip()
    album_name = str(metadata.get("album", "") or "").strip()
    artist_match_threshold = float(os.environ.get("QUEUE_ARTIST_MATCH_THRESHOLD", "0.78"))

    logger.info("Group organization started", group_id=group_id, album_artist=album_artist, album=album_name, year=year)

    updated_count = 0
    errors: list[str] = []

    for item in items:
        try:
            item_id = int(item.get("id") or 0)
            file_path = item.get("file_path")
            item_artist = item.get("artist") or ""
            item_title = item.get("title") or item.get("album") or ""
            item_track_number = item.get("track_number")
            item_disc_number = item.get("disc_number")
            item_album = item.get("album") or ""
            item_album_artist = item.get("album_artist") or ""
            item_year = item.get("year") or ""

            if not file_path or not os.path.exists(file_path):
                error_msg = f"File not found at {file_path}"
                errors.append(f"{item_title}: {error_msg}")
                continue

            resolved_album_artist = album_artist or item_album_artist or item_artist
            resolved_album_name = album_name or item_album or ""
            resolved_year = year or item_year

            try:
                with db_session() as session:
                    result = session.execute(
                        text("""
                            SELECT r.release_title, r.artist, r.release_year,
                                   rt.track_number, rt.track_title, rt.track_artist
                            FROM musicbrainz_release_tracks rt
                            JOIN musicbrainz_releases r ON r.release_id = rt.release_id
                            WHERE rt.queue_id = :id
                            LIMIT 1
                        """),
                        {"id": item_id},
                    )
                    mb_row = result.fetchone()
                if mb_row:
                    mapping = getattr(mb_row, "_mapping", None)
                    resolved_album_name = (mapping.get("release_title") if mapping else mb_row[0]) or resolved_album_name
                    resolved_album_artist = (mapping.get("artist") if mapping else mb_row[1]) or resolved_album_artist
                    resolved_year = (mapping.get("release_year") if mapping else mb_row[2]) or resolved_year
                    item_track_number = mapping.get("track_number") if mapping else mb_row[3]
                    item_title = (mapping.get("track_title") if mapping else mb_row[4]) or item_title
                    item_artist = (mapping.get("track_artist") if mapping else mb_row[5]) or item_artist
            except Exception:
                pass

            try:
                embedded_metadata = read_mp3_metadata(file_path) or {}
            except Exception:
                embedded_metadata = {}

            expected_artist = item_artist or resolved_album_artist
            artist_candidates = [embedded_metadata.get("artist"), embedded_metadata.get("album_artist")]
            scored_candidates = [
                (str(candidate), calculate_artist_similarity_score(expected_artist, candidate))
                for candidate in artist_candidates
                if candidate and str(candidate).strip()
            ]

            if not scored_candidates:
                error_msg = "Artist metadata check failed (no embedded tags found)"
                errors.append(f"{item_title}: {error_msg}")
                update_queue_item(item_id, status="failed", failure_reason=error_msg)
                continue

            best_candidate, best_score = max(scored_candidates, key=lambda entry: entry[1])
            if best_score < artist_match_threshold:
                error_msg = f"Artist metadata mismatch (expected='{expected_artist}', found='{best_candidate}')"
                errors.append(f"{item_title}: {error_msg}")
                update_queue_item(item_id, status="failed", failure_reason=error_msg)
                continue

            update_queue_item(
                item_id,
                artist=item_artist,
                title=item_title,
                album=resolved_album_name,
                album_artist=resolved_album_artist,
                year=resolved_year,
                track_number=item_track_number,
            )

            file_metadata = {
                "title": item_title,
                "artist": item_artist,
                "album_artist": resolved_album_artist,
                "album": resolved_album_name,
                "year": resolved_year,
                "track_number": item_track_number,
                "disc_number": item_disc_number,
            }
            update_file_metadata(file_path, file_metadata)

            music_root = os.environ.get("MUSIC_ROOT", "/music")
            target_path = build_organize_group_target_path(
                music_root=music_root,
                album_artist=resolved_album_artist,
                year=resolved_year,
                album_name=resolved_album_name,
                artist=item_artist,
                title=item_title,
                track_number=item_track_number,
                source_file=file_path,
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if not target_path.exists():
                shutil.copy2(file_path, target_path)

            update_queue_item(
                item_id,
                status="imported",
                music_file_path=str(target_path),
                copied_individually=1,
                copied_individually_at=os.path.getmtime(target_path) if os.path.exists(target_path) else None,
            )
            updated_count += 1

        except Exception as exc:
            errors.append(f"{item.get('title') or item.get('album') or 'Unknown'}: {exc}")

    logger.info("Group organization complete", organized=updated_count, total=len(items))
    return {
        "success": True,
        "organized": updated_count,
        "total": len(items),
        "errors": errors,
        "message": f"Organized {updated_count}/{len(items)} files",
    }


def process_completed_queue_item(queue_item: Dict[str, Any]) -> Dict[str, Any]:
    try:
        queue_id = queue_item.get("id")
        file_path = queue_item.get("file_path")

        if queue_id is None:
            return {"success": False, "error": "Missing queue id"}
        queue_id = int(queue_id)

        if not file_path:
            return {"success": False, "error": "Missing file_path"}

        metadata = {
            "track_number": queue_item.get("track_number"),
            "artist": queue_item.get("artist"),
            "album_artist": queue_item.get("album_artist") or queue_item.get("artist"),
            "album": queue_item.get("album"),
            "year": queue_item.get("year"),
            "title": queue_item.get("title"),
            "disc_number": queue_item.get("disc_number"),
        }

        update_file_metadata(file_path, metadata)
        result = rename_and_move_file(file_path, metadata)

        if not result.get("success"):
            return result

        target_path = result.get("target_path")
        update_queue_item(queue_id, status="imported", file_path=target_path)
        return {"success": True, "target_path": target_path}

    except Exception as e:
        logger.error("Process completed queue item failed", queue_id=queue_item.get("id"), error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


def process_pending_completed_items(limit: int = 10) -> Dict[str, Any]:
    stats = {"processed": 0, "failed": 0}
    try:
        items = get_completed_queue(limit)
        for item in items:
            result = process_completed_queue_item(item)
            if result.get("success"):
                stats["processed"] += 1
            else:
                stats["failed"] += 1
        return {"success": True, "stats": stats}
    except Exception as e:
        return {"success": False, "error": str(e)}


def process_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    queue_id = item.get("id")
    if not queue_id:
        return {"success": False, "error": "missing_queue_id"}

    try:
        from services.downloads.download_pipeline_service import (
            process_queue_item as _pipeline_process,
        )

        http_client = get_slskd_client()
        if http_client is None:
            logger.warning("Soulseek unavailable — returning queue item to queue", queue_id=queue_id)
            try:
                _queue_msg = f"{(item.get('artist') or '')} - {(item.get('title') or '')} → failed: soulseek_unavailable (slskd disabled/misconfigured)"
                log_unified(f"[QUEUE] {_queue_msg}")
                log_queue_event("failed", _queue_msg, queue_id=queue_id)
            except Exception:
                pass
                
            mark_failed(queue_id, "soulseek_unavailable")
            return {"success": False, "error": "soulseek_unavailable", "queue_id": queue_id}

        slskd = SlskdService(http_client=http_client)
        result = _pipeline_process(item, slskd)
        result.setdefault("queue_id", queue_id)
        return result
    except ImportError:
        artist = (item.get("artist") or "").strip()
        title = (item.get("title") or "").strip()
        logger.warning(
            "Pipeline service not available — marking queue item as unmatched",
            queue_id=queue_id, artist=artist, title=title,
        )
        update_queue_item(queue_id, status="unmatched", notes="Pipeline unavailable")
        return {"success": True, "skipped": True, "queue_id": queue_id, "reason": "pipeline_unavailable"}
    except Exception as exc:
        logger.error("Queue item processing failed", queue_id=queue_id, error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc), "queue_id": queue_id}


def get_completed_queue_items(limit: int = 50) -> list[dict[str, Any]]:
    return get_completed_queue(limit)


def process_single_file(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        path = str((data or {}).get("path") or "").strip()
        if not path:
            return {"success": False, "error": "path is required"}
        if not os.path.isfile(path):
            return {"success": False, "error": f"File not found: {path}"}

        item = get_queue_item_by_path(path)
        if not item:
            return {"success": False, "error": "No queue item found for this file"}

        item["file_path"] = item.get("file_path") or item.get("found_filename")
        return process_completed_queue_item(item)
    except Exception as exc:
        logger.error("Process single file failed", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


def process_albums(data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    stats: Dict[str, Any] = {"checked": 0, "processed": 0, "errors": []}
    try:
        items = get_completed_queue(limit=200)

        albums: Dict[tuple, list] = {}
        for item in items:
            if item.get("status") not in ("completed", "unmatched"):
                continue
            if not (item.get("file_path") or item.get("found_filename")):
                continue
            artist = item.get("album_artist") or item.get("artist") or "Unknown"
            album = item.get("album") or ""
            key = (str(album).strip().lower(), str(artist).strip().lower())
            albums.setdefault(key, []).append(item)

        for album_items in albums.values():
            stats["checked"] += 1
            processed = True
            for item in album_items:
                item["file_path"] = item.get("file_path") or item.get("found_filename")
                result = process_completed_queue_item(item)
                if not result.get("success"):
                    processed = False
                    stats["errors"].append(str(result.get("error") or "unknown error"))
            if processed:
                stats["processed"] += 1

        return {
            "success": True,
            "stats": stats,
            "message": f"Checked {stats['checked']} albums. {stats['processed']} processed.",
        }
    except Exception as exc:
        logger.error("Process albums failed", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}
