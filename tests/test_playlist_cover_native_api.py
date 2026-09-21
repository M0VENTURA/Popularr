"""Playlist cover art uses Navidrome's NATIVE artwork upload.

Reported: "Playlist artwork isn't being added properly, it's meant to be created
using the artist image for artist playlists."

Root cause: the cover was POSTed to the Subsonic ``updatePlaylist`` endpoint as a
``coverArt`` file — but that endpoint has **no cover parameter at all**. Its
parameters are exactly ``playlistId, name, comment, public, songIdToAdd,
songIndexToRemove`` (https://opensubsonic.netlify.app/docs/endpoints/updateplaylist/).
Navidrome ignores the unknown parameter, answers ``status: ok``, and the caller
reported success: the log said "Cover set for …" while no artwork was ever
stored. Navidrome only accepts a playlist image through its own web UI, which
uses its NATIVE API:

    POST {base}/api/playlist/{id}/image      multipart, field "image"

(``server/nativeapi/playlists.go`` → ``uploadPlaylistImage`` →
``handleImageUpload``, which reads ``r.FormFile("image")``, validates the image
by magic number, and requires ``EnableArtworkUpload`` for non-admin users.)
That source is also the FIRST one Navidrome's artwork docs list for playlists.

These tests pin the request shape, the honest failure reporting, and — the
discriminating check — that the Subsonic endpoint is not used for covers again.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


class _FakeSession:
    """Records every request and replays queued responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def _record(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0) if self._responses else _FakeResponse()

    def post(self, url, **kwargs):
        return self._record("post", url, **kwargs)

    def get(self, url, **kwargs):
        return self._record("get", url, **kwargs)


def _client(responses):
    from api_clients.navidrome import NavidromeClient

    session = _FakeSession(responses)
    client = NavidromeClient(
        "http://nav.local:4533", "user", "pass", http_session=session
    )
    return client, session


class TestUploadGoesToTheNativeEndpoint:
    def test_it_posts_multipart_to_the_playlist_image_endpoint(self):
        client, session = _client([
            _FakeResponse(200, {"token": "jwt-1"}),      # POST /auth/login
            _FakeResponse(200, {"status": "ok"}),        # POST .../image
        ])
        ok, reason = client.upload_playlist_cover("pl-1", b"\xff\xd8\xff\xe0art")
        assert (ok, reason) == (True, "")

        login, upload = session.calls
        assert login["url"] == "http://nav.local:4533/auth/login"
        assert login["json"] == {"username": "user", "password": "pass"}

        assert upload["method"] == "post"
        assert upload["url"] == "http://nav.local:4533/api/playlist/pl-1/image"
        assert upload["headers"]["Authorization"] == "Bearer jwt-1"
        # The handler reads ``r.FormFile("image")`` — the field name is load-bearing.
        assert "image" in upload["files"]
        assert upload["files"]["image"][1] == b"\xff\xd8\xff\xe0art"

    def test_the_login_token_is_cached_between_uploads(self):
        client, session = _client([
            _FakeResponse(200, {"token": "jwt-1"}),
            _FakeResponse(200, {"status": "ok"}),
            _FakeResponse(200, {"status": "ok"}),
        ])
        assert client.upload_playlist_cover("pl-1", b"art")[0] is True
        assert client.upload_playlist_cover("pl-2", b"art")[0] is True
        logins = [c for c in session.calls if c["url"].endswith("/auth/login")]
        assert len(logins) == 1, "a session token must be reused, not re-fetched per upload"

    def test_a_png_is_sent_with_a_png_filename(self):
        client, session = _client([
            _FakeResponse(200, {"token": "jwt-1"}),
            _FakeResponse(200, {"status": "ok"}),
        ])
        client.upload_playlist_cover("pl-1", b"\x89PNG", mime_type="image/png")
        assert session.calls[1]["files"]["image"][0].endswith(".png")

    def test_an_expired_token_is_refreshed_once(self):
        client, session = _client([
            _FakeResponse(200, {"token": "jwt-1"}),
            _FakeResponse(401),                           # token expired
            _FakeResponse(200, {"token": "jwt-2"}),       # re-login
            _FakeResponse(200, {"status": "ok"}),
        ])
        ok, reason = client.upload_playlist_cover("pl-1", b"art")
        assert (ok, reason) == (True, "")
        assert session.calls[-1]["headers"]["Authorization"] == "Bearer jwt-2"


class TestFailuresAreReportedHonestly:
    """The reported bug was a FALSE SUCCESS — these are the guard for it."""

    def test_artwork_upload_disabled_is_not_success(self):
        client, _ = _client([
            _FakeResponse(200, {"token": "jwt-1"}),
            _FakeResponse(403, text="artwork upload is disabled"),
        ])
        ok, reason = client.upload_playlist_cover("pl-1", b"art")
        assert ok is False
        assert reason == "disabled"

    def test_missing_playlist_is_not_success(self):
        client, _ = _client([
            _FakeResponse(200, {"token": "jwt-1"}),
            _FakeResponse(404),
        ])
        assert client.upload_playlist_cover("pl-1", b"art") == (False, "not_found")

    def test_a_failed_login_is_not_success(self):
        client, _ = _client([_FakeResponse(401)])
        assert client.upload_playlist_cover("pl-1", b"art") == (False, "no_login")

    def test_missing_inputs_are_not_success(self):
        client, _ = _client([])
        assert client.upload_playlist_cover("", b"art")[0] is False
        assert client.upload_playlist_cover("pl-1", b"")[0] is False


class TestTheSubsonicEndpointIsNotUsedForCovers:
    def test_no_coverart_file_to_updateplaylist(self):
        """The exact defect: a ``coverArt`` file posted to ``updatePlaylist``."""
        from api_clients import navidrome as nav

        source = inspect.getsource(nav.NavidromeClient.upload_playlist_cover)
        code_only = _strip_string_literals(source)
        assert "coverArt" not in code_only, (
            "updatePlaylist has no cover parameter — Navidrome ignores it and "
            "answers ok, which is why artwork never appeared"
        )
        assert "/rest/updatePlaylist" not in code_only
        assert "/api/playlist/" in source

    def test_the_native_endpoint_is_the_documented_one(self):
        from api_clients import navidrome as nav

        source = inspect.getsource(nav.NavidromeClient.upload_playlist_cover)
        assert "/image" in source
        assert "\"image\"" in source or "'image'" in source


def _strip_string_literals(source: str) -> str:
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.unparse(tree)


class TestAttachPlaylistCover:
    def test_a_successful_upload_is_reported_as_such(self, monkeypatch):
        from services.playlists import playlist_service as svc

        monkeypatch.setattr(svc, "_fetch_artist_image_bytes", lambda artist: b"artist-art")
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {
                "navidrome": {"playlist_cover_art": True, "base_url": "http://nav"},
            },
        )

        uploaded: list[tuple] = []

        class _Client:
            def __init__(self, *a, **k):
                pass

            def find_playlist_by_name(self, name):
                return {"id": "pl-1", "name": name}

            def upload_playlist_cover(self, pid, data, *a, **k):
                uploaded.append((pid, data))
                return True, ""

        monkeypatch.setattr("api_clients.navidrome.NavidromeClient", _Client)

        result = svc.attach_playlist_cover("Artist - Essential Collection", "Artist")
        assert result["ok"] is True
        assert result["method"] == "navidrome_native"
        assert uploaded == [("pl-1", b"artist-art")]

    def test_a_rejected_upload_is_reported_with_its_reason(self, monkeypatch):
        from services.playlists import playlist_service as svc

        monkeypatch.setattr(svc, "_fetch_artist_image_bytes", lambda artist: b"artist-art")
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {
                "navidrome": {"playlist_cover_art": True, "base_url": "http://nav"},
            },
        )

        class _Client:
            def __init__(self, *a, **k):
                pass

            def find_playlist_by_name(self, name):
                return {"id": "pl-1", "name": name}

            def upload_playlist_cover(self, *a, **k):
                return False, "disabled"

        monkeypatch.setattr("api_clients.navidrome.NavidromeClient", _Client)

        result = svc.attach_playlist_cover("Artist - Essential Collection", "Artist")
        assert result["ok"] is False
        assert "artwork upload disabled" in result["reason"], result

    def test_the_config_gate_still_short_circuits(self, monkeypatch):
        from services.playlists import playlist_service as svc

        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"navidrome": {"playlist_cover_art": False}},
        )
        monkeypatch.setattr(
            svc, "_fetch_artist_image_bytes",
            lambda artist: pytest.fail("must not fetch an image when disabled"),
        )
        assert svc.attach_playlist_cover("P", "A")["reason"] == "disabled"
