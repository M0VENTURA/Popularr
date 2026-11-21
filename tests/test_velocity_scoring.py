"""Age-adjusted velocity and ListenBrainz collision dampening.

Both are OPT-IN (``popularity.velocity``), and both exist because the blended
score only ever sees one track's counts:

* a raw listen count rewards AGE — a 1994 single and a 2024 single with the same
  listeners are not equally popular today;
* two DIFFERENT songs sharing an identical ``(listens, score)`` pair above 50k
  is not a coincidence, because ``listenbrainz_score`` is a pure function of the
  listen count. An exact tie means one recording's numbers were counted twice
  (a recording/MBID resolution miss), which is what let "Mama Said" tie "The
  Memory Remains" and "Jealous" inherit "Ironic"'s figures.
"""

from __future__ import annotations

import pytest

from services.popularity import popularity_math as pm

CURRENT_YEAR = 2026


class TestAgeAdjustedVelocity:
    def test_it_divides_by_the_tracks_age(self) -> None:
        """Same listens, older release -> lower velocity."""
        old = pm.age_adjusted_velocity(100_000, 1994, current_year=CURRENT_YEAR)
        new = pm.age_adjusted_velocity(100_000, 2024, current_year=CURRENT_YEAR)
        assert new > old

    def test_it_is_listens_per_year(self) -> None:
        # 2020 -> age is 2026 - 2020 + 1 = 7 years.
        assert pm.age_adjusted_velocity(7000, 2020, current_year=CURRENT_YEAR) == pytest.approx(
            1000.0
        )

    def test_the_age_is_floored_for_a_brand_new_release(self) -> None:
        """A track released this year must not divide by ~0."""
        velocity = pm.age_adjusted_velocity(1000, CURRENT_YEAR, current_year=CURRENT_YEAR)
        assert velocity == pytest.approx(1000 / 1.5)

    @pytest.mark.parametrize(
        "listens, year",
        [(0, 2000), (None, 2000), (1000, 0), (1000, None), (-5, 2000)],
    )
    def test_it_returns_zero_without_evidence(self, listens, year) -> None:
        assert pm.age_adjusted_velocity(listens, year, current_year=CURRENT_YEAR) == 0.0


class TestNormaliseVelocity:
    def test_the_artist_fastest_track_is_100(self) -> None:
        assert pm.normalise_velocity(500.0, 500.0) == pytest.approx(100.0)

    def test_it_is_monotonic(self) -> None:
        low = pm.normalise_velocity(50.0, 500.0)
        high = pm.normalise_velocity(400.0, 500.0)
        assert high > low

    def test_it_is_log_scaled_not_linear(self) -> None:
        """A 10x velocity must not sit at 10% — that would flatten a catalogue."""
        assert pm.normalise_velocity(50.0, 500.0) > 10.0

    def test_zero_inputs_are_zero(self) -> None:
        assert pm.normalise_velocity(0.0, 500.0) == 0.0
        assert pm.normalise_velocity(100.0, 0.0) == 0.0


class TestCollisionDetection:
    def test_two_different_songs_on_the_same_numbers_collide(self) -> None:
        rows = [
            {"title": "Ironic", "listenbrainz_listens": 900_000, "listenbrainz_score": 100.0},
            {"title": "Jealous", "listenbrainz_listens": 900_000, "listenbrainz_score": 100.0},
        ]
        assert pm.find_collision_keys(rows) == {(900_000.0, 100.0)}

    def test_the_same_song_on_two_albums_is_not_a_collision(self) -> None:
        """An album track and its compilation appearance share the recording."""
        rows = [
            {"title": "Everlong", "listenbrainz_listens": 900_000, "listenbrainz_score": 100.0},
            {"title": "Everlong", "listenbrainz_listens": 900_000, "listenbrainz_score": 100.0},
        ]
        assert pm.find_collision_keys(rows) == set()

    def test_a_low_tie_is_coincidence(self) -> None:
        rows = [
            {"title": "A", "listenbrainz_listens": 120, "listenbrainz_score": 33.0},
            {"title": "B", "listenbrainz_listens": 120, "listenbrainz_score": 33.0},
        ]
        assert pm.find_collision_keys(rows) == set()

    def test_the_threshold_is_configurable(self) -> None:
        rows = [
            {"title": "A", "listenbrainz_listens": 120, "listenbrainz_score": 33.0},
            {"title": "B", "listenbrainz_listens": 120, "listenbrainz_score": 33.0},
        ]
        assert pm.find_collision_keys(rows, min_listens=100) == {(120.0, 33.0)}


class TestCollisionDamping:
    def test_a_colliding_track_is_damped(self) -> None:
        keys = {(900_000.0, 100.0)}
        assert pm.collision_damping(900_000, 100.0, keys) == pytest.approx(0.88)

    def test_a_clean_track_is_untouched(self) -> None:
        keys = {(900_000.0, 100.0)}
        assert pm.collision_damping(500_000, 97.0, keys) == 1.0

    def test_no_keys_means_no_damping(self) -> None:
        assert pm.collision_damping(900_000, 100.0, set()) == 1.0
        assert pm.collision_damping(900_000, 100.0, None) == 1.0


class TestTheCombinedScoreStaysOptIn:
    """⚠️ The defaults must be a byte-for-byte no-op."""

    def test_defaults_change_nothing(self) -> None:
        kwargs = dict(
            lastfm_listeners=500_000,
            listenbrainz_listens=900_000,
            album_lb_listens=[900_000, 400_000, 100_000],
        )
        baseline = pm.calculate_combined_popularity_score(**kwargs)
        with_zero_terms = pm.calculate_combined_popularity_score(
            **kwargs,
            velocity_score=0.0,
            velocity_weight=0.0,
            collision_multiplier=1.0,
        )
        assert baseline["combined_score"] == with_zero_terms["combined_score"]

    def test_velocity_weight_moves_the_score(self) -> None:
        kwargs = dict(listenbrainz_listens=100_000)
        without = pm.calculate_combined_popularity_score(**kwargs)
        with_velocity = pm.calculate_combined_popularity_score(
            **kwargs, velocity_score=100.0, velocity_weight=0.3,
        )
        assert with_velocity["combined_score"] > without["combined_score"]

    def test_collision_damping_lowers_the_score(self) -> None:
        kwargs = dict(listenbrainz_listens=100_000)
        clean = pm.calculate_combined_popularity_score(**kwargs)
        damped = pm.calculate_combined_popularity_score(
            **kwargs, collision_multiplier=0.88,
        )
        assert damped["combined_score"] < clean["combined_score"]

    def test_the_result_stays_on_the_0_100_scale(self) -> None:
        out = pm.calculate_combined_popularity_score(
            listenbrainz_listens=5_000_000,
            velocity_score=100.0,
            velocity_weight=0.5,
        )
        assert 0.0 <= out["combined_score"] <= 100.0


class TestVelocityConfig:
    def test_it_defaults_off(self) -> None:
        from services.popularity.popularity_config import get_velocity_scoring_config

        cfg = get_velocity_scoring_config({})
        assert cfg["enabled"] is False
        assert cfg["weight"] == 0.0

    def test_it_reads_the_block(self) -> None:
        from services.popularity.popularity_config import get_velocity_scoring_config

        cfg = get_velocity_scoring_config(
            {"popularity": {"velocity": {"enabled": True, "weight": 0.3}}}
        )
        assert cfg["enabled"] is True
        assert cfg["weight"] == pytest.approx(0.3)

    def test_a_weight_alone_enables_it(self) -> None:
        from services.popularity.popularity_config import get_velocity_scoring_config

        cfg = get_velocity_scoring_config({"popularity": {"velocity": {"weight": 0.2}}})
        assert cfg["enabled"] is True

    def test_the_weight_is_clamped(self) -> None:
        from services.popularity.popularity_config import get_velocity_scoring_config

        assert get_velocity_scoring_config(
            {"popularity": {"velocity": {"weight": 5.0}}}
        )["weight"] == 1.0


class TestTheResyncAppliesItCatalogueWide:
    """The terms need the artist's whole catalogue, so the resync owns them."""

    def _groups(self):
        return {
            ("Metallica", "Load"): [
                {
                    "title": "Mama Said",
                    "year": 1996,
                    "listenbrainz_listens": 2_916_387,
                    "listenbrainz_score": 100.0,
                    "popularity_score": 98.96,
                },
                {
                    "title": "The Memory Remains",
                    "year": 1997,
                    "listenbrainz_listens": 2_827_299,
                    "listenbrainz_score": 100.0,
                    "popularity_score": 98.97,
                },
            ],
            ("Metallica", "Ride the Lightning"): [
                {
                    "title": "Fade to Black",
                    "year": 1984,
                    "listenbrainz_listens": 2_736_446,
                    "listenbrainz_score": 97.97,
                    "popularity_score": 97.97,
                },
            ],
        }

    def test_it_is_a_noop_when_disabled(self) -> None:
        from services.popularity import star_resync_service as svc

        groups = self._groups()
        before = [r["popularity_score"] for rows in groups.values() for r in rows]
        report = svc._apply_velocity_adjustments(groups)
        after = [r["popularity_score"] for rows in groups.values() for r in rows]
        assert report["enabled"] is False
        assert before == after

    def test_it_damps_the_collision_when_enabled(self, monkeypatch) -> None:
        from services.popularity import star_resync_service as svc

        monkeypatch.setattr(
            "services.popularity.popularity_config.get_velocity_scoring_config",
            lambda *a, **k: {
                "enabled": True,
                "weight": 0.0,
                "collision_damping": 0.88,
                "collision_min_listens": 50_000,
            },
        )
        # A genuine collision: two DIFFERENT songs on an identical pair.
        groups = {
            ("Blind Melon", "Soup"): [
                {
                    "title": "No Rain",
                    "year": 1992,
                    "listenbrainz_listens": 900_000,
                    "listenbrainz_score": 100.0,
                    "popularity_score": 95.0,
                },
                {
                    "title": "Galaxie",
                    "year": 1995,
                    "listenbrainz_listens": 900_000,
                    "listenbrainz_score": 100.0,
                    "popularity_score": 90.0,
                },
                {
                    "title": "Tones of Home",
                    "year": 1992,
                    "listenbrainz_listens": 120_000,
                    "listenbrainz_score": 83.0,
                    "popularity_score": 80.0,
                },
            ],
        }
        report = svc._apply_velocity_adjustments(groups)
        rows = {r["title"]: r for r in groups[("Blind Melon", "Soup")]}
        assert report["enabled"] is True
        assert report["collisions"] == 2
        assert rows["No Rain"]["popularity_score"] == pytest.approx(95.0 * 0.88)
        assert rows["Galaxie"]["popularity_score"] == pytest.approx(90.0 * 0.88)
        # A clean track in the same album is untouched.
        assert rows["Tones of Home"]["popularity_score"] == pytest.approx(80.0)

    def test_a_saturated_score_alone_is_not_a_collision(self, monkeypatch) -> None:
        """⚠️ Many mainstream tracks tie at score 100.0 with DIFFERENT listens.

        Flagging on the score alone would damp half a metal catalogue, so the
        rule requires the listen count to match too.
        """
        from services.popularity import star_resync_service as svc

        monkeypatch.setattr(
            "services.popularity.popularity_config.get_velocity_scoring_config",
            lambda *a, **k: {
                "enabled": True,
                "weight": 0.0,
                "collision_damping": 0.88,
                "collision_min_listens": 50_000,
            },
        )
        groups = self._groups()  # both Metallica tracks score 100.0, listens differ
        report = svc._apply_velocity_adjustments(groups)
        assert report["collisions"] == 0

    def test_the_velocity_term_uses_the_earliest_release(self, monkeypatch) -> None:
        """A greatest-hits reissue must not make a 1984 song look new."""
        from services.popularity import star_resync_service as svc

        monkeypatch.setattr(
            "services.popularity.popularity_config.get_velocity_scoring_config",
            lambda *a, **k: {
                "enabled": True,
                "weight": 0.5,
                "collision_damping": 0.88,
                "collision_min_listens": 50_000,
            },
        )
        groups = {
            ("Artist", "Greatest Hits"): [
                {
                    "title": "Old Song",
                    "year": 2005,
                    "is_compilation": 1,
                    "listenbrainz_listens": 1_000_000,
                    "listenbrainz_score": 96.0,
                    "popularity_score": 90.0,
                },
            ],
            ("Artist", "Original"): [
                {
                    "title": "Old Song",
                    "year": 1984,
                    "is_compilation": 0,
                    "listenbrainz_listens": 1_000_000,
                    "listenbrainz_score": 96.0,
                    "popularity_score": 90.0,
                },
            ],
        }
        svc._apply_velocity_adjustments(groups)
        # The compilation row is judged on the ORIGINAL's 1984 release, so its
        # velocity is the low one a 40-year-old recording deserves — not the
        # high one a 2005 "release" would imply.
        comp_row = groups[("Artist", "Greatest Hits")][0]
        orig_row = groups[("Artist", "Original")][0]
        assert comp_row["popularity_score"] == pytest.approx(
            orig_row["popularity_score"]
        )
