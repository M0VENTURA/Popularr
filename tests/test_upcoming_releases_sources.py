"""Tests for the upcoming-releases Source filter API (issue #877).

Verifies that:
  - ``GET /api/upcoming-releases/sources`` lists each scraper-rule key with a
    release count (and groups MusicBrainz rows under a pseudo-key);
  - ``GET /api/upcoming-releases?source=<key>`` filters by the exact rule.
"""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from routes import upcoming_releases_routes as routes


def _regexp_replace(text, pattern, replacement, flags=""):
    """SQLite REGEXP_REPLACE polyfill used by the hide_in_library clause."""
    if text is None:
        return None
    fl = re.IGNORECASE if flags and "i" in flags else 0
    return re.sub(pattern, replacement, text or "", flags=fl)

_CREATE_TABLE = """
    CREATE TABLE upcoming_releases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artist_name TEXT NOT NULL,
        album_name TEXT NOT NULL,
        source TEXT NOT NULL,
        source_key TEXT,
        release_date TEXT,
        release_year INTEGER,
        artist_in_collection BOOLEAN DEFAULT 0,
        album_in_collection BOOLEAN DEFAULT 0,
        release_group_mbid TEXT,
        match_source TEXT,
        primary_type TEXT,
        mbid_match_status TEXT DEFAULT 'unmatched',
        mbid_source TEXT,
        mbid_confidence TEXT,
        mbid_match_score REAL,
        mbid_last_checked_at TEXT,
        mbid_manual_override BOOLEAN DEFAULT 0,
        status TEXT DEFAULT 'discovered',
        last_seen_at TEXT,
        created_at TEXT,
        updated_at TEXT
    )
"""


@pytest.fixture()
def seeded_db(monkeypatch):
    """Point the upcoming-releases routes at a file-backed SQLite DB with rows
    from several scraper rules plus a MusicBrainz row."""
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")

    @event.listens_for(engine, "connect")
    def _register_functions(dbapi_connection, _record):
        dbapi_connection.create_function("REGEXP_REPLACE", 4, _regexp_replace)

    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text(_CREATE_TABLE))
        # Dates relative to "today" so the rows stay inside the scraper's
        # rolling display window no matter when the suite runs.
        #
        # The offsets are derived from the live window rather than hard-coded:
        # they were previously 30/60/90 days, which silently fell outside once
        # the default lookahead was narrowed to two weeks, so these tests began
        # failing for a reason that had nothing to do with source filtering.
        # Spreading them across the window keeps them meaningful for ANY
        # configured window size.
        from datetime import date, timedelta

        from services.upcoming_releases.wikipedia_scraper_service import get_release_window

        _today = date.today()
        _win_start, _win_end = get_release_window()
        _span = max(1, (_win_end.date() - _today).days)
        # Always strictly inside the window, and never after _win_end.
        d_kpop = (_today + timedelta(days=max(0, _span // 3))).isoformat()
        d_metal = (_today + timedelta(days=max(0, (2 * _span) // 3))).isoformat()
        d_mb = _win_end.strftime("%Y-%m-%d")
        assert _win_start.date() <= _today, "the display window must include today"
        conn.execute(text(
            "INSERT INTO upcoming_releases (artist_name, album_name, source, source_key, release_date) VALUES "
            f"('Kpop Artist', 'Al1', 'K-Pop/Korean Music 2026', '2026_kpop', '{d_kpop}'),"
            f"('Metal Artist', 'Al2', 'Heavy Metal 2026', '2026_heavy_metal', '{d_metal}'),"
            f"('Metal Artist 2', 'Al3', 'Heavy Metal 2026', '2026_heavy_metal', NULL),"
            f"('MB Artist', 'Al4', 'MusicBrainz Daily Collection', NULL, '{d_mb}')"
        ))
        # Minimal tracks table so the route's hide_in_library NOT EXISTS clause
        # (which queries tracks.artist/album_artist/album) can run.
        conn.execute(text(
            "CREATE TABLE tracks ("
            " id TEXT PRIMARY KEY,"
            " artist TEXT,"
            " album_artist TEXT,"
            " album TEXT"
            ")"
        ))

    @contextmanager
    def _db_session(*_args, **_kwargs):
        session = sess_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(routes, "db_session", _db_session)
    yield
    engine.dispose()


@pytest.mark.asyncio
async def test_sources_endpoint(seeded_db, client):
    response = await client.get("/api/upcoming-releases/sources")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["success"] is True
    by_key = {s["key"]: s["count"] for s in data["sources"]}
    assert by_key["2026_heavy_metal"] == 2
    assert by_key["2026_kpop"] == 1
    assert by_key["musicbrainz_daily_collection"] == 1


@pytest.mark.asyncio
async def test_source_filter_by_rule_key(seeded_db, client):
    response = await client.get("/api/upcoming-releases?source=2026_heavy_metal")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["total"] == 2
    artists = [r["artist_name"] for r in data["releases"]]
    assert artists == ["Metal Artist", "Metal Artist 2"]


@pytest.mark.asyncio
async def test_source_filter_musicbrainz(seeded_db, client):
    response = await client.get("/api/upcoming-releases?source=musicbrainz_daily_collection")
    assert response.status_code == 200
    data = await response.get_json()
    artists = [r["artist_name"] for r in data["releases"]]
    assert artists == ["MB Artist"]


@pytest.mark.asyncio
async def test_source_filter_all_returns_everything(seeded_db, client):
    response = await client.get("/api/upcoming-releases?source=all")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["total"] == 4
