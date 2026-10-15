"""Tests for the genre consensus aggregation model.

Covers the three genre fixes:
1. Junk-tag filter — "2014"/"beautiful" (lone Last.fm moods/years) can
   never surface in the ``genres`` column or a ``{Genre} - Top Tracks``
   playlist name.
2. Split-vote stacking — "nu-metal"/"nu metal"/"NuMetal" merge onto one
   vote key so weights accumulate instead of being split.
3. Consensus threshold (``genres.min_weight``) — a lone low-weight tag is
   discarded unless a second source backs it.

The ``genres.min_weight`` default (0.30) and ``genres.junk_filter`` default
(True) are monkeypatched where a test needs a specific value.

⚠️ TWO ENGINES, TWO CONTRACTS (2026-10-15)
-----------------------------------------
``aggregate_genres`` now runs the canonical selection engine by default. The
``min_weight`` CONSENSUS GATE is a legacy-only mechanism — the new engine
replaces it with classification plus per-position confidence floors — so the
tests that pin that gate force the legacy engine explicitly (``legacy_engine``).
That is deliberate rather than convenient: the config switch is the documented
way back, so it needs its own coverage, and these tests are the only ones that
exercise it.

Tests that are engine-AGNOSTIC (junk never surfaces, admin tags never surface)
run under whichever engine is active, and compare case-insensitively: the legacy
engine echoed the source's own spelling (``nu metal``) while the canonical engine
writes the vocabulary's display name (``Nu Metal``). Every consumer of these
values lowercases before comparing — ``iter_genre_tokens``, ``is_metal_genre``,
``normalize_genre``, ``clean_conflicting_genres`` — so the case is cosmetic and
pinning it would be pinning an implementation detail.
"""

from __future__ import annotations

import pytest

from services.enrichment import genre_aggregation_service as gas


def _lower(values):
    """Case-insensitive view of a genre list."""
    return [str(v).casefold() for v in values]


@pytest.fixture(autouse=True)
def _clean_module_state():
    yield


@pytest.fixture
def legacy_engine(monkeypatch):
    """Force the pre-2026-10-15 vote-counting engine.

    The ``min_weight`` gate these tests describe no longer runs by default, and
the switch that reaches it is user-facing config — so it must stay covered.
    """
    monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: False)
    return True


@pytest.fixture
def cfg(monkeypatch):
    """Set the genre config (min_weight / junk_filter / weights)."""
    from helpers import config_helpers

    _state = {}

    def _set(**kwargs):
        _state.update(kwargs)

    def _apply(**kwargs):
        _state.update(kwargs)

        cfg = {"genres": {}}
        if "min_weight" in _state:
            cfg["genres"]["min_weight"] = _state["min_weight"]
        if "junk_filter" in _state:
            cfg["genres"]["junk_filter"] = _state["junk_filter"]
        if "weights" in _state:
            cfg["genres"]["weights"] = _state["weights"]
        monkeypatch.setattr(config_helpers, "get_config", lambda: cfg)

    _apply()
    return _set


# ---------------------------------------------------------------------------
# Junk-tag filter
# ---------------------------------------------------------------------------

class TestJunkGenreFilter:
    def test_years_are_junk(self):
        assert gas.is_junk_genre("2014")
        assert gas.is_junk_genre("2015")
        assert gas.is_junk_genre("1980s")

    def test_moods_are_junk(self):
        assert gas.is_junk_genre("beautiful")
        assert gas.is_junk_genre("romantic")
        assert gas.is_junk_genre("seen live")
        assert gas.is_junk_genre("my playlist")

    def test_real_genres_survive(self):
        assert not gas.is_junk_genre("nu metal")
        assert not gas.is_junk_genre("alternative rock")
        assert not gas.is_junk_genre("post-hardcore")
        assert not gas.is_junk_genre("k-pop")

    def test_junk_filter_config_can_disable(self, cfg):
        cfg(junk_filter=False)
        assert not gas.is_junk_genre("beautiful")

    def test_junk_filter_on_by_default(self, cfg):
        assert gas.is_junk_genre("2014")


# ---------------------------------------------------------------------------
# Administrative-tag filter (cover/live/remix/demo/soundtrack)
# ---------------------------------------------------------------------------

class TestAdminGenreFilter:
    def test_admin_labels_are_filtered(self):
        assert gas.is_admin_genre("cover")
        assert gas.is_admin_genre("live")
        assert gas.is_admin_genre("remix")
        assert gas.is_admin_genre("demo")
        assert gas.is_admin_genre("soundtrack")
        assert gas.is_admin_genre("tribute")
        assert gas.is_admin_genre("karaoke")

    def test_real_genres_not_admin(self):
        assert not gas.is_admin_genre("nu metal")
        assert not gas.is_admin_genre("alternative rock")
        assert not gas.is_admin_genre("post-hardcore")
        assert not gas.is_admin_genre("metalcore")

    def test_parenthetical_admin_literals_stripped(self):
        assert gas.is_admin_genre("(cover)")
        assert gas.is_admin_genre("Nu Metal (album fallback)")
        assert gas.is_admin_genre("(remix)")
        assert gas.is_admin_genre("metal (live)")

    def test_admin_tags_never_reach_vote(self, cfg):
        """⚠️ THIS TEST WAS PASSING VACUOUSLY until 2026-10-15.

        It asserted ``"cover" not in result`` against a result containing
        ``"Cover"`` — the filter tags are added TITLE-CASED
        (``matched_filter.title()``), so a lowercase membership test could never
        match and the assertion was decoration. Comparing case-insensitively
        (below) exposed it.

        What is actually true, and is BY DESIGN: filter tags are never VOTED —
        they take no part in ranking and consume no genre slot — but they ARE
        appended to the end of the list as playlist/filter markers. An earlier
        change deliberately preserved them because live/acoustic/christmas
        playlists depend on the tag being present.
        """
        cfg(min_weight=0.0)
        result = gas.aggregate_genres({
            "lastfm": ["cover", "live", "nu metal", "remix"],
            "musicbrainz": ["nu metal"],
        }, max_genres=1)

        lowered = _lower(result)
        assert "nu metal" in lowered

        # The VOTED portion is capped by max_genres; the markers come after it.
        voted = lowered[:1]
        assert voted == ["nu metal"], f"a filter tag took a genre slot: {result}"
        for marker in ("cover", "live", "remix"):
            if marker in lowered:
                assert marker not in voted, (
                    f"{marker!r} was voted into a genre slot rather than appended"
                )


# ---------------------------------------------------------------------------
# Split-vote stacking
# ---------------------------------------------------------------------------

class TestSplitVoteStacking:
    """Split-vote stacking is a property of the VOTE model, so these force it.

    Under the canonical engine the spellings are resolved by the vocabulary's
    alias table instead, which is a different mechanism with its own tests in
    ``test_genre_selection_engine.py``.
    """

    def test_hyphen_variants_stack(self, cfg, legacy_engine):
        cfg(min_weight=0.0)
        # "nu-metal" (Last.fm 0.10) + "nu metal" (Essentia 0.20) +
        # "NuMetal" (Discogs 0.25) — all merge onto one key, 0.55 total.
        result = gas.aggregate_genres({
            "lastfm": ["nu-metal"],
            "essentia": ["nu metal"],
            "discogs": ["NuMetal"],
        }, max_genres=3)
        assert _lower(result) == ["nu metal"]  # Discogs spelling wins (0.25 > others)

    def test_pop_punk_variants_stack(self, cfg, legacy_engine):
        cfg(min_weight=0.0)
        result = gas.aggregate_genres({
            "lastfm": ["pop-punk"],
            "musicbrainz": ["Pop Punk"],
        }, max_genres=3)
        assert _lower(result) == ["pop punk"]  # MusicBrainz spelling wins (0.40)

    def test_split_vote_clears_threshold_together(self, cfg, legacy_engine):
        cfg(min_weight=0.25)
        # Individually: lastfm 0.10 + essentia 0.20 = 0.30 → passes.
        result = gas.aggregate_genres({
            "lastfm": ["nu-metal"],
            "essentia": ["nu metal"],
        }, max_genres=3)
        assert _lower(result) == ["nu metal"]

    def test_split_vote_without_second_source_fails_threshold(self, cfg, legacy_engine):
        cfg(min_weight=0.25)
        # Lone Last.fm tag = 0.10 → below 0.25 → discarded.
        result = gas.aggregate_genres({
            "lastfm": ["nu-metal"],
        }, max_genres=3)
        assert result == []


# ---------------------------------------------------------------------------
# Consensus threshold
# ---------------------------------------------------------------------------

class TestConsensusThreshold:
    """The ``min_weight`` gate is legacy-only; the canonical engine replaces it
    with classification plus per-position confidence floors (see
    ``test_genre_selection_engine.py::TestTheThresholds``)."""

    def test_lone_lastfm_tag_filtered(self, cfg, legacy_engine):
        cfg(min_weight=0.25)
        # "k-pop" tagged by a single Last.fm user = 0.10 → filtered.
        result = gas.aggregate_genres({"lastfm": ["k-pop"]}, max_genres=3)
        assert result == []

    def test_multi_source_confirmation_passes(self, cfg, legacy_engine):
        cfg(min_weight=0.25)
        result = gas.aggregate_genres({
            "lastfm": ["k-pop"],
            "musicbrainz": ["k-pop"],
        }, max_genres=3)
        assert _lower(result) == ["k-pop"]

    def test_discogs_alone_passes(self, cfg, legacy_engine):
        cfg(min_weight=0.25)
        # Discogs alone (0.25) equals the default threshold → passes.
        result = gas.aggregate_genres({"discogs": ["alternative metal"]}, max_genres=3)
        assert _lower(result) == ["alternative metal"]

    def test_zero_disables_gate(self, cfg, legacy_engine):
        cfg(min_weight=0.0)
        result = gas.aggregate_genres({"lastfm": ["weird-lone-tag"]}, max_genres=3)
        # Hyphenated spelling is kept as the readable display form.
        assert _lower(result) == ["weird-lone-tag"]

    def test_junk_blocked_before_vote(self, cfg):
        cfg(min_weight=0.0)
        # Even with the threshold disabled, junk never surfaces.
        result = gas.aggregate_genres({"lastfm": ["2014", "beautiful"]}, max_genres=3)
        assert result == []

    def test_a_lone_track_tag_is_now_ACCEPTED_where_the_gate_rejected_it(self, cfg):
        """⚠️ A DELIBERATE behaviour change, documented rather than asserted away.

        The legacy gate rejected a lone Last.fm tag because the legacy weights
        made Last.fm nearly worthless (0.10 against a 0.25 floor). The canonical
        engine gives Last.fm 0.70 — the recommended provider trust for broad
        coverage — so a TRACK's own Last.fm tag is real evidence of that track's
        genre and clears the first-position floor.

        The safety property that mattered is kept by a different route: an
        ARTIST-level lone tag still cannot define a track (see
        ``test_a_lone_ARTIST_tag_still_cannot_define_a_track``).
        """
        cfg(min_weight=0.25)
        result = gas.aggregate_genres({"lastfm": ["k-pop"]}, max_genres=3)
        assert _lower(result) == ["k-pop"], (
            "a track's own Last.fm genre should now be accepted"
        )

    def test_a_lone_ARTIST_tag_still_cannot_define_a_track(self, cfg):
        """CONTROL for the change above: the guard that matters still holds.

        Artist-level evidence is support, not a verdict — a lone artist tag is
        penalised and weighted at 0.35, so it cannot clear the first floor.
        """
        cfg(min_weight=0.25)
        result = gas.aggregate_genres({"lastfm@artist": ["k-pop"]}, max_genres=3)
        assert result == [], result


# ---------------------------------------------------------------------------
# Junk tag can never reach a playlist name
# ---------------------------------------------------------------------------

class TestNoJunkPlaylistNames:
    def test_years_never_become_playlist_genres(self, cfg):
        cfg(min_weight=0.0)
        result = gas.aggregate_genres({
            "lastfm": ["2014", "2015", "alternative metal"],
        }, max_genres=5)
        lowered = _lower(result)
        assert "2014" not in lowered
        assert "2015" not in lowered
        assert "alternative metal" in lowered
