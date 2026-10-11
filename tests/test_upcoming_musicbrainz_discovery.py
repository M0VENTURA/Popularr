"""Tests for MusicBrainz upcoming-releases GLOBAL discovery.

Verifies the fetcher can surface releases from artists NOT in the local
catalogue (the "find new MusicBrainz releases" behaviour), while still
flagging ``artist_in_collection`` correctly and persisting them into
``upcoming_releases``.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from conftest import register_sqlite_regexp_replace


@pytest.fixture()
def mb_fetcher_env(monkeypatch):
    """Fresh SQLite DB with upcoming_releases + tracks tables."""
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")
    register_sqlite_regexp_replace(engine)
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE upcoming_releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist_name TEXT NOT NULL,
                album_name TEXT NOT NULL,
                release_date TEXT,
                release_year INTEGER,
                source TEXT,
                primary_type TEXT,
                artist_in_collection INTEGER DEFAULT 0,
                release_group_mbid TEXT,
                mbid_match_status TEXT,
                mbid_source TEXT,
                mbid_confidence TEXT,
                mbid_match_score REAL,
                mbid_manual_override INTEGER DEFAULT 0,
                mbid_last_checked_at TEXT,
                status TEXT,
                last_seen_at TEXT,
                updated_at TEXT,
                UNIQUE (artist_name, album_name, primary_type)
            )
        """))
        conn.execute(text("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                artist TEXT,
                album_artist TEXT,
                album TEXT
            )
        """))
        # One catalogue artist present in the library.
        conn.execute(text(
            "INSERT INTO tracks (id, artist, album_artist, album) "
            "VALUES ('t1', 'Catalogue Artist', 'Catalogue Artist', 'Existing Album')"
        ))

    class _Session:
        def __init__(self, session):
            self._session = session

        def execute(self, *args, **kwargs):
            return self._session.execute(*args, **kwargs)

        def commit(self):
            self._session.commit()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *exc):
            if exc_type is None:
                self._session.commit()
            self._session.close()
            return False

    session = sess_factory()
    monkeypatch.setattr(
        "services.upcoming_releases.musicbrainz_fetcher_service.db_session",
        lambda *a, **kw: _Session(session),
    )
    return engine


def test_release_group_artist_extracts_credit():
    from services.upcoming_releases.musicbrainz_fetcher_service import _release_group_artist

    rg = {"artist-credit": [{"name": "Spice Girls", "joinphrase": ""}]}
    assert _release_group_artist(rg) == "Spice Girls"

    rg2 = {
        "artist-credit": [
            {"name": "Artist A", "joinphrase": " & "},
            {"name": "Artist B", "joinphrase": ""},
        ]
    }
    assert _release_group_artist(rg2) == "Artist A & Artist B"


def test_global_persist_marks_non_collection_artist(mb_fetcher_env):
    """A release from an artist NOT in the library is persisted with
    artist_in_collection=False."""
    from services.upcoming_releases.musicbrainz_fetcher_service import _persist_global_releases

    releases = [
        {
            "id": "rg-new-1",
            "title": "Brand New Album",
            "first_release_date": "2026-10-01",
            "primary_type": "album",
            "artist": "Brand New Artist",
        },
        {
            "id": "rg-cat-1",
            "title": "New Album From Collection Artist",
            "first_release_date": "2026-11-01",
            "primary_type": "album",
            "artist": "Catalogue Artist",
        },
    ]
    inserted, updated = _persist_global_releases(releases)
    assert inserted == 2
    assert updated == 0

    with mb_fetcher_env.begin() as conn:
        rows = conn.execute(text("SELECT artist_name, album_name, artist_in_collection FROM upcoming_releases")).fetchall()
        by_album = {r[1]: r for r in rows}
        assert by_album["Brand New Album"][0] == "Brand New Artist"
        assert by_album["Brand New Album"][2] == 0  # not in collection
        assert by_album["New Album From Collection Artist"][2] == 1  # in collection


def test_global_persist_upserts_duplicate(mb_fetcher_env):
    """The same artist/album persists once (upsert), not duplicated."""
    from services.upcoming_releases.musicbrainz_fetcher_service import _persist_global_releases

    releases = [
        {
            "id": "rg-x",
            "title": "Same Album",
            "first_release_date": "2026-10-01",
            "primary_type": "album",
            "artist": "Some Artist",
        },
        {
            "id": "rg-y",
            "title": "same album",  # case-insensitive dedupe
            "first_release_date": "2026-10-15",
            "primary_type": "album",
            "artist": "some artist",
        },
    ]
    inserted, updated = _persist_global_releases(releases)
    assert inserted + updated == 2

    with mb_fetcher_env.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM upcoming_releases")).scalar()
        assert count == 1
