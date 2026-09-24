"""Tests for the downloads/musicbrainz UX fixes.

Verifies:
  - ``_derive_folder_group`` groups matched folders by embedded audio
    metadata (artist/album), falling back to the folder path when metadata
    is missing or mixed.
  - ``requeue_due_failed_items`` never leaves an item stuck in ``failed`` —
    retries are unbounded, so an item requeues once its retry window arrives
    regardless of how often it already failed (config retry rules govern the
    backoff only).  ``download_queue.max_retries`` was dropped in migration 015
    because it was never enforced.
  - The MusicBrainz release-group search falls back to an artist-only query
    when the combined artist+album query returns no results.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# 1. Matched-folder metadata grouping
# ---------------------------------------------------------------------------

def test_derive_folder_group_uses_metadata_when_consistent():
    from services.downloads.download_folder_service import _derive_folder_group

    files = [
        {"is_audio": True, "artist": "Spice Girls", "album": "Greatest Hits"},
        {"is_audio": True, "artist": "Spice Girls", "album": "Greatest Hits"},
        {"is_audio": False, "name": "cover.jpg"},
    ]
    group = _derive_folder_group("Spice Girls - Greatest Hits", files)
    assert group["artist"] == "Spice Girls"
    assert group["album"] == "Greatest Hits"
    assert group["group_key"] == "Spice Girls :: Greatest Hits"


def test_derive_folder_group_falls_back_to_path_without_metadata():
    from services.downloads.download_folder_service import _derive_folder_group

    files = [
        {"is_audio": True, "artist": "", "album": ""},
        {"is_audio": True, "artist": "", "album": ""},
    ]
    group = _derive_folder_group("some-download-folder", files)
    assert group["artist"] == ""
    assert group["album"] == ""
    assert group["group_key"] == "some-download-folder"


def test_derive_folder_group_falls_back_to_path_for_mixed_albums():
    from services.downloads.download_folder_service import _derive_folder_group

    files = [
        {"is_audio": True, "artist": "Artist A", "album": "Album 1"},
        {"is_audio": True, "artist": "Artist B", "album": "Album 2"},
    ]
    group = _derive_folder_group("mixed-downloads", files)
    assert group["artist"] == ""
    assert group["album"] == ""
    assert group["group_key"] == "mixed-downloads"


# ---------------------------------------------------------------------------
# 2. Failed queue items are never stuck
# ---------------------------------------------------------------------------

@pytest.fixture()
def queue_env(monkeypatch):
    """Fresh SQLite DB with a download_queue table."""
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT,
                title TEXT,
                status TEXT DEFAULT 'queued',
                file_path TEXT,
                retry_count INTEGER DEFAULT 0,
                retry_delay_minutes INTEGER DEFAULT 30,
                failure_reason TEXT,
                next_retry_at TEXT,
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
        "db.repositories.queue.db_session",
        lambda *a, **kw: _Session(session),
    )
    return engine


def test_failed_items_requeue_even_with_many_prior_attempts(queue_env):
    """An item with a high retry_count is still requeued once its window is
    due — retries are unbounded, so nothing is ever left permanently 'failed'.

    ``download_queue.max_retries`` was never enforced (``mark_failed`` ignored
    it) and was dropped in migration 015; this asserts the behaviour that
    column used to advertise.
    """
    from db.repositories.queue import requeue_due_failed_items

    with queue_env.begin() as conn:
        conn.execute(text("""
            INSERT INTO download_queue (id, artist, title, status, retry_count,
                                        retry_delay_minutes, next_retry_at)
            VALUES
                (1, 'A', 'Song One', 'failed', 10, 30, NULL),      -- many attempts, due
                (2, 'B', 'Song Two', 'failed', 0, 30, '2099-01-01')  -- not yet due (future)
        """))

    requeued = requeue_due_failed_items(limit=50)
    ids = [r["id"] for r in requeued]
    # Item 1 is due, so it requeues regardless of how often it already failed;
    # item 2 (future next_retry_at) stays pending.
    assert 1 in ids
    assert 2 not in ids

    with queue_env.begin() as conn:
        row1 = conn.execute(text("SELECT status, retry_count FROM download_queue WHERE id = 1")).fetchone()
        assert row1[0] == "queued"
        assert row1[1] == 11  # retry_count incremented
        row2 = conn.execute(text("SELECT status FROM download_queue WHERE id = 2")).fetchone()
        assert row2[0] == "failed"  # still pending until its retry window


def test_failed_items_requeue_when_due_uses_delay(queue_env):
    """A due failed item is requeued and next_retry_at is pushed forward."""
    from db.repositories.queue import requeue_due_failed_items

    with queue_env.begin() as conn:
        conn.execute(text("""
            INSERT INTO download_queue (id, artist, title, status, retry_count,
                                        retry_delay_minutes, next_retry_at)
            VALUES (1, 'A', 'Song', 'failed', 2, 15, NULL)
        """))

    requeued = requeue_due_failed_items(limit=50)
    assert len(requeued) == 1
    assert requeued[0]["id"] == 1

    with queue_env.begin() as conn:
        row = conn.execute(text("SELECT status, retry_count FROM download_queue WHERE id = 1")).fetchone()
        assert row[0] == "queued"
        assert row[1] == 3


# ---------------------------------------------------------------------------
# 3. MusicBrainz artist+album fallback
# ---------------------------------------------------------------------------

def test_mb_release_group_search_falls_back_to_artist_only(monkeypatch, app):
    """When the combined artist+album query returns nothing, the route falls
    back to an artist-only search so 'Spice Girls' + 'Greatest Hits' still
    surfaces results (old_system parity)."""
    import routes.musicbrainz_routes as routes

    calls = []

    class _FakeClient:
        def get(self, endpoint, *, params=None, timeout=10.0):
            calls.append((endpoint, dict(params or {})))
            query = (params or {}).get("query", "")
            # Combined query (artist + releasegroup) → empty; artist-only → results.
            if "releasegroup" in query:
                return {"release-groups": []}
            if query.startswith('artist:'):
                return {
                    "release-groups": [
                        {
                            "id": "rg-1",
                            "title": "Greatest Hits",
                            "primary-type": "Album",
                            "secondary-types": ["Compilation"],
                            "artist-credit": [{"name": "Spice Girls"}],
                            "first-release-date": "2007-11-05",
                            "cover-art-archive": {"artwork": True, "count": 1},
                        }
                    ]
                }
            return {"release-groups": []}

    monkeypatch.setattr(routes, "_get_mb_client", lambda: _FakeClient())

    # Monkeypatch the request payload via a fake request.
    class _FakeRequest:
        async def get_json(self, *a, **kw):
            return {"artist": "spice girls", "album": "greatest hits"}

    import types
    monkeypatch.setattr(routes, "request", types.SimpleNamespace(
        args={"limit": "25"},
        get_json=_FakeRequest().get_json,
    ))

    from quart import jsonify as _jsonify
    original_jsonify = routes.jsonify

    captured = {}

    def _fake_jsonify(*args, **kwargs):
        payload = args[0] if args else {}
        captured["payload"] = payload
        return original_jsonify(payload)

    monkeypatch.setattr(routes, "jsonify", _fake_jsonify)

    import asyncio

    async def _run():
        ctx = app.app_context()
        await ctx.push()
        try:
            return await routes.api_musicbrainz_search()
        finally:
            await ctx.pop()

    asyncio.run(_run())

    payload = captured.get("payload") or {}
    assert payload.get("success") is True
    releases = payload.get("releases") or []
    assert len(releases) == 1
    assert releases[0]["title"] == "Greatest Hits"
    assert releases[0]["artist"] == "Spice Girls"
    # Both queries were attempted (combined then fallback).
    queries = [c[1].get("query", "") for c in calls]
    assert any("releasegroup" in q for q in queries)
    assert any(q.startswith("artist:") for q in queries)
