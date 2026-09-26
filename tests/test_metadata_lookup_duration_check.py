"""Regression tests for the duration check in MusicBrainz metadata lookup.

THE REPORT
----------
"When doing a metadata lookup, it doesn't seem that the duration is being
checked. This should be shown if it's not correct against the musicbrainz data."

WHY IT LOOKED UNCHECKED
-----------------------
``_match_mb_tracks_to_library`` compared the two durations RAW:

    Lib_ms = int(float(Library_duration or 0))
    Mb_ms  = int(Entry["mb_duration"])
    if abs(Lib_ms - Mb_ms) > 2000:      # 2000 MILLISECONDS
        Diff_fields.append("duration")

The variable name ``Lib_ms`` reveals the assumption: that ``tracks.duration`` is
milliseconds. It is **SECONDS** — both writers copy Navidrome/Subsonic's own
``duration`` field, which the Subsonic API specifies in seconds
(``services/scanning/metadata_extractor.py`` and
``db/repositories/navidrome.py``). MusicBrainz reports ``length`` in
MILLISECONDS.

So for an EXACT 4:00 match the comparison was ``abs(240 - 240000) > 2000`` —
true. EVERY track was flagged, matching or not, which is indistinguishable from
the check not running at all.

The old system normalised both sides to seconds and allowed 5 seconds, so this
restores that behaviour while keeping the server's own comparison the single
source of truth.
"""
from __future__ import annotations

import pytest

# ``pytest.importorskip`` is deliberately NOT used: a missing helper is exactly
# the regression these tests exist for, and skipping would report a green run
# for a broken build. Missing names are resolved to None / a sentinel so the
# BEHAVIOURAL tests still run and FAIL loudly, rather than aborting collection
# and hiding every failure behind one ImportError.
from helpers import normalization_service as _norm
from services.enrichment import musicbrainz_service as _mbs

_track_duration_seconds = getattr(_norm, "track_duration_seconds", None)
_format_duration_mmss = getattr(_norm, "format_duration_mmss", None)
DURATION_TOLERANCE_SECONDS = getattr(_mbs, "DURATION_TOLERANCE_SECONDS", 5)
_match_mb_tracks_to_library = _mbs._match_mb_tracks_to_library


def track_duration_seconds(value):
    assert _track_duration_seconds is not None, (
        "helpers.normalization_service.track_duration_seconds is missing — the "
        "two duration sources cannot be compared without it"
    )
    return _track_duration_seconds(value)


def format_duration_mmss(value):
    assert _format_duration_mmss is not None, (
        "helpers.normalization_service.format_duration_mmss is missing"
    )
    return _format_duration_mmss(value)


def _mb(number: str, title: str, length_ms) -> dict:
    return {
        "mb_disc_number": 1,
        "mb_track_number": number,
        "mb_title": title,
        "mb_recording_mbid": f"rec-{number}",
        "mb_duration": length_ms,
    }


def _lib(number: str, title: str, duration) -> dict:
    return {
        "id": f"t{number}",
        "title": title,
        "track_number": number,
        "disc_number": 1,
        "mbid": f"rec-{number}",
        "duration": duration,
        "mb_ignored_fields": None,
        "file_path": f"/music/{number}.mp3",
    }


def _compare(lib_duration, mb_length_ms):
    """Run the REAL comparison and return the single track's entry."""
    comparison, _extra = _match_mb_tracks_to_library(
        [_mb("1", "Song One", mb_length_ms)], [_lib("1", "Song One", lib_duration)]
    )
    return comparison[0]


# ---------------------------------------------------------------------------
# The unit helper
# ---------------------------------------------------------------------------

class TestTrackDurationSeconds:
    """One canonical normaliser, because the two sources disagree on unit."""

    def test_seconds_pass_through(self):
        assert track_duration_seconds(240) == 240.0
        assert track_duration_seconds("240") == 240.0

    def test_milliseconds_are_converted(self):
        assert track_duration_seconds(240000) == 240.0
        assert track_duration_seconds("240000") == 240.0

    def test_a_genuine_long_track_is_not_mistaken_for_milliseconds(self):
        """An 11-minute epic must stay 700s, not become 0.7s.

        This is why the ms threshold is an HOUR rather than something small.
        """
        assert track_duration_seconds(700) == 700.0

    def test_an_hour_exactly_is_still_seconds(self):
        assert track_duration_seconds(3600) == 3600.0

    def test_missing_values_are_none_not_zero(self):
        """None means UNKNOWN. Reporting unknown as a difference is a false
        positive, so these must never compare equal to a real duration.

        ``0`` must map to None rather than 0.0: the comparison guards on
        ``is not None``, so a 0.0 would slip through and make every track whose
        duration the library never recorded look mismatched.
        """
        for value in (None, "", 0, "0", -5, "nonsense"):
            result = track_duration_seconds(value)
            assert result is None, (
                f"{value!r} produced {result!r}; unknown durations must be None "
                "so the comparison skips them entirely"
            )

    def test_floats_are_accepted(self):
        assert track_duration_seconds(240.5) == 240.5


class TestFormatDurationMmss:
    def test_seconds_are_rendered_as_mmss(self):
        assert format_duration_mmss(240) == "4:00"
        assert format_duration_mmss(65) == "1:05"

    def test_milliseconds_are_rendered_the_same(self):
        """Both sides must display identically for the same real duration.

        The bug showed "Length: 240 -> 240000" for one 4-minute track."""
        assert format_duration_mmss(240000) == "4:00"
        assert format_duration_mmss(240) == format_duration_mmss(240000)

    def test_unknown_is_empty_not_a_placeholder(self):
        assert format_duration_mmss(None) == ""
        assert format_duration_mmss(0) == ""


# ---------------------------------------------------------------------------
# THE REGRESSION: an exact match must NOT be flagged
# ---------------------------------------------------------------------------

class TestAnExactMatchIsNotFlagged:
    """The core bug. Library duration is SECONDS, MusicBrainz is MILLISECONDS."""

    def test_an_exact_match_is_not_flagged(self):
        entry = _compare(240, 240000)
        assert "duration" not in (entry.get("diff_fields") or []), (
            "an exact 4:00 match was reported as a duration difference — the "
            "two sides are in different units and must be normalised first"
        )
        assert entry["needs_update"] is False

    def test_a_one_second_difference_is_tolerated(self):
        entry = _compare(239, 240000)
        assert "duration" not in (entry.get("diff_fields") or [])

    def test_a_difference_at_the_tolerance_boundary_is_tolerated(self):
        entry = _compare(240 - DURATION_TOLERANCE_SECONDS, 240000)
        assert "duration" not in (entry.get("diff_fields") or [])

    def test_a_difference_just_over_the_tolerance_is_flagged(self):
        entry = _compare(240 - DURATION_TOLERANCE_SECONDS - 1, 240000)
        assert "duration" in (entry.get("diff_fields") or [])


class TestAGenuineMismatchIsFlagged:
    def test_a_much_shorter_file_is_flagged(self):
        """A radio edit is tens of seconds shorter — a different recording."""
        entry = _compare(180, 240000)
        assert "duration" in (entry.get("diff_fields") or [])
        assert entry["needs_update"] is True

    def test_a_much_longer_file_is_flagged(self):
        entry = _compare(420, 240000)
        assert "duration" in (entry.get("diff_fields") or [])

    def test_an_unusually_long_track_is_compared_correctly(self):
        """11 minutes: still seconds, and an exact match must not be flagged."""
        entry = _compare(660, 660000)
        assert "duration" not in (entry.get("diff_fields") or [])

    def test_a_long_track_that_really_differs_is_flagged(self):
        entry = _compare(660, 300000)
        assert "duration" in (entry.get("diff_fields") or [])


class TestAnUnknownLibraryDurationIsNotADifference:
    """Nothing to compare is not the same as "wrong".

    The old code did ``float(Library_duration or 0)``, turning a missing value
    into 0 and flagging every track whose duration the library never recorded.
    """

    @pytest.mark.parametrize("missing", [None, "", 0, "0"])
    def test_a_missing_library_duration_is_not_flagged(self, missing):
        entry = _compare(missing, 240000)
        assert "duration" not in (entry.get("diff_fields") or []), (
            "an unknown library duration must not be reported as a mismatch"
        )

    def test_a_missing_musicbrainz_length_is_not_flagged(self):
        entry = _compare(240, None)
        assert "duration" not in (entry.get("diff_fields") or [])


# ---------------------------------------------------------------------------
# The comparison exposes usable display/normalised values
# ---------------------------------------------------------------------------

class TestTheComparisonExposesDisplayValues:
    """The UI showed "Length: 240 -> 240000" — raw seconds next to raw ms."""

    def test_normalised_seconds_are_present(self):
        entry = _compare(240, 240000)
        assert entry["library_duration_seconds"] == 240.0
        assert entry["mb_duration_seconds"] == 240.0

    def test_display_strings_are_present_and_equal(self):
        entry = _compare(240, 240000)
        assert entry["library_duration_display"] == "4:00"
        assert entry["mb_duration_display"] == "4:00"

    def test_a_mismatch_displays_both_readable_lengths(self):
        entry = _compare(180, 240000)
        assert entry["library_duration_display"] == "3:00"
        assert entry["mb_duration_display"] == "4:00"

    def test_display_values_survive_a_missing_library_duration(self):
        entry = _compare(None, 240000)
        assert entry["library_duration_display"] == ""
        assert entry["mb_duration_display"] == "4:00"


# ---------------------------------------------------------------------------
# `mb_duration` must stay in MILLISECONDS
# ---------------------------------------------------------------------------

class TestMbDurationKeepsItsRawUnit:
    """The missing-track queue path depends on raw milliseconds.

    ``tests/test_album_download_track_contract.py`` pins
    ``track["mb_duration"] == track["duration"]`` and the add-to-queue payload
    is documented as staying in MusicBrainz milliseconds (normalised later by
    ``queue_duration_seconds``). Fixing the COMPARISON must not change it.
    """

    def test_mb_duration_is_untouched(self):
        entry = _compare(240, 240000)
        assert entry["mb_duration"] == 240000, (
            "mb_duration must stay in raw milliseconds for the queue path"
        )

    def test_library_duration_is_reported_as_stored(self):
        entry = _compare(240, 240000)
        assert entry["library_duration"] == 240


# ---------------------------------------------------------------------------
# The Lookup MBID review reports duration (informationally)
# ---------------------------------------------------------------------------

class TestTheLookupReviewReportsDuration:
    """The proposal engine omitted duration entirely — the word did not appear
    in the module — so the primary review flow said nothing about it."""

    def test_the_module_reports_duration_checks(self):
        from services.metadata import metadata_proposal_service as proposal

        assert hasattr(proposal, "_duration_checks"), (
            "the Lookup MBID review must report duration mismatches"
        )

    def test_duration_checks_reach_the_response(self):
        """Asserting the helper EXISTS is not enough: returning an empty list
        silently reports nothing, which is the reported symptom.

        Mutation-testing caught this — the "review stops reporting duration"
        mutation (``"duration_checks": []``) SURVIVED a hasattr-only check.
        """
        import importlib
        from unittest import mock

        proposal = importlib.import_module(
            "services.metadata.metadata_proposal_service"
        )

        comparison = {
            "success": True,
            "mb_release_mbid": "rel-1",
            "comparison": [
                {
                    "matched": True,
                    "library_track_id": "t2",
                    "library_title": "B",
                    "library_track_number": "2",
                    "library_duration_display": "3:00",
                    "mb_duration_display": "4:00",
                    "diff_fields": ["duration"],
                },
            ],
            "extra_tracks": [],
        }
        mbs = importlib.import_module("services.enrichment.musicbrainz_service")

        with mock.patch.object(proposal, "_load_local_tracks",
                               return_value=[{"id": "t2"}]), \
             mock.patch.object(mbs, "compare_musicbrainz_release",
                               lambda a, al, m: comparison), \
             mock.patch.object(mbs, "fetch_musicbrainz_release_metadata",
                               lambda rid: {}):
            result = proposal.propose_album_metadata("A", "B", "rel-1")

        assert result["duration_checks"], (
            "a track whose length differs must be reported in duration_checks, "
            "not silently dropped"
        )
        assert result["duration_checks"][0]["track_id"] == "t2"
        assert result["counts"]["duration_mismatches"] == 1

    def test_only_mismatched_matched_tracks_are_reported(self):
        from services.metadata.metadata_proposal_service import _duration_checks

        comparison = [
            {  # matches, same length -> silent
                "matched": True, "library_track_id": "t1",
                "library_title": "A", "library_track_number": "1",
                "library_duration_display": "4:00", "mb_duration_display": "4:00",
                "diff_fields": [],
            },
            {  # matches, DIFFERENT length -> reported
                "matched": True, "library_track_id": "t2",
                "library_title": "B", "library_track_number": "2",
                "library_duration_display": "3:00", "mb_duration_display": "4:00",
                "diff_fields": ["duration"],
            },
            {  # NOT matched (missing from the library) -> not a mismatch
                "matched": False, "library_track_id": "",
                "library_title": "", "library_track_number": "",
                "library_duration_display": "", "mb_duration_display": "4:00",
                "diff_fields": [],
            },
        ]
        checks = _duration_checks(comparison)
        assert len(checks) == 1
        assert checks[0]["track_id"] == "t2"
        assert checks[0]["library_duration"] == "3:00"
        assert checks[0]["mb_duration"] == "4:00"

    def test_duration_is_never_staged_as_a_writable_change(self):
        """A file's length cannot be written by a metadata import.

        ``duration`` is NOT in ``routes/ui_routes.py``'s ``_STAGED_WRITABLE``,
        so putting it in `changes` would render an "Included" toggle whose value
        is silently discarded on save.
        """
        from services.metadata import metadata_proposal_service as proposal

        spec_fields = {field for field, _label, _key in proposal._TRACK_FIELD_SPECS}
        assert "duration" not in spec_fields, (
            "duration must not be a staged field — it cannot be written"
        )
