"""Navidrome import authority conflicts must ignore format-only differences.

Reported: repeated "metadata conflict — local correction preserved" on
``composer,releasestatus`` and ``composer,originaldate`` every import.

Cause: the file writer joins composers with ", " while Navidrome returns "; ";
release status case differed; and a file tag may hold ``2000`` while Navidrome
holds ``2000-05-23``. Those are the same values, not corrections.
"""

from __future__ import annotations

from services.scanning.reconciliation import (
    _dates_compatible,
    local_authority_conflicts,
)


class TestComposerCredits:
    def test_comma_vs_semicolon_is_not_a_conflict(self):
        existing = {"composer": "Billy Howerdel, Maynard James Keenan"}
        payload = {"composer": "Billy Howerdel; Maynard James Keenan"}
        assert local_authority_conflicts(existing, payload) == []

    def test_credit_order_is_not_a_conflict(self):
        existing = {"composer": "Maynard James Keenan, Billy Howerdel"}
        payload = {"composer": "Billy Howerdel; Maynard James Keenan"}
        assert local_authority_conflicts(existing, payload) == []

    def test_a_different_composer_is_still_a_conflict(self):
        existing = {"composer": "Billy Howerdel"}
        payload = {"composer": "Billy Howerdel; Trent Reznor"}
        assert local_authority_conflicts(existing, payload) == ["composer"]

    def test_an_empty_provider_credit_is_not_a_conflict(self):
        assert local_authority_conflicts(
            {"composer": "Billy Howerdel"},
            {"composer": ""},
        ) == []

    def test_record_label_separator_is_not_a_conflict(self):
        existing = {"recordlabel": "Virgin, EMI"}
        payload = {"recordlabel": "Virgin; EMI"}
        assert local_authority_conflicts(existing, payload) == []


class TestReleaseStatus:
    def test_case_only_is_not_a_conflict(self):
        assert local_authority_conflicts(
            {"releasestatus": "Official"},
            {"releasestatus": "official"},
        ) == []

    def test_a_different_status_is_a_conflict(self):
        assert local_authority_conflicts(
            {"releasestatus": "Official"},
            {"releasestatus": "Bootleg"},
        ) == ["releasestatus"]

    def test_an_empty_provider_status_is_not_a_conflict(self):
        assert local_authority_conflicts(
            {"releasestatus": "Official"},
            {"releasestatus": ""},
        ) == []


class TestAlbumArtist:
    def test_composite_va_and_plain_va_are_the_same_identity(self):
        assert local_authority_conflicts(
            {"album_artist": "Various Artists • HIM"},
            {"album_artist": "Various Artists"},
        ) == []

    def test_a_single_performer_is_still_a_different_album_artist(self):
        assert local_authority_conflicts(
            {"album_artist": "HIM"},
            {"album_artist": "Various Artists"},
        ) == ["album_artist"]

    def test_case_only_difference_is_not_a_conflict(self):
        assert local_authority_conflicts(
            {"album_artist": "Various Artists"},
            {"album_artist": "various artists"},
        ) == []


class TestOriginalDate:
    def test_year_vs_full_date_of_the_same_year_is_not_a_conflict(self):
        assert local_authority_conflicts(
            {"originaldate": "2000"},
            {"originaldate": "2000-05-23"},
        ) == []

    def test_month_disagreement_is_a_conflict(self):
        assert local_authority_conflicts(
            {"originaldate": "2000-04-23"},
            {"originaldate": "2000-05-23"},
        ) == ["originaldate"]

    def test_day_disagreement_is_a_conflict(self):
        assert local_authority_conflicts(
            {"originaldate": "2000-05-22"},
            {"originaldate": "2000-05-23"},
        ) == ["originaldate"]

    def test_year_disagreement_is_a_conflict(self):
        assert local_authority_conflicts(
            {"originaldate": "1999"},
            {"originaldate": "2000"},
        ) == ["originaldate"]

    def test_the_reported_combined_case_clears(self):
        existing = {
            "composer": "Billy Howerdel, Maynard James Keenan",
            "releasestatus": "Official",
        }
        payload = {
            "composer": "Billy Howerdel; Maynard James Keenan",
            "releasestatus": "official",
        }
        assert local_authority_conflicts(existing, payload) == []

    def test_date_helper(self):
        assert _dates_compatible("2000", "2000-05-23") is True
        assert _dates_compatible("2000-05", "2000-05-23") is True
        assert _dates_compatible("2000-00-00", "2000-05-23") is True
        assert _dates_compatible("2000-05-23", "2000-05-24") is False
