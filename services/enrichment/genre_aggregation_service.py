"""Genre Aggregation and Normalization Service

This module handles genre collection, normalization, and aggregation from multiple
music metadata sources (MusicBrainz, Discogs, AudioDB, Last.fm, ListenBrainz, Spotify, Navidrome).

Key Responsibilities:
    - Genre name normalization and synonym resolution
    - Conflict detection and removal (e.g., "electronic" vs "punk")
    - Weighted aggregation from multiple sources
    - Top-N genre selection based on source authority and cross-source agreement
    - Dynamic 3rd genre inclusion for strong consensus
    - Rank-decay voting to penalize low-frequency meme tags
    - Native JSONB/string compatibility with robust scalar and delimiter parsing
    - Preservation of filter-friendly tags (Live, Christmas, Acoustic, etc.)
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

# NOTE: these module-level snapshots are kept only as a last-resort fallback
# for `_source_weight()` / `_genre_synonyms()` below, in case `get_config()`
# itself throws (e.g. during early startup). They are NOT the source of
# truth during normal operation - both weights and synonyms are re-read from
# live config on every call so that a hot config reload (see
# `_reload_config_before_scan()` in the scan pipeline entrypoint) takes
# effect on the next scan without requiring a process restart.
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

# Tags that are stripped from the main "genre" voting pool, 
# but safely appended at the end of the list for playlist filtering
_FILTER_TAGS: frozenset[str] = frozenset({
    "live", "acoustic", "unplugged", "remix", "remixed", 
    "instrumental", "cover", "comedy", "orchestral", 
    "symphonic", "soundtrack", "christmas", "holiday"
})

_ADMIN_GENRE_WORDS: frozenset[str] = frozenset({
    "covers", "tribute", "tributes", "tribute band",
    "live album", "live recordings", "rework", "reworked", 
    "mashup", "mashups", "demo", "demos", "mixtape", "mixtapes",
    "soundtracks", "score", "scores", "original soundtrack",
    "karaoke", "instrumentals", "bootleg", "bootlegs", 
    "unofficial", "promo", "promos", "sampler",
})


def _genre_synonyms() -> dict[str, str]:
    """Live-reloaded genre synonym map."""
    try:
        return get_genre_synonyms() or {}
    except Exception:
        return GENRE_SYNONYMS or {}


def normalize_genre(genre: Any) -> str:
    value = str(genre or "").lower().strip()
    value = _BUILTIN_SYNONYMS.get(value, value)
    return _genre_synonyms().get(value, value)


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
    # Filter tags shouldn't participate in main voting pool
    if value in _FILTER_TAGS: 
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
    value = _genre_synonyms().get(value, value)
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


def _parse_genre_input(raw: Any) -> list[str]:
    """Robust parser handling lists, dicts, JSONB scalars, and delimited strings."""
    
    def _extract(r: Any) -> list[str]:
        if not r:
            return []
        if isinstance(r, (list, tuple)):
            res = []
            for item in r:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("tag") or ""
                    if name:
                        res.extend(_extract(str(name)))
                elif item is not None:
                    res.extend(_extract(str(item)))
            return res
            
        if isinstance(r, dict):
            res = []
            for _, v in r.items():
                if isinstance(v, dict):
                    name = v.get("name") or v.get("tag") or ""
                    if name:
                        res.extend(_extract(str(name)))
                elif v is not None:
                    res.extend(_extract(str(v)))
            return res
            
        if isinstance(r, str):
            stripped = r.strip()
            if not stripped or stripped.lower() in ("[]", "{}", "null", "none"):
                return []
                
            if stripped.startswith("[") or stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, (list, dict)):
                        return _extract(parsed)
                except Exception:
                    pass
                    
            # Split strings on delimiters and ruthlessly strip ALL brackets/quotes
            parts = []
            for g in re.split(r"[,;/\\]+", stripped):
                clean_g = re.sub(r"[\[\]{}'\"“”]+", "", g).strip()
                if clean_g and clean_g.lower() not in ("null", "none"):
                    parts.append(clean_g)
            return parts
            
        return []

    raw_list = _extract(raw)
    
    seen = set()
    clean = []
    for g in raw_list:
        if not g:
            continue
        k = g.lower()
        if k not in seen:
            seen.add(k)
            clean.append(g)
            
    return clean


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
        form_weight: dict[str, float] = defaultdict(float)
        for s in candidates:
            if s[1] in forms:
                form_weight[s[1]] += s[0]
        return sorted(set(forms), key=lambda f: (form_weight.get(f, 0.0), len(f)))[-1]

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
) -> tuple[dict[str, float], dict[str, list[tuple[float, str]]], dict[str, set[str]], set[str]]:
    """Returns votes, spellings, source_hits, and any intercepted _FILTER_TAGS."""
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)
    source_hits: dict[str, set[str]] = defaultdict(set)
    intercepted_filters: set[str] = set()

    for source, raw_genres in (source_map or {}).items():
        base_weight = _source_weight(source)
        genres = _parse_genre_input(raw_genres)

        for rank, genre in enumerate(genres or []):
            lower_g = genre.strip().lower()
            
            # Intercept filter tags before junk/admin validation strips them
            if lower_g in _FILTER_TAGS or _strip_admin_genre_markers(lower_g) in _FILTER_TAGS:
                # Resolve specifically if it matches our clean set
                matched_filter = next((f for f in _FILTER_TAGS if f in lower_g), None)
                if matched_filter:
                    intercepted_filters.add(matched_filter.title())

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

    # Authoritative external / primary sources
    primary_sources = {
        "musicbrainz",
        "discogs",
        "lastfm",
        "listenbrainz",
        "spotify",
        "manual",
    }

    # --- Strict Primary Confirmation Guardrail ---
    # Navidrome and Essentia can ONLY confirm primary sources. They cannot
    # confirm each other, nor can they stand alone.
    keys_to_delete = []
    
    for k, hits in source_hits.items():
        is_primary_backed = bool(hits & (primary_sources | {"context"}))
        
        if not is_primary_backed:
            keys_to_delete.append(k)

    for k in keys_to_delete:
        del votes[k]
        del spellings[k]
        del source_hits[k]

    return votes, spellings, source_hits, intercepted_filters


def _context_boost_votes(context_title: str, context_album: str) -> dict[str, tuple[float, str]]:
    context_lower = f"{context_title or ''} {context_album or ''}".lower()
    boosts: dict[str, tuple[float, str]] = {}
    if any(kw in context_lower for kw in _CHRISTMAS_KEYWORDS):
        boosts["christmas"] = (2.0, "christmas")
    return boosts


def _normalize_nav_keys(nav_genres: Any) -> frozenset[str]:
    if not nav_genres:
        return frozenset()
    keys = set()
    for g in _parse_genre_input(nav_genres):
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
    
    # -----------------------------------------------------------------
    # DYNAMIC 3RD GENRE INCLUSION
    # -----------------------------------------------------------------
    if len(cleaned) > max_genres and max_genres >= 2:
        # Check if the genre just outside the cutoff (e.g. rank #3) is strongly corroborated
        runner_up_key = normalize_genre_for_vote(cleaned[max_genres])
        runner_up_weight = votes.get(runner_up_key, 0.0)
        runner_up_hits = len(source_hits.get(runner_up_key, set()))
        
        # Compare against the weakest included genre (e.g. rank #2)
        borderline_key = normalize_genre_for_vote(cleaned[max_genres - 1])
        borderline_weight = votes.get(borderline_key, 0.0)
        
        # Rule: If it appears in at least 2 sources and has >= 60% of the weight of #2, include it!
        if runner_up_hits >= 2 and borderline_weight > 0 and (runner_up_weight / borderline_weight) >= 0.60:
            logger.debug(f"Promoted strong runner-up genre '{cleaned[max_genres]}' past the cutoff")
            return cleaned[:max_genres + 1]

    final_list = cleaned[:max_genres]
    logger.debug("Genre ranking complete", final_genres=final_list)

    return final_list


def _append_extra_genres(genres: list[str], title: str, album: str, intercepted_filters: set[str] = None) -> list[str]:
    title_lower = str(title or "").lower()
    context_lower = f"{title or ''} {album or ''}".lower()
    
    # Base heuristic checks
    if bool(re.search(r"[\(\[]\s*(live\vert{}acoustic\vert{}unplugged)[^)\]]*[\)\]]\s*$", title_lower)) or \
       any(re.search(p, context_lower) for p in [r"\bconcert\b", r"\bat\s+\w+\s+(arena|stadium|hall|club|theatre|theater)"]):
        if intercepted_filters is not None:
            intercepted_filters.add("Live")
            
    if re.search(r"\b(cover|tribute)\b", context_lower):
        if intercepted_filters is not None:
            intercepted_filters.add("Cover")
            
    if "remaster" in title_lower or "remaster" in context_lower:
        if intercepted_filters is not None:
            intercepted_filters.add("Remaster")

    # Append all safely intercepted filter tags
    existing_lower = {g.lower() for g in genres}
    for f_tag in (intercepted_filters or []):
        if f_tag.lower() not in existing_lower:
            genres.append(f_tag)

    return genres


def aggregate_genres(
    source_map: dict[str, Any],
    max_genres: int = 2,
    context_title: str = "",
    context_album: str = "",
    nav_genres: Any = None,
) -> list[str]:
    votes, spellings, source_hits, intercepted_filters = _vote_genres(
        source_map,
        extra_votes=_context_boost_votes(context_title, context_album),
    )
    nav_keys = _normalize_nav_keys(nav_genres)
    top_genres = _rank_genres(votes, spellings, source_hits, max_genres=max_genres, nav_keys=nav_keys)
    return _append_extra_genres(top_genres, context_title, context_album, intercepted_filters)


def get_top_genres_with_navidrome(
    sources: dict[str, Any],
    nav_genres: Any,
    title: str = "",
    album: str = "",
) -> tuple[list[str], list[str]]:
    votes, spellings, source_hits, intercepted_filters = _vote_genres(
        sources,
        extra_votes=_context_boost_votes(title, album),
    )
    nav_keys = _normalize_nav_keys(nav_genres)
    online_top = _rank_genres(votes, spellings, source_hits, max_genres=2, nav_keys=nav_keys)
    online_top = _append_extra_genres(online_top, title, album, intercepted_filters)

    nav_cleaned = sorted({
        normalize_genre(g).capitalize()
        for g in _parse_genre_input(nav_genres)
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
    votes, spellings, source_hits, intercepted_filters = _vote_genres(
        sources,
        extra_votes=_context_boost_votes(title, album),
    )
    nav_keys = _normalize_nav_keys(nav_genres)
    top_genres = _rank_genres(votes, spellings, source_hits, max_genres=max_genres, nav_keys=nav_keys)
    return _append_extra_genres(top_genres, title, album, intercepted_filters)


def update_get_top_genres_with_navidrome(
    sources: dict[str, Any],
    nav_genres: Any,
    title: str = "",
    album: str = "",
) -> list[str]:
    return rank_genres_with_local_tags(sources, nav_genres, title=title, album=album)


def get_track_recommendations(artist: str, album: str) -> dict[str, Any]:
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT lastfm_tags, musicbrainz_genres, discogs_genres,
                       listenbrainz_genres, spotify_genres, essentia_genres,
                       manual_genres, navidrome_genres, audiodb_genres, wikidata_genres
                FROM tracks 
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist 
                  AND album = :album
            """),
            {"artist": artist, "album": album},
        )
        rows = result.fetchall()

    source_map: dict[str, list[str]] = {}
    column_mapping = [
        ("lastfm", 0),
        ("musicbrainz", 1),
        ("discogs", 2),
        ("listenbrainz", 3),
        ("spotify", 4),
        ("essentia", 5),
        ("manual", 6),
        ("navidrome", 7),
        ("audiodb", 8),
        ("wikidata", 9),
    ]

    for row in rows:
        for src_key, idx in column_mapping:
            val = row[idx]
            if not val:
                continue
            parsed_vals = _parse_genre_input(val)
            if parsed_vals:
                source_map.setdefault(src_key, []).extend(parsed_vals)

    recommended = aggregate_genres(source_map, max_genres=2)
    return {"success": True, "artist": artist, "album": album, "genres": recommended}


#: Columns feeding a VARIOUS-ARTISTS track's genre vote, and the source key
#: ``aggregate_genres`` weights them under.
#:
#: ``navidrome_genres`` is DELIBERATELY absent. On a compilation each track has
#: a different performer, so the album-level write is skipped (it would hand
#: one blended list to every performer) — and with no per-track writer at all,
#: a VA track's ``genres`` was frozen at whatever Navidrome imported while
#: ``musicbrainz_genres`` / ``lastfm_genres`` / … sat right next to it.
VA_GENRE_SOURCES: tuple[tuple[str, str], ...] = (
    ("musicbrainz_genres", "musicbrainz"),
    ("discogs_genres", "discogs"),
    ("audiodb_genres", "audiodb"),
    ("essentia_genres", "essentia"),
    ("listenbrainz_genres", "listenbrainz"),
    ("lastfm_genres", "lastfm"),
    ("spotify_genres", "spotify"),
    ("wikidata_genres", "wikidata"),
    ("manual_genres", "manual"),
)


def va_track_source_map(track: dict[str, Any]) -> dict[str, list[str]]:
    """One VA track's genre sources, without Navidrome's own tag.

    Pure: no aggregation, no writes, so the source set can be tested on its own.
    An empty result means "this track has nothing but Navidrome" — the caller
    must leave such a track alone rather than blank it.
    """
    source_map: dict[str, list[str]] = {}
    for column, source in VA_GENRE_SOURCES:
        values = _parse_genre_input(track.get(column))
        if values:
            source_map.setdefault(source, []).extend(values)
    return source_map


# ---------------------------------------------------------------------------
# ARTIST-LEVEL GENRE FALLBACK
#
# Requested:
#
#   "When scanning a various artists collection, if the track has no genre data
#    online, can it fall back to using the genres for the track artist? If on
#    the local db it can be grabbed from that artist or looked up on
#    Musicbrainz, discogs and last.fm"
#
# A VA track with nothing of its own was LEFT ALONE (its Navidrome value
# survived) — correct, but on a compilation that value is often the disc
# tagger's guess or empty. The PERFORMER is known, and the performer's genres
# are exactly the missing evidence: the artist's own rows elsewhere in the
# library, or the artist on MusicBrainz / Discogs / Last.fm.
# ---------------------------------------------------------------------------

#: PRIORITY order. The ranks are the SAME sources the track vote weighs, so the
#: ordering can never contradict ``genres.weights`` in config — but this is a
#: PRIORITY, not a threshold. See :func:`fallback_genres_for_artist` for why the
#: ``genres.min_weight`` rule cannot be reused here.
_ARTIST_GENRE_SOURCE_ORDER: tuple[str, ...] = (
    # The user's OWN library first: those genres were already curated by an
    # earlier scan (or by hand), which is better evidence than a fresh lookup.
    "library",
    "musicbrainz",
    "discogs",
    "lastfm",
)

#: ``{casefolded artist: [genre, ...]}`` for the life of the process.
#:
#: A VA album can carry twenty different performers and every track is visited
#: once per scan, so without this the SAME artist would be looked up again for
#: each of its tracks (and again on the next album). The lookups behind a miss
#: are: one local query, up to two MusicBrainz requests at 1 req/s, one Discogs
#: search and one Last.fm call.
_ARTIST_GENRE_FALLBACK_CACHE: dict[str, list[str]] = {}


def _artist_genres_from_library(artist: str, *, exclude_track_id: str = "") -> list[str]:
    """The artist's genres that are ALREADY in this database.

    Two places, cheapest first — neither costs a network call:

    * the other tracks credited to this artist (their ``genres`` column is
      what a previous scan resolved, so it is already aggregated);
    * ``artists.lastfm_artist_tags``, the Last.fm artist tags a scan of that
      artist's own music caches (``_fetch_artist_lastfm_tags``).
    """
    genres: list[str] = []

    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT genres FROM tracks
                    WHERE LOWER(TRIM(artist)) = LOWER(TRIM(:artist))
                      AND genres IS NOT NULL AND TRIM(genres) <> ''
                      AND CAST(id AS TEXT) <> :exclude_id
                    LIMIT 25
                """),
                {"artist": artist, "exclude_id": str(exclude_track_id or "")},
            ).fetchall()
        for row in rows:
            genres.extend(_parse_genre_input(row[0]))
    except Exception as exc:
        logger.debug("Artist genre library read failed", artist=artist, error=str(exc))

    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT lastfm_artist_tags FROM artists "
                    "WHERE LOWER(TRIM(name)) = LOWER(TRIM(:artist))"
                ),
                {"artist": artist},
            ).first()
        if row and row[0]:
            genres.extend(_parse_genre_input(row[0]))
    except Exception as exc:
        logger.debug("Artist tag cache read failed", artist=artist, error=str(exc))

    return genres


def _artist_genres_from_musicbrainz(artist: str) -> list[str]:
    """MusicBrainz artist genres.

    TWO requests, because MB exposes genres on the artist LOOKUP only — a
    search result carries no ``genres`` however ``inc`` is spelled. The search
    is exact-anchored so a same-named artist cannot answer for this one.
    """
    try:
        from services.enrichment.musicbrainz_service import get_shared_mb_client

        client = get_shared_mb_client()
        escaped = _escape_lucene(artist)
        results = client.search_artists(f'artist:"{escaped}"', limit=1) or []
        mbid = str((results[0] or {}).get("id") or "") if results else ""
        if not mbid:
            return []
        data = client.get_artist(mbid, inc="genres") or {}
        return [
            str(item.get("name"))
            for item in (data.get("genres") or [])
            if isinstance(item, dict) and item.get("name")
        ]
    except Exception as exc:
        logger.debug("MusicBrainz artist genre lookup failed", artist=artist, error=str(exc))
        return []


def _artist_genres_from_discogs(artist: str) -> list[str]:
    """Discogs artist genres (an artist-only query is artist-level)."""
    try:
        from helpers.config_helpers import get_config
        from api_clients.discogs import get_discogs_genres

        cfg = (get_config().get("api_integrations", {}) or {}).get("discogs", {}) or {}
        token = str(cfg.get("token") or "")
        if not token or not cfg.get("enabled", True):
            return []
        return list(get_discogs_genres("", artist, token=token, enabled=True) or [])
    except Exception as exc:
        logger.debug("Discogs artist genre lookup failed", artist=artist, error=str(exc))
        return []


def _artist_genres_from_lastfm(artist: str) -> list[str]:
    """Last.fm artist top tags.

    The tags are also written to ``artists.lastfm_artist_tags`` (fill-if-empty),
    the same per-artist cache a scan of that artist's own music fills. That is
    what makes the fallback CONVERGE: the next scan finds the artist in the
    local tier and stops calling Last.fm for it.
    """
    try:
        from helpers.config_helpers import get_config

        cfg = (get_config().get("api_integrations", {}) or {}).get("lastfm", {}) or {}
        api_key = str(cfg.get("api_key") or "")
        if not cfg.get("enabled") or api_key in {
            "", "your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>"
        }:
            return []
        from api_clients.lastfm import LastFmClient

        tags = LastFmClient(api_key).get_artist_top_tags(artist, limit=15) or []
        names = [
            str(tag.get("name") or "").strip()
            for tag in tags
            if isinstance(tag, dict) and str(tag.get("name") or "").strip()
        ]
        if names:
            _cache_lastfm_artist_tags(artist, names)
        return names
    except Exception as exc:
        logger.debug("Last.fm artist genre lookup failed", artist=artist, error=str(exc))
        return []


def _cache_lastfm_artist_tags(artist: str, names: list[str]) -> None:
    """Store Last.fm artist tags on the artist row, only when it has none.

    Best-effort and never overwriting: ``_fetch_artist_lastfm_tags`` treats a
    populated column as a cache hit, so a second writer must not clobber it.
    """
    if not artist or not names:
        return
    try:
        with db_session() as session:
            session.execute(
                text(
                    "UPDATE artists SET lastfm_artist_tags = :tags "
                    "WHERE LOWER(TRIM(name)) = LOWER(TRIM(:artist)) "
                    "  AND (lastfm_artist_tags IS NULL OR TRIM(lastfm_artist_tags) = '')"
                ),
                {"tags": json.dumps(names, ensure_ascii=False), "artist": artist},
            )
    except Exception as exc:
        logger.debug("Could not cache Last.fm artist tags", artist=artist, error=str(exc))


def _escape_lucene(value: str) -> str:
    """Reuse the shared Lucene escaper (a raw quote would break the query)."""
    try:
        from api_clients.musicbrainz_http import escape_lucene_special_chars

        return escape_lucene_special_chars(value)
    except Exception:
        return value.replace('"', "")


def _merge_artist_genre_sources(
    sources: dict[str, list[str]], max_genres: int = 2
) -> list[str]:
    """Rank artist-level candidates: source PRIORITY first, then vote order.

    Pure, so the ordering is testable without a database or a network.

    ⚠️ ``genres.min_weight`` is deliberately NOT applied here. That rule exists
    to stop a lone weak TRACK tag defining a track — but an artist's OWN top
    tags are the artist's genre consensus, and the fallback only runs when the
    track has nothing at all: thresholding it would mean a Last.fm-only artist
    (weight 0.10) gets no fallback, and the request would fail for exactly the
    compilations it was made for. The weight is used as the ORDER instead:
    library → MusicBrainz → Discogs → Last.fm.

    Junk and admin tags (years, "seen live", filter tags) are dropped before
    ranking — the same filters the main vote uses.
    """
    ranked: list[str] = []
    seen: set[str] = set()

    for source in _ARTIST_GENRE_SOURCE_ORDER:
        for raw in sources.get(source) or []:
            name = str(raw or "").strip()
            if not name or is_junk_genre(name) or is_admin_genre(name):
                continue
            key = normalize_genre_for_vote(name)
            if not key or key in seen:
                continue
            seen.add(key)
            ranked.append(name)
            if len(ranked) >= max_genres:
                return ranked

    return ranked


def fallback_genres_for_artist(
    artist: str,
    *,
    exclude_track_id: str = "",
    max_genres: int = 2,
) -> list[str]:
    """Genres for the PERFORMER, for a VA track that has none of its own.

    Local database FIRST — an artist already in this library has curated
    genres, and reading them costs nothing. The online lookups (MusicBrainz,
    Discogs, Last.fm) only run when the local answer is empty.

    A placeholder performer ("Various Artists" on a track that inherited the
    album credit) is NOT an artist to look up, so it returns nothing.

    Memoised per artist for the life of the process: a compilation's tracks
    share performers, and every track is visited once per scan.
    """
    artist = str(artist or "").strip()
    if not artist:
        return []

    try:
        from helpers.normalization_service import is_track_artist_placeholder

        if is_track_artist_placeholder(artist):
            return []
    except Exception:
        pass

    key = artist.casefold()
    cached = _ARTIST_GENRE_FALLBACK_CACHE.get(key)
    if cached is not None:
        return list(cached)

    sources: dict[str, list[str]] = {}
    library = _artist_genres_from_library(artist, exclude_track_id=exclude_track_id)
    if library:
        sources["library"] = library
    else:
        sources["musicbrainz"] = _artist_genres_from_musicbrainz(artist)
        sources["discogs"] = _artist_genres_from_discogs(artist)
        sources["lastfm"] = _artist_genres_from_lastfm(artist)

    resolved = _merge_artist_genre_sources(sources, max_genres=max_genres)

    # A LOOKUP THAT FOUND NOTHING IS NOT CACHED. An artist whose data is added
    # to MusicBrainz (or to the library) later must be able to answer on the
    # next scan; caching the miss would freeze it for the process's lifetime.
    if resolved:
        _ARTIST_GENRE_FALLBACK_CACHE[key] = list(resolved)
        logger.info(
            "Artist genre fallback resolved",
            artist=artist,
            genres=resolved,
            from_local=bool(library),
        )
    return resolved


def _genre_spelling_canon(value: Any) -> list[str]:
    """Genre names reduced to bare letters/digits, so ``Hip Hop`` == ``hip-hop``."""
    from services.metadata.metadata_proposal_service import _genre_names

    return sorted(
        "".join(ch for ch in name.casefold() if ch.isalnum())
        for name in _genre_names(value)
        if name
    )


def _same_genre_value(existing: Any, proposed: str) -> bool:
    """True when the track already carries these genres, in any spelling.

    ``_genre_sets_equal`` normalises case and whitespace but not punctuation,
    so MusicBrainz's ``hip-hop`` read as a change against a stored ``Hip Hop``
    and the track was rewritten — a database row and a physical file-tag
    rewrite for a difference that is only a spelling variant of one genre.
    The genres still have to MATCH; only the spelling is allowed to differ.
    """
    try:
        from services.metadata.metadata_proposal_service import _genre_sets_equal

        if _genre_sets_equal(existing, proposed):
            return True
        return _genre_spelling_canon(existing) == _genre_spelling_canon(proposed)
    except Exception:
        return str(proposed).strip().casefold() == str(existing or "").strip().casefold()


def sync_various_artists_track_genres(
    tracks: list[dict[str, Any]], album: str = ""
) -> int:
    """Give each track of a VA compilation the genres ITS OWN sources rate.

    The album-level write in ``sync_album_file_tags`` is correctly skipped for a
    compilation — it hands one blended list to every performer — but nothing
    replaced it, so a VA track's ``genres`` never moved off the Navidrome value
    the import wrote. This is the per-track replacement: each row is judged on
    its own sources and written by its own id, never album-wide.

    ``nav_genres=None`` is deliberate: Navidrome normally acts as the
    tie-breaker, and the whole point here is that it must not decide.

    Two rules keep it safe:

    * a track with no online source FALLS BACK TO ITS PERFORMER'S genres
      (:func:`fallback_genres_for_artist` — the local library first, then
      MusicBrainz / Discogs / Last.fm). Only when that is empty too is the row
      left untouched, keeping its Navidrome value rather than blanking it.
    * ``aggregate_genres`` applies ``genres.min_weight`` (0.25), so a lone
      Last.fm (0.10) / ListenBrainz (0.15) / Spotify (0.05) source cannot define
      a track by itself — the same rule every other genre path follows.
      MusicBrainz (0.40), Discogs (0.25) and Manual (0.30) each clear it alone.

    Returns the number of tracks actually updated.
    """
    updated = 0
    for track in tracks:
        track_id = track.get("id")
        if not track_id:
            continue

        source_map = va_track_source_map(track)
        if source_map:
            top_genres = aggregate_genres(
                source_map,
                max_genres=2,
                context_title=str(track.get("title") or ""),
                context_album=str(album or ""),
                nav_genres=None,
            )
            origin = "own sources"
        else:
            # The track has nothing of its own on a compilation. Its PERFORMER
            # does, though — see ``fallback_genres_for_artist``.
            top_genres = fallback_genres_for_artist(
                str(track.get("artist") or ""),
                exclude_track_id=str(track_id),
            )
            origin = "track artist"
        if not top_genres:
            continue

        genres_str = ", ".join(top_genres)
        if _same_genre_value(track.get("genres"), genres_str):
            continue

        try:
            with db_session() as session:
                session.execute(
                    text("UPDATE tracks SET genres = :genres WHERE id = :track_id"),
                    {"genres": genres_str, "track_id": track_id},
                )
            track["genres"] = genres_str
            updated += 1
            if origin == "track artist":
                logger.debug(
                    "Various-artist track genre set from its artist",
                    track_id=track_id,
                    artist=str(track.get("artist") or ""),
                    genres=genres_str,
                )
        except Exception as exc:
            logger.debug(
                "Various-artist track genre sync failed",
                track_id=track_id, error=str(exc),
            )

    if updated:
        logger.info(
            "Synced various-artist track genres",
            album=album, tracks=len(tracks), updated=updated,
        )
    return updated


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
            result = session.execute(
                text(
                    "UPDATE tracks SET genres = :genres_str "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"
                ),
                {"genres_str": ", ".join(top), "artist": artist, "album": album},
            )
        logger.info(
            "Synced confident genres, cleared prior values",
            artist=artist,
            album=album,
            updated_rows=getattr(result, "rowcount", None),
            genres=top,
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


def enrich_genres_aggressively(artist_name: str, conn: Any = None, verbose: bool = False) -> set[str]:
    genres_collected: set[str] = set()

    def _add_clean(raw_genres: list[str] | None, source: str) -> None:
        if not raw_genres:
            return
        kept = []
        for g in raw_genres:
            if not g:
                continue
            if is_junk_genre(g) or is_admin_genre(g):
                logger.debug("Filtered out junk/admin genre tag", source=source, genre=g)
                continue
            kept.append(g.lower())

        if kept:
            genres_collected.update(kept)
            if verbose:
                logger.info(f"{source} genres found", artist=artist_name, count=len(kept))

    # Discogs and MusicBrainz fallback calls removed here 
    # Genres are now read directly from the database tracks table

    try:
        from api_clients.audiodb import get_audiodb_genres
        _add_clean(get_audiodb_genres(artist_name), "AudioDB")
    except Exception as e:
        logger.debug("AudioDB genre lookup failed", artist=artist_name, error=str(e))

    try:
        from helpers.config_helpers import get_config
        lastfm_config = (get_config().get("api_integrations", {}) or {}).get("lastfm", {}) or {}
        api_key = str(lastfm_config.get("api_key") or "")
        if lastfm_config.get("enabled") and api_key not in {
            "", "your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>"
        }:
            from api_clients.lastfm import LastFmClient
            tags = LastFmClient(api_key).get_artist_top_tags(artist_name, limit=15) or []
            lastfm_genres = [
                str(tag.get("name") or "").strip()
                for tag in tags
                if isinstance(tag, dict)
            ]
            _add_clean([g for g in lastfm_genres if g], "Last.fm")
    except Exception as e:
        logger.debug("Last.fm genre lookup failed", artist=artist_name, error=str(e))

    if genres_collected:
        try:
            with db_session() as session:
                result = session.execute(
                    text(
                        "UPDATE tracks SET genres = :genres_str "
                        "WHERE artist = :artist_name AND (genres IS NULL OR genres = '')"
                    ),
                    {"genres_str": ", ".join(sorted(genres_collected)), "artist_name": artist_name},
                )
            if verbose:
                logger.info("Updated tracks with enriched genres", updated_rows=result.rowcount, artist=artist_name, genres_count=len(genres_collected))
        except Exception as e:
            logger.debug("Failed to update genres in DB", artist=artist_name, error=str(e))

    return genres_collected
