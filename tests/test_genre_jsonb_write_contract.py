"""Raw-SQL genre writers must not hand a JSONB column a CSV string.

``tracks.genres`` is TEXT, but ``tracks.manual_genres`` is **JSONB**. Several
call sites write both columns in one hand-written ``UPDATE``, passing the SAME
comma-separated string to each:

    UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id
                                                ^^^^^^^^^^^^ JSONB, given CSV

``"Hardcore, Punk"`` is not valid JSON, so PostgreSQL rejects the statement with
``invalid input syntax for type json``.

⚠️ This module exists because an earlier fix was **overclaimed**. The JSON
coercion added to ``db/repositories/popularity_repository.coerce_track_value_for_pg_type``
only runs inside ``save_to_db`` — it is on the ``insert_or_update_track`` path.
Every site below uses **raw SQL**, so it bypasses that coercion entirely. The
bug was therefore still live in:

  * ``db/repositories/metadata.update_track_genres``  (the album-genres box)
  * ``routes/misc_routes.py``  (genre add/remove + track-tag merge)
  * ``services/metadata/album_service.py``  (bulk tag write)

The same columns are equally broken on the way OUT: psycopg returns a JSONB
column as a Python ``list``, so the read-side helpers that do
``raw.replace("\\\\", ",").split(",")`` raise ``AttributeError``, and
``str(row.get("manual_genres"))`` silently yields the Python repr
``"['a', 'b']"`` — braces, quotes and all — which then gets written back.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

RAW_WRITE_FILES = [
    "db/repositories/metadata.py",
    "routes/misc_routes.py",
    "services/metadata/album_service.py",
]

#: ``manual_genres`` is JSONB everywhere it appears on ``tracks``.
JSONB_GENRE_COLUMNS = ("manual_genres",)


# ---------------------------------------------------------------------------
# 1. The shared helper
# ---------------------------------------------------------------------------

class TestTheSharedHelper:
    """``coerce_json_value`` is the ONE place a value becomes JSON text."""

    def test_csv_becomes_a_json_array(self):
        from db.repositories.popularity_repository import coerce_json_value

        assert coerce_json_value("Rock") == '["Rock"]'
        assert coerce_json_value("Hardcore, Punk") == '["Hardcore", "Punk"]'

    def test_it_matches_the_column_coercer(self):
        """The helper and the column path must agree — one splitter, not two."""
        from db.repositories.popularity_repository import (
            coerce_json_value,
            coerce_track_value_for_pg_type,
        )

        for raw in ("Rock", "Hardcore, Punk", "", None, ["a"], '["a","b"]'):
            assert coerce_json_value(raw) == coerce_track_value_for_pg_type(
                "manual_genres", raw, "JSONB"
            ), raw

    def test_empty_becomes_an_empty_array_not_null(self):
        from db.repositories.popularity_repository import coerce_json_value

        assert coerce_json_value("") == "[]"
        assert coerce_json_value(None) is None


# ---------------------------------------------------------------------------
# 2. No raw SQL may pass a CSV string to a JSONB genre column
# ---------------------------------------------------------------------------

class TestRawSqlWritersCoerceJsonbGenres:
    """A source-contract guard: each raw UPDATE must coerce, not pass through."""

    @pytest.mark.parametrize("rel", RAW_WRITE_FILES)
    def test_every_manual_genres_write_is_coerced(self, rel: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")

        # Find each raw UPDATE that touches manual_genres.
        offenders = []
        for match in re.finditer(r"UPDATE tracks\b", source):
            window = source[match.start(): match.start() + 400]
            if "manual_genres" not in window:
                continue
            # The statement's parameters must include a coerced value.
            if "coerce_json_value" not in window and "to_json_text" not in window:
                # A literal '[]' / NULL assignment is fine; a bare :manual is not.
                if re.search(r"manual_genres\s*=\s*:", window):
                    offenders.append(window.splitlines()[1].strip() if "\n" in window else window[:80])

        assert not offenders, (
            f"{rel}: a raw UPDATE assigns a placeholder straight to the JSONB "
            f"column manual_genres without coercing it:\n  " + "\n  ".join(offenders)
        )


# ---------------------------------------------------------------------------
# 3. Read-side helpers must accept the list psycopg returns for JSONB
# ---------------------------------------------------------------------------

class TestReadSideHandlesJsonbLists:
    """A JSONB column comes back as a Python list, not a string.

    ``_split_genres(raw)`` doing ``raw.replace(...).split(...)`` raises
    ``AttributeError`` on a list, and ``str([...])`` produces ``"['a', 'b']"``
    — a Python repr that then gets stored. Both must be handled.
    """

    def _helpers(self, rel: str, names: tuple[str, ...]):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        found = {}
        for name in names:
            if f"def {name}(" in source:
                found[name] = source
        return found

    def test_album_service_does_not_stringify_the_manual_grades_column(self):
        source = (REPO_ROOT / "services/metadata/album_service.py").read_text(encoding="utf-8")
        # str(list) yields a Python repr, which is not valid JSON.
        assert "str(row.get(\"manual_genres\")" not in source, (
            "str() on a JSONB list produces a Python repr, not JSON"
        )

    def test_genre_splitting_accepts_a_list(self):
        """The shared splitter must take both a CSV string and a list."""
        from db.repositories.popularity_repository import parse_genre_value

        assert parse_genre_value("Hardcore, Punk") == ["Hardcore", "Punk"]
        assert parse_genre_value(["Hardcore", "Punk"]) == ["Hardcore", "Punk"]
        assert parse_genre_value(None) == []
        assert parse_genre_value("") == []
        assert parse_genre_value('["Hardcore"]') == ["Hardcore"]
        # A "\\"-separated legacy value must still split (old tag convention).
        assert parse_genre_value("Hardcore\\Punk") == ["Hardcore", "Punk"]
