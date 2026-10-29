"""Various Artists matching must use the TRACK ARTIST, not the album artist.

Reports: *"Various Artists compilations are correctly matching tracks that have
the track artist rather than the album artist? It seems to be missing tracks
where the album artist isn't matching Various Artists or they are from a
different album. As long as the Track Artist and song name are matching the
queue, they should be able to match correctly."*

Four identity checks were album-artist scoped and missed a VA row:

* ``find_library_track`` (the "already owned" check) used
  ``COALESCE(album_artist, artist)`` — a row with
  artist="Coldplay" / album_artist="Various Artists" was invisible, so the
  track was re-queued;
* ``_queued_coverage`` required the queue row's album artist AND album to
  match, so an already-queued track still showed "Missing";
* ``get_missing_tracks`` only looked for library ownership inside the album;
* ``get_release_tracks_with_status`` only fetched same-album queue rows.

All four now key on the track artist + title, and the album is a preference.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session
from db.repositories.queue import (
    get_album_queue_tracks,
    get_queue_tracks_for_artists,
)
from db.repositories.tracks import find_library_track
from services.metadata import album_missing_service as ams

_ALBUM = "2002 - MTV2 Handpicked"
_OTHER_ALBUM = "Parachutes"


def _ensure_queue_table() -> None:
    """Create/repair the shared ``download_queue`` table for these tests."""
    columns = {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "title": "TEXT",
        "artist": "TEXT",
        "album": "TEXT",
        "album_artist": "TEXT",
        "track_number": "TEXT",
        "disc_number": "TEXT",
        "status": "TEXT",
        "file_path": "TEXT",
        "created_at": "TEXT",
    }
    with db_session() as session:
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS download_queue ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, artist TEXT, "
            "album TEXT, album_artist TEXT, track_number TEXT, "
            "disc_number TEXT, status TEXT, file_path TEXT, created_at TEXT)"
        ))
        existing = {
            str(row[1])
            for row in session.execute(text("PRAGMA table_info(download_queue)")).fetchall()
        }
    for name, sql_type in columns.items():
        if name in existing or name == "id":
            continue
        with db_session() as session:
            try:
                session.execute(text(f"ALTER TABLE download_queue ADD COLUMN {name} {sql_type}"))
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _isolated_rows():
    _ensure_queue_table()

    def _wipe() -> None:
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))
            session.execute(text("DELETE FROM download_queue"))

    _wipe()
    yield
    _wipe()


def _seed_track(
    track_id: str,
    *,
    title: str,
    artist: str = "Coldplay",
    album_artist: str = "Various Artists",
    album: str = _OTHER_ALBUM,
    track_number: str = "5",
) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, title, artist, album, album_artist, "
                "track_number, disc_number, file_path) "
                "VALUES (:id, :title, :artist, :album, :album_artist, :tn, '1', :fp)"
            ),
            {
                "id": track_id,
                "title": title,
                "artist": artist,
                "album": album,
                "album_artist": album_artist,
                "tn": track_number,
                "fp": f"/music/{album_artist}/{album}/{track_number}. {artist} - {title}.mp3",
            },
        )


def _queue(
    *,
    title: str,
    artist: str,
    album_artist: str,
    album: str,
    status: str = "queued",
    track_number: str = "5",
) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO download_queue (title, artist, album, album_artist, "
                "track_number, disc_number, status) "
                "VALUES (:title, :artist, :album, :album_artist, :tn, '1', :status)"
            ),
            {
                "title": title,
                "artist": artist,
                "album": album,
                "album_artist": album_artist,
                "tn": track_number,
                "status": status,
            },
        )


class TestFindLibraryTrackUsesTheTrackArtist:
    def test_a_va_row_is_found_by_its_performer_even_on_another_album(self):
        _seed_track("t1", title="Yellow")

        found = find_library_track(
            artist="Coldplay", title="Yellow", album=_ALBUM,
        )

        assert found is not None and found["id"] == "t1"

    def test_the_same_album_is_found_either_way(self):
        _seed_track("t1", title="Yellow", album=_ALBUM)

        assert find_library_track(artist="Coldplay", title="Yellow", album=_ALBUM)
        assert find_library_track(
            artist="Various Artists", title="Yellow", album=_ALBUM,
        )

    def test_a_different_artist_still_does_not_match(self):
        _seed_track("t1", title="Yellow")

        assert find_library_track(
            artist="Oasis", title="Yellow", album=_ALBUM,
        ) is None


class TestQueuedCoverageUsesArtistAndTitle:
    def test_a_row_queued_from_another_album_matches_by_performer_and_title(self):
        _queue(
            title="Yellow",
            artist="Coldplay",
            album_artist="Coldplay",   # NOT "Various Artists"
            album=_OTHER_ALBUM,        # NOT the compilation
        )

        titles, positions, artist_titles, generic_titles = ams._queued_coverage(
            "Various Artists", _ALBUM, track_artists={"Coldplay"},
        )

        key = (ams._artist_match_key("Coldplay"), ams._title_match_key("Yellow"))
        assert artist_titles[key] == "queued"
        assert titles == {} and positions == {}, (
            "the album/position keys stay same-album only"
        )
        assert generic_titles == {}

    def test_a_va_row_without_a_performer_matches_by_title_on_this_album(self):
        _queue(
            title="Yellow",
            artist="Various Artists",
            album_artist="Various Artists",
            album=_ALBUM,
        )

        _titles, _positions, artist_titles, generic_titles = ams._queued_coverage(
            "Various Artists", _ALBUM, track_artists={"Coldplay"},
        )

        assert generic_titles[ams._title_match_key("Yellow")] == "queued"
        assert (
            ams._artist_match_key("Coldplay"),
            ams._title_match_key("Yellow"),
        ) not in artist_titles, "the placeholder must not claim to be the performer"

    def test_a_generic_row_from_another_album_is_not_attributed(self):
        """Title alone cannot attribute an unrelated compilation."""
        _queue(
            title="Yellow",
            artist="Various Artists",
            album_artist="Various Artists",
            album=_OTHER_ALBUM,
        )

        _titles, _positions, _artist_titles, generic_titles = ams._queued_coverage(
            "Various Artists", _ALBUM, track_artists={"Coldplay"},
        )

        assert generic_titles == {}

    def test_a_same_album_row_still_contributes_album_and_position_keys(self):
        _queue(
            title="Yellow",
            artist="Coldplay",
            album_artist="Various Artists",
            album=_ALBUM,
            track_number="5",
        )

        titles, positions, _artist_titles, _generic = ams._queued_coverage(
            "Various Artists", _ALBUM, track_artists={"Coldplay"},
        )

        assert titles[ams._title_match_key("Yellow")] == "queued"
        assert positions[(1, "5")] == "queued"


class TestMissingTracksAcrossAlbums:
    def _status(self, monkeypatch, tracks, *, queue_rows=()):
        monkeypatch.setattr(
            ams, "fetch_musicbrainz_release_metadata",
            lambda *a, **k: {"tracks": list(tracks), "release_year": "2002"},
        )
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)
        monkeypatch.setattr(ams, "_rejected_missing_titles", lambda *a, **k: set())
        return ams.get_missing_tracks(
            "Various Artists", _ALBUM, release_mbid="rel-1",
        )

    def test_a_track_owned_on_another_album_is_not_missing(self, monkeypatch):
        _seed_track("t1", title="Yellow", album=_OTHER_ALBUM)

        result = self._status(monkeypatch, [{
            "title": "Yellow", "artist": "Coldplay",
            "track_number": "5", "disc_number": 1,
        }])

        assert result["missing_tracks"] == []
        assert result["excluded"]["in_library"] == 1

    def test_a_genuinely_absent_track_is_still_missing(self, monkeypatch):
        _seed_track("t1", title="Yellow", artist="Coldplay")

        result = self._status(monkeypatch, [{
            "title": "Trouble", "artist": "Coldplay",
            "track_number": "6", "disc_number": 1,
        }])

        assert [m["title"] for m in result["missing_tracks"]] == ["Trouble"]

    def test_a_queue_row_from_another_album_annotates_the_missing_track(self, monkeypatch):
        _queue(
            title="Yellow",
            artist="Coldplay",
            album_artist="Coldplay",
            album=_OTHER_ALBUM,
        )

        result = self._status(monkeypatch, [{
            "title": "Yellow", "artist": "Coldplay",
            "track_number": "5", "disc_number": 1,
        }])

        assert [m["title"] for m in result["missing_tracks"]] == ["Yellow"]
        assert result["missing_tracks"][0]["queue_status"] == "queued"


class TestFolderLinkToQueue:
    def test_a_va_file_links_to_a_row_queued_from_another_album(self, monkeypatch):
        """The moved file must not leave its queue row orphaned.

        The album artist is "Various Artists", the queue row was created from
        the track's own album, and the moved file is from the compilation:
        only the TRACK ARTIST + TITLE identify it.
        """
        from services.downloads import download_folder_service as dfs
        import db.repositories.queue as queue_mod

        queue_rows = [{
            "id": 7,
            "title": "Yellow",
            "track_number": "5",
            "artist": "Coldplay",
            "album_artist": "Coldplay",
            "album": _OTHER_ALBUM,
            "status": "downloading",
        }]
        monkeypatch.setattr(queue_mod, "get_album_queue_tracks", lambda a, b: [])
        monkeypatch.setattr(queue_mod, "get_active_queue", lambda limit=500: list(queue_rows))
        updates: dict[int, dict] = {}
        monkeypatch.setattr(
            queue_mod, "update_queue_item",
            lambda qid, **kw: updates.setdefault(qid, kw) or {"id": qid, **kw},
        )

        ok, err = dfs._link_moved_file_to_queue_item(
            queue_artist="Various Artists",
            queue_album=_ALBUM,
            file_title="Yellow",
            file_artist="Coldplay",
            track_number="5",
            target_path=f"/music/Various Artists/{_ALBUM}/05. Coldplay - Yellow.mp3",
            release_mbid="rel-1",
        )

        assert ok is True, err
        assert updates[7]["status"] == "imported"


class TestReleaseStatusQueueRows:
    def test_queue_tracks_are_found_by_track_artist(self):
        _queue(
            title="Yellow",
            artist="Coldplay",
            album_artist="Coldplay",
            album=_OTHER_ALBUM,
        )

        rows = get_queue_tracks_for_artists(["Coldplay"])

        assert [row["title"] for row in rows] == ["Yellow"]

    def test_album_queue_rows_match_either_credit(self):
        _queue(
            title="Yellow",
            artist="Coldplay",
            album_artist="Various Artists",
            album=_ALBUM,
        )

        rows = get_album_queue_tracks("Various Artists", _ALBUM)

        assert [row["title"] for row in rows] == ["Yellow"]
