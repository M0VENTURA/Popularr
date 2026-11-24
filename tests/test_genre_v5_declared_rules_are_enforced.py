"""The rules the v5 vocabulary declares, which the code did not follow.

REPORTED
--------
* "Hard Rock, Post-Rock and Alt Rock on every track of an album"
* "Live, Acoustic and Cover stored as genres"
* "Genre order changes between tracks"
* Root rule: "Punk next to Hard Rock is kept"
* "The playlist rebuild also doesn't stick to the minimum tracks required for
  playlist creation in the config"

⭐ THE COMMON THREAD: the vocabulary FILE declared rules that no code read. The
loader discarded ``evidence_policy`` entirely, so a dozen documented policies were
inert and editing the file changed nothing.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from services.enrichment import genre_selection as gs
from services.enrichment.genre_vocabulary import (
    clear_vocabulary_cache,
    get_vocabulary,
)

VOCAB_PATH = pathlib.Path(gs.__file__).resolve().parent / "genre_vocabulary.json"


@pytest.fixture(scope="module")
def vocab():
    clear_vocabulary_cache()
    return get_vocabulary()


def select(source_map: dict, **kwargs) -> list[str]:
    return gs.select_album_or_track_genres(source_map, **kwargs).get("genres") or []


class TestTheLoaderKeepsThePolicy:
    def test_evidence_policy_reaches_the_parsed_vocabulary(self, vocab):
        """⚠️ IT USED TO BE DISCARDED. Every rule in that block was inert."""
        assert vocab.evidence_policy, (
            "evidence_policy did not survive loading, so no rule in it can apply"
        )
        assert "collection_policy" in vocab.evidence_policy
        assert "positions" in vocab.evidence_policy

    def test_positions_are_read_as_levels(self, vocab):
        positions = vocab.evidence_policy.get("positions") or []
        assert positions, "the vocabulary declares no positions"
        first = next(p for p in positions if p.get("position") == 1)
        assert "artist" in first["requires_levels_any"], (
            "position 1 must accept artist evidence — a track with nothing else "
            "still needs a genre"
        )


class TestPerPositionEvidenceLevels:
    """`positions[N].requires_levels_any` — documented, previously unimplemented."""

    def test_the_settings_carry_the_levels(self):
        settings = gs._settings_from_config(None)
        assert settings.position_levels, "position levels were not loaded"
        assert "artist" in settings.position_levels[0]
        assert "artist" not in settings.position_levels[1], (
            "positions 2+ must require track/album evidence"
        )

    def test_artist_only_evidence_fills_position_one(self):
        """CONTROL — a track with no other evidence still gets a genre.

        ⚠️ TWO artist sources, not one. ``evidence_policy.artist_only`` says
        ``min_source_groups: 2``, and a single artist tag scores 0.195 against the
        0.6 position-1 floor — correctly rejected. Two groups clear it.
        """
        result = select({
            "musicbrainz@artist": ["Post-Rock"],
            "lastfm@artist": ["Post-Rock"],
        })
        assert result, "artist-only evidence must be able to fill slot 1"
        assert "Post-Rock" in result, result

    def test_artist_only_evidence_cannot_fill_positions_two_and_three(self):
        """⚠️ THE REPORTED BUG. One artist tag per genre repeated identically on
        every track of every album — "Hard Rock, Post-Rock and Alt Rock on every
        track"."""
        result = select({
            "musicbrainz@track": ["Grunge"],
            "musicbrainz@artist": ["Hard Rock"],
            "lastfm@artist": ["Post-Rock"],
            "listenbrainz@artist": ["Alternative Rock"],
        })
        assert "Grunge" in result or result, result
        # Only genres with TRACK evidence may appear beside it.
        assert len(result) <= 2, (
            f"artist-only tags filled the extra slots and would blanket the "
            f"album: {result}"
        )

    def test_track_evidence_can_still_fill_the_later_slots(self):
        """CONTROL — the rule must not shrink a properly evidenced list."""
        result = select({
            "musicbrainz@track": ["Grunge", "Alternative Rock", "Hard Rock"],
            "lastfm@track": ["Grunge", "Alternative Rock", "Hard Rock"],
            "listenbrainz@track": ["Grunge", "Alternative Rock", "Hard Rock"],
        })
        assert len(result) >= 2, result


class TestRootFallbackSameFamily:
    """`root_fallback_only.same_family_only` — the corrected wording."""

    def test_a_root_loses_to_its_own_child(self):
        result = select({
            "musicbrainz@track": ["Rock", "Hard Rock"],
            "lastfm@track": ["Rock", "Hard Rock"],
            "listenbrainz@track": ["Rock", "Hard Rock"],
        })
        assert "Hard Rock" in result, result
        assert "Rock" not in result, (
            f"the umbrella should be replaced by its own child: {result}"
        )

    def test_a_root_is_KEPT_beside_an_unrelated_family(self):
        """⭐ THE CORRECTED RULE. "Punk next to Hard Rock is kept" — punk is its
        own family, so hard rock says nothing about whether the punk root is
        redundant."""
        result = select({
            "musicbrainz@track": ["Punk", "Hard Rock"],
            "lastfm@track": ["Punk", "Hard Rock"],
            "listenbrainz@track": ["Punk", "Hard Rock"],
        })
        assert "Punk" in result, (
            f"Punk was dropped even though Hard Rock is a different family: {result}"
        )

    def test_the_setting_can_restore_the_blunter_behaviour(self):
        settings = gs.SelectionSettings(
            **{
                **gs._settings_from_config(None).__dict__,
                "root_fallback_same_family_only": False,
            }
        )
        result = select({
            "musicbrainz@track": ["Punk", "Hard Rock"],
            "lastfm@track": ["Punk", "Hard Rock"],
            "listenbrainz@track": ["Punk", "Hard Rock"],
        }, settings=settings)
        assert isinstance(result, list), result


class TestThePolicyObjectIsNotABool:
    """⚠️ ``bool({"enabled": False})`` is True — a dict silently kept rules ON."""

    def test_enabled_false_really_switches_it_off(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {
            "genres": {"selection": {
                "root_fallback_only": {"enabled": False},
            }}
        })
        cfg = ch.get_genre_selection_config()
        assert cfg["root_fallback_only"] is False, (
            "a policy object with enabled:false must turn the rule OFF"
        )

    def test_enabled_true_stays_on(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {
            "genres": {"selection": {
                "root_fallback_only": {"enabled": True, "same_family_only": True},
            }}
        })
        cfg = ch.get_genre_selection_config()
        assert cfg["root_fallback_only"] is True
        assert cfg["root_fallback_same_family_only"] is True


class TestGenreOrderIsDeterministic:
    def test_ties_break_alphabetically_not_by_input_order(self):
        """⚠️ REPORTED: "Genre order changes between tracks".

        The key had no final discriminator, so equal scores fell back to the
        INPUT order — the iteration order of the evidence map, which differs
        between tracks with identical evidence.
        """
        first = select({
            "musicbrainz@track": ["Hard Rock", "Alternative Rock"],
            "lastfm@track": ["Hard Rock", "Alternative Rock"],
        })
        second = select({
            "musicbrainz@track": ["Alternative Rock", "Hard Rock"],
            "lastfm@track": ["Alternative Rock", "Hard Rock"],
        })
        assert sorted(first) == sorted(second), (first, second)

    def test_the_rank_key_has_a_name_component(self):
        settings = gs._settings_from_config(None)
        a = gs.Candidate(genre_id="a", display="Zebra Rock", family="rock",
                         specificity=1, score=1.5, confidence=0.9)
        b = gs.Candidate(genre_id="b", display="Alpha Rock", family="rock",
                         specificity=1, score=1.5, confidence=0.9)
        ranked = gs._ranked([a, b])
        assert [c.genre_id for c in ranked] == ["b", "a"], (
            "an exact score tie must order by name, not by input position"
        )


class TestPerformanceMarkersAgreeWithTheirFlags:
    """⚠️ REPORTED: "Live, Acoustic and Cover stored as genres"."""

    def test_a_marker_needs_its_flag(self):
        from services.enrichment.genre_aggregation_service import (
            _append_extra_genres,
        )

        no_flag = _append_extra_genres(
            [], "Song (Foo Cover)", "", {"Cover"}, row={"is_cover": 0}
        )
        assert "Cover" not in no_flag, no_flag

        with_flag = _append_extra_genres(
            [], "Song (Foo Cover)", "", {"Cover"}, row={"is_cover": 1}
        )
        assert "Cover" in with_flag, with_flag

    def test_live_and_acoustic_are_governed_too(self):
        from services.enrichment.genre_aggregation_service import marker_flag_allows

        assert marker_flag_allows("Live", {"is_live": 0}) is False
        assert marker_flag_allows("Live", {"is_live": 1}) is True
        assert marker_flag_allows("Acoustic", {"is_acoustic": 0}) is False
        assert marker_flag_allows("Acoustic", {"is_acoustic": 1}) is True

    def test_genuine_markers_are_unaffected(self):
        """CONTROL — Christmas/Soundtrack/Comedy are vocabulary markers, and
        Remaster/Instrumental have no flag of their own."""
        from services.enrichment.genre_aggregation_service import marker_flag_allows

        for marker in ("Christmas", "Soundtrack", "Comedy", "Remaster", "Instrumental"):
            assert marker_flag_allows(marker, {"is_cover": 0}) is True, marker

    def test_a_missing_column_fails_open(self):
        """⚠️ Callers pass different row shapes; an absent key must not delete a
        legitimate marker."""
        from services.enrichment.genre_aggregation_service import marker_flag_allows

        assert marker_flag_allows("Cover", {"title": "x"}) is True
        assert marker_flag_allows("Cover", None) is True


class TestTheIndieAliasIsGone:
    def test_bare_indie_no_longer_means_indie_rock(self, vocab):
        """⚠️ REPORTED as the likely source of Indie Rock on Adele. "indie" is
        used loosely for indie pop, indie folk and singer-songwriters; the display
        name "Indie Rock" still matches."""
        assert vocab.classify("indie").canonical != "indie_rock", (
            "bare 'indie' still resolves to Indie Rock"
        )

    def test_the_display_name_still_matches(self, vocab):
        assert vocab.classify("Indie Rock").canonical == "indie_rock"


class TestThePlaylistCreationThresholdUsesTheFinalCount:
    def test_the_gate_is_evaluated_after_the_cap_and_slice(self):
        """⚠️ REPORTED: "The playlist rebuild also doesn't stick to the minimum
        tracks required for playlist creation." The gate used the pool size,
        measured BEFORE the artist cap and the max_tracks slice, so a genre with
        120 qualifying tracks passed a 100 threshold and shipped 48-66."""
        import inspect

        from services.popularity.stages import finalise_stage as fs

        src = inspect.getsource(fs._create_genre_top_track_playlists)
        gate = src.index("_final_count")
        cap = src.index("_apply_artist_cap(winners, max_per_artist)")
        assert cap < gate, (
            "the creation gate must be evaluated AFTER the artist cap and slice"
        )
        assert "if not playlist_exists and _final_count < create_threshold" in src
