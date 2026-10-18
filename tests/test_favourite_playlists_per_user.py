"""Per-user favourite playlists: expansion, star filter and per-user routing.

The feature: for EACH Navidrome user, expand their starred ALBUMS and ARTISTS
into song ids (there is no local album/artist → Navidrome-id mapping, so the
expansion MUST go through the Navidrome API), keep the local tracks at or above
``favourite_min_stars``, and write the playlist to THAT user's own account.

The tests below drive the real functions with stub clients; only the DB star
filter is replaced, because it is the one piece that needs a real ``tracks``
table and it is exercised directly in ``TestTheStarFloor``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


class _StubClient:
    """A Navidrome client with a fixed album/artist catalogue."""

    def __init__(self, albums: dict, artist_albums: dict | None = None, username="u"):
        self.username = username
        self._albums = albums
        self._artist_albums = artist_albums or {}
        self.album_calls: list[str] = []
        self.artist_calls: list[str] = []

    def fetch_album_tracks(self, album_id):
        self.album_calls.append(album_id)
        return {"tracks": [{"id": s} for s in self._albums.get(album_id, [])]}

    def fetch_artist_albums(self, artist_id):
        self.artist_calls.append(artist_id)
        return [{"id": a} for a in self._artist_albums.get(artist_id, [])]


class TestExpansion:
    def test_albums_expand_to_their_songs(self):
        from services.playlists.favourite_playlist_service import (
            build_favourite_album_song_ids,
        )

        client = _StubClient({"a1": ["s1", "s2"], "a2": ["s3"]})

        assert build_favourite_album_song_ids(client, ["a1", "a2"]) == ["s1", "s2", "s3"]

    def test_artists_expand_through_their_albums(self):
        from services.playlists.favourite_playlist_service import (
            build_favourite_artist_song_ids,
        )

        client = _StubClient(
            {"a1": ["s1", "s2"], "a2": ["s3"]},
            artist_albums={"ar1": ["a1", "a2"]},
        )

        assert build_favourite_artist_song_ids(client, ["ar1"]) == ["s1", "s2", "s3"]

    def test_a_song_on_two_favourited_entities_appears_once(self):
        """A de-duplicated list is what lets the playlist settle as unchanged."""
        from services.playlists.favourite_playlist_service import (
            build_favourite_album_song_ids,
        )

        client = _StubClient({"a1": ["s1", "s2"], "a2": ["s2", "s3"]})

        assert build_favourite_album_song_ids(client, ["a1", "a2"]) == ["s1", "s2", "s3"]


class TestTheStarFloor:
    """``_qualifying_tracks`` keeps only rows at or above the configured stars."""

    def _patch_rows(self, monkeypatch, rows):
        from services.playlists import favourite_playlist_service as svc

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class _Session:
            def execute(self, *a, **k):
                return _Result(rows)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(svc, "db_session", lambda: _Session())

    def test_below_floor_tracks_are_dropped(self, monkeypatch):
        from services.playlists.favourite_playlist_service import _qualifying_tracks

        self._patch_rows(monkeypatch, [("s1", 5), ("s2", 4), ("s3", 3), ("s4", 0)])

        assert _qualifying_tracks(["s1", "s2", "s3", "s4"], 4) == ["s1", "s2"]

    def test_an_id_with_no_local_row_is_dropped(self, monkeypatch):
        """A starred album can hold songs this library never imported; leaving
        one in would make the pushed playlist differ from the request forever."""
        from services.playlists.favourite_playlist_service import _qualifying_tracks

        self._patch_rows(monkeypatch, [("s1", 5)])

        assert _qualifying_tracks(["s1", "not-in-library"], 4) == ["s1"]

    def test_order_follows_the_request(self, monkeypatch):
        from services.playlists.favourite_playlist_service import _qualifying_tracks

        self._patch_rows(monkeypatch, [("s3", 5), ("s1", 5), ("s2", 5)])

        assert _qualifying_tracks(["s2", "s1", "s3"], 4) == ["s2", "s1", "s3"]


class TestPerUserSync:
    def _wire(self, monkeypatch, *, favourite_ids, config=None, users=None):
        from services.playlists import favourite_playlist_service as svc
        import services.favourites_service as fav

        cfg = {
            "favourite_playlists_enabled": True,
            "favourite_min_stars": 4,
            "favourite_artists_name": "Favourite Artists",
            "favourite_albums_name": "Favourite Albums",
            "sync_favourites_from_navidrome": False,
        }
        cfg.update(config or {})
        monkeypatch.setattr(svc, "_config", lambda: cfg)
        # ⚠️ Patch the SERVICE's own binding. It does
        # ``from db.repositories.favourites_repository import
        # get_favourite_navidrome_ids`` at MODULE level, so patching the
        # repository module would not bite the call site.
        monkeypatch.setattr(
            svc, "get_favourite_navidrome_ids",
            lambda username, etype: favourite_ids.get((username, etype), []),
        )
        monkeypatch.setattr(
            fav, "get_navidrome_user_by_name",
            lambda name: {"user": name, "base_url": "http://nd", "pass": "p"}
            if (users is None or name in users) else None,
        )
        # The service patches the star floor out where a real DB is not present.
        monkeypatch.setattr(svc, "_qualifying_tracks", lambda ids, stars: list(ids))
        pushed: list[tuple] = []
        monkeypatch.setattr(
            "services.playlists.playlist_navidrome_service.sync_playlist_by_name",
            lambda client, name, ids, force=False: (
                pushed.append((client.username, name, list(ids)))
                or {"success": True, "song_count": len(ids)}
            ),
        )
        return svc, pushed

    def test_each_users_playlist_is_pushed_with_their_own_client(self, monkeypatch):
        """⭐ Ownership: Navidrome rejects a write to a playlist the caller does
        not own (code 50), so the CLIENT must be the user's own."""
        svc, pushed = self._wire(
            monkeypatch,
            favourite_ids={
                ("aaron", "album"): ["alb1"],
                ("beth", "album"): ["alb2"],
            },
        )

        import api_clients.navidrome as nd

        clients = {}

        class _Client(_StubClient):
            def __init__(self, base_url, username, password):
                super().__init__({"alb1": ["s1"], "alb2": ["s2"]}, username=username)
                clients[username] = self

        monkeypatch.setattr(nd, "NavidromeClient", _Client)

        svc.sync_favourite_playlists_for_user("aaron")
        svc.sync_favourite_playlists_for_user("beth")

        assert ("aaron", "Favourite Albums", ["s1"]) in pushed
        assert ("beth", "Favourite Albums", ["s2"]) in pushed
        # …and aaron's client never expanded beth's album.
        assert "alb2" not in clients["aaron"].album_calls

    def test_an_empty_result_is_skipped_not_written(self, monkeypatch):
        """Writing an empty list would wipe a populated playlist the moment a
        user un-stars one album."""
        svc, pushed = self._wire(monkeypatch, favourite_ids={})

        import api_clients.navidrome as nd

        monkeypatch.setattr(
            nd, "NavidromeClient",
            lambda base_url, username, password: _StubClient({}, username=username),
        )

        result = svc.sync_favourite_playlists_for_user("aaron")

        assert pushed == []
        assert result["success"] is True
        assert result["albums"].get("skipped") is True
        assert result["artists"].get("skipped") is True

    def test_an_unknown_user_is_an_error_not_a_fallback(self, monkeypatch):
        """``get_active_navidrome_user`` falls back to the first configured user;
        the per-user path must NOT, or one account's playlists get pushed as
        another's."""
        svc, _pushed = self._wire(monkeypatch, favourite_ids={}, users={"aaron"})

        import api_clients.navidrome as nd

        monkeypatch.setattr(
            nd, "NavidromeClient",
            lambda base_url, username, password: _StubClient({}, username=username),
        )

        result = svc.sync_favourite_playlists_for_user("ghost")

        assert result["success"] is False
        assert "ghost" in (result["error"] or "")

    def test_the_master_switch_stops_everything(self, monkeypatch):
        svc, pushed = self._wire(
            monkeypatch,
            favourite_ids={("aaron", "album"): ["alb1"]},
            config={"favourite_playlists_enabled": False},
        )

        import api_clients.navidrome as nd

        monkeypatch.setattr(
            nd, "NavidromeClient",
            lambda base_url, username, password: _StubClient({"alb1": ["s1"]}, username=username),
        )
        monkeypatch.setattr(
            "helpers.config_helpers.get_navidrome_users_normalized",
            lambda: [{"user": "aaron", "base_url": "http://nd", "pass": "p"}],
        )

        result = svc.sync_favourite_playlists()

        assert result["success"] is True
        assert result["users"] == []
        assert pushed == []


class TestTheSyncTargetsASpecificUser:
    def test_sync_uses_the_named_users_client(self, monkeypatch):
        """``sync_favourites_from_navidrome(username=...)`` must query AS that
        user — the stars it pulls are per-account."""
        import services.favourites_service as fav

        used: list[str] = []

        class _C:
            def get_starred_items(self):
                return {"tracks": [{"id": "s1"}], "albums": [{"id": "a1"}],
                        "artists": [{"id": "ar1"}]}

        monkeypatch.setattr(fav, "get_navidrome_client_for_user",
                            lambda name: used.append(name) or _C())
        monkeypatch.setattr(fav, "set_favourite", lambda *a, **k: {"id": 1})

        result = fav.sync_favourites_from_navidrome(username="beth")

        assert used == ["beth"]
        assert result == {"success": True, "tracks": 1, "albums": 1, "artists": 1}


class TestResolveNavidromeId:
    """The album/artist arm used to query non-existent columns."""

    def _patch_client(self, monkeypatch, found):
        import services.favourites_service as fav

        captured = {}

        class _C:
            def search(self, term, **kw):
                captured["term"] = term
                return found

        monkeypatch.setattr(fav, "get_navidrome_client_for_active_user", lambda: _C())
        return captured

    def test_a_track_id_is_its_own_navidrome_id(self, monkeypatch):
        import services.favourites_service as fav

        assert fav._resolve_navidrome_id("track", "s1") == "s1"

    def test_an_album_resolves_by_exact_name(self, monkeypatch):
        import services.favourites_service as fav

        captured = self._patch_client(
            monkeypatch,
            {"albums": [{"id": "other", "name": "Something Else"},
                        {"id": "want", "name": "Memento Mori"}]},
        )

        assert fav._resolve_navidrome_id("album", "Memento Mori") == "want"
        assert captured["term"] == "Memento Mori"

    def test_an_artist_resolves_by_exact_name(self, monkeypatch):
        import services.favourites_service as fav

        self._patch_client(
            monkeypatch, {"artists": [{"id": "want", "name": "The Warning"}]},
        )

        assert fav._resolve_navidrome_id("artist", "the warning") == "want"

    def test_no_exact_match_resolves_to_none(self, monkeypatch):
        """⭐ Exact-first, never 'take the first hit': a search for one artist's
        name returns other artists' albums, and starring the WRONG id is worse
        than starring nothing."""
        import services.favourites_service as fav

        self._patch_client(
            monkeypatch, {"albums": [{"id": "x", "name": "Memento Mori (Live)"}]},
        )

        assert fav._resolve_navidrome_id("album", "Memento Mori") is None


class TestTheConfigSurfaceExists:
    """§4.1: every configurable option must appear on the Config page FIRST.

    ⚠️ Both trees — the live one and ``test_site``. A setting the page cannot
    write is a config key that exists only in a docstring.
    """

    _HTML = ["templates/pages/config.html", "test_site/templates/Pages/config.html"]
    _JS = ["static/js/config.js", "test_site/static/js/pages/config.js"]
    _INPUT_IDS = (
        "playlists_favourite_enabled",
        "playlists_favourite_min_stars",
        "playlists_favourite_artists_name",
        "playlists_favourite_albums_name",
        "playlists_favourite_sync",
    )
    _KEYS = (
        "favourite_playlists_enabled:",
        "favourite_min_stars:",
        "favourite_artists_name:",
        "favourite_albums_name:",
        "sync_favourites_from_navidrome:",
    )

    @pytest.mark.parametrize("rel", _HTML)
    def test_the_inputs_exist(self, rel):
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")

        for input_id in self._INPUT_IDS:
            assert f'id="{input_id}"' in html, f"{rel} has no input for {input_id}"

    @pytest.mark.parametrize("rel", _JS)
    def test_the_collectors_exist(self, rel):
        """Without a collector the inputs render but never reach config.yaml."""
        js = (REPO_ROOT / rel).read_text(encoding="utf-8")

        for key in self._KEYS:
            assert key in js, f"{rel} does not collect {key}"

    def test_the_getter_reads_what_the_page_writes(self):
        """The keys the collector emits are the keys the getter reads."""
        from helpers.config_helpers import get_favourite_playlists_config

        cfg = get_favourite_playlists_config()

        for key in (
            "favourite_playlists_enabled",
            "favourite_min_stars",
            "favourite_artists_name",
            "favourite_albums_name",
            "sync_favourites_from_navidrome",
        ):
            assert key in cfg, f"get_favourite_playlists_config is missing {key}"

    def test_the_star_floor_is_clamped(self, monkeypatch):
        from helpers import config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {"playlists": {"favourite_min_stars": 99}})
        assert ch.get_favourite_playlists_config()["favourite_min_stars"] == 5
        monkeypatch.setattr(ch, "get_config", lambda: {"playlists": {"favourite_min_stars": 0}})
        assert ch.get_favourite_playlists_config()["favourite_min_stars"] == 1
