"""Tests for the album-page MusicBrainz release matching.

Verifies:
  - ``get_musicbrainz_best_release`` returns the legacy frontend contract
    (``releases`` / ``best_release`` / ``confidence`` / ``local_track_count``)
    and that an exact track-count match yields confidence 1.0 (so the album
    page can apply the match directly instead of showing "not confident
    enough").
  - ``compare_musicbrainz_release`` matches the tracks it can (by track
    number + title similarity), flags field diffs (title / track_number /
    mbid / duration) as recommendations, and reports unmatched library tracks
    as ``extra_tracks``.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def _make_engine():
    tmp = tempfile.mkdtemp()
    return create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")


@pytest.fixture()
def mb_env(monkeypatch):
    """Point the MB service's DB reads at a fresh SQLite tracks table and
    stub the shared MB client with deterministic release/track data."""
    engine = _make_engine()
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                artist TEXT,
                album_artist TEXT,
                album TEXT,
                title TEXT,
                track_number TEXT,
                disc_number TEXT,
                year TEXT,
                mbid TEXT,
                file_path TEXT,
                duration TEXT,
                mb_ignored_fields TEXT
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

    # Seed library tracks: 3 tracks matching the MB release (each carrying the
    # recording MBID the MB release has, so no "mbid" diff is flagged), plus one
    # extra.
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO tracks (id, artist, album_artist, album, title, track_number,
                                disc_number, year, mbid, file_path, duration, mb_ignored_fields)
            VALUES
                ('t1', 'Artist', 'Artist', 'Album', 'Song One', '1', '1', '2020',
                 'rec-0001', '/music/a/01 - Song One.mp3', '240000', NULL),
                ('t2', 'Artist', 'Artist', 'Album', 'Song Two', '2', '1', '2020',
                 'rec-0002', '/music/a/02 - Song Two.mp3', '250000', NULL),
                ('t3', 'Artist', 'Artist', 'Album', 'Song Three (Radio Edit)', '3', '1', '2020',
                 'rec-0003', '/music/a/03 - Song Three (Radio Edit).mp3', '180000', NULL),
                ('t4', 'Artist', 'Artist', 'Album', 'Bonus Track', '9', '1', '2020',
                 NULL, '/music/a/09 - Bonus Track.mp3', '200000', NULL)
        """))

    monkeypatch.setattr(
        "db.engine.db_session",
        lambda *a, **kw: _Session(session),
    )
    return engine


def _make_releases_raw():
    """Two releases in the group: an 3-track official (best) and a 1-track promo."""
    return [
        {
            "id": "rel-0000-0000-0000-000000000001",
            "title": "Album",
            "date": "2020-05-01",
            "country": "US",
            "status": "Official",
            "disambiguation": "",
            "media": [
                {"format": "CD", "track-count": 3, "tracks": []},
            ],
        },
        {
            "id": "rel-0000-0000-0000-000000000002",
            "title": "Album (Promo)",
            "date": "2020-01-01",
            "country": "",
            "status": "Promotion",
            "disambiguation": "",
            "media": [
                {"format": "CDr", "track-count": 1, "tracks": []},
            ],
        },
    ]


def _make_release_tracks():
    return {
        "id": "rel-0000-0000-0000-000000000001",
        "title": "Album",
        "date": "2020-05-01",
        "release-group": {"id": "rg-0000-0000-0000-000000000001"},
        "artist-credit": [{"name": "Artist"}],
        "media": [
            {
                "format": "CD",
                "track-count": 3,
                "tracks": [
                    {"position": 1, "number": "1", "title": "Song One", "length": 240000,
                     "recording": {"id": "rec-0001"}},
                    {"position": 2, "number": "2", "title": "Song Two", "length": 250000,
                     "recording": {"id": "rec-0002"}},
                    {"position": 3, "number": "3", "title": "Song Three", "length": 180000,
                     "recording": {"id": "rec-0003"}},
                ],
            }
        ],
    }


def test_best_release_contract_and_confidence(monkeypatch):
    """get_musicbrainz_best_release must return the legacy contract and give
    confidence 1.0 when the best release's track count matches the library."""
    import services.enrichment.musicbrainz_service as svc

    class _FakeClient:
        def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
            return _make_releases_raw()

    monkeypatch.setattr(svc, "get_shared_mb_client", lambda: _FakeClient())
    monkeypatch.setattr(svc, "_get_local_track_count", lambda a, b: 3)

    result = svc.get_musicbrainz_best_release("Artist", "Album", "rg-0001")
    assert result["success"] is True
    # Legacy contract keys the album-page JS depends on.
    assert "releases" in result
    assert "best_release" in result
    assert "confidence" in result
    assert "local_track_count" in result
    assert result["local_track_count"] == 3
    # The 3-track official release wins and matches the local count → 1.0.
    assert result["best_release"]["id"] == "rel-0000-0000-0000-000000000001"
    assert result["confidence"] == 1.0
    # Both releases are present for the picker.
    assert len(result["releases"]) == 2
    # Each release carries the picker fields.
    r = result["releases"][0]
    for key in ("id", "title", "date", "country", "status", "track_count",
                "disc_count", "formats", "cover_art_url"):
        assert key in r


def test_best_release_confidence_scales_down_on_count_mismatch(monkeypatch):
    """A best release whose track count differs from the library gets a
    reduced confidence (old_system parity: 1.0 - diff*0.2)."""
    import services.enrichment.musicbrainz_service as svc

    class _FakeClient:
        def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
            return [{
                "id": "rel-x",
                "title": "Album",
                "date": "2020-05-01",
                "country": "",
                "status": "Official",
                "disambiguation": "",
                "media": [{"format": "CD", "track-count": 5, "tracks": []}],
            }]

    monkeypatch.setattr(svc, "get_shared_mb_client", lambda: _FakeClient())
    monkeypatch.setattr(svc, "_get_local_track_count", lambda a, b: 3)

    result = svc.get_musicbrainz_best_release("Artist", "Album", "rg-0001")
    assert result["success"] is True
    assert result["best_release"]["id"] == "rel-x"
    # diff = 2 → confidence = 1.0 - 2*0.2 = 0.6 (< 0.8 → frontend opens picker).
    assert result["confidence"] == 0.6


def test_compare_musicbrainz_release_matches_and_recommends(monkeypatch, mb_env):
    """The comparison engine matches tracks it can, recommends field fixes and
    reports unmatched library tracks as extra."""
    import services.enrichment.musicbrainz_service as svc

    class _FakeClient:
        def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
            return _make_releases_raw()

        def get_release(self, release_mbid, inc="", timeout=10.0):
            return _make_release_tracks()

    monkeypatch.setattr(svc, "get_shared_mb_client", lambda: _FakeClient())
    # resolve_release_id calls get_release too — return the release directly.
    monkeypatch.setattr(svc, "resolve_release_id", lambda rid: "rel-0000-0000-0000-000000000001")

    result = svc.compare_musicbrainz_release("Artist", "Album", "rg-0001")
    assert result["success"] is True
    comparison = result["comparison"]

    # Track 1 & 2 match exactly (no updates needed).
    by_num = {c["mb_track_number"]: c for c in comparison}
    assert by_num[1]["matched"] is True
    assert by_num[1]["needs_update"] is False
    assert by_num[2]["matched"] is True

    # Track 3: MB title "Song Three" vs library "Song Three (Radio Edit)" —
    # matched, and the edit qualifier is a SOFT version marker: it does not
    # make this a different song and is not proposed as a rename.
    t3 = by_num[3]
    assert t3["matched"] is True
    assert t3["library_title"] == "Song Three (Radio Edit)"
    assert "title" not in t3["diff_fields"]
    assert t3["needs_update"] is False

    # The library-only "Bonus Track" (track 9) is reported as extra.
    extra_titles = [e["library_title"] for e in result["extra_tracks"]]
    assert "Bonus Track" in extra_titles

    # total_tracks counts the comparison entries (3 MB tracks).
    assert result["total_tracks"] == 3


def test_compare_uses_concrete_release_directly(monkeypatch, mb_env):
    """When handed a concrete RELEASE MBID (the shared search modal's release
    picker can pass one), compare_musicbrainz_release must use it directly
    instead of browsing it as a release-group (which 404s on MusicBrainz and
    wastes a slow round-trip — the reported ~2.7s compare)."""
    import services.enrichment.musicbrainz_service as svc

    browsed = []

    class _FakeClient:
        def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
            browsed.append(rg_mbid)
            return _make_releases_raw()

        def get_release(self, release_mbid, inc="", timeout=10.0):
            return _make_release_tracks()

    monkeypatch.setattr(svc, "get_shared_mb_client", lambda: _FakeClient())

    result = svc.compare_musicbrainz_release(
        "Artist", "Album", "rel-0000-0000-0000-000000000001"
    )
    assert result["success"] is True
    # A concrete release MBID must not be browsed as a release-group.
    assert browsed == []
    assert result["release_mbid"] == "rel-0000-0000-0000-000000000001"
    assert len(result["comparison"]) == 3
    by_num = {c["mb_track_number"]: c for c in result["comparison"]}
    assert by_num[1]["matched"] is True


def test_local_track_count_is_case_insensitive(mb_env):
    """_get_local_track_count must find tracks when the URL-decoded artist/
    album names differ in case from the stored values (the album page itself
    uses a LOWER() lookup, so the compare engine must too — otherwise the
    auto-match reports "not confident enough" and the compare reports "No
    library tracks found" for every differently-cased album)."""
    import services.enrichment.musicbrainz_service as svc

    # Exact-case match (control).
    assert svc._get_local_track_count("Artist", "Album") == 4
    # Case-different names (URL-derived) still count the same tracks.
    assert svc._get_local_track_count("artist", "album") == 4
    assert svc._get_local_track_count("ARTIST", "ALBUM") == 4
    # Genuinely missing album → 0.
    assert svc._get_local_track_count("Artist", "Does Not Exist") == 0


def test_compare_musicbrainz_release_case_insensitive_lookup(monkeypatch, mb_env):
    """compare_musicbrainz_release must find library tracks even when the
    artist/album names passed in (from the URL) differ in case from the DB —
    the reported "No library tracks found for this album" regression."""
    import services.enrichment.musicbrainz_service as svc

    class _FakeClient:
        def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
            return _make_releases_raw()

        def get_release(self, release_mbid, inc="", timeout=10.0):
            return _make_release_tracks()

    monkeypatch.setattr(svc, "get_shared_mb_client", lambda: _FakeClient())
    monkeypatch.setattr(svc, "resolve_release_id", lambda rid: "rel-0000-0000-0000-000000000001")

    # Lower-cased names (as a URL slug would produce) must still compare.
    result = svc.compare_musicbrainz_release("artist", "album", "rg-0001")
    assert result["success"] is True
    assert len(result["comparison"]) == 3
    by_num = {c["mb_track_number"]: c for c in result["comparison"]}
    assert by_num[1]["matched"] is True
    assert by_num[2]["matched"] is True


def test_compare_library_query_is_postgres_portable():
    """Regression: ``disc_number`` / ``track_number`` are TEXT columns in
    PostgreSQL, so ``compare_musicbrainz_release``'s library query must not
    COALESCE them with integer literals.  ``COALESCE(disc_number, 1)`` fails
    on Postgres with "COALESCE types text and integer cannot be matched";
    the exception was swallowed and surfaced as the misleading "No library
    tracks found for this album" — even though the album page (which uses
    string literals) showed tracks.  SQLite can't catch this (loose typing),
    so the SQL is asserted directly."""
    import services.enrichment.musicbrainz_service as svc

    sql = svc._COMPARE_LIBRARY_TRACKS_SQL
    # Must use string literals so the query runs on PostgreSQL.
    assert "COALESCE(disc_number, '1')" in sql
    assert "COALESCE(track_number, '999')" in sql
    # The exact regressions must never come back.
    assert "COALESCE(disc_number, 1)" not in sql
    assert "COALESCE(track_number, 999)" not in sql
