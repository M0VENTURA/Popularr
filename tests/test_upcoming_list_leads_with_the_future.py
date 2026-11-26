"""The Upcoming list must lead with releases that have NOT happened yet.

Reported: *"The dates on the dashboard are still wrong, it's late november and
showing october and one week of november."*

The display window opens 28 days BACK. The query ordered purely ascending from
that start, so page 1 (limit 50) was filled by the OLDEST rows in the window —
releases that had already come out — and nothing genuinely upcoming ever
reached the top of the card. The client then regrouped the rows by month
ascending, which put October above November even when the server order was
right.
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
    / "test_site" / "static" / "js" / "services" / "upcoming-releases.js"
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
def many_months(monkeypatch, tmp_path):
    """A realistic mix: a big block of past releases, then future ones."""
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

        params = []
        # 60 releases spread over the PAST 28 days — more than one page.
        for offset in range(28, 0, -1):
            d = (today - timedelta(days=offset)).isoformat()
            for i in range(2):
                params.append({
                    "a": f"Past Band {offset}-{i}",
                    "al": f"Past Album {offset}-{i}",
                    "d": d,
                })
        # Then the near future.
        for offset in range(1, 15):
            d = (today + timedelta(days=offset)).isoformat()
            params.append({"a": f"Future Band {offset}", "al": f"Future Album {offset}", "d": d})
        # A year-only row and a TBA row.
        params.append({"a": "Year Band", "al": "Year Album", "d": str(today.year)})
        params.append({"a": "Tba Band", "al": "Tba Album", "d": None})

        for p in params:
            conn.execute(
                text(
                    "INSERT INTO upcoming_releases (artist_name, album_name, source, release_date) "
                    "VALUES (:a, :al, 'MusicBrainz Daily Collection', :d)"
                ),
                p,
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


class TestTheListLeadsWithTheFuture:
    @pytest.mark.asyncio
    async def test_no_past_release_is_on_the_first_page(self, many_months, client):
        today = many_months
        response = await client.get("/api/upcoming-releases?limit=50&hide_in_library=0")
        data = await response.get_json()

        dates = [r["release_date"] for r in data["releases"] if r.get("release_date")]
        dated = [d for d in dates if len(d) == 10]
        assert dated, "expected some dated releases"
        assert all(d >= today.isoformat() for d in dated[:10]), (
            "the first rows are past releases — the card shows October in "
            f"late November: {dated[:10]}"
        )

    @pytest.mark.asyncio
    async def test_the_soonest_future_release_comes_first(self, many_months, client):
        today = many_months
        response = await client.get("/api/upcoming-releases?limit=5&hide_in_library=0")
        data = await response.get_json()

        dated = [r["release_date"] for r in data["releases"] if r.get("release_date")]
        assert dated[0] == (today + timedelta(days=1)).isoformat(), (
            f"expected tomorrow first, got {dated[0]}"
        )

    @pytest.mark.asyncio
    async def test_year_only_and_tba_rows_come_after_dated_ones(self, many_months, client):
        response = await client.get("/api/upcoming-releases?limit=100&hide_in_library=0")
        data = await response.get_json()

        dates = [r["release_date"] for r in data["releases"]]
        dated_positions = [i for i, d in enumerate(dates) if d and len(d) == 10]
        yearonly_positions = [i for i, d in enumerate(dates) if d and len(d) == 4]
        tba_positions = [i for i, d in enumerate(dates) if not d]

        if dated_positions and yearonly_positions:
            assert max(dated_positions) < min(yearonly_positions)
        if tba_positions and yearonly_positions:
            assert max(yearonly_positions) < min(tba_positions)


class TestTheOrderingIsInTheQuery:
    def test_the_query_orders_by_upcoming_not_ascending(self):
        assert ":today_iso" in _ROUTE_SOURCE
        assert "release_date >= :today_iso THEN 0" in _ROUTE_SOURCE

    def test_the_month_grouping_puts_the_future_first(self):
        """The UI regrouped by month ascending, undoing the server order."""
        assert "monthDepth" in _JS, (
            "the month keys are sorted plainly again — October (the start of the "
            "28-day lookback) will sit above the current month"
        )
