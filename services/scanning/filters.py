"""Pure scanning skip-decision helpers.

Pure functions for determining whether an album or artist should be
skipped during scanning based on filters, cache state, and mode flags.

Key Functions:
    - should_skip_album(): Check if an album should be skipped based on
      filter, missing flag, or diff mode.

Architecture:
    Pure functions with no side effects. No database access or state
    mutation. Used by the scanning pipeline to avoid unnecessary work.
"""

from __future__ import annotations

from typing import Any

import structlog

from helpers.normalization_service import album_names_match

logger = structlog.get_logger(__name__)


def should_skip_album(
    *,
    album_name: str,
    album_filter: str | None,
    filter_missing: bool,
    albums_needing_reimport: set[str],
    diff_mode: bool,
    changed_album_names: set[str] | None,
) -> bool:
    """Return True when an album can be skipped before fetching tracks."""
    if filter_missing and album_name not in albums_needing_reimport:
        logger.debug("Skipping album - no missing fields", album=album_name)
        return True
    if album_filter and not album_names_match(album_filter, album_name):
        logger.debug(
            "Skipping album - does not match filter",
            album=album_name,
            album_filter=album_filter,
        )
        return True
    if diff_mode and changed_album_names is not None and album_name not in changed_album_names:
        logger.debug("Skipping album - not changed in diff mode", album=album_name)
        return True
    return False


def should_skip_cached_album(
    *,
    artist_name: str,
    album_name: str,
    tracks: list[dict[str, Any]],
    cached_ids_for_album: set[str],
    force: bool,
    album_needs_reimport: bool,
    verbose: bool,
) -> bool:
    """Return True when the DB cache appears current for an album."""
    if force or album_needs_reimport or not tracks:
        return False
    if len(cached_ids_for_album) == len(tracks):
        # Equal counts — the ID comparison below decides whether the track
        # set really is unchanged.  (A strict ``>=`` here would skip albums
        # whose songs were REMOVED in Navidrome, because the DB then holds
        # MORE ids than Navidrome — leaving the removed songs in the DB.)
        navidrome_album_ids = {track.get("id") for track in tracks if track.get("id")}
        if navidrome_album_ids and navidrome_album_ids == cached_ids_for_album:
            logger.debug(
                "Skipping unchanged album (IDs match)",
                album=album_name,
                track_count=len(cached_ids_for_album),
            )
            return True
    return False
