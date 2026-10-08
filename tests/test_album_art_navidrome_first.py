"""Cover art: Navidrome's own ``getCoverArt`` comes FIRST.

Reported: "For the cover art on the albums, currently it looks online for it, but
it should first use the coverart from Navidrome using the subsonic api of
getCoverArt."

Three defects were in the way, all pinned here:

1. ``fetch_album_art_from_navidrome`` located the album with a library-wide
   Subsonic ``search()`` and an exact, CASE-SENSITIVE ``artist``/``album``
   comparison — while every other album lookup in the app is ``LOWER(...)``. An
   album whose stored casing differed was reported as "not in local library" and
   Navidrome was skipped entirely, so the online providers ran instead.
2. It never used the id it already had: ``tracks.id`` IS the Navidrome song id
   (the importer stores it verbatim) and ``getCoverArt`` accepts a song id, so a
   single request can return the art with no search at all.
3. Every art path checked the DB cache first and returned the cached blob, so an
   album that had once collected Cover Art Archive art could never pick up the
   library's own cover — the cache made the Navidrome step unreachable. Cached
   art a PROVIDER supplied is now upgradable; the user's own upload/URL and
   Navidrome's own art are final.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeResult:
    def __init__(self, rows):
        # One query's result is EITHER a single row or a list of rows. A tuple
        # IS a row in SQL terms — treating it as "three one-column rows" made
        # ``first()`` return just the first CELL (``b"stored-caa"``), so the
        # repository indexed into a ``bytes`` and produced ints instead of
        # ``(blob, mime, source)``: every cached-art test then saw no stored art
        # and fell through to the providers.
        if isinstance(rows, (dict, tuple)):
            rows = [rows]
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchone(self):
        row = self.first()
        # Code that reads by column POSITION does ``row[0]``. Handing it a dict
        # raised ``KeyError(0)`` whose ``str()`` is literally ``"0"``, and the
        # surrounding try/except logged that as
        # ``Navidrome guard check failed error=0`` and failed OPEN — so the song
        # id was silently dropped and step 1 never ran. That is why two of these
        # tests were failing on a row shape nothing in production ever produces.
        if isinstance(row, dict):
            return tuple(row.values())
        return row

    def scalar(self):
        return self._rows[0] if self._rows else 0


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return _FakeResult(self._results.pop(0) if self._results else [])


class _FakeCM:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False


class _FakeNavidrome:
    """Records the Subsonic calls made; serves `getCoverArt` for chosen ids."""

    art_available_for: set[str] = set()
    search_albums: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.cover_art_calls: list[tuple[str, int]] = []
        self.search_calls: list[str] = []

    def get_cover_art_bytes(self, track_or_album_id, size=600):
        type(self).last_instance = self
        self.cover_art_calls.append((track_or_album_id, size))
        if track_or_album_id in type(self).art_available_for:
            return b"\xff\xd8\xff\xe0navidrome-art"
        return None

    def search(self, query, **kwargs):
        self.search_calls.append(query)
        return {"albums": list(type(self).search_albums)}


def _patch_navidrome(monkeypatch, art_for=(), albums=()):
    import api_clients.navidrome as nav

    class _Client(_FakeNavidrome):
        art_available_for = set(art_for)
        search_albums = list(albums)

    monkeypatch.setattr(nav, "NavidromeClient", _Client)
    return _Client


def _patch_config(monkeypatch):
    import helpers.config_helpers as cfg

    monkeypatch.setattr(
        cfg,
        "get_config",
        lambda: {"navidrome_users": [
            {"base_url": "http://nav.local:4533", "user": "u", "pass": "p"}
        ]},
    )


def _patch_db(monkeypatch, svc, session):
    """Wire ONE fake session into every module that opens its own.

    ``fetch_album_art_record`` lives in ``db.repositories.metadata`` and does
    ``from db.engine import db_session``, so patching only the module under test
    left it reading the REAL database: the fake art was never seen, the call
    fell through to the Navidrome/providers branch, and three tests failed for a
    reason unrelated to what they assert (``test_the_album_page_path`` passed
    for the same wrong reason). Both bindings are replaced with the same
    session, so ``session.statements`` still accumulates every query.
    """
    import db.repositories.metadata as repo_meta

    def _cm():
        return _FakeCM(session)

    monkeypatch.setattr(svc, "db_session", _cm)
    monkeypatch.setattr(repo_meta, "db_session", _cm)
    return session


# ---------------------------------------------------------------------------
# 1. Which stored art may Navidrome replace?
# ---------------------------------------------------------------------------

class TestNavidromeArtPrecedence:
    @pytest.mark.parametrize(
        "source", ["musicbrainz", "discogs", "audiodb", "itunes", "missing_releases", "unknown", ""]
    )
    def test_provider_art_may_be_replaced(self, source):
        from services.enrichment.album_art_service import navidrome_art_may_replace

        assert navidrome_art_may_replace(source) is True

    @pytest.mark.parametrize("source", ["navidrome", "upload", "url", "UPLOAD", " upload "])
    def test_the_users_own_art_is_never_replaced(self, source):
        from services.enrichment.album_art_service import navidrome_art_may_replace

        assert navidrome_art_may_replace(source) is False


# ---------------------------------------------------------------------------
# 2. The fetch itself
# ---------------------------------------------------------------------------

class TestNavidromeFetchUsesTheStoredSongId:
    def _run(self, monkeypatch, song_row, art_for=(), albums=()):
        from services.enrichment import album_art_service as svc

        _patch_db(monkeypatch, svc, _FakeSession([song_row]))
        _patch_config(monkeypatch)
        client_cls = _patch_navidrome(monkeypatch, art_for=art_for, albums=albums)
        svc._navidrome_art_miss_cache.clear()
        data = svc.fetch_album_art_from_navidrome("Artist", "Album")
        return data, client_cls

    def test_one_request_with_the_library_song_id_no_search(self, monkeypatch):
        data, client_cls = self._run(monkeypatch, {"id": "song-123"}, art_for={"song-123"})
        assert data == b"\xff\xd8\xff\xe0navidrome-art"
        client = client_cls.last_instance
        assert client.cover_art_calls == [("song-123", 600)]
        assert client.search_calls == [], "the library-wide search must be skipped"

    def test_it_falls_back_to_the_album_search_when_the_song_has_no_art(self, monkeypatch):
        data, client_cls = self._run(
            monkeypatch,
            {"id": "song-123"},
            art_for={"album-9"},
            albums=[{"id": "album-9", "artist": "Artist", "name": "Album"}],
        )
        assert data == b"\xff\xd8\xff\xe0navidrome-art"
        client = client_cls.last_instance
        assert [c[0] for c in client.cover_art_calls] == ["song-123", "album-9"]
        assert client.search_calls, "the album-id fallback must still work"

    def test_no_art_anywhere_returns_none(self, monkeypatch):
        data, _client = self._run(monkeypatch, {"id": "song-123"})
        assert data is None

    def test_an_album_not_in_the_library_is_skipped_before_any_request(self, monkeypatch):
        data, client_cls = self._run(monkeypatch, None)
        assert data is None
        assert getattr(client_cls, "last_instance", None) is None, (
            "no Navidrome request may be made for an album that is not in the library"
        )

    def test_the_library_guard_is_case_insensitive(self, monkeypatch):
        """The defect that made Navidrome unreachable for casing differences."""
        from services.enrichment import album_art_service as svc

        session = _patch_db(monkeypatch, svc, _FakeSession([{"id": "song-1"}]))
        _patch_config(monkeypatch)
        _patch_navidrome(monkeypatch, art_for={"song-1"})
        svc.fetch_album_art_from_navidrome("artist", "album")

        sql = session.statements[0]
        assert sql.count("LOWER(") >= 2, (
            "both artist and album must be compared case-insensitively, or an "
            "album whose stored casing differs is treated as absent"
        )
        assert "= :artist" not in sql.replace("LOWER(:artist)", ""), (
            "an exact, case-sensitive artist comparison must not survive"
        )


# ---------------------------------------------------------------------------
# 3. The cache must not shadow Navidrome
# ---------------------------------------------------------------------------

class TestCacheDoesNotShadowNavidrome:
    def test_provider_art_in_the_cache_still_asks_navidrome(self, monkeypatch):
        from services.enrichment import album_art_service as svc

        saved: list[tuple] = []
        monkeypatch.setattr(svc, "save_album_art_to_db", lambda *a, **k: saved.append((a, k)))
        _patch_db(monkeypatch, svc, _FakeSession([(b"cached-caa-art", "image/jpeg", "musicbrainz")]))
        monkeypatch.setattr(
            svc, "fetch_album_art_from_navidrome", lambda artist, album: b"navidrome-art"
        )

        data, mime = svc.get_or_fetch_album_art("Artist", "Album")
        assert data == b"navidrome-art"
        assert saved and saved[0][1].get("source") == "navidrome"

    def test_user_uploaded_art_is_never_overridden(self, monkeypatch):
        from services.enrichment import album_art_service as svc

        _patch_db(monkeypatch, svc, _FakeSession([(b"my-upload", "image/png", "upload")]))
        monkeypatch.setattr(
            svc,
            "fetch_album_art_from_navidrome",
            lambda artist, album: pytest.fail("user art must not be replaced"),
        )

        data, mime = svc.get_or_fetch_album_art("Artist", "Album")
        assert data == b"my-upload"
        assert mime == "image/png"

    def test_navidrome_art_in_the_cache_is_not_refetched(self, monkeypatch):
        from services.enrichment import album_art_service as svc

        _patch_db(monkeypatch, svc, _FakeSession([(b"already-navidrome", "image/jpeg", "navidrome")]))
        monkeypatch.setattr(
            svc,
            "fetch_album_art_from_navidrome",
            lambda artist, album: pytest.fail("Navidrome art must not be refetched"),
        )

        data, _mime = svc.get_or_fetch_album_art("Artist", "Album")
        assert data == b"already-navidrome"

    def test_provider_art_stands_when_navidrome_has_none(self, monkeypatch):
        """No pointless re-download of the same provider art."""
        from services.enrichment import album_art_service as svc

        _patch_db(monkeypatch, svc, _FakeSession([(b"cached-caa-art", "image/jpeg", "musicbrainz")]))
        monkeypatch.setattr(svc, "fetch_album_art_from_navidrome", lambda artist, album: None)
        monkeypatch.setattr(
            svc,
            "fetch_album_art_from_musicbrainz",
            lambda artist, album: pytest.fail("must not re-download what we already hold"),
        )

        data, _mime = svc.get_or_fetch_album_art("Artist", "Album")
        assert data == b"cached-caa-art"


# ---------------------------------------------------------------------------
# 4. Existing art is NEVER looked up again (the reported defect)
# ---------------------------------------------------------------------------


#: Every provider that used to be re-hit on EVERY call once the stored blob had
#: been overwritten by the failed Navidrome lookup.
_PROVIDER_FETCHES = (
    "fetch_album_art_from_musicbrainz",
    "fetch_album_art_from_discogs",
    "fetch_album_art_from_audiodb",
)


class TestExistingArtIsNeverLookedUpAgain:
    """"If album art already exists, it shouldn't look for it each time."""

    @staticmethod
    def _no_provider(monkeypatch, svc):
        """Make any provider lookup explode, so a miss cannot hide a regression."""
        for name in _PROVIDER_FETCHES:
            monkeypatch.setattr(
                svc,
                name,
                lambda *args, _n=name, **kwargs: pytest.fail(
                    f"{_n} ran even though album art already exists"
                ),
            )

    def test_no_online_provider_is_consulted_when_art_exists(self, monkeypatch):
        """This is the reported symptom: one CAA call per imported track."""
        from services.enrichment import album_art_service as svc

        _patch_db(
            monkeypatch, svc, _FakeSession([(b"stored-caa", "image/jpeg", "musicbrainz")])
        )
        monkeypatch.setattr(svc, "fetch_album_art_from_navidrome", lambda a, b: None)
        self._no_provider(monkeypatch, svc)

        data, mime = svc.get_or_fetch_album_art("Artist", "Album")
        assert data == b"stored-caa"
        assert mime == "image/jpeg"

    def test_repeated_calls_still_never_reach_a_provider(self, monkeypatch):
        """A cache hit must stay a cache hit — not a lookup on every request."""
        from services.enrichment import album_art_service as svc

        row = (b"stored-caa", "image/jpeg", "musicbrainz")
        # One row per call: the fake session pops as it goes.
        _patch_db(monkeypatch, svc, _FakeSession([row, row, row]))
        monkeypatch.setattr(svc, "fetch_album_art_from_navidrome", lambda a, b: None)
        self._no_provider(monkeypatch, svc)

        for attempt in range(3):
            data, _mime = svc.get_or_fetch_album_art("Artist", "Album")
            assert data == b"stored-caa", f"attempt {attempt + 1} lost the stored art"

    def test_the_missing_releases_url_is_not_downloaded_when_art_exists(
        self, monkeypatch
    ):
        """That fast path ran BEFORE the cache decision, so it re-fetched the URL
        on every call for any album that also appears in missing_releases."""
        from unittest.mock import MagicMock

        from services.enrichment import album_art_service as svc

        # Row 1 = the stored art record. Row 2 = the cover URL the missing-
        # releases branch would read; it must never be reached.
        _patch_db(
            monkeypatch,
            svc,
            _FakeSession(
                [(b"stored-caa", "image/jpeg", "musicbrainz"), ("https://covers.example/x.jpg",)]
            ),
        )
        monkeypatch.setattr(svc, "fetch_album_art_from_navidrome", lambda a, b: None)
        client = MagicMock()
        # A LAMBDA: ``pytest.fail(...)`` would raise at assignment time, and
        # the test would fail without ever having called the function.
        client.get.side_effect = lambda *a, **k: pytest.fail(
            "the stored cover must not be re-downloaded from missing_releases"
        )
        monkeypatch.setattr(svc, "_art_client", client)

        data, _mime = svc.get_or_fetch_album_art("Artist", "Album")
        assert data == b"stored-caa"
        client.get.assert_not_called()

    def test_an_upgrade_to_navidrome_art_still_happens(self, monkeypatch):
        """CONTROL — none of the above may block the 2026-09-21 upgrade.

        Provider art must still be replaceable by Navidrome's own copy; only
        the LOOKUPS after a fruitless Navidrome check are removed.
        """
        from services.enrichment import album_art_service as svc

        saved: list[dict] = []
        _patch_db(
            monkeypatch, svc, _FakeSession([(b"old-caa", "image/jpeg", "musicbrainz")])
        )
        monkeypatch.setattr(svc, "fetch_album_art_from_navidrome", lambda a, b: b"nav-art")
        monkeypatch.setattr(
            svc, "save_album_art_to_db", lambda *a, **k: saved.append(k)
        )

        data, _mime = svc.get_or_fetch_album_art("Artist", "Album")
        assert data == b"nav-art"
        assert saved and saved[0].get("source") == "navidrome"


# ---------------------------------------------------------------------------
# 5. The scan pipeline and the album page agree
# ---------------------------------------------------------------------------

class TestTheOtherArtPathsAskNavidromeFirst:
    def test_the_scan_pipeline_consults_the_cache_source(self):
        import inspect

        from services.popularity.stages import album_stage

        source = inspect.getsource(album_stage)
        assert "navidrome_art_may_replace" in source, (
            "the scan's art pipeline must let Navidrome replace provider art"
        )
        assert "fetch_album_art_record" in source, (
            "the pipeline needs the stored SOURCE, not just the blob"
        )

    def test_the_scan_keeps_art_when_navidrome_has_none(self):
        import inspect

        from services.popularity.stages import album_stage

        source = inspect.getsource(album_stage)
        assert "album art kept" in source, "a failed Navidrome lookup must not trigger a re-download"

    def test_the_album_page_path_asks_navidrome_first(self, monkeypatch):
        from services.metadata import album_service as svc
        from services.enrichment import album_art_service as art

        monkeypatch.setattr(
            svc,
            "db_session",
            lambda: _FakeCM(_FakeSession([(b"cached-caa-art", "image/jpeg", "musicbrainz")])),
        )
        monkeypatch.setattr(art, "fetch_album_art_from_navidrome", lambda artist, album: b"navidrome-art")
        monkeypatch.setattr(art, "save_album_art_to_db", lambda *a, **k: True)

        data, _mime = svc.get_local_album_art("Artist", "Album")
        assert data == b"navidrome-art"
