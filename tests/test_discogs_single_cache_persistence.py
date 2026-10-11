"""Regression tests: a Discogs single resolved on demand persists to the cache.

The artist-releases endpoint does not carry the single/EP TYPE token, so
``_fetch_discogs_releases`` used to GUESS ``"album"`` for every format-less
row. Two rules then trusted that guess:

1. the guess re-applied itself on every 7-day re-prefetch (the upsert's
   ``ON CONFLICT ... release_type = EXCLUDED.release_type``), so even a
   scan-time on-demand correction could not survive;
2. the singles fast path (``get_artist_single_titles``, ``release_type IN
   ('single','ep')``) never saw the title, so EVERY scan re-paid the Discogs
   release-detail call the on-demand resolution makes.

The fix, pinned here:

* a format-less row stores an UNKNOWN classification (``release_type = ''``),
  never a guessed ``"album"``;
* the upsert preserves a known ``release_type``/``is_promo`` pair over an
  incoming unknown — facts still win over old facts;
* ``persist_resolved_discogs_single`` writes a scan-confirmed single back
  (UPDATE-only), and ``get_single_status`` calls it for every confirmed
  single (EP-lead promotions deliberately excluded).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeReleasesHttp:
    """Artist-releases list with one format-less row (no ``type``/``main_release``)."""

    def __init__(self, token="", **kw):
        self.token = token
        self.releases = [
            # Format-less row: what a non-master release (or one past the
            # 15-master cap) looks like on the artist-releases endpoint.
            {"title": "Ghost Song", "role": "Main", "id": "777", "year": 2006},
            {"title": "Other Cut", "role": "Main", "id": "778", "year": 2006},
            {"title": "Known Album", "format": ["CD", "Album"], "role": "Main", "id": "779", "year": 2004},
            {"title": "Known Single", "format": ["Vinyl", "7\"", "Single"], "role": "Main", "id": "780", "year": 2006},
            {"title": "Guest Spot", "role": "Appearance", "id": "781", "year": 2006},
        ]
        self.release_details = {
            "777": {
                "formats": [{"name": "CD", "descriptions": ["Single"]}],
                "tracklist": [{"position": "1", "title": "Ghost Song"}],
            },
        }

    def search_database(self, params, timeout=10.0):
        return []

    def get_artist_releases(self, artist_id, per_page=100, timeout=10.0):
        return list(self.releases)

    def get_artist_releases_all(self, artist_id, max_pages=10):
        return list(self.releases)

    def get_release(self, release_id):
        return self.release_details.get(str(release_id))


def _svc(http=None):
    from services.enrichment.discogs_service import DiscogsService

    svc = DiscogsService(
        token="test-token", http_client=http or _FakeReleasesHttp(token="test-token")
    )
    svc.get_artist_id = lambda artist, timeout=10.0: "640496"
    return svc


def _patch_discogs_env(monkeypatch, http):
    monkeypatch.setattr(
        "api_clients.discogs_http.DiscogsHttpClient", lambda **kw: http
    )
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"api_integrations": {"discogs": {"token": "test-token"}}},
    )


# ---------------------------------------------------------------------------
# File-backed cache DB fixture (same pattern as test_release_category_persistence)
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/cache.db")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE artist_release_cache (
                id INTEGER PRIMARY KEY,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                release_type TEXT,
                category TEXT,
                source TEXT NOT NULL,
                release_id TEXT,
                year INTEGER,
                is_promo BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_artist_release_artist_title_source UNIQUE (artist, title, source)
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

    from services.popularity import release_cache_service as rcs

    monkeypatch.setattr(rcs, "db_session", _session)

    def _open():
        return _session()

    yield _open


def _seed(session, artist, title, release_type, release_id, is_promo=False, source="discogs"):
    session.execute(
        text("""
            INSERT INTO artist_release_cache
                (artist, title, release_type, category, source, release_id, year, is_promo)
            VALUES (:artist, :title, :rtype, 'album', :source, :rid, 2006, :promo)
        """),
        {"artist": artist, "title": title, "rtype": release_type, "source": source,
         "rid": release_id, "promo": is_promo},
    )


def _row(_open, artist, title, release_id=None):
    with _open() as session:
        sql = "SELECT * FROM artist_release_cache WHERE artist = :a AND title = :t"
        params = {"a": artist, "t": title}
        if release_id is not None:
            sql += " AND release_id = :rid"
            params["rid"] = release_id
        row = session.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# A. Format-less releases are stored as UNKNOWN, never as a guessed "album"
# ---------------------------------------------------------------------------

class TestFormatLessIsNeverGuessedAsAlbum:
    def test_fetch_discogs_releases_stores_unknown(self, monkeypatch):
        from services.popularity import release_cache_service as rcs

        _patch_discogs_env(monkeypatch, _FakeReleasesHttp())
        rows = rcs._fetch_discogs_releases("Some Artist", "1234")
        by_title = {r["title"]: r for r in rows}
        # The format-less row is UNKNOWN, not a guessed album.
        assert by_title["Ghost Song"]["release_type"] == ""
        assert by_title["Other Cut"]["release_type"] == ""
        # Real format tokens still classify exactly as before.
        assert by_title["Known Album"]["release_type"] == "album"
        assert by_title["Known Single"]["release_type"] == "single"
        # Role=Main only, as before.
        assert "Guest Spot" not in by_title

    def test_upsert_artist_release_rows_stores_unknown(self, monkeypatch, cache_db):
        from services.popularity import release_cache_service as rcs

        rcs.upsert_artist_release_rows(
            "Cache Upsert Artist",
            [
                {"title": "Ghost Song", "role": "Main", "id": "777", "year": 2006},
                {"title": "Known Single", "format": ["CD", "Single"], "role": "Main",
                 "id": "780", "year": 2006},
            ],
        )
        assert _row(cache_db, "Cache Upsert Artist", "Ghost Song")["release_type"] == ""
        assert _row(cache_db, "Cache Upsert Artist", "Known Single")["release_type"] == "single"


# ---------------------------------------------------------------------------
# B. The upsert never lets UNKNOWN overwrite a known classification
# ---------------------------------------------------------------------------

class TestUnknownNeverOverwritesKnown:
    def test_unknown_preserves_stored_single_and_promo(self, cache_db):
        from services.popularity import release_cache_service as rcs

        with cache_db() as session:
            _seed(session, "Preserve Artist", "Ghost Song", "single", "777", is_promo=True)
        # A re-prefetch still sees the release format-less.
        rcs.upsert_artist_release_rows(
            "Preserve Artist",
            [{"title": "Ghost Song", "role": "Main", "id": "777", "year": 2006}],
        )
        row = _row(cache_db, "Preserve Artist", "Ghost Song")
        assert row["release_type"] == "single"
        assert bool(row["is_promo"]) is True

    def test_unknown_preserves_stored_album(self, cache_db):
        from services.popularity import release_cache_service as rcs

        with cache_db() as session:
            _seed(session, "Preserve Album Artist", "Old Album", "album", "900")
        rcs.upsert_artist_release_rows(
            "Preserve Album Artist",
            [{"title": "Old Album", "role": "Main", "id": "900", "year": 2004}],
        )
        assert _row(cache_db, "Preserve Album Artist", "Old Album")["release_type"] == "album"

    def test_fact_still_overwrites_old_fact(self, cache_db):
        from services.popularity import release_cache_service as rcs

        with cache_db() as session:
            _seed(session, "Fact Artist", "Now Known Single", "album", "901")
            _seed(session, "Fact Artist", "Now Known Album", "single", "902")
        rcs.upsert_artist_release_rows(
            "Fact Artist",
            [
                {"title": "Now Known Single", "format": ["CD", "Single"], "role": "Main", "id": "901"},
                {"title": "Now Known Album", "format": ["CD", "Album"], "role": "Main", "id": "902"},
            ],
        )
        assert _row(cache_db, "Fact Artist", "Now Known Single")["release_type"] == "single"
        assert _row(cache_db, "Fact Artist", "Now Known Album")["release_type"] == "album"


# ---------------------------------------------------------------------------
# C. persist_resolved_discogs_single — UPDATE-only classification correction
# ---------------------------------------------------------------------------

class TestPersistResolvedDiscogsSingle:
    def test_corrects_a_stored_album_row(self, cache_db):
        from services.popularity.release_cache_service import persist_resolved_discogs_single

        with cache_db() as session:
            _seed(session, "Correct Artist", "Ghost Song", "album", "777")
        assert persist_resolved_discogs_single("Correct Artist", "777", is_promo=False) is True
        row = _row(cache_db, "Correct Artist", "Ghost Song")
        assert row["release_type"] == "single"
        assert bool(row["is_promo"]) is False

    def test_carries_the_promo_flag(self, cache_db):
        from services.popularity.release_cache_service import persist_resolved_discogs_single

        with cache_db() as session:
            _seed(session, "Promo Artist", "Promo Song", "", "777")
        assert persist_resolved_discogs_single("Promo Artist", "777", is_promo=True) is True
        row = _row(cache_db, "Promo Artist", "Promo Song")
        assert row["release_type"] == "single"
        assert bool(row["is_promo"]) is True

    def test_is_update_only_and_invents_no_rows(self, cache_db):
        from services.popularity.release_cache_service import persist_resolved_discogs_single

        # No row for this artist/release: must NOT insert one.
        assert persist_resolved_discogs_single("Nobody Artist", "424242", is_promo=False) is False
        assert _row(cache_db, "Nobody Artist", "Ghost Song") is None
        with cache_db() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM artist_release_cache")
            ).scalar()
        assert count == 0

    def test_noop_when_classification_already_correct(self, cache_db):
        from services.popularity.release_cache_service import persist_resolved_discogs_single

        with cache_db() as session:
            _seed(session, "Noop Artist", "Ghost Song", "single", "777", is_promo=True)
        assert persist_resolved_discogs_single("Noop Artist", "777", is_promo=True) is False
        assert _row(cache_db, "Noop Artist", "Ghost Song")["release_type"] == "single"

    def test_never_touches_other_sources_or_releases(self, cache_db):
        from services.popularity.release_cache_service import persist_resolved_discogs_single

        with cache_db() as session:
            _seed(session, "Scope Artist", "MB Row", "album", "777", source="musicbrainz")
            _seed(session, "Scope Artist", "Other Release", "album", "888")
        assert persist_resolved_discogs_single("Scope Artist", "777", is_promo=False) is False
        assert _row(cache_db, "Scope Artist", "MB Row")["release_type"] == "album"
        assert _row(cache_db, "Scope Artist", "Other Release")["release_type"] == "album"

    def test_blank_inputs_never_reach_sql(self, cache_db):
        from services.popularity.release_cache_service import persist_resolved_discogs_single

        assert persist_resolved_discogs_single("", "777", is_promo=False) is False
        assert persist_resolved_discogs_single("Artist", "  ", is_promo=False) is False


# ---------------------------------------------------------------------------
# D. get_single_status wires the write-back
# ---------------------------------------------------------------------------

class TestGetSingleStatusPersistsTheResolution:
    def test_confirmed_single_is_persisted(self, monkeypatch):
        from services.popularity import release_cache_service as rcs

        calls = []

        def _record(artist, release_id, is_promo):
            calls.append({"artist": artist, "release_id": release_id, "is_promo": is_promo})
            return True

        monkeypatch.setattr(rcs, "persist_resolved_discogs_single", _record)

        # The row is format-less; the single comes from the ON-DEMAND detail
        # lookup (get_release), i.e. exactly the follow-up scenario.
        status = _svc().get_single_status("Ghost Song", "Wire Artist")
        assert status["is_single"] is True
        assert calls == [
            {"artist": "Wire Artist", "release_id": "777", "is_promo": False}
        ]

    def test_promo_match_carries_the_flag(self, monkeypatch):
        from services.popularity import release_cache_service as rcs

        calls = []
        monkeypatch.setattr(
            rcs,
            "persist_resolved_discogs_single",
            lambda artist, release_id, is_promo: calls.append(
                {"artist": artist, "release_id": release_id, "is_promo": is_promo}
            ) or True,
        )

        http = _FakeReleasesHttp()
        http.releases = [
            {"title": "Promo Song", "role": "Main", "id": "555", "year": 2006},
        ]
        http.release_details = {
            "555": {
                "formats": [{"name": "CD", "descriptions": ["Single", "Promo"]}],
                "tracklist": [{"position": "1", "title": "Promo Song"}],
            },
        }
        status = _svc(http).get_single_status("Promo Song", "Promo Wire Artist")
        assert status["is_single"] is True
        assert status["is_promo"] is True
        assert calls == [
            {"artist": "Promo Wire Artist", "release_id": "555", "is_promo": True}
        ]

    def test_ep_lead_promotion_is_never_persisted(self, monkeypatch):
        from services.popularity import release_cache_service as rcs

        calls = []
        monkeypatch.setattr(
            rcs,
            "persist_resolved_discogs_single",
            lambda artist, release_id, is_promo: calls.append(release_id) or True,
        )

        http = _FakeReleasesHttp()
        # The release IS on the list with an explicit EP format — an EP is
        # capped at medium confidence downstream and must not enter the
        # singles fast path as an exact 0.85 hit.
        http.releases = [
            {"title": "EP Title", "format": ["CD", "EP"], "role": "Main", "id": "666", "year": 2006},
        ]
        http.release_details = {
            "666": {
                "formats": [{"name": "CD", "descriptions": ["EP"]}],
                "tracklist": [{"position": "1", "title": "EP Title"}],
            },
        }
        status = _svc(http).get_single_status("EP Title", "Ep Wire Artist")
        assert status["is_single"] is True      # promoted (lead track)
        assert status["is_ep_lead"] is True
        assert calls == []

    def test_no_match_persists_nothing(self, monkeypatch):
        from services.popularity import release_cache_service as rcs

        calls = []
        monkeypatch.setattr(
            rcs,
            "persist_resolved_discogs_single",
            lambda *a, **k: calls.append(a) or True,
        )
        status = _svc().get_single_status("Totally Unrelated", "No Match Artist")
        assert status["is_single"] is False
        assert calls == []


# ---------------------------------------------------------------------------
# E. The end-to-end contract: scan-time resolution survives to the fast path
# ---------------------------------------------------------------------------

class TestNextScanFastPathSeesTheSingle:
    def test_resolution_persists_and_survives_reprefetch(self, monkeypatch, cache_db):
        from services.popularity import release_cache_service as rcs

        artist = "End To End Artist"
        # The prefetch stored the format-less row as UNKNOWN.
        with cache_db() as session:
            _seed(session, artist, "Ghost Song", "", "777")

        # Scan 1: the title matches, the format is resolved ON DEMAND, and
        # the confirmed single is written back for real (no recorder).
        status = _svc().get_single_status("Ghost Song", artist)
        assert status["is_single"] is True

        row = _row(cache_db, artist, "Ghost Song")
        assert row["release_type"] == "single"

        # The next scan's FAST PATH now sees the title without any API call.
        assert "ghost song" in rcs.get_artist_single_titles(artist, source="discogs")

        # And the next 7-day re-prefetch (still format-less) does NOT
        # clobber the correction back out of the cache.
        rcs.upsert_artist_release_rows(
            artist,
            [{"title": "Ghost Song", "role": "Main", "id": "777", "year": 2006}],
        )
        assert _row(cache_db, artist, "Ghost Song")["release_type"] == "single"
        assert "ghost song" in rcs.get_artist_single_titles(artist, source="discogs")
