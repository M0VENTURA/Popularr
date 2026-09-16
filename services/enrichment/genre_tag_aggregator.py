"""
Genre and tag aggregation utilities for displaying tags on track, album,
and artist pages.

Handles parsing JSON tag data from database columns, aggregating across
multiple tracks, and building the ``genre_sources`` dict expected by the
album/artist/track detail templates.

Architecture:
    Pure parsing + data-shaping — no database access, no side effects.
    Callers (routes) pass in raw track data; the functions return
    source-keyed dicts ready for ``render_template(…, genre_sources=…)``.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_json_tags(json_str: str | list | dict | None) -> list[dict[str, Any]]:
    """Parse a JSON tag array or fallback safely to delimited tags without log spam."""
    if not json_str:
        return []
        
    if isinstance(json_str, (list, dict)):
        data = json_str
    else:
        if not isinstance(json_str, str):
            return []
        cleaned = json_str.strip()
        if not cleaned:
            return []
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            # Fallback for plain text or non-JSON columns without filling logs with debug errors
            return parse_delimited_tags(cleaned)
            
    if isinstance(data, dict):
        data = [data]
    elif not isinstance(data, list):
        return []
        
    normalized: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                count = int(item.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            normalized.append({"name": name, "count": count})
        elif item:
            name = str(item).strip()
            if name:
                normalized.append({"name": name, "count": 1})
    return normalized


def parse_delimited_tags(value: Any) -> list[dict[str, Any]]:
    """Parse a delimited genre string into tag dicts."""
    if not value:
        return []

    items: list[str] = []
    if isinstance(value, list):
        items = list(value)
    else:
        text = str(value).strip()
        if not text:
            return []
        text = text.replace("\\", ",").replace(";", ",")
        items = [x.strip() for x in text.split(",") if x.strip()]

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        name = str(item).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        result.append({"name": name, "count": 1})
    return result


def parse_mood_values(mood_value: Any) -> list[str]:
    """Normalize a track mood field into a list of mood labels."""
    if not mood_value:
        return []

    if isinstance(mood_value, list):
        raw_items = list(mood_value)
    else:
        text = str(mood_value).strip()
        if not text:
            return []
        raw_items = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    raw_items = parsed
            except (json.JSONDecodeError, TypeError):
                pass
        if raw_items is None:
            text = text.replace("\\", ",")
            raw_items = re.split(r"[;,]", text)

    seen: set[str] = set()
    moods: list[str] = []
    for item in raw_items or []:
        mood = str(item).strip()
        if mood and mood.lower() not in seen:
            seen.add(mood.lower())
            moods.append(mood)
    return moods


# ---------------------------------------------------------------------------
# Single-track extraction
# ---------------------------------------------------------------------------

def get_track_genre_sources(track_dict: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Extract all genre/tag sources from a single track dictionary."""
    sources: dict[str, list[dict[str, Any]]] = {}

    # JSON array sources
    for key in ("lastfm_tags", "listenbrainz_genres", "discogs_genres",
                "musicbrainz_genres", "spotify_genres"):
        if track_dict.get(key):
            parsed = parse_json_tags(track_dict[key])
            if parsed:
                sources[key] = parsed

    # Comma-separated sources (Added "genres" here)
    for key in ("genres", "essentia_genres", "navidrome_genres", "manual_genres"):
        if track_dict.get(key):
            parsed = parse_delimited_tags(track_dict[key])
            if parsed:
                sources[key] = parsed

    # Moods
    if track_dict.get("mood"):
        moods = parse_mood_values(track_dict["mood"])
        if moods:
            sources["mood"] = [{"name": m, "count": 1} for m in moods]

    return sources


# ---------------------------------------------------------------------------
# Cross-track aggregation
# ---------------------------------------------------------------------------

def aggregate_tags_with_counts(
    track_list: list[dict[str, Any]],
) -> dict[str, Counter[str]]:
    """Aggregate tags from multiple tracks, counting frequency per source."""
    aggregated: dict[str, Counter[str]] = defaultdict(Counter)

    for track in track_list:
        sources = get_track_genre_sources(track)
        for source_name, tags_list in sources.items():
            for tag in tags_list:
                tag_name = str(tag.get("name", ""))
                if not tag_name:
                    continue
                try:
                    inc = int(tag.get("count", 1))
                except (ValueError, TypeError):
                    inc = 1
                aggregated[source_name][tag_name] += inc

    return dict(aggregated)


def get_album_genre_sources(
    track_list: list[dict[str, Any]],
    limit: int = 25,
) -> dict[str, list[dict[str, Any]]]:
    """Build ``genre_sources`` for an album from its tracks."""
    aggregated = aggregate_tags_with_counts(track_list)
    return _format_aggregated(aggregated, limit)


def get_artist_genre_sources(
    track_list: list[dict[str, Any]],
    limit: int = 30,
) -> dict[str, list[dict[str, Any]]]:
    """Build ``genre_sources`` for an artist from all their tracks."""
    aggregated = aggregate_tags_with_counts(track_list)
    return _format_aggregated(aggregated, limit)


def _format_aggregated(
    aggregated: dict[str, Counter[str]],
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    """Convert aggregated Counters into sorted, limited tag dicts."""
    result: dict[str, list[dict[str, Any]]] = {}
    for source_name, counter in aggregated.items():
        result[source_name] = [
            {"name": tag, "count": count}
            for tag, count in counter.most_common(limit)
        ]
    return result
