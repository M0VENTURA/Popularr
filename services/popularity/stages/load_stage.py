"""Popularity scan: data loading stage.

Loads candidate artists/albums/tracks from the database for
processing by the popularity scan pipeline. Builds the
artist→album→track structure used by subsequent stages.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from db.repositories.library import (
    get_all_artists,
    get_scan_tracks,
)
from helpers.logging_config import log_unified
from helpers.normalization_service import album_names_match

logger = structlog.get_logger(__name__)


def _artist_key(value: str) -> str:
    """Collapse an artist name to a punctuation-free lowercase key.

    "D'Artagnan", "d'Artagnan" and "dArtagnan" all collapse to "dartagnan",
    so a targeted artist scan resolves the requested name to the stored name
    even when the library uses an apostrophe or different casing.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _resolve_artist_for_scan(artists: list[str], artist_filter: str) -> str | None:
    """Resolve ``artist_filter`` to a stored artist name.

    Tries an exact case-insensitive match first, then a punctuation-stripped
    match.  Returns ``None`` when no stored artist corresponds.
    """
    if not artist_filter:
        return None
    target = artist_filter.strip().lower()
    for artist in artists:
        if artist.strip().lower() == target:
            return artist
    target_key = _artist_key(artist_filter)
    if target_key:
        for artist in artists:
            if _artist_key(artist) == target_key:
                return artist
    return None


def _resume_artist_index(artists: list[str], resume_from: str) -> int:
    """Index of the artist a resume scan starts at (0 when not found).

    Uses the SAME punctuation-tolerant matching as the per-artist loop, so the
    SQL scope and the in-loop skip cannot disagree.
    """
    target = str(resume_from or "").lower()
    key = _artist_key(resume_from)
    for index, artist in enumerate(artists):
        if (
            artist.lower() == target
            or key == _artist_key(artist)
            or (key and len(key) >= 3 and key in _artist_key(artist))
        ):
            return index
    return 0


def load_candidates(options: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build artist → album → track structure for scan pipeline.

    ⭐ ONE QUERY. The artist/album/track structure is loaded in a single
    ordered query and grouped in memory (``get_scan_tracks``). The previous
    implementation ran ``get_all_artists`` + ``get_albums_for_artist`` +
    ``get_tracks_for_album`` per album — an N+1 that also repeated the full
    DISTINCT-artists query for every artist when the full-scan orchestrator
    called this once per artist.
    """

    injected = options.get("albums")
    if injected is None:
        logger.debug("[LOAD_STAGE] Loading candidates from database")
    else:
        logger.debug("[LOAD_STAGE] Using injected album data")
    if injected is not None:
        return injected

    artist_filter = options.get("artist_filter")
    album_filter = options.get("album_filter")
    resume_from = options.get("resume_from")
    # Explicit artist scope (letter scans). ``None`` means "no scope";
    # an empty list is honoured by the repository as "no artists".
    artist_scope = options.get("artists")

    rows: list[dict[str, Any]] = []
    resolved_artist: str | None = None

    if artist_filter:
        # Try the stored name directly first. The DISTINCT-artist listing is
        # paid only when that misses (case/punctuation variant), never once per
        # artist in a full scan.
        rows = get_scan_tracks(artist=artist_filter)
        if not rows:
            resolved_artist = _resolve_artist_for_scan(
                get_all_artists(), artist_filter
            )
            if resolved_artist:
                rows = get_scan_tracks(artist=resolved_artist)
        if not rows:
            log_unified(
                f"Popularity Scan - Artist filter '{artist_filter}' matched no artist "
                "in the library. Check the artist name / that the library has been "
                "imported."
            )
            logger.warning(
                "[LOAD_STAGE] artist_filter matched no artist in the library",
                artist_filter=artist_filter,
                resolved_artist=resolved_artist,
            )
    elif artist_scope is not None:
        rows = get_scan_tracks(artists=[str(a) for a in artist_scope])
    elif resume_from:
        # Push the RESUME POINT into the query: loading tracks for artists the
        # scan is about to skip would undo the whole point of resuming. One
        # artist-list query is far cheaper than every skipped artist's tracks.
        all_artists = get_all_artists()
        start_index = _resume_artist_index(all_artists, resume_from)
        rows = get_scan_tracks(artists=all_artists[start_index:])
    else:
        rows = get_scan_tracks()

    # Group in QUERY order. Python dicts preserve insertion order and the query
    # sorts by filing artist, then album, then disc/track — the same order the
    # per-artist/per-album queries produced.
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        artist = (
            str(row.get("album_artist") or "").strip()
            or str(row.get("artist") or "").strip()
        )
        album = str(row.get("album") or "")
        if not artist or not album.strip():
            continue
        grouped.setdefault(artist, {}).setdefault(album, []).append(row)

    try:
        from helpers.config_helpers import get_feature

        min_tracks = max(1, int(get_feature("album_skip_min_tracks", 1) or 1))
    except Exception:
        min_tracks = 1

    candidates: list[dict[str, Any]] = []

    # Resume support (legacy parity): skip artists before the resume point.
    # A fuzzy match (resume artist contained in the current artist) is also
    # accepted, matching the legacy popularity scanner.
    resume_hit = False if resume_from else True
    resume_key = _artist_key(resume_from) if resume_from else ""

    for artist, albums in grouped.items():
        if not resume_hit:
            # Punctuation-tolerant resume match: "D'Artagnan" vs "dArtagnan"
            # must resolve to the same stored artist, otherwise every artist
            # is skipped and the scan reports "No tracks found" immediately.
            if resume_from and (
                artist.lower() == str(resume_from).lower()
                or resume_key == _artist_key(artist)
                or (
                    resume_key
                    and len(resume_key) >= 3
                    and resume_key in _artist_key(artist)
                )
            ):
                resume_hit = True
                # Do NOT skip the matched artist — rescan from this point.
            else:
                continue

        for album, tracks in albums.items():
            # ⚠️ Must use the SAME rule as ``should_skip_album`` does for the
            # Navidrome import: the two gate one album scan in sequence, so an
            # exact comparison here would re-skip the very album the import
            # just accepted (Navidrome's "17" vs the page's "17: Greatest
            # Hits"), leaving the popularity stage with nothing to score.
            if album_filter and not album_names_match(album_filter, album):
                continue

            if not tracks:
                continue

            if len(tracks) < min_tracks:
                logger.debug(
                    "[LOAD_STAGE] Skipping album below minimum track count",
                    artist=artist,
                    album=album,
                    track_count=len(tracks),
                    min_tracks=min_tracks,
                )
                continue

            # Album-type resolution for the album row: prefer a CONSISTENT
            # stored type (persisted by the album stage after the MB
            # release-group lookup).  Live/alternate detection then treats the
            # matched type as authoritative and only falls back to title
            # heuristics when no type is stored yet (first scan).
            _mb_types = {
                str(t.get("musicbrainz_albumtype") or "").strip()
                for t in tracks
                if str(t.get("musicbrainz_albumtype") or "").strip()
            }
            _sp_types = {
                str(t.get("spotify_album_type") or "").strip()
                for t in tracks
                if str(t.get("spotify_album_type") or "").strip()
            }
            candidates.append({
                "artist": artist,
                "album": album,
                "album_artist": artist,
                "spotify_album_type": next(iter(_sp_types)) if len(_sp_types) == 1 else None,
                "musicbrainz_album_type": next(iter(_mb_types)) if len(_mb_types) == 1 else None,
                "tracks": tracks,
            })

    if artist_filter and not candidates:
        logger.info(
            "[LOAD_STAGE] Artist produced 0 candidate albums",
            artist_filter=artist_filter,
            resolved_artist=resolved_artist or repr(artist_filter),
        )

    return candidates
