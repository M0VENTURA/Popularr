"""A single-disc release must beat a multi-disc one with the same tracklist.

REPORT

> "When doing a metadata scan, an album with multiple disks is still being
> selected over a digital release with the same number of tracks but only
> single disk"

ROOT CAUSE

There are TWO release selectors, and only one knew about discs.

``get_musicbrainz_best_release`` scores a release list and ALREADY prefers one
disc (+100, versus a 1000/track term that still dominates, so it can never
prefer an incomplete edition).

``_select_primary_release`` — the one the metadata scan uses, via
``_record_to_metadata`` — had no disc awareness at all. Among releases that
matched the album it took the EARLIEST DATE, so a 2-disc reissue from 1998 beat
a 1-disc digital edition from 2015 of the same album with the same tracklist.

The scan wants the single-disc edition, so the tie-break now runs before the
date.
"""

from __future__ import annotations

import pytest

from services.enrichment import musicbrainz_service as mbs
from services.enrichment.musicbrainz_service import _select_primary_release


def _release_disc_count(release) -> int:
    """Disc count read through the module, with a local fallback.

    Imported lazily so this module still COLLECTS against code that has not
    been fixed yet — a test that errors on import cannot report the behaviour
    it was written to guard.
    """
    helper = getattr(mbs, "_release_disc_count", None)
    if helper is not None:
        return helper(release)
    media = (release or {}).get("media")
    return len(media) if isinstance(media, list) and media else 0


def _disc_preference(release):
    helper = getattr(mbs, "_disc_preference", None)
    if helper is not None:
        return helper(release)
    count = _release_disc_count(release)
    if count == 1:
        return (0, count)
    if count > 1:
        return (2, count)
    return (1, 0)


def _rel(rel_id: str, title: str, date: str, discs: int | None) -> dict:
    release: dict = {
        "id": rel_id,
        "title": title,
        "date": date,
        "release-group": {"id": "rg-1", "primary-type": "Album",
                          "secondary-types": []},
    }
    if discs is not None:
        release["media"] = [{"track-count": 6} for _ in range(discs)]
    return release


class TestDiscCountsAreReadFromEverySpelling:
    def test_media_list(self) -> None:
        assert _release_disc_count(_rel("r", "A", "2000", 2)) == 2

    def test_medium_count_key(self) -> None:
        assert _release_disc_count({"medium-count": 3}) == 3

    def test_disc_count_key(self) -> None:
        assert _release_disc_count({"disc_count": 2}) == 2

    def test_unknown_is_zero(self) -> None:
        """0 means UNKNOWN — it must never be read as a one-disc release."""
        assert _release_disc_count({"id": "x"}) == 0
        assert _release_disc_count(None) == 0


class TestSingleDiscWinsTheTie:
    def test_the_reported_case(self) -> None:
        """2-disc dated 1998 must lose to 1-disc dated 2015."""
        releases = [
            _rel("r-multi", "The Album", "1998-05-01", 2),
            _rel("r-single", "The Album", "2015-03-01", 1),
        ]
        chosen = _select_primary_release(releases, "The Album")
        assert chosen["id"] == "r-single", (
            "the multi-disc release was still chosen — the reported bug"
        )

    def test_same_day_tie_also_goes_to_the_single(self) -> None:
        releases = [
            _rel("r-multi", "The Album", "1998-05-01", 2),
            _rel("r-single", "The Album", "1998-05-01", 1),
        ]
        assert _select_primary_release(releases, "The Album")["id"] == "r-single"

    def test_it_works_through_the_edition_path_too(self) -> None:
        """The edition-match branch honours the tie-break as well."""
        releases = [
            _rel("r-multi", "The Album (Deluxe)", "1998-05-01", 2),
            _rel("r-single", "The Album (Deluxe)", "1998-05-01", 1),
        ]
        chosen = _select_primary_release(releases, "The Album (Deluxe)")
        assert chosen["id"] == "r-single"

    def test_it_works_through_the_group_fallback_path(self) -> None:
        """No matching edition → the studio fallback also honours it."""
        releases = [
            _rel("r-multi", "Something Else", "1998-05-01", 2),
            _rel("r-single", "Something Else", "1998-05-01", 1),
        ]
        chosen = _select_primary_release(releases, None)
        assert chosen["id"] == "r-single"


class TestNothingElseChanged:
    def test_between_two_multi_disc_releases_the_date_still_decides(self) -> None:
        releases = [
            _rel("r-2", "The Album", "1998-05-01", 2),
            _rel("r-3", "The Album", "2005-05-01", 3),
        ]
        assert _select_primary_release(releases, "The Album")["id"] == "r-2"

    def test_unknown_counts_leave_the_date_ordering_alone(self) -> None:
        """A missing disc count is not evidence for a preference."""
        releases = [
            {"id": "r-a", "title": "The Album", "date": "1998-05-01",
             "release-group": {"id": "rg-1", "primary-type": "Album"}},
            {"id": "r-b", "title": "The Album", "date": "2015-03-01",
             "release-group": {"id": "rg-1", "primary-type": "Album"}},
        ]
        assert _select_primary_release(releases, "The Album")["id"] == "r-a"

    def test_a_known_single_disc_beats_an_unknown(self) -> None:
        """Unknown sorts BETWEEN, so it cannot shield a stale reissue."""
        releases = [
            {"id": "r-unknown", "title": "The Album", "date": "1990-01-01",
             "release-group": {"id": "rg-1", "primary-type": "Album"}},
            _rel("r-single", "The Album", "2015-03-01", 1),
        ]
        assert _select_primary_release(releases, "The Album")["id"] == "r-single"

    def test_a_single_candidate_is_returned_untouched(self) -> None:
        only = _rel("r-only", "The Album", "1998-05-01", 2)
        assert _select_primary_release([only], "The Album") is only

    def test_the_preference_ordering_is_single_then_unknown_then_multi(self) -> None:
        order = sorted(
            [{"media": [1, 2]}, {"media": [1]}, {}], key=_disc_preference
        )
        assert order[0] == {"media": [1]}
        assert order[1] == {}
        assert order[2] == {"media": [1, 2]}
