"""An edit qualifier is a soft version marker, not a different song.

REPORTED
--------
> Tracks with edit or single edit are not matching the main release

The album Compare offered these as renames:

    Title: When Doves Cry → When Doves Cry (single edit)
    Title: Nothing Compares 2 U → Nothing Compares 2 U (edit)

WHY IT HAPPENED
---------------
``normalize_title_for_compare`` — the one key shared by the Compare button and
the Lookup-MBID preview — stripped performance markers (``(Live)``,
``(Acoustic)``, ``(Remix)`` …) but kept ``(edit)`` as if it were wording. The
two titles therefore produced ``"when doves cry"`` and
``"when doves cry single edit"``, and the review asked the user to overwrite a
plain title with a release-specific qualifier.

MusicBrainz routinely appends ``(edit)`` / ``(single edit)`` to the recording
title on a release while the library holds the plain title. That is the same
kind of SOFT marker the popularity matcher already treats as compatible
(``{"version", "edit", "radio"}``): it may be absent from either side without
making the two titles different songs.

The rule is deliberately detected on EITHER side, matching the existing
performance-marker trade-off: the app cannot tell from text alone which take a
file holds, so a marker difference is not reported rather than guessed at.
Wording differences are still reported (the controls below), and a genuinely
shorter single edit still raises the LENGTH difference.
"""
from __future__ import annotations

import pytest

from helpers.normalization_service import (
    has_version_marker,
    normalize_title_for_compare,
    titles_match_for_review,
)
from services.enrichment.musicbrainz_service import _match_mb_tracks_to_library


# ===========================================================================
# 1. The reported titles are the same song
# ===========================================================================
class TestTheReportedTitlesMatch:
    @pytest.mark.parametrize("library,musicbrainz", [
        # The reported rows, verbatim.
        ("When Doves Cry", "When Doves Cry (single edit)"),
        ("Nothing Compares 2 U", "Nothing Compares 2 U (edit)"),
        # The other spellings a tag source / MusicBrainz uses.
        ("Song", "Song (single edit)"),
        ("Song", "Song (radio edit)"),
        ("Song", "Song (album edit)"),
        ("Song", "Song [edit]"),
        ("Song", "Song (Single Edit)"),
        # The marker may be on the LOCAL side instead.
        ("When Doves Cry (single edit)", "When Doves Cry"),
        ("Nothing Compares 2 U (edit)", "Nothing Compares 2 U"),
        ("Song (Radio Edit)", "Song"),
    ])
    def test_the_edit_qualifier_is_not_a_rename(self, library, musicbrainz):
        assert titles_match_for_review(library, musicbrainz) is True
        assert (
            normalize_title_for_compare(library)
            == normalize_title_for_compare(musicbrainz)
        ), (
            f"{library!r} vs {musicbrainz!r} produced different keys, so the "
            "Compare still proposes the edit qualifier as a title change"
        )


# ===========================================================================
# 2. The Compare button uses the shared key
# ===========================================================================
def _mb(title: str, number: int = 1, duration_ms: int = 240000, rec: str = "rec-1"):
    return {
        "mb_title": title,
        "mb_track_number": number,
        "mb_disc_number": 1,
        "mb_duration": duration_ms,
        "mb_recording_mbid": rec,
    }


def _library(title: str, number: str = "1", duration: int = 240, tid: str = "t1"):
    return {
        "id": tid,
        "title": title,
        "track_number": number,
        "disc_number": 1,
        "mbid": "rec-1",
        "duration": duration,
        "mb_ignored_fields": None,
        "file_path": f"/music/{tid}.mp3",
        "artist": "Artist",
    }


def _compare(mb_title: str, library_title: str, **kwargs):
    comparison, _extra = _match_mb_tracks_to_library(
        [_mb(mb_title, **kwargs)],
        [_library(library_title)],
    )
    return comparison[0]


class TestTheCompareButtonAgrees:
    def test_the_reported_row_no_longer_raises_a_title_change(self):
        entry = _compare(
            "When Doves Cry (single edit)", "When Doves Cry",
        )
        assert entry["matched"] is True
        assert entry["diff_fields"] == []
        assert entry["needs_update"] is False

    def test_a_local_edit_marker_is_not_stripped_either(self):
        entry = _compare("Song", "Song (Radio Edit)")
        assert entry["matched"] is True
        assert "title" not in entry["diff_fields"]

    def test_a_real_length_difference_still_surfaces(self):
        """CONTROL — a single edit really is shorter; the title rule must not
        hide that the recordings differ in length."""
        entry = _compare("Song (single edit)", "Song", duration_ms=180000)
        assert entry["matched"] is True
        assert "title" not in entry["diff_fields"]
        assert "duration" in entry["diff_fields"]
        assert entry["needs_update"] is True


# ===========================================================================
# 3. CONTROLS — the word boundary must not swallow real titles
# ===========================================================================
class TestRealDifferencesStillDiffer:
    @pytest.mark.parametrize("library,musicbrainz", [
        ("Song", "Song Two (edit)"),
        ("Song One (edit)", "Song Two (edit)"),
        ("Song", "Other Song (edit)"),
        # Words that CONTAIN "edit" are not the marker.
        ("Song (Edited)", "Song"),
        ("Song (Edition)", "Song"),
    ])
    def test_a_wording_difference_is_still_a_rename(self, library, musicbrainz):
        assert titles_match_for_review(library, musicbrainz) is False


class TestTheMarkerIsDetectedButNotOverreach:
    @pytest.mark.parametrize("title", [
        "Song (edit)", "Song (single edit)", "Song (radio edit)", "Song [edit]",
    ])
    def test_an_edit_marker_is_detected(self, title):
        assert has_version_marker(title) is True

    @pytest.mark.parametrize("title", [
        "Song", "Edited", "Song (Edited)", "Song (Edition)", "Edit",
    ])
    def test_ordinary_words_are_not_markers(self, title):
        assert has_version_marker(title) is False
