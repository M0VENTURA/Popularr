"""Re-import an owned track for a queue row instead of searching Soulseek.

When a track already exists in the collection on ANOTHER album, downloading it
again is pointless: the same audio can be copied into the downloads folder and
re-imported for the target album. The normal completion pipeline then moves and
tags it exactly like a Soulseek download, so the target album gets its own,
correctly tagged copy while the source file is left untouched.

The copy is only taken when it would have been accepted as a download:

* the file must pass the configured quality gate (allowed formats and the
  quality-priority filter);
* the full metadata matcher must accept it (track artist + title, edition and
  variant compatibility, duration);
* when the queue row carries an expected duration, the file's duration must be
  known and within the shared configured tolerance — a different-length take
  is not the requested recording;
* a candidate on the TARGET album is skipped: the track is already there.

Returns ``None`` whenever nothing qualifies, so the caller falls through to the
normal Soulseek search.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from helpers.normalization_service import (
    normalize_artist,
    normalize_title_for_lookup,
    queue_duration_seconds,
    track_duration_seconds,
)

logger = structlog.get_logger(__name__)

#: Queue sources handled by their own import paths, never by a library copy.
_SKIP_SOURCES = frozenset({"local", "discovered"})

#: Queue statuses that are already terminal (never re-copy).
_TERMINAL_STATUSES = frozenset({
    "completed", "imported", "in_collection", "matched", "deleted", "removed",
})

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_component(value: Any, default: str = "Unknown") -> str:
    text_value = _UNSAFE_FILENAME.sub("_", str(value or "")).strip(" .")
    return text_value or default


def _album_form(album_name: Any) -> tuple[bool, bool]:
    """``(is_live_or_alternate, is_acoustic)`` for an album name.

    A live take must not satisfy a studio queue row just because performer and
    title agree.
    """
    try:
        from services.catalog.album_classification_service import (
            detect_live_album_type,
            is_live_or_alternate_album,
        )

        name = str(album_name or "").strip()
        return (
            bool(is_live_or_alternate_album(name)),
            detect_live_album_type(name) == "acoustic",
        )
    except Exception:
        return (False, False)


def _length_tolerance_seconds() -> float:
    try:
        from helpers.config_helpers import get_length_tolerance_seconds

        return float(get_length_tolerance_seconds())
    except Exception:
        return 5.0


def _quality_ok(file_path: str) -> tuple[bool, str]:
    """Does *file_path* satisfy the same quality gate as a download?"""
    try:
        from helpers.config_helpers import get_search_quality_config

        allowed = {
            str(value).strip().lower().lstrip(".")
            for value in (get_search_quality_config().get("allowed_formats") or set())
            if str(value).strip()
        }
    except Exception:
        allowed = set()
    extension = os.path.splitext(file_path)[1].lower().lstrip(".")
    if allowed and extension not in allowed:
        return False, f"format '{extension or '?'}' is not an allowed download format"

    try:
        from services.downloads.download_scan_service import _matches_quality_filter

        ok, reason = _matches_quality_filter(file_path)
        if not ok:
            return False, reason or "quality filter rejected the file"
    except Exception:
        pass
    return True, ""


def _file_duration_seconds(file_path: str, db_duration: Any) -> float | None:
    try:
        from helpers.metadata_reader import read_mp3_metadata

        meta = read_mp3_metadata(file_path) or {}
        if meta.get("duration_ms"):
            seconds = queue_duration_seconds(meta.get("duration_ms"))
            if seconds:
                return float(seconds)
    except Exception:
        pass
    return track_duration_seconds(db_duration)


def _candidate_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Library rows whose TRACK ARTIST + TITLE identify this queue item."""
    artist = normalize_artist(str(item.get("artist") or ""))
    title = normalize_title_for_lookup(str(item.get("title") or ""))
    if not artist or not title:
        return []

    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, title, artist, album_artist, album, file_path, duration
                    FROM tracks
                    WHERE (
                            LOWER(COALESCE(NULLIF(artist, ''), album_artist)) = :artist
                         OR LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = :artist
                    )
                      AND file_path IS NOT NULL
                      AND TRIM(file_path) <> ''
                      AND file_path NOT LIKE '__queued_for_download__%%'
                    ORDER BY id
                """),
                {"artist": artist},
            ).mappings().all() or []
    except Exception as exc:
        logger.debug("Library-copy candidate lookup failed", error=str(exc))
        return []

    candidates: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if normalize_title_for_lookup(str(row.get("title") or "")) != title:
            continue
        path = str(row.get("file_path") or "").strip()
        if not path or not os.path.isfile(path):
            continue
        candidates.append(row)
    return candidates


def try_library_copy(item: dict[str, Any]) -> dict[str, Any] | None:
    """Copy an owned copy of *item* into the downloads folder, or return None.

    A returned dict means the item was handled: its status is ``downloading``
    with ``found_filename``/``file_path`` pointing at the copied file, so the
    normal completion hook imports it into the target album.
    """
    queue_id = item.get("id")
    title = str(item.get("title") or "").strip()
    artist = str(item.get("artist") or "").strip()
    if not queue_id or not title or not artist:
        return None

    source = str(item.get("source") or "").strip().lower()
    if source in _SKIP_SOURCES:
        return None
    if str(item.get("status") or "").strip().lower() in _TERMINAL_STATUSES:
        return None
    # A row that already has a real landed file needs no copy.
    if str(item.get("music_file_path") or "").strip():
        return None

    target_album = str(item.get("album") or "").strip().casefold()
    # The length is part of the identity. With no expected length there is
    # nothing to verify, so the copy is not taken and Soulseek decides.
    expected_duration = queue_duration_seconds(item.get("duration"))
    if not expected_duration:
        return None
    tolerance = _length_tolerance_seconds()
    target_form = _album_form(item.get("album"))

    try:
        from services.downloads.download_completion_service import (
            _file_matches_queue_item,
        )
    except Exception:
        _file_matches_queue_item = None

    selected: dict[str, Any] | None = None
    for candidate in _candidate_rows(item):
        candidate_album = str(candidate.get("album") or "").strip().casefold()
        # The track is already on the target album — there is nothing to import.
        if target_album and candidate_album == target_album:
            continue
        # Studio vs live/acoustic: the same performer and title are not the
        # same recording.
        if _album_form(candidate.get("album")) != target_form:
            continue

        file_path = str(candidate.get("file_path") or "")
        quality_ok, quality_reason = _quality_ok(file_path)
        if not quality_ok:
            logger.info(
                "Library copy skipped — quality gate",
                queue_id=queue_id,
                path=file_path,
                reason=quality_reason,
            )
            continue

        actual_duration = _file_duration_seconds(
            file_path, candidate.get("duration"),
        )
        if actual_duration is None:
            logger.info(
                "Library copy skipped — no duration to verify",
                queue_id=queue_id,
                path=file_path,
            )
            continue
        if abs(float(expected_duration) - float(actual_duration)) > tolerance:
            logger.info(
                "Library copy skipped — length mismatch",
                queue_id=queue_id,
                path=file_path,
                expected=round(float(expected_duration), 1),
                found=round(float(actual_duration), 1),
                tolerance=tolerance,
            )
            continue

        if _file_matches_queue_item is not None:
            try:
                matched, _match_source = _file_matches_queue_item(file_path, item)
            except Exception as exc:
                logger.debug("Library copy metadata check failed", error=str(exc))
                matched = False
            if not matched:
                logger.info(
                    "Library copy skipped — metadata matcher rejected it",
                    queue_id=queue_id,
                    path=file_path,
                )
                continue

        selected = candidate
        break

    if selected is None:
        return None

    try:
        from services.infrastructure.filesystem_service import resolve_downloads_dir
        from db.repositories.queue import update_queue_item

        downloads_dir = resolve_downloads_dir()
        if not downloads_dir:
            return None
        os.makedirs(downloads_dir, exist_ok=True)

        source_path = str(selected.get("file_path") or "")
        extension = os.path.splitext(source_path)[1].lower() or ".mp3"
        track_number = _safe_component(item.get("track_number"), default="")
        base_name = f"{_safe_component(artist)} - {_safe_component(title)}"
        if track_number:
            base_name = f"{track_number}. {base_name}"
        destination = os.path.join(downloads_dir, f"{base_name}{extension}")
        suffix = 2
        while os.path.exists(destination):
            destination = os.path.join(
                downloads_dir, f"{base_name} ({suffix}){extension}"
            )
            suffix += 1

        shutil.copy2(source_path, destination)
        relative = os.path.relpath(destination, downloads_dir)
        update_queue_item(
            int(queue_id),
            status="downloading",
            file_path=destination,
            found_filename=relative,
        )
    except Exception as exc:
        logger.warning(
            "Library copy failed — falling back to Soulseek",
            queue_id=queue_id,
            error=str(exc),
        )
        return None

    message = (
        f"[QUEUE] {artist} - {title} → reusing the library copy from "
        f"'{selected.get('album') or 'another album'}' (no Soulseek search)"
    )
    logger.info(
        "Queue item satisfied from the library",
        queue_id=queue_id,
        source_path=source_path,
        destination=destination,
        source_album=selected.get("album"),
    )
    try:
        from helpers.logging_config import log_unified

        log_unified(message)
    except Exception:
        pass

    return {
        "success": True,
        "queue_id": queue_id,
        "source": "library_copy",
        "file_path": destination,
        "copied_from": source_path,
        "message": message,
    }
