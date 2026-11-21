"""Canonical genre cleanup: delimiters, non-genre blocklist, spelling, casing.

The reported problem
--------------------
The collection's export held **834 raw genre strings** that reduced to **625**
case-insensitively — the remaining 209 differences were pure noise:

    'Gothic Metal | Symphonic Metal | Power Metal | ...'   one compound string
    'Industrial Thrash | Groove Metal'                     one compound string
    'Offizielle Charts', '1–4 Wochen', '5+ Wochen'         chart positions
    'Laut.De', 'Ph_Temp_Checken', 'Fixt Music'             internal debug flags
    '70'S', '80'S', '10s'                                  decade tags
    'Deutsch', 'German', 'Australian', 'Norway'            language / country
    '& Country'                                            a split fragment
    'Pîwer Metal', 'Pîwer Måtàl'                           a typo for Power Metal
    'Поп', 'Рок', 'металл', 'Металл'                       Cyrillic duplicates

Each of those either named a playlist that should not exist, or split one
genre across two playlists.

What this module is, and is not
-------------------------------
It is the ONE place that answers "what are the genre TOKENS in this value, and
are they musical genres at all". The splitters elsewhere in the app disagreed —
``_parse_genre_input`` (the legacy engine) and ``_genre_playlist_track_genres``
did not split on ``|`` at all, while ``tokenize_values`` and
``iter_genre_tokens`` did — so a pipe-delimited string stayed ONE unknown token
on the legacy path and produced a compound playlist name. The pooling path
(``genre_aggregation_service._parse_genre_input``) now delegates here.

⚠️ It deliberately does NOT decide which genre a track keeps. That is the
selection engine's job (``genre_selection``), which needs the raw tokens and
their sources. This module cleans and canonicalises; it never ranks.

Casing
------
Display casing is applied from a canonical map, never with ``.str.title()``,
because title-casing mangles the acronyms the collection listed: ``R&B`` became
``R&B`` (ok) but ``EBM``/``IDM``/``UK Garage``/``J-Pop``/``Lo-Fi`` do not survive
a naive capitalise. Anything not in the map keeps its original spelling.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# ---------------------------------------------------------------------------
# 1. Delimiters
# ---------------------------------------------------------------------------

#: Safe separators — a value is ALWAYS split on these.
#:
#: ``|`` is included because the export showed pipe-delimited provider values
#: ("Gothic Metal | Symphonic Metal | Power Metal | …") stored as ONE tag.
#:
#: ⚠️ ``\`` is REQUIRED and was missing. Navidrome / ID3v2.3 join multiple genres
#: with a backslash (``metal\nu metal\rock``), and every other splitter in the
#: app (``tag_file_service._GENRE_SPLIT_RE``, ``iter_genre_tokens``,
#: ``_parse_genre_input``) already treats it as a separator. Omitting it left
#: that whole string as ONE genre token.
SAFE_SPLIT_RE = re.compile(r"[;|\\]+")

#: Unsafe separators, applied ONLY to a part the vocabulary does not recognise
#: as a genre on its own.
#:
#: ⚠️ ``death/doom``, ``drum & bass`` and ``r&b`` are REAL single genres. A blind
#: split turns them into ``death`` + ``doom``, ``drum`` + ``bass`` and
#: ``r`` + ``b`` — which is how a ``death/doom`` tag became a bare ``doom metal``
#: one. So these are tried last, and only when the whole part matched nothing.
UNSAFE_SPLIT_RE = re.compile(r"[/&]+")

#: A comma is always a separator: it joins a genre LIST in ``tracks.genres`` and
#: in every provider column (``", ".join(...)``), and is never inside a single
#: genre name.
COMMA_SPLIT_RE = re.compile(r"\s*,\s*")

#: Punctuation stripped from the edges of a token.
_TRIM_CHARS = " \t\r\n[]{}'\"“”‘’"


def _clean_token(token: Any) -> str:
    """Trim one token and drop the fragments a bad split leaves behind."""
    text = str(token or "").strip().strip(_TRIM_CHARS).strip()
    if not text:
        return ""
    # A fragment beginning with '&' is the tail of an "X & Y" that was split
    # mid-name ("& Country" from "Rock & Country"). It is not a genre.
    if text.startswith("&"):
        return ""
    if text.lower() in ("null", "none", "unknown"):
        return ""
    return text


def _canonical_predicate(canonical_ok):
    """The "is this whole string one genre?" test, defaulting to the vocabulary.

    The vocabulary is the app's single classification authority, so a caller
    that does not supply its own predicate still gets the guarded behaviour
    (``death/doom`` stays whole). An import failure degrades to "split it",
    which is the old behaviour rather than an error.
    """
    if canonical_ok is not None:
        return canonical_ok

    def _vocab_says_known(text: str) -> bool:
        try:
            from services.enrichment.genre_vocabulary import (
                KIND_UNKNOWN,
                get_vocabulary,
            )

            return get_vocabulary().classify(text).kind != KIND_UNKNOWN
        except Exception:
            return False

    return _vocab_says_known


def _split_all(text: str, canonical_ok) -> list[str]:
    """Split ``text`` on every separator, guarding only the UNSAFE ones.

    ⚠️ ORDER IS LOAD-BEARING, and two earlier versions got it wrong:

    1. It stopped after the first delimiter family that matched, so
       ``"Rock, Metal; Punk"`` became ``["Rock, Metal", "Punk"]``.
    2. It tested ``&`` before the comma, so ``"Drum & Bass, Rock"`` was torn into
       ``["Drum", "Bass", "Rock"]``.

    The comma is therefore applied FIRST and always — it joins a genre list in
    ``tracks.genres`` and is never inside a single genre name. Safe separators
    (``; | \\``) are then applied to every comma part, and only a part that is
    still not a recognised genre is split on ``/`` or ``&``.

    ⚠️ THE COMMA SPLIT MUST PRECEDE THE WHOLE-VALUE CHECK. The vocabulary's
    normaliser strips punctuation, so the composite string ``"Pop, Rock"``
    matches the canonical ``pop_rock`` ("Pop Rock") and was kept whole — one
    genre named after two.
    """
    out: list[str] = []
    for comma_part in COMMA_SPLIT_RE.split(text):
        if not comma_part.strip():
            continue
        for part in SAFE_SPLIT_RE.split(comma_part):
            part = part.strip()
            if not part:
                continue
            # A part that IS a recognised genre is never torn apart.
            try:
                known = bool(canonical_ok(part))
            except Exception:
                known = False
            if known:
                out.append(part)
                continue
            sub = [s.strip() for s in UNSAFE_SPLIT_RE.split(part) if s.strip()]
            if len(sub) > 1:
                out.extend(sub)
            else:
                out.append(part)
    return out


def split_genre_value(value: Any, *, canonical_ok=None) -> list[str]:
    """The raw tokens in one genre value, in order, deduplicated.

    ``canonical_ok`` is an optional predicate answering "is this whole string a
    genre you recognise?". It defaults to the genre vocabulary, and is consulted
    before any UNSAFE split so that ``death/doom`` and ``drum & bass`` are never
    torn apart.
    """
    text = str(value or "").strip()
    if not text:
        return []

    predicate = _canonical_predicate(canonical_ok)

    # ⚠️ A comma is ALWAYS a separator, so a value containing one is never a
    # single genre — even when the vocabulary's punctuation-stripping normaliser
    # maps "Pop, Rock" onto the canonical ``pop_rock``. Checking the whole value
    # only makes sense when there is nothing unambiguous to split on.
    if "," not in text and ";" not in text and "|" not in text and "\\" not in text:
        try:
            if predicate(text):
                token = _clean_token(text)
                return [token] if token else []
        except Exception:
            pass

    if text.startswith("[") or text.startswith("{"):
        try:
            import json

            parsed = json.loads(text)
            if isinstance(parsed, (list, dict)):
                return split_genre_values(parsed, canonical_ok=canonical_ok)
        except Exception:
            pass

    parts = _split_all(text, predicate)

    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = _clean_token(part)
        if not token:
            continue
        key = normalize_token(token)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def split_genre_values(values: Any, *, canonical_ok=None) -> list[str]:
    """Split a value that may already be a list/dict/JSON blob."""
    if values is None:
        return []
    if isinstance(values, dict):
        out: list[str] = []
        for item in values.values():
            out.extend(split_genre_values(item, canonical_ok=canonical_ok))
        return out
    if isinstance(values, (list, tuple, set)):
        out = []
        for item in values:
            out.extend(split_genre_values(item, canonical_ok=canonical_ok))
        return out
    return split_genre_value(values, canonical_ok=canonical_ok)


def normalize_token(value: Any) -> str:
    """Comparison key: casefolded, diacritic- and punctuation-stripped."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.casefold().strip()
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return re.sub(r"[^a-z0-9]+", "", text)


# ---------------------------------------------------------------------------
# 2. Non-genre blocklist
# ---------------------------------------------------------------------------

#: Chart positions, internal debug flags and provider artefacts seen in the
#: export. These are NOT genres and must never name a playlist.
NOISE_GENRE_KEYS: frozenset[str] = frozenset(
    normalize_token(v)
    for v in (
        "Offizielle Charts",
        "1–4 Wochen", "1-4 wochen", "5+ Wochen", "Wochen",
        "Laut.De",
        "Ph_Temp_Checken",
        "Fixt Music",
        "Eurovision-Song-Contest", "Eurovision 2022 Artists",
        "Eurovision",
    )
)

#: Internal/plumbing prefixes: anything starting with these is a working flag,
#: not a genre. ``Ph_Temp_Checken`` is the export's example.
NOISE_GENRE_PREFIXES: tuple[str, ...] = ("ph_temp", "temp_check", "fixtmusic")

#: Languages and nationalities. A language tag names HOW something is sung; it
#: is not a genre. Country tags are handled by the vocabulary's geography table
#: when they are worth keeping.
LANGUAGE_GENRE_KEYS: frozenset[str] = frozenset(
    normalize_token(v)
    for v in (
        "Deutsch", "German", "English", "Francais", "Français", "Espanol",
        "Español", "Italiano", "Portuguese", "Japanese", "Korean",
    )
)

#: Countries seen as genre tags in the export.
COUNTRY_GENRE_KEYS: frozenset[str] = frozenset(
    normalize_token(v)
    for v in (
        "Australian", "Australia", "Norway", "Norwegian", "Netherlands",
        "Dutch", "Brazil", "Brazilian", "Finland", "Finnish", "UK",
        "New Zealand", "Canadian", "Canada", "French", "France", "Swedish",
        "Sweden", "Icelandic", "Iceland",
    )
)

#: Decade / era markers, matched as a shape rather than a list because the
#: export contained ``70'S``, ``80'S`` and ``10s`` spellings.
_DECADE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d{2}\s*'?s$", re.IGNORECASE),
    re.compile(r"^(19|20)\d{2}\s*'?s$", re.IGNORECASE),
    re.compile(r"^\d{2}'s$", re.IGNORECASE),
)


def is_noise_genre(value: Any) -> bool:
    """Is this a chart position, debug flag, language, country or decade?"""
    text = str(value or "").strip()
    if not text:
        return True
    key = normalize_token(text)
    if not key:
        return True
    if key in NOISE_GENRE_KEYS or key in LANGUAGE_GENRE_KEYS or key in COUNTRY_GENRE_KEYS:
        return True
    if any(key.startswith(normalize_token(p)) for p in NOISE_GENRE_PREFIXES):
        return True
    return any(p.match(text) for p in _DECADE_PATTERNS)


# ---------------------------------------------------------------------------
# 3. Canonical spelling
# ---------------------------------------------------------------------------

#: Spelling, spacing, hyphen and typo corrections applied BEFORE casing.
#:
#: Keys and values are compared through ``normalize_token``, so every spelling
#: of the key ("Pop-Punk", "pop punk", "PopPunk") reaches the same entry.
_CANONICAL_SPELLINGS: tuple[tuple[str, str], ...] = (
    # Spacing / hyphen unification (the collection's own list).
    ("Brit Pop", "Britpop"),
    ("Pop-Punk", "Pop Punk"),
    ("PopPunk", "Pop Punk"),
    ("Hip Hop", "Hip-Hop"),
    ("HipHop", "Hip-Hop"),
    ("Synth-pop", "Synthpop"),
    ("Synth-Pop", "Synthpop"),
    ("Death-Metal", "Death Metal"),
    ("Post Rock", "Post-Rock"),
    ("Alt Metal", "Alternative Metal"),
    ("Alt. Metal", "Alternative Metal"),
    ("Alt-Metal", "Alternative Metal"),
    ("Alt Rock", "Alternative Rock"),
    ("Alt. Rock", "Alternative Rock"),
    ("Alt-Rock", "Alternative Rock"),
    ("Drum And Bass", "Drum and Bass"),
    ("Drum n Bass", "Drum and Bass"),
    ("Drum & Bass", "Drum and Bass"),
    ("Rock And Roll", "Rock & Roll"),
    ("Rock and Roll", "Rock & Roll"),
    # Typo / apostrophe fixes.
    ("Pîwer Metal", "Power Metal"),
    ("Pîwer Måtàl", "Power Metal"),
    ("Pîwer Matal", "Power Metal"),
    ("Children'S Music", "Children's Music"),
    ("Childrens Music", "Children's Music"),
    ("Childrens", "Children's Music"),
    ("childrens", "Children's Music"),
    # Non-English duplicates.
    ("Поп", "Pop"),
    ("Рок", "Rock"),
    ("металл", "Metal"),
    ("Металл", "Metal"),
    ("Enfants", "Children's Music"),
    ("Educatif", "Children's Music"),
    ("Éducatif", "Children's Music"),
    ("Mittelalter", "Medieval"),
)

#: ``normalize_token(key) -> canonical spelling``.
_CANONICAL_LOOKUP: dict[str, str] = {
    normalize_token(k): v for k, v in _CANONICAL_SPELLINGS
}

#: Acronyms and stylised names whose casing a title-case rule would destroy.
#: Keyed by the SAME normalised token, so a lowercased tag still resolves.
_ACRONYM_DISPLAY: dict[str, str] = {
    normalize_token(v): v
    for v in (
        "EBM", "IDM", "EDM", "AOR", "R&B", "MPB", "UK Garage", "J-Pop",
        "K-Pop", "G-Funk", "Lo-Fi", "Nu-Disco", "Synthwave", "Drum and Bass",
        "Hip-Hop", "Rock & Roll", "Children's Music", "Rap Metal", "Pop Punk",
        "Drum & Bass", "Trip-Hop", "Post-Rock", "Post-Punk", "Post-Metal",
        "Post-Grunge", "Post-Hardcore", "Neo-Psychedelia", "Anti-Folk",
        "C-Pop", "T-Pop", "V-Pop", "Y2K", "UK Drill",
    )
}


def canonical_spelling(value: Any) -> str:
    """The canonical spelling of one token, before display casing.

    Returns the mapped form when the token is known, else the token with its
    whitespace collapsed (so "  Death   Metal " becomes "Death Metal").
    """
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    key = normalize_token(text)
    mapped = _CANONICAL_LOOKUP.get(key)
    if mapped:
        return mapped
    return text


def display_genre(value: Any) -> str:
    """Title-cased display form, with the acronym allowlist honoured.

    Title-casing is a LAST resort: a value that is already correctly cased is
    left alone, because ``.title()`` turns "R&B" into "R&B" but "UK Garage"
    into "Uk Garage" and "J-Pop" into "J-Pop".
    """
    text = canonical_spelling(value)
    if not text:
        return ""
    key = normalize_token(text)
    acronym = _ACRONYM_DISPLAY.get(key)
    if acronym:
        return acronym
    # Already mixed-case? Leave it — the source knew better than we do.
    if any(ch.isupper() for ch in text[1:]) or any(ch.islower() for ch in text[1:]):
        return text
    return text.title()


def canonicalize_genre(value: Any) -> str:
    """The single entry point: clean one tag and return its display form.

    Returns ``""`` for anything that is not a usable genre — a noise token, a
    language/country tag, a decade marker, or an empty fragment.
    """
    token = _clean_token(value)
    if not token or is_noise_genre(token):
        return ""
    return display_genre(token)


def clean_genre_values(values: Any) -> list[str]:
    """Split, blocklist, canonicalise and DEDUPE a whole genre value.

    Order is preserved (the first spelling wins the slot), because the playlist
    builder reads genres positionally for its three-slot limit.
    """
    out: list[str] = []
    seen: set[str] = set()
    for token in split_genre_values(values):
        clean = canonicalize_genre(token)
        if not clean:
            continue
        key = normalize_token(clean)
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def is_usable_genre(value: Any) -> bool:
    """Would ``canonicalize_genre`` keep this token?"""
    return bool(canonicalize_genre(value))
