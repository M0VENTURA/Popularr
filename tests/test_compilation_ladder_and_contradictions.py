"""Tests for the compilation ladder + listener contradiction report."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.popularity.contradiction_report_service import contradiction_key
from services.popularity.stages.finalise_stage import _compilation_absolute_ladder_stars

REPO_ROOT = Path(__file__).resolve().parents[1]

RESPACED = {
    "enabled": 1,
    "catalogue_is_cap_only": 1,
    "five_star_listeners": 900_000,
    "four_star_listeners": 120_000,
    "three_star_listeners": 15_000,
    "two_star_listeners": 2_000,
}


class TestLadderIsOptIn:
    def test_disabled_keeps_the_shipped_thresholds(self):
        """With the feature off, the ORIGINAL rungs must still apply."""
        assert _compilation_absolute_ladder_stars(95.0, 1_500_000, False) == 5
        assert _compilation_absolute_ladder_stars(85.0, 500_000, False) == 4
        assert _compilation_absolute_ladder_stars(70.0, 75_000, False) == 3
        assert _compilation_absolute_ladder_stars(35.0, 10_000, False) == 2
        assert _compilation_absolute_ladder_stars(10.0, 100, False) == 1

    def test_disabled_rules_dict_ignores_the_new_rungs(self):
        off = dict(RESPACED, enabled=0)
        # 900k would be 5 stars if the ladder were enabled.
        assert _compilation_absolute_ladder_stars(90.0, 900_000, False, off) == 4

    def test_verified_single_path_is_unchanged(self):
        assert _compilation_absolute_ladder_stars(55.0, 1_000_000, True) == 5
        assert _compilation_absolute_ladder_stars(30.0, 100, True) == 3
        assert _compilation_absolute_ladder_stars(5.0, 100, True) == 1


class TestRespacedLadder:
    @pytest.mark.parametrize("listeners,tier", [
        (2_000_000, 5),
        (900_000, 5),
        (899_999, 4),
        (120_000, 4),
        (119_999, 3),
        (15_000, 3),
        (14_999, 2),
        (2_000, 2),
        (1_999, 1),
        (7, 1),
    ])
    def test_each_rung(self, listeners, tier):
        assert _compilation_absolute_ladder_stars(50.0, listeners, False, RESPACED) == tier

    def test_rungs_are_monotonic(self):
        """A louder track can never earn fewer stars."""
        tiers = [
            _compilation_absolute_ladder_stars(50.0, n, False, RESPACED)
            for n in (10, 500, 2_000, 10_000, 15_000, 100_000, 120_000, 500_000, 900_000)
        ]
        assert tiers == sorted(tiers)


class TestContradictionKey:
    def test_strips_a_trailing_parenthetical(self):
        """A compilation often tags the same song with a country."""
        assert contradiction_key("The Code (Switzerland)") == contradiction_key("The Code")

    def test_strips_a_remaster_tag(self):
        assert contradiction_key("1979 (Remastered 2012)") == contradiction_key("1979")

    def test_casefolds_and_collapses_whitespace(self):
        assert contradiction_key("  Hello   WORLD ") == "hello world"

    def test_strips_a_cover_annotation(self):
        """A trailing "(X Cover)" tag is a compilation annotation, not part of
        the song's identity, so the same song on two compilations must share a
        key whether or not one of them carries the tag."""
        assert contradiction_key("A Gi (The Four Vagabonds Cover)") == "a gi"


class TestConfigIsSurfaced:
    PATHS = (
        "templates/pages/config.html",
        "test_site/templates/Pages/config.html",
    )

    @pytest.mark.parametrize("rel", PATHS)
    def test_config_page_has_ladder_fields(self, rel):
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for field in (
            "compilation_ladder_enabled",
            "compilation_ladder_catalogue_is_cap_only",
            "compilation_ladder_five_star_listeners",
            "compilation_ladder_four_star_listeners",
            "compilation_ladder_three_star_listeners",
            "compilation_ladder_two_star_listeners",
        ):
            assert field in html, f"{field} missing from {rel}"


class TestConfigReaders:
    def test_defaults_are_off(self):
        from helpers.config_helpers import get_compilation_ladder_config

        rules = get_compilation_ladder_config(config={})
        assert rules["enabled"] == 0, (
            "the ladder must ship DISABLED — enabling it changes compilation ratings"
        )
        assert rules["five_star_listeners"] == 900_000
        assert rules["four_star_listeners"] == 120_000
        assert rules["three_star_listeners"] == 15_000
        assert rules["two_star_listeners"] == 2_000

    def test_user_values_override(self):
        from helpers.config_helpers import get_compilation_ladder_config

        cfg = {
            "single_detection": {
                "compilation_ladder": {
                    "enabled": 1,
                    "five_star_listeners": 2_000_000,
                }
            }
        }
        rules = get_compilation_ladder_config(config=cfg)
        assert rules["enabled"] == 1
        assert rules["five_star_listeners"] == 2_000_000
        # untouched keys keep their defaults
        assert rules["four_star_listeners"] == 120_000


class TestReportRouteExists:
    def test_route_is_declared(self):
        """Read from source: importing misc_routes cold trips a circular import."""
        src = (REPO_ROOT / "routes" / "misc_routes.py").read_text(encoding="utf-8")
        assert '@misc_api_bp.route("/popularity/contradictions"' in src
        assert "async def api_listener_contradictions" in src
