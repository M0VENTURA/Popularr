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

# The canonical token parser lives with the classification helpers. Imported at
# module level (not inside the function) because there is no cycle:
# ``album_classification_service`` imports only json/re/structlog and
# ``helpers.normalization_service`` — it does NOT import this module.
from services.catalog.album_classification_service import iter_genre_tokens

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
    # ⚠️ COMBINING MARKS MUST BE DELETED BEFORE PUNCTUATION BECOMES A SPACE.
    #
    # NFKD splits "ü" into "u" + U+0308, and the punctuation pass below now
    # produces a SPACE for anything non-alphanumeric — so "Motörhead" became
    # "moto rhead" and stopped matching "Motorhead". Deleting the marks first
    # keeps the letter and drops the accent, which is the whole point of NFKD
    # here.
    text = "".join(c for c in text if not unicodedata.combining(c))
    # ⭐ PUNCTUATION BECOMES A SPACE, NOT NOTHING.
    #
    # ⚠️ DELETING IT MADE A SEPARATOR AND A JOINER THE SAME KEY. "Rock, Pop" and
    # "Rock Pop" both collapsed to ``rockpop``, so a two-genre LIST matched the
    # single name "Pop Rock" and never split — the reported
    # "Pop, Metal -> Glam Metal" worst case.
    #
    # ⚠️ HYPHENS ARE STILL FOLDED TO A SPACE. "hip-hop" and "hip hop" must
    # remain one key — that is a spelling difference, not a list.
    text = re.sub(r"[\u2010-\u2015\u2212\-]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    # Collapse runs and trim LAST, after both substitutions have run.
    return re.sub(r"\s+", " ", text).strip()


def values_are_alphabetical(values: Any) -> bool:
    """True when a provider returned an alphabetically sorted tag list.

    MusicBrainz/ListenBrainz tag arrays arrive sorted by name, not by
    popularity. Feeding that order into rank-decay made whichever genre sorts
    first look like the strongest evidence — e.g. "alternative rock" beat
    "pop" purely because ``a`` precedes ``p``.
    """
    tokens = [normalize_token(value) for value in values or []]
    tokens = [token for token in tokens if token]
    return len(tokens) >= 4 and tokens == sorted(tokens)


@dataclass(frozen=True)
class GenreScope:
    """Which evidence may create one genre, and whether it takes a slot.

    ⭐ THIS USED TO BE SILENTLY DISCARDED. A v4 vocabulary can carry a ``scope``
    block per genre and the loader had no field for it, so these rules were
    parsed away on load and never applied:

    * ``soundtrack`` / ``film_score`` / ``video_game_music`` declare
      ``require_confirmation: true`` with ``confirm_release_types: ["Soundtrack"]``
      — they must NOT be reachable from a track tag alone. Probed before this
      fix: three track-level "Soundtrack" tags on a plain album produced
      ``Soundtrack`` as a final genre.
    * ``christmas`` / ``comedy`` / ``soundtrack`` declare ``slot: "marker"`` —
      written on top of the selected genres, never consuming a Maximum Genres
      slot. Markers were previously only the hardcoded ``_FILTER_TAGS`` in
      Python, so a vocabulary could not change them.

    An EMPTY ``levels`` means "the vocabulary declares nothing here", not "no
    levels allowed" — the policy default applies.
    """

    #: Evidence levels that may create this genre (``track``/``album``/``artist``).
    levels: tuple[str, ...] = ()
    #: ``genre`` (default) or ``marker`` (never uses a Maximum Genres slot).
    slot: str = "genre"
    #: MusicBrainz release-group secondary types that confirm it.
    confirm_release_types: tuple[str, ...] = ()
    #: Regexes against the ALBUM title that confirm it.
    confirm_album_title_patterns: tuple[str, ...] = ()
    #: Regexes against the TRACK title that confirm it.
    confirm_title_patterns: tuple[str, ...] = ()
    #: ``True`` = rejected unless one of the confirm rules above matches.
    require_confirmation: bool = False
    #: Artist-level evidence only counts with at least this many source groups.
    artist_requires_min_sources: int = 0

    @property
    def is_marker(self) -> bool:
        return self.slot == "marker"

    @property
    def is_default(self) -> bool:
        """True when nothing was declared, i.e. the policy default applies."""
        return not (
            self.levels
            or self.confirm_release_types
            or self.confirm_album_title_patterns
            or self.confirm_title_patterns
            or self.require_confirmation
            or self.artist_requires_min_sources
            or self.is_marker
        )


#: The scope a genre gets when the vocabulary declares none for it.
DEFAULT_GENRE_SCOPE = GenreScope()


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
    #: Which evidence may create it, and whether it takes a slot.
    scope: GenreScope = DEFAULT_GENRE_SCOPE
    #: Genres that REPLACE this one when they are also selected.
    #:
    #: ⭐ A DATA-DRIVEN redundancy list, declared per genre rather than inferred
    #: from the hierarchy. The ancestry walk cannot see these relationships: a
    #: ``Nu Metal`` track does not need ``Heavy Metal`` restating the umbrella it
    #: descends from, but ``heavy_metal`` is not an ANCESTOR of ``nu_metal`` in the
    #: graph (they meet at ``metal``). This list is the collection's own statement
    #: of which names stop being informative once a narrower one lands.
    #:
    #: Direction matters: the list belongs to the genre that LOSES. If any listed
    #: id is also selected, this entry is dropped.
    redundant_with: tuple[str, ...] = ()

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
        """The nearest DECLARED family, starting with this entry's OWN.

        Declared rather than derived from the root, because ``metal`` sits under
        ``rock`` (so §9's "Rock + Death Metal -> Death Metal" works) while still
        being its own FAMILY — otherwise the family-slot limit would treat every
        rock and metal genre as one branch and cap them together.

        ⚠️ ``self.family`` is checked FIRST. The ancestor walk alone is not
        enough: ``metal`` declares family "metal" but its only ancestor is
        ``rock``, so skipping self returned **"rock"** for metal — the exact
        opposite of the branch it is meant to name. Production was accidentally
        safe because the sole call site is ``entry.family or
        entry.family_of(...)`` and ``metal`` declares its own family, so the
        ``or`` short-circuited — but the method's own docstring invites a direct
        call, and that call would have silently answered "rock".

        Ordering note: a genre with its own family ignores its parents', which is
        what lets ``metalcore`` be metal while still descending (also) from
        ``hardcore_punk``.
        """
        if self.family:
            return self.family
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
    #: Values that are never a genre, mood, place or attribute. Dropped before
    #: scoring. ⭐ This block used to be discarded: a hand-written
    #: ``noise_values`` list had no effect, and values the junk HEURISTIC did
    #: not recognise ("alternative & punk", "folk, world, & country") reached
    #: the final genres anyway.
    noise_values: frozenset[str] = frozenset()
    source_path: str = ""
    is_override: bool = False
    errors: tuple[str, ...] = ()
    #: The ``tokenizer`` block: delimiter and protected-value configuration.
    #:
    #: ⭐ IN THE JSON SO IT IS EDITABLE, not hardcoded — the protected list is a
    #: statement about how THIS collection's sources spell things.
    tokenizer: dict[str, Any] = field(default_factory=dict)

    def is_noise_value(self, raw: Any) -> bool:
        """True when ``raw`` is listed in the vocabulary's ``noise_values``.

        Matched on the NORMALISED token, so a punctuation difference between the
        list and a provider's spelling ("1–4 wochen" vs "1-4 wochen") still
        matches. Only exact normalised equality counts: a substring test would
        drop "folk" because "folk, world" was listed.
        """
        if not self.noise_values:
            return False
        return normalize_token(raw) in self.noise_values

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

    def deciding_family(self, tokens: Any) -> tuple[str, list[str]]:
        """The family that DECIDES a mixed genre set, and the tokens it cannot judge.

        Returns ``(family, unknown)`` — ``family`` is "" when nothing in the set
        is a genre the vocabulary knows, and ``unknown`` lists the tokens it
        could not classify. Callers need BOTH: the family answers the question
        for everything recognised, and ``unknown`` is the only part still
        eligible for a fallback test. Handing the whole set to a fallback after a
        considered non-metal answer re-introduces the bug this method exists to
        fix. (The former caller, ``_track_is_metal_by_identity``, was removed with
        the Genre Exclusions feature; ``deciding_family`` remains the shared
        "which family does this set belong to" authority.)

        ⭐ The rule is the requested one: *"use the third or fourth genre as a tie
        breaker — grunge is usually more aligned to rock than alt metal, where nu
        metal is closer to metal."*

        Rank the tokens by (specificity DESC, then their given order) and take
        the FIRST whose family is known. A genre's position is therefore its
        vote, and specificity means a concrete subgenre outranks the generic
        ``rock`` / ``metal`` umbrella.

        ⚠️ Why ordering decides, and family membership alone cannot: Audioslave
        and Slipknot have IDENTICAL family structure — both carry rock-family AND
        metal-family genres. No set-membership test separates them. The ORDER
        does:

            Audioslave  alternative_rock(rock/1) hard_rock(rock/1)
                        grunge(rock/2) alternative_metal(metal/2)
                        -> most specific tie at 2: grunge (rock) is earlier -> ROCK

            Slipknot    nu_metal(metal/2) alternative_metal(metal/2)
                        heavy_metal(metal/1) alternative_rock(rock/1)
                        -> most specific are both metal -> METAL
        """
        ranked: list[tuple[int, str, int]] = []
        unknown: list[str] = []
        for position, token in enumerate(iter_genre_tokens(tokens)):
            classified = self.classify(token)
            if classified.kind != KIND_GENRE:
                unknown.append(token)
                continue
            entry = self.genres.get(classified.canonical)
            if entry is None:
                unknown.append(token)
                continue
            # ``family`` is resolved at load time (declared, else inherited), so
            # it is never empty for a real genre — but an override file caught
            # mid-edit could carry one, and "" must not vote.
            if entry.family:
                ranked.append((entry.specificity, entry.family, position))
            else:
                unknown.append(token)

        if not ranked:
            return "", unknown
        # ⚠️ Position is sorted on EXPLICITLY, not left to the sort being
        # stable. Relying on stability worked, but it made the tie-break an
        # emergent property of the implementation rather than a stated rule —
        # and it left the recorded `position` unread, so removing it changed
        # nothing and the tie-break was untestable at this level.
        ranked.sort(key=lambda row: (-row[0], row[2]))
        return ranked[0][1], unknown

    def family_decider(self, tokens: Any) -> str:
        """Just the deciding family — ``deciding_family`` without the unknowns."""
        return self.deciding_family(tokens)[0]


#: Sources that are ONE voice for corroboration, because one derives from the
#: other. ListenBrainz's genres are a projection of MusicBrainz's, so counting
#: them separately would let a single upstream opinion satisfy a two-source
#: requirement on its own.
SOURCE_GROUP_OF: dict[str, str] = {
    "musicbrainz": "metabrainz",
    "musicbrainz_tags": "metabrainz",
    "listenbrainz": "metabrainz",
    "lastfm": "lastfm",
    "discogs": "discogs",
    "wikidata": "wikidata",
    "audiodb": "audiodb",
    "spotify": "spotify",
    "essentia": "essentia",
    "navidrome": "navidrome",
    "manual": "manual",
    "library": "navidrome",
    "context": "context",
}


def source_group_count(sources: Any) -> int:
    """How many INDEPENDENT voices a set of sources represents.

    ``{"musicbrainz", "listenbrainz"}`` is ONE opinion, not two — which is what
    ``scope.artist_requires_min_sources`` means by "sources". An unknown source
    counts as its own group so a new provider is never silently merged.
    """
    return len(
        {
            SOURCE_GROUP_OF.get(str(source).strip().lower(), str(source).strip().lower())
            for source in (sources or ())
            if str(source or "").strip()
        }
    )


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

    # ⚠️ A scope typo is SILENT: an unknown level or a bad regex simply never
    # matches, so a genre is either never created or never confirmed. Validated
    # here because the Config page validates before it saves.
    for raw in genres:
        if not isinstance(raw, dict):
            continue
        gid = str(raw.get("id") or "").strip()
        scope = raw.get("scope")
        if scope is None:
            continue
        if not isinstance(scope, dict):
            errors.append(f"genre {gid!r} scope must be an object")
            continue
        levels = scope.get("levels")
        if levels is not None:
            if not isinstance(levels, list):
                errors.append(f"genre {gid!r} scope.levels must be a list")
            else:
                for level in levels:
                    if str(level) not in ("track", "album", "artist"):
                        errors.append(
                            f"genre {gid!r} scope.levels has {level!r}; "
                            "expected track, album or artist"
                        )
        slot = scope.get("slot")
        if slot is not None and str(slot) not in ("genre", "marker"):
            errors.append(
                f"genre {gid!r} scope.slot is {slot!r}; expected 'genre' or 'marker'"
            )
        for pattern_key in ("confirm_album_title_patterns", "confirm_title_patterns"):
            for pattern in _string_tuple(scope.get(pattern_key)):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(
                        f"genre {gid!r} scope.{pattern_key} is not a valid regex: {exc}"
                    )

    noise = data.get("noise_values")
    if noise is not None:
        raw_noise = noise.get("values") if isinstance(noise, dict) else noise
        if not isinstance(raw_noise, list):
            errors.append("'noise_values' must be a list, or an object with a 'values' list")

    geo = data.get("geography")
    if geo is not None and not isinstance(geo, dict):
        errors.append("'geography' must be an object when present")

    return errors


def _string_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a JSON list of strings (or a lone string) to a tuple."""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item or "").strip())


def _parse_scope(raw: Any) -> GenreScope:
    """Parse one entry's ``scope`` block. Absent or malformed = the default."""
    if not isinstance(raw, dict):
        return DEFAULT_GENRE_SCOPE

    slot = str(raw.get("slot") or "genre").strip().lower()
    if slot not in ("genre", "marker"):
        slot = "genre"

    try:
        min_sources = int(raw.get("artist_requires_min_sources") or 0)
    except (TypeError, ValueError):
        min_sources = 0

    return GenreScope(
        levels=_string_tuple(raw.get("levels")),
        slot=slot,
        confirm_release_types=_string_tuple(raw.get("confirm_release_types")),
        confirm_album_title_patterns=_string_tuple(raw.get("confirm_album_title_patterns")),
        confirm_title_patterns=_string_tuple(raw.get("confirm_title_patterns")),
        require_confirmation=bool(raw.get("require_confirmation", False)),
        artist_requires_min_sources=max(0, min_sources),
    )


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
            scope=_parse_scope(raw.get("scope")),
            redundant_with=tuple(
                str(r).strip() for r in (raw.get("redundant_with") or [])
                if r and str(r).strip()
            ),
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
            # ⚠️ Carried through: this rebuild exists only to back-fill the
            # inherited family, and dropping scope here re-discarded every
            # rule a second time (the file parsed, and the value still arrived
            # empty at the selector).
            scope=entry.scope,
            redundant_with=entry.redundant_with,
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

    #: ``noise_values.values`` — matched on the NORMALISED token, so "1–4 wochen"
    #: and "1-4 wochen" (en dash vs hyphen) are one entry.
    noise_block = data.get("noise_values")
    raw_noise = noise_block.get("values") if isinstance(noise_block, dict) else noise_block
    noise_values = frozenset(
        token for token in (normalize_token(v) for v in (raw_noise or [])) if token
    )

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
        noise_values=noise_values,
        source_path=path,
        is_override=is_override,
        errors=errors,
        # ⭐ Carried through so ``tokenize_values`` can read
        # ``protected_values`` without re-reading the file (a caller may have
        # built a vocabulary from an override or a test fixture).
        tokenizer=dict(data.get("tokenizer") or {}) if isinstance(data.get("tokenizer"), dict) else {},
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
_UNSAFE_SPLIT = re.compile(r"[/&+]+")

#: ``", "`` is how this codebase WRITES a genre list into a TEXT column
#: (``", ".join(genres)`` in the album blend, the VA sync and the genre resync).
_COMMA_SPLIT = re.compile(r"\s*,\s*")

#: ⭐ THE HARD DELIMITERS: sources use these to SEPARATE genres and never inside
#: a genre name. They are split FIRST, before any whole-value match, because a
#: whole-value match on a list is always wrong — see ``tokenize_values``.
#:
#: ``\`` is included because some source columns are backslash-delimited
#: ("Nu Metal\\Alternative Rock") and no genre name contains one.
_HARD_DELIMITERS = re.compile(r"[;,|\\]+")

#: ⭐ THE SOFT JOINERS: they separate genres in a list ("Funk / Soul") AND occur
#: inside real names ("death/doom", "r&b", "drum & bass"). Only split when the
#: whole value does not resolve.
_SOFT_JOINERS = re.compile(r"[/&+]+")

#: Values that are GENUINELY one name containing a hard delimiter. Kept in the
#: vocabulary JSON (``tokenizer.protected_values``) so they are editable rather
#: than hardcoded here; this is the fallback when the block is absent.
_DEFAULT_PROTECTED_VALUES = (
    "Folk, World, & Country",
    "Stage & Screen",
    "Brass & Military",
    "Children's",
)

#: A serialized array (``['Doom Metal', 'Rock']``). Inside one, commas ARE
#: separators — that is one context where splitting on ``,`` is safe outright.
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


def _tokenizer_config(vocab: "Vocabulary") -> tuple[frozenset[str], tuple[str, ...]]:
    """``(protected_normalised, soft_joiners)`` from the vocabulary's tokenizer block.

    ⭐ THE PROTECTED LIST LIVES IN THE JSON so it is editable, not hardcoded. The
    module constant is only the fallback for a vocabulary written before the
    block existed.
    """
    protected = _DEFAULT_PROTECTED_VALUES
    raw = getattr(vocab, "tokenizer", None) or {}
    listed = raw.get("protected_values") if isinstance(raw, dict) else None
    if isinstance(listed, list) and listed:
        protected = tuple(str(v) for v in listed if str(v or "").strip())
    return frozenset(normalize_token(v) for v in protected), protected


def _split_on_separators(text: str) -> list[str]:
    """Split on the separator hierarchy, most reliable first."""
    parts = _SAFE_SPLIT.split(text)
    if len(parts) == 1:
        parts = _COMMA_SPLIT.split(text)
    if len(parts) == 1:
        parts = _UNSAFE_SPLIT.split(text)
    return parts


def _all_parts_are_known(parts: list[str], vocab: "Vocabulary") -> bool:
    """Every part resolves to something the vocabulary knows.

    ⭐ THIS IS WHAT SEPARATES A LIST FROM A NAME. "Rock, Pop" splits into two
    genres, so it was a list. "death/doom" splits into ``death`` (unknown) and
    ``doom``, so it is one name and must stay whole.
    """
    candidates = [p.strip() for p in parts if p and p.strip()]
    if len(candidates) < 2:
        return False
    return all(vocab.classify(p).kind != KIND_UNKNOWN for p in candidates)


def tokenize_values(raw: Any, vocab: "Vocabulary") -> list[str]:
    """Split a raw source value into candidate tokens.

    ⭐⭐ HARD DELIMITERS SPLIT FIRST, WITHOUT A WHOLE-VALUE MATCH.

    ``;`` ``,`` ``|`` are how a source SEPARATES genres; they never appear inside
    one. Matching the whole value first made "Rock, Pop" resolve to ``pop_rock``,
    because ``normalize_token`` deleted the comma and produced the same key as
    "Pop Rock" — and that is not a Pop Rock edge case:

        Pop, Metal   -> Glam Metal (via the "pop metal" alias)   <- worst case
        Punk, Rock   -> Punk (the Rock reading was lost)
        Folk, Rock   -> Folk Rock          Industrial, Metal -> Industrial Metal
        Alternative, Rock -> Alternative Rock     Emo, Pop -> Emo Pop

    Any two genre names that together spell a compound genre collided, so the
    list had to be split before anything else was considered.

    ⭐ PROTECTED VALUES ARE THE ONE EXCEPTION. A handful of real names DO contain
    a comma ("Folk, World, & Country"). They are listed in the vocabulary under
    ``tokenizer.protected_values`` and matched whole, before splitting.

    ⭐ SOFT JOINERS (``/`` ``&`` ``+``) MATCH THE WHOLE VALUE FIRST. They separate
    genres in "Funk / Soul" but occur inside real names — ``death/doom``, ``r&b``,
    ``drum & bass`` — so they only split once the whole value fails to resolve.

    Duplicates are collapsed, so a value listed twice by one source counts once.
    """
    tokens: list[str] = []
    protected, _ = _tokenizer_config(vocab)

    for item in _flatten(raw):
        text = item.strip()
        if not text:
            continue

        parts: list[str]
        if _ARRAY_LIKE.match(text):
            # Inside a serialized array every separator is unambiguous.
            parts = _ALL_SPLIT.split(text.strip("[]{}"))
        elif normalize_token(text) in protected:
            # ⭐ A REAL NAME CONTAINING A COMMA — "Folk, World, & Country".
            tokens.append(text)
            continue
        elif _HARD_DELIMITERS.search(text):
            # ⭐ HARD DELIMITERS SPLIT UNCONDITIONALLY.
            parts = _HARD_DELIMITERS.split(text)
            # A soft joiner inside one part is still ambiguous, so each part
            # gets the whole-first treatment below.
            expanded: list[str] = []
            for part in parts:
                piece = part.strip()
                if not piece:
                    continue
                if _SOFT_JOINERS.search(piece) and (
                    vocab.classify(piece).kind == KIND_UNKNOWN
                ):
                    expanded.extend(
                        p.strip() for p in _SOFT_JOINERS.split(piece) if p.strip()
                    )
                else:
                    expanded.append(piece)
            parts = expanded
        elif _SOFT_JOINERS.search(text):
            # ⭐ SOFT JOINERS MATCH WHOLE FIRST: death/doom, r&b, drum & bass.
            if vocab.classify(text).kind != KIND_UNKNOWN:
                parts = [text]
            else:
                parts = [
                    p.strip() for p in _SOFT_JOINERS.split(text) if p.strip()
                ]
        elif vocab.classify(text).kind != KIND_UNKNOWN:
            parts = [text]
        else:
            parts = [text]

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
