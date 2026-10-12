"""Scan-pipeline DB-first persistence (audit G3) + MB conditional GETs (G7).

* **G3** — the album-tracklist recording identity (which RECORDING each
  library track is) was resolved from ListenBrainz ONCE per album per pass
  and persisted WITHOUT the MBID (no column existed), so every later pass
  re-ran the lookup. The MBID now round-trips through
  ``track_popularity_cache``: the runner persists it, the reader returns it,
  and the artist-prefetch writer can never clobber a known MBID with NULL.

* **G7** — the MusicBrainz client re-downloaded identical bodies on every
  new process. It now revalidates with ``If-None-Match``: a 304 serves the
  cached payload, and the cache key includes EVERY query param so two
  different queries can never collide onto one payload.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _strip_python(src: str) -> str:
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


# ---------------------------------------------------------------------------
# G3 — the recording MBID round-trips through the popularity cache
# ---------------------------------------------------------------------------


@pytest.fixture
def pop_cache_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/pop.db")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE track_popularity_cache (
                id INTEGER PRIMARY KEY,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                lastfm_listeners INTEGER DEFAULT 0,
                lastfm_playcount BIGINT DEFAULT 0,
                listenbrainz_listens INTEGER DEFAULT 0,
                listenbrainz_users INTEGER DEFAULT 0,
                lastfm_tags TEXT,
                source TEXT DEFAULT 'bulk',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                recording_mbid TEXT,
                release_track_title TEXT,
                CONSTRAINT uq_track_popularity_artist_title UNIQUE (artist, title)
            )
        """))
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _session(*args, **kwargs):
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    import db.repositories.popularity_cache as repo

    monkeypatch.setattr(repo, "db_session", _session)
    yield _session


class TestRecordingMbidRoundTrips:
    def test_the_schema_declares_the_column(self):
        schema = (REPO_ROOT / "db" / "schema.py").read_text(encoding="utf-8")
        block = schema.split('"track_popularity_cache"', 1)[1].split('"""', 2)[1]
        assert "recording_mbid TEXT" in block, (
            "without the column the album-tracklist identity dies with the "
            "process and every pass re-runs the ListenBrainz lookup"
        )

    def test_written_then_read_back_as_an_entry(self, pop_cache_db):
        from db.repositories.popularity_cache import (
            get_cached_popularity_for_titles,
            upsert_track_popularity_bulk,
        )

        upsert_track_popularity_bulk([{
            "artist": "Band", "title": "Ghost Song",
            "lastfm_listeners": 5000, "listenbrainz_listens": 900,
            "source": "album_tracklist",
            "recording_mbid": "rec-123",
        }])
        out = get_cached_popularity_for_titles("Band", ["Ghost Song"])
        entry = out.get("ghost song")
        assert entry, "the row must be returned"
        from services.popularity.popularity_cache_service import _row_to_entry

        mapped = _row_to_entry(entry)
        assert mapped.get("recording_mbid") == "rec-123"
        assert mapped.get("source") == "album_tracklist"

    def test_the_prefetch_writer_never_clobbers_a_known_mbid(self, pop_cache_db):
        from db.repositories.popularity_cache import (
            get_cached_popularity_for_titles,
            upsert_track_popularity_bulk,
        )

        upsert_track_popularity_bulk([{
            "artist": "Band", "title": "Ghost Song",
            "lastfm_listeners": 5000, "source": "album_tracklist",
            "recording_mbid": "rec-123",
        }])
        # The artist-prefetch writer carries NO mbid (listeners only) and runs
        # far more often — its upsert must not wipe the identity.
        upsert_track_popularity_bulk([{
            "artist": "Band", "title": "Ghost Song",
            "lastfm_listeners": 5100, "source": "bulk",
        }])
        row = get_cached_popularity_for_titles("Band", ["Ghost Song"])["ghost song"]
        assert row.get("recording_mbid") == "rec-123", (
            "COALESCE must keep the known MBID over a NULL"
        )
        assert int(row.get("lastfm_listeners") or 0) == 5100, "counts still refresh"

    def test_a_new_mbid_still_overwrites(self, pop_cache_db):
        from db.repositories.popularity_cache import (
            get_cached_popularity_for_titles,
            upsert_track_popularity_bulk,
        )

        upsert_track_popularity_bulk([{
            "artist": "Band", "title": "Ghost Song", "recording_mbid": "rec-old",
        }])
        upsert_track_popularity_bulk([{
            "artist": "Band", "title": "Ghost Song", "recording_mbid": "rec-new",
        }])
        row = get_cached_popularity_for_titles("Band", ["Ghost Song"])["ghost song"]
        assert row.get("recording_mbid") == "rec-new"

    def test_the_runner_persists_the_identity_with_the_tracklist_rows(self):
        path = REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"
        src = _strip_python(path.read_text(encoding="utf-8"))
        assert '"recording_mbid": str(_cur.get("recording_mbid") or "")' in src, (
            "the album-tracklist persist block must carry the identity, or "
            "the column stays empty and the LB lookup runs every pass"
        )

    def test_the_migration_is_dialect_portable(self):
        mig = (
            REPO_ROOT / "migrations" / "versions"
            / "018_track_popularity_cache_recording_mbid.py"
        ).read_text(encoding="utf-8")
        # Strip comments/docstrings FIRST: the migration's docstring NAMES
        # "ADD COLUMN IF NOT EXISTS" to explain why it is not used — the
        # recorded self-quoting guard trap.
        body = _strip_python(mig)
        assert "sa.inspect" in body, (
            "ADD COLUMN IF NOT EXISTS is PostgreSQL-only and the chain also "
            "runs on SQLite — the 014/016 inspector pattern is mandatory"
        )
        assert "ADD COLUMN IF NOT EXISTS" not in body


# ---------------------------------------------------------------------------
# G7 — conditional GETs on the MusicBrainz client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, etag=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"etag": etag} if etag else {}
        self.url = type("U", (), {"path": "/ws/2/whatever"})()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no body — a 304 must never reach .json()")
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return self.responses.pop(0)


def _client(session):
    from api_clients.musicbrainz_http import MusicBrainzHttpClient, _ETAG_CACHE

    _ETAG_CACHE.clear()
    return MusicBrainzHttpClient(http_session=session, enabled=True)


class TestConditionalGets:
    def test_the_first_request_has_no_validator_and_caches_the_etag(self):
        session = _FakeSession([
            _FakeResponse(200, {"x": 1}, etag='"abc"'),
        ])
        client = _client(session)

        assert client.get("recording/abc", params={"fmt": "json"}) == {"x": 1}
        assert "If-None-Match" not in session.requests[0]["headers"]

    def test_the_identical_request_revalidates_and_serves_the_cached_body(self):
        session = _FakeSession([
            _FakeResponse(200, {"x": 1}, etag='"abc"'),
            # 304 has NO body: json() raises if it is ever called.
            _FakeResponse(304),
        ])
        client = _client(session)

        client.get("recording/abc", params={"fmt": "json"})
        result = client.get("recording/abc", params={"fmt": "json"})

        assert result == {"x": 1}, "the cached payload is served on 304"
        assert session.requests[1]["headers"].get("If-None-Match") == '"abc"'

    def test_different_params_never_share_a_validator(self):
        session = _FakeSession([
            _FakeResponse(200, {"inc": "a"}, etag='"a"'),
            _FakeResponse(200, {"inc": "b"}, etag='"b"'),
            _FakeResponse(200, {"inc": "a"}, etag='"a"'),
        ])
        client = _client(session)

        client.get("recording/abc", params={"fmt": "json", "inc": "a"})
        client.get("recording/abc", params={"fmt": "json", "inc": "b"})
        # The inc=a revalidation carries ONLY inc=a's etag.
        client.get("recording/abc", params={"fmt": "json", "inc": "a"})
        assert "If-None-Match" not in session.requests[1]["headers"], (
            "a different query must be a fresh request, never a 304 of another"
        )
        assert session.requests[2]["headers"].get("If-None-Match") == '"a"'

    def test_an_untagged_response_is_not_cached(self):
        session = _FakeSession([
            _FakeResponse(200, {"x": 1}),
            _FakeResponse(200, {"x": 2}),
        ])
        client = _client(session)

        client.get("recording/abc", params={"fmt": "json"})
        client.get("recording/abc", params={"fmt": "json"})
        assert "If-None-Match" not in session.requests[1]["headers"], (
            "without an etag there is nothing to revalidate with"
        )
        assert session.requests[1]["headers"].get("If-None-Match") is None

    def test_clear_caches_clears_the_validator_layer(self):
        session = _FakeSession([
            _FakeResponse(200, {"x": 1}, etag='"abc"'),
            _FakeResponse(200, {"x": 2}, etag='"def"'),
        ])
        client = _client(session)
        client.get("recording/abc", params={"fmt": "json"})
        client.clear_caches()
        client.get("recording/abc", params={"fmt": "json"})
        assert "If-None-Match" not in session.requests[1]["headers"]
