"""Regression tests for scan-pipeline bugs.

Covers:
- ``determine_final_status`` referencing an undefined ``is_compilation``
  (NameError) for metadata-poor medium-band tracks — e.g. "You're just a
  Ghost" during singles detection.
- ListenBrainz realism: an unrealistically low LB count (far below the
  album's LB median, or an LF/LB ratio far above the album's median ratio)
  must be treated as invalid so it no longer drags the album average down,
  while confirmed singles are never penalised.
"""

from __future__ import annotations

import pytest


class TestDetermineFinalStatusIsCompilation:
    """determine_final_status must not raise NameError on is_compilation."""

    def test_metadata_poor_medium_band_returns_none(self):
        from services.enrichment.single_detection_service import determine_final_status

        # Medium-band z-score with NO sources is not single evidence — the
        # old metadata-poor fallback flagged every mid-album track in the
        # 0.6-1.0 band (Human Era / Ph4/NT0mA / Buried in Code on Unleash
        # the Archers - Phantoma). This also guards the original NameError
        # regression: is_compilation must be handled without raising.
        result = determine_final_status(
            album_z=0.7,
            artist_z=0.7,
            high_sources=0,
            medium_sources=0,
            is_compilation=False,
        )
        assert result == "none"

    def test_compilation_not_flagged_by_medium_band(self):
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=0.7,
            artist_z=0.7,
            high_sources=0,
            medium_sources=0,
            is_compilation=True,
        )
        assert result == "none"


class TestListenBrainzValidity:
    """evaluate_listenbrainz_validity flags unrealistic LB counts."""

    HEALTHY_ALBUM_LB = [4000, 5000, 6000, 4500, 8000, 3000, 7000]
    HEALTHY_PAIRS = [
        (500, 4000), (600, 5000), (700, 6000),
        (550, 4500), (900, 8000), (400, 3000), (800, 7000),
    ]

    def _evaluate(self, lb, lf, is_single=False):
        from services.popularity.popularity_math import evaluate_listenbrainz_validity

        return evaluate_listenbrainz_validity(
            listenbrainz_listens=lb,
            lastfm_listeners=lf,
            album_lb_listens=self.HEALTHY_ALBUM_LB,
            album_lf_lb_pairs=self.HEALTHY_PAIRS,
            is_single=is_single,
        )

    def test_track_far_below_album_median_is_invalid(self):
        valid, reasons = self._evaluate(lb=8, lf=20000)
        assert not valid
        assert "lb_far_below_album_median" in reasons

    def test_ratio_outlier_is_invalid(self):
        # LB sits inside the album distribution, but LF dwarfs LB 125:1 while
        # the album median ratio is ~0.12 — clearly a mismatched LB count.
        valid, reasons = self._evaluate(lb=4000, lf=500000)
        assert not valid
        assert "lf_lb_ratio_outlier" in reasons

    def test_healthy_track_is_valid(self):
        valid, reasons = self._evaluate(lb=4500, lf=550)
        assert valid
        assert reasons == []

    def test_confirmed_single_is_never_penalised(self):
        # Same outlier LB, but the track is a confirmed single: LB stays valid.
        valid, reasons = self._evaluate(lb=8, lf=20000, is_single=True)
        assert valid
        assert reasons == []

    def test_missing_lb_is_not_invalid(self):
        # Zero / missing LB is missing data, not invalid data.
        valid, reasons = self._evaluate(lb=0, lf=550)
        assert valid
        assert reasons == []

    def test_small_album_skips_check(self):
        from services.popularity.popularity_math import evaluate_listenbrainz_validity

        valid, reasons = evaluate_listenbrainz_validity(
            listenbrainz_listens=5,
            lastfm_listeners=20000,
            album_lb_listens=[100, 200, 300],
            album_lf_lb_pairs=[(10, 100), (20, 200), (30, 300)],
            is_single=False,
        )
        # Not enough data to build a reliable distribution — no flagging.
        assert valid
        assert reasons == []
