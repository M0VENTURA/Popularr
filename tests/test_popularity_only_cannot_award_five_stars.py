"""A popularity-only scan cannot award 5 stars to a regular album track.

This pins the behaviour that explains "the log said GLOBAL 5* LOCKED but the
database stored 4*". It is NOT a bug in the z-gates or in singles detection:
in `popularity_only` mode every route to 5* for a studio track is closed —

  * `_global_5star_locked`  — excluded by `not popularity_only`
  * `is_standout`           — excluded by `... and not popularity_only`
  * forced star percentile  — excluded by `not popularity_only`
  * the banded base tier    — `_album_z_band_star` caps at `min(4, ...)`

If that contract ever changes (i.e. popularity-only starts awarding 5*), this
test fails and the decision must be made deliberately, not by accident.
"""

from __future__ import annotations

import pytest

from services.popularity.stages.finalise_stage import _assign_stars

TH = {
    "star5_album_z": 1.0, "star4_album_z": 0.5, "star3_album_z": -0.5,
    "star2_album_z": -1.2, "star5_artist_z": 1.0, "star4_artist_z": 0.5,
    "epsilon": 0.0, "listener_5star_z": 0.0,
}

#: A standout so dominant that EVERY gate would pass it in a normal scan.
POOL = [95.0, 40.0, 39.0, 38.0, 37.0, 36.0, 35.0, 34.0, 33.0, 32.0]
LISTENS = [33152.0, 18703.0, 17607.0, 13099.0, 12000.0, 11000.0]


def _assign(*, pop_only: bool, locked: bool) -> int:
    track = {
        "title": "Absolute",
        "artist": "Comeback Kid",
        "album": "Outsider",
        "popularity_score": 97.07,
        "final_score": 97.07,
        "lastfm_listeners": 33152,
        "single_confidence": "high",
        "popularity_marked": True,
    }
    if locked:
        track["_global_5star_locked"] = True
    return _assign_stars(
        track, POOL, POOL,
        popularity_only=pop_only,
        album_model={"has_benchmark": True, "era": "peak",
                     "catalog_cutoff": 50.0, "album_top_n": 3, "max_5star_slots": 3},
        artist_listen_distribution=LISTENS,
        force_star_percentiles=(0.05, 0.20),
        thresholds=TH,
    )


class TestFiveStarIsUnreachable:
    def test_a_normal_scan_awards_five_to_a_locked_standout(self):
        """CONTROL — proves the fixture is capable of 5*, so the test below is
        not passing for an unrelated reason."""
        assert _assign(pop_only=False, locked=True) == 5

    def test_popularity_only_caps_a_locked_standout_at_four(self):
        assert _assign(pop_only=True, locked=True) == 4

    def test_popularity_only_caps_an_unlocked_standout_at_four(self):
        assert _assign(pop_only=True, locked=False) == 4

    @pytest.mark.parametrize("locked", [True, False])
    def test_five_star_is_unreachable_in_popularity_only(self, locked):
        """The headline: no combination of lock + standout evidence reaches 5*."""
        assert _assign(pop_only=True, locked=locked) < 5
