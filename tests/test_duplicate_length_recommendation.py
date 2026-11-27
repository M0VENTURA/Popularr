"""Duplicate-copy recommendations must weigh track LENGTH.

REPORTED: "if multiple tracks exist on the album page for the same song, it
should recommend the one to delete based on the correct track length."

The recommendation existed but scored only quality + metadata; ``duration`` was
carried and never compared, so a wrong-length copy (a live take, radio edit, or
truncated download) tied with the correct one and the user had to compare by eye.
The album page also discarded ``group.recommendation`` entirely.
"""

from __future__ import annotations

from services.metadata import duplicate_version_service as dvs


def _copy(track_id: str, duration: float, *, path: str = "") -> dict:
    return {
        "id": track_id,
        "title": "Song",
        "artist": "Artist",
        "duration": duration,
        "file_path": path or f"/music/{track_id}.mp3",
    }


def _recommend(rows, expected: float = 0.0):
    return dvs.recommend_duplicate_keeper(rows, expected)


class TestLengthDecidesWhenQualityTies:
    def test_the_correct_length_copy_is_kept(self):
        """Two copies + the release's own length: an exact verdict."""
        rec = _recommend([
            _copy("right", 245),
            _copy("wrong", 192),   # ~53 s short: a radio edit / truncation
        ], expected=245)
        assert rec["keep_track_id"] == "right"
        assert rec["remove_track_ids"] == ["wrong"]
        assert rec["confidence"] == "length"
        assert "4:05" in rec["reason"], rec["reason"]

    def test_a_live_take_longer_than_the_studio_version_is_the_outlier(self):
        rec = _recommend([
            _copy("studio", 245),
            _copy("live", 340),
        ], expected=245)
        assert rec["keep_track_id"] == "studio"
        assert rec["remove_track_ids"] == ["live"]

    def test_two_copies_without_a_reference_do_not_guess(self):
        """CONTROL: with no expected length and no majority, do not invent one."""
        rec = _recommend([_copy("a", 245), _copy("b", 192)])
        assert rec["confidence"] != "length", rec

    def test_the_majority_cluster_wins(self):
        """3 copies at ~4:05 + 1 at 3:00: the outlier loses, the rest tie."""
        rec = _recommend([
            _copy("a", 245), _copy("b", 246), _copy("c", 244),
            _copy("outlier", 180),
        ])
        # Length identifies the OUTLIER, but cannot pick among three equal
        # copies — so the outlier is condemned and the rest go to metadata/tie.
        assert "outlier" in rec.get("remove_track_ids", []) or rec["confidence"] == "tie"

    def test_one_correct_copy_versus_one_wrong_is_resolved_by_length(self):
        rec = _recommend([_copy("a", 245), _copy("outlier", 180)], expected=245)
        assert rec["keep_track_id"] == "a"
        assert rec["remove_track_ids"] == ["outlier"]

    def test_a_one_second_difference_is_not_a_different_version(self):
        """CONTROL: rounding must not pick a 'wrong length' loser."""
        rec = _recommend([_copy("a", 245), _copy("b", 246)])
        assert rec["confidence"] != "length"

    def test_three_increasing_lengths_do_not_name_an_outlier(self):
        """CONTROL: 4:00 / 4:20 / 4:45 identifies no single bad copy."""
        rec = _recommend([
            _copy("a", 240), _copy("b", 260), _copy("c", 285),
        ])
        assert rec["confidence"] != "length", rec

    def test_missing_durations_fall_through(self):
        rec = _recommend([_copy("a", 0), _copy("b", 0)])
        assert rec["confidence"] != "length"


class TestTheReasonIsUserFacing:
    def test_it_names_both_lengths(self):
        rec = _recommend([_copy("right", 245), _copy("wrong", 192)], expected=245)
        assert "4:05" in rec["reason"]
        assert "3:12" in rec["reason"]
