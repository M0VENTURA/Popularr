"""Lookup MBID must review every album field against what the album ACTUALLY holds.

Reported
--------
> Can you confirm during the comparison when doing the lookup mbid, that all
> these fields are being correctly checked and reviewed? Some seem to be seen
> as empty even when there is information in them.

Why some fields read "(empty)"
------------------------------
``_album_level_proposals`` built its **current** values from
``local_tracks[0]`` — the FIRST track row only. Album-level values are *meant*
to be duplicated onto every row, but they routinely are not (a row imported
later, a partial re-tag, a scan that filled some rows), so the review showed
``(empty)`` for a field the album page was visibly displaying — and then
proposed a value the album already had, because
``_norm(proposed) == _norm(current)`` could not match an empty string.

Now: :func:`_first_present` — the first non-empty value across **every** row,
row-major then the field's own key order, which is the algorithm the album
page's ``first_value`` uses, so the review and the form cannot disagree.

Coverage
--------
The last class answers the other half of the question: every field the Edit
Album page shows is in ``_ALBUM_FIELD_SPECS`` (or handled as
``album_genres``), and every MusicBrainz key it names is actually produced by
``musicbrainz_service`` — a spec key nothing emits could never propose.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MB_SERVICE = REPO_ROOT / "services" / "enrichment" / "musicbrainz_service.py"

from services.metadata import metadata_proposal_service as _proposal  # noqa: E402

_ALBUM_FIELD_SPECS = _proposal._ALBUM_FIELD_SPECS
_album_level_proposals = _proposal._album_level_proposals


def _first_present(rows: list[dict], keys) -> str:
    """The shipped helper when it exists, else the OLD row-0-only read.

    Imported LAZILY on purpose: ``_first_present`` is NEW, so a module-scope
    ``from ... import _first_present`` turns this whole file into a COLLECTION
    ERROR on an unpatched tree and hides every other verdict — the lesson
    recorded after the diacritic and cover sessions.
    """
    shipped = getattr(_proposal, "_first_present", None)
    if shipped is not None:
        return shipped(rows, keys)
    head = rows[0] if rows else {}
    for key in keys:
        value = str(head.get(key) or "").strip()
        if value:
            return value
    return ""


def _rows(*rows: dict) -> list[dict]:
    """Build rows from one dict per row (positional — ``0=`` is not syntax)."""
    return [dict(row) for row in rows]


class TestFirstPresent:
    def test_finds_a_value_on_a_later_row(self):
        rows = _rows({"recordlabel": ""}, {"recordlabel": "Universal Republic Records"})
        assert _first_present(rows, ("recordlabel",)) == "Universal Republic Records"

    def test_an_empty_everywhere_returns_empty(self):
        assert _first_present(_rows({"recordlabel": ""}, {}), ("recordlabel",)) == ""

    def test_row_order_beats_key_order(self):
        """Same shape the album page's ``first_value`` has."""
        rows = _rows(
            {"releasetype": "album"},
            {"musicbrainz_albumtype": "album+live"},
        )
        assert _first_present(
            rows, ("spotify_album_type", "musicbrainz_albumtype", "releasetype")
        ) == "album"

    def test_a_later_row_fills_a_key_no_earlier_row_had(self):
        rows = _rows({"releasetype": "album"}, {"musicbrainz_albumtype": "album+live"})
        assert _first_present(
            rows, ("spotify_album_type", "musicbrainz_albumtype")
        ) == "album+live"

    def test_whitespace_only_counts_as_empty(self):
        rows = _rows({"barcode": "   "}, {"barcode": "00602527499406"})
        assert _first_present(rows, ("barcode",)) == "00602527499406"


class TestTheReviewReadsTheWholeAlbum:
    def _metadata(self, **overrides) -> dict:
        return {"recordlabel": "Universal Republic Records", **overrides}

    def test_a_value_only_on_a_later_row_is_not_reported_as_empty(self):
        """THE REPORT. Row 0 is blank, row 2 carries the label."""
        rows = _rows(
            {"album": "Feeding the Wolves", "recordlabel": ""},
            {"album": "Feeding the Wolves"},
            {"album": "Feeding the Wolves", "recordlabel": "Universal Republic Records"},
        )
        proposals = _album_level_proposals(rows, self._metadata())
        by_field = {p["field"]: p for p in proposals}

        assert "album_recordlabel" not in by_field, (
            "the review proposed a label the album already holds, because its "
            "current read stopped at row 0"
        )

    def test_a_real_change_still_reports_the_found_value(self):
        rows = _rows({"recordlabel": ""}, {"recordlabel": "Universal Republic Records"})
        proposals = _album_level_proposals(rows, {"recordlabel": "Some Other Label"})
        change = next(p for p in proposals if p["field"] == "album_recordlabel")

        assert change["current"] == "Universal Republic Records", (
            f"a change must be shown as what the album has -> what MB says, got {change}"
        )
        assert change["proposed"] == "Some Other Label"

    def test_every_reported_field_reads_across_rows(self):
        """Each spec field finds its value wherever the album stored it.

        Row 0 carries NOTHING — every value lives on row 1, which is exactly
        the shape that used to read "(empty)".
        """
        expected = {
            "album_title": ("album", "Feeding the Wolves"),
            "album_artist": ("album_artist", "10 Years"),
            "album_release_title": ("release_title", "Feeding the Wolves"),
            "album_originalyear": ("year", "2010"),
            "release_year": ("release_year", "2010"),
            "album_type": ("releasetype", "album"),
            "album_mbid": ("musicbrainz_album_mbid", "ca3e85c9-130e-449f-8aa3-801930abadac"),
            "album_release_group_mbid": (
                "musicbrainz_releasegroupid",
                "a9d1fe24-9080-454f-b140-7287ccab608c",
            ),
            "artist_mbid": ("musicbrainz_artistid", "b18bc9c4-6f22-4f11-a918-e9c86a39fe7a"),
            "album_recordlabel": ("recordlabel", "Universal Republic Records"),
            "album_catalognumber": ("catalognumber", "00602527499406"),
            "album_barcode": ("barcode", "00602527499406"),
            "album_releasedate": ("releasedate", "2010-08-31"),
            "album_media": ("media", "Digital Media"),
            "album_releasecountry": ("releasecountry", "AG"),
        }
        rows = _rows(
            {"id": "row-0"},
            {column: value for column, value in expected.values()},
        )
        # A DIFFERENT value for every MusicBrainz key, so each field is
        # guaranteed to be proposed and therefore to carry a "current".
        metadata = {mb_key: f"MB:{mb_key}" for _f, _l, mb_key in _ALBUM_FIELD_SPECS}

        proposals = _album_level_proposals(rows, metadata)
        by_field = {p["field"]: p for p in proposals}

        for form_field, (_column, value) in expected.items():
            change = by_field.get(form_field)
            assert change is not None, f"{form_field} was never reviewed"
            assert change["current"] == value, (
                f"{form_field} reads {change['current']!r} although row 1 holds "
                f"{value!r}"
            )


class TestTheSpecCoversTheAlbumPage:
    def test_every_field_shown_on_the_album_page_is_reviewed(self):
        covered = {fid for fid, _label, _key in _ALBUM_FIELD_SPECS} | {"album_genres"}
        shown = [
            "album_title", "album_artist", "album_release_title",
            "album_originalyear", "release_year", "album_type",
            "album_mbid", "album_release_group_mbid", "artist_mbid",
            "album_genres",
            "album_recordlabel", "album_catalognumber", "album_barcode",
            "album_releasedate", "album_media", "album_releasecountry",
        ]
        missing = [f for f in shown if f not in covered]
        assert not missing, f"never checked by Lookup MBID: {missing}"

    def test_every_musicbrainz_key_the_spec_names_is_actually_produced(self):
        """A spec key nothing emits could never propose anything."""
        src = MB_SERVICE.read_text(encoding="utf-8")
        absent = [
            f"{form_field} -> {mb_key}"
            for _form_field, _label, mb_key in _ALBUM_FIELD_SPECS
            if f'"{mb_key}"' not in src and f"'{mb_key}'" not in src
        ]
        assert not absent, f"musicbrainz_service never produces: {absent}"
