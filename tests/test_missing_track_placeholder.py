"""A placeholder track row must not make a missing track look present.

REPORTED: the release had a song missing at track 1, but it never appeared under
missing releases. Missing/queued tracks are inserted as rows with no
``file_path``; the missing calculation counted those rows as library presence.
"""

from __future__ import annotations

from services.metadata import album_missing_service as ams


class _Mapping:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mapping(self._rows)

    def fetchall(self):
        return self._rows


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)

    def execute(self, *_args, **_kwargs):
        return _Result(self._responses.pop(0) if self._responses else [])

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


MB_TRACKS = [
    {"title": "Gap Song", "track_number": "1", "disc_number": 1, "duration": 210000},
    {"title": "Present", "track_number": "2", "disc_number": 1, "duration": 200000},
]


def _run(monkeypatch, library_rows):
    responses = [
        [{"musicbrainz_album_mbid": "rel-1", "album": "Album"}],
        library_rows,
    ]
    monkeypatch.setattr(ams, "db_session", lambda: _Session(responses))
    monkeypatch.setattr(
        ams, "fetch_musicbrainz_release_metadata",
        lambda mbid: {"tracks": MB_TRACKS, "release_year": 2020},
    )
    monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)
    monkeypatch.setattr(ams, "_rejected_missing_titles", lambda *a, **k: set())
    return ams.get_missing_tracks("Artist", "Album", release_mbid="rel-1")


def test_a_placeholder_row_does_not_hide_the_gap(monkeypatch):
    result = _run(monkeypatch, [
        # Placeholder: the missing track exists as a row but has no audio file.
        {"id": 1, "title": "Gap Song", "track_number": "1",
         "disc_number": 1, "album": "Album", "duration": 210, "file_path": ""},
        {"id": 2, "title": "Present", "track_number": "2",
         "disc_number": 1, "album": "Album", "duration": 200,
         "file_path": "/music/02.mp3"},
    ])
    titles = [m["title"] for m in result["missing_tracks"]]
    assert titles == ["Gap Song"]


def test_a_real_file_at_that_position_still_counts(monkeypatch):
    result = _run(monkeypatch, [
        {"id": 1, "title": "Gap Song", "track_number": "1",
         "disc_number": 1, "album": "Album", "duration": 210,
         "file_path": "/music/01.mp3"},
        {"id": 2, "title": "Present", "track_number": "2",
         "disc_number": 1, "album": "Album", "duration": 200,
         "file_path": "/music/02.mp3"},
    ])
    assert result["missing_tracks"] == []


def test_the_queued_for_download_sentinel_is_not_a_file(monkeypatch):
    result = _run(monkeypatch, [
        {"id": 1, "title": "Gap Song", "track_number": "1",
         "disc_number": 1, "album": "Album", "duration": 210,
         "file_path": "__queued_for_download__"},
        {"id": 2, "title": "Present", "track_number": "2",
         "disc_number": 1, "album": "Album", "duration": 200,
         "file_path": "/music/02.mp3"},
    ])
    assert [m["title"] for m in result["missing_tracks"]] == ["Gap Song"]
