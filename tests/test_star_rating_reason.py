"""A preview must say WHY a rating moved, and what the library looks like after.

The scoring engine has ~ten ways to reach or lose 5★, and the resync preview
used to be a bare ``before → after`` list. That makes a retune impossible to
audit: you cannot tell whether a failed 5★ was the album curve, the era cap or
the minor-era penalty.

``star_rating_reason`` READS the verdict fields the rules already write
(``_force_floor``, ``_album_curve_stars``, ``_live_reason``, …) and never
re-derives a rating, so it can never disagree with the score it describes.
"""

from __future__ import annotations

import pytest

from services.popularity.stages.finalise_stage import star_rating_reason


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        ({"stars": 5, "single_confidence": "user"}, "user rating wins"),
        ({"stars": 5, "_force_floor": 5}, "forced 5★"),
        ({"stars": 4, "_force_floor": 4}, "floored at 4★"),
        ({"stars": 5, "_lock_honoured": True}, "catalogue-top lock honoured"),
        ({"stars": 5, "_album_curve_stars": 5}, "album curve put it at 5★"),
        ({"stars": 3, "_album_curve_stars": 4}, "album curve put it at 4★"),
        ({"stars": 3, "_live_reason": "artist_z=0.31"}, "live album"),
        ({"stars": 4, "_ratio_5star_reason": "floor_failed"}, "album-shape gate"),
        ({"stars": 4, "_dedicated_single_floor": True}, "dedicated single floor"),
        ({"stars": 5, "single_confidence": "high"}, "high-confidence single"),
    ],
)
def test_the_reason_names_the_deciding_rule(track: dict, expected: str) -> None:
    assert expected in star_rating_reason(track)


def test_the_reason_starts_with_the_final_tier() -> None:
    """The first thing a reader needs is the tier it actually got."""
    assert star_rating_reason({"stars": 3}).startswith("3★")
    assert star_rating_reason({"stars": 5, "_force_floor": 5}).startswith("5★")


def test_a_plain_track_says_so_rather_than_lying() -> None:
    reason = star_rating_reason({"stars": 3})
    assert "z-band" in reason, (
        "with no trace field set the honest answer is the base tier, not an "
        "invented cause"
    )


def test_the_z_scores_are_included_when_known() -> None:
    reason = star_rating_reason(
        {"stars": 4, "_computed_album_z": 0.72, "_computed_artist_z": 1.31}
    )
    assert "album_z=0.72" in reason and "artist_z=1.31" in reason


def test_a_user_rating_never_reports_a_cap() -> None:
    """The user's own rating is protected from every cap — say so, not 'capped'."""
    reason = star_rating_reason(
        {"stars": 5, "single_confidence": "user", "_era_5star": False}
    )
    assert "never capped" in reason
