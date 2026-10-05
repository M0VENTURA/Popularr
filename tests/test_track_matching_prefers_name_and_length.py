"""Track matching must be driven by the NAME and the LENGTH, not the number.

Reported:

    "Track matching is relying too much on track number and not track name
     and length"

    **MusicBrainz:** Title: *Heaven Can Wait (live)* →
    **Rock and Roll Dreams Come Through (radio edit)**
    MusicBrainz Recording ID: *e662030e-…* → **f743d192-…**
    Writer: *["Jim Steinman"]* → **Jim Steinman**

    …/203. Meat Loaf - Heaven Can Wait (live).mp3, 5:00

Both matchers paired the file with an unrelated recording purely because both
carried the same track number, and the review then offered to overwrite a
correct title, MBID and writer with a different song's. A wrong pairing is not
a cosmetic diff — ``update_file_metadata`` writes it straight into the file.

Two defects made that possible:

  * the track number was the FIRST and unconditional match, with no check that
    the two sides were describing the same song;
  * ``str(None or "")`` compared equal, so a MusicBrainz entry with no position
    at all claimed the first remaining file.

The rule now is that a number is a tie-breaker, never proof: see
``_track_number_pairing_allowed``. The controls below are as important as the
regressions — an untagged file whose title came from its filename shares no
words with the MusicBrainz title, and must still pair by number.
"""
from __future__ import annotations

import pytest

from services.enrichment import musicbrainz_service as _mbs

# ``getattr`` with a ``None`` default rather than a direct import: a missing
# helper is exactly the regression these tests exist for, so the BEHAVIOURAL
# tests must still run and fail loudly instead of the whole module erroring out
# of collection and reporting a broken build as "1 error".
_match_mb_tracks_to_library = _mbs._match_mb_tracks_to_library
match_mb_tracks_to_files = _mbs.match_mb_tracks_to_files
_titles_agree_for_pairing = getattr(_mbs, "_titles_agree_for_pairing", None)
_lengths_agree_for_pairing = getattr(_mbs, "_lengths_agree_for_pairing", None)
_track_number_pairing_allowed = getattr(_mbs, "_track_number_pairing_allowed", None)


def _require(function, name):
    assert function is not None, (
        f"{name} is missing from musicbrainz_service — the track number is "
        "being trusted again without checking the name and the length"
    )
    return function


def mb(title, number, duration_ms=None, disc=1, rec=None):
    return {
        "mb_title": title,
        "mb_track_number": number,
        "mb_disc_number": disc,
        "mb_duration": duration_ms,
        "mb_recording_mbid": rec or f"rec-{abs(hash(title)) % 10**8}",
    }


def library(title, number, duration=None, tid="t1"):
    return {
        "id": tid,
        "title": title,
        "track_number": number,
        "disc_number": 1,
        "mbid": "",
        "duration": duration,
        "mb_ignored_fields": None,
        "file_path": f"/music/{tid}.mp3",
        "artist": "Meat Loaf",
    }


def compare(mb_tracks, library_tracks):
    comparison, _extra = _match_mb_tracks_to_library(mb_tracks, library_tracks)
    return comparison


def claimed(entry) -> str:
    """Which library track the MusicBrainz entry took (``""`` = none)."""
    return str(entry.get("library_track_id") or "")


# ===========================================================================
# The report
# ===========================================================================
class TestTheReportedPairingIsRejected:
    def _report_shape(self, mb_length_ms):
        return compare(
            [
                mb("Rock and Roll Dreams Come Through (radio edit)", 12, mb_length_ms),
                mb("Heaven Can Wait", 14, 300000),
            ],
            [library("Heaven Can Wait (live)", "12", 300, tid="heaven")],
        )

    def test_the_same_number_does_not_pair_when_name_and_length_differ(self):
        comparison = self._report_shape(240000)  # radio edit ≈ 4:00, file 5:00

        radio_edit, correct = comparison[0], comparison[1]
        assert radio_edit["matched"] is False, (
            "the radio edit claimed the 5:00 file on track number alone"
        )
        assert claimed(radio_edit) == ""
        assert correct["matched"] is True
        assert claimed(correct) == "heaven"

    def test_the_review_no_longer_offers_a_different_song(self):
        """The visible symptom: title / MBID / writer all proposed at once."""
        comparison = self._report_shape(240000)
        wrong = comparison[0]

        assert wrong["matched"] is False
        assert wrong.get("library_title", "") == ""
        assert wrong.get("diff_fields") == []
        assert wrong.get("needs_update") is False

    def test_the_correct_track_still_reports_its_real_differences(self):
        """Pairing must get STRICTER, not quieter — real changes still surface."""
        comparison = self._report_shape(240000)
        correct = comparison[1]

        assert correct["matched"] is True
        diffs = set(correct.get("diff_fields") or [])
        # This folder numbers the file 12; the release carries it as track 14.
        assert "track_number" in diffs
        assert "mbid" in diffs
        # ``(live)`` is a PERFORMANCE marker and stays deliberately silent —
        # see tests/test_metadata_compare_version_markers_and_genres.py.
        assert "title" not in diffs

    def test_the_same_shape_is_rejected_by_the_folder_matcher(self):
        entries = match_mb_tracks_to_files(
            {
                "release_group_title": "Bat Out of Hell II",
                "release_title": "Bat Out of Hell II",
                "artist": "Meat Loaf",
                "tracks": [
                    mb("Rock and Roll Dreams Come Through (radio edit)", 12, 240000),
                    mb("Heaven Can Wait", 14, 300000),
                ],
            },
            [
                {
                    "file_path": "/d/203. Meat Loaf - Heaven Can Wait (live).mp3",
                    "track_number": 12,
                    "title": "Heaven Can Wait (live)",
                    "duration": 300,
                }
            ],
        )
        assert entries[0]["matched"] is False
        assert entries[1]["matched"] is True
        assert entries[1]["file_path"].startswith("/d/203")


# ===========================================================================
# Two blanks are not an identity
# ===========================================================================
class TestBlankTrackNumbersAreNotIdentity:
    def test_a_positionless_tracklist_entry_claims_nothing(self):
        """``str(None or "")`` used to compare equal and grab the first file."""
        comparison = compare(
            [mb("Heaven Can Wait", None)],
            [library("Totally Different Song", None, tid="other")],
        )
        assert comparison[0]["matched"] is False
        assert claimed(comparison[0]) == ""

    def test_the_folder_matcher_agrees(self):
        entries = match_mb_tracks_to_files(
            {"tracks": [mb("Heaven Can Wait", None)]},
            [{"file_path": "/d/x.flac", "track_number": None, "title": "Other"}],
        )
        assert entries[0]["matched"] is False


# ===========================================================================
# Controls — the number must still work when nothing contradicts it
# ===========================================================================
class TestTheTrackNumberStillWorks:
    def test_an_untagged_file_with_a_meaningless_title_still_pairs(self):
        """A filename-derived title shares no words with the MusicBrainz one.

        Without this the reported fix would leave every untagged download
        unmatched — the number is still the only identity those files have.
        """
        comparison = compare(
            [mb("I Remember You", 1, 215000)],
            [library("garbage", "1", None, tid="untagged")],
        )
        assert comparison[0]["matched"] is True
        assert claimed(comparison[0]) == "untagged"

    def test_a_title_that_differs_but_a_length_that_agrees_still_pairs(self):
        """A rename with a matching recording length is the same recording."""
        comparison = compare(
            [mb("Rock and Roll Dreams Come Through", 12, 300000)],
            [library("Rock & Roll Dreams Come Through", "12", 300, tid="renamed")],
        )
        assert comparison[0]["matched"] is True

    def test_a_correct_title_beats_a_wrong_number(self):
        comparison = compare(
            [mb("Heaven Can Wait", 12, 300000)],
            [library("Heaven Can Wait", "3", 300, tid="misnumbered")],
        )
        assert comparison[0]["matched"] is True
        assert claimed(comparison[0]) == "misnumbered"

    def test_the_folder_matcher_keeps_pairing_untagged_files(self):
        entries = match_mb_tracks_to_files(
            {"tracks": [mb("I Remember You", 1, 215000), mb("BiiiG", 2, 199000)]},
            [
                {"file_path": "/d/01.flac", "track_number": 1, "title": "garbage"},
                {"file_path": "/d/02.flac", "track_number": 2, "title": "garbage too"},
            ],
        )
        assert [entry["matched"] for entry in entries] == [True, True]


# ===========================================================================
# The rule itself
# ===========================================================================
class TestThePairingRule:
    def test_agreeing_titles_allow_the_number(self):
        rule = _require(_track_number_pairing_allowed, "_track_number_pairing_allowed")
        assert rule("Heaven Can Wait (live)", "Heaven Can Wait", 300, 300000) is True

    def test_a_blank_title_cannot_disagree(self):
        rule = _require(_track_number_pairing_allowed, "_track_number_pairing_allowed")
        assert rule("", "Heaven Can Wait", None, 300000) is True

    def test_a_disagreeing_name_with_an_unknown_length_gives_the_number_the_benefit(
        self,
    ):
        rule = _require(_track_number_pairing_allowed, "_track_number_pairing_allowed")
        assert rule("garbage", "I Remember You", None, 215000) is True

    def test_a_disagreeing_name_and_a_disagreeing_length_reject_the_number(self):
        rule = _require(_track_number_pairing_allowed, "_track_number_pairing_allowed")
        assert (
            rule(
                "Heaven Can Wait (live)",
                "Rock and Roll Dreams Come Through (radio edit)",
                300,
                240000,
            )
            is False
        )

    def test_a_disagreeing_name_with_an_agreeing_length_is_allowed(self):
        rule = _require(_track_number_pairing_allowed, "_track_number_pairing_allowed")
        assert (
            rule(
                "Rock & Roll Dreams Come Through",
                "Rock and Roll Dreams Come Through",
                300,
                300000,
            )
            is True
        )

    def test_the_length_rule_uses_the_review_s_tolerance(self):
        """Pairing and reporting must never disagree about what "matches"."""
        lengths = _require(_lengths_agree_for_pairing, "_lengths_agree_for_pairing")
        tolerance = _mbs.DURATION_TOLERANCE_SECONDS

        assert lengths(240, 240000 + tolerance * 1000) is True
        assert lengths(240, 240000 + (tolerance + 1) * 1000) is False

    @pytest.mark.parametrize(
        "left, right",
        [
            ("Heaven Can Wait (live)", "Heaven Can Wait"),
            ("Heaven Can Wait (live)", "Rock and Roll Dreams Come Through"),
            ("", "Heaven Can Wait"),
        ],
    )
    def test_title_agreement_is_exposed_for_reuse(self, left, right):
        agree = _require(_titles_agree_for_pairing, "_titles_agree_for_pairing")
        assert isinstance(agree(left, right), bool)
        assert agree(left, left) is True
