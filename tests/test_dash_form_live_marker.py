"""Dash-form performance markers are the same title as parenthesised ones.

REPORTED (compare screen):

    Sugar - Live at Irving Plaza, NYC, NY - January 1999
    vs
    Sugar (live at Irving Plaza, NYC, NY – January 1999)

The MB side was stripped by the bracketed marker rule; the file side kept
" - Live at <venue>" and looked like a different song.
"""

from __future__ import annotations

from helpers.normalization_service import (
    normalize_title_for_compare,
    titles_match_for_review,
)

LIBRARY = "Sugar - Live at Irving Plaza, NYC, NY - January 1999"
MUSICBRAINZ = "Sugar (live at Irving Plaza, NYC, NY \u2013 January 1999)"  # en dash


class TestTheDashFormMarker:
    def test_the_reported_pair_is_the_same_title(self):
        assert normalize_title_for_compare(LIBRARY) == "sugar"
        assert normalize_title_for_compare(MUSICBRAINZ) == "sugar"
        assert titles_match_for_review(LIBRARY, MUSICBRAINZ) is True

    def test_a_plain_trailing_marker_still_folds(self):
        assert normalize_title_for_compare("Song - Live") == "song"
        assert normalize_title_for_compare("Song \u2013 Acoustic") == "song"
        assert normalize_title_for_compare("Song - Live in Tokyo") == "song"

    def test_a_title_merely_containing_live_is_untouched(self):
        assert normalize_title_for_compare("Live and Let Die") == "live and let die"
        assert normalize_title_for_compare("I Live for You") == "i live for you"

    def test_two_live_venues_are_the_same_song_for_a_title_change(self):
        # The venue lives in the performance marker, not the song title.
        assert titles_match_for_review(
            "Sugar - Live at Irving Plaza", "Sugar - Live at Wembley"
        ) is True

    def test_a_genuine_wording_difference_is_still_different(self):
        assert titles_match_for_review("Sugar", "Honey") is False
