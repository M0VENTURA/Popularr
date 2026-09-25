"""Scanning cleanup orchestration helpers.

Provides safe wrappers around repository cleanup operations for
artist row normalization, file path sanitisation, and duplicate
removal during scanning.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from db.repositories.scan_repository import (
    normalize_existing_artist_rows,
    sanitize_artist_file_paths_and_duplicates,
)
from db.repositories.tracks import delete_tracks_by_id
from helpers.logging_config import log_unified

logger = structlog.get_logger(__name__)


def normalize_existing_artist_rows_safe(*, artist_name: str, canonical_artist_name: str) -> None:
    """Best-effort wrapper around artist row normalization."""
    try:
        updated_rows = normalize_existing_artist_rows(
            canonical_artist_name=canonical_artist_name,
            aliases=[artist_name],
        )
        if updated_rows:
            logger.info(
                "Normalized existing artist rows",
                canonical_artist=canonical_artist_name,
                alias=artist_name,
                updated_rows=updated_rows,
            )
    except Exception as err:
        logger.debug("Artist row normalization skipped", canonical_artist=canonical_artist_name, error=str(err))


def sanitize_artist_rows_safe(*, canonical_artist_name: str) -> None:
    """Best-effort stale path and duplicate cleanup wrapper."""
    try:
        summary = sanitize_artist_file_paths_and_duplicates(canonical_artist_name)
        path_updates, duplicates_removed = normalize_sanitize_summary(summary)
        if path_updates or duplicates_removed:
            logger.info(
                "Sanitized artist rows and file paths",
                canonical_artist=canonical_artist_name,
                path_updates=path_updates,
                duplicates_removed=duplicates_removed,
            )
    except Exception as err:
        logger.debug("Artist row sanitization skipped", canonical_artist=canonical_artist_name, error=str(err))


def cleanup_stale_album_tracks_if_needed(
    *,
    artist_name: str,
    album_name: str,
    cached_ids_for_album: set[str],
    navidrome_tracks: list[dict[str, Any]],
) -> None:
    """Delete DB tracks that no longer exist in a Navidrome album."""
    nav_ids = {track.get("id") for track in navidrome_tracks if track.get("id")}
    stale_ids = cached_ids_for_album - nav_ids
    if not stale_ids:
        return

    # A failed DELETE must not abort the rest of the artist's cleanup (other
    # albums still need pruning), but it must NOT be silent either — the
    # repository re-raises, so log the failure loudly and carry on.
    try:
        removed = delete_tracks_by_id(stale_ids, context=f"album '{album_name}' (diff_mode)")
    except Exception as err:
        logger.error(
            "Failed to remove stale album tracks — rows were NOT deleted",
            artist=artist_name,
            album=album_name,
            stale_count=len(stale_ids),
            error=str(err),
        )
        return

    if removed:
        log_unified(f"Navidrome Import - {artist_name} - Removed {removed} stale track(s) from album '{album_name}'")


def cleanup_stale_artist_tracks_if_needed(
    *,
    artist_name: str,
    existing_track_ids: set[str],
    navidrome_track_ids: set[str],
) -> None:
    """Delete DB tracks that no longer exist in Navidrome for an artist."""
    if not existing_track_ids:
        return
    if not navidrome_track_ids:
        logger.warning(
            "Navidrome returned no track IDs — skipping stale-track cleanup to preserve existing local tracks",
            artist=artist_name,
            existing_count=len(existing_track_ids),
        )
        return

    stale_ids = existing_track_ids - navidrome_track_ids
    if not stale_ids:
        return

    try:
        removed = delete_tracks_by_id(stale_ids, context=f"artist '{artist_name}'")
    except Exception as err:
        logger.error(
            "Failed to remove stale artist tracks — rows were NOT deleted",
            artist=artist_name,
            stale_count=len(stale_ids),
            error=str(err),
        )
        return

    if removed:
        log_unified(f"Navidrome Import - {artist_name} - Removed {removed} stale track(s) no longer in library")


def cleanup_empty_artist_dirs(*, artist_name: str, canonical_artist_name: str) -> None:
    """Remove empty album directories under the artist folder."""
    try:
        music_root = os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT") or "/music"
        artist_dir = os.path.join(music_root, canonical_artist_name)
        if not os.path.isdir(artist_dir):
            return

        for dirpath, _, _ in os.walk(artist_dir, topdown=False):
            if dirpath == artist_dir or os.listdir(dirpath):
                continue
            try:
                os.rmdir(dirpath)
                log_unified(f"Navidrome Import - {artist_name} - Removed empty directory: {os.path.basename(dirpath)}")
            except OSError as err:
                logger.debug("Could not remove directory", path=dirpath, error=str(err))
    except Exception as err:
        logger.debug("Empty-folder cleanup skipped", artist=artist_name, error=str(err))


def normalize_sanitize_summary(summary: Any) -> tuple[int, int]:
    """Return (path_updates, duplicates_removed) from dict/tuple summary."""
    if isinstance(summary, dict):
        return (
            int(summary.get("path_updates", 0) or 0),
            int(summary.get("duplicates_removed", 0) or summary.get("duplicate_deletes", 0) or 0),
        )
    if isinstance(summary, tuple):
        return (
            int(summary[0] or 0) if len(summary) > 0 else 0,
            int(summary[1] or 0) if len(summary) > 1 else 0,
        )
    return 0, 0
