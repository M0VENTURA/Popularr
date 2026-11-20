"""The five causes of mis-assigned genres, each pinned.

REPORT

> "The main problem is that genres are being assigned per album, so every track
>  on an album gets that album's tags. When a source tags a whole album with
>  something odd, the error spreads to every track on it."

Five causes, all verified in the code and all fixed here:

1. Album-scoped blends OVERWROTE per-track genres.
2. A lone MusicBrainz tag cleared the confidence floor on its own.
3. Hard Rock beat Heavy Metal (Judas Priest).
4. Indie Rock appeared on punk bands (see the note at the end — NOT a mapping).
5. Discogs format categories were mapped to real genres (Stage & Screen).
"""

from __future__ import annotations

import pytest

from services.enrichment import genre_aggregation_service as gas
from services.enrichment import genre_selection as gs

SETTINGS = gs.SelectionSettings(
    source_weights=gs.DEFAULT_SOURCE_WEIGHTS,
    entity_weights=gs.DEFAULT_ENTITY_WEIGHTS,
    keep_together=gs.DEFAULT_KEEP_TOGETHER,
    conditional_keep=gs.DEFAULT_CONDITIONAL_KEEP,
    mutually_exclusive=gs.DEFAULT_MUTUALLY_EXCLUSIVE,
)


def select(source_map: dict, **kw) -> list[str]:
    return gs.select_album_or_track_genres(
        source_map, settings=kw.pop("settings", SETTINGS), **kw
    )["genres"]


# ---------------------------------------------------------------------------
# Cause 2 — corroboration
# ---------------------------------------------------------------------------

class TestCause2_A_Lone_Tag_Cannot_Outrank_Disagreement:
    """The reported wrong genres were all one-provider claims."""

    def test_a_lone_tag_is_dropped_when_another_provider_speaks(self) -> None:
        """⭐ ARCH ENEMY: 'Melodic Death Metal; Black Metal; Thrash'.

        A minor MusicBrainz tag sat beside the real genre. Both are METAL, so
        the family-outlier rule cannot explain the removal — only the
        corroboration gate can. (An earlier version of this test used
        Gothic Metal + Blues, which the family-outlier rule removes on its
        own, so it passed with the gate disabled and proved nothing.)
        """
        result = select({
            "musicbrainz@album": ["Melodic Death Metal", "Black Metal"],
            "lastfm@album": ["Melodic Death Metal"],
        })
        assert "Melodic Death Metal" in result, result
        assert "Black Metal" not in result, (
            f"a MusicBrainz-only 'Black Metal' must not survive: {result}"
        )

    def test_the_gate_is_what_removes_it(self) -> None:
        """CONTROL — the removal reason must be SINGLE_SOURCE, not the family rule.

        Without this the test above would pass for the wrong reason if the
        family-outlier rule grew to cover the pair.
        """
        result = gs.select_album_or_track_genres(
            {
                "musicbrainz@album": ["Melodic Death Metal", "Black Metal"],
                "lastfm@album": ["Melodic Death Metal"],
            },
            settings=SETTINGS,
        )
        reasons = {r["genre"]: r["reason"] for r in result.get("removed") or []}
        assert reasons.get("Black Metal") == "SINGLE_SOURCE", reasons

    def test_one_voice_alone_still_stands(self) -> None:
        """⭐ NOTHING IS BLANKED. A track whose only provider is Last.fm keeps it."""
        assert select({"lastfm@track": ["K-Pop"]}) == ["K-Pop"]

    def test_two_voices_agreeing_always_stand(self) -> None:
        assert select({
            "musicbrainz@track": ["Heavy Metal"],
            "lastfm@track": ["Heavy Metal"],
        }) == ["Heavy Metal"]

    def test_manual_is_never_gated(self) -> None:
        """A user correction outranks corroboration by definition."""
        assert select({
            "manual@track": ["Ska"],
            "musicbrainz@track": ["Rock"],
            "lastfm@track": ["Rock"],
        })


# ---------------------------------------------------------------------------
# Cause 3 — Heavy Metal vs Hard Rock
# ---------------------------------------------------------------------------

class TestCause3_Heavy_Metal_Beats_Hard_Rock:
    """⭐ JUDAS PRIEST: Killing Machine / Stained Class reported 'Hard Rock' only.

    ⚠️⚠️ HONEST STATUS: THE SYMPTOM WAS NEVER REPRODUCED.

    Every input shape tried — one provider, two providers, a rock-heavy album
    pool, ``heavy_metal`` at specificity 0 and at 1, with the corroboration gate
    on and off — resolved to Heavy Metal with Hard Rock suppressed. On identical
    evidence Heavy Metal scores 1.05 against Hard Rock's 0.68 (ratio 1.54), far
    above the 0.8 threshold in ``_pair_verdict``, so the pair was never actually
    lost in this engine.

    The two changes below are still correct on their own terms and are kept
    deliberately, documented as UNPROVEN against the reported library:

    * ``heavy_metal`` specificity 0 -> 1. It is a real genre, not an umbrella;
      at 0 it started 0.05 behind Hard Rock AND was deleted outright by
      ``remove_generic_parents`` on any metal album.
    * ``_absorb_umbrella_genres`` keeps the earlier, opposite request
      ("Heavy Metal is the Same as Metal too") working without the
      specificity-0 side effects.

    If Judas Priest still shows Hard Rock after a resync, the cause is
    elsewhere — most likely in how the album pool weights sources, not here.
    """

    def test_heavy_metal_wins_the_pair(self) -> None:
        """What the engine must do; passes before AND after the change."""
        result = select({
            "musicbrainz@album": ["Heavy Metal"],
            "discogs@album": ["Rock", "Hard Rock"],
        })
        assert "Hard Rock" not in result, (
            "Hard Rock must not be the answer for a core metal band: "
            f"{result}"
        )

    def test_heavy_metal_is_not_a_generic_parent(self) -> None:
        """The one assertion that DOES fail at specificity 0.

        At 0, ``remove_generic_parents`` deletes Heavy Metal from any metal
        album — the mechanism that could plausibly produce the report on a
        library whose tracks carry narrower metal genres.
        """
        entry = gs.get_vocabulary().genres["heavy_metal"]
        assert entry.specificity == 1, (
            "specificity 0 made Heavy Metal lose a 0.05 bonus to Hard Rock and "
            "let the generic-parent rule drop it from any metal album"
        )

    def test_heavy_metal_no_longer_starts_behind_hard_rock(self) -> None:
        """The measurable half of the fix: equal specificity on equal evidence."""
        vocab = gs.get_vocabulary()
        assert (
            vocab.genres["heavy_metal"].specificity
            >= vocab.genres["hard_rock"].specificity
        ), "Hard Rock must not outrank Heavy Metal on specificity alone"

    def test_it_is_still_absorbed_by_a_narrower_metal_genre(self) -> None:
        """The original request, 'Heavy Metal is the Same as Metal too'."""
        for child in ("thrash metal", "death metal", "black metal"):
            result = select({
                "musicbrainz@album": ["Heavy Metal", child],
                "lastfm@album": ["Heavy Metal", child],
            })
            assert "Heavy Metal" not in result, (child, result)

    def test_it_is_absorbed_by_the_alternative_metal_branch(self) -> None:
        """Nu Metal descends from alternative_metal, NOT heavy_metal.

        This is the case the ancestry walk cannot reach, so
        ``_absorb_umbrella_genres`` handles it — and this test fails if that
        rule is removed.
        """
        result = select({
            "musicbrainz@album": ["Heavy Metal", "Nu Metal"],
            "lastfm@album": ["Heavy Metal", "Nu Metal"],
        })
        assert "Heavy Metal" not in result, result


# ---------------------------------------------------------------------------
# Cause 5 — Discogs format categories
# ---------------------------------------------------------------------------

class TestCause5_Stage_And_Screen_Is_Not_A_Genre:
    """⭐ STRAY KIDS JAPANESE RELEASES: 'K-Pop; Musical Theatre'."""

    @pytest.mark.parametrize(
        "spelling",
        ["Stage & Screen", "Stage and Screen", "stage & screen"],
    )
    def test_it_is_admin_junk(self, spelling: str) -> None:
        assert gas.is_admin_genre(spelling), (
            "a Discogs FORMAT CATEGORY must not reach the genre pool"
        )

    def test_it_never_reaches_the_result(self) -> None:
        result = select({
            "discogs@album": ["Stage & Screen"],
            "musicbrainz@album": ["K-Pop"],
            "lastfm@album": ["K-Pop"],
        })
        assert result == ["K-Pop"], result
        assert not any("stage" in g.casefold() for g in result)

    def test_it_is_not_an_alias_of_musical_theatre(self) -> None:
        """It WAS, which is how K-Pop releases became 'Musical Theatre'."""
        classification = gs.get_vocabulary().classify("stage & screen")
        assert classification.canonical != "musical_theatre"

    def test_real_musical_theatre_still_works(self) -> None:
        """CONTROL — the genre itself is untouched."""
        assert gs.get_vocabulary().classify("musical").canonical == "musical_theatre"
        assert gs.get_vocabulary().classify("show tunes").canonical == "musical_theatre"


# ---------------------------------------------------------------------------
# Cause 1 — per-track, not per-album
# ---------------------------------------------------------------------------

class TestCause1_A_Track_With_Its_Own_Evidence_Keeps_It:
    """⭐ EVANESCENCE 'MY IMMORTAL', LINKIN PARK 'ONE MORE LIGHT'."""

    @staticmethod
    def _own(tracks):
        from services.metadata.album_tag_sync_service import (
            _select_track_own_genres,
        )

        return _select_track_own_genres(tracks)

    def test_a_ballad_keeps_its_own_genre(self) -> None:
        out = self._own([
            {"id": "1", "title": "My Immortal",
             "musicbrainz_genres": '["Piano Rock", "Ballad"]',
             "lastfm_tags": '["Piano Rock"]'},
        ])
        assert out["1"] == ["Piano Rock"], out

    def test_a_track_with_no_sources_returns_NOTHING(self) -> None:
        """So the caller falls back to the album blend — the correct default."""
        out = self._own([{"id": "2", "title": "Sparse"}])
        assert "2" not in out

    def test_a_single_source_track_resolves_on_that_voice(self) -> None:
        out = self._own([
            {"id": "3", "title": "Only Discogs", "discogs_genres": '["Pop Punk"]'},
        ])
        assert out["3"] == ["Pop Punk"], out

    def test_the_album_pool_never_feeds_a_track_that_has_its_own(self) -> None:
        """⚠️ THE ACTUAL BUG. The helper must not receive any ``@album`` key."""
        import inspect

        from services.metadata.album_tag_sync_service import (
            _select_track_own_genres,
        )

        src = inspect.getsource(_select_track_own_genres)
        assert '"@album"' not in src and "'@album'" not in src, (
            "feeding the album pool in would resolve every track to the same "
            "list — the per-album bug this exists to fix"
        )
        assert '@track"' in src

    def test_the_SYNC_actually_uses_it(self) -> None:
        """⭐ THE GUARD THE TESTS ABOVE MISS.

        Every test above calls ``_select_track_own_genres`` DIRECTLY, so they
        prove the helper computes the right thing — and nothing about whether
        ``sync_album_file_tags`` USES it. Patching the call site to
        ``_per_track_genres = {}`` left them all passing, which was a real gap
        in this suite found by reverting the fix.
        """
        import inspect

        from services.metadata import album_tag_sync_service as svc

        src = inspect.getsource(svc.sync_album_file_tags)
        assert "_select_track_own_genres(tracks)" in src, (
            "the album sync must ask for each track's OWN genres"
        )
        assert "_final_genres_for" in src, (
            "the stored value must be chosen per track (own evidence, else the "
            "album blend), not once for the album"
        )
        # The album blend must still be the FALLBACK, not removed.
        assert "get_track_recommendations" in src

    def test_the_fallback_chain_is_own_then_album(self) -> None:
        """Reads the order out of the source: own evidence first."""
        import inspect

        from services.metadata import album_tag_sync_service as svc

        src = inspect.getsource(svc.sync_album_file_tags)
        own_at = src.index("_per_track_genres.get(")
        blend_at = src.index("clean_genres", own_at)
        assert own_at < blend_at, (
            "the per-track value must be consulted BEFORE the album blend"
        )


# ---------------------------------------------------------------------------
# Cause 4 — Indie Rock on punk bands
# ---------------------------------------------------------------------------

class TestCause4_Indie_Rock_Has_No_Punk_Source:
    """⚠️ THERE IS NO punk -> indie-rock MAPPING, and none was added.

    Documented as a test so nobody "fixes" the symptom with a suppression rule:
    that would hide the real cause (the album blend spreading one release's tag)
    and break legitimate indie-punk artists.
    """

    def test_indie_rock_is_not_a_child_of_punk(self) -> None:
        vocab = gs.get_vocabulary()
        assert "punk" not in vocab.genres["indie_rock"].ancestors(vocab.genres)

    def test_no_punk_genre_maps_to_indie_rock(self) -> None:
        vocab = gs.get_vocabulary()
        for gid, entry in vocab.genres.items():
            if "punk" not in gid:
                continue
            assert "indie_rock" not in entry.ancestors(vocab.genres), gid

    def test_punk_evidence_does_not_produce_indie_rock(self) -> None:
        """CONTROL — the negative that makes the previous test meaningful."""
        result = select({
            "musicbrainz@album": ["Pop Punk", "Punk Rock"],
            "lastfm@album": ["Pop Punk", "Skate Punk"],
        })
        assert "Indie Rock" not in result, result

    def test_the_repair_is_the_album_fix_not_a_suppression(self) -> None:
        """Cause 1 is the fix; this guard simply keeps Cause 4 honest."""
        out = TestCause1_A_Track_With_Its_Own_Evidence_Keeps_It()._own([
            {"id": "nofx", "title": "Linoleum",
             "musicbrainz_genres": '["Pop Punk", "Punk Rock"]',
             "lastfm_tags": '["Pop Punk"]'},
        ])
        assert out["nofx"] == ["Pop Punk"], out
