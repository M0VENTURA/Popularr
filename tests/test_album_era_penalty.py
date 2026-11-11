"""Album-level minor-era penalty.

A mature album whose median popularity sits well below the artist's peak album
should give up 4★/5★ slots — but only when the catalogue is large enough for
"weak for this artist" to mean anything, and never for a new album that has not
had time to gather plays.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pytest

from services.popularity.stages import finalise_stage as fs

GATES = {
    "min_age_years": 2.0,
    "min_catalogue_albums": 3.0,
    "min_catalogue_tracks": 30.0,
    "minor_max_5star_slots": 1.0,
    "minor_max_4star_slots": 2.0,
}


class TestPenaltyGates:
    def test_mature_album_in_a_large_catalogue_is_penalised(self):
        assert fs._album_penalty_applies(
            album_year=2010,
            catalogue_albums=5,
            catalogue_tracks=80,
            current_year=2026,
            gates=GATES,
        )

    def test_new_album_is_exempt(self):
        assert not fs._album_penalty_applies(
            album_year=2026,
            catalogue_albums=5,
            catalogue_tracks=80,
            current_year=2026,
            gates=GATES,
        )

    def test_unknown_year_is_exempt(self):
        assert not fs._album_penalty_applies(
            album_year=0,
            catalogue_albums=5,
            catalogue_tracks=80,
            current_year=2026,
            gates=GATES,
        )

    def test_small_album_count_is_exempt(self):
        assert not fs._album_penalty_applies(
            album_year=2010,
            catalogue_albums=2,
            catalogue_tracks=80,
            current_year=2026,
            gates=GATES,
        )

    def test_small_track_count_is_exempt(self):
        assert not fs._album_penalty_applies(
            album_year=2010,
            catalogue_albums=5,
            catalogue_tracks=10,
            current_year=2026,
            gates=GATES,
        )


def _tracks(stars: int, n: int, **overrides):
    out = []
    for i in range(n):
        track = {
            "track_id": f"t{i}",
            "title": f"Track {i}",
            "stars": stars,
            "popularity_score": 50 - i,
        }
        track.update(overrides)
        out.append(track)
    return out


class TestFourStarCap:
    def test_excess_four_stars_drop_to_three(self):
        tracks = _tracks(4, 5)
        changed = fs._apply_album_four_star_cap(tracks, max_slots=2)

        kept = [t for t in tracks if t["stars"] == 4]
        assert changed == 3
        assert len(kept) == 2
        # The highest-scoring tracks keep 4★.
        assert {t["track_id"] for t in kept} == {"t0", "t1"}
        assert all(t["stars"] == 3 for t in tracks[2:])

    def test_max_zero_drops_every_automatic_four_star(self):
        tracks = _tracks(4, 3)
        fs._apply_album_four_star_cap(tracks, max_slots=0)
        assert all(t["stars"] == 3 for t in tracks)

    def test_locked_track_is_protected(self):
        tracks = _tracks(4, 3)
        tracks[-1]["_lock_honoured"] = True
        fs._apply_album_four_star_cap(tracks, max_slots=1)

        locked = [t for t in tracks if t.get("_lock_honoured")]
        assert locked and locked[0]["stars"] == 4

    def test_forced_floor_is_protected(self):
        tracks = _tracks(4, 3)
        tracks[-1]["_force_floor"] = 4
        fs._apply_album_four_star_cap(tracks, max_slots=1)
        assert tracks[-1]["stars"] == 4

    def test_dedicated_single_floor_is_protected(self):
        tracks = _tracks(4, 3)
        tracks[-1]["_dedicated_single_floor"] = True
        fs._apply_album_four_star_cap(tracks, max_slots=1)
        assert tracks[-1]["stars"] == 4


class TestFiveStarCap:
    def test_excess_curve_five_stars_drop_to_four(self):
        """The album curve can award 5★ without ``_era_5star``; the penalty
        pass must still cap those."""
        tracks = _tracks(5, 3)
        changed = fs._apply_album_five_star_cap(tracks, max_slots=1)

        assert changed == 2
        assert sum(1 for t in tracks if t["stars"] == 5) == 1
        assert sum(1 for t in tracks if t["stars"] == 4) == 2

    def test_genuine_standout_is_protected(self):
        tracks = _tracks(5, 3)
        tracks[-1]["_era_5star"] = True  # genuine standout, even at lower score
        fs._apply_album_five_star_cap(tracks, max_slots=1)

        assert tracks[-1]["stars"] == 5
        assert sum(1 for t in tracks if t["stars"] == 5) == 1

    def test_locked_and_forced_five_stars_are_protected(self):
        tracks = _tracks(5, 3)
        tracks[1]["_lock_honoured"] = True
        tracks[2]["_force_floor"] = 5
        fs._apply_album_five_star_cap(tracks, max_slots=0)

        assert {t["stars"] for t in tracks[1:]} == {5}
        assert tracks[0]["stars"] == 4

    def test_penalty_sequence_limits_both_high_tiers(self):
        """Moderate penalty: at most 1 × 5★ and 2 × 4★ after both passes."""
        tracks = _tracks(5, 3) + _tracks(4, 5) + _tracks(3, 4)
        fs._apply_album_five_star_cap(tracks, max_slots=1)
        fs._apply_album_four_star_cap(tracks, max_slots=2)

        assert sum(1 for t in tracks if t["stars"] == 5) == 1
        assert sum(1 for t in tracks if t["stars"] == 4) == 2


# ---------------------------------------------------------------------------
# Integration: _build_album_model wires the gates into the returned caps
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return _FakeResult(self._rows)


def _session_factory(rows):
    @contextmanager
    def _cm():
        yield _FakeSession(rows)

    return _cm


def _catalogue_rows():
    rows = []
    for album, base_lf, base_score in (
        ("Peak", 1_000_000, 80.0),
        ("Mid", 300_000, 60.0),
        ("Weak", 5_000, 20.0),
    ):
        for i in range(12):
            rows.append(
                {
                    "title": f"{album} Track {i}",
                    "album": album,
                    "final_score": base_score - i,
                    "year": 2010,
                    "lastfm_listeners": base_lf,
                    "listenbrainz_listens": base_lf // 10,
                }
            )
    return rows


def _weak_album_results(year: int):
    return [
        {
            "album": "Weak",
            "title": f"Weak Track {i}",
            "popularity_score": 20.0 - i,
            "year": year,
            "lastfm_listeners": 5_000,
            "listenbrainz_listens": 500,
        }
        for i in range(12)
    ]


def _model(monkeypatch, year: int):
    monkeypatch.setattr(fs, "get_standout_config", lambda: {})
    monkeypatch.setattr(fs, "db_session", _session_factory(_catalogue_rows()))
    return fs._build_album_model(
        "Test Artist",
        _weak_album_results(year),
        artist_scores=[70.0] * 36,
    )


def test_mature_weak_album_gets_penalty_caps(monkeypatch):
    model = _model(monkeypatch, year=2010)

    assert model["era"] == "minor"
    assert model["penalty_applies"] is True
    assert model["max_5star_slots"] == 1
    assert model["max_4star_slots"] == 2
    assert model["catalogue_albums"] >= 3
    assert model["catalogue_tracks"] >= 30


def test_new_weak_album_is_not_penalised(monkeypatch):
    model = _model(monkeypatch, year=datetime.now().year)

    assert model["era"] == "minor"
    assert model["penalty_applies"] is False
    assert model["max_4star_slots"] is None
    # The ordinary minor-era allowance is kept intact.
    assert model["max_5star_slots"] == 2


def test_preloaded_catalogue_skips_the_per_album_query(monkeypatch):
    """The resync loads the artist catalogue once and reuses it per album."""
    monkeypatch.setattr(fs, "get_standout_config", lambda: {})

    def _boom(_artist):
        raise AssertionError("catalogue must be supplied, not re-queried")

    monkeypatch.setattr(fs, "_load_artist_catalogue_rows", _boom)
    model = fs._build_album_model(
        "Test Artist",
        _weak_album_results(2010),
        artist_scores=[70.0] * 36,
        catalogue_rows=_catalogue_rows(),
    )

    assert model["has_benchmark"] is True
    assert model["benchmark_source"] == "prominence"
    assert model["era"] == "minor"
    assert model["penalty_applies"] is True
