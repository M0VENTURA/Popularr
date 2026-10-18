"""Tests for the per-user favourites (heart / Navidrome star) system.

Verifies:
  - ``favourites_repository`` stores favourite state PER USER — one user's
    hearts never leak into another user's view.
  - ``set_favourite`` / ``is_favourite`` / ``get_favourite_ids`` round-trip.
  - ``favourites_service.toggle_favourite`` persists per-user and mirrors to
    Navidrome (star/unstar) for the ACTIVE user.
  - ``apply_favourite_rating_floor`` raises hearted tracks to the configured
    floor (e.g. 4★) across all configured users.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def fav_env(monkeypatch):
    """Fresh SQLite DB with user_favourites + tracks tables, plus a stubbed
    Navidrome client."""
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE user_favourites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                navidrome_id TEXT,
                is_favourite INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE (username, entity_type, entity_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                artist TEXT,
                album_artist TEXT,
                album TEXT,
                title TEXT,
                stars INTEGER,
                updated_at TEXT
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
        "db.repositories.favourites_repository.db_session",
        lambda *a, **kw: _Session(session),
    )
    monkeypatch.setattr(
        "services.favourites_service.db_session",
        lambda *a, **kw: _Session(session),
    )

    class _FakeNavidrome:
        def __init__(self):
            self.starred = set()
            self.unstarred = set()

        def star_track(self, track_id):
            self.starred.add(track_id)
            return True

        def unstar_track(self, track_id):
            self.unstarred.add(track_id)
            return True

        # ⭐ The service calls ``star_entity``/``unstar_entity``, not the
        # ``*_track`` pair: Subsonic's ``star`` takes ``id`` for a SONG but
        # ``albumId`` / ``artistId`` for an album / artist, so a kind-blind
        # method cannot serve all three (it would star nothing for the other
        # two, silently).
        def star_entity(self, entity_type, navidrome_id):
            self.starred.add(navidrome_id)
            return True

        def unstar_entity(self, entity_type, navidrome_id):
            self.unstarred.add(navidrome_id)
            return True

    fake = _FakeNavidrome()
    monkeypatch.setattr(
        "services.favourites_service.get_navidrome_client_for_active_user",
        lambda: fake,
    )
    return {"engine": engine, "session": session, "fake_navidrome": fake}


def test_favourites_are_per_user(fav_env):
    """User A's heart must not appear for user B."""
    from db.repositories.favourites_repository import (
        set_favourite,
        is_favourite,
        get_favourite_ids,
    )

    set_favourite("alice", "track", "t1", True, navidrome_id="t1")
    set_favourite("bob", "track", "t2", True, navidrome_id="t2")

    assert is_favourite("alice", "track", "t1") is True
    assert is_favourite("alice", "track", "t2") is False  # bob's heart
    assert is_favourite("bob", "track", "t2") is True
    assert is_favourite("bob", "track", "t1") is False  # alice's heart

    assert get_favourite_ids("alice", "track") == ["t1"]
    assert get_favourite_ids("bob", "track") == ["t2"]


def test_toggle_favourite_syncs_to_navidrome(fav_env, monkeypatch):
    """toggle_favourite persists per-user AND stars/unstars in Navidrome."""
    import services.favourites_service as svc

    monkeypatch.setattr(svc, "get_active_username", lambda: "alice")

    result = svc.toggle_favourite("track", "t1", True, navidrome_id="t1")
    assert result["success"] is True
    assert result["is_favourite"] is True
    assert result["navidrome_synced"] is True
    assert "t1" in fav_env["fake_navidrome"].starred

    assert svc.is_favourite("track", "t1") is True

    result2 = svc.toggle_favourite("track", "t1", False, navidrome_id="t1")
    assert result2["success"] is True
    assert result2["is_favourite"] is False
    assert "t1" in fav_env["fake_navidrome"].unstarred
    assert svc.is_favourite("track", "t1") is False


def test_apply_favourite_rating_floor(fav_env, monkeypatch):
    """Hearted tracks are raised to the configured floor across all users."""
    import services.favourites_service as svc
    from db.repositories.favourites_repository import set_favourite

    monkeypatch.setattr(svc, "get_active_username", lambda: "alice")

    with fav_env["engine"].begin() as conn:
        conn.execute(text("""
            INSERT INTO tracks (id, artist, album_artist, album, title, stars, updated_at) VALUES
                ('t1', 'Artist', 'Artist', 'Album', 'Hearted Song', 3, NULL),
                ('t2', 'Artist', 'Artist', 'Album', 'Normal Song', 5, NULL)
        """))

    # alice hearts t1; bob is also a configured user (via normalized config).
    set_favourite("alice", "track", "t1", True, navidrome_id="t1")
    monkeypatch.setattr(
        "helpers.config_helpers.get_navidrome_users_normalized",
        lambda: [{"user": "alice", "base_url": "http://nd", "pass": "x"}],
    )
    monkeypatch.setattr(svc, "favourite_rating_floor", lambda: 4)

    affected = svc.apply_favourite_rating_floor("Artist", "Album")
    assert affected == 1  # only t1 was below the floor

    with fav_env["engine"].begin() as conn:
        row = conn.execute(text("SELECT stars FROM tracks WHERE id = 't1'")).fetchone()
        assert row[0] == 4
        row2 = conn.execute(text("SELECT stars FROM tracks WHERE id = 't2'")).fetchone()
        assert row2[0] == 5  # unchanged
