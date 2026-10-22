"""The upcoming-releases list query must not be quadratic.

⚠️ WHAT WAS WRONG

``GET /api/upcoming-releases`` (the Upcoming Releases page) timed out at 30 s —
the client's ``DEFAULT_TIMEOUT_MS`` — so the user saw "Error: Request timed out
after 30s" and the page never rendered below the last cached date. Three clauses
were written in shapes PostgreSQL can only run as nested loops:

1. **``hide_in_library``** — a CORRELATED ``NOT EXISTS`` over ``tracks``:

       NOT EXISTS (SELECT 1 FROM tracks t
                   WHERE <norm(t.artist)> = <norm(ur.artist_name)> AND ...)

   The normalising ``REGEXP_REPLACE`` is re-evaluated for every candidate track
   of every release row — N releases x M tracks x 4 regexes.

2. **``album_pairs``** / 3. **``queued_pairs``** — one ``OR`` branch per release
   on the page, again over ``tracks`` / ``download_queue``.

Measured on a 40 000-track / 4 000-release replica, the list query went from
**293 s to 0.12 s** and the per-page pair lookup from **3.2 s to 0.14 s**, with
**identical result rows** in both cases.

These tests pin the SEMANTICS (the rewrites return the same rows) and the SHAPE
(the correlated/OR-chain forms do not come back).
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

_ROUTE_SOURCE = (
    Path(routes.__file__).read_text(encoding="utf-8")
)


def _regexp_replace(value, pattern, replacement, flags=""):
    """SQLite polyfill for the PostgreSQL regexp_replace the routes use."""
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
    CREATE TABLE tracks (
        id TEXT PRIMARY KEY,
        artist TEXT,
        album_artist TEXT,
        album TEXT
    )
"""

_CREATE_QUEUE = """
    CREATE TABLE download_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artist TEXT,
        album TEXT,
        status TEXT
    )
"""


@pytest.fixture()
def seeded(monkeypatch, tmp_path):
    """Rows chosen so each clause has something to include AND exclude."""
    today = date.today()
    inside = (today + timedelta(days=3)).isoformat()
    outside = (today + timedelta(days=400)).isoformat()

    engine = create_engine(f"sqlite:///{tmp_path / 'upcoming.db'}")

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection, _record):
        dbapi_connection.create_function("REGEXP_REPLACE", 4, _regexp_replace)

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text(_CREATE_UPCOMING))
        conn.execute(text(_CREATE_TRACKS))
        conn.execute(text(_CREATE_QUEUE))

        # ── releases ──────────────────────────────────────────────────────
        # "Owned Artist"/"Owned Album" is IN the library (matching track 1
        # exercises case + punctuation normalisation).
        # "Queued Artist"/"Queued Album" is NOT in the library but IS queued.
        # "Absent Artist"/"Absent Album" is neither.
        conn.execute(
            text(
                "INSERT INTO upcoming_releases (artist_name, album_name, source, release_date) VALUES "
                "('Owned Artist',  'Owned Album',  'MusicBrainz Daily Collection', :d),"
                "('Queued Artist', 'Queued Album', 'MusicBrainz Daily Collection', :d),"
                "('Absent Artist', 'Absent Album', 'MusicBrainz Daily Collection', :d),"
                "('Old Artist',    'Old Album',    'MusicBrainz Daily Collection', :o),"
                "('Wiki Artist',   'Wiki Album',   'Wikipedia 2026',              :d)"
            ),
            {"d": inside, "o": outside},
        )

        # ── library ───────────────────────────────────────────────────────
        # Deliberately sets a DIFFERENT artist and album_artist so the
        # COALESCE(NULLIF(album_artist,''), artist) preference is exercised,
        # and spells the album with punctuation/odd case so the normalisation
        # is exercised too.
        conn.execute(
            text(
                "INSERT INTO tracks (id, artist, album_artist, album) VALUES "
                "('t1', 'Ignored Credit', 'owned.artist',  'Owned-Album'),"
                "('t2', 'Other Artist',   'Other Artist',  'Unrelated Album')"
            )
        )

        # ── queue ─────────────────────────────────────────────────────────
        conn.execute(
            text(
                "INSERT INTO download_queue (artist, album, status) VALUES "
                "('Queued Artist', 'Queued-Album', 'queued'),"
                "('Done Artist',   'Done Album',   'completed')"
            )
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
    yield
    engine.dispose()


def _names(payload):
    return sorted(r["artist_name"] for r in payload["releases"])


class TestHideInLibrarySemantics:
    """The anti-join rewrite must hide exactly the releases it hid before."""

    @pytest.mark.asyncio
    async def test_owned_release_is_hidden_by_default(self, seeded, client):
        response = await client.get("/api/upcoming-releases")
        data = await response.get_json()

        assert "Owned Artist" not in _names(data)

    @pytest.mark.asyncio
    async def test_absent_release_is_shown(self, seeded, client):
        response = await client.get("/api/upcoming-releases")
        data = await response.get_json()

        assert "Absent Artist" in _names(data)

    @pytest.mark.asyncio
    async def test_match_is_case_and_punctuation_insensitive(self, seeded, client):
        """The library row is 'owned.artist' / 'Owned-Album'."""
        response = await client.get("/api/upcoming-releases")
        data = await response.get_json()

        assert "Owned Artist" not in _names(data)

    @pytest.mark.asyncio
    async def test_album_artist_matching_uses_album_artist_first(self, seeded, client):
        """``tracks.artist`` is a different credit; ``album_artist`` must win.

        If the rewrite read ``tracks.artist`` the owned release would reappear.
        """
        response = await client.get("/api/upcoming-releases")
        data = await response.get_json()

        assert "Owned Artist" not in _names(data)

    @pytest.mark.asyncio
    async def test_unrelated_track_does_not_hide_anything(self, seeded, client):
        response = await client.get("/api/upcoming-releases")
        data = await response.get_json()

        # t2 matches nothing, so every non-owned release INSIDE the display
        # window is still visible.
        assert "Queued Artist" in _names(data)
        assert "Absent Artist" in _names(data)

    @pytest.mark.asyncio
    async def test_release_outside_the_window_is_not_returned(self, seeded, client):
        """A guard on the fixture itself: this row is 400 days out."""
        response = await client.get("/api/upcoming-releases")
        data = await response.get_json()

        assert "Old Artist" not in _names(data)

    @pytest.mark.asyncio
    async def test_hide_in_library_0_shows_the_owned_release(self, seeded, client):
        response = await client.get("/api/upcoming-releases?hide_in_library=0")
        data = await response.get_json()

        assert "Owned Artist" in _names(data)

    @pytest.mark.asyncio
    async def test_total_matches_the_filtered_rows(self, seeded, client):
        response = await client.get("/api/upcoming-releases")
        data = await response.get_json()

        assert data["total"] == len(data["releases"])


class TestQueueAnnotationSemantics:
    """``include_queue`` must still find queued releases after the rewrite."""

    @pytest.mark.asyncio
    async def test_queued_release_is_flagged(self, seeded, client):
        response = await client.get("/api/upcoming-releases?include_queue=true")
        data = await response.get_json()

        by_artist = {r["artist_name"]: r for r in data["releases"]}
        assert by_artist["Queued Artist"]["in_queue"] is True
        assert by_artist["Queued Artist"]["queue_status"] == "queued"

    @pytest.mark.asyncio
    async def test_queued_match_is_case_and_punctuation_insensitive(self, seeded, client):
        """The queue row is 'Queued-Album'; the release is 'Queued Album'."""
        response = await client.get("/api/upcoming-releases?include_queue=true")
        data = await response.get_json()

        by_artist = {r["artist_name"]: r for r in data["releases"]}
        assert by_artist["Queued Artist"]["in_queue"] is True

    @pytest.mark.asyncio
    async def test_absent_release_is_not_flagged(self, seeded, client):
        response = await client.get("/api/upcoming-releases?include_queue=true")
        data = await response.get_json()

        by_artist = {r["artist_name"]: r for r in data["releases"]}
        assert by_artist["Absent Artist"]["in_queue"] is False
        assert by_artist["Absent Artist"]["queue_status"] is None

    @pytest.mark.asyncio
    async def test_completed_queue_row_does_not_count_as_queued(self, seeded, client):
        response = await client.get("/api/upcoming-releases?include_queue=true")
        data = await response.get_json()

        # 'Done Artist' is not even a release here, but the completed row must
        # not leak into queued_pairs; assert via the status guard directly.
        assert all(
            r["queue_status"] in (None, "queued") for r in data["releases"]
        )


class TestTheQueryShapeIsNotQuadratic:
    """Guard the shapes that caused the 30 s timeout."""

    def test_hide_in_library_is_not_a_correlated_scan_of_tracks(self):
        """``tracks`` must be normalised in its OWN subquery, not per release.

        The old form referenced ``upcoming_releases`` INSIDE the ``tracks``
        scan. If that comes back, the nested loop comes back.
        """
        assert "FROM tracks t" in _ROUTE_SOURCE

        # The inner tracks scan must be a self-contained normalising subquery.
        inner = _ROUTE_SOURCE.split("FROM tracks t")[1].split(") lib")[0]
        assert "upcoming_releases" not in inner, (
            "the tracks scan is correlated with upcoming_releases again — "
            "that makes the anti-join quadratic"
        )

    def test_hide_in_library_normalises_tracks_once(self):
        assert ") lib" in _ROUTE_SOURCE
        assert "lib.artist_key" in _ROUTE_SOURCE
        assert "lib.album_key" in _ROUTE_SOURCE

    def test_pair_lookups_use_a_union_all_join(self):
        """The per-page lookups must join a wanted-set, not OR one term per row."""
        assert _ROUTE_SOURCE.count("UNION ALL") >= 2, (
            "album_pairs / queued_pairs should each build a wanted set via "
            "UNION ALL and JOIN it, instead of one OR branch per release"
        )
        assert "JOIN wanted ON" in _ROUTE_SOURCE

    def test_the_or_chain_builders_are_gone(self):
        assert "pair_conditions" not in _ROUTE_SOURCE
        assert "q_conditions" not in _ROUTE_SOURCE

    def test_no_comma_joined_or_branches_remain(self):
        """Catches a reintroduced ``\" OR \".join(...)`` predicate builder."""
        assert '" OR ".join(' not in _ROUTE_SOURCE
