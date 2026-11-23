"""Enforce a vocabulary entry's ``scope`` during genre selection.

⭐ WHY THIS EXISTS

A v4 vocabulary can declare, per genre, which evidence may create it:

    "scope": {
      "levels": ["track", "album"],
      "slot": "marker",
      "confirm_release_types": ["Soundtrack"],
      "confirm_album_title_patterns": ["(?i)\\b(original )?soundtrack\\b"],
      "require_confirmation": true
    }

Those rules were parsed by nobody: ``GenreEntry`` had no ``scope`` field, so the
block was discarded on load. Measured on the real selector, three track-level
"Soundtrack" tags on a plain studio album produced ``Soundtrack`` as a final
genre — exactly what ``require_confirmation`` forbids.

This module applies the four rules, in the order they matter:

1. ``levels`` — evidence at a disallowed level never contributes at all, so it
   cannot even reach the ranking as a weak candidate.
2. ``artist_requires_min_sources`` — artist-only evidence is disabled unless the
   genre has that many independent source groups. Applied here rather than at the
   end because otherwise an artist tag would corroborate itself past the
   single-source gate and then be rejected, which reads as "no genres".
3. ``require_confirmation`` — with this set, the genre needs a confirming signal
   OR one of its confirm patterns to match. Without it the confirm rules are
   pure support: they never reject, they only satisfy the requirement.
4. ``slot: "marker"`` — the genre may still be SELECTED, but it is appended after
   the musical genres and never consumes a Maximum Genres slot.

Deliberately NOT enforced: the ``scope`` of a MOOD or an attribute. Only genres
have slots, so only genres are scoped.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import structlog

from services.enrichment.genre_vocabulary import GenreScope, Vocabulary

logger = structlog.get_logger(__name__)

#: A genre is dropped by ``levels`` when none of its evidence was allowed.
REASON_LEVEL_NOT_ALLOWED = "evidence_level_not_allowed"
REASON_ARTIST_MIN_SOURCES = "artist_evidence_needs_more_sources"
REASON_UNCONFIRMED = "needs_confirmation"

#: Release-group secondary types are matched case-insensitively.
_NORMALISE = str.casefold


def _matches_any(patterns: Iterable[str], value: str) -> bool:
    text = str(value or "")
    if not text:
        return False
    for pattern in patterns or ():
        try:
            if re.search(pattern, text):
                return True
        except re.error:
            # Validation rejects these before they are saved; a hand-edited file
            # that slipped through must not take the whole scan down.
            logger.warning("Genre scope pattern is not a valid regex", pattern=pattern)
    return False


def _release_type_matches(scope: GenreScope, release_types: Iterable[str]) -> bool:
    wanted = {_NORMALISE(t) for t in scope.confirm_release_types if t}
    if not wanted:
        return False
    return any(_NORMALISE(str(rt or "")) in wanted for rt in release_types or ())


def confirmations_for(
    scope: GenreScope,
    *,
    album_title: str = "",
    track_title: str = "",
    release_types: Iterable[str] = (),
) -> list[str]:
    """Which confirm rule matched, if any. Empty means unconfirmed.

    Returned as labels rather than a bool so a caller can say WHY a genre was
    accepted — the same reason the selector reports removals.
    """
    matched: list[str] = []
    if _release_type_matches(scope, release_types):
        matched.append("release_type")
    if _matches_any(scope.confirm_album_title_patterns, album_title):
        matched.append("album_title")
    if _matches_any(scope.confirm_title_patterns, track_title):
        matched.append("track_title")
    return matched


def evidence_levels_allowed(
    scope: GenreScope,
    *,
    default_levels: Iterable[str] = ("track", "album", "artist"),
) -> frozenset[str]:
    """The levels this genre accepts. Empty ``scope.levels`` = the policy default."""
    levels = {str(level).strip().lower() for level in (scope.levels or ()) if str(level).strip()}
    if not levels:
        levels = {str(level).strip().lower() for level in default_levels if str(level).strip()}
    return frozenset(levels)


def reject_reason(
    scope: GenreScope,
    *,
    entities: Iterable[str],
    source_groups: int,
    album_title: str = "",
    track_title: str = "",
    release_types: Iterable[str] = (),
    default_levels: Iterable[str] = ("track", "album", "artist"),
) -> str:
    """Why this genre may NOT be selected, or ``""`` when it may.

    Checked in the documented order, so a genre rejected for two reasons reports
    the more fundamental one.
    """
    allowed = evidence_levels_allowed(scope, default_levels=default_levels)
    observed = {str(e).strip().lower() for e in entities if str(e).strip()}
    if observed and not (observed & allowed):
        return REASON_LEVEL_NOT_ALLOWED

    has_artist = "artist" in observed
    non_artist = bool(observed - {"artist"})
    if (
        scope.artist_requires_min_sources > 0
        and has_artist
        and not non_artist
        and source_groups < scope.artist_requires_min_sources
    ):
        return REASON_ARTIST_MIN_SOURCES

    if scope.require_confirmation and not confirmations_for(
        scope,
        album_title=album_title,
        track_title=track_title,
        release_types=release_types,
    ):
        return REASON_UNCONFIRMED

    return ""


def split_markers(
    selected: Iterable[str],
    vocab: Vocabulary,
    *,
    max_markers: int | None = None,
    order: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Split selected genres into ``(musical, markers)``.

    A ``slot: "marker"`` genre never consumes a Maximum Genres slot, so it is
    returned separately for the caller to append after the musical ones. Order
    follows the vocabulary's declared ``marker.order`` when one is given, which
    keeps the appended tail deterministic (the playlist builder reads it).
    """
    musical: list[str] = []
    markers: list[str] = []
    by_display = {entry.display.casefold(): entry for entry in vocab.genres.values()}
    by_id = vocab.genres

    for genre in selected or ():
        entry = by_display.get(str(genre or "").casefold())
        if entry is None or not entry.scope.is_marker:
            musical.append(genre)
            continue
        markers.append(genre)

    preferred = [str(gid) for gid in order or ()]
    if preferred:
        rank = {
            gid: index
            for index, gid in enumerate(preferred)
        }

        def _rank(genre: str) -> tuple[int, str]:
            entry = by_display.get(str(genre or "").casefold())
            if entry is not None and entry.id in rank:
                return (rank[entry.id], "")
            return (len(preferred), str(genre).casefold())

        markers.sort(key=_rank)

    if max_markers is not None:
        markers = markers[: max(0, int(max_markers))]

    return musical, markers
