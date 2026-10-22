"""The /playlists page shows each playlist's cover, can regenerate it, and
lists the tracks when a playlist is selected.

Reported:

> "On the playlists page, it should show the cover art for each of the
> playlists that are imported. It should also have a button next to the
> playlist to regenerate the cover art with gemini if the setting is
> configured in the config page. Please create the layout so it fits with the
> style of the dashboard or artist page, with a nice display on mobile.
> Selecting a playlist should also show the tracks in the playlist."

What was actually missing (measured, not assumed):

* Navidrome ALREADY returns ``coverArt`` on ``getPlaylists``/``getPlaylist``
  and serves the bytes through ``getCoverArt``. ``/api/playlists/all`` simply
  did not PROJECT the field, so the page had nothing to render.
* The browser cannot fetch the art itself — ``getCoverArt`` needs the Subsonic
  credentials and an ``<img src>`` cannot carry them — so a server-side proxy
  is required.
* The track list was already implemented (``selectPlaylist`` →
  ``/api/playlists/tracks``); it is pinned here so it cannot regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_TEMPLATE = REPO_ROOT / "templates" / "playlists" / "index.html"
TS_TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Playlists" / "index.html"
MAIN_JS = REPO_ROOT / "static" / "js" / "playlists_index.js"
TS_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "playlists.js"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. The list API must PROJECT the cover, plus the AI gate
# ---------------------------------------------------------------------------

class TestTheListCarriesCoverAndGate:
    def test_every_playlist_entry_gets_a_cover_url(self):
        from routes import playlists as pl

        url = pl._playlist_cover_url("navidrome", "pl-1", None, "My Mix")
        assert url.startswith("/api/playlist/art?")

    def test_the_cover_url_carries_the_source_id_and_name(self):
        from urllib.parse import parse_qs, urlparse

        from routes import playlists as pl

        url = pl._playlist_cover_url("file", "file::Mix", "/music/Playlists/Mix.nsp", "Mix")
        params = parse_qs(urlparse(url).query)
        assert params["source"] == ["file"]
        assert params["id"] == ["file::Mix"]
        assert params["file_path"] == ["/music/Playlists/Mix.nsp"]
        assert params["name"] == ["Mix"]

    def test_the_cover_url_is_quoted(self):
        """A playlist name with '&' must not split the query string."""
        from routes import playlists as pl

        url = pl._playlist_cover_url("navidrome", "pl-1", None, "Rock & Roll")
        assert "Rock & Roll" not in url
        assert "Rock+%26+Roll" in url or "Rock%20%26%20Roll" in url

    def _list_body(self) -> str:
        """The ``api_playlists_all`` body only — NOT the rest of the module."""
        src = _read(REPO_ROOT / "routes" / "playlists.py")
        start = src.index("def api_playlists_all")
        return src[start: src.index("def _playlist_cover_url", start)]

    def test_the_route_emits_a_cover_url_per_playlist(self):
        body = self._list_body()
        # BOTH branches — file-backed and Navidrome.
        assert body.count("_playlist_cover_url(") >= 2, (
            "each playlist branch must build its own cover URL"
        )

    def test_the_route_reports_whether_ai_covers_are_available(self):
        assert "ai_cover_available" in self._list_body()


class TestTheAiGate:
    """The button is only offered where the feature can actually work."""

    def _patch(self, monkeypatch, *, gemini_enabled, key, covers_enabled):
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch, "get_gemini_config",
            lambda: {"enabled": gemini_enabled, "api_key": key, "model": "m"},
        )
        monkeypatch.setattr(
            ch, "get_genre_cover_config",
            lambda: {"enabled": covers_enabled, "regenerate": False, "prompt_template": ""},
        )

    def _check(self):
        from routes import playlists as pl

        return pl._ai_cover_available()

    def test_available_only_with_the_integration_and_a_key(self, monkeypatch):
        self._patch(monkeypatch, gemini_enabled=True, key="k", covers_enabled=True)
        assert self._check() is True

    def test_disabled_without_the_integration(self, monkeypatch):
        self._patch(monkeypatch, gemini_enabled=False, key="k", covers_enabled=True)
        assert self._check() is False

    def test_disabled_without_a_key(self, monkeypatch):
        self._patch(monkeypatch, gemini_enabled=True, key="", covers_enabled=True)
        assert self._check() is False

    def test_disabled_when_the_cover_toggle_is_off(self, monkeypatch):
        self._patch(monkeypatch, gemini_enabled=True, key="k", covers_enabled=False)
        assert self._check() is False

    def test_a_config_failure_reads_as_unavailable(self, monkeypatch):
        """Fail CLOSED — never offer a billable action you cannot verify."""
        import helpers.config_helpers as ch

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(ch, "get_gemini_config", _boom)
        assert self._check() is False


# ---------------------------------------------------------------------------
# 2. The art proxy
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, *, playlist=None, art=None):
        self._playlist = playlist
        self._art = art
        self.art_ids: list[str] = []

    def fetch_playlist(self, playlist_id):
        return self._playlist

    def find_playlist_by_name(self, name):
        return self._playlist

    def get_cover_art_bytes(self, cover_id, size=300):
        self.art_ids.append(cover_id)
        return self._art


class TestTheArtProxy:
    def _patch_client(self, monkeypatch, client):
        """Patch the SOURCE module — ``get_navidrome_client`` is imported from
        ``routes.navidrome`` INSIDE the route body, so patching the playlists
        module itself would be invisible."""
        import routes.navidrome as nav_routes

        monkeypatch.setattr(nav_routes, "get_navidrome_client", lambda: client)

    def _run(self, query: str):
        from app import app

        return app.test_request_context(query)

    @pytest.mark.asyncio
    async def test_it_serves_navidromes_bytes(self, monkeypatch):
        client = _FakeClient(playlist={"id": "pl-1", "coverArt": "pl-abc"}, art=PNG)
        self._patch_client(monkeypatch, client)

        from app import app

        async with app.test_request_context("/api/playlist/art?source=navidrome&id=pl-1&name=X"):
            from routes.playlists import api_playlist_art

            response = await api_playlist_art()
            assert response.status_code == 200
            body = await response.get_data()
            assert body == PNG

    @pytest.mark.asyncio
    async def test_it_uses_the_COVER_ART_ID_not_the_playlist_id(self, monkeypatch):
        """⭐ getCoverArt wants the ``coverArt`` value, not the playlist id."""
        client = _FakeClient(playlist={"id": "pl-1", "coverArt": "pl-abc"}, art=PNG)
        self._patch_client(monkeypatch, client)

        from app import app

        async with app.test_request_context("/api/playlist/art?source=navidrome&id=pl-1&name=X"):
            from routes.playlists import api_playlist_art

            await api_playlist_art()
        assert client.art_ids == ["pl-abc"], (
            "passing the playlist id to getCoverArt 404s or returns the wrong image"
        )

    @pytest.mark.asyncio
    async def test_a_playlist_with_no_cover_gets_the_placeholder(self, monkeypatch):
        client = _FakeClient(playlist={"id": "pl-1"}, art=None)
        self._patch_client(monkeypatch, client)

        from app import app

        async with app.test_request_context("/api/playlist/art?source=navidrome&id=pl-1&name=X"):
            from routes.playlists import api_playlist_art

            response = await api_playlist_art()
        assert response.status_code == 200
        assert b"svg" in (await response.get_data()).lower()

    @pytest.mark.asyncio
    async def test_no_navidrome_client_still_answers(self, monkeypatch):
        """A miss must render a tile, never a broken image."""
        self._patch_client(monkeypatch, None)

        from app import app

        async with app.test_request_context("/api/playlist/art?source=navidrome&id=pl-1&name=X"):
            from routes.playlists import api_playlist_art

            response = await api_playlist_art()
        assert response.status_code == 200
        assert b"svg" in (await response.get_data()).lower()

    @pytest.mark.asyncio
    async def test_a_file_playlist_resolves_by_name(self, monkeypatch):
        """A file-backed playlist has no Navidrome id of its own."""
        client = _FakeClient(playlist={"id": "pl-9", "coverArt": "pl-xyz"}, art=PNG)
        self._patch_client(monkeypatch, client)

        from app import app

        async with app.test_request_context(
            "/api/playlist/art?source=file&id=file%3A%3AMix&name=Mix"
        ):
            from routes.playlists import api_playlist_art

            response = await api_playlist_art()
        assert (await response.get_data()) == PNG
        assert client.art_ids == ["pl-xyz"]

    def test_the_proxy_is_async(self):
        """It awaits a threadpool fetch; a sync handler would block the loop."""
        import inspect

        from routes.playlists import api_playlist_art

        assert inspect.iscoroutinefunction(api_playlist_art)


# ---------------------------------------------------------------------------
# 3. The regenerate route
# ---------------------------------------------------------------------------

class TestTheRegenerateRoute:
    def _body(self) -> str:
        src = _read(REPO_ROOT / "routes" / "playlists.py")
        start = src.index("async def api_playlist_regenerate_cover")
        return src[start: src.index("\ndef ", start + 10)]

    @pytest.mark.asyncio
    async def test_it_refuses_when_ai_covers_are_not_configured(self, monkeypatch):
        import routes.playlists as pl
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_gemini_config", lambda: {"enabled": False, "api_key": "", "model": "m"})
        monkeypatch.setattr(
            ch, "get_genre_cover_config",
            lambda: {"enabled": True, "regenerate": False, "prompt_template": ""},
        )

        from app import app

        async with app.test_request_context(
            "/api/playlist/regenerate-cover", method="POST", json={"name": "Metal - Top Tracks"}
        ):
            response = await pl.api_playlist_regenerate_cover()
        payload, status = (response if isinstance(response, tuple) else (response, 200))
        assert status == 400
        assert (await payload.get_json())["ok"] is False

    @pytest.mark.asyncio
    async def test_it_requires_a_name(self, monkeypatch):
        import routes.playlists as pl
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_gemini_config", lambda: {"enabled": True, "api_key": "k", "model": "m"})
        monkeypatch.setattr(
            ch, "get_genre_cover_config",
            lambda: {"enabled": True, "regenerate": False, "prompt_template": ""},
        )

        from app import app

        async with app.test_request_context(
            "/api/playlist/regenerate-cover", method="POST", json={}
        ):
            response = await pl.api_playlist_regenerate_cover()
        payload, status = (response if isinstance(response, tuple) else (response, 200))
        assert status == 400

    @pytest.mark.asyncio
    async def test_it_calls_the_generator_with_regenerate_true(self, monkeypatch):
        """⭐ The button must force a new image, not hit the hash skip."""
        import routes.playlists as pl
        import helpers.config_helpers as ch
        import routes.navidrome as nav_routes
        import services.playlists.genre_cover_service as gcs

        monkeypatch.setattr(ch, "get_gemini_config", lambda: {"enabled": True, "api_key": "k", "model": "m"})
        monkeypatch.setattr(
            ch, "get_genre_cover_config",
            lambda: {"enabled": True, "regenerate": False, "prompt_template": ""},
        )
        monkeypatch.setattr(nav_routes, "get_navidrome_client", lambda: object())

        captured: dict = {}

        def _fake_generate(genre, name, client, *, regenerate=False, playlist_id=None):
            captured.update(genre=genre, name=name, regenerate=regenerate, playlist_id=playlist_id)
            return {"ok": True, "status": "generated", "reason": "", "playlist_id": "pl-1"}

        monkeypatch.setattr(gcs, "generate_genre_cover", _fake_generate)

        from app import app

        async with app.test_request_context(
            "/api/playlist/regenerate-cover",
            method="POST",
            json={"name": "Metalcore - Top Tracks", "id": "pl-1"},
        ):
            response = await pl.api_playlist_regenerate_cover()
        payload, status = (response if isinstance(response, tuple) else (response, 200))
        assert status == 200
        assert (await payload.get_json())["ok"] is True
        assert captured["regenerate"] is True
        assert captured["playlist_id"] == "pl-1"

    def test_a_failed_generation_is_reported_not_swallowed(self):
        body = self._body()
        assert '"ok": False' in body
        assert "result.get(\"reason\")" in body


class TestTheGenreIsReadFromTheName:
    def _genre(self, name, template="{genre} - Top Tracks"):
        import routes.playlists as pl

        return pl._genre_from_playlist_name(name)

    def test_the_default_template_suffix_is_stripped(self):
        assert self._genre("Metalcore - Top Tracks") == "Metalcore"

    def test_a_name_without_the_suffix_is_used_whole(self):
        assert self._genre("My Cool Mix") == "My Cool Mix"

    def test_an_empty_name_gives_an_empty_genre(self):
        assert self._genre("") == ""
        assert self._genre(None) == ""

    def test_a_genre_containing_the_suffix_word_survives(self):
        """Only the TRAILING affix is removed."""
        assert self._genre("Top Tracks Metal - Top Tracks") == "Top Tracks Metal"


# ---------------------------------------------------------------------------
# 4. The templates — cover, button, mobile layout, tracks
# ---------------------------------------------------------------------------

class TestTheTemplateShowsTheCover:
    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_list_card_renders_a_cover_tile(self, path):
        js = _read(MAIN_JS if path == MAIN_TEMPLATE else TS_JS)
        assert "playlist-cover" in js, "the playlist card has no cover tile"
        assert "cover_art_url" in js, "the card does not read the cover URL"

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_detail_header_has_a_cover_element(self, path):
        html = _read(path)
        assert 'id="detailCover"' in html, "the detail header shows no cover"

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_cover_falls_back_to_an_icon_tile(self, path):
        """A playlist with no art must not render a broken image."""
        js = _read(MAIN_JS if path == MAIN_TEMPLATE else TS_JS)
        assert "playlistCoverFallback" in js, "no fallback for a missing cover"

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_detail_cover_is_alt_empty(self, path):
        """The name sits beside it — alt text would read it twice."""
        html = _read(path)
        idx = html.index('id="detailCover"')
        assert 'alt=""' in html[idx: idx + 120]


class TestTheTemplateHasTheRegenerateButton:
    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_button_exists_and_starts_hidden(self, path):
        html = _read(path)
        assert 'id="detailRegenerateBtn"' in html
        idx = html.index('id="detailRegenerateBtn"')
        window = html[max(0, idx - 260): idx + 60]
        assert "d-none" in window, (
            "the button must start hidden and be revealed only when the server "
            "says AI covers are configured"
        )

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_button_is_revealed_from_the_server_flag(self, path):
        js = _read(MAIN_JS if path == MAIN_TEMPLATE else TS_JS)
        assert "aiCoverAvailable" in js or "_aiCoverAvailable" in js, (
            "the button visibility is not driven by the server's gate"
        )

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_button_calls_the_regenerate_route(self, path):
        js = _read(MAIN_JS if path == MAIN_TEMPLATE else TS_JS)
        assert "/api/playlist/regenerate-cover" in js

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_button_is_disabled_while_generating(self, path):
        js = _read(MAIN_JS if path == MAIN_TEMPLATE else TS_JS)
        start = js.index("async function regenerateCover")
        body = js[start: start + 2200]
        assert "btn.disabled = true" in body, (
            "a double-click would spend Gemini quota twice"
        )

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_thumbnail_is_cache_busted_after_regeneration(self, path):
        js = _read(MAIN_JS if path == MAIN_TEMPLATE else TS_JS)
        start = js.index("async function regenerateCover")
        body = js[start: start + 2600]
        assert "cache_bust" in body, (
            "the URL is unchanged after regeneration, so the browser would "
            "keep serving the old image"
        )


class TestSelectingAPlaylistShowsItsTracks:
    """Pinned so the existing behaviour cannot regress under the redesign."""

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_the_detail_has_a_track_container(self, path):
        html = _read(path)
        assert 'id="detailTracks"' in html

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_selecting_fetches_the_tracks_endpoint(self, path):
        js = _read(MAIN_JS if path == MAIN_TEMPLATE else TS_JS)
        assert "/api/playlists/tracks" in js

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, TS_TEMPLATE])
    def test_mobile_gets_stacked_rows_not_a_scrolling_table(self, path):
        html = _read(path)
        assert 'id="detailStacked"' in html, "no mobile track layout"
        js = _read(MAIN_JS if path == MAIN_TEMPLATE else TS_JS)
        assert "compactView" in js or "matchMedia" in js


class TestTheCoverCssFitsBothTrees:
    @pytest.mark.parametrize(
        "css",
        [REPO_ROOT / "static" / "css" / "popularr.css",
         REPO_ROOT / "test_site" / "static" / "css" / "popularr.css"],
    )
    def test_the_cover_rules_exist(self, css):
        text = _read(css)
        assert ".playlist-cover" in text
        assert ".playlist-detail-cover" in text

    @pytest.mark.parametrize(
        "css",
        [REPO_ROOT / "static" / "css" / "popularr.css",
         REPO_ROOT / "test_site" / "static" / "css" / "popularr.css"],
    )
    def test_the_cover_is_a_fixed_square(self, css):
        """A fixed box stops a 300px source reflowing the row while it loads."""
        text = _read(css)
        block = text[text.index(".playlist-cover {"):]
        block = block[: block.index("}")]
        assert "width" in block and "height" in block and "object-fit" in block

    @pytest.mark.parametrize(
        "css",
        [REPO_ROOT / "static" / "css" / "popularr.css",
         REPO_ROOT / "test_site" / "static" / "css" / "popularr.css"],
    )
    def test_mobile_shrinks_the_detail_cover(self, css):
        text = _read(css)
        idx = text.index(".playlist-detail-cover {")
        after = text[idx:]
        assert "@media (max-width: 991.98px)" in after, (
            "the detail cover must shrink below lg or the actions get pushed off"
        )
