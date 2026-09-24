"""The artist page shows upcoming (not-yet-released) releases.

Reported
--------
> I want missing releases on the artist page to also show releases that aren't
> out yet but listed as upcoming.

Two defects made that impossible:

1. **`is_upcoming` was computed but never READ.** `routes/ui_routes.py` attached
   it to every missing release, but the component the artist page actually
   renders (`components/_release_section.html`) has ZERO references to it. Only
   the ORPHANED `_album_category_section.html` reads the key, and that file is
   imported by `downloads/monitor.html` — not the artist page. So a
   not-yet-released album rendered as a plain "Missing" row.

2. **The `upcoming_releases` table was never read by the artist page at all.**
   That table is filled by an INDEPENDENT pipeline
   (`services/upcoming_releases/` — Wikipedia scraper + MusicBrainz fetcher)
   which drives the Upcoming Releases page. So an announcement already
   discovered there stayed invisible on the artist page until the scan happened
   to cache the same release group into `missing_releases`.

A third defect was found while fixing the first: see
``TestUpcomingIsDecidedFromTheDateNotTheYear``.

The rules now live in ``services/catalog/artist_release_entries`` as a PURE
function. They were previously inline in a ~600-line payload builder, reachable
only by driving the whole artist page (DB + MusicBrainz-adjacent lookups), which
is why none of them had a test.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.catalog.artist_release_entries import (
    build_missing_and_upcoming_entries,
)

#: Fixed "today" so the date rules can never flake as the real clock advances.
TODAY = date(2026, 9, 24)


def _missing(title, date_str, category="Album", **kw):
    return {
        "title": title,
        "release_id": kw.get("release_id", f"rg-{title.lower().replace(' ', '-')}"),
        "primary_type": kw.get("primary_type", "Album"),
        "first_release_date": date_str,
        "cover_art_url": kw.get("cover_art_url", ""),
        "category": category,
    }


def _upcoming(title, date_str=None, year=None, **kw):
    return {
        "album_name": title,
        "release_date": date_str,
        "release_year": year,
        "release_group_mbid": kw.get("mbid", f"rg-{title.lower().replace(' ', '-')}"),
        "primary_type": kw.get("primary_type", "Album"),
    }


def _by_title(entries):
    return {e["title"]: e for e in entries}


# ===========================================================================
# 1. Upcoming releases reach the page at all
# ===========================================================================

class TestUpcomingReleasesAreIncluded:

    def test_a_future_cached_release_is_marked_upcoming(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Next Album", "2027-03-01")],
            today=TODAY,
        )
        assert len(entries) == 1
        assert entries[0]["is_upcoming"] is True
        assert entries[0]["is_missing"] is True

    def test_a_past_cached_release_is_not_upcoming(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Old Album", "2019-01-01")],
            today=TODAY,
        )
        assert entries[0]["is_upcoming"] is False

    def test_upcoming_table_rows_are_merged_in(self):
        """The second pipeline's rows must reach the page."""
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Old Album", "2019-01-01")],
            upcoming_rows=[_upcoming("Announced Album", "2027-08-01")],
            today=TODAY,
        )
        titles = [e["title"] for e in entries]
        assert "Announced Album" in titles, (
            "the artist page still ignores the upcoming_releases table"
        )
        assert _by_title(entries)["Announced Album"]["is_upcoming"] is True

    def test_merged_rows_land_in_the_upcoming_category(self):
        entries = build_missing_and_upcoming_entries(
            upcoming_rows=[_upcoming("Announced Album", "2027-08-01")],
            today=TODAY,
        )
        assert entries[0]["_category"] == "upcoming"


# ===========================================================================
# 2. THE BUG FOUND WHILE FIXING IT: year-only comparison
# ===========================================================================

class TestUpcomingIsDecidedFromTheDateNotTheYear:
    """``year > now.year`` was wrong for most of the calendar.

    On 2026-09-24 a release dated 2026-12-05 compared ``2026 > 2026`` = False,
    so EVERY album still to come that year was labelled plain "Missing" — which
    is most of an artist's forthcoming output at any given moment.
    """

    def test_a_release_later_this_year_is_upcoming(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Out In December", "2026-12-05")],
            today=TODAY,
        )
        assert entries[0]["is_upcoming"] is True, (
            "a release dated 2026-12-05 must be upcoming on 2026-09-24; the old "
            "year-only test (2026 > 2026) said no"
        )

    def test_a_release_already_out_this_year_is_not_upcoming(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Out In January", "2026-01-15")],
            today=TODAY,
        )
        assert entries[0]["is_upcoming"] is False

    def test_a_partial_year_month_date_in_the_future_is_upcoming(self):
        """MusicBrainz publishes "2027-03" for an announced album."""
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Next Year", "2027-03")],
            today=TODAY,
        )
        assert entries[0]["is_upcoming"] is True

    def test_a_year_only_date_in_the_future_is_upcoming(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Some Year", "2027")],
            today=TODAY,
        )
        assert entries[0]["is_upcoming"] is True

    def test_today_itself_is_not_upcoming(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Out Today", "2026-09-24")],
            today=TODAY,
        )
        assert entries[0]["is_upcoming"] is False


# ===========================================================================
# 3. Undated rows are never promoted (the chosen policy)
# ===========================================================================

class TestUndatedReleasesAreNotPromoted:

    def test_an_undated_upcoming_row_is_skipped(self):
        entries = build_missing_and_upcoming_entries(
            upcoming_rows=[_upcoming("TBA Album", None)],
            today=TODAY,
        )
        assert entries == [], (
            "an announcement with no date must not be shown as upcoming — there "
            "is no date to justify it"
        )

    def test_an_undated_cached_release_is_not_upcoming(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Unknown Date", "")],
            today=TODAY,
        )
        assert entries[0]["is_upcoming"] is False

    def test_a_year_only_upcoming_row_still_classifies(self):
        """A year IS a date, so it qualifies — only a genuinely undated row does not."""
        entries = build_missing_and_upcoming_entries(
            upcoming_rows=[_upcoming("Year Only", None, year=2027)],
            today=TODAY,
        )
        assert len(entries) == 1
        assert entries[0]["is_upcoming"] is True


# ===========================================================================
# 4. De-duplication across two independent pipelines
# ===========================================================================

class TestOneEntryPerTitleAcrossBothSources:

    def test_a_release_in_both_tables_renders_once(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Same Release", "2027-05-01")],
            upcoming_rows=[_upcoming("Same Release", "2027-05-01")],
            today=TODAY,
        )
        assert [e["title"] for e in entries] == ["Same Release"]

    def test_the_cached_row_wins_because_it_has_art_and_an_id(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Same Release", "2027-05-01",
                                   cover_art_url="http://art/x.jpg",
                                   release_id="rg-cached")],
            upcoming_rows=[_upcoming("Same Release", "2027-05-01", mbid="rg-upcoming")],
            today=TODAY,
        )
        assert entries[0]["cover_art_url"] == "http://art/x.jpg"
        assert entries[0]["release_id"] == "rg-cached"

    def test_case_and_punctuation_do_not_defeat_the_dedupe(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Same Release", "2027-05-01")],
            upcoming_rows=[_upcoming("SAME  RELEASE!", "2027-05-01")],
            today=TODAY,
        )
        assert len(entries) == 1

    def test_an_already_owned_album_is_not_added_as_upcoming(self):
        """Otherwise the artist page shows an album as BOTH owned and missing."""
        entries = build_missing_and_upcoming_entries(
            upcoming_rows=[_upcoming("Owned Album", "2027-08-01")],
            owned_titles=["Owned Album"],
            today=TODAY,
        )
        assert entries == []

    def test_an_already_owned_album_is_not_missing(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Owned Album", "2019-01-01")],
            owned_titles=["Owned Album"],
            today=TODAY,
        )
        assert entries == []

    def test_owned_matching_ignores_case_and_punctuation(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Owned Album", "2019-01-01")],
            owned_titles=["owned  album!"],
            today=TODAY,
        )
        assert entries == []

    def test_duplicates_within_the_upcoming_table_render_once(self):
        entries = build_missing_and_upcoming_entries(
            upcoming_rows=[
                _upcoming("Dup", "2027-01-01"),
                _upcoming("Dup", "2027-06-01"),
            ],
            today=TODAY,
        )
        assert len(entries) == 1


# ===========================================================================
# 5. Upcoming wins over the release TYPE
# ===========================================================================

class TestUpcomingOverridesTheReleaseType:
    """Otherwise the Upcoming section is unreachable for the commonest case.

    An artist's next album is primary ``Album``, so classifying by type first
    would file every future studio album under "Studio Albums" and Upcoming
    would only ever hold oddities.
    """

    @pytest.mark.parametrize(
        "category,primary",
        [("Album", "Album"), ("EP", "EP"), ("Single", "Single"),
         ("Live Album", "Album"), ("Compilation", "Album")],
    )
    def test_a_future_release_goes_to_upcoming_whatever_its_type(self, category, primary):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Future Thing", "2027-04-01",
                                   category=category, primary_type=primary)],
            today=TODAY,
        )
        assert entries[0]["_category"] == "upcoming"

    def test_a_future_live_album_goes_to_upcoming(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Future Live", "2027-04-01",
                                   category="Live Album")],
            today=TODAY,
        )
        assert entries[0]["_category"] == "upcoming"

    def test_a_past_release_keeps_its_own_type_bucket(self):
        """The override must apply to UPCOMING only, not to everything."""
        entries = build_missing_and_upcoming_entries(
            missing_rows=[
                _missing("Past Studio", "2019-01-01", category="Album"),
                _missing("Past Live", "2019-01-01", category="Live Album"),
            ],
            today=TODAY,
        )
        got = {e["title"]: e["_category"] for e in entries}
        assert got["Past Studio"] == "album"
        assert got["Past Live"] == "live_album"

    def test_an_unknown_stored_category_never_becomes_studio(self):
        """The invariant the category registry exists to enforce."""
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("Odd Thing", "2019-01-01", category="Wibble")],
            today=TODAY,
        )
        assert entries[0]["_category"] != "album"


# ===========================================================================
# 6. Shaping and robustness
# ===========================================================================

class TestEntryShape:

    def test_entries_carry_the_keys_the_template_reads(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("An Album", "2027-05-01")],
            today=TODAY,
        )
        assert set(entries[0]) >= {
            "album", "title", "album_year", "track_count", "avg_stars",
            "total_duration", "is_missing", "is_upcoming",
            "first_release_date", "cover_art_url", "release_id", "_category",
        }

    def test_album_year_is_derived_from_the_date(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("An Album", "2027-05-01")],
            today=TODAY,
        )
        assert entries[0]["album_year"] == 2027

    def test_an_artist_with_nothing_gets_no_entries(self):
        assert build_missing_and_upcoming_entries(today=TODAY) == []

    def test_blank_titles_are_skipped(self):
        entries = build_missing_and_upcoming_entries(
            missing_rows=[_missing("", "2027-05-01"), _missing("Real", "2019-01-01")],
            upcoming_rows=[_upcoming("", "2027-05-01")],
            today=TODAY,
        )
        assert [e["title"] for e in entries] == ["Real"]

    def test_upcoming_rows_with_no_cover_art_do_not_invent_a_url(self):
        """A fabricated URL would request a file that does not exist."""
        entries = build_missing_and_upcoming_entries(
            upcoming_rows=[_upcoming("Announced", "2027-08-01")],
            today=TODAY,
        )
        assert entries[0]["cover_art_url"] == ""
