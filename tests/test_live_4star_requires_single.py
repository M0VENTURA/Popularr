"""Regression tests: live tracks reach 4★ only when marked as a single.

``single_detection.live_4star_requires_single`` (default ON): a live /
acoustic / unplugged / demo / alternate track may only reach the 4★ band
when it is marked as a single (high/medium/user confidence).  A non-single
live track is capped at 3★ — the 4★ band is reserved for studio singles /
genuine standouts, so a bonus live cut (or a live album's crowd-pleaser)
cannot sit alongside real singles at 4★ unless it was actually issued as
one.  Turning the config key OFF restores the old behaviour.
"""

from __future__ import annotations

import pytest


def _assign(track, album, artist, **kwargs):
    from services.popularity.stages import finalise_stage as fs

    return fs._assign_stars(track, album, artist, **kwargs)


def _track(score=95.0, title="Song (Live)", conf="low", single=False, **overrides):
    track = {
        "track_id": "t1", "artist": "A", "album": "B", "title": title,
        "popularity_score": score, "final_score": score,
        "lastfm_listeners": 5000, "listenbrainz_listens": 4000,
        "lb_percentile": 0.95, "lastfm_score": 8.0, "listenbrainz_score": 9.0,
        "is_single": single, "single_confidence": conf,
        "single_sources": "", "is_live": False, "popularity_marked": False,
    }
    track.update(overrides)
    return track


_ALBUM = [10.0, 20.0, 30.0, 40.0, 95.0]
_ARTIST = [10.0, 20.0, 30.0, 40.0, 95.0]


class TestLive4StarRequiresSingleDefault:
    """Default (toggle ON): non-single live tracks cap at 3★."""

    def test_non_single_live_track_capped_at_three(self):
        # A strong live track that is NOT a single: album-z would place it in
        # the 4★ band, but the gate caps it at 3★.
        assert _assign(_track(conf="low", single=False), _ALBUM, _ARTIST) == 3

    def test_single_live_track_reaches_four(self):
        # A live track marked as a single keeps the 4★ band.
        assert _assign(
            _track(conf="high", single=True), _ALBUM, _ARTIST,
        ) == 4

    def test_user_marked_live_track_reaches_five(self):
        # A USER-marked single (``single_confidence == "user"``) is an
        # explicit instruction and is NEVER demoted — ``_assign_stars``
        # returns 5 for it straight away (the live-4★ gate is
        # ALGORITHMIC only; a manual override bypasses it).
        assert _assign(
            _track(conf="user", single=True), _ALBUM, _ARTIST,
        ) == 5

    def test_non_live_track_unaffected(self):
        # A plain studio track still reaches the 4★ band on album-z alone.
        assert _assign(
            _track(title="Song", conf="low", single=False), _ALBUM, _ARTIST,
        ) == 4

    def test_acoustic_title_treated_as_live_for_cap(self):
        # Acoustic titles are live-grouped → non-single acoustic caps at 3★.
        assert _assign(
            _track(title="Song (Acoustic)", conf="low", single=False),
            _ALBUM, _ARTIST,
        ) == 3


class TestLive4StarRequiresSingleDisabled:
    """Toggle OFF: live tracks reach 4★ on album-z alone (legacy)."""

    def test_non_single_live_track_reaches_four_when_disabled(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(
            fs, "get_standout_config",
            lambda: {"live_4star_requires_single": False},
        )
        assert _assign(_track(conf="low", single=False), _ALBUM, _ARTIST) == 4

    def test_global_5star_lock_never_live(self):
        # A live track in the global 5★ pool is still capped — the lock
        # respects the live cap (no live 5★).
        from services.popularity.stages import finalise_stage as fs

        track = _track(conf="high", single=True, _global_5star_locked=True)
        assert fs._assign_stars(track, _ALBUM, _ARTIST) <= 4


class TestConfigKey:
    def test_default_is_true(self):
        from helpers.config_helpers import get_standout_config

        assert get_standout_config().get("live_4star_requires_single", True) is True
