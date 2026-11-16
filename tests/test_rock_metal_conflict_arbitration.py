"""A single track must never carry BOTH a rock and a metal classification.

REPORTED

> "I found 894 collection rows across 134 distinct artist credits containing
> both Alternative Rock and Alternative Metal in final genres."

    Deftones - Please Please Please...  Alternative Rock; Nu Metal; Alternative Metal; Cover
    Korn - Here to Stay                Nu Metal; Alternative Rock; Alternative Metal
    Saliva - Click Click Boom          Alternative Metal; Nu Metal; Funk Metal; Alternative Rock
    Incubus - Pardon Me                Alternative Rock; Alternative Metal; Funk Metal

THE RULE REQUESTED

Arbitrate PER TRACK on the evidence:

    Metal wins: retain supported metal subgenres, remove conflicting rock.
    Rock wins:  retain supported rock subgenres, remove conflicting metal.
    A weak/isolated metal tag must not defeat a strong rock body.

TWO SEPARATE DEFECTS PRODUCED THE CONFLICT

1. **The Nu Metal companion exemption skipped the exclusive-pair rule.**
   ``alternative_rock`` is an approved Nu Metal companion, and the exemption did
   ``continue`` BEFORE the ``mutually_exclusive`` check — so any track with Nu
   Metal kept Alternative Rock AND Alternative Metal.

2. **The policy was ONE-DIRECTIONAL.** When rock out-scored metal it DECLINED,
   which kept BOTH sides instead of letting rock win. That is the Deftones/Korn
   shape: rock evidence marginally stronger, so neither side was removed.

⚠️ WHAT MUST NOT BREAK

* Nu Metal beside its APPROVED companions (hard_rock / alternative_rock) is a
  legitimate crossover and must keep both.
* A genuinely supported metal genre still deletes rock.
* ``grunge`` / ``math_rock`` beside ``alternative_metal`` is a CONFLICT, not a
  crossover — they are different genres, so one side must go.
"""

from __future__ import annotations

import pytest

from services.enrichment import genre_selection as gs

ROCK_FAMILY = {"alternative rock", "hard rock", "indie rock", "grunge",
               "post-grunge", "punk rock", "math rock", "noise rock",
               "stoner rock", "folk rock", "pop rock", "blues rock",
               "classic rock", "progressive rock", "industrial rock"}

METAL_FAMILY = {"alternative metal", "nu metal", "funk metal", "heavy metal",
                "death metal", "thrash metal", "metalcore", "industrial metal",
                "groove metal", "power metal", "symphonic metal", "gothic metal",
                "melodic death metal", "progressive metal", "sludge metal",
                "stoner metal", "speed metal", "doom metal", "black metal"}


def select(*titles: str) -> list[str]:
    ev = {
        "lastfm@track": list(titles),
        "musicbrainz@album": list(titles),
        "listenbrainz@track": list(titles),
    }
    return gs.select_album_or_track_genres(ev, max_genres=4)["genres"]


def _sides(genres: list[str]) -> tuple[list[str], list[str]]:
    folded = [g.casefold() for g in genres]
    return (
        [g for g in folded if g in ROCK_FAMILY],
        [g for g in folded if g in METAL_FAMILY],
    )


class TestTheReportedRowsNoLongerConflict:
    @pytest.mark.parametrize(
        "titles",
        [
            ("alternative rock", "nu metal", "alternative metal"),
            ("alternative metal", "nu metal", "funk metal", "alternative rock"),
            ("alternative rock", "alternative metal", "funk metal"),
            ("alternative rock", "alternative metal"),
        ],
        ids=["deftones", "saliva", "incubus", "plain-pair"],
    )
    def test_alternative_rock_and_metal_never_coexist(self, titles) -> None:
        result = select(*titles)
        folded = [g.casefold() for g in result]
        assert not (
            "alternative rock" in folded and "alternative metal" in folded
        ), f"the exclusive pair co-existed: {result}"

    @pytest.mark.parametrize(
        "rock,metal",
        [
            ("grunge", "alternative metal"),
            ("post-grunge", "nu metal"),
            ("math rock", "alternative metal"),
            ("pop rock", "funk metal"),
            ("hard rock", "thrash metal"),
            ("indie rock", "metalcore"),
        ],
    )
    def test_a_track_does_not_keep_a_rock_and_a_metal_classification(
        self, rock, metal
    ) -> None:
        """THE RULE — arbitrated per track, on the evidence."""
        result = select(rock, metal)
        rocks, metals = _sides(result)
        assert not (rocks and metals), (
            f"{rock!r} and {metal!r} both survived: {result}"
        )


class TestTheOriginalBehaviourSurvives:
    @pytest.mark.parametrize(
        "rock,metal",
        [
            ("gothic rock", "gothic metal"),
            ("folk rock", "folk metal"),
            ("progressive rock", "progressive metal"),
            ("stoner rock", "stoner metal"),
            ("industrial rock", "industrial metal"),
        ],
    )
    def test_a_supported_metal_genre_still_removes_rock(self, rock, metal) -> None:
        """CONTROL — same specificity, same sound: metal is the answer."""
        result = select(rock, metal)
        assert metal.title() in result, result
        assert rock.title() not in result, result

    def test_nu_metal_keeps_its_approved_companions(self) -> None:
        """CONTROL — a legitimate crossover keeps both sides."""
        result = select("nu metal", "hard rock")
        assert "Nu Metal" in result and "Hard Rock" in result, result

    def test_nu_metal_does_not_keep_alternative_rock(self) -> None:
        """``alternative_rock`` is an ANCESTOR of ``nu_metal`` — it does not pair.

        The collection's rule: "Nu Metal should be able to be aligned with Hard
        Rock, but not Alternative Rock." Keeping both restated one identity at
        two specificities, which is how a track ended up with both Alternatives.
        """
        result = select("nu metal", "alternative rock")
        assert "Nu Metal" in result, result
        assert "Alternative Rock" not in result, result

    def test_a_non_companion_rock_genre_still_goes_beside_nu_metal(self) -> None:
        """CONTROL — the exception is a short list, not every rock genre."""
        result = select("nu metal", "blues rock")
        assert "Blues Rock" not in result, result


class TestRockWinsOnStrongerEvidence:
    def test_rock_can_now_win_instead_of_keeping_both(self) -> None:
        """THE SECOND DEFECT — the policy used to decline and keep both.

        Rock is deliberately given a stronger body of evidence: three rock
        genres against one metal genre.
        """
        result = select("alternative rock", "grunge", "post-grunge",
                        "alternative metal")
        rocks, metals = _sides(result)
        assert rocks, f"the stronger rock evidence was lost entirely: {result}"
        assert not metals, (
            f"metal survived even though rock carried more evidence: {result}"
        )

    def test_a_lone_weak_metal_tag_does_not_delete_a_rock_cluster(self) -> None:
        """The report's own boundary: "an isolated, weak metal tag".

        ⚠️ The assertion is that ROCK SURVIVES — not that metal is deleted. When
        the metal genre is the better-supported side it legitimately wins; what
        the report forbids is a weak tag wiping out a strong rock body.
        """
        result = select("punk rock", "punk", "rock", "death metal")
        assert any(g.casefold() in ROCK_FAMILY for g in result), (
            f"the rock cluster was wiped out by one metal tag: {result}"
        )
