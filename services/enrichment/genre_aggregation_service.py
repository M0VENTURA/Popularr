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
import threading
from collections import OrderedDict, defaultdict
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import get_genre_synonyms, get_genre_weights

logger = structlog.get_logger(__name__)


def _genre_track_limit() -> int:
    """How many MUSICAL genres to write onto one track (default 3).

    Read through ``helpers.config_helpers.get_genre_track_limit`` so the number
    lives in ``config.yaml`` rather than in three call sites. FAILS OPEN to 3:
    a config problem must not silently narrow every track to one genre.
    """
    try:
        from helpers.config_helpers import get_genre_track_limit

        return int(get_genre_track_limit())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Genre track limit unreadable — using 3", error=str(exc))
        return 3

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

#: The ONE token the whole holiday family collapses to.
#:
#: Christmas, Xmas, Noel, Yule, Advent, Hanukkah, Chanukah and Holiday all mean
#: the SAME thing to this pipeline — seasonal music, one playlist bucket — so
#: they merge to a single token instead of competing for genre slots. Without
#: this, an album tagged "Christmas" on one track and "Holiday" on another
#: produced two near-duplicate pools and two chips for one idea.
#:
#: "Holiday" is the umbrella deliberately: it covers Christmas AND the other
#: winter holidays. Calling the merged token "Christmas" would label a Hanukkah
#: track as Christmas.
HOLIDAY_CANONICAL_TOKEN = "Holiday"

#: Every spelling the family arrives under (lower-case, as stored).
#:
#: Matches ``album_classification_service.CHRISTMAS_GENRE_TOKENS`` — the two
#: live in different modules but must agree, so a change to one needs the other.
HOLIDAY_FAMILY_ALIASES: frozenset[str] = frozenset({
    "christmas", "xmas", "x mas", "x-mas", "holiday", "holidays",
    "noel", "yule", "yuletide", "advent", "hanukkah", "chanukah",
})

# Tags that are stripped from the main "genre" voting pool,
# but safely appended at the end of the list for playlist filtering
_FILTER_TAGS: frozenset[str] = frozenset({
    "live", "acoustic", "unplugged", "remix", "remixed",
    "remaster", "remastered", "instrumental", "cover",
    "comedy", "orchestral", "symphonic", "soundtrack",
    "christmas", "holiday",
})

#: Canonical display mapping for intercepted filter tags so variants like
#: "remixed" / "remix" or "remastered" / "remaster" resolve deterministically.
_CANONICAL_FILTER_DISPLAY: dict[str, str] = {
    "live": "Live",
    "acoustic": "Acoustic",
    "unplugged": "Unplugged",
    "remix": "Remix",
    "remixed": "Remix",
    "remaster": "Remaster",
    "remastered": "Remaster",
    "instrumental": "Instrumental",
    "cover": "Cover",
    "comedy": "Comedy",
    "orchestral": "Orchestral",
    "symphonic": "Symphonic",
    "soundtrack": "Soundtrack",
    "christmas": "Christmas",
    "holiday": HOLIDAY_CANONICAL_TOKEN,
}

#: Subset of ``_FILTER_TAGS`` that describe ONE TRACK's performance
#: (a cover, a live take, an acoustic arrangement, a remaster…). They may be
#: appended to the track whose own title/sources justify them, but must NEVER
#: be spread to every track of an album by an album-level blend — that is the
#: reported "track genres saved to album / Cover on non-cover songs".
#: The Christmas family stays album-valid: a Christmas album IS an album
#: property and the playlist builder depends on the tag surviving.
_TRACK_AFFILIATION_FILTER_TAGS: frozenset[str] = frozenset(
    _FILTER_TAGS - frozenset({"christmas", "holiday"})
)


def is_holiday_family_token(value: Any) -> bool:
    """True when ``value`` is any spelling of the holiday family."""
    return str(value or "").strip().lower() in HOLIDAY_FAMILY_ALIASES


def canonical_holiday_token(value: Any) -> str:
    """The family's single token for ``value``, or ``""`` when not holiday."""
    return HOLIDAY_CANONICAL_TOKEN if is_holiday_family_token(value) else ""


def merge_holiday_tokens(genres: list[str]) -> list[str]:
    """Collapse every holiday spelling to ONE token, keeping its position.

    Order is preserved: the family name takes the slot of the FIRST spelling
    seen, so a holiday marker that was appended last stays last and the
    playlist builder's genre window is unaffected.
    """
    merged: list[str] = []
    seen_holiday = False
    for genre in genres or []:
        if is_holiday_family_token(genre):
            if seen_holiday:
                continue
            seen_holiday = True
            merged.append(HOLIDAY_CANONICAL_TOKEN)
            continue
        merged.append(genre)
    return merged


def is_track_affiliation_tag(value: Any) -> bool:
    """True when ``value`` is a per-TRACK affiliation marker (Cover/Live/…).

    These describe ONE recording's performance, never the album. The
    holiday family is deliberately NOT one of them: a seasonal album IS an
    album property and the playlist builder relies on the tag surviving.

    ⚠️ Because the family is exempt here, it needs its OWN guard against
    spreading from one track to an album — see ``album_is_holiday_album``.
    """
    cleaned = _strip_admin_genre_markers(str(value or "").strip().lower())
    return cleaned in _TRACK_AFFILIATION_FILTER_TAGS


def is_propagatable_genre(value: Any) -> bool:
    """True when an ALBUM-level genre may be INHERITED by a track that has none.

    ⭐⭐ ONLY MUSICAL GENRES PROPAGATE.

    Two families must not spread from an album to its tracks:

    * the per-track affiliation markers (Cover/Live/Acoustic/Remix/Remaster/
      Instrumental/…) — they describe ONE recording's performance;
    * the Christmas/Holiday family — it describes the ALBUM's OCCASION, and a
      plain track on a Christmas compilation is not itself a Christmas track.

    Both used to reach every track of an album that had one cover or one
    holiday record, because the album's genre list is a FILL for a track with
    no genre evidence of its own. The reported symptom: "Holiday, Christmas,
    Cover shouldn't automatically assign to all tracks if the track doesn't
    have any genres. Only musical genres should propagate to all tracks."

    ⭐ The CHRISTMAS EXEMPTION in ``_TRACK_AFFILIATION_FILTER_TAGS`` is
    about the ALBUM's own genre list (the album IS a Christmas album, and the
    genre-playlist builder reads it from there). This rule is about a TRACK
    INHERITING that list, which is a different question — so the two do not
    conflict.
    """
    if is_track_affiliation_tag(value) or is_holiday_family_token(value):
        return False
    return not is_admin_genre(value)


def strip_track_affiliation_tags(values: Any) -> list[str]:
    """Drop every per-TRACK affiliation marker from a list of genre names.

    Used wherever an ALBUM-level (or artist-level) genre list is assembled
    from per-track sources: an affiliation belongs to the track that earned
    it, and spreading it to siblings is the reported "albums are still
    assigning the cover genre to non cover tracks".

    The input may be a list of names or any of the delimited/JSON shapes the
    genre columns use; the output is a list of plain names with the markers
    removed (case-insensitively) and order preserved.
    """
    names: list[str] = []
    for raw in _parse_genre_input(values) or []:
        if isinstance(raw, dict):
            raw = raw.get("name") or ""
        name = str(raw or "").strip()
        if not name or is_track_affiliation_tag(name):
            continue
        names.append(name)
    return names


_ADMIN_GENRE_WORDS: frozenset[str] = frozenset({
    "covers", "tribute", "tributes", "tribute band",
    "live album", "live recordings", "rework", "reworked",
    "mashup", "mashups", "demo", "demos", "mixtape", "mixtapes",
    "soundtracks", "score", "scores", "original soundtrack",
    "karaoke", "instrumentals", "bootleg", "bootlegs",
    "unofficial", "promo", "promos", "sampler",
})


#: Markers that must NEVER become a genre playlist of their own.
#:
#: ⚠️ DELIBERATELY NARROWER THAN ``_TRACK_AFFILIATION_FILTER_TAGS``. That set is
#: derived from ``_FILTER_TAGS`` and therefore also contains
#: Comedy / Orchestral / Symphonic / Soundtrack — values that are real enough to
#: be worth a playlist of their own, and which existing libraries may already
#: have pools for. Only the markers that describe a PERFORMANCE (a live take, a
#: cover, a remaster, a remix, an instrumental) are excluded from pooling, plus
#: the holiday family, which the playlist builder places with its own logic.
_PLAYLIST_POOL_EXCLUDED: tuple[str, ...] = (
    "live", "acoustic", "unplugged", "cover",
    "remix", "remixed", "remaster", "remastered", "instrumental",
)


def is_playlist_pool_genre(value: Any) -> bool:
    """May ``value`` name a genre PLAYLIST?

    False for performance markers (Cover/Live/Acoustic/Remix/Remaster/
    Instrumental) and for the Christmas/Holiday family — those are appended to
    a track's genre list as extras, never used as a pooling key of their own.
    The seasonal playlists are built from ``is_christmas_track`` instead, so a
    Christmas mix still exists without a "Christmas" pool.
    """
    cleaned = _strip_admin_genre_markers(str(value or "").strip().lower())
    if not cleaned:
        return False
    if cleaned in _PLAYLIST_POOL_EXCLUDED:
        return False
    return not is_holiday_family_token(cleaned)


def playlist_pool_genres(values: Any, limit: int | None = None) -> list[str]:
    """The genres a track POOLS BY for genre playlists.

    ⭐⭐ THIS IS THE FIX FOR "IT WAS REMOVED FROM THE TRACK BUT IT IS STILL ON
    THE PLAYLIST". The pool must be built from the track's FINAL ``genres``
    value — the three applied musical genres plus any extras — and NOT from the
    per-provider source columns. Last.fm / MusicBrainz / Discogs keep whatever
    they originally returned, so a genre removed from the track's metadata (by
    the selection engine, a manual correction, or a re-scan) survives forever
    in those columns and keeps the track in a playlist Navidrome says it is not
    in. One value, one answer.

    Performance markers are dropped so Cover/Live/Acoustic/Remaster never
    consume a slot or name a playlist; the holiday family is dropped for the
    same reason and placed by the seasonal branch instead.

    ``limit=None`` keeps every musical genre.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _parse_genre_input(values) or []:
        if isinstance(raw, dict):
            raw = raw.get("name") or ""
        name = str(raw or "").strip()
        if not name or not is_playlist_pool_genre(name):
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if limit is not None and len(out) >= max(1, int(limit)):
            break
    return out


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


def _canonical_filter_tag(raw_genre: str) -> str | None:
    """Resolve a raw genre string to its canonical filter tag display name, or None."""
    lower_g = str(raw_genre or "").strip().lower()
    if not lower_g:
        return None
    for candidate in (lower_g, _strip_admin_genre_markers(lower_g)):
        if not candidate:
            continue
        if candidate in _CANONICAL_FILTER_DISPLAY:
            return _CANONICAL_FILTER_DISPLAY[candidate]
        if is_holiday_family_token(candidate):
            return HOLIDAY_CANONICAL_TOKEN
    return None


def is_admin_genre(genre: Any) -> bool:
    if not genre:
        return True
    value = str(genre).strip().lower()
    if not value:
        return True
    if value in _FILTER_TAGS or is_holiday_family_token(value):
        return True
    value = _strip_admin_genre_markers(value)
    if not value:
        return True
    if value in _ADMIN_GENRE_WORDS:
        return True
    # Filter tags and holiday variants shouldn't participate in main voting pool
    if value in _FILTER_TAGS or is_holiday_family_token(value):
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
    re.compile(r"^\d{2}s$"),
    re.compile(r"^(my|the|a|an)\b", re.IGNORECASE),
)


def _junk_filter_enabled() -> bool:
    try:
        from helpers.config_helpers import get_config
        _cfg = get_config() or {}
        return bool(_cfg.get("genres", {}).get("junk_filter", True))
    except Exception:
        return True


def is_junk_genre(genre: Any, *, enabled: bool | None = None) -> bool:
    if not genre:
        return True
    if enabled is None:
        enabled = _junk_filter_enabled()
    if not enabled:
        return False
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


def _source_weight(source: str, weights: dict[str, Any] | None = None) -> float:
    if weights is None:
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

    junk_enabled = _junk_filter_enabled()
    try:
        weights = get_genre_weights() or {}
    except Exception:
        weights = GENRE_WEIGHTS or {}

    for source, raw_genres in (source_map or {}).items():
        # Strip @entity suffix if caller passed v2-style keys into legacy voter
        clean_source = source.split("@", 1)[0] if "@" in source else source
        base_weight = _source_weight(clean_source, weights=weights)
        genres = _parse_genre_input(raw_genres)

        for rank, genre in enumerate(genres or []):
            # Intercept filter tags before junk/admin validation strips them
            matched_filter = _canonical_filter_tag(genre)
            if matched_filter:
                intercepted_filters.add(matched_filter)

            if is_junk_genre(genre, enabled=junk_enabled) or is_admin_genre(genre):
                continue

            key = normalize_genre_for_vote(genre)
            if not key:
                continue

            decay = 1.0 / math.log2(rank + 2)
            weighted_vote = base_weight * decay

            votes[key] += weighted_vote
            spellings[key].append((weighted_vote, normalize_genre(genre)))
            source_hits[key].add(clean_source)

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
    keys_to_delete = [
        k for k, hits in source_hits.items()
        if not bool(hits & (primary_sources | {"context"}))
    ]

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
    junk_enabled = _junk_filter_enabled()
    keys = set()
    for g in _parse_genre_input(nav_genres):
        if not g or is_junk_genre(g, enabled=junk_enabled) or is_admin_genre(g):
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


def _append_extra_genres(
    genres: list[str],
    title: str,
    album: str,
    intercepted_filters: set[str] | None = None,
) -> list[str]:
    title_lower = str(title or "").lower()
    context_lower = f"{title or ''} {album or ''}".lower()
    filters = set(intercepted_filters or ())

    # Base heuristic checks
    if bool(re.search(r"[\(\[]\s*(live|acoustic|unplugged)[^)\]]*[\)\]]\s*$", title_lower)) or \
       any(re.search(p, context_lower) for p in [r"\bconcert\b", r"\bat\s+\w+\s+(arena|stadium|hall|club|theatre|theater)"]):
        filters.add("Live")

    # "Cover" is a per-TRACK affiliation, never an album property.
    if (re.search(r"[\(\[]\s*[^)\]]+\s+cover\s*[\)\]]\s*$", title_lower)
            or re.search(r"\bcover\s+of\b|\boriginally\s+by\b", title_lower)):
        filters.add("Cover")

    if "remaster" in title_lower or "remaster" in context_lower:
        filters.add("Remaster")

    # Append all safely intercepted filter tags in deterministic order
    existing_lower = {g.lower() for g in genres}
    for f_tag in sorted(filters):
        if f_tag.lower() not in existing_lower:
            genres.append(f_tag)
            existing_lower.add(f_tag.lower())

    return genres


def _collect_intercepted_filter_tags(source_map: dict[str, Any] | None) -> set[str]:
    """Every ``_FILTER_TAGS`` / holiday token present in the sources, canonically cased.

    Extracted from ``_vote_genres`` so the canonical-selection engine can keep
    appending the SAME filter tags the legacy engine does. Those tags are not
    genres — they are the playlist/filter markers (Live, Cover, Acoustic,
    Christmas/Holiday) that other code depends on, and dropping them just
    because the genre SLOTS are chosen differently would break live/acoustic
    and seasonal filtering.
    """
    intercepted: set[str] = set()
    for raw_genres in (source_map or {}).values():
        for genre in _parse_genre_input(raw_genres) or []:
            matched = _canonical_filter_tag(genre)
            if matched:
                intercepted.add(matched)
    return intercepted


def _selection_engine_v2_enabled() -> bool:
    """Is the canonical-vocabulary selection engine active?

    FAILS OPEN TO THE LEGACY ENGINE on any config problem: a selection engine that
    cannot read its own switch must not silently stop writing genres.
    """
    try:
        from helpers.config_helpers import get_genre_selection_config

        return bool(get_genre_selection_config().get("enabled", True))
    except Exception as exc:
        logger.debug("Genre selection config unreadable — using the legacy engine", error=str(exc))
        return False


def _aggregate_genres_v2(
    source_map: dict[str, Any],
    *,
    max_genres: int,
    context_title: str,
    context_album: str,
) -> list[str] | None:
    """The canonical-vocabulary path. ``None`` means "fall back to legacy".

    Everything that made the old engine produce a wrong ANSWER is replaced:
    values are classified before scoring, evidence is weighted by entity level,
    redundant ancestors are condensed, and each of the up-to-three positions has
    to earn its place. What is PRESERVED is the filter-tag append — those markers
    are not genres and their absence would break playlist filtering.
    """
    try:
        from services.enrichment.genre_selection import select_album_or_track_genres

        result = select_album_or_track_genres(source_map, max_genres=max_genres)
        selected = list(result.get("genres") or [])
    except Exception as exc:
        logger.warning(
            "Canonical genre selection failed — falling back to the legacy engine",
            error=str(exc),
            exc_info=True,
        )
        return None

    moved = result.get("moved") or {}
    if moved:
        logger.debug(
            "Genre selection set non-genre values aside",
            moods=len(moved.get("mood") or []),
            geography=len(moved.get("geography") or []),
            attributes=len(moved.get("attribute") or []),
            content=len(moved.get("content") or []),
            unknown=len(moved.get("unknown") or []),
            junk=len(moved.get("junk") or []),
        )

    return _append_extra_genres(
        selected,
        context_title,
        context_album,
        _collect_intercepted_filter_tags(source_map),
    )


def aggregate_genres(
    source_map: dict[str, Any],
    max_genres: int = 2,
    context_title: str = "",
    context_album: str = "",
    nav_genres: Any = None,
) -> list[str]:
    """Top genres for one track or album.

    Two engines, one switch (``genres.selection.enabled``):

    * **canonical (v2, default)** — ``services/enrichment/genre_selection.py``.
      Classifies every value, weights evidence by entity level, condenses the
      hierarchy, and requires each position to earn its place.
    * **legacy** — the vote-counting path below, kept intact so the switch is a
      real way back rather than a promise.

    The filter-tag append is shared: Live/Cover/Acoustic/Christmas markers are
    not genres and must survive either way.

    ⚠️ Callers SHOULD declare an entity level with the ``source@entity`` key
    convention (``"lastfm@track"``, ``"musicbrainz@album"``, ``"wikidata@artist"``).
    A bare source key is treated as ``track``, which is both the previous
    behaviour and usually the truth — but it means a caller passing pooled
    artist data under a bare key will have it weighted as the most reliable
    evidence rather than as support.
    """
    if _selection_engine_v2_enabled():
        v2 = _aggregate_genres_v2(
            source_map,
            max_genres=max_genres,
            context_title=context_title,
            context_album=context_album,
        )
        if v2 is not None:
            return v2

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

    junk_enabled = _junk_filter_enabled()
    nav_cleaned = sorted({
        normalize_genre(g).capitalize()
        for g in _parse_genre_input(nav_genres)
        if g and not is_junk_genre(g, enabled=junk_enabled) and not is_admin_genre(g)
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


def _album_holiday_votes(rows: list[Any], album: str, indices: list[int]) -> tuple[int, int]:
    """``(tracks_carrying_a_holiday_token, tracks_examined)`` for an album.

    ⚠️ ``examined`` counts EVERY row, INCLUDING rows with no genre data at all.

    That is the whole point, and it was the bug in the first version of this
    helper: counting only the rows that HAD a genre made a SPARSE album score
    1-of-1 = 100%, so an album where just one track was tagged (the Christmas
    one) and the other thirteen had no genre data evaluated as SEASONAL and the
    Holiday token was written to all of them — the reported "all songs on So
    Much for the Afterglow are being set as Holiday", 14 tracks with a single
    "Hating You for Christmas".

    ⭐ A MAJORITY MUST BE OVER THE ALBUM'S REAL TRACK COUNT. A denominator
    that shrinks to fit the evidence turns "one track of fourteen" into
    "every track we looked at", which is how a minority becomes a majority.
    """
    with_holiday = 0
    examined = 0
    for row in rows:
        row_genres: list[str] = []
        for idx in indices:
            try:
                value = row[idx]
            except (IndexError, KeyError, TypeError):
                continue
            row_genres.extend(_parse_genre_input(value) or [])
        examined += 1
        if any(is_holiday_family_token(g) for g in row_genres):
            with_holiday += 1
    return with_holiday, examined


def album_is_holiday_album(
    *,
    album: str = "",
    track_titles: Any = None,
    holiday_track_count: int = 0,
    track_count: int = 0,
) -> bool:
    """Is the ALBUM itself seasonal, rather than one track on it?

    ⭐⭐ THE FIX FOR "one track's holiday tag becomes the whole album".

    The holiday family is deliberately exempt from the per-track affiliation
    filter (a Christmas album IS an album property, and the playlist builder
    needs the tag outside its genre window). That exemption is what let a
    single Last.fm "holiday" tag on one track reach every sibling: the album
    blend kept it, and the album-wide ``UPDATE tracks SET genres`` then wrote
    it everywhere — including onto tracks that never carried it.

    An album IS seasonal when either signal says so:

    * the album TITLE names it ("A Very Special Christmas", "Holiday Album") —
      the tag always counts, because the title is album-scoped evidence; or
    * MOST of the album's tracks carry a holiday token. A majority, not "any":
      one seasonal bonus track on an otherwise ordinary album ("So Much for
      the Afterglow" with a single Holiday tag) is a per-TRACK fact, and
      spreading it re-labels the whole record.

    ⚠️ ``track_count`` MUST be the album's REAL track count — every row, whether
    or not it has any genre data. See ``_album_holiday_votes`` for why a
    denominator of "tracks that had something to say" turns a minority into a
    majority.

    A one-track release whose only track is seasonal IS a seasonal release, so
    no absolute minimum is imposed: the majority test alone is the rule, and it
    is only meaningful because the denominator is the full track count.
    """
    from services.catalog.album_classification_service import detect_christmas_song

    if detect_christmas_song("", album):
        return True

    if holiday_track_count <= 0 or track_count <= 0:
        return False
    return holiday_track_count * 2 > track_count


def get_track_recommendations(artist: str, album: str) -> dict[str, Any]:
    """The ALBUM-level genre blend, and whether the album is seasonal.

    ⚠️⚠️ ``navidrome_genres`` IS DELIBERATELY NOT READ. Reading it made the
    blend FEED ON ITS OWN OUTPUT.

    ``navidrome_genres`` is not an independent source. Navidrome serves the tags
    of the FILES ON DISK, and this pipeline WRITES those files
    (``album_tag_sync_service.sync_album_file_tags`` → ``_put("genres", …)`` →
    the ``TCON``/``genre`` frame).

    ⭐ A TAG PIPELINE MUST NOT TREAT A REFLECTION OF ITS OWN WRITE AS EVIDENCE.
    """
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT lastfm_tags, musicbrainz_genres, discogs_genres,
                       listenbrainz_genres, spotify_genres, essentia_genres,
                       manual_genres, audiodb_genres, wikidata_genres
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
        ("audiodb", 7),
        ("wikidata", 8),
    ]
    _genre_indices = [idx for _src, idx in column_mapping]

    for row in rows:
        for src_key, idx in column_mapping:
            val = row[idx]
            if not val:
                continue
            parsed_vals = _parse_genre_input(val)
            if parsed_vals:
                # ``@album``: this is a POOLED view across the album's tracks,
                # not one recording's own tags. Declaring the level stops it
                # being weighted as the most specific evidence available.
                source_map.setdefault(f"{src_key}@album", []).extend(parsed_vals)

    recommended = aggregate_genres(
        source_map,
        # The album blend hands the same list to every track of the album, so it
        # uses the SAME limit the per-track path writes — otherwise a track
        # could gain 3 genres from its own sources and then be flattened to 2
        # by the album pass a moment later.
        max_genres=_genre_track_limit(),
        context_album=album,
    )

    # ── Album blend must NOT carry per-track affiliation tags ──────────────
    recommended = [
        g for g in recommended
        if not is_track_affiliation_tag(g)
    ]

    # ── The holiday family needs the guard the affiliation filter gives ────
    _holiday_tracks, _examined = _album_holiday_votes(rows, album, _genre_indices)
    _keep_holiday = album_is_holiday_album(
        album=album,
        holiday_track_count=_holiday_tracks,
        track_count=_examined,
    )
    if not _keep_holiday:
        recommended = [
            g for g in recommended
            if not is_holiday_family_token(g)
        ]
    else:
        if not any(is_holiday_family_token(g) for g in recommended):
            recommended.append(HOLIDAY_CANONICAL_TOKEN)
        recommended = merge_holiday_tokens(recommended)

    return {"success": True, "artist": artist, "album": album, "genres": recommended}


#: Columns feeding a VARIOUS-ARTISTS track's genre vote, and the source key
#: ``aggregate_genres`` weights them under.
#:
#: ``navidrome_genres`` is DELIBERATELY absent. On a compilation each track has
#: a different performer, so the album-level write is skipped (it would hand
#: one blended list to every performer).
VA_GENRE_SOURCES: tuple[tuple[str, str], ...] = (
    ("musicbrainz_genres", "musicbrainz"),
    ("discogs_genres", "discogs"),
    ("audiodb_genres", "audiodb"),
    ("essentia_genres", "essentia"),
    ("listenbrainz_genres", "listenbrainz"),
    ("lastfm_tags", "lastfm"),
    ("lastfm_genres", "lastfm"),  # Kept as alias fallback if caller passes pre-mapped dict
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
            existing = source_map.setdefault(source, [])
            existing_lower = {v.lower() for v in existing}
            for v in values:
                if v.lower() not in existing_lower:
                    existing.append(v)
                    existing_lower.add(v.lower())
    return source_map


# ---------------------------------------------------------------------------
# ARTIST-LEVEL GENRE FALLBACK
# ---------------------------------------------------------------------------

_ARTIST_GENRE_SOURCE_ORDER: tuple[str, ...] = (
    "library",
    "musicbrainz",
    "discogs",
    "lastfm",
)

_ARTIST_GENRE_FALLBACK_LOCK = threading.Lock()
_ARTIST_GENRE_FALLBACK_CACHE: "OrderedDict[str, list[str]]" = OrderedDict()
_ARTIST_GENRE_FALLBACK_MAX = 2048


def _artist_genres_from_library(artist: str, *, exclude_track_id: str = "") -> list[str]:
    """The artist's genres that are ALREADY in this database."""
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
    """MusicBrainz artist genres."""
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
    """Last.fm artist top tags."""
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
    """Store Last.fm artist tags on the artist row, only when it has none."""
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
    """Rank artist-level candidates: source PRIORITY first, then vote order."""
    ranked: list[str] = []
    seen: set[str] = set()
    junk_enabled = _junk_filter_enabled()

    for source in _ARTIST_GENRE_SOURCE_ORDER:
        for raw in sources.get(source) or []:
            name = str(raw or "").strip()
            if not name or is_junk_genre(name, enabled=junk_enabled) or is_admin_genre(name):
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
    """Genres for the PERFORMER, for a VA track that has none of its own."""
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
    with _ARTIST_GENRE_FALLBACK_LOCK:
        cached = _ARTIST_GENRE_FALLBACK_CACHE.get(key)
        if cached is not None:
            _ARTIST_GENRE_FALLBACK_CACHE.move_to_end(key)
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

    if resolved:
        with _ARTIST_GENRE_FALLBACK_LOCK:
            _ARTIST_GENRE_FALLBACK_CACHE[key] = list(resolved)
            _ARTIST_GENRE_FALLBACK_CACHE.move_to_end(key)
            while len(_ARTIST_GENRE_FALLBACK_CACHE) > _ARTIST_GENRE_FALLBACK_MAX:
                _ARTIST_GENRE_FALLBACK_CACHE.popitem(last=False)
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
    """True when the track already carries these genres, in any spelling."""
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
    """Give each track of a VA compilation the genres ITS OWN sources rate."""
    pending_updates: list[tuple[dict[str, Any], Any, str, str]] = []

    for track in tracks:
        track_id = track.get("id")
        if not track_id:
            continue

        source_map = va_track_source_map(track)
        if source_map:
            source_map = {f"{k}@track": v for k, v in source_map.items()}
            top_genres = aggregate_genres(
                source_map,
                max_genres=_genre_track_limit(),
                context_title=str(track.get("title") or ""),
                context_album=str(album or ""),
                nav_genres=None,
            )
            origin = "own sources"
        else:
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

        pending_updates.append((track, track_id, genres_str, origin))

    if not pending_updates:
        return 0

    updated = 0
    batch_params = [
        {"genres": genres_str, "track_id": track_id}
        for _track, track_id, genres_str, _origin in pending_updates
    ]

    try:
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET genres = :genres WHERE id = :track_id"),
                batch_params,
            )
        for track, track_id, genres_str, origin in pending_updates:
            track["genres"] = genres_str
            updated += 1
            if origin == "track artist":
                logger.debug(
                    "Various-artist track genre set from its artist",
                    track_id=track_id,
                    artist=str(track.get("artist") or ""),
                    genres=genres_str,
                )
    except Exception as batch_exc:
        logger.debug("Batch VA genre update failed, falling back to per-row", error=str(batch_exc))
        for track, track_id, genres_str, origin in pending_updates:
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
    genres_collected: list[str] = []
    junk_enabled = _junk_filter_enabled()

    def _add_clean(raw_genres: list[str] | None, source: str) -> None:
        if not raw_genres:
            return
        kept = []
        for g in raw_genres:
            if not g:
                continue
            if is_junk_genre(g, enabled=junk_enabled) or is_admin_genre(g):
                logger.debug("Filtered out junk/admin genre tag", source=source, genre=g)
                continue
            kept.append(normalize_genre(g))

        if kept:
            genres_collected.extend(kept)
            if verbose:
                logger.info(f"{source} genres found", artist=artist_name, count=len(kept))

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

    cleaned = clean_conflicting_genres(genres_collected)[:3]

    if cleaned:
        try:
            with db_session() as session:
                result = session.execute(
                    text(
                        "UPDATE tracks SET genres = :genres_str "
                        "WHERE LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))) = LOWER(TRIM(:artist_name)) "
                        "  AND (genres IS NULL OR TRIM(genres) = '')"
                    ),
                    {"genres_str": ", ".join(cleaned), "artist_name": artist_name},
                )
            if verbose:
                logger.info(
                    "Updated tracks with enriched genres",
                    updated_rows=result.rowcount,
                    artist=artist_name,
                    genres_count=len(cleaned),
                )
        except Exception as e:
            logger.debug("Failed to update genres in DB", artist=artist_name, error=str(e))

    return set(cleaned)
