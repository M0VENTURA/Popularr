"""Album and track classification helpers for scan/stat eligibility."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import structlog

from helpers.normalization_service import normalize_title_for_lookup

logger = structlog.get_logger(__name__)

COMPILATION_TYPES = {"compilation", "soundtrack", "various artists"}

ALBUM_TYPE_KEYS: tuple[str, ...] = (
    "musicbrainz_album_type",
    "musicbrainz_albumtype",
    "spotify_album_type",
    "album_type",
    "detected_album_type",
    "releasetype",
)


def joined_album_type(row: Any) -> str:
    if not isinstance(row, Mapping):
        return ""
    return " ".join(
        str(row.get(key) or "").strip() for key in ALBUM_TYPE_KEYS
    ).strip()


def joined_album_type_from_rows(rows: Any) -> str:
    seen: dict[str, None] = {}
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        for key in ALBUM_TYPE_KEYS:
            value = str(row.get(key) or "").strip()
            if value:
                seen.setdefault(value, None)
    return " ".join(seen)


LIVE_ALBUM_PATTERNS = [
    r"\(live\)\s*$",
    r"\[live\]\s*$",
    r"-\s*live\s*$",
    r",\s*live\s*$",
    r"\+\s*live\s*$",
    r"\s+live\s*$",
    r"\s+live\s*[\)\]]\s*$",
    r"live\s+at\b",
    r"live\s+in\b",
    r"live\s+from\b",
    r"live\s+session\b",
    r"live\s+recording\b",
    r"live\s+tour\b",
    r"\bconcert\b",
    r"\bin\s+concert\b",
    r"\bunplugged\b",
    r"\bacoustic\b",
    r"\borchestral\b",
]

ALT_TRACK_PATTERNS = [
    r"\blive\b",
    r"\bunplugged\b",
    r"\bacoustic\b",
    r"\borchestral\b",
    r"\bremix\b",
    r"\bdemo\b",
    r"\binstrumental\b",
    r"\bkaraoke\b",
    r"\bjam[- ]along\b",
    r"\balternate\b",
    r"\balt\.\b",
    r"\b(?:single|radio|album)\s+edit\b",
    r"\bfor dogs\b",
    r"\bfür hunde\b",
]


def is_compilation_type(album_type: str) -> bool:
    return any(token in (album_type or "").lower() for token in COMPILATION_TYPES)


def normalize_primary_release_type(album_type: str) -> str:
    value = (album_type or "").lower()

    if "single" in value:
        return "single"
    if "ep" in value:
        return "ep"
    if "album" in value:
        return "album"
    if "compilation" in value:
        return "compilation"

    return value.strip()


def guess_album_type_from_track_count(track_count: int | None) -> str:
    try:
        count = int(track_count or 0)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    if count <= 2:
        return "single"
    if count <= 6:
        return "ep"
    return "album"


def classify_album_type(album_row: dict[str, Any]) -> str:
    from services.catalog.release_categories import category_for_album_row
    return category_for_album_row(album_row)


def is_live_or_alternate_album(album: str) -> bool:
    album_lower = (album or "").lower()
    if "how to live" in album_lower:
        return False
    return any(re.search(pattern, album or "", re.IGNORECASE) for pattern in LIVE_ALBUM_PATTERNS)


def detect_live_album_type(album: str, album_type_from_field: str = "") -> str:
    field_text = (album_type_from_field or "").lower()
    if field_text:
        if "acoustic" in field_text or "unplugged" in field_text:
            return "acoustic"
        if "live" in field_text:
            return "live"
        if "album" in field_text and "live" not in field_text:
            return ""

    title = album or ""
    title_text = title.lower()
    if "how to live" in title_text:
        return ""

    if "acoustic" in title_text or "unplugged" in title_text:
        return "acoustic"
        
    if is_live_album_enhanced(title):
        return "live"

    return ""


def is_live_or_unplugged_track_title(title: str) -> bool:
    return any(re.search(p, title or "", re.IGNORECASE) for p in [r"\blive\b", r"\bunplugged\b"])


_LIVE_ALTERNATE_TRACK_MARKERS = [
    r"\((?:live|unplugged|acoustic|orchestral|demo|jam[- ]along|alternate)[^)]*\)",
    r"[-–—]\s*(?:live|unplugged|acoustic|orchestral|demo|jam[- ]along|alternate)\s*$",
]


def is_live_or_alternate_track_title(title: str) -> bool:
    return any(
        re.search(pattern, title or "", re.IGNORECASE)
        for pattern in _LIVE_ALTERNATE_TRACK_MARKERS
    )


_INSTRUMENTAL_TITLE_RE = re.compile(r"\binstrumental\b", re.IGNORECASE)


def is_instrumental_track_title(title: str) -> bool:
    return bool(_INSTRUMENTAL_TITLE_RE.search(title or ""))


def is_bonus_track_title(title: str) -> bool:
    return any(re.search(pattern, title or "", re.IGNORECASE) for pattern in ALT_TRACK_PATTERNS)


def should_exclude_track_from_stats(
    title: str,
    album: str = "",
    is_live: int = 0,
    album_context_live: int = 0,
    album_type: str = "",
    duration: float | None = None,
    exclude_below_seconds: float | None = None,
    exclude_title_regex: str | None = None,
) -> bool:
    if is_live or album_context_live:
        return True

    if duration is not None:
        try:
            if exclude_below_seconds is None:
                from services.popularity.popularity_config import (
                    get_exclude_from_median_below_seconds,
                )
                exclude_below_seconds = get_exclude_from_median_below_seconds()
            if float(exclude_below_seconds) > 0 and float(duration) < float(exclude_below_seconds):
                return True
        except Exception:
            pass

    if title:
        try:
            if exclude_title_regex is None:
                from services.popularity.popularity_config import (
                    get_exclude_title_regex,
                )
                exclude_title_regex = get_exclude_title_regex()
            if exclude_title_regex:
                if re.search(exclude_title_regex, title, re.IGNORECASE):
                    return True
        except Exception:
            pass

    if not (album_type or "").strip():
        if is_live_or_alternate_album(album):
            return True

    return any(re.search(pattern, title or "", re.IGNORECASE) for pattern in ALT_TRACK_PATTERNS)


_TRAILING_ALTERNATE_PHRASES = (
    r"\s*[-–—]\s*(?:live|acoustic|unplugged|demo|instrumental|orchestral|remix|karaoke|alternate)\s*$",
    r"\s+(?:live|acoustic|unplugged|demo|instrumental|orchestral|karaoke)\s*$",
    r"\s+for\s+dogs\s*$",
    r"\s+für\s+hunde\s*$",
)


def alternate_take_group_key(title: str) -> str:
    stripped = str(title or "")
    for pattern in _TRAILING_ALTERNATE_PHRASES:
        stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE)
    key = normalize_title_for_lookup(stripped)
    return key or normalize_title_for_lookup(title)


def is_alternate_or_bonus_title(title: str) -> bool:
    return bool(
        is_bonus_track_title(title)
        or is_live_or_alternate_track_title(title)
        or is_instrumental_track_title(title)
    )


def detect_alternate_takes(tracks: list[Any]) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)

    for track in tracks or []:
        title = track.get("title") if isinstance(track, dict) else str(track)
        groups[alternate_take_group_key(title)].append(track)

    grouped: dict[str, list[Any]] = {}
    for key, variants in groups.items():
        if len(variants) < 2:
            continue
        variants.sort(key=lambda t: is_alternate_or_bonus_title(
            t.get("title") if isinstance(t, dict) else str(t)
        ))
        grouped[key] = variants
    return grouped


_VA_ALBUM_ARTIST_NAMES = frozenset(
    {
        "various artists", "various artists -", "various",
        "va", "v/a", "compilation", "soundtrack",
    }
)


def classify_compilation_category(
    artist: str,
    album: str,
    tracks: list[Any],
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
    musicbrainz_album_type: str | None = None,
    type_text: str | None = None,
) -> str:
    joined = (
        type_text
        if type_text is not None
        else " ".join(
            filter(None, (spotify_album_type or "", musicbrainz_album_type or ""))
        )
    )
    type_text_lower = joined.lower()
    is_compilation_type_tag = any(
        token in type_text_lower for token in COMPILATION_TYPES
    )

    if (album_artist or "").strip().lower() in _VA_ALBUM_ARTIST_NAMES:
        return "va"

    artists = {
        str(t.get("artist", "")).strip().lower()
        for t in tracks or []
        if isinstance(t, dict) and t.get("artist")
    }
    if len(artists) >= 4 and (artist or "").lower() not in artists:
        return "va"

    if is_compilation_type_tag or detect_greatest_hits_album(album, artist):
        return "single_artist"

    return ""


def detect_compilation_album(
    artist: str,
    album: str,
    tracks: list[Any],
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
) -> bool:
    return (
        classify_compilation_category(
            artist=artist,
            album=album,
            tracks=tracks,
            album_artist=album_artist,
            spotify_album_type=spotify_album_type,
        )
        != ""
    )


_GREATEST_HITS_TOKENS = (
    "greatest hits", "best of", "anthology", "collection", "singles",
)

_GREATEST_HITS_TITLE_RE = re.compile(r"^\s*(?:the\s+)?hits\s*(?:[([].*)?$")


def detect_greatest_hits_album(album: str, artist: str) -> bool:
    text = (album or "").lower()

    if any(token in text for token in _GREATEST_HITS_TOKENS):
        return True
    return bool(_GREATEST_HITS_TITLE_RE.match(text))


_CHRISTMAS_PATTERNS = [
    r"\bchristmas\b", r"\bxmas\b", r"\bx-mas\b", r"\bholiday",
    r"\bnoel\b", r"\bsanta\b", r"\bsleigh\b", r"\bjingle\b",
    r"\bsilent night\b", r"\bholy night\b", r"\bwinter wonderland\b",
    r"\bwhite christmas\b", r"\bjingle bells\b", r"\blast christmas\b",
    r"\byule\b", r"\byuletide\b", r"\badvent\b",
]


def detect_christmas_song(track_title: str, album_title: str) -> bool:
    combined = f"{track_title or ''} {album_title or ''}".lower()
    return any(re.search(pattern, combined) for pattern in _CHRISTMAS_PATTERNS)


CHRISTMAS_GENRE_TOKENS = frozenset({
    "christmas", "xmas", "x mas", "x-mas", "holiday", "holidays",
    "noel", "yule", "yuletide", "advent", "hanukkah", "chanukah",
})


def iter_genre_tokens(genres: Any) -> list[str]:
    if not genres:
        return []

    tokens: list[str] = []
    if isinstance(genres, (list, tuple, set)):
        tokens = [str(g) for g in genres]
    else:
        raw = str(genres).strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    tokens = [str(g) for g in parsed]
            except Exception:
                tokens = []
        if not tokens:
            tokens = [g for g in re.split(r"[,;/\\|]+", raw) if g]

    out: list[str] = []
    for token in tokens:
        norm = re.sub(r"[^\w\s-]", "", str(token).strip().lower()).strip()
        if norm:
            out.append(norm)
    return out


METAL_GENRE_SUBSTRING = "metal"
METAL_GENRE_FAMILY = "metal"


def genre_tokens_contain(genres: Any, needle: str) -> bool:
    wanted = re.sub(r"[^\w\s-]", "", str(needle or "").strip().lower()).strip()
    if not wanted:
        return False
    return any(wanted in token for token in iter_genre_tokens(genres))


def is_metal_genre(genres: Any) -> bool:
    return genre_tokens_contain(genres, METAL_GENRE_SUBSTRING)


def is_christmas_genre(genres: Any) -> bool:
    return any(token in CHRISTMAS_GENRE_TOKENS for token in iter_genre_tokens(genres))


def is_christmas_track(
    *,
    title: str = "",
    album: str = "",
    genres: Any = None,
) -> bool:
    if detect_christmas_song(title, album):
        return True
    return is_christmas_genre(genres)


_LIVE_ALBUM_ENHANCED_PATTERNS = [
    r"\(live\)\s*$",
    r"\[live\]\s*$",
    r"-\s*live\s*$",
    r",\s*live\s*$",
    r"\+\s*live\s*$",
    r"\s+live\s*$",
    r"\s+live\s*[\)\]]\s*$",
    r"live\s+at\b",
    r"live\s+in\b",
    r"live\s+from\b",
    r"live\s+session",
    r"live\s+recording",
    r"live\s+tour\b",
    r"\bin\s+concert\b",
]


def is_live_album_enhanced(album_title: str) -> bool:
    if not album_title:
        return False
    text = album_title.lower()
    if "how to live" in text:
        return False
    return any(re.search(pattern, text) for pattern in _LIVE_ALBUM_ENHANCED_PATTERNS)
