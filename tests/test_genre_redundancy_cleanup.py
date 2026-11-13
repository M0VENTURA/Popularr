"""Redundant and contradictory final genres must not survive.

REPORTED

> "Rage Against the Machine - Down Radio end with Alternative Metal, Alternative
> Rock and Math Rock."

> "I still had ... Chiodos is also showing up in the Alternative Metal playlist
> even though its tracks have Post-Hardcore/Emo in the Genre tag."

> "Heavy Metal is the Same as Metal too, so any other metal genre will wipe it
> out."

> "Based on all the genres, the final genres don't really match up."

And the export of 11 842 violations, whose top categories were:

    Rock + Metal                                          2146
    'death metal' removed by 'melodic death metal'         749
    'industrial' removed by 'industrial metal'             673
    'rock' removed by 'alternative rock'                    474
    'pop' removed by 'pop rock'                            230
    'folk' removed by 'folk rock'                          192
    'grunge' removed by 'post-grunge'                      187
    'rock' removed by 'punk rock'                          175

WHAT WAS WRONG

1. ``remove_generic_parents`` only considered ``specificity == 0``, so
   ``Heavy Metal`` (specificity 1, parent of twelve concrete subgenres) survived
   every one of them. It is the metal umbrella, exactly as the request says.
2. ``remove_redundant_ancestors`` applied ``ancestor_replace_min_ratio`` to TRUE
   ancestor/descendant pairs. Providers tag the umbrella more often, so
   ``Death Metal`` (2.33) beat ``Melodic Death Metal`` (1.85) and both survived.
3. The vocabulary was missing parent edges: ``industrial_metal`` did not list
   ``industrial``, ``folk_rock`` did not list ``folk``, and ``post_grunge`` was a
   SIBLING of ``grunge`` rather than its descendant.
4. The contract for cross-family absorption uses the child's NAME: "Pop Rock"
   names Pop, "Synthpop" does not.
"""

from __future__ import annotations

import pytest

from services.enrichment import genre_selection as gs
from services.enrichment.genre_vocabulary import get_vocabulary

SETTINGS = gs.SelectionSettings(
    source_weights=gs.DEFAULT_SOURCE_WEIGHTS,
    entity_weights=gs.DEFAULT_ENTITY_WEIGHTS,
    keep_together=gs.DEFAULT_KEEP_TOGETHER,
    conditional_keep=gs.DEFAULT_CONDITIONAL_KEEP,
    mutually_exclusive=gs.DEFAULT_MUTUALLY_EXCLUSIVE,
)


def select(source_map: dict) -> list[str]:
    return gs.select_album_or_track_genres(source_map, settings=SETTINGS)["genres"]


def _pair_evidence(parent: str, child: str) -> dict:
    """The parent is tagged by MORE sources — the real provider behaviour."""
    return {
        "lastfm@track": [child, parent],
        "musicbrainz@album": [child, parent],
        "listenbrainz@track": [parent],
    }


#: (parent that must go, child that must stay) — the export's top categories.
REDUNDANT_PAIRS = [
    ("death metal", "melodic death metal"),
    ("industrial", "industrial metal"),
    ("rock", "alternative rock"),
    ("pop", "pop rock"),
    ("folk", "folk rock"),
    ("grunge", "post-grunge"),
    ("rock", "punk rock"),
    ("rock", "indie rock"),
    ("heavy metal", "nu metal"),
    ("punk", "hardcore punk"),
]


class TestTheReportedViolationsAreGone:
    @pytest.mark.parametrize("parent,child", REDUNDANT_PAIRS)
    def test_the_parent_is_removed_by_its_child(self, parent, child) -> None:
        result = select(_pair_evidence(parent, child))
        assert parent not in [g.casefold() for g in result], (
            f"{parent!r} survived beside {child!r}: {result}"
        )

    def test_heavy_metal_is_treated_as_the_metal_umbrella(self) -> None:
        """The request, verbatim: "Heavy Metal is the Same as Metal too"."""
        vocab = get_vocabulary()
        entry = vocab.genres["heavy_metal"]
        assert entry.specificity == 0, (
            "Heavy Metal must be a generic root or the generic-parent rule skips it"
        )
        # And in practice: any concrete metal genre removes it.
        for child in ("thrash metal", "death metal", "black metal", "power metal"):
            result = select(_pair_evidence("heavy metal", child))
            assert "heavy metal" not in [g.casefold() for g in result], (
                f"Heavy Metal survived beside {child}: {result}"
            )


class TestTheVocabularyDeclaresTheRealAncestry:
    """Missing parent edges made several pairs structurally impossible to fix."""

    @pytest.mark.parametrize(
        "child,ancestor",
        [
            ("industrial_metal", "industrial"),
            ("folk_rock", "folk"),
            ("post_grunge", "grunge"),
        ],
    )
    def test_the_edge_exists(self, child, ancestor) -> None:
        vocab = get_vocabulary()
        assert ancestor in vocab.genres[child].ancestors(vocab.genres), (
            f"{child} must declare {ancestor} as an ancestor, or no rule can "
            "condense the pair"
        )


class TestCrossFamilyAbsorptionUsesTheName:
    """The rule the request itself stated: "pop removed by pop rock"."""

    def test_a_rock_child_naming_its_parent_absorbs_it(self) -> None:
        result = select(_pair_evidence("pop", "pop rock"))
        assert "Pop" not in result, result

    def test_a_different_sound_reaching_the_same_parent_does_not(self) -> None:
        """--- CONTROL ---

        ``Synthpop`` lists ``pop`` as a secondary parent but is an ELECTRONIC
        genre: a different sound, so Pop must survive. Pinned by
        ``test_an_unrelated_family_child_does_not_remove_the_root``.
        """
        result = select({
            "musicbrainz@track": ["pop", "synthpop"],
            "lastfm@track": ["pop", "synthpop"],
            "listenbrainz@track": ["pop", "synthpop"],
        })
        assert "Pop" in result, result


class TestCrossoverGenresAreRecognised:
    """REPORTED — "other than Nu Metal and Hard Rock, most other rock genres
    wouldn't cross over with Alternative Metal".

    Two gaps, both confirmed against the real vocabulary:

    * ``rap metal`` / ``rapcore`` / ``rap rock`` were NOT in the vocabulary at
      all. A Rage Against the Machine track — whose defining tag is exactly that
      hybrid — had the tag silently discarded as "unknown", leaving only Funk
      Metal and Alternative Metal.
    * ``Hard Rock`` was kept beside Nu Metal and DELETED beside Alternative
      Metal, because the companions only applied when Nu Metal was the sole
      metal genre.
    """

    @pytest.mark.parametrize(
        "value", ["rap metal", "rapcore", "rap rock", "rap-metal", "rap-rock"]
    )
    def test_rap_metal_is_a_recognised_genre(self, value) -> None:
        vocab = get_vocabulary()
        classification = vocab.classify(value)
        assert classification.kind == "genre", (
            f"{value!r} is discarded as unknown, so the tag is silently lost"
        )
        assert classification.canonical == "rap_metal"

    def test_rap_metal_is_metal_family(self) -> None:
        vocab = get_vocabulary()
        entry = vocab.genres["rap_metal"]
        assert (entry.family or "") == "metal"
        # And it descends from both parents, so neither side is lost.
        ancestors = entry.ancestors(vocab.genres)
        assert "metal" in ancestors
        assert "hip_hop" in ancestors

    def test_a_ratm_style_track_keeps_its_rap_metal_tag(self) -> None:
        """THE REPORT — the hybrid tag must survive, not be dropped."""
        result = select({
            "lastfm@track": ["rap metal", "funk metal", "alternative metal", "rock"],
            "musicbrainz@track": ["rap metal", "funk metal", "alternative metal"],
            "discogs@track": ["Rock", "Funk Metal"],
        })
        assert "Rap Metal" in result, (
            f"the defining hybrid tag was lost: {result}"
        )

    def test_hard_rock_survives_beside_alternative_metal(self) -> None:
        """The asymmetry: kept beside Nu Metal, deleted beside Alt Metal."""
        result = select({
            "musicbrainz@track": ["alternative metal", "hard rock"],
            "lastfm@track": ["alternative metal", "hard rock"],
            "listenbrainz@track": ["alternative metal", "hard rock"],
        })
        assert "Hard Rock" in result, result

    @pytest.mark.parametrize("metal", ["death metal", "thrash metal", "heavy metal"])
    def test_a_non_crossover_metal_still_suppresses_rock(self, metal) -> None:
        """--- CONTROL ---

        Widening the companions must NOT disable the policy. These three are not
        hybrids, so Alternative Rock still goes.
        """
        result = select({
            "musicbrainz@track": [metal, "alternative rock"],
            "lastfm@track": [metal, "alternative rock"],
            "listenbrainz@track": [metal, "alternative rock"],
        })
        assert "Alternative Rock" not in result, (
            f"{metal} stopped suppressing rock, so the policy is switched off: {result}"
        )


class TestTheReportedTracks:
    def test_ratm_down_radio_does_not_keep_three_rock_and_metal_tags(self) -> None:
        """THE REPORT — "Alternative Metal, Alternative Rock and Math Rock"."""
        result = select({
            "lastfm@track": "Alternative Metal, Alternative Rock, Math Rock",
            "musicbrainz@album": "Alternative Metal, Alternative Rock, Math Rock",
        })
        folded = [g.casefold() for g in result]
        assert not (
            "alternative metal" in folded and "alternative rock" in folded
        ), f"the mutually-exclusive pair co-existed: {result}"

    def test_chiodos_keeps_the_genres_that_describe_it(self) -> None:
        """THE REPORT — a post-hardcore/emo band on the Alternative Metal playlist.

        Its real evidence, pasted from the provider panels. Rock (3.85) is the
        strongest family, punk (2.00) next, metal (2.40) in between; the answer
        must not be two thirds metal.
        """
        result = select({
            "listenbrainz@track": [
                "alternative punk", "post-hardcore", "pop/rock", "punk revival",
                "emo", "heavy metal", "alternative metal",
                "alternative/indie rock", "punk-pop",
            ],
            "discogs@track": ["Alternative Rock", "Post-Hardcore", "Rock"],
            "musicbrainz@track": [
                "alternative punk", "alternative metal", "emo", "heavy metal",
                "post-hardcore",
            ],
        })
        folded = [g.casefold() for g in result]
        assert "heavy metal" not in folded, (
            f"the metal umbrella survived: {result}"
        )
        assert "emo" in folded or "post-hardcore" in folded, (
            f"neither genre that describes this band survived: {result}"
        )
