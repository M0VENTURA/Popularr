"""Genre Aggregation and Normalization Service

This module handles genre collection, normalization, and aggregation from multiple
music metadata sources (MusicBrainz, Discogs, AudioDB, Last.fm).
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import get_genre_weights, get_genre_synonyms

logger = structlog.get_logger(__name__)
logging.getLogger(__name__).setLevel(logging.DEBUG)

GENRE_WEIGHTS = get_genre_weights()
GENRE_SYNONYMS = get_genre_synonyms()

_BUILTIN_SYNONYMS: dict[str, str] = {
    "goth rock": "gothic rock",
    "goth metal": "gothic metal",
    "prog rock": "progressive rock",
    "prog metal": "progressive metal",
    "alt rock": "alternative rock",
    "alt metal": "alternative metal",
    "hip hop": "hip-hop",
    "folk": "folk rock",
    "traditional folk": "folk rock",
    "folk music": "folk rock",
}

_CHRISTMAS_KEYWORDS = [
    "christmas", "xmas", "yuletide", "jingle bells", "silent night",
    "deck the halls", "winter wonderland", "feliz navidad",
    "rudolph", "santa claus", "sleigh bells", "noel", "hanukkah",
]

_SPECIFIC_TO_GENERIC: dict[str, list[str]] = {
    "metalcore": ["metal", "heavy metal", "hardcore"],
    "deathcore": ["metal", "heavy metal", "hardcore", "death metal"],
    "post-punk": ["punk", "rock"],
    "hardcore punk": ["punk"],
    "electronic rock": ["electronic", "rock"],
    "indie rock": ["rock", "alternative"],
    "alternative rock": ["rock", "alternative"],
}

_GENERIC_ROOTS: dict[str, frozenset[str]] = {
    "metal": frozenset({"metal", "heavy metal"}),
    "folk": frozenset({"folk", "traditional folk", "folk music"}),
    "rock": frozenset({"rock", "rock music", "rock & roll"}),
    "punk": frozenset({"punk", "punk rock"}),
    "goth": frozenset({"goth"}),
    "gothic": frozenset({"gothic"}),
    "industrial": frozenset({"industrial", "industrial music"}),
}
_GENERIC_ROOT_PATTERNS: dict[str, re.Pattern[str]] = {
    root: re.compile(rf"\b{re.escape(root)}\b") for root in _GENERIC_ROOTS
}


def normalize_genre(genre: Any) -> str:
    value = str(genre or "").lower().strip()
    value = _BUILTIN_SYNONYMS.get(value, value)
    return GENRE_SYNONYMS.get(value, value)


_ADMIN_GENRE_WORDS: frozenset[str] = frozenset({
    "cover", "covers", "tribute", "tributes", "tribute band",
    "live", "unplugged", "live album", "live recordings",
    "remix", "remixes", "remixed", "rework", "reworked", "mashup", "mashups",
    "demo", "demos", "mixtape", "mixtapes",
    "soundtrack", "soundtracks", "score", "scores", "original soundtrack",
    "karaoke", "instrumental", "instrumentals",
    "bootleg", "bootlegs", "unofficial", "promo", "promos", "sampler",
})


def _strip_admin_genre_markers(value: str) -> str:
    if not value:
        return ""
    text_value = value
    text_value = re.sub(r"\([^)]*\)", "", text_value)
    text_value = re.sub(r"\[[^\]]*\]", "", text_value)
    text_value = re.sub(
        r"[-–—/\\]+\s*(remaster|remastered|bonus track|bonus|edit|radio edit|album version|single version|reissue|remastered version)\s*$",
        "",
        text_value,
        flags=re.IGNORECASE,
    )
    return text_value.strip()


def is_admin_genre(genre: Any) -> bool:
    if not genre:
        return True
    value = str(genre).strip().lower()
    if not value:
        return True
    value = _strip_admin_genre_markers(value)
    if not value:
        return True
    if value in _ADMIN_GENRE_WORDS:
        return True
    return False


_JUNK_GENRE_WORDS: frozenset[str] = frozenset({
    "2010", "2011", "2012", "2013", "2014", "2015", "2016", "2017",
    "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025",
    "2000", "2001", "2002", "2003", "2004", "2005", "2006", "2007",
    "2008", "2009", "1990", "1995", "1980", "1970", "1960", "1950",
    "beautiful", "romantic", "sad", "happy", "fun", "funny", "awesome",
    "amazing", "great", "best", "favourite", "favorite", "love", "loved",
    "loving", "beauty", "sexy", "cool", "epic", "brilliant", "good",
    "nice", "perfect", "wonderful", "powerful", "emotional", "feelings",
    "feeling", "guitar", "guitars", "singer", "voice", "vocals", "vocal",
    "drums", "bass", "songs", "song", "music", "album", "band", "artist",
    "seen live", "seen live in", "live seen", "classic", "oldies",
    "underrated", "overrated", "worship", "night", "summer", "winter",
    "party", "danceable", "melancholy", "mood", "moody", "relaxing",
    "chill", "chillout", "ambient chill", "study", "sleep", "workout",
    "work", "driving", "running", "gym", "morning", "evening", "nighttime",
})
_JUNK_GENRE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d{3,4}$"),
    re.compile(r"^\d+\s*'s$"),
    re.compile(r"^(19|20)\d{2}s?$"),
    re.compile(r"^[a-z]*\d+[a-z]*$"),
    re.compile(r"^(my|the|a|an)\b", re.IGNORECASE),
)


def is_junk_genre(genre: Any) -> bool:
    if not genre:
        return True
    try:
        from helpers.config_helpers import get_config
        _cfg = get_config() or {}
        if not bool(_cfg.get("genres", {}).get("junk_filter", True)):
            return False
    except Exception:
        pass
    value = str(genre).strip().lower()
    if not value:
        return True
    if value in _JUNK_GENRE_WORDS:
        return True
    if any(p.search(value) for p in _JUNK_GENRE_PATTERNS):
        return True
    return False


def normalize_genre_for_vote(genre: Any) -> str:
    value = str(genre or "").lower().strip()
    value = _BUILTIN_SYNONYMS.get(value, value)
    value = GENRE_SYNONYMS.get(value, value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _source_weight(source: str) -> float:
    try:
        weights = get_genre_weights() or {}
    except Exception:
        weights = GENRE_WEIGHTS or {}
    if source in weights:
        return float(weights[source] or 0)
    if source == "essentia":
        return 0.01
    return 0.05


def _genre_min_weight() -> float:
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        return max(0.0, float(cfg.get("genres", {}).get("min_weight", 0.25) or 0.25))
    except Exception:
        return 0.25


def _suppress_generic_parents(genres: list[str]) -> list[str]:
    if not genres:
        return genres
    lowered = [g.lower() for g in genres]
    to_drop: set[str] = set()

    for root, generic_labels in _GENERIC_ROOTS.items():
        pattern = _GENERIC_ROOT_PATTERNS[root]
        has_specific_subgenre = any(
            pattern.search(g) and g not in generic_labels
            for g in lowered
        )
        if has_specific_subgenre:
            to_drop.update(generic_labels)

    if not to_drop:
        return genres
    return [g for g, low in zip(genres, lowered) if low not in to_drop]


def clean_conflicting_genres(genres: list[Any]) -> list[str]:
    ordered_keys: list[str] = []
    seen: set[str] = set()
    for g in genres or []:
        norm = normalize_genre(g)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        ordered_keys.append(norm)

    lowered_set = set(ordered_keys)
    cleaned: list[str] = []
    for genre in ordered_keys:
        removed = False
        for specific, generics in _SPECIFIC_TO_GENERIC.items():
            if genre in generics and specific in lowered_set:
                removed = True
                break
        if genre == "electronic" and ("punk" in lowered_set or "metal" in lowered_set):
            continue
        if not removed:
            cleaned.append(genre)

    return _suppress_generic_parents(cleaned)


def _resolve_display_name(key: str, spellings: dict[str, list[tuple[float, str]]]) -> str:
    if key not in spellings:
        return key
    candidates = spellings[key]
    best = max(candidates, key=lambda s: s[0])

    def _top_weight(forms: list[str]) -> str:
        form_weight = {s[1]: s[0] for s in candidates if s[1] in forms}
        return sorted(forms, key=lambda f: (form_weight.get(f, 0.0), len(f)))[-1]

    spaced = [s[1] for s in candidates if " " in s[1]]
    if spaced:
        return _top_weight(spaced)
    hyphenated = [s[1] for s in candidates if "-" in s[1]]
    if hyphenated:
        return _top_weight(hyphenated)
    if " " in best[1]:
        return best[1]
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", best[1]).lower()
    return split.strip() or best[1]


def _vote_genres(
    source_map: dict[str, Any] | None,
    *,
    extra_votes: dict[str, tuple[float, str]] | None = None,
) -> tuple[dict[str, float], dict[str, list[tuple[float, str]]], dict[str, set[str]]]:
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)
    source_hits: dict[str, set[str]] = defaultdict(set)

    for source, genres in (source_map or {}).items():
        base_weight = _source_weight(source)
        if isinstance(genres, str):
            try:
                genres = json.loads(genres)
            except Exception:
                genres = [genres]
        elif isinstance(genres, dict):
            genres = list(genres.values())

        for rank, genre in enumerate(genres or []):
            if is_junk_genre(genre) or is_admin_genre(genre):
                continue
            key = normalize_genre_for_vote(genre)
            if not key:
                continue
                
            decay = 1.0 / math.log2(rank + 2)
            weighted_vote = base_weight * decay
            
            votes[key] += weighted_vote
            spellings[key].append((weighted_vote, normalize_genre(genre)))
            source_hits[key].add(source)

    for key, (weight, spelling) in (extra_votes or {}).items():
        votes[key] += weight
        spellings[key].append((weight, spelling))
        source_hits[key].add("context")

    return votes, spellings, source_hits


def _context_boost_votes(context_title: str, context_album: str) -> dict[str, tuple[float, str]]:
    context_lower = f"{context_title or ''} {context_album or ''}".lower()
    boosts: dict[str, tuple[float, str]] = {}
    if any(kw in context_lower for kw in _CHRISTMAS_KEYWORDS):
        boosts["christmas"] = (2.0, "christmas")
    return boosts


def _normalize_nav_keys(nav_genres: Any) -> frozenset[str]:
    if not nav_genres:
        return frozenset()
    if isinstance(nav_genres, str):
        try:
            nav_genres = json.loads(nav_genres)
        except Exception:
            nav_genres = [nav_genres]
    keys = set()
    for g in (nav_genres if isinstance(nav_genres, (list, tuple)) else []):
        if not g or is_junk_genre(g) or is_admin_genre(g):
            continue
        key = normalize_genre_for_vote(g)
        if key:
            keys.add(key)
    return frozenset(keys)


def _rank_genres(
    votes: dict[str, float],
    spellings: dict[str, list[tuple[float, str]]],
    source_hits: dict[str, set[str]],
    *,
    max_genres: int,
    nav_keys: frozenset[str] = frozenset(),
) -> list[str]:
    min_weight = _genre_min_weight()
    qualified = [k for k, v in votes.items() if min_weight <= 0 or v >= min_weight]

    qualified.sort(
        key=lambda k: (
            len(source_hits.get(k, ())),
            votes[k],
            1 if k in nav_keys else 0,
        ),
        reverse=True,
    )

    display_names = [_resolve_display_name(k, spellings) for k in qualified]
    cleaned = clean_conflicting_genres(display_names)
    return cleaned[:max_genres]


def _append_extra_genres(genres: list[str], title: str, album: str) -> list[str]:
    title_lower = str(title or "").lower()
    context_lower = f"{title or ''} {album or ''}".lower()
    
    if bool(re.search(r"[\(\[]\s*(live|acoustic|unplugged)[^)\]]*[\)\]]\s*$", title_lower)) or \
       any(re.search(p, context_lower) for p in [r"\bconcert\b", r"\bat\s+\w+\s+(arena|stadium|hall|club|theatre|theater)"]):
        if not any(g.lower() == "live" for g in genres):
            genres.append("Live")
            
    if re.search(r"\b(cover|tribute)\b", context_lower):
        if not any(g.lower() == "cover" for g in genres):
            genres.append("Cover")
            
    return genres


def aggregate_genres(
    source_map: dict[str, Any],
    max_genres: int = 2,
    context_title: str = "",
    context_album: str = "",
    nav_genres: Any = None,
) -> list[str]:
    votes, spellings, source_hits = _vote_genres(
        source_map,
        extra_votes=_context_boost_votes(context_title, context_album),
    )
    nav_keys = _normalize_nav_keys(nav_genres)
    top_genres = _rank_genres(votes, spellings, source_hits, max_genres=max_genres, nav_keys=nav_keys)
    return _append_extra_genres(top_genres, context_title, context_album)


def get_top_genres_with_navidrome(
    sources: dict[str, Any],
    nav_genres: Any,
    title: str = "",
    album: str = "",
) -> tuple[list[str], list[str]]:
    votes, spellings, source_hits = _vote_genres(
        sources,
        extra_votes=_context_boost_votes(title, album),
    )
    nav_keys = _normalize_nav_keys(nav_genres)
    online_top = _rank_genres(votes, spellings, source_hits, max_genres=2, nav_keys=nav_keys)
    online_top = _append_extra_genres(online_top, title, album)

    nav_list = nav_genres
    if isinstance(nav_list, str):
        try:
            nav_list = json.loads(nav_list)
        except Exception:
            nav_list = [nav_list]

    nav_cleaned = sorted({
        normalize_genre(g).capitalize()
        for g in (nav_list if isinstance(nav_list, (list, tuple)) else [])
        if g and not is_junk_genre(g) and not is_admin_genre(g)
    })
    return online_top, nav_cleaned


def rank_genres_with_local_tags(
    sources: dict[str, Any],
    nav_genres: Any,
    title: str = "",
    album: str = "",
    *,
    max_genres: int = 2,
) -> list[str]:
    votes, spellings, source_hits = _vote_genres(
        sources,
        extra_votes=_context_boost_votes(title, album),
    )
    nav_keys = _normalize_nav_keys(nav_genres)
    top_genres = _rank_genres(votes, spellings, source_hits, max_genres=max_genres, nav_keys=nav_keys)
    return _append_extra_genres(top_genres, title, album)


def sync_confident_genres(
    artist: str,
    album: str,
    source_map: dict[str, Any],
    nav_genres: Any = None,
    max_genres: int = 2,
    context_title: str = "",
    context_album: str = "",
) -> list[str]:
    top = aggregate_genres(
        source_map,
        max_genres=max_genres,
        context_title=context_title or artist,
        context_album=context_album or album,
        nav_genres=nav_genres,
    )

    try:
        with db_session() as session:
            session.execute(
                text(
                    "UPDATE tracks SET genres = :genres_str "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"
                ),
                {"genres_str": ", ".join(top), "artist": artist, "album": album},
            )
    except Exception as e:
        logger.debug("Failed to sync confident genres to DB", artist=artist, album=album, error=str(e))

    return top


def adjust_genres(genres: list[str], artist_is_metal: bool = False) -> list[str]:
    adjusted = []
    for g in genres:
        g_lower = g.lower()

        if g_lower == "goth rock":
            g = "Gothic rock"
            g_lower = "gothic rock"
        elif g_lower in ("folk", "traditional folk", "folk music"):
            g = "Folk rock"
            g_lower = "folk rock"
        elif g_lower == "hip hop":
            g = "Hip-hop"
            g_lower = "hip-hop"

        if artist_is_metal:
            if g_lower in ("prog rock", "progressive rock", "prog"):
                adjusted.append("Progressive metal")
            elif g_lower == "folk rock":
                adjusted.append("Folk metal")
            elif g_lower in ("gothic rock", "goth", "gothic"):
                adjusted.append("Gothic metal")
            elif g_lower in ("industrial", "industrial rock"):
                adjusted.append("Industrial metal")
            else:
                adjusted.append(g)
        else:
            adjusted.append(g)

    adjusted = _suppress_generic_parents(adjusted)
    return list(dict.fromkeys(adjusted))
