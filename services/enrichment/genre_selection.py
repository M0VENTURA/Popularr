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

    max_genres: int = 3
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
    #: Confidence floors, by position (1-based).
    thresholds: tuple[float, ...] = (0.60, 0.45, 0.35)
    #: Each position must also reach this fraction of the position ABOVE it,
    #: measured on raw score.
    #:
    #: ⚠️ The report's prose says 45% for position 2 and 55% for position 3, but
    #: its own worked example has position 3 at 45% of position 2 (0.74 / 1.66)
    #: — the prose rule would reject the trio the example says to save. The
    #: example is the checkable statement, so ~45% is used for both and the value
    #: is configurable. The rule's JOB is to stop a much weaker candidate taking
    #: a slot, and 45% does that; 55% additionally rejects the report's own case.
    relative_floors: tuple[float, ...] = (0.0, 0.45, 0.45)
    #: At most this many genres from one family branch...
    family_slot_limit: int = 2
    #: ...unless the extra one has track-level evidence from this many sources.
    family_override_sources: int = 2
    #: Pairs that must NOT be collapsed to their ancestor.
    keep_together: frozenset[tuple[str, str]] = frozenset()
    #: Pairs collapsed to the broader one ONLY when the narrower is unsupported.
    conditional_keep: frozenset[tuple[str, str]] = frozenset()


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

#: The report's "usually keep both" list. These pairs are NOT parent/child
#: duplicates — they describe different aspects of a sound — so the ancestor
#: rule must not collapse them even where the hierarchy makes one a parent.
DEFAULT_KEEP_TOGETHER: frozenset[tuple[str, str]] = frozenset({
    ("alternative_metal", "alternative_rock"),
    ("gothic_rock", "gothic_metal"),
    ("industrial", "industrial_metal"),
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
    except (TypeError, ValueError):
        t1, t2, t3 = 0.60, 0.45, 0.35

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

    return SelectionSettings(
        max_genres=max(1, _int("max_genres", 3)),
        source_weights=source_weights,
        entity_weights=entity_weights,
        agreement_2=_num("agreement_2", 0.30),
        agreement_3=_num("agreement_3", 0.20),
        specificity_bonus=_num("specificity_bonus", 0.05),
        artist_only_penalty=_num("artist_only_penalty", 0.15),
        microgenre_single_source_penalty=_num("microgenre_single_source_penalty", 0.10),
        thresholds=(t1, t2, t3),
        relative_floors=(0.0, 0.45, 0.45),
        family_slot_limit=max(1, _int("family_slot_limit", 2)),
        family_override_sources=max(1, _int("family_override_sources", 2)),
        keep_together=_pairs("keep_together", DEFAULT_KEEP_TOGETHER),
        conditional_keep=_pairs("conditional_keep", DEFAULT_CONDITIONAL_KEEP),
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
    from services.enrichment.genre_vocabulary import tokenize_values

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

            weight = source_weight * entity_weight * _rank_decay(rank)
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
# Condensation
# ---------------------------------------------------------------------------

def _is_ancestor(ancestor: str, descendant: str, vocab: Vocabulary) -> bool:
    entry = vocab.genres.get(descendant)
    if entry is None:
        return False
    return ancestor in entry.ancestors(vocab.genres)


def _pair_is_kept(a: str, b: str, keep: frozenset[tuple[str, str]]) -> bool:
    return (a, b) in keep or (b, a) in keep


def remove_redundant_ancestors(
    candidates: list[Candidate],
    *,
    vocab: Vocabulary | None = None,
    settings: SelectionSettings | None = None,
) -> list[Candidate]:
    """Drop a selected genre when a MORE SPECIFIC selected genre covers it.

    §9: "When one selected genre is an ancestor of another selected genre, remove
    the ancestor unless it adds meaningful independent information." ``rock``
    beside ``melodic death metal`` spends a slot on nothing.

    Two exceptions, both from the report:

    * ``keep_together`` — pairs that are NOT simple parent/child duplicates
      (``alternative metal`` + ``alternative rock`` describe different aspects).
    * ``conditional_keep`` — collapse only when the narrower genre has no
      track/album evidence of its own.
    """
    vocab = vocab or get_vocabulary()
    settings = settings or _settings_from_config(None)

    drop: set[str] = set()
    for candidate in candidates:
        for other in candidates:
            if other.genre_id == candidate.genre_id:
                continue
            if not _is_ancestor(candidate.genre_id, other.genre_id, vocab):
                continue
            if _pair_is_kept(candidate.genre_id, other.genre_id, settings.keep_together):
                continue
            if _pair_is_kept(candidate.genre_id, other.genre_id, settings.conditional_keep):
                # Keep both only when the narrower one stands on track/album
                # evidence rather than on the artist's own broad tags.
                if other.has_entity(ENTITY_TRACK) or other.has_entity(ENTITY_ALBUM):
                    continue
            drop.add(candidate.genre_id)
            break

    if drop:
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
    """Score-order the candidates, then apply the hierarchy rules.

    Split out from :func:`select_genres` so the FULL considered set stays
    available: the report's provenance requirement (§15) is that a result be
    explainable, and "why is my genre missing" is unanswerable if the losers are
    discarded before anyone can look at them.
    """
    settings = settings or _settings_from_config(None)
    ranked = sorted(
        candidates,
        key=lambda c: (c.confidence, c.score, c.specificity),
        reverse=True,
    )
    ranked = remove_redundant_ancestors(ranked, vocab=vocab, settings=settings)
    return enforce_family_diversity(ranked, settings=settings)


def select_genres(
    candidates: list[Candidate],
    *,
    settings: SelectionSettings | None = None,
) -> list[Candidate]:
    """Rank, condense, and select up to ``max_genres``.

    THREE IS A MAXIMUM, NOT A TARGET (§12). Each position has to EARN its place:
    a confidence floor plus a relative floor against the position above it, so a
    weak artist-only fallback cannot fill a blank slot.

    A manual/locked genre is never subject to the thresholds — an explicit user
    choice outranks the model, which is the point of making it manual.

    ``candidates`` should already be ranked and condensed (see
    :func:`rank_and_condense`); they are re-sorted here so the function is safe
    to call on its own.
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
        floor = (
            settings.thresholds[position]
            if position < len(settings.thresholds)
            else settings.thresholds[-1]
        )
        if candidate.confidence < floor:
            logger.debug(
                "Genre below the confidence floor — not filling the position",
                genre=candidate.display,
                confidence=round(candidate.confidence, 3),
                floor=floor,
            )
            break

        if selected:
            relative = (
                settings.relative_floors[position]
                if position < len(settings.relative_floors)
                else 0.0
            )
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

def select_album_or_track_genres(
    source_map: dict[str, Any],
    *,
    max_genres: int | None = None,
    settings: SelectionSettings | None = None,
    vocab: Vocabulary | None = None,
) -> dict[str, Any]:
    """The whole pipeline. Returns the selection plus what was set aside.

    ``{"genres": [display…], "candidates": [...], "moved": {...}, "engine": "v2"}``

    ``candidates`` is the FULL ranked, post-condensation set — each entry marked
    ``selected`` — so a genre that did not make the cut can still be explained.
    Each carries its score, confidence, sources, entity levels and raw evidence:
    the report's §15 provenance, so a result is accountable rather than a bare
    list of words.
    """
    settings = settings or _settings_from_config(None)
    if max_genres is not None:
        settings = SelectionSettings(
            **{**settings.__dict__, "max_genres": max(1, int(max_genres))}
        )

    candidates, moved = collect_candidates(source_map, vocab=vocab, settings=settings)
    score_candidates(candidates, settings=settings)
    ranked = rank_and_condense(candidates, vocab=vocab, settings=settings)
    selected = select_genres(ranked, settings=settings)
    winners = {c.genre_id for c in selected}

    return {
        "engine": "v2",
        "genres": [c.display for c in selected],
        "candidates": [
            {**c.to_dict(), "selected": c.genre_id in winners} for c in ranked
        ],
        "moved": {kind: values for kind, values in moved.items() if values},
    }
