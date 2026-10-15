"""Canonical genre/mood/geography/attribute vocabulary, and the classifier.

Why this exists
---------------
Genre aggregation used to compare raw source strings directly, so the three slots
were contested by things that are not genres at all — moods (`dark`,
`aggressive`), geography (`Australian`, `German`), performance attributes
(`live`, `cover`) and source artefacts (`Billboard Hot 100`) — alongside broad
parents (`rock`) and precise subgenres (`melodic death metal`). That is the
reported problem: **the selection stage was happening too early.** Nothing had
first asked "what KIND of value is this?".

This module answers that question, and gives every value a stable canonical
identity so four spellings of one genre become four pieces of evidence for one
genre rather than four competing entries.

Vocabulary storage
------------------
The vocabulary is DATA, not code:

* ``services/enrichment/genre_vocabulary.json`` — the shipped default, tracked in
  git so a change to it is reviewable.
* ``<config_dir>/genre_vocabulary.json`` — a user's edits from the Config page.
  It takes precedence, so a repo update never clobbers them.

The Config page edits the override and offers a reset back to the default. Both
are validated on load AND on save, because a malformed vocabulary would otherwise
fail silently at scan time.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: The shipped default, beside this module.
DEFAULT_VOCABULARY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "genre_vocabulary.json"
)

#: The user's override, in the config volume so it survives a container rebuild.
OVERRIDE_FILE_NAME = "genre_vocabulary.json"

#: Kinds a value can be classified as. ``genre`` is the ONLY kind that competes
#: for a genre slot; the rest are recognised so they can be excluded from it.
KIND_GENRE = "genre"
KIND_MOOD = "mood"
KIND_GEOGRAPHY = "geography"
KIND_ATTRIBUTE = "attribute"
KIND_CONTENT = "content"
KIND_JUNK = "junk"
KIND_UNKNOWN = "unknown"

#: How deep a chain of parents is followed before giving up. A cycle in a
#: hand-edited vocabulary must not hang the scan.
_MAX_PARENT_DEPTH = 12


def normalize_token(value: Any) -> str:
    """The lookup key for a raw value. One normaliser, used everywhere.

    Unicode NFKD first, so ``nü metal`` and ``nu metal`` are the same key —
    sources disagree about diacritics constantly (``Motörhead`` / ``Motorhead``),
    and without the decomposition those are two genres. The decomposition splits
    ``ü`` into ``u`` + a combining mark, and the punctuation strip below removes
    the mark, so no separate "strip combining marks" pass is needed (measured:
    adding one changes nothing). Then casefold, then collapse whitespace and
    strip the punctuation that only ever varies between sources — ``alt. metal``
    / ``alt-metal`` / ``alt metal`` must be ONE genre, and they were three.

    ⚠️ Punctuation is stripped only for the KEY, never for display: ``post-punk``
    and ``r&b`` keep their punctuation in the tag they write.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.casefold().strip()
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


@dataclass(frozen=True)
class GenreEntry:
    """One canonical genre."""

    id: str
    display: str
    #: Broader genres. A LIST, not one value: the domain has multi-inheritance
    #: and the report's own rules need it — ``folk metal`` is both metal and
    #: folk, ``synthpop`` is both pop and electronic. With a single parent one
    #: of each pair's stated condensation rules could not work at all.
    parents: tuple[str, ...]
    specificity: int
    family: str
    aliases: tuple[str, ...]

    def ancestors(self, by_id: dict[str, "GenreEntry"]) -> list[str]:
        """Every ancestor id, nearest first. Breadth-first, cycle-safe, bounded."""
        chain: list[str] = []
        seen: set[str] = {self.id}
        frontier = [p for p in self.parents if p]
        for _ in range(_MAX_PARENT_DEPTH):
            if not frontier:
                break
            next_frontier: list[str] = []
            for parent in frontier:
                if parent in seen:
                    continue
                seen.add(parent)
                chain.append(parent)
                entry = by_id.get(parent)
                if entry is not None:
                    next_frontier.extend(p for p in entry.parents if p)
            frontier = next_frontier
        return chain

    def family_of(self, by_id: dict[str, "GenreEntry"]) -> str:
        """The nearest DECLARED family among the ancestors, else the root id.

        Declared rather than derived from the root, because ``metal`` sits under
        ``rock`` (so §9's "Rock + Death Metal -> Death Metal" works) while still
        being its own FAMILY — otherwise the family-slot limit would treat every
        rock and metal genre as one branch and cap them together.
        """
        for ancestor in self.ancestors(by_id):
            entry = by_id.get(ancestor)
            if entry is not None and entry.family:
                return entry.family
        chain = self.ancestors(by_id)
        return chain[-1] if chain else self.id


@dataclass(frozen=True)
class SimpleEntry:
    """A mood, attribute or content value — identified, but not a genre."""

    id: str
    display: str
    family: str


@dataclass(frozen=True)
class Classification:
    """What one raw value turned out to be."""

    kind: str
    #: Canonical id for a genre/mood/attribute; the normalised country/region
    #: NAME for geography; ``""`` for junk and unknown.
    canonical: str
    display: str
    family: str = ""
    specificity: int = 0


@dataclass(frozen=True)
class Vocabulary:
    genres: dict[str, GenreEntry] = field(default_factory=dict)
    genre_lookup: dict[str, str] = field(default_factory=dict)
    moods: dict[str, SimpleEntry] = field(default_factory=dict)
    mood_lookup: dict[str, str] = field(default_factory=dict)
    countries: dict[str, str] = field(default_factory=dict)
    regions: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)
    attribute_lookup: dict[str, str] = field(default_factory=dict)
    content: dict[str, str] = field(default_factory=dict)
    source_path: str = ""
    is_override: bool = False
    errors: tuple[str, ...] = ()

    def classify(self, raw: Any) -> Classification:
        """Classify one raw value.

        ⚠️ ORDER IS LOAD-BEARING. A genre is matched FIRST, because the vocabulary
        deliberately contains words that are also moods or activities —
        ``ambient``, ``chillout``, ``dark ambient``, ``dream pop``. Running the
        mood map first would reclassify real genres as moods and delete them from
        every track's tags.
        """
        key = normalize_token(raw)
        if not key:
            return Classification(KIND_UNKNOWN, "", "")

        genre_id = self.genre_lookup.get(key)
        if genre_id:
            entry = self.genres[genre_id]
            return Classification(
                KIND_GENRE, entry.id, entry.display, entry.family, entry.specificity
            )

        attribute_id = self.attribute_lookup.get(key)
        if attribute_id:
            return Classification(KIND_ATTRIBUTE, attribute_id, raw if isinstance(raw, str) else str(raw))

        content_id = self.content.get(key)
        if content_id:
            return Classification(KIND_CONTENT, content_id, content_id.replace("_", " ").title())

        country = self.countries.get(key)
        if country:
            return Classification(KIND_GEOGRAPHY, country, country)

        region = self.regions.get(key)
        if region:
            return Classification(KIND_GEOGRAPHY, region, region)

        mood_id = self.mood_lookup.get(key)
        if mood_id:
            entry = self.moods[mood_id]
            return Classification(KIND_MOOD, entry.id, entry.display, entry.family)

        return Classification(KIND_UNKNOWN, "", raw if isinstance(raw, str) else str(raw))


def override_vocabulary_path() -> str:
    """Where the Config page writes the user's vocabulary."""
    from helpers.config_helpers import get_config_directory

    return os.path.join(get_config_directory(), OVERRIDE_FILE_NAME)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_vocabulary(data: Any) -> list[str]:
    """Return a list of human-readable problems. Empty means valid.

    Called on load AND before a save, so a bad hand-edit is caught where it was
    made rather than surfacing as a scan that silently writes no genres.
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["the vocabulary must be a JSON object"]

    genres = data.get("genres")
    if not isinstance(genres, list) or not genres:
        errors.append("'genres' must be a non-empty list")
        genres = []

    ids: set[str] = set()
    aliases_seen: dict[str, str] = {}

    for index, raw in enumerate(genres):
        if not isinstance(raw, dict):
            errors.append(f"genres[{index}] must be an object")
            continue
        gid = str(raw.get("id") or "").strip()
        display = str(raw.get("display") or "").strip()
        if not gid:
            errors.append(f"genres[{index}] has no 'id'")
            continue
        if not re.fullmatch(r"[a-z0-9_]+", gid):
            errors.append(f"genres[{index}] id {gid!r} must be lower_snake_case")
        if not display:
            errors.append(f"genre {gid!r} has no 'display'")
        if gid in ids:
            errors.append(f"duplicate genre id {gid!r}")
        ids.add(gid)

        specificity = raw.get("specificity", 1)
        if not isinstance(specificity, int) or not 0 <= specificity <= 3:
            errors.append(f"genre {gid!r} specificity must be an integer 0-3")

        # ⚠️ An alias that maps to two genres makes the winner depend on dict
        # order, which is exactly the kind of silent ambiguity this vocabulary
        # exists to remove.
        for alias in _entry_aliases(raw):
            key = normalize_token(alias)
            if not key:
                continue
            owner = aliases_seen.get(key)
            if owner and owner != gid:
                errors.append(
                    f"alias {alias!r} is claimed by both {owner!r} and {gid!r}"
                )
            aliases_seen.setdefault(key, gid)

    for index, raw in enumerate(genres):
        if not isinstance(raw, dict):
            continue
        for parent in _entry_parents(raw):
            if parent not in ids:
                errors.append(
                    f"genre {raw.get('id')!r} has parent {parent!r}, which is not a genre id"
                )

    for section in ("moods",):
        block = data.get(section)
        if block is not None and not isinstance(block, list):
            errors.append(f"'{section}' must be a list when present")

    geo = data.get("geography")
    if geo is not None and not isinstance(geo, dict):
        errors.append("'geography' must be an object when present")

    return errors


def _entry_parents(raw: dict[str, Any]) -> list[str]:
    """The entry's broader genres. Accepts ``parent`` (one) or ``parents`` (many)."""
    many = raw.get("parents")
    if isinstance(many, list):
        return [str(p).strip() for p in many if p and str(p).strip()]
    one = raw.get("parent")
    if one and str(one).strip():
        return [str(one).strip()]
    return []


def _entry_aliases(raw: dict[str, Any]) -> list[str]:
    """Every string that should resolve to this entry.

    The lowercased display name and the id-with-spaces are derived, so a
    vocabulary only lists EXTRA spellings — a hand-maintained file that repeated
    the obvious ones would drift the moment a display name changed.
    """
    display = str(raw.get("display") or "").strip()
    gid = str(raw.get("id") or "").strip()
    out = [display, gid.replace("_", " ")]
    extra = raw.get("aliases")
    if isinstance(extra, list):
        out.extend(str(a) for a in extra if a)
    elif isinstance(extra, str):
        out.append(extra)
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_CACHE: Vocabulary | None = None
_CACHE_STAMP: tuple[str, float, int] | None = None


def clear_vocabulary_cache() -> None:
    """Drop the cached vocabulary. Called after a save, and by tests."""
    global _CACHE, _CACHE_STAMP
    _CACHE = None
    _CACHE_STAMP = None


def _stamp(path: str) -> tuple[str, float, int]:
    try:
        stat = os.stat(path)
        return (path, stat.st_mtime, stat.st_size)
    except OSError:
        return (path, 0.0, 0)


def get_vocabulary() -> Vocabulary:
    """The active vocabulary: the user's override if present, else the default.

    Re-read whenever the file's mtime or size changes, so a Config-page save
    takes effect on the next scan without a restart.
    """
    global _CACHE, _CACHE_STAMP

    override = override_vocabulary_path()
    path = override if os.path.isfile(override) else DEFAULT_VOCABULARY_PATH
    stamp = _stamp(path)

    if _CACHE is not None and _CACHE_STAMP == stamp:
        return _CACHE

    data: Any = {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        logger.warning("Genre vocabulary file missing", path=path)
        data = {}
    except Exception as exc:
        logger.error("Genre vocabulary unreadable", path=path, error=str(exc))
        data = {}

    errors = validate_vocabulary(data)
    if errors:
        for problem in errors:
            logger.error("Genre vocabulary invalid", path=path, problem=problem)

    vocab = _build(data, path=path, is_override=(path == override), errors=tuple(errors))
    _CACHE = vocab
    _CACHE_STAMP = stamp
    return vocab


def _build(
    data: dict[str, Any], *, path: str, is_override: bool, errors: tuple[str, ...]
) -> Vocabulary:
    genres: dict[str, GenreEntry] = {}
    lookup: dict[str, str] = {}

    for raw in data.get("genres") or []:
        if not isinstance(raw, dict):
            continue
        gid = str(raw.get("id") or "").strip()
        if not gid or gid in genres:
            continue
        specificity = raw.get("specificity", 1)
        genres[gid] = GenreEntry(
            id=gid,
            display=str(raw.get("display") or gid).strip(),
            parents=tuple(_entry_parents(raw)),
            specificity=specificity if isinstance(specificity, int) else 1,
            family=str(raw.get("family") or "").strip(),
            aliases=tuple(str(a) for a in (raw.get("aliases") or []) if a),
        )

    # A genre that declares no family INHERITS the nearest declared one from its
    # ancestors (see ``GenreEntry.family_of``), so the vocabulary only states a
    # family where the hierarchy alone would put a genre in the wrong branch.
    genres = {
        gid: GenreEntry(
            id=entry.id,
            display=entry.display,
            parents=entry.parents,
            specificity=entry.specificity,
            family=entry.family or entry.family_of(genres),
            aliases=entry.aliases,
        )
        for gid, entry in genres.items()
    }

    for gid, entry in genres.items():
        for alias in _entry_aliases(
            {"id": gid, "display": entry.display, "aliases": list(entry.aliases)}
        ):
            key = normalize_token(alias)
            if key:
                lookup.setdefault(key, gid)

    moods: dict[str, SimpleEntry] = {}
    mood_lookup: dict[str, str] = {}
    for raw in data.get("moods") or []:
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("id") or "").strip()
        if not mid or mid in moods:
            continue
        entry = SimpleEntry(
            id=mid,
            display=str(raw.get("display") or mid).strip(),
            family=str(raw.get("family") or "other").strip(),
        )
        moods[mid] = entry
        for alias in _entry_aliases(raw):
            key = normalize_token(alias)
            if key:
                mood_lookup.setdefault(key, mid)

    geo = data.get("geography") or {}
    countries = _normalise_map(geo.get("countries") if isinstance(geo, dict) else {})
    regions = _normalise_map(geo.get("regions") if isinstance(geo, dict) else {})

    attrs = data.get("attributes") or {}
    attributes: dict[str, str] = {}
    attribute_lookup: dict[str, str] = {}
    if isinstance(attrs, dict):
        for group in ("performance", "content"):
            block = attrs.get(group)
            if not isinstance(block, dict):
                continue
            for attr_id, spellings in block.items():
                values = spellings if isinstance(spellings, list) else [spellings]
                for value in values:
                    key = normalize_token(value)
                    if key:
                        attribute_lookup.setdefault(key, str(attr_id))
                attributes.setdefault(str(attr_id), str(group))
    content = {k: v for k, v in attributes.items() if v == "content"}

    return Vocabulary(
        genres=genres,
        genre_lookup=lookup,
        moods=moods,
        mood_lookup=mood_lookup,
        countries=countries,
        regions=regions,
        attributes=attributes,
        attribute_lookup=attribute_lookup,
        content=content,
        source_path=path,
        is_override=is_override,
        errors=errors,
    )


def _normalise_map(block: Any) -> dict[str, str]:
    if not isinstance(block, dict):
        return {}
    out: dict[str, str] = {}
    for raw_key, value in block.items():
        if str(raw_key).startswith("_"):
            continue
        key = normalize_token(raw_key)
        if key and value:
            out[key] = str(value).strip()
    return out


def _root_of(entry: GenreEntry, by_id: dict[str, GenreEntry]) -> str:
    chain = entry.ancestors(by_id)
    return chain[-1] if chain else entry.id

# ---------------------------------------------------------------------------
# Tokenising composite values
# ---------------------------------------------------------------------------

#: Relatively safe separators — a genre name essentially never contains one.
_SAFE_SPLIT = re.compile(r"[;|\\]+")

#: Dangerous separators. ``/`` appears inside real labels (``death/doom``,
#: ``death/doom metal``) and ``&`` inside ``r&b`` / ``drum & bass``, so these are
#: only split when the WHOLE value failed to match anything.
_UNSAFE_SPLIT = re.compile(r"[/&]+")

#: A serialized array (``['Doom Metal', 'Rock']``). Inside one, commas ARE
#: separators — that is the only context where splitting on ``,`` is safe.
_ARRAY_LIKE = re.compile(r"^\s*[\[{]")
_ALL_SPLIT = re.compile(r"[,;|\\/]+")
_STRIP_CHARS = " \t\r\n[]{}'\"\u2018\u2019\u201c\u201d"


def _flatten(raw: Any) -> list[str]:
    """Lists/dicts/JSON strings -> raw strings, WITHOUT splitting anything."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            out.extend(_flatten(item))
        return out
    if isinstance(raw, dict):
        out = []
        for value in raw.values():
            out.extend(_flatten(value))
        return out
    text = str(raw).strip()
    if not text or text.lower() in ("[]", "{}", "null", "none"):
        return []
    if text[:1] in "[{":
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, (list, dict)):
            return _flatten(parsed)
    return [text]


def tokenize_values(raw: Any, vocab: "Vocabulary") -> list[str]:
    """Split a raw source value into candidate tokens, VOCABULARY-AWARE.

    The order is the report's, and each step exists because the naive split got
    it wrong:

    1. **A container** (list, dict, JSON string) is flattened without splitting.
    2. **The whole value is tried FIRST.** ``death/doom`` and ``r&b`` are single
       genres, but a blind split turns them into ``death`` + ``doom`` and
       ``r`` + ``b`` — which is how a ``death/doom`` tag became a bare
       ``doom metal`` one, and why the canonical ``death_doom`` entry could
       never match anything.
    3. **Then the safe separators** (``;``, ``|``, ``\\``). A pipe-delimited list
       was previously kept WHOLE and therefore classified as unknown, silently
       losing every genre in it.
    4. **Only then the unsafe ones** (``/``, ``&``), because a composite that
       matched nothing as a whole is the only evidence that they are separators
       here.
    5. **A serialized array** splits on commas too — inside one, commas are
       unambiguously separators.

    Duplicates are collapsed, so a value listed twice by one source counts once.
    """
    tokens: list[str] = []

    for item in _flatten(raw):
        text = item.strip()
        if not text:
            continue

        # Step 2: the whole value, before any splitting.
        if vocab.classify(text).kind != KIND_UNKNOWN:
            tokens.append(text)
            continue

        if _ARRAY_LIKE.match(text):
            parts = _ALL_SPLIT.split(text.strip("[]{}"))
        else:
            parts = _SAFE_SPLIT.split(text)
            if len(parts) == 1:
                parts = _UNSAFE_SPLIT.split(text)

        for part in parts:
            cleaned = part.strip(_STRIP_CHARS)
            if cleaned and cleaned.lower() not in ("null", "none"):
                tokens.append(cleaned)

    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        key = normalize_token(token)
        if key and key not in seen:
            seen.add(key)
            unique.append(token)
    return unique


# ---------------------------------------------------------------------------
# Saving (the Config page)
# ---------------------------------------------------------------------------

def read_override_text() -> str:
    """The override's raw JSON text, or the default's if there is no override."""
    path = override_vocabulary_path()
    if not os.path.isfile(path):
        path = DEFAULT_VOCABULARY_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except Exception as exc:
        logger.warning("Could not read the genre vocabulary", path=path, error=str(exc))
        return ""


def save_vocabulary_text(text: str) -> dict[str, Any]:
    """Validate *text* and write it as the override.

    Refuses to write an invalid vocabulary. That is deliberate: the file is
    read during a scan, and a broken one would silently produce no genres at all
    — far worse than a rejected save with the error shown.
    """
    try:
        data = json.loads(text or "")
    except Exception as exc:
        return {"success": False, "error": f"invalid JSON: {exc}"}

    errors = validate_vocabulary(data)
    if errors:
        return {
            "success": False,
            "error": "the vocabulary is not valid",
            "problems": errors[:20],
            "problem_count": len(errors),
        }

    path = override_vocabulary_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write-then-rename: the scan may be reading this file concurrently, and
        # a truncated read would look like an empty vocabulary.
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.error("Could not save the genre vocabulary", path=path, error=str(exc))
        return {"success": False, "error": str(exc)}

    clear_vocabulary_cache()
    vocab = get_vocabulary()
    logger.info(
        "Genre vocabulary saved",
        path=path,
        genres=len(vocab.genres),
        moods=len(vocab.moods),
    )
    return {
        "success": True,
        "path": path,
        "genres": len(vocab.genres),
        "moods": len(vocab.moods),
        "countries": len(vocab.countries),
    }


def reset_vocabulary() -> dict[str, Any]:
    """Delete the override, falling back to the shipped default."""
    path = override_vocabulary_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    clear_vocabulary_cache()
    vocab = get_vocabulary()
    return {
        "success": True,
        "reset": True,
        "genres": len(vocab.genres),
        "source_path": vocab.source_path,
    }
