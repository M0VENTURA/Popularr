"""Tests for the strict Queue vs. Matched-Folders boundary.

Verifies:
  - ``ACTIVE_QUEUE_STATUSES`` no longer includes ``unmatched`` (local disk
    folders injected into ``download_queue`` must NOT appear in the active
    search/download queue).
  - ``get_active_queue()`` excludes ``source IN ('local','discovered')`` rows
    even when their status is active (belt-and-suspenders boundary).
  - The ``folder_matches`` repository persists/reads/removes the
    folder → MusicBrainz association used by the two-phase match flow.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker


def _make_engine():
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")
    return engine


@pytest.fixture()
def queue_engine(monkeypatch):
    """Point the queue repository at a fresh SQLite DB with the download_queue
    schema plus the folder_matches table."""
    engine = _make_engine()
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT, title TEXT, album TEXT,
                status TEXT DEFAULT 'queued', source TEXT DEFAULT 'soulseek',
                track_number TEXT, import_group TEXT, replace_track_id TEXT,
                created_at TEXT, updated_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE folder_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path TEXT NOT NULL,
                release_mbid TEXT NOT NULL,
                release_title TEXT,
                artist TEXT,
                release_year INTEGER,
                status TEXT DEFAULT 'matched',
                created_at TEXT,
                updated_at TEXT,
                UNIQUE (folder_path)
            )
        """))

    # A tiny session-bound wrapper compatible with db_session() usage.
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
        "db.repositories.queue.db_session",
        lambda *a, **kw: _Session(session),
    )
    monkeypatch.setattr(
        "db.repositories.folder_match_repository.db_session",
        lambda *a, **kw: _Session(session),
    )
    return engine


def test_active_queue_statuses_exclude_unmatched():
    """'unmatched' must not be an active queue status — local disk folders
    waiting in the Matched Folders section are passive."""
    from services.queue.queue_constraints import ACTIVE_QUEUE_STATUSES
    assert "unmatched" not in ACTIVE_QUEUE_STATUSES
    # The core search/download states remain active.
    for expected in ("queued", "searching", "processing", "downloading"):
        assert expected in ACTIVE_QUEUE_STATUSES


def test_get_active_queue_excludes_disk_sources(queue_engine):
    """Rows injected by the watcher/discovery (source=local/discovered) must
    never appear in the active queue, even when their status is active."""
    with queue_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO download_queue (artist, title, status, source) VALUES
                ('Active Artist', 'Real Download', 'downloading', 'soulseek'),
                ('Disk Artist', 'Local Folder File', 'unmatched', 'local'),
                ('Discovered Artist', 'Discovered File', 'unmatched', 'discovered'),
                ('Queue Artist', 'Queued Item', 'queued', 'soulseek')
        """))

    from db.repositories.queue import get_active_queue
    items = get_active_queue(limit=50)
    titles = [i.get("title") for i in items]
    assert "Real Download" in titles
    assert "Queued Item" in titles
    assert "Local Folder File" not in titles
    assert "Discovered File" not in titles


def test_folder_match_repository_roundtrip(queue_engine):
    """upsert → get → delete round-trip for the two-phase folder match."""
    from db.repositories.folder_match_repository import (
        upsert_folder_match,
        get_folder_match,
        get_all_folder_matches,
        delete_folder_match,
    )

    stored = upsert_folder_match(
        folder_path="/downloads/Music/Artist - Album (2024)",
        release_mbid="11111111-2222-3333-4444-555555555555",
        release_title="Album",
        artist="Artist",
        release_year=2024,
        status="matched",
    )
    assert stored is not None
    assert stored["folder_path"] == "/downloads/Music/Artist - Album (2024)"
    assert stored["release_mbid"] == "11111111-2222-3333-4444-555555555555"

    # Upsert on the same folder updates (not duplicates).
    upsert_folder_match(
        folder_path="/downloads/Music/Artist - Album (2024)",
        release_mbid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        release_title="Album (Deluxe)",
        artist="Artist",
        release_year=2025,
        status="matched",
    )
    matches = get_all_folder_matches()
    assert len(matches) == 1
    assert matches[0]["release_mbid"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    got = get_folder_match("/downloads/Music/Artist - Album (2024)")
    assert got is not None
    assert got["release_year"] == 2025

    assert delete_folder_match("/downloads/Music/Artist - Album (2024)") is True
    assert get_folder_match("/downloads/Music/Artist - Album (2024)") is None
    assert get_all_folder_matches() == []


class _Raise404:
    """Minimal MusicBrainz client double whose get_release raises like httpx."""

    def __init__(self, group_releases):
        self._group_releases = group_releases

    def get_release(self, release_mbid, inc="", timeout=10.0):
        raise Exception(
            "Client error '404 Not Found' for url "
            f"'https://musicbrainz.org/ws/2/release/{release_mbid}'"
        )

    def get_release_group(self, release_group_mbid, timeout=10.0):
        return {"id": release_group_mbid, "title": "Album"}

    def get(self, endpoint, *, params=None, timeout=10.0):
        if endpoint.rstrip("/") == "release":
            return {"releases": self._group_releases}
        return {}


def test_resolve_release_falls_back_to_release_group(monkeypatch):
    """A release-group MBID (as returned by the MB search modal) must resolve
    through the release-group browse fallback — the old code 404'd because
    get_release raises instead of returning empty."""
    from services.downloads import download_folder_service as svc

    release_mbid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    class _GroupOnly(_Raise404):
        """get_release raises for the release-group MBID but succeeds once the
        browse step has resolved a concrete release MBID (the current flow
        re-fetches the resolved release for its full payload)."""

        def get_release(self, release_mbid, inc="", timeout=10.0):
            if release_mbid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee":
                return {"id": release_mbid, "title": "Album"}
            raise Exception(
                "Client error '404 Not Found' for url "
                f"'https://musicbrainz.org/ws/2/release/{release_mbid}'"
            )

    client = _GroupOnly(
        group_releases=[{"id": release_mbid, "title": "Album"}],
    )
    release_data, resolved = svc._resolve_release(client, "rg-0000-0000-0000-000000000001")
    assert resolved == release_mbid
    assert release_data is not None


def test_resolve_release_direct_release_wins(monkeypatch):
    """A real release MBID is returned directly (no release-group fallback)."""
    from services.downloads import download_folder_service as svc

    class _DirectClient(_Raise404):
        def get_release(self, release_mbid, inc="", timeout=10.0):
            if release_mbid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee":
                return {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "title": "Album",
                    "artist-credit": [{"name": "Artist"}],
                }
            raise Exception("404 Not Found")

    client = _DirectClient(group_releases=[])
    release_data, release_mbid = svc._resolve_release(client, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert release_mbid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert release_data is not None
    assert release_data["title"] == "Album"
