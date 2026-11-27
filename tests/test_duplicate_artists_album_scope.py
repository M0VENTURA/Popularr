"""Duplicate artists must be ALBUM-ARTIST identity, scoped, and never VA.

REPORTED: one row listed ~90 "variations" — Red Hot Chili Peppers, Green Day,
Gomez, Skulker… — 5174 tracks, with Merge offering to collapse them all. Clicking
it would have rewritten the TRACK and ALBUM artist of every one of those bands.

CAUSE

Two defects in `api_get_duplicate_artists`:

  1. It grouped by ``artist`` (the PERFORMER), so on a Various Artists compilation
     every track was a "variation" of any row sharing a legacy
     ``musicbrainz_artistid``.
  2. It had NO artist scope — the request names one artist, the response was
     library-wide.

Tests drive the REAL route through a temp DB, the same harness the existing
corrections tests use.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

VA_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"


@pytest.fixture()
def dup_endpoint(tmp_path, monkeypatch):
    """Seed ``tracks`` and route the endpoint's db_session at a temp DB."""
    from contextlib import contextmanager

    import routes.misc_routes as misc
    from db.models import Track

    url = f"sqlite:///{tmp_path / 'dup.db'}"
    seed_engine = create_engine(url, connect_args={"check_same_thread": False})
    Track.__table__.create(seed_engine)

    def _seed(rows) -> None:
        """rows: (track_id, track_artist, album_artist, album, artist_mbid, album_artist_mbid)."""
        with seed_engine.begin() as conn:
            conn.execute(text("DELETE FROM tracks"))
            for tid, track_artist, album_artist, album, amb, aamb in rows:
                conn.execute(
                    text("""
                        INSERT INTO tracks
                            (id, artist, album_artist, album, title,
                             musicbrainz_artistid, musicbrainz_albumartistid)
                        VALUES (:id, :ta, :aa, :al, 'T', :amb, :aamb)
                    """),
                    {"id": tid, "ta": track_artist, "aa": album_artist,
                     "al": album, "amb": amb, "aamb": aamb},
                )

    request_engine = create_engine(url, connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=request_engine, expire_on_commit=False)

    @contextmanager
    def _fake_db_session():
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    class _StubMb:
        def get_artist(self, _mbid):
            return {"name": "Red Hot Chili Peppers"}

    monkeypatch.setattr(misc, "db_session", _fake_db_session)
    monkeypatch.setattr(misc, "get_shared_mb_client", lambda: _StubMb())
    try:
        yield _seed
    finally:
        request_engine.dispose()
        seed_engine.dispose()


class TestAVariousArtistsCompilationIsNotADuplicateArtist:
    async def test_a_shared_performer_mbid_does_not_drag_in_other_acts(
        self, client, dup_endpoint
    ):
        dup_endpoint([
            ("v1", "Red Hot Chili Peppers", "Various Artists", "Comp A", "mb-1", VA_MBID),
            ("v2", "Green Day", "Various Artists", "Comp A", "mb-1", VA_MBID),
            ("v3", "Gomez", "Various Artists", "Comp B", "mb-1", VA_MBID),
        ])
        payload = await (await client.get("/api/duplicate-artists/Red Hot Chili Peppers")).get_json()
        assert payload["success"] is True
        for dup in payload["duplicates"]:
            assert "Various Artists" not in dup["variations"], dup
            assert "Green Day" not in dup["variations"], dup
            assert "Gomez" not in dup["variations"], dup


class TestGenuineSpellingsAreStillFound:
    async def test_two_spellings_of_one_album_artist_are_a_duplicate(
        self, client, dup_endpoint
    ):
        dup_endpoint([
            ("b1", "blink-182", "blink-182", "Enema", "mb-2", "mb-2"),
            ("b2", "Blink 182", "Blink 182", "Enema", "mb-2", "mb-2"),
        ])
        payload = await (await client.get("/api/duplicate-artists/blink-182")).get_json()
        assert len(payload["duplicates"]) == 1, payload
        variations = set(payload["duplicates"][0]["variations"])
        assert {"blink-182", "Blink 182"} <= variations, variations


class TestAScopedRequestCannotDescribeAnotherAct:
    async def test_a_polluted_performer_mbid_does_not_join_unrelated_acts(
        self, client, dup_endpoint
    ):
        """Two DIFFERENT album artists share a polluted PERFORMER MBID.

        The panel is about album-artist identity, so that is not a duplicate —
        and it must never offer to merge them.
        """
        dup_endpoint([
            ("n1", "Noiseworks", "Noiseworks", "A", "mb-3", ""),
            ("x1", "Unrelated", "Unrelated", "B", "mb-3", ""),
        ])
        payload = await (await client.get("/api/duplicate-artists/Noiseworks")).get_json()
        variations = [v for d in payload["duplicates"] for v in d["variations"]]
        assert "Unrelated" not in variations, variations


class TestAPlaceholderIsNeverAMergeTarget:
    async def test_merging_into_various_artists_is_refused(self, client):
        resp = await client.post(
            "/api/duplicate-artists/merge",
            json={"new_artist": "Various Artists", "source_artists": ["A", "B"]},
        )
        assert resp.status_code == 400
        body = await resp.get_json()
        assert body["success"] is False

    def test_the_helper_recognises_the_placeholders(self):
        from routes.misc_routes import _is_placeholder_artist

        for name in ("Various Artists", "unknown artist", "VA", "soundtrack"):
            assert _is_placeholder_artist(name) is True
        assert _is_placeholder_artist("Red Hot Chili Peppers") is False
