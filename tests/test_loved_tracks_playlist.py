"""The 'Loved Tracks' playlist must actually contain the hearted tracks.

Reported symptom: hearting tracks and building the playlist produced a playlist
with nothing in it. The cause was a query idiom that only works on the TEST
dialect:

    WHERE CAST(id AS TEXT) IN (SELECT value FROM json_each(:ids))

``json_each`` is SQLite's array deconstructor. PostgreSQL does have a function by
that name, but it is documented to *"expand the top-level JSON **object** into a
set of key/value pairs"*, and its ``value`` column is typed ``json`` — the docs'
own example prints ``"foo"``, quotes included. So against the array this code
passes, the result can neither be the bare ids nor compare to a ``TEXT`` column
(``text = json`` has no operator). SQLite's ``json_each`` returns the unquoted
scalar, so the idiom passed every test and disabled the feature in production.

These tests therefore assert TWO things: that the playlist is built from the
hearted tracks, and that the query the function actually executes is valid
PostgreSQL — because on the test dialect alone the broken version looked fine.
"""

from __future__ import annotations

import re

import pytest

MODULE = "services.playlists.playlist_service"


class _Recorded:
    """Captures every statement the function executes, with its parameters."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def compile_for_postgres(self) -> str:
        from sqlalchemy.dialects import postgresql

        statement, params = self.calls[-1]
        compiled = statement.params(**params).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"render_postcompile": True},
        )
        return " ".join(str(compiled).split())


class _FakeSession:
    def __init__(self, recorded: _Recorded):
        self._recorded = recorded

    def execute(self, statement, params=None):
        self._recorded.calls.append((statement, dict(params or {})))

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        return _Result(self._recorded.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Harness:
    """Runs the REAL function with a recording session and stubbed IO."""

    def __init__(self, monkeypatch):
        from services.playlists import playlist_service

        self._module = playlist_service
        self._monkeypatch = monkeypatch
        self.written = None
        self.recorded = None

    def run(self, hearted_ids, rows):
        from contextlib import contextmanager

        self.recorded = _Recorded(rows)
        recorded = self.recorded

        self._monkeypatch.setattr(
            "services.favourites_service.favourite_ids",
            lambda entity_type: list(hearted_ids),
        )

        @contextmanager
        def _session():
            yield _FakeSession(recorded)

        self._monkeypatch.setattr(self._module, "db_session", _session)

        def _write(name, data):
            self.written = (name, data)
            return True

        self._monkeypatch.setattr(self._module, "create_nsp_file", _write)
        self._monkeypatch.setattr(self._module, "playlist_path", lambda name: f"/pl/{name}.nsp")
        return self._module.create_or_update_loved_tracks_playlist()


@pytest.fixture()
def harness(monkeypatch):
    return _Harness(monkeypatch)


class TestTheLovedTracksPlaylist:
    def test_it_contains_the_hearted_tracks(self, harness):
        """⭐ The reported symptom, pinned."""
        result = harness.run(
            ["t1", "t2"],
            [("t1", "Hearted One", "An Artist"), ("t2", "Hearted Two", "An Artist")],
        )

        assert result["success"] is True
        assert result["track_count"] == 2
        name, data = harness.written
        assert name == "Loved Tracks"
        assert [t["title"] for t in data["tracks"]] == ["Hearted One", "Hearted Two"]
        assert [t["id"] for t in data["tracks"]] == ["t1", "t2"]

    def test_an_id_the_library_does_not_have_is_simply_absent(self, harness):
        """The query decides membership — an unknown id yields no row, not an error."""
        harness.run(["t1", "gone"], [("t1", "Hearted One", "An Artist")])

        _name, data = harness.written
        assert [t["id"] for t in data["tracks"]] == ["t1"]

    def test_no_hearted_tracks_does_not_touch_the_database(self, harness):
        """CONTROL: the early return must precede the query."""
        result = harness.run([], [])

        assert result == {"success": True, "track_count": 0, "note": "no hearted tracks"}
        assert harness.recorded.calls == []


class TestTheQueryIsValidPostgreSQL:
    """The test dialect is SQLite; production is PostgreSQL-only."""

    def test_the_executed_statement_has_no_json_each(self, harness):
        """⚠️ Asserted on the COMPILED SQL, not the source text.

        The comment above the query explains this trap by NAME, so a bare
        ``"json_each" not in source`` check would be satisfied by prose rather
        than by the code. The compiled statement contains neither.
        """
        harness.run(["t1"], [("t1", "T", "A")])
        sql = harness.recorded.compile_for_postgres()

        assert "json_each" not in sql
        assert "json_array_elements" not in sql

    def test_the_executed_statement_inlines_the_ids(self, harness):
        """An expanding bindparam renders one placeholder per id."""
        harness.run(["t1", "t2", "t3"], [])
        sql = harness.recorded.compile_for_postgres()

        placeholders = re.findall(r"%\(ids_\d+\)s", sql)
        assert len(placeholders) == 3, sql

    def test_the_parameter_is_a_list_not_a_json_string(self, harness):
        """The old form needed ``json.dumps``; a list is what the new one wants."""
        harness.run(["t1", "t2"], [])
        _statement, params = harness.recorded.calls[-1]

        assert params["ids"] == ["t1", "t2"]

    def test_no_python_file_deconstructs_an_in_list_with_json_each(self):
        """A repo-wide guard: this trap has now bitten two modules.

        ``db/repositories/playlists.py`` documented it after surviving it, and
        its guard test only ever checked that one file — which is exactly how
        the same idiom stayed in ``playlist_service``. This one walks every
        user Python file outside the tests, so a third occurrence cannot hide.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"old_system", "tests", ".venv", "node_modules"}:
                continue
            code = re.sub(r"#.*", "", path.read_text(encoding="utf-8", errors="replace"))
            if "json_each(" in code or "json_each (" in code:
                offenders.append(str(path.relative_to(root)))

        assert not offenders, (
            f"these use SQLite's array deconstructor, not PostgreSQL's: {offenders}"
        )
