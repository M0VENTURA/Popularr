"""Pre-search queue item sanitizer.

Queue rows created from raw downloaded filenames (pre-sanitization era) can
carry an ``Unknown`` artist and a mangled title such as
``Spice Girls - Greatest Hits - 12 - Holler_639220186280397812.flac``.
Searching and *scoring* against that verbatim target makes every clean
Soulseek candidate fail fuzzy qualification (``no_qualifying_result``),
which used to loop the search forever.

``clean_mangled_queue_item`` strips hash suffixes / extensions / track
numbers, recovers Artist/Album/Title from the filename, and persists the
cleaned values so every later cycle and the result scorer sees clean strings.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Statuses the pre-search cleaner should never touch (terminal / file-backed).
_SKIP_STATUSES = {
    "completed", "imported", "deleted", "failed", "unmatched",
    "in_collection", "matched", "moving", "downloading",
}

# Artist values that mean "no usable artist metadata".
_UNKNOWN_ARTISTS = {"unknown", "unidentified", "unidentified artist", "-"}


def _strip_filename_junk(text: str) -> str:
    """Remove slskd hash suffixes, file extensions and trailing track numbers."""
    cleaned = re.sub(r"[\s_\-]?\d{10,}\s*$", "", text).strip()
    cleaned = re.sub(r"\.(flac|mp3|m4a|wav|aac|ogg|opus)$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s*[-–]\s*\d{1,3}\s*$", "", cleaned).strip()
    return cleaned


def _recover_from_filename(title: str) -> tuple[str | None, str | None, str]:
    """Split 'Artist - Album - 01 - Title' style titles.

    Returns ``(artist, album, clean_title)`` — any of the first two may stay
    ``None`` when the pattern does not yield them.
    """
    parts = [p.strip() for p in re.split(r"\s*[-–]\s*", title) if p.strip()]
    if len(parts) >= 4:
        # Artist - Album - <trackno> - Title
        return parts[0], parts[1], parts[-1]
    if len(parts) == 3:
        if re.match(r"^\d{1,3}$", parts[1]):
            # Artist - <trackno> - Title
            return parts[0], None, parts[2]
        if re.match(r"^\d{1,3}$", parts[2]):
            # Artist - Album - <trackno> (no title segment)
            return parts[0], parts[1], title
        # Artist - Album - Title (ambiguous — treat middle as title)
        return parts[0], None, parts[1]
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return None, None, title


def _strip_duplicated_artist_prefix(artist: str, title: str) -> str | None:
    """Remove a leading "<credited artist> - " from the title, or ``None``.

    ``"Orpheus Omega / Triple Kill"`` + ``"Triple Kill - Stacy's Mom"``
    -> ``"Stacy's Mom"``.

    Delegates to the shared rule in ``normalization_service`` so the queue
    cleaner and the MusicBrainz Compare cannot disagree about the same title.
    See ``strip_credited_artist_prefix`` for the safety conditions.
    """
    from helpers.normalization_service import strip_credited_artist_prefix

    return strip_credited_artist_prefix(artist, title)


def clean_mangled_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    """Clean a queue item's artist/title/album in place and persist the result.

    Returns the (possibly mutated) item dict.  Never touches terminal or
    file-backed statuses — those belong to the matching/alignment flow.
    """
    queue_id = item.get("id")
    if not queue_id:
        return item
    if str(item.get("status") or "").lower() in _SKIP_STATUSES:
        return item

    artist = str(item.get("artist") or "").strip()
    title = str(item.get("title") or "").strip()
    album = str(item.get("album") or "").strip()
    if not title:
        return item

    needs_update = False

    # 1. Strip hash suffixes / extensions / trailing track numbers.
    clean_title = _strip_filename_junk(title)
    if clean_title != title:
        title = clean_title
        needs_update = True

    # 2. Recover Artist/Album/Title when the artist is unknown.
    if not artist or artist.lower() in _UNKNOWN_ARTISTS:
        recovered_artist, recovered_album, clean_title = _recover_from_filename(title)
        if recovered_artist:
            artist = recovered_artist
            title = clean_title
            needs_update = True
        if recovered_album and (not album or album.lower() in _UNKNOWN_ARTISTS):
            album = recovered_album
            needs_update = True

    # 3. Strip the artist when it has been duplicated into the title.
    #
    # Reported: "Orpheus Omega / Triple Kill – Triple Kill – Stacy's Mom: the
    # artist is duplicated into the title." A combined credit keeps the SECOND
    # name in the title, so the search asks for "Orpheus Omega - Triple Kill -
    # Stacy's Mom" and matches nothing. Only strip a whole credited artist that
    # the title actually starts with, so an ordinary title can never be eaten.
    if artist:
        deduped = _strip_duplicated_artist_prefix(artist, title)
        if deduped and deduped != title:
            title = deduped
            needs_update = True

    if not needs_update:
        return item

    from db.repositories.queue import update_queue_item

    try:
        update_queue_item(
            queue_id,
            artist=artist or item.get("artist"),
            title=title,
            album=album or item.get("album") or None,
        )
        logger.info(
            "🧹 Cleaned queue item %s → Artist: '%s' | Title: '%s' | Album: '%s'",
            queue_id, artist or item.get("artist"), title, album or item.get("album"),
        )
    except Exception as exc:
        logger.warning("[QUEUE_CLEAN] failed to persist cleaned metadata for %s: %s", queue_id, exc)

    item["artist"] = artist or item.get("artist")
    item["title"] = title
    item["album"] = album or item.get("album")
    return item
