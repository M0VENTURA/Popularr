"""The popularity CSV names the playlists each track is in.

REQUEST

> "On the config page, where we export the popularity scoring csv, can we add
>  playlists included in next to each track?"

The column is sourced from the playlist INDEX (``playlists`` +
``playlist_entries``) that the scan's finalise pass already maintains for the
artist page's "Included in Playlists" section — not from a live Subsonic walk,
which would cost one request per playlist per export.
"""

from __future__ import annotations

import csv
import io

import pytest
from sqlalchemy import text

from db.engine import db_session
from services.popularity.popularity_report_service import (
    POPULARITY_REPORT_COLUMNS,
    build_collection_popularity_report,
)


def playlist_names_by_track_id():
    """The index query under test, fetched lazily.

    A module-level import would ERROR during collection against unfixed code,
    hiding the behaviour this file guards.
    """
    from db.repositories import playlists as repo

    query = getattr(repo, "playlist_names_by_track_id", None)
    assert query is not None, "the repository has no playlist membership query"
    return query()


@pytest.fixture()
def library():
    """A tiny library plus a two-playlist index."""
    with db_session() as session:
        for statement in (
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY, title TEXT, artist TEXT, album TEXT,
                album_artist TEXT, disc_number TEXT, track_number TEXT, year TEXT,
                listenbrainz_genres TEXT, musicbrainz_genres TEXT, lastfm_tags TEXT,
                discogs_genres TEXT, genres TEXT,
                raw_score DOUBLE PRECISION, final_score DOUBLE PRECISION,
                popularity DOUBLE PRECISION, stars DOUBLE PRECISION,
                duration DOUBLE PRECISION,
                lastfm_listeners DOUBLE PRECISION, lastfm_playcount DOUBLE PRECISION,
                lastfm_playcount_available TEXT, lastfm_score DOUBLE PRECISION,
                listenbrainz_listens DOUBLE PRECISION, listenbrainz_users DOUBLE PRECISION,
                listenbrainz_score DOUBLE PRECISION,
                exclude_from_stats TEXT, is_single TEXT, single_confidence TEXT,
                single_confidence_score DOUBLE PRECISION, is_live TEXT,
                popularity_frozen TEXT, lastfm_last_updated TEXT,
                listenbrainz_last_updated TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS playlists (
                playlist_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                source TEXT NOT NULL, type TEXT, owner TEXT, is_public BOOLEAN,
                song_count INTEGER DEFAULT 0, comment TEXT, changed TEXT,
                managed_type TEXT, synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS playlist_entries (
                playlist_id TEXT, track_id TEXT, position INTEGER,
                PRIMARY KEY (playlist_id, position)
            )
            """,
            "DELETE FROM tracks",
            "DELETE FROM playlists",
            "DELETE FROM playlist_entries",
        ):
            session.execute(text(statement))
        for tid, title in (("t1", "Song A"), ("t2", "Song B"), ("t3", "Song C")):
            session.execute(
                text("INSERT INTO tracks (id, title, artist, album, raw_score, stars) "
                     "VALUES (:i, :t, 'Artist', 'Album', 90, 5)"),
                {"i": tid, "t": title},
            )
        session.execute(
            text("INSERT INTO playlists (playlist_id, name, source, song_count) "
                 "VALUES ('p1', 'Metal Covers', 'subsonic', 2)")
        )
        session.execute(
            text("INSERT INTO playlists (playlist_id, name, source, song_count) "
                 "VALUES ('p2', 'Hidden Gems', 'subsonic', 1)")
        )
        session.execute(text("INSERT INTO playlist_entries VALUES ('p1', 't1', 1)"))
        session.execute(text("INSERT INTO playlist_entries VALUES ('p1', 't2', 2)"))
        session.execute(text("INSERT INTO playlist_entries VALUES ('p2', 't1', 1)"))
        session.commit()
    yield


def _rows() -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(build_collection_popularity_report())))


class TestTheColumnExists:
    def test_it_is_in_the_header(self) -> None:
        assert "playlists_included_in" in POPULARITY_REPORT_COLUMNS

    def test_it_sits_beside_the_popularity_scores(self) -> None:
        """The point is reading a rank against where the track appears."""
        index = POPULARITY_REPORT_COLUMNS.index("playlists_included_in")
        assert POPULARITY_REPORT_COLUMNS[index - 1] == "listenbrainz_score"

    def test_the_header_and_every_row_agree(self, library) -> None:
        rows = _rows()
        assert rows
        for row in rows:
            assert "playlists_included_in" in row


class TestTheValues:
    def test_a_track_in_two_playlists_names_both(self, library) -> None:
        rows = {r["title"]: r for r in _rows()}
        assert rows["Song A"]["playlists_included_in"] == "Hidden Gems; Metal Covers"

    def test_a_track_in_one_playlist_names_it(self, library) -> None:
        rows = {r["title"]: r for r in _rows()}
        assert rows["Song B"]["playlists_included_in"] == "Metal Covers"

    def test_a_track_in_none_is_blank_not_missing(self, library) -> None:
        rows = {r["title"]: r for r in _rows()}
        assert rows["Song C"]["playlists_included_in"] == ""

    def test_names_are_sorted_for_a_stable_file(self, library) -> None:
        rows = {r["title"]: r for r in _rows()}
        assert rows["Song A"]["playlists_included_in"].split("; ") == sorted(
            rows["Song A"]["playlists_included_in"].split("; "), key=str.casefold
        )


class TestTheIndexQuery:
    def test_it_returns_a_map_keyed_by_track(self, library) -> None:
        assert playlist_names_by_track_id() == {
            "t1": "Hidden Gems; Metal Covers",
            "t2": "Metal Covers",
        }

    def test_a_duplicate_entry_names_the_playlist_once(self, library) -> None:
        """``playlist_entries`` allows the same track at two positions."""
        with db_session() as session:
            session.execute(text("INSERT INTO playlist_entries VALUES ('p2', 't1', 2)"))
            session.commit()
        assert playlist_names_by_track_id()["t1"].count("Hidden Gems") == 1

    def test_a_missing_index_is_an_empty_map_not_an_error(self) -> None:
        """The export must still work before the index has ever been synced."""
        with db_session() as session:
            session.execute(text("DROP TABLE IF EXISTS playlist_entries"))
            session.execute(text("DROP TABLE IF EXISTS playlists"))
            session.commit()
        assert playlist_names_by_track_id() == {}

    def test_the_report_still_exports_without_the_index(self) -> None:
        with db_session() as session:
            session.execute(text("DROP TABLE IF EXISTS playlist_entries"))
            session.execute(text("DROP TABLE IF EXISTS playlists"))
            session.commit()
        rows = _rows()
        for row in rows:
            assert row["playlists_included_in"] == ""


class TestTheExportRefreshesTheIndex:
    def test_it_uses_the_client_aware_refresh(self, monkeypatch) -> None:
        """⚠️ REGRESSION: the export used a NO-CLIENT sync and pruned the index.

        ``ensure_playlist_index`` must go through the finalise stage's
        ``refresh_playlist_index``, which supplies the server-scoped Navidrome
        clients. Otherwise the sync sees only file-backed playlists and deletes
        every generated one — leaving this column blank on every row.
        """
        import services.popularity.stages.finalise_stage as fs

        from services.popularity import popularity_report_service as svc

        calls: list[str] = []

        def _refresh() -> dict[str, int]:
            calls.append("refresh")
            return {"playlists": 4, "entries": 900}

        monkeypatch.setattr(fs, "refresh_playlist_index", _refresh)

        stats = svc.ensure_playlist_index()

        assert calls == ["refresh"], (
            "the export did not use the client-aware playlist-index refresh"
        )
        assert stats == {"playlists": 4, "entries": 900}
