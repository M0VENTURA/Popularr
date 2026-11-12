"""The collection policy: an accepted metal genre suppresses rock-family genres.

THE POLICY
----------
Once a track is credibly metal, the rock side of it is redundant, so the final
genres keep the metal identities:

    Folk Metal + Folk Rock        -> Folk Metal
    Gothic Metal + Gothic Rock    -> Gothic Metal
    Stoner Metal + Stoner Rock    -> Stoner Metal
    Progressive Metal + Prog Rock -> Progressive Metal

TWO BOUNDARIES THAT MUST NOT MOVE
---------------------------------
1. **Nu Metal alone is exempt.** Nu Metal IS a metal/rock hybrid; stripping
   ``Hard Rock`` from a Nu Metal track deletes the crossover the tag names. Only
   ``Hard Rock`` and ``Alternative Rock`` are approved companions, and a second,
   non-Nu-Metal genre cancels the exception.

2. **Only ROCK-family genres are removed.** Punk, post-hardcore, electronic,
   hip-hop, pop and R&B are untouched — ``Metalcore + Post-Hardcore`` and
   ``Metalcore + Hardstyle`` must both survive. Suppressing those would turn a
   metal preference into a metal-only filter.

ORDER MATTERS: outlier rejection runs BEFORE this policy, so one weak metal tag
cannot delete a well-supported rock cluster.
"""

from __future__ import annotations

import pytest

from services.enrichment import genre_selection as gs

SETTINGS = gs.SelectionSettings(
    source_weights=gs.DEFAULT_SOURCE_WEIGHTS,
    entity_weights=gs.DEFAULT_ENTITY_WEIGHTS,
    keep_together=gs.DEFAULT_KEEP_TOGETHER,
    conditional_keep=gs.DEFAULT_CONDITIONAL_KEEP,
    mutually_exclusive=gs.DEFAULT_MUTUALLY_EXCLUSIVE,
)


def select(source_map: dict, **kwargs) -> list[str]:
    return gs.select_album_or_track_genres(
        source_map, settings=kwargs.pop("settings", SETTINGS), **kwargs
    )["genres"]


def full(source_map: dict, **kwargs) -> dict:
    return gs.select_album_or_track_genres(
        source_map, settings=kwargs.pop("settings", SETTINGS), **kwargs
    )


def _reasons(result: dict) -> dict[str, dict]:
    """Removal records keyed by canonical id."""
    return {r["id"]: r for r in result.get("removed") or []}


# A pair listed across several track-level sources, so both sides are
# well-supported and none of them is an outlier.
def _pair(a: str, b: str) -> dict:
    return {
        "musicbrainz@track": [a, b],
        "lastfm@track": [a, b],
        "listenbrainz@track": [a, b],
    }


# ---------------------------------------------------------------------------
# 1. Metal removes its rock counterpart
# ---------------------------------------------------------------------------

class TestMetalSuppressesRock:
    @pytest.mark.parametrize(
        "metal,rock",
        [
            ("folk metal", "folk rock"),
            ("gothic metal", "gothic rock"),
            ("progressive metal", "progressive rock"),
            ("heavy metal", "hard rock"),
            ("doom metal", "psychedelic rock"),
        ],
    )
    def test_the_metal_genre_survives_and_the_rock_genre_does_not(
        self, metal: str, rock: str
    ) -> None:
        result = select(_pair(metal, rock))
        assert any("metal" in g.casefold() for g in result), result
        assert not any(g.casefold() == rock for g in result), (
            f"{rock!r} should have been suppressed by {metal!r}: {result}"
        )

    def test_the_removal_is_explained_with_a_trigger(self) -> None:
        result = full(_pair("folk metal", "folk rock"))
        removed = _reasons(result)
        assert "folk_rock" in removed
        record = removed["folk_rock"]
        assert record["reason"] == gs.REASON_ROCK_SUPPRESSED_BY_METAL
        assert record["trigger"], "the removal must name the genre that caused it"

    def test_stoner_rock_is_a_rock_genre_not_a_metal_alias(self) -> None:
        """REPORTED: ``stoner metal`` claimed ``"stoner rock"`` as an alias, so
        Stoner Rock could never exist as its own rock genre."""
        vocab = gs.get_vocabulary()
        assert "stoner_rock" in vocab.genres
        assert vocab.genres["stoner_rock"].family == "rock"
        assert vocab.genres["stoner_metal"].family == "metal"
        # The alias must no longer collapse the two.
        assert vocab.classify("stoner rock").canonical == "stoner_rock"

    def test_the_policy_can_be_switched_off(self) -> None:
        settings = gs.SelectionSettings(
            **{**SETTINGS.__dict__, "metal_suppresses_rock": False}
        )
        result = select(_pair("folk metal", "folk rock"), settings=settings)
        assert "Folk Rock" in result, result


# ---------------------------------------------------------------------------
# 2. The Nu Metal exception
# ---------------------------------------------------------------------------

class TestTheNuMetalException:
    def test_nu_metal_keeps_hard_rock(self) -> None:
        result = select(_pair("nu metal", "hard rock"))
        assert "Nu Metal" in result, result
        assert "Hard Rock" in result, result

    def test_nu_metal_keeps_alternative_rock(self) -> None:
        result = select(_pair("nu metal", "alternative rock"))
        assert "Nu Metal" in result, result
        assert "Alternative Rock" in result, result

    def test_nu_metal_keeps_both_approved_companions_when_both_are_supported(self) -> None:
        """Both companions survive the POLICY, given equal support.

        ``Alternative Rock`` is an ancestor of ``nu_metal`` (via
        ``alternative_metal``), so this also pins the ancestor-condensation
        exemption — without it the companion is dropped before the policy runs.

        Support is equalised deliberately: a companion that scores far below the
        leader is still dropped by the "each position must earn its place" rule,
        which is a separate (and correct) rule — see
        ``test_a_weakly_supported_companion_still_loses_its_slot``.
        """
        result = select({
            "musicbrainz@track": ["nu metal", "hard rock", "alternative rock"],
            "lastfm@track": ["nu metal", "hard rock", "alternative rock"],
            "listenbrainz@track": ["nu metal", "hard rock", "alternative rock"],
        })
        assert "Nu Metal" in result, result
        assert "Hard Rock" in result, result
        assert "Alternative Rock" in result, result

    def test_a_weakly_supported_companion_still_loses_its_slot(self) -> None:
        """The exception permits a companion; it does not guarantee it a slot.

        ``Alternative Rock`` listed by ONE source beside a strongly corroborated
        Nu Metal + Hard Rock pair is below the relative floor against the
        leader, so the ordinary ranking rule drops it.
        """
        result = full(_pair("nu metal", "hard rock") | {
            "discogs@track": ["alternative rock"],
        })
        assert "Nu Metal" in result["genres"], result["genres"]
        assert "Hard Rock" in result["genres"], result["genres"]
        assert "Alternative Rock" not in result["genres"], result["genres"]
        # ...and it is explained as a ranking outcome, not a policy rejection.
        removed = _reasons(result)
        assert removed["alternative_rock"]["reason"] == gs.REASON_LOW_CONFIDENCE

    def test_a_second_metal_genre_cancels_the_exception(self) -> None:
        """``Alternative Metal`` is NOT Nu Metal, so rock is suppressed again."""
        result = select({
            "musicbrainz@track": ["nu metal", "alternative metal", "hard rock"],
            "lastfm@track": ["nu metal", "alternative metal", "hard rock"],
            "listenbrainz@track": ["nu metal", "alternative metal"],
        })
        assert "Alternative Metal" in result, result
        assert "Hard Rock" not in result, result

    def test_a_non_approved_rock_genre_is_still_removed(self) -> None:
        """The exception is a SHORT list, not every rock genre."""
        result = select(_pair("nu metal", "blues rock"))
        assert "Nu Metal" in result, result
        assert "Blues Rock" not in result, result

    def test_the_exception_can_be_switched_off(self) -> None:
        settings = gs.SelectionSettings(
            **{**SETTINGS.__dict__, "nu_metal_exception": False}
        )
        result = select(_pair("nu metal", "hard rock"), settings=settings)
        assert "Hard Rock" not in result, result


# ---------------------------------------------------------------------------
# 3. Crossovers that must SURVIVE
# ---------------------------------------------------------------------------

class TestCrossoversSurvive:
    def test_metalcore_keeps_post_hardcore(self) -> None:
        """Post-Hardcore is punk-family: the policy must not touch it."""
        result = select(_pair("metalcore", "post-hardcore"))
        assert "Metalcore" in result, result
        assert "Post-Hardcore" in result, result

    def test_metalcore_keeps_hard_rock_removed_but_post_hardcore_kept(self) -> None:
        result = select({
            "musicbrainz@track": ["metalcore", "post-hardcore", "hard rock"],
            "lastfm@track": ["metalcore", "post-hardcore", "hard rock"],
            "listenbrainz@track": ["metalcore", "post-hardcore"],
        })
        assert "Metalcore" in result, result
        assert "Post-Hardcore" in result, result
        assert "Hard Rock" not in result, result

    def test_a_metal_electronic_crossover_keeps_electronic(self) -> None:
        result = select(_pair("metalcore", "hardstyle"))
        assert "Metalcore" in result, result
        assert "Hardstyle" in result, result

    def test_pop_punk_is_not_suppressed_by_metal(self) -> None:
        """Pop Punk is punk-family. The plan names it explicitly."""
        result = select(_pair("alternative metal", "pop punk"))
        assert "Alternative Metal" in result, result
        assert "Pop Punk" in result, result

    def test_hardcore_punk_is_not_suppressed_by_metal(self) -> None:
        result = select(_pair("death metal", "hardcore punk"))
        assert "Death Metal" in result, result
        assert "Hardcore Punk" in result, result


# ---------------------------------------------------------------------------
# 4. Evidence is validated BEFORE the policy
# ---------------------------------------------------------------------------

class TestOutliersAreRejectedFirst:
    def test_an_isolated_weak_metal_tag_does_not_delete_a_rock_cluster(self) -> None:
        """THE REPORTED OFFSPRING CASE.

        A well-supported punk/rock cluster plus ONE weak death-metal tag must
        not come out as death metal — the outlier is rejected on its own
        (lack of) evidence, before it can ever suppress the rock side.
        """
        result = full({
            "musicbrainz@track": ["punk rock", "punk", "rock"],
            "lastfm@track": ["punk rock", "punk", "rock"],
            "listenbrainz@track": ["punk rock", "rock"],
            "discogs@artist": ["death metal"],   # artist-level, one source
        })
        assert not any("death metal" in g.casefold() for g in result["genres"]), (
            f"a lone artist-level death metal tag won the album: {result['genres']}"
        )
        assert any(
            g.casefold() in ("punk rock", "punk") for g in result["genres"]
        ), result["genres"]

        # And it is reported as an OUTLIER, not as a metal-suppression casualty.
        removed = _reasons(result)
        if "death_metal" in removed:
            assert removed["death_metal"]["reason"] == gs.REASON_FAMILY_OUTLIER


# ---------------------------------------------------------------------------
# 5. A manual genre is never dropped by the policy
# ---------------------------------------------------------------------------

class TestManualGenresAreProtected:
    def test_a_manual_rock_genre_survives_a_metal_genre(self) -> None:
        """An explicit user correction outranks the collection policy."""
        result = select({
            "manual@track": ["folk rock"],
            "musicbrainz@track": ["folk metal"],
            "lastfm@track": ["folk metal"],
            "listenbrainz@track": ["folk metal"],
        })
        assert "Folk Rock" in result, (
            f"a manual genre was removed by the metal policy: {result}"
        )
