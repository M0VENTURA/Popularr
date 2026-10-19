"""The playlist cover fetch must never hand a relative path to an HTTP client.

REPORTED
--------
> An ``UnsupportedProtocol`` error triggered while attempting to fetch album art
> for Carpathian's *Nothing to Lose*. The request failed because the target URL
> was a relative path (``/api/album/Carpathian/Nothing%20to%20Lose/art``) missing
> the ``http://`` or ``https://`` prefix.

WHY IT HAPPENS
--------------
``artist_metadata_service.get_artist_image()`` has three resolution steps, and
the third one serves *this app's own* album-art route when the artist has no
image of its own:

```python
url = "/api/album/{artist}/{album}/art".format(...)
```

That is deliberately a ROOT-RELATIVE path: the artist page puts it in an
``<img src>``, where the browser resolves it against whatever origin the page was
loaded from. It is NOT a request target — with no scheme and no host there is
nothing to connect to, so ``httpx`` raises ``UnsupportedProtocol``.

⚠️ ``_is_valid_url`` in that module accepts ``"/"`` as a valid prefix, which is
correct for a UI value and is exactly why the shape reaches the backend. The two
consumers want different things from the same field, so the ABSOLUTE form is
built at the point of use (``playlist_service._app_local_base_url``) rather than
stored in the database: a stored absolute URL would be wrong for anyone reaching
the app by another name (LAN IP, reverse proxy, container hostname).

These tests drive the shipped resolver and assert on the URL actually handed to
the HTTP session — the one fact that was wrong.
"""
from __future__ import annotations

import pytest

from services.playlists import playlist_service as svc


class _Response:
    def __init__(self, status_code: int = 200, content: bytes = b"cover-bytes") -> None:
        self.status_code = status_code
        self.content = content


class _FakeSession:
    """Records every URL it is asked for; never leaves the process."""

    def __init__(self, response: _Response | None = None, raises: Exception | None = None):
        self.urls: list[str] = []
        self.timeouts: list[float | None] = []
        self._response = response or _Response()
        self._raises = raises

    def get(self, url, timeout=None):  # noqa: ANN001
        self.urls.append(url)
        self.timeouts.append(timeout)
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture
def artist_image(monkeypatch):
    """Point ``get_artist_image`` at a chosen ``image_url``."""
    from services.metadata import artist_metadata_service as ams

    def _set(image_url: str):
        monkeypatch.setattr(
            ams,
            "get_artist_image",
            lambda artist: ({"success": True, "image_url": image_url}, 200),
        )

    return _set


@pytest.fixture
def http_session(monkeypatch):
    """Swap ``api_clients.session`` for a recorder."""
    import api_clients

    def _install(response: _Response | None = None, raises: Exception | None = None):
        fake = _FakeSession(response=response, raises=raises)
        monkeypatch.setattr(api_clients, "session", fake, raising=False)
        return fake

    return _install


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("POPULARLR_APP_BASE_URL", raising=False)
    monkeypatch.delenv("SPTNR_GUNICORN_BIND", raising=False)
    # Default behaviour must not depend on where the tests run.
    monkeypatch.setattr(svc, "_album_art_bytes_for_artist", lambda artist: b"db-art", raising=False)
    yield


# ===========================================================================
# 1. The reported failure
# ===========================================================================
class TestARelativePathIsMadeAbsolute:
    def test_the_reported_carpathian_path_is_fetchable(self, artist_image, http_session):
        """The exact shape from the report, with the space still encoded."""
        artist_image("/api/album/Carpathian/Nothing%20to%20Lose/art")
        fake = http_session()

        svc._fetch_artist_image_bytes("Carpathian")

        assert len(fake.urls) == 1, "the image was never fetched"
        url = fake.urls[0]
        assert url.startswith(("http://", "https://")), (
            f"a relative path reached the HTTP client again: {url!r} — this is the "
            "UnsupportedProtocol failure"
        )
        assert url.endswith("/api/album/Carpathian/Nothing%20to%20Lose/art"), (
            f"the path was mangled while being made absolute: {url!r}"
        )

    def test_a_path_without_a_leading_slash_is_still_resolved(self, artist_image, http_session):
        """Defensive: the producer always emits a leading slash, but a stored
        value might not, and ``base + 'api/...'`` would drop the separator."""
        artist_image("api/album/A/B/art")
        fake = http_session()

        svc._fetch_artist_image_bytes("A")

        assert fake.urls[0].endswith("/api/album/A/B/art"), fake.urls[0]

    def test_the_fetched_bytes_are_returned(self, artist_image, http_session):
        artist_image("/api/album/A/B/art")
        http_session(_Response(200, b"real-cover"))

        assert svc._fetch_artist_image_bytes("A") == b"real-cover"


# ===========================================================================
# 2. How the base URL is chosen
# ===========================================================================
class TestTheBaseUrl:
    def test_the_wildcard_bind_address_is_not_used_as_a_destination(
        self, artist_image, http_session, monkeypatch
    ):
        """``0.0.0.0`` is a LISTEN address. Dialling it fails, which would have
        swapped one broken URL for another."""
        monkeypatch.setenv("SPTNR_GUNICORN_BIND", "0.0.0.0:5000")
        artist_image("/api/album/A/B/art")
        fake = http_session()

        svc._fetch_artist_image_bytes("A")

        assert "0.0.0.0" not in fake.urls[0], fake.urls[0]
        assert fake.urls[0].startswith("http://127.0.0.1:5000/"), fake.urls[0]

    def test_the_configured_port_is_honoured(self, artist_image, http_session, monkeypatch):
        monkeypatch.setenv("SPTNR_GUNICORN_BIND", "0.0.0.0:8080")
        artist_image("/api/album/A/B/art")
        fake = http_session()

        svc._fetch_artist_image_bytes("A")

        assert fake.urls[0].startswith("http://127.0.0.1:8080/"), fake.urls[0]

    def test_an_explicit_base_url_override_wins(self, artist_image, http_session, monkeypatch):
        """A reverse-proxy deployment points this at the public origin."""
        monkeypatch.setenv("POPULARLR_APP_BASE_URL", "https://scanner.ungoogle.au/")
        monkeypatch.setenv("SPTNR_GUNICORN_BIND", "0.0.0.0:5000")
        artist_image("/api/album/A/B/art")
        fake = http_session()

        svc._fetch_artist_image_bytes("A")

        assert fake.urls[0] == "https://scanner.ungoogle.au/api/album/A/B/art", fake.urls[0]

    def test_the_default_matches_the_documented_bind(self, artist_image, http_session):
        artist_image("/api/album/A/B/art")
        fake = http_session()

        svc._fetch_artist_image_bytes("A")

        assert fake.urls[0].startswith("http://127.0.0.1:5000/"), fake.urls[0]

    def test_a_nonsense_bind_falls_back_to_the_default(self, artist_image, http_session, monkeypatch):
        monkeypatch.setenv("SPTNR_GUNICORN_BIND", "not-a-bind")
        artist_image("/api/album/A/B/art")
        fake = http_session()

        svc._fetch_artist_image_bytes("A")

        assert fake.urls[0].startswith("http://127.0.0.1:5000/"), fake.urls[0]


# ===========================================================================
# 3. Absolute URLs are untouched
# ===========================================================================
class TestAnAbsoluteUrlIsPassedThrough:
    @pytest.mark.parametrize(
        "url",
        [
            "https://coverartarchive.org/artist/abc/front-500",
            "http://192.168.1.200:4533/rest/getCoverArt?id=123",
        ],
    )
    def test_it_is_not_rewritten(self, artist_image, http_session, url: str):
        """CONTROL: prefixing unconditionally would corrupt real provider URLs."""
        artist_image(url)
        fake = http_session()

        svc._fetch_artist_image_bytes("A")

        assert fake.urls == [url], f"an absolute URL was rewritten: {fake.urls}"

    def test_scheme_matching_is_case_insensitive(self, artist_image, http_session):
        artist_image("HTTPS://coverartarchive.org/x.jpg")
        fake = http_session()

        svc._fetch_artist_image_bytes("A")

        assert fake.urls == ["HTTPS://coverartarchive.org/x.jpg"], fake.urls


# ===========================================================================
# 4. Failure still yields a cover
# ===========================================================================
class TestItDegradesToTheStoredArt:
    def test_a_failed_fetch_falls_back_to_the_database(self, artist_image, http_session):
        """An unreachable loopback must not cost the cover."""
        artist_image("/api/album/A/B/art")
        http_session(raises=RuntimeError("connection refused"))

        assert svc._fetch_artist_image_bytes("A") == b"db-art"

    def test_a_response_with_no_content_falls_back_too(self, artist_image, http_session):
        artist_image("/api/album/A/B/art")
        http_session(_Response(200, b""))

        assert svc._fetch_artist_image_bytes("A") == b"db-art"

    def test_no_resolved_url_means_no_cover(self, artist_image, http_session, monkeypatch):
        """Never invent an image: the caller reports 'no image' and the gate
        stops there."""
        artist_image("")
        fake = http_session()
        monkeypatch.setattr(svc, "_album_art_bytes_for_artist", lambda artist: b"db-art")

        assert svc._fetch_artist_image_bytes("A") is None
        assert fake.urls == [], "nothing should be fetched when no URL resolved"
