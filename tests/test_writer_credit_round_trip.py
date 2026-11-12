"""The writer credit must survive DB → file → Navidrome import → DB unchanged.

REPORTED
--------
After a metadata scan, albums kept appearing as "needs resaving" because the
writer field was updated — with auto-apply enabled, so the same save ran again
on the next scan, forever. The album page showed mangled credits:

    Writer: Tyler Spencer, [Tyler Spencer, "Tyler Spencer"],
            Ben Began, Drew Peters, … — Tyler Spencer

ROOT CAUSE
----------
``tracks.writer`` is TEXT holding a JSON array (``["A", "B"]``). That value was
copied VERBATIM into the file tag, so the JSON brackets and quotes went on disk.
Navidrome echoed them back and the import split the string on commas, which cut
through the JSON syntax:

    ["Tyler Spencer", "Ben Began"]
      -> ["Tyler Spencer"      (leading bracket + quote)
      ->  "Ben Began"]        (trailing quote + bracket)

Those malformed names were then stored, written to the file, and re-split on the
next import — each pass deepening the damage. That is the self-sustaining loop
that made every scan report a writer change.

The fix has two halves, and BOTH are needed:
  1. ``build_tag_updates`` writes ONE comma-joined name string to the file.
  2. ``_normalize_people`` parses a JSON array instead of splitting it.
"""

from __future__ import annotations

import json

from services.metadata.tag_file_service import (
    _writer_names_text,
    build_tag_updates,
)
from services.scanning.metadata_extractor import (
    _extract_writers,
    _normalize_people,
)
from services.scanning.payload_builder import _ensure_json_string


# ---------------------------------------------------------------------------
# 1. The file tag is a NAME LIST, never a JSON array
# ---------------------------------------------------------------------------

class TestTheFileTagIsANameList:
    def test_a_json_array_writer_becomes_one_comma_string(self):
        """The reported corruption: the raw JSON went straight onto disk."""
        tags = build_tag_updates({"writer": json.dumps(["Tyler Spencer", "Ben Began"])})
        assert tags["writer"] == "Tyler Spencer, Ben Began"
        assert "[" not in tags["writer"] and '"' not in tags["writer"]

    def test_a_plain_string_is_left_as_names(self):
        tags = build_tag_updates({"writer": "Tyler Spencer, Ben Began"})
        assert tags["writer"] == "Tyler Spencer, Ben Began"

    def test_duplicate_names_collapse(self):
        tags = build_tag_updates({"writer": json.dumps(["Tyler Spencer", "Tyler Spencer"])})
        assert tags["writer"] == "Tyler Spencer"

    def test_a_slash_in_a_name_is_never_split(self):
        """``AC/DC`` is one artist; the write path must not destroy it."""
        tags = build_tag_updates({"writer": json.dumps(["AC/DC", "Ben Began"])})
        assert tags["writer"] == "AC/DC, Ben Began"

    def test_an_ampersand_name_is_never_split(self):
        tags = build_tag_updates({"writer": json.dumps(["Petters & Peters"])})
        assert tags["writer"] == "Petters & Peters"

    def test_missing_writer_is_not_invented(self):
        assert "writer" not in build_tag_updates({})


# ---------------------------------------------------------------------------
# 2. The import PARSES a JSON array instead of splitting it
# ---------------------------------------------------------------------------

class TestTheImportParsesAnArray:
    def test_a_json_array_is_read_as_names(self):
        assert _normalize_people('["Tyler Spencer", "Ben Began"]') == [
            "Tyler Spencer", "Ben Began",
        ]

    def test_a_nested_array_is_flattened_not_stringified(self):
        assert _normalize_people('[["A"], ["B"]]') == ["A", "B"]

    def test_a_plain_comma_string_still_splits(self):
        assert _normalize_people("Tyler Spencer, Ben Began") == [
            "Tyler Spencer", "Ben Began",
        ]

    def test_malformed_json_falls_back_to_text_splitting(self):
        """Garbage must not raise — it degrades to the old behaviour."""
        assert _normalize_people('["Tyler Spencer"') == ['["Tyler Spencer"']

    def test_the_reported_tag_round_trips_cleanly(self):
        """The exact value an earlier write put on disk."""
        extracted = _extract_writers({"writer": '["Tyler Spencer", "Ben Began"]'})
        stored = _ensure_json_string(json.dumps(extracted))
        assert json.loads(stored) == ["Tyler Spencer", "Ben Began"]


# ---------------------------------------------------------------------------
# 3. The whole round trip is a FIXED POINT
# ---------------------------------------------------------------------------

class TestTheRoundTripIsStable:
    def test_db_file_import_db_is_unchanged(self):
        """Scan → file → import → scan must not rewrite the writer forever."""
        db_column = json.dumps(["Tyler Spencer", "Ben Began"])

        file_tag = build_tag_updates({"writer": db_column})["writer"]
        imported = _extract_writers({"writer": file_tag})
        stored_again = _ensure_json_string(json.dumps(imported))

        assert stored_again == db_column, (
            "the writer changed across a round trip — every scan would report "
            f"a change: {db_column} -> {stored_again}"
        )

    def test_a_corrupted_value_is_repaired_and_stays_repaired(self):
        """Names already damaged on disk converge to the clean list."""
        corrupted = json.dumps(['["Tyler Spencer"', '"Tyler Spencer"]', "Ben Began"])

        first = build_tag_updates({"writer": corrupted})["writer"]
        assert first == "Tyler Spencer, Ben Began"

        # And the repaired form is a fixed point.
        assert build_tag_updates({"writer": _ensure_json_string(
            json.dumps(_extract_writers({"writer": first})))
        })["writer"] == first


# ---------------------------------------------------------------------------
# 4. Genuine names keep their decoration
# ---------------------------------------------------------------------------

class TestGenuineNamesSurvive:
    def test_a_bracketed_name_keeps_its_brackets(self):
        assert _writer_names_text(json.dumps(["[Unknown]"])) == "[Unknown]"

    def test_a_quoted_nickname_keeps_its_quotes(self):
        assert _writer_names_text(
            json.dumps(['Tyler "T-Bone" Spencer'])
        ) == 'Tyler "T-Bone" Spencer'

    def test_the_middle_element_of_a_split_array_loses_its_quotes(self):
        """``"Chris Peters"`` is JSON syntax, not part of the name."""
        assert _writer_names_text('"Chris Peters", Ben Began') == "Chris Peters, Ben Began"
