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
    get_albums_for_artist,
    get_tracks_for_album,
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


def load_candidates(options: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build artist → album → track structure for scan pipeline.
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

    candidates: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # 1. Get artists
    # -------------------------------------------------------------------------

    artists = get_all_artists()

    # Targeted artist scans must never silently no-op.  Resolve the requested
    # name to a stored artist (tolerating case/punctuation variants such as
    # "D'Artagnan" vs "dArtagnan") so the filter finds its tracks.
    resolved_artist: str | None = None
    if artist_filter:
        resolved_artist = _resolve_artist_for_scan(artists, artist_filter)
        if resolved_artist is None:
            log_unified(
                f"Popularity Scan - Artist filter '{artist_filter}' matched no artist "
                f"in the library ({len(artists)} artist(s) present). Check the artist "
                "name / that the library has been imported."
            )
            logger.warning(
                "[LOAD_STAGE] artist_filter matched no artist in the library",
                artist_filter=artist_filter,
                present_artists=", ".join(repr(a) for a in artists[:10]),
            )

    # Resume support (legacy parity): skip artists before the resume point.
    # A fuzzy match (resume artist contained in the current artist) is also
    # accepted, matching the legacy popularity scanner.
    resume_hit = False if resume_from else True

    for artist in artists:
        # Case-insensitive match — the artist/album pages render with LOWER()
        # queries, so URL casing can differ from the stored name; a
        # case-sensitive filter here silently produces zero candidates
        # ("No tracks found") for such scans.
        if artist_filter:
            if resolved_artist is not None:
                if artist.strip().lower() != resolved_artist.strip().lower():
                    continue
            else:
                # Resolution failed — fall back to the historical comparison so
                # behaviour stays predictable (and the diagnostic above shows why).
                if artist.lower().strip() != artist_filter.lower().strip():
                    continue

        if not resume_hit:
            # Punctuation-tolerant resume match: "D'Artagnan" vs "dArtagnan"
            # must resolve to the same stored artist, otherwise every artist
            # is skipped and the scan reports "No tracks found" immediately.
            _resume_key = _artist_key(resume_from)
            if resume_from and (
                artist.lower() == resume_from.lower()
                or _resume_key == _artist_key(artist)
                or (_resume_key and len(_resume_key) >= 3 and _resume_key in _artist_key(artist))
            ):
                resume_hit = True
                # Do NOT skip the matched artist — rescan from this point.
            else:
                continue

        # ---------------------------------------------------------------------
        # 2. Get albums
        # ---------------------------------------------------------------------

        albums = get_albums_for_artist(artist)

        for album in albums:
            # ⚠️ Must use the SAME rule as ``should_skip_album`` does for the
            # Navidrome import: the two gate one album scan in sequence, so an
            # exact comparison here would re-skip the very album the import
            # just accepted (Navidrome's "17" vs the page's "17: Greatest
            # Hits"), leaving the popularity stage with nothing to score.
            if album_filter and not album_names_match(album_filter, album):
                continue

            # -----------------------------------------------------------------
            # 3. Get tracks (YOU JUST FIXED THIS ✅)
            # -----------------------------------------------------------------

            tracks = get_tracks_for_album(artist, album)

            if not tracks:
                continue

            # Skip albums with fewer tracks than the configured minimum
            # (features.album_skip_min_tracks, default 1 = no-op).
            try:
                from helpers.config_helpers import get_feature
                min_tracks = int(get_feature("album_skip_min_tracks", 1) or 1)
            except Exception:
                min_tracks = 1
            if len(tracks) < max(1, min_tracks):
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
            # release-group lookup).  Live/alternate detection then treats
            # the matched type as authoritative and only falls back to title
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
