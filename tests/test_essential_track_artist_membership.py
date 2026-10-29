"""Essential Collections include tracks where the target is the TRACK artist.

REPORT: an artist's Essential Collection was missing tracks they perform on but
do not own the album of — compilations, splits, another act's album. The
membership query resolved ``COALESCE(album_artist, artist)``, i.e. the album
artist WON, and the post-filter compared only that same field. A track whose
``artist`` was the target but whose ``album_artist`` was someone else (or
"Various Artists") was therefore invisible.

Membership now checks BOTH credit fields: the album artist owns the release,
the track artist owns the performance, and EITHER puts the track in the
collection.
"""

from __future__ import annotations

from contextlib import contextmanager

from services.popularity.stages import finalise_stage as fs

ARTIST = "Target Artist"
_IDS = [f"t{i}" for i in range(12)]


class _Row:
    def __init__(self, mapping: dict):
        self._mapping = mapping


class _Result:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self):
        return [_Row(dict(r)) for r in self._rows]


class _Session:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.sql: list[str] = []
        self.params: list[dict] = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        self.params.append(dict(params or {}))
        return _Result(self._rows)


def _row(track_id: str, *, artist: str, album_artist: str, stars: int = 5) -> dict:
    return {
        "id": track_id,
        "title": f"Song {track_id}",
        "album": f"Album {track_id}",
        "file_path": f"Artist/Album/{track_id}.mp3",
        "duration": 200,
        "stars": stars,
        "is_live": 0,
        "is_compilation": 0,
        "popularity_score": 50.0,
        "final_score": 50.0,
        "lastfm_listeners": 100,
        "listenbrainz_listens": 100,
        "year": 2000,
        "release_year": 2000,
        "artist": artist,
        "album_artist": album_artist,
        "genres": "Rock",
    }


def _run(monkeypatch, rows: list[dict]) -> dict:
    """Run ``_sync_essential_playlist`` with the DB/push stubbed out."""
    session = _Session(rows)

    @contextmanager
    def _db_session(*_a, **_k):
        yield session

    import db.engine as db_engine

    monkeypatch.setattr(db_engine, "db_session", _db_session)

    pushed: dict = {"ids": [], "deleted": None}

    monkeypatch.setattr(
        fs, "_sync_playlist_to_navidrome",
        lambda name, ids, rows=None, **kw: pushed.update(ids=list(ids))
        or {"skipped": True, "playlist_id": "pl-1"},
    )
    monkeypatch.setattr(fs, "_playlist_sync_succeeded", lambda res: True)
    monkeypatch.setattr(
        fs, "_delete_playlist_from_navidrome",
        lambda name: pushed.update(deleted=name),
    )
    monkeypatch.setattr(fs, "_essential_include_featured_enabled", lambda: False)

    fs._sync_essential_playlist(
        ARTIST,
        artist_scores=[1.0, 2.0, 3.0],
        featured_rows=[],
    )
    pushed["session"] = session
    return pushed


class TestTrackArtistMembership:
    def test_album_artist_still_wins(self, monkeypatch):
        """CONTROL: the original behaviour must keep working."""
        rows = [_row(tid, artist="Someone Else", album_artist=ARTIST) for tid in _IDS]
        pushed = _run(monkeypatch, rows)
        assert set(pushed["ids"]) == set(_IDS)

    def test_track_artist_on_another_acts_album_is_included(self, monkeypatch):
        """THE fix: album_artist is someone else, artist is the target."""
        rows = [_row(tid, artist=ARTIST, album_artist="Another Act") for tid in _IDS]
        pushed = _run(monkeypatch, rows)
        assert set(pushed["ids"]) == set(_IDS), (
            "a track the artist performs on another act's album was excluded"
        )

    def test_track_artist_on_a_various_artists_compilation_is_included(self, monkeypatch):
        rows = [_row(tid, artist=ARTIST, album_artist="Various Artists") for tid in _IDS]
        pushed = _run(monkeypatch, rows)
        assert set(pushed["ids"]) == set(_IDS)

    def test_a_track_artist_guest_credit_belongs_to_the_lead(self, monkeypatch):
        rows = [
            _row(tid, artist=f"{ARTIST} feat. Guest", album_artist="Another Act")
            for tid in _IDS
        ]
        pushed = _run(monkeypatch, rows)
        assert set(pushed["ids"]) == set(_IDS)

    def test_a_lookalike_track_artist_is_not_included(self, monkeypatch):
        """A prefix match alone must not drag in a different act."""
        rows = [
            _row(tid, artist=f"{ARTIST} Tribute Band", album_artist="Another Act")
            for tid in _IDS
        ]
        pushed = _run(monkeypatch, rows)
        assert pushed["ids"] == [], (
            "a lookalike artist name was added to the collection"
        )
        assert pushed["deleted"], (
            "with no qualifying tracks the stale playlist must be removed"
        )

    def test_the_fetch_queries_the_track_artist_column(self, monkeypatch):
        """The SQL must SELECT those rows, not rely on the post-filter alone."""
        rows = [_row(tid, artist=ARTIST, album_artist="Another Act") for tid in _IDS]
        pushed = _run(monkeypatch, rows)
        sql = " ".join(pushed["session"].sql)
        assert "LOWER(TRIM(artist))" in sql, (
            "the membership query still only looks at the album artist; the "
            "track-artist rows are never fetched"
        )


class TestRefreshDiscoversTrackOnlyArtists:
    def test_the_artist_scan_also_collects_track_artists(self, monkeypatch):
        """An artist with no album of their own must still be discovered."""
        session = _Session([])

        @contextmanager
        def _db_session(*_a, **_k):
            yield session

        import db.engine as db_engine

        monkeypatch.setattr(db_engine, "db_session", _db_session)
        monkeypatch.setattr(
            fs, "_essential_include_featured_enabled", lambda: False,
        )

        fs._refresh_all_essential_collections()

        sql = " ".join(session.sql)
        assert "SELECT TRIM(artist)" in sql, (
            "the refresh only scans album artists, so an artist who exists "
            "only as a track artist never gets a collection"
        )
        assert "HAVING COUNT(*)" in sql, (
            "track-artist discovery must keep the 12-track floor so one-off "
            "guest credits do not spawn playlists"
        )
