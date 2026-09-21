"""Compilation tracks must be rated against the artist's ONLINE catalogue.

Reported: "The various artists popularity isn't working correctly as it's
matching based on the popularity in the database. I would prefer it if checked
based on popularity on the artists catalogue online."

``compute_track_artist_scores`` can only rank a compilation track against the
songs this LIBRARY owns. Under 5 usable rows it fell through to
``absolute_thin_catalogue_fallback`` (absolute score/listener thresholds),
which cannot distinguish an artist's genuine global #1 from album filler --
exactly the situation on a soundtrack, where the library owns one or two songs
by each credited artist. The reported casualty was "Natural High" by Insolence
(the band's most popular song worldwide) landing at 1-2 stars.

The fix rates such a track by its RANK within the artist's online catalogue
(Last.fm ``artist.getTopTracks``, ordered by global playcount). Because Last.fm
reports ``listeners`` on both the track and the catalogue entries from the same
dataset, the rank is directly usable.

CRITICAL SAFETY PROPERTY: every test below also pins that a lookup MISS can
never DEMOTE a track. An offline scan, a missing API key or an unknown artist
must all leave the existing behaviour untouched.
"""

from __future__ import annotations

import pytest

from services.popularity.stages import finalise_stage as fs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_rank(monkeypatch, rank_map):
    """Patch the online rank lookup with ``{normalized title: rank_info}``."""
    import services.popularity.popularity_sources as ps

    def _fake(artist, track_title, lastfm_client=None):
        key = str(track_title or "").strip().casefold()
        if key in rank_map:
            return rank_map[key]
        return {
            "rank": 0, "total": 0, "percentile": 0.0,
            "listeners": 0, "top_listeners": 0, "source": "none",
        }

    monkeypatch.setattr(ps, "get_online_artist_track_rank", _fake)


def _rank(rank, total, source="lastfm_artist_top_tracks"):
    return {
        "rank": rank,
        "total": total,
        "percentile": rank / total,
        "listeners": 1000,
        "top_listeners": 50000,
        "source": source,
    }


#: The 5★/4★/3★/2★ cut-offs, matching ``_DEFAULT_COMPILATION_ONLINE_CATALOGUE``.
RULES = {
    "enabled": 1,
    "min_local_catalogue": 5,
    "rank_percentile_5star": 0.02,
    "rank_percentile_4star": 0.10,
    "rank_percentile_3star": 0.35,
    "rank_percentile_2star": 0.65,
    "min_catalogue_size": 5,
}


# ---------------------------------------------------------------------------
# 1. The core fix: the artist's global #1 is recognised as such
# ---------------------------------------------------------------------------


class TestTheOnlineCatalogueRescuesTheThinCatalogueTrap:
    def test_the_artists_number_one_song_is_five_stars(self, monkeypatch):
        """Insolence - "Natural High" is their most-played song worldwide.

        With only one owned song the local catalogue is degenerate, so the old
        code ran the absolute fallback. Rank 1 of 47 -> top 2% -> 5 stars.
        """
        _stub_rank(monkeypatch, {"natural high": _rank(1, 47)})

        stars, detail = fs._online_catalogue_stars(
            credited_artist="Insolence",
            track_title="Natural High",
            rules=RULES,
        )

        assert stars == 5
        assert detail["rank"] == 1
        assert detail["total"] == 47

    def test_a_deep_catalogue_track_is_one_star(self, monkeypatch):
        """Rank 40 of 47 (85th percentile) is not a signature song."""
        _stub_rank(monkeypatch, {"filler": _rank(40, 47)})

        stars, _ = fs._online_catalogue_stars(
            credited_artist="Insolence",
            track_title="Filler",
            rules=RULES,
        )

        assert stars == 1

    @pytest.mark.parametrize(
        "rank,total,expected",
        [
            (1, 50, 5),    # 0.02 exactly -> 5★ (inclusive)
            (2, 50, 4),    # 0.04  -> 4★
            (10, 100, 4),  # 0.10 exactly -> 4★ (inclusive)
            (20, 100, 3),  # 0.20  -> 3★
            (35, 100, 3),  # 0.35 exactly -> 3★ (inclusive)
            (50, 100, 2),  # 0.50  -> 2★
            (65, 100, 2),  # 0.65 exactly -> 2★ (inclusive)
            (66, 100, 1),  # 0.66  -> 1★
        ],
    )
    def test_rank_percentile_bands(self, monkeypatch, rank, total, expected):
        _stub_rank(monkeypatch, {"song": _rank(rank, total)})

        stars, _ = fs._online_catalogue_stars(
            credited_artist="A", track_title="song", rules=RULES
        )

        assert stars == expected


# ---------------------------------------------------------------------------
# 2. Safety: a lookup miss must never demote
# ---------------------------------------------------------------------------


class TestALookupMissNeverDemotes:
    def test_no_catalogue_returns_zero_meaning_unknown(self, monkeypatch):
        """0 stars means "unknown", which the caller treats as "keep existing"."""
        _stub_rank(monkeypatch, {})

        stars, detail = fs._online_catalogue_stars(
            credited_artist="Nobody", track_title="Nothing", rules=RULES
        )

        assert stars == 0
        assert detail["source"] == "none"

    def test_empty_artist_returns_zero(self, monkeypatch):
        _stub_rank(monkeypatch, {})

        stars, _ = fs._online_catalogue_stars(
            credited_artist="", track_title="Song", rules=RULES
        )

        assert stars == 0

    def test_catalogue_too_small_to_rank_returns_zero(self, monkeypatch):
        """Three songs is no better than the local thin-catalogue fallback."""
        _stub_rank(monkeypatch, {"song": _rank(1, 3)})

        stars, _ = fs._online_catalogue_stars(
            credited_artist="A", track_title="song", rules=RULES
        )

        assert stars == 0, "a 3-song catalogue must not produce a confident 5★"

    def test_disabled_returns_zero_without_looking_up(self, monkeypatch):
        """Disabled must not even hit the network."""
        import services.popularity.popularity_sources as ps

        called = []
        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda *a, **k: called.append(1) or _rank(1, 50),
        )

        stars, _ = fs._online_catalogue_stars(
            credited_artist="A", track_title="song", rules={**RULES, "enabled": 0}
        )

        assert stars == 0
        assert called == [], "a disabled setting must not make a request"

    def test_a_network_exception_returns_zero_not_an_error(self, monkeypatch):
        """An offline scan must degrade to the old behaviour, not crash."""
        import services.popularity.popularity_sources as ps

        def _boom(*_a, **_k):
            raise RuntimeError("network down")

        monkeypatch.setattr(ps, "get_online_artist_track_rank", _boom)

        stars, detail = fs._online_catalogue_stars(
            credited_artist="A", track_title="song", rules=RULES
        )

        assert stars == 0
        assert detail["source"] == "none"

    def test_a_title_beyond_the_catalogue_lands_at_the_bottom(self, monkeypatch):
        """A real artist but an uncharted title -> 1★, not 5★."""
        _stub_rank(
            monkeypatch,
            {"deep cut": {
                "rank": 51, "total": 50, "percentile": 1.0,
                "listeners": 0, "top_listeners": 50000,
                "source": "lastfm_artist_top_tracks_beyond",
            }},
        )

        stars, _ = fs._online_catalogue_stars(
            credited_artist="A", track_title="Deep Cut", rules=RULES
        )

        assert stars == 1


# ---------------------------------------------------------------------------
# 3. The online path must only engage when the local catalogue is thin
# ---------------------------------------------------------------------------


class TestTheOnlineLookupOnlyEngagesWhenLocalIsThin:
    """A well-represented artist's ratings must NOT change."""

    def test_healthy_local_catalogue_skips_the_online_lookup(self, monkeypatch):
        """>= 5 usable local scores keeps the existing track_artist_catalogue path."""
        import services.popularity.popularity_sources as ps

        called = []
        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda *a, **k: called.append(1) or _rank(1, 50),
        )

        track = {
            "artist": "Muse",
            "title": "Hysteria",
            "popularity_score": 70.0,
            "lastfm_listeners": 12000,
            "single_confidence": "low",
        }
        artist_scores = [60.0, 65.0, 70.0, 75.0, 80.0]

        fs._assign_stars(
            track, artist_scores, artist_scores,
            is_compilation=True,
        )

        modes = {track.get("_compilation_rating_mode")}
        assert "track_artist_catalogue" in modes, (
            "a healthy local catalogue must keep using the local z-score"
        )
        assert called == [], (
            "the online catalogue must not be consulted when the local "
            f"catalogue is usable (got {len(called)} call(s))"
        )


# ---------------------------------------------------------------------------
# 4. Config plumbing
# ---------------------------------------------------------------------------


class TestCompilationOnlineCatalogueConfig:
    def test_defaults_present_with_no_config(self):
        from helpers.config_helpers import get_compilation_online_catalogue_config

        rules = get_compilation_online_catalogue_config({})

        assert rules["enabled"] == 1
        assert rules["min_local_catalogue"] == 5
        assert rules["rank_percentile_5star"] == 0.02
        assert rules["rank_percentile_4star"] == 0.10
        assert rules["rank_percentile_3star"] == 0.35
        assert rules["rank_percentile_2star"] == 0.65
        assert rules["min_catalogue_size"] == 5

    def test_a_partial_user_block_overrides_only_its_keys(self):
        from helpers.config_helpers import get_compilation_online_catalogue_config

        cfg = {
            "single_detection": {
                "compilation_online_catalogue": {"rank_percentile_5star": 0.05}
            }
        }

        rules = get_compilation_online_catalogue_config(cfg)

        assert rules["rank_percentile_5star"] == 0.05
        assert rules["rank_percentile_4star"] == 0.10, "other keys must survive"

    def test_enabled_false_string_disables(self):
        from helpers.config_helpers import get_compilation_online_catalogue_config

        cfg = {"single_detection": {"compilation_online_catalogue": {"enabled": "false"}}}

        assert get_compilation_online_catalogue_config(cfg)["enabled"] == 0

    def test_garbage_values_fall_back_to_defaults(self):
        from helpers.config_helpers import get_compilation_online_catalogue_config

        cfg = {
            "single_detection": {
                "compilation_online_catalogue": {"rank_percentile_5star": "abc"}
            }
        }

        rules = get_compilation_online_catalogue_config(cfg)

        assert rules["rank_percentile_5star"] == 0.02
