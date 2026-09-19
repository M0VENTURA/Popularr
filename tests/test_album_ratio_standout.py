"""Unit tests for the album standout RATIO helpers.

These are the pure functions behind the 5★ ratio gate. They are tested
directly (no DB, no config) so the arithmetic is pinned independently of the
star-assignment wiring.

Reference case (a real one — Anti-Flag, acoustic album): counts
14, 13, 13, ~11 median, 8. The top track is 1.07-1.08x the runner-up and
1.27x the median, i.e. a tie, not a standout.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_math import (
    ALBUM_RATIO_MIN_COUNTS,
    album_ratio_standout,
    album_standout_ratios,
)


#: The reference album: flat, small magnitude.
ANTIFLAG = [14, 13, 13, 11, 11, 10, 9, 8]

#: A genuine studio standout: hit + unpopular deep cuts.
REAL_STANDOUT = [500000, 40000, 30000, 25000, 20000, 15000, 12000, 10000]


class TestAlbumStandoutRatios:
    def test_antiflag_reference_multipliers(self):
        """The three multipliers reproduce the published arithmetic exactly."""
        r = album_standout_ratios(ANTIFLAG)
        assert r["top"] == 14.0
        assert r["runner_up"] == 13.0
        assert r["median"] == 11.0
        assert r["floor"] == 8.0
        # 14/13, 14/11, 14/8 — the values the report quotes.
        assert r["runner_up_ratio"] == pytest.approx(1.0769, abs=0.001)
        assert r["median_ratio"] == pytest.approx(1.2727, abs=0.001)
        assert r["floor_ratio"] == pytest.approx(1.75, abs=0.001)

    def test_real_standout_multipliers(self):
        r = album_standout_ratios(REAL_STANDOUT)
        assert r["runner_up_ratio"] == pytest.approx(12.5, abs=0.01)
        assert r["median_ratio"] == pytest.approx(22.22, abs=0.01)
        assert r["floor_ratio"] == pytest.approx(50.0, abs=0.01)

    def test_ratios_are_scale_free(self):
        """The same SHAPE at a different magnitude yields identical ratios.

        This is the whole point of using multipliers rather than raw counts:
        the test must not care whether the artist has 100 listeners or 10M.
        """
        small = album_standout_ratios([14, 13, 13, 11, 11, 10, 9, 8])
        large = album_standout_ratios([x * 1000 for x in (14, 13, 13, 11, 11, 10, 9, 8)])
        for key in ("runner_up_ratio", "median_ratio", "floor_ratio"):
            assert small[key] == pytest.approx(large[key], abs=0.001)

    @pytest.mark.parametrize("counts", [[], [50], [50, 40], [0, 0]])
    def test_too_few_counts_returns_empty(self, counts):
        """Below the minimum the answer is "unknown", not "flat"."""
        assert album_standout_ratios(counts) == {}
        assert len(counts) < ALBUM_RATIO_MIN_COUNTS

    def test_zeros_are_ignored_not_treated_as_floor(self):
        """A zero count is missing data, not a 0-listener track.

        Treating zeros as real would make the floor ratio infinite and grant a
        standout to an album with one listener and one blank row.
        """
        r = album_standout_ratios([0, 0, 50, 40, 30])
        assert r["floor"] == 30.0
        assert r["floor_ratio"] == pytest.approx(50 / 30, abs=0.01)

    def test_all_identical_is_flat(self):
        r = album_standout_ratios([10, 10, 10, 10])
        assert r["runner_up_ratio"] == pytest.approx(1.0)
        assert r["median_ratio"] == pytest.approx(1.0)
        assert r["floor_ratio"] == pytest.approx(1.0)


class TestAlbumRatioStandout:
    def test_antiflag_is_rejected(self):
        ok, reason, ratios = album_ratio_standout(14.0, ANTIFLAG)
        assert not ok
        assert "ratio_flat" in reason
        assert "runner_up" in reason and "median" in reason and "floor" in reason
        assert ratios["top"] == 14.0

    def test_real_standout_is_accepted(self):
        ok, reason, _ = album_ratio_standout(500000.0, REAL_STANDOUT)
        assert ok
        assert "ratio_standout" in reason

    def test_inconclusive_data_is_permissive(self):
        """Thin data must never block a rating — it cannot be judged."""
        for counts, cand in (([], 10.0), ([50], 50.0), ([50, 40], 50.0)):
            ok, reason, _ = album_ratio_standout(cand, counts)
            assert ok, f"{counts} should be inconclusive, not a rejection"
            assert "inconclusive" in reason

    def test_non_top_track_is_ineligible(self):
        ok, reason, _ = album_ratio_standout(40000.0, REAL_STANDOUT)
        assert not ok
        assert "not_album_top" in reason

    def test_exact_top_is_eligible(self):
        ok, _, _ = album_ratio_standout(500000.0, REAL_STANDOUT)
        assert ok

    def test_min_passed_controls_how_many_must_hold(self):
        """``min_passed=1`` accepts a shape that fails two of three.

        This is the knob the live-album rules use (they default to 2) so the
        thresholds can be relaxed without adding three separate switches.
        """
        # Top is 2x the runner-up (fails 1.5x? no — passes), 2x median (fails
        # 3.0), 2x floor (fails 10). So exactly ONE multiplier passes.
        counts = [20, 10, 10, 10, 10, 10]
        strict_ok, _, _ = album_ratio_standout(20.0, counts, min_passed=3)
        loose_ok, _, _ = album_ratio_standout(20.0, counts, min_passed=1)
        assert not strict_ok
        assert loose_ok

    def test_thresholds_are_configurable(self):
        """A caller can pass its own multipliers (live uses looser ones)."""
        # sorted desc [100, 50, 40, 30, 20, 1] -> median 35:
        #   runner-up 100/50  = 2.0x  (>= 1.5  ✓)
        #   median    100/35  = 2.86x (<  3.0  ✗)
        #   floor     100/1   = 100x  (>= 10   ✓)
        # So the studio defaults (all three) REJECT it, while min_passed=2
        # accepts it — which is exactly what the live rules rely on.
        counts = [100, 50, 40, 30, 20, 1]
        strict_ok, strict_reason, _ = album_ratio_standout(100.0, counts)
        assert not strict_ok
        assert "ratio_flat" in strict_reason

        live_ok, _, _ = album_ratio_standout(100.0, counts, min_passed=2)
        assert live_ok, "2 of 3 satisfied should pass the live-style rule"

        # Raised thresholds reject the same shape on every count.
        ok, _, _ = album_ratio_standout(
            100.0, counts, runner_up_min=5.0, median_min=10.0, floor_min=1000.0
        )
        assert not ok

    def test_returns_ratios_for_logging(self):
        """Callers log the actual numbers behind a decision."""
        _, _, ratios = album_ratio_standout(14.0, ANTIFLAG)
        assert set(ratios) >= {
            "top", "runner_up", "median", "floor",
            "runner_up_ratio", "median_ratio", "floor_ratio",
        }
