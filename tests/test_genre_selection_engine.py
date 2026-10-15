"""Genre selection: classify before scoring, weight by entity, condense, earn slots.

The reported problem
--------------------
> Your current "top three genres" approach is sensible, but the selection stage is
> happening too early. The main issue is not the limit of three. It is that broad
> parents, precise subgenres, moods, geography, formats, and malformed source tags
> are all competing for the same three positions.

So the tests are organised by the ORDER the report specifies — parse, normalise,
classify, canonicalise, score, condense, select — because a later stage running
early is the defect, and each stage's test would still pass if a LATER one were
moved in front of it.

⚠️ What is NOT asserted: the report's worked-example scores (0.74 / 2.54 / 1.66).
They are not derivable from the report's own formula — ``alternative rock``
reaches 0.74 from one rank-1 Last.fm tag, which with ``0.70 x 1.00`` implies no
rank decay at all. Its PRINCIPLES are asserted exactly; its arithmetic is not,
because pinning numbers that cannot be reproduced would freeze a coincidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.enrichment import genre_selection as gs
from services.enrichment.genre_vocabulary import (
    KIND_ATTRIBUTE,
    KIND_GENRE,
    KIND_GEOGRAPHY,
    KIND_JUNK,
    KIND_MOOD,
    KIND_UNKNOWN,
    get_vocabulary,
    normalize_token,
    tokenize_values,
    validate_vocabulary,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SETTINGS = gs.SelectionSettings(
    source_weights=gs.DEFAULT_SOURCE_WEIGHTS,
    entity_weights=gs.DEFAULT_ENTITY_WEIGHTS,
    keep_together=gs.DEFAULT_KEEP_TOGETHER,
    conditional_keep=gs.DEFAULT_CONDITIONAL_KEEP,
)


def select(source_map: dict, **kwargs) -> list[str]:
    return gs.select_album_or_track_genres(
        source_map, settings=kwargs.pop("settings", SETTINGS), **kwargs
    )["genres"]


def full(source_map: dict, **kwargs) -> dict:
    return gs.select_album_or_track_genres(
        source_map, settings=kwargs.pop("settings", SETTINGS), **kwargs
    )


# ---------------------------------------------------------------------------
# The shipped vocabulary
# ---------------------------------------------------------------------------

class TestTheShippedVocabulary:
    def test_it_is_valid(self) -> None:
        """A malformed vocabulary would silently produce no genres at all."""
        import json

        data = json.loads(
            (REPO_ROOT / "services" / "enrichment" / "genre_vocabulary.json").read_text(
                encoding="utf-8"
            )
        )
        assert validate_vocabulary(data) == []

    def test_it_is_substantial(self) -> None:
        vocab = get_vocabulary()
        assert len(vocab.genres) >= 150, (
            "the report calls for ~150-250 canonical genres to cover a real collection"
        )
        assert len(vocab.moods) >= 15
        assert len(vocab.countries) >= 40

    def test_every_parent_resolves(self) -> None:
        vocab = get_vocabulary()
        for entry in vocab.genres.values():
            for parent in entry.parents:
                assert parent in vocab.genres, (
                    f"{entry.id} has parent {parent}, which is not a genre"
                )

    def test_multi_parent_genres_are_supported(self) -> None:
        """§9 needs it: ``Folk + Folk Metal -> Folk Metal`` cannot work unless
        ``folk metal`` is under ``folk``, and it must ALSO stay under metal."""
        vocab = get_vocabulary()
        assert set(vocab.genres["folk_metal"].parents) == {"heavy_metal", "folk"}
        assert set(vocab.genres["synthpop"].parents) == {"pop", "electronic"}

    def test_metal_sits_under_rock_so_the_ancestor_rule_works(self) -> None:
        """§9's headline rule is ``Rock + Death Metal -> Death Metal``, which
        only holds if ``rock`` is an ancestor of ``death_metal``."""
        vocab = get_vocabulary()
        assert "rock" in vocab.genres["death_metal"].ancestors(vocab.genres)

    def test_families_are_their_own_branch_despite_the_hierarchy(self) -> None:
        """``metal`` is under ``rock`` but is its OWN family, or the family-slot
        limit would treat every rock and metal genre as one branch."""
        vocab = get_vocabulary()
        assert vocab.genres["metal"].family == "metal"
        assert vocab.genres["death_metal"].family == "metal"
        assert vocab.genres["indie_rock"].family == "rock"

    def test_ancestor_walks_are_cycle_safe(self) -> None:
        """A hand-edited cycle must TERMINATE, not hang a scan.

        The exact chain is not asserted: a cycle means the parent graph is
        already broken, so all that matters is that the walk is bounded.
        """
        from services.enrichment.genre_vocabulary import GenreEntry, _MAX_PARENT_DEPTH

        a = GenreEntry("a", "A", ("b",), 1, "", ())
        b = GenreEntry("b", "B", ("a",), 1, "", ())
        chain = a.ancestors({"a": a, "b": b})
        assert len(chain) <= _MAX_PARENT_DEPTH
        assert "b" in chain

    def test_a_self_parenting_genre_terminates(self) -> None:
        from services.enrichment.genre_vocabulary import GenreEntry

        loop = GenreEntry("x", "X", ("x",), 1, "", ())
        assert loop.ancestors({"x": loop}) == []

    def test_a_shared_ancestor_is_listed_once(self) -> None:
        """``folk_metal`` reaches ``metal`` through heavy_metal and ``rock``
        through metal; a diamond must not duplicate them."""
        vocab = get_vocabulary()
        chain = vocab.genres["folk_metal"].ancestors(vocab.genres)
        assert len(chain) == len(set(chain))

    def test_validation_catches_a_dangling_parent(self) -> None:
        problems = validate_vocabulary(
            {"genres": [{"id": "x", "display": "X", "parent": "nope"}]}
        )
        assert any("nope" in p for p in problems)

    def test_validation_catches_a_duplicate_id(self) -> None:
        problems = validate_vocabulary(
            {"genres": [
                {"id": "x", "display": "X"},
                {"id": "x", "display": "Y"},
            ]}
        )
        assert any("duplicate" in p for p in problems)

    def test_validation_catches_an_alias_claimed_twice(self) -> None:
        """An alias owned by two genres makes the winner depend on dict order."""
        problems = validate_vocabulary(
            {"genres": [
                {"id": "x", "display": "X", "aliases": ["shared"]},
                {"id": "y", "display": "Y", "aliases": ["shared"]},
            ]}
        )
        assert any("shared" in p for p in problems)

    def test_validation_catches_a_bad_specificity(self) -> None:
        problems = validate_vocabulary(
            {"genres": [{"id": "x", "display": "X", "specificity": 9}]}
        )
        assert any("specificity" in p for p in problems)

    def test_validation_requires_genres(self) -> None:
        assert validate_vocabulary({}) == ["'genres' must be a non-empty list"]
        assert validate_vocabulary("nope") == ["the vocabulary must be a JSON object"]


# ---------------------------------------------------------------------------
# Stage 2: classification (the stage that was missing entirely)
# ---------------------------------------------------------------------------

class TestClassification:
    @pytest.mark.parametrize(
        "value,kind",
        [
            ("death metal", KIND_GENRE),
            ("melodic death metal", KIND_GENRE),
            ("dark", KIND_MOOD),
            ("aggressive", KIND_MOOD),
            ("atmospheric", KIND_MOOD),
            ("German", KIND_GEOGRAPHY),
            ("Australia", KIND_GEOGRAPHY),
            ("Northern Europe", KIND_GEOGRAPHY),
            ("live", KIND_ATTRIBUTE),
            ("cover", KIND_ATTRIBUTE),
            ("instrumental", KIND_ATTRIBUTE),
            ("audiobook", KIND_ATTRIBUTE),
            ("Billboard Hot 100", KIND_UNKNOWN),
        ],
    )
    def test_values_are_classified(self, value: str, kind: str) -> None:
        assert get_vocabulary().classify(value).kind == kind

    @pytest.mark.parametrize(
        "value",
        ["ambient", "dark ambient", "dream pop", "post-punk"],
    )
    def test_a_genre_wins_over_its_mood_reading(self, value: str) -> None:
        """⚠️ ORDER IS LOAD-BEARING.

        ``ambient``/``dream pop`` are also plausible mood words, and the mood map
        is consulted second on purpose. Running it first would reclassify real
        genres as moods and delete them from every track's tags.
        """
        assert get_vocabulary().classify(value).kind == KIND_GENRE

    def test_genre_is_consulted_before_geography(self) -> None:
        """⚠️ Asserted on a SYNTHETIC vocabulary, because the shipped one has no
        value that is both a country and a genre — so testing the order through
        it would pass whichever order the code used.

        The order matters the moment a user adds a colliding entry (the report
        warns about ``Neue Deutsche Härte`` and ``Anatolian rock``).
        """
        from services.enrichment.genre_vocabulary import _build

        vocab = _build(
            {
                "genres": [{"id": "collide", "display": "Collide"}],
                "geography": {"countries": {"collide": "Collidia"}},
                "moods": [{"id": "collide", "display": "Collide"}],
            },
            path="synthetic",
            is_override=False,
            errors=(),
        )
        result = vocab.classify("Collide")
        assert result.kind == KIND_GENRE, (
            "a value that is both a genre and a country must resolve to the genre"
        )
        assert result.canonical == "collide"

    @pytest.mark.parametrize(
        "value,country",
        [
            ("Australian", "Australia"),
            ("german", "Germany"),
            ("Norway", "Norway"),
            ("South Korean", "South Korea"),
            ("American", "United States"),
        ],
    )
    def test_geography_normalises_to_a_country_name(self, value: str, country: str) -> None:
        result = get_vocabulary().classify(value)
        assert result.canonical == country

    @pytest.mark.parametrize(
        "value,genre_id",
        [
            ("K-pop", "k_pop"),
            ("Americana", "americana"),
            ("Afrobeat", "afrobeat"),
            ("Neue Deutsche Härte", "neue_deutsche_haerte"),
            ("Anatolian rock", "anatolian_rock"),
        ],
    )
    def test_a_geographic_GENRE_stays_a_genre(self, value: str, genre_id: str) -> None:
        """§2C: these are recognised musical classifications, not country fields."""
        result = get_vocabulary().classify(value)
        assert result.kind == KIND_GENRE
        assert result.canonical == genre_id

    def test_mood_aliases_resolve_to_one_canonical_mood(self) -> None:
        vocab = get_vocabulary()
        for alias, expected in (
            ("positive", "uplifting"),
            ("cheerful", "happy"),
            ("somber", "melancholic"),
            ("chill", "relaxed"),
            ("high energy", "energetic"),
        ):
            assert vocab.classify(alias).canonical == expected, alias

    def test_normalisation_folds_spelling_variants(self) -> None:
        assert normalize_token("Alt. Metal") == normalize_token("alt-metal")
        assert normalize_token("Alt Metal") == normalize_token("ALT  METAL")

    def test_normalisation_folds_diacritics(self) -> None:
        """⚠️ NFKC alone does NOT fold diacritics — ``nü`` stays ``ü`` and becomes
        the key ``nmetal``, which no ``nu metal`` alias can match. Sources
        disagree about diacritics constantly (``Motörhead`` / ``Motorhead``), so
        the normaliser decomposes and strips combining marks.
        """
        assert normalize_token("nü metal") == normalize_token("nu metal")
        assert normalize_token("Motörhead") == normalize_token("Motorhead")
        assert get_vocabulary().classify("nü metal").canonical == "nu_metal"
        assert get_vocabulary().classify("nü-metal").canonical == "nu_metal"


# ---------------------------------------------------------------------------
# Stage 5: tokenising (the parsing bug that lost whole genres)
# ---------------------------------------------------------------------------

class TestTokenising:
    def test_a_pipe_delimited_list_is_split(self) -> None:
        """⚠️ REGRESSION. ``|`` was not a separator, so a pipe-delimited value was
        kept WHOLE, classified as unknown, and every genre in it was lost."""
        tokens = tokenize_values("Gothic Metal | Symphonic Metal | Power Metal", get_vocabulary())
        assert tokens == ["Gothic Metal", "Symphonic Metal", "Power Metal"]

    def test_a_backslash_list_is_split(self) -> None:
        tokens = tokenize_values("Nu Metal\\Alternative Rock\\Heavy Metal", get_vocabulary())
        assert tokens == ["Nu Metal", "Alternative Rock", "Heavy Metal"]

    def test_a_semicolon_list_is_split(self) -> None:
        tokens = tokenize_values("Heavy Metal; Gothic Metal; Power Metal", get_vocabulary())
        assert tokens == ["Heavy Metal", "Gothic Metal", "Power Metal"]

    def test_a_serialized_array_is_parsed(self) -> None:
        assert tokenize_values("['Doom Metal', 'Rock']", get_vocabulary()) == [
            "Doom Metal",
            "Rock",
        ]
        assert tokenize_values('["Doom Metal", "Rock"]', get_vocabulary()) == [
            "Doom Metal",
            "Rock",
        ]

    @pytest.mark.parametrize("value", ["death/doom", "death/doom metal", "r&b", "drum & bass"])
    def test_a_known_genre_is_never_split(self, value: str) -> None:
        """⚠️ REGRESSION, and the report calls it out explicitly.

        Splitting on ``/`` turned ``death/doom`` into ``death`` + ``doom``, so the
        canonical ``death_doom`` entry could never match and a bare ``doom metal``
        was written instead. ``r&b`` split into ``r`` and ``b``.
        """
        assert tokenize_values(value, get_vocabulary()) == [value]

    def test_an_unknown_composite_falls_back_to_splitting(self) -> None:
        """``/`` IS still a separator when the whole value means nothing."""
        tokens = tokenize_values("Bebop/Swing", get_vocabulary())
        assert tokens == ["Bebop", "Swing"]

    def test_a_list_of_values_is_flattened(self) -> None:
        tokens = tokenize_values(["death metal", "rock"], get_vocabulary())
        assert tokens == ["death metal", "rock"]

    def test_duplicates_collapse(self) -> None:
        tokens = tokenize_values("Rock; rock; ROCK", get_vocabulary())
        assert len(tokens) == 1


# ---------------------------------------------------------------------------
# Stage 6: scoring — entity levels
# ---------------------------------------------------------------------------

class TestEntityLevels:
    def test_source_at_entity_is_parsed(self) -> None:
        assert gs.parse_source_key("lastfm@track") == ("lastfm", "track")
        assert gs.parse_source_key("musicbrainz@album") == ("musicbrainz", "album")
        assert gs.parse_source_key("wikidata@artist") == ("wikidata", "artist")

    def test_a_bare_source_key_is_track_level(self) -> None:
        """Backwards compatible: every pre-existing caller passes a bare key, and
        a track's own columns are what those callers hold."""
        assert gs.parse_source_key("lastfm") == ("lastfm", "track")

    def test_an_unknown_entity_falls_back_to_track(self) -> None:
        assert gs.parse_source_key("lastfm@nonsense") == ("lastfm", "track")

    def test_track_evidence_outweighs_artist_evidence(self) -> None:
        """§6: an artist spans styles, so its tags SUPPORT rather than decide."""
        by_track = full({"lastfm@track": ["death metal"]})
        by_artist = full({"lastfm@artist": ["death metal"]})
        track_score = by_track["candidates"][0]["score"]
        artist_score = by_artist["candidates"][0]["score"]
        assert track_score > artist_score, (
            f"track {track_score} should outweigh artist {artist_score}"
        )

    @pytest.mark.parametrize("entity,expected", [("track", 0.70), ("album", 0.49), ("artist", 0.245)])
    def test_the_entity_weight_is_APPLIED_to_the_contribution(
        self, entity: str, expected: float
    ) -> None:
        """⚠️ Asserted on the contribution's own weight, not on the total.

        Comparing track-total against artist-total does NOT pin the entity weight:
        the artist-only PENALTY already guarantees the track total is higher, so
        that assertion passes with the weights forced to 1.0. The weight itself is
        ``source_weight x entity_weight x rank_decay`` = 0.70 x 1.00 / 0.70 / 0.35.
        """
        result = full({f"lastfm@{entity}": ["death metal"]})
        weight = result["candidates"][0]["evidence"][0]["weight"]
        assert weight == pytest.approx(expected, abs=0.005), (
            f"lastfm@{entity} contributed {weight}, expected {expected}"
        )

    def test_entity_weights_are_ordered(self) -> None:
        assert gs.DEFAULT_ENTITY_WEIGHTS["track"] > gs.DEFAULT_ENTITY_WEIGHTS["album"]
        assert gs.DEFAULT_ENTITY_WEIGHTS["album"] > gs.DEFAULT_ENTITY_WEIGHTS["artist"]

    def test_the_same_source_at_two_levels_is_two_contributions(self) -> None:
        """A track's own tag and its album's tag are different evidence."""
        result = full({
            "musicbrainz@track": ["death metal"],
            "musicbrainz@album": ["death metal"],
        })
        assert result["candidates"][0]["entities"] == ["album", "track"]

    def test_one_source_at_one_level_counts_once(self) -> None:
        """⚠️ A source listing the same genre twice has made ONE statement.

        Without the cap, a provider that lists many spellings of one genre would
        outweigh genuine cross-source agreement.

        ⚠️ The two spellings must normalise DIFFERENTLY, or the tokeniser's own
        dedupe collapses them first and this passes with the candidate-level cap
        removed. ``alt metal`` and ``alternative metal`` are one genre with two
        unrelated token keys.
        """
        result = full({"lastfm@track": ["alt metal", "alternative metal"]})
        assert result["candidates"][0]["id"] == "alternative_metal"
        assert result["candidates"][0]["sources"] == ["lastfm"]
        assert len(result["candidates"][0]["evidence"]) == 1, (
            "two spellings of one genre from one source became two contributions"
        )


# ---------------------------------------------------------------------------
# Stage 7: condensation
# ---------------------------------------------------------------------------

class TestCondensation:
    def test_rock_is_dropped_beside_death_metal(self) -> None:
        """§9: ``rock`` adds no specificity once ``death metal`` is selected.

        ⚠️ ``rock`` is given STRONG evidence on purpose (three sources at
        track/album level). With only a weak artist tag it would fail the
        confidence floors anyway, and the test would pass with condensation
        switched off — pinning the floor rather than the rule under test.
        """
        assert select({
            "lastfm@track": ["death metal", "rock"],
            "musicbrainz@album": ["death metal", "rock"],
            "listenbrainz@track": ["rock"],
        }) == ["Death Metal"]

    def test_every_ancestor_is_dropped_not_just_the_nearest(self) -> None:
        assert select({
            "lastfm@track": ["death metal", "rock", "heavy metal", "metal"],
            "musicbrainz@album": ["death metal", "rock", "heavy metal", "metal"],
            "listenbrainz@track": ["rock", "heavy metal", "metal"],
        }) == ["Death Metal"]

    def test_a_more_specific_genre_absorbs_its_parent(self) -> None:
        assert select({
            "lastfm@track": ["melodic death metal", "death metal"],
            "musicbrainz@album": ["melodic death metal", "death metal"],
            "listenbrainz@track": ["death metal"],
        }) == ["Melodic Death Metal"]

    @pytest.mark.parametrize(
        "a,b",
        [
            # ⚠️ ONLY the pairs where the hierarchy actually nests one under the
            # other. The rest of the report's "usually keep both" list
            # (alternative metal + alternative rock, shoegaze + dream pop,
            # folk metal + power metal, …) is already safe because those are
            # SIBLINGS — listing them there documents intent, but a test using
            # them would pass with the exception switched off.
            ("hard rock", "blues rock"),
            ("nu metal", "alternative metal"),
            ("progressive metal", "djent"),
            ("post-punk", "gothic rock"),
        ],
    )
    def test_the_keep_together_pairs_survive(self, a: str, b: str) -> None:
        """§9's "usually keep both": these describe DIFFERENT aspects, so the
        ancestor rule must not collapse them even though the hierarchy nests one."""
        result = select({
            "lastfm@track": [a, b],
            "musicbrainz@album": [a, b],
            "listenbrainz@track": [a, b],
        })
        assert len(result) == 2, f"{a} + {b} collapsed to {result}"

    def test_sibling_pairs_are_kept_by_the_hierarchy_itself(self) -> None:
        """CONTROL for the pair above: siblings need no exception at all."""
        result = select({
            "lastfm@track": ["folk metal", "power metal"],
            "musicbrainz@album": ["folk metal", "power metal"],
            "listenbrainz@track": ["folk metal", "power metal"],
        })
        assert len(result) == 2, result

    def test_a_conditional_pair_collapses_without_support(self) -> None:
        """``hard_rock`` + ``heavy_metal`` collapse when the narrower one has no
        track/album evidence of its own."""
        result = select({
            "lastfm@track": ["heavy metal"],
            "musicbrainz@album": ["heavy metal"],
            "lastfm@artist": ["hard rock"],
        })
        assert result == ["Heavy Metal"], result

    def test_a_conditional_pair_survives_with_support(self) -> None:
        result = select({
            "lastfm@track": ["hard rock"],
            "musicbrainz@album": ["heavy metal"],
            "listenbrainz@track": ["hard rock", "heavy metal"],
        })
        assert "Hard Rock" in result and "Heavy Metal" in result, result


# ---------------------------------------------------------------------------
# Stage 8: selection — positions must be earned
# ---------------------------------------------------------------------------

class TestSelection:
    def test_three_is_a_maximum_not_a_target(self) -> None:
        """§12: one strongly-supported genre must not be padded to three."""
        assert select({
            "lastfm@track": ["death metal"],
            "musicbrainz@album": ["death metal"],
            "lastfm@artist": ["rock", "heavy metal", "hard rock"],
        }) == ["Death Metal"]

    def test_a_weak_third_does_not_fill_the_position(self) -> None:
        result = select({
            "lastfm@track": ["death metal"],
            "musicbrainz@album": ["death metal"],
            "listenbrainz@track": ["death metal"],
            "lastfm@artist": ["bebop"],
        })
        assert result == ["Death Metal"], result

    def test_max_genres_is_honoured(self) -> None:
        source = {
            "lastfm@track": ["death metal", "bebop", "reggae"],
            "musicbrainz@album": ["death metal", "bebop", "reggae"],
            "listenbrainz@track": ["death metal", "bebop", "reggae"],
        }
        assert len(select(source, max_genres=1)) == 1
        assert len(select(source, max_genres=2)) == 2

    def test_cross_source_agreement_outranks_a_single_source(self) -> None:
        result = full({
            "lastfm@track": ["death metal", "bebop"],
            "musicbrainz@album": ["death metal"],
            "listenbrainz@track": ["death metal"],
        })
        assert result["genres"][0] == "Death Metal"
        by_id = {c["id"]: c for c in result["candidates"]}
        assert by_id["death_metal"]["score"] > by_id["bebop"]["score"]

    def test_an_artist_only_genre_is_penalised(self) -> None:
        result = full({"lastfm@artist": ["death metal"]})
        assert result["candidates"][0]["score"] < 0.35, (
            "an artist-only contribution should not clear a strong threshold"
        )

    def test_a_microgenre_needs_two_sources(self) -> None:
        """§11: "Very narrow microgenre — two independent sources"."""
        one = full({"lastfm@track": ["atmospheric black metal"]})
        two = full({
            "lastfm@track": ["atmospheric black metal"],
            "musicbrainz@album": ["atmospheric black metal"],
        })
        assert two["candidates"][0]["score"] > one["candidates"][0]["score"]

    def test_manual_evidence_is_flagged_and_outranks(self) -> None:
        result = full({"manual@track": ["death metal"], "lastfm@artist": ["bebop"]})
        assert result["candidates"][0]["manual"] is True
        assert result["genres"][0] == "Death Metal"

    def test_a_manual_genre_bypasses_the_thresholds(self) -> None:
        """An explicit user choice outranks the model — that is the point."""
        settings = gs.SelectionSettings(
            source_weights={**gs.DEFAULT_SOURCE_WEIGHTS, "manual": 0.05},
            entity_weights=gs.DEFAULT_ENTITY_WEIGHTS,
        )
        result = gs.select_album_or_track_genres(
            {"manual@artist": ["death metal"]}, settings=settings
        )
        assert result["genres"] == ["Death Metal"]


# ---------------------------------------------------------------------------
# Stage 8b: family diversity
# ---------------------------------------------------------------------------

class TestFamilyDiversity:
    def test_three_siblings_in_one_family_are_capped(self) -> None:
        """§10: three common rock genres must not consume every slot.

        ⚠️ SIBLINGS, deliberately. ``heavy metal`` + ``thrash metal`` +
        ``power metal`` would be capped by ANCESTOR condensation (heavy metal is
        their parent) and the test would pass with the family limit switched off.
        ``thrash``/``death``/``black`` metal share the metal family without any
        one being another's parent.
        """
        result = select({
            "lastfm@track": ["thrash metal", "death metal", "black metal"],
            "musicbrainz@album": ["thrash metal", "death metal", "black metal"],
        })
        assert len(result) <= 2, result

    def test_ancestors_are_capped_by_condensation_not_the_family_limit(self) -> None:
        """CONTROL: a parent/child set is reduced to ONE, which is condensation."""
        result = select({
            "lastfm@track": ["heavy metal", "thrash metal", "power metal"],
            "musicbrainz@album": ["heavy metal", "thrash metal", "power metal"],
        })
        assert result == ["Thrash Metal", "Power Metal"] or len(result) <= 2, result

    def test_a_third_is_allowed_with_track_level_support(self) -> None:
        result = select({
            "lastfm@track": ["heavy metal", "thrash metal", "power metal"],
            "musicbrainz@track": ["thrash metal", "power metal"],
            "listenbrainz@track": ["power metal"],
        })
        assert len(result) <= gs.SelectionSettings().family_slot_limit + 1

    def test_different_families_are_not_capped(self) -> None:
        result = select({
            "lastfm@track": ["death metal", "bebop", "reggae"],
            "musicbrainz@album": ["death metal", "bebop", "reggae"],
            "listenbrainz@track": ["death metal", "bebop", "reggae"],
        })
        assert len(result) == 3, result


# ---------------------------------------------------------------------------
# Stage 8c: the thresholds, as units
# ---------------------------------------------------------------------------


def _candidate(gid: str, score: float, *, specificity: int = 2) -> gs.Candidate:
    """A candidate with an exact score, so a threshold can be tested alone.

    Building these by hand is deliberate: through the real pipeline the floors
    OVERLAP (a candidate below the absolute floor is usually below the relative
    one too), so a pipeline test cannot tell which rule did the excluding.
    """
    candidate = gs.Candidate(gid, gid.replace("_", " ").title(), "x", specificity)
    candidate.score = score
    candidate.confidence = min(1.0, score)
    candidate.sources = {"lastfm", "musicbrainz"}
    candidate.entities = {"track"}
    return candidate


class TestTheThresholds:
    def test_the_absolute_floor_excludes_a_weak_second(self) -> None:
        """0.40 is below the 0.45 second-position floor but ABOVE 45% of the
        leader, so only the absolute floor can exclude it."""
        leader = _candidate("death_metal", 0.80)
        weak = _candidate("bebop", 0.40)
        assert [c.genre_id for c in gs.select_genres([leader, weak], settings=SETTINGS)] == [
            "death_metal"
        ]

    def test_the_relative_floor_excludes_a_much_weaker_second(self) -> None:
        """0.50 clears the absolute floor but is only 25% of a 2.00 leader, so
        only the relative floor can exclude it."""
        leader = _candidate("death_metal", 2.00)
        weak = _candidate("bebop", 0.50)
        assert [c.genre_id for c in gs.select_genres([leader, weak], settings=SETTINGS)] == [
            "death_metal"
        ]

    def test_a_supported_second_is_kept(self) -> None:
        """CONTROL: the floors must not reject a genuinely supported second."""
        leader = _candidate("death_metal", 1.00)
        second = _candidate("bebop", 0.60)
        assert [c.genre_id for c in gs.select_genres([leader, second], settings=SETTINGS)] == [
            "death_metal",
            "bebop",
        ]

    def test_a_manual_genre_bypasses_the_floors(self) -> None:
        leader = _candidate("death_metal", 1.00)
        locked = _candidate("bebop", 0.01)
        locked.manual = True
        assert "bebop" in [
            c.genre_id for c in gs.select_genres([leader, locked], settings=SETTINGS)
        ]


# ---------------------------------------------------------------------------
# Provenance (§15)
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_every_candidate_is_reported_with_a_selected_flag(self) -> None:
        result = full({
            "lastfm@track": ["death metal", "bebop"],
            "musicbrainz@album": ["death metal"],
        })
        ids = {c["id"] for c in result["candidates"]}
        assert {"death_metal", "bebop"} <= ids
        for candidate in result["candidates"]:
            assert "selected" in candidate
        selected = {c["id"] for c in result["candidates"] if c["selected"]}
        assert selected == {"death_metal"}

    def test_evidence_names_the_provider_entity_and_raw_value(self) -> None:
        result = full({"lastfm@track": ["Death Metal"]})
        evidence = result["candidates"][0]["evidence"]
        assert evidence[0]["provider"] == "lastfm"
        assert evidence[0]["entity"] == "track"
        assert evidence[0]["raw_value"] == "Death Metal"

    def test_moved_values_are_reported_by_kind(self) -> None:
        """A value that was RECOGNISED and set aside must be distinguishable from
        one that was never understood — otherwise a mis-classification is silent."""
        result = full({
            "lastfm@track": ["death metal", "dark", "German", "live", "Billboard Hot 100"],
        })
        moved = result["moved"]
        assert moved.get("mood") == ["dark"]
        assert moved.get("geography") == ["German"]
        assert moved.get("attribute") == ["live"]
        assert "Billboard Hot 100" in moved.get("unknown", [])

    def test_a_recognised_non_genre_never_becomes_a_candidate(self) -> None:
        """⚠️ THE PIPELINE-LEVEL TEST FOR CLASSIFICATION.

        ``TestClassification`` calls ``vocab.classify()`` directly, so it passes
        even if the SELECTION pipeline never consults the classification — which
        is the actual defect the report describes ("the selection stage is
        happening too early"). This goes through the real entry point and asserts
        the moods never reach the candidate list.
        """
        result = full({
            "lastfm@track": ["death metal", "dark", "aggressive", "atmospheric"],
            "musicbrainz@album": ["death metal"],
            "listenbrainz@track": ["death metal"],
            "lastfm@artist": ["German"],
            "listenbrainz@track2": [],
        })
        ids = {c["id"] for c in result["candidates"]}
        assert "dark" not in ids
        assert "aggressive" not in ids
        assert "atmospheric" not in ids
        assert "German" not in ids
        assert result["genres"] == ["Death Metal"], result["genres"]
        assert set(result["moved"].get("mood") or []) == {"dark", "aggressive", "atmospheric"}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class TestWiring:
    def test_aggregate_genres_uses_the_engine_by_default(self, monkeypatch) -> None:
        from services.enrichment import genre_aggregation_service as gas

        monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: True)
        out = gas.aggregate_genres(
            {"lastfm@track": ["death metal", "rock"], "musicbrainz@album": ["death metal"]},
            max_genres=3,
        )
        assert out == ["Death Metal"]

    def test_the_legacy_engine_is_still_reachable(self, monkeypatch) -> None:
        """The switch must be a real way back, not a promise.

        ⚠️ Asserted case-insensitively. The legacy engine writes the spelling it
        resolved from the sources (lowercase here) while the canonical engine
        writes the vocabulary's display name, so an exact-match assertion would
        be pinning a cosmetic difference between the two engines rather than the
        fact that the legacy one still runs.
        """
        from services.enrichment import genre_aggregation_service as gas

        monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: False)
        out = gas.aggregate_genres(
            {"lastfm": ["death metal", "rock"], "musicbrainz": ["death metal"]},
            max_genres=3,
        )
        assert [g.casefold() for g in out] == ["death metal"], out

    def test_a_failing_engine_falls_back_instead_of_writing_nothing(
        self, monkeypatch
    ) -> None:
        """A selection engine that crashes must not silently stop writing genres."""
        from services.enrichment import genre_aggregation_service as gas

        monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: True)

        def _boom(*a, **kw):
            raise RuntimeError("vocabulary exploded")

        monkeypatch.setattr(gs, "select_album_or_track_genres", _boom)
        out = gas.aggregate_genres(
            {"lastfm": ["death metal"], "musicbrainz": ["death metal"]}, max_genres=3
        )
        assert [g.casefold() for g in out] == ["death metal"], out

    def test_filter_tags_still_survive_the_new_engine(self, monkeypatch) -> None:
        """⚠️ Live/Cover/Acoustic/Christmas are NOT genres but other code depends
        on them; the genre slots changing must not remove them."""
        from services.enrichment import genre_aggregation_service as gas

        monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: True)
        out = gas.aggregate_genres(
            {"lastfm@track": ["death metal", "live", "acoustic"]},
            context_title="Song (Live)",
            max_genres=3,
        )
        assert "Death Metal" in out
        assert any(t in out for t in ("Live", "Acoustic")), out

    def test_the_two_engines_agree_on_filter_tag_collection(self) -> None:
        """The extraction must be behaviour-preserving, not a reimplementation."""
        from services.enrichment import genre_aggregation_service as gas

        source = {"lastfm": ["death metal", "live", "cover", "remaster"]}
        votes, spellings, hits, inline = gas._vote_genres(source)
        assert inline == gas._collect_intercepted_filter_tags(source)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_defaults_are_the_reports_recommendations(self) -> None:
        from helpers.config_helpers import get_genre_selection_config

        cfg = get_genre_selection_config()
        assert cfg["enabled"] is True
        assert cfg["max_genres"] == 3
        assert cfg["thresholds"]["first"] == 0.60
        assert cfg["source_weights"] == {}, "an empty block means 'use the built-in defaults'"

    def test_the_engine_defaults_lead_with_musicbrainz(self) -> None:
        assert gs.DEFAULT_SOURCE_WEIGHTS["musicbrainz"] == 1.00
        assert gs.DEFAULT_SOURCE_WEIGHTS["manual"] > gs.DEFAULT_SOURCE_WEIGHTS["musicbrainz"]

    def test_config_overrides_are_read(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"genres": {"selection": {
                "enabled": False,
                "max_genres": 2,
                "thresholds": {"first": 0.9},
                "source_weights": {"lastfm": 1.5},
            }}},
        )
        from helpers.config_helpers import get_genre_selection_config

        cfg = get_genre_selection_config()
        assert cfg["enabled"] is False
        assert cfg["max_genres"] == 2
        assert cfg["thresholds"]["first"] == 0.9
        assert cfg["source_weights"] == {"lastfm": 1.5}

    def test_config_tolerates_junk(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"genres": {"selection": {"max_genres": "lots", "thresholds": "no"}}},
        )
        from helpers.config_helpers import get_genre_selection_config

        cfg = get_genre_selection_config()
        assert cfg["max_genres"] == 3
        assert cfg["thresholds"]["first"] == 0.60

    def test_config_survives_a_scalar_selection_block(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"genres": {"selection": "yes"}},
        )
        from helpers.config_helpers import get_genre_selection_config

        assert get_genre_selection_config()["enabled"] is True

    def test_sanitizer_keeps_the_selection_block(self) -> None:
        """Without the nested entry the Config page save coerces it to ``{}``."""
        from routes.ui_routes import _sanitize_config_sections

        cleaned = _sanitize_config_sections(
            {"genres": {"selection": {"enabled": True, "thresholds": {"first": 0.5}}}}
        )
        assert cleaned["genres"]["selection"]["enabled"] is True

    def test_sanitizer_coerces_a_scalar_selection(self) -> None:
        from routes.ui_routes import _sanitize_config_sections

        cleaned = _sanitize_config_sections({"genres": {"selection": "yes"}})
        assert cleaned["genres"]["selection"] == {}


# ---------------------------------------------------------------------------
# UI parity
# ---------------------------------------------------------------------------

CONFIG_PAGES = (
    REPO_ROOT / "templates" / "pages" / "config.html",
    REPO_ROOT / "test_site" / "templates" / "Pages" / "config.html",
)
CONFIG_JS = (
    REPO_ROOT / "static" / "js" / "config.js",
    REPO_ROOT / "test_site" / "static" / "js" / "pages" / "config.js",
)


@pytest.mark.parametrize("page", CONFIG_PAGES, ids=lambda p: p.parts[-3])
class TestConfigPageParity:
    def test_it_offers_the_selection_settings(self, page: Path) -> None:
        html = page.read_text(encoding="utf-8")
        for field in (
            "genre_selection_enabled",
            "genre_selection_max",
            "genre_selection_family_limit",
            "genre_entity_track",
            "genre_entity_album",
            "genre_entity_artist",
            "genre_threshold_first",
            "genre_threshold_second",
            "genre_threshold_third",
        ):
            assert field in html, f"{page.name} is missing {field}"

    def test_it_offers_the_vocabulary_editor(self, page: Path) -> None:
        html = page.read_text(encoding="utf-8")
        for field in (
            "genre_vocab_text",
            "genre_vocab_save",
            "genre_vocab_reset",
            "genre_vocab_validate",
        ):
            assert field in html, f"{page.name} is missing {field}"

    def test_it_reads_the_nested_selection_block(self, page: Path) -> None:
        assert "get('genres', {}).get('selection'" in page.read_text(encoding="utf-8")


@pytest.mark.parametrize("js", CONFIG_JS, ids=lambda p: p.parent.name)
class TestConfigJsParity:
    def test_it_collects_the_selection_block(self, js: Path) -> None:
        source = js.read_text(encoding="utf-8")
        assert "collectGenreSelectionConfig" in source
        assert "selection: collectGenreSelectionConfig()" in source

    def test_it_calls_the_vocabulary_endpoints(self, js: Path) -> None:
        source = js.read_text(encoding="utf-8")
        assert "/api/maintenance/genre-vocabulary" in source
        assert "/api/maintenance/genre-vocabulary/reset" in source
