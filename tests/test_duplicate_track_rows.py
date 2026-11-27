"""The same FILE listed as two tracks.

Reported
--------
> One track is showing twice — "All Day and All of the Night" — both rows with
> the same /music/Various Artists/2003 - Battlefield Vietnam/... file path.

Two rows can share one path when a re-import's metadata no longer matches the
stored row (the MP3 import used to match on title/artist/album only). That is a
bookkeeping duplicate, not a second copy of the song, so:

* the album page must show ONE row per file;
* the duplicate detector must not call it "downloaded more than once";
* an import of a path that is already known must UPDATE that row;
* deleting one row must never remove the file the surviving row still uses.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from db.repositories.tracks import dedupe_track_rows_by_file_path
from services.metadata import album_missing_service as ams
from services.metadata import artist_service as arts
from services.scanning import mp3_import_scanner as mp3


class TestDedupeRowsByFilePath:
    def test_one_path_renders_once_and_keeps_the_most_complete_row(self) -> None:
        rows = [
            {"id": "1", "file_path": "/music/A.mp3", "title": "A"},
            {
                "id": "2",
                "file_path": "/MUSIC/a.mp3",
                "title": "A",
                "duration": 225,
                "recording_mbid": "rec-1",
                "last_scanned": "2026-11-20",
            },
        ]
        out = dedupe_track_rows_by_file_path(rows)
        assert [r["id"] for r in out] == ["2"], out

    def test_separator_spellings_are_the_same_file(self) -> None:
        """Writers disagree about ``\\`` vs ``/``; both name one file."""
        out = dedupe_track_rows_by_file_path([
            {"id": "1", "file_path": r"C:\Music\Artist\Album\01 Track.mp3"},
            {"id": "2", "file_path": "C:/Music/Artist/Album/01 Track.mp3"},
        ])
        assert len(out) == 1, out

    def test_different_paths_are_still_different_tracks(self) -> None:
        out = dedupe_track_rows_by_file_path([
            {"id": "1", "file_path": "/music/a.mp3"},
            {"id": "2", "file_path": "/music/b.mp3"},
        ])
        assert [r["id"] for r in out] == ["1", "2"]

    def test_queued_placeholders_and_blanks_never_collapse(self) -> None:
        """A placeholder is not a file, and two empty paths are not one file."""
        out = dedupe_track_rows_by_file_path([
            {"id": "1", "file_path": "__queued_for_download__a"},
            {"id": "2", "file_path": "__queued_for_download__b"},
            {"id": "3", "file_path": ""},
            {"id": "4", "file_path": None},
        ])
        assert [r["id"] for r in out] == ["1", "2", "3", "4"]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return _Result(self._rows)


@contextmanager
def _fake_db(rows):
    yield _Session(rows)


def _row(**overrides):
    row = {
        "id": "1",
        "album": "Battlefield Vietnam",
        "title": "All Day and All of the Night",
        "track_number": "10",
        "disc_number": "1",
        "track_artist": "The Kinks",
        "file_path": "/music/Various Artists/2003 - Battlefield Vietnam/10. Track.mp3",
        "duration": 225,
        "stars": 3,
        "recording_mbid": "rec-1",
        "isrc": "",
        "musicbrainz_albumid": "rel-1",
        "year": "2003",
        "composer": "",
        "writer": "",
        "genres": "",
        "recordlabel": "",
    }
    row.update(overrides)
    return row


class TestDuplicateTracksIgnoresRowsForTheSameFile:
    def test_two_rows_for_one_path_are_not_a_duplicate(self, monkeypatch) -> None:
        rows = [
            _row(id="1"),
            _row(id="2", recording_mbid=""),
            # A genuine second copy: same song, DIFFERENT file.
            _row(id="3", file_path="/music/Other/10. Track.mp3"),
            _row(id="4", file_path="/music/Other/10. Track.mp3", recording_mbid=""),
        ]
        monkeypatch.setattr(ams, "db_session", lambda: _fake_db(rows))
        monkeypatch.setattr(
            ams, "_version_recommendation",
            lambda tracks, expected_duration=0.0: {},
        )

        result = ams.find_duplicate_tracks("The Kinks", "Battlefield Vietnam")

        assert result["duplicate_group_count"] == 1, result
        assert result["duplicates"][0]["count"] == 2, result
        assert set(result["duplicates"][0]["track_ids"]) == {"1", "3"}, (
            "the same-path pair must collapse to its most complete row, and the "
            "genuine different-file copy must still be flagged"
        )

    def test_the_album_page_uses_the_same_rule(self) -> None:
        import inspect

        from routes import ui_routes

        src = inspect.getsource(ui_routes.album_detail)
        assert "dedupe_track_rows_by_file_path" in src, (
            "the album page would render the same file twice again"
        )


class TestImportIsIdempotentByPath:
    def test_a_known_path_wins_before_metadata(self, monkeypatch) -> None:
        calls = []

        class _ExecResult:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        @contextmanager
        def _db(rows):
            class _S:
                def execute(self, sql, _params=None):
                    calls.append(str(sql))
                    return _ExecResult(rows.pop(0) if rows else None)
            yield _S()

        monkeypatch.setattr(mp3, "db_session", lambda: _db([("id-by-path",), ("id-by-meta",)]))

        scanner = mp3.MP3ImportScanner.__new__(mp3.MP3ImportScanner)
        found = scanner._find_existing_track(
            "New Title", "New Artist", "New Album", "/music/file.mp3"
        )

        assert found == "id-by-path", found
        assert len(calls) == 1, calls
        assert "file_path" in calls[0], "the path query must run first"

    def test_metadata_is_the_fallback_when_the_path_is_unknown(
        self, monkeypatch
    ) -> None:
        calls = []

        class _ExecResult:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        @contextmanager
        def _db(rows):
            class _S:
                def execute(self, sql, _params=None):
                    calls.append(str(sql))
                    return _ExecResult(rows.pop(0) if rows else None)
            yield _S()

        monkeypatch.setattr(mp3, "db_session", lambda: _db([None, ("id-by-meta",)]))

        scanner = mp3.MP3ImportScanner.__new__(mp3.MP3ImportScanner)
        found = scanner._find_existing_track("T", "A", "Al", "/music/new.mp3")

        assert found == "id-by-meta", found
        assert len(calls) == 2, calls


class TestDeletingADuplicateNeverRemovesASharedFile:
    @pytest.fixture
    def deleted(self, monkeypatch):
        state = {"removed": [], "row_deleted": []}

        monkeypatch.setattr(
            arts,
            "fetch_track_for_delete",
            lambda _conn, _id: ("1", "/music/shared.mp3", "A", "Al", "T"),
        )
        monkeypatch.setattr(
            arts, "delete_track_row", lambda _conn, track_id: state["row_deleted"].append(track_id)
        )
        monkeypatch.setattr(arts.os.path, "exists", lambda _p: True)
        monkeypatch.setattr(arts.os, "remove", lambda p: state["removed"].append(p))
        return state

    def test_shared_path_deletes_the_row_only(self, deleted, monkeypatch) -> None:
        monkeypatch.setattr(
            arts, "another_track_uses_file_path", lambda _c, _p, _i: True
        )
        data, code = arts.delete_track("1", delete_file=True)

        assert code == 200, data
        assert data["deleted_file"] is False, data
        assert deleted["removed"] == [], "the kept row's file was deleted"
        assert deleted["row_deleted"] == ["1"], deleted

    def test_unshared_path_still_deletes_the_file(self, deleted, monkeypatch) -> None:
        monkeypatch.setattr(
            arts, "another_track_uses_file_path", lambda _c, _p, _i: False
        )
        data, code = arts.delete_track("1", delete_file=True)

        assert code == 200, data
        assert data["deleted_file"] is True, data
        assert deleted["removed"] == ["/music/shared.mp3"], deleted
