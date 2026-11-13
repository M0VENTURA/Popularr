"""Genre selection: score canonical candidates, condense the hierarchy, select.

The reported problem
--------------------
> Your current "top three genres" approach is sensible, but the selection stage is
> happening too early. The main issue is not the limit of three. It is that broad
> parents, precise subgenres, moods, geography, formats, and malformed source tags
> are all competing for the same three positions.

``genre_vocabulary`` answers "what kind of value is this?". This module is the
rest of the pipeline, in the order the report specifies:

    parse -> normalize -> classify -> canonicalize -> score
          -> condense hierarchy -> select -> write

Scoring NEVER runs on raw values. That ordering is the whole point: scoring first
is what let ``rock`` outrank ``melodic death metal`` and let a German artist's
nationality occupy a genre slot.

Entity levels (the part that was missing entirely)
--------------------------------------------------
Evidence is aggregated by ``canonical genre × source × entity level``, because the
three levels are not equally reliable:

| entity | weight | why |
|---|---|---|
| track | 1.00 | the recording's own tags — the most specific evidence |
| album | 0.70 | the release's tags — more specific than the artist, less than the track |
| artist | 0.35 | an artist spans styles; this is SUPPORT, not a verdict |

The old engine flattened all three into one map keyed only by source, so an
artist-level ``rock`` tag carried the same authority as a track-level
``melodic death metal`` one. Callers declare a level with the ``source@entity``
key convention; a bare ``source`` key is treated as ``track``.

Thresholds are on CONFIDENCE, not raw score
-------------------------------------------
``score`` is a weighted sum and can exceed 1 (the report's own worked example
reaches 2.54), so the report's "must exceed 0.70" cannot be applied to it
directly. Confidence is therefore ``score / reference``, where ``reference`` is
what ONE strong track-level source at rank 0 produces — i.e. confidence 1.0 means
"as well supported as a single authoritative source can make it". That
calibration reproduces the report's stated outcomes for its own examples, and
every number is configurable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import structlog

from services.enrichment.genre_vocabulary import (
    KIND_GENRE,
    Vocabulary,
    get_vocabulary,
    normalize_token,
)

logger = structlog.get_logger(__name__)

#: Entity levels, most to least specific.
ENTITY_TRACK = "track"
ENTITY_ALBUM = "album"
ENTITY_ARTIST = "artist"

ENTITY_ORDER: tuple[str, ...] = (ENTITY_TRACK, ENTITY_ALBUM, ENTITY_ARTIST)

#: A source key may carry its entity level as ``source@entity``.
ENTITY_SEPARATOR = "@"

#: Rank decay: the k-th tag a source lists is weaker evidence than its first.
#: Kept from the existing engine — it is the "source popularity" term.
def _rank_decay(rank: int) -> float:
    import math

    return 1.0 / math.log2(rank + 2)


@dataclass
class Contribution:
    """One source's assertion of one canonical genre."""

    source: str
    entity: str
    weight: float
    raw: str


@dataclass
class Candidate:
    """A canonical genre, with everything known about its support."""

    genre_id: str
    display: str
    family: str
    specificity: int
    score: float = 0.0
    confidence: float = 0.0
    sources: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)
    contributions: list[Contribution] = field(default_factory=list)
    manual: bool = False

    @property
    def entity_count(self) -> int:
        return len(self.entities)

    def has_entity(self, entity: str) -> bool:
        return entity in self.entities

    def best_entity(self) -> str:
        for entity in ENTITY_ORDER:
            if entity in self.entities:
                return entity
        return ENTITY_ARTIST

    def to_dict(self) -> dict[str, Any]:
        return {
            "genre": self.display,
            "id": self.genre_id,
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 3),
            "sources": sorted(self.sources),
            "entities": sorted(self.entities),
            "specificity": self.specificity,
            "family": self.family,
            "manual": self.manual,
            "evidence": [
                {
                    "provider": c.source,
                    "entity": c.entity,
                    "raw_value": c.raw,
                    "weight": round(c.weight, 3),
                }
                for c in self.contributions
            ],
        }


@dataclass(frozen=True)
class SelectionSettings:
    """Every tunable, with the report's recommended starting values."""

    #: How many MUSICAL genres survive cleanup and reach the tag. This is the
    #: stored cardinality, not the pool — see :attr:`selection_pool`.
    max_genres: int = 4
    #: How many candidates are taken from the ranking BEFORE the collection
    #: policy runs. The policy needs a few alternatives to choose between: with
    #: only the final count in hand, suppressing a rock genre leaves an empty
    #: slot that cannot be back-filled by the next-best candidate.
    selection_pool: int = 6
    #: The collection policy: an accepted metal genre (other than Nu Metal)
    #: removes rock-family genres from the final set.
    metal_suppresses_rock: bool = True
    #: How much metal evidence the policy needs before it may overrule a STRONGER
    #: rock body of evidence. Metal must reach this fraction of the rock side's
    #: total score, or the policy declines and both sides stand.
    #:
    #: Without this the rule fired on the mere PRESENCE of a metal tag, so a
    #: post-hardcore/emo track carrying two stray metal tags lost Alternative
    #: Rock, Emo and Pop Rock — the genres that actually describe it — and came
    #: out "Post-Hardcore, Alternative Metal, Heavy Metal".
    #:
    #: 0.8, not 1.0: the intended cases (Gothic Rock + Gothic Metal, Folk Rock +
    #: Folk Metal) are close calls where the metal side is usually the LOWER
    #: score, and requiring parity would switch the policy off for exactly the
    #: pairs it exists to settle. Measured against the reported track, whose
    #: metal/rock ratio is 0.62.
    metal_over_rock_min_ratio: float = 0.8
    #: Nu Metal on its own is a metal/rock crossover, so it does NOT suppress
    #: the approved rock companions below.
    nu_metal_exception: bool = True
    #: Genres kept beside Nu Metal. Deliberately a short list: extending it to
    #: every rock genre would make the exception swallow the whole policy.
    nu_metal_rock_companions: tuple[str, ...] = ("hard_rock", "alternative_rock")
    #: Drop a specificity-0 family root ("Metal", "Rock") when a more specific
    #: genre of the same family is also selected.
    remove_generic_parents: bool = True
    #: source -> weight (the report's "recommended starting weights").
    source_weights: dict[str, float] = field(default_factory=dict)
    #: entity level -> weight.
    entity_weights: dict[str, float] = field(default_factory=dict)
    #: Bonus for corroboration across INDEPENDENT sources.
    agreement_2: float = 0.30
    agreement_3: float = 0.20
    #: Per-point bonus for a more specific genre.
    specificity_bonus: float = 0.05
    #: Subtracted when every contribution is artist-level.
    artist_only_penalty: float = 0.15
    #: Subtracted from a microgenre (specificity 3) with a single source.
    microgenre_single_source_penalty: float = 0.10
    #: Confidence floors, by position (1-based). The fourth entry exists because
    #: the final count is now up to four; a position beyond this tuple reuses the
    #: last value rather than dropping its requirement entirely.
    thresholds: tuple[float, ...] = (0.60, 0.45, 0.35, 0.35)
    #: Each position must also reach this fraction of the position ABOVE it,
    #: measured on raw score.
    #:
    #: ⚠️ The report's prose says 45% for position 2 and 55% for position 3, but
    #: its own worked example has position 3 at 45% of position 2 (0.74 / 1.66)
    #: — the prose rule would reject the trio the example says to save. The
    #: example is the checkable statement, so ~45% is used for both and the value
    #: is configurable. The rule's JOB is to stop a much weaker candidate taking
    #: a slot, and 45% does that; 55% additionally rejects the report's own case.
    relative_floors: tuple[float, ...] = (0.0, 0.45, 0.45, 0.45)
    #: At most this many genres from one family branch...
    family_slot_limit: int = 2
    #: ...unless the extra one has track-level evidence from this many sources.
    family_override_sources: int = 2
    #: A family whose total score is below this share of the strongest family
    #: is treated as an outlier and removed before slots are filled.
    family_consensus_min_share: float = 0.30
    #: Pairs that must NOT be collapsed to their ancestor.
    keep_together: frozenset[tuple[str, str]] = frozenset()
    #: Pairs collapsed to the broader one ONLY when the narrower is unsupported.
    conditional_keep: frozenset[tuple[str, str]] = frozenset()
    #: Pairs that must NEVER both survive. The broader ancestor is dropped even
    #: when it scores higher, so a track carries ONE identity from the pair.
    mutually_exclusive: frozenset[tuple[str, str]] = frozenset()
    #: A more specific descendant replaces its ancestor only when it reaches
    #: this fraction of the ancestor's score. A weaker ``synthpop`` must not
    #: delete the better-supported ``pop`` tag.
    ancestor_replace_min_ratio: float = 0.80


#: The report's "recommended starting values" for provider trust. Deliberately
#: NOT the legacy ``genres.weights`` block: that one was tuned for the old
#: vote-counting engine and puts Last.fm above ListenBrainz, which the report
#: reverses. Both blocks coexist so switching engines switches the whole model.
DEFAULT_SOURCE_WEIGHTS: dict[str, float] = {
    "musicbrainz": 1.00,   # proper genres: the strongest controlled signal
    "musicbrainz_tags": 0.65,  # free-form folksonomy: must pass classification
    "wikidata": 0.80,
    "listenbrainz": 0.85,
    "lastfm": 0.70,
    "discogs": 0.75,
    "audiodb": 0.55,
    "essentia": 0.45,
    "navidrome": 0.30,     # local file tags: a tie-breaker, never a sole source
    "spotify": 0.40,
    "library": 0.55,
    "context": 0.50,
    "manual": 1.25,        # an intentional correction outranks everything
}

DEFAULT_ENTITY_WEIGHTS: dict[str, float] = {
    ENTITY_TRACK: 1.00,
    ENTITY_ALBUM: 0.70,
    ENTITY_ARTIST: 0.35,
}

#: Pairs that describe different aspects of a sound, so the ancestor rule must
#: not collapse them even where the hierarchy makes one a parent.
#:
#: ⚠️ ``alternative_metal`` + ``alternative_rock`` is deliberately NOT here.
#: ``alternative_metal`` descends from BOTH ``metal`` and ``alternative_rock``,
#: and the pair is ``mutually_exclusive``, so a track keeps ONE "Alternative *"
#: genre: the more specific alternative metal replaces its alternative-rock
#: ancestor even when the rock tag scores higher. The reported "Alternative
#: Rock, Alternative Metal, Cover" is the regression this prevents.
#:
#: ⚠️ ``gothic_rock`` + ``gothic_metal`` was REMOVED deliberately. It used to be
#: kept as "different aspects of a sound", but the collection policy is that an
#: accepted metal genre suppresses its rock counterpart, and Gothic Metal /
#: Gothic Rock is exactly that case. ``keep_together`` would have exempted it
#: from the policy and preserved both.
#:
#: ⚠️ ``industrial`` + ``industrial_metal`` was REMOVED for the same reason. With
#: the vocabulary's missing ``industrial`` parent edge added to
#: ``industrial_metal``, ``Industrial`` IS its ancestor again — and an umbrella
#: kept beside every one of its children is the reported violation ("industrial
#: should be removed by industrial metal", 673 tracks in the export). It is not
#: two aspects of one sound; the metal one names it precisely.
DEFAULT_KEEP_TOGETHER: frozenset[tuple[str, str]] = frozenset({
    ("pop_punk", "emo"),
    ("metalcore", "melodic_death_metal"),
    ("folk_metal", "power_metal"),
    ("progressive_metal", "djent"),
    ("shoegaze", "dream_pop"),
    ("post_punk", "gothic_rock"),
    ("hard_rock", "blues_rock"),
    ("nu_metal", "alternative_metal"),
})

#: The report's "conditionally condense" list: collapse to the broader genre
#: unless the narrower one has independent track/album evidence.
DEFAULT_CONDITIONAL_KEEP: frozenset[tuple[str, str]] = frozenset({
    ("hard_rock", "heavy_metal"),
    ("metalcore", "hardcore_punk"),
})

#: Pairs where only ONE may appear on a track. ``alternative_metal`` descends
#: from ``alternative_rock``, so the ancestor is dropped UNCONDITIONALLY: a
#: track must not carry both "Alternative Rock" and "Alternative Metal" even
#: when the rock tag has more sources (the reported regression).
DEFAULT_MUTUALLY_EXCLUSIVE: frozenset[tuple[str, str]] = frozenset({
    ("alternative_metal", "alternative_rock"),
})


def parse_source_key(key: str) -> tuple[str, str]:
    """Split ``source@entity`` into its parts. A bare key is TRACK-level.

    Backwards compatible on purpose: every existing caller passes a bare source
    name, and treating those as track-level is both the previous behaviour and
    the most common truth (they are a track's own columns).
    """
    text = str(key or "").strip()
    if not text:
        return ("", ENTITY_TRACK)
    if ENTITY_SEPARATOR in text:
        source, _, entity = text.partition(ENTITY_SEPARATOR)
        source = source.strip()
        entity = entity.strip().lower()
        if entity not in ENTITY_ORDER:
            entity = ENTITY_TRACK
        return (source, entity)
    return (text, ENTITY_TRACK)


def _settings_from_config(settings: SelectionSettings | None) -> SelectionSettings:
    if settings is not None:
        return settings
    try:
        from helpers.config_helpers import get_genre_selection_config

        raw = get_genre_selection_config()
    except Exception:
        raw = {}

    source_weights = dict(DEFAULT_SOURCE_WEIGHTS)
    source_weights.update(
        {k: float(v) for k, v in (raw.get("source_weights") or {}).items() if v is not None}
    )
    entity_weights = dict(DEFAULT_ENTITY_WEIGHTS)
    entity_weights.update(
        {k: float(v) for k, v in (raw.get("entity_weights") or {}).items() if v is not None}
    )

    thresholds = raw.get("thresholds") or {}
    try:
        t1 = float(thresholds.get("first", 0.60))
        t2 = float(thresholds.get("second", 0.45))
        t3 = float(thresholds.get("third", 0.35))
        # A fourth floor exists because the final count is up to four now.
        # Defaulting it to the third's value keeps a three-entry config working
        # unchanged while still requiring the fourth slot to earn itself.
        t4 = float(thresholds.get("fourth", t3))
    except (TypeError, ValueError):
        t1, t2, t3, t4 = 0.60, 0.45, 0.35, 0.35

    def _pairs(key: str, default: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
        listed = raw.get(key)
        if not isinstance(listed, list):
            return default
        out: set[tuple[str, str]] = set()
        for item in listed:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.add((str(item[0]).strip(), str(item[1]).strip()))
            elif isinstance(item, str) and "|" in item:
                a, _, b = item.partition("|")
                out.add((a.strip(), b.strip()))
        return frozenset(out) if out else default

    def _num(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    def _int(key: str, default: int) -> int:
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    def _bool(key: str, default: bool) -> bool:
        """A config flag, accepting the YAML spellings the Config page writes.

        ``bool("false")`` is True, so a string value written by the page must be
        interpreted rather than coerced — otherwise switching the policy OFF in
        the UI would silently leave it ON.
        """
        value = raw.get(key, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().casefold()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        return default

    def _companions() -> tuple[str, ...]:
        """The genres kept beside Nu Metal, canonicalised through the vocabulary.

        Accepts ids ("hard_rock") or display names ("Hard Rock"), so a
        hand-edited config cannot quietly disable the exception by writing the
        readable spelling.
        """
        listed = raw.get("nu_metal_rock_companions")
        if not isinstance(listed, list):
            return DEFAULT_NU_METAL_COMPANIONS
        vocab = get_vocabulary()
        out: list[str] = []
        for item in listed:
            text = str(item or "").strip()
            if not text:
                continue
            key = text.casefold().replace(" ", "_")
            gid = key if key in vocab.genres else vocab.genre_lookup.get(
                normalize_token(text), ""
            )
            # An unknown spelling is kept as an id: the policy simply will not
            # match a genre that is not in the vocabulary, which is the safe
            # direction (it cannot permit an unintended genre).
            gid = gid or key
            if gid not in out:
                out.append(gid)
        return tuple(out) if out else DEFAULT_NU_METAL_COMPANIONS

    return SelectionSettings(
        max_genres=max(1, _int("max_genres", 4)),
        selection_pool=max(1, _int("selection_pool", 6)),
        metal_suppresses_rock=_bool("metal_suppresses_rock", True),
        nu_metal_exception=_bool("nu_metal_exception", True),
        nu_metal_rock_companions=_companions(),
        remove_generic_parents=_bool("remove_generic_parents", True),
        source_weights=source_weights,
        entity_weights=entity_weights,
        agreement_2=_num("agreement_2", 0.30),
        agreement_3=_num("agreement_3", 0.20),
        specificity_bonus=_num("specificity_bonus", 0.05),
        artist_only_penalty=_num("artist_only_penalty", 0.15),
        microgenre_single_source_penalty=_num("microgenre_single_source_penalty", 0.10),
        thresholds=(t1, t2, t3, t4),
        relative_floors=(0.0, 0.45, 0.45, 0.45),
        family_slot_limit=max(1, _int("family_slot_limit", 2)),
        family_override_sources=max(1, _int("family_override_sources", 2)),
        family_consensus_min_share=_num("family_consensus_min_share", 0.30),
        # How much metal evidence is needed before the metal-over-rock policy may
        # overrule a stronger rock body of evidence.
        metal_over_rock_min_ratio=_num("metal_over_rock_min_ratio", 0.80),
        ancestor_replace_min_ratio=_num("ancestor_replace_min_ratio", 0.80),
        keep_together=_pairs("keep_together", DEFAULT_KEEP_TOGETHER),
        conditional_keep=_pairs("conditional_keep", DEFAULT_CONDITIONAL_KEEP),
        mutually_exclusive=_pairs(
            "mutually_exclusive", DEFAULT_MUTUALLY_EXCLUSIVE
        ),
    )


# ---------------------------------------------------------------------------
# Candidate collection
# ---------------------------------------------------------------------------

def collect_candidates(
    source_map: dict[str, Any],
    *,
    vocab: Vocabulary | None = None,
    settings: SelectionSettings | None = None,
) -> tuple[list[Candidate], dict[str, list[str]]]:
    """Classify every value and score the GENRE candidates.

    Returns ``(candidates, moved)``, where ``moved`` names the values that were
    recognised but are not genres — the moods, geography, attributes and junk
    that used to compete for a genre slot. Returned so a caller can report what
    was set aside rather than silently dropping it.

    ⚠️ One contribution per ``(source, entity, genre)``. A source that lists
    ``rock``, ``alternative rock`` and ``alt rock`` has made ONE statement about
    ``alternative_rock``, not three — without the cap a single provider's
    vocabulary quirks would outweigh genuine cross-source agreement.
    """
    vocab = vocab or get_vocabulary()
    settings = settings or _settings_from_config(None)

    from services.enrichment.genre_aggregation_service import (
        is_admin_genre,
        is_junk_genre,
    )
    from services.enrichment.genre_vocabulary import (
        tokenize_values,
        values_are_alphabetical,
    )

    candidates: dict[str, Candidate] = {}
    moved: dict[str, list[str]] = {}
    seen_pairs: set[tuple[str, str, str]] = set()

    for key, raw_values in (source_map or {}).items():
        source, entity = parse_source_key(key)
        if not source:
            continue

        # Vocabulary-aware: the whole value is tried before any splitting, so
        # ``death/doom`` stays one genre and a pipe-delimited list is split
        # instead of being kept whole and classified as unknown.
        parsed = tokenize_values(raw_values, vocab)
        alphabetically_sorted = values_are_alphabetical(parsed)
        entity_weight = settings.entity_weights.get(entity, DEFAULT_ENTITY_WEIGHTS[ENTITY_TRACK])
        source_weight = settings.source_weights.get(source, 0.10)

        for rank, value in enumerate(parsed):
            if not value:
                continue

            classification = vocab.classify(value)

            if classification.kind != KIND_GENRE:
                # Junk is noise; everything else is real metadata that simply
                # does not belong in a genre slot. Both are recorded so the
                # caller can say what happened to them.
                if classification.kind == "unknown" and (is_junk_genre(value) or is_admin_genre(value)):
                    moved.setdefault("junk", []).append(value)
                elif classification.kind == "unknown":
                    moved.setdefault("unknown", []).append(value)
                else:
                    moved.setdefault(classification.kind, []).append(value)
                continue

            # A track's own "rock" and its album's "rock" are two contributions
            # (different evidence); the same source at the same level twice is not.
            dedupe = (source, entity, classification.canonical)
            if dedupe in seen_pairs:
                continue
            seen_pairs.add(dedupe)

            weight = source_weight * entity_weight * (
                1.0 if alphabetically_sorted else _rank_decay(rank)
            )
            candidate = candidates.get(classification.canonical)
            if candidate is None:
                candidate = Candidate(
                    genre_id=classification.canonical,
                    display=classification.display,
                    family=classification.family,
                    specificity=classification.specificity,
                )
                candidates[classification.canonical] = candidate

            candidate.score += weight
            candidate.sources.add(source)
            candidate.entities.add(entity)
            candidate.contributions.append(
                Contribution(source=source, entity=entity, weight=weight, raw=str(value))
            )
            if source == "manual":
                candidate.manual = True

    return list(candidates.values()), moved


def score_candidates(
    candidates: Iterable[Candidate],
    *,
    settings: SelectionSettings | None = None,
) -> None:
    """Apply the bonuses and penalties, then compute confidence. In place."""
    settings = settings or _settings_from_config(None)
    reference = settings.source_weights.get("musicbrainz", 1.0) * settings.entity_weights.get(
        ENTITY_TRACK, 1.0
    )
    reference = reference or 1.0

    for candidate in candidates:
        independent = len(candidate.sources)

        if independent >= 3:
            candidate.score += settings.agreement_2 + settings.agreement_3
        elif independent >= 2:
            candidate.score += settings.agreement_2

        candidate.score += candidate.specificity * settings.specificity_bonus

        # Artist-only evidence is SUPPORT, not a verdict. §6: artist values
        # "should not routinely override track and album data".
        if candidate.entities == {ENTITY_ARTIST}:
            candidate.score -= settings.artist_only_penalty

        # §11: a very narrow microgenre needs two independent sources.
        if candidate.specificity >= 3 and independent < 2:
            candidate.score -= settings.microgenre_single_source_penalty

        candidate.score = max(0.0, candidate.score)
        candidate.confidence = min(1.0, candidate.score / reference)


# ---------------------------------------------------------------------------
# Removal reasons
# ---------------------------------------------------------------------------
# Every dropped candidate carries ONE reason code, so "why is my genre missing?"
# is answerable without re-running the engine. The code is the stable contract;
# the trigger names the genre that caused the removal.

REASON_OUTSIDE_FINAL_LIMIT = "OUTSIDE_FINAL_LIMIT"
REASON_LOW_CONFIDENCE = "LOW_CONFIDENCE"
REASON_FAMILY_OUTLIER = "INCOMPATIBLE_OUTLIER"
REASON_REDUNDANT_GENERIC_PARENT = "REDUNDANT_GENERIC_PARENT"
REASON_REDUNDANT_ANCESTOR = "REDUNDANT_ANCESTOR"
REASON_ROCK_SUPPRESSED_BY_METAL = "ROCK_SUPPRESSED_BY_METAL"
REASON_ROCK_NOT_ALLOWED_BY_NU_METAL = "ROCK_NOT_ALLOWED_BY_NU_METAL_EXCEPTION"
REASON_EXCLUSIVE_CONFLICT = "EXCLUSIVE_CONFLICT"
REASON_MANUAL_OVERRIDE_LIMIT = "MANUAL_GENRE_KEPT"


FAMILY_METAL = "metal"
FAMILY_ROCK = "rock"

#: The one metal genre that does NOT suppress rock: it is a metal/rock
#: crossover by definition, so removing its rock companion would strip the
#: identity the tag is describing.
NU_METAL_ID = "nu_metal"

#: Rock genres that survive alongside Nu Metal. Kept short on purpose — see
#: ``SelectionSettings.nu_metal_rock_companions``.
DEFAULT_NU_METAL_COMPANIONS: tuple[str, ...] = ("hard_rock", "alternative_rock")


def _removal(candidate: Candidate, reason: str, trigger: str = "") -> dict[str, Any]:
    """One removal record — what was dropped, why, and by what."""
    return {
        "genre": candidate.display,
        "id": candidate.genre_id,
        "reason": reason,
        "trigger": trigger,
    }


def _family_members(
    candidates: Iterable[Candidate], family: str
) -> list[Candidate]:
    return [c for c in candidates if (c.family or "") == family]


def apply_metal_over_rock_policy(
    candidates: list[Candidate],
    *,
    settings: SelectionSettings | None = None,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """The collection policy: metal outranks rock. Returns ``(kept, removed)``.

    This is deliberately NOT a majority-family rule. Once an accepted metal
    genre is present, the rock side of the same track is redundant — that is the
    requested outcome:

        Folk Metal + Folk Rock      -> Folk Metal
        Gothic Metal + Gothic Rock  -> Gothic Metal
        Progressive Metal + Prog Rock -> Progressive Metal

    ⭐ TWO BOUNDARIES THAT MUST NOT MOVE.

    1. **Nu Metal alone is exempt.** Nu Metal is a metal/rock hybrid; stripping
       ``Hard Rock`` from a Nu Metal track deletes the crossover the tag exists
       to name. When Nu Metal is the ONLY accepted metal genre, the approved
       companions in ``settings.nu_metal_rock_companions`` survive.
    2. **Only ROCK-family genres are removed.** The policy says nothing about
       punk, post-hardcore, electronic, hip-hop, pop or R&B — a metalcore track
       keeps its ``Post-Hardcore`` tag, and a nu-metal/electronic track keeps
       ``Hardstyle``. Suppressing those would turn a metal-preference into a
       metal-only filter.

    A MANUAL genre is never removed: an explicit user correction outranks the
    collection policy, which is the whole point of it being manual.
    """
    settings = settings or _settings_from_config(None)
    if not settings.metal_suppresses_rock:
        return candidates, []

    metal = [c for c in _family_members(candidates, FAMILY_METAL)]
    rock = [c for c in _family_members(candidates, FAMILY_ROCK)]
    if not metal or not rock:
        return candidates, []

    # ⭐⭐ THE POLICY NEEDS SUPPORT TO ACT, AND THIS GUARD IS WHY.
    #
    # "Metal outranks rock" was applied UNCONDITIONALLY the moment any metal
    # genre survived. That is right for the cases it exists for — Gothic Rock
    # beside Gothic Metal, Folk Rock beside Folk Metal — where the two describe
    # the same sound at different specificity and the metal one is the answer.
    #
    # It is wrong when the rock evidence is the STRONGER body of evidence and
    # metal is a minority tag. Reported on a Chiodos track (post-hardcore/emo)
    # whose final genres came out "Post-Hardcore, Alternative Metal, Heavy
    # Metal": its family scores were rock 3.85, metal 2.40, punk 2.00, and every
    # rock genre — Alternative Rock, Emo, Pop Rock — was deleted because two
    # stray metal tags were present. The track keeps Post-Hardcore (punk, which
    # the policy correctly leaves alone) but loses the very tags that describe
    # it, and then lands on the Alternative Metal playlist.
    #
    # Requiring the metal side to carry comparable weight keeps the intended
    # outcome and makes a thin metal tag unable to dictate a track's identity —
    # the same "one thin tag must not control the result" principle the outlier
    # rejection already encodes.
    _rock_score = sum(float(c.score or 0.0) for c in rock)
    _metal_score = sum(float(c.score or 0.0) for c in metal)
    if _rock_score > 0 and _metal_score < _rock_score * settings.metal_over_rock_min_ratio:
        logger.debug(
            "Genre metal-over-rock policy declined — the rock evidence is stronger",
            rock_score=round(_rock_score, 3),
            metal_score=round(_metal_score, 3),
            required_ratio=settings.metal_over_rock_min_ratio,
            metal=[c.display for c in metal],
            rock=[c.display for c in rock],
        )
        return candidates, []

    metal_ids = {c.genre_id for c in metal}
    non_nu_metal = [c for c in metal if c.genre_id != NU_METAL_ID]

    removed: list[dict[str, Any]] = []

    if non_nu_metal:
        # Strongest metal genre names the trigger, so the reason explains which
        # genre actually won the conflict.
        trigger = max(non_nu_metal, key=lambda c: (c.confidence, c.score)).display
        permitted = frozenset()
        reason = REASON_ROCK_SUPPRESSED_BY_METAL
    elif NU_METAL_ID in metal_ids and settings.nu_metal_exception:
        trigger = next(c.display for c in metal if c.genre_id == NU_METAL_ID)
        permitted = frozenset(settings.nu_metal_rock_companions)
        reason = REASON_ROCK_NOT_ALLOWED_BY_NU_METAL
    else:
        # Only Nu Metal, but the exception is switched off: the policy applies.
        trigger = next(c.display for c in metal if c.genre_id == NU_METAL_ID)
        permitted = frozenset()
        reason = REASON_ROCK_SUPPRESSED_BY_METAL

    drop: set[str] = set()
    for candidate in rock:
        if candidate.manual:
            continue
        if candidate.genre_id in permitted:
            continue
        drop.add(candidate.genre_id)
        removed.append(_removal(candidate, reason, trigger))

    if not drop:
        return candidates, []

    logger.debug(
        "Genre metal-over-rock policy applied",
        removed=sorted(r["id"] for r in removed),
        reason=reason,
        trigger=trigger,
    )
    return [c for c in candidates if c.genre_id not in drop], removed


def remove_generic_parents(
    candidates: list[Candidate],
    *,
    settings: SelectionSettings | None = None,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Drop a family ROOT when a more specific genre of that family is present.

    ``Metal`` beside ``Melodic Death Metal`` (or ``Death Metal``) spends a slot
    restating the family name: once the evidence names a concrete subgenre, the
    umbrella adds no information. This is the plan's confirmed rule —

        Metal; Death Metal          -> Death Metal
        Rock; Alternative Rock      -> Alternative Rock
        Punk; Hardcore Punk         -> Hardcore Punk
        Electronic; House           -> House

    ⚠️ DELIBERATELY UNCONDITIONAL — the ratio does NOT apply here. The generic
    root usually SCORES HIGHER than its child (``metal`` often leads the list),
    so a "the child must be at least as well supported" guard would keep the
    umbrella almost every time and the rule would never fire. The ratio exists
    for :func:`remove_redundant_ancestors`, where the question is whether a
    NARROWER genre genuinely replaces a different, still-meaningful one. Here
    the pair is a set and its own subset, so support is not the question.

    A child is NEVER inferred from the root, and manual genres are untouched.
    """
    settings = settings or _settings_from_config(None)
    if not settings.remove_generic_parents:
        return candidates, []

    vocab = get_vocabulary()

    drop: set[str] = set()
    removed: list[dict[str, Any]] = []

    for candidate in candidates:
        if candidate.specificity != 0 or candidate.manual:
            continue
        family = candidate.family or candidate.genre_id
        more_specific = [
            other
            for other in candidates
            if other.genre_id != candidate.genre_id
            and other.specificity > 0
            # ⚠️ SAME FAMILY ONLY — deliberately.
            #
            # Families are the collection's own branches: ``metal`` sits under
            # ``rock`` but is its own family, and ``punk`` likewise. Treating a
            # hierarchy walk as the authority would let ``Synthpop`` (declared
            # electronic-family) delete ``Pop`` (pop-family), because synthpop
            # lists ``pop`` as a secondary parent — the reported "the final
            # genres don't really match up" in reverse, and
            # ``test_an_unrelated_family_child_does_not_remove_the_root`` plus
            # ``test_a_generic_root_survives_when_only_an_unrelated_child_is_present``
            # pin it.
            and (other.family or other.genre_id) == family
        ]
        if not more_specific:
            continue
        drop.add(candidate.genre_id)
        trigger = max(more_specific, key=lambda c: (c.confidence, c.score)).display
        removed.append(_removal(candidate, REASON_REDUNDANT_GENERIC_PARENT, trigger))

    if not drop:
        return candidates, []
    return [c for c in candidates if c.genre_id not in drop], removed


def trim_to_final(
    candidates: list[Candidate],
    *,
    settings: SelectionSettings | None = None,
    limit: int | None = None,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Keep up to ``limit`` of the CLEANED set. Returns ``(kept, removed)``.

    ⚠️ DELEGATES to :func:`select_genres` — the ONE implementation of "each
    position must earn its place". It is not a plain top-N slice: every position
    keeps its confidence floor and its relative floor against the leader.
    Without that, raising the final count from 3 to 4 filled the extra slot with
    whatever happened to be next — a lone ``Bebop`` beside a well-supported
    Death Metal track.

    This function only adds the REMOVAL RECORDS, working out which candidates
    ``select_genres`` dropped and why, so a result stays explainable. It must
    not re-implement the selection itself: two copies of these floors would
    drift, and the verbose one always wins by accident.
    """
    settings = settings or _settings_from_config(None)
    if limit is not None:
        settings = SelectionSettings(
            **{**settings.__dict__, "max_genres": max(1, int(limit))}
        )

    selected = select_genres(candidates, settings=settings)
    kept_ids = {c.genre_id for c in selected}
    cap = settings.max_genres

    ranked = sorted(
        candidates,
        key=lambda c: (c.confidence, c.score, c.specificity),
        reverse=True,
    )
    leader = ranked[0] if ranked else None
    leader_score = leader.score if leader else 0.0

    removed: list[dict[str, Any]] = []
    for candidate in ranked:
        if candidate.genre_id in kept_ids:
            continue
        # The reason is REPORTING only — ``select_genres`` already decided.
        # Reporting the most specific explanation available keeps the preview
        # useful without letting it influence the outcome.
        position = len(kept_ids)
        if position >= cap:
            removed.append(
                _removal(candidate, REASON_OUTSIDE_FINAL_LIMIT, f"top {cap}")
            )
        elif not candidate.manual and candidate.confidence < _floor_for(settings, position):
            removed.append(
                _removal(
                    candidate,
                    REASON_LOW_CONFIDENCE,
                    f"floor {_floor_for(settings, position):.2f}",
                )
            )
        elif (
            leader is not None
            and leader_score > 0
            and (candidate.score / leader_score) < _relative_floor_for(settings, position)
        ):
            removed.append(
                _removal(
                    candidate,
                    REASON_LOW_CONFIDENCE,
                    f"below {_relative_floor_for(settings, position):.0%} of {leader.display}",
                )
            )
        else:
            removed.append(_removal(candidate, REASON_LOW_CONFIDENCE, "not selected"))

    return selected, removed


# ---------------------------------------------------------------------------
# Condensation
# ---------------------------------------------------------------------------

def _is_ancestor(ancestor: str, descendant: str, vocab: Vocabulary) -> bool:
    entry = vocab.genres.get(descendant)
    if entry is None:
        return False
    return ancestor in entry.ancestors(vocab.genres)


def _pair_is_kept(a: str, b: str, keep: frozenset[tuple[str, str]]) -> bool:
    return (a, b) in keep or (b, a) in keep


def _names_parent(
    parent: Candidate, child: Candidate, vocab: Vocabulary
) -> bool:
    """Does *child*'s NAME literally contain *parent*'s name?

    Used only to decide whether a CROSS-FAMILY ancestor may be absorbed. The
    vocabulary files several rock-family genres under their own branch
    (``punk_rock`` -> punk family, ``pop_rock`` -> rock family), so a family
    comparison alone cannot tell "Pop Rock restates Pop" from "Synthpop reaches
    Pop through a secondary parent but is a different sound".

    The names settle it, which is exactly how the collection described it:
    "pop should be removed by pop rock", "folk should be removed by folk rock".
    Word-bounded on the parent's display name, so ``Pop Rock`` matches ``Pop``
    while ``Pop Punk`` (a punk genre, whose own display is "Pop Punk") matches
    on "Pop" too and is then judged by its own family — and ``Synthpop`` does
    not match at all.
    """
    parent_words = [w for w in parent.display.casefold().replace("-", " ").split() if w]
    if not parent_words:
        return False
    child_name = child.display.casefold().replace("-", " ")
    child_words = child_name.split()
    # The child must name EVERY word of the parent, in order, as whole words.
    for start in range(len(child_words) - len(parent_words) + 1):
        if child_words[start:start + len(parent_words)] == parent_words:
            return True
    return False


def remove_redundant_ancestors(
    candidates: list[Candidate],
    *,
    vocab: Vocabulary | None = None,
    settings: SelectionSettings | None = None,
    reasons: list[dict[str, Any]] | None = None,
) -> list[Candidate]:
    """Drop a selected genre when a MORE SPECIFIC selected genre covers it.

    §9: "When one selected genre is an ancestor of another selected genre, remove
    the ancestor unless it adds meaningful independent information." ``rock``
    beside ``melodic death metal`` spends a slot on nothing.

    Exceptions:

    * ``keep_together`` — pairs that are NOT simple parent/child duplicates.
    * ``conditional_keep`` — collapse only when the narrower genre has no
      track/album evidence of its own.
    * ``mutually_exclusive`` — pairs where only ONE identity may survive. The
      broader ancestor is dropped even when it scores HIGHER, so a track never
      carries both ``alternative rock`` and ``alternative metal``.
    """
    vocab = vocab or get_vocabulary()
    settings = settings or _settings_from_config(None)

    # ⭐ An APPROVED Nu Metal companion is exempt from ancestor condensation.
    #
    # ``alternative_rock`` is an ancestor of ``nu_metal`` (through
    # ``alternative_metal``), so this rule deleted it before the metal-over-rock
    # policy ever saw it — the Nu Metal exception could therefore never keep the
    # very genre it exists to keep. If the exception is enabled and an accepted
    # Nu Metal genre is present, its approved companions stand.
    _nu_metal_companions: frozenset[str] = frozenset()
    if settings.nu_metal_exception:
        has_nu_metal = any(
            c.genre_id == NU_METAL_ID for c in candidates
        )
        if has_nu_metal:
            _nu_metal_companions = frozenset(settings.nu_metal_rock_companions)

    drop: set[str] = set()
    triggers: dict[str, tuple[str, str]] = {}

    # Family totals, used to decide WHICH side of an exclusive pair survives.
    _family_totals: dict[str, float] = {}
    for candidate in candidates:
        key = candidate.family or candidate.genre_id
        _family_totals[key] = _family_totals.get(key, 0.0) + float(candidate.score or 0.0)

    for candidate in candidates:
        if candidate.genre_id in _nu_metal_companions:
            # Kept for the Nu Metal exception; the policy makes the final call.
            continue
        for other in candidates:
            if other.genre_id == candidate.genre_id:
                continue
            if not _is_ancestor(candidate.genre_id, other.genre_id, vocab):
                continue
            # ⚠️ THE EXPLICIT PAIR RULES COME FIRST — they are statements about
            # these two genres specifically, so no general heuristic may skip
            # past them. The cross-family guard below is a heuristic; a
            # ``mutually_exclusive`` pair is a decision.
            if _pair_is_kept(
                candidate.genre_id, other.genre_id, settings.mutually_exclusive
            ):
                # Unconditional: the exclusive pair must not co-exist, even
                # when the broader ancestor has more support. A track keeps ONE
                # "Alternative *" identity — the reported "Alternative Rock,
                # Alternative Metal, Cover" is the regression this prevents.
                drop.add(candidate.genre_id)
                triggers[candidate.genre_id] = (
                    REASON_EXCLUSIVE_CONFLICT, other.display,
                )
                break
            if _pair_is_kept(candidate.genre_id, other.genre_id, settings.keep_together):
                continue
            if _pair_is_kept(candidate.genre_id, other.genre_id, settings.conditional_keep):
                # Keep both only when the narrower one stands on track/album
                # evidence rather than on the artist's own broad tags.
                if other.has_entity(ENTITY_TRACK) or other.has_entity(ENTITY_ALBUM):
                    continue

            # ⚠️ FAMILIES ARE THE TRACK'S BRANCHES — crossing one is not A
            # replacement, WITH ONE PRECISE EXCEPTION.
            #
            # ``synthpop`` lists ``pop`` as a secondary parent while being an
            # ELECTRONIC-family genre, so a raw hierarchy walk let Synthpop
            # delete Pop from a track whose pop evidence (3.05) outweighed it.
            # That is a different sound and must not collapse.
            #
            # But ``Pop Rock``, ``Folk Rock`` and ``Punk Rock`` also cross
            # families (rock-family children of pop/folk/rock) and DO need to
            # absorb their parent — the reported "pop should be removed by pop
            # rock", "folk should be removed by folk rock". The distinction that
            # separates them is the one the request itself used: the child's NAME
            # contains the parent's name. "Pop Rock" names Pop; "Synthpop"
            # does not.
            _cross_family = (
                (other.family or other.genre_id)
                != (candidate.family or candidate.genre_id)
            )
            if _cross_family and not _names_parent(candidate, other, vocab):
                continue
            # ⚠️ THE RATIO DOES NOT APPLY TO A TRUE ANCESTOR/DESCENDANT PAIR.
            #
            # ``ancestor_replace_min_ratio`` asks "is the narrower genre at least
            # as well supported as the one it would replace?" — the right
            # question for a candidate that shows up through the hierarchy but
            # means something separate. It is the WRONG question for a genre that
            # is literally a superset of the other: ``death metal`` contains
            # ``melodic death metal`` by definition, so support cannot decide
            # which of the two names the sound. Providers tag the umbrella more
            # often, so the umbrella wins the ratio and BOTH survive — which is
            # the reported "death metal should be removed by melodic death
            # metal" (749 tracks in the export) and "grunge should be removed by
            # post-grunge" (187).
            #
            # This matches ``remove_generic_parents``, which is already
            # deliberately unconditional for exactly the same reason.
            #
            # The ratio still governs the pair types that genuinely need it,
            # handled above: ``keep_together`` keeps distinct aspects together,
            # and ``conditional_keep`` requires independent evidence.
            drop.add(candidate.genre_id)
            triggers[candidate.genre_id] = (REASON_REDUNDANT_ANCESTOR, other.display)
            break

    if drop:
        if reasons is not None:
            for candidate in candidates:
                hit = triggers.get(candidate.genre_id)
                if hit is None:
                    continue
                reasons.append(_removal(candidate, hit[0], hit[1]))
        logger.debug("Genre hierarchy condensation", dropped=sorted(drop))
    return [c for c in candidates if c.genre_id not in drop]


def enforce_family_diversity(
    candidates: list[Candidate],
    *,
    settings: SelectionSettings | None = None,
) -> list[Candidate]:
    """Cap how many genres come from one hierarchy branch.

    §10: "No more than two selected genres from the same immediate hierarchy
    branch unless the third has strong independent track-level support." Without
    it ``rock`` + ``alternative rock`` + ``indie rock`` can consume every slot
    merely because all three are common.

    Expects ``candidates`` sorted best-first; the weakest offender is dropped.
    """
    settings = settings or _settings_from_config(None)

    kept: list[Candidate] = []
    per_family: dict[str, int] = {}

    for candidate in candidates:
        family = candidate.family or candidate.genre_id
        used = per_family.get(family, 0)
        if used >= settings.family_slot_limit:
            track_sources = {
                c.source for c in candidate.contributions if c.entity == ENTITY_TRACK
            }
            if len(track_sources) < settings.family_override_sources:
                logger.debug(
                    "Family slot limit reached — dropping the weakest of the branch",
                    genre=candidate.display,
                    family=family,
                    used=used,
                )
                continue
        per_family[family] = used + 1
        kept.append(candidate)

    return kept


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def rank_and_condense(
    candidates: list[Candidate],
    *,
    vocab: Vocabulary | None = None,
    settings: SelectionSettings | None = None,
) -> list[Candidate]:
    """Score-order the candidates.

    Condensation and family diversity are applied by the caller AFTER outlier
    removal, so a family trimmed to two representatives cannot hide the full
    support it had before trimming. Split out from :func:`select_genres` so the
    FULL considered set stays available: the report's provenance requirement
    (§15) is that a result be explainable, and "why is my genre missing" is
    unanswerable if the losers are discarded before anyone can look at them.
    """
    return sorted(
        candidates,
        key=lambda c: (c.confidence, c.score, c.specificity),
        reverse=True,
    )


def remove_family_outliers(
    candidates: list[Candidate],
    *,
    settings: SelectionSettings | None = None,
    reasons: list[dict[str, Any]] | None = None,
) -> list[Candidate]:
    """Drop a genre family that is only a tiny minority of the evidence.

    Providers often return one long, alphabetically sorted tag list. After
    rank-decay is neutralised those tags are equal evidence, but an unrelated
    lone tag can still win a slot because it is more specific. A family whose
    total score is below ``family_consensus_min_share`` of the strongest
    family is an outlier — the reported ``alternative rock`` beside a large
    pop/dance-pop/synth-pop cluster — and is removed before selection.

    ⭐ ORDER IS LOAD-BEARING: this runs BEFORE the metal-over-rock policy. An
    isolated, weakly supported metal tag must be rejected as an outlier rather
    than being allowed to delete a well-supported rock cluster (the reported
    Offspring case: ``Punk Rock; Punk; Rock`` plus one weak ``Death Metal``).
    Suppressing on an outlier makes the outlier effect STRONGER than its own
    evidence, which is the opposite of validating the evidence first.
    """
    settings = settings or _settings_from_config(None)
    if not candidates:
        return []

    family_scores: dict[str, float] = {}
    for candidate in candidates:
        family = candidate.family or candidate.genre_id
        family_scores[family] = family_scores.get(family, 0.0) + max(candidate.score, 0.0)

    strongest = max(family_scores.values(), default=0.0)
    if strongest <= 0:
        return candidates

    floor = strongest * settings.family_consensus_min_share
    kept = [
        candidate for candidate in candidates
        if candidate.manual
        or family_scores.get(candidate.family or candidate.genre_id, 0.0) >= floor
    ]
    dropped = [c for c in candidates if c not in kept]
    if dropped:
        if reasons is not None:
            for candidate in dropped:
                reasons.append(
                    _removal(
                        candidate,
                        REASON_FAMILY_OUTLIER,
                        f"family below {settings.family_consensus_min_share:.0%} of the strongest",
                    )
                )
        logger.debug(
            "Genre family outlier removed",
            dropped=[c.display for c in dropped],
            family_scores={k: round(v, 3) for k, v in family_scores.items()},
        )
    return kept


def _floor_for(settings: SelectionSettings, position: int) -> float:
    """The confidence floor for a 1-based POSITION (clamped to the tuple).

    Shared by :func:`select_genres` and the removal-reporting in
    :func:`trim_to_final`, so the recorded reason always quotes the floor that
    actually applied.
    """
    floors = settings.thresholds
    if not floors:
        return 0.0
    return floors[position] if position < len(floors) else floors[-1]


def _relative_floor_for(settings: SelectionSettings, position: int) -> float:
    """The relative floor for a position (0.0 when the tuple does not cover it)."""
    floors = settings.relative_floors
    return floors[position] if position < len(floors) else 0.0


def select_genres(
    candidates: list[Candidate],
    *,
    settings: SelectionSettings | None = None,
) -> list[Candidate]:
    """Rank and select up to ``max_genres``, each position earning its place.

    THE COUNT IS A MAXIMUM, NOT A TARGET (§12). Each position has to EARN its
    place: a confidence floor plus a relative floor against the leader, so a
    weak artist-only fallback cannot fill a blank slot.

    A manual/locked genre is never subject to the thresholds — an explicit user
    choice outranks the model, which is the point of making it manual.

    ``candidates`` should already be ranked and CLEANED (see the pipeline in
    :func:`select_album_or_track_genres`); they are re-sorted here so the
    function is safe to call on its own.
    """
    settings = settings or _settings_from_config(None)

    ranked = sorted(
        candidates,
        key=lambda c: (c.confidence, c.score, c.specificity),
        reverse=True,
    )

    selected: list[Candidate] = []
    for index, candidate in enumerate(ranked):
        if len(selected) >= settings.max_genres:
            break

        if candidate.manual:
            selected.append(candidate)
            continue

        position = len(selected)
        floor = _floor_for(settings, position)
        if candidate.confidence < floor:
            logger.debug(
                "Genre below the confidence floor — not filling the position",
                genre=candidate.display,
                confidence=round(candidate.confidence, 3),
                floor=floor,
            )
            break

        if selected:
            relative = _relative_floor_for(settings, position)
            # ⚠️ The ratio uses RAW SCORES, not confidences. Confidence is
            # clamped at 1.0, so a leader at score 2.85 and a runner-up at 1.38
            # both read 1.0 — comparing those makes every relative floor
            # trivially pass, and comparing the NEXT candidate against a clamped
            # 1.0 makes it artificially harsh. The score ratio is the real
            # "how much weaker is this than the best" question.
            top_score = selected[0].score or 1.0
            if top_score > 0 and (candidate.score / top_score) < relative:
                logger.debug(
                    "Genre below the relative floor — not filling the position",
                    genre=candidate.display,
                    score=round(candidate.score, 3),
                    relative_floor=relative,
                )
                break

        selected.append(candidate)

    return selected


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _vocabulary_version(vocab: Vocabulary) -> Any:
    """The taxonomy version from the vocabulary file, or ``None``.

    Best-effort: a version is diagnostics, never a decision input, so an
    unreadable file must not fail a scan.
    """
    try:
        import json

        with open(vocab.source_path, encoding="utf-8") as handle:
            return json.load(handle).get("version")
    except Exception:
        return None


def select_album_or_track_genres(
    source_map: dict[str, Any],
    *,
    max_genres: int | None = None,
    settings: SelectionSettings | None = None,
    vocab: Vocabulary | None = None,
) -> dict[str, Any]:
    """The whole pipeline. Returns the selection plus what was set aside.

    ``{"genres": [display…], "candidates": [...], "moved": {...},
       "removed": [...], "engine": "v2", "algorithm_version": "2.1"}``

    ``candidates`` is the FULL ranked, post-condensation set — each entry marked
    ``selected`` — so a genre that did not make the cut can still be explained.
    Each carries its score, confidence, sources, entity levels and raw evidence:
    the report's §15 provenance, so a result is accountable rather than a bare
    list of words.

    ⭐ THE ORDER BELOW IS THE REQUIREMENT, not an implementation detail:

    1. rank, then reject weak family OUTLIERS — evidence is validated first;
    2. take a POOL of the best candidates;
    3. apply the metal-over-rock COLLECTION POLICY;
    4. remove redundant generic parents and ancestors;
    5. trim the cleaned pool to the final count.

    Reversing 1 and 3 would let an isolated bad metal tag delete a
    well-supported rock cluster. Reversing 4 and 5 would let the trim decide
    which genre "wins" a redundancy conflict, so a redundant parent could
    survive by outscoring its own child.
    """
    settings = settings or _settings_from_config(None)
    if max_genres is not None:
        # ``max_genres`` is the FINAL cardinality; the pool scales with it so a
        # caller asking for one genre still has alternatives to clean up from.
        settings = SelectionSettings(
            **{**settings.__dict__, "max_genres": max(1, int(max_genres))}
        )

    vocab = vocab or get_vocabulary()
    removed: list[dict[str, Any]] = []

    # ── 1. Evidence first ────────────────────────────────────────────────
    candidates, moved = collect_candidates(source_map, vocab=vocab, settings=settings)
    score_candidates(candidates, settings=settings)
    ranked = rank_and_condense(candidates, vocab=vocab, settings=settings)
    ranked = remove_family_outliers(ranked, settings=settings, reasons=removed)

    # ── 2. The pool: more candidates than the final count, so the policy has
    # something to promote when it removes a genre.
    pool_size = max(settings.selection_pool, settings.max_genres)
    pool = ranked[:pool_size]
    overflow = ranked[pool_size:]

    # ── 3. Collection policy ─────────────────────────────────────────────
    pool, policy_removed = apply_metal_over_rock_policy(pool, settings=settings)
    removed.extend(policy_removed)

    # ── 4. Redundancy ────────────────────────────────────────────────────
    pool, generic_removed = remove_generic_parents(pool, settings=settings)
    removed.extend(generic_removed)

    pool = remove_redundant_ancestors(
        pool, vocab=vocab, settings=settings, reasons=removed
    )
    pool = enforce_family_diversity(pool, settings=settings)

    # ── 5. Final cardinality ─────────────────────────────────────────────
    selected, trim_removed = trim_to_final(pool, settings=settings)
    removed.extend(trim_removed)

    winners = {c.genre_id for c in selected}
    # Candidates outside the pool are still explained rather than discarded.
    for candidate in overflow:
        if candidate.genre_id in winners:
            continue
        removed.append(
            _removal(
                candidate,
                REASON_OUTSIDE_FINAL_LIMIT,
                f"pool of {pool_size}",
            )
        )

    return {
        "engine": "v2",
        "algorithm_version": "2.1",
        "taxonomy_version": _vocabulary_version(vocab),
        "genres": [c.display for c in selected],
        "candidates": [
            {**c.to_dict(), "selected": c.genre_id in winners} for c in ranked
        ],
        "removed": removed,
        "moved": {kind: values for kind, values in moved.items() if values},
    }
