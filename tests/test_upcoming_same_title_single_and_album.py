"""Same-titled SINGLE and ALBUM must be two distinct upcoming rows.

MusicBrainz routinely has a release group for the lead single and another
for the album that shares its title (e.g. Clutch: single "Wasted Lands"
2026-09-25 and album "Wasted Lands" 2027-01-22).  The table used to dedupe
on (artist_name, album_name) alone, so the second write updated the first
row and the earliest-date rule discarded the album's future date — the
upcoming album vanished and the row showed the single's date under the
album's type.

The dedup key is now (artist, album, primary_type), while untyped rows
(Wikipedia, legacy data) remain absorbable by a later typed write.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from conftest import register_sqlite_regexp_replace


@pytest.fixture()
def upcoming_env(monkeypatch):
    """Fresh SQLite DB whose upcoming_releases mirrors the production key."""
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
                primary_type TEXT NOT NULL DEFAULT '',
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


def _clutch_shaped_releases():
    """The live Clutch shape: single and album sharing a title."""
    return [
        {
            "id": "rg-single",
            "title": "Wasted Lands",
            "first_release_date": "2026-09-25",
            "primary_type": "Single",
            "artist": "Clutch",
        },
        {
            "id": "rg-album",
            "title": "Wasted Lands",
            "first_release_date": "2027-01-22",
            "primary_type": "Album",
            "artist": "Clutch",
        },
    ]


def _rows(engine):
    with engine.begin() as conn:
        return conn.execute(text(
            "SELECT album_name, primary_type, release_date, release_group_mbid "
            "FROM upcoming_releases ORDER BY id"
        )).fetchall()


def test_same_titled_single_and_album_both_persist(upcoming_env):
    from services.upcoming_releases.musicbrainz_fetcher_service import (
        _persist_global_releases,
    )

    inserted, updated = _persist_global_releases(_clutch_shaped_releases())
    assert (inserted, updated) == (2, 0)

    rows = _rows(upcoming_env)
    assert len(rows) == 2

    by_type = {r[1]: r for r in rows}
    assert by_type["single"][2] == "2026-09-25"
    assert by_type["single"][3] == "rg-single"
    assert by_type["album"][2] == "2027-01-22"
    assert by_type["album"][3] == "rg-album"


def test_same_titled_pair_is_idempotent(upcoming_env):
    from services.upcoming_releases.musicbrainz_fetcher_service import (
        _persist_global_releases,
    )

    _persist_global_releases(_clutch_shaped_releases())
    inserted, updated = _persist_global_releases(_clutch_shaped_releases())

    assert (inserted, updated) == (0, 2)
    rows = _rows(upcoming_env)
    assert len(rows) == 2
    by_type = {r[1]: r for r in rows}
    assert by_type["album"][2] == "2027-01-22"


def test_typed_refresh_takes_incoming_date(upcoming_env):
    """MusicBrainz is the authority for its own typed row: a corrected date
    must refresh, not be pinned by the earliest-date legacy merge."""
    from services.upcoming_releases.musicbrainz_fetcher_service import (
        _persist_global_releases,
    )

    _persist_global_releases([
        {
            "id": "rg-album",
            "title": "Wasted Lands",
            "first_release_date": "2027-01-22",
            "primary_type": "album",
            "artist": "Clutch",
        },
    ])
    # MusicBrainz corrects the date forward.
    _persist_global_releases([
        {
            "id": "rg-album",
            "title": "Wasted Lands",
            "first_release_date": "2027-02-19",
            "primary_type": "album",
            "artist": "Clutch",
        },
    ])

    rows = _rows(upcoming_env)
    assert len(rows) == 1
    assert rows[0][2] == "2027-02-19"


def test_untyped_wiki_row_is_absorbed(upcoming_env):
    """A pre-existing untyped (Wikipedia/legacy) row for the same title is
    absorbed by the typed MusicBrainz write instead of duplicated."""
    from services.upcoming_releases.musicbrainz_fetcher_service import (
        _persist_global_releases,
    )

    with upcoming_env.begin() as conn:
        conn.execute(text(
            "INSERT INTO upcoming_releases (artist_name, album_name, source, "
            "release_date, primary_type, status) VALUES "
            "('Clutch', 'Wasted Lands', 'Wikipedia', '2026-12-01', '', 'discovered')"
        ))

    inserted, updated = _persist_global_releases(_clutch_shaped_releases())
    assert (inserted, updated) == (1, 1)

    rows = _rows(upcoming_env)
    assert len(rows) == 2

    by_type = {r[1]: r for r in rows}
    # The single absorbed the untyped wiki row (earliest-date merge keeps
    # 2026-09-25 over the wiki's 2026-12-01); the album inserted its own row.
    assert by_type["single"][2] == "2026-09-25"
    assert by_type["album"][2] == "2027-01-22"


def test_untyped_row_absorbs_single_type(upcoming_env):
    from services.upcoming_releases.musicbrainz_fetcher_service import (
        _persist_global_releases,
    )

    with upcoming_env.begin() as conn:
        conn.execute(text(
            "INSERT INTO upcoming_releases (artist_name, album_name, source, "
            "release_date, primary_type, status) VALUES "
            "('Clutch', 'Wasted Lands', 'Wikipedia', '2026-12-01', '', 'discovered')"
        ))

    inserted, updated = _persist_global_releases([
        {
            "id": "rg-single",
            "title": "Wasted Lands",
            "first_release_date": "2026-09-25",
            "primary_type": "single",
            "artist": "Clutch",
        },
    ])
    assert (inserted, updated) == (0, 1)

    rows = _rows(upcoming_env)
    assert len(rows) == 1
    assert rows[0][1] == "single"


def test_same_typed_case_variant_still_upserts(upcoming_env):
    """Case/punctuation-insensitive dedupe on the SAME type is unchanged."""
    from services.upcoming_releases.musicbrainz_fetcher_service import (
        _persist_global_releases,
    )

    releases = [
        {
            "id": "rg-a",
            "title": "Same Album",
            "first_release_date": "2026-10-01",
            "primary_type": "album",
            "artist": "Some Artist",
        },
        {
            "id": "rg-b",
            "title": "same album",
            "first_release_date": "2026-10-15",
            "primary_type": "Album",
            "artist": "some artist",
        },
    ]
    inserted, updated = _persist_global_releases(releases)
    assert (inserted, updated) == (1, 1)

    rows = _rows(upcoming_env)
    assert len(rows) == 1
    assert rows[0][1] == "album"
    # Typed refresh takes the incoming date of the last write.
    assert rows[0][2] == "2026-10-15"
