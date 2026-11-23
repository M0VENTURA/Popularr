"""The album review must propose Composer and Lyricist.

Both were already first-class: the scanner fills the columns
(``metadata_extractor._composer_credit``), MusicBrainz publishes them per
recording from the work's composer/lyricist relations, and the file writer emits
TCOM / the Lyricist frame. The REVIEW simply never offered them, so a wrong
credit could not be corrected from the album page at all.

⚠️ They must also compare as SETS OF PEOPLE, not as strings. The Navidrome import
already does (``reconciliation._PEOPLE_FIELDS`` joins each side with "|" and
compares), so a review that compared raw text would disagree with the code that
consumes the value — and would propose a reordered credit forever.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from services.metadata import metadata_proposal_service as mps
from services.metadata.metadata_proposal_service import _track_proposals

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

MB_COMPOSER = "Korn, Brian Welch, David Silveria"
MB_LYRICIST = "Brian Welch; David Silveria"

CMP = [{
    "library_track_id": "t1", "matched": True, "mb_recording_mbid": "rec-1",
    "mb_title": "Here to Stay", "mb_track_number": "1", "mb_disc_number": "",
}]
METADATA = {
    "release_group_genres": [], "release_genres": [],
    "tracks": [{
        "mb_recording_mbid": "rec-1",
        "composer": MB_COMPOSER, "lyricist": MB_LYRICIST,
        "writer": "Korn", "musicbrainz_genres": "",
    }],
}


def _changes(**local_overrides) -> dict[str, tuple[str, str]]:
    row = {
        "id": "t1", "title": "Here to Stay", "artist": "Korn",
        "album_artist": "Korn", "album": "Untouchables",
        "track_number": "1", "disc_number": None,
        "composer": MB_COMPOSER, "lyricist": MB_LYRICIST, "writer": "Korn",
        "musicbrainz_genres": [],
    }
    row.update(local_overrides)
    found: dict[str, tuple[str, str]] = {}
    for entry in _track_proposals([row], CMP, METADATA):
        for change in entry.get("changes") or []:
            found[change["field"]] = (change["current"], change["proposed"])
    return found


class TestTheFieldsAreOffered:
    @pytest.mark.parametrize(("field", "label"), [("composer", "Composer"),
                                                 ("lyricist", "Lyricist")])
    def test_each_is_in_the_track_specs(self, field: str, label: str) -> None:
        specs = {f: lbl for f, lbl, _k in mps._TRACK_FIELD_SPECS}
        assert specs.get(field) == label

    def test_the_save_may_write_them(self) -> None:
        """A proposal the save silently drops is worse than no proposal."""
        src = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
        start = src.index("_STAGED_WRITABLE = frozenset({")
        block = src[start:src.index("})", start)]
        assert '"composer"' in block and '"lyricist"' in block, (
            "the staged review can propose these but the whitelist would drop them"
        )


class TestOnlyARealDifferenceIsProposed:
    def test_a_wrong_credit_is_proposed(self) -> None:
        changes = _changes(composer="Someone Else")
        assert "composer" in changes
        assert changes["composer"][1] == MB_COMPOSER

    def test_an_agreeing_credit_is_silent(self) -> None:
        changes = _changes()
        assert "composer" not in changes
        assert "lyricist" not in changes

    def test_a_reorder_is_not_a_change(self) -> None:
        """The import compares name SETS, so the review must too."""
        assert "composer" not in _changes(composer="David Silveria, Korn, Brian Welch")

    def test_a_different_delimiter_is_not_a_change(self) -> None:
        assert "lyricist" not in _changes(lyricist="Brian Welch; David Silveria")

    def test_a_missing_name_still_reports(self) -> None:
        assert "composer" in _changes(composer="Korn, Brian Welch")

    def test_an_empty_credit_is_not_silently_equal(self) -> None:
        """Nothing stored vs a real credit IS a change."""
        assert "composer" in _changes(composer="")


class TestItMatchesHowTheImportJudgesThem:
    def test_the_import_treats_them_as_people_fields(self) -> None:
        from services.scanning.reconciliation import _PEOPLE_FIELDS

        assert {"composer", "lyricist"} <= set(_PEOPLE_FIELDS), (
            "the import compares these as name sets; the review must agree"
        )

    def test_the_review_compares_them_as_sets(self) -> None:
        assert {"composer", "lyricist", "writer"} <= set(mps._CREDIT_FIELDS)
        assert mps._credit_sets_equal("Korn, Brian Welch", "Brian Welch; Korn") is True
        assert mps._credit_sets_equal("Korn", "Korn, Brian Welch") is False
