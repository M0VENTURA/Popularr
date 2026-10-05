"""Album tracks must appear in listening order — Track 13 after Track 12.

Reported
--------
> The ordering on the album page is sometimes off. For instance Track 13 is
> showing before track 1.

``tracks.disc_number`` and ``tracks.track_number`` are TEXT, and the album
page's SQL ordered with

    ORDER BY COALESCE(disc_number, '1'), <numeric track>, track_number, title

``COALESCE`` only replaces NULL — a **blank** disc tag stays ``''``, and ``''``
sorts *before* ``'1'`` in a text column. One track with an empty DISC tag
therefore jumped to the top of the album while every other row sorted
perfectly, which is exactly "sometimes off".

The artist page had the mirror-image bug in Python:
``safe_int(track_number) or 0`` sends unnumbered tracks to the FRONT.

Both now share ``helpers.track_ordering``. These tests are about the *rules*,
not about SQL — the ordering the user sees is decided in Python, where it can
be exercised without a database.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers.track_ordering import (
    album_track_sort_key,
    leading_int,
    sort_album_tracks,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"


def _track(number, disc="1", title=""):
    return {"track_number": number, "disc_number": disc, "title": title}


def _numbers(tracks):
    return [str(t.get("track_number")) for t in tracks]


# ===========================================================================
# The reported bug
# ===========================================================================
class TestTheReportedOrdering:
    def test_a_blank_disc_does_not_jump_to_the_top(self):
        """Track 13 has no DISC tag; every other track is disc 1."""
        # Deliberately fed in the WRONG order too, so the assertion proves the
        # sort moved it rather than the input already being right.
        album = [_track(13, disc="", title="The Future Ain't What It Used to Be")]
        album += [_track(n) for n in range(1, 13)]
        album += [_track(14)]

        ordered = sort_album_tracks(album)

        assert _numbers(ordered) == [str(n) for n in range(1, 15)], (
            "the one track with an empty DISC tag sorted ahead of every '1' — "
            "COALESCE only replaces NULL, and '' sorts before '1'"
        )
        assert ordered[12]["track_number"] == 13

    @pytest.mark.parametrize("blank_disc", ["", " ", "0", None])
    def test_any_useless_disc_is_disc_one(self, blank_disc):
        album = [_track(13, disc=blank_disc), _track(1, disc="1"), _track(2, disc="1")]
        assert _numbers(sort_album_tracks(album)) == ["1", "2", "13"]

    def test_the_order_is_numeric_not_lexicographic(self):
        """`ORDER BY track_number` on a TEXT column gives 1, 10, 11, …, 2."""
        album = [_track(n) for n in (1, 2, 10, 11, 12, 13, 14, 3)]
        assert _numbers(sort_album_tracks(album)) == [
            "1", "2", "3", "10", "11", "12", "13", "14"
        ]


# ===========================================================================
# Discs
# ===========================================================================
class TestDiscs:
    def test_disc_two_comes_after_disc_one(self):
        album = [
            _track(1, disc="2", title="d2 t1"),
            _track(2, disc="1", title="d1 t2"),
            _track(1, disc="1", title="d1 t1"),
        ]
        ordered = sort_album_tracks(album)
        assert [(t["disc_number"], t["track_number"]) for t in ordered] == [
            ("1", 1), ("1", 2), ("2", 1)
        ]

    def test_disc_numbers_are_compared_numerically(self):
        """"10" must not sort before "2"."""
        album = [_track(1, disc="10"), _track(1, disc="2"), _track(1, disc="1")]
        assert [t["disc_number"] for t in sort_album_tracks(album)] == ["1", "2", "10"]

    def test_the_sort_is_stable_for_identical_keys(self):
        album = [_track(1, title="b"), _track(1, title="a"), _track(1, title="c")]
        assert [t["title"] for t in sort_album_tracks(album)] == ["a", "b", "c"]


# ===========================================================================
# Unnumbered tracks
# ===========================================================================
class TestUnnumberedTracks:
    def test_them_sort_after_the_numbered_ones(self):
        """The artist page used to send these to the FRONT (``or 0``)."""
        album = [
            _track(None, title="no number"),
            _track("", title="blank"),
            _track(2, title="two"),
            _track(1, title="one"),
        ]
        ordered = sort_album_tracks(album)
        assert [t["title"] for t in ordered][:2] == ["one", "two"]
        assert {t["title"] for t in ordered[2:]} == {"no number", "blank"}

    def test_unnumbered_tracks_of_disc_one_stay_behind_disc_one(self):
        album = [
            _track(None, disc="2", title="d2 unnumbered"),
            _track(1, disc="1", title="d1 numbered"),
        ]
        ordered = sort_album_tracks(album)
        assert [t["disc_number"] for t in ordered] == ["1", "2"]


# ===========================================================================
# The integer the tags actually carry
# ===========================================================================
class TestLeadingInt:
    def test_the_shapes_real_tags_use(self):
        assert leading_int("13") == 13
        assert leading_int("01") == 1
        assert leading_int("1/2") == 1       # "1 of 2" — the SQL regex did this
        assert leading_int(" 7 ") == 7
        assert leading_int(13) == 13

    def test_values_with_no_leading_number_are_unnumbered(self):
        for value in (None, "", " ", "A1", "-", "Intro"):
            assert leading_int(value) is None, value


# ===========================================================================
# The pages actually use it
# ===========================================================================
class TestThePagesUseTheSharedOrdering:
    def test_the_album_page_sorts_after_its_query(self):
        """The SQL ORDER BY is a pre-sort; Python owns what the user sees."""
        source = UI_ROUTES.read_text(encoding="utf-8")
        album_detail = source.split("async def album_detail(")[1]
        assert "sort_album_tracks(tracks)" in album_detail, (
            "album_detail no longer applies the shared ordering, so a blank "
            "DISC tag would sort ahead of '1' again"
        )

    def test_the_artist_page_no_longer_puts_unnumbered_tracks_first(self):
        source = UI_ROUTES.read_text(encoding="utf-8")
        assert "safe_int(t.get(\"track_number\")) or 0" not in source, (
            "the artist page's inline key sends tracks with no number to the "
            "front of every album"
        )
        assert source.count("sort_album_tracks(") >= 2, (
            "both the album page and the artist page must use the shared key"
        )
