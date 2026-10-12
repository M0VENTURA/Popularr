"""Tests for the popularity scan per-track collection deadline config.

The scan runner waits at most ``popularity.track_timeout_seconds`` for an
album's slowest per-track worker before finalising with the completed
tracks (plus a 60s grace window).  A too-short deadline silently drops
slow-but-legitimate tracks on well-documented artists (the A Perfect Circle
"Eat the Elephant" case: 4 of 12 futures unfinished at the old hardcoded
300s → tracks lost).
"""

from __future__ import annotations

import pytest

import helpers.config_helpers as ch


class TestGetTrackTimeoutSeconds:
    """THE single scan-timeout knob.

    The per-album and per-artist budgets are now DERIVED from this value
    (see test_album_scan_budget.py / test_album_budget_scales_with_track_count.py),
    so its semantics carry the "0 = disable the whole safety net" contract.
    """

    def _cfg(self, value):
        return {"popularity": {"track_timeout_seconds": value}}

    def test_default_is_30(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: {})
        assert ch.get_track_timeout_seconds() == 30

    def test_custom_value(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(300))
        assert ch.get_track_timeout_seconds() == 300

    def test_clamped_low(self, monkeypatch):
        # 5s is below the 10s floor.
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(5))
        assert ch.get_track_timeout_seconds() == 10

    def test_clamped_high(self, monkeypatch):
        # 3600s is above the 600s ceiling.
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(3600))
        assert ch.get_track_timeout_seconds() == 600

    def test_zero_disables_the_whole_safety_net(self, monkeypatch):
        """0 must mean OFF, not "fall back to the default" — the whole point
        is that a user can stop any album being skipped / any artist being
        abandoned."""
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(0))
        assert ch.get_track_timeout_seconds() == 0

    def test_a_negative_value_also_disables(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(-1))
        assert ch.get_track_timeout_seconds() == 0

    def test_a_bad_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg("nonsense"))
        assert ch.get_track_timeout_seconds() == 30

    def test_missing_section_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: {"popularity": {}})
        assert ch.get_track_timeout_seconds() == 30


class TestGetPrefetchBudgetSeconds:
    def _cfg(self, value):
        return {"popularity": {"prefetch_budget_seconds": value}}

    def test_default_is_180(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: {})
        assert ch.get_prefetch_budget_seconds() == 180

    def test_custom_value(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(480))
        assert ch.get_prefetch_budget_seconds() == 480

    def test_clamped_low(self, monkeypatch):
        # 60s is below the 120s floor.
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(60))
        assert ch.get_prefetch_budget_seconds() == 120

    def test_clamped_high(self, monkeypatch):
        # 3000s is above the 1800s ceiling.
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(3000))
        assert ch.get_prefetch_budget_seconds() == 1800

    def test_zero_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(0))
        assert ch.get_prefetch_budget_seconds() == 180
