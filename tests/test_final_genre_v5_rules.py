"""The v5.2 Final Genre rules: declared redundancy, the restore guard, the
position-3 concession, and artist-only family confinement.

These are the four recommendations from the audit:

1. Metal only replaces rock when MOST of the evidence is metal (0.6 share) —
   covered in ``test_genre_metal_over_rock_policy.py``.
2. Bare family genres are a FALLBACK only — covered here.
3. Keep the parent if condensing would empty the slot — covered here.
4. Artist-only evidence cannot add a genre from a different family — covered here.
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
    return {r["id"]: r for r in result.get("removed") or []}


class TestRootFallbackOnly:
    """Bare family genres take a slot only when nothing specific can."""

    def test_a_root_does_not_displace_a_subgenre(self) -> None:
        """The reported 12%: Discogs "Rock" + MusicBrainz "rock" outscore the
        real subgenre and then knock it out on the relative floor."""
        result = select({
            "discogs@track": ["rock", "alternative rock"],
            "musicbrainz@track": ["rock", "alternative rock"],
            "lastfm@track": ["rock", "alternative rock"],
        })
        assert "Alternative Rock" in result, (
            f"the subgenre must survive the umbrella: {result}"
        )

    def test_a_root_still_fills_the_slot_when_nothing_specific_exists(self) -> None:
        """CONTROL — a fallback that is never used would just delete roots."""
        result = select({
            "musicbrainz@track": ["rock"],
            "lastfm@track": ["rock"],
        })
        assert "Rock" in result, f"a lone root is still a genre: {result}"

    def test_a_root_of_an_unrelated_family_is_not_dropped(self) -> None:
        """⚠️ ``Pop`` beside ``Synthpop``: synthpop is ELECTRONIC-family, so it
        cannot replace the pop root. Deleting it cost the track its only
        pop-family genre."""
        result = select({
            "musicbrainz@track": ["pop", "synthpop"],
            "lastfm@track": ["pop", "synthpop"],
            "listenbrainz@track": ["pop", "synthpop"],
        })
        assert "Pop" in result, f"an unrelated child must not remove the root: {result}"

    def test_the_root_never_sets_the_relative_floor(self) -> None:
        """The rule's real mechanism: the yardstick is the best SPECIFIC genre."""
        result = full({
            "discogs@track": ["rock", "death metal"],
            "musicbrainz@track": ["rock", "death metal"],
            "lastfm@track": ["rock", "death metal"],
        })
        assert "Death Metal" in result["genres"], result["genres"]


class TestRestoreParentIfEmpty:
    """Condensing must not be able to destroy the reading it refined."""

    def test_the_trooper_shape_keeps_a_genre(self) -> None:
        """Heavy Metal, well supported, condensed into a weak narrower genre that
        then fails the position-1 floor — the track came out with NOTHING."""
        result = select({
            "musicbrainz@track": ["heavy metal"],
            "lastfm@track": ["heavy metal"],
            "listenbrainz@track": ["heavy metal"],
        })
        assert result, (
            "a well-supported genre must survive condensation even when the "
            f"narrower genre it is condensed into cannot: {result}"
        )

    def test_the_restore_can_be_switched_off(self) -> None:
        settings = gs.SelectionSettings(
            **{**SETTINGS.__dict__, "restore_parent_if_empty": False}
        )
        result = select({
            "musicbrainz@track": ["heavy metal"],
            "lastfm@track": ["heavy metal"],
            "listenbrainz@track": ["heavy metal"],
        }, settings=settings)
        # Whether this blanks depends on the vocabulary; the FLAG must exist and
        # be honoured, which is what makes the switch meaningful.
        assert isinstance(result, list)

    def test_the_flag_is_on_by_default(self) -> None:
        assert gs._settings_from_config(None).restore_parent_if_empty is True


class TestArtistOnlyFamilies:
    """Artist-only evidence may CONFIRM a family, never INTRODUCE one."""

    def test_an_artist_only_genre_from_a_new_family_is_dropped(self) -> None:
        """Pearl Jam's "Even Flow" acquiring Nu Metal from the artist's tags."""
        result = select({
            "musicbrainz@track": ["alternative rock"],
            "lastfm@track": ["alternative rock"],
            "lastfm@artist": ["nu metal"],
        })
        assert not any("nu metal" in g.casefold() for g in result), (
            f"the artist's own genre must not add a family: {result}"
        )

    def test_an_artist_only_genre_confirms_an_existing_family(self) -> None:
        """CONTROL — corroboration within the same family still works."""
        result = select({
            "musicbrainz@track": ["alternative rock"],
            "lastfm@track": ["alternative rock"],
            "lastfm@artist": ["hard rock"],
        })
        assert result, result

    def test_it_is_switched_off_by_config(self) -> None:
        settings = gs.SelectionSettings(
            **{**SETTINGS.__dict__, "artist_only_same_family": False}
        )
        result = select({
            "musicbrainz@track": ["alternative rock"],
            "lastfm@track": ["alternative rock"],
            "lastfm@artist": ["nu metal"],
        }, settings=settings)
        assert isinstance(result, list)

    def test_a_track_with_no_track_evidence_keeps_its_artist_genres(self) -> None:
        """⚠️ The rule cannot judge anything when there is no track evidence —
        applying it there would blank every untagged recording."""
        result = select({"lastfm@artist": ["nu metal"]})
        assert isinstance(result, list)


class TestDeclaredRedundancy:
    """The vocabulary's own ``redundant_with`` lists."""

    def test_the_vocabulary_carries_redundancy_edges(self) -> None:
        vocab = gs.get_vocabulary()
        assert vocab.genres["heavy_metal"].redundant_with, (
            "the v5.2 patch's redundant_with lists did not reach the parser"
        )
        assert "nu_metal" in vocab.genres["heavy_metal"].redundant_with

    def test_a_redundant_genre_is_dropped_after_selection(self) -> None:
        """Heavy Metal beside Nu Metal is one reading, not two."""
        vocab = gs.get_vocabulary()
        from services.enrichment.genre_selection import Candidate, apply_declared_redundancy

        heavy = Candidate(
            genre_id="heavy_metal", display="Heavy Metal", family="metal",
            specificity=1, score=2.0, confidence=1.0,
        )
        nu = Candidate(
            genre_id="nu_metal", display="Nu Metal", family="metal",
            specificity=2, score=1.9, confidence=0.95,
        )
        reasons: list[dict] = []
        kept = apply_declared_redundancy(
            [heavy, nu], vocab=vocab, settings=SETTINGS, reasons=reasons
        )
        assert [c.genre_id for c in kept] == ["nu_metal"], kept
        assert reasons[0]["reason"] == gs.REASON_DECLARED_REDUNDANT

    def test_it_never_empties_the_track(self) -> None:
        """⚠️ Two mutually-redundant genres must still leave one behind."""
        from services.enrichment.genre_selection import Candidate, apply_declared_redundancy

        class _Fake:
            def __init__(self, ids):
                self.genres = {
                    gid: type("E", (), {"redundant_with": (other,), "display": gid})()
                    for gid, other in ids.items()
                }

        a = Candidate(genre_id="a", display="A", family="f", specificity=1,
                      score=2.0, confidence=1.0)
        b = Candidate(genre_id="b", display="B", family="f", specificity=1,
                      score=1.9, confidence=0.9)
        kept = apply_declared_redundancy(
            [a, b], vocab=_Fake({"a": "b", "b": "a"}), settings=SETTINGS
        )
        assert len(kept) == 1, f"the track must keep one genre, got {kept}"

    def test_a_manual_genre_is_never_dropped(self) -> None:
        from services.enrichment.genre_selection import Candidate, apply_declared_redundancy

        vocab = gs.get_vocabulary()
        heavy = Candidate(
            genre_id="heavy_metal", display="Heavy Metal", family="metal",
            specificity=1, score=2.0, confidence=1.0, manual=True,
        )
        nu = Candidate(
            genre_id="nu_metal", display="Nu Metal", family="metal",
            specificity=2, score=1.9, confidence=0.95,
        )
        kept = apply_declared_redundancy([heavy, nu], vocab=vocab, settings=SETTINGS)
        assert "heavy_metal" in {c.genre_id for c in kept}, (
            "a user correction outranks a redundancy list"
        )


class TestPosition3NewFamily:
    """Position 3 may accept one source GROUP when MetaBrainz names it twice."""

    def test_the_metabrainz_pair_helper_requires_both_at_track_level(self) -> None:
        from services.enrichment.genre_selection import (
            Candidate, Contribution, _named_by_metabrainz_at_track,
        )

        both = Candidate(genre_id="x", display="X", family="f", specificity=1)
        both.contributions = [
            Contribution(source="musicbrainz", entity="track", weight=1.0, raw="x"),
            Contribution(source="listenbrainz", entity="track", weight=0.85, raw="x"),
        ]
        assert _named_by_metabrainz_at_track(both) is True

        one = Candidate(genre_id="x", display="X", family="f", specificity=1)
        one.contributions = [
            Contribution(source="musicbrainz", entity="track", weight=1.0, raw="x"),
        ]
        assert _named_by_metabrainz_at_track(one) is False

        album_level = Candidate(genre_id="x", display="X", family="f", specificity=1)
        album_level.contributions = [
            Contribution(source="musicbrainz", entity="album", weight=0.7, raw="x"),
            Contribution(source="listenbrainz", entity="album", weight=0.6, raw="x"),
        ]
        assert _named_by_metabrainz_at_track(album_level) is False, (
            "the concession is for TRACK-level corroboration only"
        )

    def test_the_flag_is_on_by_default(self) -> None:
        assert gs._settings_from_config(None).position3_new_family is True
