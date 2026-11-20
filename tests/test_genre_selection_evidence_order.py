"""Regressions in how evidence, corroboration and condensation interact.

Each test pins a real track from the collection export, because every failure
here was an INTERACTION between two rules that passed their own unit tests:

1. Umbrella absorption ignored support, so ONE stray tag deleted a
   three-source Heavy Metal reading.
2. The corroboration gate ran AFTER condensation, so an uncorroborated child
   deleted its corroborated parent and was then removed itself — leaving
   neither.
3. The gate's last-resort rescue was then rejected by the confidence floors,
   so the safeguard that exists to avoid blanking blanked anyway.
"""

from __future__ import annotations

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


class TestSupportGuardOnUmbrellaAbsorption:
    def test_a_stray_narrow_tag_cannot_delete_heavy_metal(self) -> None:
        """⭐ JUDAS PRIEST — Redeemer of Souls.

        MusicBrainz, Discogs and ListenBrainz all say Heavy Metal (score 2.87,
        confidence 1.0). ListenBrainz alone adds "nu metal" (score 0.95, ratio
        0.33). The umbrella rule used to absorb Heavy Metal into that stray tag
        and the result was "Nu Metal".
        """
        result = select({
            "musicbrainz@track": ["heavy metal", "metal", "rock"],
            "discogs@track": ["Rock", "Heavy Metal"],
            "listenbrainz@track": ["heavy metal", "metal", "nu metal", "rock"],
        })
        assert result == ["Heavy Metal"], result

    def test_the_umbrella_is_still_absorbed_when_the_child_is_supported(self) -> None:
        """CONTROL — the guard must not switch the rule off.

        Both providers name both genres, so the narrower genre's support is
        comparable (ratio ~0.75) and Heavy Metal is still redundant.
        """
        result = select({
            "musicbrainz@album": ["Heavy Metal", "Nu Metal"],
            "lastfm@album": ["Heavy Metal", "Nu Metal"],
        })
        assert result == ["Nu Metal"], result

    def test_concrete_metal_children_still_absorb_it(self) -> None:
        """CONTROL — the rule's original request, "Heavy Metal is the Same as
        Metal too". Equal evidence must keep Thrash/Death/Black condensing."""
        for child in ("thrash metal", "death metal", "black metal", "power metal"):
            result = select({
                "musicbrainz@album": ["Heavy Metal", child],
                "lastfm@album": ["Heavy Metal", child],
            })
            assert "Heavy Metal" not in result, (child, result)
            assert child.title() in result, (child, result)


class TestGateRunsBeforeCondensation:
    def test_a_gated_child_cannot_take_its_parent_with_it(self) -> None:
        """⭐ THE OFFSPRING — Want You Bad.

        Pop Punk is MusicBrainz-only, so the gate drops it. Condensation used to
        run FIRST, deleting Punk Rock (MusicBrainz + ListenBrainz) as Pop Punk's
        ancestor; then the gate removed Pop Punk. Neither survived, and the
        track kept only Alternative Rock.
        """
        result = select({
            "musicbrainz@track": [
                "alternative rock", "pop punk", "punk", "punk rock", "rock",
            ],
            "discogs@track": ["Punk", "Rock"],
            "listenbrainz@track": [
                "rock", "punk rock", "punk", "pop rock", "alternative pop",
                "rock", "pop", "punk revival", "alternative", "indie rock",
                "post-grunge", "alternative and punk", "pop", "rock",
                "alternative rock",
            ],
        })
        assert "Pop Punk" not in result, result
        assert "Punk" in result, (
            f"the corroborated punk identity was deleted altogether: {result}"
        )

    def test_a_corroborated_ancestor_is_not_condensed_away_by_a_lone_tag(self) -> None:
        """Minimal shape of the same defect, so a broad pipeline change cannot
        hide it: ``punk rock`` has two voices, ``pop punk`` one.

        ⚠️ ``punk rock`` and ``punk`` are now ONE genre (the spelling is an
        alias), so the survivor is reported as ``Punk``.
        """
        result = select({
            "musicbrainz@track": ["pop punk", "punk rock", "alternative rock"],
            "listenbrainz@track": ["punk rock", "alternative rock"],
        })
        assert "Pop Punk" not in result, result
        assert "Punk" in result, result


class TestGateRescueSurvivesTheFloors:
    def test_a_lone_fallback_is_not_blanked_by_the_confidence_floor(self) -> None:
        """⭐ PAUL KELLY — The Oldest Story in the Book.

        MusicBrainz says Pop, Discogs says Rock + Country Rock. The providers
        disagree, so every candidate is single-source. The gate rescues the most
        specific one (Country Rock, confidence 0.57), and the trim used to reject
        it against the 0.60 floor — leaving NO genre at all.
        """
        result = select({
            "musicbrainz@track": ["pop"],
            "discogs@track": ["Rock", "Country Rock"],
        })
        assert result == ["Country Rock"], result

    def test_the_rescue_is_flagged_in_provenance(self) -> None:
        result = gs.select_album_or_track_genres(
            {
                "musicbrainz@track": ["pop"],
                "discogs@track": ["Rock", "Country Rock"],
            },
            settings=SETTINGS,
        )
        rescued = [c for c in result["candidates"] if c["rescued"]]
        assert [c["genre"] for c in rescued] == ["Country Rock"], result["candidates"]

    def test_one_provider_alone_is_never_gated(self) -> None:
        """CONTROL — corroboration needs a second voice to exist."""
        result = select({"discogs@track": ["Country Rock"]})
        assert result == ["Country Rock"], result
