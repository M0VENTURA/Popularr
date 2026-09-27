"""The scan's album filter must recognise the album the user clicked.

REPORTED: "In Step 1, the scanner attempted to pull the album from Navidrome,
but the album title didn't match. Navidrome sees one Ricky Martin album in your
library, but it isn't named exactly 17: Greatest Hits (it might be tagged as
just 17 or Greatest Hits). As a result, it skipped importing the tracks."

ROOT CAUSE: the filter was EXACT string equality, first at
``services/scanning/filters.py::should_skip_album`` and again at
``services/popularity/stages/load_stage.py``. A page title and Navidrome's own
album name legitimately differ, so the album was skipped and reported as a
mismatch that did not exist.

⚠️ THE DANGEROUS DIRECTION IS OVER-MATCHING. Matching a DIFFERENT album is worse
than matching none: the import would pull the wrong tracks and the album
cleanup would run against them. So every tolerance is anchored on a SEPARATOR,
and the tests below spend as much effort proving what must NOT match.
"""
from __future__ import annotations

import pytest

from helpers.normalization_service import album_name_key, album_names_match
from services.scanning.filters import should_skip_album


def _skipped(album_name, album_filter, **overrides):
    kwargs = dict(
        album_name=album_name,
        album_filter=album_filter,
        filter_missing=False,
        albums_needing_reimport=set(),
        diff_mode=False,
        changed_album_names=None,
    )
    kwargs.update(overrides)
    return should_skip_album(**kwargs)


class TestTheReportedCaseIsNoLongerSkipped:
    """The exact failure the user described."""

    def test_a_navidrome_short_title_matches_the_longer_requested_one(self):
        assert _skipped("17", "17: Greatest Hits") is False, (
            "Navidrome's '17' IS the album the page calls '17: Greatest Hits'"
        )

    def test_a_navidrome_subtitle_only_title_matches(self):
        assert _skipped("Greatest Hits", "17: Greatest Hits") is False, (
            "the user's second suggested spelling ('Greatest Hits') must match too"
        )

    def test_the_exact_name_still_matches(self):
        assert _skipped("17: Greatest Hits", "17: Greatest Hits") is False

    def test_a_case_and_spacing_difference_is_not_a_mismatch(self):
        assert _skipped("absolution", "Absolution") is False
        assert _skipped("  Absolution  ", "Absolution") is False


class TestAGenuinelyDifferentAlbumIsStillSkipped:
    """The safety direction — this must not become a match-anything filter."""

    @pytest.mark.parametrize("navidrome,requested", [
        ("Absolution", "Origin of Symmetry"),
        ("Showbiz", "Absolution"),
        ("18: Greatest Hits", "17: Greatest Hits"),
        ("Greatest Hits, Vol 2", "17: Greatest Hits"),
        ("Greatest Hits Live", "17: Greatest Hits"),
        ("17 Again", "17: Greatest Hits"),
        ("17: Greatest Hits II", "17: Greatest Hits"),
    ])
    def test_a_different_album_is_skipped(self, navidrome, requested):
        assert _skipped(navidrome, requested) is True

    def test_a_space_boundary_is_not_enough(self):
        """⚠️ "Absolution" must not match "Absolution II" / "Absolution Live".

        These are different releases. Allowing a bare-space cut is the obvious
        way to get this wrong, because it would also swallow "Absolution II".
        """
        assert album_names_match("Absolution", "Absolution II") is False
        assert album_names_match("Absolution", "Absolution Live") is False
        assert _skipped("Absolution II", "Absolution") is True

    def test_a_word_internal_hyphen_is_not_a_boundary(self):
        assert album_names_match("Spider", "Spider-Man") is False
        assert album_names_match("Re", "Re-Animator") is False


class TestSeparatorFormsAgree:
    """Real-world spellings of the SAME album must all match."""

    @pytest.mark.parametrize("candidate", [
        "17: Greatest Hits",
        "17 - Greatest Hits",
        "17 – Greatest Hits",
        "17 (Greatest Hits)",
        "17. Greatest Hits",
        "17: greatest hits",
        "Seventeen 17: Greatest Hits",  # not equal — see the explicit check below
    ])
    def test_separator_variants(self, candidate):
        expected = candidate != "Seventeen 17: Greatest Hits"
        assert album_names_match("17: Greatest Hits", candidate) is expected

    def test_an_edition_marker_is_ignored(self):
        assert album_names_match(
            "17: Greatest Hits", "17: Greatest Hits (Deluxe Edition)"
        ) is True
        assert album_names_match("Showbiz", "Showbiz (Deluxe)") is True

    def test_empty_inputs_never_match(self):
        assert album_names_match("", "") is False
        assert album_names_match("Absolution", "") is False
        assert album_names_match("", "Absolution") is False


class TestMultipleMatchesArePossible:
    """⚠️ The tolerance CAN identify more than one album.

    Documented as a test because callers must not assume a single hit: the
    Navidrome import imports every match and logs the ambiguity, rather than
    silently picking one.
    """

    def test_a_short_request_matches_every_numbered_sibling(self):
        fleet = ["17: Greatest Hits", "18: Greatest Hits"]
        matched = [a for a in fleet if album_names_match("Greatest Hits", a)]
        assert matched == fleet, (
            "asking for the bare 'Greatest Hits' genuinely matches both; the "
            "fix must not pretend otherwise"
        )

    def test_a_numbered_request_still_selects_exactly_one(self):
        fleet = ["17: Greatest Hits", "18: Greatest Hits"]
        matched = [a for a in fleet if album_names_match("17: Greatest Hits", a)]
        assert matched == ["17: Greatest Hits"], (
            "the numbered case, which is the reported one, must stay unambiguous"
        )


class TestTheKeyShape:
    def test_the_key_collapses_separators_to_one_sentinel(self):
        assert album_name_key("17: Greatest Hits") == album_name_key("17 - Greatest Hits")
        assert album_name_key("17 (Greatest Hits)") == album_name_key("17 - Greatest Hits")

    def test_the_key_keeps_a_word_internal_hyphen(self):
        assert album_name_key("Spider-Man") == "spider-man"


class TestBothFilterSitesUseTheSameRule:
    """Wiring: an exact comparison at EITHER site re-breaks the reported case.

    The two gate one album scan in sequence — Navidrome import (step 1), then
    the popularity scan (step 2) — so a strict comparison in the second would
    re-skip the album the first just accepted, leaving nothing to score.
    """

    def test_the_scanning_filter_uses_the_shared_rule(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "services" / "scanning" / "filters.py"
        ).read_text(encoding="utf-8")
        assert "album_names_match(album_filter, album_name)" in src, (
            "should_skip_album must use the tolerant rule"
        )
        assert "album_name.strip() != album_filter.strip()" not in src, (
            "the exact comparison is the defect; it must not survive"
        )

    def test_the_load_stage_uses_the_shared_rule(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "services" / "popularity" / "stages" / "load_stage.py"
        ).read_text(encoding="utf-8")
        assert "album_names_match(album_filter, album)" in src, (
            "the popularity load stage must use the same rule, or it re-skips "
            "the album the Navidrome import just accepted"
        )
        assert "album.lower().strip() != album_filter.lower().strip()" not in src, (
            "the exact comparison is the defect; it must not survive"
        )

    def test_the_import_reports_an_ambiguous_filter(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "services" / "scanning" / "navidrome_import.py"
        ).read_text(encoding="utf-8")
        assert "_albums_matched_filter > 1" in src, (
            "a filter matching several albums must be reported rather than "
            "silently importing all of them"
        )
