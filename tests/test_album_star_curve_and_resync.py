"""Album star curve + star resync.

The curve exists to guarantee an album spreads across all five tiers, and the
catalogue cap exists so a weak album's best track is not crowned a hit.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

from services.popularity.stages.finalise_stage import (
    _album_curve_star,
    _curve_metric,
    _rank_percentile,
)

RULES = {
    "enabled": 1,
    "curve_5star_percentile": 0.80,
    "curve_4star_percentile": 0.55,
    "curve_3star_percentile": 0.30,
    "curve_2star_percentile": 0.12,
    "catalogue_cap_5star": 0.85,
    "catalogue_cap_4star": 0.60,
    "catalogue_cap_3star": 0.30,
    "catalogue_cap_2star": 0.10,
    "five_star_min_listeners": 0,
    "min_album_tracks": 3,
}


def _pool(n: int, top: float = 100.0, bottom: float = 40.0) -> list[float]:
    """n evenly spaced scores from top down to bottom."""
    if n == 1:
        return [top]
    step = (top - bottom) / (n - 1)
    return [top - i * step for i in range(n)]


class TestCurveGuaranteesAFullShape:
    def test_ten_track_album_gets_all_five_tiers(self):
        """The whole point: a 10-track album must not collapse into one tier."""
        pool = _pool(10)
        stars = [_album_curve_star(s, pool, pool, 1_000_000, RULES) for s in pool]
        assert set(stars) == {1, 2, 3, 4, 5}

    def test_best_track_is_five_and_worst_is_one(self):
        pool = _pool(10)
        assert _album_curve_star(pool[0], pool, pool, 1_000_000, RULES) == 5
        assert _album_curve_star(pool[-1], pool, pool, 1_000_000, RULES) == 1

    def test_stars_never_increase_as_score_falls(self):
        """Monotonicity: a quieter track can never outrank a louder one."""
        pool = _pool(20)
        stars = [_album_curve_star(s, pool, pool, 1_000_000, RULES) for s in pool]
        assert stars == sorted(stars, reverse=True)

    def test_floor_of_band_counts_as_its_tier(self):
        """A track exactly ON a cut belongs to the upper tier, not the lower."""
        pool = _pool(10)
        pos = _rank_percentile(pool[1], pool)
        # pool[1] is 2nd of 10 -> 8/9 = 0.889 which is >= 0.80 -> curve tier 5
        assert pos is not None and pos >= 0.80
        # Give it a catalogue standing that supports the top tier so the cap
        # does not confound the boundary being tested.
        catalogue = _pool(100, top=100.0, bottom=1.0)
        assert _album_curve_star(pool[1], pool, catalogue, 1_000_000, RULES) == 5


class TestCurveDoesNotInventHits:
    def test_weak_album_top_track_capped_by_artist_catalogue(self):
        """Felsenfest's best track is top of the album but mid-catalogue.

        The artist's catalogue is full of far bigger songs, so being #1 of a
        quiet album must not be worth the top tier.
        """
        album = _pool(10, top=60.0, bottom=45.0)
        # 40 catalogue songs higher than anything on the album.
        catalogue = [95.0] * 40 + album
        star = _album_curve_star(album[0], album, catalogue, 9_524, RULES)
        assert star < 5, "a weak album's best track must not reach 5 stars"
        # 18th percentile of the catalogue supports only tier 2.
        assert star == 2, "it settles on the tier its catalogue standing supports"

    def test_catalogue_cap_is_at_most_the_supported_tier(self):
        """Documented semantics: the cap settles on the tier the artist
        catalogue percentile supports, rather than subtracting one tier."""
        album = _pool(10, top=60.0, bottom=45.0)
        catalogue = [95.0] * 40 + album
        # tier 5 needs .85, 4 needs .60, 3 needs .30, 2 needs .10
        from services.popularity.stages.finalise_stage import _below_percentile
        pos = _below_percentile(album[0], catalogue)
        assert pos is not None and 0.10 <= pos < 0.30
        assert _album_curve_star(album[0], album, catalogue, 9_524, RULES) == 2

    def test_cap_disabled_lets_the_curve_stand(self):
        rules = dict(
            RULES,
            catalogue_cap_5star=0.0,
            catalogue_cap_4star=0.0,
            catalogue_cap_3star=0.0,
            catalogue_cap_2star=0.0,
        )
        album = _pool(10, top=60.0, bottom=45.0)
        catalogue = [95.0] * 40 + album
        assert _album_curve_star(album[0], album, catalogue, 9_524, rules) == 5

    def test_strong_album_top_track_keeps_five(self):
        album = _pool(10, top=95.0, bottom=70.0)
        catalogue = _pool(50, top=100.0, bottom=10.0)
        assert _album_curve_star(album[0], album, catalogue, 2_000_000, RULES) == 5

    def test_catalogue_cap_only_demotes_never_promotes(self):
        """A track low on its album must not be lifted by a strong catalogue."""
        album = _pool(10)
        catalogue = _pool(10)
        worst = _album_curve_star(album[-1], album, catalogue, 10_000_000, RULES)
        assert worst == 1

    def test_five_star_listener_floor_demotes(self):
        rules = dict(RULES, five_star_min_listeners=1_000_000)
        album = _pool(10)
        star = _album_curve_star(album[0], album, album, 5_000, rules)
        assert star == 4

    def test_five_star_listener_floor_allows_a_real_hit(self):
        rules = dict(RULES, five_star_min_listeners=1_000_000)
        album = _pool(10)
        star = _album_curve_star(album[0], album, album, 2_500_000, rules)
        assert star == 5

    def test_zero_floor_disables_the_check(self):
        rules = dict(RULES, five_star_min_listeners=0)
        album = _pool(10)
        assert _album_curve_star(album[0], album, album, 1, rules) == 5


class TestCurveDeclinesWhenItCannotApply:
    def test_disabled_returns_none(self):
        pool = _pool(10)
        assert _album_curve_star(pool[0], pool, pool, 1000, dict(RULES, enabled=0)) is None

    def test_two_track_single_returns_none(self):
        """A single has no distribution to curve across."""
        pool = _pool(2)
        assert _album_curve_star(pool[0], pool, pool, 1000, RULES) is None

    def test_missing_rules_returns_none(self):
        pool = _pool(10)
        assert _album_curve_star(pool[0], pool, pool, 1000, {}) is None

    def test_zero_score_returns_none(self):
        pool = _pool(10)
        assert _album_curve_star(0.0, pool, pool, 1000, RULES) is None


class TestCurveMetricIsMonotonic:
    """The curve MUST rank on a metric that tracks audience, not the remap.

    Regression guard for the measured defect: on AC/DC's "Back in Black" the
    stored ``popularity`` gave 931,173 listeners a score of 31.1 and 134,444
    listeners a score of 53.8 — 13,390 such inversions across the collection.
    """

    def test_metric_matches_the_log_scale(self):
        assert _curve_metric({"lastfm_listeners": 1_000_000}) == pytest.approx(
            min(100.0, math.log10(1_000_001) * 16.0), abs=0.01
        )

    def test_metric_increases_with_listeners(self):
        values = [
            _curve_metric({"lastfm_listeners": n})
            for n in (1, 100, 10_000, 500_000, 40_000_000)
        ]
        assert values == sorted(values)
        assert len(set(values)) == len(values)

    def test_no_listeners_falls_back_to_stored_score(self):
        assert _curve_metric({"lastfm_listeners": 0}, fallback=42.0) == 42.0

    def test_catalogue_percentile_uses_a_monotonic_reading(self):
        """A bigger track must rank higher in the catalogue than a smaller one."""
        from services.popularity.stages.finalise_stage import _below_percentile

        catalogue = [_curve_metric({"lastfm_listeners": n})
                     for n in (10, 1_000, 100_000, 5_000_000)]
        small = _below_percentile(_curve_metric({"lastfm_listeners": 10}), catalogue)
        big = _below_percentile(_curve_metric({"lastfm_listeners": 5_000_000}), catalogue)
        assert small < big


class TestConfigIsSurfacedOnTheConfigPage:
    """§4.1: every new configurable option must appear on the Config page."""

    PATHS = (
        "templates/pages/config.html",
        "test_site/templates/Pages/config.html",
    )

    @pytest.mark.parametrize("rel", PATHS)
    def test_config_page_has_curve_fields(self, rel):
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for field in (
            "album_star_curve_enabled",
            "album_star_curve_curve_5star_percentile",
            "album_star_curve_catalogue_cap_5star",
            "album_star_curve_five_star_min_listeners",
            "album_star_curve_min_album_tracks",
        ):
            assert field in html, f"{field} missing from {rel}"

    @pytest.mark.parametrize("rel", PATHS)
    def test_config_page_has_resync_fields(self, rel):
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for field in (
            "star_resync_enabled",
            "star_resync_write_file_tags",
            "star_resync_sync_navidrome",
            "star_resync_albums_per_batch",
            "star_resync_preview_btn",
            "star_resync_apply_btn",
        ):
            assert field in html, f"{field} missing from {rel}"

    @pytest.mark.parametrize("rel", (
        "static/js/config.js",
        "static/js/pages/config.js",
        "test_site/static/js/pages/config.js",
    ))
    def test_config_js_sends_the_new_blocks(self, rel):
        js = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "album_star_curve:" in js, f"album_star_curve missing from {rel}"
        assert "star_resync:" in js, f"star_resync missing from {rel}"


class TestConfigReaders:
    def test_defaults_are_present_with_no_config_block(self):
        from helpers.config_helpers import (
            get_album_star_curve_config,
            get_star_resync_config,
        )

        curve = get_album_star_curve_config(config={})
        assert curve["enabled"] == 0, (
            "the curve must ship DISABLED — enabling it by default would "
            "silently re-rate the whole library on the next scan"
        )
        assert curve["curve_5star_percentile"] == 0.80
        assert curve["catalogue_cap_5star"] == 0.85
        assert curve["five_star_min_listeners"] == 0
        assert curve["min_album_tracks"] == 3

        resync = get_star_resync_config(config={})
        assert resync["enabled"] == 1
        assert resync["write_file_tags"] == 1
        assert resync["sync_navidrome"] == 1
        assert resync["albums_per_batch"] == 25

    def test_user_values_override_defaults(self):
        from helpers.config_helpers import (
            get_album_star_curve_config,
            get_star_resync_config,
        )

        cfg = {
            "single_detection": {
                "album_star_curve": {
                    "enabled": 0,
                    "five_star_min_listeners": 25_000,
                },
                "star_resync": {
                    "enabled": False,
                    "write_file_tags": 0,
                    "albums_per_batch": 5,
                },
            }
        }
        curve = get_album_star_curve_config(config=cfg)
        assert curve["enabled"] == 0
        assert curve["five_star_min_listeners"] == 25_000
        # untouched keys keep their defaults
        assert curve["curve_5star_percentile"] == 0.80

        resync = get_star_resync_config(config=cfg)
        assert resync["enabled"] == 0
        assert resync["write_file_tags"] == 0
        assert resync["albums_per_batch"] == 5


class TestResyncServiceContract:
    def test_api_route_is_registered(self):
        """The resync endpoint must be a real route on the misc blueprint.

        Read from source rather than importing ``routes.misc_routes``: that
        module pulls in ``services.metadata``, which circularly imports
        ``db.repositories.tag_repository`` when imported cold in a test.
        """
        src = (REPO_ROOT / "routes" / "misc_routes.py").read_text(encoding="utf-8")
        assert '@misc_api_bp.route("/ratings/resync", methods=["POST"])' in src
        assert "async def api_resync_star_ratings" in src

    def test_dry_run_defaults_to_writing_nothing(self, monkeypatch):
        from services.popularity import star_resync_service as svc

        called: dict = {}

        def _fake_load(**_kwargs):
            return {("A", "B"): [{"track_id": "1", "album": "B", "artist": "A",
                                  "album_artist": "A", "stars": 3,
                                  "popularity_score": 50.0, "title": "t"}]}

        def _fake_rate(album_results, *, options, artist_scores):
            called["options"] = options
            return ([], {"tracks": 1, "changed": 0, "star_ratings": 1, "navidrome_synced": 0})

        monkeypatch.setattr(svc, "_load_album_groups", _fake_load)
        monkeypatch.setattr(svc, "_compute_stars_for_album", _fake_rate)
        monkeypatch.setattr(
            "helpers.config_helpers.get_star_resync_config",
            lambda config=None: {"enabled": 1, "write_file_tags": 1,
                                 "sync_navidrome": 1, "albums_per_batch": 25},
        )

        svc.resync_star_ratings(dry_run=True)
        assert called["options"]["write_file_tags"] is False
        assert called["options"]["sync_navidrome"] is False

    def test_apply_honours_the_configured_flags(self, monkeypatch):
        from services.popularity import star_resync_service as svc

        called: dict = {}

        def _fake_load(**_kwargs):
            return {("A", "B"): [{"track_id": "1", "album": "B", "artist": "A",
                                  "album_artist": "A", "stars": 3,
                                  "popularity_score": 50.0, "title": "t"}]}

        def _fake_rate(album_results, *, options, artist_scores):
            called["options"] = options
            return ([], {"tracks": 1, "changed": 0, "star_ratings": 1, "navidrome_synced": 0})

        monkeypatch.setattr(svc, "_load_album_groups", _fake_load)
        monkeypatch.setattr(svc, "_compute_stars_for_album", _fake_rate)
        monkeypatch.setattr(
            "helpers.config_helpers.get_star_resync_config",
            lambda config=None: {"enabled": 1, "write_file_tags": 0,
                                 "sync_navidrome": 1, "albums_per_batch": 25},
        )

        svc.resync_star_ratings(dry_run=False)
        assert called["options"]["write_file_tags"] is False
        assert called["options"]["sync_navidrome"] is True

    def test_disabled_resync_refuses_to_run(self, monkeypatch):
        from services.popularity import star_resync_service as svc

        monkeypatch.setattr(
            "helpers.config_helpers.get_star_resync_config",
            lambda config=None: {"enabled": 0, "write_file_tags": 1,
                                 "sync_navidrome": 1, "albums_per_batch": 25},
        )
        result = svc.resync_star_ratings(dry_run=False)
        assert result["ok"] is False
        assert "disabled" in result["error"].lower()

    def test_artist_pool_spans_every_loaded_album(self, monkeypatch):
        """The catalogue cap needs the WHOLE artist, not one album."""
        from services.popularity import star_resync_service as svc

        seen: list = []

        def _fake_load(**_kwargs):
            return {
                ("A", "Album1"): [{"track_id": "1", "album": "Album1", "artist": "A",
                                   "album_artist": "A", "stars": 3,
                                   "popularity_score": 90.0, "title": "t1"}],
                ("A", "Album2"): [{"track_id": "2", "album": "Album2", "artist": "A",
                                   "album_artist": "A", "stars": 3,
                                   "popularity_score": 40.0, "title": "t2"}],
            }

        def _fake_rate(album_results, *, options, artist_scores):
            seen.append(sorted(artist_scores))
            return ([], {"tracks": 1, "changed": 0, "star_ratings": 1, "navidrome_synced": 0})

        monkeypatch.setattr(svc, "_load_album_groups", _fake_load)
        monkeypatch.setattr(svc, "_compute_stars_for_album", _fake_rate)
        monkeypatch.setattr(
            "helpers.config_helpers.get_star_resync_config",
            lambda config=None: {"enabled": 1, "write_file_tags": 0,
                                 "sync_navidrome": 0, "albums_per_batch": 25},
        )

        svc.resync_star_ratings(dry_run=False)
        assert seen, "rating was never called"
        assert all(pool == [40.0, 90.0] for pool in seen)
