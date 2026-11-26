"""The dashboard card must show BOTH upcoming and the last few weeks.

Reported: *"it's not showing the last 3 weeks of the dashboard."*

The card is 50 rows. Ordering strictly future-first (correct for the dedicated
Upcoming page) meant months of upcoming releases filled it and nothing from the
recent past appeared — releases that came out days ago, which the user still
wants to see. ``?split=recent`` fetches both halves with their own LIMIT so
neither can crowd the other out.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from routes import upcoming_releases_routes as routes

_ROUTE_SOURCE = Path(routes.__file__).read_text(encoding="utf-8")
_JS = (
    Path(__file__).resolve().parents[1]
    / "test_site" / "static" / "js" / "pages" / "dashboard.js"
).read_text(encoding="utf-8")


def _regexp_replace(value, pattern, replacement, flags=""):
    if value is None:
        return None
    fl = re.IGNORECASE if flags and "i" in flags else 0
    return re.sub(pattern, replacement, value or "", flags=fl)


_CREATE_UPCOMING = """
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

_CREATE_TRACKS = """
    CREATE TABLE tracks (id TEXT PRIMARY KEY, artist TEXT, album_artist TEXT, album TEXT)
"""

_CREATE_QUEUE = """
    CREATE TABLE download_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT, album TEXT, status TEXT)
"""


@pytest.fixture()
def crowded(monkeypatch, tmp_path):
    """60 future releases INSIDE the display lookahead, plus the last three weeks.

    The display window is 28 days back / 14 forward, so the only way the card
    can be crowded out is by future releases within those 14 days — several per
    day, which a broad global discovery pass produces.
    """
    today = date.today()
    engine = create_engine(f"sqlite:///{tmp_path / 'upcoming.db'}")

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection, _record):
        dbapi_connection.create_function("REGEXP_REPLACE", 4, _regexp_replace)

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text(_CREATE_UPCOMING))
        conn.execute(text(_CREATE_TRACKS))
        conn.execute(text(_CREATE_QUEUE))

        # 60 FUTURE releases inside the 14-day lookahead: enough to fill a
        # 50-row page on their own.
        for offset in range(1, 15):
            for n in range(5):
                conn.execute(
                    text("INSERT INTO upcoming_releases (artist_name, album_name, source, release_date) "
                         "VALUES (:a, :al, 'MusicBrainz Discovery', :d)"),
                    {
                        "a": f"Future {offset}-{n}",
                        "al": f"Future Album {offset}-{n}",
                        "d": (today + timedelta(days=offset)).isoformat(),
                    },
                )
        # And the last three weeks — the reported missing rows.
        for offset in range(1, 6):
            conn.execute(
                text("INSERT INTO upcoming_releases (artist_name, album_name, source, release_date) "
                     "VALUES (:a, :al, 'MusicBrainz Discovery', :d)"),
                {
                    "a": f"Recent {offset}",
                    "al": f"Recent Album {offset}",
                    "d": (today - timedelta(days=offset * 4)).isoformat(),
                },
            )

    @contextmanager
    def _db_session(*_a, **_k):
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(routes, "db_session", _db_session)
    yield today
    engine.dispose()


class TestTheCardShowsBothHalves:
    @pytest.mark.asyncio
    async def test_split_recent_includes_the_last_weeks(self, crowded, client):
        today = crowded
        response = await client.get(
            "/api/upcoming-releases?limit=50&hide_in_library=0&split=recent"
        )
        data = await response.get_json()

        dates = [
            r["release_date"] for r in data["releases"]
            if r.get("release_date") and len(r["release_date"]) == 10
        ]
        past = [d for d in dates if d < today.isoformat()]

        assert past, (
            "the card shows no release from the last few weeks — 80 future "
            "rows filled every slot"
        )

    @pytest.mark.asyncio
    async def test_split_recent_still_includes_upcoming(self, crowded, client):
        today = crowded
        response = await client.get(
            "/api/upcoming-releases?limit=50&hide_in_library=0&split=recent"
        )
        data = await response.get_json()

        dates = [
            r["release_date"] for r in data["releases"]
            if r.get("release_date") and len(r["release_date"]) == 10
        ]
        future = [d for d in dates if d > today.isoformat()]

        assert future, "the card lost its upcoming half"

    @pytest.mark.asyncio
    async def test_the_reported_shape_without_split(self, crowded, client):
        """Guard the regression: future-only ordering hides the recent weeks."""
        today = crowded
        response = await client.get(
            "/api/upcoming-releases?limit=50&hide_in_library=0"
        )
        data = await response.get_json()

        dates = [
            r["release_date"] for r in data["releases"]
            if r.get("release_date") and len(r["release_date"]) == 10
        ]
        past = [d for d in dates if d < today.isoformat()]

        assert past == [], (
            "without split, a crowded future WOULD hide the recent weeks — "
            "which is the bug; if this ever fails the ordering changed"
        )


class TestTheRouteAdvertisesTheMode:
    def test_split_is_parsed_and_reported(self):
        assert 'request.args.get("split")' in _ROUTE_SOURCE
        assert '"split": split_mode or "upcoming"' in _ROUTE_SOURCE

    def test_the_dashboard_asks_for_both_halves(self):
        assert "split: 'recent'" in _JS
