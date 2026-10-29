"""One runaway hit must not throw off the rest of the album's z-scores.

Reported (Aqua "Aquarium"):

> "Tracks like Barbie Girl that were a runaway hit throw the zscore off for the
> rest of the album."

Barbie Girl has ~2,000,000 Last.fm listeners; every other track on the album
sits around 28k-60k.  The Last.fm component used a MEAN + STDEV z across those
raw counts, so the outlier moved the mean to ~200k and the standard deviation to
~550k.  Every sibling then scored z ≈ -0.3 and was mapped below the midpoint —
not because it was unpopular, but because the album contained a hit.

The fix is a robust (median + MAD) z in LOG space, shared by the album score and
the standout/listener z.  These tests pin the property, not the exact numbers.
"""

from __future__ import annotations

from statistics import mean, stdev

from services.popularity.popularity_math import (
    calculate_lastfm_zscore_popularity,
    zscore_to_popularity,
)
from services.popularity.popularity_zscore import log_listener_z

#: Aqua's album in the report, rounded: the hit and the tracks around it.
RUNAWAY_ALBUM = [2_000_000, 60_000, 45_000, 31_000, 28_000]


def _lf_score(listeners: int) -> float:
    return calculate_lastfm_zscore_popularity(
        listeners, listeners, RUNAWAY_ALBUM, RUNAWAY_ALBUM
    )


class TestLastfmScoreIgnoresTheRunawayHit:
    def test_the_album_median_track_sits_at_the_midpoint(self) -> None:
        """The 45k track IS the album's typical track — it must not be punished
        for sharing a release with a 2M-listener single."""
        assert 45.0 <= _lf_score(45_000) <= 55.0

    def test_the_hit_is_still_the_album_top(self) -> None:
        assert _lf_score(2_000_000) > 95.0
        assert _lf_score(2_000_000) > _lf_score(60_000)

    def test_siblings_keep_their_real_differences(self) -> None:
        """A mean/stdev reference collapsed all of these near 43; they differ
        by more than 2x in listeners and must stay ordered."""
        assert _lf_score(60_000) > _lf_score(45_000) > _lf_score(28_000)

    def test_it_beats_the_non_robust_mean_stdev_reference(self) -> None:
        """⭐ THE REGRESSION. Control computed with the old formula."""
        old_z = (60_000 - mean(RUNAWAY_ALBUM)) / stdev(RUNAWAY_ALBUM)
        old_score = zscore_to_popularity(old_z)
        new_score = _lf_score(60_000)
        assert new_score > old_score + 10.0, (
            f"the outlier still suppresses the album: {old_score:.1f} -> "
            f"{new_score:.1f}"
        )

    def test_a_two_track_album_keeps_the_simple_symmetric_z(self) -> None:
        """With only two counts there is no robust distribution to speak of;
        the pair must still map symmetrically around the midpoint."""
        album = [100_000, 50_000]
        top = calculate_lastfm_zscore_popularity(100_000, 0, album, album)
        bottom = calculate_lastfm_zscore_popularity(50_000, 0, album, album)
        assert top > 50.0 > bottom
        assert 55.0 < top < 70.0
        assert 30.0 < bottom < 45.0


class TestLogListenerZIsRobustToo:
    """The standout/listener z is used for 5★ evidence and single detection; it
    must not read the album's typical track as a non-standout merely because a
    hit exists."""

    def test_the_album_median_is_the_baseline(self) -> None:
        z = log_listener_z(45_000, RUNAWAY_ALBUM)
        assert -0.05 <= z <= 0.05

    def test_a_track_above_the_median_is_positive(self) -> None:
        assert log_listener_z(60_000, RUNAWAY_ALBUM) > 0.5

    def test_the_hit_still_registers_as_a_standout(self) -> None:
        assert log_listener_z(2_000_000, RUNAWAY_ALBUM) > 5.0

    def test_a_uniform_album_has_no_signal(self) -> None:
        assert log_listener_z(300, [300, 300, 300]) == 0.0
